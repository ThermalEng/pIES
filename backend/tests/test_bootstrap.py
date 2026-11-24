"""组合根契约测试: assemble_api / assemble_embedded_scheduler 装配形状与 metadata 注册。"""

from __future__ import annotations

import pytest

from iesplan.bootstrap import (
    ApplicationContext,
    assemble_api,
    assemble_embedded_scheduler,
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


def test_assemble_container_execution_plane_and_scheduler(monkeypatch) -> None:
    """计算执行面只通过 ContainerStageRunner + JobRunnerClient 装配，无本地进程回退。"""
    from iesplan.config import settings

    # 未配置 job_runner_url 时不回退到进程内 LocalProcessProvider，compute_execution_plane 为 None。
    monkeypatch.setattr(settings, "job_runner_url", "")
    no_runner = assemble_api()
    assert no_runner.stage_runner is None
    assert no_runner.compute_execution_plane() is None
    assert "executors" not in no_runner.health()

    # 配置 job_runner_url 后装配 ContainerStageRunner + BuiltinStageCatalog。
    monkeypatch.setattr(settings, "job_runner_url", "http://job_runner:8090")
    api = assemble_api()
    assert api.stage_runner is not None
    assert api.job_runner_client is not None
    policy = api.container_stage_policy
    assert policy is not None
    assert policy.builtin_image == settings.stage_builtin_image
    assert policy.max_timeout_seconds == settings.solver_max_timeout_seconds
    assert policy.memory_mb == settings.solver_max_memory_mib
    assert policy.cpu_cores == settings.solver_max_cpu_cores
    assert policy.pids_limit == settings.stage_pids_limit
    assert policy.tmpfs_size_mb == settings.stage_tmpfs_size_mb
    assert policy.stop_grace_seconds == settings.stage_stop_grace_seconds
    plane = api.compute_execution_plane()
    assert plane is not None
    assert plane.stage_runner is api.stage_runner
    assert plane.stage_catalog is api.builtin_stage_catalog
    assert "job_runner" in api.health()

    scheduler = assemble_embedded_scheduler(api)
    try:
        assert scheduler.session_factory is api.session_factory
        assert scheduler.compute == plane
        assert scheduler.stage_runner is api.stage_runner
        assert scheduler.compute_slots == settings.compute_slots
        assert scheduler.io_slots == settings.io_slots
        assert scheduler.build_slots == settings.build_slots
    finally:
        scheduler.stop()

    # job_runner_url 已配置但缺少执行面时拒绝装配调度器。
    broken = ApplicationContext(session_factory=api.session_factory)
    with pytest.raises(RuntimeError, match="compute execution plane"):
        assemble_embedded_scheduler(broken)


def test_assemble_configures_queue_mode_from_environment(monkeypatch) -> None:
    """组合根是队列选型的唯一环境解释点: 任务队列接收解析后的配置。"""
    from iesplan.tasks import queue as queue_module

    monkeypatch.setenv("IESPLAN_QUEUE", "memory")
    try:
        assemble_api()
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


def test_collect_trigger_statements_covers_immutable_tables() -> None:
    """触发器语句由各领域钩子编排收集: 每张不可变表都有函数和两个触发器。"""
    statements = collect_trigger_statements()
    assert statements
    ddl = "\n\n".join(statements)
    for table in collect_immutable_tables():
        assert f"CREATE FUNCTION tg_{table}_immutable() RETURNS trigger" in ddl
        assert f"CREATE TRIGGER tg_{table}_no_update BEFORE UPDATE ON {table}" in ddl
        assert f"CREATE TRIGGER tg_{table}_no_delete BEFORE DELETE ON {table}" in ddl
