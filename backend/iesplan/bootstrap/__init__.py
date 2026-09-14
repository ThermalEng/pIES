"""应用组合根(backend-decoupling Wave3)。

系统唯一知道"这次部署具体使用哪些实现"的位置: 按进程角色
(API / compute Worker / I/O Worker)装配所需能力子集, 输出完整
``ApplicationContext`` 或明确启动失败。

- ``assemble_api``: 装配 database + storage + devices 2.0 注册表 +
  空 computation provider 目录(0.8 未实现, 明确无可用 provider);
- ``assemble_compute_worker``: 计算所需子集(database + devices 注册表 +
  空 computation provider 目录, 不含 storage);
- ``assemble_io_worker``: I/O 所需子集(database + storage, 不含 devices
  注册表与 computation provider)。

失败语义: 任一必需依赖装配失败即抛异常(启动失败), 不发布半初始化
状态, 不设 fallback。``computation_providers`` 当前允许为空(0.8 延期),
必需 provider 清单见 ``REQUIRED_COMPUTATION_PROVIDERS``(当前为空, 故
不触发缺失异常; 未来登记必需 provider 后缺失即启动失败)。

本包只做装配与健康聚合, 不实现业务规则, 不新增全局 registry(装配结果
由调用方持有, API 进程挂在 ``app.state.bootstrap_context``)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: computation 必需 provider 名单(当前为空: 0.8 未实现, 允许无可用 provider)。
#: 未来在此登记必需 provider 后, ``assemble_*`` 在缺失时抛异常(启动失败)。
REQUIRED_COMPUTATION_PROVIDERS: tuple[str, ...] = ()


@dataclass
class ApplicationContext:
    """一次装配的完整应用上下文(只暴露模块公开门面与健康能力)。"""

    #: 会话工厂(已装配的数据库连接能力, 如 ``iesplan.db.SessionLocal``)
    session_factory: Any
    #: 已装配的存储适配能力对象或其公开句柄(未装配该能力时为 None)
    storage: Any | None = None
    #: devices 2.0 注册状态对象(未装配该能力时为 None)
    device_registry: Any | None = None
    #: computation provider 目录(当前可为空, 明确无可用 provider)
    computation_providers: dict[str, object] = field(default_factory=dict)
    #: 进程配置单例
    settings: Any | None = None

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

    def ready(self) -> bool:
        """已装配的依赖是否全部就绪(任一 ``unavailable`` 即 False)。"""
        states = self.health()
        return bool(states) and all(v == "ok" for v in states.values())


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


def _assemble(
    *,
    with_storage: bool,
    with_registry: bool,
    seed_admin: bool,
) -> ApplicationContext:
    """通用装配: 显式注册 metadata → 建表/迁移/种子 → 装配能力子集。

    异常直接抛给调用方(启动失败, 不发布半初始化状态)。
    """
    from iesplan import db as db_module
    from iesplan.config import settings

    # 0. 队列/限速后端选型(组合根唯一环境解释点): 一次解析部署环境,
    #    扇出给各接收模块; 业务模块只接收传入配置, 不再自行解释环境选型。
    _configure_queue_mode(_resolve_queue_mode())
    # 1. 各领域 metadata 注册(assemble 显式调用; init_db 内部为测试兼容亦调用)
    db_module.register_domain_metadata()
    # 2. 建表 + 版本化迁移 + 不可变触发器
    db_module.init_db()
    # 3. 内置身份种子(幂等)
    if seed_admin:
        from iesplan.application.identity import seed_builtin_admin

        with db_module.SessionLocal() as session:
            seed_builtin_admin(session)
    # 4. 存储适配能力(公开句柄)
    storage: Any | None = None
    if with_storage:
        from iesplan.storage.service import get_blob_store

        storage = get_blob_store()
    # 5. devices 2.0 注册表(任一设备校验失败即整体拒绝, 原子发布)
    device_registry: Any | None = None
    if with_registry:
        from iesplan.devices import init_registry

        device_registry = init_registry()
    # 6. computation provider 目录(0.8 未实现: 明确为空, 无可用 provider;
    #    目录唯一来源为 computation 门面, 此处只取不建)
    from iesplan.computation import available_providers

    computation_providers: dict[str, object] = dict(available_providers())
    missing = [name for name in REQUIRED_COMPUTATION_PROVIDERS if name not in computation_providers]
    if missing:
        raise RuntimeError(f"必需 computation provider 缺失: {missing}, 启动失败")
    context = ApplicationContext(
        session_factory=db_module.SessionLocal,
        storage=storage,
        device_registry=device_registry,
        computation_providers=computation_providers,
        settings=settings,
    )
    logger.info(
        "组合根装配完成: storage=%s registry=%s computation_providers=%s",
        "assembled" if storage is not None else "skipped",
        (
            f"assembled({len(device_registry.snapshot())} devices)"
            if device_registry is not None
            else "skipped"
        ),
        "none-available(0.8 deferred)" if not computation_providers else sorted(computation_providers),
    )
    return context


def assemble_api() -> ApplicationContext:
    """装配 API 进程: database + storage + devices 2.0 注册表 + 空 provider 目录。"""
    return _assemble(with_storage=True, with_registry=True, seed_admin=True)


def assemble_compute_worker() -> ApplicationContext:
    """装配计算 Worker: database + devices 注册表 + 空 provider 目录(不含 storage)。"""
    return _assemble(with_storage=False, with_registry=True, seed_admin=True)


def assemble_io_worker() -> ApplicationContext:
    """装配 I/O Worker: database + storage(不含 devices 注册表与 provider 目录)。"""
    return _assemble(with_storage=True, with_registry=False, seed_admin=True)


__all__ = [
    "REQUIRED_COMPUTATION_PROVIDERS",
    "ApplicationContext",
    "assemble_api",
    "assemble_compute_worker",
    "assemble_io_worker",
]
