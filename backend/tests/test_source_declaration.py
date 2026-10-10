"""B5-2a 数据序列来源声明入口: 设备声明 params 字典形态 → 装配绑定。

- 构造器单元测试: 无数据库依赖;
- 门控级验收: validate_project_export 全管线(构造→结构→内容→签发),
  注入 device-data 字节，断言绑定冻结 content.object_id；
  同一产物经 materialize_non_predictive_series 物化全周期序列。
标量 constant 形态不受影响；形态语义以 formats/device-data-csv 与
装配结构阶段严格判别联合为准。
API 端到端以真实上传数据集为前提，当前上传只收通用时间戳格式、
门控只认 device-data step 格式（探针证否互通），属独立上传格式切片，
不在此验收。
"""

from __future__ import annotations

import json

import pytest

from iesplan.assembly.builder import build_assembly_doc_from_content
from iesplan.assembly.validator import validate_project_export
from iesplan.computation.sequences import materialize_non_predictive_series


@pytest.fixture()
def devices():
    from iesplan.devices import init_registry  # noqa: E402

    init_registry()
    yield


def _content(params: dict) -> dict:
    return {
        "name": "decl_test",
        "model": {
            "devices": [
                {
                    "name": "load1",
                    "device_type": "ies.device.electric_load",
                    "kind": "new",
                    "params": dict(params),
                }
            ],
            "connections": [],
        },
        "project_baseline": {"resolution": "1h", "start_year": 2026, "scenario_mode": "single"},
    }


def _binding(params: dict):
    result = build_assembly_doc_from_content(_content(params))
    assert result.doc is not None, [(d.code, d.params) for d in result.diagnostics if d.blocking]
    return result.doc["devices"]["load1"]["predefined_interfaces"]["electricity_demand"]


class TestSourceDeclaration:
    def test_repeat_dict_declares_synthesized_path(self, devices) -> None:
        assert _binding({"electricity_demand": {"mode": "data_repeat", "dataset_version_id": 7}}) == {
            "mode": "data_repeat",
            "path": "data/ds7.data.csv",
        }

    def test_predict_dict_declares_path_and_target_type(self, devices) -> None:
        assert _binding(
            {
                "electricity_demand": {
                    "mode": "data_predict",
                    "dataset_version_id": 3,
                    "target_type": "load",
                }
            }
        ) == {"mode": "data_predict", "path": "data/ds3.data.csv", "target_type": "load"}

    def test_scalar_constant_unchanged(self, devices) -> None:
        assert _binding({"electricity_demand": 100.0}) == {"mode": "constant", "value": 100.0}

    @pytest.mark.parametrize(
        "decl",
        [
            {"mode": "data_guess", "dataset_version_id": 7},
            {"mode": "data_repeat", "dataset_version_id": "7"},
            {"mode": "data_repeat", "dataset_version_id": True},
            {"mode": "data_repeat"},
            {"mode": "data_repeat", "dataset_version_id": 7, "target_type": "load"},
            {"mode": "data_predict", "dataset_version_id": 7},
            {"mode": "data_predict", "dataset_version_id": 7, "target_type": ""},
            {"mode": "data_predict", "dataset_version_id": 7, "target_type": "load", "path": "x"},
        ],
    )
    def test_malformed_declaration_blocked(self, devices, decl: dict) -> None:
        result = build_assembly_doc_from_content(_content({"electricity_demand": decl}))
        assert result.doc is None
        assert any(d.blocking for d in result.diagnostics)


def _repeat_csv() -> bytes:
    lines = [
        "# schema: ies.device-data",
        "# schema_version: 2.1.0",
        "# dataset_id: load_repeat_day",
        "# device_id: ies.device.electric_load",
        "# source_mode: data_repeat",
        "# resolution: 1h",
        "# unit.electricity_demand: kW",
        "step,electricity_demand",
    ]
    lines += [f"{i},{48.0 + (i % 5)}" for i in range(24)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _gate_repeat_artifact():
    """经全管线签发含重复绑定的产物，返回 (artifact, 资源字节表)。"""
    from test_assembly_artifact import (  # noqa: E402
        _EXPORT_FINANCE,
        _EXPORT_PLANNING,
        _EXPORT_TARIFF,
        _finance_snapshots,
    )

    raw = _repeat_csv()
    contents = {"data/ds9.data.csv": {"object_id": "obj-repeat-9", "media_type": "text/csv", "data": raw}}
    content = {
        "name": "decl_gate",
        "model": {
            "devices": [
                {
                    "name": "grid1",
                    "device_type": "ies.device.grid_connection",
                    "kind": "existing",
                    "params": {"max_import_power_kw": 800, "max_export_power_kw": 200},
                },
                {
                    "name": "hp1",
                    "device_type": "ies.device.heat_pump",
                    "kind": "new",
                    "params": {"rated_heat_kw": 600, "cop": 3.5},
                },
                {
                    "name": "load1",
                    "device_type": "ies.device.electric_load",
                    "kind": "new",
                    "params": {
                        "peak_power_kw": 100,
                        "electricity_demand": {"mode": "data_repeat", "dataset_version_id": 9},
                    },
                },
            ],
            "connections": [
                {
                    "name": "c-grid-hp",
                    "from_device": "grid1",
                    "from_port": "electricity_import",
                    "to_device": "hp1",
                    "to_port": "electricity_in",
                },
                {
                    "name": "c-grid-load",
                    "from_device": "grid1",
                    "from_port": "electricity_import",
                    "to_device": "load1",
                    "to_port": "electricity_in",
                },
            ],
        },
        "project_baseline": {"resolution": "1h", "start_year": 2026, "scenario_mode": "single"},
    }
    result = validate_project_export(
        content,
        finance=dict(_EXPORT_FINANCE),
        finance_binding={
            "instances": {
                "grid1": {"type": "none"},
                "hp1": {
                    "type": "costed",
                    "finance_type": "heat_pump_system",
                    "drivers": {
                        "heat_kw": {"property": "rated_heat_kw"},
                        "consumed_kwh": {"interface": "electricity_in", "aggregation": "integrate_positive"},
                    },
                },
                "load1": {"type": "none"},
            }
        },
        tariff_bindings=dict(_EXPORT_TARIFF),
        finance_snapshots=_finance_snapshots(),
        planning=dict(_EXPORT_PLANNING),
        dataset_contents=contents,
    )
    assert result.ok, [(d.code, d.params) for d in result.diagnostics if d.blocking]
    assert result.artifact is not None
    return result.artifact, raw


class TestGateAndMaterialize:
    def test_declared_repeat_binding_freezes_content_ref(self, devices) -> None:
        """声明入口 → 全管线 → 绑定冻结 content.object_id（快照复用同一文本即冻结）。"""
        artifact, _raw = _gate_repeat_artifact()
        doc = json.loads(artifact.canonical_text)
        binding = doc["devices"]["load1"]["predefined_interfaces"]["electricity_demand"]
        assert binding["mode"] == "data_repeat"
        assert binding["path"] == "data/ds9.data.csv"
        assert binding["content"]["object_id"] == "obj-repeat-9"

    def test_frozen_repeat_materializes_full_series(self, devices) -> None:
        """冻结产物 + 同一字节 → 物化全周期序列（8760 点，来源可追溯）。"""
        artifact, raw = _gate_repeat_artifact()
        materialized = materialize_non_predictive_series(
            artifact, {"obj-repeat-9": raw}, operation_years=1
        )
        assert not materialized.pending_predict
        series = [s for s in materialized.series if s.device == "load1"]
        assert len(series) == 1
        assert series[0].mode == "data_repeat"
        assert series[0].point_count == 8760
        assert series[0].values[0] == 48.0
        assert series[0].values[24] == 48.0
        assert series[0].source == {"kind": "data_repeat", "object_id": "obj-repeat-9"}
