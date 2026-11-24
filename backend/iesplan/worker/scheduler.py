"""Embedded task scheduler & isolated bounded pools converged into backend.

Replaces standalone persistent ``io_worker`` and ``compute_worker`` daemon
containers (ADR-0016):
- Isolates HTTP request threads from background scheduling, compute execution,
  large-file I/O, and plugin image builds via separate bounded executors;
- Enforces ``compute_slots``, ``io_slots``, and ``build_slots`` from ``pies.toml``;
- Reconciles in-flight ``job_runner`` stage containers on startup (and retries
  reconciliation on transient failures) via existing ``TaskAttempt``,
  ``Fencing Token``, and container labels without creating a second task state
  machine or launching duplicate stage containers.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from iesplan.application import worker as worker_app
from iesplan.worker import runner

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ActiveTask:
    claim: worker_app.Claim
    pool: str
    future: Future[str]
    cancel_event: threading.Event
    fence_event: threading.Event


class EmbeddedScheduler:
    """Lightweight embedded scheduler hosting separate bounded compute, IO, and build pools."""

    def __init__(
        self,
        *,
        session_factory: Any,
        compute_slots: int,
        io_slots: int,
        build_slots: int,
        compute: Any = None,
        stage_runner: Any = None,
        worker_id: str | None = None,
        poll_interval: float = 0.5,
        heartbeat_interval: float = 5.0,
    ) -> None:
        if session_factory is None:
            raise TypeError("EmbeddedScheduler 缺少必需的 session_factory(由组合根装配显式传入)")
        self.session_factory = session_factory
        self.compute_slots = int(compute_slots)
        self.io_slots = int(io_slots)
        self.build_slots = int(build_slots)
        self.compute = compute
        self.stage_runner = stage_runner
        self.worker_id = worker_id or f"backend-{socket.gethostname()}-{os.getpid()}"
        self.poll_interval = float(poll_interval)
        self.heartbeat_interval = float(heartbeat_interval)

        self._compute_executor = ThreadPoolExecutor(
            max_workers=self.compute_slots,
            thread_name_prefix="pies-compute",
        )
        self._io_executor = ThreadPoolExecutor(
            max_workers=self.io_slots,
            thread_name_prefix="pies-io",
        )
        self._build_executor = ThreadPoolExecutor(
            max_workers=self.build_slots,
            thread_name_prefix="pies-build",
        )
        self._lock = threading.Lock()
        self._active: dict[int, _ActiveTask] = {}
        self._stop = threading.Event()
        self._shutdown_event = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_heartbeat = 0.0
        self._started = False
        self._reconciled = False

    @property
    def shutdown_event(self) -> threading.Event:
        """Control-plane shutdown event propagated as detach signal to active stage waits."""
        return self._shutdown_event

    @property
    def is_running(self) -> bool:
        return self._started and not self._stop.is_set()

    @property
    def active_compute_count(self) -> int:
        with self._lock:
            return sum(1 for item in self._active.values() if item.pool == "compute")

    @property
    def active_io_count(self) -> int:
        with self._lock:
            return sum(1 for item in self._active.values() if item.pool == "io")

    def submit_plugin_build(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[Any]:
        """Run a plugin image build in the dedicated bounded build executor, off HTTP threads."""
        return self._build_executor.submit(fn, *args, **kwargs)

    def submit_io_job(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[Any]:
        """Run a background IO operation in the bounded IO executor, off HTTP threads."""
        return self._io_executor.submit(fn, *args, **kwargs)

    def start(self) -> None:
        if self._started:
            return
        self._shutdown_event.clear()
        self._stop.clear()
        if not self._reconciled:
            self.reconcile_on_startup()
        self._thread = threading.Thread(
            target=self._loop,
            name="pies-scheduler",
            daemon=True,
        )
        self._started = True
        self._thread.start()
        logger.info(
            "EmbeddedScheduler started: id=%s compute_slots=%s io_slots=%s build_slots=%s",
            self.worker_id,
            self.compute_slots,
            self.io_slots,
            self.build_slots,
        )

    def stop(self, *, timeout: float = 10.0) -> None:
        self._shutdown_event.set()
        self._stop.set()
        self._wake.set()
        deadline = time.monotonic() + max(0.0, float(timeout))
        if self._started:
            if self._thread is not None:
                remaining = max(0.0, deadline - time.monotonic())
                self._thread.join(timeout=remaining)
                self._thread = None
            with self._lock:
                active_futures = [item.future for item in self._active.values()]
            if active_futures:
                remaining = max(0.0, deadline - time.monotonic())
                wait_futures(active_futures, timeout=remaining)
            try:
                worker_app.publish_heartbeat(self.worker_id, {}, ttl=1)
            except Exception:  # noqa: BLE001 - best-effort cleanup on shutdown
                logger.debug("Failed to clear scheduler heartbeat on shutdown", exc_info=True)
            self._started = False
        self._shutdown_executors()
        logger.info("EmbeddedScheduler stopped: id=%s", self.worker_id)

    def _shutdown_executors(self) -> None:
        for executor in (self._compute_executor, self._io_executor, self._build_executor):
            executor.shutdown(wait=False, cancel_futures=True)

    def reconcile_on_startup(self) -> list[str]:
        """Reconcile managed job_runner containers and authoritative active DB claims."""
        actions: list[str] = []
        stages: tuple[Any, ...] = ()
        if self.stage_runner is not None:
            try:
                stages = tuple(self.stage_runner.list_stages(include_stats=False))
            except Exception:  # noqa: BLE001 - retry on subsequent ticks until job_runner responds
                logger.warning(
                    "Startup stage reconciliation could not list job_runner stages",
                    exc_info=True,
                )
                return actions

        with self.session_factory() as db:
            active_claims = worker_app.list_active_reconcilable_claims(db)
            claim_pools = {
                claim.attempt_id: worker_app.get_task_pool(db, claim.task_id)
                for claim in active_claims
            }

        active_by_key = {
            (str(claim.attempt_id), str(claim.lease_token)): claim
            for claim in active_claims
        }
        adopted_attempts: set[int] = set()

        # (a) Clean up any job_runner container whose (attempt_id, fencing_token)
        # does not match any active DB claim; adopt matched container attempts.
        for stage in stages:
            matched_claim = active_by_key.get(
                (str(stage.attempt_id), str(stage.fencing_token))
            )
            if matched_claim is None:
                if stage.running or stage.status in ("created", "running", "paused", "restarting"):
                    try:
                        self.stage_runner.stop_stage(stage.stage_execution_id, reason="fenced")
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "Failed to stop orphaned stage %s",
                            stage.stage_execution_id,
                            exc_info=True,
                        )
                try:
                    self.stage_runner.cleanup_stage(stage.stage_execution_id, force=False)
                    actions.append(f"cleaned:{stage.stage_execution_id}")
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Failed to cleanup orphaned stage %s",
                        stage.stage_execution_id,
                        exc_info=True,
                    )
                continue

            pool = claim_pools[matched_claim.attempt_id]
            if (
                (pool != "compute" or self.compute is not None)
                and matched_claim.attempt_id not in adopted_attempts
                and self._can_adopt_attempt(matched_claim.attempt_id)
            ):
                adopted_attempts.add(matched_claim.attempt_id)
                self._launch_claimed_task(matched_claim, pool=pool)
                actions.append(f"adopted:{stage.stage_execution_id}")

        # (b) Launch recovery for EVERY remaining active DB claim even if job_runner
        # has zero containers for that attempt (pre-Stage-1, inter-stage, post-cleanup).
        for claim in active_claims:
            if claim.attempt_id in adopted_attempts:
                continue
            pool = claim_pools[claim.attempt_id]
            if pool == "compute" and self.compute is None:
                continue
            if self._can_adopt_attempt(claim.attempt_id):
                adopted_attempts.add(claim.attempt_id)
                self._launch_claimed_task(claim, pool=pool)
                actions.append(f"adopted:attempt-{claim.attempt_id}")

        self._reconciled = True
        return actions

    def _can_adopt_attempt(self, attempt_id: int) -> bool:
        with self._lock:
            return attempt_id not in self._active

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick_once()
            except Exception:  # noqa: BLE001 - single tick failure must not kill scheduler loop
                logger.exception("EmbeddedScheduler tick error")
            self._wake.wait(timeout=self.poll_interval)
            self._wake.clear()

    def tick_once(self) -> None:
        """Execute one scheduler cycle: reconcile if needed, reap, control check, heartbeat, claim."""
        if self.stage_runner is not None and not self._reconciled:
            self.reconcile_on_startup()
            if not self._reconciled:
                return
        self._reap_finished()
        now = time.monotonic()
        if now - self._last_heartbeat >= self.heartbeat_interval:
            self._publish_heartbeat()
            self._check_controls()
            self._last_heartbeat = now
        if self._stop.is_set():
            return
        if self.compute is not None:
            self._fill_pool("compute", self.compute_slots)
        self._fill_pool("io", self.io_slots)

    def _reap_finished(self) -> None:
        with self._lock:
            done_ids = [aid for aid, item in self._active.items() if item.future.done()]
            done_items = [self._active.pop(aid) for aid in done_ids]
        for item in done_items:
            try:
                item.future.result()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Background task failed unexpectedly: task=%s attempt=%s pool=%s",
                    item.claim.task_id,
                    item.claim.attempt_id,
                    item.pool,
                )

    def _publish_heartbeat(self) -> None:
        with self._lock:
            running_attempts = [
                {"task_id": item.claim.task_id, "attempt_id": item.claim.attempt_id, "pool": item.pool}
                for item in self._active.values()
            ]
        payload = {
            "worker_id": self.worker_id,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "pool": "backend",
            "alive_at": datetime.now(UTC).isoformat(),
            "running_attempts": running_attempts,
            "load": {
                "compute_active": self.active_compute_count,
                "compute_slots": self.compute_slots,
                "io_active": self.active_io_count,
                "io_slots": self.io_slots,
            },
        }
        worker_app.publish_heartbeat(
            self.worker_id, payload, ttl=max(5, int(self.heartbeat_interval * 3))
        )

    def _check_controls(self) -> None:
        with self._lock:
            items = list(self._active.values())
        for item in items:
            try:
                with self.session_factory() as db:
                    reason = worker_app.attempt_stop_reason(db, item.claim)
            except Exception:  # noqa: BLE001
                logger.debug("Control probe failed for attempt=%s", item.claim.attempt_id, exc_info=True)
                continue
            if reason == "fenced":
                item.fence_event.set()
            elif reason == "cancelled":
                item.cancel_event.set()

    def _fill_pool(self, pool: str, capacity: int) -> None:
        while not self._stop.is_set():
            current = self.active_compute_count if pool == "compute" else self.active_io_count
            if current >= capacity:
                return
            with self.session_factory() as db:
                if not worker_app.slot_available(db, pool):
                    return
            task_id = worker_app.dequeue_task(pool)
            if task_id is None:
                with self.session_factory() as db:
                    task_id = worker_app.find_queued_task(db, pool)
                if task_id is None:
                    return
            with self.session_factory() as db:
                claim = worker_app.acquire_attempt(db, task_id, self.worker_id)
            if claim is None:
                return
            self._launch_claimed_task(claim, pool=pool)

    def _launch_claimed_task(self, claim: worker_app.Claim, *, pool: str) -> None:
        cancel_event = threading.Event()
        fence_event = threading.Event()
        executor = self._compute_executor if pool == "compute" else self._io_executor
        compute_arg = self.compute if pool == "compute" else None
        future = executor.submit(
            runner.run_task,
            self.session_factory,
            claim,
            worker_id=self.worker_id,
            stop_event=fence_event,
            cancel_event=cancel_event,
            detach_event=self._shutdown_event,
            compute=compute_arg,
        )
        with self._lock:
            self._active[claim.attempt_id] = _ActiveTask(
                claim=claim,
                pool=pool,
                future=future,
                cancel_event=cancel_event,
                fence_event=fence_event,
            )


__all__ = ["EmbeddedScheduler"]
