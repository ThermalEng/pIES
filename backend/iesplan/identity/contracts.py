"""身份域公开契约(用户/角色/授权/凭证/会话/设置/认证事件表，归属 identity)。

- 绝不携带密钥材料：无 secret_hash、无 cost_params、无 session_token_hash；
  凭证只暴露元数据，token 只以哈希形态进入 repository（见 repository.py）；
- 只含不可变值对象与领域错误；不导入 ORM、Session、services 或 application。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from iesplan.core.errors import AppError, ConflictError, NotFoundError


class ExternalAuthError(AppError):
    """外部认证失败(配置/提供方/令牌校验错误)。

    由 services/external_auth.py 收敛至本域(纠偏 Wave 1 切片 C);
    语义与旧实现一致(HTTP 400, 前端经 message_key 渲染)。
    """

    code = "AUTH-OIDC-001"
    http_status = 400
    severity = "error"
    message_key = "ies.diag.auth.external_failed"

    def __init__(self, message: str = "外部认证失败", **params: Any) -> None:
        # http_status 为类属性, 不得传给 AppError 构造函数
        super().__init__(
            message, code=self.code, severity=self.severity,
            message_key=self.message_key, params=params,
        )


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


#: 用户状态取值唯一权威(users.status; 应用层与 persistence 经此复用, 不各自写字面量)。
USER_STATUS_ACTIVE = "active"
USER_STATUS_DISABLED = "disabled"

#: 会话状态取值唯一权威(window_sessions.status)。
SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_TAKEOVER_PENDING = "takeover_pending"
SESSION_STATUS_REVOKED = "revoked"
SESSION_STATUS_EXPIRED = "expired"


# ---------------------------------------------------------------------------
# 认证业务异常(message_key 前缀 ies.diag.auth.*; http_status 供全局处理器映射)
#
# 身份错误归 identity 域所有(后端解耦 Wave 3-A: 由 application.identity 搬入,
# 行为与诊断码不变; 应用层经本模块或应用门面复用, 不重定义)。
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


class DeleteConfirmRequiredError(BadRequestError):
    """删除账号缺少确认或确认令牌无效(400)。

    误操作防护(0.2.0 B1): 删除账号会级联软删其拥有的全部项目且不可恢复,
    必须在预览后携带签名确认令牌显式确认。错误可能原因:
    - 未携带 confirm=true;
    - 确认令牌缺失/过期/被篡改;
    - 预览后目标用户拥有的项目清单发生变化(令牌与当前清单不一致)。
    """

    code = "AUTH-DEL-001"
    message_key = "ies.diag.auth.delete_confirm_required"
