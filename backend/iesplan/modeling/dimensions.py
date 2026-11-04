"""Dimensions of the public technical AST, independent of assembly/solver."""

from collections import Counter

from iesplan.core.units import UnitError, dims_of
from iesplan.modeling.contract2 import BinaryNode, NumberNode, RefNode, UnaryNode


def equation_unit_dimensions(unit):
    result = Counter(dims_of(unit))
    energy = result.pop("energy", 0)
    result["power"] += energy
    result["time"] += energy
    return {key: value for key, value in result.items() if value}


def equation_dimensions(node, units):
    """Bare SI literals take their additive/relational dimension from context."""
    if isinstance(node, NumberNode):
        return None
    if isinstance(node, RefNode):
        return equation_unit_dimensions(units[node.name])
    if isinstance(node, UnaryNode):
        return equation_dimensions(node.operand, units)
    if not isinstance(node, BinaryNode):
        raise ValueError("不支持的技术 AST 节点")
    left = equation_dimensions(node.left, units)
    right = equation_dimensions(node.right, units)
    if node.op in ("+", "-", "%"):
        if left is not None and right is not None and left != right:
            raise UnitError("加减/余数两侧量纲不同")
        return right if left is None else left
    if node.op in ("*", "/"):
        if left is None and right is None:
            return None
        result = Counter(left or {})
        for key, value in (right or {}).items():
            result[key] += value * (1 if node.op == "*" else -1)
        return {key: value for key, value in result.items() if value}
    if node.op == "^" and isinstance(node.right, NumberNode) and node.right.value.is_integer():
        if left is None:
            return None
        return {key: value * int(node.right.value) for key, value in (left or {}).items() if value}
    raise ValueError("该技术表达式的量纲无法确定")


__all__ = ["equation_dimensions", "equation_unit_dimensions"]
