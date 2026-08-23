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
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware

from iesplan import __version__

logger = logging.getLogger(__name__)

#: 建模命令注册表启动状态("ok" 或错误描述; 就绪探针上报, 03 §5.2)
_registry_status: str = "pending"

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


def _db_available() -> bool:
    """最小数据库连通性检查: 建立会话并执行 SELECT 1。"""
    try:
        from iesplan.db import SessionLocal
    except Exception:
        logger.warning("iesplan.db 模块不可用, 数据库视为不可用")
        return False
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("数据库连通性检查失败")
        return False


def _init_database() -> None:
    """启动时幂等初始化数据库: init_db() + seed_admin()。

    并行阶段 db 模块可能尚未就绪或数据库暂不可用, 仅记录日志不阻断启动,
    数据库状态由 /api/readyz 上报。
    """
    try:
        from iesplan.db import init_db

        init_db()
    except Exception:
        logger.exception("启动时数据库初始化失败, 应用继续运行, 就绪检查将返回 503")


def _init_modeling_registry() -> None:
    """启动时注册建模命令(设备目录 → 标准调用命令, 03 §5.2)。

    任一设备校验/命令生成失败 → 抛 AppError 阻断启动(受控加载语义,
    避免生产进程运行在空命令注册表上)。注册表不可用时记录日志并把状态
    记入 ``_registry_status`` 供 /api/readyz 上报(API 不阻断启动,
    但就绪探针返回 503, 容器编排不会把流量切到半初始化实例)。
    """
    global _registry_status
    try:
        from iesplan.devices import init_registry
        from iesplan.modeling.registry_loader import register_catalog_commands

        init_registry()  # 设备 YAML 注册表(插件式, 供装配检查/端口派生)
        register_catalog_commands()  # 建模命令注册表(标准调用命令)
        _registry_status = "ok"
    except Exception as exc:
        logger.exception("启动时建模命令注册失败(受控加载), 应用继续运行但命令注册表为空")
        _registry_status = f"error: {exc}"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期: 启动时初始化数据库与建模命令注册表, 关闭时记录日志。"""
    logger.info("pIES API 启动, 版本=%s", __version__)
    _init_database()
    _init_modeling_registry()
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
    async def readyz() -> JSONResponse:
        """就绪探针: 数据库与建模命令注册表均可用返回 200, 否则 503。"""
        if not _db_available():
            body = _error_envelope(
                code="API-RZ-001",
                message_key="ies.error.db_unavailable",
                params={"service": "db"},
            )
            return JSONResponse(status_code=503, content=body)
        if _registry_status != "ok":
            # A3 脱敏: 注册表初始化失败的原始异常串(可能含内部路径/凭证/堆栈)
            # 只进日志(_init_modeling_registry 已记录), 探针响应只给服务标识, 不泄详情
            body = _error_envelope(
                code="API-RZ-002",
                message_key="ies.error.registry_unavailable",
                params={"service": "modeling_registry", "detail": "unavailable"},
            )
            return JSONResponse(status_code=503, content=body)
        return JSONResponse(status_code=200, content={"status": "ok", "service": APP_NAME, "db": "ok"})

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
        datasets,
        exports,
        health,
        model,
        objects,
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
    # 系统模型(U04) + 设备类型注册表(公开)
    application.include_router(model.registry_router)
    application.include_router(model.model_router)
    # 数据集(U05) + 模板
    application.include_router(datasets.router)
    # 计算配置(U06) + 算法注册表
    application.include_router(config.config_router)
    application.include_router(config.registry_router)
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
