"""装配检查规则包:阶段 B(连接合法性)/ C(模型可解性)/ D(整体可解性)。

各阶段规则统一签名 RuleFn(spec, ctx) -> list[Diagnostic];阶段 D 额外返回母线汇总。
"""

from iesplan.assembly.rules.connection import run_phase_b
from iesplan.assembly.rules.completeness import run_phase_c
from iesplan.assembly.rules.solvability import build_buses, run_phase_d

__all__ = ["run_phase_b", "run_phase_c", "run_phase_d", "build_buses"]
