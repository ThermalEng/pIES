"""Worker 会话生命周期跨会话行为测试(补正 C: 长时 attempt 不是一个事务)。

只经公开阶段网关推进（``iesplan.application.worker`` 阶段命令与
tasks 域公开读取），不涉及尚未实现的 0.8 计算链：

- 进度短事务提交后即刻对其他会话可见, 调用方回滚不影响已提交进度;
- 续租在独立会话短事务中提交, 与调用方会话状态隔离。

数据库: 文件 SQLite(默认连接池: 不同会话即不同连接, 真实跨会话语义);
队列: IESPLAN_QUEUE=memory; 对象存储: settings.data_dir → tmp_path。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")

import pytest  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from worker_testkit import setup_environment  # noqa: E402

from iesplan import tasks as tasks_domain  # noqa: E402
from iesplan.application import worker as worker_app  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.tasks.persistence import Task, TaskLease  # noqa: E402
from iesplan.tasks import queue  # noqa: E402

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


def _claim(factory, task_id: int, worker_id: str = "cw-sess") -> worker_app.Claim:
    """经公开阶段网关领取(领取事务已提交, 租约即刻可见)。"""
    with factory() as db:
        claim = worker_app.acquire_attempt(db, task_id, worker_id)
    assert claim is not None
    return claim


# ---------------------------------------------------------------------------
# 1. 进度提交后即刻对其他会话可见
# ---------------------------------------------------------------------------


class TestProgressVisibleAcrossSessions:
    def test_reported_progress_visible_without_caller_commit(
        self, factory, env: dict[str, Any]
    ):
        claim = _claim(factory, env["task"].id)
        with factory() as db:
            assert worker_app.report_attempt_progress(
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
            assert worker_app.report_attempt_progress(
                db, claim.attempt_id, uuid4(), env["task"].id, 50.0, "solve",
            ) is False
        with factory() as other:
            assert tasks_domain.get_progress(other, claim.attempt_id) is None


# ---------------------------------------------------------------------------
# 2. 续租在独立会话短事务中提交
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
                assert worker_app.renew_attempt_lease(
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
