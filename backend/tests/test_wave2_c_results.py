"""Wave 2 W2-C: application/results 用例测试(证据写入/评估写入/index 重建)。

覆盖要求:
- 用例可独立执行并提交事务(新会话可见, 无需调用方 commit);
- 行为与旧服务一致(关键路径对照: 状态/维度/index 落点逐项对比)。

环境: SQLite :memory:(StaticPool) + 内存队列 + 临时对象存储目录。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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
from iesplan import results as results_domain  # noqa: E402
from iesplan.api import projects as projects_api  # noqa: E402
from iesplan.api import tasks as tasks_api  # noqa: E402
from iesplan.application import results as results_uc  # noqa: E402
from iesplan.application import tasks as tasks_uc  # noqa: E402
from iesplan.config import settings  # noqa: E402
from iesplan.db import Base, get_db  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.services import queue  # noqa: E402
from iesplan.services import results as results_service  # noqa: E402
from iesplan.storage import put_object  # noqa: E402

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
    app.include_router(tasks_api.router)

    def _override_get_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _h(client: TestClient, user) -> dict[str, str]:
    return login_headers(client, user)


def _user(db: Session, name: str):
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
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def _canonical(doc: dict[str, Any]) -> str:
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _store_hourly(db: Session, rows: int = 100, fields: list[str] | None = None) -> int:
    fields = fields or ["p_grid_buy", "p_grid_sell", "soc"]
    data = {f: [float(i % 100) for i in range(rows)] for f in fields}
    doc = {
        "fields": fields,
        "data": data,
        "meta": {"resolution": "1h", "rows": rows,
                 "units": {f: "kW" for f in fields}},
    }
    obj = put_object(db, _canonical(doc).encode("utf-8"), "application/json", "eval_results")
    db.commit()
    return obj.id


def _base_content(snapshot_id: int, hourly_object_ids: list[int]) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "task_type": "optimization",
        "snapshot": {"calc_snapshot_id": snapshot_id},
        "algorithm": "milp",
        "seed": 42,
        "stop_condition": {"status": "OPTIMAL", "stop_reason": "求解完成",
                           "time_limit_s": 600, "mip_rel_gap": 0.001, "gap": 0.0},
        "solve": {"solver_status": "OPTIMAL", "objective": 1234567.89, "gap": 0.0,
                  "stop_reason": "求解完成", "feasible": True},
        "candidate_indices": [0, 1],
        "candidates": [
            {"index": 0, "capacities": {"ies.device.pv": 500.0}, "irr": 0.12, "npv": 123456.0},
            {"index": 1, "capacities": {"ies.device.pv": 400.0}, "irr": 0.09, "npv": 50000.0},
        ],
        "metrics": {"annual_buy_kwh": 1000000.0},
        "hourly_refs": [
            {"solution_id": 0, "object_id": hourly_object_ids[0], "rows": 100,
             "fields": ["p_grid_buy", "p_grid_sell", "soc"]},
            {"solution_id": 1, "object_id": hourly_object_ids[1], "rows": 100,
             "fields": ["p_grid_buy"]},
        ],
        "residuals": {"all_passed": True, "max_normalized": 1.2e-9, "items": [
            {"name": "电平衡", "normalized": 1.2e-9, "tol": 1e-6, "passed": True,
             "residual": 0.001, "scale": 1000.0, "tau": None},
        ]},
        "constraints": {"checked": True, "capacity_violations": [], "boundary_violations": []},
        "financial": {"investment": 800000.0, "irr": 0.12, "irr_status": "unique",
                      "npv": 123456.0, "cashflows": [-800000.0, 100000.0, 100000.0]},
        "reliability": {"executed": True, "mode": "fixed_plan", "total_samples": 100,
                        "valid_samples": 100, "invalid_samples": 0, "failure_reasons": [],
                        "scope": "年度典型年", "metrics": {}},
    }


def _build_payload(snapshot_id: int, hourly_object_ids: list[int], created_by: int) -> dict[str, Any]:
    content = _base_content(snapshot_id, hourly_object_ids)
    return {
        "snapshot_id": snapshot_id,
        "algorithm": content["algorithm"],
        "seed": content["seed"],
        "stop_condition": content["stop_condition"],
        "solve": content["solve"],
        "candidate_indices": content["candidate_indices"],
        "metrics": content["metrics"],
        "hourly_refs": content["hourly_refs"],
        "content": content,
        "created_by": created_by,
    }


def _prepare_running(
    client: TestClient, db: Session, user, name: str, key: str
) -> tuple[int, int, Any, int]:
    """项目 → 新用例提交计算任务 → 新用例领取。返回 (pid, task_id, claim, snapshot_id)。"""
    pid = _project(client, user, name)
    task, _ = tasks_uc.submit_task(db, user, pid, "optimization", idempotency_key=key)
    claim = tasks_uc.claim_task(db, task.id, "w2c-fake-exec")
    assert claim is not None
    assert task.calc_snapshot_id is not None
    return pid, task.id, claim, task.calc_snapshot_id


# ---------------------------------------------------------------------------
# 证据写入: 独立提交 + 与旧服务一致
# ---------------------------------------------------------------------------


def test_submit_evidence_commits_and_matches_old(
    client: TestClient, db: Session, engine: Engine
) -> None:
    owner = _user(db, "w2c_res_owner")
    pid, task_id, claim, snap = _prepare_running(client, db, owner, "w2c-proj-ev", "w2c-ev-1")
    obj_a = _store_hourly(db)
    obj_b = _store_hourly(db, fields=["p_grid_buy"])
    payload = _build_payload(snap, [obj_a, obj_b], owner.id)

    pkg = results_uc.submit_evidence(db, task_id, claim.attempt_id, claim.lease_token, payload)
    assert pkg.status == "complete"

    # 事务提交证明: 新会话可见
    with _fresh_db(engine) as fresh:
        got = results_domain.get_evidence(fresh, pkg.id)
        assert got is not None and got.status == "complete"

    # 行为对照: 旧服务同任务追加提交, 同为 complete 且行数 +1
    before = len(results_domain.list_evidence_for_tasks(db, [task_id]))
    old = results_service.submit_evidence(db, task_id, claim.attempt_id, claim.lease_token, payload)
    db.commit()
    assert old.status == pkg.status == "complete"
    after = len(results_domain.list_evidence_for_tasks(db, [task_id]))
    assert after == before + 1

    # fencing 对照: 错误 token 新旧同为 EVID-FENCE-001
    with pytest.raises(results_uc.EvidenceWriteDeniedError) as e1:
        results_uc.submit_evidence(db, task_id, claim.attempt_id, "00000000-0000-0000-0000-000000000000", payload)
    assert e1.value.code == "EVID-FENCE-001"
    db.rollback()
    with pytest.raises(results_service.EvidenceWriteDeniedError) as e2:
        results_service.submit_evidence(db, task_id, claim.attempt_id, "00000000-0000-0000-0000-000000000000", payload)
    assert e2.value.code == "EVID-FENCE-001"
    db.rollback()


# ---------------------------------------------------------------------------
# 评估写入: 独立提交 + 维度一致
# ---------------------------------------------------------------------------


def test_run_assessment_commits_and_matches_old(
    client: TestClient, db: Session, engine: Engine
) -> None:
    owner = _user(db, "w2c_res_owner2")
    pid, task_id, claim, snap = _prepare_running(client, db, owner, "w2c-proj-as", "w2c-as-1")
    payload = _build_payload(snap, [_store_hourly(db), _store_hourly(db)], owner.id)
    pkg = results_uc.submit_evidence(db, task_id, claim.attempt_id, claim.lease_token, payload)

    asm = results_uc.run_assessment(db, pkg.id, "full", user=owner)
    assert asm.detail["dimensions"]["physical"] == "passed"
    assert asm.detail["dimensions"]["optimality"] == "passed"
    assert asm.detail["dimensions"]["financial"] == "passed"
    assert asm.detail["dimensions"]["reliability"] == "ok"
    assert asm.overall_score == 100.0

    with _fresh_db(engine) as fresh:
        got = results_domain.get_assessment(fresh, asm.id)
        assert got is not None

    # 行为对照: 旧服务评估同证据包, 维度与得分一致, 且为追加新行
    old = results_service.run_assessment(db, pkg.id, "full", user=owner)
    db.commit()
    assert old.detail["dimensions"] == asm.detail["dimensions"]
    assert old.overall_score == asm.overall_score
    assert old.id != asm.id

    # 非法评估类型新旧同码
    with pytest.raises(results_uc.ResultInvalidRequestError) as e1:
        results_uc.run_assessment(db, pkg.id, "nope")
    assert e1.value.code == "RES-REQ-002"
    db.rollback()


# ---------------------------------------------------------------------------
# index 重建: 落点一致(同证据更新指针 / 新证据转交 latest)
# ---------------------------------------------------------------------------


def test_update_result_index_pointer_and_handover(
    client: TestClient, db: Session, engine: Engine
) -> None:
    owner = _user(db, "w2c_res_owner3")
    pid, task_id, claim, snap = _prepare_running(client, db, owner, "w2c-proj-ix", "w2c-ix-1")
    payload = _build_payload(snap, [_store_hourly(db), _store_hourly(db)], owner.id)
    pkg1 = results_uc.submit_evidence(db, task_id, claim.attempt_id, claim.lease_token, payload)
    asm1 = results_uc.run_assessment(db, pkg1.id, "full", user=owner)

    idx1 = results_uc.update_result_index(db, task_id, asm1.id)
    assert idx1.is_latest is True
    with _fresh_db(engine) as fresh:
        assert results_domain.get_index(fresh, idx1.id) is not None

    # 同证据新评估 → 只更新指针(行数不变)
    asm2 = results_service.run_assessment(db, pkg1.id, "physical", user=owner)
    db.commit()
    idx2 = results_uc.update_result_index(db, task_id, asm2.id)
    assert idx2.id == idx1.id and idx2.assessment_id == asm2.id

    # 新证据 → 转交 latest(旧行翻转 + 新行)
    pkg2 = results_service.submit_evidence(db, task_id, claim.attempt_id, claim.lease_token, payload)
    db.commit()
    asm3 = results_uc.run_assessment(db, pkg2.id, "full", user=owner)
    idx3 = results_uc.update_result_index(db, task_id, asm3.id)
    assert idx3.id != idx1.id and idx3.is_latest is True
    assert results_domain.get_index(db, idx1.id).is_latest is False

    # 旧服务同语义: 同证据更新指针返回同行
    asm4 = results_service.run_assessment(db, pkg2.id, "physical", user=owner)
    db.commit()
    idx4 = results_service.update_result_index(db, task_id, asm4.id)
    db.commit()
    assert idx4.id == idx3.id and idx4.assessment_id == asm4.id
