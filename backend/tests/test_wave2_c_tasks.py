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
from iesplan.api.deps import get_request_db  # noqa: E402
from iesplan.application import tasks as tasks_uc  # noqa: E402
from iesplan.config import settings  # noqa: E402
from iesplan.core.diagnostics import SEVERITY_ERROR, TASK_DATA_SNAPSHOT_MISSING, TASK_SOLVE_FAILED  # noqa: E402
from iesplan.core.errors import NotFoundError  # noqa: E402
from iesplan.dataset.persistence import DatasetVersion  # noqa: E402,F401
from iesplan.db import Base  # noqa: E402
from iesplan.identity.persistence import User  # noqa: E402,F401
from iesplan.main import create_app  # noqa: E402
from iesplan.storage.persistence import StoredObject  # noqa: E402,F401
from iesplan.tasks import queue  # noqa: E402
from iesplan.tasks.persistence import Task  # noqa: E402,F401

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

    def _override_request_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_request_db] = _override_request_db
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


def _registry(client: TestClient):
    """组合根已发布的生成器目录(随 app lifespan 装配; 测试只消费, 不组装)。"""
    return client.app.state.bootstrap_context.generator_registry


def _project(client: TestClient, user, name: str) -> int:
    resp = client.post(
        "/api/projects",
        json={
            "name": name,
            "currency": "CNY",
            "baseline_resolution": "1h",
            "baseline_start_year": 2026,
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
    assert flags == {"replay": False}
    assert task.status == "queued" and task.calc_snapshot_id is None

    # 事务提交证明: 新会话可见, 无需调用方 commit
    with _fresh_db(engine) as fresh:
        got = tasks_domain.get_task(fresh, task.id)
        assert got is not None and got.status == "queued"

    # 行为对照: 同类型任务关键字段一致
    old, old_flags = tasks_uc.submit_task(db, owner, pid, "report", idempotency_key="w2c-rpt-2")
    assert old_flags == {"replay": False}
    assert (old.type, old.status, old.calc_snapshot_id) == (task.type, task.status, task.calc_snapshot_id)


#: 计算配置正文(12 字段契约; 与 test_http_contract._CALC_PAYLOAD 同构)。
_CONFIG_BODY: dict = {
    "schema": "ies.calculation-config",
    "schema_version": "1.0.0",
    "mode": "planning",
    "predictions": [],
    "generator": "ies.generator.highs_milp@1.0.0",
    "solver": "ies.solver.highs@1.7.2",
    "precision": "medium",
    "convergence": None,
    "time_limit_seconds": 600,
    "options": {},
    "seed": None,
    "outputs": ["solution"],
}


#: 现行装配边界要求的显式计算声明(无静默默认): mode/generator/solver/
#: time_axis 缺一即阻断(ASM-CONV-001)。与 test_tasks_api.EXPLICIT_CALC_PATCH
#: 同值, 经 config.patch 草稿命令显式声明, 不得依赖回退。
EXPLICIT_CALC_PATCH = {
    "mode": "fixed_operation",
    "generator": "ies.generator.highs_milp@1.0.0",
    "solver": "ies.solver.highs@1.7.2",
    "time_axis": {"resolution": "1h", "start": "2025-01-01T00:00:00Z"},
}


def _declare_explicit_calc(client: TestClient, user, pid: int, revision: int = 1) -> int:
    """经 config.patch 显式声明计算装配字段, 返回新修订号。"""
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": revision, "commands": [{
            "id": "c-calc", "unit": "config", "type": "config.patch",
            "payload": EXPLICIT_CALC_PATCH,
        }]},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def _declare_finance(db: Session, user, pid: int) -> None:
    """装配闸门要求项目固定有效财务快照: 登记地区 Profile 并引用。"""
    from test_config_revisions import PROFILE_PAYLOAD

    from iesplan.application import configuration as config_cases

    config_cases.register_finance_profile(db, dict(PROFILE_PAYLOAD), user.id)
    config_cases.set_project_finance_profile(
        db, pid, PROFILE_PAYLOAD["profile"]["id"], user.id
    )


#: 无意图计算任务的装配声明占位(机械测试用；真实项目声明真实目标/绑定)。
#: finance_binding/tariff_bindings 经起草命令显式声明；planning 已改为冻结
#: PlanningConfig 修订的确定性投影(见 assembly_fixture)，不再经草稿声明。
def _assembly_commands(instances: dict) -> list:
    return [
        {
            "id": "c-finbind",
            "unit": "assembly",
            "type": "assembly.set_finance_binding",
            "payload": {"instances": instances},
        },
        {
            "id": "c-tariff",
            "unit": "assembly",
            "type": "assembly.set_tariff_bindings",
            "payload": {"bindings": {}},
        },
    ]


def _declare_assembly_sections(
    client: TestClient, user, pid: int, revision: int, instances: dict,
) -> int:
    """经 assembly.* 起草命令显式声明装配节, 返回新修订号。"""
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": revision, "commands": _assembly_commands(instances)},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def _declare_optimization_ready(
    client: TestClient, db: Session, user, pid: int,
) -> tuple:
    """计算任务提交前置: 显式财务引用 + 夹具设备/规划 + 显式计算声明 + 显式装配两节。

    返回(夹具设备装配实例引用, 草稿当前修订号)(调用方按需复用)。
    """
    from assembly_fixture import declare_compute_device, save_compute_planning

    _declare_finance(db, user, pid)
    device_ref, revision = declare_compute_device(client, pid, _h(client, user))
    save_compute_planning(db, pid, user.id, device_ref)
    revision = _declare_explicit_calc(client, user, pid, revision)
    revision = _declare_assembly_sections(
        client, user, pid, revision, {device_ref: {"type": "none"}}
    )
    return device_ref, revision


def test_submit_compute_task_snapshot_replay_and_reuse(
    client: TestClient, db: Session
) -> None:
    """计算任务提交: 快照装配 + 幂等重放 + 快照复用(不同键同输入共享快照)。

    无键提交由用户完整用例拒绝(宪法 §5.5), 不再有快照级任务去重。
    """
    from iesplan.application.configuration.calc_config import save_calculation_config_case

    owner = _user(db, "w2c_owner_opt")
    pid = _project(client, owner, "w2c-proj-opt")
    _declare_optimization_ready(client, db, owner, pid)
    saved = save_calculation_config_case(db, owner, pid, dict(_CONFIG_BODY), None)
    rev = saved["revision"]

    task, flags = tasks_uc.submit_task(
        db, owner, pid, "optimization",
        config={"config_revision": rev}, evaluation={"evaluation_years": 1, "discount_rate": 0.08}, idempotency_key="w2c-opt-1",
        generator_registry=_registry(client),
    )
    assert flags == {"replay": False}
    assert task.calc_snapshot_id is not None

    replay, rflags = tasks_uc.submit_task(
        db, owner, pid, "optimization",
        config={"config_revision": rev}, evaluation={"evaluation_years": 1, "discount_rate": 0.08}, idempotency_key="w2c-opt-1",
        generator_registry=_registry(client),
    )
    assert rflags == {"replay": True}
    assert replay.id == task.id

    # 不同幂等键、同输入 → 新任务, 但复用同一快照(快照去重保留)
    other, oflags = tasks_uc.submit_task(
        db, owner, pid, "optimization",
        config={"config_revision": rev}, evaluation={"evaluation_years": 1, "discount_rate": 0.08}, idempotency_key="w2c-opt-2",
        generator_registry=_registry(client),
    )
    assert oflags == {"replay": False}
    assert other.id != task.id
    assert other.calc_snapshot_id == task.calc_snapshot_id

    # 用户完整用例空键提交 → 拒绝(400, TASK-REQ-003, 权威用例唯一校验)
    from iesplan.tasks.contracts import InvalidRequestError

    with pytest.raises(InvalidRequestError) as exc:
        tasks_uc.submit_task_case(db, owner, pid, "optimization", idempotency_key="")
    assert exc.value.code == "TASK-REQ-003"


def test_submit_without_frozen_refs_fails_explicitly(
    client: TestClient, db: Session
) -> None:
    """无冻结引用即明确失败(公开提交路径)：缺财务块 → 缺规划块，各自 400。

    不读当前指针、不 fallback；与“静默默认/猜测”行为对立。
    """
    from iesplan.tasks.contracts import InvalidRequestError

    from iesplan.application.configuration.calc_config import save_calculation_config_case

    owner = _user(db, "w2c_owner_noref")
    pid = _project(client, owner, "w2c-proj-noref")
    saved = save_calculation_config_case(db, owner, pid, dict(_CONFIG_BODY), None)
    rev = saved["revision"]
    with pytest.raises(InvalidRequestError):
        tasks_uc.submit_task(
            db, owner, pid, "optimization",
            config={"config_revision": rev}, evaluation={"evaluation_years": 1, "discount_rate": 0.08}, idempotency_key="w2c-noref-1",
        generator_registry=_registry(client),
        )

    from test_config_revisions import PROFILE_PAYLOAD

    from iesplan.application import configuration as config_cases

    config_cases.register_finance_profile(db, dict(PROFILE_PAYLOAD), owner.id)
    config_cases.set_project_finance_profile(
        db, pid, PROFILE_PAYLOAD["profile"]["id"], owner.id
    )
    # 仅财务就绪、规划缺席 → 仍明确失败(规划引用缺失)
    with pytest.raises(InvalidRequestError):
        tasks_uc.submit_task(
            db, owner, pid, "optimization",
            config={"config_revision": rev}, evaluation={"evaluation_years": 1, "discount_rate": 0.08}, idempotency_key="w2c-noref-2",
        generator_registry=_registry(client),
        )


def test_restore_version_keeps_frozen_pins(client: TestClient, db: Session) -> None:
    """A→B→恢复 A：恢复版本内容、项目当前指针、新快照回执均为 A。

    全公开流程：公开提交冻结 v1(A)→公开移动指针到 B→公开恢复端点恢复 v1
    →公开读取与再次提交断言。不导入 application 下划线私有函数。
    恢复必须搬回完整 lineage：当前 Profile 行、Overrides、Effective、
    Planning 四个公开读数与项目四个指针全部属于 A，杜绝
    Profile B + Overrides B + Effective A 的自相矛盾状态。
    """
    import copy

    from test_config_revisions import PROFILE_PAYLOAD

    from iesplan import project as project_domain
    from iesplan.application import configuration as config_cases
    from iesplan.application import projects as projects_uc
    from iesplan.core.jsonutil import jsonable

    owner = _user(db, "w2c_owner_restore")
    pid = _project(client, owner, "w2c-proj-restore")
    device_ref, _ = _declare_optimization_ready(client, db, owner, pid)
    from iesplan.application.configuration.calc_config import save_calculation_config_case

    saved = save_calculation_config_case(db, owner, pid, dict(_CONFIG_BODY), None)
    task, _ = tasks_uc.submit_task(
        db, owner, pid, "optimization",
        config={"config_revision": saved["revision"]}, evaluation={"evaluation_years": 1, "discount_rate": 0.08}, idempotency_key="w2c-restore-1",
        generator_registry=_registry(client),
    )
    assert task.calc_snapshot_id is not None
    snap1 = tasks_domain.get_snapshot(db, task.calc_snapshot_id)
    assert snap1 is not None
    v1 = snap1.project_version_id

    profile_a = PROFILE_PAYLOAD["profile"]["id"]
    payload_b = copy.deepcopy(dict(PROFILE_PAYLOAD))
    payload_b["profile"] = dict(PROFILE_PAYLOAD["profile"], id="cn-north-demo-b")
    config_cases.register_finance_profile(db, payload_b, owner.id)
    eff_rev2, _, _ = config_cases.set_project_finance_profile(
        db, pid, "cn-north-demo-b", owner.id
    )
    assert eff_rev2 == 2
    _, plan_rev2 = config_cases.save_planning_config(
        db, pid,
        {
            "objective": {"sense": "minimize", "expression": "0"},
            "variables": {
                "cap2": {
                    "device_ref": device_ref,
                    "parameter": "rated_power_kw",
                    "year_offset": 0,
                    "lower_bound": "0",
                    "upper_bound": "100",
                    "unit": "kW",
                },
            },
            "constraints": {},
        },
        None, owner.id,
    )
    assert plan_rev2 == 2

    resp = client.post(
        f"/api/projects/{pid}/versions/{v1}/restore", headers=_h(client, owner)
    )
    assert resp.status_code == 200, resp.text
    restored = resp.json()["version"]
    assert restored["reason"] == "restore"
    assert restored["parent_version_id"] == v1

    ver = projects_uc.get_version(db, owner, pid, restored["id"])
    content = projects_uc.load_content_object(db, ver.content_object_id)
    assert content["effective_finance"] == {
        "profile_id": profile_a, "overrides_revision": 1, "revision": 1,
    }
    assert content["planning_config"] == {"revision": 1}

    # 公开读数: Profile 行 / Overrides / Effective / Planning 全部属于 A
    profile_row_a, _ = config_cases.get_finance_profile_by_ref(db, profile_a)
    assert profile_row_a.profile_id == profile_a
    _, ovr_rev = config_cases.get_finance_overrides(db, pid)
    assert ovr_rev == 1
    eff, eff_rev, _ = config_cases.get_effective_finance_config(db, pid)
    assert eff_rev == 1
    assert eff.profile_id == profile_a
    _, plan_rev, _ = config_cases.get_planning_config(db, pid)
    assert plan_rev == 1

    # 项目四个指针全部属于 A(无 B 残留)
    proj = project_domain.require_project(db, pid)
    assert proj.finance_profile_id == profile_row_a.id
    assert proj.overrides_revision == 1
    assert proj.effective_finance_revision == 1
    assert proj.planning_revision == 1

    task2, _ = tasks_uc.submit_task(
        db, owner, pid, "optimization",
        config={"config_revision": saved["revision"]}, evaluation={"evaluation_years": 1, "discount_rate": 0.08}, idempotency_key="w2c-restore-2",
        generator_registry=_registry(client),
    )
    assert task2.calc_snapshot_id is not None
    snap2 = tasks_domain.get_snapshot(db, task2.calc_snapshot_id)
    assert snap2 is not None
    receipt = jsonable(snap2.assembly_receipt)
    assert receipt["finance_snapshot"]["revision"] == 1
    assert receipt["finance_snapshot"]["profile_id"] == profile_a
    assert receipt["planning"]["variables"] == ["cap"]


def test_restore_version_without_finance_clears_pointers(
    client: TestClient, db: Session
) -> None:
    """恢复无财务/规划配置的早期版本 → 四指针全空，不残留 B。

    全公开流程：裸草稿固化 v_early(无配置块)→配置 A→移到 B(四指针非空)
    →公开恢复端点恢复 v_early→公开断言四指针全 None、恢复版本内容无
    配置块→公开提交按既有明确错误失败。不导入私有函数。
    """
    import copy

    from test_config_revisions import PROFILE_PAYLOAD

    from iesplan import project as project_domain
    from iesplan.application import configuration as config_cases
    from iesplan.application import projects as projects_uc
    from iesplan.tasks.contracts import InvalidRequestError

    owner = _user(db, "w2c_owner_restore_empty")
    pid = _project(client, owner, "w2c-proj-restore-empty")

    early = client.post(
        f"/api/projects/{pid}/versions",
        json={"name": "早期空白", "reason": "milestone"},
        headers=_h(client, owner),
    )
    assert early.status_code == 201, early.text
    v_early = early.json()["version"]["id"]

    device_ref, _ = _declare_optimization_ready(client, db, owner, pid)
    payload_b = copy.deepcopy(dict(PROFILE_PAYLOAD))
    payload_b["profile"] = dict(PROFILE_PAYLOAD["profile"], id="cn-north-demo-b2")
    config_cases.register_finance_profile(db, payload_b, owner.id)
    config_cases.set_project_finance_profile(db, pid, "cn-north-demo-b2", owner.id)
    _, plan_rev2 = config_cases.save_planning_config(
        db, pid,
        {
            "objective": {"sense": "minimize", "expression": "0"},
            "variables": {
                "cap2": {
                    "device_ref": device_ref,
                    "parameter": "rated_power_kw",
                    "year_offset": 0,
                    "lower_bound": "0",
                    "upper_bound": "100",
                    "unit": "kW",
                },
            },
            "constraints": {},
        },
        None, owner.id,
    )
    assert plan_rev2 == 2
    before = project_domain.require_project(db, pid)
    assert before.finance_profile_id is not None
    assert before.overrides_revision is not None
    assert before.effective_finance_revision is not None
    assert before.planning_revision is not None

    resp = client.post(
        f"/api/projects/{pid}/versions/{v_early}/restore", headers=_h(client, owner)
    )
    assert resp.status_code == 200, resp.text
    restored = resp.json()["version"]
    assert restored["reason"] == "restore"
    assert restored["parent_version_id"] == v_early

    after = project_domain.require_project(db, pid)
    assert after.finance_profile_id is None
    assert after.overrides_revision is None
    assert after.effective_finance_revision is None
    assert after.planning_revision is None

    ver = projects_uc.get_version(db, owner, pid, restored["id"])
    content = projects_uc.load_content_object(db, ver.content_object_id)
    assert "effective_finance" not in content
    assert "planning_config" not in content

    from iesplan.application.configuration.calc_config import save_calculation_config_case

    saved = save_calculation_config_case(db, owner, pid, dict(_CONFIG_BODY), None)
    # 引用合法但财务/规划引用已清空 → 仍明确失败(装配门禁, 非绑定问题)
    with pytest.raises(InvalidRequestError):
        tasks_uc.submit_task(
            db, owner, pid, "optimization",
            config={"config_revision": saved["revision"]}, evaluation={"evaluation_years": 1, "discount_rate": 0.08}, idempotency_key="w2c-early-1",
        generator_registry=_registry(client),
        )


def test_bind_unknown_dataset_version_fails_at_input(
    client: TestClient, db: Session
) -> None:
    """dataset.bind 不存在的版本 → PUT /draft 当场业务错误，不建新草稿。

    存在性与归属确认在命令输入边界经 dataset 公开门面完成；失败不推进
    草稿修订、不写入绑定。
    """
    owner = _user(db, "w2c_owner_badvid")
    pid = _project(client, owner, "w2c-proj-badvid")
    _, revision = _declare_optimization_ready(client, db, owner, pid)
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": revision, "commands": [{
            "id": "c-bind-bogus", "unit": "dataset", "type": "dataset.bind",
            "payload": {"dataset_version_id": 999999, "role": "main"},
        }]},
        headers=_h(client, owner),
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "PROJ-CMD-005"
    view = client.get(f"/api/projects/{pid}", headers=_h(client, owner)).json()
    assert view["draft"]["revision"] == revision
    assert view["draft"]["content"].get("dataset_bindings", []) == []


def test_submit_errors_match_old_codes(client: TestClient, db: Session) -> None:
    """非法类型/非法幂等键: 新旧错误码一致。"""
    owner = _user(db, "w2c_owner_err")
    pid = _project(client, owner, "w2c-proj-err")

    with pytest.raises(Exception) as exc:
        tasks_uc.submit_task(db, owner, pid, "nope", idempotency_key="w2c-err-1")
    assert exc.value.code == "TASK-REQ-002"
    db.rollback()

    with pytest.raises(Exception) as exc:
        tasks_uc.submit_task(db, owner, pid, "report", idempotency_key="bad key!")
    assert exc.value.code == "TASK-REQ-003"
    db.rollback()

    # analysis 缺 sweeps
    with pytest.raises(Exception) as exc_new:
        tasks_uc.submit_task(db, owner, pid, "analysis", config={}, idempotency_key="w2c-err-2")
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


def _fail_task(db: Session, task_id: int, *, code: str | None = None, message: str = "") -> None:
    """假执行器失败: running → failed(经 tasks 域门面: 尝试收尾 + 租约吊销 + 槽释放)。"""
    task = tasks_domain.get_task(db, task_id)
    if task is None:
        raise NotFoundError(
            "任务不存在",
            params={"task_id": task_id},
            location={"object_type": "task", "object_id": task_id},
        )
    if task.status == "failed":
        return
    tasks_domain.check_transition(task, "failed")
    outcome = "insufficient_evidence" if code == TASK_DATA_SNAPSHOT_MISSING else None
    attempt = tasks_domain.get_running_attempt(db, task.id)
    finished_attempt_id = attempt.id if attempt is not None else None
    if attempt is not None:
        tasks_domain.finish_attempt(db, attempt.id, "failed", stop_reason=code or "error")
        lease = tasks_domain.get_active_lease_for_attempt(db, attempt.id)
        if lease is not None:
            tasks_domain.release_lease(db, attempt.id, lease.lease_token, status="revoked")
        tasks_domain.release_slot(db, attempt.id)
    tasks_domain.set_task_status(db, task.id, "failed", business_outcome=outcome)
    tasks_domain.append_diagnostic(
        db, task_id=task.id, level=SEVERITY_ERROR, message=message,
        attempt_id=finished_attempt_id, code=code or TASK_SOLVE_FAILED,
        context={"outcome": outcome},
    )


def _fail(db: Session, task_id: int) -> None:
    """新用例领取 → 经 tasks 域门面将任务置终态(failed), 供重试对照。"""
    claim = tasks_uc.claim_task(db, task_id, "w2c-fail-exec")
    assert claim is not None
    _fail_task(db, task_id, code="TASK-SOLVE-001", message="w2c fail")
    db.commit()


def test_retry_terminal_task_consistent(client: TestClient, db: Session, engine: Engine) -> None:
    """重试: 终态 → queued(快照不变, 已提交); 重复落点一致; 非终态拒绝同码。"""
    from iesplan.application.configuration.calc_config import save_calculation_config_case

    owner = _user(db, "w2c_owner_retry")
    pid = _project(client, owner, "w2c-proj-retry")
    _declare_optimization_ready(client, db, owner, pid)
    saved = save_calculation_config_case(db, owner, pid, dict(_CONFIG_BODY), None)
    rev = saved["revision"]

    t1, _ = tasks_uc.submit_task(
        db, owner, pid, "optimization",
        config={"config_revision": rev}, evaluation={"evaluation_years": 1, "discount_rate": 0.08}, idempotency_key="w2c-t1",
        generator_registry=_registry(client),
    )
    snap = t1.calc_snapshot_id
    _fail(db, t1.id)
    retried = tasks_uc.retry_task(db, owner, t1.id)
    assert retried.status == "queued" and retried.calc_snapshot_id == snap
    with _fresh_db(engine) as fresh:
        assert tasks_domain.get_task(fresh, t1.id).status == "queued"

    t2, _ = tasks_uc.submit_task(
        db, owner, pid, "optimization",
        config={"config_revision": rev}, evaluation={"evaluation_years": 1, "discount_rate": 0.08}, idempotency_key="w2c-t2",
        generator_registry=_registry(client),
    )
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
