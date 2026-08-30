"""逐时运行 → 财务数据（见 modules/finance.md 与 ARCHITECTURE_CONSTITUTION.md §4.6）。

输入为计算模块的逐时 flows(费用列 cost_buy/cost_gas/revenue_sell,单位 CNY/步,
SI 已归一)+ 年度 KPI(buy_cost/gas_cost/sell_revenue/total_op_cost/annual_*_kwh)
+ capex + baseline_cost + FinanceParams;输出 FinancialResult(evidence `financial` 块)。

口径(修复「财务基于年度聚合」):
- 年运营费 = Σ(cost_buy+cost_gas) - Σ(revenue_sell)(逐时费用列求和,权威);
  与 kpi.total_op_cost 交叉校验,偏差 >1% 记入 detail.diagnostics;
- 节能收益 = baseline_cost - annual_op_cost(增量语义);
- 现金流 = 税后项目现金流(metrics.build_project_cashflows);
- LCOE = Σ(年成本贴现) / Σ(年发电量贴现),年发电量取 kpi 年度发电量(kWh);
  单位换算经 core.units.convert(SI 能量 J → kWh 时)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import numpy as np

from iesplan.core.units import convert as units_convert
from iesplan.finance.metrics import IRRStatus, build_project_cashflows, cashflow_irr, npv
from iesplan.finance.params import FinanceParams

# ---------------------------------------------------------------------------
# 键约定(与 engines/eval_run.py flows/kpi 键一致)
# ---------------------------------------------------------------------------

# 逐时费用列(CNY/步,SI 已归一)
FLOW_COST_BUY = "cost_buy"
FLOW_COST_GAS = "cost_gas"
FLOW_REVENUE_SELL = "revenue_sell"
FLOW_COST_TOTAL = "cost_total_step"

# 年度 KPI(业务单位:金额 CNY,能量 kWh)
KPI_TOTAL_OP_COST = "total_op_cost"
KPI_BUY_COST = "buy_cost"
KPI_GAS_COST = "gas_cost"
KPI_SELL_REVENUE = "sell_revenue"

# 年度发电量键(业务单位 kWh,自上而下优先)
KPI_ENERGY_KWH_KEYS = (
    "annual_pv_kwh",
    "annual_gen_kwh",
    "annual_energy_kwh",
    "annual_generation_kwh",
    "total_energy_kwh",
)
# 年度发电量键(SI 单位 J,经 core.units 换算到 kWh)
KPI_ENERGY_SI_KEYS = ("annual_pv_gen_j", "annual_gen_j", "annual_energy_j")

# 交叉校验阈值:逐时费用列与 kpi 年度值偏差超过该比例给出诊断
CROSS_CHECK_TOL = Decimal("0.01")


# ---------------------------------------------------------------------------
# 结果类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FinancialResult:
    """财务计算结果(evidence `financial` 块,03 §7.4)。

    属性:
        irr: 税后项目 IRR(无解时为 None)。
        irr_status: IRR 求根分类(IRRStatus)。
        npv: 贴现后的项目净现值。
        payback_years: 静态回收期(年,小数);未回收为 None。
        lcoe: 平准化度电成本(CNY/kWh);年发电量缺失/非正为 None。
        capex: 初始投资额。
        baseline_cost: 基准方案年成本。
        annual_op_cost: 年运营费(逐时费用列求和)。
        annual_revenue: 年售电/售热等收益(逐时 revenue_sell 求和)。
        cashflows: 税后项目现金流序列(evidence financial.cashflows)。
        detail: 折旧/税/贴现率/交叉校验/诊断等分解。
    """

    irr: float | None
    irr_status: IRRStatus
    npv: Decimal
    payback_years: float | None
    lcoe: Decimal | None
    capex: Decimal
    baseline_cost: Decimal
    annual_op_cost: Decimal
    annual_revenue: Decimal
    cashflows: list[Decimal]
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _to_decimal(value) -> Decimal:
    """统一转 Decimal(float 经字符串转换避免二进制误差)。"""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _sum_series(value) -> float:
    """对逐时列(ndarray/list)求和;None/空返回 0.0。"""
    if value is None:
        return 0.0
    arr = np.asarray(value, dtype=np.float64)
    if arr.size == 0:
        return 0.0
    return float(np.sum(arr))


def _annual_op_cost_from_flows(flows: dict | None) -> Decimal | None:
    """逐时费用列 → 年运营费(口径:Σ(cost_buy+cost_gas) - Σ(revenue_sell))。

    费用列缺失(flows 为空/无费用键)时返回 None,由调用方回退 kpi。
    """
    if not flows:
        return None
    has_buy = flows.get(FLOW_COST_BUY) is not None
    has_gas = flows.get(FLOW_COST_GAS) is not None
    has_rev = flows.get(FLOW_REVENUE_SELL) is not None
    if has_buy or has_gas or has_rev:
        buy = _sum_series(flows.get(FLOW_COST_BUY)) if has_buy else 0.0
        gas = _sum_series(flows.get(FLOW_COST_GAS)) if has_gas else 0.0
        rev = _sum_series(flows.get(FLOW_REVENUE_SELL)) if has_rev else 0.0
        return _to_decimal(buy + gas - rev)
    if flows.get(FLOW_COST_TOTAL) is not None:
        return _to_decimal(_sum_series(flows.get(FLOW_COST_TOTAL)))
    return None


def _annual_revenue_from_flows(flows: dict | None) -> Decimal | None:
    """逐时 revenue_sell 求和(年收益);缺失返回 None。"""
    if not flows or flows.get(FLOW_REVENUE_SELL) is None:
        return None
    return _to_decimal(_sum_series(flows.get(FLOW_REVENUE_SELL)))


def _kpi_decimal(kpi: dict, key: str) -> Decimal | None:
    """取 kpi 数值键并转 Decimal;缺失/None 返回 None。"""
    value = (kpi or {}).get(key)
    if value is None:
        return None
    return _to_decimal(value)


def _annual_energy_kwh(kpi: dict) -> Decimal | None:
    """年度发电量(kWh)提取:kWh 键直取,SI(J)键经 core.units.convert 换算。"""
    if not kpi:
        return None
    for key in KPI_ENERGY_KWH_KEYS:
        value = kpi.get(key)
        if value is not None and float(value) != 0:
            return _to_decimal(value)
    for key in KPI_ENERGY_SI_KEYS:
        value = kpi.get(key)
        if value is not None and float(value) != 0:
            kwh = units_convert(float(value), "J", "kWh")  # 单位换算走单位标准化模块
            return _to_decimal(kwh)
    # 键存在但全为 0(如无 PV 项目)→ 返回 0,由 compute_lcoe 判 None
    if any(kpi.get(k) is not None for k in KPI_ENERGY_KWH_KEYS + KPI_ENERGY_SI_KEYS):
        return Decimal("0")
    return None


# ---------------------------------------------------------------------------
# 逐时 → 财务
# ---------------------------------------------------------------------------


def compute_lcoe(total_lifecycle_cost: Decimal | float, total_energy_kwh: Decimal | float) -> Decimal | None:
    """平准化度电成本 LCOE = Σ(年成本贴现) / Σ(年发电量贴现);发电量 <= 0 → None。"""
    cost = _to_decimal(total_lifecycle_cost)
    energy = _to_decimal(total_energy_kwh)
    if energy <= 0:
        return None
    return cost / energy


def compute_payback(cashflows: list[Decimal] | tuple[Decimal, ...]) -> float | None:
    """静态回收期:累计现金流首次转正的年数(小数,线性插值);未转正 → None。

    - 首期(含 CF[0])累计已非负 → 0.0(投资期即回收);
    - 空序列 → None。
    """
    if not cashflows:
        return None
    cum = Decimal("0")
    prev: Decimal | None = None
    for i, c in enumerate(cashflows):
        c = _to_decimal(c)
        cum += c
        if i == 0:
            if cum >= 0:
                return 0.0
            prev = cum
            continue
        if prev < 0 <= cum:
            delta = cum - prev
            frac = float(-prev / delta) if delta > 0 else 0.0
            return float(i - 1) + frac
        prev = cum
    return None


def compute_financials(
    kpi: dict,
    flows: dict[str, np.ndarray] | None,
    capex: Decimal | float,
    baseline_cost: Decimal | float,
    params: FinanceParams | None = None,
) -> FinancialResult:
    """逐时运行 → 财务数据(03 §7.2/§7.3)。

    参数:
        kpi: evaluate_plan 的 kpi(年度聚合:total_op_cost/buy_cost/gas_cost/
            sell_revenue/annual_*_kwh,业务单位)。
        flows: 逐时流费用列(cost_buy/cost_gas/revenue_sell,CNY/步,SI 已归一)。
        capex: 初始投资额(CNY)。
        baseline_cost: 基准方案年成本(CNY/a,节能收益的参照)。
        params: 财务参数;None 时取 FinanceParams() 默认。

    返回:
        FinancialResult;年运营费以逐时费用列求和为权威口径,与 kpi 交叉校验
        (偏差 >1% 记 detail.diagnostics,不阻断)。
    """
    if capex is None or baseline_cost is None:
        raise ValueError("capex 与 baseline_cost 不能为空")
    params = params if params is not None else FinanceParams()
    diagnostics: list[str] = []

    # --- 年运营费:逐时费用列权威,缺失回退 kpi ---
    flows_op = _annual_op_cost_from_flows(flows)
    kpi_op = _kpi_decimal(kpi, KPI_TOTAL_OP_COST)
    if flows_op is None and kpi_op is None:
        raise ValueError("flows 费用列与 kpi.total_op_cost 均缺失,无法计算年运营费")
    if flows_op is not None:
        annual_op_cost = flows_op
    else:
        annual_op_cost = kpi_op
        diagnostics.append("flows 无费用列,年运营费回退 kpi.total_op_cost")

    # --- 年收益:逐时 revenue_sell 权威,缺失回退 kpi ---
    flows_rev = _annual_revenue_from_flows(flows)
    kpi_rev = _kpi_decimal(kpi, KPI_SELL_REVENUE)
    annual_revenue = flows_rev if flows_rev is not None else kpi_rev
    if annual_revenue is None:
        annual_revenue = Decimal("0")

    # --- 交叉校验(偏差 >1% 给诊断,不阻断) ---
    cross_check: dict[str, str] = {
        "flows_annual_op_cost": str(flows_op) if flows_op is not None else "missing"
    }
    if flows_op is not None and kpi_op is not None:
        deviation = abs(flows_op - kpi_op) / max(abs(flows_op), abs(kpi_op), Decimal("0.01"))
        cross_check["kpi_total_op_cost"] = str(kpi_op)
        cross_check["deviation"] = str(deviation)
        if deviation > CROSS_CHECK_TOL:
            diagnostics.append(
                f"逐时费用列与 kpi.total_op_cost 偏差 {float(deviation) * 100:.2f}%>1%,"
                f"以逐时费用列为准(flows={flows_op}, kpi={kpi_op})"
            )

    # --- 税后项目现金流(增量语义:saving = baseline - annual_op) ---
    cashflows = build_project_cashflows(
        capex,
        0,
        annual_op_cost,
        0,
        params.tax_rate,
        params.depreciation_years,
        project_years=params.project_years,
        baseline_annual_cost=baseline_cost,
    )
    irr, irr_status, irr_message = cashflow_irr(cashflows)
    npv_value = npv(params.discount_rate, cashflows)
    payback = compute_payback(cashflows)

    # --- LCOE:Σ(年成本贴现) / Σ(年发电量贴现),税前口径 ---
    energy_kwh = _annual_energy_kwh(kpi)
    lcoe: Decimal | None = None
    lcoe_note = ""
    if energy_kwh is not None and energy_kwh > 0:
        base = Decimal("1") + params.discount_rate
        denom = Decimal("1")
        cost_discounted = _to_decimal(capex)  # t=0 投资
        energy_discounted = Decimal("0")
        for _ in range(1, params.project_years + 1):
            denom *= base
            cost_discounted += annual_op_cost / denom
            energy_discounted += energy_kwh / denom
        lcoe = compute_lcoe(cost_discounted, energy_discounted)
    elif energy_kwh is None:
        lcoe_note = "kpi 无年度发电量键,无法计算 LCOE"
    else:
        lcoe_note = "年度发电量为 0,无法计算 LCOE"

    detail: dict = {
        "discount_rate": str(params.discount_rate),
        "tax_rate": str(params.tax_rate),
        "depreciation_years": params.depreciation_years,
        "project_years": params.project_years,
        "currency": params.currency,
        "irr_floor": str(params.irr_floor),
        "annual_saving": str(_to_decimal(baseline_cost) - annual_op_cost),
        "annual_energy_kwh": str(energy_kwh) if energy_kwh is not None else "missing",
        "irr_message": irr_message,
        "lcoe_note": lcoe_note,
        "cross_check": cross_check,
        "diagnostics": diagnostics,
    }

    return FinancialResult(
        irr=irr,
        irr_status=irr_status,
        npv=npv_value,
        payback_years=payback,
        lcoe=lcoe,
        capex=_to_decimal(capex),
        baseline_cost=_to_decimal(baseline_cost),
        annual_op_cost=annual_op_cost,
        annual_revenue=annual_revenue,
        cashflows=cashflows,
        detail=detail,
    )
