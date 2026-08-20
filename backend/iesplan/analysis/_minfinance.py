"""财务计算最小可用实现(analysis 模块内置依赖,接口对齐 03 §7.2)。

背景: 计算分析模块(analysis,4 层)按 03 §8.2 依赖财务计算模块(finance,2 层)的
`FinanceParams` / `FinancialResult` / `compute_financials` 接口;finance 包属里程碑
M5 交付。本文件按 03 §7.2 文档签名先行实现最小可用版本(实施规则第 4 条:依赖未落地时
按文档接口自行实现,不等待他人)。待 finance 包落地后,`wrapper.py` 的导入点整体切换
到 `iesplan.finance.*` 即可,本文件保留一个版本周期后退化(与 metrics 转发兼容策略
一致,03 §14.4)。

数值口径: 现金流/NPV/IRR 复用既有 `iesplan.metrics.financial`(Decimal 语义,
02 §5.2/§5.4);新增 LCOE 与静态回收期实现(03 §7.2)。逐时费用列 → 财务口径
(03 §7.3): 年运营费 = Σ(cost_buy + cost_gas − revenue_sell),与 kpi 交叉校验。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

import numpy as np

from iesplan.metrics.financial import (
    IRRStatus,
    build_project_cashflows as _m_build_project_cashflows,
    cashflow_irr,
    equity_irr,
    npv,
    project_irr,
    project_npv,
)

__all__ = [
    "FinancialResult",
    "FinanceParams",
    "IRRStatus",
    "build_project_cashflows",
    "cashflow_irr",
    "compute_financials",
    "compute_lcoe",
    "compute_payback",
    "equity_irr",
    "financial_params_from_config",
    "npv",
    "project_irr",
    "project_npv",
]


def _as_decimal(value: Decimal | float | int | str) -> Decimal:
    """统一转 Decimal(float 经字符串转换避免二进制误差)。"""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


# ---------------------------------------------------------------------------
# 财务参数(03 §7.2;默认值与 prices.yaml finance 节一致,M2 后改经 devices.pricing)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FinanceParams:
    """财务计算参数(03 §7.2)。

    discount_rate: 贴现率(默认 0.08);tax_rate: 所得税率(默认 0.25);
    depreciation_years: 直线折旧年限(默认 10);project_years: 项目期(默认 20);
    currency: 币种(默认 CNY);irr_floor: IRR 达标阈值(默认 0.08)。
    """

    discount_rate: Decimal = Decimal("0.08")
    tax_rate: Decimal = Decimal("0.25")
    depreciation_years: int = 10
    project_years: int = 20
    currency: str = "CNY"
    irr_floor: Decimal = Decimal("0.08")


def financial_params_from_config(calc_config: dict | None) -> FinanceParams:
    """calc_config → FinanceParams(03 §7.2)。

    来源: calc_config['params'][economic_*](discount_rate/tax_rate/project_years/
    depreciation_years/currency)+ calc_config['irr_floor'];缺失字段用默认值。
    """
    cfg = (calc_config or {}).get("params") or {}
    irr_floor = (calc_config or {}).get("irr_floor")

    def _d(key: str, default: Decimal) -> Decimal:
        raw = cfg.get(key)
        if raw is None:
            return default
        try:
            return _as_decimal(raw)
        except (TypeError, ValueError):
            return default

    def _i(key: str, default: int) -> int:
        raw = cfg.get(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    floor = _d("irr_floor", Decimal("0.08"))
    if irr_floor is not None:  # calc_config 顶层 irr_floor(03 §8.2 路径示例)
        try:
            floor = _as_decimal(irr_floor)
        except (TypeError, ValueError):
            pass
    return FinanceParams(
        discount_rate=_d("discount_rate", Decimal("0.08")),
        tax_rate=_d("tax_rate", Decimal("0.25")),
        depreciation_years=_i("depreciation_years", 10),
        project_years=_i("project_years", 20),
        currency=str(cfg.get("currency") or "CNY"),
        irr_floor=floor,
    )


# ---------------------------------------------------------------------------
# 现金流构建(03 §7.2 签名顺序:capex 在前;实现复用 metrics.financial)
# ---------------------------------------------------------------------------


def build_project_cashflows(
    capex: Decimal | float,
    annual_om: Decimal | float,
    annual_energy_saving: Decimal | float,
    revenue: Decimal | float,
    tax_rate: Decimal | float,
    depreciation_years: int,
    project_years: int = 20,
    discount_rate: Decimal | float = Decimal("0.08"),
) -> list[Decimal]:
    """税后项目现金流序列(03 §7.2 签名;口径见 metrics.financial.build_project_cashflows)。

    - CF[0] = -capex;
    - CF[y] = (1 - t) × (年净收益 − 年运营费 − 折旧) + 折旧,y = 1..project_years。
    discount_rate 不参与现金流构造(仅快照/文档记录),贴现 NPV 用 project_npv。
    """
    return _m_build_project_cashflows(
        capex,
        annual_om,
        annual_energy_saving,
        revenue,
        tax_rate,
        depreciation_years,
        discount_rate=discount_rate,
        project_years=project_years,
    )


# ---------------------------------------------------------------------------
# LCOE / 静态回收期(03 §7.2 新增)
# ---------------------------------------------------------------------------


def compute_lcoe(total_lifecycle_cost: Decimal | float, total_energy_kwh: Decimal | float) -> Decimal | None:
    """平准化度电成本 LCOE = 贴现生命周期成本 / 贴现年发电量(03 §7.2)。

    total_energy_kwh <= 0 → 返回 None(无发电量,LCOE 未定义)。
    """
    energy = _as_decimal(total_energy_kwh)
    if energy <= 0:
        return None
    return _as_decimal(total_lifecycle_cost) / energy


def compute_payback(cashflows: Sequence[Decimal | float]) -> float | None:
    """静态回收期:累计现金流首次转正的年数(跨年线性插值;03 §7.2)。

    例: [-1000, 300, 300, 300, 300] → 3 + 100/300 = 3.333...;未转正返回 None。
    """
    if not cashflows:
        return None
    cum = 0.0
    prev_cum = 0.0
    for year, c in enumerate(cashflows):
        cum += float(c)
        if cum >= 0.0:
            if year == 0:
                return 0.0
            if prev_cum >= 0.0:  # 首年即回本(防御)
                return float(year - 1)
            frac = -prev_cum / (cum - prev_cum)
            return float(year - 1) + frac
        prev_cum = cum
    return None


def _discounted_sum(annual: Decimal, rate: Decimal, years: int) -> Decimal:
    """Σ annual/(1+r)^y,y=1..years(贴现年金)。"""
    total = Decimal("0")
    denom = Decimal("1")
    base = Decimal("1") + rate
    for _ in range(years):
        denom *= base
        total += annual / denom
    return total


# ---------------------------------------------------------------------------
# 逐时运行 → 财务数据(03 §7.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FinancialResult:
    """财务计算结果(03 §7.2,即 evidence `financial` 块)。

    irr: IRR 值(无解/退化时为 None);irr_status: IRRStatus 细分;
    npv: 税后项目 NPV(贴现);payback_years: 静态回收期;lcoe: 平准化度电成本;
    capex: 新增设备投资;baseline_cost: 基准年成本;annual_op_cost: 年运营费
    (逐时费用列口径);annual_revenue: 年售电收入;cashflows: 税后项目现金流;
    detail: 分解说明(费用来源/交叉校验/折旧税等)。
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


def compute_financials(
    kpi: dict,
    flows: dict[str, np.ndarray] | None,
    capex: Decimal | float,
    baseline_cost: Decimal | float | None,
    params: FinanceParams | None = None,
) -> FinancialResult:
    """逐时运行结果 → 财务数据(03 §7.2/§7.3)。

    口径:
      - 年运营费 = Σ逐时费用列(cost_buy + cost_gas − revenue_sell,CNY/步);缺列时
        回退 kpi['total_op_cost'];
      - 节能收益 = baseline_cost − 年运营费(baseline_cost 缺失 → 0,detail 记说明);
      - 税后项目现金流 project_years 年(增量口径:annual_om 视为 0,运营费已净入收益);
      - LCOE = (capex + 贴现年运营费) / 年发电量(kpi['annual_pv_kwh'],缺失 → None);
      - 静态回收期 compute_payback。
    交叉校验: 逐时费用列汇总与 kpi['total_op_cost'] 偏差 > 1% 记入 detail(03 §7.3)。
    """
    p = params if params is not None else FinanceParams()
    flows = flows or {}

    # 年运营费:优先逐时费用列(03 §7.3 修复"财务基于年度聚合")
    # 注: flows 值为 ndarray, 不可用 `or []` 判空(真值歧义 ValueError), 用 is not None
    hourly_op: Decimal | None = None
    try:
        cost_buy = np.asarray(flows.get("cost_buy") if flows.get("cost_buy") is not None else [],
                              dtype=np.float64)
        cost_gas = np.asarray(flows.get("cost_gas") if flows.get("cost_gas") is not None else [],
                              dtype=np.float64)
        revenue_sell = np.asarray(flows.get("revenue_sell") if flows.get("revenue_sell") is not None else [],
                                  dtype=np.float64)
        if cost_buy.size or cost_gas.size or revenue_sell.size:
            hourly_op = _as_decimal(float(np.sum(cost_buy) + np.sum(cost_gas) - np.sum(revenue_sell)))
    except (TypeError, ValueError):
        hourly_op = None

    kpi_op_raw = kpi.get("total_op_cost") if isinstance(kpi, dict) else None
    kpi_op = _as_decimal(kpi_op_raw) if kpi_op_raw is not None else None
    detail: dict = {}
    if hourly_op is None:
        annual_op = kpi_op if kpi_op is not None else Decimal("0")
        detail["op_cost_source"] = "kpi" if kpi_op is not None else "none"
    else:
        annual_op = hourly_op
        detail["op_cost_source"] = "hourly"
    if kpi_op is not None and hourly_op is not None and kpi_op != 0:
        deviation = abs(hourly_op - kpi_op) / abs(kpi_op) * 100
        detail["kpi_crosscheck"] = {
            "kpi_op_cost": float(kpi_op),
            "hourly_op_cost": float(hourly_op),
            "deviation_pct": float(deviation),
        }

    capex_d = _as_decimal(capex) if capex is not None else Decimal("0")
    baseline_d = _as_decimal(baseline_cost) if baseline_cost is not None else None
    if baseline_d is None:
        saving = Decimal("0")
        detail["baseline_note"] = "baseline_cost 缺失,节能收益按 0 计"
    else:
        saving = baseline_d - annual_op

    # 年售电收入(逐时 revenue_sell 或 kpi)
    revenue = Decimal("0")
    try:
        rev_arr = np.asarray(flows.get("revenue_sell") if flows.get("revenue_sell") is not None else [],
                             dtype=np.float64)
        if rev_arr.size:
            revenue = _as_decimal(float(np.sum(rev_arr)))
    except (TypeError, ValueError):
        revenue = Decimal("0")
    if revenue == 0 and kpi.get("sell_revenue") is not None:
        revenue = _as_decimal(kpi["sell_revenue"])

    cashflows = build_project_cashflows(
        capex_d,
        Decimal("0"),  # 运营费已净入节能收益(增量口径)
        saving,
        revenue,
        p.tax_rate,
        p.depreciation_years,
        project_years=p.project_years,
        discount_rate=p.discount_rate,
    )
    irr, irr_status, irr_message = cashflow_irr(cashflows)
    npv_val = project_npv(
        p.discount_rate,
        investment=capex_d,
        annual_om=Decimal("0"),
        annual_energy_saving=saving,
        revenue=revenue,
        tax_rate=p.tax_rate,
        depreciation_years=p.depreciation_years,
        project_years=p.project_years,
    )

    # LCOE = 贴现生命周期成本 / 贴现发电量(03 §7.2;kpi['annual_pv_kwh'] 缺失 → None)
    lcoe: Decimal | None = None
    gen_raw = kpi.get("annual_pv_kwh") if isinstance(kpi, dict) else None
    if gen_raw is not None:
        lifecycle = capex_d + _discounted_sum(annual_op, p.discount_rate, p.project_years)
        lcoe = compute_lcoe(lifecycle, _as_decimal(gen_raw))

    detail["irr_message"] = irr_message
    return FinancialResult(
        irr=irr,
        irr_status=irr_status,
        npv=npv_val,
        payback_years=compute_payback(cashflows),
        lcoe=lcoe,
        capex=capex_d,
        baseline_cost=baseline_d if baseline_d is not None else Decimal("0"),
        annual_op_cost=annual_op,
        annual_revenue=revenue,
        cashflows=cashflows,
        detail=detail,
    )
