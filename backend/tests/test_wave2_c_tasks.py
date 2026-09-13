"""Wave 2 W2-C: application/tasks 用例测试(提交/取消/重试/租约)。

覆盖要求:
- 用例可独立执行并提交事务(新会话可见, 无需调用方 commit);
- 行为由现行用例拥有(关键路径对照: 状态/标记/快照/错误码逐项对比)。

环境与既有任务测试一致: SQLite :memory:(StaticPool) + 内存队列。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

# 单文件运行时的安全网: 固定 SQLite + 内存队列, 避免误连部署 Postgres/Redis
os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")

import pytest  # noqa: E402
from auth_helpers import login_headers, make_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan import identity as identity_domain  # noqa: E402
from iesplan import tasks as tasks_domain  # noqa: E402
from iesplan.api import projects as projects_api  # noqa: E402
from iesplan.application import tasks as tasks_uc  # noqa: E402
from iesplan.application import worker as worker_app  # noqa: E402
from iesplan.config import settings  # noqa: E402
from iesplan.db import Base, get_db  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.tasks import queue  # noqa: E402

# ---------------------------------------------------------------------------
# 测试环境
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _clean_state(engine: Engine, db: Session) -> Iterator[None]:
    queue.force_memory()
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def db(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


@pytest.fixture()
def client(engine: Engine, db: Session, tmp_path: Path) -> Iterator[TestClient]:
    settings.data_dir = tmp_path
    app = create_app()
    app.include_router(projects_api.router)

    def _override_get_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _h(client: TestClient, user) -> dict[str, str]:
    return login_headers(client, user)


def _user(db: Session, name: str):
    """真实 UserRecord(供用例直接调用), 区别于 auth_helpers 的快照命名空间。"""
    ns = make_user(db, name)
    db.commit()
    rec = identity_domain.get_user(db, ns.id)
    assert rec is not None
    return rec


def _project(client: TestClient, user, name: str) -> int:
    resp = client.post(
        "/api/projects",
        json={
            "name": name,
            "currency": "CNY",
            "baseline_resolution": "1h",
            "baseline_leap_year": False,
            "baseline_scenario_mode": "single",
        },
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["project"]["id"]


def _fresh_db(engine: Engine) -> Session:
    """新会话(验证顶层用例已提交, 无需调用方 commit)。"""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


# ---------------------------------------------------------------------------
# 提交: 独立执行 + 事务提交 + 重复提交一致
# ---------------------------------------------------------------------------


def test_submit_report_task_commits_and_repeatable(
    client: TestClient, db: Session, engine: Engine
) -> None:
    """io 任务提交: 用例独立提交(新会话可见), 重复提交关键字段一致。"""
    owner = _user(db, "w2c_owner_report")
    pid = _project(client, owner, "w2c-proj-report")

    task, flags = tasks_uc.submit_task(db, owner, pid, "report", idempotency_key="w2c-rpt-1")
    assert flags == {"replay": False, "duplicate": False}
    assert task.status == "queued" and task.calc_snapshot_id is None

    # 事务提交证明: 新会话可见, 无需调用方 commit
    with _fresh_db(engine) as fresh:
        got = tasks_domain.get_task(fresh, task.id)
        assert got is not None and got.status == "queued"

    # 行为对照: 同类型任务关键字段一致
    old, old_flags = tasks_uc.submit_task(db, owner, pid, "report", idempotency_key="w2c-rpt-2")
    assert old_flags == {"replay": False, "duplicate": False}
    assert (old.type, old.status, old.calc_snapshot_id) == (task.type, task.status, task.calc_snapshot_id)


def test_submit_compute_task_snapshot_replay_and_duplicate(
    client: TestClient, db: Session
) -> None:
    """计算任务提交: 快照装配 + 幂等重放 + 快照去重。"""
    owner = _user(db, "w2c_owner_opt")
    pid = _project(client, owner, "w2c-proj-opt")

    task, flags = tasks_uc.submit_task(db, owner, pid, "optimization", idempotency_key="w2c-opt-1")
    assert flags == {"replay": False, "duplicate": False}
    assert task.calc_snapshot_id is not None

    replay, rflags = tasks_uc.submit_task(db, owner, pid, "optimization", idempotency_key="w2c-opt-1")
    assert rflags == {"replay": True, "duplicate": False}
    assert replay.id == task.id

    # 无幂等键重复提交同快照 → 去重对照
    dup_new, dflags = tasks_uc.submit_task(db, owner, pid, "optimization")
    assert dflags == {"replay": False, "duplicate": True}
    assert dup_new.id == task.id
    dup_old, dflags_old = tasks_uc.submit_task(db, owner, pid, "optimization")
    assert dflags_old == {"replay": False, "duplicate": True}
    assert dup_old.id == task.id


def test_submit_errors_match_old_codes(client: TestClient, db: Session) -> None:
    """非法类型/非法幂等键: 新旧错误码一致。"""
    owner = _user(db, "w2c_owner_err")
    pid = _project(client, owner, "w2c-proj-err")

    with pytest.raises(Exception) as exc:
        tasks_uc.submit_task(db, owner, pid, "nope")
    assert exc.value.code == "TASK-REQ-002"
    db.rollback()

    with pytest.raises(Exception) as exc:
        tasks_uc.submit_task(db, owner, pid, "report", idempotency_key="bad key!")
    assert exc.value.code == "TASK-REQ-003"
    db.rollback()

    # analysis 缺 sweeps
    with pytest.raises(Exception) as exc_new:
        tasks_uc.submit_task(db, owner, pid, "analysis", config={})
    db.rollback()
    assert exc_new.value.code == "TASK-REQ-001"


# ---------------------------------------------------------------------------
# 取消/确认取消: 状态机 + 事务提交 + 重复调用一致
# ---------------------------------------------------------------------------


def test_cancel_queued_and_running_flows(client: TestClient, db: Session, engine: Engine) -> None:
    """取消: queued 直接取消; running 经 cancelling; 确认后 cancelled(均已提交)。"""
    owner = _user(db, "w2c_owner_cancel")
    pid = _project(client, owner, "w2c-proj-cancel")

    t1, _ = tasks_uc.submit_task(db, owner, pid, "report", idempotency_key="w2c-c1")
    cancelled = tasks_uc.cancel_task(db, t1.id)
    assert cancelled.status == "cancelled"
    with _fresh_db(engine) as fresh:
        assert tasks_domain.get_task(fresh, t1.id).status == "cancelled"

    t2, _ = tasks_uc.submit_task(db, owner, pid, "report", idempotency_key="w2c-c2")
    claim = tasks_uc.claim_task(db, t2.id, "w2c-worker-1")
    assert claim is not None
    cancelling = tasks_uc.cancel_task(db, t2.id)
    assert cancelling.status == "cancelling"
    done = tasks_uc.acknowledge_cancel(db, t2.id)
    assert done.status == "cancelled"
    with _fresh_db(engine) as fresh:
        assert tasks_domain.get_task(fresh, t2.id).status == "cancelled"

    # 行为对照: 同流程落点一致
    t3, _ = tasks_uc.submit_task(db, owner, pid, "report", idempotency_key="w2c-c3")
    assert tasks_uc.cancel_task(db, t3.id).status == "cancelled"
    t4, _ = tasks_uc.submit_task(db, owner, pid, "report", idempotency_key="w2c-c4")
    assert tasks_uc.cancel_task(db, t4.id).status == "cancelled"


def test_cancel_terminal_denied_consistent(client: TestClient, db: Session) -> None:
    """终态任务不可取消: 重复取消同为 TASK-CANCEL-001。"""
    owner = _user(db, "w2c_owner_denied")
    pid = _project(client, owner, "w2c-proj-denied")

    t1, _ = tasks_uc.submit_task(db, owner, pid, "report", idempotency_key="w2c-d1")
    tasks_uc.cancel_task(db, t1.id)
    with pytest.raises(tasks_domain.CancelDeniedError) as e1:
        tasks_uc.cancel_task(db, t1.id)
    assert e1.value.code == "TASK-CANCEL-001"
    db.rollback()

    t2, _ = tasks_uc.submit_task(db, owner, pid, "report", idempotency_key="w2c-d2")
    tasks_uc.cancel_task(db, t2.id)
    with pytest.raises(tasks_domain.CancelDeniedError) as e2:
        tasks_uc.cancel_task(db, t2.id)
    assert e2.value.code == "TASK-CANCEL-001"
    db.rollback()


# ---------------------------------------------------------------------------
# 重试/租约: 重复调用落点一致
# ---------------------------------------------------------------------------


def _fail(db: Session, task_id: int) -> None:
    """新用例领取 → 经 worker 用例将任务置终态(failed), 供重试对照。"""
    claim = tasks_uc.claim_task(db, task_id, "w2c-fail-exec")
    assert claim is not None
    worker_app.fail_task(db, task_id, code="TASK-SOLVE-001", message="w2c fail")
    db.commit()


def test_retry_terminal_task_consistent(client: TestClient, db: Session, engine: Engine) -> None:
    """重试: 终态 → queued(快照不变, 已提交); 重复落点一致; 非终态拒绝同码。"""
    owner = _user(db, "w2c_owner_retry")
    pid = _project(client, owner, "w2c-proj-retry")

    t1, _ = tasks_uc.submit_task(db, owner, pid, "optimization", idempotency_key="w2c-t1")
    snap = t1.calc_snapshot_id
    _fail(db, t1.id)
    retried = tasks_uc.retry_task(db, owner, t1.id)
    assert retried.status == "queued" and retried.calc_snapshot_id == snap
    with _fresh_db(engine) as fresh:
        assert tasks_domain.get_task(fresh, t1.id).status == "queued"

    t2, _ = tasks_uc.submit_task(db, owner, pid, "optimization", idempotency_key="w2c-t2")
    _fail(db, t2.id)
    old_retried = tasks_uc.retry_task(db, owner, t2.id)
    assert (old_retried.status, old_retried.calc_snapshot_id) == ("queued", t2.calc_snapshot_id)

    t3, _ = tasks_uc.submit_task(db, owner, pid, "report", idempotency_key="w2c-t3")
    with pytest.raises(tasks_domain.TaskStateError) as e1:
        tasks_uc.retry_task(db, owner, t3.id)
    assert e1.value.code == "TASK-STATE-001"
    db.rollback()
    with pytest.raises(tasks_domain.TaskStateError) as e2:
        tasks_uc.retry_task(db, owner, t3.id)
    assert e2.value.code == "TASK-STATE-001"
    db.rollback()


def test_claim_task_lease_shape(client: TestClient, db: Session, engine: Engine) -> None:
    """租约: 领取建尝试+租约+running(已提交); 重复领取 None。"""
    owner = _user(db, "w2c_owner_claim")
    pid = _project(client, owner, "w2c-proj-claim")

    t1, _ = tasks_uc.submit_task(db, owner, pid, "report", idempotency_key="w2c-k1")
    claim = tasks_uc.claim_task(db, t1.id, "w2c-worker-1")
    assert claim is not None
    assert claim.task_id == t1.id and claim.attempt_no == 1
    assert isinstance(claim.lease_token, UUID)
    assert tasks_uc.claim_task(db, t1.id, "w2c-worker-2") is None
    with _fresh_db(engine) as fresh:
        got = tasks_domain.get_task(fresh, t1.id)
        assert got.status == "running"
        lease = tasks_domain.get_active_lease_for_task(fresh, t1.id)
        assert lease is not None and lease.attempt_id == claim.attempt_id

    t2, _ = tasks_uc.submit_task(db, owner, pid, "report", idempotency_key="w2c-k2")
    old_claim = tasks_uc.claim_task(db, t2.id, "w2c-worker-1")
    assert old_claim is not None
    assert (old_claim.task_id, old_claim.attempt_no) == (t2.id, 1)
    assert isinstance(old_claim.lease_token, UUID)
    assert tasks_domain.get_task(db, t2.id).status == "running"


def test_estimate_storage_readonly_stable(client: TestClient, db: Session) -> None:
    """存储门禁估算: 重复估算数值一致(只读, 不写库)。"""
    owner = _user(db, "w2c_owner_est")
    pid = _project(client, owner, "w2c-proj-est")
    new = tasks_uc.estimate_storage(db, pid, "optimization", {})
    old = tasks_uc.estimate_storage(db, pid, "optimization", {})
    assert (new.need, new.avail, new.blocked) == (old.need, old.avail, old.blocked)
