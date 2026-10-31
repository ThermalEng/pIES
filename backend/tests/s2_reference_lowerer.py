"""Frozen test-only S2 modeling lowerer for independent S3 comparisons.

Copied from the pre-S3 linear.py input algorithm. Production/public lowering
must NOT import this test module: it exists only to keep an independent
historical semantics and resource comparator as the public path converges on
the S3 shared compiler. This baseline has no runtime fallback role.
"""
from __future__ import annotations

import math
from collections.abc import Mapping

from iesplan.modeling import BinaryNode, MathRelation, NumberNode, RefNode, UnaryNode
from iesplan.computation.generators.linear import (
    LinearConstraint, LinearExpression, LoweredRelation, LoweringUnsupportedError,
    _Affine, _check_number, _check_symbols, _resolve_symbol, lower_mutex,
)

def _modeling_ref(
    name: str,
    offset: int,
    symbols: Mapping[str, SymbolEntry],
    *,
    step: int,
    initial: Mapping[str, float | LinearExpression],
) -> _Affine:
    out = _Affine()
    entry = _resolve_symbol(name, symbols)
    if entry[0] == "const":
        if offset != 0:
            raise LoweringUnsupportedError(
                f"常量 {name!r} 不得带时间索引 [t{offset:+d}]"
            )
        out.add_const(_check_number(entry[1], f"常量 {name!r}"))
        return out
    if offset < -1:
        raise LoweringUnsupportedError(
            f"{name!r} 的 [t{offset:+d}] 超出首版能力（仅 t 与 t-1）"
        )
    if offset == -1 and step == 0:
        try:
            value = initial[name]
        except KeyError:
            raise LoweringUnsupportedError(
                f"状态 {name!r} 首步缺少初值"
            ) from None
        if isinstance(value, LinearExpression):
            for key, coeff in value.terms:
                out.add_term(key, coeff)
            out.add_const(value.constant)
        else:
            out.add_const(_check_number(value, f"状态 {name!r} 初值"))
        return out
    out.add_term((name, offset), 1.0)
    return out


def _lower_modeling_node(
    node: object,
    symbols: Mapping[str, SymbolEntry],
    *,
    step: int,
    initial: Mapping[str, float | LinearExpression],
    where: str,
) -> _Affine:
    if isinstance(node, NumberNode):
        out = _Affine()
        out.add_const(_check_number(node.value, f"{where}数值字面量"))
        return out
    if isinstance(node, RefNode):
        return _modeling_ref(
            node.name, node.offset, symbols, step=step, initial=initial
        )
    if isinstance(node, UnaryNode):
        inner = _lower_modeling_node(
            node.operand, symbols, step=step, initial=initial, where=where
        )
        if node.op == "-":
            inner.scale(-1.0)
            return inner
        if node.op == "+":
            return inner
        raise LoweringUnsupportedError(f"{where}不支持的一元运算 {node.op!r}")
    if isinstance(node, BinaryNode):
        left = _lower_modeling_node(
            node.left, symbols, step=step, initial=initial, where=where
        )
        right = _lower_modeling_node(
            node.right, symbols, step=step, initial=initial, where=where
        )
        if node.op in ("+", "-"):
            if node.op == "-":
                right.scale(-1.0)
            for key, coeff in right.coeffs.items():
                left.add_term(key, coeff)
            left.add_const(right.constant)
            return left.checked(where)
        if node.op == "*":
            if not left.coeffs:
                left, right = right, left
            if not right.coeffs:
                factor = right.constant
                left.scale(factor)
                return left.checked(where)
            raise LoweringUnsupportedError(f"{where}变量×变量超出首版能力")
        if node.op == "/":
            if right.coeffs:
                raise LoweringUnsupportedError(f"{where}变量作除数超出首版能力")
            if right.constant == 0:
                raise LoweringUnsupportedError(f"{where}除零")
            left.scale(1.0 / right.constant)
            return left.checked(where)
        if node.op == "^":
            if not left.coeffs and not right.coeffs:
                try:
                    result = math.pow(left.constant, right.constant)
                except (ValueError, OverflowError):
                    raise LoweringUnsupportedError(
                        f"{where}常数幂定义域/溢出"
                    ) from None
                out = _Affine()
                out.add_const(_check_number(result, f"{where}常数幂"))
                return out
            if left.coeffs and not right.coeffs and right.constant == 1:
                return left
            if left.coeffs and not right.coeffs and right.constant == 0:
                out = _Affine()
                out.add_const(1.0)
                return out
            raise LoweringUnsupportedError(f"{where}非常数幂超出首版能力")
        raise LoweringUnsupportedError(f"{where}不支持的运算 {node.op!r}")
    raise LoweringUnsupportedError(f"{where}不支持的 AST 节点 {type(node).__name__}")




def _single_var_product(
    node: object,
    symbols: Mapping[str, SymbolEntry],
    *,
    step: int,
    initial: Mapping[str, float | LinearExpression],
    where: str,
) -> tuple[tuple[str, int], float] | None:
    """判定节点是否为“常数 × 单个变量引用”（返回 (键, 系数））。"""
    lowered = _lower_modeling_node(
        node, symbols, step=step, initial=initial, where=where
    ).checked(where)
    if len(lowered.coeffs) == 1 and lowered.constant == 0:
        (key, coeff), = lowered.coeffs.items()
        return key, coeff
    return None




def _split_const_product(
    node: object,
    symbols: Mapping[str, SymbolEntry],
    *,
    step: int,
    initial: Mapping[str, float | LinearExpression],
    where: str,
) -> tuple[float, tuple[tuple[str, int], float], tuple[tuple[str, int], float]] | None:
    """剥离常数乘法脊：返回 (总因子, X 单变量项, Y 单变量项)，非单积返回 None。"""
    factor = 1.0
    current = node
    while isinstance(current, BinaryNode) and current.op == "*":
        advanced = False
        for cand_const, cand_rest in (
            (current.left, current.right),
            (current.right, current.left),
        ):
            try:
                probe = _lower_modeling_node(
                    cand_const, symbols, step=step, initial=initial, where=where,
                )
            except LoweringUnsupportedError:
                continue
            if not probe.coeffs:
                factor *= probe.constant
                current = cand_rest
                advanced = True
                break
        if not advanced:
            break
    if not (isinstance(current, BinaryNode) and current.op == "*"):
        return None
    try:
        left_single = _single_var_product(
            current.left, symbols, step=step, initial=initial, where=where,
        )
        right_single = _single_var_product(
            current.right, symbols, step=step, initial=initial, where=where,
        )
    except LoweringUnsupportedError:
        return None
    if left_single is None or right_single is None:
        return None
    return factor, left_single, right_single




def lower_relation_s2(
    relation: MathRelation,
    symbols: Mapping[str, SymbolEntry],
    *,
    step: int = 0,
    initial: Mapping[str, float | LinearExpression] | None = None,
) -> LoweredRelation:
    """降级单个 modeling 关系为约束行（关系级）。

    ``lhs = rhs`` 先按 ``lhs - rhs == 0`` 降级；两侧整体构成
    ``X * Y`` 单积（常数因子允许）时按互斥 Big-M 处理。恒真
    （``0 == 0``）返回空行；恒假即明确不支持。
    """
    if not isinstance(relation, MathRelation):
        raise TypeError("relation 须为 MathRelation")
    if not isinstance(step, int) or isinstance(step, bool) or step < 0:
        raise TypeError(f"step 须为非负整数: {step!r}")
    checked = _check_symbols(symbols)
    resolved_initial = dict(initial or {})
    where = f"关系 {relation.id!r}"
    try:
        lhs = _lower_modeling_node(
            relation.ast.lhs_root, checked, step=step,
            initial=resolved_initial, where=where,
        )
    except LoweringUnsupportedError as lhs_exc:
        lhs = lhs_exc
    try:
        rhs = _lower_modeling_node(
            relation.ast.rhs_root, checked, step=step,
            initial=resolved_initial, where=where,
        )
    except LoweringUnsupportedError as rhs_exc:
        rhs = rhs_exc
    if isinstance(lhs, _Affine) and isinstance(rhs, _Affine):
        for key, coeff in rhs.coeffs.items():
            lhs.add_term(key, -coeff)
        lhs.add_const(-rhs.constant)
        diff = lhs.checked(where)
        if not diff.coeffs:
            if diff.constant == 0:
                return LoweredRelation(constraints=(), variables=())
            raise LoweringUnsupportedError(
                f"{where}恒假（常数 {diff.constant}≠0）"
            )
        row = LinearConstraint(
            name=relation.id, expression=diff.freeze(), sense="eq", rhs=0.0,
        )
        return LoweredRelation(constraints=(row,), variables=())
    # 一侧仿射失败：仅当另一侧为常数零且本侧为单积 X*Y 时走互斥 Big-M。
    lhs_ok = isinstance(lhs, _Affine)
    rhs_ok = isinstance(rhs, _Affine)
    if not lhs_ok and not rhs_ok:
        raise lhs  # 两侧均不可降级，抛左侧异常（确定性优先）
    if (lhs_ok and lhs.coeffs) or (rhs_ok and rhs.coeffs):
        raise rhs if isinstance(rhs, LoweringUnsupportedError) else lhs
    zero_side = lhs if lhs_ok else rhs
    product_root = relation.ast.rhs_root if lhs_ok else relation.ast.lhs_root
    if zero_side.constant != 0:
        raise LoweringUnsupportedError(f"{where}非零常数侧无法与乘积配对")
    split = _split_const_product(
        product_root, checked, step=step, initial=resolved_initial, where=where,
    )
    if split is None:
        raise rhs if isinstance(rhs, LoweringUnsupportedError) else lhs
    factor, (x_key, _), (y_key, _) = split
    if factor == 0:
        return LoweredRelation(constraints=(), variables=())
    return lower_mutex(relation.id, x_key, y_key, checked)



