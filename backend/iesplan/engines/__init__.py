"""计算引擎包(02 计算引擎数学模型规格 §3-§8 的实现)。

模块:
- solver.py   求解器适配层:封装 scipy.optimize.milp / linprog(HiGHS),返回 SolveResult。
- balance.py  三母线(电/热/冷)能量平衡矩阵构建器,返回 scipy LinearConstraint。
- devices.py  设备出力参数化(PV/热泵/锅炉/制冷机/电池 SOC 确定性模拟)。
- eval_run.py 任意方案评价引擎(固定容量运行 MILP,evaluate_plan)。
- planning.py 规划引擎(简版):容量离散网格枚举 + IRR 硬约束过滤。
"""

from iesplan.engines.balance import (
    build_cold_balance,
    build_electric_balance,
    build_grid_capacity,
    build_heat_balance,
    build_pump_equation,
)
from iesplan.engines.devices import (
    boiler_output,
    chiller_output,
    gas_volume_m3,
    heat_pump_cop,
    pv_output,
    simulate_battery,
)
from iesplan.engines.eval_run import EvalResult, evaluate_plan
from iesplan.engines.planning import PlanCandidate, PlanningResult, run_planning
from iesplan.engines.solver import SolveResult, solve_lp, solve_milp

__all__ = [
    "SolveResult",
    "solve_milp",
    "solve_lp",
    "build_electric_balance",
    "build_heat_balance",
    "build_cold_balance",
    "build_grid_capacity",
    "build_pump_equation",
    "pv_output",
    "heat_pump_cop",
    "boiler_output",
    "chiller_output",
    "gas_volume_m3",
    "simulate_battery",
    "evaluate_plan",
    "EvalResult",
    "run_planning",
    "PlanCandidate",
    "PlanningResult",
]
