"""CSRF 防护(切片 A2: Cookie 认证 CSRF)。

威胁模型: 前端浏览器用 ``credentials: 'include'`` 携带 `ies_session` Cookie
(``HttpOnly`` + ``SameSite=Lax``) 访问状态变更接口。SameSite=Lax 不阻止顶级
导航发出的跨站 POST, 第三方站点可通过自动提交的表单触发注销、改密、接管、
创建/修改/删除项目、上传、对象清理等状态变更(登录用户被动执行)。

设计(RPD 0.2.0 批次 A / OWASP CSRF 防护速查表「双源校验」方案):
- 只拦截「基于 Cookie 会话 + 状态变更方法(POST/PUT/PATCH/DELETE)」的请求;
- Bearer 认证请求(API 客户端/无 Cookie 场景)不经过本校验, 不受影响;
- 无 Origin/Referer 的非浏览器客户端(CLI/脚本/测试 TestClient)放行;
- 浏览器跨站请求必须携带匹配的来源头: 优先校验 ``Origin``(现代浏览器对
  非 GET/HEAD 请求一律携带), 缺失时回退校验 ``Referer``(表单导航默认携带);
  规范化后必须命中可信来源 —— ``settings.app_url`` + CORS 来源清单 +
  请求自身 Host 推导的同源来源(nginx 反向代理后 Host 由 ``$host`` 转发,
  浏览器同源 POST 的 Origin 恒等于该 Host, 覆盖任意部署拓扑)。

为什么优先 Origin + 回退 Referer + 无头放行:
- CSRF 本质是浏览器自动附加的 Cookie 被第三方站点冒用; 跨站请求的来源头
  由浏览器生成、第三方无法伪造, 是比 token 更轻的同等强度防护(OWASP 双源校验);
- 旧浏览器/隐私环境缺失 Origin 时 Referer 提供同等校验来源;
- 二者都缺失说明请求不来自浏览器导航流(fetch/XHR 与表单自动提交均携带),
  对 API 客户端保持零摩擦放行(任务约束: 不误伤无来源头的非浏览器客户端)。

本模块不依赖第三方库, 纯 Starlette 中间件, 无状态可多 Worker 部署。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Final
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from iesplan.config import settings
from iesplan.core.errors import error_envelope

logger = logging.getLogger(__name__)

#: 会话 Cookie 名(与 iesplan.api.auth 一致, 避免循环导入故本地复制)
SESSION_COOKIE_NAME: Final[str] = "ies_session"
#: 需要 CSRF 校验的状态变更方法
UNSAFE_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: 开发环境默认可信来源(Vite 3000/5173 代理 + nginx 8080; 与 main._setup_cors 同源)
_DEFAULT_DEV_ORIGINS: Final[tuple[str, ...]] = (
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
)

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def normalize_origin(value: str) -> str:
    """规范化来源: 只保留 scheme://host[:port], 小写, 丢弃路径/query/fragment。

    输入可能是 ``Origin: https://example.com`` 或 ``Referer: https://example.com/a/b``。
    """
    raw = value.strip()
    if not raw or raw.lower() == "null":
        return ""
    if not _SCHEME_RE.match(raw):
        return ""
    try:
        parts = urlparse(raw)
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    if not hostname:
        return ""
    port = parts.port
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        return f"{scheme}://{hostname}:{port}"
    return f"{scheme}://{hostname}"


def cors_origin_list() -> list[str]:
    """CORS 允许来源清单(环境变量 ``IESPLAN_CORS_ORIGINS`` 覆盖, 否则开发默认值)。

    单一事实源: ``main._setup_cors`` 与本模块都引用它, 保证「允许跨域携带
    凭据的来源」与「CSRF 信任的来源」一致 —— 被 CORS 放行携带 Cookie 的
    跨源页面同样能通过同源校验, 不产生配置漂移。
    """
    raw = os.environ.get("IESPLAN_CORS_ORIGINS", "")
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if not origins:
        origins = list(_DEFAULT_DEV_ORIGINS)
    return origins


def build_trusted_origins() -> frozenset[str]:
    """静态可信来源集合: ``settings.app_url`` + CORS 来源清单(规范化后)。

    注意: 仅覆盖「已配置的部署来源」。实际部署时浏览器同源 POST 的来源恒等于
    请求 Host(nginx 转发 ``$host``), 由 dispatch 动态并入 Host 推导来源,
    因此本集合不要求穷举所有可达域名。
    """
    trusted: set[str] = set()
    for raw in (settings.app_url, *cors_origin_list()):
        norm = normalize_origin(raw)
        if norm:
            trusted.add(norm)
    return frozenset(trusted)


def _request_own_origin(request: Request) -> str:
    """从请求 Host 推导其自身的浏览器来源(scheme://host[:port])。

    - 反代部署时后端看到的 Host 由 nginx ``proxy_set_header Host $host`` 转发,
      与浏览器请求头一致; scheme 优先取 X-Forwarded-Proto(HTTPS 反代)。
    - 失败/无法解析时返回空串(不参与可信集合, 仅依赖静态集合兜底)。
    """
    scheme = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() or request.url.scheme
    host = request.headers.get("host", "").strip()
    if not host:
        return ""
    return normalize_origin(f"{scheme}://{host}")


def csrf_reject_response() -> JSONResponse:
    """CSRF 校验失败的标准 403 错误信封(与全局 AppError 处理器同构)。"""
    return JSONResponse(
        status_code=403,
        content=error_envelope(
            code="AUTH-CSRF-001",
            message_key="ies.diag.auth.csrf_origin_rejected",
            severity="blocking",
            blocking=True,
        ),
    )


class CSRFOriginGuardMiddleware(BaseHTTPMiddleware):
    """状态变更请求来源校验中间件。

    规则:
    1. 非状态变更方法(GET/HEAD/OPTIONS 等)→ 放行;
    2. 未携带 ``ies_session`` Cookie → 非 Cookie 会话(Bearer/匿名)→ 放行;
    3. 缺少 Origin 与 Referer → 非浏览器客户端(API/TestClient)→ 放行;
    4. 来源头存在 → 规范化后必须命中可信来源
       (静态: app_url + CORS 来源; 动态: 请求自身 Host 同源来源), 否则 403。
    """

    def __init__(self, app, trusted_origins: frozenset[str] | None = None) -> None:
        super().__init__(app)
        self.trusted_origins = trusted_origins if trusted_origins is not None else build_trusted_origins()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        method = request.method.upper()
        if method not in UNSAFE_METHODS:
            return await call_next(request)
        # 只保护携带会话 Cookie 的状态变更请求(Bearer/匿名无 Cookie 不受影响)
        if not request.cookies.get(SESSION_COOKIE_NAME):
            return await call_next(request)
        origin = request.headers.get("origin", "").strip()
        referer = request.headers.get("referer", "").strip()
        # 无来源头的非浏览器客户端(CLI/测试)放行 —— 不来自浏览器导航流
        if not origin and not referer:
            return await call_next(request)
        source = origin or referer
        trusted = self.trusted_origins
        own = _request_own_origin(request)
        if own:
            trusted = trusted | {own}
        if normalize_origin(source) in trusted:
            return await call_next(request)
        logger.warning(
            "CSRF 拒绝: %s %s origin=%r referer=%r host=%r",
            method,
            request.url.path,
            origin,
            referer,
            request.headers.get("host"),
        )
        return csrf_reject_response()
