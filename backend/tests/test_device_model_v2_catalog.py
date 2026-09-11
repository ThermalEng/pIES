"""设备目录 2.0 直接契约测试（静态定义，不运行）。

范围：10 档六字段、无旧概念、公式/单位/接口方向
"""

from __future__ import annotations

import pathlib

import pytest  # type: ignore[import-untyped]

from iesplan.core.yamlmini import load as yaml_load
from iesplan.devices.contracts2 import SCHEMA_VERSION
from iesplan.devices.loader import load_all_devices, validate_device_file
from iesplan.devices.parser2 import parse_device_model_v2

CATALOG = pathlib.Path(__file__).parent.parent / "iesplan" / "devices" / "catalog"

EXPECTED_IDS = {
    "ies.device.battery",
    "ies.device.grid_connection",
    "ies.device.electric_load",
    "ies.device.heat_load",
    "ies.device.cooling_load",
    "ies.device.heat_pump",
    "ies.device.pv",
    "ies.device.gas_boiler",
    "ies.device.electric_chiller",
    "ies.device.transport_pipe",
}

BANNED_TOP_FIELDS = {"parameters", "ports", "data_inputs", "states", "model_commands", "extensions", "inputs"}
BANNED_DEVICE_FIELDS = {"version", "capabilities", "energy_carriers", "model_method", "fidelity", "stateful", "finance_type"}
# 注: "max_" 前缀不在此列 —— 进出口容量(max_import/export_power_kw)、
# 储能 SOC 边界(min/max_soc)为引擎/可解性评估消费的现行纯技术参数,
# 与已删除的价格/延迟/朝向等旧概念不同, 不得误禁。
BANNED_SUBSTRINGS = ["annual_", "$price", "prices.yaml", "delay_steps", "delay_buffer", "delayed", "tilt", "azimuth"]


def _load_raw(path: pathlib.Path) -> dict:
    return yaml_load(path.read_text(encoding="utf-8"))  # type: ignore[return-value]


def _parse(text: str, file: str = "test.yaml"):
    return parse_device_model_v2(yaml_load(text), file=file)  # type: ignore[arg-type]


def _no_source_in_interfaces(doc) -> bool:
    """非 predefined 接口不得携带 source(真断言, 非桩)。"""
    return all(iface.source is None for iface in doc.interfaces.values())


class TestCatalogFilesExist:
    def test_catalog_has_ten_yamls(self):
        files = sorted(p.name for p in CATALOG.glob("*.yaml"))
        assert len(files) == 10, f"期望 10 个设备文件，实际 {files}"
        assert "prices.yaml" not in files
        assert not any("migration" in n for n in files)
        assert list(CATALOG.glob("*.csv")) == []

    def test_all_device_ids_present(self):
        docs = load_all_devices(CATALOG)
        ids = {d.device.id for d in docs if d.device is not None}
        assert ids == EXPECTED_IDS


class TestSixFieldsAndNoLegacy:
    def test_each_file_has_six_fields_and_no_banned(self):
        for yaml_path in sorted(CATALOG.glob("*.yaml")):
            raw = _load_raw(yaml_path)
            assert raw.get("schema") == "ies.device-model"
            assert raw.get("schema_version") == "2.0.0"
            for field in ("device", "properties", "interfaces", "equations"):
                assert field in raw
            for banned in BANNED_TOP_FIELDS:
                assert banned not in raw
            for banned in BANNED_DEVICE_FIELDS:
                assert banned not in raw.get("device", {})
            text = yaml_path.read_text(encoding="utf-8")
            for sub in BANNED_SUBSTRINGS:
                assert sub not in text
            assert "default" not in text
            assert not validate_device_file(yaml_path)
            res = parse_device_model_v2(raw, file=str(yaml_path))
            assert res.ok and res.document is not None and res.document.schema_version == SCHEMA_VERSION


class TestDeviceSpecificContracts:
    def _doc(self, device_id: str):
        docs = {d.device.id: d for d in load_all_devices(CATALOG) if d.device is not None}
        doc = docs[device_id]
        assert doc.device is not None
        return doc

    def test_battery_bidirectional_and_soc(self):
        doc = self._doc("ies.device.battery")
        assert doc.interfaces["electricity"].type == "bidirectional"
        assert "soc" in doc.equations.variables and doc.equations.variables["soc"].initial_property_ref == "initial_soc"
        assert doc.properties["cycle_life"].unit == "1"
        exprs = {r.id: r.expression for r in doc.equations.relations}
        assert "charge[t] * discharge[t] = 0" in exprs["charge_discharge_exclusivity"]
        assert "step_duration" in exprs["soc_update"] and "capacity_kwh" in exprs["soc_update"]
        assert _no_source_in_interfaces(doc)

    def test_grid_import_out_export_in(self):
        doc = self._doc("ies.device.grid_connection")
        assert doc.interfaces["electricity_import"].type == "out"
        assert doc.interfaces["electricity_export"].type == "in"
        assert any("electricity_import[t] * electricity_export[t] = 0" in e for e in (r.expression for r in doc.equations.relations))
        assert _no_source_in_interfaces(doc)

    def test_loads_predefined_kw_with_source(self):
        for did in ("ies.device.electric_load", "ies.device.heat_load", "ies.device.cooling_load"):
            doc = self._doc(did)
            assert len(doc.interfaces) == 1
            iface = next(iter(doc.interfaces.values()))
            assert iface.type == "predefined" and iface.unit == "kW"
            assert iface.source is not None and iface.source.mode == "data_repeat"
            assert iface.source.data_ref

    def test_heat_pump_only_heating(self):
        doc = self._doc("ies.device.heat_pump")
        assert doc.interfaces["electricity_in"].type == "in" and doc.interfaces["heat_out"].type == "out"
        assert "cool_out" not in doc.interfaces
        assert "heat_out[t] = electricity_in[t] * cop" in {r.id: r.expression for r in doc.equations.relations}["heat_conversion"]
        assert _no_source_in_interfaces(doc)

    def test_pv_predefined_and_single_equation_with_source(self):
        doc = self._doc("ies.device.pv")
        assert doc.interfaces["solar_irradiance"].type == "predefined" and doc.interfaces["ambient_temperature"].type == "predefined"
        assert len(doc.equations.relations) == 1
        expr = doc.equations.relations[0].expression
        assert "solar_irradiance[t]" in expr and "ambient_temperature[t]" in expr
        for iid in ("solar_irradiance", "ambient_temperature"):
            src = doc.interfaces[iid].source
            assert src is not None and src.mode == "data_repeat" and src.data_ref

    def test_gas_boiler_units(self):
        doc = self._doc("ies.device.gas_boiler")
        assert doc.interfaces["gas_in"].unit == "m3/h" and doc.properties["lhv_kwh_per_m3"].unit == "kWh/m3"
        assert "gas_in[t] * lhv_kwh_per_m3 * thermal_efficiency" in next(r.expression for r in doc.equations.relations if r.id == "heat_generation")
        assert _no_source_in_interfaces(doc)

    def test_transport_pipe_loss_only(self):
        doc = self._doc("ies.device.transport_pipe")
        assert "loss_rate" in doc.properties and "delay_steps" not in doc.properties
        assert not doc.equations.variables
        assert "heat_out[t] = heat_in[t] * (1 - loss_rate)" in doc.equations.relations[0].expression

    def test_electric_chiller(self):
        doc = self._doc("ies.device.electric_chiller")
        assert doc.interfaces["electricity_in"].type == "in" and doc.interfaces["cool_out"].type == "out"
        assert _no_source_in_interfaces(doc)


PER_DEVICE_VALID = {
    "ies.device.battery": """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: ies.device.battery, names: {zh-CN: 电池, en-US: Battery}}
properties:
  capacity_kwh: {value: 800, unit: kWh, valid_range: {minimum: 0, maximum: 10000000}}
  rated_power_kw: {value: 400, unit: kW, valid_range: {minimum: 0, maximum: 1000000}}
  charge_efficiency: {value: 0.92, unit: "1", valid_range: {minimum: 0.5, maximum: 1}}
  discharge_efficiency: {value: 0.93, unit: "1", valid_range: {minimum: 0.5, maximum: 1}}
  initial_soc: {value: 0.4, unit: "1", valid_range: {minimum: 0, maximum: 1}}
  cycle_life: {value: 5000, unit: "1", valid_range: {minimum: 100, maximum: 20000}}
interfaces:
  electricity: {type: bidirectional, carrier: electricity, unit: kW, valid_range: {minimum: null, maximum: null}}
equations:
  variables:
    soc: {unit: "1", valid_range: {minimum: 0, maximum: 1}, initial: {property_ref: initial_soc}}
    charge: {unit: kW, valid_range: {minimum: 0, maximum: null}}
    discharge: {unit: kW, valid_range: {minimum: 0, maximum: null}}
  relations:
    - {id: power_balance, expression: "electricity[t] = discharge[t] - charge[t]"}
    - {id: exclusivity, expression: "charge[t] * discharge[t] = 0"}
    - {id: soc_update, expression: "soc[t] = soc[t-1] + (charge[t] * charge_efficiency - discharge[t] / discharge_efficiency) * step_duration / capacity_kwh"}
""",
    "ies.device.grid_connection": """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: ies.device.grid_connection, names: {zh-CN: 电网, en-US: Grid}}
properties: {}
interfaces:
  electricity_import: {type: out, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
  electricity_export: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations: {variables: {}, relations: [{id: excl, expression: "electricity_import[t] * electricity_export[t] = 0"}]}
""",
    "ies.device.electric_load": """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: ies.device.electric_load, names: {zh-CN: 电负荷, en-US: Electric Load}}
properties: {}
interfaces:
  electricity_demand: {type: predefined, source: {mode: data_repeat, data_ref: data/electricity_demand.csv}, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations: {variables: {}, relations: []}
""",
    "ies.device.heat_load": """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: ies.device.heat_load, names: {zh-CN: 热负荷, en-US: Heat Load}}
properties: {}
interfaces:
  heat_demand: {type: predefined, source: {mode: data_repeat, data_ref: data/heat_demand.csv}, carrier: heat, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations: {variables: {}, relations: []}
""",
    "ies.device.cooling_load": """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: ies.device.cooling_load, names: {zh-CN: 冷负荷, en-US: Cooling Load}}
properties: {}
interfaces:
  cool_demand: {type: predefined, source: {mode: data_repeat, data_ref: data/cool_demand.csv}, carrier: cool, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations: {variables: {}, relations: []}
""",
    "ies.device.heat_pump": """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: ies.device.heat_pump, names: {zh-CN: 热泵, en-US: Heat Pump}}
properties:
  cop: {value: 3.0, unit: "1", valid_range: {minimum: 2, maximum: 6.5}}
  rated_heat_kw: {value: 300, unit: kW, valid_range: {minimum: 0, maximum: 1000000}}
interfaces:
  electricity_in: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
  heat_out: {type: out, carrier: heat, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations: {variables: {}, relations: [{id: conv, expression: "heat_out[t] = electricity_in[t] * cop"}]}
""",
    "ies.device.pv": """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: ies.device.pv, names: {zh-CN: 光伏, en-US: PV}}
properties:
  rated_capacity_kwp: {value: 50, unit: kWp, valid_range: {minimum: 0, maximum: 1000000}}
  reference_irradiance: {value: 1000, unit: W/m2, valid_range: {minimum: 0, maximum: null}}
  temp_coeff: {value: -0.004, unit: "1", valid_range: {minimum: -0.01, maximum: 0}}
interfaces:
  solar_irradiance: {type: predefined, source: {mode: data_repeat, data_ref: data/solar_irradiance.csv}, carrier: solar, unit: W/m2, valid_range: {minimum: 0, maximum: 2000}}
  ambient_temperature: {type: predefined, source: {mode: data_repeat, data_ref: data/ambient_temperature.csv}, carrier: environment, unit: "°C", valid_range: {minimum: -50, maximum: 60}}
  electric_out: {type: out, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations: {variables: {}, relations: [{id: gen, expression: "electric_out[t] = rated_capacity_kwp * solar_irradiance[t] / reference_irradiance * (1 + temp_coeff * (ambient_temperature[t] - 25))"}]}
""",
    "ies.device.gas_boiler": """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: ies.device.gas_boiler, names: {zh-CN: 锅炉, en-US: Boiler}}
properties:
  rated_heat_kw: {value: 400, unit: kW, valid_range: {minimum: 0, maximum: 1000000}}
  thermal_efficiency: {value: 0.88, unit: "1", valid_range: {minimum: 0.5, maximum: 1}}
  lhv_kwh_per_m3: {value: 9.5, unit: kWh/m3, valid_range: {minimum: 5, maximum: 20}}
interfaces:
  gas_in: {type: in, carrier: gas, unit: m3/h, valid_range: {minimum: 0, maximum: null}}
  heat_out: {type: out, carrier: heat, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations: {variables: {}, relations: [{id: gen, expression: "heat_out[t] = gas_in[t] * lhv_kwh_per_m3 * thermal_efficiency"}]}
""",
    "ies.device.electric_chiller": """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: ies.device.electric_chiller, names: {zh-CN: 冷机, en-US: Chiller}}
properties:
  rated_cooling_kw: {value: 400, unit: kW, valid_range: {minimum: 0, maximum: 1000000}}
  cop: {value: 3.5, unit: "1", valid_range: {minimum: 1.5, maximum: 8}}
interfaces:
  electricity_in: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
  cool_out: {type: out, carrier: cool, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations: {variables: {}, relations: [{id: gen, expression: "cool_out[t] = electricity_in[t] * cop"}]}
""",
    "ies.device.transport_pipe": """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: ies.device.transport_pipe, names: {zh-CN: 管道, en-US: Pipe}}
properties:
  loss_rate: {value: 0.03, unit: "1", valid_range: {minimum: 0, maximum: 0.5}}
interfaces:
  heat_in: {type: in, carrier: heat, unit: kW, valid_range: {minimum: 0, maximum: null}}
  heat_out: {type: out, carrier: heat, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations: {variables: {}, relations: [{id: loss, expression: "heat_out[t] = heat_in[t] * (1 - loss_rate)"}]}
""",
}

PER_DEVICE_INVALID = [
    ("ies.device.grid-wrong-type", """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: ies.device.grid_connection, names: {zh-CN: 电网, en-US: Grid}}
properties: {}
interfaces:
  electricity_import: {type: magic, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
  electricity_export: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations: {variables: {}, relations: []}
""", "type 必须是"),
    ("ies.device.heat_pump-with-finance", """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: ies.device.heat_pump, names: {zh-CN: 热泵, en-US: HP}}
properties: {cop: {value: 3.2, unit: "1", valid_range: {minimum: 2, maximum: 6}}}
interfaces:
  electricity_in: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
  heat_out: {type: out, carrier: heat, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations: {variables: {}, relations: []}
finance_type: acme.finance.hp
""", "was unexpected"),
    ("ies.device.pipe-with-delay", """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: ies.device.transport_pipe, names: {zh-CN: 管道, en-US: Pipe}}
properties: {loss_rate: {value: 0.02, unit: "1", valid_range: {minimum: 0, maximum: 0.5}}}
interfaces:
  heat_in: {type: in, carrier: heat, unit: kW, valid_range: {minimum: 0, maximum: null}}
  heat_out: {type: out, carrier: heat, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations: {variables: {}, relations: []}
delay_steps: 1
""", "delay_steps"),
    ("ies.device.electric_load-annual", """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: ies.device.electric_load, names: {zh-CN: 电负荷, en-US: Load}}
properties: {}
interfaces:
  electricity_demand: {type: predefined, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations: {variables: {}, relations: []}
annual_energy: 1
""", "annual"),
]


class TestPerDeviceValidStructures:
    @pytest.mark.parametrize("device_id,yaml_text", list(PER_DEVICE_VALID.items()))
    def test_valid_per_device(self, device_id, yaml_text):
        r = _parse(yaml_text, file=f"{device_id}.yaml")
        assert r.ok, f"{device_id} 合法结构应通过: {[d.params.get('detail') for d in r.diagnostics]}"
        assert r.document is not None and r.document.device is not None
        assert r.document.device.id == device_id
        # predefined 必须带 source(data_repeat), 其余类型禁止带 source
        for iface in r.document.interfaces.values():
            if iface.type == "predefined":
                assert iface.source is not None and iface.source.mode == "data_repeat"
            else:
                assert iface.source is None


class TestPerDeviceInvalidStructures:
    @pytest.mark.parametrize("case_id,yaml_text,detail_contains", PER_DEVICE_INVALID)
    def test_invalid_rejected(self, case_id, yaml_text, detail_contains):
        r = _parse(yaml_text, file=f"{case_id}.yaml")
        assert not r.ok and r.document is None
        details = [str(d.params.get("detail", "")) for d in r.diagnostics]
        assert any(detail_contains in d for d in details)
