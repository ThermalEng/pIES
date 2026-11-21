"""身份与认证服务: 用户、凭证、会话与认证审计。

依据 架构宪法 §16 安全与审计 + 领域模型 §身份、权限和审计。
本模块是身份域的唯一写入单元:

- users / credentials / window_sessions / auth_events 的写入与状态迁移均在此完成;
- 密码只存 bcrypt 哈希, 会话令牌只存 sha256 摘要(库内无明文令牌);
- 同一账号可创建多个独立会话。
- 凭证变更(改密/重置)递增 users.credential_version, 使全部旧会话失效(宪法 §16 + 领域模型 §身份、权限和审计);
- 业务错误统一抛 AppError(+ 诊断 message_key, 前缀 ies.diag.auth.*),
  响应不泄露堆栈/哈希/明文。
- 输入规则、身份错误与身份状态归 identity 域所有(经 identity 门面复用);
  本模块负责身份用例以及审计、项目等跨域编排与事务。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from iesplan import audit as audit_domain
from iesplan import identity as identity_domain
from iesplan import project as project_domain
from iesplan.config import settings
from iesplan.core.errors import ConflictError, ForbiddenError
from iesplan.core.security import (
    hash_password,
    new_session_token,
    token_hash,
    verify_password,
)
from iesplan.identity.contracts import (
    BadOldPasswordError,
    BadRequestError,
    CredentialRecord,
    SamePasswordError,
    UserRecord,
    WeakPasswordError,
    WindowSessionRecord,
)
#: 用户名/邮箱格式唯一权威: iesplan.identity.contracts(应用层不得导入
#: iesplan.models.*, 改接领域公开契约; 用户输入边界校验行为不变)。
from iesplan.identity.contracts import EMAIL_RE as EMAIL_RE
from iesplan.identity.contracts import USERNAME_RE as USERNAME_RE

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# 时间与序列化辅助
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    """当前 UTC 时间(所有时间列统一 UTC 存储)。"""
    return datetime.now(UTC)


def as_utc(dt: datetime | str | None) -> datetime | None:
    """将可能为 naive 的 datetime 按 UTC 解释(SQLite 测试环境回读为 naive)。

    域记录以 ISO 字符串携带时间, 此处一并接受(认证上下文直接消费会话记录)。
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# 应用级设置(键值, 多 Worker 权威来源, M-12)
# ---------------------------------------------------------------------------

#: 设置键: 自助注册开关(默认关闭)
KEY_REGISTRATION_ENABLED = "registration_enabled"


def get_app_setting(db: Session, key: str, default: Any = None) -> Any:
    """读取应用级设置(未设置返回 default)。"""
    record = identity_domain.get_app_setting(db, key)
    if record is None:
        return default
    return record.value.get("value", default)


def set_app_setting(db: Session, key: str, value: Any, updated_by: int | None = None) -> None:
    """写入应用级设置(upsert), 全部 Worker 从数据库读取同一值。"""
    identity_domain.set_app_setting(db, key, value, updated_by)


def registration_enabled(db: Session) -> bool:
    """自助注册开关(数据库权威值, 默认关闭)。"""
    return bool(get_app_setting(db, KEY_REGISTRATION_ENABLED, False))


def set_registration_enabled(db: Session, value: bool, updated_by: int | None = None) -> None:
    """切换自助注册开关(持久化, 多 Worker 一致)。"""
    set_app_setting(db, KEY_REGISTRATION_ENABLED, bool(value), updated_by=updated_by)


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


def record_auth_event(
    db: Session,
    event_type: str,
    *,
    user_id: int | None = None,
    session_id: int | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    detail: dict | None = None,
) -> None:
    """写认证审计事件(auth_events, 不可变表, 仅 INSERT)。

    参数:
        event_type: 枚举值, 见 auth_events 表 CHECK 约束(如 login_success/
            login_failure/logout/password_change/credential_reset/...)。
        user_id: 关联用户; 登录失败且用户不存在时为 None。
        session_id: 关联窗口会话; 可为 None。
        detail: 事件详情(JSON 可序列化), 如失败原因、变更前后快照。
    """
    identity_domain.record_auth_event(
        db,
        event_type=event_type,
        user_id=user_id,
        session_id=session_id,
        ip=identity_domain.clean_ip(ip),
        user_agent=user_agent,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# 角色与用户查询
# ---------------------------------------------------------------------------


def ensure_role(db: Session, code: str, name: str):
    """确保全局角色存在(幂等), 返回角色记录。

    参数:
        code: 角色编码(如 admin / engineer)。
        name: 角色显示名(仅首次创建时使用)。
    """
    return identity_domain.ensure_role(db, code, name)


def seed_builtin_admin(db: Session, password: str | None = None) -> None:
    """幂等创建内置管理员(admin, 首登强制改密)。

    种子身份数据的唯一归属(原 iesplan.db.seed_admin 整体迁移至此;
    db.py 只负责建表/迁移/触发器, 不再保留业务种子逻辑):
    - 已有 admin 角色有效授权则直接返回;
    - 否则补齐 admin 系统角色, 建 admin 用户(创建时分配公开命名空间)
      + password 凭证(requires_change=True), 初始密码取参数, 缺省取配置默认值,
      强度不足只记低分不断言(沿用旧种子语义);
    - 管理员自授权(授权人即本人), 完成后提交(调用方拥有事务时由调用方提交
      亦可, 此处提交与 create_user 一致)。
    """
    users = identity_domain.list_users(db)
    if users:
        roles = identity_domain.roles_by_user(db, [u.id for u in users])
        if any(identity_domain.ROLE_ADMIN in r for r in roles.values()):
            return
    role = ensure_role(db, identity_domain.ROLE_ADMIN, "管理员")
    pwd = password or settings.default_admin_password
    ok, _ = identity_domain.validate_new_password(pwd)
    user = identity_domain.create_user(
        db,
        username="admin",
        display_name="管理员",
        public_namespace=identity_domain.allocate_public_namespace(db),
    )
    identity_domain.add_credential(
        db,
        user_id=user.id,
        credential_type="password",
        secret_hash=hash_password(pwd),
        algorithm="bcrypt",
        strength_score=100 if ok else 0,
        requires_change=True,
    )
    identity_domain.grant_role(db, user_id=user.id, role_id=role.id, granted_by=user.id)
    db.commit()


def user_roles(db: Session, user: UserRecord) -> list[str]:
    """返回用户当前(未撤销)的角色编码列表, 按角色 id 升序。"""
    return identity_domain.user_roles(db, user.id)


def has_role(db: Session, user: UserRecord, code: str) -> bool:
    """用户是否拥有指定角色(当前有效授权)。"""
    return code in user_roles(db, user)


def list_users(db: Session) -> list[UserRecord]:
    """全部用户(含停用), 按 id 升序。"""
    return identity_domain.list_users(db)


def get_user_by_id(db: Session, user_id: int) -> UserRecord | None:
    """按主键取用户(含停用), 不存在返回 None。"""
    return identity_domain.get_user(db, user_id)


def get_user_by_username(db: Session, username: str) -> UserRecord | None:
    """按用户名(强制小写)取用户。"""
    return identity_domain.get_user_by_username(db, username)


def get_active_password_credential(db: Session, user: UserRecord) -> CredentialRecord | None:
    """返回用户当前有效的 password 凭证(至多一条), 无则返回 None。"""
    return identity_domain.get_active_credential(db, user.id)


# ---------------------------------------------------------------------------
# 用户生命周期
# ---------------------------------------------------------------------------


def create_user(
    db: Session,
    username: str,
    password: str,
    role: str = identity_domain.ROLE_ENGINEER,
    force_password_change: bool = True,
    *,
    display_name: str | None = None,
    email: str | None = None,
    created_by: int | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> UserRecord:
    """创建用户 + 密码凭证 + 角色授权(角色表幂等补齐), 并写认证审计。

    参数:
        username: 登录名(自动去空格并转小写, 须匹配 ^[a-z0-9_]{3,32}$)。
        password: 明文密码(bcrypt 哈希入库, 须满足强度规则)。
        role: 内置角色编码, 仅允许 admin / engineer(宪法 §16 + 领域模型 §身份、权限和审计)。
        force_password_change: 是否要求首次登录后强制改密(种子/重置场景为 True)。
        created_by: 创建者用户 id; 自助注册为 None, 此时自授权。
    返回:
        新用户(已提交)。
    """
    # 输入规则归 identity 域(用户名/邮箱/密码); 本用例只做跨域编排与事务
    username = identity_domain.validate_username(username)
    email = identity_domain.validate_email(email)
    ok, reason = identity_domain.validate_new_password(password)
    if not ok:
        raise WeakPasswordError(params={"reason": reason})
    if role not in (identity_domain.ROLE_ADMIN, identity_domain.ROLE_ENGINEER):
        raise BadRequestError(
            "",
            code="AUTH-USER-006",
            message_key="ies.diag.auth.role_invalid",
            params={"role": role},
        )
    if get_user_by_username(db, username) is not None:
        raise ConflictError(
            "",
            code="AUTH-USER-002",
            message_key="ies.diag.auth.username_taken",
            params={"username": username},
        )
    if email:
        dup_email = identity_domain.get_user_by_email(db, email)
        if dup_email is not None:
            raise ConflictError(
                "",
                code="AUTH-USER-005",
                message_key="ies.diag.auth.email_taken",
                params={"email": email},
            )
    role_row = ensure_role(
        db, role, name="工程师" if role == identity_domain.ROLE_ENGINEER else "管理员"
    )

    # 分配公开命名空间（身份域唯一实现：CSPRNG，60 bit 熵；全局唯一，碰撞重试）
    ns = identity_domain.allocate_public_namespace(db)
    user = identity_domain.create_user(
        db,
        username=username,
        display_name=(display_name or "").strip() or username,
        email=email,
        public_namespace=ns,
    )
    identity_domain.add_credential(
        db,
        user_id=user.id,
        credential_type="password",
        secret_hash=hash_password(password),
        algorithm="bcrypt",
        strength_score=100 if ok else 0,
        requires_change=force_password_change,
        created_by=created_by,
    )
    # 追加式授权: 授权人缺省为本人(自注册), 否则为操作管理员
    identity_domain.grant_role(db, user_id=user.id, role_id=role_row.id, granted_by=created_by or user.id)
    record_auth_event(
        db,
        "role_change",
        user_id=user.id,
        ip=ip,
        user_agent=user_agent,
        detail={"action": "grant", "role": role, "granted_by": created_by},
    )
    db.commit()
    return user


def deactivate_user(
    db: Session,
    admin: UserRecord,
    user: UserRecord,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """停用账号(管理员): 状态置 disabled, 立即撤销全部会话并写审计。

    约束: 不能停用自己(避免管理员自锁), 不能停用系统账号。

    注: 状态幂等判定必须读取当前行(传入的 UserRecord 是不可变快照,
    调用方持有旧快照时不能作为判定依据)。
    """
    if user.id == admin.id:
        raise ForbiddenError("", params={"reason": "cannot_deactivate_self"})
    if user.is_system:
        raise ForbiddenError("", params={"reason": "system_account"})
    current = identity_domain.get_user(db, user.id)
    if current is not None and current.status == identity_domain.USER_STATUS_DISABLED:
        return
    identity_domain.set_user_status(db, user.id, identity_domain.USER_STATUS_DISABLED)
    identity_domain.bump_credential_version(db, user.id)
    revoke_all_user_sessions(db, user, revoked_by=admin.id)
    record_auth_event(
        db,
        "account_disabled",
        user_id=user.id,
        ip=ip,
        user_agent=user_agent,
        detail={"disabled_by": admin.id},
    )
    db.commit()


def reactivate_user(
    db: Session,
    admin: UserRecord,
    user: UserRecord,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """重新启用账号(管理员): 状态置 active 并写权限变更审计。

    注: 幂等判定读取当前行(理由同 deactivate_user, 快照不可作为判定依据)。
    """
    current = identity_domain.get_user(db, user.id)
    if current is not None and current.status == identity_domain.USER_STATUS_ACTIVE:
        return
    identity_domain.set_user_status(db, user.id, identity_domain.USER_STATUS_ACTIVE)
    record_auth_event(
        db,
        "permission_change",
        user_id=user.id,
        ip=ip,
        user_agent=user_agent,
        detail={"action": "user_reactivated", "by": admin.id},
    )
    db.commit()


# ---------------------------------------------------------------------------
# 删除账号确认/预告(0.2.0 B1: 误操作防护)
# ---------------------------------------------------------------------------


def _owned_projects(db: Session, user_id: int) -> list:
    """该用户拥有、且尚未删除(软删)的项目记录(经 project 域 repository，分页取全)。"""
    owned = []
    cursor = None
    while True:
        page = project_domain.list_projects(
            db, owner_id=user_id, statuses=["active", "archived"], limit=500, cursor=cursor
        )
        owned.extend(page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    return owned


def preview_user_delete(
    db: Session,
    admin: UserRecord,
    user: UserRecord,
) -> dict:
    """返回该账号拥有的项目清单，供管理员确认删除范围。"""
    if user.id == admin.id:
        raise ForbiddenError("", params={"reason": "cannot_delete_self"})
    if user.is_system:
        raise ForbiddenError("", params={"reason": "system_account"})
    owned = sorted(_owned_projects(db, user.id), key=lambda p: p.id)
    return {
        "user_id": user.id,
        "username": user.username,
        "project_count": len(owned),
        "projects": [{"id": p.id, "name": p.name, "status": p.status} for p in owned],
    }


def delete_user(
    db: Session,
    admin: UserRecord,
    user: UserRecord,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> dict:
    """停用账号、撤销凭证，并删除该账号拥有的项目。"""
    if user.id == admin.id:
        raise ForbiddenError("", params={"reason": "cannot_delete_self"})
    if user.is_system:
        raise ForbiddenError("", params={"reason": "system_account"})
    # 该用户拥有的项目 → 软删(级联，状态写入经 project 域 repository)
    owned = _owned_projects(db, user.id)
    deleted_projects = 0
    for project in owned:
        project_domain.set_project_status(db, project.id, "deleted")
        deleted_projects += 1
        # 级联软删逐项目写审计(经 audit 域公开门面; 项目状态写入经
        # project 域公开门面, 本模块不直调任何 services 实现)。
        audit_domain.append_entry(
            db,
            actor_id=admin.id,
            action="project.deleted_by_account",
            entity_type="project",
            entity_id=project.id,
            actor_type="user",
            before=None,
            extra={"reason": "account_deleted", "account_id": user.id},
        )
    identity_domain.set_user_status(db, user.id, identity_domain.USER_STATUS_DISABLED)
    identity_domain.bump_credential_version(db, user.id)
    revoke_all_user_sessions(db, user, revoked_by=admin.id)
    identity_domain.revoke_credentials(db, user.id)
    record_auth_event(
        db,
        "account_disabled",
        user_id=user.id,
        ip=ip,
        user_agent=user_agent,
        detail={"action": "account_deleted", "deleted_by": admin.id, "deleted_projects": deleted_projects},
    )
    db.commit()
    return {"deleted_projects": deleted_projects}


# ---------------------------------------------------------------------------
# 凭证(不可变: 变更 = 撤销旧行 + 插入新行 + 递增 credential_version)
# ---------------------------------------------------------------------------


def change_password(
    db: Session,
    user: UserRecord,
    old_password: str,
    new_password: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """修改密码: 校验旧密码/强度, 递增凭证版本使全部旧会话失效, 写审计。

    - 首登强制改密状态(credential.requires_change)在校验通过后清除;
    - 凭证版本递增后, 旧窗口会话全部失效, 前端须重新登录(凭证失效机制, 宪法 §16 + 领域模型 §身份、权限和审计)。
    """
    cred = get_active_password_credential(db, user)
    secret = identity_domain.get_active_password_secret(db, user.id)
    if cred is None or secret is None or not verify_password(old_password, secret):
        raise BadOldPasswordError()
    if old_password == new_password:
        raise SamePasswordError()
    ok, reason = identity_domain.validate_new_password(new_password)
    if not ok:
        raise WeakPasswordError(params={"reason": reason})
    was_force_change = cred.requires_change
    now = utcnow()
    identity_domain.revoke_credentials(db, user.id)
    identity_domain.add_credential(
        db,
        user_id=user.id,
        credential_type="password",
        secret_hash=hash_password(new_password),
        algorithm="bcrypt",
        strength_score=100 if ok else 0,
        requires_change=False,
        created_by=user.id,
        rotated_at=now.isoformat(),
    )
    updated = identity_domain.bump_credential_version(db, user.id)
    revoke_all_user_sessions(db, user, revoked_by=user.id)
    record_auth_event(
        db,
        "password_change",
        user_id=user.id,
        ip=ip,
        user_agent=user_agent,
        detail={"was_force_change": was_force_change, "credential_version": updated.credential_version},
    )
    db.commit()


def reset_password(
    db: Session,
    admin: UserRecord,
    target_user: UserRecord,
    new_tmp: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """管理员重置密码: 签发临时密码(requires_change=True), 全部旧会话失效, 写审计。"""
    ok, reason = identity_domain.validate_new_password(new_tmp)
    if not ok:
        raise WeakPasswordError(params={"reason": reason})
    now = utcnow()
    identity_domain.revoke_credentials(db, target_user.id)
    identity_domain.add_credential(
        db,
        user_id=target_user.id,
        credential_type="password",
        secret_hash=hash_password(new_tmp),
        algorithm="bcrypt",
        strength_score=100 if ok else 0,
        requires_change=True,
        created_by=admin.id,
        rotated_at=now.isoformat(),
    )
    updated = identity_domain.bump_credential_version(db, target_user.id)
    revoke_all_user_sessions(db, target_user, revoked_by=admin.id)
    record_auth_event(
        db,
        "credential_reset",
        user_id=target_user.id,
        ip=ip,
        user_agent=user_agent,
        detail={"reset_by": admin.id, "credential_version": updated.credential_version},
    )
    db.commit()


# ---------------------------------------------------------------------------
# 登录认证
# ---------------------------------------------------------------------------


def authenticate(
    db: Session,
    username: str,
    password: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
    device: str | None = None,
) -> UserRecord | None:
    """验证登录凭证并记录认证结果。"""
    username = (username or "").strip().lower()
    user = get_user_by_username(db, username)
    if user is None:
        record_auth_event(
            db,
            "login_failure",
            ip=ip,
            user_agent=user_agent,
            detail={"reason": "user_not_found", "username": username},
        )
        db.commit()
        return None
    block_reason = identity_domain.login_block_reason(user)
    if block_reason is not None:
        record_auth_event(
            db,
            "login_failure",
            user_id=user.id,
            ip=ip,
            user_agent=user_agent,
            detail={"reason": block_reason},
        )
        db.commit()
        return None
    cred = get_active_password_credential(db, user)
    secret = identity_domain.get_active_password_secret(db, user.id)
    if cred is None or secret is None:
        record_auth_event(
            db,
            "login_failure",
            user_id=user.id,
            ip=ip,
            user_agent=user_agent,
            detail={"reason": "no_credential"},
        )
        db.commit()
        return None
    if not verify_password(password, secret):
        record_auth_event(
            db,
            "login_failure",
            user_id=user.id,
            ip=ip,
            user_agent=user_agent,
            detail={"reason": "bad_password"},
        )
        db.commit()
        return None
    user = identity_domain.touch_login(db, user.id)
    record_auth_event(
        db,
        "login_success",
        user_id=user.id,
        ip=ip,
        user_agent=user_agent,
        detail={"device": device} if device else None,
    )
    db.commit()
    return user


# ---------------------------------------------------------------------------
# 会话( 宪法 §16 + 领域模型 §身份、权限和审计)
# ---------------------------------------------------------------------------


def create_window_session(db: Session, user: UserRecord) -> tuple[WindowSessionRecord, str]:
    """Create an independent session and persist only the token hash."""
    identity_domain.expire_sessions(db, user.id)
    token = new_session_token()
    session = identity_domain.create_session(
        db,
        user_id=user.id,
        token_hash=token_hash(token),
        credential_version_at_issue=user.credential_version,
        expires_at=identity_domain.new_session_expiry(utcnow()).isoformat(),
    )
    db.commit()
    return session, token


def get_session_by_token(db: Session, token: str) -> WindowSessionRecord | None:
    """按窗口凭证(原文)查找会话(令牌以 sha256 摘要存储)。"""
    return identity_domain.get_session_by_token_hash(db, token_hash(token))


def revoke_session(
    db: Session,
    user: UserRecord,
    session: WindowSessionRecord,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
    reason: str = "logout",
) -> None:
    """撤销单个会话(登出场景), 写 logout / session_revoke 审计。"""
    identity_domain.set_session_status(
        db, session.id, identity_domain.SESSION_STATUS_REVOKED, revoked_by=user.id
    )
    record_auth_event(
        db,
        "logout" if reason == "logout" else "session_revoke",
        user_id=user.id,
        session_id=session.id,
        ip=ip,
        user_agent=user_agent,
        detail={"reason": reason},
    )
    db.commit()


def revoke_all_user_sessions(
    db: Session,
    user: UserRecord,
    *,
    revoked_by: int | None = None,
) -> int:
    """撤销用户全部活动会话(凭证变更/停用时调用), 返回撤销数量。"""
    rows = identity_domain.list_active_sessions(db, user.id)
    for s in rows:
        identity_domain.set_session_status(
            db, s.id, identity_domain.SESSION_STATUS_REVOKED, revoked_by=revoked_by
        )
    if rows:
        record_auth_event(
            db,
            "session_revoke",
            user_id=user.id,
            detail={"revoked_session_ids": [s.id for s in rows], "reason": "all_revoked"},
        )
        db.commit()
    return len(rows)


def expire_sessions(db: Session, user_id: int | None = None) -> int:
    """清理过期会话(状态置 expired, 系统自动过期 revoked_by 为空), 返回数量。"""
    count = identity_domain.expire_sessions(db, user_id)
    if count:
        db.commit()
    return count


def extend_session(db: Session, session: WindowSessionRecord) -> datetime:
    """会话续期: 更新最后活跃时间并按 TTL 顺延过期时刻, 返回新的过期时刻。"""
    now = utcnow()
    new_expires_at = identity_domain.new_session_expiry(now)
    identity_domain.extend_session(db, session.id, expires_at=new_expires_at.isoformat())
    db.commit()
    return new_expires_at


def expire_session(db: Session, session_id: int) -> None:
    """将会话置为 expired(过期访问时系统自动过期), 并提交。"""
    identity_domain.set_session_status(db, session_id, identity_domain.SESSION_STATUS_EXPIRED)
    db.commit()


def revoke_session_after_credential_change(db: Session, session_id: int, user_id: int) -> None:
    """凭证已轮换(改密/重置): 撤销旧会话(无审计事件, 调用方随后抛 401), 并提交。"""
    identity_domain.set_session_status(
        db, session_id, identity_domain.SESSION_STATUS_REVOKED, revoked_by=user_id
    )
    db.commit()


def touch_session(db: Session, session_id: int) -> None:
    """刷新会话最后活跃时间(认证通过后的顺带更新), 并提交。"""
    identity_domain.heartbeat_session(db, session_id)
    db.commit()
