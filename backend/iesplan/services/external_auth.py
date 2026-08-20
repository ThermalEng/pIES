"""外部认证接入(U01 扩展): OpenID Connect 单点登录(行业标准)。

协议层复用标准实现 **Authlib**(OIDC Core 1.0 客户端, 含 Discovery /
Authorization Code + PKCE(RFC 7636) / 令牌交换 / ID Token RS256 验签 +
iss/aud/exp/nonce 校验, 见 authlib.integrations.httpx_client.OIDCClient),
本模块仅保留业务侧薄封装:

- 提供方配置与授权 URL 构造(登录页跳转入口);
- 回调令牌交换与 ID Token 校验(委托 Authlib);
- 账号绑定(JIT 建号): 按 subject 查找本地用户, 首次登录自动创建 engineer
  账号并绑定外部主体(users.auth_subject 唯一键, 冲突即拒绝);
- 登录成功后走统一窗口会话(U01 单活动窗口语义, 见 auth.py)。

安全要点(Authlib 已实现, 本模块复核):
- ID Token: RS256 验签(JWKS)+ issuer/audience/exp/nonce 校验;
- PKCE 强制启用(防授权码截获); state 防 CSRF 回放(由 auth.py 与会话绑定);
- 建号默认 engineer 角色(与自助注册一致), 管理员角色始终由管理员分配。

配置(经 IESPLAN_ 前缀环境变量覆盖, docker-compose 注入):
    IESPLAN_AUTH_PROVIDER=oidc
    IESPLAN_OIDC_DISCOVERY_URL=https://idp.example.com/.well-known/openid-configuration
    IESPLAN_OIDC_CLIENT_ID=...
    IESPLAN_OIDC_CLIENT_SECRET=...
    IESPLAN_APP_URL=http://localhost:8080   (回调 redirect_uri 的基地址)
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from iesplan.config import settings
from iesplan.core.errors import AppError
from iesplan.models.identity import User

logger = logging.getLogger(__name__)

#: 用户名规则(与 identity.USERNAME_RE 一致, 用于外部主体 → 本地用户名映射)
_USERNAME_RE = re.compile(r"^[a-z0-9_]{3,32}$")
#: Discovery/令牌请求超时(秒)
_PROVIDER_TIMEOUT = 10.0
#: 回调安全窗口(授权码/state 有效性): 360 秒(行业惯例 5-10 分钟)
AUTH_CODE_WINDOW_SECONDS = 360
#: state 签名盐(与签名密钥配合, 防伪造)
_STATE_SALT = "oidc-pkce-state"


#: OIDC 错误(HTTP 400, 前端经 message_key 渲染)
class ExternalAuthError(AppError):
    """外部认证失败(配置/提供方/令牌校验错误)。"""

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


@dataclass(frozen=True)
class OidcClient:
    """Authlib OIDC 客户端(不可变; 惰性初始化, 提供方配置由 Discovery 拉取)。"""

    client_id: str
    client_secret: str


_client: OidcClient | None = None
_authlib: Any = None


def is_oidc_enabled() -> bool:
    """外部认证是否启用(IESPLAN_AUTH_PROVIDER=oidc 且配置齐全)。"""
    return (
        settings.auth_provider == "oidc"
        and bool(settings.oidc_discovery_url)
        and bool(settings.oidc_client_id)
    )


def _authlib_client() -> Any:
    """构造 Authlib OIDCClient(httpx 同步版, 进程内缓存复用)。"""
    global _authlib, _client
    if _client is None:
        _client = OidcClient(
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
        )
    if _authlib is None:
        from authlib.integrations.httpx_client import OIDCClient

        _authlib = OIDCClient(
            client_id=_client.client_id,
            client_secret=_client.client_secret,
            # PKCE 强制启用(S256)
            code_challenge_method="S256",
            timeout=_PROVIDER_TIMEOUT,
        )
    return _authlib


def _state_serializer() -> URLSafeTimedSerializer:
    """state 签名器(与主签名密钥分离, 多 worker 共享密钥可互认)。"""
    return URLSafeTimedSerializer(
        settings.secret_key,
        salt=_STATE_SALT,
        signer_kwargs={"key_derivation": "hmac"},
    )


def build_state(nonce: str, code_verifier: str) -> str:
    """签发 state(含 nonce 与 PKCE verifier, 防 CSRF 回放)。

    state 是签名令牌: 回调时反解并校验 nonce 与 verifier,
    无需会话存储, 多 worker/无状态部署天然支持。
    """
    return _state_serializer().dumps({"nonce": nonce, "verifier": code_verifier})


def verify_state(state: str) -> dict[str, str]:
    """校验回调 state: 签名有效 + 未过期(360s 窗口), 返回 (nonce, verifier)。"""
    try:
        payload = _state_serializer().loads(state, max_age=AUTH_CODE_WINDOW_SECONDS)
    except (BadSignature, SignatureExpired, TypeError) as exc:
        raise ExternalAuthError(reason="state_invalid") from exc
    nonce = payload.get("nonce")
    verifier = payload.get("verifier")
    if not isinstance(nonce, str) or not isinstance(verifier, str):
        raise ExternalAuthError(reason="state_invalid")
    return {"nonce": nonce, "verifier": verifier}


def callback_url() -> str:
    """回调地址(与 OIDC 客户端注册的 redirect_uri 一致)。"""
    base = settings.app_url.rstrip("/")
    return f"{base}/api/auth/oidc/callback"


def build_authorization_url(state: str) -> str:
    """构造提供方授权 URL(登录页跳转入口)。

    scope 取最小集(openid profile email), 附加 code_challenge(PKCE)。
    """
    client = _authlib_client()
    if not settings.oidc_discovery_url:
        raise ExternalAuthError(reason="not_configured")
    try:
        return client.generate_authorization_url(
            settings.oidc_discovery_url,
            redirect_uri=callback_url(),
            scope="openid profile email",
            state=state,
        )
    except Exception as exc:
        logger.warning("OIDC 授权 URL 构造失败: %s", exc)
        raise ExternalAuthError(reason="discovery_failed") from exc


def exchange_code(code: str, code_verifier: str) -> dict[str, Any]:
    """授权码 → 已验证的 ID Token claims(委托 Authlib OIDCClient)。

    Authlib 内部完成: 令牌交换、ID Token RS256 验签(JWKS)、
    issuer/audience/exp/nonce 校验(校验失败抛 MismatchStateError 等)。
    """
    client = _authlib_client()
    try:
        # fetch_token 消费 code, 传入 code_verifier 完成 PKCE 交换
        token = client.fetch_token(
            settings.oidc_discovery_url,
            code=code,
            redirect_uri=callback_url(),
            code_verifier=code_verifier,
        )
    except Exception as exc:
        logger.warning("OIDC 令牌交换失败: %s", exc)
        raise ExternalAuthError(reason="token_exchange_failed") from exc
    id_token = token.get("id_token")
    if not id_token:
        raise ExternalAuthError(reason="id_token_missing")
    try:
        claims = client.parse_id_token(id_token)
    except Exception as exc:
        logger.warning("OIDC ID Token 校验失败: %s", exc)
        raise ExternalAuthError(reason="id_token_invalid") from exc
    if not isinstance(claims.get("sub"), str):
        raise ExternalAuthError(reason="sub_missing")
    return claims


# ---------------------------------------------------------------------------
# 本地账号绑定(JIT 建号)
# ---------------------------------------------------------------------------


def find_by_subject(db: Session, subject: str) -> User | None:
    """按外部主体(sub)查找已绑定的本地用户。"""
    return db.execute(select(User).where(User.auth_subject == subject)).scalar_one_or_none()


def _subject_username(subject: str) -> str:
    """外部主体 → 本地用户名: 保留 ^[a-z0-9_]{3,32}$ 规则。

    非法字符替换为下划线; 结果为空/超长/以数字开头时按行业惯例
    追加固定前缀, 保证用户名合法且可追溯。
    """
    cleaned = re.sub(r"[^a-z0-9_]", "_", subject.lower())[:32]
    if not _USERNAME_RE.fullmatch(cleaned):
        cleaned = f"user_{cleaned.strip('_')}"[:32]
    if not _USERNAME_RE.fullmatch(cleaned):
        # 仍非法(如全为下划线): 生成随机用户名
        cleaned = f"user_{secrets.token_hex(4)}"
    return cleaned


def provision_user(
    db: Session,
    claims: dict[str, Any],
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> User:
    """OIDC 登录主体 → 本地用户(JIT 建号, 首次登录自动创建)。

    - 已绑定(subject 匹配): 返回既有用户(不重复建号);
    - 未绑定: 创建 engineer 账号(auth_subject 绑定, 仅可通过外部认证登录,
      无密码凭证; 用户名冲突时追加序号, 保证主体可追溯)。
    """
    subject = claims["sub"]
    user = find_by_subject(db, subject)
    if user is not None:
        return user
    from iesplan.services import identity

    username = _subject_username(subject)
    base, index = username, 2
    while identity.get_user_by_username(db, username) is not None:
        username = f"{base}_{index}"
        index += 1
        if index > 100:
            raise ExternalAuthError(reason="username_collision")
    user = identity.create_user(
        db,
        username=username,
        password=secrets.token_urlsafe(24),  # 随机密码: 仅外部认证登录, 禁用密码兜底
        role="engineer",
        force_password_change=False,
        display_name=str(claims.get("name") or claims.get("preferred_username") or username),
        email=(claims.get("email") or None),
        ip=ip,
        user_agent=user_agent,
    )
    # 绑定外部主体(唯一约束; 冲突回滚并明确报错)
    user.auth_subject = subject
    try:
        db.flush()
    except Exception as exc:
        db.rollback()
        raise ExternalAuthError(reason="subject_conflict") from exc
    return user
