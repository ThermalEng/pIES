"""任务冻结输入载入工作草稿 API 测试。

覆盖 POST /api/projects/{id}/tasks/{task_id}/load-draft（单事务工作区更新）：
1. 载入后草稿 revision 恰好 +1，内容等于来源版本内容去命令簿记；
2. 计算配置追加新 revision，正文为冻结正文去任务级附加键；
3. 指针恢复后再次提交的新任务，其冻结引用与原任务一致；
4. 载入本身不创建任务与项目版本，快照行不变；
5. 跨项目/未知任务 404 且不改草稿；无快照任务 400 且不改草稿。

- 数据库: SQLite :memory: + 内存队列（沿 test_tasks_api 约定）；
- 项目准备复用 test_tasks_api 的公开装配 helper（真实提交闸门冻结的快照）。
"""

from __future__ import annotations

import os
import time
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
    _claim,
    _complete_task,
    _h,
    _prepare_project,
    _save_config,
    _submit_task,
)

from iesplan.api import projects as projects_api  # noqa: E402
from iesplan.api import tasks as tasks_api  # noqa: E402
from iesplan.api.config import config_router  # noqa: E402
from iesplan.api.deps import get_request_db  # noqa: E402
from iesplan.application.projects.content_objects import load_content_object  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.results import create_evidence  # noqa: E402
from iesplan.storage import put_object  # noqa: E402
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


def _setup(client: TestClient, db: Session, tag: str) -> tuple[int, int, Any]:
    owner = make_user(db, f"owner_loaddraft_{tag}")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)
    status, body = _submit_task(
        client, pid, owner, config={"config_revision": rev}, idempotency_key=f"loaddraft-{tag}"
    )
    assert status == 201, body
    return pid, body["task"]["id"], owner


def _draft_revision(client: TestClient, pid: int, owner: Any) -> int | None:
    """当前草稿 revision；尚无草稿返回 None（与 GET /projects/{id} 缺省语义一致）。"""
    resp = client.get(f"/api/projects/{pid}", headers=_h(client, owner))
    assert resp.status_code == 200, resp.text
    draft = resp.json().get("draft")
    return int(draft["revision"]) if isinstance(draft, dict) else None


def _versions(client: TestClient, pid: int, owner: Any) -> list[dict[str, Any]]:
    resp = client.get(f"/api/projects/{pid}/versions", headers=_h(client, owner))
    assert resp.status_code == 200, resp.text
    return list(resp.json()["versions"])


def _draft_content(client: TestClient, pid: int, owner: Any) -> dict[str, Any]:
    resp = client.get(f"/api/projects/{pid}", headers=_h(client, owner))
    assert resp.status_code == 200, resp.text
    return dict(resp.json()["draft"]["content"])


def _put_draft(
    client: TestClient, pid: int, owner: Any, commands: list[dict[str, Any]]
) -> tuple[int, dict[str, Any]]:
    """PUT 草稿语义命令（调用方自带幂等命令 id；返回状态码与响应体）。"""
    view = client.get(f"/api/projects/{pid}", headers=_h(client, owner))
    assert view.status_code == 200, view.text
    put = client.put(
        f"/api/projects/{pid}/draft",
        headers=_h(client, owner),
        json={
            "expected_revision": int(view.json()["draft"]["revision"]),
            "commands": commands,
        },
    )
    return put.status_code, put.json()


def test_load_draft_restores_frozen_inputs(client: TestClient, db: Session) -> None:
    """载入：草稿+1 且内容等于来源版本内容；配置追加去附加键正文；无任务/版本新增。”"""
    pid, task_id, owner = _setup(client, db, "a")
    versions_before = _versions(client, pid, owner)
    tasks_before = db.query(Task).filter_by(project_id=pid).count()

    inputs_before = client.get(
        f"/api/projects/{pid}/tasks/{task_id}/inputs", headers=_h(client, owner)
    ).json()["inputs"]
    snap = inputs_before["snapshot"]
    assert snap is not None

    resp = client.post(
        f"/api/projects/{pid}/tasks/{task_id}/load-draft", headers=_h(client, owner)
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    first_rev = int(payload["draft"]["revision"])
    # 单次载入恰好追加一个草稿 revision（再载一次即 +1，无重复写）。
    assert first_rev >= 1
    assert payload["config_revision"] is not None
    again = client.post(
        f"/api/projects/{pid}/tasks/{task_id}/load-draft", headers=_h(client, owner)
    )
    assert again.status_code == 200, again.text
    assert int(again.json()["draft"]["revision"]) == first_rev + 1

    # 草稿内容 = 来源版本内容去命令簿记（与 restore 同派生）+ 本次所选任务的
    # 提交工作输入（整体替换；缺席记空文本，不混入他任务字段）。
    source_version = next(v for v in versions_before if v["id"] == snap["project_version_id"])
    source_content = load_content_object(db, int(source_version["content_object_id"]))
    expected = {k: v for k, v in source_content.items() if k != "applied_commands"}
    view = client.get(f"/api/projects/{pid}", headers=_h(client, owner)).json()
    assert view["draft"]["revision"] == first_rev + 1
    working = view["draft"]["content"].pop("task_working_inputs")
    assert view["draft"]["content"] == expected
    frozen_eval = snap["evaluation"]
    # 工作输入是冻结业务值原样拷贝（snake_case 业务键），非显示文本。
    assert working["task_type"] == "optimization"
    for key in (
        "construction_period",
        "operation_years",
        "discount_rate",
        "construction_payments",
        "maintenance_expenses",
    ):
        assert working[key] == frozen_eval[key]
    assert "task_params" not in working

    # 配置正文 = 冻结正文去 task_params/config_ref（两次载入追加了两次）。
    cfg = client.get(f"/api/projects/{pid}/config", headers=_h(client, owner)).json()
    assert int(cfg["revision"]) == int(again.json()["config_revision"])
    assert "task_params" not in cfg["calculation_config"]
    assert "config_ref" not in cfg["calculation_config"]
    for key, value in snap["calc_config_snapshot"].items():
        if key in ("task_params", "config_ref"):
            continue
        assert cfg["calculation_config"][key] == value

    # 不创建任务与项目版本；快照行不变。
    assert db.query(Task).filter_by(project_id=pid).count() == tasks_before
    assert len(_versions(client, pid, owner)) == len(versions_before)
    inputs_after = client.get(
        f"/api/projects/{pid}/tasks/{task_id}/inputs", headers=_h(client, owner)
    ).json()["inputs"]
    assert inputs_after["snapshot"]["id"] == snap["id"]


def test_load_draft_roundtrip_through_submit(client: TestClient, db: Session) -> None:
    """载入后走现有提交流的新任务：冻结引用与原任务一致（指针恢复有效）。"""
    pid, task_id, owner = _setup(client, db, "b")
    refs_before = client.get(
        f"/api/projects/{pid}/tasks/{task_id}/inputs", headers=_h(client, owner)
    ).json()["inputs"]["frozen_refs"]

    loaded = client.post(
        f"/api/projects/{pid}/tasks/{task_id}/load-draft", headers=_h(client, owner)
    )
    assert loaded.status_code == 200, loaded.text
    versions_mid = len(_versions(client, pid, owner))

    status, body = _submit_task(
        client,
        pid,
        owner,
        config={"config_revision": loaded.json()["config_revision"]},
        idempotency_key="loaddraft-b2",
    )
    assert status == 201, body
    refs_after = client.get(
        f"/api/projects/{pid}/tasks/{body['task']['id']}/inputs", headers=_h(client, owner)
    ).json()["inputs"]["frozen_refs"]
    assert refs_after == refs_before
    # 载入本身未新增版本（提交是否固化新版本由现有提交流按草稿变化决定）。
    assert len(_versions(client, pid, owner)) >= versions_mid


def test_load_draft_cross_project_404(client: TestClient, db: Session) -> None:
    """跨项目载入 → 404，草稿不动。"""
    pid_a, task_id, owner_a = _setup(client, db, "c")
    rev_before = _draft_revision(client, pid_a, owner_a)
    owner_b = make_user(db, "owner_loaddraft_c2")
    pid_b = _prepare_project(client, db, owner_b, name="载入隔离项目B")
    resp = client.post(
        f"/api/projects/{pid_b}/tasks/{task_id}/load-draft", headers=_h(client, owner_b)
    )
    assert resp.status_code == 404, resp.text
    assert pid_a != pid_b
    assert _draft_revision(client, pid_a, owner_a) == rev_before


def test_load_draft_unknown_task_404(client: TestClient, db: Session) -> None:
    """未知任务 id → 404，草稿不动。"""
    owner = make_user(db, "owner_loaddraft_d")
    pid = _prepare_project(client, db, owner)
    rev_before = _draft_revision(client, pid, owner)
    resp = client.post(
        f"/api/projects/{pid}/tasks/999999/load-draft", headers=_h(client, owner)
    )
    assert resp.status_code == 404, resp.text
    assert _draft_revision(client, pid, owner) == rev_before


def test_load_draft_task_without_snapshot_400(client: TestClient, db: Session) -> None:
    """无快照任务（非计算任务）→ 400 可见失败，不改草稿、不建任务/版本。”"""
    owner = make_user(db, "owner_loaddraft_e")
    pid = _prepare_project(client, db, owner)
    rev_before = _draft_revision(client, pid, owner)
    versions_before = len(_versions(client, pid, owner))
    row = Task(project_id=pid, type="export", status="completed", requested_by=owner.id)
    db.add(row)
    db.commit()
    resp = client.post(
        f"/api/projects/{pid}/tasks/{row.id}/load-draft", headers=_h(client, owner)
    )
    assert resp.status_code == 400, resp.text
    assert _draft_revision(client, pid, owner) == rev_before
    assert len(_versions(client, pid, owner)) == versions_before
    assert db.query(Task).filter_by(project_id=pid).count() == 1


def test_load_draft_writes_working_evaluation_and_params(client: TestClient, db: Session) -> None:
    """载入写工作输入：评价确认块可编辑态 + 任务级参数整体替换，去确认人/时间。"""
    owner = make_user(db, "owner_loaddraft_f")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)
    evaluation = {
        "evaluation_years": 3,
        "construction_period": 1,
        "operation_years": 2,
        "discount_rate": 0.06,
        "construction_payments": [{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.4}],
        "maintenance_expenses": [{"year": 2, "amount": "10.50", "currency": "CNY"}],
    }
    status, body = _submit_task(
        client,
        pid,
        owner,
        config={"config_revision": rev, "priority": 3},
        evaluation=evaluation,
        idempotency_key="loaddraft-f1",
    )
    assert status == 201, body
    task_id = int(body["task"]["id"])
    snap = client.get(
        f"/api/projects/{pid}/tasks/{task_id}/inputs", headers=_h(client, owner)
    ).json()["inputs"]["snapshot"]
    assert snap is not None

    loaded = client.post(
        f"/api/projects/{pid}/tasks/{task_id}/load-draft", headers=_h(client, owner)
    )
    assert loaded.status_code == 200, loaded.text
    working = _draft_content(client, pid, owner)["task_working_inputs"]
    # 工作输入与冻结快照逐值一致（业务值原样拷贝；真实值不断言快照 id）。
    frozen = snap["evaluation"]
    assert working["task_type"] == "optimization"
    for key in (
        "construction_period",
        "operation_years",
        "discount_rate",
        "construction_payments",
        "maintenance_expenses",
    ):
        assert working[key] == frozen[key]
    # 付款比例/维护金额保持冻结形态（定点字符串），不转显示用 number。
    assert working["construction_payments"] == [
        {"t": 0, "ratio": "0.6"},
        {"t": 1, "ratio": "0.4"},
    ]
    assert working["maintenance_expenses"] == [{"year": 2, "amount": "10.5", "currency": "CNY"}]
    assert working["task_params"] == {"priority": 3}
    # 服务端确认人/时间不进入工作副本（重提时由服务端重写）。
    assert "confirmed_by" not in working
    assert "confirmed_at" not in working
    # 冻结快照本身未被载入改动（确认块仍带确认人/时间）。
    assert snap["evaluation"]["confirmed_by"] == owner.id
    assert isinstance(snap["evaluation"]["confirmed_at"], str)


def test_task_set_working_inputs_command(client: TestClient, db: Session) -> None:
    """task.set_working_inputs：原样暂存（含未完成编辑态）；形态非法 400；版本冻结剥离。"""
    owner = make_user(db, "owner_loaddraft_g")
    pid = _prepare_project(client, db, owner)
    rev_before = _draft_revision(client, pid, owner)

    working = {
        "task_type": "calc",
        "construction_period": "",
        "operation_years": "2",
        "discount_rate": "not-a-number",
        "construction_payments": "[{bad json",
        "maintenance_expenses": "[]",
    }
    status, body = _put_draft(
        client,
        pid,
        owner,
        [
            {
                "id": "e2e-working-1",
                "project_id": pid,
                "unit": "task",
                "type": "task.set_working_inputs",
                "payload": {"working": working},
            }
        ],
    )
    assert status == 200, body
    assert _draft_revision(client, pid, owner) == (rev_before or 0) + 1
    assert _draft_content(client, pid, owner)["task_working_inputs"] == working

    bad_status, bad_body = _put_draft(
        client,
        pid,
        owner,
        [
            {
                "id": "e2e-working-2",
                "project_id": pid,
                "unit": "task",
                "type": "task.set_working_inputs",
                "payload": {"working": [1, 2]},
            }
        ],
    )
    assert bad_status == 400, bad_body
    assert _draft_content(client, pid, owner)["task_working_inputs"] == working

    # 从含工作输入的草稿创建版本：版本内容不带工作暂存（草稿侧副本不冻结）。
    created = client.post(
        f"/api/projects/{pid}/versions",
        headers=_h(client, owner),
        json={"name": "含工作输入的版本", "description": None},
    )
    assert created.status_code == 201, created.text
    version = created.json()["version"]
    content = load_content_object(db, int(version["content_object_id"]))
    assert "task_working_inputs" not in content


def test_task_list_completion_fields(client: TestClient, db: Session) -> None:
    """列表完成时刻/最新包读模型：12 个已完成 + 1 个排队中；最早创建者最晚完成。"""
    owner = make_user(db, "owner_loaddraft_h")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)
    task_ids: list[int] = []
    for i in range(12):
        status, body = _submit_task(
            client,
            pid,
            owner,
            config={"config_revision": rev},
            idempotency_key=f"loaddraft-h{i}",
        )
        assert status == 201, body
        task_ids.append(int(body["task"]["id"]))
    first, rest = task_ids[0], task_ids[1:]
    for tid in rest:
        assert _claim(db, tid) is not None
        _complete_task(db, tid, outcome="normal_completion")
    db.commit()
    time.sleep(1.5)
    assert _claim(db, first) is not None
    _complete_task(db, first, outcome="normal_completion")
    db.commit()

    # 证据包：最早创建者 complete，第 12 个 partial，其余无证据。
    snap_first = (
        client.get(f"/api/projects/{pid}/tasks/{first}/inputs", headers=_h(client, owner))
        .json()["inputs"]["snapshot"]
    )
    obj1 = put_object(db, b'{"k":1}', "application/json", "eval_results", purpose="test")
    pkg1 = create_evidence(
        db,
        task_id=first,
        calc_snapshot_id=int(snap_first["id"]),
        object_id=int(obj1.id),
        status="complete",
        created_by=owner.id,
    )
    last = task_ids[-1]
    snap_last = (
        client.get(f"/api/projects/{pid}/tasks/{last}/inputs", headers=_h(client, owner))
        .json()["inputs"]["snapshot"]
    )
    obj2 = put_object(db, b'{"k":2}', "application/json", "eval_results", purpose="test")
    pkg2 = create_evidence(
        db,
        task_id=last,
        calc_snapshot_id=int(snap_last["id"]),
        object_id=int(obj2.id),
        status="partial",
        created_by=owner.id,
    )
    db.commit()

    status, body = _submit_task(
        client, pid, owner, config={"config_revision": rev}, idempotency_key="loaddraft-hq"
    )
    assert status == 201, body
    queued_id = int(body["task"]["id"])

    # 分页走完（limit=5 强制多页）：全部条目携带完成时刻/最新包字段。
    seen: dict[int, dict[str, Any]] = {}
    cursor: Any = None
    for _ in range(10):
        params: dict[str, Any] = {"limit": 5}
        if cursor is not None:
            params["cursor"] = cursor
        resp = client.get("/api/projects/%d/tasks" % pid, headers=_h(client, owner), params=params)
        assert resp.status_code == 200, resp.text
        page = resp.json()
        for item in page["items"]:
            seen[int(item["id"])] = item
        cursor = page.get("next_cursor")
        if cursor is None:
            break
    assert len(seen) == 13
    by_id = seen
    # 最早创建者完成最晚（完成时刻最大），尽管 id 最小。
    first_time = by_id[first]["completed_at"]
    assert isinstance(first_time, str) and first_time != ""
    for tid in rest:
        assert by_id[tid]["completed_at"] is not None
        assert str(by_id[tid]["completed_at"]) < first_time
    assert by_id[first]["latest_package_id"] == int(pkg1.id)
    assert by_id[first]["latest_evidence_status"] == "complete"
    assert by_id[last]["latest_package_id"] == int(pkg2.id)
    assert by_id[last]["latest_evidence_status"] == "partial"
    assert by_id[rest[0]]["latest_package_id"] is None
    assert by_id[rest[0]]["latest_evidence_status"] is None
    # 排队中任务三项恒为 null。
    assert by_id[queued_id]["completed_at"] is None
    assert by_id[queued_id]["latest_package_id"] is None
    assert by_id[queued_id]["latest_evidence_status"] is None
