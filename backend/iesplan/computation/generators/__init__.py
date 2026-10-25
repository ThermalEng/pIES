"""GeneratorProvider 独立注册(0.8 切片 A)。

公开 descriptor 形状、首个真实 provider(``highs_milp``)与原子注册表。
provider 选择与装配只归 bootstrap 组合根；API/Worker 不得自行发现或
动态加载 provider。无可用 provider 时统一抛
``ComputationUnavailableError``。
"""

from iesplan.computation.generators.descriptor import GeneratorDescriptor
from iesplan.computation.generators.highs_milp import (
    HIGHS_MILP_PROVIDER,
    HighsMilpProvider,
)
from iesplan.computation.generators.linear import (
    LinearConstraint,
    LinearExpression,
    LinearModel,
    LinearVariable,
    LoweredRelation,
    LoweringUnsupportedError,
    lower_expression,
    lower_mutex,
    lower_planning_constraint,
    lower_planning_expression,
    lower_relation,
)
from iesplan.computation.generators.mps import write_mps
from iesplan.computation.generators.project import (
    build_project_model,
    build_project_model_with_facts,
)
from iesplan.computation.generators.registry import (
    GeneratorRegistry,
    build_registry,
)

__all__ = [
    "HIGHS_MILP_PROVIDER",
    "GeneratorDescriptor",
    "GeneratorRegistry",
    "HighsMilpProvider",
    "LoweredRelation",
    "LoweringUnsupportedError",
    "LinearConstraint",
    "LinearExpression",
    "LinearModel",
    "LinearVariable",
    "build_project_model",
    "build_project_model_with_facts",
    "build_registry",
    "lower_expression",
    "lower_mutex",
    "lower_planning_constraint",
    "lower_planning_expression",
    "lower_relation",
    "write_mps",
]
