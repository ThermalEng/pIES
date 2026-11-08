"""Optional deployment observations and evidence-based resource failures."""

import signal
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_worker_outcome as outcome
from sqlalchemy import select

from iesplan import tasks
from iesplan.application import worker
from iesplan.computation import observe_local_resources
from iesplan.core.diagnostics import TASK_PROCESS_EXITED, TASK_RESOURCE_EXHAUSTED
from iesplan.tasks.persistence import ComputeSlot
from iesplan.worker import runner
from iesplan.worker.executors import EngineRunError, ResourceExhaustedError, TaskTimedOut

# Reuse the existing transaction fixture without a second lifecycle model.
engine, db, env, factory = outcome.engine, outcome.db, outcome.env, outcome.factory
_clean_state, _task_diagnostics = outcome._clean_state, outcome._task_diagnostics


def observation(root: Path, *, kills: int = 0, memory: str = "8589934592"):
    root.mkdir(exist_ok=True)
    for name, value in {
        "memory.max": memory, "memory.swap.max": "0", "memory.current": "100",
        "memory.peak": "200", "memory.swap.current": "0", "memory.events": f"oom_kill {kills}\n",
    }.items():
        (root / name).write_text(value)
    return observe_local_resources(root)


def test_finite_limits_zero_swap_and_shared_scope(tmp_path):
    data = observation(tmp_path).to_dict()
    assert data["memory_limit"] == {"status": "limited", "bytes": 8 * 1024**3}
    assert data["swap_limit"] == {"status": "limited", "bytes": 0}
    assert data["scope"] == "worker_cgroup"
    assert data["memory_current_bytes"] == 100
    assert data["memory_peak_bytes"] == 200


def test_unknown_is_not_unlimited_and_observation_does_not_block(tmp_path):
    unknown = observe_local_resources(tmp_path / "not-mounted").to_dict()
    assert unknown["memory_limit"] == {"status": "unknown", "bytes": None}
    assert unknown["memory_peak_bytes"] is None
    assert unknown["oom_kill_count"] is None
    unlimited = observation(tmp_path, memory="max").to_dict()
    assert unlimited["memory_limit"] == {"status": "unlimited", "bytes": None}
    (tmp_path / "memory.current").write_text("not-a-number")
    (tmp_path / "memory.events").write_text("oom_kill invalid\n")
    malformed = observe_local_resources(tmp_path).to_dict()
    assert malformed["memory_current_bytes"] is None
    assert malformed["oom_kill_count"] is None


@pytest.mark.parametrize("status,reason,error", [
    ("process_failed", "oom", ResourceExhaustedError),
    ("process_failed", "signaled", EngineRunError),
    ("timed_out", "timeout", TaskTimedOut),
])
def test_execution_resource_failure_uses_receipt_not_solver_identity(status, reason, error):
    receipt = SimpleNamespace(status=status, termination_reason=reason,
                              receipt_id="test-receipt", error="actual executor evidence")
    with pytest.raises(error):
        runner._raise_for_receipt_status(receipt)


def test_confirmed_process_oom_fails_immediately_without_identical_retry(db, env, tmp_path):
    claim = worker.acquire_attempt(db, env["task"].id, "resource-test")
    worker.report_attempt_progress(db, claim.attempt_id, claim.lease_token, claim.task_id,
                                   20, "materialize", {"reason": "test"})
    before = observation(tmp_path / "before", kills=2)
    after = observation(tmp_path / "after", kills=3)
    worker.report_task_process_exit(db, claim, exit_code=-signal.SIGKILL, before=before, after=after)
    task = worker.get_task_record(db, claim.task_id)
    assert task.status == "failed" and task.attempt_count == 1
    assert tasks.get_attempt(db, claim.attempt_id).stop_reason == TASK_RESOURCE_EXHAUSTED
    assert worker.recover_expired_attempts(db, "compute") == 0
    assert all(slot.in_use == 0 for slot in db.scalars(select(ComputeSlot)))
    diag = _task_diagnostics(db, claim.task_id)[-1]
    assert diag.code == TASK_RESOURCE_EXHAUSTED
    assert "materialize" in diag.message and "8589934592" in diag.message
    assert diag.context["worker_resources"] == after.to_dict()


@pytest.mark.parametrize("before_kills,after_kills,exit_code", [
    (0, 0, -signal.SIGKILL), (None, 1, -signal.SIGKILL),
    (2, 1, -signal.SIGKILL), (0, 1, 7),
])
def test_abrupt_exit_without_matching_evidence_is_not_oom(
    db, env, tmp_path, before_kills, after_kills, exit_code,
):
    claim = worker.acquire_attempt(db, env["task"].id, "resource-test")
    before = observation(tmp_path / "before", kills=before_kills)
    after = observation(tmp_path / "after", kills=after_kills)
    worker.report_task_process_exit(db, claim, exit_code=exit_code, before=before, after=after)
    assert worker.get_task_record(db, claim.task_id).status == "running"
    diag = _task_diagnostics(db, claim.task_id)[-1]
    assert diag.code == TASK_PROCESS_EXITED and "原因未确认" in diag.message


def test_oom_cannot_overwrite_a_fenced_attempt(db, env, tmp_path):
    claim = worker.acquire_attempt(db, env["task"].id, "resource-test")
    worker.fail_attempt(db, claim, code="TEST", message="already closed")
    before = observation(tmp_path / "before", kills=0)
    after = observation(tmp_path / "after", kills=1)
    count = len(_task_diagnostics(db, claim.task_id))
    worker.report_task_process_exit(db, claim, exit_code=-signal.SIGKILL, before=before, after=after)
    assert len(_task_diagnostics(db, claim.task_id)) == count
    assert tasks.get_attempt(db, claim.attempt_id).stop_reason == "TEST"


def test_new_attempt_does_not_borrow_previous_attempt_stage(db, env, tmp_path):
    from iesplan.application.tasks import retry_task
    from iesplan.identity import get_user

    task_id = env["task"].id
    previous = worker.acquire_attempt(db, task_id, "resource-test")
    worker.report_attempt_progress(db, previous.attempt_id, previous.lease_token, task_id, 85, "adapt")
    worker.fail_attempt(db, previous, code="TEST", message="interrupted")
    retry_task(db, get_user(db, env["user"].id), task_id)
    current = worker.acquire_attempt(db, task_id, "resource-test")
    worker.fail_resource_attempt(db, current, cause="MemoryError",
                                 resources=observation(tmp_path).to_dict())
    assert _task_diagnostics(db, task_id)[-1].context["stage"] == "not_reported"


def test_memory_error_records_stage_and_does_not_become_generic_failure(
    db, env, factory, monkeypatch, tmp_path,
):
    claim = worker.acquire_attempt(db, env["task"].id, "resource-test")
    observed = observation(tmp_path / "resources")
    monkeypatch.setattr(worker, "observe_local_resources", lambda: observed)

    def exhaust(ctx):
        ctx.progress(85, "adapt", {"receipt_status": "succeeded"})
        raise MemoryError()

    monkeypatch.setattr(runner, "dispatch", exhaust)
    assert runner.run_task(factory, claim) == "failed"
    progress = tasks.latest_progress_for_task(db, claim.task_id)
    assert progress.detail["worker_resources"] == observed.to_dict()
    diag = _task_diagnostics(db, claim.task_id)[-1]
    assert diag.code == TASK_RESOURCE_EXHAUSTED
    assert "adapt" in diag.message and "MemoryError" in diag.message
