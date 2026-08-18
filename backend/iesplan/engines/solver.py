"""求解器适配层:封装 scipy.optimize.milp / linprog(HiGHS)(02 §9、§11.4)。

- solve_milp(c, integrality, bounds, constraints) / solve_lp(c, bounds, constraints):
  输入直接用 scipy.optimize.LinearConstraint / Bounds(02 §11.1 模型装配器输出)。
- 返回 SolveResult:status(ok/infeasible/unbounded/time_limit/numerical_failure)、
  objective、x(np.ndarray)、gap(可行且有界时按 02 §9.2 计算)、stop_reason、raw。
- 时间上限:timeout 秒(scipy options["time_limit"],02 §9.3 硬上限;超时返回
  incumbent,status=time_limit)。
- 容差:原始可行性 1e-7、整数容差 1e-5(HiGHS 默认已满足,见 02 §9.2),金额
  目标绝对 gap 默认 1e-3 元;MIP 相对 gap 默认 0.1%(mip_rel_gap 参数)。
- 可复现性:固定随机种子(默认 42,02 §9.4)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
from scipy import sparse
from scipy.optimize import Bounds, LinearConstraint, linprog, milp

# ---------------------------------------------------------------------------
# 状态码(02 §11.4 状态码的引擎内部映射;对外输出见 SolveResult.status)
# ---------------------------------------------------------------------------

STATUS_OK = "ok"                      # 最优(OPTIMAL)
STATUS_INFEASIBLE = "infeasible"      # 无可行解(NO_FEASIBLE_FOUND)
STATUS_UNBOUNDED = "unbounded"        # 无界
STATUS_TIME_LIMIT = "time_limit"      # 时间上限,返回 incumbent(TIME_LIMIT_WITH_INCUMBENT)
STATUS_NUMERICAL_FAILURE = "numerical_failure"  # 数值失败/其他(MODEL_AUDIT_FAIL 等)

#: scipy OptimizeResult.status 与引擎状态的映射(milp/linprog 一致)
#: 0=optimal, 1=iteration/time limit, 2=infeasible, 3=unbounded, 4=other
_SCIPY_STATUS_MAP: dict[int, str] = {
    0: STATUS_OK,
    1: STATUS_TIME_LIMIT,
    2: STATUS_INFEASIBLE,
    3: STATUS_UNBOUNDED,
    4: STATUS_NUMERICAL_FAILURE,
}

#: MIP 相对 gap 默认 0.1%(02 §9.2 默认停止条件)
DEFAULT_MIP_REL_GAP = 0.001
#: 时间上限默认 600 s(02 附录 B)
DEFAULT_TIME_LIMIT = 600.0
#: 求解器随机种子(02 §9.4 固定种子 42)
DEFAULT_SEED = 42

#: 金额目标绝对 gap 默认 1e-3 元(02 §9.2)
DEFAULT_ABS_GAP = 1e-3


@dataclass(slots=True)
class SolveResult:
    """求解结果(02 §9.3 要求超时也返回 incumbent 与最优性信息,不静默丢弃)。

    属性:
        status: ok / infeasible / unbounded / time_limit / numerical_failure。
        objective: 目标函数值(最优解);不可行/无界时为 None。
        x: 解向量(np.ndarray);无可行解时为 None。
        gap: MIP gap(%)或 LP 的 0.0;不可行/无界时为 None(02 §9.2 公式)。
        stop_reason: 人类可读停止原因(中文,简短)。
        raw: 求解器原始返回(scipy OptimizeResult 的关键字段快照 dict)。
    """

    status: str
    objective: float | None
    x: np.ndarray | None
    gap: float | None
    stop_reason: str
    raw: dict = field(default_factory=dict)

    @property
    def feasible(self) -> bool:
        """是否有可行解(x 非空)。"""
        return self.x is not None


def _check_inputs(
    c: Sequence[float],
    n_vars: int,
    integrality: Sequence[int] | None = None,
) -> np.ndarray:
    """校验目标系数并返回 float64 数组;维度与变量数不符时报错。"""
    c_arr = np.asarray(c, dtype=np.float64)
    if c_arr.ndim != 1 or c_arr.size != n_vars:
        raise ValueError(f"目标系数长度 {c_arr.size} 与变量数 {n_vars} 不一致")
    if not np.all(np.isfinite(c_arr)):
        raise ValueError("目标系数包含 NaN/Inf")
    if integrality is not None:
        i_arr = np.asarray(integrality, dtype=np.int8)
        if i_arr.ndim != 1 or i_arr.size != n_vars:
            raise ValueError(f"integrality 长度 {i_arr.size} 与变量数 {n_vars} 不一致")
        if set(np.unique(i_arr)) - {0, 1}:
            raise ValueError("integrality 只能取值 0(连续)/1(整数)")
    return c_arr


def _infer_n_vars(bounds: Bounds, c_len: int) -> int:
    """从 Bounds 推断变量数:lb/ub 中任一为长度 >1 的向量时取其长度,否则取 c 长度。

    scipy Bounds(标量)会存成 size=1 的对象数组,不可作为变量数依据。
    """
    for arr in (bounds.lb, bounds.ub):
        if arr is not None and np.ndim(arr) > 0 and np.size(arr) > 1:
            return int(np.size(arr))
    return c_len


def _normalize_bounds(bounds: Bounds, n_vars: int) -> Bounds:
    """把 Bounds 归一化为显式 float64 数组边界。

    scipy Bounds 把 None 存成 object 数组(元素 None),直接转 float 会得到 NaN
    并触发 HiGHS "Model error";这里统一展开为 (n_vars,) float64 数组,
    None → ±inf。
    """
    def _expand(x: object, default: float) -> np.ndarray:
        if x is None:
            return np.full(n_vars, default, dtype=np.float64)
        arr = np.asarray(x)
        if arr.dtype == object:
            vals = np.broadcast_to(arr, (n_vars,))
            return np.array([default if v is None else float(v) for v in vals], dtype=np.float64)
        return np.broadcast_to(arr.astype(np.float64), (n_vars,)).copy()

    lb = _expand(bounds.lb, -np.inf)
    ub = _expand(bounds.ub, np.inf)
    return Bounds(lb, ub)


def _gap_from_result(res, is_lp: bool) -> float | None:
    """按 02 §9.2 从求解结果计算 gap:Gap = |UB-LB| / max(1,|UB|) × 100%。

    LP 最优时 gap=0;MILP 用 scipy 的 mip_gap(相对 gap)与 mip_dual_bound:
    计算 |mip_dual_bound - fun| / max(1,|mip_dual_bound|) × 100%。
    """
    if res.fun is None:
        return None
    if is_lp:
        return 0.0
    db = getattr(res, "mip_dual_bound", None)
    if db is None or not np.isfinite(db):
        return None
    denom = max(1.0, abs(float(db)))
    return float(abs(db - res.fun)) / denom * 100.0


def _summarize(
    res,
    is_lp: bool,
    timeout: float,
) -> tuple[str, float | None, np.ndarray | None, float | None, str]:
    """从 scipy OptimizeResult 提取 (status, objective, x, gap, stop_reason)。"""
    status = _SCIPY_STATUS_MAP.get(int(res.status), STATUS_NUMERICAL_FAILURE)
    x = None
    if res.x is not None:
        x = np.asarray(res.x, dtype=np.float64)
    if status == STATUS_TIME_LIMIT:
        msg = str(getattr(res, "message", "")).lower()
        if "time" in msg:
            reason = f"达到时间上限 {timeout:g} 秒,已返回 incumbent" if x is not None else \
                f"达到时间上限 {timeout:g} 秒,无可行解"
        else:
            reason = f"达到迭代上限,已返回 incumbent({msg})" if x is not None else \
                f"达到迭代上限,无可行解({msg})"
        return status, res.fun, x, _gap_from_result(res, is_lp), reason
    if status == STATUS_OK:
        return status, res.fun, x, _gap_from_result(res, is_lp), "求解达到最优(OPTIMAL)"
    if status == STATUS_INFEASIBLE:
        return status, None, None, None, "模型无可行解(NO_FEASIBLE_FOUND)"
    if status == STATUS_UNBOUNDED:
        return status, None, None, None, "模型无界(UNBOUNDED)"
    return status, res.fun, x, None, f"数值失败:{getattr(res, 'message', 'unknown')}"


def _milp_options(
    timeout: float,
    mip_rel_gap: float,
    extra: dict | None,
) -> dict:
    """HiGHS 选项(02 §9.3 时间上限、§9.2 gap 停止条件)。

    HiGHS 对同一输入是确定性的(02 §9.4 可复现性不依赖随机种子);
    random_state 由调用方透传 extra(当前 scipy 版本不支持时忽略)。
    """
    opts = {
        "time_limit": timeout,
        "mip_rel_gap": mip_rel_gap,
        "disp": False,
        "presolve": True,
    }
    if extra:
        opts.update(extra)
    return opts


def solve_milp(
    c: Sequence[float],
    integrality: Sequence[int],
    bounds: Bounds,
    constraints: Sequence[LinearConstraint] | None = None,
    *,
    timeout: float = DEFAULT_TIME_LIMIT,
    mip_rel_gap: float = DEFAULT_MIP_REL_GAP,
    seed: int = DEFAULT_SEED,
    options: dict | None = None,
) -> SolveResult:
    """求解混合整数线性规划(MILP,HiGHS 引擎,scipy.optimize.milp)。

    参数:
        c: 目标系数(最小化 c·x,长度 = 变量数)。
        integrality: 每个变量的整数性,0=连续,1=整数(二进制也用 1)。
        bounds: scipy.optimize.Bounds(变量上下界)。
        constraints: scipy.optimize.LinearConstraint 列表(逐时平衡/设备约束)。
        timeout: 时间硬上限秒(02 §9.3,默认 600)。
        mip_rel_gap: MIP 相对 gap 停止条件(02 §9.2,默认 0.001 = 0.1%)。
        seed: 求解器随机种子(02 §9.4,默认 42)。
        options: 透传给 scipy.optimize.milp 的额外 options。
    返回:
        SolveResult;status 为 ok/infeasible/unbounded/time_limit/numerical_failure。
    异常:
        ValueError: c/integrality 维度不一致或 bounds 缺失。
    """
    if bounds is None:
        raise ValueError("bounds 不能为空(请提供 scipy.optimize.Bounds)")
    n_vars = _infer_n_vars(bounds, len(c))
    c_arr = _check_inputs(c, n_vars, integrality)
    bnds = _normalize_bounds(bounds, n_vars)
    cons = list(constraints) if constraints else None
    opts = _milp_options(timeout, mip_rel_gap, options)
    res = milp(
        c=c_arr,
        integrality=np.asarray(integrality, dtype=np.int8),
        bounds=bnds,
        constraints=cons,
        options=opts,
    )
    status, objective, x, gap, reason = _summarize(res, is_lp=False, timeout=timeout)
    raw = {
        "solver": "HiGHS (scipy.optimize.milp)",
        "scipy_status": int(res.status),
        "message": str(getattr(res, "message", "")),
        "mip_gap": getattr(res, "mip_gap", None),
        "mip_dual_bound": getattr(res, "mip_dual_bound", None),
        "mip_node_count": getattr(res, "mip_node_count", None),
        "nit": getattr(res, "nit", None),
        "time_limit": timeout,
        "mip_rel_gap_setting": mip_rel_gap,
        "seed": seed,
    }
    return SolveResult(status=status, objective=objective, x=x, gap=gap, stop_reason=reason, raw=raw)


def _split_linear_constraints(
    constraints: Sequence[LinearConstraint],
) -> tuple[list[np.ndarray], list[float], list[np.ndarray], list[float]]:
    """把 LinearConstraint 列表拆成 linprog 的 (A_ub, b_ub, A_eq, b_eq)。

    - lb == ub(逐行)的行 → 等式 A_eq·x = b;
    - 单侧有限行 → 不等式(lb 有限: -A·x ≤ -lb;ub 有限: A·x ≤ ub);
    - 两侧均有限且不等 → 两条不等式;两侧无限 → 跳过。
    """
    A_ub: list[np.ndarray] = []
    b_ub: list[float] = []
    A_eq: list[np.ndarray] = []
    b_eq: list[float] = []
    for con in constraints:
        A = con.A
        if sparse.issparse(A):
            # 逐行保持稀疏:仅物化当前行(全年模型的 A 为 (n, n_vars) 稀疏矩阵)
            A = A.tocsr()
            lb = np.broadcast_to(np.asarray(con.lb, dtype=np.float64), (A.shape[0],))
            ub = np.broadcast_to(np.asarray(con.ub, dtype=np.float64), (A.shape[0],))
            for i in range(A.shape[0]):
                li, ui = float(lb[i]), float(ub[i])
                eq = li == ui and np.isfinite(li)
                row = A.getrow(i).toarray().ravel()
                if eq:
                    A_eq.append(row)
                    b_eq.append(li)
                    continue
                if np.isfinite(li):
                    A_ub.append(-row)
                    b_ub.append(-li)  # A·x >= li ⇔ -A·x <= -li
                if np.isfinite(ui):
                    A_ub.append(row)
                    b_ub.append(ui)
            continue
        A = np.asarray(A, dtype=np.float64)
        if A.ndim == 1:
            A = A.reshape(1, -1)
        lb = np.broadcast_to(np.asarray(con.lb, dtype=np.float64), (A.shape[0],))
        ub = np.broadcast_to(np.asarray(con.ub, dtype=np.float64), (A.shape[0],))
        for i in range(A.shape[0]):
            li, ui = float(lb[i]), float(ub[i])
            eq = li == ui and np.isfinite(li)
            if eq:
                A_eq.append(A[i])
                b_eq.append(li)
                continue
            if np.isfinite(li):
                A_ub.append(-A[i])
                b_ub.append(-li)  # A·x >= li ⇔ -A·x <= -li
            if np.isfinite(ui):
                A_ub.append(A[i])
                b_ub.append(ui)
    return A_ub, b_ub, A_eq, b_eq


def solve_lp(
    c: Sequence[float],
    bounds: Bounds,
    constraints: Sequence[LinearConstraint] | None = None,
    *,
    timeout: float = DEFAULT_TIME_LIMIT,
    options: dict | None = None,
) -> SolveResult:
    """求解线性规划(LP,HiGHS 引擎,scipy.optimize.linprog,method='highs')。

    参数与 solve_milp 相同(无 integrality 参数);LP 最优时 gap=0(02 §9.2)。
    兼容 02 §7.2 的 LP 松弛快速模式:对运行问题先解 LP,再校验二进制约束。
    """
    if bounds is None:
        raise ValueError("bounds 不能为空(请提供 scipy.optimize.Bounds)")
    n_vars = _infer_n_vars(bounds, len(c))
    c_arr = _check_inputs(c, n_vars)
    bnds = _normalize_bounds(bounds, n_vars)
    # linprog 的 bounds 参数只接受 (n,2) 数组(不接受 Bounds 对象)
    bounds_array = np.column_stack([bnds.lb, bnds.ub])
    a_ub, b_ub, a_eq, b_eq = [], [], [], []
    if constraints:
        a_ub, b_ub, a_eq, b_eq = _split_linear_constraints(constraints)
    opts = {"time_limit": timeout, "disp": False}
    if options:
        opts.update(options)
    res = linprog(
        c=c_arr, bounds=bounds_array,
        A_ub=a_ub if a_ub else None, b_ub=b_ub if b_ub else None,
        A_eq=a_eq if a_eq else None, b_eq=b_eq if b_eq else None,
        method="highs", options=opts,
    )
    status, objective, x, gap, reason = _summarize(res, is_lp=True, timeout=timeout)
    raw = {
        "solver": "HiGHS (scipy.optimize.linprog)",
        "scipy_status": int(res.status),
        "message": str(getattr(res, "message", "")),
        "nit": getattr(res, "nit", None),
        "time_limit": timeout,
    }
    return SolveResult(status=status, objective=objective, x=x, gap=gap, stop_reason=reason, raw=raw)
