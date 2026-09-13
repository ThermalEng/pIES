"""Worker 会话生命周期跨会话行为测试(补正 C: 长时 attempt 不是一个事务)。

只经公共入口推进(``iesplan.worker.lease`` 门面 / ``iesplan.worker.runner``
执行闭环 / tasks 域公开读取), 不绑定私有函数名与源码文本:

- 进度短事务提交后即刻对其他会话可见, 调用方回滚不影响已提交进度;
- 长执行阶段(求解/分析/导出)不持有打开的会话或数据库事务;
- 阶段失败/租约失效不连带回滚已提交进度, 也不把未提交副作用一并提交;
- 续租在独立会话短事务中提交, 与调用方会话状态隔离。

数据库: 文件 SQLite(默认连接池: 不同会话即不同连接, 真实跨会话语义);
队列: IESPLAN_QUEUE=memory; 对象存储: settings.data_dir → tmp_path。
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")

import pytest  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from worker_testkit import setup_environment  # noqa: E402

from iesplan import tasks as tasks_domain  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.models.calc import Task, TaskLease  # noqa: E402
from iesplan.tasks import queue  # noqa: E402
from iesplan.worker import executors, lease, runner  # noqa: E402
from iesplan.worker.executors import EngineRunError  # noqa: E402

# ---------------------------------------------------------------------------
# 测试环境(函数级文件库: 跨会话可见性按提交判定, 非同一连接假象)
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine(tmp_path: Path) -> Iterator[Engine]:
    """函数级文件 SQLite 引擎(默认池: 并发会话使用不同连接)。"""
    eng = create_engine(f"sqlite:///{tmp_path}/sessions.db")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _memory_queue() -> Iterator[None]:
    queue.force_memory()
    yield


@pytest.fixture()
def factory(engine: Engine):
    """会话工厂(Worker 持有的形态: 可调用, 不持有长寿命 Session)。"""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def env(factory, tmp_path: Path) -> dict[str, Any]:
    """迷你 calc 任务环境(任务 queued 已入队, 构建事务已提交)。"""
    with factory() as db:
        return setup_environment(db, tmp_path, task_type="calc")


def _claim(factory, task_id: int, worker_id: str = "cw-sess") -> lease.Claim:
    """经公共领取门面领取(领取事务已提交, 租约即刻可见)。"""
    with factory() as db:
        claim = lease.acquire_attempt(db, task_id, worker_id)
    assert claim is not None
    return claim


def _task_status(factory, task_id: int) -> str:
    with factory() as db:
        row = db.get(Task, task_id)
        assert row is not None
        return row.status


# ---------------------------------------------------------------------------
# 会话追踪工厂(只观测会话开关区间, 不介入业务)
# ---------------------------------------------------------------------------


class SessionTracker:
    """记录每个会话的打开/关闭时刻, 回答“某时间窗内是否有会话处于打开态”。"""

    def __init__(self, maker) -> None:
        self._maker = maker
        self.spans: list[list[float | None]] = []
        self._lock = threading.Lock()

    def __call__(self) -> Session:
        sess = self._maker()
        rec: list[float | None] = [time.monotonic(), None]
        with self._lock:
            self.spans.append(rec)
        orig_close = sess.close

        def _close(*args: Any, **kwargs: Any):
            rec[1] = time.monotonic()
            return orig_close(*args, **kwargs)

        sess.close = _close  # type: ignore[method-assign]
        return sess

    def open_count(self) -> int:
        with self._lock:
            return sum(1 for opened, closed in self.spans if closed is None)

    def spans_window(self, start: float, end: float) -> bool:
        """是否有会话的生命周期覆盖整个 [start, end] 窗口(长寿命会话)。"""
        with self._lock:
            return any(
                opened is not None and opened < start and (closed is None or closed > end)
                for opened, closed in self.spans
            )


# ---------------------------------------------------------------------------
# 1. 进度提交后即刻对其他会话可见
# ---------------------------------------------------------------------------


class TestProgressVisibleAcrossSessions:
    def test_reported_progress_visible_without_caller_commit(
        self, factory, env: dict[str, Any]
    ):
        claim = _claim(factory, env["task"].id)
        with factory() as db:
            assert lease.report_progress(
                db, claim.attempt_id, claim.lease_token,
                env["task"].id, 45.0, "solve", {"it": 1},
            ) is True
            db.rollback()  # 调用方回滚: 已提交进度不受影响
        # 全新会话(从未提交任何事务)直接可见
        with factory() as other:
            progress = tasks_domain.get_progress(other, claim.attempt_id)
        assert progress is not None
        assert progress.progress_percent == 45.0
        assert progress.stage == "solve"

    def test_rejected_progress_writes_nothing(
        self, factory, env: dict[str, Any]
    ):
        """错误 token 的进度被拒绝, 不在其他会话留下任何进度行。"""
        from uuid import uuid4

        claim = _claim(factory, env["task"].id)
        with factory() as db:
            assert lease.report_progress(
                db, claim.attempt_id, uuid4(), env["task"].id, 50.0, "solve",
            ) is False
        with factory() as other:
            assert tasks_domain.get_progress(other, claim.attempt_id) is None


# ---------------------------------------------------------------------------
# 2. 长执行阶段无打开会话/无活动数据库事务
# ---------------------------------------------------------------------------


class TestLongPhaseHoldsNoSession:
    def test_execute_window_has_no_open_session(
        self, factory, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ):
        tracker = SessionTracker(factory)
        marks: dict[str, Any] = {}

        def _slow_calc(ctx, content, data, axis):
            marks["start"] = time.monotonic()
            marks["open_during"] = tracker.open_count()
            time.sleep(0.3)  # 长时求解窗口: 期间不得有打开的会话
            marks["end"] = time.monotonic()
            return {
                "result_kind": "test_result", "status": "ok",
                "outcome": "normal_completion",
            }

        monkeypatch.setattr(executors, "execute_calc", _slow_calc)
        claim = _claim(tracker, env["task"].id)

        status = runner.run_task(tracker, claim, worker_id="w-sess", isolate=False)

        assert status == "completed", status
        assert marks["open_during"] == 0
        assert tracker.spans_window(marks["start"], marks["end"]) is False
        assert _task_status(factory, env["task"].id) == "completed"


# ---------------------------------------------------------------------------
# 3. 阶段失败不连带回滚已提交进度; 迟到收拢不写终态
# ---------------------------------------------------------------------------


class TestFailureDoesNotTakeCommittedProgress:
    def test_rejected_terminal_keeps_committed_progress(
        self, factory, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ):
        """执行中进度已提交 → 租约中途失效 → 失败收拢被 fencing 拒绝:

        已提交进度仍在, 任务保持 running(收拢回滚不连带已提交事务)。
        """
        claim = _claim(factory, env["task"].id)

        def _progress_then_expire(ctx, content, data, axis):
            ctx.progress(50.0, "solve", {"it": 2})
            # 模拟守护进程过期回收(独立会话提交, 执行线程不可见旧状态)
            with factory() as killer:
                row = killer.execute(
                    select(TaskLease).where(TaskLease.attempt_id == claim.attempt_id)
                ).scalars().one()
                row.status = "expired"
                killer.commit()
            raise EngineRunError("求解中租约失效")

        monkeypatch.setattr(executors, "execute_calc", _progress_then_expire)

        status = runner.run_task(factory, claim, worker_id="w-sess", isolate=False)

        assert status == "lease_rejected", status
        with factory() as other:
            progress = tasks_domain.get_progress(other, claim.attempt_id)
            assert progress is not None and progress.progress_percent == 50.0
        assert _task_status(factory, env["task"].id) == "running"

    def test_failed_terminal_keeps_committed_progress(
        self, factory, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ):
        """执行失败 → failed 终态落库, 之前已提交进度仍在且不被改写。"""
        claim = _claim(factory, env["task"].id)

        def _progress_then_fail(ctx, content, data, axis):
            ctx.progress(30.0, "generate", {"step": 1})
            raise EngineRunError("求解失败")

        monkeypatch.setattr(executors, "execute_calc", _progress_then_fail)

        status = runner.run_task(factory, claim, worker_id="w-sess", isolate=False)

        assert status == "failed", status
        with factory() as other:
            progress = tasks_domain.get_progress(other, claim.attempt_id)
            assert progress is not None and progress.progress_percent == 30.0
        assert _task_status(factory, env["task"].id) == "failed"


# ---------------------------------------------------------------------------
# 4. 续租在独立会话短事务中提交
# ---------------------------------------------------------------------------


class TestRenewIndependentSession:
    def test_renew_survives_caller_rollback(
        self, factory, env: dict[str, Any]
    ):
        claim = _claim(factory, env["task"].id)
        with factory() as reader:
            before = reader.execute(
                select(TaskLease).where(TaskLease.attempt_id == claim.attempt_id)
            ).scalars().one().expires_at

        caller = factory()
        try:
            row = caller.get(Task, env["task"].id)
            assert row is not None
            row.priority = 999  # 调用方未提交的脏写
            # 续租走独立会话(Worker 心跳线程形态), 提交自己的短事务
            with factory() as renew_db:
                assert lease.renew_lease(
                    renew_db, claim.attempt_id, claim.lease_token
                ) is True
            caller.rollback()  # 调用方回滚不得连带续租
        finally:
            caller.close()

        with factory() as other:
            lease_row = other.execute(
                select(TaskLease).where(TaskLease.attempt_id == claim.attempt_id)
            ).scalars().one()
            assert lease_row.expires_at > before
            assert other.get(Task, env["task"].id).priority != 999
