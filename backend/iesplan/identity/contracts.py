"""身份域公开契约(用户/角色/授权/凭证/会话/设置/认证事件表，归属 identity)。

- 绝不携带密钥材料：无 secret_hash、无 cost_params、无 session_token_hash；
  凭证只暴露元数据，token 只以哈希形态进入 repository（见 repository.py）；
- 只含不可变值对象与领域错误；不导入 ORM、Session、services 或 application。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from iesplan.core.errors import ConflictError, NotFoundError


class UserNotFoundError(NotFoundError):
    """用户/角色/会话/设置不存在（沿用基类诊断码，不新增码）。"""


class IdentityConflictError(ConflictError):
    """身份唯一冲突或状态冲突（沿用基类诊断码，不新增码）。"""


@dataclass(frozen=True, slots=True)
class UserRecord:
    """用户账号公开视图（users 表；软删以 status 表达）。"""

    id: int
    username: str
    display_name: str
    email: str | None = None
    status: str = "active"
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    fixed_utc_offset_minutes: int = 480
    credential_version: int = 0
    is_system: bool = False
    auth_subject: str | None = None
    public_namespace: str | None = None
    last_login_at: str | None = None


@dataclass(frozen=True, slots=True)
class CredentialRecord:
    """凭证元数据（credentials 表；绝不含 secret_hash）。"""

    id: int
    user_id: int
    credential_type: str
    algorithm: str | None = None
    strength_score: int = 0
    requires_change: bool = True
    expires_at: str | None = None
    revoked_at: str | None = None


@dataclass(frozen=True, slots=True)
class RoleRecord:
    """全局角色（roles 表公开视图）。"""

    id: int
    code: str
    name: str
    description: str | None = None
    is_system: bool = False


@dataclass(frozen=True, slots=True)
class UserRoleRecord:
    """用户-角色授权行（user_roles 表公开视图，追加式历史）。"""

    id: int
    user_id: int
    role_id: int
    granted_by: int
    revoked_at: str | None = None
    revoked_by: int | None = None


@dataclass(frozen=True, slots=True)
class WindowSessionRecord:
    """浏览器会话公开视图（window_sessions 表；绝不含 token 哈希）。"""

    id: int
    user_id: int
    status: str
    credential_version_at_issue: int
    replaced_by_session_id: int | None = None
    last_seen_at: str | None = None
    expires_at: str | None = None
    revoked_at: str | None = None
    revoked_by: int | None = None


@dataclass(frozen=True, slots=True)
class AppSettingRecord:
    """应用级键值设置（app_settings 表公开视图）。"""

    key: str
    value: dict[str, Any]
    updated_by: int | None = None


@dataclass(frozen=True, slots=True)
class AuthEventRecord:
    """身份认证审计事件（auth_events 表公开视图，仅 INSERT）。"""

    id: int
    user_id: int | None
    session_id: int | None
    event_type: str
    occurred_at: str | None = None
    detail: dict[str, Any] | None = None
