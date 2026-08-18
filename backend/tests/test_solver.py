"""求解器封装单元测试:LP/MILP 解析解验证、状态码映射、gap 计算(02 §9/§11.4)。

纯计算测试,不依赖 DB;全部用手算解析解断言。
"""

import numpy as np
import pytest
from scipy.optimize import Bounds, LinearConstraint

from iesplan.engines.solver import SolveResult, solve_lp, solve_milp


class TestSolveLp:
    """LP 解析解验证(02 §7.2 LP 松弛)。"""

    def test_lp_analytic_solution(self):
        # min 2x + 3y  s.t. x + y >= 2, x,y >= 0  → 最优 x=2, y=0, 目标 4
        con = LinearConstraint(np.array([[1.0, 1.0]]), lb=2.0, ub=np.inf)
        res = solve_lp([2.0, 3.0], Bounds(0, None), [con])
        assert res.status == "ok"
        assert res.objective == pytest.approx(4.0, abs=1e-9)
        assert res.x[0] == pytest.approx(2.0, abs=1e-9)
        assert res.x[1] == pytest.approx(0.0, abs=1e-9)
        assert res.gap == pytest.approx(0.0, abs=1e-12)  # LP 最优 gap = 0(02 §9.2)

    def test_lp_equality_constraint(self):
        # min x + y  s.t. x - y = 1, x, y >= 0  → 最优 x=1, y=0, 目标 1
        con = LinearConstraint(np.array([[1.0, -1.0]]), lb=1.0, ub=1.0)
        res = solve_lp([1.0, 1.0], Bounds(0, None), [con])
        assert res.status == "ok"
        assert res.objective == pytest.approx(1.0, abs=1e-9)
        assert res.x[0] == pytest.approx(1.0, abs=1e-9)

    def test_lp_infeasible(self):
        # x >= 1 且 x <= 0 不可行
        c1 = LinearConstraint(np.array([[1.0]]), lb=1.0, ub=np.inf)
        c2 = LinearConstraint(np.array([[1.0]]), lb=-np.inf, ub=0.0)
        res = solve_lp([1.0], Bounds(0, None), [c1, c2])
        assert res.status == "infeasible"
        assert res.objective is None
        assert res.x is None

    def test_lp_unbounded(self):
        # min -x, x >= 0 → 无界
        res = solve_lp([-1.0], Bounds(0, None))
        assert res.status == "unbounded"
        assert res.x is None

    def test_lp_input_validation(self):
        # c 长度与 bounds 数组长度不一致 → 报错
        with pytest.raises(ValueError):
            solve_lp([1.0, 2.0], Bounds([0.0, 0.0, 0.0], [1.0, 1.0, 1.0]))
        with pytest.raises(ValueError):
            solve_lp([1.0], None)


class TestSolveMilp:
    """MILP 解析解验证(02 §5 标准 MILP)。"""

    def test_milp_binary_knapsack(self):
        # min -(3x + 4y)  s.t. 2x + 3y <= 6, x,y ∈ {0,1} → x=1, y=1, 目标 -7
        con = LinearConstraint(np.array([[2.0, 3.0]]), lb=-np.inf, ub=6.0)
        res = solve_milp([-3.0, -4.0], [1, 1], Bounds(0, 1), [con])
        assert res.status == "ok"
        assert res.objective == pytest.approx(-7.0, abs=1e-9)
        assert int(round(res.x[0])) == 1
        assert int(round(res.x[1])) == 1
        assert res.gap is not None and res.gap < 1.0  # gap ≤ 0.1%(02 §9.2)

    def test_milp_continuous_integer_mix(self):
        # min x + 2y  s.t. x + y >= 3, x ∈ [0,10] 连续, y ∈ {0,1} → x=3, y=0?
        # 但 min 目标下 y=0 时 x=3: 3;y=1 时 x>=2: 2+2=4 → 最优 x=3, y=0, 目标 3
        con = LinearConstraint(np.array([[1.0, 1.0]]), lb=3.0, ub=np.inf)
        res = solve_milp([1.0, 2.0], [0, 1], Bounds(0, 10), [con])
        assert res.status == "ok"
        assert res.objective == pytest.approx(3.0, abs=1e-9)
        assert res.x[0] == pytest.approx(3.0, abs=1e-9)
        assert int(round(res.x[1])) == 0

    def test_milp_infeasible(self):
        # x ∈ {0,1}, x + 1 >= 3 不可行
        con = LinearConstraint(np.array([[1.0]]), lb=2.0, ub=np.inf)
        res = solve_milp([1.0], [1], Bounds(0, 1), [con])
        assert res.status == "infeasible"
        assert res.x is None

    def test_milp_unbounded(self):
        res = solve_milp([-1.0], [0], Bounds(0, None))
        assert res.status == "unbounded"

    def test_milp_input_validation(self):
        with pytest.raises(ValueError):
            solve_milp([1.0], [0, 1], Bounds(0, 1))  # integrality 长度 2,变量数 1
        with pytest.raises(ValueError):
            solve_milp([1.0], [2], Bounds(0, 1))  # integrality 取值非法


class TestSolveResult:
    """SolveResult 数据结构(02 §9.3 停止原因/原始信息)。"""

    def test_result_fields(self):
        con = LinearConstraint(np.array([[1.0, 1.0]]), lb=2.0, ub=np.inf)
        res = solve_lp([2.0, 3.0], Bounds(0, None), [con])
        assert isinstance(res, SolveResult)
        assert isinstance(res.x, np.ndarray)
        assert isinstance(res.raw, dict)
        assert "solver" in res.raw and "HiGHS" in str(res.raw["solver"])
        assert res.stop_reason
        assert res.feasible
