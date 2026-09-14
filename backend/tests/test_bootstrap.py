"""组合根契约测试: assemble_* 装配形状与 metadata 注册。"""

from __future__ import annotations

from iesplan.bootstrap import (
    ApplicationContext,
    assemble_api,
    assemble_compute_worker,
    assemble_io_worker,
)


def test_assemble_api_full_context() -> None:
    """assemble_api 装配 database+storage+registry, provider 目录为空且明确无可用项。"""
    ctx = assemble_api()
    assert isinstance(ctx, ApplicationContext)
    assert ctx.session_factory is not None
    assert ctx.storage is not None
    assert ctx.device_registry is not None
    assert ctx.computation_providers == {}
    assert ctx.settings is not None
    health = ctx.health()
    assert health["db"] == "ok"
    assert health["registry"] == "ok"
    assert all(isinstance(v, str) for v in health.values())
    assert ctx.ready() is True


def test_assemble_worker_subsets() -> None:
    """computeWorker 装配计算子集(含 registry, 不含 storage); ioWorker 装配 I/O 子集(含 storage)。"""
    compute = assemble_compute_worker()
    assert compute.device_registry is not None
    assert compute.storage is None
    assert compute.computation_providers == {}
    assert compute.health()["registry"] == "ok"
    assert "storage" not in compute.health()

    io = assemble_io_worker()
    assert io.storage is not None
    assert io.device_registry is None
    assert "registry" not in io.health()
    assert io.health()["db"] == "ok"


def test_register_domain_metadata_registers_tables() -> None:
    """register_domain_metadata 只做导入注册: Base.metadata 含各领域表, 无建表副作用断言。"""
    from iesplan.db import Base, register_domain_metadata

    register_domain_metadata()
    names = set(Base.metadata.tables)
    assert "users" in names
    assert "projects" in names
    assert "tasks" in names
    assert "objects" in names
