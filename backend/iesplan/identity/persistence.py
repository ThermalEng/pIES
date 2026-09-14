"""身份域 SQL repository 实现（users/roles/user_roles/credentials/window_sessions/app_settings/auth_events）。

- 本模块是 `IdentityRepository` 协议的实现，可经 `iesplan.identity` 门面调用；
- 表真相收归本模块（Wave2A 由 iesplan.models.identity 迁入）；绝不 commit/rollback（调用方事务拥有）；
- 唯一冲突转 `IdentityConflictError`；冲突后调用方须回滚会话
  （与旧 services 契约一致；实测 SA 2.0 下 flush 失败后会话不可继续）；
- 密钥材料绝不进入记录：密码哈希只经 `get_active_password_secret`
  以原文字符串返回供验算，永不装入 DTO、不落日志；
- 用户名/邮箱按旧 services 语义规范化（去空格转小写）后查询。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from iesplan.db import (
    Base,
    JSONB,
    InetType,
    bigint_pk,
    drop_trigger_function_sql,
    immutable_revoke_sql,
    immutable_trigger_sql,
    regex_check,
)
from iesplan.identity.contracts import (
    EMAIL_RE,
    USERNAME_RE,
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_EXPIRED,
    SESSION_STATUS_REVOKED,
    SESSION_STATUS_TAKEOVER_PENDING,
    USER_STATUS_ACTIVE,
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
    status: str = USER_STATUS_ACTIVE,
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


def active_credentials_by_user(db: Session, user_ids: list[int]) -> dict[int, CredentialRecord]:
    """多用户当前有效 password 凭证元数据（无哈希），单条 SQL。

    取数口径与单发 get_active_credential 一致（password 类型、未撤销、
    创建时间倒序取首条）；空输入直接返回空映射，不发查询。
    """
    if not user_ids:
        return {}
    rows = (
        db.execute(
            select(Credential)
            .where(
                Credential.user_id.in_(user_ids),
                Credential.credential_type == "password",
                Credential.revoked_at.is_(None),
            )
            .order_by(Credential.created_at.desc())
        )
        .scalars()
        .all()
    )
    grouped: dict[int, CredentialRecord] = {}
    for row in rows:
        if row.user_id not in grouped:
            grouped[row.user_id] = _row_to_credential(row)
    return grouped


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


def roles_by_user(db: Session, user_ids: list[int]) -> dict[int, list[str]]:
    """多用户当前有效角色 code（按角色 id 升序），单条 SQL。

    空输入直接返回空映射，不发查询。
    """
    if not user_ids:
        return {}
    rows = db.execute(
        select(UserRole.user_id, Role.code, Role.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id.in_(user_ids), UserRole.revoked_at.is_(None))
        .order_by(Role.id)
    ).all()
    grouped: dict[int, list[str]] = {}
    for user_id, code, _role_id in rows:
        grouped.setdefault(user_id, []).append(code)
    return grouped


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
        WindowSession.status.in_((SESSION_STATUS_ACTIVE, SESSION_STATUS_TAKEOVER_PENDING)),
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
    status: str = SESSION_STATUS_ACTIVE,
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
    if status in (SESSION_STATUS_REVOKED, SESSION_STATUS_EXPIRED):
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
        WindowSession.status.in_((SESSION_STATUS_ACTIVE, SESSION_STATUS_TAKEOVER_PENDING))
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
                row.status = SESSION_STATUS_EXPIRED
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


# ---------------------------------------------------------------------------
# ORM 表定义: Wave2A 由 iesplan.models.identity 迁入, 表真相归本域所有。
# ---------------------------------------------------------------------------

class User(Base):
    """用户账号(01 §1.1)。

    生命周期状态: active 正常 / disabled 停用 / locked 锁定;删除一律软删。
    新增 public_namespace：系统分配的 12 位小写 Crockford Base32（60 bit 熵），
    首次需要时分配，终身不变、不可转让、不复用；不包含个人信息；账号改名/停用/重启用不改变。
    """

    __tablename__ = "users"

    id: Mapped[int] = bigint_pk()
    username: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    locale: Mapped[str] = mapped_column(Text, nullable=False, server_default="zh-CN")
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="Asia/Shanghai")
    fixed_utc_offset_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa.text("480")
    )
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa.text("false"))
    #: 外部认证主体(OIDC sub; 仅外部认证账号非空, 唯一约束防重复绑定)
    auth_subject: Mapped[str | None] = mapped_column(Text)
    #: 公开命名空间（12 位小写 Crockford Base32，全局唯一，首次需要时分配）
    public_namespace: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("username = lower(username)", name="ck_users_username_lower"),
        regex_check(f"username ~ '{USERNAME_RE}'", name="ck_users_username_format"),
        regex_check(f"email IS NULL OR email ~ '{EMAIL_RE}'", name="ck_users_email_format"),
        CheckConstraint("status IN ('active','disabled','locked')", name="ck_users_status"),
        CheckConstraint("fixed_utc_offset_minutes BETWEEN -720 AND 840", name="ck_users_utc_offset"),
        regex_check(
            "public_namespace IS NULL OR public_namespace ~ '^[0-9a-hjkmnp-tv-z]{12}$'",
            name="ck_users_public_namespace",
        ),
        UniqueConstraint("username", name="uq_users_username"),
        UniqueConstraint("email", name="uq_users_email"),
        UniqueConstraint("auth_subject", name="uq_users_auth_subject"),
        Index("idx_users_status", "status"),
        Index("uq_users_public_namespace", "public_namespace", unique=True,
              postgresql_where=sa.text("public_namespace IS NOT NULL"),
              sqlite_where=sa.text("public_namespace IS NOT NULL")),
    )


class Role(Base):
    """全局角色(01 §1.2)。"""

    __tablename__ = "roles"

    id: Mapped[int] = bigint_pk()
    code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa.text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        regex_check("code ~ '^[a-z_]{1,32}$'", name="ck_roles_code_format"),
        UniqueConstraint("code", name="uq_roles_code"),
    )


class UserRole(Base):
    """用户-角色授权(追加式历史, 01 §1.3)。"""

    __tablename__ = "user_roles"

    id: Mapped[int] = bigint_pk()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False)
    granted_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (
        UniqueConstraint("user_id", "role_id", "granted_at", name="uq_user_roles_grant"),
        Index(
            "uq_user_roles_current",
            "user_id",
            "role_id",
            unique=True,
            postgresql_where=sa.text("revoked_at IS NULL"),
            sqlite_where=sa.text("revoked_at IS NULL"),
        ),
        Index("idx_user_roles_role", "role_id"),
        Index("idx_user_roles_user", "user_id"),
    )


class Credential(Base):
    """凭证(哈希、强度、首次改密;不可变——只 INSERT, 01 §1.4)。"""

    __tablename__ = "credentials"

    id: Mapped[int] = bigint_pk()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    credential_type: Mapped[str] = mapped_column(Text, nullable=False)
    secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm: Mapped[str | None] = mapped_column(Text)
    cost_params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'"))
    strength_score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    requires_change: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa.text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (
        CheckConstraint(
            "credential_type IN ('password','totp','webauthn','recovery_code')",
            name="ck_credentials_type",
        ),
        CheckConstraint("strength_score BETWEEN 0 AND 100", name="ck_credentials_strength"),
        Index(
            "uq_credentials_active_password",
            "user_id",
            unique=True,
            postgresql_where=sa.text("credential_type = 'password' AND revoked_at IS NULL"),
            sqlite_where=sa.text("credential_type = 'password' AND revoked_at IS NULL"),
        ),
        Index("idx_credentials_user", "user_id", "revoked_at"),
    )


class WindowSession(Base):
    """浏览器会话(单点登录, 01 §1.5)。"""

    __tablename__ = "window_sessions"

    id: Mapped[int] = bigint_pk()
    session_token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    credential_version_at_issue: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    replaced_by_session_id: Mapped[int | None] = mapped_column(ForeignKey("window_sessions.id"))
    ip: Mapped[str | None] = mapped_column(InetType)
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (
        CheckConstraint(
            "status IN ('active','takeover_pending','revoked','expired')",
            name="ck_window_sessions_status",
        ),
        CheckConstraint(
            "replaced_by_session_id IS NULL OR replaced_by_session_id <> id",
            name="ck_window_sessions_no_self_replace",
        ),
        UniqueConstraint("session_token_hash", name="uq_window_sessions_token"),
        Index(
            "uq_window_sessions_one_active",
            "user_id",
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
            sqlite_where=sa.text("status = 'active'"),
        ),
        Index(
            "uq_window_sessions_one_pending",
            "user_id",
            unique=True,
            postgresql_where=sa.text("status = 'takeover_pending'"),
            sqlite_where=sa.text("status = 'takeover_pending'"),
        ),
        Index("idx_window_sessions_user", "user_id", "status"),
    )


class AppSetting(Base):
    """应用级键值设置(身份/安全域, 如自助注册开关)。

    多 worker 一致性的权威来源(M-12): 注册开关等设置落库,
    所有 Worker 从同一来源读取, 避免进程内存态在多 Worker 间不一致。
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'"))
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class AuthEvent(Base):
    """身份认证审计(不可变, 仅 INSERT, 01 §1.6)。"""

    __tablename__ = "auth_events"

    id: Mapped[int] = bigint_pk()
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[int | None] = mapped_column(ForeignKey("window_sessions.id"))
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    ip: Mapped[str | None] = mapped_column(InetType)
    user_agent: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('login_success','login_failure','logout','password_change',"
            "'credential_reset','account_disabled','session_takeover','permission_change',"
            "'role_change','session_revoke','maintenance')",
            name="ck_auth_events_type",
        ),
        Index("idx_auth_events_user", "user_id", sa.text("occurred_at DESC")),
        Index("idx_auth_events_type", "event_type", sa.text("occurred_at DESC")),
        Index("idx_auth_events_time", "occurred_at"),
    )


#: 本域拥有的不可变表(仅 INSERT, 禁止 UPDATE/DELETE)
IMMUTABLE_TABLES: tuple[str, ...] = ("auth_events",)


def install_tables() -> None:
    """公开安装钩子: 导入本模块即完成 Base.metadata 表注册; 幂等, 无其他副作用。"""
    return None


def install_triggers() -> tuple[str, ...]:
    """公开钩子: 返回本域触发器部署语句(按执行序, 含幂等 DROP, 供组合根编排收集)。"""
    statements = [drop_trigger_function_sql(f"tg_{table}_immutable") for table in IMMUTABLE_TABLES]
    statements.extend(immutable_trigger_sql(table) for table in IMMUTABLE_TABLES)
    statements.extend(immutable_revoke_sql(table) for table in IMMUTABLE_TABLES)
    return tuple(statements)
