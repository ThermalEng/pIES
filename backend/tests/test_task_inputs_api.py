"""任务冻结输入查询 API 测试。

覆盖 GET /api/projects/{id}/tasks/{task_id}/inputs（只读投影，不重验、不回退）：
1. 已提交计算任务返回快照冻结的评价确认块/计算配置/数据集版本与版本钉选引用；
2. 跨项目任务 id 返回 404（归属检查，不泄露他项目输入）；
3. 未知任务 id 返回 404；
4. 无快照任务返回 snapshot=null（非计算任务，无冻结输入可读）。

- 数据库: SQLite :memory: + 内存队列（沿 test_tasks_api 约定）；
- 项目准备复用 test_tasks_api 的公开装配 helper（真实提交闸门冻结的快照）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")

import pytest  # noqa: E402
from auth_helpers import make_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from test_tasks_api import (  # noqa: E402
    _h,
    _prepare_project,
    _save_config,
    _submit_task,
)

from iesplan.api import projects as projects_api  # noqa: E402
from iesplan.api import tasks as tasks_api  # noqa: E402
from iesplan.api.config import config_router  # noqa: E402
from iesplan.api.deps import get_request_db  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.tasks import queue  # noqa: E402
from iesplan.tasks.persistence import Task  # noqa: E402


@pytest.fixture(scope="module")
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
def _clean_state(engine: Engine) -> Iterator[None]:
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
    from iesplan.config import settings  # noqa: E402

    settings.data_dir = tmp_path
    app = create_app()
    app.include_router(projects_api.router)
    app.include_router(tasks_api.router)
    app.include_router(config_router)

    def _override_request_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_request_db] = _override_request_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _submit(client: TestClient, db: Session, tag: str) -> tuple[int, int, Any]:
    owner = make_user(db, f"owner_inputs_{tag}")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)
    status, body = _submit_task(
        client, pid, owner, config={"config_revision": rev}, idempotency_key=f"inputs-{tag}"
    )
    assert status == 201, body
    return pid, body["task"]["id"], owner


def test_inputs_returns_frozen_snapshot(client: TestClient, db: Session) -> None:
    """冻结输入返回提交时冻结的评价/配置/版本引用（非当前草稿）。"""
    pid, task_id, owner = _submit(client, db, "a")
    resp = client.get(f"/api/projects/{pid}/tasks/{task_id}/inputs", headers=_h(client, owner))
    assert resp.status_code == 200, resp.text
    payload = resp.json()["inputs"]
    assert payload["task"]["id"] == task_id
    assert payload["task"]["type"] == "optimization"
    assert payload["snapshot"] is not None
    snap = payload["snapshot"]
    assert snap["dataset_version_ids"] == []
    assert snap["calc_config_snapshot"]["config_ref"]["revision"] >= 1
    frozen_eval = snap["evaluation"]
    # 默认评价输入（_submit_task 缺省 K=0/E=1/N=1）：冻结值与提交一致，
    # 确认人/确认时间由服务端写入快照顶层。
    assert frozen_eval["evaluation_years"] == 1
    assert frozen_eval["construction_period"] == 0
    assert frozen_eval["operation_years"] == 1
    assert frozen_eval["discount_rate"] == 0.08
    # 付款比例冻结为规范定点字符串（提交边界只收 JSON number，见 contracts；
    # 预填时按同一数值转回 number，金额 amount 本就是字符串，直通）。
    assert frozen_eval["construction_payments"] == [{"t": 0, "ratio": "1"}]
    assert frozen_eval["maintenance_expenses"] == []
    assert frozen_eval["confirmed_by"] == owner.id
    assert isinstance(frozen_eval["confirmed_at"], str) and frozen_eval["confirmed_at"]
    assert isinstance(snap["device_models"], dict)
    refs = payload["frozen_refs"]
    assert isinstance(refs["planning_revision"], int)
    assert isinstance(refs["finance_profile_id"], str) and refs["finance_profile_id"]
    assert isinstance(refs["finance_revision"], int)


def test_inputs_cross_project_404(client: TestClient, db: Session) -> None:
    """跨项目读他任务冻结输入 → 404，不泄露。"""
    pid_a, task_id, owner_a = _submit(client, db, "b")
    owner_b = make_user(db, "owner_inputs_b2")
    pid_b = _prepare_project(client, db, owner_b, name="输入隔离项目B")
    resp = client.get(f"/api/projects/{pid_b}/tasks/{task_id}/inputs", headers=_h(client, owner_b))
    assert resp.status_code == 404, resp.text
    assert pid_a != pid_b


def test_inputs_unknown_task_404(client: TestClient, db: Session) -> None:
    """未知任务 id → 404。"""
    owner = make_user(db, "owner_inputs_c")
    pid = _prepare_project(client, db, owner)
    resp = client.get(f"/api/projects/{pid}/tasks/999999/inputs", headers=_h(client, owner))
    assert resp.status_code == 404, resp.text


def test_inputs_task_without_snapshot(client: TestClient, db: Session) -> None:
    """无快照任务（非计算任务）返回 snapshot=null，不伪造冻结输入。"""
    owner = make_user(db, "owner_inputs_d")
    pid = _prepare_project(client, db, owner)
    row = Task(project_id=pid, type="export", status="completed", requested_by=owner.id)
    db.add(row)
    db.commit()
    resp = client.get(f"/api/projects/{pid}/tasks/{row.id}/inputs", headers=_h(client, owner))
    assert resp.status_code == 200, resp.text
    payload = resp.json()["inputs"]
    assert payload["task"]["id"] == row.id
    assert payload["snapshot"] is None
