"""三母线能量平衡矩阵构建器(02 §3)。

约定:
- 所有功率单位为 W(内部 SI,C0NTRACT §3);负荷数组为 (n,) float64。
- 变量布局:每个变量占据 n 个连续列(变量块),第 i 步的列号 = 块起始索引 + i。
- 每个构建器返回 scipy.optimize.LinearConstraint(A, lb, ub),A 形状 (n, n_vars);
  lb == ub 即等式约束(电/热/冷平衡、泵耗电方程均为等式,02 §3.2-§3.5)。
- 电平衡(E-BAL):购电+光伏+电池放电 = 电负荷+售电+电池充电+热泵耗电+制冷机耗电+泵耗电
  (默认不允许削减;启用削减时左侧加入 p_shed_e 项并转为不等式,见 build_electric_balance)。
- 热平衡(H-BAL):锅炉产热+热泵供热 = (1+λ_h)·(热负荷−热削减),λ_h 默认 0.05(02 §3.3)。
- 冷平衡(C-BAL):制冷机产冷+热泵供冷 = (1+λ_c)·(冷负荷−冷削减),λ_c 默认 0.08(02 §3.4)。
- 泵耗电(PUMP):p_pump = c_ph·Q_sup,h + c_pc·Q_sup,c,c_ph/c_pc 默认 20 W_e/kW_th
  = 0.02 W/W(02 §3.5)。
- 并网约束(GRID-CAP,02 §3.6):0 ≤ 购电 ≤ C_imp,0 ≤ 售电 ≤ C_exp;
  禁止反送电时 p_grid_sell = 0 等式约束(绝不使用惩罚项软化,02 §3.6)。
- 削减(02 §3.7):默认不允许削减(平衡为等式、负荷全额满足);允许削减时削减量
  变量 ≥ 0,平衡方程右侧需求相应减少,惩罚项由调用方加入目标。
"""

from __future__ import annotations

import numpy as np
from scipy import sparse
from scipy.optimize import LinearConstraint

#: 默认热输配损耗率 λ_h(02 §3.3/附录 B)
DEFAULT_LAMBDA_H = 0.05
#: 默认冷输配损耗率 λ_c(02 §3.4/附录 B)
DEFAULT_LAMBDA_C = 0.08
#: 默认泵耗电系数 W_e/W_th(20 W/kW = 0.02,02 §3.5/附录 B)
DEFAULT_C_PH = 0.02
DEFAULT_C_PC = 0.02


def _check_series(arr: np.ndarray, n: int, name: str) -> np.ndarray:
    """校验逐时参数序列:长度 n、形状 (n,)、值有限。"""
    a = np.asarray(arr, dtype=np.float64)
    if a.ndim != 1 or a.size != n:
        raise ValueError(f"{name} 长度应为 {n},实际 {a.size}(形状 {a.shape})")
    if not np.all(np.isfinite(a)):
        raise ValueError(f"{name} 包含 NaN/Inf")
    return a


def _eq_constraint(
    n: int,
    n_vars: int,
    coefs: dict[int, float],
    rhs: np.ndarray,
) -> LinearConstraint:
    """按变量块列号构造 n 行等式约束:A[row=τ, col] 只在本块 τ 步的列非零。

    使用 scipy.sparse 稀疏矩阵(n_vars 与 n 同阶时,稠密矩阵为 O(n·n_vars)
    内存,稀疏矩阵仅 O(n·|coefs|),保证全年 8760 步模型可构建)。
    """
    rows: list[np.ndarray] = []
    cols: list[np.ndarray] = []
    vals: list[float] = []
    tau = np.arange(n, dtype=np.int64)
    for col, coef in coefs.items():
        rows.append(tau)
        cols.append(col + tau)
        vals.append(coef)
    if not vals:
        A = sparse.csr_matrix((n, n_vars), dtype=np.float64)
    else:
        r = np.concatenate(rows)
        c = np.concatenate(cols)
        v = np.repeat(np.asarray(vals, dtype=np.float64), n)
        A = sparse.csr_matrix((v, (r, c)), shape=(n, n_vars))
    return LinearConstraint(A, lb=rhs, ub=rhs)


def build_electric_balance(
    n: int,
    n_vars: int,
    *,
    e_load: np.ndarray,
    p_grid_buy: int,
    p_grid_sell: int,
    p_pv: int,
    p_bat_ch: int,
    p_bat_dis: int,
    p_hp_elec: int,
    p_chiller_elec: int,
    p_pump: int,
    p_shed_e: int | None = None,
) -> LinearConstraint:
    """电平衡(E-BAL,02 §3.2)。

    供给(购电 + 光伏 + 电池放电)= 需求(电负荷 + 售电 + 电池充电 + 热泵耗电
    + 制冷机耗电 + 泵耗电);启用削减(p_shed_e 非 None)时需求侧减去削减量。
    所有功率单位 W,每变量占据 n 个连续列(块起始索引由参数给出)。
    """
    load = _check_series(e_load, n, "e_load")
    rhs = load.copy()
    coefs: dict[int, float] = {
        p_grid_buy: 1.0,
        p_pv: 1.0,
        p_bat_dis: 1.0,
        p_grid_sell: -1.0,
        p_bat_ch: -1.0,
        p_hp_elec: -1.0,
        p_chiller_elec: -1.0,
        p_pump: -1.0,
    }
    if p_shed_e is not None:
        coefs[p_shed_e] = 1.0  # 削减减少需求:供给 = 负荷 - 削减 + ...
    return _eq_constraint(n, n_vars, coefs, rhs)


def build_heat_balance(
    n: int,
    n_vars: int,
    *,
    h_load: np.ndarray,
    p_boiler: int,
    p_hp_heat: int,
    p_shed_h: int | None = None,
    lambda_h: float = DEFAULT_LAMBDA_H,
) -> LinearConstraint:
    """热平衡(H-BAL,02 §3.3):锅炉产热 + 热泵供热 = (1+λ_h)·(热负荷 − 热削减)。"""
    if lambda_h < 0:
        raise ValueError(f"lambda_h 必须 >= 0,实际 {lambda_h}")
    load = _check_series(h_load, n, "h_load")
    rhs = (1.0 + lambda_h) * load
    coefs: dict[int, float] = {p_boiler: 1.0, p_hp_heat: 1.0}
    if p_shed_h is not None:
        coefs[p_shed_h] = 1.0 + lambda_h
    return _eq_constraint(n, n_vars, coefs, rhs)


def build_cold_balance(
    n: int,
    n_vars: int,
    *,
    c_load: np.ndarray,
    p_chiller: int,
    p_hp_cool: int,
    p_shed_c: int | None = None,
    lambda_c: float = DEFAULT_LAMBDA_C,
) -> LinearConstraint:
    """冷平衡(C-BAL,02 §3.4):制冷机产冷 + 热泵供冷 = (1+λ_c)·(冷负荷 − 冷削减)。"""
    if lambda_c < 0:
        raise ValueError(f"lambda_c 必须 >= 0,实际 {lambda_c}")
    load = _check_series(c_load, n, "c_load")
    rhs = (1.0 + lambda_c) * load
    coefs: dict[int, float] = {p_chiller: 1.0, p_hp_cool: 1.0}
    if p_shed_c is not None:
        coefs[p_shed_c] = 1.0 + lambda_c
    return _eq_constraint(n, n_vars, coefs, rhs)


def build_pump_equation(
    n: int,
    n_vars: int,
    *,
    p_pump: int,
    p_boiler: int,
    p_hp_heat: int,
    p_chiller: int,
    p_hp_cool: int,
    c_ph: float = DEFAULT_C_PH,
    c_pc: float = DEFAULT_C_PC,
) -> LinearConstraint:
    """泵耗电方程(PUMP,02 §3.5):p_pump = c_ph·(锅炉+热泵供热) + c_pc·(制冷机+热泵供冷)。

    c_ph/c_pc 默认 0.02 W/W(= 20 W_e/kW_th)。
    """
    if c_ph < 0 or c_pc < 0:
        raise ValueError("c_ph/c_pc 必须 >= 0")
    coefs: dict[int, float] = {
        p_pump: 1.0,
        p_boiler: -c_ph,
        p_hp_heat: -c_ph,
        p_chiller: -c_pc,
        p_hp_cool: -c_pc,
    }
    return _eq_constraint(n, n_vars, coefs, np.zeros(n, dtype=np.float64))


def build_grid_capacity(
    n: int,
    n_vars: int,
    *,
    p_grid_buy: int,
    p_grid_sell: int,
    c_import: float,
    c_export: float = 0.0,
    forbid_reverse_feed: bool = True,
) -> list[LinearConstraint]:
    """并网容量约束(GRID-CAP,02 §3.6)。

    - 0 ≤ 购电 ≤ C_imp;0 ≤ 售电 ≤ C_exp(单位 W)。
    - 禁止反送电(forbid_reverse_feed=True,默认):p_grid_sell = 0 等式约束,
      模型层面直接置零,绝不使用惩罚项软化(02 §3.6)。
    返回约束列表(容量为 0 时上界约束自动退化为 0,由调用方一并纳入 Bounds)。
    """
    if c_import < 0 or c_export < 0:
        raise ValueError("c_import/c_export 必须 >= 0")
    cons: list[LinearConstraint] = []
    tau = np.arange(n, dtype=np.int64)
    A = sparse.csr_matrix(
        (np.ones(n), (tau, p_grid_buy + tau)), shape=(n, n_vars),
    )
    cons.append(LinearConstraint(A, lb=-np.inf, ub=float(c_import)))
    A2 = sparse.csr_matrix(
        (np.ones(n), (tau, p_grid_sell + tau)), shape=(n, n_vars),
    )
    cons.append(LinearConstraint(A2, lb=-np.inf, ub=float(c_export)))
    if forbid_reverse_feed:
        # p_grid_sell(τ) = 0,逐时等式
        A3 = sparse.csr_matrix(
            (np.ones(n), (tau, p_grid_sell + tau)), shape=(n, n_vars),
        )
        cons.append(LinearConstraint(A3, lb=np.zeros(n), ub=np.zeros(n)))
    return cons
