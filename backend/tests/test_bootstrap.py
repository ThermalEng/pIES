"""组合根契约测试: assemble_* 装配形状与 metadata 注册。"""

from __future__ import annotations

import pytest

from iesplan.bootstrap import (
    ApplicationContext,
    assemble_api,
    assemble_compute_worker,
    assemble_io_worker,
    collect_immutable_tables,
    collect_trigger_statements,
)
from iesplan.computation import ComputationUnavailableError


def test_assemble_api_full_context() -> None:
    """assemble_api 装配 database、storage 与 registry。"""
    ctx = assemble_api()
    assert isinstance(ctx, ApplicationContext)
    assert ctx.session_factory is not None
    assert ctx.storage is not None
    assert ctx.device_registry is not None
    health = ctx.health()
    assert health["db"] == "ok"
    assert health["registry"] == "ok"
    assert all(isinstance(v, str) for v in health.values())
    assert ctx.ready() is True


def test_assemble_worker_subsets() -> None:
    """0.8 前计算 Worker 拒绝启动；I/O Worker 正常装配。"""
    with pytest.raises(ComputationUnavailableError):
        assemble_compute_worker()

    io = assemble_io_worker()
    assert io.storage is not None
    assert io.device_registry is None
    assert "registry" not in io.health()
    assert io.health()["db"] == "ok"


def test_assemble_configures_queue_mode_from_environment(monkeypatch) -> None:
    """组合根是队列选型的唯一环境解释点: 装配扇出后各接收模块不再回读环境。"""
    from iesplan import identity as identity_module
    from iesplan.api import limits as limits_module
    from iesplan.tasks import queue as queue_module

    monkeypatch.setenv("IESPLAN_QUEUE", "memory")
    try:
        assemble_io_worker()
        assert queue_module._resolve_queue_mode() == "memory"
        assert limits_module._queue_mode_is_memory() is True
        assert identity_module._queue_mode_is_memory() is True
    finally:
        queue_module.configure_queue_mode(None)
        limits_module.configure_queue_mode(None)
        identity_module.configure_queue_mode(None)


def test_public_facades_register_domain_metadata() -> None:
    """组合根加载领域公开门面后，metadata 已包含各领域表。"""
    from iesplan.db import Base

    names = set(Base.metadata.tables)
    assert "users" in names
    assert "projects" in names
    assert "tasks" in names
    assert "objects" in names


def test_collect_trigger_statements_covers_immutable_tables() -> None:
    """触发器语句由各领域钩子编排收集: 每张不可变表都有函数/两触发器/REVOKE 片段。"""
    statements = collect_trigger_statements()
    assert statements
    ddl = "\n\n".join(statements)
    for table in collect_immutable_tables():
        assert f"CREATE FUNCTION tg_{table}_immutable() RETURNS trigger" in ddl
        assert f"CREATE TRIGGER tg_{table}_no_update BEFORE UPDATE ON {table}" in ddl
        assert f"CREATE TRIGGER tg_{table}_no_delete BEFORE DELETE ON {table}" in ddl
        assert f"REVOKE UPDATE, DELETE ON {table} FROM PUBLIC;" in ddl
