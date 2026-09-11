"""身份域公开门面（用户/角色/授权/凭证/会话/设置/认证事件表，归属 identity）。

外部只允许经本门面消费 contract 与 repository 协议；不得导入本域
repository 实现（切片 4 落实）、`iesplan.models` 或 services。
本门面不导出任何密钥材料（无 secret_hash/token 明文）。
"""

from __future__ import annotations

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
from iesplan.identity.repository import IdentityRepository

__all__ = [
    "AppSettingRecord",
    "AuthEventRecord",
    "CredentialRecord",
    "IdentityConflictError",
    "IdentityRepository",
    "RoleRecord",
    "UserNotFoundError",
    "UserRecord",
    "UserRoleRecord",
    "WindowSessionRecord",
]
