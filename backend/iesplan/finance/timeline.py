"""项目评价时间轴现金流(0.12-T2): 同一次全期解 → 年度净额 → CF[0..E]。

纯财务算法: 输入全部为已冻结事实的 plain 数据(快照评价确认块的
时间/比例/维护、Effective 的价格与成本条目、适配输出的业务值、物化
序列值); 不读 DB/快照/对象, 不解释装配, 不做公开输入边界校验(提交
边界与装配边界唯一权威, 适配器是求解输出进入结果模型的权威校验
入口)。内部交接信任类型化数据, 残缺即自然错误可见, 不加 fallback。

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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from iesplan.core.contracts import horizon_year_boundary_steps
from iesplan.core.units import energy_to_joules, from_si, power_to_watts, to_si

#: 计费方向记账符号(财务契约公开规则: purchase 为 +, sale 为 −;
#: 方向由 Effective 边界保证, 此处直接取键)。
_DIRECTION_SIGN: dict[str, float] = {"purchase": 1.0, "sale": -1.0}


def _decimal(value: Decimal | float | int) -> Decimal:
    """数值 → Decimal(float 经字符串转换, 不引入二进制误差)。"""
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass(frozen=True, slots=True)
class TariffYearFacts:
    """单个计费绑定的逐点事实(调用方按冻结声明与求解/物化值如实组装)。"""

    binding_id: str
    direction: str  # purchase | sale(公开记账符号)
    aggregation: str  # integrate_positive | sum_positive(装配边界保证)
    energy_values: tuple[float, ...]  # 逐 step 能量(业务单位)
    energy_unit: str  # 功率单位(integrate)或能量单位(sum)
    price_values: tuple[float, ...]  # 逐 step 价格(业务单位)
    price_unit: str  # 金额/能量单位


@dataclass(frozen=True, slots=True)
class InterfaceDriverFacts:
    """单个接口型可变 O&M driver 的逐点事实(按年真实运行结果归集)。"""

    inst: str
    driver: str
    values: tuple[float, ...]  # 逐 step 数量(业务单位)
    unit: str
    aggregation: str  # integrate_positive | sum_positive(装配边界保证)
    unit_cost: Decimal  # 金额/数量单位(冻结 Decimal)
    unit_cost_unit: str


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


def annual_boundaries(resolution: str, start_year: int, operation_years: int) -> tuple[tuple[int, int], ...]:
    """运营年 step 区间(公历逐年推导, 与求解域同一来源)。"""
    bounds = horizon_year_boundary_steps(resolution, start_year, operation_years)
    return tuple((bounds[i], bounds[i + 1]) for i in range(operation_years))


def _step_joules(values: Sequence[float], unit: str, aggregation: str, step_seconds: float) -> list[float]:
    """逐 step 能量 → 焦耳(聚合语义为公开契约, 非生成器私有公式)。"""
    if aggregation == "integrate_positive":
        return [power_to_watts(float(v), unit) * step_seconds for v in values]
    return [energy_to_joules(float(v), unit) for v in values]


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
    nets = [0.0 for _ in boundaries]
    for facts in tariffs:
        sign = _DIRECTION_SIGN[facts.direction]
        energy_j = _step_joules(facts.energy_values, facts.energy_unit, facts.aggregation, step_seconds)
        price_si = [to_si(float(p), facts.price_unit) for p in facts.price_values]
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
    interface_drivers: Sequence[InterfaceDriverFacts],
    currency: str,
) -> list[Decimal]:
    """各运营年可变 O&M(成本为正)。

    只归集接口型(按该年真实运行结果); 标量存量组合在装配公开输入
    边界已阻断(``period_variable_om_scalar_unsupported``), 此处只有
    接口型可达, 不做二次核对、不均摊。
    """
    nets = [0.0 for _ in boundaries]
    for facts in interface_drivers:
        rate_si = to_si(float(facts.unit_cost), facts.unit_cost_unit)
        qty_j = _step_joules(facts.values, facts.unit, facts.aggregation, step_seconds)
        for year, (start, end) in enumerate(boundaries):
            nets[year] += sum(rate_si * qty_j[step] for step in range(start, end))
    return [_decimal(from_si(total, currency)) for total in nets]


def construction_total(values: Sequence[float]) -> Decimal:
    """建设总额 C(已求解 upfront 分量业务值之和, 十进制精确累加)。

    调用方只传入已选定计费实例的 ``__upfront_capex`` 分量值(固定 +
    线性×已求解容量, 适配输出已按业务币种换算); 此处只求和, 不解释
    选建状态(求解与分量语义的权威在生成器/适配输出)。
    """
    total = Decimal("0")
    for value in values:
        total += _decimal(value)
    return total


def annual_fixed_om_total(values: Sequence[float]) -> Decimal:
    """年固定 O&M 合计(已求解 ``__annual_fixed_om`` 年值之和)。

    年值口径按选定容量与明确费率求得, 每个完整运营年计同一金额(容量
    全期恒定, 非平均替代); 此处只求和。
    """
    total = Decimal("0")
    for value in values:
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
    K(第 j 年对应财务时轴 ``K + j``); 维护按发生年扣除(同年多项累加,
    不限制年份唯一), 不回填建设投资, 不重复扣已列 O&M。
    """
    out: list[AnnualBreakdown] = []
    for index in range(len(energy_signed)):
        signed, variable = energy_signed[index], variable_om[index]
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
    除)计入 ``CF[K+j]``。
    """
    years = inputs.evaluation_years
    period = inputs.construction_period
    flows = [Decimal("0") for _ in range(years + 1)]
    for t, ratio in inputs.payments:
        flows[t] -= _decimal(ratio) * _decimal(construction)
    for index, annual in enumerate(inputs.annual):
        flows[period + index + 1] += annual.net
    return flows
