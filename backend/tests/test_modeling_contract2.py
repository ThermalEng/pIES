"""`ies.modeling.contribution` 2.0.0 纯协议测试(不依赖注册表/数据库)。

覆盖: 公共数学贡献(变量/关系/状态/接口流/结果映射)、版本化公共 AST、
方程合法/非法(未知引用、单位冲突、循环引用、非法表达式、输出冲突、
状态初值、property 非时变、blind 引用)、确定性(相同输入相同摘要)。
"""

from __future__ import annotations

import inspect

from iesplan.core.yamlmini import load as yaml_load
from iesplan.devices.contracts2 import (
    DeviceInfo,
    DeviceModelDocument,
    EquationRelation,
    Equations,
    EquationVariable,
    InterfaceSpec,
    PropertySpec,
    SourceSpec,
    content_sha256,
)
from iesplan.devices.parser2 import parse_device_model_v2
from iesplan.modeling.contract2 import (
    MOD_EQ_BLIND_REF,
    MOD_EQ_CYCLE,
    MOD_EQ_OUTPUT_CONFLICT,
    MOD_EQ_PROPERTY_INDEXED,
    MOD_EQ_STATE_NO_INITIAL,
    MOD_EQ_SYNTAX,
    MOD_EQ_UNIT_CONFLICT,
    MOD_EQ_UNKNOWN_REF,
    BinaryNode,
    DeviceMathContribution,
    NumberNode,
    RefNode,
    build_math_contribution,
    contribution_to_dict,
)


def _doc(text: str) -> DeviceModelDocument:
    r = parse_device_model_v2(yaml_load(text), file="test.yaml")
    assert r.ok, [d.params.get("detail") for d in r.diagnostics]
    return r.document


def _make_document(
    *,
    device_id: str = "acme.device.x",
    properties: dict | None = None,
    interfaces: dict | None = None,
    variables: dict | None = None,
    relations: list[tuple[str, str]] | None = None,
) -> DeviceModelDocument:
    """手工构造 2.0 descriptor(绕过 parser2,用于 contract2 防御性校验测试)。"""

    def _range(r):
        return (r["minimum"], r["maximum"]) if r else None

    props = {
        pid: PropertySpec(id=pid, value=p["value"], unit=p["unit"], valid_range=_range(p.get("valid_range")))
        for pid, p in (properties or {}).items()
    }
    ifaces: dict[str, InterfaceSpec] = {}
    for iid, i in (interfaces or {}).items():
        src = None
        if i.get("source"):
            s = i["source"]
            src = SourceSpec(mode=s["mode"], value=s.get("value"), data_ref=s.get("data_ref"))
        ifaces[iid] = InterfaceSpec(
            id=iid,
            type=i.get("type", "blind"),
            carrier=i.get("carrier", "electricity"),
            unit=i["unit"],
            valid_range=_range(i.get("valid_range")) or (None, None),
            source=src,
        )
    eq_vars = {
        vid: EquationVariable(
            id=vid,
            unit=v["unit"],
            valid_range=_range(v.get("valid_range")),
            initial_property_ref=v.get("initial_property_ref"),
        )
        for vid, v in (variables or {}).items()
    }
    rels = tuple(EquationRelation(id=rid, expression=expr) for rid, expr in (relations or []))
    return DeviceModelDocument(
        device=DeviceInfo(id=device_id),
        properties=props,
        interfaces=ifaces,
        equations=Equations(variables=eq_vars, relations=rels),
    )


def _out(name: str, unit: str = "kW", vr=None) -> dict:
    return {"type": "out", "carrier": "electricity", "unit": unit,
            "valid_range": vr or {"minimum": 0, "maximum": None}}


HEAT_PUMP = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.heat_pump, names: {zh-CN: 热泵, en-US: Heat Pump}}
properties:
  cop: {value: 3.2, unit: "1", valid_range: {minimum: 1, maximum: 10}}
interfaces:
  electricity_in: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
  heat_out: {type: out, carrier: heat, unit: kW, valid_range: {minimum: 0, maximum: null}}
  unused_terminal: {carrier: heat, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations:
  variables: {}
  relations:
    - id: heat_conversion
      expression: "heat_out[t] = electricity_in[t] * cop"
"""

BATTERY = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.battery, names: {zh-CN: 电池, en-US: Battery}}
properties:
  charge_efficiency: {value: 0.95, unit: "1", valid_range: {minimum: 0, maximum: 1}}
  initial_soc: {value: 0.5, unit: "1", valid_range: {minimum: 0, maximum: 1}}
interfaces:
  power: {type: bidirectional, carrier: electricity, unit: kW, valid_range: {minimum: -100, maximum: 100}}
equations:
  variables:
    soc:
      unit: "1"
      valid_range: {minimum: 0.1, maximum: 0.9}
      initial: {property_ref: initial_soc}
  relations:
    - id: soc_transition
      expression: "soc[t] = soc[t-1] + power[t] * charge_efficiency"
"""


class TestValidContribution:
    def test_heat_pump_contribution(self):
        doc = _doc(HEAT_PUMP)
        r = build_math_contribution(doc)
        assert r.ok
        c = r.contribution
        assert c.device_id == "acme.device.heat_pump"
        assert c.content_sha256 == content_sha256(doc)
        assert c.verify()
        # 变量: property + 内部变量(接口进入 flows)
        assert set(c.variables) == {"cop"}
        assert c.variables["cop"].kind == "property"
        assert c.variables["cop"].value == 3.2
        assert c.variables["cop"].unit == "1"
        # 接口流: 五类语义 + 缺省 blind
        assert set(c.interfaces) == {"electricity_in", "heat_out", "unused_terminal"}
        assert c.interfaces["heat_out"].type == "out"
        assert c.interfaces["heat_out"].carrier == "heat"
        assert c.interfaces["unused_terminal"].type == "blind"
        assert c.interfaces["unused_terminal"].source_mode is None
        # 关系: 版本化公共 AST(无 eval/函数路径)
        assert len(c.relations) == 1
        rel = c.relations[0]
        assert rel.id == "heat_conversion"
        assert rel.output == "heat_out"
        assert isinstance(rel.ast.rhs_root, BinaryNode)
        assert rel.ast.rhs_root.op == "*"
        assert isinstance(rel.ast.rhs_root.left, RefNode)
        assert rel.ast.rhs_root.left.name == "electricity_in"
        assert isinstance(rel.ast.rhs_root.right, RefNode)
        assert rel.ast.rhs_root.right.name == "cop"
        assert [(x.name, x.offset) for x in rel.ast.lhs_refs] == [("heat_out", 0)]
        assert [(x.name, x.offset) for x in rel.ast.rhs_refs] == [("electricity_in", 0), ("cop", 0)]
        assert rel.is_state_transition is False
        # 状态/结果映射
        assert c.states == ()
        assert [(x.variable, x.unit, x.kind) for x in c.results] == [("heat_out", "kW", "interface")]

    def test_battery_state_and_results(self):
        r = build_math_contribution(_doc(BATTERY))
        assert r.ok, [d.params.get("detail") for d in r.diagnostics]
        c = r.contribution
        assert c.states == ("soc",)
        assert c.relations[0].is_state_transition is True
        assert c.variables["soc"].is_state is True
        # 状态变量在自身 rhs 自引用 [t-1](合法状态递推)
        assert ("soc", -1) in [(x.name, x.offset) for x in c.relations[0].ast.rhs_refs]
        # 结果映射: 关系输出(本设备唯一输出是 soc;power 是外部双向流,未被方程定义)
        assert [(x.variable, x.unit, x.kind) for x in c.results] == [
            ("soc", "1", "variable"),
        ]

    def test_relation_without_equations(self):
        text = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.minimal, names: {zh-CN: 最小, en-US: Minimal}}
properties: {}
interfaces: {}
equations: {variables: {}, relations: []}
"""
        r = build_math_contribution(_doc(text))
        assert r.ok
        assert r.contribution is not None and r.contribution.verify()

    def test_in_interface_as_output_allowed(self):
        # 可中断负荷模式: 设备方程定义自己的输入接口
        doc = _make_document(
            interfaces={
                "electricity_in": {"type": "in", "unit": "kW",
                                   "valid_range": {"minimum": 0, "maximum": None}},
            },
            relations=[("r1", "electricity_in[t] = 1")],
        )
        r = build_math_contribution(doc)
        assert r.ok, [d.params.get("detail") for d in r.diagnostics]
        assert r.contribution.results[0].variable == "electricity_in"

    def test_deterministic_same_input_same_digest(self):
        a = build_math_contribution(_doc(HEAT_PUMP)).contribution
        b = build_math_contribution(_doc(HEAT_PUMP)).contribution
        assert a.canonical_text == b.canonical_text
        assert a.contribution_sha256 == b.contribution_sha256

    def test_number_spelling_normalized(self):
        # 3.0 与 3.20 语义相同 → 相同规范文本与摘要
        a = build_math_contribution(_doc(HEAT_PUMP)).contribution
        b = build_math_contribution(_doc(HEAT_PUMP.replace("value: 3.2", "value: 3.20"))).contribution
        assert a.contribution_sha256 == b.contribution_sha256

    def test_relation_order_insensitive(self):
        # 关系声明顺序是设备内容的一部分(规范设备摘要随列表顺序变化),贡献
        # 内容锁跟随设备摘要;但去掉内容锁后贡献结构完全一致(关系按 id 规范化)
        text_a = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.two, names: {zh-CN: 双关系}}
properties: {}
interfaces:
  a: {type: out, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
  b: {type: out, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations:
  variables: {}
  relations:
    - id: r2
      expression: "b[t] = 1"
    - id: r1
      expression: "a[t] = 2"
"""
        text_b = text_a.replace(
            '    - id: r2\n      expression: "b[t] = 1"\n    - id: r1\n      expression: "a[t] = 2"',
            '    - id: r1\n      expression: "a[t] = 2"\n    - id: r2\n      expression: "b[t] = 1"',
        )
        ca = build_math_contribution(_doc(text_a)).contribution
        cb = build_math_contribution(_doc(text_b)).contribution
        # 设备内容摘要随声明顺序变化 → 贡献内容锁变化(内容寻址语义)
        assert ca.content_sha256 != cb.content_sha256
        assert ca.contribution_sha256 != cb.contribution_sha256
        # 去除内容锁后,贡献规范结构完全一致(关系按键排序,与声明顺序无关)
        da = contribution_to_dict(ca)
        db = contribution_to_dict(cb)
        da.pop("content_sha256")
        db.pop("content_sha256")
        assert da == db

    def test_semantics_change_changes_digest(self):
        a = build_math_contribution(_doc(HEAT_PUMP)).contribution
        b = build_math_contribution(_doc(HEAT_PUMP.replace("value: 3.2", "value: 4.0"))).contribution
        assert a.contribution_sha256 != b.contribution_sha256

    def test_ast_number_node(self):
        doc = _make_document(interfaces={"a": _out("a")}, relations=[("r1", "a[t] = 42")])
        c = build_math_contribution(doc).contribution
        assert isinstance(c.relations[0].ast.rhs_root, NumberNode)
        assert c.relations[0].ast.rhs_root.value == 42.0


class TestInvalidEquations:
    def _codes(self, doc: DeviceModelDocument) -> list[str]:
        r = build_math_contribution(doc)
        assert not r.ok
        assert r.contribution is None
        return [d.code for d in r.diagnostics]

    def test_unknown_reference(self):
        doc = _make_document(
            interfaces={"a": _out("a")},
            relations=[("r1", "a[t] = mystery_thing[t]")],
        )
        assert MOD_EQ_UNKNOWN_REF in self._codes(doc)

    def test_unit_conflict_initial_property(self):
        # 状态变量单位 "1" 但 initial property 单位 kWh → 量纲不兼容
        doc = _make_document(
            properties={"initial_soc": {"value": 0.5, "unit": "kWh",
                                        "valid_range": {"minimum": 0, "maximum": 1}}},
            variables={"soc": {"unit": "1", "initial_property_ref": "initial_soc"}},
            relations=[("r1", "soc[t] = soc[t-1] + 1")],
        )
        assert MOD_EQ_UNIT_CONFLICT in self._codes(doc)

    def test_cycle(self):
        doc = _make_document(
            variables={"a": {"unit": "1"}, "b": {"unit": "1"}},
            relations=[("r1", "a[t] = b[t-1] + 1"), ("r2", "b[t] = a[t-1] + 1")],
        )
        assert MOD_EQ_CYCLE in self._codes(doc)

    def test_cycle_self_reference_allowed(self):
        # soc[t] = soc[t-1] 是合法状态递推,不构成循环
        doc = _make_document(
            properties={"initial_soc": {"value": 0.5, "unit": "1",
                                        "valid_range": {"minimum": 0, "maximum": 1}}},
            variables={"soc": {"unit": "1", "initial_property_ref": "initial_soc"}},
            relations=[("r1", "soc[t] = soc[t-1] + 1")],
        )
        r = build_math_contribution(doc)
        assert r.ok, [d.params.get("detail") for d in r.diagnostics]

    def test_syntax_decimal_rejected(self):
        # 公共语法契约(equation_grammar)不允许小数/指数字面量(数字只作占位符);
        # 数值以 property 常量或预定义数据表达,不在方程字面量中
        for literal in ("0.5", "1e3", "3."):
            doc = _make_document(
                interfaces={"a": _out("a")},
                relations=[("r1", f"a[t] = {literal}")],
            )
            assert MOD_EQ_SYNTAX in self._codes(doc), literal

    def test_syntax_function_call(self):
        doc = _make_document(
            interfaces={"a": _out("a")},
            relations=[("r1", "a[t] = sin(a[t])")],
        )
        assert MOD_EQ_SYNTAX in self._codes(doc)

    def test_syntax_illegal_character(self):
        doc = _make_document(
            interfaces={"a": _out("a")},
            relations=[("r1", "a[t] = b[t] @ 2")],
        )
        assert MOD_EQ_SYNTAX in self._codes(doc)

    def test_syntax_future_reference(self):
        doc = _make_document(
            interfaces={"a": _out("a")},
            relations=[("r1", "a[t] = a[t+1]")],
        )
        assert MOD_EQ_SYNTAX in self._codes(doc)

    def test_syntax_bad_index(self):
        doc = _make_document(
            interfaces={"a": _out("a")},
            relations=[("r1", "a[t] = b[x]")],
        )
        assert MOD_EQ_SYNTAX in self._codes(doc)

    def test_syntax_missing_equals(self):
        doc = _make_document(interfaces={"a": _out("a")}, relations=[("r1", "a[t] b[t]")])
        assert MOD_EQ_SYNTAX in self._codes(doc)

    def test_syntax_lhs_multi_var(self):
        doc = _make_document(
            interfaces={"a": _out("a"), "b": _out("b")},
            relations=[("r1", "a[t] + b[t] = 1")],
        )
        assert MOD_EQ_SYNTAX in self._codes(doc)

    def test_syntax_no_eval(self):
        # 方程语言禁止任意代码: import / 分号 / 属性访问 / 函数调用
        for evil in (
            "a[t] = 1; import os",
            "a[t] = b[t].__class__",
            "a[t] = eval('1')",
            "a[t] = __import__('os')",
        ):
            doc = _make_document(interfaces={"a": _out("a")}, relations=[("r1", evil)])
            assert MOD_EQ_SYNTAX in self._codes(doc), evil

    def test_output_defined_twice(self):
        doc = _make_document(
            variables={"a": {"unit": "1"}},
            relations=[("r1", "a[t] = 1"), ("r2", "a[t] = 2")],
        )
        assert MOD_EQ_OUTPUT_CONFLICT in self._codes(doc)

    def test_lhs_property_rejected(self):
        doc = _make_document(
            properties={"cop": {"value": 3.0, "unit": "1"}},
            interfaces={"a": _out("a")},
            relations=[("r1", "cop = a[t] * cop")],
        )
        assert MOD_EQ_OUTPUT_CONFLICT in self._codes(doc)

    def test_lhs_predefined_rejected(self):
        doc = _make_document(
            interfaces={"demand": {
                "type": "predefined", "unit": "kW",
                "valid_range": {"minimum": 0, "maximum": None},
                "source": {"mode": "constant", "value": 5},
            }},
            relations=[("r1", "demand[t] = 7")],
        )
        assert MOD_EQ_OUTPUT_CONFLICT in self._codes(doc)

    def test_state_without_initial(self):
        doc = _make_document(
            interfaces={"in_flow": {"type": "in", "unit": "m3/h",
                                    "valid_range": {"minimum": 0, "maximum": None}}},
            variables={"level": {"unit": "m3"}},
            relations=[("r1", "level[t] = level[t-1] + in_flow[t]")],
        )
        assert MOD_EQ_STATE_NO_INITIAL in self._codes(doc)

    def test_property_indexed(self):
        doc = _make_document(
            properties={"cop": {"value": 3.0, "unit": "1"}},
            interfaces={"a": _out("a")},
            relations=[("r1", "a[t] = cop[t-1]")],
        )
        assert MOD_EQ_PROPERTY_INDEXED in self._codes(doc)

    def test_blind_referenced(self):
        doc = _make_document(
            interfaces={"a": _out("a"), "status": {"type": "blind", "unit": "1",
                                                   "valid_range": {"minimum": 0, "maximum": None}}},
            relations=[("r1", "a[t] = status[t]")],
        )
        assert MOD_EQ_BLIND_REF in self._codes(doc)

    def test_invalid_never_produces_contribution(self):
        doc = _make_document(interfaces={"a": _out("a")}, relations=[("r1", "a[t] = nope[t]")])
        r = build_math_contribution(doc)
        assert not r.ok
        assert r.contribution is None

    def test_aggregated_diagnostics(self):
        # 同一文档多个独立错误一次返回(未知引用 + property 索引)
        doc = _make_document(
            properties={"cop": {"value": 3.0, "unit": "1"}},
            interfaces={"a": _out("a")},
            relations=[("r1", "a[t] = cop[t-1] * nope[t]")],
        )
        codes = self._codes(doc)
        assert MOD_EQ_UNKNOWN_REF in codes
        assert MOD_EQ_PROPERTY_INDEXED in codes


class TestNoOldModelCommand:
    def test_no_old_modeling_imports(self):
        # 2.0 纯协议模块的 import 不指向旧 1.0 ModelCommand/DeviceSpec 模块
        import ast as _ast
        import inspect

        import iesplan.modeling.contract2 as contract2

        tree = _ast.parse(inspect.getsource(contract2))
        imported: list[str] = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, _ast.ImportFrom):
                imported.append(node.module or "")
        assert not any(
            "modeling.command" in m or "devspec" in m or "modeling.functions" in m
            for m in imported
        ), imported
        # 模块命名空间中不存在旧 1.0 类型
        assert "ModelCommand" not in dir(contract2)
        assert "DeviceSpec" not in dir(contract2)

    def test_contribution_public_dict(self):
        c = build_math_contribution(_doc(HEAT_PUMP)).contribution
        d = contribution_to_dict(c)
        assert d["schema"] == "ies.modeling.contribution"
        assert d["schema_version"] == "2.0.0"
        assert d["equation_ast"]["version"] == "2.0.0"
        assert set(d) == {
            "schema", "schema_version", "device_id", "content_sha256",
            "equation_ast", "variables", "interfaces", "relations",
            "states", "results",
        }
        assert isinstance(c, DeviceMathContribution)

    def test_no_dynamic_import(self):
        import iesplan.modeling.contract2 as contract2

        src = inspect.getsource(contract2)
        assert "__import__" not in src
        assert "importlib" not in src
        assert "exec(" not in src
