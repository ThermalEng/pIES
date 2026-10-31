"""Per-generate technical equation templates (P1-S3).

The public single-relation lowerer remains available. The production project
generator instead compiles modeling AST *structure* once per compatible model
and symbol-role mode, then binds actual constants/series/state per timestep.

This is an internal instruction representation, never a second user-facing
expression language, serialized Bundle schema or persisted cache.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from iesplan.computation.generators.linear import (
    LinearConstraint, LinearExpression, LoweredRelation, LoweringUnsupportedError,
    lower_mutex,
)
from iesplan.modeling import BinaryNode, MathRelation, NumberNode, RefNode, UnaryNode


# A compact postfix instruction: ("number", float), ("known", identifier),
# ("var", (identifier, time_offset)), ("unary", op), ("binary", op).
Instruction = tuple[str, object]
Key = tuple[str, int]


@dataclass(frozen=True, slots=True)
class ProductCandidate:
    """A compiled conditional mutex; evaluated only after affine failure."""

    factor: tuple[tuple[Instruction, ...], ...]
    left: tuple[Instruction, ...]
    right: tuple[Instruction, ...]


@dataclass(frozen=True, slots=True)
class TechnicalTemplate:
    relation_id: str
    lhs: tuple[Instruction, ...]
    rhs: tuple[Instruction, ...]
    lhs_mutex: ProductCandidate | None
    rhs_mutex: ProductCandidate | None
    refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StepConstraintTemplate:
    relation_id: str
    sense: str
    rhs: float
    constant: float
    # (actual instance, interface, coefficient) or scalar planning name.
    ports: tuple[tuple[str, str, float], ...]
    planning: tuple[tuple[str, float], ...]


def _is_known(node: object, roles: Mapping[str, str]) -> bool:
    if isinstance(node, NumberNode):
        return True
    if isinstance(node, RefNode):
        return node.offset == 0 and roles[node.name] == "known"
    if isinstance(node, UnaryNode):
        return _is_known(node.operand, roles)
    if isinstance(node, BinaryNode):
        return _is_known(node.left, roles) and _is_known(node.right, roles)
    return False


def _instructions(node: object, roles: Mapping[str, str]) -> tuple[Instruction, ...]:
    out: list[Instruction] = []

    def visit(expr: object) -> None:
        if isinstance(expr, NumberNode):
            out.append(("number", expr.value))
        elif isinstance(expr, RefNode):
            mode = roles[expr.name]
            if mode == "known":
                if expr.offset != 0:
                    raise LoweringUnsupportedError(
                        f"常量 {expr.name!r} 不得带时间索引 [t{expr.offset:+d}]"
                    )
                out.append(("known", expr.name))
            else:
                out.append(("var", (expr.name, expr.offset)))
        elif isinstance(expr, UnaryNode):
            visit(expr.operand)
            out.append(("unary", expr.op))
        elif isinstance(expr, BinaryNode):
            visit(expr.left)
            visit(expr.right)
            out.append(("binary", expr.op))
        else:
            raise LoweringUnsupportedError(
                f"不支持的 AST 节点 {type(expr).__name__}"
            )
    visit(node)
    return tuple(out)


def _mutex_product(
    root: object, roles: Mapping[str, str],
) -> ProductCandidate | None:
    scalars: list[tuple[Instruction, ...]] = []
    expr = root
    while isinstance(expr, BinaryNode) and expr.op == "*":
        if _is_known(expr.left, roles):
            scalars.append(_instructions(expr.left, roles))
            expr = expr.right
        elif _is_known(expr.right, roles):
            scalars.append(_instructions(expr.right, roles))
            expr = expr.left
        else:
            break
    if not (isinstance(expr, BinaryNode) and expr.op == "*"):
        return None
    return ProductCandidate(
        tuple(scalars),
        _instructions(expr.left, roles),
        _instructions(expr.right, roles),
    )


def compile_technical(
    relation: MathRelation, roles: Mapping[str, str],
) -> TechnicalTemplate:
    """Inspect modeling AST once; no instance property or series values captured."""
    lhs = relation.ast.lhs_root
    rhs = relation.ast.rhs_root
    return TechnicalTemplate(
        relation_id=relation.id,
        lhs=_instructions(lhs, roles),
        rhs=_instructions(rhs, roles),
        lhs_mutex=_mutex_product(lhs, roles),
        rhs_mutex=_mutex_product(rhs, roles),
        refs=tuple(sorted(roles)),
    )


@dataclass(slots=True)
class _Affine:
    constant: float
    terms: dict[Key, float]

    def add_term(self, key: Key, coeff: float) -> None:
        total = self.terms.get(key, 0.0) + coeff
        if total == 0:
            self.terms.pop(key, None)
        else:
            self.terms[key] = total

    def scaled(self, factor: float) -> _Affine:
        return _Affine(self.constant * factor, {
            key: coeff * factor for key, coeff in self.terms.items()
            if coeff * factor != 0.0
        })

    def checked(self, location: str) -> _Affine:
        if not math.isfinite(self.constant) or any(
            not math.isfinite(v) for v in self.terms.values()
        ):
            raise LoweringUnsupportedError(f"{location}系数或常数非有限")
        return self


def _evaluate(
    code: tuple[Instruction, ...], *, step: int,
    known: Mapping[str, float], initial: Mapping[str, float | LinearExpression],
    where: str,
) -> _Affine:
    """Evaluate precompiled instructions, not modeling AST or a copied symbol map."""
    stack: list[_Affine] = []
    for op, value in code:
        if op == "number":
            stack.append(_Affine(float(value), {}))
        elif op == "known":
            stack.append(_Affine(float(known[value]), {}))
        elif op == "var":
            name, offset = value
            if offset < -1:
                raise LoweringUnsupportedError(
                    f"{where}{name!r} 的 [t{offset:+d}] 超出首版能力"
                )
            if offset == -1 and step == 0:
                if name not in initial:
                    raise LoweringUnsupportedError(f"{where}状态 {name!r} 首步缺少初值")
                start = initial[name]
                if isinstance(start, LinearExpression):
                    acc = _Affine(start.constant, {})
                    for key, coeff in start.terms:
                        acc.add_term(key, coeff)
                    stack.append(acc)
                else:
                    stack.append(_Affine(float(start), {}))
            else:
                stack.append(_Affine(0.0, {(name, offset): 1.0}))
        elif op == "unary":
            node = stack.pop()
            if value == "-":
                stack.append(node.scaled(-1.0))
            elif value == "+":
                stack.append(node)
            else:
                raise LoweringUnsupportedError(f"{where}不支持一元运算 {value!r}")
        elif op == "binary":
            right = stack.pop()
            left = stack.pop()
            if value == "+" or value == "-":
                if value == "-":
                    right = right.scaled(-1.0)
                left.constant += right.constant
                for key, coeff in right.terms.items():
                    left.add_term(key, coeff)
                result = left
            elif value == "*":
                if left.terms and right.terms:
                    raise LoweringUnsupportedError(f"{where}变量×变量超出首版能力")
                if not left.terms:
                    result = right.scaled(left.constant)
                else:
                    result = left.scaled(right.constant)
            elif value == "/":
                if right.terms:
                    raise LoweringUnsupportedError(f"{where}变量作除数超出首版能力")
                if right.constant == 0:
                    raise LoweringUnsupportedError(f"{where}除零")
                result = left.scaled(1.0 / right.constant)
            elif value == "^":
                if not left.terms and not right.terms:
                    try:
                        power = math.pow(left.constant, right.constant)
                    except (ValueError, OverflowError):
                        raise LoweringUnsupportedError(f"{where}常数幂定义域/溢出") from None
                    result = _Affine(power, {})
                elif left.terms and not right.terms and right.constant == 1:
                    result = left
                elif left.terms and not right.terms and right.constant == 0:
                    result = _Affine(1.0, {})
                else:
                    raise LoweringUnsupportedError(f"{where}非常数幂超出首版能力")
            else:
                raise LoweringUnsupportedError(f"{where}不支持运算 {value!r}")
            stack.append(result.checked(where))
    if len(stack) != 1:
        raise RuntimeError("invalid compiler instruction stack")
    return stack[0].checked(where)


def bind_technical(
    template: TechnicalTemplate, *, instance: str, step: int,
    known: Mapping[str, float], initial: Mapping[str, float | LinearExpression],
    bound: Mapping[str, tuple[float | None, float | None]],
) -> LoweredRelation:
    """Bind actual step values, returning the unchanged solver-neutral lowerer IR."""
    where = f"关系 {template.relation_id!r} 实例 {instance!r} step {step}: "
    try:
        lhs = _evaluate(template.lhs, step=step, known=known, initial=initial, where=where)
    except LoweringUnsupportedError as exc:
        lhs = exc
    try:
        rhs = _evaluate(template.rhs, step=step, known=known, initial=initial, where=where)
    except LoweringUnsupportedError as exc:
        rhs = exc
    if isinstance(lhs, _Affine) and isinstance(rhs, _Affine):
        lhs.constant -= rhs.constant
        for key, coeff in rhs.terms.items():
            lhs.add_term(key, -coeff)
        diff = lhs.checked(where)
        if not diff.terms:
            if diff.constant == 0:
                return LoweredRelation()
            raise LoweringUnsupportedError(
                f"{where}恒假（常数 {diff.constant}≠0）"
            )
        return LoweredRelation(constraints=(
            LinearConstraint(
                template.relation_id,
                LinearExpression(
                    tuple((key, coeff) for key, coeff in diff.terms.items()),
                    diff.constant,
                ),
                sense="eq", rhs=0.0,
            ),
        ))
    lhs_ok, rhs_ok = isinstance(lhs, _Affine), isinstance(rhs, _Affine)
    if not lhs_ok and not rhs_ok:
        raise lhs
    if (lhs_ok and lhs.terms) or (rhs_ok and rhs.terms):
        raise rhs if isinstance(rhs, LoweringUnsupportedError) else lhs
    constant_side = lhs if lhs_ok else rhs
    if constant_side.constant != 0.0:
        raise LoweringUnsupportedError(f"{where}非零常数侧无法与乘积配对")
    candidate = template.rhs_mutex if lhs_ok else template.lhs_mutex
    if candidate is None:
        raise rhs if isinstance(rhs, LoweringUnsupportedError) else lhs
    factor = 1.0
    for code in candidate.factor:
        item = _evaluate(code, step=step, known=known, initial=initial, where=where)
        if item.terms:
            raise LoweringUnsupportedError(f"{where}互斥系数不是已知量")
        factor *= item.constant
    if factor == 0:
        return LoweredRelation()
    x = _evaluate(candidate.left, step=step, known=known, initial=initial, where=where)
    y = _evaluate(candidate.right, step=step, known=known, initial=initial, where=where)
    if not x.terms or not y.terms:
        raise LoweringUnsupportedError(f"{where}互斥因子不是单变量")
    if (x.constant != 0 or y.constant != 0
            or len(x.terms) != 1 or len(y.terms) != 1):
        raise LoweringUnsupportedError(f"{where}互斥因子不是单变量")
    (x_key, x_coeff), = x.terms.items()
    (y_key, y_coeff), = y.terms.items()
    if x_coeff == 0 or y_coeff == 0:
        return LoweredRelation()
    # Bounds are the actual declarations reported by the S2 analysis view;
    # the precise zero-box and finite-bound cases remain in lower_mutex.
    bindings = {name: ("var", *bound[name]) for name in (x_key[0], y_key[0])}
    return lower_mutex(template.relation_id, x_key, y_key, bindings)


def compile_step_constraint(
    relation_id: str, expression: LinearExpression, sense: str, rhs: float,
    references: Mapping[str, Mapping[str, str]], planning: frozenset[str],
) -> StepConstraintTemplate:
    """Resolve aliases once to existing actual port identities and scalar plans."""
    ports: list[tuple[str, str, float]] = []
    scalars: list[tuple[str, float]] = []
    for (name, offset), coeff in expression.terms:
        if offset:
            raise LoweringUnsupportedError(
                f"step relation {relation_id!r} has an unsupported offset"
            )
        if name in references:
            target = references[name]
            ports.append((target["instance"], target["interface"], coeff))
        elif name in planning:
            scalars.append((name, coeff))
        else:
            raise LoweringUnsupportedError(
                f"step relation {relation_id!r}: unknown symbol {name!r}"
            )
    return StepConstraintTemplate(
        relation_id, sense, rhs, expression.constant, tuple(ports), tuple(scalars),
    )
