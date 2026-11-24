"""组合根调度接线与探针隔离行为测试(ADR-0016 五容器收敛)。

覆盖:
- backend lifespan 按 IESPLAN_EMBEDDED_SCHEDULER 启停 EmbeddedScheduler,
  并在退出时优雅关闭;
- 业务模块队列模式收敛为 bootstrap 传入配置(tasks/queue 的 configure_queue_mode;
  默认行为不变);
- 启动/就绪行为: 存活探针不依赖就绪依赖, 缺依赖时 /readyz 503。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from iesplan.main import create_app

# ---------------------------------------------------------------------------
# EmbeddedScheduler lifespan 接线
# ---------------------------------------------------------------------------


def test_lifespan_starts_and_stops_embedded_scheduler_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IESPLAN_EMBEDDED_SCHEDULER=true 时 lifespan 经组合根启停调度器。"""
    from iesplan import main as main_mod
    from iesplan.config import settings

    events: list[str] = []

    class _FakeScheduler:
        def start(self) -> None:
            events.append("start")

        def stop(self) -> None:
            events.append("stop")

    fake_ctx = SimpleNamespace(
        session_factory=object(),
        stage_runner=None,
        job_runner_client=None,
        compute_execution_plane=lambda: None,
        health=lambda: {"db": "ok"},
        ready=lambda: True,
    )
    monkeypatch.setattr(settings, "embedded_scheduler", True)
    monkeypatch.setattr(main_mod, "assemble_api", lambda: fake_ctx)
    monkeypatch.setattr(main_mod, "assemble_embedded_scheduler", lambda ctx: _FakeScheduler())

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        assert events == ["start"]
        assert test_client.get("/api/healthz").status_code == 200
    assert events == ["start", "stop"]


def test_lifespan_skips_embedded_scheduler_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IESPLAN_EMBEDDED_SCHEDULER=false 时不启动后台调度器。"""
    from iesplan import main as main_mod
    from iesplan.config import settings

    called = False

    def _forbid(_ctx: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("embedded scheduler must not be assembled when disabled")

    monkeypatch.setattr(settings, "embedded_scheduler", False)
    monkeypatch.setattr(main_mod, "assemble_embedded_scheduler", _forbid)

    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        assert test_client.get("/api/healthz").status_code == 200
    assert not called


# ---------------------------------------------------------------------------
# 队列模式收敛: bootstrap 传入配置优先, 默认行为不变
# ---------------------------------------------------------------------------


@pytest.fixture()
def _clean_queue_state():
    from iesplan.tasks import queue

    saved = (queue._backend, queue._degraded, queue._queue_mode_override)
    queue._backend = None
    queue._degraded = False
    queue._queue_mode_override = None
    try:
        yield queue
    finally:
        queue._backend, queue._degraded, queue._queue_mode_override = saved


def test_queue_bootstrap_override_selects_memory(
    _clean_queue_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bootstrap 传入 memory 时即便环境变量指向外部也走内存后端。"""
    from iesplan.tasks.queue import _get_backend, _MemoryBackend, configure_queue_mode

    monkeypatch.setenv("IESPLAN_QUEUE", "redis")
    configure_queue_mode("memory")
    assert isinstance(_get_backend(), _MemoryBackend)


def test_queue_default_still_env_driven(
    _clean_queue_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未配置时默认行为不变: 仍由 IESPLAN_QUEUE 环境变量驱动。"""
    from iesplan.tasks.queue import (
        _get_backend,
        _MemoryBackend,
        _resolve_queue_mode,
        configure_queue_mode,
    )

    configure_queue_mode(None)
    monkeypatch.setenv("IESPLAN_QUEUE", "memory")
    assert _resolve_queue_mode() == "memory"
    assert isinstance(_get_backend(), _MemoryBackend)


# ---------------------------------------------------------------------------
# 启动/readiness 行为: 存活独立于就绪, 缺依赖 503
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> Any:
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def test_healthz_independent_of_readiness_deps(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """数据库不可用时存活探针仍 200, 就绪探针 503(两者不耦合)。"""
    monkeypatch.setattr("iesplan.bootstrap._check_database", lambda factory: False)
    assert client.get("/api/healthz").status_code == 200
    resp = client.get("/api/readyz")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "API-RZ-001"
