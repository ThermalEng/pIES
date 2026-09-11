"""建模公开契约门面。

旧的 DeviceSpec、Python 机理函数和进程内命令注册表已删除；当前只保留
2.0 声明式数学贡献契约。计算生成与执行将在 0.8.0 通过 GeneratorProvider
和 Solver Bundle 接入。
"""

from iesplan.modeling.contract2 import (
    CONTRACT_SCHEMA_ID, CONTRACT_SCHEMA_VERSION, EQUATION_AST_ID, EQUATION_AST_VERSION,
    BinaryNode, DeviceMathContribution, InterfaceFlow, MathContributionResult, MathRelation,
    MathVariable, NumberNode, RefNode, RelationAst, ResultMappingEntry, TimeIndexedRef,
    UnaryNode, ast_to_dict, build_math_contribution, canonical_bytes, contribution_to_dict,
)

__all__ = [
    "CONTRACT_SCHEMA_ID", "CONTRACT_SCHEMA_VERSION", "EQUATION_AST_ID", "EQUATION_AST_VERSION",
    "TimeIndexedRef", "NumberNode", "RefNode", "UnaryNode", "BinaryNode", "RelationAst",
    "MathVariable", "InterfaceFlow", "MathRelation", "ResultMappingEntry", "DeviceMathContribution",
    "MathContributionResult", "ast_to_dict", "build_math_contribution", "canonical_bytes",
    "contribution_to_dict",
]
