"""项目模型保存用例(切片 dm2-A)集成测试。

覆盖(任务书第 6 条):
- 候选校验失败: 不落盘、不占号(清单行/编号计数器/对象引用均无变化);
- 校验通过: 项目内分配 _1、_2……(单调递增); _N 删除后不复用;
- 并发保存: 编号唯一(原子 UPDATE..RETURNING + 文件 SQLite 多连接);
- 写文件/DB/finalize 中途失败: 无半成品(事务回滚, 临时引用保留可恢复);
- 直接 YAML 与模板实例化汇合同一用例(同端点、同编号域、规范摘要一致);
- 幂等键重放: 返回同一逻辑结果, 不重复占号;
- reconciliation: 超龄临时数据文件解绑(幂等)。

测试环境: SQLite 文件/内存 + tmp 对象存储目录, 不依赖部署 Postgres;
create_app() 挂载全部业务路由(含 project_models), get_db 依赖替换。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

# 单文件运行安全网: 固定 SQLite(全量运行时已被其他测试模块先行导入, 无副作用)
os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")

import pytest  # noqa: E402
from auth_helpers import login_headers, make_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan.application.projects import (
    DataFileRef,
    reconcile_stale_temp_files,
    save_project_model,
    validate_candidate,
)
from iesplan.config import settings  # noqa: E402
from iesplan.db import Base, get_db  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.models.audit import AuditLog  # noqa: E402
from iesplan.models.identity import User  # noqa: E402
from iesplan.models.project import Project  # noqa: E402
from iesplan.models.project_model import ProjectModel, ProjectModelSequence  # noqa: E402
from iesplan.services import identity  # noqa: E402
from iesplan.services import project as project_service  # noqa: E402
from iesplan.storage import find_refs_by_entity_type, object_info  # noqa: E402

PASSWORD = "Test12345"

#: 合法 2.0.0 设备模型(阶段 1 契约样例)
HEAT_PUMP_YAML = """
schema: ies.device-model
schema_version: "2.0.0"

device:
  id: acme.device.heat_pump
  names:
    zh-CN: 热泵
    en-US: Heat Pump

properties:
  cop:
    value: 3.2
    unit: "1"
    valid_range:
      minimum: 1
      maximum: 10
  rated_heat_kw:
    value: 500
    unit: kW
    valid_range:
      minimum: 0
      maximum: 1000000

interfaces:
  electricity_in:
    type: in
    carrier: electricity
    unit: kW
    valid_range:
      minimum: 0
      maximum: null
  heat_out:
    type: out
    carrier: heat
    unit: kW
    valid_range:
      minimum: 0
      maximum: null
  fixed_temperature:
    type: predefined
    carrier: environment
    unit: "°C"
    valid_range:
      minimum: -50
      maximum: 60
    source:
      mode: constant
      value: 25
  unused_terminal:
    carrier: heat
    unit: kW
    valid_range:
      minimum: 0
      maximum: null

equations:
  variables: {}
  relations:
    - id: heat_conversion
      expression: "heat_out[t] = electricity_in[t] * cop"
"""

#: 多错误候选(interfaces type + property unit + equation 引用, 聚合 ≥3 条)
MULTI_ERROR_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.multi, names: {zh-CN: 多错, en-US: Multi}}
properties:
  p1: {value: 1, unit: not-a-unit, valid_range: {minimum: 0, maximum: 1}}
interfaces:
  i1: {type: magic, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations:
  variables: {}
  relations:
    - id: r1
      expression: "x[t] = unknown_var[t]"
"""

#: 模板样例(与等值直接 YAML 汇合; inputs 实例化后删除顶层 inputs)
TEMPLATE_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.electric_load, names: {zh-CN: 电负荷, en-US: Electric Load}}
inputs:
  properties:
    peak_power_kw:
      value:
        type: number
        unit: kW
        valid_range: {minimum: 0, maximum: 1000}
        default: 100
    is_switchable:
      value: {type: boolean, default: false}
properties:
  cop: {value: 3.0, unit: "1", valid_range: {minimum: 1, maximum: 10}}
interfaces:
  electric_demand:
    type: predefined
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: null}
    source: {mode: constant, value: 0}
equations:
  variables: {}
  relations: []
"""

#: 与模板实例化等值的直接 YAML
DIRECT_EQ_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.electric_load, names: {zh-CN: 电负荷, en-US: Electric Load}}
properties:
  cop: {value: 3.0, unit: "1", valid_range: {minimum: 1, maximum: 10}}
  peak_power_kw: {value: 250, unit: kW, valid_range: {minimum: 0, maximum: 1000}}
  is_switchable: {value: true, unit: "-"}
interfaces:
  electric_demand:
    type: predefined
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: null}
    source: {mode: constant, value: 0}
equations:
  variables: {}
  relations: []
"""

TEMPLATE_INPUTS = {"properties": {"peak_power_kw": {"value": 250}, "is_switchable": {"value": True}}}

DATA_MODEL_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.profile_load, names: {zh-CN: 曲线负荷, en-US: Profile Load}}
properties: {}
interfaces:
  electric_demand:
    type: predefined
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: 1000}
    source: {mode: data_repeat, data_ref: load_data}
equations: {variables: {}, relations: []}
"""


def _data_csv() -> bytes:
    from iesplan.core.yamlmini import load as yaml_load
    from iesplan.devices import content_sha256, parse_device_model_v2

    parsed = parse_device_model_v2(yaml_load(DATA_MODEL_YAML))
    assert parsed.document is not None
    lines = [
        "# schema: ies.device-data",
        "# schema_version: 2.0.0",
        "# dataset_id: test.load.profile",
        "# device_id: acme.device.profile_load",
        f"# device_content_sha256: {content_sha256(parsed.document)}",
        "# source_mode: data_repeat",
        "# resolution: 1h",
        "# period: day",
        "# unit.electric_demand: kW",
        "step,electric_demand",
        *[f"{step},{10 + step}" for step in range(24)],
    ]
    return ("\n".join(lines) + "\n").encode()


# ---------------------------------------------------------------------------
# 测试环境(与 test_project_api 同构)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    # 版本化迁移(宪法 §11; 与 init_db 发布路径一致, 台账幂等)
    from iesplan.migrations import apply_migrations

    apply_migrations(eng)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(engine: Engine) -> Iterator[None]:
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def db_session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


@pytest.fixture()
def client(engine: Engine, db_session: Session, tmp_path: Path) -> Iterator[TestClient]:
    settings.data_dir = tmp_path
    app = create_app()

    def _override_get_db() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    from iesplan.api.limits import reset_rate_limit

    reset_rate_limit()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _make_owner(client: TestClient, db: Session, name: str) -> tuple[dict, int]:
    user = make_user(db, name)
    headers = login_headers(client, user)
    resp = client.post("/api/projects", json={"name": f"{name} 项目"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return headers, resp.json()["project"]["id"]


def _headers_for(client: TestClient, db: Session, username: str) -> dict:
    user = make_user(db, username)
    return login_headers(client, user)


def _upload_temp(client: TestClient, pid: int, headers: dict, data_ref: str = "load_data") -> dict:
    resp = client.post(
        f"/api/projects/{pid}/models/temp-files",
        data={"data_ref": data_ref},
        files={"file": ("load.csv", _data_csv(), "text/csv")},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _save(
    client: TestClient, pid: int, headers: dict, yaml_text: str,
    *,
    source: str = "direct_yaml",
    template_inputs: dict | None = None,
    data_files: list[dict] | None = None,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
):
    if expected_revision is None:
        project = client.get(f"/api/projects/{pid}", headers=headers)
        assert project.status_code == 200, project.text
        expected_revision = project.json()["draft"]["revision"]
    body: dict = {
        "model_yaml": yaml_text,
        "source": source,
        "expected_revision": expected_revision,
    }
    if source == "template":
        body["template_inputs"] = template_inputs
    if data_files:
        body["data_files"] = data_files
    if idempotency_key:
        body["idempotency_key"] = idempotency_key
    return client.post(f"/api/projects/{pid}/models", json=body, headers=headers)


def _seq_rows(db: Session, pid: int) -> list:
    return list(
        db.execute(
            select(ProjectModelSequence).where(ProjectModelSequence.project_id == pid)
        ).scalars()
    )


def _model_rows(db: Session, pid: int) -> list:
    return list(
        db.execute(select(ProjectModel).where(ProjectModel.project_id == pid)).scalars()
    )


# ---------------------------------------------------------------------------
# 1. 候选校验端点(不保存)
# ---------------------------------------------------------------------------


def test_validate_endpoint_valid_candidate(client: TestClient, db_session: Session) -> None:
    headers, _pid = _make_owner(client, db_session, "val_owner")
    resp = client.post(
        f"/api/projects/{_pid}/models/validate",
        json={"model_yaml": HEAT_PUMP_YAML},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is True
    assert body["diagnostics"] == []


def test_validate_endpoint_aggregated_diagnostics(client: TestClient, db_session: Session) -> None:
    headers, pid = _make_owner(client, db_session, "val_agg")
    resp = client.post(
        f"/api/projects/{pid}/models/validate",
        json={"model_yaml": MULTI_ERROR_YAML},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is False
    diags = body["diagnostics"]
    assert len(diags) >= 3
    for d in diags:
        assert d["code"]
        assert d["message_key"]
        assert "field" in d["location"]
    # 至少一条诊断携带 expected/actual(设备契约诊断带字段路径, 部分仅 detail;
    # 本切片新增 PROJ-MDL 码全部带 expected/actual, 见数据文件校验用例)
    assert any(
        "expected" in d["params"] or "actual" in d["params"] for d in diags
    )
    codes = {d["code"] for d in diags}
    assert "SYS-CFG-001" in codes  # devices 2.0 契约诊断(parser2)


def test_validate_endpoint_yaml_parse_error(client: TestClient, db_session: Session) -> None:
    headers, pid = _make_owner(client, db_session, "val_yaml")
    # 重复键: yamlmini 安全子集在解析层拒绝(YAML 1.2 安全子集要求)
    bad_yaml = HEAT_PUMP_YAML.replace(
        "schema: ies.device-model", "schema: ies.device-model\nschema: ies.device-model"
    )
    resp = client.post(
        f"/api/projects/{pid}/models/validate",
        json={"model_yaml": bad_yaml},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    diags = resp.json()["diagnostics"]
    assert any(d["code"] == "PROJ-MDL-006" for d in diags)


def test_validate_endpoint_template(client: TestClient, db_session: Session) -> None:
    headers, pid = _make_owner(client, db_session, "val_tpl")
    resp = client.post(
        f"/api/projects/{pid}/models/validate",
        json={"model_yaml": TEMPLATE_YAML, "source": "template", "template_inputs": TEMPLATE_INPUTS},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is True, body["diagnostics"]
    # 未声明字段拒绝: inputs 中不存在键提交 → 诊断
    resp2 = client.post(
        f"/api/projects/{pid}/models/validate",
        json={
            "model_yaml": TEMPLATE_YAML, "source": "template",
            "template_inputs": {"properties": {"not_declared": {"value": 1}}},
        },
        headers=headers,
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["valid"] is False


def test_validate_data_file_missing_and_digest(client: TestClient, db_session: Session) -> None:
    headers, pid = _make_owner(client, db_session, "val_data")
    up = _upload_temp(client, pid, headers)
    ref = {
        "data_ref": "load_data",
        "upload_id": str(up["upload_id"]),
        "object_id": up["temp_file"]["object_id"],
        "sha256": up["temp_file"]["sha256"],
    }
    # 正确引用: valid
    resp = client.post(
        f"/api/projects/{pid}/models/validate",
        json={"model_yaml": DATA_MODEL_YAML, "data_files": [ref]},
        headers=headers,
    )
    assert resp.json()["valid"] is True, resp.json()
    # 摘要不一致 → PROJ-MDL-002
    bad_digest = dict(ref, sha256="0" * 64)
    resp = client.post(
        f"/api/projects/{pid}/models/validate",
        json={"model_yaml": DATA_MODEL_YAML, "data_files": [bad_digest]},
        headers=headers,
    )
    codes = [d["code"] for d in resp.json()["diagnostics"]]
    assert "PROJ-MDL-002" in codes
    # 对象不存在 → PROJ-MDL-001
    missing = dict(ref, object_id="99999999")
    resp = client.post(
        f"/api/projects/{pid}/models/validate",
        json={"model_yaml": DATA_MODEL_YAML, "data_files": [missing]},
        headers=headers,
    )
    codes = [d["code"] for d in resp.json()["diagnostics"]]
    assert "PROJ-MDL-001" in codes
    # 归属不一致(错误 upload_id)→ PROJ-MDL-003
    wrong_owner = dict(ref, upload_id="123456")
    resp = client.post(
        f"/api/projects/{pid}/models/validate",
        json={"model_yaml": DATA_MODEL_YAML, "data_files": [wrong_owner]},
        headers=headers,
    )
    codes = [d["code"] for d in resp.json()["diagnostics"]]
    assert "PROJ-MDL-003" in codes


def test_validate_requires_project_view(client: TestClient, db_session: Session) -> None:
    headers, pid = _make_owner(client, db_session, "val_owner2")
    other = _headers_for(client, db_session, "val_intruder")
    resp = client.post(
        f"/api/projects/{pid}/models/validate",
        json={"model_yaml": HEAT_PUMP_YAML},
        headers=other,
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# 2. 正式保存: 成功路径与编号
# ---------------------------------------------------------------------------


def test_save_success_direct_yaml(client: TestClient, db_session: Session) -> None:
    headers, pid = _make_owner(client, db_session, "sv_ok")
    resp = _save(client, pid, headers, HEAT_PUMP_YAML)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == {"project_model", "receipt", "project_revision", "duplicate"}
    assert body["project_revision"] == 2
    model = body["project_model"]
    assert model["device_id"] == "acme.device.heat_pump_1"
    assert model["base_device_id"] == "acme.device.heat_pump"
    assert model["suffix"] == 1
    assert model["source"] == "direct_yaml"
    assert model["revision"] == 1
    assert len(model["content_sha256"]) == 64
    assert body["receipt"]["content_sha256"] == model["content_sha256"]
    assert body["receipt"]["schema"] == "ies.device-model"
    assert body["duplicate"] is False
    # 清单行 + 编号计数器 + 对象引用 + 审计
    rows = _model_rows(db_session, pid)
    assert len(rows) == 1
    seq = _seq_rows(db_session, pid)
    assert seq[0].next_suffix == 2
    owner_refs = find_refs_by_entity_type(db_session, "project_model")
    assert len(owner_refs) == 2  # model_yaml + receipt
    audit = db_session.execute(
        select(AuditLog).where(AuditLog.entity_type == "project_model")
    ).scalars().all()
    assert len(audit) == 1
    assert audit[0].action == "project_model.created"
    # 清单端点可见编号
    resp_list = client.get(f"/api/projects/{pid}/models", headers=headers)
    assert resp_list.status_code == 200
    assert [m["suffix"] for m in resp_list.json()["project_models"]] == [1]


def test_save_with_temp_data_file_finalize(client: TestClient, db_session: Session) -> None:
    headers, pid = _make_owner(client, db_session, "sv_data")
    up = _upload_temp(client, pid, headers)
    ref = {
        "data_ref": "load_data",
        "upload_id": str(up["upload_id"]),
        "object_id": up["temp_file"]["object_id"],
        "sha256": up["temp_file"]["sha256"],
    }
    resp = _save(client, pid, headers, DATA_MODEL_YAML, data_files=[ref])
    assert resp.status_code == 201, resp.text
    # 临时引用已解绑; 数据对象转为最终 owner(purpose=data:load_data)
    assert find_refs_by_entity_type(db_session, "project_model_temp") == []
    final_refs = find_refs_by_entity_type(db_session, "project_model")
    data_purposes = [r["purpose"] for r in final_refs]
    assert "data:load_data" in data_purposes
    handle = object_info(db_session, int(ref["object_id"]))
    assert handle["status"] == "stored"
    assert handle["ref_count"] == 1


def test_save_validation_failure_no_save_no_number(client: TestClient, db_session: Session) -> None:
    headers, pid = _make_owner(client, db_session, "sv_fail")
    resp = _save(client, pid, headers, MULTI_ERROR_YAML)
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert set(body) == {"error"}
    err = body["error"]
    assert set(err) == {
        "code", "message_key", "severity", "blocking",
        "params", "location", "fix_hint_key", "ref_ids",
    }
    assert err["code"] == "PROJ-MDL-005"
    assert err["message_key"] == "ies.diag.proj.model_validation_failed"
    diags = err["params"]["diagnostics"]
    assert len(diags) >= 3
    # 不落盘 / 不占号 / 无对象引用 / 无审计
    assert _model_rows(db_session, pid) == []
    assert _seq_rows(db_session, pid) == []
    assert find_refs_by_entity_type(db_session, "project_model") == []
    assert find_refs_by_entity_type(db_session, "project_model_temp") == []
    audit = db_session.execute(
        select(AuditLog).where(AuditLog.entity_type == "project_model")
    ).scalars().all()
    assert audit == []


def test_save_yaml_parse_failure_no_number(client: TestClient, db_session: Session) -> None:
    headers, pid = _make_owner(client, db_session, "sv_yaml_fail")
    bad_yaml = HEAT_PUMP_YAML.replace("cop:", "cop:\n  cop:")
    resp = _save(client, pid, headers, bad_yaml)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "PROJ-MDL-005"
    codes = [d["code"] for d in resp.json()["error"]["params"]["diagnostics"]]
    assert "PROJ-MDL-006" in codes
    assert _seq_rows(db_session, pid) == []


def test_numbering_monotonic_and_delete_no_reuse(client: TestClient, db_session: Session) -> None:
    headers, pid = _make_owner(client, db_session, "sv_num")
    r1 = _save(client, pid, headers, HEAT_PUMP_YAML)
    r2 = _save(client, pid, headers, HEAT_PUMP_YAML)
    assert r1.status_code == r2.status_code == 201
    m1 = r1.json()["project_model"]
    m2 = r2.json()["project_model"]
    assert m1["device_id"] == "acme.device.heat_pump_1"
    assert m2["device_id"] == "acme.device.heat_pump_2"
    assert m1["content_sha256"] != m2["content_sha256"]  # 最终 ID 不同 → 摘要不同
    # 删除 _2 → 编号不复用: 下一次保存是 _3
    current = client.get(f"/api/projects/{pid}", headers=headers).json()["draft"]["revision"]
    resp_del = client.request(
        "DELETE",
        f"/api/projects/{pid}/models/{m2['id']}",
        json={"expected_revision": current},
        headers=headers,
    )
    assert resp_del.status_code == 200, resp_del.text
    assert resp_del.json()["deleted"] == m2["id"]
    r3 = _save(client, pid, headers, HEAT_PUMP_YAML)
    assert r3.status_code == 201, r3.text
    assert r3.json()["project_model"]["device_id"] == "acme.device.heat_pump_3"
    rows = _model_rows(db_session, pid)
    assert {m["suffix"] for m in [r1.json()["project_model"], r3.json()["project_model"]]} == {1, 3}
    assert len(rows) == 2
    # 删除后: 被删模型的两个对象(model_yaml + receipt)的最终 owner 引用解绑
    # (对象进入孤儿生命周期), 其余清单行引用不受影响。
    # 注意: SQLite 测试库 INTEGER 主键在删除后可能复用行号(Postgres
    # GENERATED ALWAYS AS IDENTITY 不复用), 因此按对象 id 断言而非行 id。
    final_refs = find_refs_by_entity_type(db_session, "project_model")
    for obj_id in (m2["model_object_id"], m2["receipt_object_id"]):
        assert not any(str(r["object_id"]) == obj_id for r in final_refs), f"被删模型对象 {obj_id} 仍有引用"
    assert len(final_refs) == 4  # 模型 _1 与 _3 各 2 个引用
    audit_actions = [
        a.action
        for a in db_session.execute(select(AuditLog).where(AuditLog.entity_type == "project_model")).scalars()
    ]
    assert audit_actions.count("project_model.created") == 3
    assert audit_actions.count("project_model.deleted") == 1


def test_save_duplicate_device_id_conflict(client: TestClient, db_session: Session) -> None:
    """同基础 ID 并发冲突由编号域吸收: 顺序保存各得 _N, 不冲突。"""
    headers, pid = _make_owner(client, db_session, "sv_dup")
    assert _save(client, pid, headers, HEAT_PUMP_YAML).status_code == 201
    assert _save(client, pid, headers, HEAT_PUMP_YAML).status_code == 201


def test_idempotency_replay(client: TestClient, db_session: Session) -> None:
    headers, pid = _make_owner(client, db_session, "sv_idem")
    key = "idem-key-0001"
    r1 = _save(client, pid, headers, HEAT_PUMP_YAML, idempotency_key=key)
    assert r1.status_code == 201, r1.text
    assert r1.json()["duplicate"] is False
    r2 = _save(client, pid, headers, HEAT_PUMP_YAML, idempotency_key=key)
    assert r2.status_code == 201, r2.text
    body2 = r2.json()
    assert body2["duplicate"] is True
    assert body2["project_model"]["id"] == r1.json()["project_model"]["id"]
    assert body2["receipt"]["content_sha256"] == r1.json()["receipt"]["content_sha256"]
    # 不重复占号
    assert len(_model_rows(db_session, pid)) == 1
    assert _seq_rows(db_session, pid)[0].next_suffix == 2


def test_save_permissions_and_project_state(client: TestClient, db_session: Session) -> None:
    headers, pid = _make_owner(client, db_session, "sv_perm")
    viewer = _headers_for(client, db_session, "sv_viewer")
    # 非成员保存/校验 → 403(权限在读取敏感内容前失败)
    assert _save(client, pid, viewer, HEAT_PUMP_YAML, expected_revision=1).status_code == 403
    assert client.post(
        f"/api/projects/{pid}/models/validate", json={"model_yaml": HEAT_PUMP_YAML},
        headers=viewer,
    ).status_code == 403
    # 归档项目 → 保存 409
    resp = client.post(f"/api/projects/{pid}/archive", headers=headers)
    assert resp.status_code == 200, resp.text
    assert _save(client, pid, headers, HEAT_PUMP_YAML).status_code == 409


# ---------------------------------------------------------------------------
# 3. 模板与直接 YAML 汇合同一用例
# ---------------------------------------------------------------------------


def test_template_and_direct_yaml_converge(client: TestClient, db_session: Session) -> None:
    headers, pid = _make_owner(client, db_session, "sv_conv")
    r_direct = _save(client, pid, headers, DIRECT_EQ_YAML)
    r_tpl = _save(
        client, pid, headers, TEMPLATE_YAML,
        source="template", template_inputs=TEMPLATE_INPUTS,
    )
    assert r_direct.status_code == 201, r_direct.text
    assert r_tpl.status_code == 201, r_tpl.text
    # 同一用例、同一编号域: _1 / _2
    assert r_direct.json()["project_model"]["suffix"] == 1
    assert r_tpl.json()["project_model"]["suffix"] == 2
    assert r_tpl.json()["project_model"]["source"] == "template"
    assert r_direct.json()["project_model"]["template_sha256"] is None
    assert len(r_tpl.json()["project_model"]["template_sha256"]) == 64
    assert len(r_tpl.json()["project_model"]["inputs_sha256"]) == 64
    # 等值语义: 候选校验级规范摘要一致(去后缀后的基础摘要);
    # 回执结构按来源不同(模板回执含 instantiator/追溯摘要), 规范摘要必须一致
    owner = identity_user(db_session, "sv_conv")
    v_direct = validate_candidate(
        db_session, owner, pid, model_yaml=DIRECT_EQ_YAML, source="direct_yaml",
    )
    v_tpl = validate_candidate(
        db_session, owner, pid,
        model_yaml=TEMPLATE_YAML, source="template", template_inputs=TEMPLATE_INPUTS,
    )
    assert v_direct.ok and v_tpl.ok
    assert v_direct.content_sha256 == v_tpl.content_sha256
    assert v_direct.receipt["content_sha256"] == v_tpl.receipt["content_sha256"]
    assert v_tpl.receipt["instantiator"] == "ies.device-model.instantiator@1.0.0"
    assert "instantiator" not in v_direct.receipt


def identity_user(db_session: Session, username: str):
    from iesplan.models.identity import User

    return db_session.execute(select(User).where(User.username == username)).scalar_one()


# ---------------------------------------------------------------------------
# 4. 中途失败: 无半成品、临时引用可恢复
# ---------------------------------------------------------------------------


def test_save_failure_midway_no_half_state(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, pid = _make_owner(client, db_session, "sv_crash")
    import iesplan.application.projects.model_save as ms

    calls = {"n": 0}
    real_put = ms.put_object

    def _flaky_put(db_, content, content_type, source_category, **_kw):
        # 第 2 次 put(回执对象)时抛错: 模型对象已写盘但清单/编号尚未提交
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated storage failure")
        return real_put(db_, content, content_type, source_category, **_kw)

    monkeypatch.setattr(ms, "put_object", _flaky_put)
    owner = identity_user(db_session, "sv_crash")
    with pytest.raises(RuntimeError, match="simulated storage failure"):
        save_project_model(
            db_session, owner, pid, model_yaml=HEAT_PUMP_YAML, expected_revision=1
        )
    # API 层不提交 → 事务整体回滚: 无清单行、无编号、无对象引用、无审计
    # (回滚后再断言: 未提交行只对会话可见, 对用户不可见)
    db_session.rollback()
    assert _model_rows(db_session, pid) == []
    assert _seq_rows(db_session, pid) == []
    assert find_refs_by_entity_type(db_session, "project_model") == []
    assert find_refs_by_entity_type(db_session, "project_model_temp") == []
    assert db_session.execute(
        select(AuditLog).where(AuditLog.entity_type == "project_model")
    ).scalars().all() == []
    # 会话未被污染: 正常保存成功, 编号从 1 开始
    assert _save(client, pid, headers, HEAT_PUMP_YAML).status_code == 201
    assert _seq_rows(db_session, pid)[0].next_suffix == 2


def test_save_failure_during_finalize_keeps_temp_owner(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """finalize(detach 临时 owner)中途失败: 临时引用保留(可 reconciliation),
    清单/编号整体回滚。"""
    headers, pid = _make_owner(client, db_session, "sv_finalize")
    up = _upload_temp(client, pid, headers)
    ref = {
        "data_ref": "load_data",
        "upload_id": str(up["upload_id"]),
        "object_id": up["temp_file"]["object_id"],
        "sha256": up["temp_file"]["sha256"],
    }
    import iesplan.application.projects.model_save as ms

    def _explode_detach(db_, object_id, ref_type, ref_id, **_kw):
        raise RuntimeError("simulated finalize failure")

    monkeypatch.setattr(ms, "detach", _explode_detach)
    owner = identity_user(db_session, "sv_finalize")
    ref_obj = DataFileRef(
        data_ref="load_data",
        upload_id=int(up["upload_id"]),
        object_id=int(up["temp_file"]["object_id"]),
        sha256=up["temp_file"]["sha256"],
    )
    with pytest.raises(RuntimeError, match="simulated finalize failure"):
        save_project_model(
            db_session, owner, pid,
            model_yaml=DATA_MODEL_YAML,
            data_files=(ref_obj,),
            expected_revision=1,
        )
    db_session.rollback()
    # 清单/编号未提交; 数据对象仍属临时 owner(可恢复)
    assert _model_rows(db_session, pid) == []
    assert _seq_rows(db_session, pid) == []
    temp_refs = find_refs_by_entity_type(db_session, "project_model_temp")
    assert len(temp_refs) == 1
    assert str(temp_refs[0]["object_id"]) == up["temp_file"]["object_id"]
    assert find_refs_by_entity_type(db_session, "project_model") == []
    # 撤销 detach 故障注入: 保存失败后临时文件仍可用, 正常保存成功
    monkeypatch.undo()
    resp = _save(client, pid, headers, DATA_MODEL_YAML, data_files=[ref])
    assert resp.status_code == 201, resp.text
    assert find_refs_by_entity_type(db_session, "project_model_temp") == []


# ---------------------------------------------------------------------------
# 5. reconciliation: 超龄临时数据文件解绑(幂等)
# ---------------------------------------------------------------------------


def test_reconcile_stale_temp_files(client: TestClient, db_session: Session) -> None:
    headers, pid = _make_owner(client, db_session, "sv_rec")
    up = _upload_temp(client, pid, headers)
    # 保留期内不清理
    report = reconcile_stale_temp_files(db_session, dry_run=True)
    assert report["stale_count"] == 0
    assert report["kept_count"] == 1
    # 超龄(0 分钟) → dry_run 报告 + 执行解绑
    report2 = reconcile_stale_temp_files(db_session, older_than=timedelta(minutes=0), dry_run=True)
    assert report2["stale_count"] == 1
    report3 = reconcile_stale_temp_files(db_session, older_than=timedelta(minutes=0), dry_run=False)
    assert report3["stale_count"] == 1
    assert find_refs_by_entity_type(db_session, "project_model_temp") == []
    handle = object_info(db_session, int(up["temp_file"]["object_id"]))
    assert handle["status"] == "orphaned"
    # 幂等: 再次执行无副作用
    report4 = reconcile_stale_temp_files(db_session, older_than=timedelta(minutes=0), dry_run=False)
    assert report4["stale_count"] == 0
    # 已解绑对象重新上传引用可恢复(对象未物理删)
    up2 = _upload_temp(client, pid, headers)
    assert up2["temp_file"]["object_id"] == up["temp_file"]["object_id"]


# ---------------------------------------------------------------------------
# 6. 并发编号唯一(文件 SQLite 多连接 + 线程)
# ---------------------------------------------------------------------------


def test_concurrent_numbering_unique(tmp_path: Path) -> None:
    from sqlalchemy.orm import sessionmaker as _sessionmaker

    settings.data_dir = tmp_path
    db_path = tmp_path / "concurrent.db_session"
    eng = create_engine(
        f"sqlite+pysqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(eng)
    from iesplan.migrations import apply_migrations

    apply_migrations(eng)
    factory = _sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)

    with factory() as s:
        user = identity.create_user(
            s, "conc_owner", PASSWORD, role="engineer", force_password_change=False,
            display_name="并发所有者",
        )
        s.commit()
    with factory() as s:
        u = s.get(User, user.id)
        project = project_service.create_project(s, u, "并发项目")
        s.commit()
        pid = project.id

    results: list[dict] = []
    errors: list[Exception] = []

    def _worker() -> None:
        try:
            from iesplan.core.errors import ConflictError

            for _attempt in range(10):
                with factory() as s:
                    u = s.get(User, user.id)
                    project = s.get(Project, pid)
                    revision = project_service.get_current_draft(s, project).revision
                    try:
                        result = save_project_model(
                            s,
                            u,
                            pid,
                            model_yaml=HEAT_PUMP_YAML,
                            source="direct_yaml",
                            expected_revision=revision,
                        )
                    except ConflictError:
                        continue
                    results.append(result)
                    return
            raise AssertionError("并发 revision 冲突重试耗尽")
        except Exception as exc:  # noqa: BLE001 - 线程内收集
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    suffixes = sorted(r["project_model"]["suffix"] for r in results)
    device_ids = [r["project_model"]["device_id"] for r in results]
    # 并发唯一: 恰好 {1,2,3}, 无重复
    assert suffixes == [1, 2, 3]
    assert len(set(device_ids)) == 3
    with factory() as s:
        seq = s.execute(
            select(ProjectModelSequence).where(ProjectModelSequence.project_id == pid)
        ).scalar_one()
        assert seq.next_suffix == 4
