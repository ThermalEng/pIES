"""身份与认证服务(U01): 用户、凭证、窗口会话、登录限速与认证审计。

对应 RPD 第 3 节(用户/权限/会话)与 01-db-schema.md 第 1 节(身份)。
本模块是身份域的唯一写入单元:

- users / credentials / window_sessions / auth_events 的写入与状态迁移均在此完成;
- 密码只存 bcrypt 哈希, 会话令牌只存 sha256 摘要(库内无明文令牌);
- 登录限速: 同一用户名 5 次失败锁定 15 分钟(进程内存状态);
- 单活动窗口(RPD 3.3): 新登录使旧 active 会话撤销, 新会话以 takeover_pending
  创建, 确认接管后当前会话保留为 active(不轮换凭证);
- 凭证变更(改密/重置)递增 users.credential_version, 使全部旧会话失效;
- 业务错误统一抛 AppError(+ 诊断 message_key, 前缀 ies.diag.auth.*),
  响应不泄露堆栈/哈希/明文。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from iesplan.config import settings
from iesplan.core.errors import AppError, ConflictError, ForbiddenError
from iesplan.core.security import (
    check_password_strength,
    hash_password,
    new_session_token,
    token_hash,
    verify_password,
)
from iesplan.models.common import EMAIL_RE, USERNAME_RE
from iesplan.models.identity import AppSetting, AuthEvent, Credential, Role, User, UserRole, WindowSession
from iesplan.models.project import Project

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 登录限速: 同一用户名最大连续失败次数
MAX_LOGIN_FAILURES: Final[int] = 5
#: 锁定时长(秒): 达到失败上限后锁定 15 分钟
LOCKOUT_SECONDS: Final[int] = 15 * 60
#: 内置角色(RPD 3.1: 管理员、工程师)
ROLE_ADMIN: Final[str] = "admin"
ROLE_ENGINEER: Final[str] = "engineer"
#: 会话活动状态集合(非终态)
_ACTIVE_STATUSES: Final[tuple[str, str]] = ("active", "takeover_pending")
#: 假哈希: 用户不存在/停用/无凭证时也执行一次 bcrypt 校验,
#: 使各种失败路径耗时均匀, 避免通过响应时间枚举用户名/账号状态
_DUMMY_PASSWORD_HASH: Final[str] = (
    "$2b$12$P5GwAaopJcdx8Bx7CEUWOeNFfS/4KQ6wvr321HDFA.oQakKY.W9v."
)


# ---------------------------------------------------------------------------
# 认证业务异常(message_key 前缀 ies.diag.auth.*; http_status 供全局处理器映射)
# ---------------------------------------------------------------------------


class AuthError(AppError):
    """认证/授权错误基类: 默认 401。"""

    http_status = 401
    code = "AUTH-REQ-001"
    message_key = "ies.diag.auth.required"


class AuthRequiredError(AuthError):
    """缺少窗口凭证。"""

    code = "AUTH-REQ-001"
    message_key = "ies.diag.auth.required"


class SessionInvalidError(AuthError):
    """窗口凭证无效(未找到/过期/已撤销/待接管/凭证版本不匹配)。"""

    code = "AUTH-SESS-001"
    message_key = "ies.diag.auth.session_invalid"


class LoginFailedError(AuthError):
    """登录失败(统一文案: 不区分用户不存在/密码错误/账号停用)。"""

    code = "AUTH-LOGIN-001"
    message_key = "ies.diag.auth.login_failed"


class LockedError(AuthError):
    """登录限速锁定(429)。"""

    http_status = 429
    code = "AUTH-LOCK-001"
    message_key = "ies.diag.auth.locked"


class UserDisabledError(AuthError):
    """账号停用(403)。"""

    http_status = 403
    code = "AUTH-USER-001"
    message_key = "ies.diag.auth.user_disabled"


class WeakPasswordError(AuthError):
    """新密码强度不足(400)。"""

    http_status = 400
    code = "AUTH-PWD-002"
    message_key = "ies.diag.auth.weak_password"


class BadOldPasswordError(AuthError):
    """旧密码不正确(400)。"""

    http_status = 400
    code = "AUTH-PWD-001"
    message_key = "ies.diag.auth.bad_old_password"


class SamePasswordError(AuthError):
    """新旧密码相同(400)。"""

    http_status = 400
    code = "AUTH-PWD-003"
    message_key = "ies.diag.auth.same_password"


class RegistrationDisabledError(AuthError):
    """自助注册未开启(403)。"""

    http_status = 403
    code = "AUTH-REG-001"
    message_key = "ies.diag.auth.registration_disabled"


class ForcePasswordChangeError(AuthError):
    """强制改密门禁(403): 有效密码凭证 requires_change=True 时,
    除改密/登出/本人信息外的全部业务请求被拒(C-02, AUTH-FPC-001)。"""

    http_status = 403
    code = "AUTH-FPC-001"
    message_key = "ies.diag.auth.force_password_change"


class BadRequestError(AuthError):
    """请求参数非法(400)。"""

    http_status = 400
    code = "AUTH-BAD-001"
    message_key = "ies.diag.auth.bad_request"


# ---------------------------------------------------------------------------
# 登录限速(进程内存: username -> 失败时间戳 / 锁定截止时刻)
#
# 局限说明(H-03): 内存限速为单进程状态 —— 多 Uvicorn Worker 下失败计数被
# 分散到各进程, 进程重启后锁定状态丢失。生产多 Worker 部署应使用 Redis 原子
# 计数(见下方 _rate_redis); Redis 不可用(依赖缺失/连接失败/运行期错误)时
# 自动降级为内存限速并记 warning 日志, 不阻断登录功能。
# ---------------------------------------------------------------------------

try:
    import redis as _redis_module

    _REDIS_IMPORT_OK = True
except Exception:  # pragma: no cover - 环境缺 redis 依赖时降级内存限速
    _redis_module = None  # type: ignore[assignment]
    _REDIS_IMPORT_OK = False

#: Redis 登录限速键前缀
_RATE_KEY_PREFIX = "iesplan:ratelimit:login"
#: 惰性初始化的 Redis 客户端(单例; 连接失败置 None 后不再重试, 保持内存降级)
_rate_redis_client: Any = None


def _rate_redis() -> Any | None:
    """尝试获取 Redis 客户端用于跨 Worker 限速; 不可用返回 None(降级内存)。

    IESPLAN_QUEUE=memory(测试/单机模式)时直接跳过 Redis, 保持进程内限速,
    避免测试环境共享 Redis 键造成跨测试/跨进程状态污染;
    超时/连接失败/运行期错误一律捕获, 由调用方回退内存限速。
    """
    global _rate_redis_client
    if os.environ.get("IESPLAN_QUEUE", "auto").lower() == "memory":
        return None
    if _rate_redis_client is not None:
        return _rate_redis_client
    if not _REDIS_IMPORT_OK:
        return None
    try:
        client = _redis_module.Redis.from_url(
            settings.redis_url, decode_responses=True,
            socket_connect_timeout=1.0, socket_timeout=2.0,
        )
        client.ping()  # 探测连接, 失败抛异常
        _rate_redis_client = client
        logger.warning("登录限速使用 Redis 后端(跨 Worker 共享)")
    except Exception:  # noqa: BLE001 - 降级内存限速, 不阻断登录
        logger.warning("Redis 不可用, 登录限速降级为进程内存(单进程有效)")
        _rate_redis_client = None
    return _rate_redis_client


def _rate_key(username: str) -> str:
    """Redis 限速键(用户名小写化; TTL 即锁定期, INCR 幂等)。"""
    return f"{_RATE_KEY_PREFIX}:{(username or '').strip().lower()}"


def _redis_is_locked(username: str) -> bool:
    """Redis 限速判定: 计数达到上限即锁定(键 TTL 过期后自动解除)。"""
    r = _rate_redis()
    if r is None:
        return False
    try:
        count = r.get(_rate_key(username))
        return count is not None and int(count) >= MAX_LOGIN_FAILURES
    except Exception:  # noqa: BLE001 - 运行期错误降级内存
        return False


def _redis_record_failure(username: str) -> None:
    """Redis 记录一次失败: INCR 计数, 键 TTL = 锁定时长(滑动重置)。"""
    r = _rate_redis()
    if r is None:
        return
    try:
        r.incr(_rate_key(username))
        r.expire(_rate_key(username), LOCKOUT_SECONDS)
    except Exception:  # noqa: BLE001 - 运行期错误降级内存
        pass


def _redis_clear(username: str) -> None:
    """登录成功后清除 Redis 限速键。"""
    r = _rate_redis()
    if r is None:
        return
    try:
        r.delete(_rate_key(username))
    except Exception:  # noqa: BLE001
        pass


_LOGIN_FAILURES: dict[str, list[float]] = {}
_LOGIN_LOCKED_UNTIL: dict[str, float] = {}
_RATE_LOCK = threading.RLock()


def _is_locked(username: str) -> bool:
    """是否处于锁定期(Redis 优先, 降级内存); 锁定时间已过则顺带清理状态。"""
    if _redis_is_locked(username):
        return True
    until = _LOGIN_LOCKED_UNTIL.get(username)
    if until is None:
        return False
    if time.monotonic() < until:
        return True
    with _RATE_LOCK:
        _LOGIN_LOCKED_UNTIL.pop(username, None)
        _LOGIN_FAILURES.pop(username, None)
    return False


def _record_failure(username: str) -> None:
    """记录一次登录失败(Redis 优先, 降级内存);
    时间窗(锁定时长)内累计达到上限则触发锁定。"""
    _redis_record_failure(username)
    with _RATE_LOCK:
        if _is_locked(username):
            return
        now = time.monotonic()
        window = [t for t in _LOGIN_FAILURES.get(username, []) if now - t < LOCKOUT_SECONDS]
        window.append(now)
        if len(window) >= MAX_LOGIN_FAILURES:
            _LOGIN_LOCKED_UNTIL[username] = now + LOCKOUT_SECONDS
            _LOGIN_FAILURES.pop(username, None)
        else:
            _LOGIN_FAILURES[username] = window


def _clear_failures(username: str) -> None:
    """登录成功后清除该用户名限速状态(Redis + 内存)。"""
    _redis_clear(username)
    with _RATE_LOCK:
        _LOGIN_FAILURES.pop(username, None)
        _LOGIN_LOCKED_UNTIL.pop(username, None)


def reset_login_rate_limit(username: str | None = None) -> None:
    """清空登录限速状态(测试与运维恢复用; Redis 键一并清除)。

    参数:
        username: 为空时清空全部用户名; 否则只清指定用户名。
    """
    r = _rate_redis()
    if r is not None and username is not None:
        try:
            r.delete(_rate_key(username))
        except Exception:  # noqa: BLE001
            pass
    with _RATE_LOCK:
        if username is None:
            _LOGIN_FAILURES.clear()
            _LOGIN_LOCKED_UNTIL.clear()
        else:
            _LOGIN_FAILURES.pop(username, None)
            _LOGIN_LOCKED_UNTIL.pop(username, None)


# ---------------------------------------------------------------------------
# 时间与序列化辅助
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    """当前 UTC 时间(所有时间列统一 UTC 存储)。"""
    return datetime.now(UTC)


def as_utc(dt: datetime | None) -> datetime | None:
    """将可能为 naive 的 datetime 按 UTC 解释(SQLite 测试环境回读为 naive)。"""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _session_expires_at(now: datetime | None = None) -> datetime:
    """按配置的会话 TTL 计算过期时刻。"""
    return (now or utcnow()) + timedelta(minutes=settings.session_ttl_minutes)


def _clean_ip(ip: str | None) -> str | None:
    """IP 白名单化: 仅保留合法 IP, 其余(PG INET 不接受的测试客户端名等)存 NULL。"""
    if not ip:
        return None
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        return None


def _validate_new_password(password: str) -> tuple[bool, str]:
    """新密码校验: 强度规则 + bcrypt 72 字节上限, 返回 (ok, reason)。"""
    ok, reason = check_password_strength(password)
    if ok and len(password.encode("utf-8")) > 72:
        return False, "密码过长(UTF-8 编码后不能超过 72 字节)"
    return ok, reason


# ---------------------------------------------------------------------------
# 应用级设置(键值, 多 Worker 权威来源, M-12)
# ---------------------------------------------------------------------------

#: 设置键: 自助注册开关(默认关闭)
KEY_REGISTRATION_ENABLED = "registration_enabled"


def get_app_setting(db: Session, key: str, default: Any = None) -> Any:
    """读取应用级设置(未设置返回 default)。"""
    row = db.execute(
        select(AppSetting).where(AppSetting.key == key)
    ).scalar_one_or_none()
    if row is None:
        return default
    return row.value.get("value", default)


def set_app_setting(db: Session, key: str, value: Any, updated_by: int | None = None) -> None:
    """写入应用级设置(upsert), 全部 Worker 从数据库读取同一值。"""
    row = db.execute(
        select(AppSetting).where(AppSetting.key == key)
    ).scalar_one_or_none()
    if row is None:
        db.add(AppSetting(key=key, value={"value": value}, updated_by=updated_by))
    else:
        row.value = {"value": value}
        row.updated_by = updated_by
        row.updated_at = datetime.now(UTC)
    db.flush()


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
    db.add(
        AuthEvent(
            event_type=event_type,
            user_id=user_id,
            session_id=session_id,
            ip=_clean_ip(ip),
            user_agent=user_agent,
            detail=detail,
        )
    )


# ---------------------------------------------------------------------------
# 角色与用户查询
# ---------------------------------------------------------------------------


def ensure_role(db: Session, code: str, name: str) -> Role:
    """确保全局角色存在(幂等), 返回角色行。

    参数:
        code: 角色编码(如 admin / engineer)。
        name: 角色显示名(仅首次创建时使用)。
    """
    role = db.execute(select(Role).where(Role.code == code)).scalar_one_or_none()
    if role is None:
        role = Role(code=code, name=name, description=f"内置角色:{name}", is_system=True)
        db.add(role)
        db.flush()
    return role


def user_roles(db: Session, user: User) -> list[str]:
    """返回用户当前(未撤销)的角色编码列表, 按角色 id 升序。"""
    rows = db.execute(
        select(Role.code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user.id, UserRole.revoked_at.is_(None))
        .order_by(Role.id)
    ).scalars()
    return list(rows)


def has_role(db: Session, user: User, code: str) -> bool:
    """用户是否拥有指定角色(当前有效授权)。"""
    return code in user_roles(db, user)


def list_users(db: Session) -> list[User]:
    """全部用户(含停用), 按 id 升序。"""
    return list(db.execute(select(User).order_by(User.id)).scalars())


def get_user_by_id(db: Session, user_id: int) -> User | None:
    """按主键取用户(含停用), 不存在返回 None。"""
    return db.get(User, user_id)


def get_user_by_username(db: Session, username: str) -> User | None:
    """按用户名(强制小写)取用户。"""
    return db.execute(
        select(User).where(User.username == (username or "").strip().lower())
    ).scalar_one_or_none()


def get_active_password_credential(db: Session, user: User) -> Credential | None:
    """返回用户当前有效的 password 凭证(至多一条), 无则返回 None。"""
    return db.execute(
        select(Credential)
        .where(
            Credential.user_id == user.id,
            Credential.credential_type == "password",
            Credential.revoked_at.is_(None),
        )
        .order_by(Credential.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# 用户生命周期
# ---------------------------------------------------------------------------


def create_user(
    db: Session,
    username: str,
    password: str,
    role: str = ROLE_ENGINEER,
    force_password_change: bool = True,
    *,
    display_name: str | None = None,
    email: str | None = None,
    created_by: int | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> User:
    """创建用户 + 密码凭证 + 角色授权(角色表幂等补齐), 并写认证审计。

    参数:
        username: 登录名(自动去空格并转小写, 须匹配 ^[a-z0-9_]{3,32}$)。
        password: 明文密码(bcrypt 哈希入库, 须满足强度规则)。
        role: 内置角色编码, 仅允许 admin / engineer(RPD 3.1)。
        force_password_change: 是否要求首次登录后强制改密(种子/重置场景为 True)。
        created_by: 创建者用户 id; 自助注册为 None, 此时自授权。
    返回:
        新用户(已提交)。
    """
    username = (username or "").strip().lower()
    if not re.fullmatch(USERNAME_RE, username):
        raise BadRequestError(
            "",
            code="AUTH-USER-003",
            message_key="ies.diag.auth.username_invalid",
            params={"username": username, "pattern": USERNAME_RE},
        )
    if email:
        email = email.strip().lower()
        if not re.fullmatch(EMAIL_RE, email):
            raise BadRequestError(
                "",
                code="AUTH-USER-004",
                message_key="ies.diag.auth.email_invalid",
                params={"email": email},
            )
    ok, reason = _validate_new_password(password)
    if not ok:
        raise WeakPasswordError(params={"reason": reason})
    if role not in (ROLE_ADMIN, ROLE_ENGINEER):
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
        dup_email = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if dup_email is not None:
            raise ConflictError(
                "",
                code="AUTH-USER-005",
                message_key="ies.diag.auth.email_taken",
                params={"email": email},
            )
    role_row = ensure_role(db, role, name="工程师" if role == ROLE_ENGINEER else "管理员")
    user = User(
        username=username,
        display_name=(display_name or "").strip() or username,
        email=email,
    )
    db.add(user)
    db.flush()
    db.add(
        Credential(
            user_id=user.id,
            credential_type="password",
            secret_hash=hash_password(password),
            algorithm="bcrypt",
            strength_score=100 if ok else 0,
            requires_change=force_password_change,
            created_by=created_by,
        )
    )
    # 追加式授权: 授权人缺省为本人(自注册), 否则为操作管理员
    db.add(UserRole(user_id=user.id, role_id=role_row.id, granted_by=created_by or user.id))
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
    admin: User,
    user: User,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """停用账号(管理员): 状态置 disabled, 立即撤销全部会话并写审计。

    约束: 不能停用自己(避免管理员自锁), 不能停用系统账号。
    """
    if user.id == admin.id:
        raise ForbiddenError("", params={"reason": "cannot_deactivate_self"})
    if user.is_system:
        raise ForbiddenError("", params={"reason": "system_account"})
    if user.status == "disabled":
        return
    now = utcnow()
    user.status = "disabled"
    user.updated_at = now
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
    admin: User,
    user: User,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """重新启用账号(管理员): 状态置 active 并写权限变更审计。"""
    if user.status == "active":
        return
    now = utcnow()
    user.status = "active"
    user.updated_at = now
    record_auth_event(
        db,
        "permission_change",
        user_id=user.id,
        ip=ip,
        user_agent=user_agent,
        detail={"action": "user_reactivated", "by": admin.id},
    )
    db.commit()


def delete_user(
    db: Session,
    admin: User,
    user: User,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> dict:
    """删除账号(管理员): 该账号拥有的项目一并删除(RPD 5.4 账号生命周期)。

    - 不能删除自己, 不能删除系统账号;
    - 目标用户拥有的项目全部置 status='deleted'(软删, 与项目删除一致,
      不可变版本/审计保留, 对象清理由 U16 重试执行);
    - 用户状态置 disabled, 全部会话撤销, 凭证撤销(不可变表只置撤销标记)。
    返回: {"deleted_projects": N, "deleted_datasets": N}(数据集一并回收)。
    """
    if user.id == admin.id:
        raise ForbiddenError("", params={"reason": "cannot_delete_self"})
    if user.is_system:
        raise ForbiddenError("", params={"reason": "system_account"})
    from iesplan.services import project as project_service

    now = utcnow()
    # 该用户拥有的项目 → 软删(级联)
    owned = db.execute(
        select(Project).where(Project.owner_id == user.id, Project.status != "deleted")
    ).scalars().all()
    deleted_projects = 0
    for project in owned:
        project.status = "deleted"
        project.updated_at = now
        deleted_projects += 1
        project_service._audit(
            db, "project", project.id, "project.deleted_by_account", admin.id,
            after={"reason": "account_deleted", "account_id": user.id},
        )
    # 账号停用 + 会话/凭证撤销
    user.status = "disabled"
    user.updated_at = now
    revoke_all_user_sessions(db, user, revoked_by=admin.id)
    creds = db.execute(
        select(Credential).where(
            Credential.user_id == user.id,
            Credential.credential_type == "password",
            Credential.revoked_at.is_(None),
        )
    ).scalars().all()
    for cred in creds:
        _revoke_credential(cred, now)
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


def _revoke_credential(cred: Credential, now: datetime) -> None:
    """撤销旧凭证(credentials 为不可变表, 只置撤销标记不允许物理删除)。"""
    cred.revoked_at = now
    if cred.rotated_at is None:
        cred.rotated_at = now


def change_password(
    db: Session,
    user: User,
    old_password: str,
    new_password: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """修改密码: 校验旧密码/强度, 递增凭证版本使全部旧会话失效, 写审计。

    - 首登强制改密状态(credential.requires_change)在校验通过后清除;
    - 凭证版本递增后, 旧窗口会话全部失效, 前端须重新登录(RPD 3.4 凭证失效机制)。
    """
    cred = get_active_password_credential(db, user)
    if cred is None or not verify_password(old_password, cred.secret_hash):
        raise BadOldPasswordError()
    if old_password == new_password:
        raise SamePasswordError()
    ok, reason = _validate_new_password(new_password)
    if not ok:
        raise WeakPasswordError(params={"reason": reason})
    was_force_change = cred.requires_change
    now = utcnow()
    _revoke_credential(cred, now)
    db.add(
        Credential(
            user_id=user.id,
            credential_type="password",
            secret_hash=hash_password(new_password),
            algorithm="bcrypt",
            strength_score=100 if ok else 0,
            requires_change=False,
            rotated_at=now,
            created_by=user.id,
        )
    )
    user.credential_version += 1
    user.updated_at = now
    revoke_all_user_sessions(db, user, revoked_by=user.id)
    record_auth_event(
        db,
        "password_change",
        user_id=user.id,
        ip=ip,
        user_agent=user_agent,
        detail={"was_force_change": was_force_change, "credential_version": user.credential_version},
    )
    db.commit()


def reset_password(
    db: Session,
    admin: User,
    target_user: User,
    new_tmp: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """管理员重置密码: 签发临时密码(requires_change=True), 全部旧会话失效, 写审计。"""
    ok, reason = _validate_new_password(new_tmp)
    if not ok:
        raise WeakPasswordError(params={"reason": reason})
    now = utcnow()
    old = get_active_password_credential(db, target_user)
    if old is not None:
        _revoke_credential(old, now)
    db.add(
        Credential(
            user_id=target_user.id,
            credential_type="password",
            secret_hash=hash_password(new_tmp),
            algorithm="bcrypt",
            strength_score=100 if ok else 0,
            requires_change=True,
            created_by=admin.id,
        )
    )
    target_user.credential_version += 1
    target_user.updated_at = now
    revoke_all_user_sessions(db, target_user, revoked_by=admin.id)
    record_auth_event(
        db,
        "credential_reset",
        user_id=target_user.id,
        ip=ip,
        user_agent=user_agent,
        detail={"reset_by": admin.id, "credential_version": target_user.credential_version},
    )
    db.commit()


# ---------------------------------------------------------------------------
# 认证(登录 + 限速)
# ---------------------------------------------------------------------------


def authenticate(
    db: Session,
    username: str,
    password: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
    device: str | None = None,
) -> tuple[User | None, str | None]:
    """校验用户名与密码, 返回 (user, error_code|None)。

    error_code 取值:
        - "invalid_credentials": 用户不存在 / 密码错误 / 账号停用(统一文案, 不区分);
        - "locked": 登录限速锁定(5 次失败锁 15 分钟)。
    成功时更新 last_login_at 并清空该用户名限速计数。
    """
    username = (username or "").strip().lower()
    if _is_locked(username):
        return None, "locked"
    user = get_user_by_username(db, username)
    if user is None:
        # 假校验: 与真实校验耗时保持一致, 防用户名枚举(时间侧信道)
        verify_password(password, _DUMMY_PASSWORD_HASH)
        _record_failure(username)
        record_auth_event(
            db, "login_failure", ip=ip, user_agent=user_agent,
            detail={"reason": "user_not_found", "username": username},
        )
        db.commit()
        return None, "invalid_credentials"
    if user.status != "active" or user.is_system:
        # 假校验: 同上, 防账号状态枚举(停用/系统账号与密码错误耗时一致)
        verify_password(password, _DUMMY_PASSWORD_HASH)
        _record_failure(username)
        record_auth_event(
            db, "login_failure", user_id=user.id, ip=ip, user_agent=user_agent,
            detail={"reason": "user_disabled" if user.status != "active" else "system_account"},
        )
        db.commit()
        return None, "invalid_credentials"
    cred = get_active_password_credential(db, user)
    if cred is None:
        # 假校验: 无有效凭证与密码错误的耗时一致(常规 401 路径)
        verify_password(password, _DUMMY_PASSWORD_HASH)
        _record_failure(username)
        record_auth_event(
            db, "login_failure", user_id=user.id, ip=ip, user_agent=user_agent,
            detail={"reason": "no_credential"},
        )
        db.commit()
        return None, "invalid_credentials"
    if not verify_password(password, cred.secret_hash):
        _record_failure(username)
        record_auth_event(
            db, "login_failure", user_id=user.id, ip=ip, user_agent=user_agent,
            detail={"reason": "bad_password"},
        )
        db.commit()
        return None, "invalid_credentials"
    _clear_failures(username)
    user.last_login_at = utcnow()
    record_auth_event(
        db, "login_success", user_id=user.id, ip=ip, user_agent=user_agent,
        detail={"device": device} if device else None,
    )
    db.commit()
    return user, None


# ---------------------------------------------------------------------------
# 窗口会话(单活动窗口, RPD 3.3)
# ---------------------------------------------------------------------------


def _new_window_session(
    user: User, now: datetime, status: str = "active"
) -> tuple[WindowSession, str]:
    """构造新会话行, 返回 (会话行, 令牌原文; 令牌原文只由调用方持有)。

    参数:
        status: 新会话初始状态 —— "active" 为正式活动窗口;
                "takeover_pending" 为待接管窗口(H-01: 接管确认前不拥有业务权限,
                仅允许确认接管/改密/登出/本人信息接口)。
    """
    token = new_session_token()
    return (
        WindowSession(
            session_token_hash=token_hash(token),
            user_id=user.id,
            credential_version_at_issue=user.credential_version,
            status=status,
            created_at=now,
            last_seen_at=now,
            expires_at=_session_expires_at(now),
        ),
        token,
    )


def _revoke_session(
    session: WindowSession,
    now: datetime,
    revoked_by: int | None = None,
    replaced_by: int | None = None,
) -> None:
    """将会话置为 revoked(列级可更新字段: status/revoked_at/revoked_by/replaced_by)。"""
    session.status = "revoked"
    session.revoked_at = now
    if revoked_by is not None:
        session.revoked_by = revoked_by
    if replaced_by is not None:
        session.replaced_by_session_id = replaced_by


def _expire_stale_sessions(db: Session, user_id: int, now: datetime) -> int:
    """将指定用户已过期的活动/待接管会话置为 expired(不提交, 由调用方统一提交)。"""
    rows = db.execute(
        select(WindowSession).where(
            WindowSession.user_id == user_id,
            WindowSession.status.in_(_ACTIVE_STATUSES),
        )
    ).scalars()
    count = 0
    for s in rows:
        expires_at = as_utc(s.expires_at)
        if expires_at is not None and expires_at < now:
            s.status = "expired"
            s.revoked_at = now
            count += 1
    return count


def create_window_session(
    db: Session,
    user: User,
    device_info: str | None = None,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[WindowSession, str, bool]:
    """创建窗口会话(单活动窗口, RPD 3.3; 接管确认语义 H-01)。

    流程:
        1. 先清理该用户已过期的会话;
        2. 撤销残留的 takeover_pending 会话(被本次登录取代; 部分唯一索引
           每用户至多一条 pending);
        3. 若存在 active 会话, 直接撤销(本次登录触发接管);
        4. 触发接管时(存在被撤销的 active 或残留 pending)新会话创建为
           takeover_pending —— 在确认接管(confirm_takeover)之前不拥有任何
           业务权限; 否则创建为 active。

    返回:
        (session, token, old_session_displaced):
        old_session_displaced 为 True 表示存在旧活动窗口被降级/撤销
        (前端据此提示确认接管并重新加载最新修订)。
    """
    now = utcnow()
    _expire_stale_sessions(db, user.id, now)
    # 先撤销残留的 pending 会话(更早接管流程遗留; 部分唯一索引每用户至多一条 pending)
    pending = list(
        db.execute(
            select(WindowSession).where(
                WindowSession.user_id == user.id,
                WindowSession.status == "takeover_pending",
            )
        ).scalars()
    )
    # 若存在 active 会话, 直接撤销 —— 新会话以 takeover_pending 创建,
    # 避免同时存在两条 pending 触发唯一索引冲突(部分唯一索引每用户至多一条 active)
    active = db.execute(
        select(WindowSession).where(
            WindowSession.user_id == user.id,
            WindowSession.status == "active",
        )
    ).scalar_one_or_none()
    displaced = active is not None or bool(pending)
    for old in pending:
        _revoke_session(old, now, user.id)
    if active is not None:
        _revoke_session(active, now, user.id)
    db.flush()
    # 创建新会话: 触发接管时初始为 takeover_pending(H-01, 确认前无业务权限)
    new_session, token = _new_window_session(
        user, now, status="takeover_pending" if displaced else "active"
    )
    db.add(new_session)
    db.flush()
    # 被撤销的残留 pending/active 会话由新会话接管(补 replaced_by 指针, 接管追溯)
    for old in pending:
        old.replaced_by_session_id = new_session.id
    if active is not None:
        active.replaced_by_session_id = new_session.id
    if displaced:
        record_auth_event(
            db,
            "session_takeover",
            user_id=user.id,
            session_id=new_session.id,
            ip=ip,
            user_agent=user_agent,
            detail={
                "from_session_id": active.id if active is not None else None,
                "to_session_id": new_session.id,
                "reason": "new_login",
            },
        )
    db.commit()
    return new_session, token, displaced


def confirm_takeover(
    db: Session,
    user: User,
    current_session: WindowSession,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> WindowSession:
    """确认接管(RPD 3.3 / 01 §1.5): 保留当前会话并转为 active。

    接管流程(schema 01 §1.5): 新登录使旧会话撤销、新会话以 takeover_pending
    创建(H-01: 确认前无业务权限); 用户确认接管后, 当前待接管会话直接保留
    为 active(状态列级迁移, 不轮换凭证) —— 客户端既有 Cookie/Bearer 凭证
    立即生效, 不再依赖响应 Set-Cookie 替换凭证, 避免"确认接管后旧 session
    仍处于 takeover_pending"导致全部写 API 被拒。

    其余 pending/active 会话(理论上至多各一条, 部分唯一索引保证)一并撤销,
    保持单活动窗口不变量。返回保留的活动会话。
    """
    now = utcnow()
    # 并发防御: 确认前被新登录撤销的会话不再恢复(重新读取最新状态)
    current = db.get(WindowSession, current_session.id)
    if current is None or current.status not in ("takeover_pending", "active"):
        raise SessionInvalidError()
    others = list(
        db.execute(
            select(WindowSession).where(
                WindowSession.user_id == user.id,
                WindowSession.status.in_(_ACTIVE_STATUSES),
                WindowSession.id != current.id,
            )
        ).scalars()
    )
    # 先撤销其余 pending/active 会话, 再把当前会话置为 active
    # (状态迁移顺序避免触犯 active/pending 部分唯一索引)
    for old in others:
        _revoke_session(old, now, user.id)
    if current.status != "active":
        current.status = "active"
    db.flush()
    for old in others:
        old.replaced_by_session_id = current.id
    record_auth_event(
        db,
        "session_takeover",
        user_id=user.id,
        session_id=current.id,
        ip=ip,
        user_agent=user_agent,
        detail={
            "from_session_id": None,
            "to_session_id": current.id,
            "reason": "confirm_takeover",
        },
    )
    db.commit()
    return current


def get_session_by_token(db: Session, token: str) -> WindowSession | None:
    """按窗口凭证(原文)查找会话(令牌以 sha256 摘要存储)。"""
    return db.execute(
        select(WindowSession).where(WindowSession.session_token_hash == token_hash(token))
    ).scalar_one_or_none()


def revoke_session(
    db: Session,
    user: User,
    session: WindowSession,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
    reason: str = "logout",
) -> None:
    """撤销单个会话(登出场景), 写 logout / session_revoke 审计。"""
    now = utcnow()
    _revoke_session(session, now, user.id)
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


def revoke_other_sessions(
    db: Session,
    user: User,
    keep_session_id: int,
    *,
    revoked_by: int | None = None,
) -> int:
    """撤销用户除指定会话外的全部活动/待接管会话, 返回撤销数量。"""
    now = utcnow()
    rows = list(
        db.execute(
            select(WindowSession).where(
                WindowSession.user_id == user.id,
                WindowSession.status.in_(_ACTIVE_STATUSES),
                WindowSession.id != keep_session_id,
            )
        ).scalars()
    )
    for s in rows:
        _revoke_session(s, now, revoked_by)
    if rows:
        record_auth_event(
            db,
            "session_revoke",
            user_id=user.id,
            detail={"revoked_session_ids": [s.id for s in rows], "reason": "revoke_others"},
        )
        db.commit()
    return len(rows)


def revoke_all_user_sessions(
    db: Session,
    user: User,
    *,
    revoked_by: int | None = None,
) -> int:
    """撤销用户全部活动/待接管会话(凭证变更/停用时调用), 返回撤销数量。"""
    now = utcnow()
    rows = list(
        db.execute(
            select(WindowSession).where(
                WindowSession.user_id == user.id,
                WindowSession.status.in_(_ACTIVE_STATUSES),
            )
        ).scalars()
    )
    for s in rows:
        _revoke_session(s, now, revoked_by)
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
    stmt = select(WindowSession).where(WindowSession.status.in_(_ACTIVE_STATUSES))
    if user_id is not None:
        stmt = stmt.where(WindowSession.user_id == user_id)
    now = utcnow()
    count = 0
    for s in db.execute(stmt).scalars():
        expires_at = as_utc(s.expires_at)
        if expires_at is not None and expires_at < now:
            s.status = "expired"
            s.revoked_at = now
            count += 1
    if count:
        db.commit()
    return count


def extend_session(db: Session, session: WindowSession) -> datetime:
    """会话续期: 更新最后活跃时间并按 TTL 顺延过期时刻, 返回新的过期时刻。"""
    now = utcnow()
    session.last_seen_at = now
    session.expires_at = _session_expires_at(now)
    db.commit()
    return session.expires_at
