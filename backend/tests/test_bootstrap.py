"""组合根契约测试: assemble_* 装配形状与 metadata 注册。"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from iesplan.bootstrap import (
    ApplicationContext,
    assemble_api,
    assemble_compute_worker,
    assemble_io_worker,
    assemble_task_runtime,
    collect_immutable_tables,
    collect_trigger_statements,
)


def test_assemble_api_full_context() -> None:
    """assemble_api 装配 database、storage、registry、生成器与内置阶段目录。"""
    ctx = assemble_api()
    assert isinstance(ctx, ApplicationContext)
    assert ctx.session_factory is not None
    assert ctx.storage is not None
    assert ctx.device_registry is not None
    assert ctx.generator_registry is not None
    assert [d.ref for d in ctx.generator_registry.snapshot()] == [
        "ies.generator.highs_milp@1.0.0"
    ]
    assert len(ctx.generator_registry) == 1
    health = ctx.health()
    assert health["db"] == "ok"
    assert health["registry"] == "ok"
    assert health["generators"] == "ok"
    assert health["stages"] == "ok"
    assert "job_runner" not in health
    assert ctx.builtin_stage_catalog is not None
    assert len(ctx.builtin_stage_catalog.snapshot()) == 4
    assert ctx.container_stage_policy is not None
    assert all(isinstance(v, str) for v in health.values())
    assert ctx.ready() is True


def test_assemble_worker_subsets(monkeypatch) -> None:
    """计算执行面只通过 ContainerStageRunner + JobRunnerClient 装配，无本地进程回退。"""
    from iesplan.config import settings

    # 未配置 job_runner_url 时不回退到进程内 LocalProcessProvider，compute_execution_plane 为 None。
    monkeypatch.setattr(settings, "job_runner_url", "")
    no_runner = assemble_compute_worker()
    assert no_runner.stage_runner is None
    assert no_runner.compute_execution_plane() is None
    assert "executors" not in no_runner.health()

    # 配置 job_runner_url 后装配 ContainerStageRunner + BuiltinStageCatalog。
    monkeypatch.setattr(settings, "job_runner_url", "http://job_runner:8090")
    compute = assemble_compute_worker()
    assert compute.stage_runner is not None
    assert compute.job_runner_client is not None
    policy = compute.container_stage_policy
    assert policy is not None
    assert policy.builtin_image == settings.stage_builtin_image
    assert policy.max_timeout_seconds == settings.solver_max_timeout_seconds
    assert policy.memory_mb == settings.solver_max_memory_mib
    assert policy.cpu_cores == settings.solver_max_cpu_cores
    assert policy.pids_limit == settings.stage_pids_limit
    assert policy.tmpfs_size_mb == settings.stage_tmpfs_size_mb
    assert policy.stop_grace_seconds == settings.stage_stop_grace_seconds
    plane = compute.compute_execution_plane()
    assert plane is not None
    assert plane.stage_runner is compute.stage_runner
    assert plane.stage_catalog is compute.builtin_stage_catalog

    api = assemble_api()
    assert api.compute_execution_plane() is not None
    assert "job_runner" in api.health()

    io = assemble_io_worker()
    assert io.storage is not None
    assert io.device_registry is None
    assert io.generator_registry is None
    assert io.compute_execution_plane() is None
    assert "registry" not in io.health()
    assert "generators" not in io.health()
    assert io.health()["db"] == "ok"


def test_assemble_configures_queue_mode_from_environment(monkeypatch) -> None:
    """组合根是队列选型的唯一环境解释点: 任务队列接收解析后的配置。"""
    from iesplan.tasks import queue as queue_module

    monkeypatch.setenv("IESPLAN_QUEUE", "memory")
    try:
        assemble_io_worker()
        assert queue_module._resolve_queue_mode() == "memory"
    finally:
        queue_module.configure_queue_mode(None)


def test_public_facades_register_domain_metadata() -> None:
    """组合根加载领域公开门面后，metadata 已包含各领域表。"""
    from iesplan.db import Base

    names = set(Base.metadata.tables)
    assert "users" in names
    assert "projects" in names
    assert "tasks" in names
    assert "objects" in names


@pytest.mark.parametrize("worker_type", ["compute", "io"])
def test_task_runtime_does_not_redeploy_database_or_seed_identity(worker_type, monkeypatch) -> None:
    from iesplan import db
    from iesplan.application import identity
    from iesplan.config import settings

    initialize = Mock()
    seed = Mock()
    monkeypatch.setattr(db, "init_db", initialize)
    monkeypatch.setattr(identity, "seed_builtin_admin", seed)
    monkeypatch.setattr(settings, "job_runner_url", "http://job_runner:8090")

    ctx = assemble_task_runtime(worker_type)

    initialize.assert_not_called()
    seed.assert_not_called()
    assert ctx.session_factory is db.SessionLocal
    assert ctx.storage is not None
    assert (ctx.compute_execution_plane() is not None) == (worker_type == "compute")


def test_collect_trigger_statements_covers_immutable_tables() -> None:
    """触发器语句由各领域钩子编排收集: 每张不可变表都有函数和两个触发器。"""
    statements = collect_trigger_statements()
    assert statements
    ddl = "\n\n".join(statements)
    for table in collect_immutable_tables():
        assert f"CREATE FUNCTION tg_{table}_immutable() RETURNS trigger" in ddl
        assert f"CREATE TRIGGER tg_{table}_no_update BEFORE UPDATE ON {table}" in ddl
        assert f"CREATE TRIGGER tg_{table}_no_delete BEFORE DELETE ON {table}" in ddl
