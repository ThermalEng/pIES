"""项目级线性模型构造(0.8f-B)。

把直接输入装配成首个 HiGHS MILP 链可消费的、求解器中立且
MPS-ready 的 ``LinearModel``，并附带结果反向换算所需的最小单位映射。
内部构造函数同样直接列出各项，不设包装层。

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
import math
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any

from iesplan.assembly.contracts import ValidatedAssemblyArtifact
from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.generators.compiler_ir import (
    CompilerInventory,
    IndexDomain,
    KnownCoefficient,
    OutputReference,
    RelationFamily,
    SourceLocation,
    SymbolDeclaration,
)
from iesplan.computation.generators.linear import (
    LinearConstraint,
    LinearExpression,
    LinearModel,
    LinearVariable,
    LoweringUnsupportedError,
    lower_expression,
    lower_mutex,
    lower_planning_constraint,
    lower_planning_expression,
    lower_relation,
)
from iesplan.computation.protocols import CalculationConfig, StopProbe, read_stop_reason
from iesplan.computation.sequences import (
    MaterializedPriceSeries,
    MaterializedSeries,
    MaterializedSet,
)
from iesplan.core.contracts import (
    SYSTEM_AFTER_TAX_NPV,
    compile_planning_expression,
    compile_step_relation_expression,
)
from iesplan.core.contracts.baseline import TIMELINE_STEP_DURATION_VAR
from iesplan.core.contracts.planning_expression import PlanningExpression
from iesplan.core.units import dims_of, to_si, unit_meta
from iesplan.finance import (
    ConstructionVatRule,
    CostComponentShareTerm,
    EffectiveFinanceConfig,
    EvaluationInput,
    IncomeTaxRule,
    MaintenanceExpenseNetBaseFrom,
    ScaleUnitCostTerm,
    VatRule,
    annual_boundaries,
)
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
    "period_demand_charge": "period",
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
    """物理量转 SI；同币种金额/费率只规范物理与时间分母。

    财务输入边界已核对币种一致；这里绝不进行 USD→CNY 兑换。
    core.units.to_si 对含 USD 的通用换汇仍明确禁止。
    """
    if dims_of(unit).get("currency") == 1:
        return float(value) * float(unit_meta(unit)["to_si"])
    return to_si(float(value), unit)


def _unit_cost_to_internal(value: float, unit: str) -> float:
    """同币种单位费用按物理分母归一，币值不做无汇率的跨币种折算。

    注册单位元数据给出复合单位的 SI 系数；财务入口已核对单价的
    币种与模型一致，USD/kW 也只换算 kW→W 而保持美元金额。
    """
    return float(value) * float(unit_meta(unit)["to_si"])


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

    def snapshot(self) -> _AffineAcc:
        """独立副本（留存事实与后续变更隔离；调用方保证语义时点）"""
        out = _AffineAcc()
        out.add_scaled(self, 1.0)
        return out


#: 年秒数（与单位表 ``a`` 同源：适配器 ``from_si(..., "CNY/a")`` 发布年值
#: 即按此折算，生成器年金额取同一因子，保证目标与评价现金流同源）。
_YEAR_SECONDS = _to_internal(1.0, "a")


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
    evaluation: EvaluationInput,
) -> tuple[LinearModel, Mapping[str, str], str]:
    """由直接输入构造项目级线性模型、单位映射与目标业务单位。

    第三项为已完成物化的集合；求解域步数由装配运行起始年/分辨率与评价
    确认的已确认运营年限推导，``series`` 为空时仍为完整运营时域（物化
    集合不再承载财务年限权威，此处不复核两者一致）。
    返回 ``(LinearModel, unit_map, objective_unit)``：``unit_map`` 为
    LP 名 → 业务单位，供后续 ResultAdapter 反向换算（内部 binary 不进入
    结果，不收录）；``objective_unit`` 为目标业务单位（金额取财务币种，
    无量纲取 ``"1"``，单一物理量纲取 SI 基准单位；复合量纲无可报告业务
    单位，超出首版能力）。
    同一组直接输入产生同一模型；超出首版降级能力的数学抛
    ``LoweringUnsupportedError``。
    """
    return build_project_model_with_facts(
        artifact, models, materialized, finance, config, evaluation
    )[:3]


def build_project_model_with_facts(
    artifact: ValidatedAssemblyArtifact,
    models: Mapping[str, DeviceMathContribution],
    materialized: MaterializedSet,
    finance: EffectiveFinanceConfig,
    config: CalculationConfig,
    evaluation: EvaluationInput,
) -> tuple[LinearModel, Mapping[str, str], str, Mapping[str, object]]:
    """由直接输入构造模型、单位映射、目标单位与同次解财务事实声明。

    与 ``build_project_model`` 同一构造器一次建模：模型三元组与该函数
    完全一致，第四项为 ``financial_facts`` 结构化声明（格式页映射表为
    唯一正文，供配套适配器用同一次主解恢复，不重建模型）。
    """
    model, unit_map, objective_unit, facts, _, _ = build_project_model_package(
        artifact, models, materialized, finance, config, evaluation
    )
    return model, unit_map, objective_unit, facts


def build_project_model_package(
    artifact: ValidatedAssemblyArtifact,
    models: Mapping[str, DeviceMathContribution],
    materialized: MaterializedSet,
    finance: EffectiveFinanceConfig,
    config: CalculationConfig,
    evaluation: EvaluationInput,
    *,
    stop: StopProbe | None = None,
) -> tuple[
    LinearModel, Mapping[str, str], str, Mapping[str, object],
    Mapping[str, Mapping[str, object]], tuple[Mapping[str, object], ...],
]:
    """一次建模取得 MPS、财务事实、分量与时序声明，不重复展开月峰。"""
    builder = _new_builder(artifact, models, materialized, finance, evaluation, stop=stop)
    model, unit_map, objective_unit = builder.build()
    return (
        model, unit_map, objective_unit,
        builder.financial_facts(), builder.finance_components(),
        builder.step_series(),
    )


def _new_builder(
    artifact: ValidatedAssemblyArtifact,
    models: Mapping[str, DeviceMathContribution],
    materialized: MaterializedSet,
    finance: EffectiveFinanceConfig,
    evaluation: EvaluationInput,
    *,
    stop: StopProbe | None = None,
) -> _ProjectBuilder:
    """由直接输入构造内部编排（建模与分量声明共用同一构造器）。"""
    from iesplan.core.contracts import horizon_point_count

    doc = json.loads(artifact.canonical_text)
    baseline = doc["project_baseline"]
    point_count = horizon_point_count(
        baseline["resolution"], baseline["start_year"], evaluation.operation_years
    )
    step_s = _step_seconds(baseline["resolution"], baseline["start_year"])
    return _ProjectBuilder(
        doc=doc,
        devices=doc["devices"],
        models=models,
        series=materialized.series,
        prices=materialized.prices,
        finance=finance,
        evaluation=evaluation,
        point_count=point_count,
        step_s=step_s,
        stop=stop,
    )


def finance_component_declarations(
    artifact: ValidatedAssemblyArtifact,
    models: Mapping[str, DeviceMathContribution],
    materialized: MaterializedSet,
    finance: EffectiveFinanceConfig,
    evaluation: EvaluationInput,
) -> Mapping[str, Mapping[str, object]]:
    """财务分量求值声明（ident → unit/caliber/constant/terms）。

    与 ``build_project_model`` 同一构造器、同一分量展开路径；存在需量
    绑定时需生成真实峰值求解列，同一输入恒得同一声明。
    供 GeneratorProvider 写入 Bundle ``parameters.json``，由配套
    ResultAdapter 用同一次真实解求值发布，不重建财务模型。
    """
    builder = _new_builder(artifact, models, materialized, finance, evaluation)
    if json.loads(artifact.canonical_text).get("demand_bindings"):
        # 需量分量必须消费本次建模生成的精确峰值求解列。
        builder.build()
    return builder.finance_components()


def step_series_declarations(
    artifact: ValidatedAssemblyArtifact,
    models: Mapping[str, DeviceMathContribution],
    materialized: MaterializedSet,
    finance: EffectiveFinanceConfig,
    evaluation: EvaluationInput,
) -> tuple[Mapping[str, object], ...]:
    """可发布求解步序列声明（与建变量同一路径形成）。

    与 ``build_project_model`` 同一构造器、同一变量声明路径，只做
    初始化与变量声明展开，不执行装配建模；同一输入恒得同一声明。
    供 GeneratorProvider 写入 Bundle ``parameters.json``，由配套
    ResultAdapter 用同一次真实解恢复发布，不重建变量映射。
    无可发布步变量时为空元组。
    """
    return _new_builder(artifact, models, materialized, finance, evaluation).step_series()


class _ProjectBuilder:
    """项目级构造的内部编排（实例方法只传 derived 状态，直接输入只进厂）。"""

    def __init__(
        self,
        *,
        doc: Mapping[str, Any],
        devices: Mapping[str, Any],
        models: Mapping[str, DeviceMathContribution],
        series: tuple[MaterializedSeries, ...],
        prices: tuple[MaterializedPriceSeries, ...] = (),
        finance: EffectiveFinanceConfig,
        evaluation: EvaluationInput,
        point_count: int,
        step_s: float,
        stop: StopProbe | None = None,
    ) -> None:
        self.stop = stop
        self._stop_ticks = 0
        self.doc = doc
        self.devices = devices
        self.models = models
        self.finance = finance
        self.evaluation = evaluation
        self._vat_cash: _AffineAcc | None = None
        self._income_cash: _AffineAcc | None = None
        #: 同源财务事实留存（与目标同一表达式/循环内并行累积，供
        #: ``financial_facts`` 声明；键与结构见格式页映射表）。
        self._demand_peaks: dict[tuple[str, str, int], _AffineAcc] = {}
        self._demand_rows: list[dict[str, Any]] = []
        self._demand_totals: dict[str, _AffineAcc] = {}
        self._fact_cash: dict[int, _AffineAcc] = {}
        self._fact_annual: dict[int, dict[str, _AffineAcc]] = {}
        #: 维护清单的 (评价年, 同年原始位置) → 唯一金额表达式；现金、
        #: VAT、所得税和同次解声明均消费此表，不拆分组合支付。
        self._maintenance_items: dict[tuple[int, int], _AffineAcc] = {}
        self._maintenance_annual: dict[int, _AffineAcc] = {}
        self._fact_construction = _AffineAcc()
        self._fact_fixed_om = _AffineAcc()
        self._fact_payments: list[tuple[int, str, _AffineAcc]] = []
        self._fact_vat: list[dict[str, Any]] = []
        self._fact_refunds: list[dict[str, Any]] = []
        self._fact_income: list[dict[str, Any]] = []
        self._fact_depr: list[dict[str, Any]] = []
        self._fact_balances: list[dict[str, Any]] = []
        self._fact_net_cashflows: list[dict[str, Any]] = []
        self._net_sources: dict[tuple, _AffineAcc] = {}
        self.point_count = point_count
        self.step_s = step_s
        self.series: dict[tuple[str, str], list[float]] = {}
        self._series_sources: dict[tuple[str, str], Mapping[str, object]] = {}
        for entry in series:
            key = (entry.device, entry.interface)
            self.series[key] = list(entry.values)
            self._series_sources[key] = entry.source
        self.prices: dict[str, list[float]] = {}
        for entry in prices:
            self.prices[entry.price_id] = [
                _to_internal(v, entry.unit) for v in entry.values
            ]
        self._series_si_cache: dict[tuple[str, str], list[float]] = {}
        self.variables: dict[str, LinearVariable] = {}
        self.rows: list[LinearConstraint] = []
        self.unit_map: dict[str, str] = {}
        # Per-generate declarations: no published input or Bundle schema change.
        self.inventory = CompilerInventory()
        self._instance_relations: dict[str, tuple[RelationFamily, ...]] = {}
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

    def _checkpoint(self) -> None:
        """生成器协作停止；按有限步数采样，不引入线程或进程管理。"""
        if self.stop is None:
            return
        self._stop_ticks += 1
        if self._stop_ticks % 128:
            return
        reason = read_stop_reason(self.stop)
        if reason is not None:
            raise ComputationUnavailableError("项目模型生成已停止", reason=reason)

    # ------------------------------------------------------------------
    # 变量声明
    # ------------------------------------------------------------------

    def _declare(
        self, name: str, lower: float | None, upper: float | None,
        kind: str, unit: str | None,
    ) -> str:
        """声明 LP 变量（同名复现即取首次；调用方保证同一事实）。"""
        self._checkpoint()
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

    def _declare_global_symbols(self) -> None:
        """Declare each planning decision once, before binding any instance."""
        for name in sorted(self.plan_vars):
            spec = self.plan_vars[name]
            inst, prop = spec["device_ref"], spec["parameter"]
            model_var = self.models[inst].variables[prop]
            unit = spec.get("unit") or model_var.unit
            lower = spec.get("lower_bound")
            upper = spec.get("upper_bound")
            lo = 0.0 if lower is None else _to_internal(float(lower), unit)
            hi = None if upper is None else _to_internal(float(upper), unit)
            self._declare(name, lo, hi, "continuous", unit)
            self.inventory.symbols[name] = SymbolDeclaration(
                name, "planning", IndexDomain("scalar"), unit,
                SourceLocation(f"planning.variables.{name}", inst,
                               self.models[inst].device_id,
                               self.devices[inst]["definition"].get("revision"),
                               binding_id=name),
            )
            self.inventory.property_bindings[(inst, prop)] = name
        self.inventory.coefficients["step_duration"] = KnownCoefficient(
            "step_duration", "step_duration",
            SourceLocation("project_baseline.resolution"), self.step_s,
        )

    def _bind_instance_symbols(
        self, inst: str, bus_flows: set[str] | frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """Bind all instance-local symbols; do NOT allocate time columns or rows.

        The source, original AST and coefficient origin remain inspectable
        before any step-level relation is converted.
        """
        contribution = self.models[inst]
        dev = self.devices[inst]
        overrides = dev.get("properties") or {}
        prop_const: dict[str, float] = {}
        prop_unit: dict[str, str] = {}
        column_specs: list[tuple[str, float | None, float | None, str]] = []
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
            source = SourceLocation(
                f"devices.{inst}.properties.{name}" if name in overrides
                else f"properties.{name}",
                inst, contribution.device_id, dev["definition"].get("revision"),
            )
            identity = self.bound_prop.get((inst, name), f"{inst}.{name}")
            self.inventory.property_bindings[(inst, name)] = identity
            if (inst, name) not in self.bound_prop:
                self.inventory.symbols[identity] = SymbolDeclaration(
                    identity, "property", IndexDomain("scalar"), unit, source,
                )
                self.inventory.coefficients[identity] = KnownCoefficient(
                    identity, "fixed_property", source, prop_const[name],
                )
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
                    # Global declaration already owns this exact capacity symbol.
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
            column_specs.append((iface_name, lower, upper, flow.unit))
            origin = SourceLocation(
                f"interfaces.{iface_name}", inst, contribution.device_id,
                dev["definition"].get("revision"),
            )
            identity = f"{inst}.{iface_name}"
            self.inventory.symbols[identity] = SymbolDeclaration(
                identity, "flow", IndexDomain("step", self.point_count),
                flow.unit, origin,
            )
            self.inventory.outputs.append(OutputReference(
                identity, identity, IndexDomain("step", self.point_count), origin,
            ))
        for name, var in contribution.variables.items():
            if var.kind == "property":
                continue
            lower, upper = symbols[name][1], symbols[name][2]
            column_specs.append((name, lower, upper, var.unit))
            origin = SourceLocation(
                f"equations.variables.{name}", inst, contribution.device_id,
                dev["definition"].get("revision"),
            )
            identity = f"{inst}.{name}"
            self.inventory.symbols[identity] = SymbolDeclaration(
                identity, "state" if name in contribution.states else "internal",
                IndexDomain("step", self.point_count), var.unit, origin,
            )
            self.inventory.outputs.append(OutputReference(
                identity, identity, IndexDomain("step", self.point_count), origin,
            ))
        initial: dict[str, float | LinearExpression] = {}
        for state in contribution.states:
            variable = contribution.variables[state]
            if variable.initial_ast is not None:
                initial[state] = lower_expression(variable.initial_ast, symbols)
            else:
                ref = variable.initial_property_ref
                # A taken-over property is the SAME scalar decision at t=0,
                # not the historic/default property value.
                if (inst, ref) in self.bound_prop:
                    initial[state] = LinearExpression(
                        terms=(((ref, 0), 1.0),), constant=0.0,
                    )
                else:
                    initial[state] = prop_const[ref]
        for name, flow in contribution.interfaces.items():
            if flow.type != "predefined":
                continue
            identity = f"{inst}.{name}"
            source = self._series_sources.get((inst, name), {})
            data_ref = source.get("object_id")
            self.inventory.coefficients[identity] = KnownCoefficient(
                identity, "materialized_series",
                SourceLocation(
                    f"devices.{inst}.predefined_interfaces.{name}",
                    inst, contribution.device_id,
                    dev["definition"].get("revision"),
                    data_object_ref=data_ref if isinstance(data_ref, str) else None,
                ),
            )
        families: list[RelationFamily] = []
        known = {
            name for name in prop_const
            if (inst, name) not in self.bound_prop
        } | {
            name for name, flow in contribution.interfaces.items()
            if flow.type == "predefined"
        } | {TIMELINE_STEP_DURATION_VAR}
        for relation in contribution.relations:
            refs = _refs_of(relation.ast.lhs_root) | _refs_of(relation.ast.rhs_root)
            family = RelationFamily(
                identity=f"{inst}.{relation.id}",
                relation=relation,
                domain=IndexDomain("step", self.point_count),
                referenced_symbols=tuple(sorted(refs)),
                output_symbol=relation.ast.output,
                known_coefficients=tuple(sorted(refs & known)),
                source=SourceLocation(
                    f"equations.relations.{relation.id}.expression",
                    inst, contribution.device_id,
                    dev["definition"].get("revision"),
                    relation.id,
                ),
            )
            families.append(family)
            self.inventory.relations.append(family)
        self._instance_relations[inst] = tuple(families)
        return {
            "symbols": symbols,
            "initial": initial,
            "prop_const": prop_const,
            "flows": flow_names,
            "columns": column_specs,
            "caps": caps,
        }

    def _expand_instance(self, inst: str, statics: Mapping[str, Any]) -> None:
        """Allocate step columns and variable capacity rows AFTER global binding."""
        for local, lower, upper, unit in statics["columns"]:
            for step in range(self.point_count):
                cname = self._tech_name(inst, local, step)
                self._declare(cname, lower, upper, "continuous", unit)
                self._step_columns.setdefault((inst, local), []).append(cname)
            self._step_units[(inst, local)] = unit
        for local, pname, sense in statics["caps"]:
            for step in range(self.point_count):
                vname = self._tech_name(inst, local, step)
                suffix = "plan_max" if sense == "le" else "plan_min"
                self.rows.append(LinearConstraint(
                    name=f"{inst}__{local}__{suffix}__t{step}",
                    expression=LinearExpression(
                        terms=tuple(sorted((
                            ((vname, 0), 1.0), ((pname, 0), -1.0),
                        ))),
                        constant=0.0,
                    ),
                    sense=sense, rhs=0.0,
                ))

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
        for binding in (self.doc.get("demand_bindings") or {}).values():
            if binding.get("instance") == inst:
                refs.add(binding["interface"])
        instances = (self.doc.get("finance_binding") or {}).get("instances") or {}
        drivers = ((instances.get(inst) or {}).get("drivers") or {})
        for target in drivers.values():
            if "interface" in target:
                refs.add(target["interface"])
        # Step relations consume the SAME port columns as device rows, bus rows,
        # tariff bindings and the solution series (including otherwise unused ports).
        for relation in (self.doc["planning"].get("constraints") or {}).values():
            if relation.get("type") != "step_relation" or relation.get("enabled", True) is False:
                continue
            for target in relation["references"].values():
                if target["instance"] == inst:
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
            self._checkpoint()
            step_symbols = dict(symbols)
            for name in predefined:
                series_si = self._series_si(inst, name, contribution)
                step_symbols[name] = ("const", series_si[step])
            for family in self._instance_relations[inst]:
                relation = family.relation
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
                self._checkpoint()
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

    def _cost_affine(
        self, inst: str, component: str, start: int = 0, end: int | None = None,
        *, part: str | None = None,
    ) -> _AffineAcc:
        """单个成本分量的仿射式（fixed 常数 + linear driver 展开）。

        ``fixed`` 按常数计入：仅静态实例与必建变量（下界 > 0，建设必然
        发生）可达本路径；可选建设变量 + 正固定费在用户输入边界拒收，
        不进入生成器。``start``/``end`` 只约束接口型 driver 的 step
        区间（property 标量按年值口径本年全额，不切片）。
        """
        out = _AffineAcc()
        instances = (self.doc.get("finance_binding") or {}).get("instances") or {}
        entry = instances[inst]
        ft_entry = self.finance.finance_types[entry["finance_type"]]
        comp = getattr(ft_entry, component)
        if comp.fixed is not None and part in (None, "fixed"):
            out.add_const(_money_to_internal(comp.fixed))
        for driver, cost in (comp.linear or {}).items():
            if part is not None and part != f"linear.{driver}":
                continue
            # S2 的 upkeep 比例引用同一实例 upfront；美元建设单价只按
            # 物理分母归一，不进行汇率换算。其他运维分量沿既有换算路径。
            coeff = (
                _unit_cost_to_internal(float(cost.unit_cost.value), cost.unit_cost.unit)
                if component == "upfront_capex"
                else _to_internal(float(cost.unit_cost.value), cost.unit_cost.unit)
            )
            out.add_scaled(self._driver_affine(inst, driver, start, end), coeff)
        return out

    def _driver_affine(
        self, inst: str, driver: str, start: int = 0, end: int | None = None,
    ) -> _AffineAcc:
        """单个 driver 的数量仿射式（property 标量或接口能量）。

        被规划接管的容量 property 直接引用首年建设容量变量（全期在产
        即建设量）；未接管 property 保持旧行为（实例常量）。property
        口径为年值（费率含时间维度由装配边界保证），逐年路径下本年全额，
        不按区间切片；接口能量按区间内实际步求和。
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
        stop = self.point_count if end is None else end
        if (inst, iface) in self.series:
            values = self._series_si(inst, iface, contribution)[start:stop]
            if aggregation == "integrate_positive":
                out.add_const(sum(values) * self.step_s)
            else:
                out.add_const(sum(values))
            return out
        prefix = f"{inst}__{iface}"
        for step in range(start, stop):
            coeff = self.step_s if aggregation == "integrate_positive" else 1.0
            out.add_term(f"{prefix}__t{step}", coeff)
        return out

    def _prop_const(self, inst: str, prop: str) -> float:
        value, unit = self._prop_value_unit(inst, prop)
        return _to_internal(value, unit)

    def _tariff_affine(
        self, binding_id: str, start: int = 0, end: int | None = None,
    ) -> _AffineAcc:
        """单个计费绑定的已带符号记账分量仿射式（逐点 ``price[t]×e[t]``）。

        ``constant`` 价格即每点同值的逐点计价（与 ``price×Σe[t]`` 结果
        一致，语义仍为逐点）；``time_series`` 和 ``monthly_daily`` 取第三项已物化价格区
        同 ``price_id`` 的全周期序列逐点相乘（长度/分辨率/单位由提交
        边界核对，此处只消费不复核）。符号按财务方向：
        ``purchase`` 为 ``+``、`sale` 为 ``-``；价格自身的正/零/负按
        财务契约自然通过（负价按该方向反转），不做钳零或聚合。
        ``start``/``end`` 约束归集 step 区间（时变价格仍按全局步下标
        取价，不重排价格序列）。
        """
        out = _AffineAcc()
        binding = (self.doc.get("tariff_bindings") or {})[binding_id]
        price = self.finance.energy_prices[binding["price"]]
        sign = 1.0 if price.direction == "purchase" else -1.0
        stop = self.point_count if end is None else end
        if price.kind == "constant":
            price_si = _to_internal(float(price.value.value), price.value.unit)  # type: ignore[union-attr]
            energy = self._driver_energy(
                binding["instance"], binding["interface"], binding["aggregation"],
                start, stop,
            )
            out.add_scaled(energy, sign * price_si)
            return out
        if price.kind not in ("time_series", "monthly_daily"):
            raise LoweringUnsupportedError(
                f"计费绑定 {binding_id!r} 的价格类型未知: {price.kind!r}"
            )
        price_values = self.prices[binding["price"]]
        energy_steps = self._driver_energy_steps(
            binding["instance"], binding["interface"], binding["aggregation"],
            start, stop,
        )
        for offset, (is_const, name_or_value, coeff) in enumerate(energy_steps):
            charge = sign * price_values[start + offset] * coeff
            if is_const:
                out.add_const(charge * name_or_value)
            else:
                out.add_term(name_or_value, charge)
        return out

    def _driver_energy(
        self, inst: str, iface: str, aggregation: str,
        start: int = 0, end: int | None = None,
    ) -> _AffineAcc:
        """接口能量序列仿射式（流变量或物化序列，integrate 乘步长秒）。"""
        out = _AffineAcc()
        for is_const, name_or_value, coeff in self._driver_energy_steps(
            inst, iface, aggregation, start, end
        ):
            if is_const:
                out.add_const(name_or_value * coeff)
            else:
                out.add_term(name_or_value, coeff)
        return out

    def _driver_energy_steps(
        self, inst: str, iface: str, aggregation: str,
        start: int = 0, end: int | None = None,
    ) -> list[tuple[bool, Any, float]]:
        """接口能量逐点形态（供时变价格逐点相乘）。

        每点为 ``(is_const, 名或值, 系数)``：物化序列点给出 SI 值
        （常量），流变量点给出 LP 名；系数为聚合因子（integrate 取
        步长秒，否则 1.0）。与 ``_driver_energy`` 同一事实展开。
        """
        contribution = self.models[inst]
        factor = self.step_s if aggregation == "integrate_positive" else 1.0
        stop = self.point_count if end is None else end
        if (inst, iface) in self.series:
            return [
                (True, value, factor)
                for value in self._series_si(inst, iface, contribution)[start:stop]
            ]
        prefix = f"{inst}__{iface}"
        return [
            (False, f"{prefix}__t{step}", factor)
            for step in range(start, stop)
        ]

    def _build_monthly_demand(self) -> None:
        """精确月最大功率：二元编码选择实际峰值点，不依赖目标函数。

        peak>=所有逐点功率；被编码索引选中的点强制 peak<=power[t]。
        未选中的点由实例真实功率上下界 U-L[t] 放松。索引被限制
        0..n-1，故一个且仅有一个真实点被选中，峰值严格等于月最大值。
        不满足有限盒界的实例在实际构模时拒绝，不猜 Big-M 或 epsilon。
        物化的功率输入无需变量，其真实峰值是直接常量。
        """
        spans = self._vat_month_spans()
        baseline_year = self.doc["project_baseline"]["start_year"]
        construction = self.evaluation.construction_period
        bindings = self.doc.get("demand_bindings") or {}
        for billing_id, binding in sorted(bindings.items()):
            instance = binding["instance"]
            interface = binding["interface"]
            charge_id = binding["charge"]
            rate = self.finance.demand_charges[charge_id].rate
            rate_si = _money_to_internal(rate)
            total = _AffineAcc()
            for month, start, end in spans:
                key = (instance, interface, month)
                peak = self._demand_peaks.get(key)
                if peak is None:
                    peak = _AffineAcc()
                    if (instance, interface) in self.series:
                        values = self._series_si(
                            instance, interface, self.models[instance]
                        )[start:end]
                        peak.add_const(max(values))
                    else:
                        names = [
                            self._tech_name(instance, interface, t)
                            for t in range(start, end)
                        ]
                        candidates = [self.variables[name] for name in names]
                        if any(
                            variable.lower is None or variable.upper is None
                            for variable in candidates
                        ):
                            raise LoweringUnsupportedError(
                                f"需量绑定 {billing_id!r} 计量点 "
                                f"{instance}.{interface} 的月 {month} 缺实际有限界"
                            )
                        upper = max(float(var.upper) for var in candidates)
                        lower = max(float(var.lower) for var in candidates)
                        peak_name = f"{instance}__{interface}__demand_peak_m{month}"
                        self._declare(peak_name, lower, upper, "continuous", None)
                        peak.add_term(peak_name, 1.0)
                        bit_count = (len(names) - 1).bit_length()
                        bits = [
                            self._declare(
                                f"{peak_name}__bit{bit}", 0.0, 1.0, "binary", None
                            )
                            for bit in range(bit_count)
                        ]
                        if bits:
                            valid = _AffineAcc()
                            for bit, flag in enumerate(bits):
                                valid.add_term(flag, float(1 << bit))
                            self.rows.append(LinearConstraint(
                                name=f"{peak_name}__valid",
                                expression=valid.freeze(), sense="le",
                                rhs=float(len(names) - 1),
                            ))
                        for offset, variable in enumerate(candidates):
                            floor = _AffineAcc()
                            floor.add_term(peak_name, 1.0)
                            floor.add_term(variable.name, -1.0)
                            self.rows.append(LinearConstraint(
                                name=f"{peak_name}__floor_{offset}",
                                expression=floor.freeze(), sense="ge", rhs=0.0,
                            ))
                            slack = upper - float(variable.lower)
                            select = floor.snapshot()
                            ones = 0
                            for bit, flag in enumerate(bits):
                                if offset & (1 << bit):
                                    select.add_term(flag, slack)
                                    ones += 1
                                else:
                                    select.add_term(flag, -slack)
                            self.rows.append(LinearConstraint(
                                name=f"{peak_name}__select_{offset}",
                                expression=select.freeze(), sense="le",
                                rhs=slack * ones,
                            ))
                    self._demand_peaks[key] = peak
                base_charge = _AffineAcc()
                base_charge.add_scaled(peak, rate_si)
                total.add_scaled(base_charge, 1.0)
                operating_month = month - 12 * construction - 1
                self._demand_rows.append({
                    "billing_id": billing_id,
                    "charge_id": charge_id,
                    "calendar_year": baseline_year + operating_month // 12,
                    "calendar_month": operating_month % 12 + 1,
                    "financial_year": (month - 1) // 12 + 1,
                    "month": month,
                    "peak": peak,
                    "base_charge": base_charge,
                    "rate": rate.to_dict(),
                })
            self._demand_totals[billing_id] = total

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
        if prefix in (self.doc.get("demand_bindings") or {}):
            if component != "period_demand_charge":
                raise LoweringUnsupportedError(
                    f"需量标识符 {ident!r} 的分量后缀非法"
                )
            return self._demand_totals[prefix].snapshot()
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
        for billing_id in sorted(self.doc.get("demand_bindings") or {}):
            symbols[f"{billing_id}__period_demand_charge"] = Counter({"currency": 1})
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
        for billing_id in self.doc.get("demand_bindings") or {}:
            names.add(f"{billing_id}__period_demand_charge")
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
        for billing_id, amount in sorted(self._demand_totals.items()):
            out[f"{billing_id}__period_demand_charge"] = {
                "unit": self.finance.currency,
                "caliber": "period",
                "constant": amount.constant,
                "terms": dict(sorted(amount.terms.items())),
            }
        return MappingProxyType(out)

    def _construction_affine(self) -> _AffineAcc:
        """建设总额仿射式（新建 costed 实例 upfront 分量之和，既有沉没不计）。

        与 ``finance_components`` 同一新增/既有增量语义：existing 只计
        周期可变分量，建设沉没成本不进入前瞻现金流；成本子项的明确
        税率分别形成实际付款。
        """
        out = _AffineAcc()
        instances = (self.doc.get("finance_binding") or {}).get("instances") or {}
        for inst in sorted(self.devices):
            if inst not in instances or instances[inst].get("type") != "costed":
                continue
            if self.devices[inst].get("asset_origin") != "new":
                continue
            finance_type = instances[inst]["finance_type"]
            ft_entry = self.finance.finance_types[finance_type]
            if ft_entry.upfront_capex is None:
                continue
            out.add_scaled(self._cost_paid_net(inst, "upfront_capex")[0], 1.0)
        return out

    def _cost_tax_factors(self) -> dict[str, tuple[float, float]]:
        """Explicit tax scope → (gross-payment multiplier, deductible input fraction).

        A whole-component scope covers all its leaves; a leaf overrides nothing:
        overlapping scopes were rejected by the finance input contract.  An
        unmentioned leaf retains its original payment and has no input credit.
        """
        factors: dict[str, tuple[float, float]] = {}
        for rule in self.finance.tax_rules.values():
            if isinstance(rule, VatRule):
                entries = [(item.for_path, item.rate, item.deductible_ratio)
                           for item in rule.om_input_tax]
            elif isinstance(rule, ConstructionVatRule):
                entries = [(rule.for_path, rule.rate, rule.deductible_ratio)]
            else:
                continue
            for path, rate, ratio in entries:
                gross = 1.0 + float(rate.value) if self.finance.price_basis == "tax_exclusive" else 1.0
                factors[path] = (gross, self._vat_gross_factor(rate) * float(ratio.value))
        return factors

    def _cost_paid_net(
        self, inst: str, component: str, start: int = 0, end: int | None = None,
    ) -> tuple[_AffineAcc, _AffineAcc]:
        """One leaf-level source for actual gross payment and tax-net basis.

        Both amounts are affine expressions of the same construction/operation
        variables.  VAT availability or refund timing never changes the net
        accounting basis.
        """
        binding = self.doc["finance_binding"]["instances"][inst]
        prefix = f"{binding['finance_type']}.{component}"
        comp = getattr(self.finance.finance_types[binding["finance_type"]], component)
        factors = self._cost_tax_factors()
        gross, net = _AffineAcc(), _AffineAcc()
        parts = (["fixed"] if comp.fixed is not None else []) + [
            f"linear.{driver}" for driver in sorted(comp.linear or {})
        ]
        for part in parts:
            amount = self._cost_affine(inst, component, start, end, part=part)
            if component == "annual_fixed_om":
                # Convert the declared annual rate into *one* full-year amount.
                amount_year = _AffineAcc()
                amount_year.add_scaled(amount, _YEAR_SECONDS)
                amount = amount_year
            multiplier, credit = factors.get(f"{prefix}.{part}", factors.get(prefix, (1.0, 0.0)))
            gross.add_scaled(amount, multiplier)
            net.add_scaled(amount, multiplier - credit)
        return gross, net

    def _cost_scope_affine(
        self, inst: str, path: str, start: int = 0, end: int | None = None,
    ) -> _AffineAcc:
        """The precise cost leaf or whole component referenced by a VAT rule."""
        _, component, *suffix = path.split(".")
        amount = self._cost_affine(inst, component, start, end,
                                   part=".".join(suffix) if suffix else None)
        if component == "annual_fixed_om":
            annual = _AffineAcc()
            annual.add_scaled(amount, _YEAR_SECONDS)
            return annual
        return amount

    def _vat_gross_markups(self) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
        """不含税口径下已声明税率对应的收付加成（价税合计出现金）。

        返回（价格加成，运维分量加成，建设加成），键分别为价格 ID、
        ``for`` 路径、finance_type，值为 ``1 + 税率``。含税口径基础
        金额已为全额、无规则无池时均返回空（系数恒 1）。可抵比例只
        影响抵扣资格，不缩小实际付款，加成恒用全额税率；维护按明确
        金额口径不加成；未声明税率不推断。
        """
        grouped = self._vat_grouped_rules()
        if grouped is None or self.finance.price_basis != "tax_exclusive":
            return {}, {}, {}
        prices: dict[str, float] = {}
        oms: dict[str, float] = {}
        construction: dict[str, float] = {}
        for pool_rules in grouped.values():
            for rule in pool_rules["vat"]:
                for entry in rule.in_tax:
                    prices[entry.price] = 1.0 + float(entry.rate.value)
                for entry in rule.out_tax:
                    prices[entry.price] = 1.0 + float(entry.rate.value)
                # Cost markups are applied per leaf by _cost_paid_net.  A
                # component-level multiplier would double-charge a leaf or
                # tax an explicitly uncovered sibling.
        return prices, oms, construction

    def _demand_tax_factors(self) -> dict[str, tuple[float, float]]:
        """费项→(实际付款加成, 可抵进项占基本账单比例)。

        不含税口径先全额加税再形成现金；含税口径先拆出进项。
        未声明进项的费项既不臆测税率也不臆测抵扣资格。
        """
        out: dict[str, tuple[float, float]] = {}
        for rule in self.finance.tax_rules.values():
            if not isinstance(rule, VatRule):
                continue
            for entry in rule.demand_input_tax:
                rate = float(entry.rate.value)
                gross = 1.0 + rate if self.finance.price_basis == "tax_exclusive" else 1.0
                deductible = self._vat_gross_factor(entry.rate) * float(
                    entry.deductible_ratio.value
                )
                out[entry.charge] = (gross, deductible)
        return out

    def _annual_fixed_om_annual(self, inst: str) -> _AffineAcc:
        """单个实例年固定 O&M 的年金额仿射式（每年全额）。

        复用既有年费仿射表达：分量声明口径为年率，年金额 = 年率 ×
        年秒数（与适配器 ``from_si`` 年值折算同源）。调用方已确保该
        分量存在，不为缺席兜空。
        """
        return self._cost_paid_net(inst, "annual_fixed_om")[0]

    def _retain_maintenance(self) -> None:
        """冻结清单逐笔构造一次仿射付款，并按评价年求和。

        属性接管复用 ``bound_prop``，未接管按该实例属性换算 SI；建设
        比例只取当前实例的 upfront_capex 分量，既有资产只可贡献规模项。
        所有引用/量纲/币种和成本存在性均已在公开提交边界核对。
        """
        positions: dict[int, int] = {}
        for entry in self.evaluation.maintenance_expenses:
            position = positions.get(entry.year, 0) + 1
            positions[entry.year] = position
            amount = _AffineAcc()
            amount.add_const(float(entry.amount))
            for term in entry.terms:
                if isinstance(term, ScaleUnitCostTerm):
                    factor = _unit_cost_to_internal(float(term.unit_cost.value), term.unit_cost.unit)
                    planned = self.bound_prop.get((term.instance_id, term.property))
                    if planned is not None:
                        amount.add_term(planned, factor)
                    else:
                        amount.add_const(
                            self._prop_const(term.instance_id, term.property) * factor
                        )
                elif isinstance(term, CostComponentShareTerm):
                    amount.add_scaled(
                        self._cost_affine(term.instance_id, "upfront_capex"),
                        float(term.ratio),
                    )
            self._maintenance_items[(entry.year, position)] = amount
            annual = self._maintenance_annual.setdefault(entry.year, _AffineAcc())
            annual.add_scaled(amount, 1.0)

    def _year_cash(self, year: int) -> _AffineAcc:
        """评价年未折现现金累积（与目标同一表达式并行累积，同源声明用）"""
        slot = self._fact_cash.get(year)
        if slot is None:
            slot = _AffineAcc()
            self._fact_cash[year] = slot
        return slot

    def _npv_objective(self) -> _AffineAcc:
        """显式全周期 NPV 目标仿射式（gross 现金流基础，同次解内表达）。

        现金表达式由 ``_retain_operating_facts`` 与声明同源构建，本方法
        只按冻结折现率折现求和（``CF[t]`` 口径见该方法）。
        """
        self._retain_operating_facts()
        evaluation = self.evaluation
        npv = _AffineAcc()

        def discount(year: int) -> float:
            return (1.0 + evaluation.discount_rate) ** year

        for t, ratio, acc in self._fact_payments:
            npv.add_scaled(acc, -1.0 / discount(t))
        for year_no in sorted(self._fact_annual):
            npv.add_scaled(self._fact_annual[year_no]["net"], 1.0 / discount(year_no))
        for row in self._fact_net_cashflows:
            sign = 1.0 if row["direction"] == "inflow" else -1.0
            npv.add_scaled(row["amount"], sign / discount(row["t"]))
        return npv

    def _retain_operating_facts(self) -> None:
        """实际 gross 经营/建设现金表达式（目标与声明同源，见方法文档）。

        ``CF[t]`` 评价年口径（``t = 0..E``，``E = K + N``）：建设总额按
        冻结付款比例落款；第 j 个运营年净额（能源净收益 − 可变 O&M −
        固定 O&M − 当年维护）计入 ``CF[K+j]``。能源按区间内实际步归集
        （公历年界与求解域同源）；固定 O&M 按年值口径每年全额；维护按
        清单年一次扣除。不含税口径下，已声明税率对应的建设/价格/实际
        消费 OM 按价税合计出现金（含税口径基础金额已为全额，不重复加
        税）；维护按明确金额口径不变。与规划目标选择无关，恒执行一次
        （构造器单次建模；重复调用会重复累积，调用方保证单次）。
        """
        evaluation = self.evaluation
        price_markup, _, _ = self._vat_gross_markups()
        demand_tax = self._demand_tax_factors()
        construction = self._construction_affine()
        baseline = self.doc["project_baseline"]
        bounds = annual_boundaries(
            baseline["resolution"], baseline["start_year"], evaluation.operation_years
        )
        instances = (self.doc.get("finance_binding") or {}).get("instances") or {}
        bindings = self.doc.get("tariff_bindings") or {}

        self._fact_construction.add_scaled(construction, 1.0)
        for payment in evaluation.construction_payments:
            amount = _AffineAcc()
            amount.add_scaled(construction, float(payment.ratio))
            self._year_cash(payment.t).add_scaled(construction, -float(payment.ratio))
            self._fact_payments.append((payment.t, payment.ratio, amount.snapshot()))
        for index, (start, end) in enumerate(bounds):
            year_no = evaluation.construction_period + index + 1
            energy = _AffineAcc()
            variable = _AffineAcc()
            fixed = _AffineAcc()
            demand = _AffineAcc()
            annual = _AffineAcc()
            for row in self._demand_rows:
                if row["financial_year"] == year_no:
                    demand.add_scaled(
                        row["base_charge"], demand_tax.get(row["charge_id"], (1.0, 0.0))[0]
                    )
            for binding_id in sorted(bindings):
                price_id = bindings[binding_id]["price"]
                energy.add_scaled(
                    self._tariff_affine(binding_id, start, end),
                    -price_markup.get(price_id, 1.0),
                )
            for inst in sorted(self.devices):
                if inst not in instances or instances[inst].get("type") != "costed":
                    continue
                origin = self.devices[inst].get("asset_origin")
                produced = _COMPONENTS_NEW if origin == "new" else _COMPONENTS_EXISTING
                finance_type = instances[inst]["finance_type"]
                ft_entry = self.finance.finance_types[finance_type]
                if (
                    "period_variable_om" in produced
                    and ft_entry.period_variable_om is not None
                ):
                    variable.add_scaled(
                        self._cost_paid_net(inst, "period_variable_om", start, end)[0], -1.0,
                    )
                if (
                    "annual_fixed_om" in produced
                    and ft_entry.annual_fixed_om is not None
                ):
                    fixed.add_scaled(self._annual_fixed_om_annual(inst), -1.0)
            annual.add_scaled(energy, 1.0)
            annual.add_scaled(variable, 1.0)
            annual.add_scaled(fixed, 1.0)
            annual.add_scaled(demand, -1.0)
            maintenance = self._maintenance_annual.get(year_no) or _AffineAcc()
            annual.add_scaled(maintenance, -1.0)
            self._year_cash(year_no).add_scaled(annual, 1.0)
            self._fact_annual[year_no] = {
                "energy": energy.snapshot(),
                "variable": variable.snapshot(),
                "fixed": fixed.snapshot(),
                "maintenance": maintenance.snapshot(),
                "demand": demand.snapshot(),
                "net": annual.snapshot(),
            }
            if index == 0:
                # 总量沿既有封存口径记正数（分量累积带负号，此处取反）。
                self._fact_fixed_om.add_scaled(fixed, -1.0)

    # -- VAT 月度精确关系（3.2.0；无规则无池时整段跳过）--

    def _vat_month_spans(self) -> list[tuple[int, int, int]]:
        """税务月 → 求解步半开区间（仅经营月有步，公历月界，含闰年）。

        步按运行起始年/分辨率映射公历月，再平移建设期（首运营月为
        ``12K+1``）。步数由已确认运营年限生成，映射恒落
        ``12K+1..12E``，此处不重复核对时域。
        """
        baseline = self.doc["project_baseline"]
        start_year = baseline["start_year"]
        step_hours = self.step_s / 3600.0
        construction = self.evaluation.construction_period
        origin = datetime(start_year, 1, 1)
        spans: dict[int, list[int]] = {}
        for step in range(self.point_count):
            moment = origin + timedelta(hours=step * step_hours)
            month = (
                12 * construction
                + (moment.year - start_year) * 12
                + moment.month
            )
            slot = spans.setdefault(month, [step, step])
            slot[1] = step
        return [
            (month, bounds[0], bounds[1] + 1)
            for month, bounds in sorted(spans.items())
        ]

    def _vat_grouped_rules(self) -> dict[str, dict[str, list]] | None:
        """已支持税则按池分组；无规则无池返回 None；未支持类型明确拒绝。

        结算口径/确认方式/池存在/分量口径已由公开财务入口核对，此处
        信任类型化交接。仅实现 ``vat`` 与 ``construction_input_vat``
        的建模；所得税规则由独立所得税路径建模，此处跳过不拒；其余
        未知类型不忽略、不半生成，在此唯一能力边界拒绝（provider 不
        再复制该判断）。
        """
        rules = self.finance.tax_rules
        pools = self.finance.credit_pools
        if not rules and not pools:
            return None
        grouped: dict[str, dict[str, list]] = {}
        for rule_id in sorted(rules):
            rule = rules[rule_id]
            if isinstance(rule, VatRule):
                grouped.setdefault(
                    rule.credit_pool, {"vat": [], "construction": []}
                )["vat"].append(rule)
            elif isinstance(rule, ConstructionVatRule):
                grouped.setdefault(
                    rule.credit_pool, {"vat": [], "construction": []}
                )["construction"].append(rule)
            elif isinstance(rule, IncomeTaxRule):
                continue
            else:
                raise LoweringUnsupportedError(
                    f"税务规则 {rule_id!r} 类型 {type(rule).__name__} 超出已实现关系"
                )
        for pool_id in sorted(pools):
            grouped.setdefault(pool_id, {"vat": [], "construction": []})
        return grouped

    def _vat_gross_factor(self, rate: Any) -> float:
        """含税/不含税总额 → 税额系数（税率已知，先合并金额系数）。"""
        value = float(rate.value)
        if self.finance.price_basis == "tax_exclusive":
            return value
        return value / (1.0 + value)

    @staticmethod
    def _vat_gross_split_factor(rate: Any) -> float:
        """实际含税付款 → 税额拆分系数（维护金额口径，不随文件 basis 改变）。"""
        value = float(rate.value)
        return value / (1.0 + value)

    def _vat_pool_month_bases(
        self,
        pool_rules: dict[str, list],
        spans: list[tuple[int, int, int]],
    ) -> tuple[list[_AffineAcc], list[_AffineAcc]]:
        """单池每税务月 (销项, 可抵进项) 仿射（无交易月为零）。

        经营数据按步区间归集；年度固定 OM 按运营年年末月确认；维护按
        清单年年末月确认；建设进项总额按确认方法落月。金额符号如实
        保留，不钳零。
        """
        months = 12 * self.evaluation.evaluation_years
        out: list[_AffineAcc] = [_AffineAcc() for _ in range(months)]
        inp: list[_AffineAcc] = [_AffineAcc() for _ in range(months)]
        construction = self.evaluation.construction_period
        operation = self.evaluation.operation_years
        bindings = self.doc.get("tariff_bindings") or {}
        instances = (self.doc.get("finance_binding") or {}).get("instances") or {}
        for rule in pool_rules["vat"]:
            for entry in rule.demand_input_tax:
                factor = self._vat_gross_factor(entry.rate) * float(
                    entry.deductible_ratio.value
                )
                for row in self._demand_rows:
                    if row["charge_id"] == entry.charge:
                        inp[row["month"] - 1].add_scaled(row["base_charge"], factor)
            for entry in rule.in_tax:
                factor = self._vat_gross_factor(entry.rate) * float(
                    entry.deductible_ratio.value
                )
                for binding_id, binding in bindings.items():
                    if binding.get("price") != entry.price:
                        continue
                    for month, start, end in spans:
                        inp[month - 1].add_scaled(
                            self._tariff_affine(binding_id, start, end), factor
                        )
            for entry in rule.out_tax:
                factor = self._vat_gross_factor(entry.rate)
                for binding_id, binding in bindings.items():
                    if binding.get("price") != entry.price:
                        continue
                    for month, start, end in spans:
                        # 销项记账为负，税基取相反数（符号如实保留）。
                        out[month - 1].add_scaled(
                            self._tariff_affine(binding_id, start, end), -factor
                        )
            for entry in rule.om_input_tax:
                finance_type, _, scope = entry.for_path.partition(".")
                component = scope.split(".", 1)[0]
                factor = self._vat_gross_factor(entry.rate) * float(
                    entry.deductible_ratio.value
                )
                for inst in sorted(self.devices):
                    if inst not in instances or instances[inst].get("type") != "costed":
                        continue
                    if instances[inst].get("finance_type") != finance_type:
                        continue
                    # 与基础现金同一增量语义：既有沉没固定 OM 不消费，
                    # 不产生进项；真实消费的可变分量保留。
                    produced = (
                        _COMPONENTS_NEW
                        if self.devices[inst].get("asset_origin") == "new"
                        else _COMPONENTS_EXISTING
                    )
                    if component not in produced:
                        continue
                    ft_entry = self.finance.finance_types[finance_type]
                    if getattr(ft_entry, component) is None:
                        continue
                    if component == "annual_fixed_om":
                        amount = self._cost_scope_affine(inst, entry.for_path)
                        for year in range(construction + 1, construction + operation + 1):
                            inp[12 * year - 1].add_scaled(amount, factor)
                    else:
                        for month, start, end in spans:
                            inp[month - 1].add_scaled(
                                self._cost_scope_affine(inst, entry.for_path, start, end), factor
                            )
            for entry in rule.maintenance_input_tax:
                # 引用存在性与跨输入对应已由提交边界核对；同一 (year,
                # position) 直接消费逐笔金额。维护金额为实际含税付款，
                # 恒按含税拆价计税，不随文件 price_basis 改变。
                factor = self._vat_gross_split_factor(entry.rate) * float(
                    entry.deductible_ratio.value
                )
                inp[12 * entry.year - 1].add_scaled(
                    self._maintenance_items[(entry.year, entry.position)], factor
                )
        for rule in pool_rules["construction"]:
            # 分量口径（仅 upfront_capex）与引用存在性已由公开财务入口
            # 核对；新建范围与基础建设总额同一增量语义。
            finance_type, _, _ = rule.for_path.partition(".")
            total = _AffineAcc()
            for inst in sorted(self.devices):
                if inst not in instances or instances[inst].get("type") != "costed":
                    continue
                if self.devices[inst].get("asset_origin") != "new":
                    continue
                if instances[inst].get("finance_type") != finance_type:
                    continue
                ft_entry = self.finance.finance_types[finance_type]
                if ft_entry.upfront_capex is None:
                    continue
                total.add_scaled(self._cost_scope_affine(inst, rule.for_path), 1.0)
            factor = self._vat_gross_factor(rule.rate) * float(
                rule.deductible_ratio.value
            )
            deductible = _AffineAcc()
            deductible.add_scaled(total, factor)
            method = rule.confirmation.method
            if method == "payment_period":
                for payment in self.evaluation.construction_payments:
                    inp[12 * payment.t].add_scaled(deductible, float(payment.ratio))
            else:
                # explicit_schedule：比例直接作用于派生总额，不再次乘
                # 付款比例。方法枚举与确认期上界已在入口/提交边界核对。
                for item in rule.confirmation.schedule:
                    inp[item.period - 1].add_scaled(deductible, float(item.ratio))
        return out, inp

    @staticmethod
    def _affine_side(
        affine: _AffineAcc,
        var_bounds: Mapping[str, tuple[float | None, float | None]],
        *,
        side: str,
        label: str,
    ) -> float:
        """仿射式在变量盒上的确界；无界即明确能力不支持。

        有限界由实际变量界推导，不猜常量；缺界命名位置/变量后拒绝，
        不填业务默认。``label`` 为调用方业务位置（如 VAT 池月、所得税年）。
        """
        total = affine.constant
        for name, coeff in affine.terms.items():
            if coeff == 0.0:
                continue
            if name not in var_bounds:
                raise LoweringUnsupportedError(
                    f"{label}引用未知变量 {name!r}"
                )
            lower, upper = var_bounds[name]
            bound = upper if (coeff > 0.0) == (side == "max") else lower
            if bound is None or not math.isfinite(bound):
                raise LoweringUnsupportedError(
                    f"{label}缺有限界：变量 {name!r} 界不足，无法精确"
                )
            total += coeff * bound
        return total

    def _build_vat(self) -> _AffineAcc | None:
        """各池连续月度 paid/carry 精确链；返回 NPV 现金仿射（或 None）。

        ``paid - carry = out - in - 上月余额`` 等式 + 非负 + 互斥
        （已有 Big-M，界由变量界与累计推导）确定唯一精确状态，不靠
        目标压紧；退款按声明从可用留抵扣减（超额即不可行），到账月记
        现金，期后到账仅应收。约束与目标选择无关，现金只入 NPV。
        """
        grouped = self._vat_grouped_rules()
        if grouped is None:
            return None
        evaluation = self.evaluation
        months = 12 * evaluation.evaluation_years
        spans = self._vat_month_spans()
        var_bounds = {
            variable.name: (variable.lower, variable.upper)
            for variable in self.variables.values()
        }
        taken = set(var_bounds)
        cash = _AffineAcc()
        rate = evaluation.discount_rate

        def discount(year: int) -> float:
            return (1.0 + rate) ** year

        for pool_id in sorted(grouped):
            pool = self.finance.credit_pools[pool_id]
            out_list, in_list = self._vat_pool_month_bases(grouped[pool_id], spans)
            claims: dict[int, float] = {}
            arrivals: dict[int, float] = {}
            for refund in pool.refunds:
                amount = _money_to_internal(refund.amount)
                claims[refund.period] = claims.get(refund.period, 0.0) + amount
                if refund.arrival_period <= months:
                    arrivals[refund.arrival_period] = (
                        arrivals.get(refund.arrival_period, 0.0) + amount
                    )
                self._fact_refunds.append(
                    {
                        "pool": pool_id,
                        "period": refund.period,
                        "arrival_period": refund.arrival_period,
                        "amount": amount,
                    }
                )
            cumulative_hi = _money_to_internal(pool.opening_credit)
            previous = _AffineAcc()
            previous.add_const(cumulative_hi)
            for month in range(1, months + 1):
                location = f"VAT 池 {pool_id!r} 第 {month} 月"
                out_max = self._affine_side(
                    out_list[month - 1], var_bounds, side="max", label=location
                )
                out_min = self._affine_side(
                    out_list[month - 1], var_bounds, side="min", label=location
                )
                in_max = self._affine_side(
                    in_list[month - 1], var_bounds, side="max", label=location
                )
                in_min = self._affine_side(
                    in_list[month - 1], var_bounds, side="min", label=location
                )
                # 留抵上界须覆盖销项为负（销售记账抵减）的一侧：单调递推
                # cumulative_hi（只增不减）保证归纳每一步不切割真解；
                # max 内置于递推会破坏归纳，此处不取。
                cumulative_hi += max(in_max - out_min, 0.0)
                # 上月余额非负（期初解析禁负 + 退款声明受可用约束），
                # 故实缴上界取 max(out - in, 0) 的盒确界即 sound。
                paid_hi = max(out_max - in_min, 0.0)
                if not math.isfinite(paid_hi):
                    raise LoweringUnsupportedError(
                        f"VAT 池 {pool_id!r} 第 {month} 月实缴无有限上界"
                    )
                paid_name = f"vat__{pool_id}__paid__m{month}"
                carry_name = f"vat__{pool_id}__carry__m{month}"
                for name in (paid_name, carry_name):
                    if name in taken:
                        raise LoweringUnsupportedError(
                            f"VAT 内部列名与已有变量冲突 {name!r}"
                        )
                    taken.add(name)
                self.variables[paid_name] = LinearVariable(
                    paid_name, lower=0.0, upper=paid_hi,
                )
                self.variables[carry_name] = LinearVariable(
                    carry_name, lower=0.0, upper=cumulative_hi,
                )
                if paid_hi > 0.0 and cumulative_hi > 0.0:
                    mutex = lower_mutex(
                        f"vat__{pool_id}__m{month}",
                        (paid_name, 0),
                        (carry_name, 0),
                        {
                            paid_name: ("var", 0.0, paid_hi),
                            carry_name: ("var", 0.0, cumulative_hi),
                        },
                    )
                    binary = mutex.variables[0]
                    self.variables[binary.name] = binary
                    self.rows.extend(mutex.constraints)
                equation = _AffineAcc()
                equation.add_term(paid_name, 1.0)
                equation.add_term(carry_name, -1.0)
                equation.add_scaled(out_list[month - 1], -1.0)
                equation.add_scaled(in_list[month - 1], 1.0)
                equation.add_scaled(previous, 1.0)
                self.rows.append(
                    LinearConstraint(
                        name=f"vat__{pool_id}__eq__m{month}",
                        expression=equation.freeze(),
                        sense="eq",
                        rhs=0.0,
                    )
                )
                claim = claims.get(month, 0.0)
                if claim > 0.0:
                    claim_expr = _AffineAcc()
                    claim_expr.add_term(carry_name, 1.0)
                    self.rows.append(
                        LinearConstraint(
                            name=f"vat__{pool_id}__claim__m{month}",
                            expression=claim_expr.freeze(),
                            sense="ge",
                            rhs=claim,
                        )
                    )
                year = (month + 11) // 12
                cash.add_term(paid_name, -1.0 / discount(year))
                if month in arrivals:
                    cash.add_const(arrivals[month] / discount(year))
                self._year_cash(year).add_term(paid_name, -1.0)
                if month in arrivals:
                    self._year_cash(year).add_const(arrivals[month])
                self._fact_vat.append(
                    {
                        "pool": pool_id,
                        "month": month,
                        "paid": paid_name,
                        "carry": carry_name,
                        "claim": claim,
                        "arrival": arrivals.get(month, 0.0),
                    }
                )
                previous = _AffineAcc()
                previous.add_term(carry_name, 1.0)
                previous.add_const(-claim)
        return cash

    # -- 所得税年度关系（3.3.0；无所得税规则时整段跳过）--

    def _income_rules(self) -> list[tuple[str, IncomeTaxRule]]:
        """已解析所得税规则（按规则 ID 排序；未知类型由分组边界拒绝）。"""
        return sorted(
            (rule_id, rule)
            for rule_id, rule in self.finance.tax_rules.items()
            if isinstance(rule, IncomeTaxRule)
        )

    def _income_vat_credits(
        self, grouped: dict[str, dict[str, list]] | None
    ) -> tuple[
        dict[str, tuple[float, float]],
        dict[tuple[int, int], float],
        dict[str, tuple[float, float]],
        dict[str, tuple[float, float]],
        dict[str, tuple[float, float]],
    ]:
        """所得税可扣口径对应的 VAT 抵免查找。

        返回（运维抵免，维护抵免，建设抵免，销项税，购能进项税）：运维
        键为 ``for`` 路径，值为（抵免系数，价税合计系数）；维护键为
        ``(year, position)``，值为多条合计系数（维护金额恒含税拆价）；
        建设键为 finance_type，值为（抵免系数，价税合计系数）；销项键
        为价格 ID，值为（税额系数，价税合计系数）；购能键为价格 ID，
        值为（抵免系数，价税合计系数）。价税合计系数仅在不含税口径且
        有已声明税率时为 ``1 + 税率``（先形成实际收付款），其余为 1；
        无声明不推断。引用存在性与口径已由公开财务入口核对，此处只
        消费；同一 ``for``/价格全局唯一，已在入口保证。
        """
        om: dict[str, tuple[float, float]] = {}
        maint: dict[tuple[int, int], float] = {}
        construction: dict[str, tuple[float, float]] = {}
        out_tax: dict[str, tuple[float, float]] = {}
        in_tax: dict[str, tuple[float, float]] = {}
        exclusive = self.finance.price_basis == "tax_exclusive"
        if grouped is None:
            return om, maint, construction, out_tax, in_tax
        for pool_rules in grouped.values():
            for rule in pool_rules["vat"]:
                for entry in rule.in_tax:
                    rate = float(entry.rate.value)
                    in_tax[entry.price] = (
                        self._vat_gross_factor(entry.rate)
                        * float(entry.deductible_ratio.value),
                        1.0 + rate if exclusive else 1.0,
                    )
                for entry in rule.out_tax:
                    rate = float(entry.rate.value)
                    out_tax[entry.price] = (
                        self._vat_gross_factor(entry.rate),
                        1.0 + rate if exclusive else 1.0,
                    )
                for entry in rule.om_input_tax:
                    rate = float(entry.rate.value)
                    om[entry.for_path] = (
                        self._vat_gross_factor(entry.rate)
                        * float(entry.deductible_ratio.value),
                        1.0 + rate if exclusive else 1.0,
                    )
                for entry in rule.maintenance_input_tax:
                    key = (entry.year, entry.position)
                    maint[key] = maint.get(key, 0.0) + self._vat_gross_split_factor(
                        entry.rate
                    ) * float(entry.deductible_ratio.value)
            for rule in pool_rules["construction"]:
                finance_type, _, _ = rule.for_path.partition(".")
                rate = float(rule.rate.value)
                markup = 1.0 + rate if exclusive else 1.0
                construction[finance_type] = (
                    self._vat_gross_factor(rule.rate) * float(rule.deductible_ratio.value),
                    markup,
                )
        return om, maint, construction, out_tax, in_tax

    def _income_deduct_om(
        self,
        base: _AffineAcc,
        for_path: str,
        span: tuple[int, int] | None,
        om_credit: dict[str, tuple[float, float]],
        instances: Mapping[str, Any],
    ) -> None:
        """运维可扣额 = 实际付款 − 已声明 VAT 抵免（与基础现金同一增量范围）。

        不含税口径下已声明税率的实际付款先按价税合计加成，再减明确可抵
        额；含税口径基础金额已为全额。既有沉没固定 OM 不消费亦不抵扣；
        真实消费的可变分量保留。可抵比例只改变抵扣额，不改变实际付款。
        """
        finance_type, _, component = for_path.partition(".")
        for inst in sorted(self.devices):
            if inst not in instances or instances[inst].get("type") != "costed":
                continue
            if instances[inst].get("finance_type") != finance_type:
                continue
            produced = (
                _COMPONENTS_NEW
                if self.devices[inst].get("asset_origin") == "new"
                else _COMPONENTS_EXISTING
            )
            if component not in produced:
                continue
            ft_entry = self.finance.finance_types[finance_type]
            if getattr(ft_entry, component) is None:
                continue
            # Income tax deducts the whole incurred component, not just its
            # VAT-covered leaves.  Uncovered siblings keep full basis.
            start, end = span if span is not None else (0, None)
            base.add_scaled(self._cost_paid_net(inst, component, start, end)[1], -1.0)

    def _maintenance_net(self, year: int, position: int) -> _AffineAcc:
        """The uniquely identified payment less explicitly eligible input VAT."""
        key = (year, position)
        credit = 0.0
        for rule in self.finance.tax_rules.values():
            if isinstance(rule, VatRule):
                for item in rule.maintenance_input_tax:
                    if (item.year, item.position) == key:
                        credit += self._vat_gross_split_factor(item.rate) * float(
                            item.deductible_ratio.value
                        )
        net = _AffineAcc()
        net.add_scaled(self._maintenance_items[key], 1.0 - credit)
        return net

    def _source_net(self, source: Any) -> _AffineAcc:
        """Resolve each typed money source once for depreciation and net cash."""
        key = source.identity
        if key not in self._net_sources:
            amount = _AffineAcc()
            if source.kind == "upfront_capex_net":
                instances = self.doc["finance_binding"]["instances"]
                for inst in sorted(self.devices):
                    binding = instances.get(inst)
                    if (binding is None or binding["type"] != "costed"
                            or binding["finance_type"] != source.finance_type
                            or self.devices[inst]["asset_origin"] != "new"):
                        continue
                    amount.add_scaled(self._cost_paid_net(inst, "upfront_capex")[1], 1.0)
            else:
                amount.add_scaled(self._maintenance_net(source.year, source.position), 1.0)
            self._net_sources[key] = amount
        return self._net_sources[key]

    def _income_deduct_depreciation(
        self,
        base: _AffineAcc,
        rule: IncomeTaxRule,
        rule_id: str,
        year: int,
        con_credit: dict[str, tuple[float, float]],
        instances: Mapping[str, Any],
        var_bounds: Mapping[str, tuple[float | None, float | None]],
        location: str,
    ) -> None:
        """直线折旧可扣额（计税基础随建设选择与容量派生；残值只减一次）。

        单资产年额 =（价税合计建设额 − 可抵建设进项 − 残值）/ 年限，
        仅在 ``[K+first_year, K+first_year+life)`` 计提。不可抵进项留在
        计税基础中；无已声明建设税率时基础即陈述建设额，不推断。派生
        建设额静态已知为零（含无本课题新增可折旧资产）时不计提，残值
        亦不产生税盾或反向所得；静态恒已形成且残值不超过最小计税基础
        时按公式计提，超基础则明确拒绝（不以负折旧放行）；可变容量下
        固定正残值无法静态判定资产是否形成时明确拒绝，不猜形成状态。
        界由实际变量盒推导，不猜常量。
        """
        construction = self.evaluation.construction_period
        for entry in rule.depreciation:
            if not (
                construction + entry.first_year
                <= year
                < construction + entry.first_year + entry.life_years
            ):
                continue
            source = entry.base_from
            depreciable = self._source_net(source)
            residual = float(entry.residual.value)
            position = f"{location}折旧来源 {source.identity!r}"
            # A ratio residual is affine even when the asset can be absent.
            # A positive fixed residual must not turn absence into a tax shield.
            if entry.residual.unit == "1":
                residual_amount = _AffineAcc()
                residual_amount.add_scaled(depreciable, residual)
            else:
                residual_amount = _AffineAcc()
                residual_amount.add_const(residual)
                if residual > 0.0:
                    basis_lo = self._affine_side(depreciable, var_bounds, side="min", label=position)
                    basis_hi = self._affine_side(depreciable, var_bounds, side="max", label=position)
                    if basis_hi <= 0.0:
                        residual_amount = _AffineAcc()
                    elif basis_lo <= 0.0:
                        raise LoweringUnsupportedError(
                            f"{position}的可变容量固定正残值在零建设与形成资产之间无法精确选择，"
                            "超出当前选建能力"
                        )
                    elif residual > basis_lo:
                        raise LoweringUnsupportedError(
                            f"{position}残值 {residual!r} 超过最小计税基础 {basis_lo!r}"
                        )
            annual = _AffineAcc()
            life = float(entry.life_years)
            annual.add_scaled(depreciable, 1.0 / life)
            annual.add_scaled(residual_amount, -1.0 / life)
            base.add_scaled(annual, -1.0)
            self._fact_depr.append(
                {"rule": rule_id, "base_from": source.to_dict(),
                 "year": year, "amount": annual}
            )

    def _income_annual_base(
        self,
        rule: IncomeTaxRule,
        rule_id: str,
        year: int,
        year_span: tuple[int, int] | None,
        credits: tuple[
            dict[str, tuple[float, float]],
            dict[tuple[int, int], float],
            dict[str, tuple[float, float]],
            dict[str, tuple[float, float]],
            dict[str, tuple[float, float]],
        ],
        var_bounds: Mapping[str, tuple[float | None, float | None]],
        location: str,
    ) -> _AffineAcc:
        """单所得税规则单评价年应税基础（收入 − 成本 − 可扣维护 − 折旧）。

        计税收入 = 实际销售收款 − 已声明销项税；可扣购能费用 = 实际含税
        付款 − 已声明可抵进项，不可抵部分保留。不含税口径下先按已声明
        税率形成实际收付款；无声明不推断。负价符号如实保留。维护可扣额
        为含税付款扣已声明 VAT 抵免。实缴/留抵兑现时点不改变这些税基。
        """
        base = _AffineAcc()
        instances = (self.doc.get("finance_binding") or {}).get("instances") or {}
        bindings = self.doc.get("tariff_bindings") or {}
        om_credit, maint_credit, _, out_tax, in_tax = credits
        if year_span is not None:
            start, end = year_span
            revenue = set(rule.revenue)
            energy = set(rule.energy_cost)
            for binding_id in sorted(bindings):
                price_id = bindings[binding_id]["price"]
                if price_id in revenue:
                    factor, markup = out_tax.get(price_id, (0.0, 1.0))
                    base.add_scaled(
                        self._tariff_affine(binding_id, start, end), -(markup - factor)
                    )
                elif price_id in energy:
                    coef, markup = in_tax.get(price_id, (0.0, 1.0))
                    base.add_scaled(
                        self._tariff_affine(binding_id, start, end), -(markup - coef)
                    )
            demand_tax = self._demand_tax_factors()
            for row in self._demand_rows:
                if row["financial_year"] != year or row["charge_id"] not in rule.demand_cost:
                    continue
                gross, credit = demand_tax.get(row["charge_id"], (1.0, 0.0))
                base.add_scaled(row["base_charge"], -(gross - credit))
            for for_path in rule.annual_om:
                self._income_deduct_om(base, for_path, None, om_credit, instances)
            for for_path in rule.variable_om:
                self._income_deduct_om(
                    base, for_path, (start, end), om_credit, instances
                )
        for item in rule.deductible_maintenance.items:
            if item.year != year or not item.deductible:
                continue
            source = MaintenanceExpenseNetBaseFrom(year=item.year, position=item.position)
            base.add_scaled(self._source_net(source), -1.0)
        self._income_deduct_depreciation(
            base, rule, rule_id, year, credits[2], instances, var_bounds, location
        )
        return base

    def _retain_net_cashflows(self) -> None:
        """Declare each explicit after-tax receipt/payment and enter CF once.

        These entries are already *net*.  They do not belong to operating net
        income, VAT output or the income-tax base and are never inferred from
        book residual value.
        """
        for cash_id, entry in sorted(self.finance.net_cashflows.items()):
            t = (self.evaluation.evaluation_years if entry.at.kind == "evaluation_end"
                 else entry.at.year)
            amount = _AffineAcc()
            if entry.amount.kind == "fixed":
                amount.add_const(float(entry.amount.value.value))
            else:
                amount.add_scaled(self._source_net(entry.amount.base_from),
                                  float(entry.amount.ratio.value))
            direction = 1.0 if entry.direction == "inflow" else -1.0
            self._year_cash(t).add_scaled(amount, direction)
            self._fact_net_cashflows.append({
                "id": cash_id, "t": t, "direction": entry.direction,
                "amount": amount.snapshot(),
            })
        self._fact_net_cashflows.sort(key=lambda row: (row["t"], row["id"]))

    def _build_income(self) -> _AffineAcc | None:
        """各所得税规则年度计税/亏损/税额精确链；返回 NPV 现金仿射（或 None）。

        ``profit - gen = base`` 等式 + 非负 + 互斥确定唯一正负分段；
        亏损按（到期年，输入顺序）逐笔精确领取（有序 min 的等价约束，
        界由变量盒与累计推导），到期自动核销，新生来自负原始基础且
        按明确年限结转。税额 = 税率 ×（正所得 − 领取），按评价年入现金。
        约束与目标选择无关，现金只入 NPV；折旧/亏损抵扣不是现金流，
        不重复扣除。
        """
        rules = self._income_rules()
        if not rules:
            return None
        credits = self._income_vat_credits(self._vat_grouped_rules())
        evaluation = self.evaluation
        years = evaluation.evaluation_years
        construction = evaluation.construction_period
        operation = evaluation.operation_years
        baseline = self.doc["project_baseline"]
        bounds = annual_boundaries(
            baseline["resolution"], baseline["start_year"], operation
        )
        span_by_year = {
            construction + index + 1: span for index, span in enumerate(bounds)
        }
        var_bounds = {
            variable.name: (variable.lower, variable.upper)
            for variable in self.variables.values()
        }
        taken = set(var_bounds)
        cash = _AffineAcc()
        tax_rate_default = evaluation.discount_rate

        def discount(year: int) -> float:
            return (1.0 + tax_rate_default) ** year

        def declare(name: str, upper: float, binary: bool = False) -> None:
            if name in taken:
                raise LoweringUnsupportedError(
                    f"所得税内部列名与已有变量冲突 {name!r}"
                )
            taken.add(name)
            self.variables[name] = LinearVariable(
                name,
                lower=0.0,
                upper=upper,
                kind="binary" if binary else "continuous",
            )

        def constrain(
            name: str, expression: _AffineAcc, sense: str, rhs: float
        ) -> None:
            self.rows.append(
                LinearConstraint(
                    name=name, expression=expression.freeze(), sense=sense, rhs=rhs
                )
            )

        for rule_id, rule in rules:
            tax_rate = float(rule.rate.value)
            carry_years = int(rule.loss_carryforward_years)
            vintages: list[dict] = []
            for position, opening in enumerate(rule.opening_losses):
                opening_acc = _AffineAcc()
                opening_acc.add_const(float(opening.amount.value))
                vintages.append(
                    {
                        "seq": position,
                        "last": int(opening.last_usable_year),
                        "init_upper": float(opening.amount.value),
                        "rem": opening_acc,
                    }
                )
            seq_next = len(vintages)
            for year in range(1, years + 1):
                location = f"所得税规则 {rule_id!r} 第 {year} 年"
                expired: list[tuple[str, int, _AffineAcc]] = []
                for vintage in vintages:
                    if vintage["last"] == year - 1:
                        expired.append(
                            (
                                f"v{vintage['seq']}",
                                vintage["last"],
                                vintage["rem"].snapshot(),
                            )
                        )
                base = self._income_annual_base(
                    rule, rule_id, year, span_by_year.get(year), credits, var_bounds,
                    location,
                )
                base_kept = base.snapshot()
                base_hi = self._affine_side(
                    base, var_bounds, side="max", label=location
                )
                base_lo = self._affine_side(
                    base, var_bounds, side="min", label=location
                )
                profit_hi = max(base_hi, 0.0)
                gen_hi = max(-base_lo, 0.0)
                profit_name = f"inc__{rule_id}__profit__y{year}"
                gen_name = f"inc__{rule_id}__gen__y{year}"
                declare(profit_name, profit_hi)
                declare(gen_name, gen_hi)
                equation = _AffineAcc()
                equation.add_term(profit_name, 1.0)
                equation.add_term(gen_name, -1.0)
                equation.add_scaled(base, -1.0)
                constrain(f"inc__{rule_id}__eq__y{year}", equation, "eq", 0.0)
                if profit_hi > 0.0 and gen_hi > 0.0:
                    mutex = lower_mutex(
                        f"inc__{rule_id}__m{year}",
                        (profit_name, 0),
                        (gen_name, 0),
                        {
                            profit_name: ("var", 0.0, profit_hi),
                            gen_name: ("var", 0.0, gen_hi),
                        },
                    )
                    binary = mutex.variables[0]
                    self.variables[binary.name] = binary
                    self.rows.extend(mutex.constraints)
                available = sorted(
                    (vintage for vintage in vintages if vintage["last"] >= year),
                    key=lambda vintage: (vintage["last"], vintage["seq"]),
                )
                need = _AffineAcc()
                need.add_term(profit_name, 1.0)
                used_total = _AffineAcc()
                used_names: list[str] = []
                for vintage in available:
                    seq = vintage["seq"]
                    use_name = f"inc__{rule_id}__use__y{year}__v{seq}"
                    used_names.append(use_name)
                    order_name = f"inc__{rule_id}__ord__y{year}__v{seq}"
                    use_upper = min(profit_hi, vintage["init_upper"])
                    declare(use_name, use_upper)
                    declare(order_name, 1.0, binary=True)
                    big_m = profit_hi + vintage["init_upper"]
                    take_cap = _AffineAcc()
                    take_cap.add_term(use_name, 1.0)
                    take_cap.add_scaled(need, -1.0)
                    constrain(
                        f"inc__{rule_id}__take__y{year}__v{seq}__a",
                        take_cap, "le", 0.0,
                    )
                    take_avail = _AffineAcc()
                    take_avail.add_term(use_name, 1.0)
                    take_avail.add_scaled(vintage["rem"], -1.0)
                    constrain(
                        f"inc__{rule_id}__take__y{year}__v{seq}__b",
                        take_avail, "le", 0.0,
                    )
                    take_need = _AffineAcc()
                    take_need.add_term(use_name, 1.0)
                    take_need.add_scaled(need, -1.0)
                    take_need.add_term(order_name, -big_m)
                    constrain(
                        f"inc__{rule_id}__take__y{year}__v{seq}__c",
                        take_need, "ge", -big_m,
                    )
                    take_balance = _AffineAcc()
                    take_balance.add_term(use_name, 1.0)
                    take_balance.add_scaled(vintage["rem"], -1.0)
                    take_balance.add_term(order_name, big_m)
                    constrain(
                        f"inc__{rule_id}__take__y{year}__v{seq}__d",
                        take_balance, "ge", 0.0,
                    )
                    need.add_term(use_name, -1.0)
                    used_total.add_term(use_name, 1.0)
                    vintage["rem"].add_term(use_name, -1.0)
                cash.add_term(profit_name, -tax_rate / discount(year))
                cash.add_scaled(used_total, tax_rate / discount(year))
                self._year_cash(year).add_term(profit_name, -tax_rate)
                self._year_cash(year).add_scaled(used_total, tax_rate)
                self._fact_income.append(
                    {
                        "rule": rule_id,
                        "year": year,
                        "base": base_kept,
                        "used": list(used_names),
                        "gen": gen_name,
                        "expired": expired,
                        "rate": tax_rate,
                        "profit": profit_name,
                    }
                )
                if gen_hi > 0.0:
                    # 上界为零则实现恒为零，B0 亦不追加余额；跳过以保持
                    # 序号与 B0 对齐（上界非零而实现为零的年份不受影响，
                    # 其后年份的可用集与 B0 一致）。
                    gen_acc = _AffineAcc()
                    gen_acc.add_term(gen_name, 1.0)
                    vintages.append(
                        {
                            "seq": seq_next,
                            "last": year + carry_years,
                            "init_upper": gen_hi,
                            "rem": gen_acc,
                        }
                    )
                    seq_next += 1
                # 年末余额：本年新生已入列（B0 年末剩余含当年新生），
                # 到期批次已在可用集外，不在此重复。
                for vintage in vintages:
                    if vintage["last"] >= year:
                        self._fact_balances.append(
                            {
                                "rule": rule_id,
                                "vintage_id": f"v{vintage['seq']}",
                                "year": year,
                                "last": vintage["last"],
                                "rem": vintage["rem"].snapshot(),
                            }
                        )
        return cash

    def _build_objective(self) -> tuple[_AffineAcc, str]:
        planning = self.doc["planning"]
        raw = planning["objective"]
        if (
            isinstance(raw.get("expression"), str)
            and raw["expression"].strip() == SYSTEM_AFTER_TAX_NPV
        ):
            # 显式全周期 NPV 目标：方向已由装配输入边界核对，此处信任
            # 类型化交接，不重复拒绝。基础 gross 现金与 VAT 现金（实缴
            # 流出、到账退税流入）只入本目标；税额状态约束与目标选择
            # 无关，已在建模段落实。
            npv = self._npv_objective()
            if self._vat_cash is not None:
                npv.add_scaled(self._vat_cash, 1.0)
            if self._income_cash is not None:
                npv.add_scaled(self._income_cash, 1.0)
            return npv, self.finance.currency
        # 普通目标不消费经营现金，但同次解财务事实仍须声明（与目标无关）。
        self._retain_operating_facts()
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

    def _build_step_relations(self) -> None:
        """Compile each frozen relation once; bind it to existing port columns at every t.

        The public assembly gate has already checked indices, references and
        dimensions. There is no independent flow or post-solve repair variable.
        """
        constraints = self.doc["planning"].get("constraints") or {}
        plan_dimensions = {}
        for name, spec in self.plan_vars.items():
            unit = spec.get("unit") or self._bound_prop_unit(
                spec["device_ref"], spec["parameter"],
            )
            plan_dimensions[name] = Counter(dict(dims_of(unit)))
        for relation_id in sorted(constraints):
            entry = constraints[relation_id]
            if entry.get("type") != "step_relation" or entry.get("enabled", True) is False:
                continue
            references = entry["references"]
            dimensions = {
                **plan_dimensions,
                TIMELINE_STEP_DURATION_VAR: Counter({"time": 1}),
            }
            for alias, target in references.items():
                flow = self.models[target["instance"]].interfaces[target["interface"]]
                dimensions[alias] = Counter(dict(dims_of(flow.unit)))
            compiled = compile_step_relation_expression(entry["expression"], dimensions)
            placeholders = {
                name: (("const", self.step_s) if name == TIMELINE_STEP_DURATION_VAR
                       else ("var", None, None))
                for name in dimensions
            }
            # The pure lowerer uses the shared planning IR/affine rules. It
            # produces one symbolic row that is instantiated without re-parsing.
            template = lower_planning_constraint(
                relation_id, compiled, placeholders,
            )
            for step in range(self.point_count):
                self._checkpoint()
                row = _AffineAcc()
                row.add_const(template.expression.constant)
                for (name, offset), coefficient in template.expression.terms:
                    if offset:
                        raise LoweringUnsupportedError(
                            f"step relation {relation_id!r} has an unsupported offset"
                        )
                    if name in references:
                        target = references[name]
                        key = (target["instance"], target["interface"])
                        # _step_columns is created by _declare_instance_statics,
                        # the same registry subsequently used by step_series().
                        column = self._step_columns[key][step]
                    elif name in self.plan_vars:
                        column = name  # one scalar capacity decision for all steps
                    else:
                        raise LoweringUnsupportedError(
                            f"step relation {relation_id!r}: unknown symbol {name!r}"
                        )
                    row.add_term(column, coefficient)
                self.rows.append(LinearConstraint(
                    name=f"step__{relation_id}__t{step}",
                    expression=row.freeze(),
                    sense=template.sense,
                    rhs=template.rhs,
                ))

    def _build_planning_rows(self) -> None:
        """已启用规划约束行（约束 type 标签留装配产物，不进 LP）。"""
        planning = self.doc["planning"]
        constraints = planning.get("constraints") or {}
        symbols, placeholders = self._planning_symbols()
        for name in sorted(constraints):
            entry = constraints[name]
            if entry.get("type") == "step_relation" or entry.get("enabled", True) is False:
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
        # 单次包构造在 build() 后调用，此时步列已声明；不得再次
        # _declare_instance_statics，否则步列会被重复追加，破坏时序长度。
        # 兼容此公开辅助函数的独立调用（未执行 build 的新构造器）。
        if not self._step_columns:
            buses = self._compute_buses()
            bus_flows = self._bus_flow_index(buses)
            self._declare_global_symbols()
            for inst in sorted(self.devices):
                bound = self._bind_instance_symbols(inst, bus_flows.get(inst, frozenset()))
                self._expand_instance(inst, bound)
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

    @staticmethod
    def _decl_affine(acc: _AffineAcc) -> dict[str, object]:
        """仿射留存 → 声明节点（求解列引用，消费者不得拆解）。"""
        return {"constant": acc.constant, "terms": dict(sorted(acc.terms.items()))}

    @staticmethod
    def _decl_var(name: str) -> dict[str, object]:
        """单求解列引用声明节点。"""
        return {"constant": 0.0, "terms": {name: 1.0}}

    @staticmethod
    def _decl_const(value: float) -> dict[str, object]:
        """已知常量声明节点（与求值路径统一，适配器同口径恢复）。"""
        return {"constant": value, "terms": {}}

    @staticmethod
    def _decl_negated(acc: _AffineAcc) -> dict[str, object]:
        """同源仿射的相反数声明（费用类沿既有封存口径记正数）。"""
        negated = _AffineAcc()
        negated.add_scaled(acc, -1.0)
        return _ProjectBuilder._decl_affine(negated)

    def financial_facts(self) -> Mapping[str, object]:
        """同次解结构化财务事实声明（格式页映射表为唯一正文）。

        须在 ``build`` 之后调用：全部金额/现金仿射与目标同一表达式、
        同一循环内并行留存，同源；业务身份/期间为字面量，求解列引用
        只出现在声明节点内，消费者不得拆解。无适用规则的数组为空
        （真实不适用）；适用零金额显式保留；无税则仍声明完整现金流。
        同一输入恒得同一声明。
        """
        years = self.evaluation.evaluation_years
        annual_operation: list[dict[str, object]] = []
        for year_no in sorted(self._fact_annual):
            fields = self._fact_annual[year_no]
            annual_operation.append(
                {
                    "financial_year": year_no,
                    "energy_net": self._decl_affine(fields["energy"]),
                    "variable_om": self._decl_negated(fields["variable"]),
                    "fixed_om": self._decl_negated(fields["fixed"]),
                    "maintenance": self._decl_affine(fields["maintenance"]),
                    "demand_charge": self._decl_affine(fields["demand"]),
                    "net": self._decl_affine(fields["net"]),
                }
            )
        vat: list[dict[str, object]] = []
        for row in self._fact_vat:
            vat.append(
                {
                    "pool": row["pool"],
                    "month": row["month"],
                    "paid": self._decl_var(row["paid"]),
                    "refund_claimed": self._decl_const(row["claim"]),
                    "refund_arrived": self._decl_const(row["arrival"]),
                    # 期末可结转留抵 = 当月 carry 列 - 当月确认退款，与
                    # 下月 previous 同源（claim 为已知常量；无退款时恒
                    # 等于 carry 列本身）。
                    "carry": {
                        "constant": -row["claim"],
                        "terms": {row["carry"]: 1.0},
                    },
                }
            )
        income: list[dict[str, object]] = []
        for row in self._fact_income:
            used = _AffineAcc()
            for name in row["used"]:
                used.add_term(name, 1.0)
            expired = _AffineAcc()
            for _, _, acc in row["expired"]:
                expired.add_scaled(acc, 1.0)
            tax = _AffineAcc()
            # 税额为正（现金贡献为其相反数，已在 _year_cash 并行累积）。
            tax.add_term(row["profit"], row["rate"])
            for name in row["used"]:
                tax.add_term(name, -row["rate"])
            gen = _AffineAcc()
            gen.add_term(row["gen"], 1.0)
            income.append(
                {
                    "rule": row["rule"],
                    "year": row["year"],
                    "taxable_base": self._decl_affine(row["base"]),
                    "loss_used": self._decl_affine(used),
                    "loss_generated": self._decl_affine(gen),
                    "loss_expired": self._decl_affine(expired),
                    "tax": self._decl_affine(tax),
                }
            )
        demand_tax = self._demand_tax_factors()
        monthly_demand: list[dict[str, object]] = []
        for row in self._demand_rows:
            peak_kw = _AffineAcc()
            peak_kw.add_scaled(row["peak"], 0.001)
            paid = _AffineAcc()
            paid.add_scaled(
                row["base_charge"],
                demand_tax.get(row["charge_id"], (1.0, 0.0))[0],
            )
            monthly_demand.append({
                "billing_id": row["billing_id"],
                "charge_id": row["charge_id"],
                "calendar_year": row["calendar_year"],
                "calendar_month": row["calendar_month"],
                "financial_year": row["financial_year"],
                "peak_power": self._decl_affine(peak_kw),
                "peak_unit": "kW",
                "rate": row["rate"],
                "charge": self._decl_affine(paid),
            })
        out: dict[str, object] = {
            "currency": self.finance.currency,
            "monthly_demand": monthly_demand,
            "construction_total": self._decl_affine(self._fact_construction),
            "annual_fixed_om": self._decl_affine(self._fact_fixed_om),
            "payments_applied": [
                {"t": t, "ratio": ratio, "amount": self._decl_affine(acc)}
                for t, ratio, acc in self._fact_payments
            ],
            "net_cashflows": [
                {"id": row["id"], "t": row["t"], "direction": row["direction"],
                 "amount": self._decl_affine(row["amount"])}
                for row in self._fact_net_cashflows
            ],
            "annual_operation": annual_operation,
            "tax_cash": {
                "vat": vat,
                "refunds": [
                    {
                        "pool": row["pool"],
                        "period": row["period"],
                        "arrival_period": row["arrival_period"],
                        "amount": self._decl_const(row["amount"]),
                    }
                    for row in self._fact_refunds
                ],
                "income": income,
                "depreciation": [
                    {
                        "rule": row["rule"],
                        "base_from": row["base_from"],
                        "year": row["year"],
                        "amount": self._decl_affine(row["amount"]),
                    }
                    for row in self._fact_depr
                ],
                "loss_balances": [
                    {
                        "rule": row["rule"],
                        "vintage_id": row["vintage_id"],
                        "year": row["year"],
                        "last_usable_year": row["last"],
                        "amount": self._decl_affine(row["rem"]),
                    }
                    for row in self._fact_balances
                ],
            },
            # 完整税后现金流按 t=0..E 顺序排列（时点为下标位置，不另存年键）。
            "annual_net_flows": [
                self._decl_affine(self._fact_cash.get(t) or _AffineAcc())
                for t in range(years + 1)
            ],
        }
        return MappingProxyType(out)

    def build(self) -> tuple[LinearModel, Mapping[str, str], str]:
        if self.stop is not None:
            reason = read_stop_reason(self.stop)
            if reason is not None:
                raise ComputationUnavailableError("项目模型生成已停止", reason=reason)
        buses = self._compute_buses()
        bus_flows = self._bus_flow_index(buses)
        self._declare_global_symbols()
        bound_instances: dict[str, dict[str, Any]] = {}
        for inst in sorted(self.devices):
            self._checkpoint()
            bound_instances[inst] = self._bind_instance_symbols(
                inst, bus_flows.get(inst, frozenset())
            )
        # Global barrier: every instance symbol and technical AST now exists.
        for inst in sorted(bound_instances):
            self._expand_instance(inst, bound_instances[inst])
        for inst in sorted(bound_instances):
            self._build_relations(inst, bound_instances[inst])
        self._build_bus_rows(buses)
        self._build_step_relations()
        self._build_monthly_demand()
        self._checkpoint()
        self._build_planning_rows()
        # VAT/所得税在现金目标构造之前使用同一逐笔维护表达式。
        self._retain_maintenance()
        self._vat_cash = self._build_vat()
        self._income_cash = self._build_income()
        self._retain_net_cashflows()
        self._checkpoint()
        objective, objective_unit = self._build_objective()
        self._checkpoint()
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


__all__ = [
    "build_project_model",
    "build_project_model_with_facts",
    "build_project_model_package",
    "finance_component_declarations",
]
