"""项目模型消费链回归: 已保存模型进入选择器/草稿/装配冻结。

原缺陷(修复前红色基线): 项目模型保存成功且清单可见, 但
- 画布选择器只消费公开目录, 已保存的 ``_N`` 类型不出现;
- ``model.upsert_device`` 只认目录注册, 引用 ``_N`` 类型一律 400
  PROJ-CMD-005;
- 装配闸门同样以未注册类型阻断, 已保存模型无法进入计算。

覆盖:
- 设备描述反序列化: 规范 ``to_dict`` 形态的纯结构还原(不重校验);
- 项目作用域选择器: 公开目录 ∪ 本项目已保存模型, 参数/真实端口完整;
- 建模命令输入边界: 本项目 ``_N`` 类型接受; 跨项目与未注册类型拒绝;
- 计算提交装配闸门: 快照冻结逐实例数学贡献/装配文本为项目模型真实
  身份与内容, 后续删除项目模型不改变既有快照(历史任务不读当前草稿)。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

# 单文件运行安全网: 固定 SQLite/内存队列(全量运行时由 conftest 先行覆盖)。
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
from iesplan.api.deps import get_request_db  # noqa: E402
from iesplan.application import tasks as tasks_uc  # noqa: E402
from iesplan.config import settings  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.core.yamlmini import load as yaml_load  # noqa: E402
from iesplan.devices import parse_device_model_v2, to_dict  # noqa: E402
from iesplan.devices.contracts2 import from_dict  # noqa: E402
from iesplan.identity.persistence import User  # noqa: E402,F401
from iesplan.main import create_app  # noqa: E402
from iesplan.storage.persistence import StoredObject  # noqa: E402,F401
from iesplan.tasks import queue  # noqa: E402
from iesplan.tasks.persistence import Task  # noqa: E402,F401

# ---------------------------------------------------------------------------
# 项目模型样例(消费侧恒定热负荷: 属性驱动 sink, 与 0.15 外部设备验收同模式,
# 独立稳定 ID/参数默认值)
# ---------------------------------------------------------------------------

SINK_TYPE_BASE = "acme.device.consumption_load"
SINK_TYPE_FINAL = f"{SINK_TYPE_BASE}_1"

AUX_TYPE_BASE = "acme.device.standby_load"
#: 编号是项目级单一序列(不按 base 分列): 本项目第二次保存拿到 suffix 2。
AUX_TYPE_FINAL = f"{AUX_TYPE_BASE}_2"

SINK_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device:
  id: acme.device.consumption_load
  names:
    zh-CN: 消费链负荷
    en-US: Consumption Load
properties:
  rated_demand_kw:
    value: 10
    unit: kW
    valid_range:
      minimum: 0
      maximum: 100000
interfaces:
  heat_in:
    type: in
    carrier: heat
    unit: kW
    valid_range:
      minimum: 0
      maximum: null
equations:
  variables: {}
  relations:
    - id: serve
      expression: "heat_in[t] = rated_demand_kw"
"""

#: 第二样例(同形态、不同稳定 ID; 引用重建语义回归)。
AUX_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device:
  id: acme.device.standby_load
  names:
    zh-CN: 备用负荷
    en-US: Standby Load
properties:
  rated_demand_kw:
    value: 5
    unit: kW
    valid_range:
      minimum: 0
      maximum: 100000
interfaces:
  heat_in:
    type: in
    carrier: heat
    unit: kW
    valid_range:
      minimum: 0
      maximum: null
equations:
  variables: {}
  relations:
    - id: serve
      expression: "heat_in[t] = rated_demand_kw"
"""

#: 计算配置正文(与 test_wave2_c_tasks._CONFIG_BODY 同构)。
CONFIG_BODY: dict = {
    "schema": "ies.calculation-config",
    "schema_version": "2.0.0",
    "mode": "planning",
    "predictions": [],
    "generator": "ies.generator.highs_milp@3.4.0",
    "solver": "ies.solver.highs@1.7.2",
    "precision": "medium",
    "convergence": None,
    "time_limit_seconds": 600,
    "options": {},
    "seed": None,
    "outputs": ["solution"],
}

EXPLICIT_CALC_PATCH = {
    "mode": "fixed_operation",
    "generator": "ies.generator.highs_milp@3.4.0",
    "solver": "ies.solver.highs@1.7.2",
    "time_axis": {"resolution": "1h", "start": "2025-01-01T00:00:00Z"},
}


# ---------------------------------------------------------------------------
# 测试环境(与 test_wave2_c_tasks 同模式: 内存 SQLite + 请求级会话替换)
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

    def _override_request_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_request_db] = _override_request_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _h(client: TestClient, user) -> dict[str, str]:
    return login_headers(client, user)


def _user(db: Session, name: str):
    """真实 UserRecord(供用例直接调用)。"""
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
            "baseline_start_year": 2026,
            "baseline_scenario_mode": "single",
        },
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["project"]["id"]


def _revision(client: TestClient, user, pid: int) -> int:
    view = client.get(f"/api/projects/{pid}", headers=_h(client, user)).json()
    return view["draft"]["revision"]


def _save_project_model(
    client: TestClient, user, pid: int, model_yaml: str = SINK_YAML
) -> dict:
    resp = client.post(
        f"/api/projects/{pid}/models",
        json={"model_yaml": model_yaml, "expected_revision": _revision(client, user, pid)},
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["project_model"]


def _upsert_device(
    client: TestClient, user, pid: int, name: str, device_type: str, params: dict | None = None
):
    return client.put(
        f"/api/projects/{pid}/draft",
        json={
            "expected_revision": _revision(client, user, pid),
            "commands": [
                {
                    "id": f"consume-{name}",
                    "unit": "model",
                    "type": "model.upsert_device",
                    "payload": {
                        "name": name,
                        "device_type": device_type,
                        "kind": "new",
                        "params": params or {},
                    },
                },
            ],
        },
        headers=_h(client, user),
    )


def _declare_finance(db: Session, user, pid: int) -> None:
    """装配闸门要求项目固定有效财务快照: 登记地区 Profile 并引用。"""
    from test_config_revisions import PROFILE_PAYLOAD

    from iesplan.application import configuration as config_cases

    config_cases.register_finance_profile(db, dict(PROFILE_PAYLOAD), user.id)
    config_cases.set_project_finance_profile(
        db, pid, PROFILE_PAYLOAD["profile"]["id"], user.id
    )


def _declare_calc(client: TestClient, user, pid: int, revision: int) -> int:
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={
            "expected_revision": revision,
            "commands": [
                {
                    "id": "consume-calc",
                    "unit": "config",
                    "type": "config.patch",
                    "payload": EXPLICIT_CALC_PATCH,
                },
            ],
        },
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def _declare_assembly(client: TestClient, user, pid: int, revision: int, instances: dict) -> int:
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={
            "expected_revision": revision,
            "commands": [
                {
                    "id": "consume-finbind",
                    "unit": "assembly",
                    "type": "assembly.set_finance_binding",
                    "payload": {"instances": instances},
                },
                {
                    "id": "consume-tariff",
                    "unit": "assembly",
                    "type": "assembly.set_tariff_bindings",
                    "payload": {"bindings": {}},
                },
            ],
        },
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def _registry(client: TestClient):
    """组合根已发布的生成器目录(随 app lifespan 装配)。"""
    return client.app.state.bootstrap_context.generator_registry


# ---------------------------------------------------------------------------
# 1) 规范文档反序列化(保存对象 → 不可变文档, 纯结构还原)
# ---------------------------------------------------------------------------


def test_project_device_document_deserialization_roundtrip() -> None:
    parsed = parse_device_model_v2(yaml_load(SINK_YAML), file="<sink>")
    assert parsed.ok, [d.to_dict() for d in parsed.diagnostics]
    assert parsed.document is not None
    assert from_dict(to_dict(parsed.document)) == parsed.document


# ---------------------------------------------------------------------------
# 2) 选择器 + 建模命令门禁(项目作用域)
# ---------------------------------------------------------------------------


def test_saved_model_enters_selector_and_draft(client: TestClient, db: Session) -> None:
    owner = _user(db, "consume_owner")
    pid = _project(client, owner, "消费链项目")
    model = _save_project_model(client, owner, pid)
    assert model["device_id"] == SINK_TYPE_FINAL

    # 项目作用域选择器: 目录项仍在, 本项目模型以其真实端口/参数出现。
    resp = client.get(f"/api/projects/{pid}/device-types", headers=_h(client, owner))
    assert resp.status_code == 200, resp.text
    items = {item["type_id"]: item for item in resp.json()["items"]}
    assert "ies.device.heat_pump" in items  # 目录设备不因项目作用域消失
    assert SINK_TYPE_FINAL in items  # 修复前缺失(红)
    sink = items[SINK_TYPE_FINAL]
    assert {p["name"] for p in sink["ports"]} == {"heat_in"}
    assert sink["parameters"]["rated_demand_kw"] == {
        "name": "rated_demand_kw", "unit": "kW", "min": 0.0, "max": 100000.0, "default": 10,
    }

    # 建模命令接受本项目模型类型(修复前 400 PROJ-CMD-005, 红)。
    resp = _upsert_device(client, owner, pid, "load2", SINK_TYPE_FINAL, {"rated_demand_kw": 20})
    assert resp.status_code == 200, resp.text
    devices = client.get(f"/api/projects/{pid}", headers=_h(client, owner)).json()[
        "draft"
    ]["content"]["model"]["devices"]
    assert devices == [
        {
            "name": "load2",
            "device_type": SINK_TYPE_FINAL,
            "kind": "new",
            "params": {"rated_demand_kw": 20},
        }
    ]

    # 目录设备行为不变(同项目直接接受)。
    resp = _upsert_device(client, owner, pid, "grid1", "ies.device.grid_connection")
    assert resp.status_code == 200, resp.text

    # 跨项目: 另一项目不得引用本项目 _N 类型, 选择器也不可见。
    pid_b = _project(client, owner, "消费链旁观项目")
    resp = _upsert_device(client, owner, pid_b, "load2", SINK_TYPE_FINAL)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "PROJ-CMD-005"
    items_b = {
        item["type_id"]
        for item in client.get(
            f"/api/projects/{pid_b}/device-types", headers=_h(client, owner)
        ).json()["items"]
    }
    assert SINK_TYPE_FINAL not in items_b
    assert "ies.device.heat_pump" in items_b

    # 未注册类型仍拒绝(既有边界不变)。
    resp = _upsert_device(client, owner, pid, "nope", "ies.device.nope")
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "PROJ-CMD-005"


# ---------------------------------------------------------------------------
# 3) 计算提交装配闸门: 冻结项目模型的真实身份/内容
# ---------------------------------------------------------------------------


def test_snapshot_freezes_project_model_math(
    client: TestClient, db: Session
) -> None:
    import json

    from iesplan.application.configuration.calc_config import save_calculation_config_case
    from iesplan.application import configuration as config_cases

    owner = _user(db, "consume_task_owner")
    pid = _project(client, owner, "消费链计算项目")
    _declare_finance(db, owner, pid)
    _save_project_model(client, owner, pid)

    resp = _upsert_device(client, owner, pid, "load2", SINK_TYPE_FINAL, {"rated_demand_kw": 20})
    assert resp.status_code == 200, resp.text
    revision = resp.json()["revision"]
    resp = _upsert_device(client, owner, pid, "gas1", "ies.device.gas_boiler")
    assert resp.status_code == 200, resp.text
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={
            "expected_revision": resp.json()["revision"],
            "commands": [
                {
                    "id": "consume-conn",
                    "unit": "model",
                    "type": "model.upsert_connection",
                    "payload": {
                        "name": "gas_to_load",
                        "from_device": "gas1",
                        "from_port": "heat_out",
                        "to_device": "load2",
                        "to_port": "heat_in",
                    },
                },
            ],
        },
        headers=_h(client, owner),
    )
    assert resp.status_code == 200, resp.text
    revision = resp.json()["revision"]

    config_cases.save_planning_config(
        db,
        pid,
        {
            "objective": {"sense": "minimize", "expression": "0"},
            "variables": {
                "gas_cap": {
                    "device_ref": "gas1",
                    "parameter": "rated_heat_kw",
                    "lower_bound": "10",
                    "upper_bound": "1000",
                    "unit": "kW",
                },
            },
            "constraints": {},
        },
        None,
        owner.id,
    )
    revision = _declare_calc(client, owner, pid, revision)
    revision = _declare_assembly(
        client, owner, pid, revision, {"load2": {"type": "none"}, "gas1": {"type": "none"}}
    )
    saved = save_calculation_config_case(db, owner, pid, dict(CONFIG_BODY), None)

    task, flags = tasks_uc.submit_task(
        db,
        owner,
        pid,
        "optimization",
        config={"config_revision": saved["revision"]},
        evaluation={
            "evaluation_years": 1,
            "construction_period": 0,
            "operation_years": 1,
            "discount_rate": 0.08,
            "construction_payments": [{"t": 0, "ratio": 1}],
            "maintenance_expenses": [],
        },
        idempotency_key="consume-task-1",
        generator_registry=_registry(client),
    )
    assert flags == {"replay": False}
    assert task.calc_snapshot_id is not None

    snapshot = tasks_domain.get_snapshot(db, task.calc_snapshot_id)
    assert snapshot is not None
    # 冻结的逐实例数学贡献来自项目模型本身(修复前类型未注册, 提交不可达)。
    frozen = snapshot.device_models["load2"]
    assert frozen["device_id"] == SINK_TYPE_FINAL
    assert frozen["variables"]["rated_demand_kw"]["unit"] == "kW"
    assert frozen["variables"]["rated_demand_kw"]["value"] == 10
    assert frozen["interfaces"]["heat_in"]["type"] == "in"
    assert frozen["interfaces"]["heat_in"]["carrier"] == "heat"
    assert any(
        ref["name"] == "rated_demand_kw"
        for relation in frozen["relations"]
        for ref in relation["rhs_refs"]
    )
    # 装配文本冻结实例级真实参数(20, 非模型默认 10)。
    assembly = json.loads(snapshot.canonical_assembly_text)
    assert assembly["devices"]["load2"]["definition"]["id"] == SINK_TYPE_FINAL
    assert assembly["devices"]["load2"]["properties"]["rated_demand_kw"]["value"] == 20
    # 依赖锁记录项目模型的契约版本。
    assert snapshot.assembly_receipt["dependencies"]["devices"][SINK_TYPE_FINAL] == "2.0.0"

    # 历史任务不读当前草稿: 删除项目模型后, 既有快照冻结内容原样保留。
    frozen_before = snapshot.device_models
    resp = client.request(
        "DELETE",
        f"/api/projects/{pid}/models/{model_id_of(client, owner, pid)}",
        json={"expected_revision": _revision(client, owner, pid)},
        headers=_h(client, owner),
    )
    assert resp.status_code == 200, resp.text
    db.expire_all()
    again = tasks_domain.get_snapshot(db, task.calc_snapshot_id)
    assert again is not None
    assert again.device_models == frozen_before


def model_id_of(client: TestClient, user, pid: int) -> int:
    models = client.get(f"/api/projects/{pid}/models", headers=_h(client, user)).json()[
        "project_models"
    ]
    assert len(models) == 1
    return int(models[0]["id"])


# ---------------------------------------------------------------------------
# 4) 消费边界 = 内容精确引用(非清单表): 版本恢复后引用集随之变化
# ---------------------------------------------------------------------------


def _selector_ids(client: TestClient, user, pid: int) -> set[str]:
    resp = client.get(f"/api/projects/{pid}/device-types", headers=_h(client, user))
    assert resp.status_code == 200, resp.text
    return {item["type_id"] for item in resp.json()["items"]}


def test_consumption_follows_content_model_refs_across_restore(
    client: TestClient, db: Session
) -> None:
    """恢复不含该模型的版本后, 后保存的模型不再进入选择器/命令边界。

    清单表仍保留该模型(项目资产), 但消费侧只认草稿/版本内容声明的
    ``project_models`` 引用; 若读清单表, 恢复后的项目会泄漏恢复点之后的
    模型引用(修复前红)。
    """
    owner = _user(db, "consume_refs_owner")
    pid = _project(client, owner, "内容引用项目")

    # 1) 先建基线版本: 其内容尚无 project_models 引用。
    resp = client.post(
        f"/api/projects/{pid}/versions",
        json={"name": "基线", "reason": "milestone"},
        headers=_h(client, owner),
    )
    assert resp.status_code == 201, resp.text
    baseline = resp.json()["version"]

    # 2) 之后保存模型: 草稿内容获得引用, 选择器可见且命令可引用。
    _save_project_model(client, owner, pid)
    assert SINK_TYPE_FINAL in _selector_ids(client, owner, pid)
    assert _upsert_device(client, owner, pid, "load2", SINK_TYPE_FINAL).status_code == 200

    # 3) 恢复基线版本: 草稿内容回到无引用状态。
    resp = client.post(
        f"/api/projects/{pid}/versions/{baseline['id']}/restore",
        headers=_h(client, owner),
    )
    assert resp.status_code == 200, resp.text
    view = client.get(f"/api/projects/{pid}", headers=_h(client, owner)).json()
    assert not view["draft"]["content"].get("project_models")

    # 修复前: 选择器仍出现该模型, upsert 仍 200; 修复后引用随之消失。
    assert SINK_TYPE_FINAL not in _selector_ids(client, owner, pid)
    resp = _upsert_device(client, owner, pid, "load2b", SINK_TYPE_FINAL)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "PROJ-CMD-005"
    # 目录设备不受影响。
    assert (
        _upsert_device(client, owner, pid, "grid1", "ies.device.grid_connection").status_code
        == 200
    )


# ---------------------------------------------------------------------------
# 5) 引用重建语义(澄清记录): 保存/删除按当前清单快照重建草稿引用
# ---------------------------------------------------------------------------


def test_save_or_delete_after_restore_rebuilds_refs_from_full_model_list(
    client: TestClient, db: Session
) -> None:
    """恢复之后任一次保存/删除都把引用重建为当前清单完整快照(既定语义)。

    恢复版本把草稿引用收窄为该版本内容声明的集合; 但保存路径(保存/删除
    模型)始终按项目模型清单重建引用 —— 清单中的既有资产会一并回到选择器
    与建模命令边界。本用例锚定该语义与其用户可见后果(撤销入口是清单资产,
    不新增启用/停用入口), 不视为缺陷修复。
    """
    owner = _user(db, "consume_rebuild_owner")
    pid = _project(client, owner, "引用重建项目")

    # 基线版本(无引用) → 保存两个模型 → 恢复基线: 引用清空。
    resp = client.post(
        f"/api/projects/{pid}/versions",
        json={"name": "基线", "reason": "milestone"},
        headers=_h(client, owner),
    )
    assert resp.status_code == 201, resp.text
    baseline = resp.json()["version"]
    first = _save_project_model(client, owner, pid)
    second = _save_project_model(client, owner, pid, AUX_YAML)
    assert first["device_id"] == SINK_TYPE_FINAL
    assert second["device_id"] == AUX_TYPE_FINAL
    resp = client.post(
        f"/api/projects/{pid}/versions/{baseline['id']}/restore",
        headers=_h(client, owner),
    )
    assert resp.status_code == 200, resp.text
    view = client.get(f"/api/projects/{pid}", headers=_h(client, owner)).json()
    assert not view["draft"]["content"].get("project_models")
    assert _selector_ids(client, owner, pid) & {SINK_TYPE_FINAL, AUX_TYPE_FINAL} == set()

    # 删除其一: 引用按删除后的清单重建 → 另一个资产回到选择器与命令边界。
    resp = client.request(
        "DELETE",
        f"/api/projects/{pid}/models/{second['id']}",
        json={"expected_revision": _revision(client, owner, pid)},
        headers=_h(client, owner),
    )
    assert resp.status_code == 200, resp.text
    refs = client.get(f"/api/projects/{pid}", headers=_h(client, owner)).json()["draft"][
        "content"
    ]["project_models"]
    assert [ref["device_id"] for ref in refs] == [SINK_TYPE_FINAL]
    assert SINK_TYPE_FINAL in _selector_ids(client, owner, pid)
    assert _upsert_device(client, owner, pid, "load3", SINK_TYPE_FINAL).status_code == 200
