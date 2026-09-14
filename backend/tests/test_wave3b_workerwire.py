"""Wave3-B Worker 接线行为测试(bootstrap 组合根消费侧)。

覆盖(不碰 bootstrap 包内部/main.py/db.py):
- worker/main.main 按 worker_type 选择 assemble_compute_worker()/
  assemble_io_worker(), Daemon 会话工厂取自装配好的 ApplicationContext;
- assemble 抛异常时直接传播(启动失败, 不发布半初始化状态, 无 fallback);
- 业务模块队列模式收敛为 bootstrap 传入配置(api/limits、identity、
  tasks/queue 的 configure_queue_mode; 默认行为不变);
- 启动/就绪行为: 存活探针不依赖就绪依赖, 缺依赖时 /readyz 503。

bootstrap 真包由 W3-A 切片提供; 本文件用 sys.modules 桩验证消费契约,
不锁定装配实现。
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from iesplan.main import create_app


# ---------------------------------------------------------------------------
# 桩 bootstrap(装配实现归 W3-A, 此处只立消费契约)
# ---------------------------------------------------------------------------


@pytest.fixture()
def stub_bootstrap(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """向 sys.modules 注入桩 iesplan.bootstrap, 记录各 assemble 调用次数。"""
    calls: dict[str, int] = {"compute": 0, "io": 0}
    failures: dict[str, Exception] = {}
    sentinel_factory = object()

    def _compute() -> SimpleNamespace:
        calls["compute"] += 1
        if "compute" in failures:
            raise failures["compute"]
        return SimpleNamespace(session_factory=sentinel_factory)

    def _io() -> SimpleNamespace:
        calls["io"] += 1
        if "io" in failures:
            raise failures["io"]
        return SimpleNamespace(session_factory=sentinel_factory)

    module = types.ModuleType("iesplan.bootstrap")
    module.assemble_compute_worker = _compute  # type: ignore[attr-defined]
    module.assemble_io_worker = _io  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "iesplan.bootstrap", module)
    return {"calls": calls, "failures": failures, "session_factory": sentinel_factory}


@pytest.fixture()
def fake_worker(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """替换 iesplan.worker.main.Worker, 记录构造参数与 run 调用。"""
    import iesplan.worker.main as worker_main

    state: dict[str, Any] = {"kwargs": None, "runs": 0}

    class _FakeWorker:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            state["kwargs"] = kwargs
            state["args"] = args

        def run(self) -> None:
            state["runs"] += 1

    monkeypatch.setattr(worker_main, "Worker", _FakeWorker)
    return state


def _run_main(argv: list[str]) -> int:
    import iesplan.worker.main as worker_main

    return worker_main.main(argv)


# ---------------------------------------------------------------------------
# worker_type → assemble 选择
# ---------------------------------------------------------------------------


def test_main_selects_compute_assemble(
    stub_bootstrap: dict[str, Any], fake_worker: dict[str, Any]
) -> None:
    assert _run_main(["--worker-type", "compute"]) == 0
    assert stub_bootstrap["calls"] == {"compute": 1, "io": 0}
    assert fake_worker["runs"] == 1
    assert fake_worker["kwargs"]["worker_type"] == "compute"


def test_main_selects_io_assemble(
    stub_bootstrap: dict[str, Any], fake_worker: dict[str, Any]
) -> None:
    assert _run_main(["--worker-type", "io"]) == 0
    assert stub_bootstrap["calls"] == {"compute": 0, "io": 1}
    assert fake_worker["runs"] == 1
    assert fake_worker["kwargs"]["worker_type"] == "io"


def test_main_worker_type_from_env(
    stub_bootstrap: dict[str, Any],
    fake_worker: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI 未指定时取 IESPLAN_WORKER_TYPE 环境变量(部署编排现状不变)。"""
    monkeypatch.setenv("IESPLAN_WORKER_TYPE", "io")
    assert _run_main([]) == 0
    assert stub_bootstrap["calls"] == {"compute": 0, "io": 1}


def test_main_session_factory_comes_from_context(
    stub_bootstrap: dict[str, Any], fake_worker: dict[str, Any]
) -> None:
    """Daemon 会话工厂必须取自装配好的 context, 不再直连 SessionLocal。"""
    _run_main(["--worker-type", "compute"])
    assert fake_worker["kwargs"]["session_factory"] is stub_bootstrap["session_factory"]


def test_main_isolation_flag_passthrough(
    stub_bootstrap: dict[str, Any], fake_worker: dict[str, Any]
) -> None:
    _run_main(["--worker-type", "compute", "--no-isolation"])
    assert fake_worker["kwargs"]["isolate"] is False


# ---------------------------------------------------------------------------
# 启动失败: 无 fallback, 无半初始化
# ---------------------------------------------------------------------------


def test_main_assemble_failure_propagates_without_half_init(
    stub_bootstrap: dict[str, Any], fake_worker: dict[str, Any]
) -> None:
    """必需 provider 缺失(assemble 抛异常)时启动失败, Daemon 永不运行。"""
    stub_bootstrap["failures"]["compute"] = RuntimeError("db unavailable")
    with pytest.raises(RuntimeError, match="db unavailable"):
        _run_main(["--worker-type", "compute"])
    assert fake_worker["kwargs"] is None
    assert fake_worker["runs"] == 0


def test_main_invalid_worker_type_rejected_before_assemble(
    stub_bootstrap: dict[str, Any],
    fake_worker: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IESPLAN_WORKER_TYPE", "bogus")
    with pytest.raises(ValueError, match="非法 worker_type"):
        _run_main([])
    assert stub_bootstrap["calls"] == {"compute": 0, "io": 0}
    assert fake_worker["runs"] == 0


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
    from iesplan.tasks.queue import _MemoryBackend, _get_backend, configure_queue_mode

    monkeypatch.setenv("IESPLAN_QUEUE", "redis")
    configure_queue_mode("memory")
    assert isinstance(_get_backend(), _MemoryBackend)


def test_queue_default_still_env_driven(
    _clean_queue_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未配置时默认行为不变: 仍由 IESPLAN_QUEUE 环境变量驱动。"""
    from iesplan.tasks.queue import (
        _MemoryBackend,
        _get_backend,
        _resolve_queue_mode,
        configure_queue_mode,
    )

    configure_queue_mode(None)
    monkeypatch.setenv("IESPLAN_QUEUE", "memory")
    assert _resolve_queue_mode() == "memory"
    assert isinstance(_get_backend(), _MemoryBackend)


def _assert_rate_limit_memory_mode(mod_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """限速模块: bootstrap 传入 memory 则跳过 Redis 且不建连; 缺省走环境变量。"""
    import importlib

    mod = importlib.import_module(mod_name)
    monkeypatch.setenv("IESPLAN_QUEUE", "redis")

    class _ExplodingRedis:
        @staticmethod
        def from_url(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("内存模式不应连接 Redis")

    monkeypatch.setattr(mod, "_redis_module", SimpleNamespace(Redis=_ExplodingRedis))
    monkeypatch.setattr(mod, "_REDIS_IMPORT_OK", True)
    monkeypatch.setattr(mod, "_rate_redis_client", None)
    try:
        mod.configure_queue_mode("memory")
        assert mod._rate_redis() is None
        mod.configure_queue_mode(None)
        assert mod._queue_mode_is_memory() is False
    finally:
        mod.configure_queue_mode(None)
        mod._rate_redis_client = None


def test_limits_queue_mode_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_rate_limit_memory_mode("iesplan.api.limits", monkeypatch)


def test_identity_queue_mode_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_rate_limit_memory_mode("iesplan.identity", monkeypatch)


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
    monkeypatch.setattr("iesplan.main._db_available", lambda: False)
    assert client.get("/api/healthz").status_code == 200
    resp = client.get("/api/readyz")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "API-RZ-001"
