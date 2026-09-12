"""W1-Assembly 收敛测试(parser → context → rules → validator → artifact)。

覆盖(纯标准库 AST 静态断言 + 公开门面运行时断言,无需数据库):
- rules 不再导入 checker/parser/schema 私有符号(门禁白名单 4 项已消除);
- assembly 内部依赖方向无反转(每模块只允许依赖其下层);
- 共享能力已提升为 context 公开接口,checker 存量入口保持兼容;
- 公共行为不变(HAPPY 文本一次检查通过)。
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
ASM_DIR = BACKEND_DIR / "iesplan" / "assembly"

#: 门禁白名单中本次收敛必须消除的 4 项(键 = (模块, 私有符号))。
REMEDIATED_PRIVATE_IMPORTS = frozenset(
    {
        ("iesplan.assembly.rules.completeness", "_split_model"),
        ("iesplan.assembly.rules.solvability", "_PEAK_PARAM_BY_LOAD"),
        ("iesplan.assembly.rules.solvability", "_to_watts"),
        ("iesplan.assembly.checker", "_QUANTITY_DIMS"),
    }
)

#: 内部依赖方向(模块 → 允许的 assembly 内部目标;__init__ 为公开门面,允许聚合)。
ALLOWED_INTERNAL_DEPS: dict[str, frozenset[str]] = {
    "iesplan.assembly.schema": frozenset(),
    "iesplan.assembly.diags": frozenset(),
    "iesplan.assembly.parser": frozenset({"iesplan.assembly.schema", "iesplan.assembly.diags"}),
    "iesplan.assembly.context": frozenset(
        {"iesplan.assembly.parser", "iesplan.assembly.schema", "iesplan.assembly.diags"}
    ),
    "iesplan.assembly.rules.connection": frozenset(
        {"iesplan.assembly.context", "iesplan.assembly.diags"}
    ),
    "iesplan.assembly.rules.completeness": frozenset(
        {"iesplan.assembly.context", "iesplan.assembly.diags"}
    ),
    "iesplan.assembly.rules.constraints": frozenset(
        {"iesplan.assembly.context", "iesplan.assembly.diags"}
    ),
    "iesplan.assembly.rules.solvability": frozenset(
        {"iesplan.assembly.context", "iesplan.assembly.diags"}
    ),
    "iesplan.assembly.rules": frozenset(
        {
            "iesplan.assembly.rules.connection",
            "iesplan.assembly.rules.completeness",
            "iesplan.assembly.rules.constraints",
            "iesplan.assembly.rules.solvability",
        }
    ),
    "iesplan.assembly.checker": frozenset(
        {
            "iesplan.assembly.context",
            "iesplan.assembly.rules",
            "iesplan.assembly.rules.constraints",
            "iesplan.assembly.schema",
            "iesplan.assembly.parser",
            "iesplan.assembly.builder",
            "iesplan.assembly.diags",
        }
    ),
    "iesplan.assembly.builder": frozenset(
        {"iesplan.assembly.context", "iesplan.assembly.schema", "iesplan.assembly.diags"}
    ),
    "iesplan.assembly.validator": frozenset(
        {
            "iesplan.assembly.context",
            "iesplan.assembly.rules",
            "iesplan.assembly.schema",
            "iesplan.assembly.parser10",
            "iesplan.assembly.builder10",
            "iesplan.assembly.canonicalizer",
            "iesplan.assembly.contracts",
            "iesplan.assembly.diags",
        }
    ),
    "iesplan.assembly.parser10": frozenset(
        {"iesplan.assembly.canonicalizer", "iesplan.assembly.contracts", "iesplan.assembly.diags"}
    ),
    "iesplan.assembly.builder10": frozenset(
        {"iesplan.assembly.canonicalizer", "iesplan.assembly.diags"}
    ),
    "iesplan.assembly.canonicalizer": frozenset({"iesplan.assembly.contracts"}),
    "iesplan.assembly.contracts": frozenset({"iesplan.assembly.diags"}),
    "iesplan.assembly.plan": frozenset({"iesplan.assembly.schema", "iesplan.assembly.builder"}),
    "iesplan.assembly.validator2": frozenset({"iesplan.assembly.diags"}),
}


def _iter_asm_modules():
    for path in sorted(ASM_DIR.rglob("*.py")):
        if path.name == "__init__.py":
            rel = path.parent.relative_to(BACKEND_DIR)
        else:
            rel = path.relative_to(BACKEND_DIR).with_suffix("")
        yield ".".join(rel.parts), path


def _from_imports(path: Path) -> list[tuple[str, str]]:
    """源码中 from <target> import <name> 全量(函数级局部导入同样计入)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name != "*":
                    found.append((node.module, alias.name))
    return found


def _is_private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


class TestNoPrivateCrossImports:
    def test_rules_do_not_import_private_symbols(self):
        """rules 四阶段不再导入 checker/parser/schema 私有符号。"""
        violations = []
        for mod, path in _iter_asm_modules():
            if not mod.startswith("iesplan.assembly.rules."):
                continue
            for target, name in _from_imports(path):
                if target.startswith("iesplan.assembly.") and _is_private(name):
                    violations.append((mod, target, name))
        assert not violations, f"rules 仍存在内部私有符号导入: {violations}"

    def test_gate_whitelist_four_items_remediated(self):
        """门禁白名单 4 项 assembly 私有导入已被消除。"""
        detected = set()
        for mod, path in _iter_asm_modules():
            for target, name in _from_imports(path):
                if target.startswith("iesplan.") and _is_private(name):
                    detected.add((mod, name))
        remaining = REMEDIATED_PRIVATE_IMPORTS & detected
        assert not remaining, f"白名单条目仍存在,收敛未完成: {sorted(remaining)}"

    def test_no_private_imports_inside_assembly(self):
        """assembly 域内从此无下划线私有符号跨文件导入。"""
        violations = []
        for mod, path in _iter_asm_modules():
            for target, name in _from_imports(path):
                if target.startswith("iesplan.assembly.") and _is_private(name):
                    violations.append((mod, target, name))
        assert not violations, f"assembly 内部仍有私有符号导入: {violations}"


class TestDependencyDirection:
    def test_no_reversed_internal_edges(self):
        """内部依赖方向无反转:parser → context → rules → validator → artifact。"""
        violations = []
        for mod, path in _iter_asm_modules():
            if mod == "iesplan.assembly":
                continue  # 公开门面允许聚合
            allowed = ALLOWED_INTERNAL_DEPS.get(mod)
            assert allowed is not None, f"新模块 {mod} 未在方向表登记"
            for target, _name in _from_imports(path):
                if target.startswith("iesplan.assembly.") and target != mod and target not in allowed:
                    violations.append((mod, target))
        assert not violations, f"存在反转/越层内部依赖: {sorted(set(violations))}"

    def test_rules_depend_only_on_context(self):
        """rules 四阶段的唯一 assembly 内部依赖是 context(+ 诊断码目录)。"""
        for phase in ("connection", "completeness", "constraints", "solvability"):
            path = ASM_DIR / "rules" / f"{phase}.py"
            targets = {
                target
                for target, _name in _from_imports(path)
                if target.startswith("iesplan.assembly.")
            }
            assert targets <= {
                "iesplan.assembly.context",
                "iesplan.assembly.diags",
            }, f"rules.{phase} 出现 context/diags 之外的内部依赖: {sorted(targets)}"


class TestPromotedSharedCapabilities:
    def test_context_exposes_promoted_api(self):
        """rules 真正需要的共享能力已提升为 context 公开接口。"""
        from iesplan.assembly import context

        for name in (
            "CheckContext",
            "BusSummary",
            "AssemblySpec",
            "PORT_DECL_OVERRIDE_FIELDS",
            "PORT_TYPE_TO_CARRIER",
            "PIPELINE_MODEL_IDS",
            "PEAK_PARAM_BY_LOAD",
            "GRID_SIDE_PORTS",
            "EXOGENOUS_SUPPLY_CARRIERS",
            "split_model",
            "resolve_model",
            "grid_side_used",
            "default_registry",
            "yaml_device_ports",
            "derive_device_ports",
            "derive_pipeline_ports",
            "resolve_ports",
            "ensure_ports",
            "units_compatible",
            "unit_dims",
            "to_watts",
        ):
            assert hasattr(context, name), f"context 缺少公开能力: {name}"
            assert not name.startswith("_")

    def test_checker_compat_paths_alive(self):
        """checker 存量命名空间入口保持可用(旧单测/调用方不破)。"""
        import iesplan.assembly.checker as checker_mod
        from iesplan.assembly import context

        assert checker_mod.CheckContext is context.CheckContext
        assert checker_mod.BusSummary is context.BusSummary
        assert checker_mod.ensure_ports is context.ensure_ports
        assert checker_mod.resolve_ports is context.resolve_ports
        assert checker_mod.resolve_model is context.resolve_model
        assert checker_mod.units_compatible is context.units_compatible
        assert checker_mod._split_model is context.split_model
        assert checker_mod._to_watts is context.to_watts
        for name in (
            "_default_registry",
            "_yaml_device_ports",
            "_derive_device_ports",
            "_derive_pipeline_ports",
            "CheckResult",
            "AssemblyCheckError",
            "check_assembly",
            "check_assembly_text",
            "check_graph_inputs",
            "run_constraint_checks",
        ):
            assert callable(getattr(checker_mod, name)), f"checker.{name} 不可用"
        assert checker_mod.__all__ == [
            "CheckContext",
            "BusSummary",
            "CheckResult",
            "AssemblyCheckError",
            "check_assembly",
            "check_assembly_text",
            "check_graph_inputs",
            "resolve_ports",
            "resolve_model",
            "ensure_ports",
            "units_compatible",
            "run_constraint_checks",
            "PIPELINE_MODEL_IDS",
            "PORT_TYPE_TO_CARRIER",
            "_split_model",
        ]

    def test_rules_entry_via_package(self):
        """rules 包入口含约束检查(validator 经 rules 包调用)。"""
        from iesplan.assembly import rules

        assert set(rules.__all__) == {
            "run_phase_b",
            "run_phase_c",
            "run_phase_d",
            "build_buses",
            "run_constraint_checks",
        }

    def test_facade_identity(self):
        """公开门面类型与 context 同一对象,公开行为不变。"""
        import iesplan.assembly as facade
        from iesplan.assembly import context

        assert facade.CheckContext is context.CheckContext
        assert facade.BusSummary is context.BusSummary


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
        model: ies.device.grid_connection@2.0.0
        kind: existing
        model_method: mechanism
        stateful: false
        params: {max_import_power_kw: 800, max_export_power_kw: 200, export_tariff: 0.35}
      - id: pv1
        model: ies.device.pv@2.0.0
        kind: new
        params: {rated_capacity_kwp: 300}
      - id: bat1
        model: ies.device.battery@2.0.0
        kind: new
        stateful: true
        params: {capacity_kwh: 400, rated_power_kw: 200}
      - id: hp1
        model: ies.device.heat_pump@2.0.0
        kind: new
        params: {rated_heat_kw: 600, cop: 3.5, cop_profile: 0}
      - id: elec_load
        model: ies.device.electric_load@2.0.0
        kind: existing
        data_refs:
          - key: electricity_demand
            dataset_version_id: 17
            columns: [power_kw]
            unit: kW
            resolution: 1h
      - id: heat_load
        model: ies.device.heat_load@2.0.0
        kind: existing
        data_refs:
          - key: heat_demand
            dataset_version_id: 18
            unit: kW

    ports:
      - device: pv1
        name: electric_out
        carrier: electricity
        direction: out
        quantity: power
        unit: kW
        nature: instantaneous
        capacity: 320000.0

    edges:
      - id: e_grid_pv
        from: grid.electricity_import
        to: pv1.electric_out
      - id: e_bat
        from: bat1.electricity
        to: grid.electricity_export
      - id: e_hp_elec
        from: grid.electricity_import
        to: hp1.electricity_in
      - id: e_load
        from: grid.electricity_import
        to: elec_load.electricity_demand
      - id: e_pipe_in
        from: hp1.heat_out
        to: pipe_hot.heat_in
      - id: e_pipe_out
        from: pipe_hot.heat_out
        to: heat_load.heat_demand

    pipelines:
      - id: pipe_hot
        model: ies.device.transport_pipe@2.0.0
        params: {delay_steps: 2, loss_per_step: 0.02}

    constraints:
      - id: c1
        type: ratio
        expr: "hp1.electricity_in <= 0.8 * grid.electricity_import"
      - id: c2
        type: capacity
        expr: "grid.electricity_import <= 800 W"

    requirements:
      algorithm: ies.algo.milp_hybrid@1.0.0
      tolerances: {mip_rel_gap: 0.001, time_limit_s: 600}
      seed: 42
    """
)

DATASETS = {
    17: {"name": "campus_electric_2025", "columns": ["power_kw"], "unit": "kW", "resolution": "1h"},
    18: {"name": "campus_heat_2025", "columns": ["heat_kw"], "unit": "kW", "resolution": "1h"},
}


class TestBehaviorPreserved:
    def test_parse_phase_a_without_registry(self):
        """阶段 A 解析不依赖注册表,收敛后行为不变。"""
        from iesplan.assembly import parse_assembly

        result = parse_assembly(HAPPY_TEXT)
        assert result.spec is not None, [d.to_dict() for d in result.diagnostics]
        assert result.spec.device_by_id("grid") is not None

    def test_happy_text_full_check_passes(self):
        """收敛后全量检查行为不变:合法文本一次通过。"""
        from iesplan.assembly import CheckContext, check_assembly_text

        result = check_assembly_text(HAPPY_TEXT, ctx=CheckContext(datasets=DATASETS))
        assert result.ok, [(d.code, d.params) for d in result.diagnostics]
        assert result.buses, "阶段 D 应产出母线汇总"
