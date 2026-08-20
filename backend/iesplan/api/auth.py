"""身份与认证 API(U01): /api/auth 路由。

- 窗口凭证: 登录返回 token(同时写入 HttpOnly Cookie `ies_session`),
  后续请求可从 ``Authorization: Bearer <token>`` 或 Cookie 读取;
- 依赖: get_current_user / get_current_admin(供其他业务单元复用);
- 错误统一 AppError + 诊断 message_key(ies.diag.auth.*), 响应不泄露堆栈/哈希;
- 自助注册开关默认关闭, 持久化到数据库(app_settings, 修复 M-12 多 Worker
  不一致), 由管理员 PUT /api/auth/settings 切换;
- 外部认证(OIDC/SSO): IESPLAN_AUTH_PROVIDER=oidc 时登录页展示 SSO 入口,
  回调经 services.external_auth(标准实现 Authlib)完成令牌交换与账号绑定。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from iesplan.config import settings
from iesplan.core.errors import ForbiddenError, NotFoundError
from iesplan.db import get_db
from iesplan.models.identity import User, WindowSession
from iesplan.services import identity
from iesplan.services.external_auth import ExternalAuthError

#: 会话 Cookie 名
SESSION_COOKIE_NAME = "ies_session"

#: 强制改密门禁(C-02)豁免路径: 仅允许改密/登出/本人信息
_FPC_ALLOWED_PATHS: frozenset[str] = frozenset(
    {"/api/auth/change-password", "/api/auth/logout", "/api/auth/me"}
)
#: 待接管(pending)会话允许的路径(H-01): 确认接管 + 强制改密门禁豁免路径
#: (避免强制改密用户被 pending 状态卡死: 可先改密或登出, 再重新登录)
_PENDING_ALLOWED_PATHS: frozenset[str] = frozenset(
    _FPC_ALLOWED_PATHS | {"/api/auth/confirm-takeover"}
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ---------------------------------------------------------------------------
# 公开设置(登录页无需认证即可感知注册开关/SSO 入口)
# ---------------------------------------------------------------------------


class PublicSettings(BaseModel):
    """登录页可见的公开设置(不泄露任何内部细节)。"""

    registration_enabled: bool
    sso_enabled: bool
    sso_provider_name: str = ""


def public_settings(db: Session) -> PublicSettings:
    """公开设置: 注册开关(数据库权威值) + 外部认证入口。"""
    from iesplan.services import external_auth

    return PublicSettings(
        registration_enabled=identity.registration_enabled(db),
        sso_enabled=external_auth.is_oidc_enabled(),
        sso_provider_name="OIDC" if external_auth.is_oidc_enabled() else "",
    )


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    """登录请求。"""

    username: str
    password: str
    device: str | None = None  # 窗口/设备标识(仅审计与提示用)


class RegisterRequest(BaseModel):
    """自助注册请求(仅工程师角色)。"""

    username: str
    password: str
    display_name: str | None = None
    email: str | None = None


class ChangePasswordRequest(BaseModel):
    """修改密码请求。"""

    old_password: str
    new_password: str


class ResetPasswordRequest(BaseModel):
    """管理员重置密码请求(临时密码)。"""

    new_password: str


class SettingsUpdate(BaseModel):
    """安全设置更新。"""

    registration_enabled: bool

class UserOut(BaseModel):
    """用户信息(不含任何敏感字段)。"""

    id: int
    username: str
    display_name: str
    role: str  # 主角色(admin 优先, 其次按授权顺序取第一个)
    status: str
    force_password_change: bool
    credential_version: int
    last_login_at: datetime | None = None


class UsersListResponse(BaseModel):
    """用户列表响应。"""

    users: list[UserOut]


class AuthResponse(BaseModel):
    """登录/接管响应: 窗口凭证 + 用户信息。"""

    token: str
    token_type: str = "bearer"
    user: UserOut
    needs_takeover_confirm: bool = False


# ---------------------------------------------------------------------------
# 认证上下文依赖
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthContext:
    """已认证请求上下文: 当前用户 + 其活动会话 + 数据库会话。"""

    db: Session
    user: User
    session: WindowSession


#: 数据库会话依赖别名(Annotated 风格, 规避 B008)
DbSession = Annotated[Session, Depends(get_db)]


def _extract_token(request: Request) -> str | None:
    """从 Authorization: Bearer 头或 Cookie 中提取窗口凭证原文。"""
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip() or None
    return request.cookies.get(SESSION_COOKIE_NAME)


def _client_ip(request: Request) -> str | None:
    """客户端 IP(代理部署时由反向代理注入 X-Forwarded-For, 后续阶段可扩展)。"""
    return request.client.host if request.client else None


def _pkce_verifier() -> str:
    """生成 PKCE code_verifier(S256 要求 43-128 字符)。"""
    return secrets.token_urlsafe(48)[:64]


def get_auth_context(request: Request, db: DbSession) -> AuthContext:
    """解析窗口凭证并校验: 哈希匹配 + 状态 + 未过期 + 凭证版本一致 + 用户有效。

    安全门禁(校验失败抛对应异常):
    - H-01: takeover_pending 会话仅允许确认接管/改密/登出/本人信息路径,
      其余业务请求一律 401(SessionInvalidError, params.reason=takeover_pending);
    - C-02: 有效密码凭证 requires_change=True 时, 除改密/登出/本人信息外
      全部业务请求返回 403(AUTH-FPC-001, 强制改密未解除前无业务权限)。

    任一次校验通过都会顺带刷新 last_seen_at(会话活跃时间)。
    """
    token = _extract_token(request)
    if not token:
        raise identity.AuthRequiredError()
    session = identity.get_session_by_token(db, token)
    if session is None:
        raise identity.SessionInvalidError()
    now = identity.utcnow()
    path = request.url.path
    if session.status == "takeover_pending":
        # H-01: 接管确认前新会话不拥有业务权限; 仅放行接管/改密/登出/本人信息
        if path not in _PENDING_ALLOWED_PATHS:
            raise identity.SessionInvalidError(params={"reason": "takeover_pending"})
    elif session.status != "active":
        raise identity.SessionInvalidError()
    expires_at = identity.as_utc(session.expires_at)
    if expires_at is not None and expires_at < now:
        # 已过期: 置终态(系统自动过期, 无操作者)
        session.status = "expired"
        session.revoked_at = now
        db.commit()
        raise identity.SessionInvalidError()
    user = db.get(User, session.user_id)
    if user is None or user.status != "active":
        raise identity.SessionInvalidError()
    if session.credential_version_at_issue != user.credential_version:
        # 凭证已轮换(改密/重置): 旧会话立即失效(RPD 3.4 凭证失效机制)
        session.status = "revoked"
        session.revoked_at = now
        session.revoked_by = user.id
        db.commit()
        raise identity.SessionInvalidError()
    # C-02: 强制改密门禁(服务端统一执行, 不依赖前端配合)
    cred = identity.get_active_password_credential(db, user)
    if cred is not None and cred.requires_change and path not in _FPC_ALLOWED_PATHS:
        raise identity.ForcePasswordChangeError(
            params={"hint": "首次登录请先修改初始密码, 未改密前仅可使用改密/登出/本人信息接口"}
        )
    session.last_seen_at = now
    db.commit()
    return AuthContext(db=db, user=user, session=session)


def get_current_user(ctx: AuthCtx) -> User:
    """依赖: 当前已认证用户(其他业务单元复用)。"""
    return ctx.user


def get_current_admin(ctx: AuthCtx) -> User:
    """依赖: 当前管理员(无管理员角色抛 403)。"""
    if not identity.has_role(ctx.db, ctx.user, "admin"):
        raise ForbiddenError()
    return ctx.user


#: 认证上下文 / 当前用户 / 当前管理员依赖别名(须在依赖函数定义后声明)
AuthCtx = Annotated[AuthContext, Depends(get_auth_context)]
CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentAdmin = Annotated[User, Depends(get_current_admin)]


def _require_admin(ctx: AuthContext) -> User:
    """便捷校验: 当前用户须为管理员, 否则抛 403(与 get_current_admin 同逻辑)。"""
    return get_current_admin(ctx)


def _user_out(db: Session, user: User) -> UserOut:
    """构造用户响应(角色取 admin 优先; force_password_change 来自有效凭证)。"""
    roles = identity.user_roles(db, user)
    cred = identity.get_active_password_credential(db, user)
    return UserOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role="admin" if "admin" in roles else (roles[0] if roles else ""),
        status=user.status,
        force_password_change=bool(cred is not None and cred.requires_change),
        credential_version=user.credential_version,
        last_login_at=user.last_login_at,
    )


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    """写入窗口凭证安全 Cookie(HttpOnly + SameSite=Lax, HTTPS 下加 Secure)。"""
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=settings.session_ttl_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


# ---------------------------------------------------------------------------
# 公开端点: 登录 / 登出 / 改密 / 续期 / 接管 / 注册
# ---------------------------------------------------------------------------


@router.post("/login", response_model=AuthResponse, summary="登录(签发窗口凭证)")
def login(
    req: LoginRequest,
    request: Request,
    response: Response,
    db: DbSession,
) -> AuthResponse:
    """登录: 校验密码 + 登录限速, 创建单活动窗口会话。

    若该账号已有活动窗口, 旧窗口被撤销、新会话以 takeover_pending 创建
    (确认接管前无业务权限), 响应 needs_takeover_confirm=True,
    前端提示确认接管(RPD 3.3 / H-01)。
    """
    ip = _client_ip(request)
    ua = request.headers.get("user-agent")
    user, error_code = identity.authenticate(
        db, req.username, req.password, ip=ip, user_agent=ua, device=req.device
    )
    if error_code is not None:
        if error_code == "locked":
            raise identity.LockedError()
        raise identity.LoginFailedError()
    session, token, displaced = identity.create_window_session(db, user, req.device, ip=ip, user_agent=ua)
    _set_session_cookie(response, request, token)
    return AuthResponse(token=token, user=_user_out(db, user), needs_takeover_confirm=displaced)


@router.post("/logout", summary="登出(撤销当前窗口会话)")
def logout(request: Request, response: Response, ctx: AuthCtx) -> dict:
    """登出: 撤销当前窗口凭证、清除浏览器 Cookie 并写 logout 审计。"""
    identity.revoke_session(
        ctx.db,
        ctx.user,
        ctx.session,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        reason="logout",
    )
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/change-password", summary="修改密码(首登强制改密)")
def change_password(
    req: ChangePasswordRequest,
    request: Request,
    ctx: AuthCtx,
) -> dict:
    """修改密码: 校验旧密码与新密码强度。

    成功后凭证版本递增, 当前及全部旧会话失效, 前端须重新登录。
    """
    identity.change_password(
        ctx.db,
        ctx.user,
        req.old_password,
        req.new_password,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return {"ok": True, "message_key": "ies.diag.auth.password_changed"}


@router.post("/refresh", summary="会话续期")
def refresh(request: Request, ctx: AuthCtx) -> dict:
    """会话续期: 按 TTL 顺延过期时刻, 返回新的过期时间。"""
    expires_at = identity.extend_session(ctx.db, ctx.session)
    return {"ok": True, "expires_at": expires_at}


@router.get("/me", response_model=UserOut, summary="当前登录用户(页面刷新恢复会话)")
def me(ctx: AuthCtx) -> UserOut:
    """返回当前会话对应的用户信息(前端刷新后恢复登录态)。"""
    return _user_out(ctx.db, ctx.user)


@router.post("/confirm-takeover", response_model=AuthResponse, summary="确认接管")
def confirm_takeover(
    request: Request,
    response: Response,
    ctx: AuthCtx,
) -> AuthResponse:
    """确认接管(RPD 3.3 / 01 §1.5): 当前待接管会话直接转为 active。

    不轮换凭证: 客户端既有 Cookie/Bearer 凭证即为最终凭证, 确认后立即
    拥有业务权限(其余 pending/active 会话被撤销)。返回当前窗口凭证。
    """
    token = _extract_token(request) or ""
    identity.confirm_takeover(
        ctx.db,
        ctx.user,
        ctx.session,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_session_cookie(response, request, token)
    return AuthResponse(token=token, user=_user_out(ctx.db, ctx.user), needs_takeover_confirm=False)


@router.post("/register", response_model=UserOut, summary="自助注册(默认关闭, 仅工程师)")
def register(req: RegisterRequest, request: Request, db: DbSession) -> UserOut:
    """自助注册: 注册开关开启时可用, 只能创建 engineer 角色(RPD 3.1)。"""
    if not identity.registration_enabled(db):
        raise identity.RegistrationDisabledError()
    user = identity.create_user(
        db,
        req.username,
        req.password,
        role="engineer",
        force_password_change=False,
        display_name=req.display_name,
        email=req.email,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return _user_out(db, user)


# ---------------------------------------------------------------------------
# 管理员端点: 用户管理 / 安全设置
# ---------------------------------------------------------------------------


@router.get("/users", response_model=UsersListResponse, summary="用户列表(管理员)")
def list_users(db: DbSession, admin: CurrentAdmin) -> UsersListResponse:
    """用户列表(管理员): 含停用账号, 返回角色与强制改密状态。"""
    users = identity.list_users(db)
    return UsersListResponse(users=[_user_out(db, u) for u in users])


@router.post("/users/{user_id}/reset-password", summary="重置密码(管理员)")
def admin_reset_password(
    user_id: int,
    req: ResetPasswordRequest,
    request: Request,
    ctx: AuthCtx,
) -> dict:
    """管理员重置密码: 签发临时密码(强制改密), 使目标用户全部会话失效。"""
    _require_admin(ctx)
    target = identity.get_user_by_id(ctx.db, user_id)
    if target is None:
        raise NotFoundError("", params={"object_type": "user", "id": user_id})
    identity.reset_password(
        ctx.db,
        ctx.user,
        target,
        req.new_password,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return {"ok": True, "message_key": "ies.diag.auth.password_reset"}


@router.post("/users/{user_id}/deactivate", summary="停用用户(管理员)")
def admin_deactivate_user(user_id: int, request: Request, ctx: AuthCtx) -> dict:
    """停用用户(管理员): 账号禁止登录, 全部会话立即失效。"""
    _require_admin(ctx)
    target = identity.get_user_by_id(ctx.db, user_id)
    if target is None:
        raise NotFoundError("", params={"object_type": "user", "id": user_id})
    identity.deactivate_user(
        ctx.db,
        ctx.user,
        target,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return {"ok": True}


@router.post("/users/{user_id}/reactivate", summary="重新启用用户(管理员)")
def admin_reactivate_user(user_id: int, request: Request, ctx: AuthCtx) -> dict:
    """重新启用用户(管理员)。"""
    _require_admin(ctx)
    target = identity.get_user_by_id(ctx.db, user_id)
    if target is None:
        raise NotFoundError("", params={"object_type": "user", "id": user_id})
    identity.reactivate_user(
        ctx.db,
        ctx.user,
        target,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return {"ok": True}


@router.delete("/users/{user_id}", summary="删除账号(管理员, 级联删除其项目)")
def admin_delete_user(user_id: int, request: Request, ctx: AuthCtx) -> dict:
    """删除账号(管理员): 该账号拥有的项目一并删除。

    - 不能删除自己 / 系统账号;
    - 目标账号置 disabled, 全部会话/凭证撤销, 其拥有的项目全部软删。
    """
    _require_admin(ctx)
    target = identity.get_user_by_id(ctx.db, user_id)
    if target is None:
        raise NotFoundError("", params={"object_type": "user", "id": user_id})
    result = identity.delete_user(
        ctx.db,
        ctx.user,
        target,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return {"ok": True, **result}


@router.put("/settings", summary="更新安全设置(管理员)")
def update_settings(payload: SettingsUpdate, request: Request, ctx: AuthCtx) -> dict:
    """更新安全设置: 自助注册开关(默认关闭, 持久化到数据库, 多 Worker 一致)。"""
    _require_admin(ctx)
    identity.set_registration_enabled(ctx.db, payload.registration_enabled, updated_by=ctx.user.id)
    identity.record_auth_event(
        ctx.db,
        "maintenance",
        user_id=ctx.user.id,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        detail={"action": "registration_toggle", "registration_enabled": payload.registration_enabled},
    )
    ctx.db.commit()
    return {"registration_enabled": identity.registration_enabled(ctx.db)}


@router.get("/settings", summary="读取安全设置(管理员)")
def read_settings(ctx: AuthCtx) -> dict:
    """读取安全设置(管理员)。"""
    _require_admin(ctx)
    return {"registration_enabled": identity.registration_enabled(ctx.db)}


# ---------------------------------------------------------------------------
# 公开端点: 登录页感知(注册开关 / SSO 入口) + OIDC 单点登录
# ---------------------------------------------------------------------------


@router.get("/public-settings", response_model=PublicSettings, summary="公开设置(登录页)")
def get_public_settings(db: DbSession) -> PublicSettings:
    """登录页公开设置: 自助注册开关与外部认证(SSO)入口状态。

    无需认证(登录页渲染前置条件); 仅暴露登录页需要的布尔与显示名,
    不泄露任何内部配置细节。
    """
    return public_settings(db)


@router.get("/oidc/login", summary="外部认证(SSO)登录入口")
def oidc_login(request: Request, db: DbSession) -> RedirectResponse:
    """跳转 OIDC 提供方授权页(PKCE + state 防 CSRF)。

    state 为签名令牌(含 nonce 与 PKCE verifier, 360s 窗口), 回调时校验。
    """
    from iesplan.services import external_auth

    if not external_auth.is_oidc_enabled():
        raise NotFoundError("", params={"object_type": "auth_provider"})
    nonce = secrets.token_urlsafe(24)
    verifier = _pkce_verifier()
    state = external_auth.build_state(nonce, verifier)
    url = external_auth.build_authorization_url(state)
    # 回调完成前由签名 state 携带 nonce/verifier(无状态, 多 Worker 可用)
    return RedirectResponse(url)


@router.get("/oidc/callback", summary="OIDC 回调(令牌交换)")
def oidc_callback(
    request: Request,
    response: Response,
    db: DbSession,
    code: str = "",
    state: str = "",
) -> RedirectResponse:
    """OIDC 提供方回调: 校验 state → 交换令牌 → 账号绑定 → 签发窗口会话。

    成功: 建立浏览器会话并 302 回首页(带 ies_session Cookie);
    失败: 302 回登录页并携带 error 提示(不泄露提供方细节)。
    """
    from iesplan.services import external_auth

    if not external_auth.is_oidc_enabled():
        raise NotFoundError("", params={"object_type": "auth_provider"})
    ip = _client_ip(request)
    ua = request.headers.get("user-agent")
    try:
        payload = external_auth.verify_state(state)
        claims = external_auth.exchange_code(code, payload["verifier"])
    except ExternalAuthError as exc:
        identity.record_auth_event(
            db, "login_failure", ip=ip, user_agent=ua,
            detail={"reason": exc.params.get("reason", "oidc_failed")},
        )
        db.commit()
        return RedirectResponse(f"/login?error=oidc_failed", status_code=302)
    user = external_auth.provision_user(db, claims, ip=ip, user_agent=ua)
    db.flush()
    session, token, displaced = identity.create_window_session(
        db, user, "oidc", ip=ip, user_agent=ua
    )
    _set_session_cookie(response, request, token)
    db.commit()
    return RedirectResponse("/", status_code=302)
