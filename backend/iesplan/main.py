"""IES Plan 后端应用入口。

提供 create_app() 应用工厂与模块级 app 实例 (uvicorn 入口: iesplan.main:app)。
本阶段仅挂载健康检查路由, 业务 API 路由在后续阶段通过 include_router 追加。
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, FastAPI, Request
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
# 全局异常 → 标准错误信封(与 AppError.to_dict 同构; fix_hint_key/ref_ids 补默认)
# ---------------------------------------------------------------------------

from iesplan.core.errors import AppError


def _error_envelope(
    *,
    code: str,
    message_key: str,
    severity: str = "error",
    blocking: bool = True,
    params: dict[str, Any] | None = None,
    location: dict[str, Any] | None = None,
    fix_hint_key: str = "",
    ref_ids: list[str] | None = None,
) -> dict[str, Any]:
    """构造标准错误响应体 {"error": {...}}, 字段对齐契约中的 Diagnostic。"""
    return {
        "error": {
            "code": code,
            "message_key": message_key,
            "severity": severity,
            "blocking": blocking,
            "params": params or {},
            "location": location,
            "fix_hint_key": fix_hint_key,
            "ref_ids": ref_ids or [],
        }
    }


def _app_error_response(exc: Exception) -> JSONResponse:
    """将 AppError 转换为诊断 JSON 响应 (异常自带 code/http_status 等属性)。"""
    body = _error_envelope(
        code=str(getattr(exc, "code", "API-APP-001")),
        message_key=str(getattr(exc, "message_key", "ies.error.app")),
        severity=str(getattr(exc, "severity", "error")),
        blocking=bool(getattr(exc, "blocking", True)),
        params=getattr(exc, "params", None),
        location=getattr(exc, "location", None),
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
    logger.info("IES Plan API 启动, 版本=%s", __version__)
    _init_database()
    _init_modeling_registry()
    yield
    logger.info("IES Plan API 关闭")


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
            body = _error_envelope(
                code="API-RZ-002",
                message_key="ies.error.registry_unavailable",
                params={"service": "modeling_registry", "detail": _registry_status},
            )
            return JSONResponse(status_code=503, content=body)
        return JSONResponse(status_code=200, content={"status": "ok", "service": APP_NAME, "db": "ok"})

    return router


def _setup_cors(app: FastAPI) -> None:
    """配置 CORS: 允许同域/本地开发来源携带凭据访问。

    来源列表可用环境变量 IESPLAN_CORS_ORIGINS (逗号分隔) 覆盖。
    """
    raw = os.environ.get("IESPLAN_CORS_ORIGINS", "")
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if not origins:
        origins = [
            "http://localhost:3000",
            "http://localhost:5173",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:5173",
        ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
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
        title="IES Plan API",
        description="综合能源系统规划平台后端",
        version=__version__,
        lifespan=lifespan,
    )
    _setup_cors(application)
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
