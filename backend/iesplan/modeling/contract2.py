"""`ies.modeling.contribution` 2.0.0 纯协议: 设备 2.0 descriptor → 公共数学贡献。

消费 ``devices.contracts2.DeviceModelDocument``(2.0 设备描述符,已由
devices.parser2 校验),输出与求解器无关的公共数学贡献 contract: 变量、关系、
状态迁移、接口流与结果映射元数据(宪法 §4.3 + modules/modeling.md)。方程以
版本化公共 AST 表达,不 eval、不动态导入、无函数/模块路径。

方程词法与拓扑约束(``split_relation``/``reference_atoms``/``check_cycles``)
直接复用 core 公共语法契约 ``iesplan.core.equation_grammar``(devices 与
modeling 共用同一实现,不复制);本模块只在其上叠加建模语义(输出唯一性、
property 非时变、blind 不可引用、状态初值)与版本化公共 AST 的树构建。

**迁移边界**: 本模块是 modeling 2.0 切片的纯协议实现,不导入、不消费旧 1.0
的 ``ModelCommand``/``DeviceSpec``(modeling/command.py、devspec.py)与全局
命令注册表;旧 1.0 代码保持原样,由后续整体迁移切片删除。本模块不建立任何
设备 ID 分支或私有命令映射,方程语义随设备内容寻址。

依赖边界: 只消费 core(diagnostics/units/equation_grammar)与 devices 公开
descriptor(contracts2 纯类型);不访问数据库、网络、目录路径与旧注册表。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from iesplan.core.diagnostics import (
    SEVERITY_ERROR,
    Diagnostic,
    make_diag,
)
from iesplan.core.equation_grammar import (
    EquationSyntaxError,
    check_cycles,
    reference_atoms,
    split_relation,
)
from iesplan.core.units import UnitError, dims_of
from iesplan.devices.contracts2 import (
    DeviceModelDocument,
)

# ---------------------------------------------------------------------------
# 契约常量
# ---------------------------------------------------------------------------

CONTRACT_SCHEMA_ID = "ies.modeling.contribution"
CONTRACT_SCHEMA_VERSION = "2.0.0"

EQUATION_AST_ID = "ies.equation.ast"
EQUATION_AST_VERSION = "2.0.0"

#: 稳定 ID 模式(与 devices 2.0 一致)
_ID_PATTERN = re.compile(r"^[a-z0-9]+([._-][a-z0-9]+)*$")

#: 时间索引(``name[t]``、``name[t±k]``;纯 [t] 无偏移)。
#: 与 core.equation_grammar 的公共语法一致;此处仅用于版本化公共 AST 的
#: 树构建(词法校验已由 reference_atoms 完成)。
_TIME_INDEX_PATTERN = re.compile(r"\[t\s*(?:([-+])\s*([0-9]+))?\]")

#: 规范序列化(稳定键序、紧凑、非 ASCII 保留、禁 NaN/Infinity)
_CANONICAL_KWARGS: dict[str, Any] = {
    "ensure_ascii": False,
    "sort_keys": True,
    "separators": (",", ":"),
    "allow_nan": False,
}

#: 方程语言允许的运算符
_BINARY_OPS = ("+", "-", "*", "/", "%", "^")
_PRECEDENCE = {"+": 1, "-": 1, "*": 2, "/": 2, "%": 2, "^": 3}

# ---------------------------------------------------------------------------
# 诊断码与消息键(mod 域;码已在 core/diagnostics.py::NEW_DIAG_CODES 登记)
# ---------------------------------------------------------------------------

MOD_EQ_SYNTAX = "MOD-EQ-001"            # 表达式语法非法
MOD_EQ_UNKNOWN_REF = "MOD-EQ-002"       # 引用未知标识符
MOD_EQ_UNIT_CONFLICT = "MOD-EQ-003"     # 量纲/单位冲突
MOD_EQ_CYCLE = "MOD-EQ-004"             # 循环引用
MOD_EQ_OUTPUT_CONFLICT = "MOD-EQ-005"   # 关系输出冲突(多次定义/非法输出)
MOD_EQ_STATE_NO_INITIAL = "MOD-EQ-006"  # 状态变量缺少 initial 初值
MOD_EQ_PROPERTY_INDEXED = "MOD-EQ-007"  # 非时变 property 被时间索引引用
MOD_EQ_BLIND_REF = "MOD-EQ-008"         # 方程引用 blind 接口

MOD_MESSAGE_KEYS: dict[str, str] = {
    MOD_EQ_SYNTAX: "ies.diag.mod.eq_syntax",
    MOD_EQ_UNKNOWN_REF: "ies.diag.mod.eq_unknown_ref",
    MOD_EQ_UNIT_CONFLICT: "ies.diag.mod.eq_unit_conflict",
    MOD_EQ_CYCLE: "ies.diag.mod.eq_cycle",
    MOD_EQ_OUTPUT_CONFLICT: "ies.diag.mod.eq_output_conflict",
    MOD_EQ_STATE_NO_INITIAL: "ies.diag.mod.eq_state_no_initial",
    MOD_EQ_PROPERTY_INDEXED: "ies.diag.mod.eq_property_indexed",
    MOD_EQ_BLIND_REF: "ies.diag.mod.eq_blind_ref",
}
MOD_FIX_HINT_KEYS: dict[str, str] = {
    code: "ies.fix.mod." + key[len("ies.diag.mod."):]
    for code, key in MOD_MESSAGE_KEYS.items()
}


def _diag(
    code: str,
    detail: str,
    *,
    relation_id: str | None = None,
    name: str | None = None,
    blocking: bool = True,
) -> Diagnostic:
    """构造 mod 域诊断(字段路径定位到 relation/name)。"""
    location: dict[str, object] = {"object_type": "device-equations"}
    if relation_id is not None:
        location["relation"] = relation_id
    if name is not None:
        location["name"] = name
    return make_diag(
        code,
        severity=SEVERITY_ERROR,
        message_key=MOD_MESSAGE_KEYS[code],
        fix_hint_key=MOD_FIX_HINT_KEYS[code],
        blocking=blocking,
        params={"detail": detail},
        location=location,
    )


# ---------------------------------------------------------------------------
# 版本化公共方程 AST(不 eval;确定性序列化见 ``ast_to_dict``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimeIndexedRef:
    """一次标识符引用(``name[t]`` / ``name[t-k]``;offset ≤ 0)。"""

    name: str
    offset: int = 0


@dataclass(frozen=True, slots=True)
class NumberNode:
    value: float


@dataclass(frozen=True, slots=True)
class RefNode:
    name: str
    offset: int = 0


@dataclass(frozen=True, slots=True)
class UnaryNode:
    op: str
    operand: object


@dataclass(frozen=True, slots=True)
class BinaryNode:
    op: str
    left: object
    right: object


@dataclass(frozen=True, slots=True)
class RelationAst:
    """一条关系的版本化公共 AST(``lhs = rhs``;output 为左侧唯一输出变量)。"""

    id: str
    output: str
    lhs_root: object | None
    rhs_root: object | None
    lhs_refs: tuple[TimeIndexedRef, ...]
    rhs_refs: tuple[TimeIndexedRef, ...]


class _ExprError(Exception):
    """表达式词法/语法错误(携带关系 ID 与中文说明)。"""

    def __init__(self, relation_id: str, detail: str) -> None:
        super().__init__(detail)
        self.relation_id = relation_id
        self.detail = detail


def _tokenize(expr: str, relation_id: str) -> list[tuple[str, Any]]:
    """受限表达式 → token 流(词法规则与 core.equation_grammar.reference_atoms
    的公共语法一致;本函数用于版本化公共 AST 的树构建,词法校验已由
    reference_atoms 在调用前完成)。"""
    tokens: list[tuple[str, Any]] = []
    pos, n = 0, len(expr)
    while pos < n:
        ch = expr[pos]
        if ch.isspace():
            pos += 1
            continue
        if ch.isdigit():
            m = re.match(r"[0-9]+(?:\.[0-9]+)?", expr[pos:])
            assert m is not None
            text = m.group(0)
            end = pos + len(text)
            if end < n and (expr[end].isalnum() or expr[end] in "_."):
                raise _ExprError(relation_id, f"数字字面量 {text!r} 后跟非法字符 {expr[end]!r}")
            tokens.append(("number", float(text)))
            pos = end
            continue
        if ch in "()":
            tokens.append(("paren", ch))
            pos += 1
            continue
        if ch in _BINARY_OPS:
            tokens.append(("op", ch))
            pos += 1
            continue
        m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", expr[pos:])
        if m is not None:
            name = m.group(0)
            end = pos + len(name)
            offset = 0
            if end < n and expr[end] == "[":
                tm = _TIME_INDEX_PATTERN.match(expr, end)
                if tm is None:
                    raise _ExprError(
                        relation_id, f"标识符 {name!r} 的索引必须是 [t]、[t+k] 或 [t-k]"
                    )
                if tm.group(1) is not None:
                    offset = int(tm.group(2))
                    if tm.group(1) == "-":
                        offset = -offset
                    if offset > 0:
                        raise _ExprError(relation_id, f"禁止未来引用 {name}[t+{offset}]")
                end = tm.end()
            tokens.append(("ident", (name, offset)))
            pos = end
            continue
        raise _ExprError(
            relation_id,
            f"表达式含非法字符 {ch!r}(只允许标识符、数字、运算符与 [t±k] 索引)",
        )
    return tokens


def _parse_side(tokens: list[tuple[str, Any]], relation_id: str) -> object | None:
    """Pratt 递归下降解析单侧表达式 → AST 根节点(grammar 见模块 docstring)。"""
    if not tokens:
        return None
    index = [0]

    def peek() -> tuple[str, Any] | None:
        return tokens[index[0]] if index[0] < len(tokens) else None

    def next_token() -> tuple[str, Any]:
        tok = tokens[index[0]]
        index[0] += 1
        return tok

    def parse_primary() -> object:
        tok = peek()
        if tok is None:
            raise _ExprError(relation_id, "表达式不完整(意外结尾)")
        kind, value = tok
        if kind == "op" and value in ("+", "-"):
            next_token()
            return UnaryNode(op=value, operand=parse_primary())
        if kind == "number":
            next_token()
            return NumberNode(value=value)
        if kind == "ident":
            next_token()
            name, offset = value
            nxt = peek()
            if nxt is not None and nxt[0] == "paren" and nxt[1] == "(":
                raise _ExprError(relation_id, f"禁止函数调用形式 {name!r}(…)(方程语言无函数)")
            return RefNode(name=name, offset=offset)
        if kind == "paren" and value == "(":
            next_token()
            node = parse_expr(0)
            nxt = peek()
            if nxt is None or not (nxt[0] == "paren" and nxt[1] == ")"):
                raise _ExprError(relation_id, "缺少右括号")
            next_token()
            return node
        raise _ExprError(relation_id, f"意外的记号 {tok!r}")

    def parse_expr(min_prec: int) -> object:
        left = parse_primary()
        while True:
            tok = peek()
            if tok is None or tok[0] != "op" or tok[1] not in _BINARY_OPS:
                break
            op = tok[1]
            prec = _PRECEDENCE[op]
            if prec < min_prec:
                break
            next_token()
            right = parse_expr(prec if op == "^" else prec + 1)
            left = BinaryNode(op=op, left=left, right=right)
        return left

    root = parse_expr(0)
    if index[0] != len(tokens):
        raise _ExprError(relation_id, f"多余记号: {tokens[index[0]]!r}")
    return root


def ast_to_dict(node: object | None) -> Any:
    """AST 节点 → 确定性 JSON 兼容 dict(供规范文本/回执序列化)。"""
    if isinstance(node, NumberNode):
        return {"number": node.value}
    if isinstance(node, RefNode):
        return {"ref": node.name, "offset": node.offset}
    if isinstance(node, UnaryNode):
        return {"unary": node.op, "operand": ast_to_dict(node.operand)}
    if isinstance(node, BinaryNode):
        return {
            "binary": node.op,
            "left": ast_to_dict(node.left),
            "right": ast_to_dict(node.right),
        }
    return None


# ---------------------------------------------------------------------------
# 公共数学贡献 contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MathVariable:
    """命名数学量: property 常量(携带技术值)或 equations 内部变量。"""

    name: str
    kind: str  # property | variable
    unit: str
    valid_range: tuple[float | None, float | None] | None = None
    value: float | bool | str | None = None  # property 常量值;变量为 None
    is_state: bool = False


@dataclass(frozen=True, slots=True)
class InterfaceFlow:
    """接口流: 五类接口的序列语义、载体、业务单位与值域。"""

    id: str
    type: str  # in/out/bidirectional/predefined/blind
    carrier: str
    unit: str
    valid_range: tuple[float | None, float | None]
    source_mode: str | None = None  # predefined: constant/data_repeat/data_predict


@dataclass(frozen=True, slots=True)
class MathRelation:
    """关系式(版本化 AST + 输出变量 + 状态迁移标记)。"""

    id: str
    output: str
    ast: RelationAst
    is_state_transition: bool = False


@dataclass(frozen=True, slots=True)
class ResultMappingEntry:
    """结果映射元数据: 关系输出 → 业务单位(供 ResultAdapter 反向换算)。"""

    variable: str
    unit: str
    kind: str  # interface | variable


@dataclass(frozen=True, slots=True)
class DeviceMathContribution:
    """设备级公共数学贡献(不可变;确定性摘要)。

    - variables:   property 常量与内部变量(索引域=时间轴);
    - interfaces:  五类接口流;
    - relations:   方程关系(版本化公共 AST);
    - states:      状态变量清单(带 initial 或自引用递推);
    - results:     结果字段与反向单位映射元数据。
    """

    device_id: str
    content_sha256: str
    equation_ast_id: str = EQUATION_AST_ID
    equation_ast_version: str = EQUATION_AST_VERSION
    contract_schema_id: str = CONTRACT_SCHEMA_ID
    contract_schema_version: str = CONTRACT_SCHEMA_VERSION
    variables: Mapping[str, MathVariable] = field(default_factory=dict)
    interfaces: Mapping[str, InterfaceFlow] = field(default_factory=dict)
    relations: tuple[MathRelation, ...] = ()
    states: tuple[str, ...] = ()
    results: tuple[ResultMappingEntry, ...] = ()
    canonical_text: str = ""
    contribution_sha256: str = ""

    def verify(self) -> bool:
        """重算摘要核对规范文本与声明摘要一致(确定性证据)。"""
        return bool(self.contribution_sha256) and hashlib.sha256(
            self.canonical_text.encode("utf-8")
        ).hexdigest() == self.contribution_sha256


@dataclass(slots=True)
class MathContributionResult:
    """转换结果: 要么有完整贡献(含规范摘要),要么有阻断诊断列表。"""

    diagnostics: list[Diagnostic] = field(default_factory=list)
    contribution: DeviceMathContribution | None = None

    @property
    def ok(self) -> bool:
        return self.contribution is not None and not any(d.blocking for d in self.diagnostics)


def contribution_to_dict(contribution: DeviceMathContribution) -> dict[str, Any]:
    """贡献 → 确定性 dict(规范序列化输入)。"""
    return {
        "schema": contribution.contract_schema_id,
        "schema_version": contribution.contract_schema_version,
        "device_id": contribution.device_id,
        "content_sha256": contribution.content_sha256,
        "equation_ast": {
            "id": contribution.equation_ast_id,
            "version": contribution.equation_ast_version,
        },
        "variables": {
            name: {
                "kind": v.kind,
                "unit": v.unit,
                "valid_range": (
                    {"minimum": v.valid_range[0], "maximum": v.valid_range[1]}
                    if v.valid_range is not None
                    else None
                ),
                **({"value": v.value} if v.kind == "property" and v.value is not None else {}),
                "is_state": v.is_state,
            }
            for name, v in sorted(contribution.variables.items())
        },
        "interfaces": {
            iid: {
                "type": flow.type,
                "carrier": flow.carrier,
                "unit": flow.unit,
                "valid_range": {"minimum": flow.valid_range[0], "maximum": flow.valid_range[1]},
                **({"source_mode": flow.source_mode} if flow.source_mode is not None else {}),
            }
            for iid, flow in sorted(contribution.interfaces.items())
        },
        "relations": [
            {
                "id": rel.id,
                "output": rel.output,
                "is_state_transition": rel.is_state_transition,
                "lhs_refs": [{"name": r.name, "offset": r.offset} for r in rel.ast.lhs_refs],
                "rhs_refs": [{"name": r.name, "offset": r.offset} for r in rel.ast.rhs_refs],
                "lhs": ast_to_dict(rel.ast.lhs_root),
                "rhs": ast_to_dict(rel.ast.rhs_root),
            }
            for rel in sorted(contribution.relations, key=lambda r: r.id)
        ],
        "states": list(contribution.states),
        "results": [
            {"variable": r.variable, "unit": r.unit, "kind": r.kind}
            for r in sorted(contribution.results, key=lambda r: r.variable)
        ],
    }


def canonical_bytes(contribution: DeviceMathContribution) -> bytes:
    """版本化规范字节(稳定键序、紧凑 JSON、UTF-8;禁 NaN/Infinity)。"""
    return json.dumps(contribution_to_dict(contribution), **_CANONICAL_KWARGS).encode("utf-8")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def build_math_contribution(
    document: DeviceModelDocument,
    *,
    source_name: str = "<device>",
) -> MathContributionResult:
    """消费 2.0 descriptor → 公共数学贡献(确定性;失败返回阻断诊断)。

    方程词法与拓扑约束(拆分/引用白名单/时间索引/未来引用/循环引用)复用
    core 公共语法契约 equation_grammar,并叠加建模语义检查: 输出唯一性、
    property 非时变、blind 不可引用、状态初值完整性与量纲兼容。全部通过后
    生成规范贡献文本与 SHA-256。
    """
    diags: list[Diagnostic] = []
    device_id = document.device.id if document.device is not None else ""
    if not device_id:
        diags.append(_diag(MOD_EQ_SYNTAX, "文档缺少 device.id", name="device"))

    properties = document.properties
    interfaces = document.interfaces
    eq_vars = document.equations.variables

    # ---- 引用表: name → 类别 ----
    kinds: dict[str, str] = {pid: "property" for pid in properties}
    iface_type: dict[str, str] = {iid: iface.type for iid, iface in interfaces.items()}
    for iid in interfaces:
        kinds[iid] = "interface"
    for vid in eq_vars:
        kinds[vid] = "variable"

    # ---- 逐关系: 公共语法契约词法 → AST 构建 → 语义校验(聚合独立问题) ----
    relations: list[MathRelation] = []
    relation_ids: set[str] = set()
    outputs: dict[str, str] = {}  # output 变量名 → relation id(唯一性)
    state_refs: dict[str, bool] = {}  # 变量 → 是否自引用 [t-k]
    for raw_rel in document.equations.relations:
        rid = raw_rel.id
        if rid in relation_ids:
            diags.append(_diag(MOD_EQ_SYNTAX, f"relation ID 重复: {rid!r}", name=rid))
            continue
        relation_ids.add(rid)
        expr = raw_rel.expression
        try:
            lhs_text, rhs_text = split_relation(expr)
            lhs_atoms, lhs_offsets = reference_atoms(lhs_text, rid)
            rhs_atoms, rhs_offsets = reference_atoms(rhs_text, rid)
            lhs_root = _parse_side(_tokenize(lhs_text, rid), rid)
            rhs_root = _parse_side(_tokenize(rhs_text, rid), rid)
        except (EquationSyntaxError, _ExprError) as exc:
            detail = exc.detail if isinstance(exc, _ExprError) else str(exc)
            diags.append(_diag(MOD_EQ_SYNTAX, f"relation {rid!r} 表达式非法: {detail}", relation_id=rid))
            continue
        lhs_refs = [TimeIndexedRef(name=n, offset=o) for n, o in zip(lhs_atoms, lhs_offsets, strict=True)]
        rhs_refs = [TimeIndexedRef(name=n, offset=o) for n, o in zip(rhs_atoms, rhs_offsets, strict=True)]

        # 引用白名单 + 语义约束(单条关系内互相独立,逐项聚合)
        for ref in lhs_refs + rhs_refs:
            kind = kinds.get(ref.name)
            if kind is None:
                diags.append(_diag(
                    MOD_EQ_UNKNOWN_REF,
                    f"relation {rid!r} 引用了未声明的标识符: {ref.name!r}",
                    relation_id=rid, name=ref.name,
                ))
                continue
            if kind == "property" and ref.offset != 0:
                diags.append(_diag(
                    MOD_EQ_PROPERTY_INDEXED,
                    f"relation {rid!r} 对非时变 property {ref.name!r} 使用时间索引",
                    relation_id=rid, name=ref.name,
                ))
            if kind == "interface" and iface_type.get(ref.name) == "blind":
                diags.append(_diag(
                    MOD_EQ_BLIND_REF,
                    f"relation {rid!r} 引用了 blind 接口 {ref.name!r}(不连接、不接收数据)",
                    relation_id=rid, name=ref.name,
                ))
        # 左侧必须恰好一个输出变量
        if len(lhs_refs) != 1:
            diags.append(_diag(
                MOD_EQ_SYNTAX,
                f"relation {rid!r} 左侧必须恰好一个变量(当前 {len(lhs_refs)} 个)",
                relation_id=rid,
            ))
            continue
        output = lhs_refs[0].name
        out_kind = kinds.get(output)
        if out_kind == "property":
            diags.append(_diag(
                MOD_EQ_OUTPUT_CONFLICT,
                f"relation {rid!r} 左侧是常量 property {output!r}(只能定义 interface 或内部变量)",
                relation_id=rid, name=output,
            ))
            continue
        if out_kind == "interface" and iface_type.get(output) == "predefined":
            diags.append(_diag(
                MOD_EQ_OUTPUT_CONFLICT,
                f"relation {rid!r} 左侧是 predefined 接口 {output!r}(由预定义数据源决定)",
                relation_id=rid, name=output,
            ))
            continue
        if output in outputs:
            diags.append(_diag(
                MOD_EQ_OUTPUT_CONFLICT,
                f"变量 {output!r} 被多个关系定义: {outputs[output]!r} 与 {rid!r}",
                relation_id=rid, name=output,
            ))
            continue
        outputs[output] = rid
        # 状态自引用: 输出变量在自身 rhs 以 [t-k] 出现 → 需要 initial
        if out_kind == "variable":
            self_negative = any(
                ref.name == output and ref.offset < 0 for ref in rhs_refs
            )
            if self_negative:
                state_refs[output] = True
        relations.append(MathRelation(
            id=rid,
            output=output,
            ast=RelationAst(
                id=rid,
                output=output,
                lhs_root=lhs_root,
                rhs_root=rhs_root,
                lhs_refs=tuple(lhs_refs),
                rhs_refs=tuple(rhs_refs),
            ),
        ))

    # ---- 变量间循环引用(core.equation_grammar.check_cycles;忽略自环) ----
    if not any(d.blocking for d in diags):
        edges = [
            (rel.output, tuple(r.name for r in rel.ast.rhs_refs))
            for rel in relations
        ]
        try:
            check_cycles(eq_vars, edges)
        except EquationSyntaxError as exc:
            diags.append(_diag(MOD_EQ_CYCLE, str(exc)))

    # ---- 状态变量: 初值完整性与量纲兼容 ----
    for vid, vspec in sorted(eq_vars.items()):
        initial = vspec.initial_property_ref
        is_state = initial is not None or state_refs.get(vid, False)
        if is_state and initial is None:
            diags.append(_diag(
                MOD_EQ_STATE_NO_INITIAL,
                f"状态变量 {vid!r} 自引用 [t-k] 但未声明 initial",
                name=vid,
            ))
        elif initial is not None:
            prop = properties.get(initial)
            if prop is None:
                diags.append(_diag(
                    MOD_EQ_UNKNOWN_REF,
                    f"变量 {vid!r} 的 initial.property_ref 未声明: {initial!r}",
                    name=vid,
                ))
            else:
                try:
                    if dims_of(vspec.unit) != dims_of(prop.unit):
                        diags.append(_diag(
                            MOD_EQ_UNIT_CONFLICT,
                            f"状态变量 {vid!r} 单位 {vspec.unit!r} 与 initial property "
                            f"{initial!r} 单位 {prop.unit!r} 量纲不兼容",
                            name=vid,
                        ))
                except UnitError as exc:
                    diags.append(_diag(MOD_EQ_UNIT_CONFLICT, str(exc), name=vid))

    if any(d.blocking for d in diags):
        return MathContributionResult(diagnostics=diags, contribution=None)

    # ---- 组装贡献 ----
    variables: dict[str, MathVariable] = {}
    for pid, p in sorted(properties.items()):
        variables[pid] = MathVariable(
            name=pid, kind="property", unit=p.unit, valid_range=p.valid_range, value=p.value
        )
    for vid, v in sorted(eq_vars.items()):
        variables[vid] = MathVariable(
            name=vid,
            kind="variable",
            unit=v.unit,
            valid_range=v.valid_range,
            is_state=v.initial_property_ref is not None or state_refs.get(vid, False),
        )
    interfaces_out: dict[str, InterfaceFlow] = {
        iid: InterfaceFlow(
            id=iid,
            type=iface.type,
            carrier=iface.carrier,
            unit=iface.unit,
            valid_range=iface.valid_range,
            source_mode=iface.source.mode if iface.source is not None else None,
        )
        for iid, iface in sorted(interfaces.items())
    }
    states = tuple(
        vid for vid in sorted(variables)
        if variables[vid].kind == "variable" and variables[vid].is_state
    )
    results = tuple(
        ResultMappingEntry(
            variable=rel.output,
            unit=variables[rel.output].unit if rel.output in variables else interfaces[rel.output].unit,
            kind="variable" if rel.output in variables else "interface",
        )
        for rel in sorted(relations, key=lambda r: r.id)
    )
    contribution = DeviceMathContribution(
        device_id=device_id,
        content_sha256=_document_sha256(document),
        variables=variables,
        interfaces=interfaces_out,
        relations=tuple(
            MathRelation(
                id=rel.id,
                output=rel.output,
                ast=rel.ast,
                is_state_transition=rel.output in states,
            )
            for rel in relations
        ),
        states=states,
        results=results,
    )
    text = canonical_bytes(contribution).decode("utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    contribution = DeviceMathContribution(
        device_id=contribution.device_id,
        content_sha256=contribution.content_sha256,
        variables=contribution.variables,
        interfaces=contribution.interfaces,
        relations=contribution.relations,
        states=contribution.states,
        results=contribution.results,
        canonical_text=text,
        contribution_sha256=digest,
    )
    assert contribution.verify()
    return MathContributionResult(diagnostics=diags, contribution=contribution)


def _document_sha256(document: DeviceModelDocument) -> str:
    """设备内容摘要(与 devices.contracts2 规范一致)。"""
    from iesplan.devices.contracts2 import content_sha256

    return content_sha256(document)


__all__ = [
    "CONTRACT_SCHEMA_ID",
    "CONTRACT_SCHEMA_VERSION",
    "EQUATION_AST_ID",
    "EQUATION_AST_VERSION",
    # AST
    "TimeIndexedRef",
    "NumberNode",
    "RefNode",
    "UnaryNode",
    "BinaryNode",
    "RelationAst",
    "ast_to_dict",
    # 贡献
    "MathVariable",
    "InterfaceFlow",
    "MathRelation",
    "ResultMappingEntry",
    "DeviceMathContribution",
    "MathContributionResult",
    "contribution_to_dict",
    "canonical_bytes",
    "build_math_contribution",
    # 诊断码
    "MOD_EQ_SYNTAX",
    "MOD_EQ_UNKNOWN_REF",
    "MOD_EQ_UNIT_CONFLICT",
    "MOD_EQ_CYCLE",
    "MOD_EQ_OUTPUT_CONFLICT",
    "MOD_EQ_STATE_NO_INITIAL",
    "MOD_EQ_PROPERTY_INDEXED",
    "MOD_EQ_BLIND_REF",
]
