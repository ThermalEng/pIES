"""GeneratorProvider 独立注册(0.8 切片 A)。

只声明公开 descriptor 形状与原子注册表，不实现任何真实 generator。
provider 选择与装配只归 bootstrap 组合根；API/Worker 不得自行发现或
动态加载 provider。无可用 provider 时统一抛
``ComputationUnavailableError``。
"""

from iesplan.computation.generators.descriptor import GeneratorDescriptor
from iesplan.computation.generators.inputs import GenerationInputs
from iesplan.computation.generators.linear import (
    LinearConstraint,
    LinearExpression,
    LinearModel,
    LinearVariable,
    LoweredRelation,
    LoweringUnsupportedError,
    lower_expression,
    lower_planning_constraint,
    lower_planning_expression,
    lower_relation,
)
from iesplan.computation.generators.registry import (
    GeneratorRegistry,
    build_registry,
)

__all__ = [
    "GenerationInputs",
    "GeneratorDescriptor",
    "GeneratorRegistry",
    "LoweredRelation",
    "LoweringUnsupportedError",
    "LinearConstraint",
    "LinearExpression",
    "LinearModel",
    "LinearVariable",
    "build_registry",
    "lower_expression",
    "lower_planning_constraint",
    "lower_planning_expression",
    "lower_relation",
]
