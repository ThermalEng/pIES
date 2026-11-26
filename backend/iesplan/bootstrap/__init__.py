"""应用组合根(ADR-0016 五容器收敛)。

系统唯一知道"这次部署具体使用哪些实现"的位置: 统一装配 ``backend``
进程所需的数据库、对象存储、设备注册表、生成器/预测器目录、内置容器阶段目录、
``job_runner`` 客户端及可选嵌入式调度器, 输出完整 ``ApplicationContext``
或明确启动失败。

- ``assemble_api``: 装配 database + storage + devices 2.0 注册表 +
  生成器/预测器目录 + 内置阶段目录 + (配置 ``IESPLAN_JOB_RUNNER_URL`` 时)
  ``JobRunnerClient`` 与 ``ContainerStageRunner``;
- ``assemble_embedded_scheduler``: 在 ``IESPLAN_EMBEDDED_SCHEDULER=true`` 时
  为 ``backend`` lifespan 装配隔离有界计算/IO/构建执行池。

失败语义: 任一必需依赖装配失败即抛异常(启动失败), 不发布半初始化
状态, 不设 fallback。provider 选择与装配只归本组合根；API/调度层
不得自行发现或动态加载 provider，不做动态扫描。

本包只做装配与健康聚合, 不实现业务规则, 不新增全局 registry(装配结果
由调用方持有, API 进程挂在 ``app.state.bootstrap_context``)。

领域 ORM 在拥有者领域公开门面加载时完成 metadata 注册；触发器规则由
各领域公开门面导出。本包只静态导入公开门面并收集真实触发器语句，不设
无行为的表安装钩子，也不做字符串驱动的动态导入。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from iesplan import audit as audit_domain
from iesplan import configuration as configuration_domain
from iesplan import dataset as dataset_domain
from iesplan import identity as identity_domain
from iesplan import model as model_domain
from iesplan import package as package_domain  # noqa: F401 - 加载本域 ORM 声明
from iesplan import project as project_domain
from iesplan import results as results_domain
from iesplan import storage as storage_domain  # noqa: F401 - 加载本域 ORM 声明
from iesplan import tasks as tasks_domain
from iesplan.identity import IMMUTABLE_TABLES as IDENTITY_IMMUTABLE_TABLES

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ApplicationContext:
    """一次装配的完整应用上下文(只暴露模块公开门面与健康能力)。"""

    #: 会话工厂(已装配的数据库连接能力, 如 ``iesplan.db.SessionLocal``)
    session_factory: Any
    #: 已装配的存储适配能力对象或其公开句柄(未装配该能力时为 None)
    storage: Any | None = None
    #: devices 2.0 注册状态对象(未装配该能力时为 None)
    device_registry: Any | None = None
    #: 已发布的生成器目录(未装配该能力时为 None; 内置真实 provider
    #: 由组合根显式注册，未知引用选择时即明确不可用)
    generator_registry: Any | None = None
    #: 已发布的预测算法目录(未装配该能力时为 None; 内置朴素 predictor
    #: 由组合根显式注册，未知引用选择时即明确不可用)
    predictor_registry: Any | None = None
    #: 已发布的求解器目录(未装配该能力时为 None)
    solver_registry: Any | None = None
    #: 已发布的评价器目录(未装配该能力时为 None)
    evaluator_registry: Any | None = None
    #: 已发布的一体化算法目录(未装配该能力时为 None)
    algorithm_registry: Any | None = None
    #: 内置计算阶段容器描述符目录
    builtin_stage_catalog: Any | None = None
    #: 临时计算容器执行策略(来自 pies.toml)
    container_stage_policy: Any | None = None
    #: job_runner 代理客户端(仅配置 IESPLAN_JOB_RUNNER_URL 时装配)
    job_runner_client: Any | None = None
    #: 6 步临时容器阶段执行器(仅配置 IESPLAN_JOB_RUNNER_URL 时装配)
    stage_runner: Any | None = None

    def compute_execution_plane(self) -> Any | None:
        """容器计算执行面句柄：生成器/预测目录与容器阶段目录/执行器齐备才装配。"""
        if (
            self.generator_registry is None
            or self.predictor_registry is None
            or self.builtin_stage_catalog is None
            or self.stage_runner is None
        ):
            return None
        from iesplan.application.worker import ComputeExecutionPlane

        return ComputeExecutionPlane(
            generator_registry=self.generator_registry,
            predictor_registry=self.predictor_registry,
            stage_runner=self.stage_runner,
            stage_catalog=self.builtin_stage_catalog,
            solver_registry=self.solver_registry,
            evaluator_registry=self.evaluator_registry,
            algorithm_registry=self.algorithm_registry,
            evaluator_max_concurrency=int(
                self.container_stage_policy.evaluator_max_concurrency
                if self.container_stage_policy is not None
                else 1
            ),
        )

    def health(self) -> dict[str, str]:
        """各已装配依赖的健康状态(值仅为 ``"ok"`` 或 ``"unavailable"``)。"""
        result: dict[str, str] = {}
        result["db"] = "ok" if _check_database(self.session_factory) else "unavailable"
        if self.storage is not None:
            result["storage"] = "ok" if _check_storage(self.storage) else "unavailable"
        if self.device_registry is not None:
            result["registry"] = "ok" if _check_registry(self.device_registry) else "unavailable"
        if self.generator_registry is not None:
            result["generators"] = (
                "ok" if _check_generator_registry(self.generator_registry) else "unavailable"
            )
        if self.builtin_stage_catalog is not None:
            result["stages"] = (
                "ok" if _check_stage_catalog(self.builtin_stage_catalog) else "unavailable"
            )
        if self.job_runner_client is not None:
            result["job_runner"] = (
                "ok" if _check_job_runner(self.job_runner_client) else "unavailable"
            )
        return result

    def readiness(self) -> dict[str, Any]:
        """公开就绪结果: ``{"ready": 是否全部就绪, "health": 各能力健康}``。

        就绪探针(``iesplan.main`` readyz)只消费本结果, 不直接探活依赖;
        ``health`` 的键为已装配能力(``db`` / ``storage`` / ``registry`` /
        ``generators``), 值仅为 ``"ok"`` 或 ``"unavailable"``。
        """
        health = self.health()
        ready = bool(health) and all(v == "ok" for v in health.values())
        return {"ready": ready, "health": health}

    def ready(self) -> bool:
        """已装配的依赖是否全部就绪(任一 ``unavailable`` 即 False)。"""
        return bool(self.readiness()["ready"])


def _check_database(session_factory: Any) -> bool:
    """最小数据库连通性检查: 建立会话并执行 SELECT 1。"""
    try:
        from sqlalchemy import text

        with session_factory() as session:
            session.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("组合根数据库探活失败")
        return False


def _check_storage(storage: Any) -> bool:
    """存储适配能力检查: 已装配的公开句柄可响应存在性探针(只验证装配, 不判容量)。

    """
    try:
        storage.exists("objects/.health-probe")
        return True
    except Exception:
        logger.exception("组合根存储探活失败")
        return False


def _check_registry(registry: Any) -> bool:
    """devices 2.0 注册状态检查: 注册表可给出确定性快照。"""
    try:
        registry.snapshot()
        return True
    except Exception:
        logger.exception("组合根设备注册表探活失败")
        return False


def _check_generator_registry(registry: Any) -> bool:
    """生成器目录装配检查: 已发布目录可给出确定性快照。

    只验证目录本身装配健康；空表仍健康，未注册引用不可用语义由选择时
    ``ComputationUnavailableError`` 表达，不在此污染就绪状态。
    """
    try:
        registry.snapshot()
        return True
    except Exception:
        logger.exception("组合根生成器目录探活失败")
        return False


def _check_stage_catalog(catalog: Any) -> bool:
    """内置容器阶段目录检查: 至少包含四个内置阶段描述符。"""
    try:
        return len(catalog.snapshot()) >= 4
    except Exception:
        logger.exception("组合根内置阶段目录探活失败")
        return False


def _check_job_runner(client: Any) -> bool:
    """job_runner 代理可达性检查: 失败如实返回 False, 绝不伪造 ok。"""
    try:
        payload = client.check_ready()
        return isinstance(payload, dict) and payload.get("status") in ("ready", "ok")
    except Exception:
        logger.exception("组合根 job_runner 探活失败")
        return False


def _container_stage_policy_from_settings() -> Any:
    """从统一 Settings 构造临时容器阶段策略(唯一权威来自 pies.toml)。"""
    from iesplan.computation.runtime.stage_runner import ContainerStagePolicy
    from iesplan.config import settings

    return ContainerStagePolicy(
        builtin_image=settings.stage_builtin_image,
        cpu_cores=float(settings.solver_max_cpu_cores),
        memory_mb=int(settings.solver_max_memory_mib),
        pids_limit=int(settings.stage_pids_limit),
        tmpfs_size_mb=int(settings.stage_tmpfs_size_mb),
        stop_grace_seconds=int(settings.stage_stop_grace_seconds),
        max_timeout_seconds=float(settings.solver_max_timeout_seconds),
        allowed_environment_vars=tuple(settings.solver_allowed_environment_vars),
        evaluator_max_concurrency=int(settings.stage_evaluator_max_concurrency),
    )


def _resolve_queue_mode() -> str:
    """解析队列模式: 组合根唯一读取 IESPLAN_QUEUE 的位置(默认 auto)。"""
    import os

    return os.environ.get("IESPLAN_QUEUE", "auto").lower()


def _configure_queue_mode(mode: str) -> None:
    """配置任务队列模式。"""
    from iesplan.tasks import queue as queue_module

    queue_module.configure_queue_mode(mode)


def collect_trigger_statements() -> tuple[str, ...]:
    """编排收集各领域触发器部署语句: 按顺序拼接拥有者公开门面 install_triggers() 结果。

    返回可直接传给 ``db.init_db(trigger_statements=...)`` 的执行序语句序列
    (各域自带幂等 DROP, 语句内不含业务规则解释, 只做拼接; 无触发器的域
    不参评, 按能力缺席而非空函数占位)。
    """
    statements: list[str] = []
    statements.extend(identity_domain.install_triggers())
    statements.extend(project_domain.install_triggers())
    statements.extend(dataset_domain.install_triggers())
    statements.extend(tasks_domain.install_triggers())
    statements.extend(results_domain.install_triggers())
    statements.extend(audit_domain.install_triggers())
    statements.extend(configuration_domain.install_triggers())
    return tuple(statements)


def collect_immutable_tables() -> tuple[str, ...]:
    """编排收集各领域不可变表清单(各拥有者公开门面 IMMUTABLE_TABLES 之和, 按触发器编排序)。

    清单唯一真相仍归各域 persistence 所有(门面只做引用重导出), 本包不建
    第二份表名清单; 无 IMMUTABLE_TABLES 的域按能力缺席。
    """
    tables: list[str] = []
    tables.extend(IDENTITY_IMMUTABLE_TABLES)
    tables.extend(getattr(project_domain, "IMMUTABLE_TABLES", ()))
    tables.extend(getattr(dataset_domain, "IMMUTABLE_TABLES", ()))
    tables.extend(getattr(tasks_domain, "IMMUTABLE_TABLES", ()))
    tables.extend(getattr(results_domain, "IMMUTABLE_TABLES", ()))
    tables.extend(getattr(audit_domain, "IMMUTABLE_TABLES", ()))
    tables.extend(getattr(configuration_domain, "IMMUTABLE_TABLES", ()))
    tables.extend(getattr(model_domain, "IMMUTABLE_TABLES", ()))
    return tuple(tables)


def _initialize_database() -> None:
    """服务入口初始化数据库；任务子进程只使用已初始化的数据库。"""
    from iesplan import db as db_module
    from iesplan.application.identity import seed_builtin_admin

    db_module.init_db(trigger_statements=collect_trigger_statements())
    with db_module.SessionLocal() as session:
        seed_builtin_admin(session)


def _assemble(
    *,
    with_storage: bool,
    with_registry: bool,
    with_generators: bool,
    with_execution_plane: bool,
    check_readiness: bool = False,
) -> ApplicationContext:
    """装配进程内连接、目录和执行能力，不执行数据库部署或身份播种。

    异常直接抛给调用方(启动失败, 不发布半初始化状态)。
    """
    from iesplan import db as db_module

    # 0. 队列后端选型(组合根唯一环境解释点): 一次解析部署环境,
    #    扇出给各接收模块; 业务模块只接收传入配置, 不再自行解释环境选型。
    _configure_queue_mode(_resolve_queue_mode())
    # 1. 存储适配能力(公开句柄)
    storage: Any | None = None
    if with_storage:
        from iesplan.storage.service import get_blob_store

        storage = get_blob_store()
    # 2. devices 2.0 注册表(任一设备校验失败即整体拒绝, 原子发布)
    device_registry: Any | None = None
    if with_registry:
        from iesplan.devices import init_registry

        device_registry = init_registry()
    # 3. 生成器目录(原子发布; 显式注册内置真实 provider, 不做任何
    #    动态发现或扫描; 任一候选不符即整体失败, 不发布半表)
    generator_registry: Any | None = None
    predictor_registry: Any | None = None
    solver_registry: Any | None = None
    evaluator_registry: Any | None = None
    algorithm_registry: Any | None = None
    if with_generators:
        from iesplan.computation.config import (
            BUILTIN_EVALUATOR_FINANCE_SPEC,
            BUILTIN_SOLVER_HIGHS_SPEC,
            build_algorithm_registry,
            build_evaluator_registry,
            build_solver_registry,
        )
        from iesplan.computation.generators import (
            HIGHS_MILP_PROVIDER,
            build_registry,
        )
        from iesplan.computation.predictors import (
            NAIVE_PREDICTOR,
            build_predictor_registry,
        )

        generator_registry = build_registry((HIGHS_MILP_PROVIDER,))
        predictor_registry = build_predictor_registry((NAIVE_PREDICTOR,))
        solver_registry = build_solver_registry((BUILTIN_SOLVER_HIGHS_SPEC,))
        evaluator_registry = build_evaluator_registry((BUILTIN_EVALUATOR_FINANCE_SPEC,))
        algorithm_registry = build_algorithm_registry(())
    # 4. 容器化阶段目录与 job_runner 客户端(所有计算通过 job_runner 临时容器执行)
    builtin_stage_catalog: Any | None = None
    container_stage_policy: Any | None = None
    job_runner_client: Any | None = None
    stage_runner: Any | None = None
    if with_generators or with_execution_plane:
        from iesplan.computation.runtime.job_runner_client import JobRunnerClient
        from iesplan.computation.runtime.stage_runner import (
            ContainerStageRunner,
            build_builtin_stage_catalog,
        )
        from iesplan.config import settings

        container_stage_policy = _container_stage_policy_from_settings()
        builtin_stage_catalog = build_builtin_stage_catalog(settings.stage_builtin_image)
        if settings.job_runner_url.strip():
            job_runner_client = JobRunnerClient(settings.job_runner_url.strip())
            stage_runner = ContainerStageRunner(
                job_runner_client,
                stage_root=settings.stage_root,
                policy=container_stage_policy,
            )
    context = ApplicationContext(
        session_factory=db_module.SessionLocal,
        storage=storage,
        device_registry=device_registry,
        generator_registry=generator_registry,
        predictor_registry=predictor_registry,
        solver_registry=solver_registry,
        evaluator_registry=evaluator_registry,
        algorithm_registry=algorithm_registry,
        builtin_stage_catalog=builtin_stage_catalog,
        container_stage_policy=container_stage_policy,
        job_runner_client=job_runner_client,
        stage_runner=stage_runner,
    )
    logger.info(
        "组合根装配完成: storage=%s registry=%s generators=%s stage_runner=%s",
        "assembled" if storage is not None else "skipped",
        (
            f"assembled({len(device_registry.snapshot())} devices)"
            if device_registry is not None
            else "skipped"
        ),
        (
            f"assembled({len(generator_registry.snapshot())} generators)"
            if generator_registry is not None
            else "skipped"
        ),
        "assembled" if stage_runner is not None else "skipped",
    )
    return context


def assemble_api() -> ApplicationContext:
    """装配 backend 进程：database、storage、devices 2.0 注册表、生成器/预测器目录、
    内置容器阶段目录与 (配置 job_runner_url 时的) ContainerStageRunner。
    """
    _initialize_database()
    return _assemble(
        with_storage=True,
        with_registry=True,
        with_generators=True,
        with_execution_plane=False,
    )


def assemble_embedded_scheduler(
    context: ApplicationContext,
    *,
    compute: Any = None,
    poll_interval: float = 0.5,
) -> Any:
    """装配嵌入式后台调度器(仅在 IESPLAN_EMBEDDED_SCHEDULER=true 时由 lifespan 启停)。

    计算、IO 与插件构建各自使用独立有界执行池，分别受 pies.toml 中
    compute_slots / io_slots / build_slots 控制，与 HTTP 请求线程完全隔离。
    """
    from iesplan.config import settings
    from iesplan.worker import EmbeddedScheduler

    resolved_compute = compute if compute is not None else context.compute_execution_plane()
    if resolved_compute is None and settings.job_runner_url.strip():
        raise RuntimeError(
            "EmbeddedScheduler requires a complete compute execution plane when "
            "IESPLAN_JOB_RUNNER_URL is configured"
        )
    return EmbeddedScheduler(
        session_factory=context.session_factory,
        compute_slots=settings.compute_slots,
        io_slots=settings.io_slots,
        build_slots=settings.build_slots,
        compute=resolved_compute,
        stage_runner=context.stage_runner,
        poll_interval=poll_interval,
    )


__all__ = [
    "ApplicationContext",
    "assemble_api",
    "assemble_embedded_scheduler",
    "collect_immutable_tables",
    "collect_trigger_statements",
]
