"""pIES 后端应用入口。

提供 create_app() 应用工厂与模块级 app 实例 (uvicorn 入口: iesplan.main:app)。
本阶段仅挂载健康检查路由, 业务 API 路由在后续阶段通过 include_router 追加。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware

from iesplan import __version__
from iesplan.bootstrap import assemble_api

logger = logging.getLogger(__name__)

APP_NAME = "iesplan"

# ---------------------------------------------------------------------------
# 全局异常 → 标准错误信封(与 AppError.to_dict 同构; 构造器权威源在 core.errors)
# ---------------------------------------------------------------------------

from iesplan.core.errors import AppError, error_envelope as _error_envelope


def _app_error_response(exc: Exception) -> JSONResponse:
    """将 AppError 转换为诊断 JSON 响应 (异常自带 code/http_status 等属性)。"""
    body = _error_envelope(
        code=str(getattr(exc, "code", "API-APP-001")),
        message_key=str(getattr(exc, "message_key", "ies.error.app")),
        severity=str(getattr(exc, "severity", "error")),
        blocking=bool(getattr(exc, "blocking", True)),
        params=getattr(exc, "params", None),
        location=getattr(exc, "location", None),
        fix_hint_key=str(getattr(exc, "fix_hint_key", "") or ""),
        ref_ids=list(getattr(exc, "ref_ids", ()) or ()),
    )
    # 优先采用异常自带的 http_status(403/404/409/413...), 否则兜底 400
    status = getattr(exc, "http_status", None)
    if not isinstance(status, int):
        status = 400
    return JSONResponse(status_code=status, content=body)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期: 启动时经组合根装配, 关闭时记录日志。

    ``assemble_api`` 失败即启动失败: 不捕获、不发布半初始化状态、
    不设 fallback(就绪探针只读已装配上下文的公开健康状态)。
    """
    logger.info("pIES API 启动, 版本=%s", __version__)
    context = assemble_api()
    app.state.bootstrap_context = context
    yield
    logger.info("pIES API 关闭")


def _build_health_router() -> APIRouter:
    """构建健康检查路由器: /api/healthz (存活) 与 /api/readyz (就绪)。"""
    router = APIRouter(prefix="/api", tags=["health"])

    @router.get("/healthz", summary="存活探针")
    async def healthz() -> dict[str, Any]:
        """存活探针: 进程存活即返回 200, 不依赖任何外部资源。"""
        return {
            "status": "ok",
            "service": APP_NAME,
            "version": __version__,
            "time": datetime.now(UTC).isoformat(),
        }

    @router.get("/readyz", summary="就绪探针")
    async def readyz(request: Request) -> JSONResponse:
        """就绪探针: API 实际必需的已装配能力(db/storage/registry)均就绪返回 200, 缺一 503。

        只消费组合根 ``ApplicationContext.readiness()`` 的公开结果, 不直接探活依赖;
        未装配(启动失败则进程根本不起 serving, 此处为防御)同样 503, 不 fallback。
        """
        context = getattr(request.app.state, "bootstrap_context", None)
        readiness: dict[str, Any] = (
            context.readiness() if context is not None else {"ready": False, "health": {}}
        )
        health: dict[str, str] = dict(readiness.get("health", {}))
        # API 进程实际必需的已装配能力(与 assemble_api 装配子集一致): 缺一即 503
        required: tuple[tuple[str, str, str, dict[str, str]], ...] = (
            ("db", "API-RZ-001", "ies.error.db_unavailable", {"service": "db"}),
            ("storage", "API-RZ-003", "ies.error.storage_unavailable", {"service": "storage"}),
            (
                "registry",
                "API-RZ-002",
                "ies.error.registry_unavailable",
                {"service": "modeling_registry", "detail": "unavailable"},
            ),
        )
        for capability, code, message_key, params in required:
            if health.get(capability) != "ok":
                # A3 脱敏: 探活失败的原始异常串(可能含内部路径/凭证/堆栈)
                # 只进日志(bootstrap 侧 _check_* 已记录), 探针响应只给服务标识, 不泄详情
                body = _error_envelope(
                    code=code,
                    message_key=message_key,
                    params=params,
                )
                return JSONResponse(status_code=503, content=body)
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "service": APP_NAME,
                "db": "ok",
                "storage": "ok",
                "registry": "ok",
            },
        )

    return router


def _setup_middleware(app: FastAPI) -> None:
    """注册资源使用边界中间件(0.2.0 A4): 全局限流(按 IP)。

    只限流高成本端点与合理全局阈值; 健康/就绪探针与登录接口豁免
    (登录已有用户名级限速)。关闭开关时透明放行。
    """
    from iesplan.api.limits import RateLimitMiddleware

    app.add_middleware(RateLimitMiddleware)


def _setup_cors(app: FastAPI) -> None:
    """配置 CORS: 允许同域/本地开发来源携带凭据访问。

    来源列表可用环境变量 IESPLAN_CORS_ORIGINS (逗号分隔) 覆盖。
    CSRF 中间件信任同一来源清单(iesplan.api.csrf.cors_origin_list),
    保证「允许跨域携带凭据的来源」与「CSRF 校验信任的来源」一致。
    """
    from iesplan.api.csrf import cors_origin_list

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origin_list(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def _register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理: 404/AppError/未捕获异常统一输出标准错误 JSON。"""

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """HTTP 异常: 404 路由未找到, 其余透出状态码与 detail。"""
        if exc.status_code == 404:
            body = _error_envelope(
                code="API-NF-001",
                message_key="ies.error.route_not_found",
                params={"path": request.url.path},
            )
        else:
            body = _error_envelope(
                code=f"HTTP-{exc.status_code}",
                message_key="ies.error.http_exception",
                params={"status_code": exc.status_code, "detail": str(exc.detail)},
            )
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """FastAPI/Pydantic 请求体校验失败: 422 + 标准 8 字段信封。

        码复用 API-REQ-001: 与业务域"请求无效"(projects/datasets 的 empty_file
        / invalid_json / invalid_resolution 等)同码, 均表示"请求体不可处理"
        —— message_key 区分文案(本路径用 ies.error.invalid_request, 业务域
        用具体字段级键如 ies.error.empty_file)。

        校验错误定位到字段路径与消息, 进 params.errors 数组; 当前端点
        与方法进 params.location 以辅助前端定位。params 不直接渲染进文案,
        避免文案键膨胀。
        """
        errors = [
            {
                "loc": ".".join(str(p) for p in e.get("loc", ())),
                "msg": e.get("msg", ""),
                "type": e.get("type", ""),
            }
            for e in exc.errors()
        ]
        body = _error_envelope(
            code="API-REQ-001",
            message_key="ies.error.invalid_request",
            params={"errors": errors, "count": len(errors)},
            location={"path": request.url.path, "method": request.method},
        )
        return JSONResponse(status_code=422, content=body)

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """未捕获异常: AppError 映射为诊断 JSON, 其余返回 500 且不泄露堆栈。"""
        if isinstance(exc, AppError):
            return _app_error_response(exc)
        # 完整堆栈仅写入日志, 响应只含通用错误体
        logger.exception(
            "未捕获异常: %s %s",
            request.method,
            request.url.path,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return JSONResponse(
            status_code=500,
            content=_error_envelope(code="API-500-001", message_key="ies.error.internal"),
        )


def _register_business_routers(application: FastAPI) -> None:
    """挂载全部业务 API 路由(集成阶段汇总, 按域分组)。

    挂载顺序说明(STO-07): 对象存储路由(objects)只提供 /storage 与
    /storage/health; 全系统运维健康(/admin/health)由独立聚合层 health
    提供, 两者不重复定义路径, 无兼容并集。
    """
    from iesplan.api import (
        admin,
        auth,
        config,
        config_revisions,
        datasets,
        exports,
        health,
        model,
        model_templates,
        objects,
        project_models,
        projects,
        results,
        tasks,
        validation,
    )

    # 身份与认证(U01, 窗口会话凭证)
    application.include_router(auth.router)
    # 管理维护: 存储路由(objects) + 运维健康聚合(health) + admin 独有端点
    application.include_router(objects.router)
    application.include_router(health.router)
    application.include_router(admin.router)
    # 项目(U02/U03)
    application.include_router(projects.router)
    # 项目模型候选门禁与保存(切片 dm2-A)
    application.include_router(project_models.router)
    # 用户自定义模型模板(切片 dm2: 完整生命周期 + 项目模板目录)
    application.include_router(model_templates.router)
    # 系统模型(U04) + 设备类型注册表(公开)
    application.include_router(model.registry_router)
    application.include_router(model.model_router)
    # 数据集(U05) + 模板
    application.include_router(datasets.router)
    # 计算配置(U06) + 算法注册表
    application.include_router(config.config_router)
    application.include_router(config.registry_router)
    # 规划/财务三件套配置 revision(0.6.5 条目 1-2; 含地区 Profile 注册表)
    application.include_router(config_revisions.router)
    application.include_router(config_revisions.profile_router)
    # 校验(U07)
    application.include_router(validation.router)
    # 任务(U08)
    application.include_router(tasks.router)
    # 结果(U09/U12/U14)
    application.include_router(results.router)
    # 导出(U14/U15)
    application.include_router(exports.router)


def create_app() -> FastAPI:
    """创建 FastAPI 应用: 中间件、健康路由、根路由、业务路由与全局异常处理。"""
    application = FastAPI(
        title="pIES API",
        description="综合能源系统规划平台后端",
        version=__version__,
        lifespan=lifespan,
    )
    _setup_cors(application)
    _setup_middleware(application)
    # CSRF 防护: Cookie 会话状态变更请求的双源校验(切片 A2)
    from iesplan.api.csrf import CSRFOriginGuardMiddleware, build_trusted_origins

    application.add_middleware(CSRFOriginGuardMiddleware, trusted_origins=build_trusted_origins())
    # 挂载 API 路由: 健康检查 + 全部业务路由
    application.include_router(_build_health_router())
    _register_business_routers(application)

    @application.get("/api", tags=["meta"], summary="服务元信息")
    async def api_root() -> dict[str, str]:
        """返回服务名称、版本与文档地址。"""
        return {"name": APP_NAME, "version": __version__, "docs": "/docs"}

    _register_exception_handlers(application)
    return application


# uvicorn 入口实例 (uvicorn iesplan.main:app)
app = create_app()
