"""`ies.device-model` 2.0.0 纯协议测试（不依赖设备注册表/数据库）。

覆盖：合法/非法 YAML、五类 interface、source 组合、equations 校验、
模板 inputs 实例化、规范摘要等价性、1.0→2.0 显式迁移。
"""

from __future__ import annotations

import copy

from iesplan.core.yamlmini import YamlParseError
from iesplan.core.yamlmini import load as yaml_load
from iesplan.devices.contracts2 import (
    SCHEMA_ID,
    SCHEMA_VERSION,
    canonical_bytes,
    content_sha256,
    to_dict,
)
from iesplan.devices.migration2 import migrate_v1_to_v2
from iesplan.devices.parser2 import parse_device_model_v2
from iesplan.devices.template2 import instantiate_template

# ---------------------------------------------------------------------------
# 合法样例
# ---------------------------------------------------------------------------

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
  ambient_temperature:
    type: predefined
    carrier: environment
    unit: "°C"
    valid_range:
      minimum: -50
      maximum: 60
    source:
      mode: data_predict
      data_ref: ambient_temperature_prediction
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


def _parse(text: str, file: str = "test.yaml"):
    return parse_device_model_v2(yaml_load(text), file=file)


class TestValidModel:
    def test_heat_pump_valid(self):
        r = _parse(HEAT_PUMP_YAML)
        assert r.ok, [d.params.get("detail") for d in r.diagnostics]
        doc = r.document
        assert doc.schema_version == SCHEMA_VERSION
        assert doc.device.id == "acme.device.heat_pump"
        assert doc.properties["cop"].value == 3.2
        assert doc.interfaces["electricity_in"].type == "in"
        assert doc.interfaces["heat_out"].type == "out"
        assert doc.interfaces["ambient_temperature"].type == "predefined"
        assert doc.interfaces["fixed_temperature"].source.mode == "constant"
        assert doc.interfaces["fixed_temperature"].source.value == 25
        # 缺省 type 规范化为 blind
        assert doc.interfaces["unused_terminal"].type == "blind"
        assert doc.interfaces["unused_terminal"].source is None
        assert len(doc.equations.relations) == 1

    def test_interface_default_blind(self):
        r = _parse(HEAT_PUMP_YAML)
        assert r.document.interfaces["unused_terminal"].type == "blind"

    def test_equations_with_state_variable(self):
        text = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.battery, names: {zh-CN: 电池, en-US: Battery}}
properties:
  charge_efficiency: {value: 0.95, unit: "1", valid_range: {minimum: 0, maximum: 1}}
  discharge_efficiency: {value: 0.95, unit: "1", valid_range: {minimum: 0, maximum: 1}}
  initial_soc: {value: 0.5, unit: "1", valid_range: {minimum: 0, maximum: 1}}
interfaces:
  charge_in: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
  discharge_out: {type: out, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations:
  variables:
    soc:
      unit: "1"
      valid_range: {minimum: 0.1, maximum: 0.9}
      initial: {property_ref: initial_soc}
  relations:
    - id: soc_transition
      expression: "soc[t] = soc[t-1] + charge_in[t] - discharge_out[t]"
"""
        r = _parse(text)
        assert r.ok, [d.params.get("detail") for d in r.diagnostics]
        doc = r.document
        assert doc.equations.variables["soc"].initial_property_ref == "initial_soc"

    def test_empty_sections(self):
        text = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.minimal, names: {zh-CN: 最小, en-US: Minimal}}
properties: {}
interfaces: {}
equations:
  variables: {}
  relations: []
"""
        r = _parse(text)
        assert r.ok, [d.params.get("detail") for d in r.diagnostics]

    def test_boolean_and_string_property(self):
        text = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.switch, names: {zh-CN: 开关, en-US: Switch}}
properties:
  enabled: {value: true, unit: "-"}
  label: {value: "A", unit: "-"}
interfaces: {}
equations: {variables: {}, relations: []}
"""
        r = _parse(text)
        assert r.ok, [d.params.get("detail") for d in r.diagnostics]
        assert r.document.properties["enabled"].value is True


class TestCanonical:
    def test_same_semantics_same_sha(self):
        # 数值字面量 3.2 与 3.20 语义相同 → 规范摘要相同
        r1 = _parse(HEAT_PUMP_YAML)
        text2 = HEAT_PUMP_YAML.replace("value: 3.2", "value: 3.20")
        r2 = _parse(text2)
        assert r1.ok and r2.ok
        assert content_sha256(r1.document) == content_sha256(r2.document)

    def test_unit_spelling_preserved(self):
        # 单位拼写（kW vs kw）是业务单位的一部分，不归一化 → 摘要不同
        r1 = _parse(HEAT_PUMP_YAML)
        text2 = HEAT_PUMP_YAML.replace("unit: kW", "unit: kw")
        r2 = _parse(text2)
        assert r1.ok and r2.ok
        assert content_sha256(r1.document) != content_sha256(r2.document)

    def test_semantics_change_changes_sha(self):
        r1 = _parse(HEAT_PUMP_YAML)
        text2 = HEAT_PUMP_YAML.replace("value: 3.2", "value: 4.0")
        r2 = _parse(text2)
        assert content_sha256(r1.document) != content_sha256(r2.document)

    def test_canonical_bytes_deterministic(self):
        r1 = _parse(HEAT_PUMP_YAML)
        assert canonical_bytes(r1.document) == canonical_bytes(r1.document)

    def test_canonical_roundtrip_dict(self):
        r = _parse(HEAT_PUMP_YAML)
        d = to_dict(r.document)
        assert d["schema"] == SCHEMA_ID
        assert d["schema_version"] == SCHEMA_VERSION
        # 顶层只有六个键
        assert set(d) == {"schema", "schema_version", "device", "properties", "interfaces", "equations"}


class TestInvalidModel:
    def _diag_details(self, text: str):
        r = _parse(text)
        assert not r.ok
        return [d.params.get("detail", "") for d in r.diagnostics]

    def test_unknown_top_field(self):
        d = self._diag_details(
            HEAT_PUMP_YAML + "\nparameters:\n  x: 1\n")
        assert any("Additional properties" in x or "parameters" in x for x in d)

    def test_forbidden_device_version(self):
        # device.version 是 1.0 残留字段，2.0 schema 拒绝
        d = self._diag_details(HEAT_PUMP_YAML.replace(
            "device:\n  id: acme.device.heat_pump",
            "device:\n  id: acme.device.heat_pump\n  version: 1.0.0",
        ))
        assert any("Additional properties" in x or "version" in x for x in d)

    def test_wrong_schema_version(self):
        d = self._diag_details(HEAT_PUMP_YAML.replace('schema_version: "2.0.0"', 'schema_version: "9.9.9"'))
        # jsonschema const 错误消息为 "'2.0.0' was expected"；确保版本被拒绝
        assert d
        assert any("2.0.0" in x for x in d)

    def test_old_schema_1(self):
        d = self._diag_details(HEAT_PUMP_YAML.replace("ies.device-model", "ies.device-model-x"))
        assert d

    def test_property_bad_type(self):
        d = self._diag_details(HEAT_PUMP_YAML.replace("value: 3.2", "value: [1,2]"))
        assert d

    def test_property_nan_value(self):
        # 1e999 溢出为 inf，必须被拒绝（不允许 NaN/Infinity 进入模型）
        d = self._diag_details(HEAT_PUMP_YAML.replace("value: 3.2", "value: 1e999"))
        assert d

    def test_property_range_inverted(self):
        d = self._diag_details(
            HEAT_PUMP_YAML.replace(
                "minimum: 1\n      maximum: 10",
                "minimum: 10\n      maximum: 1",
            )
        )
        assert any("minimum" in x and "maximum" in x for x in d)

    def test_interface_bad_type(self):
        d = self._diag_details(HEAT_PUMP_YAML.replace("type: in", "type: magic"))
        assert d

    def test_interface_source_on_in(self):
        text = HEAT_PUMP_YAML.replace(
            "  electricity_in:\n    type: in\n    carrier: electricity",
            "  electricity_in:\n    type: in\n    carrier: electricity\n"
            "    source:\n      mode: constant\n      value: 5",
        )
        d = self._diag_details(text)
        assert any("禁止声明 source" in x for x in d)

    def test_predefined_missing_source(self):
        text = HEAT_PUMP_YAML.replace(
            "    source:\n      mode: data_predict\n      data_ref: ambient_temperature_prediction",
            "",
        )
        # 删除后 predefined 无 source → 非法
        d = self._diag_details(text)
        assert any("必须声明 source" in x for x in d)

    def test_blind_with_source(self):
        text = HEAT_PUMP_YAML.replace(
            "  unused_terminal:\n    carrier: heat",
            "  unused_terminal:\n    type: blind\n    carrier: heat\n"
            "    source:\n      mode: constant\n      value: 1",
        )
        d = self._diag_details(text)
        assert any("禁止声明 source" in x for x in d)

    def test_constant_source_missing_value(self):
        text = HEAT_PUMP_YAML.replace("      mode: constant\n      value: 25", "      mode: constant")
        d = self._diag_details(text)
        assert any("必须声明 value" in x for x in d)

    def test_data_repeat_missing_data_ref(self):
        text = HEAT_PUMP_YAML.replace(
            "      mode: data_predict\n      data_ref: ambient_temperature_prediction",
            "      mode: data_predict",
        )
        d = self._diag_details(text)
        assert any("必须声明 data_ref" in x for x in d)

    def test_unknown_carrier_vocabulary_rejected(self):
        text = HEAT_PUMP_YAML.replace("carrier: electricity", "carrier: ENERGY!")
        d = self._diag_details(text)
        assert d

    def test_bad_unit(self):
        d = self._diag_details(HEAT_PUMP_YAML.replace("unit: kW", "unit: banana-unit"))
        assert any("无法识别" in x for x in d)

    def test_equation_unknown_reference(self):
        text = HEAT_PUMP_YAML.replace("heat_out[t] = electricity_in[t] * cop",
                                      "heat_out[t] = mystery_thing[t] * cop")
        d = self._diag_details(text)
        assert any("未声明的标识符" in x for x in d)

    def test_equation_future_reference(self):
        text = HEAT_PUMP_YAML.replace("heat_out[t] = electricity_in[t] * cop",
                                      "heat_out[t] = electricity_in[t+1] * cop")
        d = self._diag_details(text)
        assert any("禁止未来引用" in x for x in d)

    def test_equation_arbitrary_index(self):
        text = HEAT_PUMP_YAML.replace("heat_out[t] = electricity_in[t] * cop",
                                      "heat_out[t] = electricity_in[x] * cop")
        d = self._diag_details(text)
        assert any("索引必须是" in x for x in d)

    def test_equation_duplicate_relation_id(self):
        # 直接构造重复 relation ID（避免文本替换缩进问题）
        raw = yaml_load(HEAT_PUMP_YAML)
        rel = raw["equations"]["relations"][0]
        raw["equations"]["relations"].append(copy.deepcopy(rel))
        r = parse_device_model_v2(raw)
        assert not r.ok
        assert any("重复" in d.params.get("detail", "") for d in r.diagnostics)

    def test_equation_cycle(self):
        text = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.cycle, names: {zh-CN: 环, en-US: Cycle}}
properties: {}
interfaces: {}
equations:
  variables:
    a: {unit: "1"}
    b: {unit: "1"}
  relations:
    - id: r1
      expression: "a[t] = b[t-1] + 1"
    - id: r2
      expression: "b[t] = a[t-1] + 1"
"""
        d = self._diag_details(text)
        assert any("循环引用" in x for x in d)

    def test_equation_bad_syntax(self):
        text = HEAT_PUMP_YAML.replace("heat_out[t] = electricity_in[t] * cop",
                                      "heat_out[t] = electricity_in[t] ** cop; import os")
        d = self._diag_details(text)
        assert d

    def test_duplicate_yaml_keys(self):
        import pytest

        with pytest.raises(YamlParseError):
            yaml_load("schema: ies.device-model\nschema: ies.device-model")

    def test_aggregated_diagnostics(self):
        # 多个独立错误一次返回（interfaces type + property unit + equation）
        text = """
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
        r = _parse(text)
        assert not r.ok
        assert len(r.diagnostics) >= 3
        # 多个独立错误一次返回（interfaces type + property unit + equation）
        text = """
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
        r = _parse(text)
        assert not r.ok
        assert len(r.diagnostics) >= 3

    def test_invalid_never_produces_document(self):
        text = HEAT_PUMP_YAML.replace("unit: kW", "unit: banana-unit")
        r = _parse(text)
        assert r.document is None


class TestTemplateInstantiation:
    TEMPLATE = """
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
    new_prop:
      value: {type: number, unit: kW, valid_range: {minimum: 0, maximum: 500}}
  interfaces:
    electric_demand:
      source:
        data_ref:
          type: data_repeat
          data_ref: typical_day_load
properties:
  cop: {value: 3.0, unit: "1", valid_range: {minimum: 1, maximum: 10}}
interfaces:
  electric_demand:
    type: predefined
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: null}
    source: {mode: data_repeat, data_ref: typical_day_load}
equations:
  variables: {}
  relations: []
"""

    def test_template_parse(self):
        r = _parse(self.TEMPLATE)
        assert r.ok, [d.params.get("detail") for d in r.diagnostics]
        assert r.document.inputs is not None

    def test_overwrite_existing_field(self):
        res, diags = instantiate_template(
            yaml_load(self.TEMPLATE), {"properties": {"peak_power_kw": {"value": 250}}}
        )
        assert res is not None, [d.params.get("detail") for d in diags]
        doc = res.document
        assert doc.properties["peak_power_kw"].value == 250
        assert doc.properties["cop"].value == 3.0  # 其他字段不受影响

    def test_add_new_field(self):
        res, _ = instantiate_template(
            yaml_load(self.TEMPLATE), {"properties": {"new_prop": {"value": 120}}}
        )
        doc = res.document
        assert doc.properties["new_prop"].value == 120
        assert doc.properties["new_prop"].unit == "kW"
        assert doc.properties["new_prop"].valid_range == (0.0, 500.0)

    def test_boolean_add_default_unit(self):
        res, _ = instantiate_template(
            yaml_load(self.TEMPLATE), {"properties": {"is_switchable": {"value": True}}}
        )
        assert res.document.properties["is_switchable"].unit == "-"

    def test_data_ref_overwrite(self):
        res, _ = instantiate_template(
            yaml_load(self.TEMPLATE),
            {"interfaces": {"electric_demand": {"source": {"data_ref": "my_curve"}}}},
        )
        src = res.document.interfaces["electric_demand"].source
        assert src.data_ref == "my_curve"
        assert src.mode == "data_repeat"

    def test_add_interface_fields_declared_by_inputs(self):
        """inputs 可添加任意同构字段；完整模型校验负责判断新增结构是否合法。"""
        raw = yaml_load(
            """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.load_template, names: {zh-CN: 负荷模板, en-US: Load Template}}
inputs:
  interfaces:
    demand:
      type: object
      fields:
        type: {type: string}
        carrier: {type: string}
        unit: {type: string}
        valid_range:
          minimum: {type: number, unit: kW}
          maximum: {type: number, unit: kW}
        source:
          mode: {type: string}
          data_ref: {type: data_repeat, data_ref: load_curve}
properties: {}
interfaces: {}
equations: {variables: {}, relations: []}
"""
        )
        values = {
            "interfaces": {
                "demand": {
                    "type": "predefined",
                    "carrier": "electricity",
                    "unit": "kW",
                    "valid_range": {"minimum": 0, "maximum": 1000},
                    "source": {"mode": "data_repeat", "data_ref": "project_load"},
                }
            }
        }
        res, diags = instantiate_template(raw, values)
        assert res is not None, [d.params.get("detail") for d in diags]
        iface = res.document.interfaces["demand"]
        assert iface.type == "predefined"
        assert iface.source.data_ref == "project_load"

    def test_inputs_removed_after_instantiation(self):
        res, _ = instantiate_template(
            yaml_load(self.TEMPLATE), {"properties": {"peak_power_kw": {"value": 250}}}
        )
        assert res.document.inputs is None
        assert '"inputs"' not in res.canonical_text

    def test_undeclared_field_rejected(self):
        res, diags = instantiate_template(yaml_load(self.TEMPLATE), {"properties": {"evil": {"value": 1}}})
        assert res is None
        assert any("未在模板 inputs 中声明" in d.params.get("detail", "") for d in diags)

    def test_type_error_rejected(self):
        res, diags = instantiate_template(
            yaml_load(self.TEMPLATE),
            {"properties": {"peak_power_kw": {"value": "x"}}},
        )
        assert res is None
        assert any("期望 number" in d.params.get("detail", "") for d in diags)

    def test_range_error_rejected(self):
        res, diags = instantiate_template(
            yaml_load(self.TEMPLATE),
            {"properties": {"peak_power_kw": {"value": 9999}}},
        )
        assert res is None
        assert any("高于 valid_range.maximum" in d.params.get("detail", "") for d in diags)

    def test_equivalent_direct_yaml_same_sha(self):
        res, diags = instantiate_template(
            yaml_load(self.TEMPLATE),
            {
                "properties": {
                    "peak_power_kw": {"value": 250},
                    "is_switchable": {"value": True},
                    "new_prop": {"value": 120},
                }
            },
        )
        assert res is not None, [d.params.get("detail") for d in diags]
        direct = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.electric_load, names: {zh-CN: 电负荷, en-US: Electric Load}}
properties:
  cop: {value: 3.0, unit: "1", valid_range: {minimum: 1, maximum: 10}}
  peak_power_kw: {value: 250, unit: kW, valid_range: {minimum: 0, maximum: 1000}}
  is_switchable: {value: true, unit: "-"}
  new_prop: {value: 120, unit: kW, valid_range: {minimum: 0, maximum: 500}}
interfaces:
  electric_demand:
    type: predefined
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: null}
    source: {mode: data_repeat, data_ref: typical_day_load}
equations:
  variables: {}
  relations: []
"""
        r_direct = _parse(direct)
        assert r_direct.ok
        assert content_sha256(r_direct.document) == res.content_sha256

    def test_final_model_passes_full_validation(self):
        res, _ = instantiate_template(
            yaml_load(self.TEMPLATE), {"properties": {"peak_power_kw": {"value": 250}}}
        )
        # 实例化后文档应通过完整 2.0.0 校验（解析器内部已保证；此处以 to_dict 往返验证）
        assert res is not None
        r = parse_device_model_v2(to_dict(res.document))
        assert r.ok

    def test_template_without_inputs_rejected(self):
        no_inputs = HEAT_PUMP_YAML  # 普通模型不是模板
        res, diags = instantiate_template(yaml_load(no_inputs), {})
        assert res is None
        assert any("必须声明顶层 inputs" in d.params.get("detail", "") for d in diags)

    def test_receipt_contains_traceability(self):
        res, _ = instantiate_template(
            yaml_load(self.TEMPLATE), {"properties": {"peak_power_kw": {"value": 250}}}
        )
        receipt = res.receipt
        assert receipt["instantiator"] == "ies.device-model.instantiator@1.0.0"
        assert receipt["template_sha256"]
        assert receipt["inputs_sha256"]
        assert receipt["content_sha256"] == res.content_sha256
        assert len(receipt["content_sha256"]) == 64


# ---------------------------------------------------------------------------
# 1.0 → 2.0 显式迁移
# ---------------------------------------------------------------------------

V1_LOAD_YAML = """
schema: ies.device-model
schema_version: "1.0.0"

device:
  id: ies.device.electric_load
  version: "1.2.0"
  names:
    zh-CN: 电负荷
    en-US: Electric Load
  model_method: data_repeat
  stateful: false
  fidelity: medium
  energy_carriers: [electric]
  capabilities: [load, switchable]

parameters:
  peak_power_kw:
    value_type: number
    quantity: power
    unit: kW
    required: false
    default: 0
    minimum: 0
    maximum: 10000000
  is_switchable:
    value_type: boolean
    quantity: ratio
    unit: "-"
    required: false
    default: false

ports:
  electric_in:
    carrier: electric
    direction: in
    quantity: power
    unit: kW
    capacity_parameter: peak_power_kw

data_inputs:
  e_load:
    value_type: number
    quantity: energy
    unit: kWh
    required: true

states: {}

model_commands:
  run: ies.modeling.electric_load@1.0.0
"""


class TestMigrationV1:
    def test_migrate_electric_load(self):
        result = migrate_v1_to_v2(yaml_load(V1_LOAD_YAML))
        assert result.ok, [d.params.get("detail") for d in result.diagnostics]
        doc = result.document
        assert doc.schema_version == SCHEMA_VERSION
        assert doc.device.id == "ies.device.electric_load"
        # parameters → properties
        assert doc.properties["peak_power_kw"].value == 0
        assert doc.properties["peak_power_kw"].unit == "kW"
        # ports → interfaces（carrier 归一化 electric → electricity）
        iface = doc.interfaces["electric_in"]
        assert iface.type == "in"
        assert iface.carrier == "electricity"
        assert doc.interfaces["electric_in"].valid_range[0] == 0
        # data_inputs → predefined interface
        dload = doc.interfaces.get("e_load")
        assert dload is not None
        assert dload.type == "predefined"
        assert dload.source.mode == "data_repeat"
        # model_commands / states / device version 等全部移除
        assert not hasattr(doc, "model_commands")
        assert "model_commands" not in to_dict(doc)

    def test_migrate_rejects_mechanism_with_data_inputs(self):
        # mechanism 设备的 data_inputs 无法映射到 predefined 来源（constant 无外部文件）
        raw = yaml_load(V1_LOAD_YAML)
        raw["device"]["model_method"] = "mechanism"
        result = migrate_v1_to_v2(raw)
        assert not result.ok
        assert any("不支持迁移" in d.params.get("detail", "") for d in result.diagnostics)

    def test_migrate_data_predict_keeps_mode(self):
        raw = yaml_load(V1_LOAD_YAML)
        raw["device"]["model_method"] = "data_predict"
        result = migrate_v1_to_v2(raw)
        assert result.ok, [d.params.get("detail") for d in result.diagnostics]
        src = result.document.interfaces["e_load"].source
        assert src.mode == "data_predict"

    def test_migrate_deterministic(self):
        r1 = migrate_v1_to_v2(yaml_load(V1_LOAD_YAML))
        r2 = migrate_v1_to_v2(yaml_load(V1_LOAD_YAML))
        assert content_sha256(r1.document) == content_sha256(r2.document)

    def test_migrated_output_passes_v2_validation(self):
        r = migrate_v1_to_v2(yaml_load(V1_LOAD_YAML))
        assert r.ok, [d.params.get("detail") for d in r.diagnostics]
        r2 = parse_device_model_v2(to_dict(r.document))
        assert r2.ok

    def test_v2_input_rejected_by_migrator(self):
        # 已经是 2.0.0 的文件不能走 1.0 迁移
        result = migrate_v1_to_v2(yaml_load(HEAT_PUMP_YAML))
        assert not result.ok
