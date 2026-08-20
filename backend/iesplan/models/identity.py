"""身份域模型(U01 身份写入单元): users / roles / user_roles / credentials / window_sessions / auth_events。

对应 01-db-schema.md 第1节。
"""

from __future__ import annotations

from datetime import datetime

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
)
from sqlalchemy.orm import Mapped, mapped_column

from iesplan.db import Base
from iesplan.models.common import EMAIL_RE, JSONB, USERNAME_RE, InetType, bigint_pk, regex_check


class User(Base):
    """用户账号(01 §1.1)。

    生命周期状态: active 正常 / disabled 停用 / locked 锁定;删除一律软删。
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
        UniqueConstraint("username", name="uq_users_username"),
        UniqueConstraint("email", name="uq_users_email"),
        UniqueConstraint("auth_subject", name="uq_users_auth_subject"),
        Index("idx_users_status", "status"),
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
