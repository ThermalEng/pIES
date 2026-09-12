"""身份域公开门面（用户/角色/授权/凭证/会话/设置/认证事件表，归属 identity）。

外部只允许经本门面消费 contract、repository 协议、repository 实现函数
与外部认证能力；不得导入 `iesplan.models`、services 或其他域的内部模块。
本门面不导出任何密钥材料（无 secret_hash/token 明文）。

外部认证（ U01 扩展， OIDC 单点登录，由 services/external_auth.py 收敛至此，
纠偏 Wave 1 切片 C）：协议层复用标准实现 Authlib，本域仅保留业务侧薄封装
（提供方配置与授权 URL 构造、回调令牌交换与 ID Token 校验委托 Authlib、
账号绑定 JIT 建号、登录成功后走统一窗口会话）。
"""

from __future__ import annotations

import ipaddress
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from iesplan.config import settings
from iesplan.core.namespace import generate_namespace
from iesplan.core.patterns import USERNAME_RE
from iesplan.core.security import hash_password
from iesplan.identity import persistence
from iesplan.identity.contracts import (
    AppSettingRecord,
    AuthEventRecord,
    CredentialRecord,
    ExternalAuthError,
    IdentityConflictError,
    RoleRecord,
    UserNotFoundError,
    UserRecord,
    UserRoleRecord,
    WindowSessionRecord,
)

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
    "AUTH_CODE_WINDOW_SECONDS",
    "AppSettingRecord",
    "AuthEventRecord",
    "CredentialRecord",
    "ExternalAuthError",
    "IdentityConflictError",
    "OidcClient",
    "RoleRecord",
    "UserNotFoundError",
    "UserRecord",
    "UserRoleRecord",
    "WindowSessionRecord",
    "add_credential",
    "bind_auth_subject",
    "build_authorization_url",
    "build_state",
    "bump_credential_version",
    "callback_url",
    "create_session",
    "create_user",
    "ensure_role",
    "exchange_code",
    "expire_sessions",
    "extend_session",
    "find_by_subject",
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
    "is_oidc_enabled",
    "list_active_sessions",
    "list_users",
    "provision_user",
    "record_auth_event",
    "revoke_credentials",
    "revoke_role",
    "set_app_setting",
    "set_public_namespace",
    "set_session_status",
    "set_user_status",
    "touch_login",
    "user_roles",
    "verify_state",
]

logger = logging.getLogger(__name__)

#: 用户名规则(唯一权威: iesplan.core.patterns.USERNAME_RE, 用于外部主体 → 本地用户名映射)
_USERNAME_RE = re.compile(USERNAME_RE)
#: Discovery/令牌请求超时(秒)
_PROVIDER_TIMEOUT = 10.0
#: 回调安全窗口(授权码/state 有效性): 360 秒(行业惯例 5-10 分钟)
AUTH_CODE_WINDOW_SECONDS = 360
#: state 签名盐(与签名密钥配合, 防伪造)
_STATE_SALT = "oidc-pkce-state"


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


def find_by_subject(db: Session, subject: str) -> UserRecord | None:
    """按外部主体(sub)查找已绑定的本地用户(经本域 repository)。"""
    return persistence.get_user_by_auth_subject(db, subject)


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


def _clean_ip(ip: str | None) -> str | None:
    """IP 白名单化: 仅保留合法 IP, 其余(PG INET 不接受的测试客户端名等)存 NULL。"""
    if not ip:
        return None
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        return None


def provision_user(
    db: Session,
    claims: dict[str, Any],
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> UserRecord:
    """OIDC 登录主体 → 本地用户(JIT 建号, 首次登录自动创建)。

    - 已绑定(subject 匹配): 返回既有用户(不重复建号);
    - 未绑定: 创建 engineer 账号(auth_subject 绑定; 用户名冲突时追加序号,
      保证主体可追溯)。

    建号有效语义与旧 services.external_auth.provision_user 一致
    (经 application.identity.create_user 的那条路径):
    用户名/显示名/邮箱组装、公开命名空间分配(CSPRNG 唯一)、bcrypt 密码凭证
    (engineer 缺省免改密)、自授权 engineer 角色、role_change 认证审计。
    本域不拥有事务: 只 flush, 提交由调用方(如 OIDC 回调用例经窗口会话签发
    一并提交)控制; 邮箱仅做去空格转小写规范化(与本域查询语义一致),
    邮箱唯一检查仍归调用方(见 persistence.create_user 约定)。
    """
    subject = claims["sub"]
    user = find_by_subject(db, subject)
    if user is not None:
        return user
    username = _subject_username(subject)
    base, index = username, 2
    while persistence.get_user_by_username(db, username) is not None:
        username = f"{base}_{index}"
        index += 1
        if index > 100:
            raise ExternalAuthError(reason="username_collision")
    # 分配公开命名空间（CSPRNG，60 bit 熵；全局唯一，碰撞重试）
    ns = None
    for _ in range(20):
        candidate = generate_namespace()
        if persistence.get_user_by_namespace(db, candidate) is None:
            ns = candidate
            break
    if ns is None:
        raise RuntimeError("无法分配唯一的 public_namespace")
    email = claims.get("email") or None
    if isinstance(email, str):
        email = email.strip().lower() or None
    else:
        email = None
    user = persistence.create_user(
        db,
        username=username,
        display_name=(str(claims.get("name") or claims.get("preferred_username") or username)).strip()
        or username,
        email=email,
        public_namespace=ns,
    )
    persistence.add_credential(
        db,
        user_id=user.id,
        credential_type="password",
        # 固定前缀保证满足本地密码复杂度门禁；随机主体仍提供充足熵。
        # 该凭证不向用户公开，外部账号仍只能经 OIDC 登录。
        secret_hash=hash_password(f"Aa1!{secrets.token_urlsafe(24)}"),
        algorithm="bcrypt",
        strength_score=100,
        requires_change=False,
        created_by=None,
    )
    role_row = persistence.ensure_role(db, "engineer", "工程师")
    persistence.grant_role(db, user_id=user.id, role_id=role_row.id, granted_by=user.id)
    persistence.record_auth_event(
        db,
        event_type="role_change",
        user_id=user.id,
        ip=_clean_ip(ip),
        user_agent=user_agent,
        detail={"action": "grant", "role": "engineer", "granted_by": None},
    )
    # 绑定外部主体(唯一约束; 冲突明确报错, 由调用方回滚;
    # 域门面不拥有事务, 不调用 commit/rollback)
    try:
        user = persistence.bind_auth_subject(db, user.id, subject)
    except IdentityConflictError as exc:
        raise ExternalAuthError(reason="subject_conflict") from exc
    return user
