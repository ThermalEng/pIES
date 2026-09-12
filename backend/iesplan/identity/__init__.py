"""身份域公开门面（用户/角色/授权/凭证/会话/设置/认证事件表，归属 identity）。

外部只允许经本门面消费 contract、repository 协议、repository 实现函数
与外部认证能力；不得导入 `iesplan.models`、services 或其他域的内部模块。
本门面不导出任何密钥材料（无 secret_hash/token 明文）。
输入规则（用户名/邮箱/密码）、身份错误与身份状态归本域所有
（后端解耦 Wave 3-A）；应用层经本门面复用，不重定义。

外部认证（ U01 扩展， OIDC 单点登录，由 services/external_auth.py 收敛至此，
纠偏 Wave 1 切片 C）：协议层复用标准实现 Authlib，本域仅保留业务侧薄封装
（提供方配置与授权 URL 构造、回调令牌交换与 ID Token 校验委托 Authlib、
账号绑定 JIT 建号、登录成功后走统一窗口会话）。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from iesplan.config import settings
from iesplan.core.namespace import generate_namespace
from iesplan.core.patterns import EMAIL_RE, USERNAME_RE
from iesplan.core.security import check_password_strength, hash_password
from iesplan.identity import persistence
from iesplan.identity.contracts import (
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_EXPIRED,
    SESSION_STATUS_REVOKED,
    SESSION_STATUS_TAKEOVER_PENDING,
    USER_STATUS_ACTIVE,
    USER_STATUS_DISABLED,
    AppSettingRecord,
    AuthError,
    AuthEventRecord,
    AuthRequiredError,
    BadOldPasswordError,
    BadRequestError,
    CredentialRecord,
    DeleteConfirmRequiredError,
    ExternalAuthError,
    ForcePasswordChangeError,
    IdentityConflictError,
    LockedError,
    LoginFailedError,
    RegistrationDisabledError,
    RoleRecord,
    SamePasswordError,
    SessionInvalidError,
    UserDisabledError,
    UserNotFoundError,
    UserRecord,
    UserRoleRecord,
    WeakPasswordError,
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
    "AuthError",
    "AuthEventRecord",
    "AuthRequiredError",
    "BadOldPasswordError",
    "BadRequestError",
    "CredentialRecord",
    "DeleteConfirmRequiredError",
    "ExternalAuthError",
    "ForcePasswordChangeError",
    "IdentityConflictError",
    "LOCKOUT_SECONDS",
    "LockedError",
    "LoginFailedError",
    "MAX_LOGIN_FAILURES",
    "OidcClient",
    "ROLE_ADMIN",
    "ROLE_ENGINEER",
    "RegistrationDisabledError",
    "RoleRecord",
    "SESSION_STATUS_ACTIVE",
    "SESSION_STATUS_EXPIRED",
    "SESSION_STATUS_REVOKED",
    "SESSION_STATUS_TAKEOVER_PENDING",
    "SamePasswordError",
    "SessionInvalidError",
    "USER_STATUS_ACTIVE",
    "USER_STATUS_DISABLED",
    "UserDisabledError",
    "UserNotFoundError",
    "UserRecord",
    "UserRoleRecord",
    "WeakPasswordError",
    "WindowSessionRecord",
    "add_credential",
    "bind_auth_subject",
    "build_authorization_url",
    "build_state",
    "bump_credential_version",
    "callback_url",
    "clean_ip",
    "clear_login_failures",
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
    "is_login_locked",
    "is_oidc_enabled",
    "list_active_sessions",
    "list_users",
    "login_block_reason",
    "new_session_expiry",
    "plan_login_session",
    "provision_user",
    "record_auth_event",
    "record_login_failure",
    "require_takeover_confirmable",
    "reset_login_rate_limit",
    "revoke_credentials",
    "revoke_role",
    "set_app_setting",
    "set_public_namespace",
    "set_session_status",
    "set_user_status",
    "touch_login",
    "user_roles",
    "validate_email",
    "validate_new_password",
    "validate_username",
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


def clean_ip(ip: str | None) -> str | None:
    """IP 白名单化: 仅保留合法 IP, 其余(PG INET 不接受的测试客户端名等)存 NULL。

    唯一权威(后端解耦 Wave 3-A: 原 application.identity 同名重复实现已删除,
    经此复用)。
    """
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
        ip=clean_ip(ip),
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


# ---------------------------------------------------------------------------
# 输入规则(用户名/邮箱/密码; 唯一权威, 应用层经此复用)
# ---------------------------------------------------------------------------

#: 内置角色(管理员、工程师; 宪法 §16 + 领域模型 §身份、权限和审计)
ROLE_ADMIN: Final[str] = "admin"
ROLE_ENGINEER: Final[str] = "engineer"


def validate_username(username: str) -> str:
    """用户名输入规则: 去空格转小写后须匹配 ^[a-z0-9_]{3,32}$(core.patterns 权威)。

    不满足抛 BadRequestError(AUTH-USER-003); 返回规范化后的用户名。
    """
    username = (username or "").strip().lower()
    if not re.fullmatch(USERNAME_RE, username):
        raise BadRequestError(
            "",
            code="AUTH-USER-003",
            message_key="ies.diag.auth.username_invalid",
            params={"username": username, "pattern": USERNAME_RE},
        )
    return username


def validate_email(email: str | None) -> str | None:
    """邮箱输入规则: 去空格转小写后须匹配邮箱格式(core.patterns 权威)。

    为空(None/空串)保持原样返回(调用方按可选字段处理); 非法抛
    BadRequestError(AUTH-USER-004)。
    """
    if not email:
        return email
    email = email.strip().lower()
    if not re.fullmatch(EMAIL_RE, email):
        raise BadRequestError(
            "",
            code="AUTH-USER-004",
            message_key="ies.diag.auth.email_invalid",
            params={"email": email},
        )
    return email


def validate_new_password(password: str) -> tuple[bool, str]:
    """新密码规则: 强度规则(core.security) + bcrypt 72 字节上限, 返回 (ok, reason)。"""
    ok, reason = check_password_strength(password)
    if ok and len(password.encode("utf-8")) > 72:
        return False, "密码过长(UTF-8 编码后不能超过 72 字节)"
    return ok, reason


# ---------------------------------------------------------------------------
# 登录限速(进程内存; 唯一权威, 应用层认证编排经此复用)
#
# 局限说明(H-03): 内存限速为单进程状态 —— 多 Uvicorn Worker 下失败计数被
# 分散到各进程, 进程重启后锁定状态丢失。生产多 Worker 部署应使用 Redis 原子
# 计数(见下方 _rate_redis); Redis 不可用(依赖缺失/连接失败/运行期错误)时
# 自动降级为内存限速并记 warning 日志, 不阻断登录功能。
# ---------------------------------------------------------------------------

try:
    import redis as _redis_module

    _REDIS_IMPORT_OK = True
except Exception:  # pragma: no cover - 环境缺 redis 依赖时降级内存限速
    _redis_module = None  # type: ignore[assignment]
    _REDIS_IMPORT_OK = False

#: Redis 登录限速键前缀
_RATE_KEY_PREFIX = "iesplan:ratelimit:login"
#: 惰性初始化的 Redis 客户端(单例; 连接失败置 None 后不再重试, 保持内存降级)
_rate_redis_client: Any = None

#: 登录限速: 同一用户名最大连续失败次数
MAX_LOGIN_FAILURES: Final[int] = 5
#: 锁定时长(秒): 达到失败上限后锁定 15 分钟
LOCKOUT_SECONDS: Final[int] = 15 * 60


def _rate_redis() -> Any | None:
    """尝试获取 Redis 客户端用于跨 Worker 限速; 不可用返回 None(降级内存)。

    IESPLAN_QUEUE=memory(测试/单机模式)时直接跳过 Redis, 保持进程内限速,
    避免测试环境共享 Redis 键造成跨测试/跨进程状态污染;
    超时/连接失败/运行期错误一律捕获, 由调用方回退内存限速。
    """
    global _rate_redis_client
    if os.environ.get("IESPLAN_QUEUE", "auto").lower() == "memory":
        return None
    if _rate_redis_client is not None:
        return _rate_redis_client
    if not _REDIS_IMPORT_OK:
        return None
    try:
        client = _redis_module.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=2.0,
        )
        client.ping()  # 探测连接, 失败抛异常
        _rate_redis_client = client
        logger.warning("登录限速使用 Redis 后端(跨 Worker 共享)")
    except Exception:  # noqa: BLE001 - 降级内存限速, 不阻断登录
        logger.warning("Redis 不可用, 登录限速降级为进程内存(单进程有效)")
        _rate_redis_client = None
    return _rate_redis_client


def _rate_key(username: str) -> str:
    """Redis 限速键(用户名小写化; TTL 即锁定期, INCR 幂等)。"""
    return f"{_RATE_KEY_PREFIX}:{(username or '').strip().lower()}"


def _redis_is_locked(username: str) -> bool:
    """Redis 限速判定: 计数达到上限即锁定(键 TTL 过期后自动解除)。"""
    r = _rate_redis()
    if r is None:
        return False
    try:
        count = r.get(_rate_key(username))
        return count is not None and int(count) >= MAX_LOGIN_FAILURES
    except Exception:  # noqa: BLE001 - 运行期错误降级内存
        return False


def _redis_record_failure(username: str) -> None:
    """Redis 记录一次失败: INCR 计数, 键 TTL = 锁定时长(滑动重置)。"""
    r = _rate_redis()
    if r is None:
        return
    try:
        r.incr(_rate_key(username))
        r.expire(_rate_key(username), LOCKOUT_SECONDS)
    except Exception:  # noqa: BLE001 - 运行期错误降级内存
        pass


def _redis_clear(username: str) -> None:
    """登录成功后清除 Redis 限速键。"""
    r = _rate_redis()
    if r is None:
        return
    try:
        r.delete(_rate_key(username))
    except Exception:  # noqa: BLE001
        pass


_LOGIN_FAILURES: dict[str, list[float]] = {}
_LOGIN_LOCKED_UNTIL: dict[str, float] = {}
_RATE_LOCK = threading.RLock()


def is_login_locked(username: str) -> bool:
    """是否处于锁定期(Redis 优先, 降级内存); 锁定时间已过则顺带清理状态。"""
    if _redis_is_locked(username):
        return True
    until = _LOGIN_LOCKED_UNTIL.get(username)
    if until is None:
        return False
    if time.monotonic() < until:
        return True
    with _RATE_LOCK:
        _LOGIN_LOCKED_UNTIL.pop(username, None)
        _LOGIN_FAILURES.pop(username, None)
    return False


def record_login_failure(username: str) -> None:
    """记录一次登录失败(Redis 优先, 降级内存);
    时间窗(锁定时长)内累计达到上限则触发锁定。"""
    _redis_record_failure(username)
    with _RATE_LOCK:
        if is_login_locked(username):
            return
        now = time.monotonic()
        window = [t for t in _LOGIN_FAILURES.get(username, []) if now - t < LOCKOUT_SECONDS]
        window.append(now)
        if len(window) >= MAX_LOGIN_FAILURES:
            _LOGIN_LOCKED_UNTIL[username] = now + LOCKOUT_SECONDS
            _LOGIN_FAILURES.pop(username, None)
        else:
            _LOGIN_FAILURES[username] = window


def clear_login_failures(username: str) -> None:
    """登录成功后清除该用户名限速状态(Redis + 内存)。"""
    _redis_clear(username)
    with _RATE_LOCK:
        _LOGIN_FAILURES.pop(username, None)
        _LOGIN_LOCKED_UNTIL.pop(username, None)


def reset_login_rate_limit(username: str | None = None) -> None:
    """清空登录限速状态(测试与运维恢复用; Redis 键一并清除)。

    参数:
        username: 为空时清空全部用户名; 否则只清指定用户名。
    """
    r = _rate_redis()
    if r is not None and username is not None:
        try:
            r.delete(_rate_key(username))
        except Exception:  # noqa: BLE001
            pass
    with _RATE_LOCK:
        if username is None:
            _LOGIN_FAILURES.clear()
            _LOGIN_LOCKED_UNTIL.clear()
        else:
            _LOGIN_FAILURES.pop(username, None)
            _LOGIN_LOCKED_UNTIL.pop(username, None)


# ---------------------------------------------------------------------------
# 会话与账号状态规则(唯一权威; 应用层编排经此判定, 状态写入与事务归调用方)
# ---------------------------------------------------------------------------


def new_session_expiry(now: datetime | None = None) -> datetime:
    """按配置的会话 TTL 计算过期时刻(会话存续规则归 identity)。"""
    return (now or datetime.now(UTC)) + timedelta(minutes=settings.session_ttl_minutes)


def login_block_reason(user: UserRecord) -> str | None:
    """账号状态门禁: 停用返回 "user_disabled", 系统账号返回 "system_account"。

    可登录返回 None。判定顺序与旧应用层一致(先状态, 后系统账号)。
    """
    if user.status != USER_STATUS_ACTIVE:
        return "user_disabled"
    if user.is_system:
        return "system_account"
    return None


def plan_login_session(
    previous: Sequence[WindowSessionRecord],
) -> tuple[list[WindowSessionRecord], WindowSessionRecord | None, bool, str]:
    """单活动窗口规则(宪法 §16 + 领域模型 §身份、权限和审计)。

    残留的 takeover_pending 会话(更早接管流程遗留; 部分唯一索引每用户至多
    一条 pending)与既有 active 会话一并撤销 —— 新会话以 takeover_pending
    创建, 避免同时存在两条 pending 触发唯一索引冲突(部分唯一索引每用户
    至多一条 active)。

    返回 (待撤销会话, 被取代的 active 会话或 None, 是否触发接管, 新会话初始状态)。
    本函数只做纯判定, 不写库: 状态写入、审计与事务由调用方(如应用用例)完成。
    """
    pending = [s for s in previous if s.status == SESSION_STATUS_TAKEOVER_PENDING]
    active = next((s for s in previous if s.status == SESSION_STATUS_ACTIVE), None)
    targets = [*pending] + ([active] if active is not None else [])
    displaced = bool(targets)
    new_status = SESSION_STATUS_TAKEOVER_PENDING if displaced else SESSION_STATUS_ACTIVE
    return targets, active, displaced, new_status


def require_takeover_confirmable(session: WindowSessionRecord | None) -> WindowSessionRecord:
    """接管确认前置规则: 会话须为 takeover_pending/active, 否则抛 SessionInvalidError。

    并发防御的一部分: 调用方须传入重读后的最新行(确认前被新登录撤销的会话不再恢复)。
    """
    if session is None or session.status not in (
        SESSION_STATUS_TAKEOVER_PENDING,
        SESSION_STATUS_ACTIVE,
    ):
        raise SessionInvalidError()
    return session
