"""Unit & contract tests for JobRunnerClient, 6-step ContainerStageRunner,
EmbeddedScheduler pool isolation & reconciliation, and /api/readyz job_runner checks.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from iesplan import tasks as tasks_domain
from iesplan.application import worker as worker_app
from iesplan.computation.runtime.job_runner_client import (
    JobRunnerBadRequestError,
    JobRunnerClient,
    JobRunnerConflictError,
    JobRunnerNotFoundError,
    JobRunnerUnavailableError,
    StartStageSpec,
)
from iesplan.computation.runtime.stage_runner import (
    ContainerStagePolicy,
    ContainerStageRunner,
    StageExecutionSpec,
    build_builtin_stage_catalog,
)
from iesplan.config import settings
from iesplan.core.content import MemoryContent
from iesplan.db import Base
from iesplan.main import create_app
from iesplan.worker.scheduler import EmbeddedScheduler


def _sample_resources(*, oom_killed: bool = False, memory_peak: int = 12_345_678) -> dict[str, Any]:
    return {
        "source": "docker_and_cgroup_v2",
        "memory_limit_bytes": {"status": "limited", "value": 536_870_912},
        "memory_current_bytes": {"status": "known", "value": 1_048_576},
        "memory_peak_bytes": {"status": "known", "value": memory_peak},
        "cpu_limit_cores": {"status": "limited", "value": 2.0},
        "cpu_usage_ns": {"status": "known", "value": 9_000_000},
        "pids_limit": {"status": "limited", "value": 256},
        "oom_killed": {"status": "known", "value": oom_killed},
        "oom_kill_count": {"status": "known", "value": 1 if oom_killed else 0},
    }


def _stage_payload(
    *,
    stage_execution_id: str,
    attempt_id: str = "10",
    stage_id: str = "solver",
    fencing_token: str = "tok-10",
    workspace_rel: str | None = None,
    status: str = "exited",
    running: bool = False,
    exit_code: int | None = 0,
    termination_reason: str | None = "normal",
    reused: bool = False,
    started_at: str | None = None,
    output_files: list[str] | None = None,
    iterations: dict[str, Any] | None = None,
    oom_killed: bool = False,
) -> dict[str, Any]:
    return {
        "stage_execution_id": stage_execution_id,
        "attempt_id": attempt_id,
        "stage_id": stage_id,
        "fencing_token": fencing_token,
        "container_id": f"cid-{stage_execution_id}",
        "container_name": f"pies-stage-{stage_execution_id}",
        "image": "pies-builtin-stages:1.1.5",
        "workspace_rel": workspace_rel or stage_execution_id,
        "reused": reused,
        "status": status,
        "running": running,
        "exit_code": exit_code,
        "termination_reason": termination_reason,
        "started_at": started_at or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "finished_at": None if running else "2026-11-24T08:00:01Z",
        "resources": _sample_resources(oom_killed=oom_killed),
        "iterations": iterations or {"present": False, "count": 0, "tail": []},
        "output_files": output_files or [],
    }


def _default_policy() -> ContainerStagePolicy:
    return ContainerStagePolicy(
        builtin_image="pies-builtin-stages:1.1.5",
        cpu_cores=2.0,
        memory_mb=512,
        pids_limit=256,
        tmpfs_size_mb=128,
        stop_grace_seconds=3,
        max_timeout_seconds=60.0,
    )


# ---------------------------------------------------------------------------
# JobRunnerClient contract & error mapping tests
# ---------------------------------------------------------------------------


def test_job_runner_client_fixed_endpoints_and_error_mapping() -> None:
    seen_calls: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        seen_calls.append((request.method, request.url.path, body))
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"status": "ready", "docker": "ok", "stage_root": "/data/stages"})
        if request.url.path == "/v1/plugins/build":
            return httpx.Response(
                200,
                json={
                    "image": "pies-plugin/demo.algo:1.0.0",
                    "plugin_id": "demo.algo",
                    "version": "1.0.0",
                    "built_at": "2026-11-24T08:00:00Z",
                    "logs": ["Step 1/4"],
                },
            )
        if request.url.path == "/v1/stages/start":
            return httpx.Response(
                200,
                json=_stage_payload(
                    stage_execution_id="10--solver",
                    status="running",
                    running=True,
                    exit_code=None,
                    termination_reason=None,
                ),
            )
        if request.url.path == "/v1/stages":
            return httpx.Response(
                200,
                json={"items": [_stage_payload(stage_execution_id="10--solver")], "total": 1},
            )
        if request.url.path == "/v1/stages/10--solver":
            return httpx.Response(
                200,
                json=_stage_payload(stage_execution_id="10--solver", output_files=["solution.sol"]),
            )
        if request.url.path == "/v1/stages/10--solver/stop":
            return httpx.Response(
                200,
                json=_stage_payload(
                    stage_execution_id="10--solver",
                    exit_code=137,
                    termination_reason="cancelled",
                ),
            )
        if request.url.path == "/v1/stages/10--solver/cleanup":
            return httpx.Response(
                200,
                json={
                    "stage_execution_id": "10--solver",
                    "status": "cleaned",
                    "container_removed": True,
                    "workspace_removed": True,
                },
            )
        if request.url.path == "/v1/stages/missing":
            return httpx.Response(404, json={"detail": "stage container not found"})
        if request.url.path == "/v1/stages/busy/cleanup":
            return httpx.Response(409, json={"detail": "stage container is still running"})
        if request.url.path == "/v1/stages/bad/stop":
            return httpx.Response(400, json={"detail": "invalid request"})
        return httpx.Response(503, json={"detail": "docker daemon unreachable"})

    http_client = httpx.Client(base_url="http://job_runner:8090", transport=httpx.MockTransport(handler))
    client = JobRunnerClient("http://job_runner:8090", client=http_client)

    assert client.check_ready()["status"] == "ready"
    built = client.build_plugin("demo.algo", "1.0.0", "demo_rel", requirements_rel="deps/req.txt")
    assert built.image == "pies-plugin/demo.algo:1.0.0"

    started = client.start_stage(
        StartStageSpec(
            attempt_id="10",
            stage_id="solver",
            fencing_token="tok-10",
            image="pies-builtin-stages:1.1.5",
            cpu_cores=2.0,
            memory_mb=512,
            pids_limit=256,
            tmpfs_size_mb=128,
        )
    )
    assert started.running is True
    listed = client.list_stages(attempt_id="10", stage_id="solver")
    assert len(listed) == 1
    inspected = client.inspect_stage("10--solver")
    assert inspected.output_files == ("solution.sol",)
    stopped = client.stop_stage("10--solver", reason="cancelled", grace_seconds=3)
    assert stopped.termination_reason == "cancelled"
    cleaned = client.cleanup_stage("10--solver")
    assert cleaned.container_removed and cleaned.workspace_removed

    with pytest.raises(JobRunnerNotFoundError):
        client.inspect_stage("missing")
    with pytest.raises(JobRunnerConflictError):
        client.cleanup_stage("busy")
    with pytest.raises(JobRunnerBadRequestError):
        client.stop_stage("bad", grace_seconds=5)
    with pytest.raises(JobRunnerUnavailableError):
        client.inspect_stage("unavailable")

    assert [path for _, path, _ in seen_calls[:7]] == [
        "/readyz",
        "/v1/plugins/build",
        "/v1/stages/start",
        "/v1/stages",
        "/v1/stages/10--solver",
        "/v1/stages/10--solver/stop",
        "/v1/stages/10--solver/cleanup",
    ]


# ---------------------------------------------------------------------------
# 6-step ContainerStageRunner lifecycle & persist-before-cleanup tests
# ---------------------------------------------------------------------------


def test_stage_runner_six_step_lifecycle_and_direct_handoff(tmp_path: Path) -> None:
    stage_root = tmp_path / "stages"
    events: list[str] = []
    start_payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        if request.method == "GET" and request.url.path == "/v1/stages":
            events.append("list_stages")
            return httpx.Response(200, json={"items": [], "total": 0})
        if request.method == "POST" and request.url.path == "/v1/stages/start":
            events.append("start_stage")
            start_payloads.append(body)
            in_file = stage_root / body["workspace_rel"] / "in" / "request.json"
            assert in_file.read_text(encoding="utf-8") == '{"k":1}'
            out_dir = stage_root / body["workspace_rel"] / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "result.json").write_text('{"ok":true}', encoding="utf-8")
            return httpx.Response(
                200,
                json=_stage_payload(
                    stage_execution_id=body["stage_execution_id"],
                    status="running",
                    running=True,
                    exit_code=None,
                    termination_reason=None,
                ),
            )
        if request.method == "GET" and request.url.path == "/v1/stages/21--generator":
            events.append("inspect_stage")
            return httpx.Response(
                200,
                json=_stage_payload(
                    stage_execution_id="21--generator",
                    attempt_id="21",
                    stage_id="generator",
                    fencing_token="tok-21",
                    output_files=["result.json"],
                    iterations={"present": True, "count": 2, "tail": [{"iter": 1}, {"iter": 2}]},
                ),
            )
        if request.method == "POST" and request.url.path == "/v1/stages/21--generator/cleanup":
            events.append("cleanup_stage")
            return httpx.Response(
                200,
                json={
                    "stage_execution_id": "21--generator",
                    "status": "cleaned",
                    "container_removed": True,
                    "workspace_removed": True,
                },
            )
        raise AssertionError(f"Unexpected call: {request.method} {request.url}")

    client = JobRunnerClient(
        "http://job_runner:8090",
        client=httpx.Client(base_url="http://job_runner:8090", transport=httpx.MockTransport(handler)),
    )
    runner = ContainerStageRunner(client, stage_root=stage_root, policy=_default_policy(), poll_interval=0.01)

    def persist(res: Any) -> str:
        events.append("persist")
        assert res.status == "succeeded"
        assert res.resources["memory_peak_bytes"]["value"] == 12_345_678
        assert res.iterations["count"] == 2
        assert b"".join(res.outputs["result.json"].iter_chunks()) == b'{"ok":true}'
        return "persisted-ref"

    result = runner.run_stage(
        StageExecutionSpec(
            attempt_id="21",
            stage_id="generator",
            fencing_token="tok-21",
            input_files={"request.json": MemoryContent(b'{"k":1}')},
            required_outputs=("result.json",),
        ),
        persist=persist,
    )

    assert result.cleaned_up is True
    assert result.persisted_result == "persisted-ref"
    assert start_payloads[0]["pids_limit"] == 256
    assert start_payloads[0]["tmpfs_size_mb"] == 128
    assert start_payloads[0]["cpu_cores"] == 2.0
    assert start_payloads[0]["memory_mb"] == 512
    assert events.index("persist") < events.index("cleanup_stage")
    assert events[-1] == "cleanup_stage"


@pytest.mark.parametrize("mode", ["cancel", "timeout"])
def test_stage_runner_persist_before_cleanup_even_on_cancel_or_timeout(
    tmp_path: Path, mode: str
) -> None:
    stage_root = tmp_path / "stages"
    events: list[str] = []
    stop_reasons: list[str] = []
    stage_state: dict[str, Any] = {"stopped": False, "reason": None}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        if request.method == "GET" and request.url.path == "/v1/stages":
            return httpx.Response(200, json={"items": [], "total": 0})
        if request.method == "POST" and request.url.path == "/v1/stages/start":
            events.append("start_stage")
            stage_state["stopped"] = False
            stage_state["reason"] = None
            out_dir = stage_root / body["workspace_rel"] / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "partial.log").write_text("partial", encoding="utf-8")
            return httpx.Response(
                200,
                json=_stage_payload(
                    stage_execution_id=body["stage_execution_id"],
                    status="running",
                    running=True,
                    exit_code=None,
                    termination_reason=None,
                ),
            )
        if request.method == "POST" and request.url.path.endswith("/stop"):
            events.append("stop_stage")
            stop_reasons.append(body["reason"])
            stage_state["stopped"] = True
            stage_state["reason"] = body["reason"]
            assert body["grace_seconds"] == 3
            return httpx.Response(
                200,
                json=_stage_payload(
                    stage_execution_id="30--solver",
                    exit_code=137,
                    termination_reason=body["reason"],
                    output_files=["partial.log"],
                ),
            )
        if request.method == "GET" and request.url.path == "/v1/stages/30--solver":
            events.append("inspect_stage")
            if not stage_state["stopped"]:
                return httpx.Response(
                    200,
                    json=_stage_payload(
                        stage_execution_id="30--solver",
                        status="running",
                        running=True,
                        exit_code=None,
                        termination_reason=None,
                    ),
                )
            reason = stage_state["reason"] or ("cancelled" if mode == "cancel" else "timed_out")
            return httpx.Response(
                200,
                json=_stage_payload(
                    stage_execution_id="30--solver",
                    exit_code=137,
                    termination_reason=reason,
                    output_files=["partial.log"],
                ),
            )
        if request.method == "POST" and request.url.path.endswith("/cleanup"):
            events.append("cleanup_stage")
            return httpx.Response(
                200,
                json={
                    "stage_execution_id": "30--solver",
                    "status": "cleaned",
                    "container_removed": True,
                    "workspace_removed": True,
                },
            )
        raise AssertionError(f"Unexpected call: {request.method} {request.url}")

    client = JobRunnerClient(
        "http://job_runner:8090",
        client=httpx.Client(base_url="http://job_runner:8090", transport=httpx.MockTransport(handler)),
    )
    runner = ContainerStageRunner(client, stage_root=stage_root, policy=_default_policy(), poll_interval=0.01)

    poll_count = {"n": 0}

    def stop_on_second_poll() -> str | None:
        poll_count["n"] += 1
        return "cancelled" if poll_count["n"] >= 2 else None

    # First verify: if persistence raises, cleanup_stage MUST NOT be called!
    def failing_persist(_res: Any) -> None:
        events.append("persist_fail")
        raise RuntimeError("lease lost before persist")

    with pytest.raises(RuntimeError, match="lease lost"):
        runner.run_stage(
            StageExecutionSpec(
                attempt_id="30",
                stage_id="solver",
                fencing_token="tok-30",
                timeout_seconds=0.02 if mode == "timeout" else 10.0,
            ),
            stop=stop_on_second_poll if mode == "cancel" else None,
            persist=failing_persist,
        )
    assert "cleanup_stage" not in events
    assert "persist_fail" in events

    # Second verify: when persistence succeeds on cancel/timeout, cleanup runs strictly AFTER persist!
    events.clear()
    stop_reasons.clear()
    poll_count["n"] = 0

    def ok_persist(res: Any) -> str:
        events.append("persist_ok")
        assert res.status == ("cancelled" if mode == "cancel" else "timed_out")
        assert "partial.log" in res.outputs
        return "ok"

    res = runner.run_stage(
        StageExecutionSpec(
            attempt_id="30",
            stage_id="solver",
            fencing_token="tok-30",
            timeout_seconds=0.02 if mode == "timeout" else 10.0,
        ),
        stop=stop_on_second_poll if mode == "cancel" else None,
        persist=ok_persist,
    )
    assert res.cleaned_up is True
    assert stop_reasons == ["cancelled" if mode == "cancel" else "timed_out"]
    stop_idx = events.index("stop_stage")
    assert events[stop_idx:] == ["stop_stage", "inspect_stage", "persist_ok", "cleanup_stage"]


@pytest.mark.parametrize("existing_status", ["exited", "running", "created"])
def test_stage_runner_restart_reconciliation_no_duplicate_launch(
    tmp_path: Path, existing_status: str
) -> None:
    stage_root = tmp_path / "stages"
    out_dir = stage_root / "44--solver" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "solution.sol").write_text("kOptimal", encoding="utf-8")

    start_calls = 0
    inspect_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal start_calls, inspect_calls
        if request.method == "GET" and request.url.path == "/v1/stages":
            return httpx.Response(
                200,
                json={
                    "items": [
                        _stage_payload(
                            stage_execution_id="44--solver",
                            attempt_id="44",
                            stage_id="solver",
                            fencing_token="tok-44",
                            status=existing_status,
                            running=existing_status == "running",
                            exit_code=0 if existing_status == "exited" else None,
                            termination_reason="normal" if existing_status == "exited" else None,
                            output_files=["solution.sol"] if existing_status == "exited" else [],
                        )
                    ],
                    "total": 1,
                },
            )
        if request.method == "POST" and request.url.path == "/v1/stages/start":
            start_calls += 1
            # Only expected when resuming a container stuck in status="created".
            return httpx.Response(
                200,
                json=_stage_payload(
                    stage_execution_id="44--solver",
                    attempt_id="44",
                    stage_id="solver",
                    fencing_token="tok-44",
                    reused=True,
                    status="exited",
                    running=False,
                    exit_code=0,
                    termination_reason="normal",
                    output_files=["solution.sol"],
                ),
            )
        if request.method == "GET" and request.url.path == "/v1/stages/44--solver":
            inspect_calls += 1
            return httpx.Response(
                200,
                json=_stage_payload(
                    stage_execution_id="44--solver",
                    attempt_id="44",
                    stage_id="solver",
                    fencing_token="tok-44",
                    reused=True,
                    status="exited",
                    running=False,
                    exit_code=0,
                    termination_reason="normal",
                    output_files=["solution.sol"],
                ),
            )
        if request.method == "POST" and request.url.path == "/v1/stages/44--solver/cleanup":
            return httpx.Response(
                200,
                json={
                    "stage_execution_id": "44--solver",
                    "status": "cleaned",
                    "container_removed": True,
                    "workspace_removed": True,
                },
            )
        raise AssertionError(f"Unexpected call: {request.method} {request.url}")

    client = JobRunnerClient(
        "http://job_runner:8090",
        client=httpx.Client(base_url="http://job_runner:8090", transport=httpx.MockTransport(handler)),
    )
    runner = ContainerStageRunner(client, stage_root=stage_root, policy=_default_policy(), poll_interval=0.01)

    res = runner.run_stage(
        StageExecutionSpec(
            attempt_id="44",
            stage_id="solver",
            fencing_token="tok-44",
            required_outputs=("solution.sol",),
        ),
        persist=lambda _res: "ok",
    )
    assert res.reused is True
    assert res.status == "succeeded"
    assert res.cleaned_up is True
    assert b"".join(res.outputs["solution.sol"].iter_chunks()) == b"kOptimal"
    if existing_status == "created":
        assert start_calls == 1
    else:
        assert start_calls == 0


# ---------------------------------------------------------------------------
# /api/readyz & lifespan embedded scheduler gating tests
# ---------------------------------------------------------------------------


def test_readyz_reports_job_runner_health_and_never_fakes_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from iesplan.computation.runtime.job_runner_client import (
        JobRunnerClient as RealJobRunnerClient,
    )

    monkeypatch.setenv("IESPLAN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "job_runner_url", "http://job_runner:8090")
    monkeypatch.setattr(settings, "embedded_scheduler", False)

    # 1. Reachable /readyz -> 200 with job_runner="ok" and stages="ok"
    def ok_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/readyz"
        return httpx.Response(200, json={"status": "ready", "docker": "ok", "stage_root": "/data/stages"})

    monkeypatch.setattr(
        "iesplan.computation.runtime.job_runner_client.JobRunnerClient",
        lambda base_url: RealJobRunnerClient(
            base_url,
            client=httpx.Client(base_url=base_url, transport=httpx.MockTransport(ok_handler)),
        ),
    )

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        resp = client.get("/api/readyz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["job_runner"] == "ok"
        assert body["stages"] == "ok"

    # 2. Unreachable / 503 job_runner -> 503 (never fake ok!)
    def down_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "docker daemon unreachable"})

    monkeypatch.setattr(
        "iesplan.computation.runtime.job_runner_client.JobRunnerClient",
        lambda base_url: RealJobRunnerClient(
            base_url,
            client=httpx.Client(base_url=base_url, transport=httpx.MockTransport(down_handler)),
        ),
    )

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        resp = client.get("/api/readyz")
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "API-RZ-004"


def test_lifespan_starts_and_stops_embedded_scheduler_only_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "job_runner_url", "")
    monkeypatch.setattr(settings, "embedded_scheduler", False)

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        assert client.app.state.scheduler is None  # type: ignore[union-attr]

    monkeypatch.setattr(settings, "embedded_scheduler", True)
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        scheduler = client.app.state.scheduler  # type: ignore[union-attr]
        assert isinstance(scheduler, EmbeddedScheduler)
        assert scheduler.is_running is True
    assert scheduler.is_running is False


# ---------------------------------------------------------------------------
# EmbeddedScheduler pool cap separation & startup reconciliation tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def memory_db() -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    eng = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    try:
        yield eng, factory
    finally:
        eng.dispose()


def test_embedded_scheduler_enforces_compute_and_io_pool_separation(
    memory_db: tuple[Engine, sessionmaker[Session]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _eng, factory = memory_db
    tasks_domain.force_memory()

    # Seed 3 compute tasks + 2 io tasks directly via tasks_domain
    with factory() as db:
        from auth_helpers import make_user

        from iesplan import project as project_domain

        user = make_user(db, "sched_user", role="admin")
        proj = project_domain.create_project(
            db,
            name="P",
            description="",
            owner_id=user.id,
            created_by=user.id,
            baseline_start_year=2026,
        )
        for i in range(3):
            t = tasks_domain.create_task(
                db,
                project_id=proj.id,
                type="calc",
                idempotency_key=f"c-{i}",
                requested_by=user.id,
            )
            tasks_domain.enqueue(t.id, "compute")
        for i in range(2):
            t = tasks_domain.create_task(
                db,
                project_id=proj.id,
                type="report",
                idempotency_key=f"io-{i}",
                requested_by=user.id,
            )
            tasks_domain.enqueue(t.id, "io")
        db.commit()

    lock = threading.Lock()
    active_by_pool = {"compute": 0, "io": 0}
    max_by_pool = {"compute": 0, "io": 0}
    started_threads: list[str] = []
    release_compute = threading.Event()
    io_done = threading.Event()
    io_completed = 0

    def fake_run_task(
        _factory: Any,
        claim: worker_app.Claim,
        **_kwargs: Any,
    ) -> str:
        nonlocal io_completed
        with _factory() as db:
            task = tasks_domain.get_task(db, claim.task_id)
            assert task is not None
            pool = "compute" if task.type == "calc" else "io"
        with lock:
            started_threads.append(threading.current_thread().name)
            active_by_pool[pool] += 1
            max_by_pool[pool] = max(max_by_pool[pool], active_by_pool[pool])
        try:
            if pool == "compute":
                release_compute.wait(timeout=5.0)
            else:
                time.sleep(0.02)
            with _factory() as db:
                worker_app.submit_attempt_result(
                    db,
                    claim,
                    payload={"outcome": "normal_completion"},
                    outcome="normal_completion",
                )
            if pool == "io":
                with lock:
                    io_completed += 1
                    if io_completed == 2:
                        io_done.set()
            return "completed"
        finally:
            with lock:
                active_by_pool[pool] -= 1

    monkeypatch.setattr("iesplan.worker.scheduler.runner.run_task", fake_run_task)
    monkeypatch.setattr(settings, "compute_slots", 2)
    monkeypatch.setattr(settings, "io_slots", 1)

    scheduler = EmbeddedScheduler(
        session_factory=factory,
        compute_slots=2,
        io_slots=1,
        build_slots=1,
        poll_interval=0.02,
    )
    try:
        scheduler.start()
        # Wait for IO tasks to complete even while compute slots are blocked!
        assert io_done.wait(timeout=5.0), "IO pool was blocked by saturated compute pool"
        assert max_by_pool["compute"] <= 2
        assert max_by_pool["io"] == 1
        assert scheduler.active_compute_count <= 2

        # Dedicated plugin build executor also works independently of saturated compute pool.
        build_thread = scheduler.submit_plugin_build(
            lambda: threading.current_thread().name
        ).result(timeout=2.0)
        assert build_thread.startswith("pies-build")
    finally:
        release_compute.set()
        scheduler.stop(timeout=5.0)

    assert max_by_pool["compute"] == 2
    assert any(name.startswith("pies-compute") for name in started_threads)
    assert any(name.startswith("pies-io") for name in started_threads)


def test_embedded_scheduler_reconcile_on_startup_adopts_active_and_cleans_orphans(
    memory_db: tuple[Engine, sessionmaker[Session]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _eng, factory = memory_db
    with factory() as db:
        from auth_helpers import make_user

        from iesplan import project as project_domain

        user = make_user(db, "rec_user", role="admin")
        proj = project_domain.create_project(
            db,
            name="R",
            description="",
            owner_id=user.id,
            created_by=user.id,
            baseline_start_year=2026,
        )
        task = tasks_domain.create_task(
            db, project_id=proj.id, type="calc", idempotency_key="rec-1", requested_by=user.id
        )
        attempt = tasks_domain.create_attempt(db, task_id=task.id, worker_id="old-backend")
        token = str(uuid4())
        tasks_domain.acquire_lease(db, attempt_id=attempt.id, lease_token=token, acquired_by="old-backend")
        db.commit()
        active_attempt_id = attempt.id

    cleaned_ids: list[str] = []
    stopped_ids: list[str] = []
    adopted_claims: list[int] = []
    adopted_event = threading.Event()

    class FakeStage:
        def __init__(
            self, stage_execution_id: str, attempt_id: str, fencing_token: str, running: bool
        ) -> None:
            self.stage_execution_id = stage_execution_id
            self.attempt_id = attempt_id
            self.stage_id = "solver"
            self.fencing_token = fencing_token
            self.running = running
            self.status = "running" if running else "exited"

    class FakeStageRunner:
        def list_stages(self, **_kwargs: Any) -> list[FakeStage]:
            return [
                FakeStage(f"{active_attempt_id}--solver", str(active_attempt_id), token, True),
                FakeStage("99999--solver", "99999", "stale-tok", True),
            ]

        def stop_stage(self, stage_execution_id: str, **_kwargs: Any) -> None:
            stopped_ids.append(stage_execution_id)

        def cleanup_stage(self, stage_execution_id: str, **_kwargs: Any) -> None:
            cleaned_ids.append(stage_execution_id)

    def fake_run_task(_factory: Any, claim: worker_app.Claim, **_kwargs: Any) -> str:
        adopted_claims.append(claim.attempt_id)
        adopted_event.set()
        return "completed"

    monkeypatch.setattr("iesplan.worker.scheduler.runner.run_task", fake_run_task)
    scheduler = EmbeddedScheduler(
        session_factory=factory,
        compute_slots=1,
        io_slots=1,
        build_slots=1,
        stage_runner=FakeStageRunner(),
        poll_interval=0.5,
    )
    try:
        actions = scheduler.reconcile_on_startup()
        assert adopted_event.wait(timeout=3.0)
    finally:
        scheduler.stop(timeout=3.0)

    assert actions == [f"adopted:{active_attempt_id}--solver", "cleaned:99999--solver"]
    assert adopted_claims == [active_attempt_id]
    assert stopped_ids == ["99999--solver"]
    assert cleaned_ids == ["99999--solver"]


def test_builtin_stage_catalog_contains_all_four_official_stages() -> None:
    catalog = build_builtin_stage_catalog("pies-builtin-stages:1.1.5")
    refs = [d.ref for d in catalog.snapshot()]
    assert refs == [
        "ies.generator.highs_milp@1.0.0",
        "ies.predictor.naive@1.0.0",
        "ies.result-adapter.highs@1.0.0",
        "ies.solver.highs@1.7.2",
    ]
    assert catalog.get_by_kind("evaluator").ref == "ies.result-adapter.highs@1.0.0"
    assert catalog.get_by_kind("solver").command == (
        "python",
        "-m",
        "pies_sdk.runner",
        "--manifest",
        "/app/plugins/solver_highs/plugin.yaml",
    )


def test_computation_package_imports_cleanly_without_httpx() -> None:
    """iesplan.computation & runtime must remain importable in plugin containers without httpx."""
    import subprocess
    import sys

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.modules['httpx'] = None; "
                "import iesplan.computation; import iesplan.computation.runtime; "
                "assert not hasattr(iesplan.computation.runtime, 'JobRunnerClient'); "
                "assert not hasattr(iesplan.computation.runtime, 'ContainerStageRunner')"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_stage_runner_requires_persist_callback_and_enforces_timeout_ceiling(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime, timedelta

    stage_root = tmp_path / "stages"
    out_dir = stage_root / "61--solver" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "solution.sol").write_text("ok", encoding="utf-8")

    started_at = (datetime.now(UTC) - timedelta(seconds=9.5)).isoformat().replace("+00:00", "Z")
    events: list[str] = []
    stopped = {"flag": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/stages":
            events.append("list_stages")
            return httpx.Response(
                200,
                json={
                    "items": [
                        _stage_payload(
                            stage_execution_id="61--solver",
                            attempt_id="61",
                            stage_id="solver",
                            fencing_token="tok-61",
                            status="running",
                            running=True,
                            exit_code=None,
                            termination_reason=None,
                            started_at=started_at,
                            output_files=["solution.sol"],
                        )
                    ],
                    "total": 1,
                },
            )
        if request.method == "POST" and request.url.path == "/v1/stages/61--solver/stop":
            body = json.loads(request.content.decode())
            stopped["flag"] = True
            events.append(f"stop:{body['reason']}")
            return httpx.Response(
                200,
                json=_stage_payload(
                    stage_execution_id="61--solver",
                    attempt_id="61",
                    stage_id="solver",
                    fencing_token="tok-61",
                    status="exited",
                    running=False,
                    exit_code=137,
                    termination_reason=body["reason"],
                    started_at=started_at,
                    output_files=["solution.sol"],
                ),
            )
        if request.method == "GET" and request.url.path == "/v1/stages/61--solver":
            if not stopped["flag"]:
                return httpx.Response(
                    200,
                    json=_stage_payload(
                        stage_execution_id="61--solver",
                        attempt_id="61",
                        stage_id="solver",
                        fencing_token="tok-61",
                        status="running",
                        running=True,
                        exit_code=None,
                        termination_reason=None,
                        started_at=started_at,
                    ),
                )
            return httpx.Response(
                200,
                json=_stage_payload(
                    stage_execution_id="61--solver",
                    attempt_id="61",
                    stage_id="solver",
                    fencing_token="tok-61",
                    status="exited",
                    running=False,
                    exit_code=137,
                    termination_reason="timed_out",
                    started_at=started_at,
                    output_files=["solution.sol"],
                ),
            )
        if request.method == "POST" and request.url.path == "/v1/stages/61--solver/cleanup":
            events.append("cleanup_stage")
            return httpx.Response(
                200,
                json={
                    "stage_execution_id": "61--solver",
                    "status": "cleaned",
                    "container_removed": True,
                    "workspace_removed": True,
                },
            )
        raise AssertionError(f"Unexpected call: {request.method} {request.url}")

    client = JobRunnerClient(
        "http://job_runner:8090",
        client=httpx.Client(base_url="http://job_runner:8090", transport=httpx.MockTransport(handler)),
    )
    policy = ContainerStagePolicy(
        builtin_image="pies-builtin-stages:1.1.5",
        cpu_cores=2.0,
        memory_mb=512,
        pids_limit=256,
        tmpfs_size_mb=128,
        stop_grace_seconds=3,
        max_timeout_seconds=10.0,
    )
    runner = ContainerStageRunner(client, stage_root=stage_root, policy=policy, poll_interval=0.05)

    with pytest.raises(TypeError, match="persist"):
        runner.run_stage(
            StageExecutionSpec(attempt_id="61", stage_id="solver", fencing_token="tok-61"),
            persist=None,  # type: ignore[arg-type]
        )

    # Elapsed ~9.5s out of max_timeout_seconds=10.0s -> remaining <= ~0.5s even with timeout_seconds=None.
    result = runner.run_stage(
        StageExecutionSpec(
            attempt_id="61",
            stage_id="solver",
            fencing_token="tok-61",
            timeout_seconds=None,
            optional_outputs=("solution.sol",),
        ),
        persist=lambda res: events.append(f"persist:{res.status}"),
    )
    assert result.reused is True
    assert result.status == "timed_out"
    assert events == ["list_stages", "stop:timed_out", "persist:timed_out", "cleanup_stage"]


def test_stage_runner_reconciles_existing_container_before_pre_start_stop_probe(
    tmp_path: Path,
) -> None:
    stage_root = tmp_path / "stages"
    events: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/stages":
            events.append("list_stages")
            return httpx.Response(
                200,
                json={
                    "items": [
                        _stage_payload(
                            stage_execution_id="71--solver",
                            attempt_id="71",
                            stage_id="solver",
                            fencing_token="tok-71",
                            status="running",
                            running=True,
                            exit_code=None,
                            termination_reason=None,
                        )
                    ],
                    "total": 1,
                },
            )
        if request.method == "POST" and request.url.path == "/v1/stages/71--solver/stop":
            body = json.loads(request.content.decode())
            events.append(f"stop:{body['reason']}")
            return httpx.Response(
                200,
                json=_stage_payload(
                    stage_execution_id="71--solver",
                    attempt_id="71",
                    stage_id="solver",
                    fencing_token="tok-71",
                    status="exited",
                    running=False,
                    exit_code=137,
                    termination_reason=body["reason"],
                ),
            )
        if request.method == "GET" and request.url.path == "/v1/stages/71--solver":
            return httpx.Response(
                200,
                json=_stage_payload(
                    stage_execution_id="71--solver",
                    attempt_id="71",
                    stage_id="solver",
                    fencing_token="tok-71",
                    status="exited",
                    running=False,
                    exit_code=137,
                    termination_reason="cancelled",
                ),
            )
        if request.method == "POST" and request.url.path == "/v1/stages/71--solver/cleanup":
            events.append("cleanup_stage")
            return httpx.Response(
                200,
                json={
                    "stage_execution_id": "71--solver",
                    "status": "cleaned",
                    "container_removed": True,
                    "workspace_removed": True,
                },
            )
        raise AssertionError(f"Unexpected call: {request.method} {request.url}")

    client = JobRunnerClient(
        "http://job_runner:8090",
        client=httpx.Client(base_url="http://job_runner:8090", transport=httpx.MockTransport(handler)),
    )
    runner = ContainerStageRunner(client, stage_root=stage_root, policy=_default_policy(), poll_interval=0.01)

    res = runner.run_stage(
        StageExecutionSpec(attempt_id="71", stage_id="solver", fencing_token="tok-71"),
        stop=lambda: "cancelled",
        persist=lambda r: events.append(f"persist:{r.status}"),
    )
    assert res.reused is True
    assert res.status == "cancelled"
    assert events == ["list_stages", "stop:cancelled", "persist:cancelled", "cleanup_stage"]


def test_embedded_scheduler_retries_reconciliation_after_transient_startup_failure(
    memory_db: tuple[Engine, sessionmaker[Session]],
) -> None:
    _eng, factory = memory_db
    calls = {"n": 0}

    class FlakyStageRunner:
        def list_stages(self, **_kwargs: Any) -> list[Any]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("job_runner not yet reachable")
            return []

    scheduler = EmbeddedScheduler(
        session_factory=factory,
        compute_slots=1,
        io_slots=1,
        build_slots=1,
        stage_runner=FlakyStageRunner(),
        poll_interval=0.5,
    )
    assert scheduler.reconcile_on_startup() == []
    assert scheduler._reconciled is False
    scheduler.tick_once()
    assert calls["n"] == 2
    assert scheduler._reconciled is True
