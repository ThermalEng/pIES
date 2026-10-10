"""项目评价时间轴现金流(0.12-T2): 同一次全期解 → 年度净额 → CF[0..E]。

纯财务算法: 输入全部为已冻结事实的 plain 数据(快照评价确认块的
时间/比例/维护、Effective 的价格与成本条目、适配输出的业务值、物化
序列值); 不读 DB/快照/对象, 不解释装配, 不做公开输入边界校验(提交
边界唯一权威)。形状错误(长度/缺件/非有限)按财务失败语义大声失败
(ValueError), 不返回默认现金流或伪指标。

时间口径(``manual/developer-guide/zh-CN/modules/finance.md``
“项目建设安排与全周期现金流”):

- ``K = construction_period``, ``N = operation_years``, ``E = K + N``;
- 运行仿真只覆盖投产后连续 N 年, 年度边界 step 下标由基线运行起始年、
  分辨率与 N 推导(此处取调用方传入的已推导边界);
- 求解得到建设总额 C 后, 按冻结比例 ``-ratio_t × C`` 计入 ``CF[t]``;
- 第 j 个完整运营年净额计入 ``CF[K+j]``; 年净额 = 能源净收益 −
  可变 O&M − 固定 O&M − 当年维护/换新;
- 财务序列总长 ``E + 1``; ``evaluation_years`` 回显调用方传入的冻结 E,
  不由序列长度推导。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from iesplan.core.contracts import horizon_year_boundary_steps
from iesplan.core.units import energy_to_joules, from_si, power_to_watts, to_si

#: 计费方向记账符号(财务契约公开规则: purchase 为 +, sale 为 −)。
_DIRECTION_SIGN: dict[str, float] = {"purchase": 1.0, "sale": -1.0}


def _decimal(value: Decimal | float | int) -> Decimal:
    """数值 → Decimal(float 经字符串转换, 不引入二进制误差)。"""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    if not d.is_finite():
        raise ValueError(f"财务时间轴输入含非有限值: {value!r}")
    return d


def _finite_float(value: object, what: str) -> float:
    """业务浮点输入断言有限(物化/求解值缺件或非有限即实际错误)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} 须为有限数值, 实际 {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{what} 须为有限数值, 实际 {value!r}")
    return result


@dataclass(frozen=True, slots=True)
class TariffYearFacts:
    """单个计费绑定的逐点事实(调用方按冻结声明与求解/物化值如实组装)。"""

    binding_id: str
    direction: str  # purchase | sale(公开记账符号)
    aggregation: str  # integrate_positive | sum_positive
    energy_values: tuple[float, ...]  # 逐 step 能量(业务单位)
    energy_unit: str  # 功率单位(integrate)或能量单位(sum)
    price_values: tuple[float, ...]  # 逐 step 价格(业务单位)
    price_unit: str  # 金额/能量单位


@dataclass(frozen=True, slots=True)
class VariableOmFacts:
    """单个可变 O&M driver 的事实(接口时序或 property 存量)。"""

    inst: str
    driver: str
    kind: str  # interface | property
    values: tuple[float, ...] = ()  # interface: 逐 step 数量(业务单位)
    unit: str = ""
    aggregation: str = "integrate_positive"  # interface 专用
    quantity: float = 0.0  # property: 存量(业务单位, 全期恒定)
    unit_cost: Decimal = Decimal("0")  # 金额/数量单位(冻结 Decimal)
    unit_cost_unit: str = ""


@dataclass(frozen=True, slots=True)
class AnnualBreakdown:
    """单个完整运营年的净额分量(金额 Decimal, 业务币种)。"""

    energy_net: Decimal = Decimal("0")
    variable_om: Decimal = Decimal("0")
    fixed_om: Decimal = Decimal("0")
    maintenance: Decimal = Decimal("0")
    net: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class TimelineInputs:
    """时间轴组装输入(全部已冻结事实, 无默认值)。"""

    construction_period: int
    operation_years: int
    evaluation_years: int
    payments: tuple[tuple[int, Decimal], ...] = ()  # (t, ratio)
    annual: tuple[AnnualBreakdown, ...] = ()
    maintenance: tuple[tuple[int, Decimal], ...] = ()  # (year, amount)


def annual_boundaries(resolution: str, start_year: int, operation_years: int) -> tuple[tuple[int, int], ...]:
    """运营年 step 区间(公历逐年推导, 与求解域同一来源)。"""
    if not isinstance(operation_years, int) or operation_years < 1:
        raise ValueError(f"operation_years 须为正整数, 实际 {operation_years!r}")
    bounds = horizon_year_boundary_steps(resolution, start_year, operation_years)
    return tuple((bounds[i], bounds[i + 1]) for i in range(operation_years))


def _step_joules(
    values: Sequence[float], unit: str, aggregation: str, step_seconds: float, what: str
) -> list[float]:
    """逐 step 能量 → 焦耳(聚合语义为公开契约, 非生成器私有公式)。"""
    if aggregation == "integrate_positive":
        return [power_to_watts(_finite_float(v, what), unit) * step_seconds for v in values]
    if aggregation == "sum_positive":
        return [energy_to_joules(_finite_float(v, what), unit) for v in values]
    raise ValueError(f"{what} 聚合未知: {aggregation!r}(仅 integrate_positive/sum_positive)")


def annual_energy_nets(
    *,
    step_seconds: float,
    boundaries: Sequence[tuple[int, int]],
    tariffs: Sequence[TariffYearFacts],
    currency: str,
) -> list[Decimal]:
    """各运营年能源净额(已带符号记账贡献之和; purchase +, sale −)。

    逐点 ``price[t]×e[t]``(财务契约公开语义), 按年区间求和; 单位经公共
    单位换算走 SI, 结果回业务币种。
    """
    if not boundaries:
        raise ValueError("运营年边界为空, 无法归集")
    total_steps = boundaries[-1][1]
    nets = [0.0 for _ in boundaries]
    for facts in tariffs:
        sign = _DIRECTION_SIGN.get(facts.direction)
        if sign is None:
            raise ValueError(f"计费方向未知: {facts.binding_id}={facts.direction!r}")
        if len(facts.energy_values) < total_steps or len(facts.price_values) < total_steps:
            raise ValueError(
                f"计费 {facts.binding_id} 序列长度不足(能量 {len(facts.energy_values)},"
                f"价格 {len(facts.price_values)}, 需要 {total_steps})"
            )
        energy_j = _step_joules(
            facts.energy_values,
            facts.energy_unit,
            facts.aggregation,
            step_seconds,
            f"计费 {facts.binding_id} 能量",
        )
        price_si = [
            to_si(_finite_float(p, f"计费 {facts.binding_id} 价格"), facts.price_unit)
            for p in facts.price_values
        ]
        for year, (start, end) in enumerate(boundaries):
            year_sum = 0.0
            for step in range(start, end):
                year_sum += sign * price_si[step] * energy_j[step]
            nets[year] += year_sum
    return [_decimal(from_si(total, currency)) for total in nets]


def annual_variable_om(
    *,
    step_seconds: float,
    boundaries: Sequence[tuple[int, int]],
    drivers: Sequence[VariableOmFacts],
    currency: str,
) -> list[Decimal]:
    """各运营年可变 O&M(成本为正)。

    interface driver 按该年真实运行结果归集; property driver 为全期恒定
    存量, 其窗口合计按年均分(容量不跨年变化, 均分即逐年真实值, 非近似)。
    """
    if not boundaries:
        raise ValueError("运营年边界为空, 无法归集")
    years = len(boundaries)
    total_steps = boundaries[-1][1]
    nets = [0.0 for _ in boundaries]
    for facts in drivers:
        rate_si = to_si(float(facts.unit_cost), facts.unit_cost_unit)
        if facts.kind == "interface":
            if len(facts.values) < total_steps:
                raise ValueError(
                    f"可变 O&M {facts.inst}.{facts.driver} 序列长度不足"
                    f"({len(facts.values)}, 需要 {total_steps})"
                )
            qty_j = _step_joules(
                facts.values,
                facts.unit,
                facts.aggregation,
                step_seconds,
                f"可变 O&M {facts.inst}.{facts.driver}",
            )
            for year, (start, end) in enumerate(boundaries):
                nets[year] += sum(rate_si * qty_j[step] for step in range(start, end))
        elif facts.kind == "property":
            qty_si = to_si(
                _finite_float(facts.quantity, f"可变 O&M {facts.inst}.{facts.driver} 存量"),
                facts.unit,
            )
            yearly_si = rate_si * qty_si / years
            for year in range(years):
                nets[year] += yearly_si
        else:
            raise ValueError(f"可变 O&M driver 种类未知: {facts.inst}.{facts.driver}={facts.kind!r}")
    return [_decimal(from_si(total, currency)) for total in nets]


def construction_total(values: Sequence[tuple[float, str]], currency: str) -> Decimal:
    """建设总额 C(已求解 upfront 分量业务值之和, 十进制精确累加)。

    调用方只传入已选定计费实例的 ``__upfront_capex`` 分量值(固定 +
    线性×已求解容量, 适配输出已按业务币种换算); 此处只求和, 不解释
    选建状态(求解与分量语义的权威在生成器/适配输出)。
    """
    total = Decimal("0")
    for value, unit in values:
        if unit != currency:
            raise ValueError(f"建设分量单位须为业务币种 {currency}, 实际 {unit!r}")
        total += _decimal(value)
    return total


def annual_fixed_om_total(values: Sequence[tuple[float, str]], currency: str) -> Decimal:
    """年固定 O&M 合计(已求解 ``__annual_fixed_om`` 年值之和)。

    年值口径(currency/年)按选定容量与明确费率求得, 每个完整运营年计
    同一金额(容量全期恒定, 非平均替代); 此处只求和。
    """
    total = Decimal("0")
    for value, unit in values:
        if unit != f"{currency}/a":
            raise ValueError(f"固定 O&M 分量单位须为 {currency}/a, 实际 {unit!r}")
        total += _decimal(value)
    return total


def combine_annual_nets(
    energy_signed: Sequence[Decimal],
    variable_om: Sequence[Decimal],
    fixed_om: Decimal,
    maintenance_by_year: Mapping[int, Decimal],
    year_offset: int,
) -> list[AnnualBreakdown]:
    """各运营年净额 = 能源净收益 − 可变 O&M − 固定 O&M − 当年维护。

    ``energy_signed`` 为已带符号记账贡献之和(purchase 为正成本向、
    sale 为负收益向); 能源净收益取其相反数。``year_offset`` 为建设期
    K(第 j 年对应财务时轴 ``K + j``); 维护按发生年扣除, 不回填建设
    投资, 不重复扣已列 O&M(调用方只传未含项)。
    """
    if not (len(energy_signed) == len(variable_om) and len(energy_signed) >= 1):
        raise ValueError(f"年度序列长度不一致(能源 {len(energy_signed)}, 可变 O&M {len(variable_om)})")
    out: list[AnnualBreakdown] = []
    for index, (signed, variable) in enumerate(zip(energy_signed, variable_om, strict=True)):
        year = year_offset + index + 1
        maintenance = maintenance_by_year.get(year, Decimal("0"))
        energy = -_decimal(signed)
        net = energy - _decimal(variable) - _decimal(fixed_om) - _decimal(maintenance)
        out.append(
            AnnualBreakdown(
                energy_net=_decimal(energy),
                variable_om=_decimal(variable),
                fixed_om=_decimal(fixed_om),
                maintenance=_decimal(maintenance),
                net=net,
            )
        )
    return out


def assemble_cashflows(inputs: TimelineInputs, construction: Decimal) -> list[Decimal]:
    """组装 ``CF[0..E]``(长 ``E + 1``; E 回显冻结值, 不由长度推导)。

    付款按冻结比例落款 ``CF[t] −= ratio_t × C``(同一 t 多笔累加; 无付
    款的建设时点为 0, 不自动均摊); 第 j 个运营年净额(已含当年维护扣
    除)计入 ``CF[K+j]``。时点越界即输入诊断错误。
    """
    years = inputs.evaluation_years
    period = inputs.construction_period
    if years != period + inputs.operation_years or len(inputs.annual) != inputs.operation_years:
        raise ValueError(
            f"时间轴事实不一致(E={years}, K={period}, N={inputs.operation_years},"
            f" 年度净额 {len(inputs.annual)} 项)"
        )
    flows = [Decimal("0") for _ in range(years + 1)]
    for t, ratio in inputs.payments:
        if not isinstance(t, int) or not 0 <= t <= period:
            raise ValueError(f"付款时点须为开工起整数年 0..{period}, 实际 {t!r}")
        flows[t] -= _decimal(ratio) * _decimal(construction)
    for index, annual in enumerate(inputs.annual):
        flows[period + index + 1] += annual.net
    if len(flows) != years + 1:
        raise ValueError("现金流长度须恰为 evaluation_years + 1")
    return flows
