"""任务解释器长停顿不得阻塞控制循环；仍保留租约隔离和原终态协议。"""

import ctypes
import json
import subprocess
import sys
import time
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from iesplan.application import worker as worker_app
from iesplan.worker import main as worker_main


def _hold_gil(started, finished, stop, seconds):
    # PyDLL（不同于 CDLL）在 C 调用期间不释放 GIL。无需大内存即可
    # 稳定模拟真实构模中完整 GC 导致的解释器全停顿。
    started.set()
    ctypes.PyDLL(None).usleep(int(seconds * 1_000_000))
    finished.set()
    stop.wait(3)


def _exit_abruptly():
    raise SystemExit(7)


@pytest.fixture
def worker(monkeypatch):
    instance = worker_main.Worker(
        compute=object(), session_factory=nullcontext,
        heartbeat_interval=0.03, poll_interval=0.01,
    )
    monkeypatch.setattr(worker_app, "publish_heartbeat", Mock())
    yield instance
    if instance._task_process is not None:
        instance._cancel_event.set()
        instance._task_process.join(timeout=5)
        if instance._task_process.is_alive():
            instance._task_process.kill()
        instance._reap_task_process()


def test_claim_starts_spawn_process_with_only_claim_and_stop(worker, monkeypatch):
    claim = SimpleNamespace(task_id=11, attempt_id=12, attempt_no=1, lease_token="token")
    monkeypatch.setattr(worker_app, "acquire_attempt", Mock(return_value=claim))
    context = Mock()
    worker._process_context = context
    worker._claim_and_run(claim.task_id)
    assert worker._cancel_event is context.Event.return_value
    context.Process.assert_called_once_with(
        target=worker_main.run_claimed_task,
        args=("compute", worker.worker_id, claim, worker._fence_event, worker._cancel_event), name="task-11",
    )
    context.Process.return_value.start.assert_called_once_with()
    # This test only checks launch arguments; there is no real process to reap.
    worker._task_process = None


def test_process_start_failure_does_not_leave_a_phantom_running_process(worker, monkeypatch):
    claim = SimpleNamespace(task_id=11, attempt_id=12, attempt_no=1, lease_token="token")
    monkeypatch.setattr(worker_app, "acquire_attempt", Mock(return_value=claim))
    context = Mock()
    worker._process_context = context
    monkeypatch.setattr(worker_app, "fail_attempt", Mock())
    context.Process.return_value.start.side_effect = OSError("cannot start process")
    with pytest.raises(OSError, match="cannot start process"):
        worker._claim_and_run(claim.task_id)
    context.Process.return_value.close.assert_called_once_with()
    assert worker._task_process is None
    assert worker._cancel_event is None
    assert worker._current_task_id is None


def test_abrupt_task_exit_is_reaped_without_fabricating_terminal_state(worker, monkeypatch):
    worker._current_task_id = 11
    worker._task_process = worker._process_context.Process(target=_exit_abruptly)
    worker._task_process.start()
    worker._task_process.join(timeout=10)
    assert worker._task_process.exitcode == 7
    fail = Mock()
    monkeypatch.setattr(worker_app, "fail_attempt", fail)
    worker._tick()
    assert worker._task_process is None
    assert worker._current_task_id is None
    fail.assert_not_called()  # No claim was registered for this process.


def test_supervised_exit_reports_evidence_with_the_original_claim(worker, monkeypatch):
    claim = SimpleNamespace(task_id=11, attempt_id=12, attempt_no=1, lease_token="token")
    before, after = object(), object()
    worker._claim, worker._resource_start = claim, before
    worker._current_task_id = claim.task_id
    worker._cancel_event = worker._process_context.Event()
    worker._task_process = worker._process_context.Process(target=_exit_abruptly)
    worker._task_process.start()
    worker._task_process.join(timeout=10)
    report = Mock()
    monkeypatch.setattr(worker_app, "observe_local_resources", lambda: after)
    monkeypatch.setattr(worker_app, "report_task_process_exit", report)
    worker._reap_task_process()
    report.assert_called_once_with(None, claim, exit_code=7, before=before, after=after)
    assert worker._claim is None and worker._resource_start is None


@pytest.mark.parametrize("worker_type", ["compute", "io"])
def test_task_entrypoint_assembles_its_own_runtime(worker_type, monkeypatch):
    from iesplan import bootstrap

    context = Mock()
    assembly = Mock(return_value=context)
    monkeypatch.setattr(bootstrap, "assemble_task_runtime", assembly)
    run_task = Mock()
    monkeypatch.setattr(worker_main.runner, "run_task", run_task)
    claim, stop, cancel = object(), object(), object()
    worker_main.run_claimed_task(worker_type, "worker", claim, stop, cancel)
    assembly.assert_called_once_with(worker_type)
    run_task.assert_called_once_with(
        context.session_factory, claim, worker_id="worker", stop_event=stop, cancel_event=cancel,
        compute=context.compute_execution_plane.return_value if worker_type == "compute" else None,
    )


def test_spawned_attempt_completes_real_solve_and_seals_results(tmp_path, monkeypatch):
    """公开小模型经真实领取、spawn、HiGHS、检查点与结果封存，不继承测试桩。"""
    from collections.abc import Iterator

    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session, sessionmaker
    from test_worker_compute_chain import _CONFIG_BODY, PLANNING_BODY, _submit_calc_task

    from iesplan.api.deps import get_request_db
    from iesplan.config import settings
    from iesplan.db import Base
    from iesplan.main import create_app
    from iesplan.results.persistence import EvidencePackage, ResultIndex
    from iesplan.storage import get_object
    from iesplan.tasks.persistence import Task, TaskAttempt

    url = f"sqlite+pysqlite:///{tmp_path}/task.db"
    stage_root = tmp_path / "stages"
    monkeypatch.setenv("IESPLAN_DB_URL", url)
    monkeypatch.setenv("IESPLAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IESPLAN_STAGE_ROOT", str(stage_root))
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = create_app()
    with factory() as db:
        def request_db() -> Iterator[Session]:
            yield db

        app.dependency_overrides[get_request_db] = request_db
        with TestClient(app, raise_server_exceptions=False) as client:
            task_id = _submit_calc_task(client, db, dict(_CONFIG_BODY), dict(PLANNING_BODY))
        snapshot_id = db.get(Task, task_id).calc_snapshot_id
    log_path = tmp_path / "worker.log"
    from stage_testkit import serve_mock_job_runner

    with serve_mock_job_runner(stage_root) as job_runner_url:
        monkeypatch.setenv("IESPLAN_JOB_RUNNER_URL", job_runner_url)
        with log_path.open("w") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "iesplan.worker.main", "--worker-type", "compute"],
                stdout=log, stderr=subprocess.STDOUT, text=True,
            )
        try:
            deadline = time.monotonic() + 60
            while process.poll() is None and time.monotonic() < deadline:
                with factory() as db:
                    if db.get(Task, task_id).status == "completed":
                        break
                time.sleep(0.05)
            with factory() as db:
                task = db.get(Task, task_id)
                assert (task.status, task.business_outcome) == (
                    "completed", "normal_completion",
                ), log_path.read_text()
                assert (task.calc_snapshot_id, task.attempt_count) == (snapshot_id, 1)
                attempt = db.scalar(select(TaskAttempt).where(TaskAttempt.task_id == task_id))
                assert attempt.status == "succeeded"
                evidence = db.scalar(select(EvidencePackage).where(EvidencePackage.task_id == task_id))
                assert evidence.attempt_id == attempt.id
                assert db.scalar(select(ResultIndex).where(ResultIndex.evidence_package_id == evidence.id))
                sealed = json.loads(get_object(db, evidence.object_id))
                assert sealed["compute_result"]["math_status"] == "solved"
                assert sealed["receipt"]["status"] == "succeeded"
                assert sealed["receipt"]["attempt_id"] == str(attempt.id)
        finally:
            process.terminate()
            try:
                process.wait(timeout=40)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            engine.dispose()
    assert process.returncode == 0, log_path.read_text()


def test_control_plane_pause_does_not_stop_a_live_attempt(worker, monkeypatch):
    from sqlalchemy.exc import OperationalError

    worker._claim = SimpleNamespace(task_id=11)
    process = Mock()
    worker._task_process = process
    worker._fence_event = worker._process_context.Event()
    worker._cancel_event = worker._process_context.Event()
    check = Mock(side_effect=[OperationalError("SELECT", None, OSError("DB offline")), None])
    monkeypatch.setattr(worker_app, "attempt_stop_reason", check)
    monotonic = Mock(side_effect=[100.0, 221.0])
    monkeypatch.setattr(worker_main.time, "monotonic", monotonic)

    try:
        with pytest.raises(OperationalError):
            worker._tick()
        worker._tick()
        assert not worker._fence_event.is_set()
        assert not worker._cancel_event.is_set()
        process.terminate.assert_not_called()
    finally:
        worker._task_process = None
