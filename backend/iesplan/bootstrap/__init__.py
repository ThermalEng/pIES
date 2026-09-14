"""应用组合根(backend-decoupling Wave3)。

系统唯一知道"这次部署具体使用哪些实现"的位置: 按进程角色
(API / compute Worker / I/O Worker)装配所需能力子集, 输出完整
``ApplicationContext`` 或明确启动失败。

- ``assemble_api``: 装配 database + storage + devices 2.0 注册表;
- ``assemble_compute_worker``: 0.8 前明确拒绝启动;
- ``assemble_io_worker``: I/O 所需子集(database + storage, 不含 devices
  注册表)。

失败语义: 任一必需依赖装配失败即抛异常(启动失败), 不发布半初始化
状态, 不设 fallback。计算能力将在 0.8 实现；当前计算 Worker 组合根
明确拒绝启动，因而不会领取无法执行的计算任务。

本包只做装配与健康聚合, 不实现业务规则, 不新增全局 registry(装配结果
由调用方持有, API 进程挂在 ``app.state.bootstrap_context``)。

领域 ORM 在拥有者领域公开门面加载时完成 metadata 注册；触发器规则由
各领域公开门面导出。本包只静态导入公开门面并收集真实触发器语句，不设
无行为的表安装钩子，也不做字符串驱动的动态导入。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Never

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

    def health(self) -> dict[str, str]:
        """各已装配依赖的健康状态(值仅为 ``"ok"`` 或 ``"unavailable"``)。

        - ``db``: 始终上报(会话 SELECT 1 探活);
        - ``storage`` / ``registry``: 仅在装配了该能力时上报; 未装配
          (能力子集, 如 Worker 上下文)时不出现该键, 不伪装健康。
        探针失败原因只进日志, 不进入返回值(避免内部细节外泄)。
        """
        result: dict[str, str] = {}
        result["db"] = "ok" if _check_database(self.session_factory) else "unavailable"
        if self.storage is not None:
            result["storage"] = "ok" if _check_storage(self.storage) else "unavailable"
        if self.device_registry is not None:
            result["registry"] = "ok" if _check_registry(self.device_registry) else "unavailable"
        return result

    def readiness(self) -> dict[str, Any]:
        """公开就绪结果: ``{"ready": 是否全部就绪, "health": 各能力健康}``。

        就绪探针(``iesplan.main`` readyz)只消费本结果, 不直接探活依赖;
        ``health`` 的键为已装配能力(``db`` / ``storage`` / ``registry``),
        值仅为 ``"ok"`` 或 ``"unavailable"``。
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
    statements.extend(model_domain.install_triggers())
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
    context = ApplicationContext(
        session_factory=db_module.SessionLocal,
        storage=storage,
        device_registry=device_registry,
    )
    logger.info(
        "组合根装配完成: storage=%s registry=%s",
        "assembled" if storage is not None else "skipped",
        (
            f"assembled({len(device_registry.snapshot())} devices)"
            if device_registry is not None
            else "skipped"
        ),
    )
    return context


def assemble_api() -> ApplicationContext:
    """装配 API 进程：database、storage 与 devices 2.0 注册表。"""
    return _assemble(with_storage=True, with_registry=True, seed_admin=True)


def assemble_compute_worker() -> Never:
    """计算能力将在 0.8 提供；当前拒绝启动，避免领取后再失败。"""
    from iesplan.computation import ComputationUnavailableError

    raise ComputationUnavailableError(reason="no-provider")


def assemble_io_worker() -> ApplicationContext:
    """装配 I/O Worker：database 与 storage，不含 devices 注册表。"""
    return _assemble(with_storage=True, with_registry=False, seed_admin=True)


__all__ = [
    "ApplicationContext",
    "assemble_api",
    "assemble_compute_worker",
    "assemble_io_worker",
    "collect_immutable_tables",
    "collect_trigger_statements",
]
