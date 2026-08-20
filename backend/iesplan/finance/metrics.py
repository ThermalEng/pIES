"""财务指标模块:IRR 求根分类与税后现金流构建(自 metrics/financial.py 迁入,签名与 Decimal 语义不变)。

依据 03-module-decoupling.md §7(财务计算模块)与 02-calc-model.md §5.2/§5.3/§5.4/附录 B
(IRR 语义与线性化前提、基准方案与净收益、税后现金流 ATCF、默认参数:贴现率 8%、税率 25%、折旧年限 10)。

金额一律使用 Decimal;内部数值求根使用 numpy float64。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import Decimal
from enum import StrEnum

import numpy as np

# ---------------------------------------------------------------------------
# 常量与类型
# ---------------------------------------------------------------------------

_MONEY = Decimal  # 金额类型别名(DB NUMERIC(18,4),此处保留完整 Decimal 精度)

# 求根扫描上界:IRR 超过 1e4(即 100 万 %)已无工程意义,按 02 §5.6 取 1e4。
_RATE_MAX = 1e4
# 收敛容差:NPV 相对初始投资绝对值的比例(02 §5.6: |NPV| < 1e-6 * |ATCF0|)
_RATE_TOL = 1e-9
_NPV_REL_TOL = 1e-6
# 牛顿迭代上限
_MAX_ITER = 200


class IRRStatus(StrEnum):
    """IRR 求根结果分类(02 §5.2/§5.6 与 REQ-FIN-005)。

    - unique:      唯一正实根,标准符号型现金流
    - none:        无解——现金流符号无变化(全部同号),r >= 0 上 NPV 恒不为 0
    - multiple:    多解——符号变化 > 1 次,NPV 可能存在多个根
    - degenerate:  退化——现金流全零或常数(无时间价值差异)
    - out_of_domain: 超出定义域——存在符号变化但 r >= 0 上无正实根(投资不可回收)
    - numerical_failure: 数值失败——输入非法(空/非有限)或迭代不收敛
    """

    unique = "unique"
    none = "none"
    multiple = "multiple"
    degenerate = "degenerate"
    out_of_domain = "out_of_domain"
    numerical_failure = "numerical_failure"


# ---------------------------------------------------------------------------
# 输入处理
# ---------------------------------------------------------------------------


def _to_float_array(cashflows: Sequence[Decimal | float]) -> np.ndarray | None:
    """把 Decimal/float 序列转成 float64 数组;空或含非有限值时返回 None。"""
    if cashflows is None or len(cashflows) == 0:
        return None
    try:
        arr = np.asarray([float(c) for c in cashflows], dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if not np.all(np.isfinite(arr)):
        return None
    return arr


def _sign_changes(arr: np.ndarray, zero_tol: float) -> int:
    """统计现金流序列的符号变化次数(近零值视为 0,不计入变号)。"""
    nonzero = arr[np.abs(arr) > zero_tol]
    if len(nonzero) <= 1:
        return 0
    return int(np.sum(np.sign(nonzero[1:]) != np.sign(nonzero[:-1])))


# ---------------------------------------------------------------------------
# NPV 与求根
# ---------------------------------------------------------------------------


def npv(rate: Decimal | float, cashflows: Sequence[Decimal | float]) -> Decimal:
    """净现值 NPV = Σ cf[j] / (1+rate)^j,全程 Decimal 精确计算。

    金额按 CONTRACT §3 使用 Decimal;结果不预先舍入,由调用方按展示规则处理。
    """
    r = rate if isinstance(rate, Decimal) else Decimal(str(rate))
    total = Decimal("0")
    denom = Decimal("1")
    base = Decimal("1") + r
    for c in cashflows:
        total += (c if isinstance(c, Decimal) else Decimal(str(c))) / denom
        denom *= base
    return total


def _npv_float(rate: float, arr: np.ndarray) -> float:
    """float64 版 NPV,用于求根迭代。"""
    x = 1.0 / (1.0 + rate)
    p = 1.0
    total = 0.0
    for c in arr:
        total += c * p
        p *= x
    return total


def _npv_deriv(rate: float, arr: np.ndarray) -> float:
    """NPV 关于 r 的导数(浮点),用于牛顿迭代。"""
    x = 1.0 / (1.0 + rate)
    p = 1.0
    total = 0.0
    for j, c in enumerate(arr):
        if j > 0:
            total -= j * c * p / (1.0 + rate)
        p *= x
    return total


def _bisect_root(arr: np.ndarray, lo: float, hi: float, scale: float) -> float | None:
    """在 [lo, hi] 上对 NPV 做二分求根(NPV 在区间内单调变号)。"""
    f_lo = _npv_float(lo, arr)
    f_hi = _npv_float(hi, arr)
    if f_lo == 0.0:
        return lo
    if f_hi == 0.0:
        return hi
    if f_lo * f_hi > 0:
        return None
    tol = max(_NPV_REL_TOL * scale, 1e-14)
    for _ in range(_MAX_ITER):
        mid = 0.5 * (lo + hi)
        f_mid = _npv_float(mid, arr)
        if abs(f_mid) <= tol or abs(hi - lo) <= _RATE_TOL * max(1.0, abs(mid)):
            return mid
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)


def _newton_polish(rate: float, arr: np.ndarray) -> float | None:
    """牛顿法精化根;以步长收敛(约 1e-12 精度),不收敛时返回 None。"""
    r = rate
    for _ in range(50):
        f = _npv_float(r, arr)
        d = _npv_deriv(r, arr)
        if d == 0.0 or not math.isfinite(d):
            return None
        step = f / d
        r_new = r - step
        if not math.isfinite(r_new) or r_new <= -1.0:
            return None
        # 步长收敛判据(函数值容差受 float64 求和误差限制,步长更可靠)
        if abs(step) <= 1e-12 * max(1.0, abs(r)):
            return r_new
        r = r_new
    return None


def _scan_roots(arr: np.ndarray, scale: float) -> list[float]:
    """在 r ∈ [0, RATE_MAX] 上扫描并二分求全部实根(02 §5.6:最多 64 段)。"""
    n_seg = 128
    bounds = np.logspace(np.log10(1e-6), np.log10(_RATE_MAX), num=n_seg)
    roots: list[float] = []
    # 端点 0 处根
    if abs(_npv_float(0.0, arr)) <= _NPV_REL_TOL * scale:
        r = _newton_polish(0.0, arr)
        roots.append(0.0 if r is None else r)
    prev_r, prev_f = 0.0, _npv_float(0.0, arr)
    for r_hi in bounds:
        f_hi = _npv_float(r_hi, arr)
        if prev_f == 0.0:
            pass  # 端点根已在上面处理
        elif prev_f * f_hi < 0.0:
            mid = _bisect_root(arr, prev_r, r_hi, scale)
            if mid is not None:
                polished = _newton_polish(mid, arr)
                if polished is None:
                    roots.append(mid)
                else:
                    roots.append(polished)
        prev_r, prev_f = r_hi, f_hi
    # 去重(相邻根距离小于容差视为同一根)
    unique: list[float] = []
    for r in sorted(roots):
        if not unique or abs(r - unique[-1]) > _RATE_TOL * 1e3:
            unique.append(r)
    return unique


def cashflow_irr(
    cashflows: Sequence[Decimal | float],
) -> tuple[float | None, IRRStatus, str]:
    """计算现金流的内部收益率 IRR 并给出完整分类。

    方法:单次变号(标准符号型)时 NPV 关于 r 严格单调,走二分+牛顿精化;
    多次变号时做 128 段对数扫描 + 二分 + 牛顿精化,收集全部正实根。
    返回 (rate|None, status, message);rate 为最小正根(多次变号时按 02 §5.6
    取最小正根为保守值)。
    """
    arr = _to_float_array(cashflows)
    if arr is None:
        return None, IRRStatus.numerical_failure, "现金流为空或包含非有限值"
    scale = float(np.max(np.abs(arr)))
    if scale == 0.0:
        return None, IRRStatus.degenerate, "现金流全为零,无法求 IRR"
    # 退化:所有元素近似相等(常数现金流,NPV 恒不为 0)
    if float(np.max(arr) - np.min(arr)) <= 1e-12 * scale:
        return None, IRRStatus.degenerate, "现金流为常数,无时间价值差异"
    n_changes = _sign_changes(arr, zero_tol=1e-12 * scale)
    if n_changes == 0:
        return None, IRRStatus.none, "现金流符号无变化(全部同号),r>=0 上无根"

    try:
        if n_changes == 1:
            # 标准符号型:NPV 严格单调递减
            f0 = _npv_float(0.0, arr)
            if abs(f0) <= _NPV_REL_TOL * scale:
                r = _newton_polish(0.0, arr)
                return float(r) if r is not None else 0.0, IRRStatus.unique, "唯一根 r=0"
            if f0 < 0.0:
                return None, IRRStatus.out_of_domain, "存在符号变化但无正实根(投资不可回收)"
            hi = 1.0
            while _npv_float(hi, arr) > 0.0 and hi < _RATE_MAX:
                hi *= 2.0
            if _npv_float(hi, arr) > 0.0:
                return None, IRRStatus.out_of_domain, "扫描区间内无正实根"
            mid = _bisect_root(arr, 0.0, hi, scale)
            r = _newton_polish(mid, arr)
            if r is None:
                return None, IRRStatus.numerical_failure, "二分/牛顿迭代未收敛"
            return float(r), IRRStatus.unique, "唯一正实根"
        # 多次变号:扫描全部根
        roots = _scan_roots(arr, scale)
        if not roots:
            return None, IRRStatus.out_of_domain, "符号变化>1 但 r>=0 上无正实根"
        return (
            float(roots[0]),
            IRRStatus.multiple,
            f"检测到多次符号变化,正实根共 {len(roots)} 个,取最小正根 {roots[0]:.6f}",
        )
    except (FloatingPointError, OverflowError, ValueError) as exc:  # pragma: no cover
        return None, IRRStatus.numerical_failure, f"数值计算失败:{exc}"


# ---------------------------------------------------------------------------
# 项目现金流构建(02 §5.3 / §5.4)
# ---------------------------------------------------------------------------


def _as_decimal(value: Decimal | float | int) -> Decimal:
    """统一转 Decimal(float 经字符串转换避免二进制误差)。"""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def build_project_cashflows(
    investment: Decimal | float,
    annual_om: Decimal | float,
    annual_energy_saving: Decimal | float,
    revenue: Decimal | float,
    tax_rate: Decimal | float,
    depreciation_years: int,
    discount_rate: Decimal | float | None = None,
    project_years: int = 20,
    salvage: Decimal | float = 0,
    baseline_annual_cost: Decimal | float | None = None,
) -> list[Decimal]:
    """构建税后项目投资现金流序列(02 §5.4 口径)。

    - CF[0] = -投资(CAPEX_0)
    - CF[y] = (1 - t_c) * (NB_y - OM_y - DEP_y) + DEP_y,y = 1..project_years
      - NB_y 为相对基准的年净收益;DEP_y 为直线折旧(折旧年限内有效);
      - 负应纳税所得按公式产生负税(税收抵免),与规格一致;
      - 期末残值 salvage 计入最后一年现金流(视为税后残值回收)。
    - 增量语义:若给定 baseline_annual_cost,则 annual_energy_saving 解释为
      新方案年运行成本,节省 = 基准年成本 - 新方案年成本(只计增量部分);
      否则 annual_energy_saving 直接作为相对基准的年运行节省。
    - discount_rate 不参与现金流构造(现金流与贴现无关),保留作快照/文档记录;
      需要贴现 NPV 时用 project_npv()。
    """
    inv = _as_decimal(investment)
    om = _as_decimal(annual_om)
    revenue_d = _as_decimal(revenue)
    tax = _as_decimal(tax_rate)
    if not (0 <= tax <= 1):
        raise ValueError(f"tax_rate 必须位于 [0,1],实际 {tax}")
    if project_years < 1:
        raise ValueError(f"project_years 必须 >= 1,实际 {project_years}")
    dep_years = max(1, int(depreciation_years))

    if baseline_annual_cost is not None:
        # 增量语义:只计算新增部分
        saving = _as_decimal(baseline_annual_cost) - _as_decimal(annual_energy_saving)
    else:
        saving = _as_decimal(annual_energy_saving)
    net_benefit = saving + revenue_d

    dep = inv / Decimal(dep_years) if dep_years > 0 else Decimal("0")
    salvage_d = _as_decimal(salvage)

    flows: list[Decimal] = [-inv]
    for y in range(1, project_years + 1):
        dep_y = dep if y <= dep_years else Decimal("0")
        taxable = net_benefit - om - dep_y
        atcf = (Decimal("1") - tax) * taxable + dep_y
        if y == project_years:
            atcf += salvage_d
        flows.append(atcf)
    return flows


def project_npv(
    discount_rate: Decimal | float,
    *,
    investment: Decimal | float,
    annual_om: Decimal | float,
    annual_energy_saving: Decimal | float,
    revenue: Decimal | float = 0,
    tax_rate: Decimal | float = Decimal("0.25"),
    depreciation_years: int = 10,
    project_years: int = 20,
    salvage: Decimal | float = 0,
    baseline_annual_cost: Decimal | float | None = None,
) -> Decimal:
    """按给定贴现率计算项目税后 NPV(02 §5.2 阶段 1 代理目标口径)。"""
    flows = build_project_cashflows(
        investment,
        annual_om,
        annual_energy_saving,
        revenue,
        tax_rate,
        depreciation_years,
        discount_rate=discount_rate,
        project_years=project_years,
        salvage=salvage,
        baseline_annual_cost=baseline_annual_cost,
    )
    return npv(discount_rate, flows)


def project_irr(
    investment: Decimal | float,
    annual_om: Decimal | float,
    annual_energy_saving: Decimal | float,
    revenue: Decimal | float = 0,
    tax_rate: Decimal | float = Decimal("0.25"),
    depreciation_years: int = 10,
    project_years: int = 20,
    salvage: Decimal | float = 0,
    baseline_annual_cost: Decimal | float | None = None,
) -> tuple[float | None, IRRStatus, str]:
    """税后项目投资 IRR(02 §5.4 口径:对全部投资 CAPEX_0 求 IRR)。"""
    flows = build_project_cashflows(
        investment,
        annual_om,
        annual_energy_saving,
        revenue,
        tax_rate,
        depreciation_years,
        project_years=project_years,
        salvage=salvage,
        baseline_annual_cost=baseline_annual_cost,
    )
    return cashflow_irr(flows)


def build_equity_cashflows(
    investment: Decimal | float,
    annual_om: Decimal | float,
    annual_energy_saving: Decimal | float,
    revenue: Decimal | float,
    tax_rate: Decimal | float,
    depreciation_years: int,
    project_years: int,
    salvage: Decimal | float = 0,
    baseline_annual_cost: Decimal | float | None = None,
    loan_ratio: Decimal | float = Decimal("0.7"),
    loan_rate: Decimal | float = Decimal("0.05"),
    loan_years: int = 10,
) -> list[Decimal]:
    """构建税后资本金现金流(项目投资现金流扣除债务服务与利息税盾)。

    口径:资本金现金流 = 项目现金流 - 本金偿还 - (1 - t_c) * 利息。
    即:CF[0] = -(1 - loan_ratio) * 投资;年度现金流中利息可税前抵扣,
    税后净额 = (1 - t_c) * (NB - OM - DEP - 利息) + DEP - 本金偿还;
    贷款按等额本息(年金)偿还,还款期 loan_years。
    """
    inv = _as_decimal(investment)
    lr = _as_decimal(loan_ratio)
    if not (0 <= lr <= 1):
        raise ValueError(f"loan_ratio 必须位于 [0,1],实际 {lr}")
    debt = inv * lr
    loan_r = _as_decimal(loan_rate)
    n_loan = max(1, int(loan_years))

    # 等额本息:每期还款 A = P * r * (1+r)^n / ((1+r)^n - 1)
    if debt == 0:
        annuity = Decimal("0")
    elif loan_r == 0:
        annuity = debt / Decimal(n_loan)
    else:
        g = (Decimal("1") + loan_r) ** n_loan
        annuity = debt * loan_r * g / (g - Decimal("1"))

    project_flows = build_project_cashflows(
        investment,
        annual_om,
        annual_energy_saving,
        revenue,
        tax_rate,
        depreciation_years,
        project_years=project_years,
        salvage=salvage,
        baseline_annual_cost=baseline_annual_cost,
    )
    tax = _as_decimal(tax_rate)
    flows: list[Decimal] = [project_flows[0] + debt]  # 只出资本金
    balance = debt
    for y in range(1, project_years + 1):
        if y <= n_loan and debt > 0:
            interest = balance * loan_r
            principal = annuity - interest
        else:
            interest = Decimal("0")
            principal = Decimal("0")
        if principal > balance:  # 末年结清尾差
            principal = balance
        balance -= principal
        # 项目税后现金流(含税盾)再扣除:利息税后净额 + 本金
        base = project_flows[y]
        flows.append(base - (Decimal("1") - tax) * interest - principal)
    return flows


def equity_irr(
    investment: Decimal | float,
    annual_om: Decimal | float,
    annual_energy_saving: Decimal | float,
    revenue: Decimal | float = 0,
    tax_rate: Decimal | float = Decimal("0.25"),
    depreciation_years: int = 10,
    project_years: int = 20,
    salvage: Decimal | float = 0,
    baseline_annual_cost: Decimal | float | None = None,
    loan_ratio: Decimal | float = Decimal("0.7"),
    loan_rate: Decimal | float = Decimal("0.05"),
    loan_years: int = 10,
) -> tuple[float | None, IRRStatus, str]:
    """税后资本金 IRR:对资本金现金流求 IRR(与项目投资 IRR 分离口径)。"""
    flows = build_equity_cashflows(
        investment,
        annual_om,
        annual_energy_saving,
        revenue,
        tax_rate,
        depreciation_years,
        project_years,
        salvage=salvage,
        baseline_annual_cost=baseline_annual_cost,
        loan_ratio=loan_ratio,
        loan_rate=loan_rate,
        loan_years=loan_years,
    )
    return cashflow_irr(flows)
