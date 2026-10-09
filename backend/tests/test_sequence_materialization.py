"""确定性序列物化契约测试(0.8 切片 E)。

只测本切片业务语义，不复核装配边界已确认的事实：

- constant 普通年/闰年与三种分辨率的完整展开；
- data_repeat 日/周/年周期的重复与尾部截断；
- 多设备/接口与同文件多列的精确选择；
- 输入输出不可变，调用不修改原对象；
- 资源按 canonical 绑定 object_id 解析，缺失明确失败，无 path 回退；
- data_predict 进入待定边界，不被误报为已物化。
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from iesplan.assembly.contracts import ValidatedAssemblyArtifact, ValidationReceipt
from iesplan.computation import (
    MaterializedSeries,
    MaterializedSet,
    materialize_non_predictive_series,
)

CSV_META = (
    "# schema: ies.device-data\n"
    "# schema_version: 2.0.0\n"
    "# dataset_id: ds1\n"
    "# device_id: acme.device.load\n"
    "# source_mode: data_repeat\n"
    "# resolution: 1h\n"
    "# unit.power: kW\n"
)


def _csv_bytes(columns: list[str], rows: list[list[float]], resolution: str = "1h") -> bytes:
    lines = [CSV_META.replace("# resolution: 1h", f"# resolution: {resolution}")]
    lines.append("step," + ",".join(columns))
    lines.extend(f"{i},{','.join(str(v) for v in row)}" for i, row in enumerate(rows))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _artifact(
    baseline: dict,
    devices: dict,
) -> ValidatedAssemblyArtifact:
    doc = {
        "schema": "ies.assembly",
        "schema_version": "2.0.0",
        "project_baseline": baseline,
        "devices": devices,
    }
    return ValidatedAssemblyArtifact(
        canonical_text=json.dumps(doc), receipt=ValidationReceipt()
    )


def _const_binding(value: float) -> dict:
    return {"mode": "constant", "value": value}


def _repeat_binding(object_id: str) -> dict:
    return {
        "mode": "data_repeat",
        "path": "/nonexistent/evil.csv",
        "content": {"object_id": object_id, "media_type": "text/csv"},
    }


def test_constant_point_counts() -> None:
    """constant 按基线展开到完整点数（普通年/闰年 × 三种分辨率代表）。"""
    cases = [
        ({"resolution": "15min", "start_year": 2026}, 35040),
        ({"resolution": "30min", "start_year": 2026}, 17520),
        ({"resolution": "1h", "start_year": 2026}, 8760),
        ({"resolution": "1h", "start_year": 2024}, 8784),
    ]
    for baseline, expected in cases:
        result = materialize_non_predictive_series(
            _artifact(baseline, {"d1": {"predefined_interfaces": {"p": _const_binding(2.5)}}}),
            {}, evaluation_years=1,
        )
        assert isinstance(result, MaterializedSet)
        assert len(result.series) == 1
        series = result.series[0]
        assert series.point_count == expected == len(series.values)
        assert set(series.values) == {2.5}
        assert series.resolution == baseline["resolution"]
        assert result.pending_predict == ()


def test_repeat_day_week_year() -> None:
    """日周期重复、周周期尾部截断、年周期恒等。"""
    baseline = {"resolution": "1h", "start_year": 2026}
    day = [float(i) for i in range(24)]
    week = [float(i) for i in range(168)]
    year = [float(i % 100) for i in range(8760)]
    resources = {
        "obj-11": _csv_bytes(["p"], [[v] for v in day]),
        "obj-12": _csv_bytes(["p"], [[v] for v in week]),
        "obj-13": _csv_bytes(["p"], [[v] for v in year]),
    }
    devices = {
        "day": {"predefined_interfaces": {"p": _repeat_binding("obj-11")}},
        "week": {"predefined_interfaces": {"p": _repeat_binding("obj-12")}},
        "year": {"predefined_interfaces": {"p": _repeat_binding("obj-13")}},
    }
    result = materialize_non_predictive_series(_artifact(baseline, devices), resources, evaluation_years=1)
    by_device = {s.device: s for s in result.series}
    assert list(by_device) == ["day", "week", "year"]
    assert list(by_device["day"].values) == day * 365
    assert list(by_device["week"].values) == week * 52 + week[:24]
    assert list(by_device["year"].values) == year


def test_multi_device_interface_column_selection() -> None:
    """多设备/接口与同文件多列精确选择；source 追溯 object_id。"""
    baseline = {"resolution": "1h", "start_year": 2026}
    resources = {
        "obj-21": _csv_bytes(["pa", "pb"], [[1.0, 2.0], [3.0, 4.0]]),
    }
    devices = {
        "zeta": {"predefined_interfaces": {"pb": _repeat_binding("obj-21")}},
        "alpha": {
            "predefined_interfaces": {
                "pa": _repeat_binding("obj-21"),
                "pc": _const_binding(7.0),
            }
        },
    }
    result = materialize_non_predictive_series(_artifact(baseline, devices), resources, evaluation_years=1)
    assert [(s.device, s.interface) for s in result.series] == [
        ("alpha", "pa"),
        ("alpha", "pc"),
        ("zeta", "pb"),
    ]
    by_key = {(s.device, s.interface): s for s in result.series}
    assert list(by_key[("alpha", "pa")].values)[:4] == [1.0, 3.0, 1.0, 3.0]
    assert list(by_key[("zeta", "pb")].values)[:4] == [2.0, 4.0, 2.0, 4.0]
    assert by_key[("zeta", "pb")].source["object_id"] == "obj-21"
    assert set(by_key[("alpha", "pc")].values) == {7.0}


def test_immutable_and_caller_mutation_ignored() -> None:
    """载体不可变；调用方事后改写输入不影响已物化结果。"""
    baseline = {"resolution": "1h", "start_year": 2026}
    resources = {"obj-31": _csv_bytes(["p"], [[9.0], [8.0]])}
    artifact = _artifact(
        baseline, {"d1": {"predefined_interfaces": {"p": _repeat_binding("obj-31")}}}
    )
    result = materialize_non_predictive_series(artifact, resources, evaluation_years=1)
    series = result.series[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        series.values = ()  # type: ignore[misc]
    with pytest.raises(TypeError):
        series.values[0] = 0.0  # type: ignore[index]
    resources["obj-31"] = _csv_bytes(["p"], [[0.0]])
    resources["obj-99"] = b"junk"
    assert list(result.series[0].values)[:2] == [9.0, 8.0]
    dumped = result.to_dict()
    dumped["series"][0]["values"][0] = 0.0
    assert result.to_dict()["series"][0]["values"][0] == 9.0


def test_missing_resource_fails_without_path_fallback() -> None:
    """资源缺失明确失败；path 字段永不用于读取（无 fallback）。"""
    baseline = {"resolution": "1h", "start_year": 2026}
    artifact = _artifact(
        baseline, {"d1": {"predefined_interfaces": {"p": _repeat_binding("obj-41")}}}
    )
    with pytest.raises(ValueError, match="固定资源缺失"):
        materialize_non_predictive_series(artifact, {}, evaluation_years=1)


def test_predict_is_pending_not_materialized() -> None:
    """data_predict 进入待定边界（含资源身份），不被误报为已物化。"""
    baseline = {"resolution": "1h", "start_year": 2026}
    devices = {
        "d1": {
            "predefined_interfaces": {
                "pf": {
                    "mode": "data_predict",
                    "path": "future.csv",
                    "target_type": "load",
                    "content": {"object_id": "obj-51", "media_type": "text/csv"},
                },
                "pr": _repeat_binding("obj-52"),
            }
        }
    }
    resources = {
        "obj-51": _csv_bytes(["pf"], [[1.0]]),
        "obj-52": _csv_bytes(["pr"], [[1.0]]),
    }
    result = materialize_non_predictive_series(_artifact(baseline, devices), resources, evaluation_years=1)
    assert [s.interface for s in result.series] == ["pr"]
    assert result.pending_predict == (
        {
            "device": "d1",
            "interface": "pf",
            "target_type": "load",
            "object_id": "obj-51",
        },
    )
    dumped = result.to_dict()
    assert dumped["pending_predict"] == [
        {
            "device": "d1",
            "interface": "pf",
            "target_type": "load",
            "object_id": "obj-51",
        }
    ]


def test_predict_missing_resource_fails() -> None:
    """预测绑定身份缺失同样是必要输入不完整，明确失败。"""
    baseline = {"resolution": "1h", "start_year": 2026}
    devices = {
        "d1": {
            "predefined_interfaces": {
                "pf": {
                    "mode": "data_predict",
                    "path": "future.csv",
                    "target_type": "load",
                    "content": {"object_id": "obj-51", "media_type": "text/csv"},
                },
            }
        }
    }
    with pytest.raises(ValueError, match="固定资源缺失"):
        materialize_non_predictive_series(_artifact(baseline, devices), {}, evaluation_years=1)


def test_quoted_numeric_cells_consumed() -> None:
    """RFC 4180 引号数值单元格可消费（标准库 csv，非 split 解析）。"""
    baseline = {"resolution": "1h", "start_year": 2026}
    raw = (
        CSV_META
        + 'step,p\n0,"48.3"\n1,"46.7"\n'
    ).encode("utf-8")
    devices = {"d1": {"predefined_interfaces": {"p": _repeat_binding("obj-61")}}}
    result = materialize_non_predictive_series(
        _artifact(baseline, devices), {"obj-61": raw}, evaluation_years=1
    )
    assert list(result.series[0].values)[:2] == [48.3, 46.7]


def test_predicted_series_uses_same_materialized_type() -> None:
    """预测完成值以同一 MaterializedSeries 进入（mode=data_predict）。"""
    predicted = MaterializedSeries(
        device="d1",
        interface="pf",
        mode="data_predict",
        resolution="1h",
        point_count=3,
        values=(1.0, 2.0, 3.0),
        source={"kind": "data_predict", "object_id": "obj-51"},
    )
    assert predicted.to_dict()["mode"] == "data_predict"
    assert len(predicted.values) == predicted.point_count == 3
    with pytest.raises(ValueError, match="未知来源模式"):
        MaterializedSeries(
            device="d1",
            interface="pf",
            mode="data_guess",
            resolution="1h",
            point_count=1,
            values=(1.0,),
            source={},
        )
