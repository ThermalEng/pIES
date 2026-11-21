"""身份与认证 API(U01): /api/auth 路由。

- 窗口凭证: 登录返回 token(同时写入 HttpOnly Cookie `ies_session`),
  后续请求可从 ``Authorization: Bearer <token>`` 或 Cookie 读取;
- 依赖: get_current_user / get_current_admin(供其他业务单元复用);
- 错误统一 AppError + 诊断 message_key(ies.diag.auth.*), 响应不泄露堆栈/哈希;
- 自助注册开关默认关闭, 持久化到数据库(app_settings, 修复 M-12 多 Worker
  不一致), 由管理员 PUT /api/auth/settings 切换;
- 外部认证(OIDC/SSO): IESPLAN_AUTH_PROVIDER=oidc 时登录页展示 SSO 入口,
  回调经 application.identity 用例门面(底经 identity 域 OIDC 能力,
  标准实现 Authlib)完成令牌交换与账号绑定。

传输适配说明: 每个业务动作只转交 application.identity 中的一个完整
用例; Cookie/token 提取、FastAPI dependency、HTTP 重定向/Cookie 写入归
本层, 身份状态机、账号读写、跨域计数与事务归用例。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from iesplan.api.deps import get_request_db
from iesplan.application import identity
from iesplan.config import settings
from iesplan.core.errors import ForbiddenError
from iesplan.identity.contracts import UserRecord, WindowSessionRecord

#: 会话 Cookie 名
SESSION_COOKIE_NAME = "ies_session"

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
    """公开设置: 注册开关(数据库权威值) + 外部认证入口(经 application 门面)。"""
    registration_enabled, sso_enabled, sso_provider_name = identity.get_public_auth_settings(db)
    return PublicSettings(
        registration_enabled=registration_enabled,
        sso_enabled=sso_enabled,
        sso_provider_name=sso_provider_name,
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
    """用户信息(不含任何敏感字段; 普通用户响应语义)。"""

    id: int
    username: str
    display_name: str
    role: str  # 主角色(admin 优先, 其次按授权顺序取第一个)
    status: str
    force_password_change: bool
    credential_version: int
    last_login_at: datetime | None = None


class AdminUserOut(UserOut):
    """管理员用户列表项(UserOut + 账号管理展示字段)。

    project_count: 该用户拥有的未删除项目数(active + archived, deleted 不计),
    由项目领域公开 read model(project_count_by_owner)单次聚合查询提供,
    仅服务 GET /api/auth/users 管理员列表。
    """

    project_count: int = 0


class UsersListResponse(BaseModel):
    """用户列表响应(管理员)。"""

    users: list[AdminUserOut]


class AuthResponse(BaseModel):
    """登录响应: 窗口凭证 + 用户信息。"""

    token: str
    token_type: str = "bearer"
    user: UserOut


# ---------------------------------------------------------------------------
# 认证上下文依赖
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthContext:
    """已认证请求上下文: 当前用户 + 其活动会话 + 数据库会话。

    用户与会话为身份域公开记录(UserRecord/WindowSessionRecord,
    经 application/identity 门面取数), 不直接引用 ORM。
    """

    db: Session
    user: UserRecord
    session: WindowSessionRecord


#: 数据库会话依赖别名(Annotated 风格, 规避 B008)
DbSession = Annotated[Session, Depends(get_request_db)]


def _extract_token(request: Request) -> str | None:
    """从 Authorization: Bearer 头或 Cookie 中提取窗口凭证原文。"""
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip() or None
    return request.cookies.get(SESSION_COOKIE_NAME)


def _client_ip(request: Request) -> str | None:
    """客户端 IP(代理部署时由反向代理注入 X-Forwarded-For, 后续阶段可扩展)。"""
    return request.client.host if request.client else None


def get_auth_context(request: Request, db: DbSession) -> AuthContext:
    """解析窗口凭证并校验: HTTP 凭证提取归本层, 身份业务校验归 application 用例。

    本层只从 Authorization 头/Cookie 提取凭证原文; 哈希匹配、状态、
    过期、凭证版本、账号状态和 last_seen_at 刷新由
    ``resolve_auth_context`` 处理。
    """
    token = _extract_token(request)
    user, session = identity.resolve_auth_context(db, token=token)
    return AuthContext(db=db, user=user, session=session)


def get_current_user(ctx: AuthCtx) -> UserRecord:
    """依赖: 当前已认证用户(其他业务单元复用)。"""
    return ctx.user


def get_current_admin(ctx: AuthCtx) -> UserRecord:
    """依赖: 当前管理员(无管理员角色抛 403)。"""
    if not identity.has_role(ctx.db, ctx.user, "admin"):
        raise ForbiddenError()
    return ctx.user


#: 认证上下文 / 当前用户 / 当前管理员依赖别名(须在依赖函数定义后声明)
AuthCtx = Annotated[AuthContext, Depends(get_auth_context)]
CurrentUser = Annotated[UserRecord, Depends(get_current_user)]
CurrentAdmin = Annotated[UserRecord, Depends(get_current_admin)]


def _user_out(view: identity.UserView) -> UserOut:
    """纯映射: 身份展示视图 → 用户响应 DTO(无 Session、无查询、无业务)。"""
    return UserOut(
        id=view.id,
        username=view.username,
        display_name=view.display_name,
        role=view.role,
        status=view.status,
        force_password_change=view.force_password_change,
        credential_version=view.credential_version,
        last_login_at=view.last_login_at,
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
# 会话与注册端点
# ---------------------------------------------------------------------------


@router.post("/login", response_model=AuthResponse, summary="登录(签发窗口凭证)")
def login(
    req: LoginRequest,
    request: Request,
    response: Response,
    db: DbSession,
) -> AuthResponse:
    """登录并签发会话。"""
    ip = _client_ip(request)
    ua = request.headers.get("user-agent")
    view, token = identity.login_case(
        db, username=req.username, password=req.password, device=req.device,
        ip=ip, user_agent=ua,
    )
    _set_session_cookie(response, request, token)
    return AuthResponse(token=token, user=_user_out(view))


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
def refresh(request: Request, response: Response, ctx: AuthCtx) -> dict:
    """用户活动续期: 同步顺延服务端会话与浏览器 Cookie。"""
    expires_at = identity.extend_session(ctx.db, ctx.session)
    # AuthCtx 已确权该凭证；续期不更换 token，也不复活已失效会话。
    _set_session_cookie(response, request, cast(str, _extract_token(request)))
    return {"ok": True, "expires_at": expires_at}


@router.get("/me", response_model=UserOut, summary="当前登录用户(页面刷新恢复会话)")
def me(ctx: AuthCtx) -> UserOut:
    """返回当前会话对应的用户信息(前端刷新后恢复登录态)。

    认证依赖确权后, 一次转交 ``get_user_view_case`` 完整视图用例,
    本层只做纯 DTO 映射。
    """
    return _user_out(identity.get_user_view_case(ctx.db, user_id=ctx.user.id))


@router.post("/register", response_model=UserOut, summary="自助注册(默认关闭, 仅工程师)")
def register(req: RegisterRequest, request: Request, db: DbSession) -> UserOut:
    """自助注册: 注册开关开启时可用, 只能创建 engineer 角色(domain-model §身份权限审计)。

    本端点一次转交 ``register_case`` 完整用例(开关 → 建账号 → 展示视图),
    本层只做纯 DTO 映射。
    """
    view = identity.register_case(
        db,
        username=req.username,
        password=req.password,
        display_name=req.display_name,
        email=req.email,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return _user_out(view)


# ---------------------------------------------------------------------------
# 管理员端点: 用户管理 / 安全设置
# ---------------------------------------------------------------------------


@router.get("/users", response_model=UsersListResponse, summary="用户列表(管理员)")
def list_users(db: DbSession, admin: CurrentAdmin) -> UsersListResponse:
    """用户列表(管理员): 含停用账号, 返回角色与强制改密状态与项目数。

    本端点一次转交 ``list_users_case`` 完整用例(视图一次批量组装 +
    项目计数一次聚合, 禁逐用户回调用例); 数据库故障沿用统一错误处理
    (异常向上传播, 不转为 0)。本层只做纯 DTO 映射与 project_count 拼装。
    """
    entries = identity.list_users_case(db)
    return UsersListResponse(
        users=[
            AdminUserOut(
                **_user_out(e.view).model_dump(),
                project_count=e.project_count,
            )
            for e in entries
        ]
    )


@router.post("/users/{user_id}/reset-password", summary="重置密码(管理员)")
def admin_reset_password(
    user_id: int,
    req: ResetPasswordRequest,
    request: Request,
    db: DbSession,
    admin: CurrentAdmin,
) -> dict:
    """管理员重置密码: 签发临时密码(强制改密), 使目标用户全部会话失效。

    管理员身份由依赖链确权(CurrentAdmin), 体内不再二次校验;
    本端点一次转交 ``reset_password_case`` 完整用例(目标预检 → 重置)。
    """
    identity.reset_password_case(
        db,
        admin=admin,
        target_id=user_id,
        new_password=req.new_password,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return {"ok": True, "message_key": "ies.diag.auth.password_reset"}


@router.post("/users/{user_id}/deactivate", summary="停用用户(管理员)")
def admin_deactivate_user(
    user_id: int, request: Request, db: DbSession, admin: CurrentAdmin
) -> dict:
    """停用用户(管理员): 账号禁止登录, 全部会话立即失效。

    管理员身份由依赖链确权(CurrentAdmin), 体内不再二次校验;
    本端点一次转交 ``deactivate_user_case`` 完整用例(目标预检 → 停用)。
    """
    identity.deactivate_user_case(
        db,
        admin=admin,
        target_id=user_id,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return {"ok": True}


@router.post("/users/{user_id}/reactivate", summary="重新启用用户(管理员)")
def admin_reactivate_user(
    user_id: int, request: Request, db: DbSession, admin: CurrentAdmin
) -> dict:
    """重新启用用户(管理员)。

    管理员身份由依赖链确权(CurrentAdmin), 体内不再二次校验;
    本端点一次转交 ``reactivate_user_case`` 完整用例(目标预检 → 启用)。
    """
    identity.reactivate_user_case(
        db,
        admin=admin,
        target_id=user_id,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return {"ok": True}


@router.post(
    "/users/{user_id}/delete-preview",
    summary="删除账号预告(管理员): 返回将受影响项目清单",
)
def admin_delete_user_preview(
    user_id: int, request: Request, db: DbSession, admin: CurrentAdmin
) -> dict:
    return identity.preview_user_delete_case(db, admin=admin, target_id=user_id)


@router.delete("/users/{user_id}", summary="删除账号(管理员, 级联删除其项目)")
def admin_delete_user(
    user_id: int,
    request: Request,
    db: DbSession,
    admin: CurrentAdmin,
) -> dict:
    result = identity.delete_user_case(
        db,
        admin=admin,
        target_id=user_id,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return {"ok": True, **result}


@router.put("/settings", summary="更新安全设置(管理员)")
def update_settings(
    payload: SettingsUpdate, request: Request, db: DbSession, admin: CurrentAdmin
) -> dict:
    """更新安全设置: 自助注册开关(默认关闭, 持久化到数据库, 多 Worker 一致)。

    管理员身份由依赖链确权(CurrentAdmin), 体内不再二次校验;
    开关写入与维护审计经 application.identity 用例单事务提交。
    """
    registration_enabled = identity.update_security_settings(
        db,
        enabled=payload.registration_enabled,
        updated_by=admin.id,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return {"registration_enabled": registration_enabled}


@router.get("/settings", summary="读取安全设置(管理员)")
def read_settings(db: DbSession, admin: CurrentAdmin) -> dict:
    """读取安全设置(管理员): 一次转交 ``get_security_settings`` 完整用例。

    管理员身份由依赖链确权(CurrentAdmin, 参数仅确权), 体内不再二次校验。
    """
    return identity.get_security_settings(db)


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

    state 为签名令牌(含 nonce 与 PKCE verifier, 360s 窗口), 回调时校验;
    授权 URL 构造经 application.identity 用例(未启用抛 404)。
    """
    # 回调完成前由签名 state 携带 nonce/verifier(无状态, 多 Worker 可用)
    return RedirectResponse(identity.begin_oidc_login())


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
    本端点一次转交 ``oidc_callback_case`` 完整用例(含启用判断、失败记录
    与事务提交), 只做 Cookie 写入与重定向映射。
    """
    ip = _client_ip(request)
    ua = request.headers.get("user-agent")
    result = identity.oidc_callback_case(db, code=code, state=state, ip=ip, user_agent=ua)
    if not result.ok:
        return RedirectResponse("/login?error=oidc_failed", status_code=302)
    assert result.token is not None
    _set_session_cookie(response, request, result.token)
    return RedirectResponse("/", status_code=302)
