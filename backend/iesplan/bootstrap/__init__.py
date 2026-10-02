"""应用组合根(backend-decoupling Wave3)。

系统唯一知道"这次部署具体使用哪些实现"的位置: 按进程角色
(API / compute Worker / I/O Worker)装配所需能力子集, 输出完整
``ApplicationContext`` 或明确启动失败。

- ``assemble_api``: 装配 database + storage + devices 2.0 注册表 +
  生成器目录(显式注册内置真实 provider)；不装配执行面（执行器目录/
  运行时/执行策略只归计算 Worker）;
- ``assemble_compute_worker``: 装配 database + storage + devices 2.0
  注册表 + 生成器目录 + 执行器目录(含 HiGHS 本地进程绑定) + 结果适配器
  目录 + 统一 Settings 派生的 DeploymentPolicy + 通用 SolverRuntime；
  任一缺失即拒绝启动，不领取无法执行的计算任务；
- ``assemble_io_worker``: I/O 所需子集(database + storage, 不含 devices
  注册表与生成器目录）。

失败语义: 任一必需依赖装配失败即抛异常(启动失败), 不发布半初始化
状态, 不设 fallback。provider 选择与装配只归本组合根；API/Worker
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
    #: 已发布的执行器目录(只归计算 Worker 装配; API 不装配执行面)
    executor_registry: Any | None = None
    #: 已发布的结果适配器目录(只归计算 Worker 装配)
    result_adapter_registry: Any | None = None
    #: 通用求解运行时(只归计算 Worker 装配)
    solver_runtime: Any | None = None
    #: 统一 Settings 派生的部署执行策略(只归计算 Worker 装配)
    deployment_policy: Any | None = None

    def compute_execution_plane(self) -> Any | None:
        """计算执行面句柄：生成器/执行器/适配器目录与运行时四件齐备才装配。

        任一缺席返回 None（计算 Worker 构造即拒绝，不领取任务）。
        """
        if (
            self.generator_registry is None
            or self.executor_registry is None
            or self.result_adapter_registry is None
            or self.solver_runtime is None
        ):
            return None
        from iesplan.application.worker import ComputeExecutionPlane

        return ComputeExecutionPlane(
            generator_registry=self.generator_registry,
            executor_registry=self.executor_registry,
            adapter_registry=self.result_adapter_registry,
            runtime=self.solver_runtime,
        )

    def health(self) -> dict[str, str]:
        """各已装配依赖的健康状态(值仅为 ``"ok"`` 或 ``"unavailable"``)。

        - ``db``: 始终上报(会话 SELECT 1 探活);
        - ``storage`` / ``registry`` / ``generators`` / ``executors``:
          仅在装配了该能力时上报; 未装配(能力子集, 如 Worker 上下文)
          时不出现该键, 不伪装健康。
        - ``generators`` / ``executors`` 只探针目录本身可确定性快照
          (装配健康), 空表仍为 ``"ok"``; 未注册引用不可用语义在选择时
          表达(``get``/``resolve`` 抛 ``ComputationUnavailableError``),
          不污染就绪状态。
        探针失败原因只进日志, 不进入返回值(避免内部细节外泄)。
        """
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
        if self.executor_registry is not None:
            result["executors"] = (
                "ok" if _check_executor_registry(self.executor_registry) else "unavailable"
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

    容量门禁由写路径 ``check_capacity`` 与运维健康视图负责, 此处仅确认
    句柄真实可用(异常即不可用)。
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


def _check_executor_registry(registry: Any) -> bool:
    """执行器目录装配检查: 已发布目录可给出确定性快照(语义同生成器目录)。"""
    try:
        registry.snapshot()
        return True
    except Exception:
        logger.exception("组合根执行器目录探活失败")
        return False


def _deployment_policy_from_settings() -> Any:
    """从统一 Settings 构造部署执行策略(组合根唯一装配点，不复制常量)。

    数值与 allowlist 全部取自 pies.toml 经 Settings 类型化的部署值；
    Settings 缺失/非法在进程启动时已失败，此处直接装配、不重复校验。
    """
    from iesplan.computation.runtime import DeploymentPolicy
    from iesplan.config import settings

    return DeploymentPolicy(
        executables=tuple(settings.solver_allowed_executables),
        environment_vars=tuple(settings.solver_allowed_environment_vars),
        max_timeout_seconds=settings.solver_max_timeout_seconds,
        max_memory_mib=settings.solver_max_memory_mib,
        max_cpu_cores=settings.solver_max_cpu_cores,
        allow_network=settings.solver_allow_network,
        max_output_files=settings.solver_max_output_files,
        max_output_bytes=settings.solver_max_output_bytes,
    )


def _assemble_execution_plane(policy: Any) -> tuple[Any, Any, Any]:
    """装配计算执行面三件套(执行器目录 + 适配器目录 + 通用运行时)。

    HiGHS 本地进程绑定与结果适配器均为组合根显式注册的内置真实
    provider；任一不可用(缺二进制/版本自检失败)即整体拒绝，不发布
    半表，不回退。执行器引用取生成器声明的配套值，不另存第二份。
    """
    from iesplan.computation import (
        HIGHS_ADAPTER,
        HIGHS_ADAPTER_PATH,
        HIGHS_EXECUTABLE_REF,
        HIGHS_VERSION,
        ExecutableReadiness,
        ExecutorDescriptor,
        LocalExecutable,
        LocalProcessProvider,
        SolverRuntime,
        build_executor_registry,
        build_result_adapter_registry,
    )
    from iesplan.computation.generators.highs_milp import EXECUTOR_REF
    from iesplan.computation.runtime import validate_highs_arguments

    executor_id, _, executor_version = EXECUTOR_REF.rpartition("@")
    local_provider = LocalProcessProvider(
        ExecutorDescriptor(
            executor_id=executor_id,
            version=executor_version,
            executables=(HIGHS_EXECUTABLE_REF,),
        ),
        (
            LocalExecutable(
                ref=HIGHS_EXECUTABLE_REF,
                path=HIGHS_ADAPTER_PATH,
                argv_validator=validate_highs_arguments,
                readiness=ExecutableReadiness(
                    version=HIGHS_VERSION,
                    self_test=("--self-test",),
                    output_path="self-test.sol",
                ),
            ),
        ),
        base_environ={},
    )
    executor_registry = build_executor_registry([local_provider])
    adapter_registry = build_result_adapter_registry([HIGHS_ADAPTER])
    runtime = SolverRuntime(executor_registry, policy)
    return executor_registry, adapter_registry, runtime


def _resolve_queue_mode() -> str:
    """解析队列模式: 组合根唯一读取 IESPLAN_QUEUE 的位置(默认 auto)。"""
    import os

    return os.environ.get("IESPLAN_QUEUE", "auto").lower()


def _configure_queue_mode(mode: str) -> None:
    """把队列模式扇出给各接收模块(backend 实现选择只归组合根)。"""
    from iesplan import identity as identity_module
    from iesplan.api import limits as limits_module
    from iesplan.tasks import queue as queue_module

    queue_module.configure_queue_mode(mode)
    limits_module.configure_queue_mode(mode)
    identity_module.configure_queue_mode(mode)


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
    tables.extend(getattr(identity_domain, "IMMUTABLE_TABLES", ()))
    tables.extend(getattr(project_domain, "IMMUTABLE_TABLES", ()))
    tables.extend(getattr(dataset_domain, "IMMUTABLE_TABLES", ()))
    tables.extend(getattr(tasks_domain, "IMMUTABLE_TABLES", ()))
    tables.extend(getattr(results_domain, "IMMUTABLE_TABLES", ()))
    tables.extend(getattr(audit_domain, "IMMUTABLE_TABLES", ()))
    tables.extend(getattr(configuration_domain, "IMMUTABLE_TABLES", ()))
    tables.extend(getattr(model_domain, "IMMUTABLE_TABLES", ()))
    return tuple(tables)


def _assemble(
    *,
    with_storage: bool,
    with_registry: bool,
    with_generators: bool,
    with_execution_plane: bool,
    seed_admin: bool,
) -> ApplicationContext:
    """通用装配：加载领域门面后建表、部署触发器并装配能力子集。

    异常直接抛给调用方(启动失败, 不发布半初始化状态)。
    """
    from iesplan import db as db_module

    # 0. 队列/限速后端选型(组合根唯一环境解释点): 一次解析部署环境,
    #    扇出给各接收模块; 业务模块只接收传入配置, 不再自行解释环境选型。
    _configure_queue_mode(_resolve_queue_mode())
    # 1. 领域公开门面已在模块加载时注册各自 ORM；此处建表并部署触发器。
    db_module.init_db(trigger_statements=collect_trigger_statements())
    # 2. 内置身份种子(幂等)
    if seed_admin:
        from iesplan.application.identity import seed_builtin_admin

        with db_module.SessionLocal() as session:
            seed_builtin_admin(session)
    # 3. 存储适配能力(公开句柄)
    storage: Any | None = None
    if with_storage:
        from iesplan.storage.service import get_blob_store

        storage = get_blob_store()
    # 4. devices 2.0 注册表(任一设备校验失败即整体拒绝, 原子发布)
    device_registry: Any | None = None
    if with_registry:
        from iesplan.devices import init_registry

        device_registry = init_registry()
    # 5. 生成器目录(原子发布; 显式注册内置真实 provider, 不做任何
    #    动态发现或扫描; 任一候选不符即整体失败, 不发布半表)
    generator_registry: Any | None = None
    if with_generators:
        from iesplan.computation.generators import (
            HIGHS_MILP_PROVIDER,
            build_registry,
        )

        generator_registry = build_registry((HIGHS_MILP_PROVIDER,))
    # 6. 计算执行面(只归计算 Worker: 执行器/适配器目录 + 通用运行时 +
    #    统一 Settings 派生的部署策略; 任一缺失即拒绝启动)
    executor_registry: Any | None = None
    result_adapter_registry: Any | None = None
    solver_runtime: Any | None = None
    deployment_policy: Any | None = None
    if with_execution_plane:
        deployment_policy = _deployment_policy_from_settings()
        executor_registry, result_adapter_registry, solver_runtime = _assemble_execution_plane(
            deployment_policy
        )
    context = ApplicationContext(
        session_factory=db_module.SessionLocal,
        storage=storage,
        device_registry=device_registry,
        generator_registry=generator_registry,
        executor_registry=executor_registry,
        result_adapter_registry=result_adapter_registry,
        solver_runtime=solver_runtime,
        deployment_policy=deployment_policy,
    )
    logger.info(
        "组合根装配完成: storage=%s registry=%s generators=%s executors=%s",
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
        (
            f"assembled({len(executor_registry.snapshot())} executors)"
            if executor_registry is not None
            else "skipped"
        ),
    )
    return context


def assemble_api() -> ApplicationContext:
    """装配 API 进程：database、storage、devices 2.0 注册表与生成器目录。

    API 不装配执行面（执行器目录/运行时/执行策略只归计算 Worker）。
    """
    return _assemble(
        with_storage=True,
        with_registry=True,
        with_generators=True,
        with_execution_plane=False,
        seed_admin=True,
    )


def assemble_compute_worker() -> ApplicationContext:
    """装配计算 Worker：database、storage、devices 2.0 注册表、生成器目录
    与完整计算执行面（执行器目录 + 适配器目录 + 通用运行时 + 统一 Settings
    派生的 DeploymentPolicy）。

    任一必需依赖缺失（缺二进制/版本自检失败/策略非法）即抛异常拒绝启动，
    不领取无法执行的计算任务。
    """
    return _assemble(
        with_storage=True,
        with_registry=True,
        with_generators=True,
        with_execution_plane=True,
        seed_admin=True,
    )


def assemble_io_worker() -> ApplicationContext:
    """装配 I/O Worker：database 与 storage，不含 devices 注册表、生成器目录与执行面。"""
    return _assemble(
        with_storage=True,
        with_registry=False,
        with_generators=False,
        with_execution_plane=False,
        seed_admin=True,
    )


__all__ = [
    "ApplicationContext",
    "assemble_api",
    "assemble_compute_worker",
    "assemble_io_worker",
    "collect_immutable_tables",
    "collect_trigger_statements",
]
