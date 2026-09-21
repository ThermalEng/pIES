"""受限表达式引擎(04 §4):安全 AST 解析 + 白名单求值。

安全模型(docstring 说明):
- 使用 Python 标准库 `ast` 解析(绝不使用 eval/exec/compile 执行用户输入);
- 编译阶段做 AST 白名单:仅允许 数字常量 / 变量(Name) / 算术(BinOp/UnaryOp) /
  比较(Compare) / 逻辑(BoolOp) / 白名单函数调用(Call) / 括号;
  其余任何节点(属性访问、下标、lambda、推导式、import、字符串、字典等)→ EXPR-SEC-001;
- 函数白名单:abs/min/max/sin/cos/tan/exp/log/sqrt/pow,另加 if() 控制结构
  (04 §4.1 BNF 的比较/逻辑只在 if()/when() 中合法;本引擎同时允许顶层比较返回 0/1);
- 变量名必须在 parse_expr 传入的 allowed_vars 集合内,否则 EXPR-CODE-001;
- 量纲检查:变量可声明量纲(var_dims),加减要求同量纲、乘除按量纲合并,
  sin/cos/tan/exp/log 要求无量纲,sqrt 要求偶次量纲,幂指数必须为常数(EXPR-TYP-001),
  幂结果量纲为原量纲 × 指数;不匹配报 EXPR-DIM-001;
- 范围检查:常量字面量 |v| ≤ 1e15,幂指数 |n| ≤ 100(EXPR-RNG-001);
- 求值阶段:步数上限 100000(04 §4.4),除零/对数定义域/溢出/NaN 一律抛
  ExpressionRunError(EXPR-RUN-001);
- 禁止字符串字面量、禁止 `_` 前缀标识符、禁止任何属性访问,杜绝内建对象逃逸。
"""

from __future__ import annotations

import ast
import math
import re
from collections import Counter
from typing import Final

from iesplan.core.errors import AppError
from iesplan.core.unitparse import NUMBER_RE as _NUMBER_RE

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
MAX_NODES: Final[int] = 512  # AST 节点数上限(04 §4.2)
MAX_DEPTH: Final[int] = 32  # AST 深度上限(04 §4.2)
MAX_EVAL_STEPS: Final[int] = 100_000  # 求值步数上限(04 §4.4)
MAX_LITERAL_ABS: Final[float] = 1e15  # 常量字面量绝对值上限
MAX_POW_EXPONENT: Final[float] = 100.0  # 幂指数范围

#: 函数白名单(任务清单;if 为控制结构)
WHITELIST_FUNCTIONS: frozenset[str] = frozenset(
    {"abs", "min", "max", "sin", "cos", "tan", "exp", "log", "sqrt", "pow", "if"}
)

#: 禁止的标识符模式(双扫描之一;AST 白名单为主防线)
FORBIDDEN_IDENT_PATTERNS: tuple[str, ...] = (
    "import",
    "eval",
    "exec",
    "compile",
    "getattr",
    "globals",
    "locals",
    "open",
    "__",
)

#: 维度类型标签(与 04 §8.3 业务简写一致)
DIM_ENERGY = "energy"
DIM_POWER = "power"
DIM_TIME = "time"
DIM_TEMPERATURE = "temperature"
DIM_CURRENCY = "currency"
DIM_ANGLE = "angle"

# 量纲 = 类别 → 次数的多重集(如功率 [power^1],能量 [energy^1] ≡ power·time)
Dimensions = Counter[str]


def _dim_of(categories: dict[str, int] | None = None) -> Dimensions:
    """构造量纲(类别 → 次数)。"""
    return Counter(categories or {})


def _dim_str(d: Dimensions) -> str:
    """量纲可读表示(如 "power^1 time^1")。"""
    if not d:
        return "dimensionless"
    return " ".join(f"{k}^{v}" if v != 1 else k for k, v in sorted(d.items()))


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class ExpressionError(AppError):
    """表达式引擎错误基类(code 取 EXPR-* 域已登记码)。"""

    severity = "error"
    blocking = True
    location = {"object_type": "formula", "object_id": "", "field": "expression"}

    def __init__(
        self,
        message: str,
        *,
        code: str,
        message_key: str,
        params: dict | None = None,
    ) -> None:
        self.code = code
        self.message_key = message_key
        super().__init__(message, code=code, message_key=message_key, params=params, location=self.location)


class ExpressionSyntaxError(ExpressionError):
    """语法错误(EXPR-SYN-001)。"""

    def __init__(self, message: str, **kw) -> None:
        super().__init__(message, code="EXPR-SYN-001", message_key="ies.expr.syntax_error", params=kw)


class ExpressionSecurityError(ExpressionError):
    """白名单/禁止名单违规(EXPR-SEC-001)。"""

    def __init__(self, message: str, **kw) -> None:
        super().__init__(message, code="EXPR-SEC-001", message_key="ies.expr.forbidden_fn", params=kw)


class ExpressionDimensionError(ExpressionError):
    """量纲不匹配(EXPR-DIM-001)。"""

    def __init__(self, message: str, **kw) -> None:
        super().__init__(message, code="EXPR-DIM-001", message_key="ies.expr.dim_mismatch", params=kw)


class ExpressionRangeError(ExpressionError):
    """范围检查失败(EXPR-RNG-001)。"""

    def __init__(self, message: str, **kw) -> None:
        super().__init__(message, code="EXPR-RNG-001", message_key="ies.expr.range_error", params=kw)


class ExpressionTypeError(ExpressionError):
    """类型检查失败(EXPR-TYP-001)。"""

    def __init__(self, message: str, **kw) -> None:
        super().__init__(message, code="EXPR-TYP-001", message_key="ies.expr.type_error", params=kw)


class ExpressionCodeError(ExpressionError):
    """变量未登记(EXPR-CODE-001)。"""

    def __init__(self, message: str, **kw) -> None:
        super().__init__(message, code="EXPR-CODE-001", message_key="ies.expr.code_error", params=kw)


class ExpressionRunError(ExpressionError):
    """运行期错误(EXPR-RUN-001)。"""

    def __init__(self, message: str, **kw) -> None:
        super().__init__(message, code="EXPR-RUN-001", message_key="ies.expr.run_error", params=kw)


# ---------------------------------------------------------------------------
# 编译(白名单 AST → 受限 IR)
# ---------------------------------------------------------------------------

# IR 节点类型:(kind, ...)
#   ("num", float) ("var", name) ("bin", op, l, r) ("unary", op, x)
#   ("cmp", op, l, r) ("bool", "and"|"or", items) ("call", fn, args) ("if", c, a, b)

_ARITH_OPS: dict[type, str] = {
    ast.Add: "add",
    ast.Sub: "sub",
    ast.Mult: "mul",
    ast.Div: "div",
    ast.FloorDiv: "floordiv",
    ast.Mod: "mod",
    ast.Pow: "pow",
}
_CMP_OPS: dict[type, str] = {
    ast.Eq: "eq",
    ast.NotEq: "ne",
    ast.Lt: "lt",
    ast.LtE: "le",
    ast.Gt: "gt",
    ast.GtE: "ge",
}

#: 期望各白名单函数的参数个数(可变参函数用 None)
_FUNC_ARG_COUNT: dict[str, int | None] = {
    "abs": 1,
    "min": None,  # ≥ 1
    "max": None,
    "sin": 1,
    "cos": 1,
    "tan": 1,
    "exp": 1,
    "log": 1,
    "sqrt": 1,
    "pow": 2,
    "if": 3,
}


class CompiledExpr:
    """编译后的受限表达式(不可变,可缓存)。

    属性:
        text: 原始表达式文本。
        allowed_vars: 允许的变量集合。
        ir: 内部受限 IR(嵌套元组,不含任何 Python 对象访问能力)。
        node_count: 编译后节点数(≤ MAX_NODES)。
        depth: 表达式深度(≤ MAX_DEPTH)。
    """

    __slots__ = ("text", "allowed_vars", "ir", "node_count", "depth")

    def __init__(
        self,
        text: str,
        allowed_vars: frozenset[str],
        ir: tuple,
        node_count: int,
        depth: int,
    ) -> None:
        self.text = text
        self.allowed_vars = allowed_vars
        self.ir = ir
        self.node_count = node_count
        self.depth = depth

    def eval(self, values: dict) -> float:
        """在给定变量值下求值(纯函数,无副作用)。

        参数:
            values: 变量名 → 数值;变量必须在 allowed_vars 中,数值必须为
                有限数(int/float),否则抛 ExpressionCodeError / ExpressionRunError。
        返回:
            浮点结果(含 0.0/1.0 表示的比较逻辑值)。
        异常:
            ExpressionError 子类:缺变量、除零、定义域、溢出、步数超限。
        """
        ctx: dict[str, float] = {}
        for name, val in values.items():
            if name not in self.allowed_vars:
                raise ExpressionCodeError(f"变量 {name} 不在允许集合中", variable=name, expr=self.text)
            try:
                f = float(val)
            except (TypeError, ValueError):
                raise ExpressionRunError(
                    f"变量 {name} 不是数值", variable=name, value=repr(val), expr=self.text
                ) from None
            if not math.isfinite(f):
                raise ExpressionRunError(
                    f"变量 {name} 不是有限数", variable=name, value=str(f), expr=self.text
                )
            ctx[name] = f

        state = {"steps": 0}

        def run(ir: tuple) -> float:
            state["steps"] += 1
            if state["steps"] > MAX_EVAL_STEPS:
                raise ExpressionRunError("求值步数超过上限", limit=MAX_EVAL_STEPS, expr=self.text)
            kind = ir[0]
            if kind == "num":
                return ir[1]
            if kind == "var":
                name = ir[1]
                if name not in ctx:
                    raise ExpressionCodeError(f"缺少变量 {name}", variable=name, expr=self.text)
                return ctx[name]
            if kind == "bin":
                op = ir[1]
                if op == "pow":
                    return _safe_pow(run(ir[2]), run(ir[3]), self.text)
                left, right = run(ir[2]), run(ir[3])
                return _bin(op, left, right, self.text)
            if kind == "unary":
                x = run(ir[2])
                return _neg(x, self.text) if ir[1] == "neg" else x
            if kind == "cmp":
                left, right = run(ir[2]), run(ir[3])
                return 1.0 if _cmp(ir[1], left, right) else 0.0
            if kind == "bool":
                items = ir[2]
                if ir[1] == "and":
                    return 1.0 if all(run(it) != 0.0 for it in items) else 0.0
                return 1.0 if any(run(it) != 0.0 for it in items) else 0.0
            if kind == "call":
                return _call(ir[1], [run(a) for a in ir[2]], self.text)
            if kind == "if":
                c = run(ir[1])
                return run(ir[2]) if c != 0.0 else run(ir[3])
            raise ExpressionRunError(f"未知 IR 节点 {kind!r}", expr=self.text)  # 不可达

        return _check_finite(run(self.ir), self.text)


def _neg(x: float, text: str) -> float:
    return _check_finite(-x, text)


def _bin(op: str, left: float, right: float, text: str) -> float:
    try:
        if op == "add":
            return _check_finite(left + right, text)
        if op == "sub":
            return _check_finite(left - right, text)
        if op == "mul":
            return _check_finite(left * right, text)
        if op == "div":
            if right == 0.0:
                raise ZeroDivisionError
            return _check_finite(left / right, text)
        if op == "floordiv":
            if right == 0.0:
                raise ZeroDivisionError
            return _check_finite(math.floor(left / right), text)
        if op == "mod":
            if right == 0.0:
                raise ZeroDivisionError
            return _check_finite(math.fmod(left, right), text)
        if op == "pow":
            return _safe_pow(left, right, text)
        raise ExpressionRunError(f"未知二元运算 {op!r}", expr=text)
    except ZeroDivisionError:
        raise ExpressionRunError("除数为零", expr=text) from None
    except OverflowError:
        raise ExpressionRunError("数值溢出", expr=text) from None


def _safe_pow(base: float, exp: float, text: str) -> float:
    if base < 0 and exp != math.floor(exp):
        raise ExpressionRunError("负底数的非整数次幂无实数结果", expr=text)
    try:
        return _check_finite(base**exp, text)
    except OverflowError:
        raise ExpressionRunError("幂运算溢出", expr=text) from None
    except ValueError:
        raise ExpressionRunError("幂运算定义域错误", expr=text) from None


def _cmp(op: str, left: float, right: float) -> bool:
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if op == "lt":
        return left < right
    if op == "le":
        return left <= right
    if op == "gt":
        return left > right
    if op == "ge":
        return left >= right
    raise ExpressionRunError(f"未知比较运算 {op!r}")


def _check_finite(x: float, text: str) -> float:
    if not math.isfinite(x):
        raise ExpressionRunError("产生非有限数值(NaN/Inf)", expr=text)
    return x


def _call(fn: str, args: list[float], text: str) -> float:
    try:
        if fn == "abs":
            return _check_finite(abs(args[0]), text)
        if fn == "min":
            return _check_finite(min(args), text)
        if fn == "max":
            return _check_finite(max(args), text)
        if fn == "sin":
            return _check_finite(math.sin(math.radians(args[0])), text)
        if fn == "cos":
            return _check_finite(math.cos(math.radians(args[0])), text)
        if fn == "tan":
            return _check_finite(math.tan(math.radians(args[0])), text)
        if fn == "exp":
            return _check_finite(math.exp(args[0]), text)
        if fn == "log":
            if args[0] <= 0:
                raise ValueError
            return _check_finite(math.log(args[0]), text)
        if fn == "sqrt":
            if args[0] < 0:
                raise ValueError
            return _check_finite(math.sqrt(args[0]), text)
        if fn == "pow":
            return _safe_pow(args[0], args[1], text)
        raise ExpressionSecurityError(f"调用白名单外的函数 {fn}", fn=fn, expr=text)
    except ValueError:
        raise ExpressionRunError("函数定义域错误", fn=fn, expr=text) from None
    except OverflowError:
        raise ExpressionRunError("数值溢出", fn=fn, expr=text) from None


# ---------------------------------------------------------------------------
# 解析与编译
# ---------------------------------------------------------------------------

#: 表达式中的显式单位后缀("1500 W" → 常量变量),正则匹配 <数值> <空格> <单位token>
#: 数值复用 unitparse.NUMBER_RE(科学计数/千分位/中文字数), 单位 token 覆盖
#: 注册单位形态(含 %、℃ 等单字符)(codex 二次审核 Low-2)
_UNIT_SUFFIX_RE = re.compile(
    r"(?<![\w.])(" + _NUMBER_RE.pattern + r")\s+"
    r"([A-Za-z°℃μ%][A-Za-z0-9°℃μ·/]*)"
)
#: 单位后缀改写后的常量变量前缀(01 §5.5 量纲检查配套)
_QCONST_PREFIX = "qconst"


def _unit_dims(unit: str) -> Dimensions:
    """单位 → 表达式量纲(core/units.dims_of;未注册单位无量纲)。"""
    try:
        from iesplan.core.units import dims_of

        return dims_of(unit)
    except Exception:
        return _dim_of()


def rewrite_unit_suffixes(
    expr: str, var_dims: dict[str, Dimensions]
) -> tuple[str, dict[str, Dimensions], dict[str, float]]:
    """把 "1500 W" 类显式单位后缀改写为带量纲的 SI 数值常量。

    返回 (新表达式, 变量量纲, qconstN → SI 数值): "50 kW" → "qconst1"(量纲
    power, 数值 50000 W)。qconstN 在编译期按数值常量处理(参与量纲检查,
    求值期直接代入), 供 parse_expr 与装配检查器/配置校验共用(01 §5.5)。
    数值统一转为 SI: 表达式变量均为 SI 值(core/units 边界约定),
    常量不换算将造成 1000 倍量级偏差(codex 二次审核 High-4)。
    """
    from iesplan.core.units import ALIAS_MAP, UNITS, UnitError, to_si
    from iesplan.core.unitparse import parse_number

    counter = 0
    values: dict[str, float] = {}

    def repl(match: re.Match) -> str:
        nonlocal counter
        token = match.group(2)
        if ALIAS_MAP.get(token.lower()) is None and token not in UNITS:
            return match.group(0)  # 非已知单位,原样保留
        counter += 1
        name = f"{_QCONST_PREFIX}{counter}"
        var_dims[name] = _unit_dims(token)
        number = parse_number(match.group(1))  # 含科学计数/千分位/中文字数
        try:
            values[name] = to_si(number, token)
        except UnitError:
            # 非固定汇率币种(USD 等)禁止自动折算: 保持原数值(量纲仍参与检查,
            # 折算语义由汇率配置处理, 与 units.to_si 拒绝口径一致)
            values[name] = number
        return name

    new_expr = _UNIT_SUFFIX_RE.sub(repl, expr)
    return new_expr, var_dims, values


def parse_expr(
    text: str,
    allowed_vars: set[str] | frozenset[str],
    var_dims: dict[str, Dimensions] | None = None,
) -> CompiledExpr:
    """解析并编译受限表达式(04 §4.2 流水线 1-6 步的纯 Python 实现)。

    参数:
        text: 表达式源码(单行,如 "a * 2 + b / 3")。
        allowed_vars: 允许引用的变量集合(白名单,04 §4.1 EXPR-CODE-001)。
        var_dims: 可选,变量名 → 量纲(默认无量纲);用于量纲一致性检查。
            含 "1500 W" 类显式单位后缀的表达式在此改写为常量变量(01 §5.5)。

    返回:
        CompiledExpr;.eval(values: dict) → float。
    异常:
        ExpressionError 子类:
            ExpressionSyntaxError   语法错误(EXPR-SYN-001)
            ExpressionSecurityError 白名单外节点/函数/危险标识符(EXPR-SEC-001)
            ExpressionCodeError     变量不在 allowed_vars(EXPR-CODE-001)
            ExpressionDimensionError 量纲不匹配(EXPR-DIM-001)
            ExpressionTypeError     类型问题(幂指数非常数等,EXPR-TYP-001)
            ExpressionRangeError    常量/幂指数越界(EXPR-RNG-001)
    """
    if not text or not text.strip():
        raise ExpressionSyntaxError("空表达式", expr=text)

    # 显式单位后缀改写("1500 W" → qconstN, 量纲取自单位; 01 §5.5);
    # 改写在白名单构建之前完成, qconstN 为引擎内部带量纲常量, 白名单放行
    dims: dict[str, Dimensions] = dict(var_dims or {})
    text, dims, qconst_values = rewrite_unit_suffixes(text, dims)
    allowed = frozenset(allowed_vars) | {k for k in dims if k.startswith(_QCONST_PREFIX)}

    # 双扫描之一:危险标识符模式(混淆手段一律拒绝,04 §4.3 禁止列表)
    lowered = text.lower()
    for pat in FORBIDDEN_IDENT_PATTERNS:
        if pat in lowered:
            raise ExpressionSecurityError(f"表达式含被禁止的标识符模式 {pat!r}", pattern=pat, expr=text)

    # 语法归一化(在危险模式扫描之后进行,避免引入扫描误报):
    # 1) 04 §4.1 BNF 中幂运算符为 '^'(power := atom ('^' atom)?),先转为 Python 的 '**';
    # 2) BNF 的 if(cond, a, b) 是 Python 关键字,先改写为内部标记名,
    #    编译时再映射回 if 控制结构;仅改写"前导非标识符字符"的 if(。
    normalized = re.sub(r"(?<![A-Za-z0-9_])if\(", "__ies_if__(", text)
    normalized = normalized.replace("^", "**")

    try:
        tree = ast.parse(normalized, mode="eval")
    except SyntaxError as exc:
        raise ExpressionSyntaxError(
            "表达式语法错误",
            expr=text,
            line=exc.lineno or 0,
            col=exc.offset or 0,  # SyntaxError 的位置属性为 offset
        ) from None

    state = {"nodes": 0, "depth": 0}

    def compile_node(node: ast.AST, depth: int) -> tuple:
        nonlocal qconst_values
        state["nodes"] += 1
        if state["nodes"] > MAX_NODES:
            raise ExpressionRangeError("表达式节点数超过上限", limit=MAX_NODES, expr=text)
        if depth > MAX_DEPTH:
            raise ExpressionRangeError("表达式嵌套深度超过上限", limit=MAX_DEPTH, expr=text)

        if isinstance(node, ast.Expression):
            return compile_node(node.body, depth)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                return ("num", 1.0 if node.value else 0.0), _dim_of()
            if isinstance(node.value, (int, float)):
                v = float(node.value)
                if abs(v) > MAX_LITERAL_ABS:
                    raise ExpressionRangeError(
                        "常量字面量超出允许范围", value=v, limit=MAX_LITERAL_ABS, expr=text
                    )
                return ("num", v), _dim_of()
            raise ExpressionSecurityError("仅允许数值常量", literal_type=type(node.value).__name__, expr=text)
        if isinstance(node, ast.Name):
            if node.id.startswith("_"):
                raise ExpressionSecurityError("禁止引用下划线前缀标识符", variable=node.id, expr=text)
            if node.id.startswith(_QCONST_PREFIX):
                # 单位后缀常量("50 kW" → qconst1): 编译为带量纲数值常量
                val = qconst_values.get(node.id)
                if val is None:
                    raise ExpressionCodeError(
                        f"单位常量 {node.id} 缺失", variable=node.id, expr=text
                    )
                return ("num", val), dims.get(node.id, _dim_of())
            if node.id not in allowed:
                raise ExpressionCodeError(f"变量 {node.id} 未登记", variable=node.id, expr=text)
            return ("var", node.id), dims.get(node.id, _dim_of())
        if isinstance(node, ast.BinOp):
            op_type = type(node.op)
            if op_type not in _ARITH_OPS:
                raise ExpressionSecurityError(
                    f"不允许的二元运算 {op_type.__name__}", node=op_type.__name__, expr=text
                )
            left, dl = compile_node(node.left, depth + 1)
            r, dr = compile_node(node.right, depth + 1)
            op = _ARITH_OPS[op_type]
            if op in ("add", "sub"):
                if dl != dr:
                    raise ExpressionDimensionError(
                        f"加减两侧量纲不一致: {_dim_str(dl)} 与 {_dim_str(dr)}",
                        op=op,
                        left=_dim_str(dl),
                        right=_dim_str(dr),
                        expr=text,
                    )
                return ("bin", op, left, r), dl
            if op == "pow":
                if not isinstance(node.right, ast.Constant) or isinstance(node.right.value, bool):
                    raise ExpressionTypeError("幂指数必须为常数", op="pow", expr=text)
                e = float(node.right.value)
                if abs(e) > MAX_POW_EXPONENT:
                    raise ExpressionRangeError(
                        "幂指数超出范围", exponent=e, limit=MAX_POW_EXPONENT, expr=text
                    )
                return ("bin", op, left, r), _pow_dims(dl, e, text)
            # 乘除:量纲合并
            if op == "mul":
                return ("bin", op, left, r), dl + dr
            return ("bin", op, left, r), dl - dr
        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, (ast.USub, ast.UAdd)):
                raise ExpressionSecurityError(
                    f"不允许的一元运算 {type(node.op).__name__}", node=type(node.op).__name__, expr=text
                )
            x, d = compile_node(node.operand, depth + 1)
            return ("unary", "neg" if isinstance(node.op, ast.USub) else "pos", x), d
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1 or len(node.comparators) != 1:
                raise ExpressionSecurityError("不支持链式比较", expr=text)
            op_type = type(node.ops[0])
            if op_type not in _CMP_OPS:
                raise ExpressionSecurityError(
                    f"不允许的比较运算 {op_type.__name__}", node=op_type.__name__, expr=text
                )
            left, dl = compile_node(node.left, depth + 1)
            r, dr = compile_node(node.comparators[0], depth + 1)
            if dl != dr:
                raise ExpressionDimensionError(
                    f"比较两侧量纲不一致: {_dim_str(dl)} 与 {_dim_str(dr)}",
                    op="compare",
                    left=_dim_str(dl),
                    right=_dim_str(dr),
                    expr=text,
                )
            return ("cmp", _CMP_OPS[op_type], left, r), _dim_of()
        if isinstance(node, ast.BoolOp):
            if not isinstance(node.op, (ast.And, ast.Or)):
                raise ExpressionSecurityError(
                    f"不允许的逻辑运算 {type(node.op).__name__}", node=type(node.op).__name__, expr=text
                )
            items: list[tuple] = []
            for v in node.values:
                ir, d = compile_node(v, depth + 1)
                if d:
                    raise ExpressionDimensionError("逻辑运算要求无量纲操作数", dim=_dim_str(d), expr=text)
                items.append(ir)
            return ("bool", "and" if isinstance(node.op, ast.And) else "or", tuple(items)), _dim_of()
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ExpressionSecurityError("只允许直接调用白名单函数名", expr=text)
            fn = node.func.id
            if fn == "__ies_if__":
                fn = "if"  # 语法归一化标记还原为 if() 控制结构
            if fn not in WHITELIST_FUNCTIONS:
                raise ExpressionSecurityError(f"调用白名单外的函数 {fn}", fn=fn, expr=text)
            expected = _FUNC_ARG_COUNT[fn]
            if expected is not None and len(node.args) != expected:
                raise ExpressionTypeError(
                    f"函数 {fn} 需要 {expected} 个参数,实际 {len(node.args)}",
                    fn=fn,
                    expected=expected,
                    actual=len(node.args),
                    expr=text,
                )
            if fn in ("min", "max") and len(node.args) < 1:
                raise ExpressionTypeError(f"函数 {fn} 至少需要 1 个参数", fn=fn, expr=text)
            if node.keywords:
                raise ExpressionSecurityError("不支持关键字参数", expr=text)
            if fn == "pow":
                # 04 §4.3:pow 指数必须为常数,且受限(与 '**' 运算符同一规则)
                exp_node = node.args[1]
                if not isinstance(exp_node, ast.Constant) or isinstance(exp_node.value, bool):
                    raise ExpressionTypeError("pow 指数必须为常数", fn="pow", expr=text)
                e = float(exp_node.value)
                if abs(e) > MAX_POW_EXPONENT:
                    raise ExpressionRangeError(
                        "幂指数超出范围", exponent=e, limit=MAX_POW_EXPONENT, expr=text
                    )
            args: list[tuple] = []
            dims_list: list[Dimensions] = []
            for a in node.args:
                ir, d = compile_node(a, depth + 1)
                args.append(ir)
                dims_list.append(d)
            if fn == "if":
                # 编译为专用 ("if", cond, a, b) 节点,cond 惰性求值(只算选中的分支)
                return ("if", args[0], args[1], args[2]), _call_dims(fn, dims_list, text)
            return ("call", fn, tuple(args)), _call_dims(fn, dims_list, text)
        if isinstance(node, ast.IfExp):
            raise ExpressionSecurityError("不支持三目表达式,请使用 if(cond, a, b) 函数", expr=text)
        # 其余全部拒绝:属性访问/下标/列表/字典/集合/lambda/推导式/import 等
        raise ExpressionSecurityError(
            f"不允许的 AST 节点 {type(node).__name__}", node=type(node).__name__, expr=text
        )

    ir, _ = compile_node(tree, 0)
    return CompiledExpr(text, allowed, ir, state["nodes"], state["depth"])


def _pow_dims(d: Dimensions, e: float, text: str) -> Dimensions:
    """幂量纲:dims × 指数;指数非整数且量纲非空 → 拒绝。"""
    if not d:
        return _dim_of()
    if e != math.floor(e):
        raise ExpressionDimensionError("带量纲的底数只能取整数次幂", exponent=e, expr=text)
    return _dim_of({k: v * int(e) for k, v in d.items() if v * int(e) != 0})


def _call_dims(fn: str, dims_list: list[Dimensions], text: str) -> Dimensions:
    """按函数语义推导结果量纲(04 §4.3 返回量纲列)。"""
    if fn in ("abs", "min", "max"):
        first = dims_list[0]
        for d in dims_list[1:]:
            if d != first:
                raise ExpressionDimensionError(
                    f"函数 {fn} 的参数量纲不一致",
                    fn=fn,
                    expr=text,
                    left=_dim_str(first),
                    right=_dim_str(d),
                )
        return first
    if fn == "if":
        cond_d, a_d, b_d = dims_list
        if cond_d:
            raise ExpressionDimensionError("if 条件必须无量纲", dim=_dim_str(cond_d), expr=text)
        if a_d != b_d:
            raise ExpressionDimensionError(
                "if 分支量纲不一致", left=_dim_str(a_d), right=_dim_str(b_d), expr=text
            )
        return a_d
    if fn in ("sin", "cos", "tan", "exp", "log"):
        if dims_list[0]:
            raise ExpressionDimensionError(
                f"函数 {fn} 要求无量纲参数(角度/纯数)", fn=fn, dim=_dim_str(dims_list[0]), expr=text
            )
        return _dim_of()
    if fn == "sqrt":
        d = dims_list[0]
        if any(v % 2 != 0 for v in d.values()):
            raise ExpressionDimensionError("sqrt 要求偶次量纲", dim=_dim_str(d), expr=text)
        return _dim_of({k: v // 2 for k, v in d.items() if v // 2 != 0})
    if fn == "pow":
        if dims_list[1]:
            raise ExpressionDimensionError("pow 指数必须无量纲", dim=_dim_str(dims_list[1]), expr=text)
        return _dim_of()
    raise ExpressionSecurityError(f"调用白名单外的函数 {fn}", fn=fn, expr=text)  # 不可达
