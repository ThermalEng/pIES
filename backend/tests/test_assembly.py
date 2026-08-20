"""装配与检查模块测试(04-assembly-checker.md):纯 pytest,无需数据库。

覆盖:
- 诊断码登记(ASM 域 ↔ 消息键/修复键);
- 阶段 A 语法/结构(ASM-SYN-001..005,失败时 spec=None 阶段隔离);
- 阶段 B 连接合法性(ASM-EDGE-001..009,含母线汇合写法放行);
- 阶段 C 模型可解性(ASM-REF/INPUT/PIPE 全码);
- 阶段 D 整体可解性(ASM-SOLV-001..007 + 母线汇总);
- 约束表达式(ASM-CONST-001..003);
- builder 确定性 / loss_rate 管道包裹 / check_graph_inputs 闸门。
"""

import textwrap

from iesplan.assembly import (
    CheckContext,
    CheckResult,
    build_assembly,
    build_assembly_text,
    check_assembly,
    check_assembly_text,
    check_graph_inputs,
    dumps_assembly,
    parse_assembly,
)
from iesplan.assembly.diags import (
    ASM_ALL_CODES,
    ASM_CONST_DIM,
    ASM_CONST_SYNTAX,
    ASM_CONST_UNDEF,
    ASM_EDGE_BAD_SINK,
    ASM_EDGE_BAD_SOURCE,
    ASM_EDGE_CARRIER,
    ASM_EDGE_DUPLICATE,
    ASM_EDGE_LOOSE_BIDI,
    ASM_EDGE_QUANTITY,
    ASM_EDGE_SELF_LOOP,
    ASM_EDGE_UNIT_DIM,
    ASM_EDGE_ZERO_CAP,
    ASM_INPUT_DATA_UNIT,
    ASM_INPUT_LOAD_DATA,
    ASM_INPUT_PARAM,
    ASM_INPUT_RANGE,
    ASM_INPUT_UNFED,
    ASM_PIPE_DELAY_MISSING,
    ASM_PIPE_DELAY_RANGE,
    ASM_PIPE_NOT_PATH,
    ASM_REF_DATASET,
    ASM_REF_DUP_DEVICE,
    ASM_REF_MODEL_UNREG,
    ASM_REF_PORT_DECL,
    ASM_REF_PORT_UNDEF,
    ASM_SOLV_CAUSAL_CYCLE,
    ASM_SOLV_DOF,
    ASM_SOLV_INFEASIBLE,
    ASM_SOLV_NO_SINK,
    ASM_SOLV_NO_SOURCE,
    ASM_SOLV_ORPHAN,
    ASM_SOLV_OVER_CONSTRAINED,
    ASM_SYN_FIELD,
    ASM_SYN_PARSE,
    ASM_SYN_SECTION,
    ASM_SYN_TYPE,
    ASM_SYN_VERSION,
)
from iesplan.assembly.schema import AssemblySpec
from iesplan.core.diagnostics import DIAG_FIX_HINT_KEYS, DIAG_MESSAGE_KEYS, make_diag
from iesplan.core.registry import DeviceTypeSpec, list_device_types

# ---------------------------------------------------------------------------
# 通用夹具与辅助
# ---------------------------------------------------------------------------

DATASETS = {
    17: {"name": "campus_electric_2025", "columns": ["power_kw"], "unit": "kW", "resolution": "1h"},
    18: {"name": "campus_heat_2025", "columns": ["heat_kw"], "unit": "kW", "resolution": "1h"},
}


def check_text(text: str, datasets: dict | None = None) -> CheckResult:
    """文本 + 默认上下文全量检查。"""
    return check_assembly_text(text, ctx=CheckContext(datasets=datasets))


def check_spec(spec: AssemblySpec, datasets: dict | None = None) -> CheckResult:
    return check_assembly(spec, ctx=CheckContext(datasets=datasets))


def codes(result: CheckResult) -> list[str]:
    return [d.code for d in result.diagnostics]


def parse_ok(text: str) -> AssemblySpec:
    result = parse_assembly(text)
    assert result.spec is not None, f"解析失败: {[d.code for d in result.diagnostics]}"
    return result.spec


def minimal_text(devices: str, edges: str = "", extra: str = "") -> str:
    """最小装配文本(assembly + time_axis + devices + edges + 额外章节)。"""
    body = 'assembly:\n  name: t\n  format_version: "1.0"\ntime_axis:\n  resolution: 1h\n'
    if devices:
        body += "devices:\n" + textwrap.indent(devices, "  ")
    if edges:
        body += "edges:\n" + textwrap.indent(edges, "  ")
    if extra:
        body += extra + ("\n" if not extra.endswith("\n") else "")
    return body


def port_decl(
    device: str,
    name: str,
    carrier: str,
    direction: str,
    quantity: str,
    unit: str,
    capacity: str = "",
) -> str:
    """显式端口声明文本(端口覆盖/反例构造)。"""
    s = (
        f"ports:\n  - device: {device}\n    name: {name}\n    carrier: {carrier}\n"
        f"    direction: {direction}\n    quantity: {quantity}\n    unit: {unit}\n"
        f"    nature: instantaneous\n"
    )
    if capacity:
        s += f"    capacity: {capacity}\n"
    return s


def pipe_section(params: str) -> str:
    """管道章节文本(params 为流式参数字面量,如 "{delay_steps: 2}")。"""
    return f"pipelines:\n  - id: pipe_hot\n    model: ies.device.transport_pipe@1.0.0\n    params: {params}\n"


def constraint_section(expr: str, ctype: str = "ratio") -> str:
    """约束章节文本。"""
    return f'constraints:\n  - id: c1\n    type: {ctype}\n    expr: "{expr}"\n'


# 设备行模板(供 minimal_text 复用;"- id: " 前缀在调用处拼接)
GRID = (
    "grid\n    model: ies.device.grid_connection@1.2.0\n"
    "    params: {max_import_power_kw: 800, max_export_power_kw: 200}\n"
)
GRID_NO_EXPORT = (
    "grid\n    model: ies.device.grid_connection@1.2.0\n"
    "    params: {max_import_power_kw: 800, max_export_power_kw: 0, export_tariff: 0}\n"
)
GRID_ZERO = (
    "grid\n    model: ies.device.grid_connection@1.2.0\n"
    "    params: {max_import_power_kw: 0, max_export_power_kw: 0, export_tariff: 0}\n"
)
HP = "hp1\n    model: ies.device.heat_pump@1.3.0\n    params: {rated_heat_kw: 600, cop_profile: 0}\n"
PV = "pv1\n    model: ies.device.pv@1.3.0\n"
BAT = "bat1\n    model: ies.device.battery@1.4.0\n    stateful: true\n"
E_LOAD = (
    "elec_load\n    model: ies.device.electric_load@1.1.0\n"
    "    data_refs:\n"
    "      - key: load_profile\n        dataset_version_id: 17\n        unit: kW\n"
)
H_LOAD = (
    "heat_load\n    model: ies.device.heat_load@1.1.0\n"
    "    data_refs:\n"
    "      - key: heat_profile\n        dataset_version_id: 18\n        unit: kW\n"
)
LOAD100 = (
    "elec_load\n    model: ies.device.electric_load@1.1.0\n    params: {peak_power_kw: 100}\n"
    "    data_refs:\n      - key: load_profile\n        dataset_version_id: 17\n"
)

# 常用边文本
HP_PIPE_EDGES = (
    "- id: e1\n  from: hp1.heat_out\n  to: pipe_hot.heat_in\n"
    "- id: e2\n  from: pipe_hot.heat_out\n  to: heat_load.heat_in\n"
)
GRID_HP_EDGE = "- id: e1\n  from: grid.electric_out\n  to: hp1.electric_in\n"
GRID_PV_EDGE = "- id: e1\n  from: grid.electric_out\n  to: pv1.electric_out\n"


def _registry_with(extra: dict[str, DeviceTypeSpec]) -> dict[str, DeviceTypeSpec]:
    """注册表快照 + 自定义类型(供构造性反例)。"""
    registry = {s.type_id: s for s in list_device_types()}
    registry.update(extra)
    return registry


#: 合法装配文本(04 §2.2 示例,模型版本与注册表一致,model_method 按 05 §7.1 裁决)
HAPPY_TEXT = textwrap.dedent(
    """\
    assembly:
      name: campus_demo_v3
      format_version: "1.0"

    time_axis:
      resolution: 1h
      start: "2025-01-01T00:00:00Z"
      timezone_offset_min: 480

    devices:
      - id: grid
        model: ies.device.grid_connection@1.2.0
        kind: existing
        model_method: mechanism
        stateful: false
        params: {max_import_power_kw: 800, max_export_power_kw: 200, export_tariff: 0.35}
      - id: pv1
        model: ies.device.pv@1.3.0
        kind: new
        params: {rated_capacity_kwp: 300}
      - id: bat1
        model: ies.device.battery@1.4.0
        kind: new
        stateful: true
        params: {capacity_kwh: 400, rated_power_kw: 200}
      - id: hp1
        model: ies.device.heat_pump@1.3.0
        kind: new
        params: {rated_heat_kw: 600, cop: 3.5, cop_profile: 0}
      - id: elec_load
        model: ies.device.electric_load@1.1.0
        kind: existing
        data_refs:
          - key: load_profile
            dataset_version_id: 17
            columns: [power_kw]
            unit: kW
            resolution: 1h
      - id: heat_load
        model: ies.device.heat_load@1.1.0
        kind: existing
        data_refs:
          - key: heat_profile
            dataset_version_id: 18
            unit: kW

    ports:
      - device: pv1
        name: electric_out
        carrier: electric
        direction: out
        quantity: power
        unit: W
        nature: instantaneous
        capacity: 320000.0

    edges:
      - id: e_grid_pv
        from: grid.electric_out
        to: pv1.electric_out
      - id: e_bat
        from: bat1.electric
        to: grid.electric_out
      - id: e_hp_elec
        from: grid.electric_out
        to: hp1.electric_in
      - id: e_load
        from: grid.electric_out
        to: elec_load.electric_in
      - id: e_pipe_in
        from: hp1.heat_out
        to: pipe_hot.heat_in
      - id: e_pipe_out
        from: pipe_hot.heat_out
        to: heat_load.heat_in

    pipelines:
      - id: pipe_hot
        model: ies.device.transport_pipe@1.0.0
        params: {delay_steps: 2, loss_per_step: 0.02}

    constraints:
      - id: c1
        type: ratio
        expr: "hp1.electric_in <= 0.8 * grid.electric_out"
      - id: c2
        type: capacity
        expr: "grid.electric_out <= 800 W"

    requirements:
      algorithm: ies.algo.milp_hybrid@1.0.0
      tolerances: {mip_rel_gap: 0.001, time_limit_s: 600}
      seed: 42
    """
)


# ---------------------------------------------------------------------------
# 诊断码登记
# ---------------------------------------------------------------------------


class TestDiagRegistration:
    def test_asm_codes_registered_in_core_directory(self):
        for code in ASM_ALL_CODES:
            assert code in DIAG_MESSAGE_KEYS, f"{code} 未登记消息键"
            assert code in DIAG_FIX_HINT_KEYS, f"{code} 未登记修复键"

    def test_message_key_namespace(self):
        for code in ASM_ALL_CODES:
            key = DIAG_MESSAGE_KEYS[code]
            assert key.startswith("ies.diag.asm."), f"{code} → {key}"
            fix = DIAG_FIX_HINT_KEYS[code]
            assert fix.startswith("ies.fix.asm."), f"{code} → {fix}"

    def test_make_diag_accepts_asm_codes(self):
        d = make_diag(ASM_SOLV_NO_SOURCE, params={"carrier": "heat"})
        assert d.message_key == "ies.diag.asm.solv.no_source"
        assert d.fix_hint_key == "ies.fix.asm.solv.no_source"
        assert d.severity == "error"

    def test_codes_unique(self):
        assert len(ASM_ALL_CODES) == len(set(ASM_ALL_CODES))
        assert "ASM-SYN-001" in ASM_ALL_CODES and "ASM-CONST-003" in ASM_ALL_CODES


# ---------------------------------------------------------------------------
# 阶段 A:语法与结构
# ---------------------------------------------------------------------------


class TestPhaseA:
    def test_happy_parse(self):
        result = parse_assembly(HAPPY_TEXT)
        assert result.ok and result.spec is not None
        spec = result.spec
        assert spec.name == "campus_demo_v3"
        assert spec.format_version == "1.0"
        assert spec.time_axis.resolution == "1h"
        assert len(spec.devices) == 6
        assert len(spec.edges) == 6
        assert len(spec.pipelines) == 1
        assert spec.pipelines[0].params["delay_steps"] == 2
        assert len(spec.constraints) == 2
        assert spec.requirements.seed == 42

    def test_yaml_syntax_error_syn_001(self):
        result = parse_assembly("devices: [1, 2\n")
        assert result.spec is None
        diag = result.diagnostics[0]
        assert diag.code == ASM_SYN_PARSE
        assert diag.blocking is True

    def test_unknown_section_syn_002(self):
        text = 'assembly:\n  name: x\n  format_version: "1.0"\ntime_axis:\n  resolution: 1h\nbogus: 1\n'
        result = parse_assembly(text)
        assert result.spec is None
        assert ASM_SYN_SECTION in [d.code for d in result.diagnostics]

    def test_version_syn_003(self):
        text = 'assembly:\n  name: x\n  format_version: "2.0"\ntime_axis:\n  resolution: 1h\n'
        result = parse_assembly(text)
        assert result.spec is None
        assert ASM_SYN_VERSION in [d.code for d in result.diagnostics]

    def test_missing_required_field_syn_004(self):
        result = parse_assembly(minimal_text("- id: hp1\n    params: {cop: 3.5}\n"))
        assert result.spec is None
        fields = [d.params.get("field") for d in result.diagnostics if d.code == ASM_SYN_FIELD]
        assert "devices[0].model" in fields

    def test_missing_assembly_section_syn_004(self):
        result = parse_assembly("time_axis:\n  resolution: 1h\ndevices: []\n")
        assert result.spec is None
        assert ASM_SYN_FIELD in [d.code for d in result.diagnostics]

    def test_bad_type_syn_005(self):
        text = minimal_text("- id: hp1\n    model: ies.device.heat_pump@1.3.0\n    params: [1, 2]\n")
        result = parse_assembly(text)
        assert result.spec is None
        assert ASM_SYN_TYPE in [d.code for d in result.diagnostics]

    def test_bad_enum_syn_005(self):
        text = minimal_text("- id: hp1\n    model: ies.device.heat_pump@1.3.0\n    model_method: wizardry\n")
        result = parse_assembly(text)
        assert result.spec is None
        assert ASM_SYN_TYPE in [d.code for d in result.diagnostics]

    def test_unknown_key_syn_001(self):
        text = minimal_text("- id: hp1\n    model: ies.device.heat_pump@1.3.0\n    magic: 1\n")
        result = parse_assembly(text)
        assert result.spec is None
        assert ASM_SYN_PARSE in [d.code for d in result.diagnostics]

    def test_phase_a_blocks_phase_bcd(self):
        # 阶段 A 失败 → 只产出 SYN 诊断,不进入 B/C/D
        result = check_text("devices: [1, 2\n")
        assert not result.ok
        assert all(d.code.startswith("ASM-SYN") for d in result.diagnostics)

    def test_flow_and_quoted_scalars(self):
        spec = parse_ok(HAPPY_TEXT)
        grid = spec.device_by_id("grid")
        assert grid.params["max_import_power_kw"] == 800
        assert spec.time_axis.start == "2025-01-01T00:00:00Z"

    def test_dumps_parse_roundtrip(self):
        spec = parse_ok(HAPPY_TEXT)
        text = dumps_assembly(spec)
        spec2 = parse_ok(text)
        assert spec2.device_ids() == spec.device_ids()
        assert {e.id for e in spec2.edges} == {e.id for e in spec.edges}
        assert {p.id for p in spec2.pipelines} == {p.id for p in spec.pipelines}
        assert spec2.time_axis == spec.time_axis
        assert spec2.requirements == spec.requirements
        assert {p.ref for p in spec2.all_ports()} == {p.ref for p in spec.all_ports()}


# ---------------------------------------------------------------------------
# 阶段 B:连接合法性
# ---------------------------------------------------------------------------


class TestPhaseB:
    def test_happy_text_passes_phase_b(self):
        result = check_text(HAPPY_TEXT, datasets=DATASETS)
        assert result.ok, codes(result)

    def test_bus_join_out_to_out_allowed(self):
        # 母线汇合写法(04 §2.3.4):grid.electric_out → pv1.electric_out 合法
        text = minimal_text(f"- id: {GRID}- id: {PV}", GRID_PV_EDGE)
        result = check_spec(parse_ok(text))
        assert ASM_EDGE_BAD_SOURCE not in codes(result)
        assert ASM_EDGE_BAD_SINK not in codes(result)

    def test_edge_001_bad_source(self):
        edge = "- id: e1\n  from: hp1.electric_in\n  to: grid.electric_out\n"
        text = minimal_text(f"- id: {GRID}- id: {HP}", edge)
        result = check_spec(parse_ok(text))
        assert ASM_EDGE_BAD_SOURCE in codes(result)
        diag = result.by_code(ASM_EDGE_BAD_SOURCE)[0]
        assert diag.location == {"object_type": "edge", "object_id": "e1", "field": "from"}
        assert diag.blocking is True

    def test_edge_002_bad_sink(self):
        # 输入对输出(方向倒挂)
        edge = "- id: e1\n  from: hp1.electric_in\n  to: grid.electric_out\n"
        text = minimal_text(f"- id: {GRID}- id: {HP}", edge)
        result = check_spec(parse_ok(text))
        assert ASM_EDGE_BAD_SINK in codes(result)

    def test_edge_003_carrier_mismatch(self):
        edge = "- id: e1\n  from: grid.electric_out\n  to: hp1.heat_out\n"
        text = minimal_text(f"- id: {GRID}- id: {HP}", edge)
        result = check_spec(parse_ok(text))
        assert ASM_EDGE_CARRIER in codes(result)

    def test_edge_004_quantity_mismatch(self):
        # 电力端口(power)对燃气端口(flow):注册表推导物理量不一致
        boiler = "boiler1\n    model: ies.device.gas_boiler@1.2.0\n"
        edge = "- id: e1\n  from: grid.electric_out\n  to: boiler1.gas\n"
        text = minimal_text(f"- id: {GRID}- id: {boiler}", edge)
        result = check_spec(parse_ok(text))
        assert ASM_EDGE_QUANTITY in codes(result)

    def test_edge_005_unit_dim_mismatch(self):
        # W 与 m3/s 量纲不可换算(units.convert 跨类拒绝)
        boiler = "boiler1\n    model: ies.device.gas_boiler@1.2.0\n"
        edge = "- id: e1\n  from: grid.electric_out\n  to: boiler1.gas\n"
        text = minimal_text(f"- id: {GRID}- id: {boiler}", edge)
        result = check_spec(parse_ok(text))
        assert ASM_EDGE_UNIT_DIM in codes(result)

    def test_edge_006_self_loop(self):
        edge = "- id: e1\n  from: grid.electric_out\n  to: grid.electric_out\n"
        text = minimal_text(f"- id: {GRID}", edge)
        result = check_spec(parse_ok(text))
        assert ASM_EDGE_SELF_LOOP in codes(result)

    def test_edge_007_duplicate(self):
        edges = GRID_HP_EDGE + "- id: e2\n  from: grid.electric_out\n  to: hp1.electric_in\n"
        text = minimal_text(f"- id: {GRID}- id: {HP}", edges)
        result = check_spec(parse_ok(text))
        assert ASM_EDGE_DUPLICATE in codes(result)
        diag = result.by_code(ASM_EDGE_DUPLICATE)[0]
        assert diag.params["edge"] == "e2" and diag.params["dup_of"] == "e1"

    def test_edge_009_zero_capacity_warning(self):
        edge = "- id: e1\n  from: grid.electric_out\n  to: hp1.electric_in\n  capacity: 0\n"
        text = minimal_text(f"- id: {GRID}- id: {HP}", edge)
        result = check_spec(parse_ok(text))
        assert ASM_EDGE_ZERO_CAP in codes(result)
        diag = result.by_code(ASM_EDGE_ZERO_CAP)[0]
        assert diag.severity == "warning" and diag.blocking is False

    def test_edge_008_loose_bidi_bus(self):
        # 双向-双向直连且母线无确定方向端口 → 悬空警告(阶段 D 母线构造后)
        boiler = "boiler1\n    model: ies.device.gas_boiler@1.2.0\n"
        edge = "- id: e1\n  from: bat1.electric\n  to: boiler1.gas\n"
        text = minimal_text(f"- id: {BAT}- id: {boiler}", edge)
        result = check_spec(parse_ok(text))
        assert ASM_EDGE_LOOSE_BIDI in codes(result)


# ---------------------------------------------------------------------------
# 阶段 C:模型可解性(引用与输入完备)
# ---------------------------------------------------------------------------


class TestPhaseC:
    def test_ref_001_dup_device(self):
        text = minimal_text(f"- id: {HP}- id: {HP}")
        result = check_spec(parse_ok(text))
        assert ASM_REF_DUP_DEVICE in codes(result)

    def test_ref_002_unregistered_model(self):
        text = minimal_text("- id: ghost\n    model: ies.device.unknown@1.0.0\n")
        result = check_spec(parse_ok(text))
        assert ASM_REF_MODEL_UNREG in codes(result)

    def test_ref_002_version_mismatch(self):
        text = minimal_text("- id: hp1\n    model: ies.device.heat_pump@9.9.9\n")
        result = check_spec(parse_ok(text))
        diag = result.by_code(ASM_REF_MODEL_UNREG)[0]
        assert diag.params["registered"] == "ies.device.heat_pump@1.3.0"

    def test_ref_002_version_omitted_resolves_latest(self):
        text = minimal_text("- id: hp1\n    model: ies.device.heat_pump\n")
        result = check_spec(parse_ok(text))
        assert ASM_REF_MODEL_UNREG not in codes(result)

    def test_ref_003_undefined_port(self):
        edge = "- id: e1\n  from: grid.electric_out\n  to: hp1.missing_in\n"
        text = minimal_text(f"- id: {GRID}- id: {HP}", edge)
        result = check_spec(parse_ok(text))
        diag = result.by_code(ASM_REF_PORT_UNDEF)[0]
        assert diag.params["ref"] == "hp1.missing_in"
        assert diag.location == {"object_type": "edge", "object_id": "e1", "field": "to"}

    def test_ref_003_undefined_explicit_port(self):
        ports = port_decl("hp1", "water_in", "water", "in", "flow", "m3/s")
        text = minimal_text(f"- id: {HP}", "", ports)
        result = check_spec(parse_ok(text))
        diag = result.by_code(ASM_REF_PORT_UNDEF)[0]
        assert diag.location["object_id"] == "hp1.water_in"

    def test_ref_005_port_decl_mismatch_warning(self):
        ports = port_decl("pv1", "electric_out", "heat", "out", "power", "W")
        text = minimal_text(f"- id: {GRID}- id: {PV}", GRID_PV_EDGE, ports)
        result = check_spec(parse_ok(text))
        assert ASM_REF_PORT_DECL in codes(result)
        assert result.by_code(ASM_REF_PORT_DECL)[0].severity == "warning"

    def test_ref_004_dataset_missing(self):
        text = minimal_text(
            "- id: elec_load\n    model: ies.device.electric_load@1.1.0\n"
            "    data_refs:\n      - key: load_profile\n        dataset_version_id: 999\n"
        )
        result = check_spec(parse_ok(text), datasets=DATASETS)
        diag = result.by_code(ASM_REF_DATASET)[0]
        assert diag.params["reason"] == "dataset_version_not_found"

    def test_ref_004_dataset_column_missing(self):
        text = minimal_text(
            "- id: elec_load\n    model: ies.device.electric_load@1.1.0\n"
            "    data_refs:\n      - key: load_profile\n        dataset_version_id: 17\n"
            "        columns: [nope_kw]\n"
        )
        result = check_spec(parse_ok(text), datasets=DATASETS)
        diag = result.by_code(ASM_REF_DATASET)[0]
        assert diag.params["reason"] == "column_not_found"

    def test_input_001_port_unfed(self):
        text = minimal_text(f"- id: {GRID}- id: {HP}")
        result = check_spec(parse_ok(text))
        diag = result.by_code(ASM_INPUT_UNFED)[0]
        assert diag.params["device"] == "hp1" and diag.params["port"] == "electric_in"
        assert diag.blocking is True

    def test_input_002_required_param_missing(self):
        text = minimal_text("- id: heat_load\n    model: ies.device.heat_load@1.1.0\n")
        result = check_spec(parse_ok(text))
        assert ASM_INPUT_PARAM in codes(result)

    def test_input_003_param_out_of_range(self):
        text = minimal_text(
            "- id: boiler1\n    model: ies.device.gas_boiler@1.2.0\n"
            "    params: {thermal_efficiency: 1.5}\n"
        )
        result = check_spec(parse_ok(text))
        diag = result.by_code(ASM_INPUT_RANGE)[0]
        assert diag.params["param"] == "thermal_efficiency"
        assert diag.blocking is False  # 非阻断

    def test_input_004_load_without_data(self):
        text = minimal_text("- id: elec_load\n    model: ies.device.electric_load@1.1.0\n")
        result = check_spec(parse_ok(text))
        assert ASM_INPUT_LOAD_DATA in codes(result)

    def test_input_005_data_unit_dim(self):
        text = minimal_text(
            "- id: elec_load\n    model: ies.device.electric_load@1.1.0\n"
            "    data_refs:\n      - key: load_profile\n        dataset_version_id: 17\n"
            "        unit: K\n"
        )
        result = check_spec(parse_ok(text), datasets=DATASETS)
        assert ASM_INPUT_DATA_UNIT in codes(result)

    def test_pipe_001_delay_missing_warning(self):
        text = minimal_text(
            f"- id: {HP}- id: {H_LOAD}", HP_PIPE_EDGES, pipe_section("{loss_per_step: 0.02}")
        )
        result = check_spec(parse_ok(text))
        diag = result.by_code(ASM_PIPE_DELAY_MISSING)[0]
        assert diag.severity == "warning"

    def test_pipe_002_delay_out_of_range(self):
        text = minimal_text(
            f"- id: {HP}- id: {H_LOAD}", HP_PIPE_EDGES, pipe_section("{delay_steps: 8760}")
        )
        result = check_spec(parse_ok(text))
        diag = result.by_code(ASM_PIPE_DELAY_RANGE)[0]
        assert diag.params["delay_steps"] == 8760

    def test_pipe_003_not_in_path_warning(self):
        text = minimal_text(f"- id: {HP}", "", pipe_section("{delay_steps: 2}"))
        result = check_spec(parse_ok(text))
        assert ASM_PIPE_NOT_PATH in codes(result)


# ---------------------------------------------------------------------------
# 阶段 D:整体可解性
# ---------------------------------------------------------------------------


class TestPhaseD:
    def test_happy_text_buses(self):
        result = check_text(HAPPY_TEXT, datasets=DATASETS)
        carriers = {b.carrier for b in result.buses}
        assert carriers == {"electric", "heat"}
        electric = next(b for b in result.buses if b.carrier == "electric")
        assert electric.has_grid and electric.has_storage
        assert "grid.electric_out" in electric.source_port_refs
        heat = next(b for b in result.buses if b.carrier == "heat")
        assert "pipe_hot.heat_in" in heat.sink_port_refs
        assert "pipe_hot.heat_out" in heat.source_port_refs

    def test_solv_001_no_source(self):
        text = minimal_text(f"- id: {H_LOAD}")
        result = check_spec(parse_ok(text))
        assert ASM_SOLV_NO_SOURCE in codes(result)
        diag = result.by_code(ASM_SOLV_NO_SOURCE)[0]
        assert diag.location == {"object_type": "bus", "field": "carrier:heat"}

    def test_solv_002_no_sink(self):
        text = minimal_text(f"- id: {PV}")
        result = check_spec(parse_ok(text))
        assert ASM_SOLV_NO_SINK in codes(result)

    def test_solv_003_infeasible(self):
        # 电网禁进口禁反送 + 负荷 100 kW:固定供给 0 < 需求 100000 W,无调节手段
        edge = "- id: e1\n  from: grid.electric_out\n  to: elec_load.electric_in\n"
        text = minimal_text(f"- id: {GRID_ZERO}- id: {LOAD100}", edge)
        result = check_spec(parse_ok(text), datasets=DATASETS)
        diag = result.by_code(ASM_SOLV_INFEASIBLE)[0]
        assert diag.params["fixed_supply_max"] == 0.0
        assert diag.params["demand_max"] == 100000.0

    def test_solv_004_over_constrained_fixed_supply(self):
        # 自定义非可控固定源(注册表快照注入)端口容量 500000 W > 负荷 100000 W
        fixed_gen = DeviceTypeSpec(
            type_id="ies.device.fixed_gen",
            version="1.0.0",
            name_zh="固定发电",
            name_en="Fixed Generation",
            energy_carriers=["electric"],
            is_load=False,
            capabilities=["generation"],
        )
        ctx = CheckContext(
            registry=_registry_with({"ies.device.fixed_gen": fixed_gen}), datasets=DATASETS
        )
        gen = "gen1\n    model: ies.device.fixed_gen@1.0.0\n"
        edge = "- id: e1\n  from: gen1.electric_out\n  to: elec_load.electric_in\n"
        ports = port_decl("gen1", "electric_out", "electric", "out", "power", "W", "500000.0")
        text = minimal_text(f"- id: {gen}- id: {LOAD100}", edge, ports)
        spec = parse_ok(text)
        result = check_assembly(spec, ctx=ctx)
        diag = result.by_code(ASM_SOLV_OVER_CONSTRAINED)[0]
        assert diag.params["reason"] == "fixed_supply_exceeds_demand_no_adjustment"

    def test_solv_004_grid_export_disabled_with_renewable(self):
        text = minimal_text(f"- id: {GRID_NO_EXPORT}- id: {PV}", GRID_PV_EDGE)
        result = check_spec(parse_ok(text))
        diag = result.by_code(ASM_SOLV_OVER_CONSTRAINED)[0]
        assert diag.params["reason"] == "grid_export_disabled_with_renewable_surplus"

    def test_solv_005_causal_cycle(self):
        # grid → pipe1 → pipe2 → grid 有向环,环内含管道(延迟设备)
        edges = (
            "- id: e1\n  from: grid.electric_out\n  to: pipe1.heat_in\n"
            "- id: e2\n  from: pipe1.heat_out\n  to: pipe2.heat_in\n"
            "- id: e3\n  from: pipe2.heat_out\n  to: grid.electric_out\n"
        )
        pipelines = (
            "pipelines:\n"
            "  - id: pipe1\n    model: ies.device.transport_pipe@1.0.0\n"
            "    params: {delay_steps: 2}\n"
            "  - id: pipe2\n    model: ies.device.transport_pipe@1.0.0\n"
            "    params: {delay_steps: 3}\n"
        )
        text = minimal_text(f"- id: {GRID}- id: {HP}", edges, pipelines)
        result = check_spec(parse_ok(text))
        diag = result.by_code(ASM_SOLV_CAUSAL_CYCLE)[0]
        assert set(diag.params["cycle"]) == {"grid", "pipe1", "pipe2"}
        assert diag.location["object_id"] in ("pipe1", "pipe2")

    def test_solv_006_orphan(self):
        pv2 = "pv2\n    model: ies.device.pv@1.3.0\n"
        text = minimal_text(f"- id: {GRID}- id: {PV}- id: {pv2}", GRID_PV_EDGE)
        result = check_spec(parse_ok(text))
        diag = result.by_code(ASM_SOLV_ORPHAN)[0]
        assert diag.params["device"] == "pv2"
        assert diag.severity == "warning"

    def test_solv_007_dof_info(self):
        result = check_text(HAPPY_TEXT, datasets=DATASETS)
        diag = result.by_code(ASM_SOLV_DOF)[0]
        assert diag.severity == "info"
        assert diag.params["n_eq"] == 8760
        assert diag.params["n_vars"] >= 1


# ---------------------------------------------------------------------------
# 约束表达式
# ---------------------------------------------------------------------------


class TestConstraints:
    def test_const_001_syntax(self):
        text = minimal_text(
            f"- id: {GRID}- id: {HP}", "", constraint_section("hp1.electric_in <=")
        )
        result = check_spec(parse_ok(text))
        assert ASM_CONST_SYNTAX in codes(result)

    def test_const_002_dim_mismatch(self):
        text = minimal_text(
            f"- id: {GRID}- id: {HP}", "", constraint_section("hp1.electric_in <= 800")
        )
        result = check_spec(parse_ok(text))
        assert ASM_CONST_DIM in codes(result)

    def test_const_003_undefined_symbol(self):
        text = minimal_text(
            f"- id: {GRID}- id: {HP}", "", constraint_section("ghost.port <= 5")
        )
        result = check_spec(parse_ok(text))
        diag = result.by_code(ASM_CONST_UNDEF)[0]
        assert diag.params["symbol"] == "ghost.port"

    def test_const_unit_suffix_rewrite(self):
        # "800 W" 显式单位后缀 → 常量变量(power 量纲),通过
        text = minimal_text(
            f"- id: {GRID}- id: {HP}",
            "",
            constraint_section("grid.electric_out <= 800 W", ctype="capacity"),
        )
        result = check_spec(parse_ok(text))
        assert ASM_CONST_DIM not in codes(result)
        assert ASM_CONST_SYNTAX not in codes(result)

    def test_const_param_symbol_dims(self):
        # 参数符号量纲(rated_heat_kw = kW → power):power <= power 通过
        text = minimal_text(
            f"- id: {GRID}- id: {HP}",
            "",
            constraint_section("hp1.rated_heat_kw <= grid.electric_out"),
        )
        result = check_spec(parse_ok(text))
        assert ASM_CONST_DIM not in codes(result)


# ---------------------------------------------------------------------------
# builder:项目图 → 装配文本
# ---------------------------------------------------------------------------


def _simple_graph(conn_loss: float = 0.0) -> dict:
    """测试图:grid + heat_pump + electric_load + heat_load(热网经 thermal_pipe)。"""
    return {
        "graph_id": 42,
        "name": "g42",
        "devices": [
            {
                "id": 1,
                "device_type": "ies.device.grid_connection",
                "kind": "existing",
                "params": {
                    "type_detail": "ies.device.grid_connection",
                    "max_import_power_kw": 800,
                    "max_export_power_kw": 200,
                },
            },
            {
                "id": 2,
                "device_type": "ies.device.heat_pump",
                "kind": "new",
                "params": {
                    "type_detail": "ies.device.heat_pump",
                    "rated_heat_kw": 600,
                    "cop": 3.5,
                    "cop_profile": 0,
                },
            },
            {
                "id": 3,
                "device_type": "ies.device.electric_load",
                "kind": "existing",
                "params": {
                    "type_detail": "ies.device.electric_load",
                    "load_profile": {"dataset_version_id": 17, "unit": "kW"},
                },
            },
            {
                "id": 4,
                "device_type": "ies.device.heat_load",
                "kind": "existing",
                "params": {
                    "type_detail": "ies.device.heat_load",
                    "heat_profile": {"dataset_version_id": 18, "unit": "kW"},
                },
            },
        ],
        "ports": [
            {
                "id": 11,
                "device_id": 1,
                "port_type": "electric",
                "direction": "out",
                "name": "electric_out",
                "capacity": None,
                "params": {},
            },
            {
                "id": 21,
                "device_id": 2,
                "port_type": "electric",
                "direction": "in",
                "name": "electric_in",
                "capacity": None,
                "params": {},
            },
            {
                "id": 22,
                "device_id": 2,
                "port_type": "thermal",
                "direction": "out",
                "name": "heat_out",
                "capacity": None,
                "params": {},
            },
            {
                "id": 31,
                "device_id": 3,
                "port_type": "electric",
                "direction": "in",
                "name": "electric_in",
                "capacity": None,
                "params": {},
            },
            {
                "id": 41,
                "device_id": 4,
                "port_type": "thermal",
                "direction": "in",
                "name": "heat_in",
                "capacity": None,
                "params": {},
            },
        ],
        "connections": [
            {
                "id": 101,
                "from_port_id": 11,
                "to_port_id": 21,
                "conn_type": "electric_line",
                "capacity": None,
                "loss_rate": 0,
                "params": {},
            },
            {
                "id": 102,
                "from_port_id": 11,
                "to_port_id": 31,
                "conn_type": "electric_line",
                "capacity": None,
                "loss_rate": 0,
                "params": {},
            },
            {
                "id": 103,
                "from_port_id": 22,
                "to_port_id": 41,
                "conn_type": "thermal_pipe",
                "capacity": None,
                "loss_rate": conn_loss,
                "params": {"delay_steps": 2} if conn_loss > 0 else {},
            },
        ],
    }


class TestBuilder:
    def test_determinism(self):
        graph = _simple_graph()
        t1 = build_assembly_text(graph, datasets=DATASETS)
        t2 = build_assembly_text(graph, datasets=DATASETS)
        assert t1 == t2

    def test_build_assembly_structure(self):
        spec = build_assembly(_simple_graph(), datasets=DATASETS)
        assert spec.source_graph_id == 42
        assert spec.name == "g42"
        assert {d.id for d in spec.devices} == {"d1", "d2", "d3", "d4"}
        assert spec.device_by_id("d2").model == "ies.device.heat_pump@1.3.0"
        load = spec.device_by_id("d3")
        assert load.data_refs and load.data_refs[0].key == "load_profile"
        assert load.data_refs[0].dataset_version_id == 17
        assert "load_profile" not in load.params  # 引用类参数已转 data_refs
        assert spec.requirements.algorithm == "ies.algo.milp_hybrid@1.0.0"
        assert len(spec.edges) == 3

    def test_loss_rate_wraps_pipeline(self):
        spec = build_assembly(_simple_graph(conn_loss=0.05), datasets=DATASETS)
        assert len(spec.pipelines) == 1
        pipe = spec.pipelines[0]
        assert pipe.id == "e103_pipe"
        assert pipe.params["delay_steps"] == 2
        assert pipe.params["loss_per_step"] == 0.05
        # 原连接被拆成两条瞬时边:from→管道 in、管道 out→to
        edge_ids = {e.id for e in spec.edges}
        assert "e103" in edge_ids and "e103_out" in edge_ids
        text = dumps_assembly(spec)
        spec2 = parse_ok(text)
        assert spec2.pipeline_by_id("e103_pipe") is not None
        pipe_edges = [e for e in spec2.edges if "e103" in e.id]
        assert len(pipe_edges) == 2

    def test_loss_zero_direct_edge(self):
        spec = build_assembly(_simple_graph(conn_loss=0.0), datasets=DATASETS)
        assert spec.pipelines == []
        assert len(spec.edges) == 3

    def test_graph_port_capacity_becomes_explicit_ports(self):
        graph = _simple_graph()
        graph["ports"][1]["capacity"] = 1200.0  # hp1.electric_in
        spec = build_assembly(graph)
        hp1 = spec.device_by_id("d2")
        assert any(p.name == "electric_in" and p.capacity == 1200.0 for p in hp1.ports)
        assert "capacity: 1200" in dumps_assembly(spec)

    def test_roundtrip_text_check(self):
        text = build_assembly_text(_simple_graph(), datasets=DATASETS)
        result = check_assembly_text(text, ctx=CheckContext(datasets=DATASETS))
        assert result.ok, codes(result)

    def test_check_graph_inputs_gate(self):
        graph = _simple_graph()
        content = {
            "model": {k: graph[k] for k in ("devices", "ports", "connections")},
            "calc_config": {
                "algorithm": "ies.algo.milp_hybrid@1.0.0",
                "tolerances": {"mip_rel_gap": 0.001},
            },
        }
        result = check_graph_inputs(content, datasets=DATASETS)
        assert result.ok, codes(result)

    def test_check_graph_inputs_flat_structure(self):
        # 无 model 包裹的扁平图结构
        graph = _simple_graph()
        content = {k: graph[k] for k in ("devices", "ports", "connections")}
        result = check_graph_inputs(content, datasets=DATASETS)
        assert result.ok, codes(result)

    def test_check_graph_inputs_blocks_error(self):
        content = {
            "model": {
                "devices": [
                    {
                        "id": 1,
                        "device_type": "ies.device.bogus",
                        "kind": "existing",
                        "params": {"type_detail": "ies.device.bogus"},
                    },
                ],
                "ports": [],
                "connections": [],
            }
        }
        result = check_graph_inputs(content)
        assert not result.ok
        assert ASM_REF_MODEL_UNREG in codes(result)


# ---------------------------------------------------------------------------
# CheckResult 辅助
# ---------------------------------------------------------------------------


class TestCheckResult:
    def test_ok_and_helpers(self):
        result = check_text(HAPPY_TEXT, datasets=DATASETS)
        assert result.ok
        assert result.blocking_diags == []
        assert len(result.by_code(ASM_SOLV_DOF)) == 2  # 电/热两个母线各一条 info

    def test_not_ok_with_error(self):
        text = minimal_text(f"- id: {PV}")
        result = check_spec(parse_ok(text))
        assert not result.ok
        assert any(d.code == ASM_SOLV_NO_SINK for d in result.blocking_diags)

    def test_diagnostics_serializable(self):
        result = check_text(HAPPY_TEXT, datasets=DATASETS)
        assert result.diagnostics  # 至少含 info 级自由度提示
        for d in result.diagnostics:
            payload = d.to_dict()
            assert payload["code"] and payload["message_key"].startswith("ies.diag.asm.")
            assert isinstance(payload["params"], dict)
