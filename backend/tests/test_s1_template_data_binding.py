"""S1 修正: 模板数据输入 → 项目数据集版本 → 装配绑定 → 消费。

证明 0.15 S1 修正后的生产链:
1. 模板数据叶子必填(缺失即拒绝, 无隐式回退);
2. 数据值是项目数据集版本的公开装配路径, 不在本项目即明确失败;
3. 保存成功的项目模型回执保留数据绑定, 且装配构造器在没有手工补绑的
   情况下为使用该模型的设备实例生成 ``predefined_interfaces`` 来源声明。

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
    load_project_device_data_bindings,
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


def _add_device(client, headers, pid: int, name: str, device_type: str) -> None:
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
                        "params": {"peak_power_kw": 120},
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
    final_id = f"{base_id}_1"

    vid = _upload_version(client, headers, pid, final_id)
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

    _add_device(client, headers, pid, "load1", final_id)
    project = project_domain.require_project(db_session, pid)
    draft = project_domain.require_current_draft(db_session, project)
    content = load_content_object(db_session, draft.content_object_id)
    documents = load_project_device_documents(db_session, content)
    bindings = load_project_device_data_bindings(db_session, content)
    assert bindings == {
        final_id: [
            {
                "interface": "electric_demand",
                "mode": "data_repeat",
                "dataset_version_id": vid,
            }
        ]
    }

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
        device_data_bindings=bindings,
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
    final_id = f"{base_id}_1"
    template_yaml = TEMPLATE_YAML.replace(
        "electric_demand: {type: data_repeat}", f"electric_demand: {{type: {mode}}}"
    )
    template_id, revision = _publish_template(
        client, headers, template_yaml, slug=f"electric-load-{mode.replace('_', '-')}"
    )
    vid = _upload_version(client, headers, pid, final_id, mode)
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
