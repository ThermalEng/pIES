"""求解器中立的线性/MILP 降级基础(0.8f-A)。

仅为首个 MPS writer 服务的最小不可变边界，不是通用优化框架：
``LinearVariable`` / ``LinearExpression``(仿射) / ``LinearConstraint``
/ ``LinearModel`` + 纯仿射 normalizer/lowerer。

消费 modeling 公共 AST（``NumberNode``/``RefNode``/``UnaryNode``/
/// ``BinaryNode``，时间索引经符号表解析）或已编译规划 IR
（``PlanningExpression`` 的 num/var/bin/unary/cmp 节点）。首版能力（已裁定）：

- 可证明的仿射模型：常量、property、已固定标量/序列系数确定性部分
  求值；加减、常数乘/除降为仿射；
- relation 的 ``t=0`` 状态引用取 ``initial`` 初值，``t>0`` 仅支持
  ``t-1``（``t-2`` 在设备发布边界已拒绝，此处不再接受）；
- ``x*y=0`` 互斥：仅当 x/y 均非负且具有有限上界时引入稳定命名的
  binary 与两条 Big-M 约束；缺上界/非负不可证即明确不支持；
- 其余（变量×变量、变量作除数、非常数幂、函数/if/bool、
  严格不等式等）一律 ``LoweringUnsupportedError``，不近似、不回退。

输出变量/行/系数稳定排序，数值表示确定（-0.0 归一为 0.0）。
本轮只做到表达式/单关系级降级；总线平衡、财务成本聚合与项目级
MPS 属 0.8f-B。不按设备 ID 分支。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from iesplan.core.contracts import OBJECTIVE_SENSES, PlanningExpression
from iesplan.modeling import (
    BinaryNode,
    MathRelation,
    NumberNode,
    RefNode,
    UnaryNode,
)

__all__ = [
    "LoweringUnsupportedError",
    "LinearConstraint",
    "LinearExpression",
    "LinearModel",
    "LinearVariable",
    "LoweredRelation",
    "lower_expression",
    "lower_planning_constraint",
    "lower_planning_expression",
    "lower_relation",
]

#: 约束方向：eq 相等 / le 小于等于 / ge 大于等于。
ROW_SENSES: tuple[str, ...] = ("eq", "le", "ge")

#: 变量种类（规划变量均为连续；binary 只由受支持的互斥转换内部引入）。
VARIABLE_KINDS: tuple[str, ...] = ("continuous", "binary")


class LoweringUnsupportedError(ValueError):
    """数学形式超出首版降级能力（明确拒绝，非诊断包装）。"""


# ---------------------------------------------------------------------------
# 不可变线性 IR
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LinearVariable:
    """线性变量声明（名 + 有限/无穷界 + 种类）。"""

    name: str
    lower: float | None = None
    upper: float | None = None
    kind: str = "continuous"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise TypeError(f"变量名须为非空字符串: {self.name!r}")
        for side, value in (("lower", self.lower), ("upper", self.upper)):
            if value is not None and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
            ):
                raise TypeError(f"变量界 {side} 须为有限数或 None: {value!r}")
        if self.kind not in VARIABLE_KINDS:
            raise ValueError(f"变量种类非法: {self.kind!r}")
        if (
            self.lower is not None
            and self.upper is not None
            and self.lower > self.upper
        ):
            raise ValueError(
                f"变量界颠倒: lower={self.lower} > upper={self.upper}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "lower": self.lower,
            "upper": self.upper,
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class LinearExpression:
    """仿射表达式：terms 为 ((名, 步偏移), 系数) 排序元组 + 常数。"""

    terms: tuple[tuple[tuple[str, int], float], ...] = ()
    constant: float = 0.0

    def __post_init__(self) -> None:
        for key, coeff in self.terms:
            if (
                not isinstance(key, tuple)
                or len(key) != 2
                or not isinstance(key[0], str)
                or not isinstance(key[1], int)
                or isinstance(key[1], bool)
            ):
                raise TypeError(f"仿射项变量键非法: {key!r}")
            if (
                not isinstance(coeff, (int, float))
                or isinstance(coeff, bool)
                or not math.isfinite(coeff)
            ):
                raise TypeError(f"仿射系数须为有限数: {coeff!r}")
        if (
            not isinstance(self.constant, (int, float))
            or isinstance(self.constant, bool)
            or not math.isfinite(self.constant)
        ):
            raise TypeError(f"仿射常数须为有限数: {self.constant!r}")
        object.__setattr__(
            self, "terms", tuple(sorted(self.terms, key=lambda item: item[0]))
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "terms": [
                {"variable": name, "offset": offset, "coeff": coeff}
                for (name, offset), coeff in self.terms
            ],
            "constant": self.constant,
        }


@dataclass(frozen=True, slots=True)
class LinearConstraint:
    """线性约束行：仿射表达式 sense 右端常数。"""

    name: str
    expression: LinearExpression
    sense: str = "eq"
    rhs: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise TypeError(f"约束名须为非空字符串: {self.name!r}")
        if not isinstance(self.expression, LinearExpression):
            raise TypeError("expression 须为 LinearExpression")
        if self.sense not in ROW_SENSES:
            raise ValueError(f"约束方向非法: {self.sense!r}")
        if (
            not isinstance(self.rhs, (int, float))
            or isinstance(self.rhs, bool)
            or not math.isfinite(self.rhs)
        ):
            raise TypeError(f"右端常数须为有限数: {self.rhs!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "expression": self.expression.to_dict(),
            "sense": self.sense,
            "rhs": self.rhs,
        }


@dataclass(frozen=True, slots=True)
class LinearModel:
    """项目级线性模型：变量声明 + 约束行 + 目标（均稳定排序冻结）。

    ``objective`` 为目标仿射式（MPS 无法承载的常数项由 writer 以
    偏移量单独报告，不影响最优解）；``sense`` 为 minimize/maximize。
    本模型只服务首个 HiGHS MILP 链的项目级构造，不是通用优化框架。
    """

    variables: tuple[LinearVariable, ...] = ()
    constraints: tuple[LinearConstraint, ...] = ()
    objective: LinearExpression = LinearExpression()
    sense: str = "minimize"

    def __post_init__(self) -> None:
        for variable in self.variables:
            if not isinstance(variable, LinearVariable):
                raise TypeError(f"变量条目非法: {variable!r}")
        names = [variable.name for variable in self.variables]
        if len(set(names)) != len(names):
            raise ValueError(f"变量名重复: {sorted(names)}")
        for row in self.constraints:
            if not isinstance(row, LinearConstraint):
                raise TypeError(f"约束条目非法: {row!r}")
        row_names = [row.name for row in self.constraints]
        if len(set(row_names)) != len(row_names):
            raise ValueError(f"约束名重复: {sorted(row_names)}")
        if not isinstance(self.objective, LinearExpression):
            raise TypeError("objective 须为 LinearExpression")
        if self.sense not in OBJECTIVE_SENSES:
            raise ValueError(f"目标方向非法: {self.sense!r}")
        object.__setattr__(
            self,
            "variables",
            tuple(sorted(self.variables, key=lambda item: item.name)),
        )
        object.__setattr__(
            self,
            "constraints",
            tuple(sorted(self.constraints, key=lambda item: item.name)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "variables": [variable.to_dict() for variable in self.variables],
            "constraints": [row.to_dict() for row in self.constraints],
            "objective": self.objective.to_dict(),
            "sense": self.sense,
        }


@dataclass(frozen=True, slots=True)
class LoweredRelation:
    """单关系降级结果：约束行 + 本关系内部引入的变量（仅 Big-M binary）。"""

    constraints: tuple[LinearConstraint, ...] = ()
    variables: tuple[LinearVariable, ...] = ()


# ---------------------------------------------------------------------------
# 符号表与仿射累加
# ---------------------------------------------------------------------------

#: 符号条目：("const", 值) | ("var", 下界, 上界)。
SymbolEntry = tuple


def _resolve_symbol(name: str, symbols: Mapping[str, SymbolEntry]) -> SymbolEntry:
    try:
        entry = symbols[name]
    except KeyError:
        raise LoweringUnsupportedError(f"未知符号: {name!r}") from None
    if (
        not isinstance(entry, tuple)
        or not entry
        or entry[0] not in ("const", "var")
    ):
        raise TypeError(f"符号条目形态非法: {name!r}={entry!r}")
    return entry


def _check_number(value: object, what: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        raise LoweringUnsupportedError(f"{what}须为有限数，得到 {value!r}")
    return float(value)


def _norm_zero(value: float) -> float:
    return 0.0 if value == 0 else value


class _Affine:
    """可变仿射累加器（内部使用；出口一律冻结为 LinearExpression）。"""

    __slots__ = ("coeffs", "constant")

    def __init__(self) -> None:
        self.coeffs: dict[tuple[str, int], float] = {}
        self.constant: float = 0.0

    def add_term(self, key: tuple[str, int], coeff: float) -> None:
        total = self.coeffs.get(key, 0.0) + coeff
        if total == 0:
            self.coeffs.pop(key, None)
        else:
            self.coeffs[key] = total

    def add_const(self, value: float) -> None:
        self.constant += value

    def scale(self, factor: float) -> None:
        for key in list(self.coeffs):
            self.coeffs[key] *= factor
        self.constant *= factor

    def checked(self, what: str) -> _Affine:
        for coeff in self.coeffs.values():
            if not math.isfinite(coeff):
                raise LoweringUnsupportedError(f"{what}系数非有限")
        if not math.isfinite(self.constant):
            raise LoweringUnsupportedError(f"{what}常数非有限")
        return self

    def freeze(self) -> LinearExpression:
        terms = tuple(
            (key, _norm_zero(coeff)) for key, coeff in self.coeffs.items()
        )
        return LinearExpression(terms=terms, constant=_norm_zero(self.constant))


# ---------------------------------------------------------------------------
# 前端：modeling AST
# ---------------------------------------------------------------------------


def _modeling_ref(
    name: str,
    offset: int,
    symbols: Mapping[str, SymbolEntry],
    *,
    step: int,
    initial: Mapping[str, float],
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
        out.add_const(_check_number(value, f"状态 {name!r} 初值"))
        return out
    out.add_term((name, offset), 1.0)
    return out


def _lower_modeling_node(
    node: object,
    symbols: Mapping[str, SymbolEntry],
    *,
    step: int,
    initial: Mapping[str, float],
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


# ---------------------------------------------------------------------------
# 前端：规划 IR
# ---------------------------------------------------------------------------


def _planning_ref(
    alias: str,
    aliases: Mapping[str, str],
    symbols: Mapping[str, SymbolEntry],
) -> _Affine:
    out = _Affine()
    original = aliases.get(alias, alias)
    entry = _resolve_symbol(original, symbols)
    if entry[0] == "const":
        out.add_const(_check_number(entry[1], f"常量 {original!r}"))
        return out
    out.add_term((original, 0), 1.0)
    return out


def _lower_planning_node(
    node: object,
    aliases: Mapping[str, str],
    symbols: Mapping[str, SymbolEntry],
    where: str,
) -> _Affine:
    if not isinstance(node, tuple) or not node:
        raise LoweringUnsupportedError(f"{where}非法 IR 节点 {node!r}")
    head = node[0]
    if head == "num":
        out = _Affine()
        out.add_const(_check_number(node[1], f"{where}数值"))
        return out
    if head == "var":
        return _planning_ref(node[1], aliases, symbols)
    if head == "unary":
        inner = _lower_planning_node(node[2], aliases, symbols, where)
        if node[1] == "neg":
            inner.scale(-1.0)
            return inner
        if node[1] == "pos":
            return inner
        raise LoweringUnsupportedError(f"{where}不支持的一元运算 {node[1]!r}")
    if head == "bin":
        _, op, left_node, right_node = node
        left = _lower_planning_node(left_node, aliases, symbols, where)
        right = _lower_planning_node(right_node, aliases, symbols, where)
        if op in ("add", "sub"):
            if op == "sub":
                right.scale(-1.0)
            for key, coeff in right.coeffs.items():
                left.add_term(key, coeff)
            left.add_const(right.constant)
            return left.checked(where)
        if op == "mul":
            if not left.coeffs:
                left, right = right, left
            if not right.coeffs:
                left.scale(right.constant)
                return left.checked(where)
            raise LoweringUnsupportedError(f"{where}变量×变量超出首版能力")
        if op == "div":
            if right.coeffs:
                raise LoweringUnsupportedError(f"{where}变量作除数超出首版能力")
            if right.constant == 0:
                raise LoweringUnsupportedError(f"{where}除零")
            left.scale(1.0 / right.constant)
            return left.checked(where)
        if op == "pow":
            if not right.coeffs and not left.coeffs:
                result = math.pow(left.constant, right.constant)
                out = _Affine()
                out.add_const(_check_number(result, f"{where}常数幂"))
                return out
            if not right.coeffs and right.constant == 1 and left.coeffs:
                return left
            if not right.coeffs and right.constant == 0 and left.coeffs:
                out = _Affine()
                out.add_const(1.0)
                return out
            raise LoweringUnsupportedError(f"{where}非常数幂超出首版能力")
        raise LoweringUnsupportedError(f"{where}不支持的运算 {op!r}")
    raise LoweringUnsupportedError(f"{where}不支持的 IR 节点 {head!r}")


# ---------------------------------------------------------------------------
# 公开降级入口
# ---------------------------------------------------------------------------


def _check_symbols(symbols: object) -> Mapping[str, SymbolEntry]:
    if not isinstance(symbols, Mapping):
        raise TypeError("symbols 须为 Mapping")
    return symbols


def lower_expression(
    node: object,
    symbols: Mapping[str, SymbolEntry],
    *,
    step: int = 0,
    initial: Mapping[str, float] | None = None,
) -> LinearExpression:
    """降级单个 modeling AST 表达式为仿射式（表达式级）。

    参数:
        node: AST 根节点（单侧）。
        symbols: 符号名 → ("const", 值) | ("var", 下界, 上界)。
        step: 当前步（t=0 时 ``t-1`` 取 ``initial`` 初值）。
        initial: 状态变量名 → 初值。
    """
    if not isinstance(step, int) or isinstance(step, bool) or step < 0:
        raise TypeError(f"step 须为非负整数: {step!r}")
    lowered = _lower_modeling_node(
        node, _check_symbols(symbols), step=step,
        initial=initial or {}, where="表达式",
    )
    return lowered.checked("表达式").freeze()


def _single_var_product(
    node: object,
    symbols: Mapping[str, SymbolEntry],
    *,
    step: int,
    initial: Mapping[str, float],
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


def _mutex_big_m(
    name: str,
    x_key: tuple[str, int],
    y_key: tuple[str, int],
    symbols: Mapping[str, SymbolEntry],
) -> LoweredRelation:
    """``x*y=0``（非负、有限上界）→ binary + 两条 Big-M 约束。

    互斥语义只依赖两个非负基础变量是否为零：乘积上的非零常数因子
    不改变可行域，约束一律按基础变量及其上界生成（系数恒为正）。
    """
    bounds: dict[tuple[str, int], float] = {}
    for key in (x_key, y_key):
        entry = _resolve_symbol(key[0], symbols)
        if entry[0] != "var":
            raise LoweringUnsupportedError(f"互斥项 {key[0]!r} 不是变量")
        lower, upper = entry[1], entry[2]
        if lower is None or lower < 0:
            raise LoweringUnsupportedError(
                f"互斥项 {key[0]!r} 非负不可证（下界 {lower!r}）"
            )
        if upper is None or not math.isfinite(upper) or upper <= 0:
            raise LoweringUnsupportedError(
                f"互斥项 {key[0]!r} 缺有限上界（上界 {upper!r}）"
            )
        bounds[key] = float(upper)
    mx = bounds[x_key]
    my = bounds[y_key]
    b_var = LinearVariable(name=f"{name}__mutex", lower=0.0, upper=1.0, kind="binary")
    b_key = (b_var.name, 0)
    first = _Affine()
    first.add_term(x_key, 1.0)
    first.add_term(b_key, -mx)
    second = _Affine()
    second.add_term(y_key, 1.0)
    second.add_term(b_key, my)
    return LoweredRelation(
        constraints=(
            LinearConstraint(
                name=f"{name}__mutex_x", expression=first.freeze(),
                sense="le", rhs=0.0,
            ),
            LinearConstraint(
                name=f"{name}__mutex_y", expression=second.freeze(),
                sense="le", rhs=my,
            ),
        ),
        variables=(b_var,),
    )


def _split_const_product(
    node: object,
    symbols: Mapping[str, SymbolEntry],
    *,
    step: int,
    initial: Mapping[str, float],
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


def lower_relation(
    relation: MathRelation,
    symbols: Mapping[str, SymbolEntry],
    *,
    step: int = 0,
    initial: Mapping[str, float] | None = None,
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
    return _mutex_big_m(relation.id, x_key, y_key, checked)


def lower_planning_expression(
    expression: PlanningExpression,
    symbols: Mapping[str, SymbolEntry],
) -> LinearExpression:
    """降级已编译规划表达式（目标式等仿射部分）为仿射式。"""
    if not isinstance(expression, PlanningExpression):
        raise TypeError("expression 须为 PlanningExpression")
    aliases = {alias: original for original, alias in expression.aliases}
    lowered = _lower_planning_node(
        expression.ir, aliases, _check_symbols(symbols), "规划表达式",
    )
    return lowered.checked("规划表达式").freeze()


def lower_planning_constraint(
    name: str,
    expression: PlanningExpression,
    symbols: Mapping[str, SymbolEntry],
) -> LinearConstraint:
    """降级已启用规划约束（顶层比较）为单约束行。

    直接接收约束名与已编译 ``PlanningExpression``；约束是否启用、
    归属哪个规划由装配产物决定，不另设包装对象。
    """
    if not isinstance(name, str) or not name:
        raise TypeError(f"约束名须为非空字符串: {name!r}")
    if not isinstance(expression, PlanningExpression):
        raise TypeError("expression 须为 PlanningExpression")
    node = expression.ir
    if not isinstance(node, tuple) or not node or node[0] != "cmp":
        raise LoweringUnsupportedError(
            f"约束 {name!r} 顶层不是比较，超出首版能力"
        )
    _, op, left_node, right_node = node
    aliases = {
        alias: original
        for original, alias in expression.aliases
    }
    checked = _check_symbols(symbols)
    left = _lower_planning_node(
        left_node, aliases, checked, f"约束 {name!r}"
    )
    right = _lower_planning_node(
        right_node, aliases, checked, f"约束 {name!r}"
    )
    for key, coeff in right.coeffs.items():
        left.add_term(key, -coeff)
    left.add_const(-right.constant)
    diff = left.checked(f"约束 {name!r}")
    if op == "eq":
        sense = "eq"
    elif op == "le":
        sense = "le"
    elif op == "ge":
        sense = "ge"
    else:
        raise LoweringUnsupportedError(
            f"约束 {name!r} 的 {op!r} 不是 LP 可表示比较"
        )
    return LinearConstraint(
        name=name, expression=diff.freeze(), sense=sense, rhs=0.0,
    )
