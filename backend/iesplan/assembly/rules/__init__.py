"""装配检查规则包:阶段 B(连接合法性)/ D(整体可解性)
+ 财务完整性(finance_binding)/能源计费(tariff_bindings)/规划(planning)。

阶段 B/D 规则统一签名 RuleFn(spec, ctx) -> list[Diagnostic];阶段 D
额外返回母线汇总。财务/计费/规划规则直接消费装配文档映射与密封有效
快照(签名见各模块)。模型引用与输入完备由校验器阶段 2/3 直接判定，
不再经独立阶段 C 模块。
"""

from iesplan.assembly.rules.connection import run_phase_b
from iesplan.assembly.rules.finance import check_finance_binding, resolve_finance_snapshot
from iesplan.assembly.rules.planning import check_planning
from iesplan.assembly.rules.solvability import build_buses, run_phase_d
from iesplan.assembly.rules.tariff import check_tariff_bindings

__all__ = [
    "run_phase_b",
    "run_phase_d",
    "build_buses",
    "check_finance_binding",
    "resolve_finance_snapshot",
    "check_tariff_bindings",
    "check_planning",
]
