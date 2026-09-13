"""身份端点级完整用例(application/identity.auth_cases)。

路由层不直接依赖 services、不提交/回滚事务、不直接导入 ORM。
本模块拥有身份业务顺序与事务: 端点级完整用例(登录/注册/用户列表/
账号状态变更/删除预告确认/安全设置/OIDC 回调/会话校验)全部在此,
API 只留请求 DTO、凭证提取/Cookie/重定向传输与错误/响应映射。

- 公开设置三元组: 注册开关 + OIDC 入口状态;
- 管理员用户列表项目数: project 域 read model 单次聚合(防 N+1);
- 安全设置更新: 注册开关持久化 + 维护审计, 单事务提交;
- OIDC 登录入口/回调: identity 域外部认证能力, 会话写入与事务提交在此。
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from iesplan import identity as identity_domain
from iesplan import project as project_domain
from iesplan.application.identity.service import (
    as_utc,
    authenticate,
    confirm_takeover,
    create_user,
    create_window_session,
    deactivate_user,
    delete_user,
    expire_session,
    get_active_password_credential,
    get_session_by_token,
    get_user_by_id,
    list_users,
    preview_user_delete,
    reactivate_user,
    record_auth_event,
    registration_enabled,
    reset_password,
    revoke_session_after_credential_change,
    set_registration_enabled,
    touch_session,
    utcnow,
)
from iesplan.application.identity.views import UserView, user_view, user_views
from iesplan.core.errors import NotFoundError
from iesplan.identity import ExternalAuthError
from iesplan.identity.contracts import (
    AuthRequiredError,
    ForcePasswordChangeError,
    LockedError,
    LoginFailedError,
    RegistrationDisabledError,
    SessionInvalidError,
    UserRecord,
    WindowSessionRecord,
)

__all__ = [
    "ExternalAuthError",
    "FPC_ALLOWED_PATHS",
    "PENDING_ALLOWED_PATHS",
    "OidcCallbackResult",
    "UserWithProjectCount",
    "begin_oidc_login",
    "complete_oidc_login",
    "confirm_takeover_case",
    "deactivate_user_case",
    "delete_user_case",
    "get_public_auth_settings",
    "get_security_settings",
    "get_user_view_case",
    "is_oidc_enabled",
    "list_users_case",
    "login_case",
    "oidc_callback_case",
    "preview_user_delete_case",
    "project_counts_by_owner",
    "reactivate_user_case",
    "record_oidc_login_failure",
    "register_case",
    "reset_password_case",
    "resolve_auth_context",
    "update_security_settings",
]


#: 强制改密门禁(C-02)豁免路径: 仅允许改密/登出/本人信息
FPC_ALLOWED_PATHS: frozenset[str] = frozenset(
    {"/api/auth/change-password", "/api/auth/logout", "/api/auth/me"}
)
#: 待接管(pending)会话允许的路径(H-01): 确认接管 + 强制改密门禁豁免路径
#: (避免强制改密用户被 pending 状态卡死: 可先改密或登出, 再重新登录)
PENDING_ALLOWED_PATHS: frozenset[str] = frozenset(
    FPC_ALLOWED_PATHS | {"/api/auth/confirm-takeover"}
)


def is_oidc_enabled() -> bool:
    """外部认证(SSO)是否启用(登录页入口与回调门禁共用)。"""
    return identity_domain.is_oidc_enabled()


def get_public_auth_settings(db: Session) -> tuple[bool, bool, str]:
    """登录页公开设置三元组(注册开关, SSO 是否启用, SSO 提供方显示名)。

    无需认证(登录页渲染前置条件); 仅返回登录页需要的布尔与显示名。
    """
    sso_enabled = identity_domain.is_oidc_enabled()
    return registration_enabled(db), sso_enabled, "OIDC" if sso_enabled else ""


def project_counts_by_owner(db: Session, owner_ids: Sequence[int]) -> dict[int, int]:
    """项目数量 read model: owner_id → 未删除项目数(管理员用户列表消费)。

    统计口径: 该用户拥有的 active + archived 项目, deleted 一律排除;
    一次 GROUP BY 聚合查询(单条 SQL, 防 N+1)。
    数据库故障沿用统一错误处理(异常向上传播, 不在此转为 0)。
    """
    return project_domain.count_projects_by_owner(db, owner_ids)


def update_security_settings(
    db: Session,
    *,
    enabled: bool,
    updated_by: int | None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> bool:
    """更新安全设置: 自助注册开关持久化(多 Worker 一致)+ 维护审计, 单事务提交。

    返回持久化后的最新开关值。
    """
    set_registration_enabled(db, enabled, updated_by=updated_by)
    record_auth_event(
        db,
        "maintenance",
        user_id=updated_by,
        ip=ip,
        user_agent=user_agent,
        detail={"action": "registration_toggle", "registration_enabled": enabled},
    )
    db.commit()
    return registration_enabled(db)


def begin_oidc_login() -> str:
    """OIDC 登录入口: 未启用抛 404; 否则签发 state(PKCE)并返回提供方授权 URL。

    state 为签名令牌(含 nonce 与 PKCE verifier, 360s 窗口), 回调时校验;
    回调完成前由签名 state 携带 nonce/verifier(无状态, 多 Worker 可用)。
    """
    if not identity_domain.is_oidc_enabled():
        raise NotFoundError("", params={"object_type": "auth_provider"})
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)[:64]
    state = identity_domain.build_state(nonce, verifier)
    return identity_domain.build_authorization_url(state)


def complete_oidc_login(
    db: Session,
    *,
    code: str,
    state: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[UserRecord, str, bool]:
    """OIDC 回调主路径: 校验 state → 交换令牌 → 账号绑定(JIT)→ 签发窗口会话。

    失败抛 ExternalAuthError(调用方记录 login_failure 审计后回登录页)。
    返回 (user, token, displaced), displaced 为 True 表示存在旧活动窗口
    被取代(前端据此提示确认接管)。
    """
    payload = identity_domain.verify_state(state)
    claims = identity_domain.exchange_code(code, payload["verifier"])
    user = identity_domain.provision_user(db, claims, ip=ip, user_agent=user_agent)
    db.flush()
    _session, token, displaced = create_window_session(db, user, "oidc", ip=ip, user_agent=user_agent)
    return user, token, displaced


def record_oidc_login_failure(
    db: Session,
    *,
    reason: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """OIDC 回调失败审计(login_failure, 单事务提交)。"""
    record_auth_event(db, "login_failure", ip=ip, user_agent=user_agent, detail={"reason": reason})
    db.commit()


# ---------------------------------------------------------------------------
# 端点级完整用例(API 只留请求 DTO、凭证提取/Cookie/重定向传输与错误/响应映射)
# ---------------------------------------------------------------------------


def resolve_auth_context(
    db: Session, *, token: str | None, path: str
) -> tuple[UserRecord, WindowSessionRecord]:
    """凭证解析后的身份业务校验: 哈希匹配 + 状态 + 未过期 + 凭证版本一致 + 用户有效。

    安全门禁(校验失败抛对应异常):
    - H-01: takeover_pending 会话仅允许确认接管/改密/登出/本人信息路径,
      其余业务请求一律 401(SessionInvalidError, params.reason=takeover_pending);
    - C-02: 有效密码凭证 requires_change=True 时, 除改密/登出/本人信息外
      全部业务请求返回 403(AUTH-FPC-001, 强制改密未解除前无业务权限)。

    任一次校验通过都会顺带刷新 last_seen_at(会话活跃时间)。
    凭证原文提取(HTTP 头/Cookie)归 API, 本用例只收已提取的 token 与请求路径。
    """
    if not token:
        raise AuthRequiredError()
    session = get_session_by_token(db, token)
    if session is None:
        raise SessionInvalidError()
    now = utcnow()
    if session.status == "takeover_pending":
        # H-01: 接管确认前新会话不拥有业务权限; 仅放行接管/改密/登出/本人信息
        if path not in PENDING_ALLOWED_PATHS:
            raise SessionInvalidError(params={"reason": "takeover_pending"})
    elif session.status != "active":
        raise SessionInvalidError()
    expires_at = as_utc(session.expires_at)
    if expires_at is not None and expires_at < now:
        # 已过期: 置终态(系统自动过期, 无操作者; 会话写入经 application 身份用例)
        expire_session(db, session.id)
        raise SessionInvalidError()
    user = get_user_by_id(db, session.user_id)
    if user is None or user.status != "active":
        raise SessionInvalidError()
    if session.credential_version_at_issue != user.credential_version:
        # 凭证已轮换(改密/重置): 旧会话立即失效(domain-model §身份权限审计 凭证失效机制)
        revoke_session_after_credential_change(db, session.id, user.id)
        raise SessionInvalidError()
    # C-02: 强制改密门禁(服务端统一执行, 不依赖前端配合)
    cred = get_active_password_credential(db, user)
    if cred is not None and cred.requires_change and path not in FPC_ALLOWED_PATHS:
        raise ForcePasswordChangeError(
            params={"hint": "首次登录请先修改初始密码, 未改密前仅可使用改密/登出/本人信息接口"}
        )
    touch_session(db, session.id)
    return user, session


def login_case(
    db: Session,
    *,
    username: str,
    password: str,
    device: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[UserView, str, bool]:
    """登录完整用例: 密码校验 + 登录限速 → 创建单活动窗口会话。

    若该账号已有活动窗口, 旧窗口被撤销、新会话以 takeover_pending 创建
    (确认接管前无业务权限), displaced=True(前端提示确认接管)。
    失败按 error_code 映射为 LockedError/LoginFailedError(语义与原路由一致)。
    返回 (view, token, displaced), view 为身份展示视图(用例内一次组装,
    API 只做纯 DTO 映射); Cookie 写入与响应组装归 API。
    """
    user, error_code = authenticate(
        db, username, password, ip=ip, user_agent=user_agent, device=device
    )
    if error_code is not None:
        if error_code == "locked":
            raise LockedError()
        raise LoginFailedError()
    assert user is not None
    _session, token, displaced = create_window_session(
        db, user, device, ip=ip, user_agent=user_agent
    )
    return user_view(db, user), token, displaced


def register_case(
    db: Session,
    *,
    username: str,
    password: str,
    display_name: str | None = None,
    email: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> UserView:
    """自助注册完整用例: 注册开关 → 仅创建 engineer 角色。

    开关关闭时抛 RegistrationDisabledError(语义与原路由一致)。
    返回身份展示视图(用例内一次组装, API 只做纯 DTO 映射)。
    """
    if not registration_enabled(db):
        raise RegistrationDisabledError()
    user = create_user(
        db,
        username,
        password,
        role="engineer",
        force_password_change=False,
        display_name=display_name,
        email=email,
        ip=ip,
        user_agent=user_agent,
    )
    return user_view(db, user)


@dataclass(frozen=True)
class UserWithProjectCount:
    """管理员用户列表行: 身份展示视图 + 其拥有的未删除项目数。"""

    view: UserView
    project_count: int = 0


def list_users_case(db: Session) -> list[UserWithProjectCount]:
    """用户列表完整用例: 全部用户(含停用, 一次批量组装视图) + 项目计数一次聚合。

    展示视图经 user_views 一次公开调用组装(禁 API 逐用户回调用例);
    数据库故障沿用统一错误处理(异常向上传播, 不转为 0)。
    API 只做 view→DTO 纯映射与 project_count 拼装。
    """
    users = list_users(db)
    counts = project_counts_by_owner(db, [u.id for u in users])
    views = user_views(db, users)
    return [
        UserWithProjectCount(view=view, project_count=counts.get(user.id, 0))
        for user, view in zip(users, views, strict=True)
    ]


def confirm_takeover_case(
    db: Session,
    *,
    user: UserRecord,
    session: WindowSessionRecord,
    ip: str | None = None,
    user_agent: str | None = None,
) -> UserView:
    """确认接管完整用例: 当前待接管会话转 active(不轮换凭证) → 身份展示视图。

    用例内一次组装视图, API 只做传输映射(凭证提取/Cookie 写入归 API,
    不二次查询身份状态)。
    """
    confirm_takeover(db, user, session, ip=ip, user_agent=user_agent)
    return user_view(db, user)


def get_user_view_case(db: Session, *, user_id: int) -> UserView:
    """本人视图完整用例(/me): 目标预检 → 身份展示视图(认证依赖链已确权)。"""
    return user_view(db, _require_target(db, user_id))


def _require_target(db: Session, target_id: int) -> UserRecord:
    """账号管理目标预检: 不存在 → 404(语义与原路由一致)。"""
    target = get_user_by_id(db, target_id)
    if target is None:
        raise NotFoundError("", params={"object_type": "user", "id": target_id})
    return target


def reset_password_case(
    db: Session,
    *,
    admin: UserRecord,
    target_id: int,
    new_password: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """管理员重置密码完整用例: 目标预检 → 签发临时密码(强制改密, 旧会话失效)。"""
    target = _require_target(db, target_id)
    reset_password(db, admin, target, new_password, ip=ip, user_agent=user_agent)


def deactivate_user_case(
    db: Session,
    *,
    admin: UserRecord,
    target_id: int,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """停用用户完整用例: 目标预检 → 状态置 disabled, 全部会话立即失效。"""
    target = _require_target(db, target_id)
    deactivate_user(db, admin, target, ip=ip, user_agent=user_agent)


def reactivate_user_case(
    db: Session,
    *,
    admin: UserRecord,
    target_id: int,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """重新启用用户完整用例: 目标预检 → 状态置 active。"""
    target = _require_target(db, target_id)
    reactivate_user(db, admin, target, ip=ip, user_agent=user_agent)


def preview_user_delete_case(
    db: Session, *, admin: UserRecord, target_id: int
) -> dict:
    """删除账号预告完整用例: 目标预检 → 受影响项目清单 + 签名确认令牌。"""
    target = _require_target(db, target_id)
    return preview_user_delete(db, admin, target)


def delete_user_case(
    db: Session,
    *,
    admin: UserRecord,
    target_id: int,
    confirm: bool = False,
    confirm_token: str = "",
    ip: str | None = None,
    user_agent: str | None = None,
) -> dict:
    """删除账号完整用例: 目标预检 → 确认+令牌校验 → 级联软删其项目。

    目标拥有的项目一并软删(不可恢复); 不能删除自己/系统账号。
    """
    target = _require_target(db, target_id)
    return delete_user(
        db, admin, target,
        confirm=confirm, confirm_token=confirm_token, ip=ip, user_agent=user_agent,
    )


def get_security_settings(db: Session) -> dict:
    """读取安全设置完整用例: 自助注册开关(数据库权威值)。"""
    return {"registration_enabled": registration_enabled(db)}


@dataclass(frozen=True)
class OidcCallbackResult:
    """OIDC 回调用例结果(与 HTTP 无关): 成功带窗口凭证, 失败由 API 映射为登录页重定向。"""

    ok: bool
    token: str | None = None


def oidc_callback_case(
    db: Session,
    *,
    code: str,
    state: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> OidcCallbackResult:
    """OIDC 回调完整用例: 提供方启用判断 → 令牌交换/账号绑定/会话签发(含事务提交)。

    未启用抛 404; 失败记录 login_failure 审计后返回 ok=False(不泄露提供方细节),
    由 API 映射为登录页错误重定向。
    """
    if not is_oidc_enabled():
        raise NotFoundError("", params={"object_type": "auth_provider"})
    try:
        _, token, _ = complete_oidc_login(db, code=code, state=state, ip=ip, user_agent=user_agent)
    except ExternalAuthError as exc:
        record_oidc_login_failure(
            db, reason=exc.params.get("reason", "oidc_failed"), ip=ip, user_agent=user_agent
        )
        return OidcCallbackResult(ok=False)
    return OidcCallbackResult(ok=True, token=token)
