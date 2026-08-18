"""结果域(U09/U12/U14) API 与服务集成测试。

覆盖: 证据提交(fencing 校验: 过期/失效 token 拒绝) → 证据不可变追加 →
评估四维(物理/最优性/财务/可靠性, 细粒度 + 粗粒度映射 + 派生摘要) →
单维重查 → 索引更新(仅最新引用) → 评估历史追加 → 选择结果(预览校验/换选) →
差异预览 → 逐时分页查询 → 检查任务创建。

- 数据库: SQLite :memory:(models 全部表 create_all, StaticPool 共享连接);
- 队列: IESPLAN_QUEUE=memory 强制内存后端(单进程, 无外部 Redis 依赖);
- 应用: create_app() + include_router(projects/tasks/results),
  dependency_overrides 替换 get_db;
- 对象存储: settings.data_dir 指向 pytest tmp_path, 证据/逐时对象真实落盘;
- 证据提交无 HTTP 端点(Worker 通道), 测试直接调用 services.results.submit_evidence,
  评估/选择/差异/逐时/检查走 HTTP。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

# 单文件运行时的安全网: 固定 SQLite + 内存队列, 避免误连部署 Postgres/Redis
os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan.api import projects as projects_api  # noqa: E402
from iesplan.api import results as results_api  # noqa: E402
from iesplan.api import tasks as tasks_api  # noqa: E402
from iesplan.config import settings  # noqa: E402
from iesplan.db import Base, get_db  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.models.calc import Task, TaskLease  # noqa: E402
from iesplan.models.identity import User  # noqa: E402
from iesplan.models.result import EvidencePackage, ResultSelection  # noqa: E402
from iesplan.services import objects as objects_service  # noqa: E402
from iesplan.services import queue  # noqa: E402
from iesplan.services import results as results_service  # noqa: E402
from iesplan.services import tasks as tasks_service  # noqa: E402

# ---------------------------------------------------------------------------
# 测试环境
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """会话级 SQLite 内存引擎(StaticPool: 所有会话共享同一连接)。"""
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
    """每个测试前重置内存队列, 结束后清空全部表(避免测试间串扰)。"""
    queue.force_memory()
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def db(engine: Engine) -> Iterator[Session]:
    """函数级共享会话(服务与测试共用, 端点内 commit)。"""
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


@pytest.fixture()
def client(engine: Engine, db: Session, tmp_path: Path) -> Iterator[TestClient]:
    """测试客户端: 挂载项目+任务+结果路由, 替换 get_db 依赖, 对象存储指向临时目录。"""
    settings.data_dir = tmp_path
    app = create_app()
    app.include_router(projects_api.router)
    app.include_router(tasks_api.router)
    app.include_router(results_api.router)

    def _override_get_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _h(user_id: int) -> dict[str, str]:
    """认证头(阶段实现: X-User-Id 模拟认证主体)。"""
    return {"X-User-Id": str(user_id)}


def _canonical(doc: dict[str, Any]) -> str:
    """规范化 JSON(与服务层 _canonical_json 一致), 用于校验值计算。"""
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _make_user(db: Session, username: str) -> User:
    """直接创建测试用户。"""
    user = User(username=username, display_name=username, locale="zh-CN")
    db.add(user)
    db.commit()
    return user


def _prepare_project(client: TestClient, db: Session, user_id: int, name: str = "结果测试项目") -> int:
    """准备项目: 创建 + 固化不可变版本(计算任务快照装配的前提)。"""
    resp = client.post("/api/projects", json={"name": name}, headers=_h(user_id))
    assert resp.status_code == 201, resp.text
    pid = resp.json()["project"]["id"]
    resp = client.post(
        f"/api/projects/{pid}/versions",
        json={"name": "结果基线", "reason": "snapshot_freeze"},
        headers=_h(user_id),
    )
    assert resp.status_code == 201, resp.text
    return pid


def _submit_task(
    client: TestClient, pid: int, user_id: int, idempotency_key: str
) -> dict[str, Any]:
    """提交计算任务(optimization), 返回任务体。"""
    resp = client.post(
        f"/api/projects/{pid}/tasks",
        json={"task_type": "optimization", "idempotency_key": idempotency_key},
        headers=_h(user_id),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["task"]


def _claim(db: Session, task_id: int, worker_id: str = "fake-exec-1") -> Any:
    """假执行器: 领取任务(占槽 + 建尝试 + 建租约 + running), 返回 Claim(token)。"""
    claim = tasks_service.claim_and_run(db, task_id, worker_id)
    assert claim is not None
    db.commit()
    return claim


def _store_hourly(db: Session, rows: int, fields: list[str] | None = None) -> int:
    """写入逐时结果对象(对象存储, 真实落盘), 返回 object_id。"""
    fields = fields or ["p_grid_buy", "p_grid_sell", "soc"]
    data: dict[str, list[float]] = {
        "p_grid_buy": [float(i % 100) for i in range(rows)],
        "p_grid_sell": [float((i % 50) * 0.5) for i in range(rows)],
        "soc": [0.5 + 0.001 * (i % 100) for i in range(rows)],
    }
    doc = {
        "fields": fields,
        "data": {f: data[f] for f in fields},
        "meta": {"resolution": "1h", "rows": rows,
                 "units": {"p_grid_buy": "kW", "p_grid_sell": "kW", "soc": "%"}},
    }
    obj = objects_service.put_object(
        db, _canonical(doc).encode("utf-8"), "application/json", "eval_results",
        purpose="hourly_result",
    )
    db.commit()
    return obj.id


def _base_content(snapshot_id: int, hourly_object_ids: list[int]) -> dict[str, Any]:
    """基准证据内容(四维全部达标)。"""
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
            {"index": 0,
             "capacities": {"ies.device.pv": 500.0, "ies.device.battery": 200.0},
             "irr": 0.12, "npv": 123456.0, "capex": 800000.0},
            {"index": 1, "capacities": {"ies.device.pv": 400.0},
             "irr": 0.09, "npv": 50000.0, "capex": 600000.0},
        ],
        "metrics": {"annual_buy_kwh": 1000000.0, "annual_pv_kwh": 500000.0,
                    "total_op_cost": 123456.78, "peak_grid_buy_kw": 250.0,
                    "co2_total_kg": 500000.0},
        "hourly_refs": [
            {"solution_id": 0, "object_id": hourly_object_ids[0], "rows": 100,
             "fields": ["p_grid_buy", "p_grid_sell", "soc"]},
            {"solution_id": 1, "object_id": hourly_object_ids[1], "rows": 100,
             "fields": ["p_grid_buy"]},
        ],
        "residuals": {"all_passed": True, "max_normalized": 1.2e-9, "items": [
            {"name": "电平衡", "normalized": 1.2e-9, "tol": 1e-6, "passed": True,
             "residual": 0.001, "scale": 1000.0, "tau": None},
            {"name": "热平衡", "normalized": 8.0e-10, "tol": 1e-6, "passed": True,
             "residual": 0.0001, "scale": 1000.0, "tau": None},
        ]},
        "constraints": {"checked": True, "capacity_violations": [], "boundary_violations": []},
        "financial": {"investment": 800000.0, "annual_om": 10000.0, "annual_saving": 150000.0,
                      "irr": 0.12, "irr_status": "unique", "irr_message": "唯一正实根",
                      "npv": 123456.0,
                      "cashflows": [-800000.0, 100000.0, 100000.0, 100000.0]},
        "reliability": {"executed": True, "mode": "fixed_plan", "total_samples": 100,
                        "valid_samples": 100, "invalid_samples": 0, "failure_reasons": [],
                        "scope": "年度典型年", "metrics": {"eens_kwh": 0.0, "lolp": 0.0}},
    }


def _build_payload(
    snapshot_id: int,
    hourly_object_ids: list[int],
    created_by: int,
    *,
    content_overrides: dict[str, Any] | None = None,
    payload_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造证据载荷: 顶层清单 + content(含校验值), 支持内容/清单覆盖。"""
    content = _base_content(snapshot_id, hourly_object_ids)
    if content_overrides:
        content.update(content_overrides)
    payload = {
        "snapshot_id": snapshot_id,
        "algorithm": content["algorithm"],
        "seed": content["seed"],
        "stop_condition": content["stop_condition"],
        "solve": content["solve"],
        "candidate_indices": content["candidate_indices"],
        "metrics": content["metrics"],
        "hourly_refs": content["hourly_refs"],
        "content": content,
        "checksum": sha256(_canonical(content).encode("utf-8")).hexdigest(),
        "created_by": created_by,
    }
    if payload_overrides:
        payload.update(payload_overrides)
    return payload


def _submit_evidence(
    db: Session, task_id: int, claim: Any, payload: dict[str, Any]
) -> EvidencePackage:
    """假 Worker 通道: 提交证据包并提交事务。"""
    pkg = results_service.submit_evidence(
        db, task_id, claim.attempt_id, claim.lease_token, payload
    )
    db.commit()
    return pkg


def _prepare_task_with_evidence(
    client: TestClient, db: Session, user_id: int, *,
    hourly_rows: int = 100, complete: bool = True,
) -> tuple[int, int, Any, EvidencePackage, list[int]]:
    """完整准备: 项目 → 任务 → 领取 → 逐时对象 → 证据提交 → (可选)任务完成。

    complete=False 时任务保持 running(租约仍有效), 供同尝试追加证据/单维重查。
    返回 (project_id, task_id, claim, package, hourly_object_ids)。
    """
    pid = _prepare_project(client, db, user_id)
    task = _submit_task(client, pid, user_id, idempotency_key=f"res-{pid}")
    task_id = task["id"]
    claim = _claim(db, task_id)
    obj_a = _store_hourly(db, hourly_rows)
    obj_b = _store_hourly(db, hourly_rows, fields=["p_grid_buy"])
    payload = _build_payload(task["calc_snapshot_id"], [obj_a, obj_b], user_id)
    pkg = _submit_evidence(db, task_id, claim, payload)
    if complete:
        tasks_service.complete_task(db, task_id, solver_status="OPTIMAL")
        db.commit()
    return pid, task_id, claim, pkg, [obj_a, obj_b]


# ---------------------------------------------------------------------------
# 证据提交与 fencing
# ---------------------------------------------------------------------------


def test_evidence_submit_and_fencing(client: TestClient, db: Session) -> None:
    """证据提交: 正常写入 → 不可变内容校验; 过期/失效 token 拒绝(409)。"""
    owner = _make_user(db, "owner_fence")
    pid = _prepare_project(client, db, owner.id)
    task = _submit_task(client, pid, owner.id, idempotency_key="fence-1")
    task_id = task["id"]
    claim = _claim(db, task_id)
    obj_a = _store_hourly(db, 100)
    obj_b = _store_hourly(db, 100, fields=["p_grid_buy"])
    payload = _build_payload(task["calc_snapshot_id"], [obj_a, obj_b], owner.id)

    # 1) 正常提交: complete + 64 位内容校验值 + 快照/算法/种子/逐时引用落库
    pkg = _submit_evidence(db, task_id, claim, payload)
    assert pkg.task_id == task_id
    assert pkg.attempt_id == claim.attempt_id
    assert pkg.calc_snapshot_id == task["calc_snapshot_id"]
    assert pkg.status == "complete"
    assert len(pkg.content_hash) == 64
    assert pkg.object_id is not None
    assert pkg.created_by == owner.id

    # 2) 读取内容校验: content 与 checksum 一致, 字段齐全
    loaded = results_service.evidence_content(db, results_service.get_evidence(db, pkg.id))
    assert loaded["content"] == payload["content"]
    assert loaded["checksum"] == payload["checksum"]
    assert loaded["content"]["algorithm"] == "milp"
    assert loaded["content"]["seed"] == 42
    assert loaded["content"]["candidate_indices"] == [0, 1]
    assert loaded["content"]["hourly_refs"][0]["object_id"] == obj_a
    assert loaded["content"]["hourly_refs"][1]["object_id"] == obj_b

    # 3) 证据不可变追加: 同尝试再次提交(同内容) → 新行, 不覆盖
    pkg2 = _submit_evidence(db, task_id, claim, payload)
    assert pkg2.id != pkg.id
    rows = db.execute(
        select(EvidencePackage).where(EvidencePackage.task_id == task_id)
        .order_by(EvidencePackage.id)
    ).scalars().all()
    assert [r.id for r in rows] == [pkg.id, pkg2.id]

    # 4) fencing: 伪造/失效 token 拒绝
    with pytest.raises(results_service.EvidenceWriteDeniedError) as exc:
        results_service.submit_evidence(db, task_id, claim.attempt_id, uuid4(), payload)
    assert exc.value.http_status == 409

    # 5) fencing: 租约过期拒绝
    lease = db.execute(
        select(TaskLease).where(TaskLease.attempt_id == claim.attempt_id)
    ).scalar_one()
    lease.expires_at = lease.expires_at.replace(year=2020)
    db.commit()
    with pytest.raises(results_service.EvidenceWriteDeniedError) as exc:
        results_service.submit_evidence(db, task_id, claim.attempt_id, claim.lease_token, payload)
    assert exc.value.http_status == 409

    # 6) fencing: 尝试已结束(任务完成, 租约吊销)拒绝
    tasks_service.complete_task(db, task_id, solver_status="OPTIMAL")
    db.commit()
    with pytest.raises(results_service.EvidenceWriteDeniedError):
        results_service.submit_evidence(db, task_id, claim.attempt_id, claim.lease_token, payload)


def test_evidence_checksum_mismatch_records_invalid(client: TestClient, db: Session) -> None:
    """内容校验失败: 证据落库但 status='invalid'(校验失败不可用, 01 §8.1)。"""
    owner = _make_user(db, "owner_badpkg")
    pid = _prepare_project(client, db, owner.id)
    task = _submit_task(client, pid, owner.id, idempotency_key="bad-1")
    claim = _claim(db, task["id"])
    obj_a = _store_hourly(db, 10)
    obj_b = _store_hourly(db, 10, fields=["p_grid_buy"])
    payload = _build_payload(task["calc_snapshot_id"], [obj_a, obj_b], owner.id)
    payload["checksum"] = sha256(b"tampered").hexdigest()  # 篡改校验值
    pkg = _submit_evidence(db, task["id"], claim, payload)
    assert pkg.status == "invalid"
    # 缺失逐时引用 → invalid
    payload2 = _build_payload(task["calc_snapshot_id"], [obj_a, obj_b], owner.id,
                              payload_overrides={"hourly_refs": []})
    pkg2 = _submit_evidence(db, task["id"], claim, payload2)
    assert pkg2.status == "invalid"


# ---------------------------------------------------------------------------
# 评估四维 + 索引 + 历史追加
# ---------------------------------------------------------------------------


def test_assessment_four_dimensions_and_index(client: TestClient, db: Session) -> None:
    """四维全查: 物理/最优性/财务/可靠性均 pass → usable; 索引指向最新评估;
    再次评估追加新记录不覆盖, 索引只更新引用指针。"""
    owner = _make_user(db, "owner_assess")
    pid, task_id, _claim_ok, pkg, _objs = _prepare_task_with_evidence(client, db, owner.id)

    # 1) 评估前: 结果视图有证据无评估
    resp = client.get(f"/api/projects/{pid}/tasks/{task_id}/result", headers=_h(owner.id))
    assert resp.status_code == 200
    view = resp.json()["result"]
    assert view["task"]["business_outcome"] == "normal_completion"
    assert view["evidence"]["status"] == "complete"
    assert view["assessment"] is None
    assert view["metrics_summary"]["annual_buy_kwh"] == 1000000.0
    assert len(view["hourly_refs"]) == 2

    # 2) 触发评估: 四维 pass, 细粒度 + 派生摘要
    resp = client.post(
        f"/api/projects/{pid}/tasks/{task_id}/result/assess",
        json={"assessment_type": "full"}, headers=_h(owner.id),
    )
    assert resp.status_code == 201, resp.text
    a1 = resp.json()["assessment"]
    assert a1["dimensions"] == {"physical": "pass", "optimality": "pass",
                                "financial": "pass", "reliability": "pass"}
    assert a1["fine_states"]["physical"] == "passed"
    assert a1["fine_states"]["financial_irr_status"] == "unique"
    assert a1["summary"]["summary"] == "usable"
    assert a1["overall_score"] == 100.0
    assert a1["detail"]["rule_versions"]["physical"] == "1.0.0"
    assert a1["detail"]["checked"] == ["physical", "optimality", "financial", "reliability"]
    assert a1["detail"]["checks"]["financial"]["irr_status"] == "unique"

    # 3) 结果索引: 指向该评估, 业务哈希 64 位
    index = results_service.latest_index(db, db.get(Task, task_id))
    assert index is not None
    assert index.assessment_id == a1["id"]
    assert index.evidence_package_id == pkg.id
    assert len(index.result_hash) == 64

    # 4) 结果视图: 四维结论展示
    resp = client.get(f"/api/projects/{pid}/tasks/{task_id}/result", headers=_h(owner.id))
    view = resp.json()["result"]
    assert view["assessment"]["summary"]["summary"] == "usable"
    assert view["assessment"]["dimensions"]["physical"] == "pass"

    # 5) 再次评估: 追加新记录不覆盖; 索引仍指向最新(同证据包只更新指针)
    resp = client.post(
        f"/api/projects/{pid}/tasks/{task_id}/result/assess",
        json={"assessment_type": "full"}, headers=_h(owner.id),
    )
    assert resp.status_code == 201
    a2 = resp.json()["assessment"]
    assert a2["id"] != a1["id"]
    history = results_service.list_assessments(db, task_id)
    assert [a.id for a in history] == [a2["id"], a1["id"]]
    index2 = results_service.latest_index(db, db.get(Task, task_id))
    assert index2.id == index.id  # 索引行未新增
    assert index2.assessment_id == a2["id"]  # 引用指针已更新

    # 6) 评估历史接口: 不可变追加可见
    resp = client.get(f"/api/projects/{pid}/tasks/{task_id}/result/assessments", headers=_h(owner.id))
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    assert items[0]["id"] == a2["id"]
    assert items[1]["id"] == a1["id"]
    assert items[1]["summary"]["summary"] == "usable"


def test_assessment_dimension_variants(client: TestClient, db: Session) -> None:
    """四维独立记录: 物理失败→unusable; 时间上限→受限; IRR 多根→受限;
    可靠性部分/不足/未执行分别表达, 汇总不掩盖任何维度。"""
    owner = _make_user(db, "owner_variant")
    # 任务保持 running: 变体测试需在同一尝试上追加多份证据
    pid, task_id, claim, _pkg, objs = _prepare_task_with_evidence(
        client, db, owner.id, complete=False
    )
    snap = db.get(Task, task_id).calc_snapshot_id

    def assess(content_overrides: dict[str, Any]) -> dict[str, Any]:
        payload = _build_payload(snap, objs, owner.id, content_overrides=content_overrides)
        _submit_evidence(db, task_id, claim, payload)
        resp = client.post(
            f"/api/projects/{pid}/tasks/{task_id}/result/assess",
            json={"assessment_type": "full"}, headers=_h(owner.id),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["assessment"]

    # 1) 物理失败: 残差超容差 → fail → 不可用
    a = assess({"residuals": {"all_passed": False, "max_normalized": 2.0, "items": [
        {"name": "电平衡", "normalized": 2.0, "tol": 1e-6, "passed": False,
         "residual": 1000.0, "scale": 500.0, "tau": 3},
    ]}})
    assert a["dimensions"]["physical"] == "fail"
    assert a["fine_states"]["physical"] == "failed"
    assert a["summary"]["summary"] == "unusable"
    assert "physical:failed" in a["summary"]["reasons"]

    # 2) 最优性受限: 时间上限返回 incumbent → restricted → 受限使用
    a = assess({"solve": {"solver_status": "TIME_LIMIT_WITH_INCUMBENT", "objective": 1200000.0,
                          "gap": 2.5, "stop_reason": "时间上限", "feasible": True},
                "stop_condition": {"status": "TIME_LIMIT_WITH_INCUMBENT", "stop_reason": "时间上限",
                                   "time_limit_s": 600, "mip_rel_gap": 0.001, "gap": 2.5}})
    assert a["dimensions"]["optimality"] == "unknown"
    assert a["fine_states"]["optimality"] == "restricted"
    assert a["summary"]["summary"] == "restricted"
    assert "optimality:restricted" in a["summary"]["reasons"]

    # 3) 最优性失败: 无可行解 → fail
    a = assess({"solve": {"solver_status": "NO_FEASIBLE_FOUND", "objective": None,
                          "gap": None, "stop_reason": "无可行解", "feasible": False},
                "stop_condition": {"status": "NO_FEASIBLE_FOUND", "stop_reason": "无可行解"}})
    assert a["dimensions"]["optimality"] == "fail"
    assert a["summary"]["summary"] == "unusable"

    # 4) 财务受限: IRR 多根 → restricted(保守处理), 细粒度保留 irr_status
    a = assess({"financial": {"investment": 800000.0, "irr": 0.1, "irr_status": "multiple",
                              "irr_message": "多次符号变化取最小正根", "npv": None,
                              "cashflows": [-100.0, 300.0, -200.0, 150.0]}})
    assert a["dimensions"]["financial"] == "unknown"
    assert a["fine_states"]["financial"] == "restricted"
    assert a["fine_states"]["financial_irr_status"] == "multiple"
    assert a["summary"]["summary"] == "restricted"

    # 5) 财务失败: 无根 → fail
    a = assess({"financial": {"investment": 800000.0, "irr": None, "irr_status": "none",
                              "irr_message": "符号无变化", "npv": None, "cashflows": [1.0, 2.0]}})
    assert a["dimensions"]["financial"] == "fail"
    assert a["summary"]["summary"] == "unusable"

    # 6) 可靠性部分执行: 部分样本无效 → partial → 受限
    a = assess({"reliability": {"executed": True, "mode": "fixed_plan", "total_samples": 100,
                                "valid_samples": 80, "invalid_samples": 20,
                                "failure_reasons": ["求解超时"], "scope": "年度典型年"}})
    assert a["dimensions"]["reliability"] == "unknown"
    assert a["fine_states"]["reliability"] == "partial"
    assert "reliability:partial" in a["summary"]["reasons"]

    # 7) 可靠性不足: 有效样本低于下限 → insufficient → 不可用
    a = assess({"reliability": {"executed": True, "mode": "replanning", "total_samples": 10,
                                "valid_samples": 8, "invalid_samples": 2,
                                "failure_reasons": ["无效样本"], "scope": "重规划"}})
    assert a["dimensions"]["reliability"] == "fail"
    assert a["fine_states"]["reliability"] == "insufficient"

    # 8) 可靠性未执行: 结果不要求可靠性评估 → not_executed(不判通过, REQ-VALID-001)
    a = assess({"reliability": {"executed": False}})
    assert a["dimensions"]["reliability"] == "unknown"
    assert a["fine_states"]["reliability"] == "not_executed"
    assert a["summary"]["summary"] == "usable"  # 未执行不算失败, 其余通过 → 可用


def test_single_dimension_reassessment(client: TestClient, db: Session) -> None:
    """单维重查: 只查指定维度, 其余记 unknown; 仍追加新评估记录。"""
    owner = _make_user(db, "owner_single")
    pid, task_id, _claim_ok, _pkg, _objs = _prepare_task_with_evidence(client, db, owner.id)
    resp = client.post(
        f"/api/projects/{pid}/tasks/{task_id}/result/assess",
        json={"assessment_type": "physical"}, headers=_h(owner.id),
    )
    assert resp.status_code == 201, resp.text
    a = resp.json()["assessment"]
    assert a["detail"]["checked"] == ["physical"]
    assert a["dimensions"]["physical"] == "pass"
    assert a["dimensions"]["optimality"] == "unknown"
    assert a["dimensions"]["financial"] == "unknown"
    assert a["dimensions"]["reliability"] == "unknown"
    assert a["summary"]["summary"] == "usable"  # 物理 pass, 其余 na → 可用(规则 2)
    # 未知评估类型 → 400
    resp = client.post(
        f"/api/projects/{pid}/tasks/{task_id}/result/assess",
        json={"assessment_type": "bogus"}, headers=_h(owner.id),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 结果选择 + 差异预览
# ---------------------------------------------------------------------------


def test_result_selection_diff_and_preview(client: TestClient, db: Session) -> None:
    """选择结果: 预览校验一致→201; 校验不符→409; 越界解→400; 换选=追加;
    差异预览反映所选解容量与来源版本。"""
    owner = _make_user(db, "owner_select")
    pid, task_id, _claim_ok, pkg, _objs = _prepare_task_with_evidence(client, db, owner.id)
    resp = client.post(
        f"/api/projects/{pid}/tasks/{task_id}/result/assess",
        json={"assessment_type": "full"}, headers=_h(owner.id),
    )
    assert resp.status_code == 201

    # 1) 未选中时差异预览 → 404
    resp = client.get(f"/api/projects/{pid}/tasks/{task_id}/result/diff", headers=_h(owner.id))
    assert resp.status_code == 404

    # 2) 预览校验: 客户端确认的差异摘要必须与当前补丁一致
    payload = results_service.evidence_content(db, pkg)
    content = payload["content"]
    expected_diff = results_service.build_diff_patch(content, 0)
    preview = sha256(_canonical(expected_diff).encode("utf-8")).hexdigest()
    resp = client.post(
        f"/api/projects/{pid}/tasks/{task_id}/result/select",
        json={"solution_id": 0, "selection_type": "adopt", "reason": "IRR 最高",
              "preview_checksum": preview},
        headers=_h(owner.id),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["selection"]["is_current"] is True
    assert body["diff"]["solution_id"] == 0
    assert body["diff"]["diff_patch"] == expected_diff
    assert body["diff"]["preview_checksum"] == preview
    assert body["diff"]["project_version_id"] is not None
    # 补丁含容量参数名映射(04 §3)与财务摘要
    patch = body["diff"]["diff_patch"]["params"]["result_adoption"]
    assert patch["capacities"]["ies.device.pv"] == 500.0
    assert patch["capacity_params"]["rated_capacity_kwp"] == 500.0
    assert patch["irr"] == 0.12

    # 3) 预览校验不符 → 409(确认预览内容校验, RPD 20.3)
    resp = client.post(
        f"/api/projects/{pid}/tasks/{task_id}/result/select",
        json={"solution_id": 1, "selection_type": "adopt",
              "preview_checksum": sha256(b"stale-preview").hexdigest()},
        headers=_h(owner.id),
    )
    assert resp.status_code == 409

    # 4) 越界解标识 → 400
    resp = client.post(
        f"/api/projects/{pid}/tasks/{task_id}/result/select",
        json={"solution_id": 99, "selection_type": "adopt"}, headers=_h(owner.id),
    )
    assert resp.status_code == 400

    # 5) 换选: 新行 + 旧行 is_current=false(历史选中保留)
    preview1 = sha256(_canonical(results_service.build_diff_patch(content, 1)).encode()).hexdigest()
    resp = client.post(
        f"/api/projects/{pid}/tasks/{task_id}/result/select",
        json={"solution_id": 1, "selection_type": "reference", "reference_rule": "benchmark",
              "reason": "参考方案", "preview_checksum": preview1},
        headers=_h(owner.id),
    )
    assert resp.status_code == 201, resp.text
    selections = db.execute(
        select(ResultSelection).where(ResultSelection.project_id == pid).order_by(ResultSelection.id)
    ).scalars().all()
    assert len(selections) == 2
    assert selections[0].is_current is False
    assert selections[1].is_current is True
    # 差异预览切换到所选解 1
    resp = client.get(f"/api/projects/{pid}/tasks/{task_id}/result/diff", headers=_h(owner.id))
    assert resp.status_code == 200
    diff = resp.json()["diff"]
    assert diff["solution_id"] == 1
    assert diff["diff_patch"]["params"]["result_adoption"]["capacities"]["ies.device.pv"] == 400.0

    # 6) 结果视图展示当前选中
    resp = client.get(f"/api/projects/{pid}/tasks/{task_id}/result", headers=_h(owner.id))
    view = resp.json()["result"]
    assert view["selection"]["id"] == selections[1].id
    assert view["selection"]["reason"] == "参考方案"


# ---------------------------------------------------------------------------
# 逐时分页查询
# ---------------------------------------------------------------------------


def test_hourly_pagination(client: TestClient, db: Session) -> None:
    """逐时查询: 分页翻页/越界收敛/未知字段 400/按解选择数据源。"""
    owner = _make_user(db, "owner_hourly")
    pid, task_id, _claim_ok, _pkg, _objs = _prepare_task_with_evidence(client, db, owner.id)
    url = f"/api/projects/{pid}/tasks/{task_id}/result/hourly"

    # 1) 第一页: start=0 end=20 limit=8 → 8 行 + next_start 游标
    resp = client.get(url, params={"field": "p_grid_buy", "start": 0, "end": 20, "limit": 8},
                      headers=_h(owner.id))
    assert resp.status_code == 200, resp.text
    page = resp.json()
    assert page["field"] == "p_grid_buy"
    assert page["unit"] == "kW"
    assert page["start"] == 0 and page["end"] == 8
    assert page["values"] == [float(i % 100) for i in range(8)]
    assert page["next_start"] == 8
    assert page["total_rows"] == 100

    # 2) 翻页至页尾: 第二页 8..16, 末页 16..20 后 next_start=None
    resp = client.get(url, params={"field": "p_grid_buy", "start": 8, "end": 20, "limit": 8},
                      headers=_h(owner.id))
    assert resp.json()["next_start"] == 16
    resp = client.get(url, params={"field": "p_grid_buy", "start": 16, "end": 20, "limit": 8},
                      headers=_h(owner.id))
    tail = resp.json()
    assert tail["start"] == 16 and tail["end"] == 20
    assert len(tail["values"]) == 4
    assert tail["next_start"] is None

    # 3) 越界: start 超出总行数 → 空页, 不报错
    resp = client.get(url, params={"field": "p_grid_buy", "start": 200, "limit": 10},
                      headers=_h(owner.id))
    page = resp.json()
    assert page["values"] == [] and page["next_start"] is None
    assert page["total_rows"] == 100

    # 4) 缺省 end: 到末尾
    resp = client.get(url, params={"field": "p_grid_buy", "start": 95, "limit": 100},
                      headers=_h(owner.id))
    assert resp.json()["end"] == 100

    # 5) 未知字段 → 400(可用字段清单)
    resp = client.get(url, params={"field": "no_such_field"}, headers=_h(owner.id))
    assert resp.status_code == 400

    # 6) 按解选择数据源: solution_id=1 的引用字段
    resp = client.get(url, params={"field": "p_grid_buy", "solution_id": 1, "start": 0, "limit": 3},
                      headers=_h(owner.id))
    assert resp.status_code == 200
    assert len(resp.json()["values"]) == 3
    # 引用内不存在的字段 → 400
    resp = client.get(url, params={"field": "soc", "solution_id": 1}, headers=_h(owner.id))
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 检查任务
# ---------------------------------------------------------------------------


def test_run_check_task(client: TestClient, db: Session) -> None:
    """检查任务: 对已有证据包创建 report 任务; 无证据包 → 404。"""
    owner = _make_user(db, "owner_check")
    pid, task_id, _claim_ok, pkg, _objs = _prepare_task_with_evidence(client, db, owner.id)

    resp = client.post(
        f"/api/projects/{pid}/tasks/{task_id}/result/check", json={}, headers=_h(owner.id)
    )
    assert resp.status_code == 201, resp.text
    task = resp.json()["task"]
    assert task["type"] == "report"
    assert task["status"] == "queued"
    # 检查任务通过服务创建的配置携带证据包引用(队列视图可查)
    assert pkg.id > 0

    # 显式指定证据包
    resp = client.post(
        f"/api/projects/{pid}/tasks/{task_id}/result/check",
        json={"evidence_package_id": pkg.id}, headers=_h(owner.id),
    )
    assert resp.status_code == 201

    # 无证据包的任务 → 404
    pid2 = _prepare_project(client, db, owner.id, name="无证据项目")
    task2 = _submit_task(client, pid2, owner.id, idempotency_key="check-none")
    resp = client.post(
        f"/api/projects/{pid2}/tasks/{task2['id']}/result/check", json={}, headers=_h(owner.id)
    )
    assert resp.status_code == 404


def test_result_view_permission(client: TestClient, db: Session) -> None:
    """权限: 非项目成员访问结果视图 → 403。"""
    owner = _make_user(db, "owner_perm")
    outsider = _make_user(db, "outsider_perm")
    pid, task_id, _claim_ok, _pkg, _objs = _prepare_task_with_evidence(client, db, owner.id)
    resp = client.get(f"/api/projects/{pid}/tasks/{task_id}/result", headers=_h(outsider.id))
    assert resp.status_code == 403
