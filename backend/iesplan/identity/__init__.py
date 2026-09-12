"""身份域公开门面（用户/角色/授权/凭证/会话/设置/认证事件表，归属 identity）。

外部只允许经本门面消费 contract、repository 协议与 repository 实现函数；
不得导入 `iesplan.models`、services 或其他域的内部模块。
本门面不导出任何密钥材料（无 secret_hash/token 明文）。
"""

from __future__ import annotations

from iesplan.identity import persistence
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

add_credential = persistence.add_credential
bind_auth_subject = persistence.bind_auth_subject
bump_credential_version = persistence.bump_credential_version
create_session = persistence.create_session
create_user = persistence.create_user
ensure_role = persistence.ensure_role
expire_sessions = persistence.expire_sessions
extend_session = persistence.extend_session
get_active_credential = persistence.get_active_credential
get_active_password_secret = persistence.get_active_password_secret
get_app_setting = persistence.get_app_setting
get_session = persistence.get_session
get_session_by_token_hash = persistence.get_session_by_token_hash
get_user = persistence.get_user
get_user_by_auth_subject = persistence.get_user_by_auth_subject
get_user_by_namespace = persistence.get_user_by_namespace
get_user_by_email = persistence.get_user_by_email
get_user_by_username = persistence.get_user_by_username
grant_role = persistence.grant_role
heartbeat_session = persistence.heartbeat_session
list_active_sessions = persistence.list_active_sessions
list_users = persistence.list_users
record_auth_event = persistence.record_auth_event
revoke_credentials = persistence.revoke_credentials
revoke_role = persistence.revoke_role
set_app_setting = persistence.set_app_setting
set_public_namespace = persistence.set_public_namespace
set_session_status = persistence.set_session_status
set_user_status = persistence.set_user_status
touch_login = persistence.touch_login
user_roles = persistence.user_roles

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
    "add_credential",
    "bind_auth_subject",
    "bump_credential_version",
    "create_session",
    "create_user",
    "ensure_role",
    "expire_sessions",
    "extend_session",
    "get_active_credential",
    "get_active_password_secret",
    "get_app_setting",
    "get_session",
    "get_session_by_token_hash",
    "get_user",
    "get_user_by_auth_subject",
    "get_user_by_namespace",
    "get_user_by_email",
    "get_user_by_username",
    "grant_role",
    "heartbeat_session",
    "list_active_sessions",
    "list_users",
    "record_auth_event",
    "revoke_credentials",
    "revoke_role",
    "set_app_setting",
    "set_public_namespace",
    "set_session_status",
    "set_user_status",
    "touch_login",
    "user_roles",
]
