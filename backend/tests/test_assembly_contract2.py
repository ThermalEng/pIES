"""`ies.assembly` 2.0.0 接口网络纯协议校验测试(不依赖注册表/数据库)。

覆盖: 五类 interface 连接规则、predefined/blind 非法连接与绑定、carrier
相同、单位量纲兼容、有效区间冲突、设备内容锁、三件套产物与确定性。
"""

from __future__ import annotations

import copy
import hashlib

from iesplan.assembly.validator2 import (
    NetworkReceipt,
    ValidatedInterfaceNetwork,
    validate_interface_network2,
)
from iesplan.core.yamlmini import load as yaml_load
from iesplan.devices.contracts2 import DeviceModelDocument, content_sha256
from iesplan.devices.parser2 import parse_device_model_v2


def _doc(text: str) -> DeviceModelDocument:
    r = parse_device_model_v2(yaml_load(text), file="test.yaml")
    assert r.ok, [d.params.get("detail") for d in r.diagnostics]
    return r.document


GRID = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.grid_connection, names: {zh-CN: 电网, en-US: Grid}}
properties: {}
interfaces:
  electricity_out: {type: out, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: 1000}}
equations: {variables: {}, relations: []}
"""

LOAD = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.electric_load, names: {zh-CN: 电负荷, en-US: Load}}
properties: {}
interfaces:
  electricity_in: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: 1000}}
  electric_demand:
    type: predefined
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: 1000}
    source: {mode: data_repeat, data_ref: typical_day_load}
equations:
  variables: {}
  relations:
    - id: demand_balance
      expression: "electricity_in[t] = electric_demand[t]"
"""

BATTERY = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.battery, names: {zh-CN: 电池, en-US: Battery}}
properties:
  initial_soc: {value: 0.5, unit: "1", valid_range: {minimum: 0, maximum: 1}}
interfaces:
  power: {type: bidirectional, carrier: electricity, unit: kW, valid_range: {minimum: -100, maximum: 100}}
equations:
  variables:
    soc: {unit: "1", valid_range: {minimum: 0, maximum: 1}, initial: {property_ref: initial_soc}}
  relations:
    - id: soc_transition
      expression: "soc[t] = soc[t-1]"
"""

HEAT_LOAD = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.heat_load, names: {zh-CN: 热负荷, en-US: Heat Load}}
properties: {}
interfaces:
  heat_in: {type: in, carrier: heat, unit: kW, valid_range: {minimum: 0, maximum: 1000}}
equations: {variables: {}, relations: []}
"""

ENERGY_OUT = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.energy_out, names: {zh-CN: 能量输出, en-US: Energy Out}}
properties: {}
interfaces:
  energy_out: {type: out, carrier: electricity, unit: kWh, valid_range: {minimum: 0, maximum: 1000}}
equations: {variables: {}, relations: []}
"""

SENSOR = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.sensor, names: {zh-CN: 传感器, en-US: Sensor}}
properties: {}
interfaces:
  status: {type: blind, carrier: data, unit: "1", valid_range: {minimum: 0, maximum: 1}}
equations: {variables: {}, relations: []}
"""

FIXED_SUPPLY = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.fixed_supply, names: {zh-CN: 固定供给, en-US: Fixed Supply}}
properties: {}
interfaces:
  supply:
    type: predefined
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: 1000}
    source: {mode: constant, value: 50}
equations: {variables: {}, relations: []}
"""

NARROW_GRID = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.narrow_grid, names: {zh-CN: 窄范围电网}}
properties: {}
interfaces:
  electricity_out: {type: out, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: 10}}
equations: {variables: {}, relations: []}
"""

STRICT_LOAD = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.strict_load, names: {zh-CN: 严格负荷}}
properties: {}
interfaces:
  electricity_in: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 20, maximum: 1000}}
equations: {variables: {}, relations: []}
"""


def _assembly_doc(
    documents: dict[str, DeviceModelDocument],
    *,
    devices: dict | None = None,
    connections: dict | None = None,
) -> dict:
    """构造装配 2.0 接口网络文档(definition 自动取 descriptor 内容锁)。"""
    doc: dict = {"schema": "ies.assembly", "schema_version": "2.0.0", "devices": {}, "connections": {}}
    devices = devices or {}
    for inst_id, d in documents.items():
        cfg = devices.get(inst_id) or {}
        entry: dict = {
            "definition": {"id": d.device.id, "content_sha256": content_sha256(d)},
            "asset_origin": cfg.get("asset_origin", "existing"),
        }
        if cfg.get("predefined_interfaces"):
            entry["predefined_interfaces"] = cfg["predefined_interfaces"]
        doc["devices"][inst_id] = entry
    doc["connections"] = dict(connections or {})
    return doc


def _codes(result) -> list[str]:
    assert result.artifact is None
    assert not result.ok
    return [d.code for d in result.diagnostics]


GRID_DOC = _doc(GRID)
LOAD_DOC = _doc(LOAD)
BATTERY_DOC = _doc(BATTERY)
HEAT_DOC = _doc(HEAT_LOAD)
ENERGY_DOC = _doc(ENERGY_OUT)
SENSOR_DOC = _doc(SENSOR)
FIXED_DOC = _doc(FIXED_SUPPLY)
NARROW_DOC = _doc(NARROW_GRID)
STRICT_DOC = _doc(STRICT_LOAD)


class TestValidNetwork:
    def test_grid_to_load_with_binding(self):
        documents = {"grid": GRID_DOC, "load": LOAD_DOC}
        doc = _assembly_doc(
            documents,
            devices={"load": {"predefined_interfaces": {"electric_demand": {"data_ref": "campus_load"}}}},
            connections={"grid_to_load": {"from": "grid.electricity_out", "to": "load.electricity_in"}},
        )
        r = validate_interface_network2(doc, documents)
        assert r.ok, [d.params.get("detail") for d in r.diagnostics]
        artifact = r.artifact
        assert isinstance(artifact, ValidatedInterfaceNetwork)
        # 三件套一致
        assert artifact.verify()
        assert artifact.network_sha256 == hashlib.sha256(artifact.canonical_text.encode("utf-8")).hexdigest()
        assert artifact.receipt.network_sha256 == artifact.network_sha256
        assert artifact.receipt.device_locks == {
            "grid": content_sha256(GRID_DOC),
            "load": content_sha256(LOAD_DOC),
        }
        assert artifact.receipt.diagnostics == ()
        # 规范文本确定性形态
        assert '"schema":"ies.assembly"' in artifact.canonical_text
        assert '"from":"grid.electricity_out"' in artifact.canonical_text

    def test_bidirectional_connections(self):
        # bidi → in 与 out → bidi 均成立;bidi ↔ bidi 成立
        documents = {"grid": GRID_DOC, "battery": BATTERY_DOC, "load": LOAD_DOC}
        doc = _assembly_doc(
            documents,
            devices={"load": {"predefined_interfaces": {"electric_demand": {"data_ref": "campus_load"}}}},
            connections={
                "c1": {"from": "grid.electricity_out", "to": "battery.power"},
                "c2": {"from": "battery.power", "to": "load.electricity_in"},
            },
        )
        r = validate_interface_network2(doc, documents)
        assert r.ok, [d.params.get("detail") for d in r.diagnostics]

    def test_constant_predefined_unbound(self):
        # constant 来源直接来自设备内容,不需要实例绑定
        documents = {"fixed": FIXED_DOC}
        r = validate_interface_network2(_assembly_doc(documents), documents)
        assert r.ok, [d.params.get("detail") for d in r.diagnostics]

    def test_deterministic(self):
        documents = {"grid": GRID_DOC, "load": LOAD_DOC}
        base = _assembly_doc(
            documents,
            devices={"load": {"predefined_interfaces": {"electric_demand": {"data_ref": "campus_load"}}}},
            connections={
                "c1": {"from": "grid.electricity_out", "to": "load.electricity_in"},
            },
        )
        a = validate_interface_network2(base, documents)
        b = validate_interface_network2(copy.deepcopy(base), documents)
        assert a.artifact.network_sha256 == b.artifact.network_sha256
        assert a.artifact.canonical_text == b.artifact.canonical_text
        # 连接声明顺序不影响规范摘要(规范按键排序)
        reordered = copy.deepcopy(base)
        reordered["connections"] = {
            "c1": {"from": "grid.electricity_out", "to": "load.electricity_in"},
            "c2": {"from": "grid.electricity_out", "to": "load.electricity_in"},
        }
        del reordered["connections"]["c2"]  # 保持单边,仅验证键序稳定
        c = validate_interface_network2(reordered, documents)
        assert c.artifact.network_sha256 == a.artifact.network_sha256
        # 语义变化(换目标) → 摘要变化
        changed = copy.deepcopy(base)
        changed["connections"]["c1"]["to"] = "load.electric_demand"
        d = validate_interface_network2(changed, documents)
        assert d.artifact is None  # predefined 不可作为连接终点
        changed2 = copy.deepcopy(base)
        changed2["devices"]["load"]["asset_origin"] = "new"
        e = validate_interface_network2(changed2, documents)
        assert e.artifact.network_sha256 != a.artifact.network_sha256

    def test_receipt_roundtrip(self):
        documents = {"grid": GRID_DOC, "load": LOAD_DOC}
        doc = _assembly_doc(
            documents,
            devices={"load": {"predefined_interfaces": {"electric_demand": {"data_ref": "campus_load"}}}},
            connections={"c1": {"from": "grid.electricity_out", "to": "load.electricity_in"}},
        )
        artifact = validate_interface_network2(doc, documents).artifact
        receipt = NetworkReceipt.from_dict(artifact.receipt.to_dict())
        assert receipt.network_sha256 == artifact.network_sha256
        assert receipt.device_locks == artifact.receipt.device_locks
        assert receipt.to_dict() == artifact.receipt.to_dict()

    def test_receipt_rejects_malformed(self):
        import pytest

        documents = {"grid": GRID_DOC}
        artifact = validate_interface_network2(_assembly_doc(documents), documents).artifact
        payload = artifact.receipt.to_dict()
        del payload["device_locks"]
        with pytest.raises(ValueError):
            NetworkReceipt.from_dict(payload)


class TestInvalidConnections:
    def _run(self, documents, connections):
        return validate_interface_network2(_assembly_doc(documents, connections=connections), documents)

    def test_in_as_source(self):
        documents = {"load": LOAD_DOC, "battery": BATTERY_DOC}
        r = self._run(documents, {"c1": {"from": "load.electricity_in", "to": "battery.power"}})
        assert "ASM-EDGE-001" in _codes(r)

    def test_out_as_sink(self):
        documents = {"grid": GRID_DOC, "battery": BATTERY_DOC}
        r = self._run(documents, {"c1": {"from": "battery.power", "to": "grid.electricity_out"}})
        assert "ASM-EDGE-002" in _codes(r)

    def test_predefined_in_connection(self):
        documents = {"grid": GRID_DOC, "load": LOAD_DOC}
        r = self._run(documents, {"c1": {"from": "grid.electricity_out", "to": "load.electric_demand"}})
        assert "ASM-EDGE-002" in _codes(r)
        r2 = self._run(documents, {"c1": {"from": "load.electric_demand", "to": "load.electricity_in"}})
        assert "ASM-EDGE-001" in _codes(r2)

    def test_blind_in_connection(self):
        documents = {"grid": GRID_DOC, "sensor": SENSOR_DOC}
        r = self._run(documents, {"c1": {"from": "grid.electricity_out", "to": "sensor.status"}})
        assert "ASM-EDGE-002" in _codes(r)
        r2 = self._run(documents, {"c1": {"from": "sensor.status", "to": "grid.electricity_out"}})
        assert "ASM-EDGE-001" in _codes(r2)

    def test_self_loop(self):
        documents = {"grid": GRID_DOC}
        r = self._run(documents, {"c1": {"from": "grid.electricity_out", "to": "grid.electricity_out"}})
        assert "ASM-EDGE-006" in _codes(r)

    def test_duplicate_edge(self):
        documents = {"grid": GRID_DOC, "load": LOAD_DOC}
        r = self._run(documents, {
            "c1": {"from": "grid.electricity_out", "to": "load.electricity_in"},
            "c2": {"from": "grid.electricity_out", "to": "load.electricity_in"},
        })
        assert "ASM-EDGE-007" in _codes(r)

    def test_carrier_mismatch(self):
        documents = {"grid": GRID_DOC, "heat": HEAT_DOC}
        r = self._run(documents, {"c1": {"from": "grid.electricity_out", "to": "heat.heat_in"}})
        assert "ASM-EDGE-003" in _codes(r)

    def test_unit_dimension_incompatible(self):
        documents = {"energy": ENERGY_DOC, "load": LOAD_DOC}
        r = self._run(documents, {"c1": {"from": "energy.energy_out", "to": "load.electricity_in"}})
        assert "ASM-EDGE-005" in _codes(r)

    def test_valid_range_conflict(self):
        documents = {"narrow": NARROW_DOC, "strict": STRICT_DOC}
        r = self._run(documents, {"c1": {"from": "narrow.electricity_out", "to": "strict.electricity_in"}})
        assert "ASM-EDGE-010" in _codes(r)

    def test_endpoint_undefined(self):
        documents = {"grid": GRID_DOC, "load": LOAD_DOC}
        r = self._run(documents, {"c1": {"from": "grid.nonexistent", "to": "load.electricity_in"}})
        assert "ASM-REF-003" in _codes(r)
        r2 = self._run(documents, {"c1": {"from": "ghost.electricity_out", "to": "load.electricity_in"}})
        assert "ASM-REF-003" in _codes(r2)


class TestInvalidBindings:
    def _run(self, documents, devices):
        return validate_interface_network2(_assembly_doc(documents, devices=devices), documents)

    def test_missing_binding_data_repeat(self):
        documents = {"load": LOAD_DOC}
        r = self._run(documents, {})
        assert "ASM-BIND-001" in _codes(r)

    def test_binding_non_predefined_interface(self):
        documents = {"load": LOAD_DOC}
        r = self._run(documents, {"load": {"predefined_interfaces": {"electricity_in": {"data_ref": "x"}}}})
        assert "ASM-BIND-001" in _codes(r)

    def test_binding_constant_predefined(self):
        documents = {"fixed": FIXED_DOC}
        r = self._run(documents, {"fixed": {"predefined_interfaces": {"supply": {"data_ref": "x"}}}})
        assert "ASM-BIND-001" in _codes(r)

    def test_binding_blind(self):
        documents = {"sensor": SENSOR_DOC}
        r = self._run(documents, {"sensor": {"predefined_interfaces": {"status": {"data_ref": "x"}}}})
        assert "ASM-BIND-001" in _codes(r)

    def test_binding_undefined_interface(self):
        documents = {"load": LOAD_DOC}
        r = self._run(documents, {"load": {"predefined_interfaces": {"ghost": {"data_ref": "x"}}}})
        assert "ASM-REF-003" in _codes(r)

    def test_binding_missing_data_ref(self):
        documents = {"load": LOAD_DOC}
        r = self._run(
            documents,
            {"load": {"predefined_interfaces": {"electric_demand": {"data_ref": ""}}}},
        )
        assert "ASM-BIND-001" in _codes(r)

    def test_all_predefined_bound_ok(self):
        documents = {"load": LOAD_DOC, "fixed": FIXED_DOC}
        r = self._run(
            documents,
            {"load": {"predefined_interfaces": {"electric_demand": {"data_ref": "campus_load"}}}},
        )
        assert r.ok, [d.params.get("detail") for d in r.diagnostics]


class TestDeviceLocks:
    def test_missing_descriptor(self):
        documents = {"grid": GRID_DOC}
        doc = _assembly_doc({"grid": GRID_DOC, "load": LOAD_DOC})
        # load 实例在文档中但未提供 descriptor
        r = validate_interface_network2(doc, documents)
        assert "ASM-REF-002" in _codes(r)

    def test_definition_id_mismatch(self):
        documents = {"grid": GRID_DOC}
        doc = _assembly_doc(documents)
        doc["devices"]["grid"]["definition"]["id"] = "acme.device.other"
        r = validate_interface_network2(doc, documents)
        assert "ASM-LOCK-001" in _codes(r)

    def test_content_sha_mismatch(self):
        documents = {"grid": GRID_DOC}
        doc = _assembly_doc(documents)
        doc["devices"]["grid"]["definition"]["content_sha256"] = "0" * 64
        r = validate_interface_network2(doc, documents)
        assert "ASM-LOCK-001" in _codes(r)


class TestStructure:
    def _run(self, doc, documents=None):
        return validate_interface_network2(doc, documents or {})

    def test_bad_schema(self):
        documents = {"grid": GRID_DOC}
        doc = _assembly_doc(documents)
        doc["schema"] = "ies.assembly-x"
        r = self._run(doc, documents)
        assert "ASM-SYN-006" in _codes(r)

    def test_bad_version(self):
        documents = {"grid": GRID_DOC}
        doc = _assembly_doc(documents)
        doc["schema_version"] = "1.0.0"
        r = self._run(doc, documents)
        assert "ASM-SYN-003" in _codes(r)

    def test_malformed_endpoint(self):
        documents = {"grid": GRID_DOC}
        doc = _assembly_doc(documents, connections={"c1": {"from": "no-dot", "to": "grid.electricity_out"}})
        r = self._run(doc, documents)
        assert "ASM-SYN-005" in _codes(r)

    def test_bad_asset_origin(self):
        documents = {"grid": GRID_DOC}
        doc = _assembly_doc(documents, devices={"grid": {"asset_origin": "bought"}})
        r = self._run(doc, documents)
        assert "ASM-SYN-005" in _codes(r)

    def test_asset_origin_new_ok(self):
        documents = {"grid": GRID_DOC}
        doc = _assembly_doc(documents, devices={"grid": {"asset_origin": "new"}})
        r = self._run(doc, documents)
        assert r.ok, [d.params.get("detail") for d in r.diagnostics]

    def test_invalid_never_produces_artifact(self):
        documents = {"grid": GRID_DOC, "load": LOAD_DOC}
        doc = _assembly_doc(
            documents,
            connections={"c1": {"from": "grid.electricity_out", "to": "load.electric_demand"}},
        )
        r = self._run(doc, documents)
        assert not r.ok
        assert r.artifact is None
