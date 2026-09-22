"""项目级线性模型构造(0.8f-B)。

把五项直接输入装配成首个 HiGHS MILP 链可消费的、求解器中立且
MPS-ready 的 ``LinearModel``，并附带结果反向换算所需的最小单位映射。
内部构造函数同样直接列出五项，不设包装层。

事实来源（只读公开契约，不重复校验）：

- ``artifact``：规范文本中的项目基线、实例 properties 覆盖、连接、
  规划（目标/变量/约束）与财务/计费绑定；
- ``models``：实例 ID → 设备数学贡献（properties 常量、接口流、
  关系 AST、状态清单、单位）；
- ``series``：已物化全周期序列（device/interface 键，接口业务单位数值）；
- ``finance``：不可变有效财务快照（成本分量、能源价格）；
- ``config``：计算配置（本切片构造不按其分支，仅随协议列出，
  后续切片做能力核对与求解选项）。

内部单位：物理量一律 SI（``core.units.to_si``；时间取秒），金额保持
财务币种。规划表达式中的单位字面量（``50 kW`` → 50000、
``1 year`` → 31536000）已由公共编译入口换算为同一体系，
显式时间跨度与按年费率相乘后自然落到窗口金额。

规划变量接管其绑定的设备 property：成本 driver 与技术方程中，
被规划变量绑定的 property 取变量值（界来自规划变量上下界），
未绑定的取实例 property 常量。t=0 状态初值取实例 property 常量。
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from iesplan.assembly.contracts import ValidatedAssemblyArtifact
from iesplan.computation.generators.linear import (
    LinearConstraint,
    LinearExpression,
    LinearModel,
    LinearVariable,
    LoweringUnsupportedError,
    lower_planning_constraint,
    lower_planning_expression,
    lower_relation,
)
from iesplan.computation.protocols import CalculationConfig
from iesplan.computation.sequences import MaterializedSeries
from iesplan.core.contracts import (
    compile_planning_expression,
)
from iesplan.core.units import dims_of, to_si
from iesplan.finance import EffectiveFinanceConfig
from iesplan.modeling import (
    BinaryNode,
    DeviceMathContribution,
    NumberNode,
    RefNode,
    UnaryNode,
)

#: 增量语义（装配 YAML finance_binding）：new 计全部分量，
#: existing 仅计周期可变分量。
_COMPONENTS_NEW = ("upfront_capex", "annual_fixed_om", "period_variable_om")
_COMPONENTS_EXISTING = ("period_variable_om",)


def _to_internal(value: float, unit: str) -> float:
    """业务数值 → 内部单位（物理量 SI，纯金额守币种原值）。"""
    if dims_of(unit) == Counter({"currency": 1}):
        return float(value)
    return to_si(float(value), unit)


def _refs_of(node: object) -> set[str]:
    """AST 引用的裸名集合（时间偏移不影响存在性收集）。"""
    if isinstance(node, NumberNode):
        return set()
    if isinstance(node, RefNode):
        return {node.name}
    if isinstance(node, UnaryNode):
        return _refs_of(node.operand)
    if isinstance(node, BinaryNode):
        return _refs_of(node.left) | _refs_of(node.right)
    return set()


def _relation_refs(contribution: DeviceMathContribution) -> set[str]:
    """贡献全部关系引用的裸名集合。"""
    names: set[str] = set()
    for relation in contribution.relations:
        names |= _refs_of(relation.ast.lhs_root)
        names |= _refs_of(relation.ast.rhs_root)
    return names


class _AffineAcc:
    """构造期仿射累加（const + lp 名 → 系数），出口冻结。"""

    __slots__ = ("constant", "terms")

    def __init__(self) -> None:
        self.constant = 0.0
        self.terms: dict[str, float] = {}

    def add_const(self, value: float) -> None:
        self.constant += value

    def add_term(self, name: str, coeff: float) -> None:
        total = self.terms.get(name, 0.0) + coeff
        if total == 0:
            self.terms.pop(name, None)
        else:
            self.terms[name] = total

    def add_scaled(self, other: "_AffineAcc", factor: float) -> None:
        self.add_const(other.constant * factor)
        for name, coeff in other.terms.items():
            self.add_term(name, coeff * factor)

    def freeze(self) -> LinearExpression:
        return LinearExpression(
            terms=tuple(
                ((name, 0), coeff) for name, coeff in self.terms.items()
            ),
            constant=self.constant,
        )


def _step_seconds(point_count: int, leap_year: bool) -> float:
    """步长秒数（由已可信点数唯一推导，不重建基线对象）。"""
    days = 366 if leap_year else 365
    return 86400.0 / (point_count // days)


def build_project_model(
    artifact: ValidatedAssemblyArtifact,
    models: Mapping[str, DeviceMathContribution],
    series: tuple[MaterializedSeries, ...],
    finance: EffectiveFinanceConfig,
    config: CalculationConfig,
) -> tuple[LinearModel, Mapping[str, str]]:
    """由五项直接输入构造项目级线性模型与单位映射。

    返回 ``(LinearModel, unit_map)``：``unit_map`` 为 LP 名 → 业务单位，
    供后续 ResultAdapter 反向换算（内部 binary 不进入结果，不收录）。
    同一五项输入产生同一模型；超出首版降级能力的数学抛
    ``LoweringUnsupportedError``。
    """
    doc = json.loads(artifact.canonical_text)
    baseline = doc["project_baseline"]
    point_count = _point_count(baseline["resolution"], baseline["leap_year"])
    step_s = _step_seconds(point_count, baseline["leap_year"])
    devices = doc["devices"]
    builder = _ProjectBuilder(
        doc=doc,
        devices=devices,
        models=models,
        series=series,
        finance=finance,
        point_count=point_count,
        step_s=step_s,
    )
    return builder.build()


def _point_count(resolution: str, leap_year: bool) -> int:
    """基线点数（复用 core 权威纯函数）。"""
    from iesplan.core.contracts import point_count_for_baseline

    return point_count_for_baseline(resolution, leap_year)


class _ProjectBuilder:
    """项目级构造的内部编排（实例方法只传 derived 状态，五项只进厂）。"""

    def __init__(
        self,
        *,
        doc: Mapping[str, Any],
        devices: Mapping[str, Any],
        models: Mapping[str, DeviceMathContribution],
        series: tuple[MaterializedSeries, ...],
        finance: EffectiveFinanceConfig,
        point_count: int,
        step_s: float,
    ) -> None:
        self.doc = doc
        self.devices = devices
        self.models = models
        self.finance = finance
        self.point_count = point_count
        self.step_s = step_s
        self.series: dict[tuple[str, str], list[float]] = {}
        for entry in series:
            self.series[(entry.device, entry.interface)] = list(entry.values)
        self._series_si_cache: dict[tuple[str, str], list[float]] = {}
        self.variables: dict[str, LinearVariable] = {}
        self.rows: list[LinearConstraint] = []
        self.unit_map: dict[str, str] = {}
        planning = doc["planning"]
        self.plan_vars: Mapping[str, Any] = planning.get("variables") or {}
        self.bound_prop: dict[tuple[str, str], str] = {}
        for var_name, var in self.plan_vars.items():
            self.bound_prop[(var["device_ref"], var["parameter"])] = var_name

    # ------------------------------------------------------------------
    # 变量声明
    # ------------------------------------------------------------------

    def _declare(
        self, name: str, lower: float | None, upper: float | None,
        kind: str, unit: str | None,
    ) -> str:
        """声明 LP 变量（同名复现即取首次；调用方保证同一事实）。"""
        if name not in self.variables:
            self.variables[name] = LinearVariable(
                name=name, lower=lower, upper=upper, kind=kind,
            )
            if unit is not None:
                self.unit_map[name] = unit
        return name

    @staticmethod
    def _bound(value: float | None, unit: str) -> float | None:
        return None if value is None else _to_internal(value, unit)

    def _tech_name(self, inst: str, local: str, step: int) -> str:
        return f"{inst}__{local}__t{step}"

    def _declare_instance_statics(self, inst: str) -> dict[str, Any]:
        """实例的静态符号与变量声明（property 常量/接管变量、内部变量、流变量）。"""
        contribution = self.models[inst]
        dev = self.devices[inst]
        overrides = dev.get("properties") or {}
        prop_const: dict[str, float] = {}
        prop_unit: dict[str, str] = {}
        for name, var in contribution.variables.items():
            if var.kind != "property":
                continue
            if name in overrides:
                value = float(overrides[name]["value"])
                unit = overrides[name]["unit"]
            else:
                value = float(var.value)  # type: ignore[arg-type]
                unit = var.unit
            prop_const[name] = _to_internal(value, unit)
            prop_unit[name] = unit
        symbols: dict[str, tuple] = {}
        for name, var in contribution.variables.items():
            if var.kind == "property":
                key = (inst, name)
                if key in self.bound_prop:
                    pname = self.bound_prop[key]
                    spec = self.plan_vars[pname]
                    unit = spec.get("unit") or var.unit
                    lower = spec.get("lower_bound")
                    upper = spec.get("upper_bound")
                    lower_f = None if lower is None else _to_internal(float(lower), unit)
                    upper_f = None if upper is None else _to_internal(float(upper), unit)
                    symbols[name] = ("var", lower_f, upper_f)
                    self._declare(pname, lower_f, upper_f, "continuous", unit)
                else:
                    symbols[name] = ("const", prop_const[name])
            else:
                lower = self._bound(
                    var.valid_range[0] if var.valid_range else None, var.unit,
                )
                upper = self._bound(
                    var.valid_range[1] if var.valid_range else None, var.unit,
                )
                symbols[name] = ("var", lower, upper)
        flow_names = self._referenced_flows(inst, contribution)
        for iface_name in flow_names:
            flow = contribution.interfaces[iface_name]
            lower = self._bound(flow.valid_range[0], flow.unit)
            upper = self._bound(flow.valid_range[1], flow.unit)
            symbols[iface_name] = ("var", lower, upper)
            for step in range(self.point_count):
                self._declare(
                    self._tech_name(inst, iface_name, step),
                    lower, upper, "continuous", flow.unit,
                )
        for name, var in contribution.variables.items():
            if var.kind == "property":
                continue
            lower, upper = symbols[name][1], symbols[name][2]
            for step in range(self.point_count):
                self._declare(
                    self._tech_name(inst, name, step),
                    lower, upper, "continuous", var.unit,
                )
        initial: dict[str, float] = {}
        for state in contribution.states:
            ref = contribution.variables[state].initial_property_ref
            initial[state] = prop_const[ref]  # type: ignore[index]
        return {
            "symbols": symbols,
            "initial": initial,
            "prop_const": prop_const,
            "flows": flow_names,
        }

    def _referenced_flows(
        self, inst: str, contribution: DeviceMathContribution,
    ) -> list[str]:
        """被关系、连接、计费或成本 driver 实际引用的时序流接口（排序）。"""
        refs = _relation_refs(contribution)
        for conn in (self.doc.get("connections") or {}).values():
            for end in (conn["from"], conn["to"]):
                dev_id, _, iface_id = end.partition(".")
                if dev_id == inst:
                    refs.add(iface_id)
        for binding in (self.doc.get("tariff_bindings") or {}).values():
            if binding.get("instance") == inst:
                refs.add(binding["interface"])
        instances = (self.doc.get("finance_binding") or {}).get("instances") or {}
        drivers = ((instances.get(inst) or {}).get("drivers") or {})
        for target in drivers.values():
            if "interface" in target:
                refs.add(target["interface"])
        return sorted(
            name
            for name, flow in contribution.interfaces.items()
            if name in refs and flow.type in ("in", "out", "bidirectional")
        )

    # ------------------------------------------------------------------
    # 技术关系
    # ------------------------------------------------------------------

    def _build_relations(self, inst: str, statics: dict[str, Any]) -> None:
        contribution = self.models[inst]
        symbols = statics["symbols"]
        initial = statics["initial"]
        predefined = {
            name
            for name, flow in contribution.interfaces.items()
            if flow.type == "predefined"
        }
        for step in range(self.point_count):
            step_symbols = dict(symbols)
            for name in predefined:
                series_si = self._series_si(inst, name, contribution)
                step_symbols[name] = ("const", series_si[step])
            for relation in contribution.relations:
                lowered = lower_relation(
                    relation, step_symbols, step=step, initial=initial,
                )
                self._emit_lowered(inst, relation.id, step, lowered)

    def _series_si(
        self, inst: str, iface: str, contribution: DeviceMathContribution,
    ) -> list[float]:
        """接口序列的 SI 值（按接口缓存，只换算一次）。"""
        key = (inst, iface)
        cached = self._series_si_cache.get(key)
        if cached is None:
            unit = contribution.interfaces[iface].unit
            cached = [_to_internal(v, unit) for v in self.series[key]]
            self._series_si_cache[key] = cached
        return cached

    def _emit_lowered(
        self, inst: str, rel_id: str, step: int, lowered: Any,
    ) -> None:
        """单关系降级结果的实例/step 命名落地。"""
        qualified_vars: dict[str, str] = {}
        for var in lowered.variables:
            qname = f"{inst}__{var.name}__t{step}"
            qualified_vars[var.name] = qname
            self._declare(qname, var.lower, var.upper, var.kind, None)

        def qualify(key: tuple[str, int]) -> str:
            name, offset = key
            if name in qualified_vars:
                return qualified_vars[name]
            if (inst, name) in self.bound_prop:
                if offset != 0:
                    raise LoweringUnsupportedError(
                        f"规划接管 property {name!r} 不得带时间索引"
                    )
                return self.bound_prop[(inst, name)]
            return self._tech_name(inst, name, step + offset)

        for row in lowered.constraints:
            terms = tuple(
                ((qualify(key), 0), coeff) for key, coeff in row.expression.terms
            )
            self.rows.append(
                LinearConstraint(
                    name=f"{inst}__{row.name}__t{step}",
                    expression=LinearExpression(
                        terms=tuple(sorted(terms)), constant=row.expression.constant,
                    ),
                    sense=row.sense,
                    rhs=row.rhs,
                )
            )

    # ------------------------------------------------------------------
    # 连接守恒
    # ------------------------------------------------------------------

    def _build_balances(self) -> None:
        connections = self.doc.get("connections") or {}
        for conn_id in sorted(connections):
            conn = connections[conn_id]
            from_lp = self._flow_prefix(conn["from"])
            to_lp = self._flow_prefix(conn["to"])
            for step in range(self.point_count):
                self.rows.append(
                    LinearConstraint(
                        name=f"{conn_id}__t{step}",
                        expression=LinearExpression(
                            terms=(
                                ((f"{from_lp}__t{step}", 0), 1.0),
                                ((f"{to_lp}__t{step}", 0), -1.0),
                            ),
                            constant=0.0,
                        ),
                        sense="eq",
                        rhs=0.0,
                    )
                )

    def _flow_prefix(self, endpoint: str) -> str:
        inst, _, iface = endpoint.partition(".")
        prefix = f"{inst}__{iface}"
        probe = f"{prefix}__t0"
        if probe not in self.variables:
            raise LoweringUnsupportedError(
                f"连接端点 {endpoint!r} 非已声明时序流接口"
            )
        return prefix

    # ------------------------------------------------------------------
    # 财务分量仿射式
    # ------------------------------------------------------------------

    def _cost_affine(self, inst: str, component: str) -> _AffineAcc:
        """单个成本分量的仿射式（fixed 常数 + linear driver 展开）。"""
        out = _AffineAcc()
        instances = (self.doc.get("finance_binding") or {}).get("instances") or {}
        entry = instances[inst]
        ft_entry = self.finance.finance_types[entry["finance_type"]]
        comp = getattr(ft_entry, component)
        if comp.fixed is not None:
            out.add_const(_money_to_internal(comp.fixed))
        for driver, cost in (comp.linear or {}).items():
            coeff = _to_internal(float(cost.unit_cost.value), cost.unit_cost.unit)
            out.add_scaled(self._driver_affine(inst, driver), coeff)
        return out

    def _driver_affine(self, inst: str, driver: str) -> _AffineAcc:
        """单个 driver 的数量仿射式（property 标量或接口能量）。"""
        out = _AffineAcc()
        instances = (self.doc.get("finance_binding") or {}).get("instances") or {}
        target = instances[inst]["drivers"][driver]
        if "property" in target:
            prop = target["property"]
            if (inst, prop) in self.bound_prop:
                out.add_term(self.bound_prop[(inst, prop)], 1.0)
                return out
            out.add_const(self._prop_const(inst, prop))
            return out
        iface = target["interface"]
        aggregation = target["aggregation"]
        contribution = self.models[inst]
        if (inst, iface) in self.series:
            values = self._series_si(inst, iface, contribution)
            if aggregation == "integrate_positive":
                out.add_const(sum(values) * self.step_s)
            else:
                out.add_const(sum(values))
            return out
        prefix = f"{inst}__{iface}"
        for step in range(self.point_count):
            coeff = self.step_s if aggregation == "integrate_positive" else 1.0
            out.add_term(f"{prefix}__t{step}", coeff)
        return out

    def _prop_const(self, inst: str, prop: str) -> float:
        contribution = self.models[inst]
        dev = self.devices[inst]
        overrides = dev.get("properties") or {}
        if prop in overrides:
            return _to_internal(float(overrides[prop]["value"]), overrides[prop]["unit"])
        var = contribution.variables[prop]
        return _to_internal(float(var.value), var.unit)  # type: ignore[arg-type]

    def _tariff_affine(self, binding_id: str) -> _AffineAcc:
        """单个计费绑定的已带符号记账分量仿射式。"""
        out = _AffineAcc()
        binding = (self.doc.get("tariff_bindings") or {})[binding_id]
        price = self.finance.energy_prices[binding["price"]]
        if price.kind != "constant":
            raise LoweringUnsupportedError(
                f"计费绑定 {binding_id!r} 的时变价格序列值不在五项输入内"
            )
        price_si = _to_internal(float(price.value.value), price.value.unit)  # type: ignore[union-attr]
        sign = 1.0 if price.direction == "purchase" else -1.0
        energy = self._driver_energy(
            binding["instance"], binding["interface"], binding["aggregation"],
        )
        out.add_scaled(energy, sign * price_si)
        return out

    def _driver_energy(self, inst: str, iface: str, aggregation: str) -> _AffineAcc:
        """接口能量序列仿射式（流变量或物化序列，integrate 乘步长秒）。"""
        out = _AffineAcc()
        contribution = self.models[inst]
        if (inst, iface) in self.series:
            values = self._series_si(inst, iface, contribution)
            factor = self.step_s if aggregation == "integrate_positive" else 1.0
            out.add_const(sum(values) * factor)
            return out
        prefix = f"{inst}__{iface}"
        for step in range(self.point_count):
            coeff = self.step_s if aggregation == "integrate_positive" else 1.0
            out.add_term(f"{prefix}__t{step}", coeff)
        return out

    def _ident_affine(self, ident: str) -> _AffineAcc:
        """规划财务标识符 → 仿射式（`<前缀>__<分量>` 首个 __ 切分）。"""
        prefix, _, component = ident.partition("__")
        instances = (self.doc.get("finance_binding") or {}).get("instances") or {}
        if prefix in instances:
            origin = self.devices[prefix].get("asset_origin")
            produced = (
                _COMPONENTS_NEW if origin == "new" else _COMPONENTS_EXISTING
            )
            if component not in produced:
                raise LoweringUnsupportedError(
                    f"财务标识符 {ident!r} 的分量未计入（资产身份 {origin!r}）"
                )
            entry = instances[prefix]
            ft_entry = self.finance.finance_types[entry["finance_type"]]
            if getattr(ft_entry, component) is None:
                raise LoweringUnsupportedError(
                    f"财务标识符 {ident!r} 的分量未声明"
                )
            return self._cost_affine(prefix, component)
        tariff = self.doc.get("tariff_bindings") or {}
        if prefix in tariff:
            if component not in ("period_energy_purchase", "period_energy_sale"):
                raise LoweringUnsupportedError(
                    f"计费标识符 {ident!r} 后缀非法"
                )
            return self._tariff_affine(prefix)
        raise LoweringUnsupportedError(f"未知规划标识符 {ident!r}")

    # ------------------------------------------------------------------
    # 规划目标与约束
    # ------------------------------------------------------------------

    def _planning_symbols(self) -> tuple[dict[str, Any], dict[str, str]]:
        """规划编译符号表（量纲）与降级占位（全 var 占位后替换）。"""
        symbols: dict[str, Any] = {}
        instances = (self.doc.get("finance_binding") or {}).get("instances") or {}
        for inst in sorted(self.devices):
            if inst not in instances or instances[inst].get("type") != "costed":
                continue
            origin = self.devices[inst].get("asset_origin")
            produced = _COMPONENTS_NEW if origin == "new" else _COMPONENTS_EXISTING
            for component in produced:
                ident = f"{inst}__{component}"
                if component == "annual_fixed_om":
                    symbols[ident] = Counter({"currency": 1, "time": -1})
                else:
                    symbols[ident] = Counter({"currency": 1})
        for binding_id in sorted(self.doc.get("tariff_bindings") or {}):
            for suffix in ("period_energy_purchase", "period_energy_sale"):
                symbols[f"{binding_id}__{suffix}"] = Counter({"currency": 1})
        for var_name, var in self.plan_vars.items():
            unit = var.get("unit")
            if unit is None:
                unit = self._bound_prop_unit(var["device_ref"], var["parameter"])
            symbols[var_name] = Counter(dict(dims_of(unit)))
        placeholders = {name: ("var", None, None) for name in symbols}
        return symbols, placeholders

    def _bound_prop_unit(self, inst: str, parameter: str) -> str:
        dev = self.devices[inst]
        overrides = dev.get("properties") or {}
        if parameter in overrides:
            return overrides[parameter]["unit"]
        return self.models[inst].variables[parameter].unit

    def _substitute(self, lowered: LinearExpression) -> _AffineAcc:
        """占位降级结果 → LP 仿射式（财务标识符展开，规划变量直通）。"""
        out = _AffineAcc()
        out.add_const(lowered.constant)
        idents = self._ident_names()
        for (name, _offset), coeff in lowered.terms:
            if name in self.plan_vars:
                out.add_term(name, coeff)
            elif name in idents:
                out.add_scaled(self._ident_affine(name), coeff)
            else:
                raise LoweringUnsupportedError(f"未知规划标识符 {name!r}")
        return out

    def _ident_names(self) -> set[str]:
        names: set[str] = set()
        instances = (self.doc.get("finance_binding") or {}).get("instances") or {}
        for inst in self.devices:
            if inst in instances and instances[inst].get("type") == "costed":
                origin = self.devices[inst].get("asset_origin")
                produced = _COMPONENTS_NEW if origin == "new" else _COMPONENTS_EXISTING
                for component in produced:
                    names.add(f"{inst}__{component}")
        for binding_id in (self.doc.get("tariff_bindings") or {}):
            names.add(f"{binding_id}__period_energy_purchase")
            names.add(f"{binding_id}__period_energy_sale")
        return names

    def _build_objective(self) -> _AffineAcc:
        planning = self.doc["planning"]
        raw = planning["objective"]
        symbols, placeholders = self._planning_symbols()
        compiled = compile_planning_expression(
            raw["expression"], symbols, currency=None,
        )
        return self._substitute(
            lower_planning_expression(compiled, placeholders),
        )

    def _build_planning_rows(self) -> None:
        """已启用规划约束行（约束 type 标签留装配产物，不进 LP）。"""
        planning = self.doc["planning"]
        constraints = planning.get("constraints") or {}
        symbols, placeholders = self._planning_symbols()
        for name in sorted(constraints):
            entry = constraints[name]
            if entry.get("enabled", True) is False:
                continue
            compiled = compile_planning_expression(
                entry["expression"], symbols, currency=None,
            )
            lowered = lower_planning_constraint(name, compiled, placeholders)
            substituted = self._substitute(lowered.expression)
            self.rows.append(
                LinearConstraint(
                    name=name,
                    expression=substituted.freeze(),
                    sense=lowered.sense,
                    rhs=lowered.rhs,
                )
            )

    # ------------------------------------------------------------------
    # 总装
    # ------------------------------------------------------------------

    def build(self) -> tuple[LinearModel, Mapping[str, str]]:
        for inst in sorted(self.devices):
            statics = self._declare_instance_statics(inst)
            self._build_relations(inst, statics)
        self._build_balances()
        self._build_planning_rows()
        objective = self._build_objective()
        model = LinearModel(
            variables=tuple(self.variables.values()),
            constraints=tuple(self.rows),
            objective=objective.freeze(),
            sense=self.doc["planning"]["objective"]["sense"],
        )
        return model, MappingProxyType(dict(sorted(self.unit_map.items())))


def _money_to_internal(money: Any) -> float:
    """金额 → 内部币值（守财务币种；单价类按分母 SI 归一）。"""
    return _to_internal(float(money.value), money.unit)


__all__ = ["build_project_model"]
