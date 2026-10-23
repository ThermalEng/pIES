"""S1 修正: 模板数据输入 → 项目数据集版本 → 装配绑定 → 消费。

证明 0.15 S1 修正后的生产链:
1. 模板数据叶子必填(缺失即拒绝, 无隐式回退);
2. 数据值是项目数据集版本的公开装配路径, 不在本项目即明确失败;
3. 回执只记录模板来源溯源; 装配来源事实由设备实例 params 显式持有
   (与页面实例来源编辑同一 model.upsert_device 入口), 构造器不读取回执;
4. 数据 CSV 的 device_id 是稳定基础 ID, 不要求用户猜项目作用域 _N 编号。

只使用自造合成 CSV 与临时 SQLite/对象目录; 不依赖部署环境。
"""

from __future__ import annotations

import os

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from iesplan import project as project_domain
from iesplan.application.models.project_devices import (
    load_project_device_base_ids,
    load_project_device_documents,
)
from iesplan.application.projects.content_objects import load_content_object
from iesplan.assembly import build_assembly_doc_from_content
from iesplan.assembly.builder import dataset_export_path
from iesplan.core.namespace import build_stable_id

from test_project_model_save import (  # noqa: E402
    _make_owner,
    _publish_template,
    _save,
    _token_ns_map,
    client,  # noqa: F401  (pytest fixture)
    db_session,  # noqa: F401  (pytest fixture)
    engine,  # noqa: F401  (pytest fixture)
)

TEMPLATE_YAML = """
schema: ies.device-model
schema_version: "2.1.0"
device: {id: acme.device.electric_load, names: {zh-CN: 电负荷, en-US: Electric Load}}
inputs:
  properties:
    peak_power_kw:
      value: {type: number, unit: kW, valid_range: {minimum: 0, maximum: 10000}}
  interfaces:
    electric_demand: {type: data_repeat}
properties:
  peak_power_kw:
    value: 100
    unit: kW
    valid_range: {minimum: 0, maximum: 10000}
interfaces:
  electric_demand:
    type: predefined
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: null}
  electricity_in:
    type: in
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: null}
equations:
  variables: {}
  relations:
    - id: serve
      expression: "electricity_in[t] = electric_demand[t]"
"""


def _dd_csv(device_id: str, mode: str = "data_repeat") -> bytes:
    """自造 1h 完整日(24 点)设备数据 CSV（模式可切换为 data_full/data_predict）。"""
    header = "step,electric_demand"
    rows = [f"{i},{10 + (i % 3)}" for i in range(24)]
    lines = [
        "# schema: ies.device-data",
        "# schema_version: 2.1.0",
        "# dataset_id: synth_template_data",
        f"# device_id: {device_id}",
        f"# source_mode: {mode}",
        "# resolution: 1h",
        "# unit.electric_demand: kW",
        header,
        *rows,
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _upload_version(client, headers, pid: int, device_id: str, mode: str = "data_repeat") -> int:
    resp = client.post(
        f"/api/projects/{pid}/datasets",
        json={"name": "模板数据", "source_category": "user_upload", "license": "CC-BY-4.0"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    ds_id = int(resp.json()["dataset"]["id"])
    resp = client.post(
        f"/api/projects/{pid}/datasets/{ds_id}/versions",
        data={"resolution": "1h", "utc_offset_minutes": "480"},
        files={"file": ("load.csv", _dd_csv(device_id, mode), "text/csv")},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["dataset_version"]["id"])


def _bind_dataset(client, headers, pid: int, vid: int) -> int:
    project = client.get(f"/api/projects/{pid}", headers=headers)
    revision = int(project.json()["draft"]["revision"])
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={
            "expected_revision": revision,
            "commands": [
                {
                    "id": "bind-template-data",
                    "unit": "dataset",
                    "type": "dataset.bind",
                    "payload": {"dataset_version_id": vid, "role": "main"},
                }
            ],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return int(resp.json()["revision"])


def _add_device(
    client, headers, pid: int, name: str, device_type: str, params: dict | None = None,
) -> None:
    project = client.get(f"/api/projects/{pid}", headers=headers)
    revision = int(project.json()["draft"]["revision"])
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={
            "expected_revision": revision,
            "commands": [
                {
                    "id": f"add-{name}",
                    "unit": "model",
                    "type": "model.upsert_device",
                    "payload": {
                        "name": name,
                        "device_type": device_type,
                        "kind": "new",
                        "params": params or {"peak_power_kw": 120},
                    },
                }
            ],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


def _template_inputs(path: str) -> dict:
    return {
        "properties": {"peak_power_kw": {"value": 120}},
        "interfaces": {"electric_demand": path},
    }


def test_template_data_binding_reaches_assembly(
    client, db_session: Session, tmp_path: Path
) -> None:
    headers, pid = _make_owner(client, db_session, "s1_bind_owner")
    token = headers["Authorization"].replace("Bearer ", "")
    base_id = build_stable_id(_token_ns_map[token], "electric-load")

    # CSV 的 device_id 是稳定设备 ID(不含项目作用域 _N 编号), 用户无需猜编号。
    vid = _upload_version(client, headers, pid, base_id)
    _bind_dataset(client, headers, pid, vid)
    template_id, revision = _publish_template(client, headers, TEMPLATE_YAML, slug="electric-load")

    resp = _save(
        client,
        pid,
        headers,
        "",
        source="template",
        template_id=template_id,
        template_revision=revision,
        template_inputs=_template_inputs(dataset_export_path(vid)),
    )
    assert resp.status_code == 201, resp.text
    receipt = resp.json()["receipt"]
    assert receipt["data_bindings"] == [
        {
            "interface": "electric_demand",
            "mode": "data_repeat",
            "dataset_version_id": vid,
        }
    ]

    # 实例显式持有来源事实(与页面参数编辑同一 model.upsert_device params)。
    _add_device(
        client, headers, pid, "load1", f"{base_id}_1",
        params={
            "peak_power_kw": 120,
            "electric_demand": {"mode": "data_repeat", "dataset_version_id": vid},
        },
    )
    project = project_domain.require_project(db_session, pid)
    draft = project_domain.require_current_draft(db_session, project)
    content = load_content_object(db_session, draft.content_object_id)
    documents = load_project_device_documents(db_session, content)
    # CSV 身份用稳定基础 ID(不随项目作用域 _N 编号变化)。
    assert load_project_device_base_ids(db_session, content) == {f"{base_id}_1": base_id}

    export_content = dict(content)
    export_content["name"] = f"project_{pid}"
    export_content["project_baseline"] = {
        "resolution": "1h",
        "start_year": 2026,
        "scenario_mode": "single",
    }
    built = build_assembly_doc_from_content(
        export_content,
        device_documents=documents,
    )
    assert built.doc is not None
    instance = built.doc["devices"]["load1"]
    assert instance["predefined_interfaces"]["electric_demand"] == {
        "mode": "data_repeat",
        "path": dataset_export_path(vid),
    }


def test_template_data_input_required_and_must_resolve(
    client, db_session: Session
) -> None:
    headers, pid = _make_owner(client, db_session, "s1_bind_required")
    token = headers["Authorization"].replace("Bearer ", "")
    base_id = build_stable_id(_token_ns_map[token], "electric-load")
    template_id, revision = _publish_template(client, headers, TEMPLATE_YAML, slug="electric-load")

    # 缺少数据叶子 → 拒绝(不静默保留模板值)
    missing = _save(
        client,
        pid,
        headers,
        "",
        source="template",
        template_id=template_id,
        template_revision=revision,
        template_inputs={"properties": {"peak_power_kw": {"value": 120}}},
    )
    assert missing.status_code == 400, missing.text
    details = [d["params"].get("detail", "") for d in missing.json()["error"]["params"]["diagnostics"]]
    assert any("必填输入" in d for d in details), details

    # 路径不在本项目数据集版本 → 拒绝(真实入口明确失败)
    unresolved = _save(
        client,
        pid,
        headers,
        "",
        source="template",
        template_id=template_id,
        template_revision=revision,
        template_inputs=_template_inputs("data/ds999999.data.csv"),
    )
    assert unresolved.status_code == 400, unresolved.text
    details = [
        d["params"].get("detail", "")
        for d in unresolved.json()["error"]["params"]["diagnostics"]
    ]
    assert any("不在本项目数据集版本中" in d for d in details), details

    # 保存失败不产生模型清单行
    listed = client.get(f"/api/projects/{pid}/models", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["project_models"] == []


@pytest.mark.parametrize("mode", ["data_full", "data_predict"])
def test_template_data_modes_declared_and_saved(
    client, db_session: Session, mode: str
) -> None:
    """data_full / data_predict 叶子按现行规范声明、必填并解析为数据集版本绑定。

    数据本体与具体设备实例的 id/长度核对发生在装配边界; 此处证明模型保存
    入口接受两种模式、要求必填并把路径解析到项目数据集版本(不写模型内 source)。
    """
    headers, pid = _make_owner(client, db_session, f"s1_bind_{mode}")
    token = headers["Authorization"].replace("Bearer ", "")
    base_id = build_stable_id(_token_ns_map[token], "electric-load")
    template_yaml = TEMPLATE_YAML.replace(
        "electric_demand: {type: data_repeat}", f"electric_demand: {{type: {mode}}}"
    )
    template_id, revision = _publish_template(
        client, headers, template_yaml, slug=f"electric-load-{mode.replace('_', '-')}"
    )
    vid = _upload_version(client, headers, pid, base_id, mode)
    _bind_dataset(client, headers, pid, vid)
    resp = _save(
        client,
        pid,
        headers,
        "",
        source="template",
        template_id=template_id,
        template_revision=revision,
        template_inputs=_template_inputs(dataset_export_path(vid)),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["receipt"]["data_bindings"] == [
        {"interface": "electric_demand", "mode": mode, "dataset_version_id": vid}
    ]


SAMPLES = Path(__file__).parent.parent / "iesplan" / "assembly" / "samples"


def test_numbered_model_csv_identity_uses_base_id_through_assembly() -> None:
    """同基名模型实际分配 _2 时, CSV 用稳定基础 ID 仍通过真实装配身份核对。"""
    import dataclasses

    from iesplan.assembly import validate_assembly_doc
    from iesplan.devices import get_device
    from iesplan.devices.contracts2 import DeviceInfo

    sample_text = (SAMPLES / "valid" / "campus.assembly.yaml").read_text(encoding="utf-8")
    numbered = sample_text.replace(
        "    definition:\n      id: ies.device.pv",
        "    definition:\n      id: acme.device.pv_2",
    )
    assert numbered != sample_text
    # 用基础 ID 的合成 CSV(与样例同值形状)覆盖样例副本。
    csv_text = (SAMPLES / "data" / "pv_irradiance.data.csv").read_text(encoding="utf-8")
    csv_base = csv_text.replace("ies.device.pv", "acme.device.pv")
    contents = {
        "data/pv_irradiance.data.csv": {
            "object_id": "synthetic", "media_type": "text/csv", "data": csv_base.encode("utf-8"),
        }
    }

    pv_doc = get_device("ies.device.pv")
    numbered_doc = dataclasses.replace(
        pv_doc,
        device=DeviceInfo(id="acme.device.pv_2", names=pv_doc.device.names),
    )
    documents = {"acme.device.pv_2": numbered_doc}

    from iesplan.assembly import parse_assembly_doc

    parsed = parse_assembly_doc(numbered, source_name="campus.assembly.yaml")
    assert parsed.doc is not None
    ok = validate_assembly_doc(
        parsed.doc,
        package_dir=SAMPLES,
        dataset_contents=contents,
        device_documents=documents,
        device_base_ids={"acme.device.pv_2": "acme.device.pv"},
    )
    assert ok.artifact is not None, [d.code for d in ok.diagnostics if d.blocking]
    assert not any(
        d.code == "ASM-BIND-001" and d.params.get("reason") == "csv_device_mismatch"
        for d in ok.diagnostics
    )

    # 去掉基础 ID 映射: 身份约束仍然生效, 明确阻断(重新解析, 避免同次校验就地写入)。
    parsed_bad = parse_assembly_doc(numbered, source_name="campus.assembly.yaml")
    assert parsed_bad.doc is not None
    bad = validate_assembly_doc(
        parsed_bad.doc,
        package_dir=SAMPLES,
        dataset_contents=contents,
        device_documents=documents,
    )
    assert bad.artifact is None
    assert any(
        d.code == "ASM-BIND-001" and d.params.get("reason") == "csv_device_mismatch"
        for d in bad.diagnostics
    )


def test_data_predict_binding_with_explicit_target_passes_assembly() -> None:
    """data_predict 由实例显式 target_type 声明, 真实装配校验签发产物。"""
    from iesplan.assembly import parse_assembly_doc, validate_assembly_doc
    from iesplan.devices import get_device

    sample_text = (SAMPLES / "valid" / "campus.assembly.yaml").read_text(encoding="utf-8")
    predict = sample_text.replace(
        """      ambient_temperature:
        mode: constant
        value: 25""",
        """      ambient_temperature:
        mode: data_predict
        path: data/pv_predict.data.csv
        target_type: ambient_temperature""",
    )
    assert predict != sample_text
    solar_csv = (SAMPLES / "data" / "pv_irradiance.data.csv").read_text(encoding="utf-8")
    predict_csv = "\n".join([
        "# schema: ies.device-data",
        "# schema_version: 2.1.0",
        "# dataset_id: synthetic_predict",
        "# device_id: ies.device.pv",
        "# source_mode: data_predict",
        "# resolution: 1h",
        "# unit.ambient_temperature: °C",
        "step,ambient_temperature",
        *[f"{i},{20 + (i % 3)}" for i in range(24)],
        "",
    ])
    contents = {
        "data/pv_irradiance.data.csv": {
            "object_id": "solar", "media_type": "text/csv", "data": solar_csv.encode("utf-8"),
        },
        "data/pv_predict.data.csv": {
            "object_id": "predict", "media_type": "text/csv", "data": predict_csv.encode("utf-8"),
        },
    }
    parsed = parse_assembly_doc(predict, source_name="campus.assembly.yaml")
    assert parsed.doc is not None
    result = validate_assembly_doc(
        parsed.doc,
        package_dir=SAMPLES,
        dataset_contents=contents,
        device_documents={"ies.device.pv": get_device("ies.device.pv")},
    )
    assert result.artifact is not None, [
        (d.code, d.params.get("reason")) for d in result.diagnostics if d.blocking
    ]
    assert "target_type: ambient_temperature" in result.artifact.canonical_text.replace(
        '"target_type":"ambient_temperature"', "target_type: ambient_temperature"
    ) or "ambient_temperature" in result.artifact.canonical_text
