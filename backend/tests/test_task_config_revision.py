"""0.12-A2: 计算任务绑定已保存 CalculationConfig revision。

约定:
- 计算类任务(calc/optimization/uncertainty/analysis)提交须在 config 中
  明确引用本项目已保存 revision(``config_revision`` 正整数);
- 后端按 (project_id, version) 精确解析行正文并冻结进快照
  (``calc_config_snapshot`` = 正文 + task_params + config_ref), 不读最新
  指针, 不回退草稿/默认/空字典;
- 缺失或形态非法 → 400 TASK-REQ-006; 查无(未保存/超出现有/跨项目号码)
  → 404 RES-MISS-003;
- 快照复用身份以冻结输入为准(含 config_ref), 不同 revision 永不复用;
  同幂等键命中返回同一任务(命中在先, 不再解析 revision)。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest  # noqa: E402
from auth_helpers import login_headers, make_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan import tasks as tasks_domain  # noqa: E402
from iesplan.core.jsonutil import jsonable  # noqa: E402
from iesplan.api import projects as projects_api  # noqa: E402
from iesplan.api import tasks as tasks_api  # noqa: E402
from iesplan.api import validation as validation_api  # noqa: E402
from iesplan.api.config import config_router  # noqa: E402
from iesplan.api.deps import get_request_db  # noqa: E402
from iesplan.application import identity  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.identity.persistence import User  # noqa: E402,F401
from iesplan.main import create_app  # noqa: E402
from iesplan.project.persistence import Project  # noqa: E402,F401
from iesplan.tasks import persistence as tasks_persistence  # noqa: E402,F401
from iesplan.tasks.persistence import Task  # noqa: E402,F401

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")
os.environ.setdefault("IESPLAN_STORAGE_BACKEND", "local")


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """模块级 SQLite 内存引擎(StaticPool: 所有会话共享同一连接)。"""
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
    """每个测试结束后清空全部表(避免测试间串扰, 与 test_tasks_api 同构)。"""
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def db(engine: Engine):
    """每个测试独立 Session(自动回滚)。"""
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def factory(engine: Engine) -> sessionmaker:
    """API 请求级 Session 工厂(供 get_request_db 覆盖)。"""
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def client(factory: sessionmaker, tmp_path: Path) -> Iterator[TestClient]:
    """挂载业务路由的内存应用(队列内存后端, 本地存储指向 tmp)。"""
    from iesplan.config import settings  # noqa: E402

    settings.data_dir = tmp_path
    app = create_app()
    app.include_router(projects_api.router)
    app.include_router(tasks_api.router)
    app.include_router(config_router)
    app.include_router(validation_api.router)

    def _override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_request_db] = _override
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.clear()


def _h(client: TestClient, user) -> dict[str, str]:
    """认证头: 与 test_tasks_api 共用 login_headers 缓存, 避免多窗口接管。"""
    return login_headers(client, user)


def _prepare_project(client: TestClient, db: Session, owner, name: str = "绑定测试项目") -> int:
    """准备可提交计算任务的项目(与 test_tasks_api 同构: 财务/设备/规划/显式计算声明/装配节/固化版本)。"""
    from test_tasks_api import (  # noqa: E402
        _bind_and_freeze,
        _create_project,
        _declare_assembly_sections,
        _declare_device_and_planning,
        _declare_finance,
        _set_explicit_calc,
    )

    pid = _create_project(client, owner, name)
    _declare_finance(db, owner, pid)
    device_ref, revision = _declare_device_and_planning(client, db, owner, pid)
    revision = _set_explicit_calc(client, pid, owner, revision)
    revision = _declare_assembly_sections(
        client, pid, owner, revision, {device_ref: {"type": "none"}}
    )
    _bind_and_freeze(client, pid, owner, None, revision=revision)
    return pid


def _body(**overrides: Any) -> dict[str, Any]:
    """合法计算配置正文(12 字段契约)。"""
    payload: dict[str, Any] = {
        "schema": "ies.calculation-config",
        "schema_version": "2.0.0",
        "mode": "planning",
        "predictions": [],
        "generator": "ies.generator.highs_milp@7.0.0",
        "solver": "ies.solver.highs@1.7.2",
        "precision": "medium",
        "convergence": None,
        "time_limit_seconds": 600,
        "options": {},
        "seed": None,
        "outputs": ["solution"],
    }
    payload.update(overrides)
    return payload


def _save_config(
    client: TestClient, pid: int, user,
    body: dict[str, Any] | None = None, expected_revision: int | None = None,
) -> int:
    """保存一份计算配置, 返回 revision。"""
    resp = client.put(
        f"/api/projects/{pid}/config",
        json={"calculation_config": body or _body(), "expected_revision": expected_revision},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def _submit(
    client: TestClient, pid: int, user, config: dict[str, Any] | None, key: str,
    task_type: str = "calc",
) -> tuple[int, dict[str, Any]]:
    """提交任务, 返回 (HTTP 状态码, 响应体)。"""
    payload: dict[str, Any] = {"task_type": task_type, "idempotency_key": key}
    if config is not None:
        payload["config"] = dict(config)
        payload["evaluation"] = {
                "evaluation_years": 1, "construction_period": 0,
                "operation_years": 1, "discount_rate": 0.08,
                "construction_payments": [{"t": 0, "ratio": 1}],
                "maintenance_expenses": [],
            }
    resp = client.post(f"/api/projects/{pid}/tasks", json=payload, headers=_h(client, user))
    return resp.status_code, resp.json()


def _detail(client: TestClient, pid: int, user, task_id: int) -> dict[str, Any]:
    resp = client.get(f"/api/projects/{pid}/tasks/{task_id}", headers=_h(client, user))
    assert resp.status_code == 200, resp.text
    return resp.json()["task"]


def _error(resp_body: dict[str, Any]) -> dict[str, Any]:
    assert "error" in resp_body, resp_body
    return resp_body["error"]


def test_missing_revision_rejected_400(client: TestClient, db: Session) -> None:
    """计算任务缺 config_revision → 400 TASK-REQ-006, 无回退。"""
    owner = make_user(db, "owner_bind_req")
    pid = _prepare_project(client, db, owner)
    _save_config(client, pid, owner)
    for config in (None, {}, {"horizon_years": 1}):
        status, body = _submit(client, pid, owner, config, key=f"req-{id(config)}")
        assert status == 400, body
        err = _error(body)
        assert err["code"] == "TASK-REQ-006", err


def test_malformed_revision_rejected_400(client: TestClient, db: Session) -> None:
    """config_revision 非正整数(0/负数/字符串/布尔) → 400 TASK-REQ-006。"""
    owner = make_user(db, "owner_bind_shape")
    pid = _prepare_project(client, db, owner)
    _save_config(client, pid, owner)
    for i, raw in enumerate([0, -1, "1", True, 1.5]):
        status, body = _submit(
            client, pid, owner, {"config_revision": raw}, key=f"shape-{i}"
        )
        assert status == 400, body
        assert _error(body)["code"] == "TASK-REQ-006", body


def test_unknown_revision_404(client: TestClient, db: Session) -> None:
    """未保存/超出现有号码 → 404 RES-MISS-003, 不回退最新或默认。"""
    owner = make_user(db, "owner_bind_miss")
    pid = _prepare_project(client, db, owner)
    status, body = _submit(client, pid, owner, {"config_revision": 1}, key="miss-1")
    assert status == 404, body
    assert _error(body)["code"] == "RES-MISS-003", body

    _save_config(client, pid, owner)
    status, body = _submit(client, pid, owner, {"config_revision": 2}, key="miss-2")
    assert status == 404, body
    assert _error(body)["code"] == "RES-MISS-003", body


def test_cross_project_revision_404(client: TestClient, db: Session) -> None:
    """跨项目号码在本项目查无 → 404, 不得串用他项目正文。"""
    owner = make_user(db, "owner_bind_cross")
    pid_a = _prepare_project(client, db, owner, name="绑定项目A")
    pid_b = _prepare_project(client, db, owner, name="绑定项目B")
    _save_config(client, pid_a, owner)
    _save_config(client, pid_b, owner)
    rev_b2 = _save_config(client, pid_b, owner, expected_revision=1)
    assert rev_b2 == 2
    status, body = _submit(client, pid_a, owner, {"config_revision": 2}, key="cross-1")
    assert status == 404, body
    assert _error(body)["code"] == "RES-MISS-003", body


def test_pinned_body_frozen_and_isolated(
    client: TestClient, db: Session, factory
) -> None:
    """两 revision 各自冻结正文 + config_ref; 改最新不影响既有快照。"""
    owner = make_user(db, "owner_bind_freeze")
    pid = _prepare_project(client, db, owner)
    rev1 = _save_config(client, pid, owner, _body(seed=11, outputs=["solution"]))
    status, body = _submit(client, pid, owner, {"config_revision": rev1}, key="frz-1")
    assert status == 201, body
    task1 = body["task"]
    rev2 = _save_config(
        client, pid, owner, _body(seed=22, outputs=["metrics"]), expected_revision=rev1
    )
    assert rev2 == 2

    status, body = _submit(client, pid, owner, {"config_revision": rev2}, key="frz-2")
    assert status == 201, body
    task2 = body["task"]
    assert task2["id"] != task1["id"]
    assert task2["calc_snapshot_id"] != task1["calc_snapshot_id"]

    snap1 = tasks_domain.get_snapshot(factory(), task1["calc_snapshot_id"])
    snap2 = tasks_domain.get_snapshot(factory(), task2["calc_snapshot_id"])
    assert snap1 is not None and snap2 is not None
    # 冻结内容按领域 canonical 语义比较(jsonable: 元组/列表归一, 与复用判定一致)
    frozen1 = jsonable(dict(snap1.calc_config_snapshot))
    frozen2 = jsonable(dict(snap2.calc_config_snapshot))
    assert frozen1["seed"] == 11 and frozen1["outputs"] == ["solution"]
    assert frozen2["seed"] == 22 and frozen2["outputs"] == ["metrics"]
    assert frozen1["config_ref"] == {"project_id": pid, "revision": 1}
    assert frozen2["config_ref"] == {"project_id": pid, "revision": 2}
    assert "config_revision" not in frozen1.get("task_params", {})
    assert "evaluation" not in frozen1
    assert "evaluation" not in frozen1.get("task_params", {})
    frozen_eval1 = jsonable(dict(snap1.evaluation))
    assert frozen_eval1["evaluation_years"] == 1
    assert frozen_eval1["discount_rate"] == 0.08
    assert frozen_eval1["confirmed_by"] == owner.id
    assert frozen_eval1["confirmed_at"]
    # 改最新后重读旧快照: 正文保持钉选时内容
    snap1_again = tasks_domain.get_snapshot(factory(), task1["calc_snapshot_id"])
    assert snap1_again is not None
    assert jsonable(dict(snap1_again.calc_config_snapshot))["seed"] == 11


def test_same_inputs_reuse_snapshot(client: TestClient, db: Session, factory) -> None:
    """同 revision 同输入不同键 → 新任务但复用同一快照。"""
    owner = make_user(db, "owner_bind_reuse")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)
    status, first = _submit(client, pid, owner, {"config_revision": rev}, key="reuse-1")
    assert status == 201, first
    status, second = _submit(client, pid, owner, {"config_revision": rev}, key="reuse-2")
    assert status == 201, second
    assert second["task"]["id"] != first["task"]["id"]
    assert second["task"]["calc_snapshot_id"] == first["task"]["calc_snapshot_id"]


def test_same_key_replay_returns_same_task(client: TestClient, db: Session) -> None:
    """同键重放返回同一任务(命中在先, 不再解析 revision)。"""
    owner = make_user(db, "owner_bind_replay")
    pid = _prepare_project(client, db, owner)
    rev1 = _save_config(client, pid, owner)
    status, first = _submit(client, pid, owner, {"config_revision": rev1}, key="replay-1")
    assert status == 201, first
    rev2 = _save_config(client, pid, owner, expected_revision=rev1)
    assert rev2 == 2
    status, second = _submit(client, pid, owner, {"config_revision": rev2}, key="replay-1")
    assert status == 200, second
    assert second["replayed"] is True
    assert second["task"]["id"] == first["task"]["id"]


def test_task_params_preserved(client: TestClient, db: Session, factory) -> None:
    """任务级参数保留在 task_params, 不覆盖配置正文, 绑定键不混入。"""
    owner = make_user(db, "owner_bind_params")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)
    status, body = _submit(
        client, pid, owner,
        {"config_revision": rev, "horizon_years": 3, "priority": 5},
        key="params-1",
    )
    assert status == 201, body
    snap = tasks_domain.get_snapshot(factory(), body["task"]["calc_snapshot_id"])
    assert snap is not None
    frozen = dict(snap.calc_config_snapshot)
    assert frozen["task_params"]["horizon_years"] == 3
    assert "config_revision" not in frozen["task_params"]
    assert frozen["mode"] == "planning" and frozen["generator"] == "ies.generator.highs_milp@7.0.0"


def test_detail_shows_config_revision(client: TestClient, db: Session) -> None:
    """任务详情快照摘要展示所用 revision 与项目身份。"""
    owner = make_user(db, "owner_bind_detail")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)
    status, body = _submit(client, pid, owner, {"config_revision": rev}, key="detail-1")
    assert status == 201, body
    detail = _detail(client, pid, owner, body["task"]["id"])
    snap = detail["calc_snapshot"]
    assert snap is not None
    assert snap["config_revision"] == 1
    assert snap["config_project_id"] == pid


def test_report_task_needs_no_revision(client: TestClient, db: Session) -> None:
    """io 类任务无快照, 不要求 config_revision(既有行为不倒退)。"""
    owner = make_user(db, "owner_bind_report")
    pid = _prepare_project(client, db, owner)
    resp = client.post(
        f"/api/projects/{pid}/tasks",
        json={"task_type": "report", "idempotency_key": "rpt-bind-1"},
        headers=_h(client, owner),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["task"]["calc_snapshot_id"] is None


#: 负荷项目装配实例引用(与装配 slug 规则一致, 均为合法本地 id)。
_LOAD_INSTANCES = ("grid1", "load1")


def _prepare_load_project(
    client: TestClient, db: Session, owner,
    demands: dict[str, Any], name: str = "负荷绑定测试项目",
) -> int:
    """含负荷的可提交项目: 电网 + 电负荷(需求经 params 声明) + 电池(规划变量载体)。

    demands 并入负荷 params(如 {"electricity_demand": 100.0}); finance_binding
    全覆盖三实例(none), tariff 空声明。调用方用 demands 构造合法/非法常量。
    """
    from test_tasks_api import (  # noqa: E402
        _bind_and_freeze,
        _create_project,
        _declare_assembly_sections,
        _declare_device_and_planning,
        _declare_finance,
        _set_explicit_calc,
    )

    pid = _create_project(client, owner, name)
    _declare_finance(db, owner, pid)
    device_ref, revision = _declare_device_and_planning(client, db, owner, pid)
    commands = [
        {
            "id": "m-grid1", "unit": "model", "type": "model.upsert_device",
            "payload": {
                "name": "grid1", "device_type": "ies.device.grid_connection",
                "kind": "new", "params": {},
            },
        },
        {
            "id": "m-load1", "unit": "model", "type": "model.upsert_device",
            "payload": {
                "name": "load1", "device_type": "ies.device.electric_load",
                "kind": "new", "params": {"peak_power_kw": 100, **demands},
            },
        },
        {
            "id": "m-conn1", "unit": "model", "type": "model.upsert_connection",
            "payload": {
                "name": "grid1.electricity_import->load1.electricity_in",
                "from_device": "grid1", "from_port": "electricity_import",
                "to_device": "load1", "to_port": "electricity_in",
            },
        },
    ]
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": revision, "commands": commands},
        headers=_h(client, owner),
    )
    assert resp.status_code == 200, resp.text
    revision = resp.json()["revision"]
    revision = _set_explicit_calc(client, pid, owner, revision)
    instances = {device_ref: {"type": "none"}}
    instances.update({inst: {"type": "none"} for inst in _LOAD_INSTANCES})
    revision = _declare_assembly_sections(client, pid, owner, revision, instances)
    _bind_and_freeze(client, pid, owner, None, revision=revision)
    return pid


def _run_validation(client: TestClient, pid: int, user) -> dict[str, Any]:
    """执行完整预检, 返回报告体(run 返回 {report, stored} 信封)。"""
    resp = client.post(
        f"/api/projects/{pid}/validation/run", headers=_h(client, user)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "report" in body, body
    return body["report"]


def _blocking(report: dict[str, Any], code: str) -> list[dict[str, Any]]:
    """报告中指定编码的阻断诊断。"""
    return [
        d for d in report.get("diagnostics", [])
        if d.get("code") == code and d.get("blocking") is True
    ]


def test_predefined_constant_passes_precheck_and_submits(
    client: TestClient, db: Session,
) -> None:
    """合法需求常量: 预检无 PARAM-UNIT-002 阻断, 提交 201(装配可签发)。"""
    owner = make_user(db, "owner_const_ok")
    pid = _prepare_load_project(client, db, owner, {"electricity_demand": 100.0})
    rev = _save_config(client, pid, owner)
    report = _run_validation(client, pid, owner)
    assert _blocking(report, "PARAM-UNIT-002") == [], report.get("diagnostics")
    status, body = _submit(client, pid, owner, {"config_revision": rev}, key="const-ok-1")
    assert status == 201, body
    assert body["task"]["calc_snapshot_id"] is not None


def test_unknown_param_key_still_blocked(client: TestClient, db: Session) -> None:
    """未知参数键仍被预检阻断(不对齐例外不豁免未知键)。"""
    owner = make_user(db, "owner_const_unknown")
    pid = _prepare_load_project(
        client, db, owner,
        {"electricity_demand": 100.0, "mystery_param": 1},
    )
    report = _run_validation(client, pid, owner)
    hits = _blocking(report, "PARAM-UNIT-002")
    assert any(
        d.get("params", {}).get("actual") == "unknown"
        and d.get("params", {}).get("param") == "mystery_param"
        for d in hits
    ), report.get("diagnostics")


def test_illegal_predefined_constant_blocked(client: TestClient, db: Session) -> None:
    """非法需求常量被预检阻断: 布尔/字符串/越界(负值低于 minimum 0)。"""
    for i, bad in (("bool", True), ("str", "100"), ("neg", -5.0)):
        owner = make_user(db, f"owner_const_bad_{i}")
        pid = _prepare_load_project(
            client, db, owner, {"electricity_demand": bad},
            name=f"非法常量项目{i}",
        )
        report = _run_validation(client, pid, owner)
        diags = report.get("diagnostics", [])
        assert any(
            d.get("blocking") is True
            and d.get("code") in ("PARAM-UNIT-002", "PARAM-RNG-003")
            for d in diags
        ), (bad, diags)


def test_nonfinite_predefined_constant_rejected_at_entry() -> None:
    """非有限需求常量在入口被拒绝(inf/-inf/nan 走不到 JSON, 直测入口函数)。

    blocking 由报告聚合按 error 严重度统一标记(见 HTTP 层用例), 此处断言
    编码与 error 严重度。
    """
    from iesplan.application.validations.precheck import (  # noqa: E402
        _validate_device_params,
    )

    for bad in (float("inf"), float("-inf"), float("nan")):
        diags = _validate_device_params(
            "ies.device.electric_load", {"electricity_demand": bad},
            device_documents={}, device_name="load1",
        )
        assert any(
            d.code == "PARAM-UNIT-002" and d.severity == "error" for d in diags
        ), bad


def test_predefined_source_declaration_passes_precheck(
    client: TestClient, db: Session,
) -> None:
    """合法数据序列来源声明: 预检无 PARAM-UNIT-002 假阻断(0.15 对齐构造器入口)。"""
    owner = make_user(db, "owner_decl_ok")
    pid = _prepare_load_project(
        client, db, owner,
        {"electricity_demand": {"mode": "data_repeat", "dataset_version_id": 1}},
    )
    report = _run_validation(client, pid, owner)
    assert _blocking(report, "PARAM-UNIT-002") == [], report.get("diagnostics")


def test_malformed_source_declaration_still_blocked(
    client: TestClient, db: Session,
) -> None:
    """形态不符的来源声明仍被预检明确阻断(不放行任意 dict)。"""
    owner = make_user(db, "owner_decl_bad")
    pid = _prepare_load_project(
        client, db, owner,
        {"electricity_demand": {"mode": "data_guess", "dataset_version_id": 7}},
    )
    report = _run_validation(client, pid, owner)
    hits = _blocking(report, "PARAM-UNIT-002")
    assert any(d.get("params", {}).get("param") == "electricity_demand" for d in hits), (
        report.get("diagnostics")
    )


def test_changed_inputs_with_new_key_yield_new_task_and_snapshot(
    client: TestClient, db: Session, factory,
) -> None:
    """同 revision 但项目输入改变后用新键提交 → 新任务 + 新快照(不误复用)。"""
    owner = make_user(db, "owner_bind_evolve")
    pid = _prepare_load_project(client, db, owner, {"electricity_demand": 100.0})
    rev = _save_config(client, pid, owner)
    status, first = _submit(client, pid, owner, {"config_revision": rev}, key="evo-1")
    assert status == 201, first
    # 改变项目输入: 调整负荷需求常量并重新固化版本
    view = client.get(f"/api/projects/{pid}", headers=_h(client, owner)).json()
    draft_rev = view["draft"]["revision"]
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": draft_rev, "commands": [{
            "id": "m-dem2", "unit": "model", "type": "model.upsert_device",
            "payload": {
                "name": "load1", "params": {"peak_power_kw": 100, "electricity_demand": 200.0},
            },
        }]},
        headers=_h(client, owner),
    )
    assert resp.status_code == 200, resp.text
    from test_tasks_api import _bind_and_freeze  # noqa: E402

    _bind_and_freeze(client, pid, owner, None, revision=resp.json()["revision"])
    status, second = _submit(client, pid, owner, {"config_revision": rev}, key="evo-2")
    assert status == 201, second
    assert second["task"]["id"] != first["task"]["id"]
    assert second["task"]["calc_snapshot_id"] != first["task"]["calc_snapshot_id"]
    snap1 = tasks_domain.get_snapshot(factory(), first["task"]["calc_snapshot_id"])
    snap2 = tasks_domain.get_snapshot(factory(), second["task"]["calc_snapshot_id"])
    assert snap1 is not None and snap2 is not None
    # 冻结装配正文随项目输入变化(需求常量 100 → 200), 快照不相同
    assert snap1.canonical_assembly_text != snap2.canonical_assembly_text
    assert snap1.project_version_id != snap2.project_version_id


def test_snapshot_schema_version_written_and_scoped_to_reuse(
    client: TestClient, db: Session, factory,
) -> None:
    """快照显式写入 schema_version 5.0.0; 复用身份包含版本, 无 tolerances 死字段。

    容差唯一权威仍是正文 convergence(此处冻结往返即证, 不另设字段)。
    """
    from iesplan.application.tasks.submissions import (  # noqa: E402
        _snapshot_inputs_equal,
    )

    owner = make_user(db, "owner_schema_ver")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner, _body())
    status, body = _submit(client, pid, owner, {"config_revision": rev}, key="schemaver-1")
    assert status == 201, body
    snap = tasks_domain.get_snapshot(factory(), body["task"]["calc_snapshot_id"])
    assert snap is not None
    assert snap.schema_version == "5.0.0"
    assert not hasattr(snap, "tolerances")
    frozen = jsonable(dict(snap.calc_config_snapshot))
    assert frozen["convergence"] is None

    status, second = _submit(client, pid, owner, {"config_revision": rev}, key="schemaver-2")
    assert status == 201, second
    assert second["task"]["calc_snapshot_id"] == body["task"]["calc_snapshot_id"]

    same = dict(
        dataset_ids=list(snap.dataset_version_ids or []),
        calc_config=frozen,
        evaluation=jsonable(dict(snap.evaluation or {})),
        result_adapter="ies.result-adapter.highs@6.0.0",
        executor="ies.executor.local_process@1.0.0",
        program_version=snap.program_version or "",
        extensions=jsonable(dict(snap.extension_versions or {})),
        random_seed=snap.random_seed,
        canonical_text=snap.canonical_assembly_text,
        receipt=jsonable(dict(snap.assembly_receipt or {})),
        device_models=jsonable(dict(snap.device_models or {})),
    )
    assert _snapshot_inputs_equal(snap, schema_version="5.0.0", **same) is True
    assert _snapshot_inputs_equal(snap, schema_version="9.9.9", **same) is False


def test_unknown_generator_ref_rejected(client: TestClient, db: Session) -> None:
    """旧算法引用在保存输入边界拒绝，不再可冻结成任务。"""
    owner = make_user(db, "owner_old_algo")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)
    body = _body(generator="ies.algo.milp_hybrid@1.0.0")
    resp = client.put(
        f"/api/projects/{pid}/config",
        json={"calculation_config": body, "expected_revision": rev},
        headers=_h(client, owner),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["location"]["field"] == "generator"
    assert _save_config(client, pid, owner, expected_revision=rev) == rev + 1


def test_solver_not_in_descriptor_rejected(client: TestClient, db: Session) -> None:
    """solver 不在所选生成器兼容清单 → 保存时 422，阻断任务。"""
    owner = make_user(db, "owner_bad_solver")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)
    body = _body(solver="ies.solver.unknown@9.9.9")
    resp = client.put(
        f"/api/projects/{pid}/config",
        json={"calculation_config": body, "expected_revision": rev},
        headers=_h(client, owner),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["location"]["field"] == "solver"
    assert _save_config(client, pid, owner, expected_revision=rev) == rev + 1


def test_execution_refs_frozen_from_descriptor(
    client: TestClient, db: Session, factory,
) -> None:
    """配套 adapter/executor 按 descriptor 冻结; 复用身份包含二者。"""
    from iesplan.application.tasks.submissions import (  # noqa: E402
        _snapshot_inputs_equal,
    )

    owner = make_user(db, "owner_exec_refs")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)
    status, body = _submit(client, pid, owner, {"config_revision": rev}, key="execrefs-1")
    assert status == 201, body
    snap = tasks_domain.get_snapshot(factory(), body["task"]["calc_snapshot_id"])
    assert snap is not None
    assert snap.result_adapter == "ies.result-adapter.highs@6.0.0"
    assert snap.executor == "ies.executor.local_process@1.0.0"

    same = dict(
        dataset_ids=list(snap.dataset_version_ids or []),
        calc_config=jsonable(dict(snap.calc_config_snapshot)),
        evaluation=jsonable(dict(snap.evaluation or {})),
        schema_version=snap.schema_version,
        program_version=snap.program_version or "",
        extensions=jsonable(dict(snap.extension_versions or {})),
        random_seed=snap.random_seed,
        canonical_text=snap.canonical_assembly_text,
        receipt=jsonable(dict(snap.assembly_receipt or {})),
        device_models=jsonable(dict(snap.device_models or {})),
    )
    assert (
        _snapshot_inputs_equal(
            snap, result_adapter="ies.result-adapter.highs@6.0.0",
            executor="ies.executor.local_process@1.0.0", **same,
        )
        is True
    )
    assert (
        _snapshot_inputs_equal(
            snap, result_adapter="ies.result-adapter.other@2.0.0",
            executor="ies.executor.local_process@1.0.0", **same,
        )
        is False
    )


def test_ambiguous_executors_blocked_without_guessing() -> None:
    """多执行器无公开选择规则 → 400 TASK-REQ-009(桩 descriptor, 仅证阻断)。"""
    from types import SimpleNamespace  # noqa: E402

    from iesplan.application.tasks.submissions import (  # noqa: E402
        _resolve_execution_refs,
    )
    from iesplan.computation.generators.descriptor import (  # noqa: E402
        GeneratorDescriptor,
    )
    from iesplan.tasks.contracts import InvalidRequestError  # noqa: E402

    desc = GeneratorDescriptor(
        generator_id="ies.generator.multi",
        version="1.0.0",
        options_schema={"type": "object", "additionalProperties": False},
        assembly_schema_version="2.0",
        modes=("planning",),
        outputs=("solution",),
        precisions=("medium",),
        solvers=("ies.solver.highs@1.7.2",),
        executors=("ies.executor.a@1.0.0", "ies.executor.b@1.0.0"),
        result_adapter="ies.result-adapter.highs@6.0.0",
        unit_system="SI",
        seed_semantics="seed 解释见 descriptor",
    )
    registry = SimpleNamespace(get=lambda ref: SimpleNamespace(descriptor=desc))
    body = {"generator": "ies.generator.multi@1.0.0", "solver": "ies.solver.highs@1.7.2"}
    try:
        _resolve_execution_refs(body, registry, project_id=1)
    except InvalidRequestError as exc:
        assert exc.code == "TASK-REQ-009"
    else:
        raise AssertionError("多执行器应当阻断")


def test_missing_generator_registry_is_server_error() -> None:
    """目录未装配(None) → 500, 无回退。"""
    from iesplan.application.tasks.submissions import (  # noqa: E402
        _resolve_execution_refs,
    )
    from iesplan.core.errors import AppError  # noqa: E402

    body = {"generator": "ies.generator.highs_milp@7.0.0", "solver": "ies.solver.highs@1.7.2"}
    try:
        _resolve_execution_refs(body, None, project_id=1)
    except AppError as exc:
        assert exc.http_status == 500
    else:
        raise AssertionError("缺目录应当 500")


def test_snapshot_freezes_per_instance_device_models(
    client: TestClient, db: Session, factory,
) -> None:
    """快照按装配实例冻结设备数学贡献；复用身份包含贡献；可结构恢复。

    贡献由提交闸门同一次设备解析构建（`{instance_id: contribution dict}`），
    Worker 后续只做类型化恢复，不重跑校验。
    """
    from iesplan.modeling import (
        build_math_contribution,
        contribution_from_dict,
        contribution_to_dict,
    )

    owner = make_user(db, "owner_dev_models")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)
    status, body = _submit(client, pid, owner, {"config_revision": rev}, key="devmodels-1")
    assert status == 201, body
    snap = tasks_domain.get_snapshot(factory(), body["task"]["calc_snapshot_id"])
    assert snap is not None
    models = jsonable(dict(snap.device_models or {}))
    assert models, "快照必须冻结逐实例设备贡献"
    for entry in models.values():
        assert entry["device_id"] == "ies.device.battery"
        assert entry["variables"]["capacity_kwh"]["value"] == 1000

    status, second = _submit(client, pid, owner, {"config_revision": rev}, key="devmodels-2")
    assert status == 201, second
    assert second["task"]["calc_snapshot_id"] == body["task"]["calc_snapshot_id"]

    restored = {
        inst: contribution_to_dict(contribution_from_dict(entry))
        for inst, entry in models.items()
    }
    assert restored == models

    from iesplan.devices import get_device

    built = build_math_contribution(get_device("ies.device.battery"))
    assert built.ok and built.contribution is not None
    original = built.contribution
    assert original.canonical_text
    revived = contribution_from_dict(contribution_to_dict(original))
    # 封存形态按键排序 relations：对象级比较先按 id 规范化顺序；
    # 规范字节相等才是 Worker 侧有效的同一性。
    assert sorted(revived.relations, key=lambda r: r.id) == sorted(
        original.relations, key=lambda r: r.id
    )
    assert revived.canonical_text == original.canonical_text
    assert contribution_to_dict(revived) == contribution_to_dict(original)
    assert models[next(iter(sorted(models)))] == contribution_to_dict(original)


def test_device_catalog_change_does_not_rewrite_history(
    client: TestClient, db: Session, factory, monkeypatch,
) -> None:
    """提交后修改设备目录，历史快照输入不变，新提交不再复用旧快照。

    设备文档变更只影响新冻结的贡献；已封存快照行按当时内容解释。
    """
    import dataclasses

    import iesplan.devices as devices_module

    owner = make_user(db, "owner_dev_hist")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)
    status, body = _submit(client, pid, owner, {"config_revision": rev}, key="devhist-1")
    assert status == 201, body
    first_id = body["task"]["calc_snapshot_id"]
    first = tasks_domain.get_snapshot(factory(), first_id)
    assert first is not None
    first_models = jsonable(dict(first.device_models or {}))
    assert first_models

    real_list_devices = devices_module.list_devices

    def _altered_capacity():
        docs = []
        for doc in real_list_devices():
            if doc.device is not None and doc.device.id == "ies.device.battery":
                props = dict(doc.properties)
                props["capacity_kwh"] = dataclasses.replace(props["capacity_kwh"], value=2000)
                doc = dataclasses.replace(doc, properties=props)
            docs.append(doc)
        return docs

    monkeypatch.setattr(devices_module, "list_devices", _altered_capacity)
    status, body2 = _submit(client, pid, owner, {"config_revision": rev}, key="devhist-2")
    assert status == 201, body2
    assert body2["task"]["calc_snapshot_id"] != first_id
    second = tasks_domain.get_snapshot(factory(), body2["task"]["calc_snapshot_id"])
    assert second is not None
    second_models = jsonable(dict(second.device_models or {}))
    assert second_models != first_models
    for entry in second_models.values():
        assert entry["variables"]["capacity_kwh"]["value"] == 2000

    status, body3 = _submit(client, pid, owner, {"config_revision": rev}, key="devhist-3")
    assert status == 201, body3
    assert body3["task"]["calc_snapshot_id"] == body2["task"]["calc_snapshot_id"]

    first_again = tasks_domain.get_snapshot(factory(), first_id)
    assert first_again is not None
    assert jsonable(dict(first_again.device_models or {})) == first_models


def test_step_relation_save_reopen_and_atomic_invalid_revision(
    client: TestClient, db: Session,
) -> None:
    """真实规划保存用例：保留禁用引用；坏端口不创建下一修订。"""
    from iesplan.application import configuration as config_uc
    from iesplan.application.configuration import revisions as revisions_uc

    owner = make_user(db, "owner_step_s1_save")
    pid = _prepare_project(client, db, owner, name="逐步保存修订测试")
    previous, rev, _ = config_uc.get_planning_config(db, pid)
    assert rev == 1
    payload = previous.to_dict()
    payload["constraints"]["electric_limit"] = {
        "type": "step_relation",
        "references": {"electric": {"instance": "bat1", "interface": "electricity"}},
        "expression": "electric[t] <= 10 kW",
        "enabled": False,
    }
    _, rev2 = config_uc.save_planning_config(db, pid, payload, 1, owner.id)
    assert rev2 == 2
    loaded, loaded_rev, _ = config_uc.get_planning_config(db, pid)
    assert loaded_rev == 2
    assert loaded.to_dict()["constraints"]["electric_limit"] == payload["constraints"]["electric_limit"]

    bad = loaded.to_dict()
    bad["constraints"]["electric_limit"]["references"]["electric"]["interface"] = "unknown"
    with pytest.raises(revisions_uc.InvalidRequestError) as exc:
        config_uc.save_planning_config(db, pid, bad, rev2, owner.id)
    assert exc.value.code == "PROJ-PLAN-003"
    assert "references.electric.interface" in str(exc.value.params)
    still, current_rev, _ = config_uc.get_planning_config(db, pid)
    assert current_rev == rev2
    assert still.to_dict() == loaded.to_dict()


def test_step_relation_enabled_submits_with_supported_generator(
    client: TestClient, db: Session,
) -> None:
    """真实提交：已支持 step_relation 的 v7 接受已冻结启用关系。"""
    from iesplan.application import configuration as config_uc

    owner = make_user(db, "owner_step_s1_gate")
    pid = _prepare_project(client, db, owner, name="逐步任务拒收测试")
    current, rev, _ = config_uc.get_planning_config(db, pid)
    payload = current.to_dict()
    payload["constraints"]["electric_limit"] = {
        "type": "step_relation",
        "references": {"electric": {"instance": "bat1", "interface": "electricity"}},
        "expression": "electric[t] <= 10 kW",
        "enabled": True,
    }
    _, rev2 = config_uc.save_planning_config(db, pid, payload, rev, owner.id)
    assert rev2 == 2
    # 项目版本冻结的是确定规划修订；调用现有版本用例，不直接改数据库。
    from iesplan import project as project_domain
    draft = project_domain.require_current_draft(db, project_domain.require_project(db, pid))
    response = client.post(
        f"/api/projects/{pid}/versions",
        json={"name": "逐步启用版本", "reason": "snapshot_freeze", "expected_revision": draft.revision},
        headers=_h(client, owner),
    )
    assert response.status_code == 201, response.text
    calc_rev = _save_config(client, pid, owner)
    status, body = _submit(client, pid, owner, {"config_revision": calc_rev}, key="step-enabled-s1")
    # S2 implemented the advertised generator capability. The old S1
    # rejection expectation is obsolete; preserve the full public save →
    # freeze → submit workflow and verify the accepted task instead.
    assert status == 201, body
    task = body["task"]
    assert task["type"] == "calc"
    assert task["status"] == "queued"
    assert task["calc_snapshot_id"] is not None


def test_step_duration_shadow_rejected_on_public_planning_save(
    client: TestClient, db: Session,
) -> None:
    """保存边界拒绝原语名冲突且不发布新 revision。"""
    from iesplan.application import configuration as config_uc
    from iesplan.application.configuration.revisions import InvalidRequestError

    owner = make_user(db, "owner_step_reserved")
    pid = _prepare_project(client, db, owner, name="逐步原语保留名")
    current, rev, _ = config_uc.get_planning_config(db, pid)
    payload = current.to_dict()
    payload["variables"]["step_duration"] = payload["variables"].pop("cap")
    payload["constraints"]["electric_limit"] = {
        "type": "step_relation",
        "references": {"electric": {"instance": "bat1", "interface": "electricity"}},
        "expression": "electric[t] * step_duration <= electric[t] * 1 kW",
        "enabled": False,
    }
    with pytest.raises(InvalidRequestError) as exc:
        config_uc.save_planning_config(db, pid, payload, rev, owner.id)
    assert exc.value.code == "PROJ-PLAN-003"
    assert "variables.step_duration" in str(exc.value.params)
    restored, unchanged_rev, _ = config_uc.get_planning_config(db, pid)
    assert unchanged_rev == rev
    assert restored.to_dict() == current.to_dict()
