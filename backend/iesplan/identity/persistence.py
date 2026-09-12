"""身份域 SQL repository 实现（users/roles/user_roles/credentials/window_sessions/app_settings/auth_events）。

- 本模块是 `IdentityRepository` 协议的实现，可经 `iesplan.identity` 门面调用；
- 只访问 `iesplan.models.identity` 的表；绝不 commit/rollback（调用方事务拥有）；
- 唯一冲突转 `IdentityConflictError`；冲突后调用方须回滚会话
  （与旧 services 契约一致；实测 SA 2.0 下 flush 失败后会话不可继续）；
- 密钥材料绝不进入记录：密码哈希只经 `get_active_password_secret`
  以原文字符串返回供验算，永不装入 DTO、不落日志；
- 用户名/邮箱按旧 services 语义规范化（去空格转小写）后查询。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from iesplan.models.identity import (
    AppSetting,
    AuthEvent,
    Credential,
    Role,
    User,
    UserRole,
    WindowSession,
)
from iesplan.identity.contracts import (
    AppSettingRecord,
    AuthEventRecord,
    CredentialRecord,
    IdentityConflictError,
    RoleRecord,
    UserNotFoundError,
    UserRecord,
    UserRoleRecord,
    WindowSessionRecord,
)


def _iso(value: datetime | None) -> str | None:
    """ORM 时间 → 记录字符串（原样 isoformat，不增减时区后缀）。"""
    return value.isoformat() if value is not None else None


def _now() -> datetime:
    return datetime.now(UTC)


def _row_to_user(row: User) -> UserRecord:
    return UserRecord(
        id=row.id,
        username=row.username,
        display_name=row.display_name,
        email=row.email,
        status=row.status,
        locale=row.locale,
        timezone=row.timezone,
        fixed_utc_offset_minutes=row.fixed_utc_offset_minutes,
        credential_version=row.credential_version,
        is_system=row.is_system,
        auth_subject=row.auth_subject,
        public_namespace=row.public_namespace,
        last_login_at=_iso(row.last_login_at),
    )


def _row_to_credential(row: Credential) -> CredentialRecord:
    return CredentialRecord(
        id=row.id,
        user_id=row.user_id,
        credential_type=row.credential_type,
        algorithm=row.algorithm,
        strength_score=row.strength_score,
        requires_change=row.requires_change,
        expires_at=_iso(row.expires_at),
        revoked_at=_iso(row.revoked_at),
    )


def _row_to_role(row: Role) -> RoleRecord:
    return RoleRecord(
        id=row.id,
        code=row.code,
        name=row.name,
        description=row.description,
        is_system=row.is_system,
    )


def _row_to_user_role(row: UserRole) -> UserRoleRecord:
    return UserRoleRecord(
        id=row.id,
        user_id=row.user_id,
        role_id=row.role_id,
        granted_by=row.granted_by,
        revoked_at=_iso(row.revoked_at),
        revoked_by=row.revoked_by,
    )


def _row_to_session(row: WindowSession) -> WindowSessionRecord:
    return WindowSessionRecord(
        id=row.id,
        user_id=row.user_id,
        status=row.status,
        credential_version_at_issue=row.credential_version_at_issue,
        replaced_by_session_id=row.replaced_by_session_id,
        last_seen_at=_iso(row.last_seen_at),
        expires_at=_iso(row.expires_at),
        revoked_at=_iso(row.revoked_at),
        revoked_by=row.revoked_by,
    )


def _row_to_setting(row: AppSetting) -> AppSettingRecord:
    return AppSettingRecord(key=row.key, value=row.value, updated_by=row.updated_by)


def _row_to_auth_event(row: AuthEvent) -> AuthEventRecord:
    return AuthEventRecord(
        id=row.id,
        user_id=row.user_id,
        session_id=row.session_id,
        event_type=row.event_type,
        occurred_at=_iso(row.occurred_at),
        detail=row.detail,
    )


def get_user(db: Session, user_id: int) -> UserRecord | None:
    """按主键取用户（含停用）；不存在返回 None。"""
    row = db.get(User, user_id)
    return _row_to_user(row) if row is not None else None


def get_user_by_username(db: Session, username: str) -> UserRecord | None:
    """按用户名取用户（去空格转小写后查询，与旧 services 语义一致）。"""
    row = db.execute(
        select(User).where(User.username == (username or "").strip().lower())
    ).scalar_one_or_none()
    return _row_to_user(row) if row is not None else None


def get_user_by_email(db: Session, email: str) -> UserRecord | None:
    """按邮箱取用户（去空格转小写后查询）。"""
    row = db.execute(
        select(User).where(User.email == (email or "").strip().lower())
    ).scalar_one_or_none()
    return _row_to_user(row) if row is not None else None


def get_user_by_namespace(db: Session, namespace: str) -> UserRecord | None:
    """按公开命名空间取用户；不存在返回 None。"""
    row = db.execute(
        select(User).where(User.public_namespace == namespace)
    ).scalar_one_or_none()
    return _row_to_user(row) if row is not None else None


def get_user_by_auth_subject(db: Session, subject: str) -> UserRecord | None:
    """按外部主体(sub)取已绑定用户；未绑定返回 None。"""
    row = db.execute(
        select(User).where(User.auth_subject == subject)
    ).scalar_one_or_none()
    return _row_to_user(row) if row is not None else None


def bind_auth_subject(db: Session, user_id: int, subject: str) -> UserRecord:
    """绑定外部主体（唯一键；冲突抛 IdentityConflictError，调用方回滚）。"""
    row = db.get(User, user_id)
    if row is None:
        raise UserNotFoundError("用户不存在", params={"user_id": user_id})
    row.auth_subject = subject
    try:
        db.flush()
    except IntegrityError as exc:
        raise IdentityConflictError("外部主体已被绑定", params={"user_id": user_id}) from exc
    return _row_to_user(row)


def list_users(db: Session) -> list[UserRecord]:
    """全部用户（含停用），按 id 升序。"""
    rows = db.execute(select(User).order_by(User.id)).scalars().all()
    return [_row_to_user(row) for row in rows]


def create_user(
    db: Session,
    *,
    username: str,
    display_name: str,
    email: str | None = None,
    status: str = "active",
    is_system: bool = False,
    public_namespace: str | None = None,
) -> UserRecord:
    """插入用户裸行（校验/哈希/授权由调用方先完成）；重名/命名空间重复抛错。

    注: email 列无唯一约束, 邮箱重复由调用方(services 建用户)前置检查。
    """
    row = User(
        username=username,
        display_name=display_name,
        email=email,
        status=status,
        is_system=is_system,
        public_namespace=public_namespace,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise IdentityConflictError("用户名或邮箱已存在", params={"username": username}) from exc
    return _row_to_user(row)


def set_user_status(db: Session, user_id: int, status: str) -> UserRecord:
    """切换用户状态（并刷新 updated_at）；用户缺失抛 UserNotFoundError。"""
    row = db.get(User, user_id)
    if row is None:
        raise UserNotFoundError("用户不存在", params={"user_id": user_id})
    row.status = status
    row.updated_at = _now()
    db.flush()
    return _row_to_user(row)


def set_public_namespace(db: Session, user_id: int, namespace: str) -> UserRecord:
    """分配公开命名空间（终身一次；重复分配抛 IdentityConflictError）。"""
    row = db.get(User, user_id)
    if row is None:
        raise UserNotFoundError("用户不存在", params={"user_id": user_id})
    row.public_namespace = namespace
    try:
        db.flush()
    except IntegrityError as exc:
        raise IdentityConflictError("公开命名空间已被占用", params={"user_id": user_id}) from exc
    return _row_to_user(row)


def touch_login(db: Session, user_id: int) -> UserRecord:
    """更新最后登录时间；用户缺失抛 UserNotFoundError。"""
    row = db.get(User, user_id)
    if row is None:
        raise UserNotFoundError("用户不存在", params={"user_id": user_id})
    row.last_login_at = _now()
    db.flush()
    return _row_to_user(row)


def bump_credential_version(db: Session, user_id: int) -> UserRecord:
    """递增用户凭证版本（改密/重置时使旧会话失效）并刷新 updated_at。"""
    row = db.get(User, user_id)
    if row is None:
        raise UserNotFoundError("用户不存在", params={"user_id": user_id})
    row.credential_version += 1
    row.updated_at = _now()
    db.flush()
    return _row_to_user(row)


def get_active_credential(db: Session, user_id: int) -> CredentialRecord | None:
    """用户当前有效密码凭证元数据（无哈希；创建时间倒序取首条）。"""
    row = db.execute(
        select(Credential)
        .where(
            Credential.user_id == user_id,
            Credential.credential_type == "password",
            Credential.revoked_at.is_(None),
        )
        .order_by(Credential.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _row_to_credential(row) if row is not None else None


def get_active_password_secret(db: Session, user_id: int) -> str | None:
    """用户当前有效密码哈希（仅供验算；永不装入 DTO、不落日志）。"""
    row = db.execute(
        select(Credential.secret_hash)
        .where(
            Credential.user_id == user_id,
            Credential.credential_type == "password",
            Credential.revoked_at.is_(None),
        )
        .order_by(Credential.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return row


def add_credential(
    db: Session,
    *,
    user_id: int,
    credential_type: str,
    secret_hash: str,
    algorithm: str | None = None,
    strength_score: int = 0,
    requires_change: bool = True,
    created_by: int | None = None,
    rotated_at: str | None = None,
) -> CredentialRecord:
    """写入新凭证行（哈希由调用方域服务生成后传入，repository 不做密码学）。"""
    row = Credential(
        user_id=user_id,
        credential_type=credential_type,
        secret_hash=secret_hash,
        algorithm=algorithm,
        strength_score=strength_score,
        requires_change=requires_change,
        created_by=created_by,
        rotated_at=datetime.fromisoformat(rotated_at) if rotated_at is not None else None,
    )
    db.add(row)
    db.flush()
    return _row_to_credential(row)


def revoke_credentials(
    db: Session, user_id: int, *, credential_type: str = "password"
) -> int:
    """吊销用户指定类型的未撤销凭证（不可变表只置标记），返回吊销行数。"""
    rows = db.execute(
        select(Credential).where(
            Credential.user_id == user_id,
            Credential.credential_type == credential_type,
            Credential.revoked_at.is_(None),
        )
    ).scalars().all()
    now = _now()
    for row in rows:
        row.revoked_at = now
        if row.rotated_at is None:
            row.rotated_at = now
    db.flush()
    return len(rows)


def ensure_role(db: Session, code: str, name: str) -> RoleRecord:
    """取或建全局角色（并发建重抛 IdentityConflictError，调用方回滚后重读）。"""
    row = db.execute(select(Role).where(Role.code == code)).scalar_one_or_none()
    if row is not None:
        return _row_to_role(row)
    row = Role(code=code, name=name, description=f"内置角色:{name}", is_system=True)
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise IdentityConflictError("角色已存在", params={"code": code}) from exc
    return _row_to_role(row)


def user_roles(db: Session, user_id: int) -> list[str]:
    """用户当前有效角色 code 列表（按角色 id 升序）。"""
    rows = db.execute(
        select(Role.code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id, UserRole.revoked_at.is_(None))
        .order_by(Role.id)
    ).scalars()
    return list(rows)


def grant_role(
    db: Session, *, user_id: int, role_id: int, granted_by: int
) -> UserRoleRecord:
    """追加授权行；重复授权抛 IdentityConflictError。"""
    row = UserRole(user_id=user_id, role_id=role_id, granted_by=granted_by)
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise IdentityConflictError(
            "重复授权", params={"user_id": user_id, "role_id": role_id}
        ) from exc
    return _row_to_user_role(row)


def revoke_role(db: Session, *, user_id: int, role_id: int, revoked_by: int) -> None:
    """撤销授权（只置标记；无有效授权时静默无操作）。"""
    row = db.execute(
        select(UserRole).where(
            UserRole.user_id == user_id,
            UserRole.role_id == role_id,
            UserRole.revoked_at.is_(None),
        )
    ).scalar_one_or_none()
    if row is None:
        return
    row.revoked_at = _now()
    row.revoked_by = revoked_by
    db.flush()


def get_session(db: Session, session_id: int) -> WindowSessionRecord | None:
    """按 id 取会话；不存在返回 None。"""
    row = db.get(WindowSession, session_id)
    return _row_to_session(row) if row is not None else None


def get_session_by_token_hash(db: Session, token_hash: str) -> WindowSessionRecord | None:
    """按 token 哈希取会话（本方法只接受哈希，不接受明文 token）。"""
    row = db.execute(
        select(WindowSession).where(WindowSession.session_token_hash == token_hash)
    ).scalar_one_or_none()
    return _row_to_session(row) if row is not None else None


def list_active_sessions(
    db: Session, user_id: int, *, exclude_session_id: int | None = None
) -> list[WindowSessionRecord]:
    """用户非终态会话（active/takeover_pending），按 id 升序）。"""
    stmt = select(WindowSession).where(
        WindowSession.user_id == user_id,
        WindowSession.status.in_(("active", "takeover_pending")),
    )
    if exclude_session_id is not None:
        stmt = stmt.where(WindowSession.id != exclude_session_id)
    rows = db.execute(stmt.order_by(WindowSession.id)).scalars().all()
    return [_row_to_session(row) for row in rows]


def create_session(
    db: Session,
    *,
    user_id: int,
    token_hash: str,
    credential_version_at_issue: int,
    expires_at: str,
    status: str = "active",
) -> WindowSessionRecord:
    """创建会话行（部分唯一冲突抛 IdentityConflictError，调用方回滚）。"""
    now = _now()
    row = WindowSession(
        session_token_hash=token_hash,
        user_id=user_id,
        credential_version_at_issue=credential_version_at_issue,
        status=status,
        created_at=now,
        last_seen_at=now,
        expires_at=datetime.fromisoformat(expires_at),
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise IdentityConflictError("会话创建冲突", params={"user_id": user_id}) from exc
    return _row_to_session(row)


def set_session_status(
    db: Session,
    session_id: int,
    status: str,
    *,
    revoked_by: int | None = None,
    replaced_by_session_id: int | None = None,
) -> WindowSessionRecord:
    """切换会话状态；会话缺失抛 UserNotFoundError。"""
    row = db.get(WindowSession, session_id)
    if row is None:
        raise UserNotFoundError("会话不存在", params={"session_id": session_id})
    row.status = status
    if status in ("revoked", "expired"):
        row.revoked_at = _now()
    if revoked_by is not None:
        row.revoked_by = revoked_by
    if replaced_by_session_id is not None:
        row.replaced_by_session_id = replaced_by_session_id
    db.flush()
    return _row_to_session(row)


def heartbeat_session(db: Session, session_id: int) -> WindowSessionRecord:
    """刷新会话 last_seen（高频调用，只 flush 不提交）；缺失抛 UserNotFoundError。"""
    row = db.get(WindowSession, session_id)
    if row is None:
        raise UserNotFoundError("会话不存在", params={"session_id": session_id})
    row.last_seen_at = _now()
    db.flush()
    return _row_to_session(row)


def extend_session(db: Session, session_id: int, *, expires_at: str) -> WindowSessionRecord:
    """续期会话（last_seen 与 expires_at 同步推进）；缺失抛 UserNotFoundError。"""
    row = db.get(WindowSession, session_id)
    if row is None:
        raise UserNotFoundError("会话不存在", params={"session_id": session_id})
    row.last_seen_at = _now()
    row.expires_at = datetime.fromisoformat(expires_at)
    db.flush()
    return _row_to_session(row)


def expire_sessions(db: Session, user_id: int | None = None) -> int:
    """将已过期的非终态会话置 expired，返回数量（不提交）。"""
    stmt = select(WindowSession).where(
        WindowSession.status.in_(("active", "takeover_pending"))
    )
    if user_id is not None:
        stmt = stmt.where(WindowSession.user_id == user_id)
    now = _now()
    count = 0
    for row in db.execute(stmt).scalars():
        expires_at = row.expires_at
        if expires_at is not None:
            aware = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=UTC)
            if aware < now:
                row.status = "expired"
                row.revoked_at = now
                count += 1
    db.flush()
    return count


def get_app_setting(db: Session, key: str) -> AppSettingRecord | None:
    """按 key 取应用设置；缺失返回 None。"""
    row = db.execute(select(AppSetting).where(AppSetting.key == key)).scalar_one_or_none()
    return _row_to_setting(row) if row is not None else None


def set_app_setting(
    db: Session, key: str, value: dict[str, Any], updated_by: int | None = None
) -> AppSettingRecord:
    """写入应用设置（upsert）。"""
    row = db.execute(select(AppSetting).where(AppSetting.key == key)).scalar_one_or_none()
    if row is None:
        row = AppSetting(key=key, value={"value": value}, updated_by=updated_by)
        db.add(row)
    else:
        row.value = {"value": value}
        row.updated_by = updated_by
        row.updated_at = _now()
    db.flush()
    return _row_to_setting(row)


def record_auth_event(
    db: Session,
    *,
    event_type: str,
    user_id: int | None = None,
    session_id: int | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuthEventRecord:
    """追加认证审计事件（仅 INSERT，不提交；ip 由调用方域服务先白名单化）。"""
    row = AuthEvent(
        event_type=event_type,
        user_id=user_id,
        session_id=session_id,
        ip=ip,
        user_agent=user_agent,
        detail=detail,
    )
    db.add(row)
    db.flush()
    return _row_to_auth_event(row)
