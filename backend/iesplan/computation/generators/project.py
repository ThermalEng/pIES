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

母线：连接只定义连通性，按 carrier + 无向连通分量逐 step 生成一条
守恒式（out/bidirectional 为供给正项，in 为消耗负项）；孤立端口
不建平衡式。值域界可引用实例 property（额定能力），实例级解析。
时变电价由第三项价格区逐点计价（``constant`` 同一逐点语义），
能力核对留待 GeneratorDescriptor 切片。

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
from iesplan.computation.sequences import (
    MaterializedPriceSeries,
    MaterializedSeries,
    MaterializedSet,
)
from iesplan.core.contracts import (
    compile_planning_expression,
)
from iesplan.core.contracts.baseline import TIMELINE_STEP_DURATION_VAR
from iesplan.core.contracts.planning_expression import PlanningExpression
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

#: 分量时间口径标签（与装配/财务契约的 upfront/annual/period 口径一致）。
_COMPONENT_CALIBER = {
    "upfront_capex": "upfront",
    "annual_fixed_om": "annual",
    "period_variable_om": "period",
    "period_energy_purchase": "period",
    "period_energy_sale": "period",
}


def _tariff_suffix(direction: str) -> str:
    """计费价格方向 → 唯一合法分量后缀（与装配规划校验同一规则）。

    purchase 只产生 ``period_energy_purchase``（正记账），sale 只产生
    ``period_energy_sale``（负记账）；反向后缀引用同一计费事实即非法，
    不在此处另建第二映射。
    """
    if direction == "purchase":
        return "period_energy_purchase"
    if direction == "sale":
        return "period_energy_sale"
    raise LoweringUnsupportedError(f"未知计费方向: {direction!r}")


def _binding_direction(builder: _ProjectBuilder, binding_id: str) -> str:
    """取某计费绑定的价格方向（快照价格条目为准，不靠名称推断）。"""
    bindings = builder.doc.get("tariff_bindings") or {}
    price_id = bindings[binding_id]["price"]
    return builder.finance.energy_prices[price_id].direction


def _to_internal(value: float, unit: str) -> float:
    """业务数值 → 内部单位（物理量 SI，纯金额守币种原值）。"""
    if dims_of(unit) == Counter({"currency": 1}):
        return float(value)
    return to_si(float(value), unit)


#: 单一物理量纲 → SI 基准单位符号（目标单位报告用；仅公开 dims_of 推导）。
_SI_UNIT_BY_DIM_KEY: dict[str, str] = {}
for _si_symbol in ("J", "W", "K", "s", "rad", "kg", "m³", "V", "m²"):
    _si_dims = dims_of(_si_symbol)
    if len(_si_dims) == 1:
        _SI_UNIT_BY_DIM_KEY[next(iter(_si_dims))] = _si_symbol


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

    def add_scaled(self, other: _AffineAcc, factor: float) -> None:
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


def _step_seconds(resolution: str, start_year: int) -> float:
    """步长秒数（由分辨率唯一推导，与年份无关：闰年 1h 同样是 3600 秒）。

    唯一权威是基线 ``step_duration``（小时/步），此处只做单位换算，
    不另建步长事实。``start_year`` 仅满足基线构造形态，不参与推导。
    """
    from iesplan.core.contracts import ProjectBaseline

    baseline = ProjectBaseline(resolution=resolution, start_year=start_year)
    return baseline.step_duration * 3600.0


def build_project_model(
    artifact: ValidatedAssemblyArtifact,
    models: Mapping[str, DeviceMathContribution],
    materialized: MaterializedSet,
    finance: EffectiveFinanceConfig,
    config: CalculationConfig,
) -> tuple[LinearModel, Mapping[str, str], str]:
    """由五项直接输入构造项目级线性模型、单位映射与目标业务单位。

    第三项为已完成物化的集合；求解域步数由装配运行起始年/分辨率与集合
    携带的已确认运营年限推导，``series`` 为空时仍为完整运营时域。
    返回 ``(LinearModel, unit_map, objective_unit)``：``unit_map`` 为
    LP 名 → 业务单位，供后续 ResultAdapter 反向换算（内部 binary 不进入
    结果，不收录）；``objective_unit`` 为目标业务单位（金额取财务币种，
    无量纲取 ``"1"``，单一物理量纲取 SI 基准单位；复合量纲无可报告业务
    单位，超出首版能力）。
    同一五项输入产生同一模型；超出首版降级能力的数学抛
    ``LoweringUnsupportedError``。
    """
    return _new_builder(artifact, models, materialized, finance).build()


def _new_builder(
    artifact: ValidatedAssemblyArtifact,
    models: Mapping[str, DeviceMathContribution],
    materialized: MaterializedSet,
    finance: EffectiveFinanceConfig,
) -> _ProjectBuilder:
    """由四项直接输入构造内部编排（建模与分量声明共用同一构造器）。"""
    from iesplan.core.contracts import horizon_point_count

    doc = json.loads(artifact.canonical_text)
    baseline = doc["project_baseline"]
    point_count = horizon_point_count(
        baseline["resolution"], baseline["start_year"], materialized.operation_years
    )
    step_s = _step_seconds(baseline["resolution"], baseline["start_year"])
    return _ProjectBuilder(
        doc=doc,
        devices=doc["devices"],
        models=models,
        series=materialized.series,
        prices=materialized.prices,
        finance=finance,
        point_count=point_count,
        step_s=step_s,
    )


def finance_component_declarations(
    artifact: ValidatedAssemblyArtifact,
    models: Mapping[str, DeviceMathContribution],
    materialized: MaterializedSet,
    finance: EffectiveFinanceConfig,
) -> Mapping[str, Mapping[str, object]]:
    """财务分量求值声明（ident → unit/caliber/constant/terms）。

    与 ``build_project_model`` 同一构造器、同一分量展开路径，只做
    初始化与分量展开，不执行装配建模；同一输入恒得同一声明。
    供 GeneratorProvider 写入 Bundle ``parameters.json``，由配套
    ResultAdapter 用同一次真实解求值发布，不重建财务模型。
    """
    return _new_builder(artifact, models, materialized, finance).finance_components()


def step_series_declarations(
    artifact: ValidatedAssemblyArtifact,
    models: Mapping[str, DeviceMathContribution],
    materialized: MaterializedSet,
    finance: EffectiveFinanceConfig,
) -> tuple[Mapping[str, object], ...]:
    """可发布求解步序列声明（与建变量同一路径形成）。

    与 ``build_project_model`` 同一构造器、同一变量声明路径，只做
    初始化与变量声明展开，不执行装配建模；同一输入恒得同一声明。
    供 GeneratorProvider 写入 Bundle ``parameters.json``，由配套
    ResultAdapter 用同一次真实解恢复发布，不重建变量映射。
    无可发布步变量时为空元组。
    """
    return _new_builder(artifact, models, materialized, finance).step_series()


class _ProjectBuilder:
    """项目级构造的内部编排（实例方法只传 derived 状态，五项只进厂）。"""

    def __init__(
        self,
        *,
        doc: Mapping[str, Any],
        devices: Mapping[str, Any],
        models: Mapping[str, DeviceMathContribution],
        series: tuple[MaterializedSeries, ...],
        prices: tuple[MaterializedPriceSeries, ...] = (),
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
        self.prices: dict[str, list[float]] = {}
        for entry in prices:
            self.prices[entry.price_id] = [
                _to_internal(v, entry.unit) for v in entry.values
            ]
        self._series_si_cache: dict[tuple[str, str], list[float]] = {}
        self.variables: dict[str, LinearVariable] = {}
        self.rows: list[LinearConstraint] = []
        self.unit_map: dict[str, str] = {}
        #: (实例, 局部变量身份) → 按 step 顺序的求解列名, 仅收录带业务单位的
        #: 步变量(接口流与设备技术变量, 与 _declare 调用同源; 关系降级
        #: 临时变量 unit 为 None, 不进入)。下游不得拆解列名反推身份。
        self._step_columns: dict[tuple[str, str], list[str]] = {}
        self._step_units: dict[tuple[str, str], str] = {}
        planning = doc["planning"]
        self.plan_vars: Mapping[str, Any] = planning.get("variables") or {}
        #: (实例, 参数) → 首年建设容量变量名。同一实例同一参数只允许
        #: 一个变量（重复声明在用户输入边界阻断）。
        self.bound_prop: dict[tuple[str, str], str] = {}
        for var_name, var in self.plan_vars.items():
            key = (var["device_ref"], var["parameter"])
            self.bound_prop[key] = var_name

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

    def _tech_name(self, inst: str, local: str, step: int) -> str:
        return f"{inst}__{local}__t{step}"

    def _prop_value_unit(self, inst: str, prop: str) -> tuple[float, str]:
        """实例 property 值与单位（装配覆盖优先，否则设备默认值）。"""
        dev = self.devices[inst]
        overrides = dev.get("properties") or {}
        if prop in overrides:
            return float(overrides[prop]["value"]), overrides[prop]["unit"]
        var = self.models[inst].variables[prop]
        return float(var.value), var.unit  # type: ignore[arg-type]

    def _limit(
        self, inst: str, unit: str, number: float | None, ref: str | None,
        side: str,
    ) -> tuple[float | None, str | None]:
        """值域界内部值与接管规划变量名。

        数值直接换算；未被接管的 property 引用按实例解析。被规划
        变量接管的 property 不再用实例旧值：静态安全界取规划变量
        对应界（maximum 取 upper_bound，minimum 取 lower_bound，
        缺失则 None），并返回规划变量名供逐时容量行使用。
        """
        if ref is not None and (inst, ref) in self.bound_prop:
            pname = self.bound_prop[(inst, ref)]
            spec = self.plan_vars[pname]
            punit = spec.get("unit") or unit
            raw = spec.get("upper_bound" if side == "upper" else "lower_bound")
            bound = None if raw is None else _to_internal(float(raw), punit)
            return bound, pname
        if ref is not None:
            value, punit = self._prop_value_unit(inst, ref)
            return _to_internal(value, punit), None
        if number is None:
            return None, None
        return _to_internal(number, unit), None

    def _declare_instance_statics(
        self, inst: str, bus_flows: set[str] | frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """实例的静态符号与变量声明（property 常量/接管变量、内部变量、流变量）。

        公开计算原语 ``step_duration`` 以基线导出的 SI 秒值注入符号表，
        设备不得自行声明。
        """
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
        caps: list[tuple[str, str, str]] = []
        for name, var in contribution.variables.items():
            if var.kind == "property":
                key = (inst, name)
                if key in self.bound_prop:
                    pname = self.bound_prop[key]
                    spec = self.plan_vars[pname]
                    unit = spec.get("unit") or var.unit
                    lower = spec.get("lower_bound")
                    upper = spec.get("upper_bound")
                    # 建设容量变量缺席下界即模型语义下界 0（规划契约；
                    # 线性层 None 表 -inf，MPS 的 FR+UP 会被 HiGHS 判重复
                    # 定义丢弃上界，必须在此落实为 LO 0）。
                    lower_f = 0.0 if lower is None else _to_internal(float(lower), unit)
                    upper_f = None if upper is None else _to_internal(float(upper), unit)
                    symbols[name] = ("var", lower_f, upper_f)
                    self._declare(pname, lower_f, upper_f, "continuous", unit)
                else:
                    symbols[name] = ("const", prop_const[name])
            else:
                lower, pname_lo = self._limit(
                    inst, var.unit,
                    var.valid_range[0] if var.valid_range else None,
                    var.minimum_property_ref, "lower",
                )
                upper, pname_hi = self._limit(
                    inst, var.unit,
                    var.valid_range[1] if var.valid_range else None,
                    var.maximum_property_ref, "upper",
                )
                symbols[name] = ("var", lower, upper)
                if pname_lo is not None:
                    caps.append((name, pname_lo, "ge"))
                if pname_hi is not None:
                    caps.append((name, pname_hi, "le"))
        symbols[TIMELINE_STEP_DURATION_VAR] = ("const", self.step_s)
        flow_names = sorted(
            set(self._referenced_flows(inst, contribution)) | set(bus_flows)
        )
        for iface_name in flow_names:
            flow = contribution.interfaces[iface_name]
            lower, pname_lo = self._limit(
                inst, flow.unit, flow.valid_range[0],
                flow.minimum_property_ref, "lower",
            )
            upper, pname_hi = self._limit(
                inst, flow.unit, flow.valid_range[1],
                flow.maximum_property_ref, "upper",
            )
            symbols[iface_name] = ("var", lower, upper)
            if pname_lo is not None:
                caps.append((iface_name, pname_lo, "ge"))
            if pname_hi is not None:
                caps.append((iface_name, pname_hi, "le"))
            for step in range(self.point_count):
                cname = self._tech_name(inst, iface_name, step)
                self._declare(cname, lower, upper, "continuous", flow.unit)
                self._step_columns.setdefault((inst, iface_name), []).append(cname)
            self._step_units[(inst, iface_name)] = flow.unit
        for name, var in contribution.variables.items():
            if var.kind == "property":
                continue
            lower, upper = symbols[name][1], symbols[name][2]
            for step in range(self.point_count):
                cname = self._tech_name(inst, name, step)
                self._declare(cname, lower, upper, "continuous", var.unit)
                self._step_columns.setdefault((inst, name), []).append(cname)
            self._step_units[(inst, name)] = var.unit
        initial: dict[str, float] = {}
        for state in contribution.states:
            ref = contribution.variables[state].initial_property_ref
            initial[state] = prop_const[ref]  # type: ignore[index]
        for local, pname, sense in caps:
            for step in range(self.point_count):
                vname = self._tech_name(inst, local, step)
                suffix = "plan_max" if sense == "le" else "plan_min"
                self.rows.append(
                    LinearConstraint(
                        name=f"{inst}__{local}__{suffix}__t{step}",
                        expression=LinearExpression(
                            terms=tuple(
                                sorted((((vname, 0), 1.0), ((pname, 0), -1.0)))
                            ),
                            constant=0.0,
                        ),
                        sense=sense,
                        rhs=0.0,
                    )
                )
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
    # 母线守恒（carrier + 无向连通分量；连接只定义连通性）
    # ------------------------------------------------------------------

    def _compute_buses(
        self,
    ) -> list[tuple[str, tuple[str, ...], tuple[tuple[str, str, float], ...]]]:
        """母线表：(carrier, 成员实例排序元组, ((实例, 接口, 符号)…))。

        先按 carrier 分图、再分别求无向连通分量（与装配
        ``solvability.build_buses`` 的 ``carrier_adj`` 语义一致；边载体
        取 from 端端口载体）。跨 carrier 的转换设备不合并对侧同载体
        子网。out/bidirectional 为供给正项，in 为消耗负项；
        bidirectional 以设备公开符号约定作为有符号净流量。
        不按设备 ID 分支。
        """
        connections = self.doc.get("connections") or {}
        carrier_adj: dict[str, dict[str, set[str]]] = {}
        for conn in connections.values():
            dev_a, _, iface_a = conn["from"].partition(".")
            dev_b, _, _ = conn["to"].partition(".")
            carrier = self.models[dev_a].interfaces[iface_a].carrier
            adj = carrier_adj.setdefault(carrier, {})
            adj.setdefault(dev_a, set()).add(dev_b)
            adj.setdefault(dev_b, set()).add(dev_a)
        buses: list[
            tuple[str, tuple[str, ...], tuple[tuple[str, str, float], ...]]
        ] = []
        for carrier in sorted(carrier_adj):
            adj = carrier_adj[carrier]
            seen: set[str] = set()
            components: list[tuple[str, ...]] = []
            for root in sorted(adj):
                if root in seen:
                    continue
                members = set([root])
                frontier = [root]
                while frontier:
                    current = frontier.pop()
                    for peer in adj.get(current, ()):
                        if peer not in members:
                            members.add(peer)
                            frontier.append(peer)
                seen |= members
                components.append(tuple(sorted(members)))
            for members in sorted(components):
                ports: list[tuple[str, str, float]] = []
                for inst in members:
                    contribution = self.models[inst]
                    for iface_name, flow in contribution.interfaces.items():
                        if flow.carrier != carrier:
                            continue
                        if flow.type == "out" or flow.type == "bidirectional":
                            sign = 1.0
                        elif flow.type == "in":
                            sign = -1.0
                        else:
                            continue
                        ports.append((inst, iface_name, sign))
                ordered = tuple(sorted(ports))
                if not ordered:
                    continue
                buses.append((carrier, members, ordered))
        return buses

    def _build_bus_rows(
        self,
        buses: list[
            tuple[str, tuple[str, ...], tuple[tuple[str, str, float], ...]]
        ],
    ) -> None:
        for index, (carrier, _members, ports) in enumerate(buses):
            for step in range(self.point_count):
                terms = tuple(
                    (((f"{inst}__{iface}__t{step}", 0), sign))
                    for inst, iface, sign in ports
                )
                self.rows.append(
                    LinearConstraint(
                        name=f"bus__{carrier}__{index}__t{step}",
                        expression=LinearExpression(
                            terms=tuple(sorted(terms)), constant=0.0,
                        ),
                        sense="eq",
                        rhs=0.0,
                    )
                )

    # ------------------------------------------------------------------
    # 财务分量仿射式
    # ------------------------------------------------------------------

    def _cost_affine(self, inst: str, component: str) -> _AffineAcc:
        """单个成本分量的仿射式（fixed 常数 + linear driver 展开）。

        ``fixed`` 按常数计入：仅静态实例与必建变量（下界 > 0，建设必然
        发生）可达本路径；可选建设变量 + 正固定费在用户输入边界拒收，
        不进入生成器。
        """
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
        """单个 driver 的数量仿射式（property 标量或接口能量）。

        被规划接管的容量 property 直接引用首年建设容量变量（全期在产
        即建设量）；未接管 property 保持旧行为（实例常量）。
        """
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
        value, unit = self._prop_value_unit(inst, prop)
        return _to_internal(value, unit)

    def _tariff_affine(self, binding_id: str) -> _AffineAcc:
        """单个计费绑定的已带符号记账分量仿射式（逐点 ``price[t]×e[t]``）。

        ``constant`` 价格即每点同值的逐点计价（与 ``price×Σe[t]`` 结果
        一致，语义仍为逐点）；``time_series`` 取第三项已物化价格区
        同 ``price_id`` 的全周期序列逐点相乘（长度/分辨率/单位由提交
        边界核对，此处只消费不复核）。符号按财务方向：
        ``purchase`` 为 ``+``、`sale` 为 ``-``；价格自身的正/零/负按
        财务契约自然通过（负价按该方向反转），不做钳零或聚合。
        """
        out = _AffineAcc()
        binding = (self.doc.get("tariff_bindings") or {})[binding_id]
        price = self.finance.energy_prices[binding["price"]]
        sign = 1.0 if price.direction == "purchase" else -1.0
        if price.kind == "constant":
            price_si = _to_internal(float(price.value.value), price.value.unit)  # type: ignore[union-attr]
            energy = self._driver_energy(
                binding["instance"], binding["interface"], binding["aggregation"],
            )
            out.add_scaled(energy, sign * price_si)
            return out
        if price.kind != "time_series":
            raise LoweringUnsupportedError(
                f"计费绑定 {binding_id!r} 的价格类型未知: {price.kind!r}"
            )
        price_values = self.prices[binding["price"]]
        energy_steps = self._driver_energy_steps(
            binding["instance"], binding["interface"], binding["aggregation"],
        )
        for step, (is_const, name_or_value, coeff) in enumerate(energy_steps):
            charge = sign * price_values[step] * coeff
            if is_const:
                out.add_const(charge * name_or_value)
            else:
                out.add_term(name_or_value, charge)
        return out

    def _driver_energy(self, inst: str, iface: str, aggregation: str) -> _AffineAcc:
        """接口能量序列仿射式（流变量或物化序列，integrate 乘步长秒）。"""
        out = _AffineAcc()
        for is_const, name_or_value, coeff in self._driver_energy_steps(
            inst, iface, aggregation
        ):
            if is_const:
                out.add_const(name_or_value * coeff)
            else:
                out.add_term(name_or_value, coeff)
        return out

    def _driver_energy_steps(
        self, inst: str, iface: str, aggregation: str,
    ) -> list[tuple[bool, Any, float]]:
        """接口能量逐点形态（供时变价格逐点相乘）。

        每点为 ``(is_const, 名或值, 系数)``：物化序列点给出 SI 值
        （常量），流变量点给出 LP 名；系数为聚合因子（integrate 取
        步长秒，否则 1.0）。与 ``_driver_energy`` 同一事实展开。
        """
        contribution = self.models[inst]
        factor = self.step_s if aggregation == "integrate_positive" else 1.0
        if (inst, iface) in self.series:
            return [
                (True, value, factor)
                for value in self._series_si(inst, iface, contribution)
            ]
        prefix = f"{inst}__{iface}"
        return [
            (False, f"{prefix}__t{step}", factor)
            for step in range(self.point_count)
        ]

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
            expected = _tariff_suffix(_binding_direction(self, prefix))
            if component != expected:
                raise LoweringUnsupportedError(
                    f"计费标识符 {ident!r} 后缀与价格方向不一致"
                    f"（该绑定只产生 {expected!r}）"
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
            suffix = _tariff_suffix(_binding_direction(self, binding_id))
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
            suffix = _tariff_suffix(_binding_direction(self, binding_id))
            names.add(f"{binding_id}__{suffix}")
        return names

    def _component_unit(self, component: str) -> str:
        """分量业务单位（一次性/窗口合计取财务币种，年值取币种/年）。"""
        if component == "annual_fixed_om":
            return f"{self.finance.currency}/a"
        return self.finance.currency

    def finance_components(self) -> Mapping[str, Mapping[str, object]]:
        """全部已声明财务分量的求值声明（与目标同一构造路径）。

        每个产出分量（资产身份下已声明分量、各计费绑定的方向后缀）各一条：
        ``{unit, caliber, constant, terms}``，constant/terms 为内部单位
        仿射系数（LP 名 → 系数），与目标共用同一分量展开函数
        （``_cost_affine``/``_tariff_affine``），不依赖规划目标写法。
        未声明的分量不存在，不声明、不补零；未知价格类型沿
        ``_tariff_affine`` 直接拒绝。
        """
        out: dict[str, Mapping[str, object]] = {}
        instances = (self.doc.get("finance_binding") or {}).get("instances") or {}
        for inst in sorted(self.devices):
            if inst not in instances or instances[inst].get("type") != "costed":
                continue
            origin = self.devices[inst].get("asset_origin")
            produced = _COMPONENTS_NEW if origin == "new" else _COMPONENTS_EXISTING
            ft_entry = self.finance.finance_types[instances[inst]["finance_type"]]
            for component in produced:
                if getattr(ft_entry, component) is None:
                    continue
                ident = f"{inst}__{component}"
                affine = self._cost_affine(inst, component)
                out[ident] = {
                    "unit": self._component_unit(component),
                    "caliber": _COMPONENT_CALIBER[component],
                    "constant": affine.constant,
                    "terms": dict(sorted(affine.terms.items())),
                }
        for binding_id in sorted(self.doc.get("tariff_bindings") or {}):
            component = _tariff_suffix(_binding_direction(self, binding_id))
            ident = f"{binding_id}__{component}"
            affine = self._tariff_affine(binding_id)
            out[ident] = {
                "unit": self._component_unit(component),
                "caliber": _COMPONENT_CALIBER[component],
                "constant": affine.constant,
                "terms": dict(sorted(affine.terms.items())),
            }
        return MappingProxyType(out)

    def _build_objective(self) -> tuple[_AffineAcc, str]:
        planning = self.doc["planning"]
        raw = planning["objective"]
        symbols, placeholders = self._planning_symbols()
        compiled = compile_planning_expression(
            raw["expression"], symbols, currency=None,
        )
        lowered = lower_planning_expression(compiled, placeholders)
        return self._substitute(lowered), self._objective_unit(compiled)

    def _objective_unit(self, compiled: PlanningExpression) -> str:
        """目标业务单位（编译期根量纲直读，不重解析表达式）。

        金额取财务币种，无量纲取 ``"1"``，单一物理量纲取 SI 基准单位；
        其余复合量纲无可报告业务单位，超出首版能力明确拒绝。报告单位
        与内部值恒一致（物理内部值即 SI，金额守币种），适配器不再二次
        判断量纲。
        """
        dims = dict(compiled.dimension)
        if not dims:
            return "1"
        if dims == {"currency": 1}:
            return self.finance.currency
        items = sorted(dims.items())
        if len(items) == 1 and items[0][1] == 1 and items[0][0] in _SI_UNIT_BY_DIM_KEY:
            return _SI_UNIT_BY_DIM_KEY[items[0][0]]
        raise LoweringUnsupportedError(
            f"目标量纲无可报告业务单位，超出首版能力: {items!r}"
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

    def _bus_flow_index(
        self, buses: list[tuple[str, tuple[str, ...], tuple[tuple[str, str, float], ...]]]
    ) -> dict[str, set[str]]:
        bus_flows: dict[str, set[str]] = {}
        for _carrier, _members, ports in buses:
            for inst, iface, _sign in ports:
                bus_flows.setdefault(inst, set()).add(iface)
        return bus_flows

    def step_series(self) -> tuple[Mapping[str, object], ...]:
        """可发布求解步序列声明（与建变量同一路径形成，不拆列名）。

        对实例每个带业务单位的步变量集合（接口流与设备技术变量）各一条：
        ``{series_id, instance_id, variable_id, unit, columns}``，
        ``columns`` 按连续 ``step`` 顺序为显式求解列名；无步变量时为空元组。
        ``series_id`` 为 ``{instance}.{variable}``，Bundle 内稳定唯一，
        消费者不得拆解。只做初始化与变量声明展开，不执行装配建模。
        """
        buses = self._compute_buses()
        bus_flows = self._bus_flow_index(buses)
        for inst in sorted(self.devices):
            self._declare_instance_statics(inst, bus_flows.get(inst, frozenset()))
        out: list[Mapping[str, object]] = []
        for inst, local in sorted(self._step_columns):
            out.append({
                "series_id": f"{inst}.{local}",
                "instance_id": inst,
                "variable_id": local,
                "unit": self._step_units[(inst, local)],
                "columns": list(self._step_columns[(inst, local)]),
            })
        return tuple(out)

    def build(self) -> tuple[LinearModel, Mapping[str, str], str]:
        buses = self._compute_buses()
        bus_flows = self._bus_flow_index(buses)
        for inst in sorted(self.devices):
            statics = self._declare_instance_statics(
                inst, bus_flows.get(inst, frozenset())
            )
            self._build_relations(inst, statics)
        self._build_bus_rows(buses)
        self._build_planning_rows()
        objective, objective_unit = self._build_objective()
        model = LinearModel(
            variables=tuple(self.variables.values()),
            constraints=tuple(self.rows),
            objective=objective.freeze(),
            sense=self.doc["planning"]["objective"]["sense"],
        )
        return (
            model,
            MappingProxyType(dict(sorted(self.unit_map.items()))),
            objective_unit,
        )


def _money_to_internal(money: Any) -> float:
    """金额 → 内部币值（守财务币种；单价类按分母 SI 归一）。"""
    return _to_internal(float(money.value), money.unit)


__all__ = ["build_project_model", "finance_component_declarations"]
