"""槽并发回归(PG 定向): ensure 增建在双事务下不重复(唯一键 + upsert 串行)。

默认跳过; 设置 IESPLAN_TEST_PG_URL(指向隔离空库)即运行。conftest 把默认
引擎钉死在 sqlite, 本文件自建 PG 引擎、自建表、自清行, 不污染套件。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

PG_URL = os.environ.get("IESPLAN_TEST_PG_URL")

pytestmark = pytest.mark.skipif(not PG_URL, reason="需 IESPLAN_TEST_PG_URL 指向隔离 PG 空库")

#: 本文件专属池名前缀；清理只删这些池，不碰其他数据。
OWN_POOLS = ("pg_race_a", "pg_race_b", "pg_race_c")

#: 用户数据表黑名单：目标库一旦存在其中任何表，即视为非隔离库，拒绝建表/删行。
USER_TABLE_DENYLIST = frozenset(
    {"users", "roles", "projects", "project_versions", "tasks", "datasets", "calc_snapshots"}
)


def _assert_isolated(eng: Engine) -> None:
    """建表/删行前守卫：用户表有行即视为非隔离库；自有池外不得有槽行。

    只看行数不看表存在：本文件 create_all 建的空表不算用户数据，
    否则同库重跑会被自己的空表拒绝。
    """
    tables = set(sa.inspect(eng).get_table_names())
    for table in sorted(tables & USER_TABLE_DENYLIST):
        with Session(eng) as db:
            rows = db.execute(sa.text(f'SELECT COUNT(*) FROM "{table}"')).scalar() or 0
        if rows:
            pytest.fail(f"IESPLAN_TEST_PG_URL 非隔离库(用户表 {table} 有 {rows} 行)，拒绝建表/删行")
    if "compute_slots" in tables:
        with Session(eng) as db:
            foreign = (
                db.execute(
                    select(ComputeSlot.pool_name).where(ComputeSlot.pool_name.not_in(OWN_POOLS)).limit(1)
                )
                .scalars()
                .first()
            )
        if foreign is not None:
            pytest.fail(f"IESPLAN_TEST_PG_URL 含外部槽池 {foreign!r}，拒绝删行")


def _delete_own_rows(db: Session) -> None:
    """只删本文件专属池的槽行。"""
    db.execute(delete(ComputeSlot).where(ComputeSlot.pool_name.in_(OWN_POOLS)))
    db.commit()


# 注册全部领域表(create_all 需要被引表; 与组合根加载范围一致)
import iesplan.audit.persistence  # noqa: E402,F401
import iesplan.configuration.persistence  # noqa: E402,F401
import iesplan.dataset.persistence  # noqa: E402,F401
import iesplan.identity.persistence  # noqa: E402,F401
import iesplan.model.persistence  # noqa: E402,F401
import iesplan.package.persistence  # noqa: E402,F401
import iesplan.project.persistence  # noqa: E402,F401
import iesplan.results.persistence  # noqa: E402,F401
import iesplan.storage.persistence  # noqa: E402,F401
from iesplan.db import Base  # noqa: E402
from iesplan.tasks import persistence as slots  # noqa: E402
from iesplan.tasks.persistence import ComputeSlot  # noqa: E402


@pytest.fixture(scope="module")
def pg_engine() -> Iterator[Engine]:
    """模块级 PG 引擎(建表一次; 本文件只写 compute_slots, 结束清槽行。

    不用 drop_all：model_template* 存在既有循环外键，DROP 排序无解
    （与槽逻辑无关，生产从不删表）。
    """
    eng = create_engine(str(PG_URL), pool_size=4, max_overflow=0)
    _assert_isolated(eng)
    Base.metadata.create_all(eng)
    with Session(eng) as db:
        _delete_own_rows(db)  # 崩溃残留的自有池行先清，保证重跑确定性
    yield eng
    with Session(eng) as db:
        _delete_own_rows(db)
    eng.dispose()


@pytest.fixture()
def pg_db(pg_engine: Engine) -> Iterator[Session]:
    """函数级 PG 会话; 结束只清专属池槽行(跨测试无串扰，不碰外部数据)。"""
    with Session(pg_engine) as session:
        yield session
        _delete_own_rows(session)


def _stats(db: Session, pool: str) -> tuple[int, int]:
    rows = db.execute(select(ComputeSlot).where(ComputeSlot.pool_name == pool)).scalars().all()
    return len(rows), sum(r.in_use for r in rows)


def _run_pair(eng: Engine, work) -> list:
    """双事务 barrier 同步执行 work(db, idx) 并 commit；线程异常回抛主线程(防假绿)。"""
    barrier = threading.Barrier(2, timeout=60)
    outcomes: list = [None, None]
    errors: list = [None, None]

    def worker(idx: int) -> None:
        try:
            with Session(eng) as db:
                barrier.wait()
                outcomes[idx] = work(db, idx)
                db.commit()
        except BaseException as exc:  # 必须回传，否则双异常会伪装成 [None, None]
            errors[idx] = exc
            barrier.abort()

    threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert not any(t.is_alive() for t in threads)
    raised = [e for e in errors if e is not None]
    raised.sort(key=lambda e: isinstance(e, threading.BrokenBarrierError))
    if raised:
        raise raised[0]
    return outcomes


def _race_ensure_acquire(
    eng: Engine, pool: str, capacity: int, pre_ensure: int | None
) -> tuple[int, int, list]:
    """双事务 barrier 同步做 ensure + acquire + commit; 返回(行数, 总量, 领取结果)。"""
    if pre_ensure:
        with Session(eng) as db:
            slots.ensure_slots(db, {pool: pre_ensure})
            db.commit()

    def work(db: Session, idx: int):
        slots.ensure_slots(db, {pool: capacity})
        got = slots.acquire_slot(db, pool, capacity)
        return got.id if got is not None else None

    outcomes = _run_pair(eng, work)
    with Session(eng) as db:
        n, total = _stats(db, pool)
    return n, total, outcomes


class TestSlotPgConcurrency:
    @pytest.mark.parametrize("round_no", range(3))
    def test_empty_pool_single_row_single_acquire(
        self, pg_engine: Engine, pg_db: Session, round_no: int
    ) -> None:
        """空池 capacity=1 双事务：只建出一行、总量 1，一方领到、另一方干净得 None。"""
        n, total, got = _race_ensure_acquire(pg_engine, "pg_race_a", 1, None)
        assert (n, total) == (1, 1)
        assert sorted(g is not None for g in got) == [False, True]

    @pytest.mark.parametrize("round_no", range(3))
    def test_grow_no_duplicate_rows(self, pg_engine: Engine, pg_db: Session, round_no: int) -> None:
        """1 行已提交后双事务扩到 3：恰好 3 行，双方领到不同行，总量不超限。"""
        n, total, got = _race_ensure_acquire(pg_engine, "pg_race_b", 3, 1)
        assert n == 3
        assert total == 2
        assert got[0] is not None and got[1] is not None and got[0] != got[1]

    def test_shrunk_pool_concurrent_acquire_refused(self, pg_engine: Engine, pg_db: Session) -> None:
        """对照：3 行全占用已提交，capacity=1 双事务同领 → 全拒，总量不动。"""
        slots.ensure_slots(pg_db, {"pg_race_c": 3})
        assert slots.acquire_slot(pg_db, "pg_race_c", 3) is not None
        assert slots.acquire_slot(pg_db, "pg_race_c", 3) is not None
        assert slots.acquire_slot(pg_db, "pg_race_c", 3) is not None
        pg_db.commit()

        def claim(db: Session, idx: int):
            slot = slots.acquire_slot(db, "pg_race_c", 1)
            return slot.id if slot is not None else None

        assert _run_pair(pg_engine, claim) == [None, None]
        assert _stats(pg_db, "pg_race_c") == (3, 3)


def test_pg_engine_configured() -> None:
    """本文件被选中运行时，URL 必须存在（防误配成默认库）。"""
    assert PG_URL and PG_URL.startswith("postgresql")
