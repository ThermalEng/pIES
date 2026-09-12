"""身份 API 薄封装用例(application/identity.auth_cases)。

pIES Wave 4(api/auth.py 迁移): 路由层不得直接依赖 iesplan.services.*、
不得提交/回滚事务、不得直接导入 ORM(架构门禁 3/4/5)。
本模块承接原 api/auth.py 中的跨层调用, 全部为无业务决策的直通委托,
事务只在此提交/回滚:

- 公开设置三元组: 注册开关 + OIDC 入口状态;
- 管理员用户列表项目数: project 域 read model 单次聚合(防 N+1);
- 安全设置更新: 注册开关持久化 + 维护审计, 单事务提交;
- OIDC 登录入口/回调: services.external_auth 薄封装(标准实现 Authlib),
  会话写入与事务提交上收至此。
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence

from sqlalchemy.orm import Session

from iesplan import project as project_domain
from iesplan.application.identity.service import (
    create_window_session,
    record_auth_event,
    registration_enabled,
    set_registration_enabled,
)
from iesplan.core.errors import NotFoundError
from iesplan.identity.contracts import UserRecord
from iesplan.services import external_auth
from iesplan.services.external_auth import ExternalAuthError

__all__ = [
    "ExternalAuthError",
    "begin_oidc_login",
    "complete_oidc_login",
    "get_public_auth_settings",
    "is_oidc_enabled",
    "project_counts_by_owner",
    "record_oidc_login_failure",
    "update_security_settings",
]


def is_oidc_enabled() -> bool:
    """外部认证(SSO)是否启用(登录页入口与回调门禁共用)。"""
    return external_auth.is_oidc_enabled()


def get_public_auth_settings(db: Session) -> tuple[bool, bool, str]:
    """登录页公开设置三元组(注册开关, SSO 是否启用, SSO 提供方显示名)。

    无需认证(登录页渲染前置条件); 仅返回登录页需要的布尔与显示名。
    """
    sso_enabled = external_auth.is_oidc_enabled()
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
    if not external_auth.is_oidc_enabled():
        raise NotFoundError("", params={"object_type": "auth_provider"})
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)[:64]
    state = external_auth.build_state(nonce, verifier)
    return external_auth.build_authorization_url(state)


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
    payload = external_auth.verify_state(state)
    claims = external_auth.exchange_code(code, payload["verifier"])
    user = external_auth.provision_user(db, claims, ip=ip, user_agent=user_agent)
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
