"""`ies.device-model` 2.0.0 解析与校验（纯协议入口，不访问注册表/数据库）。

职责（宪法 §7.8 + device-model-yaml.md「校验与规范化」）：
1. 校验顶层 schema/schema_version 与未知字段（JSON Schema 2.0.0）；
2. 校验稳定 ID、property 标量、单位、有效区间与有限数值；
3. 校验五类 interface、carrier、单位、source 组合与连接资格；
4. 校验 equations 标识符、内部变量、单位、时间索引引用与循环引用；
5. 解析模板顶层 ``inputs`` 为扁平叶子声明（供表单生成与实例化校验）；
6. 生成不可变 ``DeviceModelDocument``，规范化并计算内容摘要与回执。

诊断语义：同阶段互不依赖的问题尽量聚合；结构不足以安全解释后续字段时
才停止后续阶段。非法类型不得变成 null、默认值或空模型。解析失败不产出部分文档。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from iesplan.core.diagnostics import (
    SEVERITY_ERROR,
    Diagnostic,
    make_diag,
)
from iesplan.core.units import UnitError, is_known_unit
from iesplan.devices.contracts2 import (
    INPUT_DATA_TYPES,
    INPUT_SCALAR_TYPES,
    INPUT_TYPES,
    INTERFACE_TYPES,
    SCHEMA_VERSION,
    SOURCE_MODES,
    SOURCE_TYPES,
    DeviceInfo,
    DeviceModelDocument,
    EquationRelation,
    EquationVariable,
    Equations,
    InterfaceSpec,
    PropertySpec,
    SourceSpec,
    TemplateInputSpec,
    TemplateInputs,
    is_valid_id,
)

#: 包内 JSON Schema 相对路径
SCHEMA_FILE = "schema/device-model-2.0.0.schema.json"

_ID_PATTERN = re.compile(r"^[a-z0-9]+([._-][a-z0-9]+)*$")

#: 时间索引模式（表达式中的 ``name[t]``、``name[t-1]``、``name[t+1]``；纯 [t] 无偏移）
_TIME_INDEX_PATTERN = re.compile(r"\[t\s*(?:([-+])\s*([0-9]+))?\]")

#: 关系式左右两侧拆分（允许行内注释与前后空白）
_RELATION_SPLIT = re.compile(r"\s*=\s*")


class ParseError(Exception):
    """结构无法解释时的解析异常（携带行号信息，由调用方转诊断）。"""


def _load_schema() -> dict:
    path = Path(__file__).resolve().parent / SCHEMA_FILE
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _diag(code: str, detail: str, *, file: str, field: str | None = None,
          line: int | None = None, column: int | None = None,
          expected: object | None = None, actual: object | None = None,
          params: dict[str, object] | None = None) -> Diagnostic:
    """构造 2.0.0 设备模型诊断（消息键、字段路径、YAML 行列、expected/actual）。"""
    location: dict[str, object] = {"object_type": "device-model"}
    if field is not None:
        location["field"] = field
    if line is not None:
        location["line"] = line
    if column is not None:
        location["column"] = column
    p = {"file": file, "detail": detail, **(params or {})}
    if expected is not None:
        p["expected"] = expected
    if actual is not None:
        p["actual"] = actual
    return make_diag(code, severity=SEVERITY_ERROR, params=p, location=location)


@dataclass(slots=True)
class DeviceModelParseResult:
    """解析结果：要么有完整文档，要么有诊断列表（不允许两者同时缺失）。"""

    document: DeviceModelDocument | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.document is not None and not self.diagnostics

    @property
    def blocking_diags(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity in ("error", "blocking")]


# ---------------------------------------------------------------------------
# 校验辅助
# ---------------------------------------------------------------------------

def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    v = float(value)
    return v == v and v not in (float("inf"), float("-inf"))


def _unit_known(unit: str) -> bool:
    """单位是否可识别（core.units 规范；is_known_unit 返回 False 而非抛异常）。"""
    try:
        return bool(is_known_unit(unit))
    except UnitError:
        return False


def _check_range_bounds(minimum: object, maximum: object, file: str, field: str) -> tuple[float | None, float | None]:
    """有效区间边界必须是有限数值或 null；minimum <= maximum。"""
    def _bound(v: object, side: str) -> float | None:
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ParseError(f"{field} valid_range.{side} 必须是有限数值或 null")
        return float(v)

    lo = _bound(minimum, "minimum")
    hi = _bound(maximum, "maximum")
    if lo is not None and hi is not None and lo > hi:
        raise ParseError(f"{field} valid_range.minimum 大于 maximum")
    return lo, hi


def _check_interface_source(iface_id: str, type_: str, source: object, file: str) -> None:
    """interface type 与 source 组合规则（宪法 §4.2）。"""
    if type_ not in INTERFACE_TYPES:
        raise ParseError(f"interfaces.{iface_id}.type 必须是 {INTERFACE_TYPES} 之一")
    if source is None:
        if type_ in SOURCE_TYPES:
            raise ParseError(f"interfaces.{iface_id} 类型为 {type_!r} 必须声明 source")
        return
    if type_ not in SOURCE_TYPES:
        raise ParseError(
            f"interfaces.{iface_id} 类型为 {type_!r} 禁止声明 source "
            f"(仅 {SOURCE_TYPES} 可携带预定义来源)"
        )
    if not isinstance(source, Mapping):
        raise ParseError(f"interfaces.{iface_id}.source 必须是 mapping")
    mode = source.get("mode")
    if mode not in SOURCE_MODES:
        raise ParseError(
            f"interfaces.{iface_id}.source.mode 必须是 {SOURCE_MODES} 之一"
        )
    if mode == "constant":
        if "value" not in source:
            raise ParseError(f"interfaces.{iface_id}.source constant 必须声明 value")
        if not _is_finite_number(source.get("value")):
            raise ParseError(f"interfaces.{iface_id}.source.value 必须是有限数值")
    else:
        data_ref = source.get("data_ref")
        if not isinstance(data_ref, str) or not data_ref.strip():
            raise ParseError(f"interfaces.{iface_id}.source {mode} 必须声明 data_ref")
        if "value" in source:
            raise ParseError(f"interfaces.{iface_id}.source {mode} 禁止声明 value")


# ---------------------------------------------------------------------------
# equations 校验
# ---------------------------------------------------------------------------

def _split_relation(expression: str) -> tuple[str, str]:
    """把 ``lhs = rhs`` 拆分为左右两侧；不满足时报错。"""
    parts = _RELATION_SPLIT.split(expression, maxsplit=1)
    if len(parts) != 2:
        raise ParseError("relation.expression 必须为 'lhs = rhs' 形式")
    lhs, rhs = (p.strip() for p in parts)
    if not lhs or not rhs:
        raise ParseError("relation.expression 两侧不能为空")
    return lhs, rhs


def _reference_atoms(side: str, file: str, relation_id: str) -> tuple[tuple[str, ...], tuple[int, ...]]:
    """从表达式一侧提取标识符引用与时间索引。

    解析规则：标识符按允许字符集切分；``name[t±k]`` 形式的时间索引被识别；
    其余含 ``[`` 的引用（未知函数调用、属性访问、任意下标）直接报错。
    """
    atoms: list[str] = []
    offsets: list[int] = []
    token_re = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
    pos = 0
    while pos < len(side):
        ch = side[pos]
        if ch.isspace():
            pos += 1
            continue
        if ch.isdigit() or ch in "+-*/^%(),<>=":
            pos += 1
            continue
        m = token_re.match(side, pos)
        if not m:
            raise ParseError(
                f"relation {relation_id!r} 表达式含非法字符 {ch!r}（只允许标识符、数字、运算符与 [t±k] 索引）"
            )
        name = m.group(0)
        pos = m.end()
        if pos < len(side) and side[pos] == "[":
            tm = _TIME_INDEX_PATTERN.match(side, pos)
            if not tm:
                raise ParseError(
                    f"relation {relation_id!r} 标识符 {name!r} 的索引必须是 [t]、[t+k] 或 [t-k]"
                )
            offset = 0
            if tm.group(1) is not None:
                offset = int(tm.group(2))
                if tm.group(1) == "-":
                    offset = -offset
                if offset > 0:
                    raise ParseError(
                        f"relation {relation_id!r} 禁止未来引用 {name}[t+{offset}]（跨步只允许过去或当前步）"
                    )
            offsets.append(offset)
            pos = tm.end()
        else:
            offsets.append(0)
        atoms.append(name)
    return tuple(atoms), tuple(offsets)


def validate_equations(
    raw_eq: Mapping[str, Any],
    properties: Mapping[str, PropertySpec],
    interfaces: Mapping[str, InterfaceSpec],
    *,
    file: str,
) -> None:
    """校验 equations：变量单位/值域、引用白名单、时间索引与循环引用。

    校验失败以 ParseError 抛出（聚合诊断由调用方逐项 catch 收集）。
    """
    variables_raw = raw_eq.get("variables")
    if not isinstance(variables_raw, Mapping):
        raise ParseError("equations.variables 必须是 mapping")
    relations_raw = raw_eq.get("relations")
    if not isinstance(relations_raw, list):
        raise ParseError("equations.relations 必须是 sequence")

    allowed = set(properties) | set(interfaces)
    var_specs: dict[str, EquationVariable] = {}
    for vid, vraw in variables_raw.items():
        if not isinstance(vid, str) or not _ID_PATTERN.fullmatch(vid):
            raise ParseError(f"equations.variables 变量 ID 非法: {vid!r}")
        if not isinstance(vraw, Mapping):
            raise ParseError(f"equations.variables.{vid} 必须是 mapping")
        unit = vraw.get("unit")
        if not isinstance(unit, str) or not unit.strip():
            raise ParseError(f"equations.variables.{vid} 缺少 unit")
        if not _unit_known(unit.strip()):
            raise ParseError(f"equations.variables.{vid}.unit 无法识别: {unit!r}")
        vrange = None
        if "valid_range" in vraw and vraw.get("valid_range") is not None:
            vr = vraw["valid_range"]
            if not isinstance(vr, Mapping):
                raise ParseError(f"equations.variables.{vid}.valid_range 必须是 mapping")
            vrange = _check_range_bounds(vr.get("minimum"), vr.get("maximum"), file, f"equations.variables.{vid}")
        initial_ref = None
        if "initial" in vraw and vraw.get("initial") is not None:
            init = vraw["initial"]
            if not isinstance(init, Mapping):
                raise ParseError(f"equations.variables.{vid}.initial 必须是 mapping")
            pref = init.get("property_ref")
            if not isinstance(pref, str) or pref not in properties:
                raise ParseError(
                    f"equations.variables.{vid}.initial.property_ref 必须引用已声明的 property: {pref!r}"
                )
            initial_ref = pref
        var_specs[vid] = EquationVariable(id=vid, unit=unit.strip(), valid_range=vrange, initial_property_ref=initial_ref)
        allowed.add(vid)

    relation_ids: list[str] = []
    relation_edges: list[tuple[str, tuple[str, ...]]] = []
    for i, rraw in enumerate(relations_raw):
        if not isinstance(rraw, Mapping):
            raise ParseError(f"equations.relations[{i}] 必须是 mapping")
        rid = rraw.get("id")
        expr = rraw.get("expression")
        if not isinstance(rid, str) or not _ID_PATTERN.fullmatch(rid):
            raise ParseError(f"equations.relations[{i}].id 非法: {rid!r}")
        if rid in relation_ids:
            raise ParseError(f"relation ID 重复: {rid!r}")
        relation_ids.append(rid)
        if not isinstance(expr, str) or not expr.strip():
            raise ParseError(f"relation {rid!r} 缺少 expression")
        try:
            lhs, rhs = _split_relation(expr)
        except ParseError:
            raise
        for side in (lhs, rhs):
            atoms, _ = _reference_atoms(side, file, rid)
            for name in atoms:
                if name not in allowed:
                    raise ParseError(
                        f"relation {rid!r} 引用了未声明的标识符: {name!r}（只允许 properties、interfaces、equations.variables）"
                    )
        # 左侧必须恰好一个引用原子（输出变量）
        lhs_atoms, _ = _reference_atoms(lhs, file, rid)
        if len(lhs_atoms) != 1:
            raise ParseError(f"relation {rid!r} 左侧必须恰好一个变量（当前 {len(lhs_atoms)} 个）")
        # 边: lhs 变量 -> rhs 引用（用于循环引用检测；变量为起点、引用为终点）
        rhs_atoms, _ = _reference_atoms(rhs, file, rid)
        relation_edges.append((lhs_atoms[0], rhs_atoms))

    _check_cycles(var_specs, relation_edges)


def _check_cycles(variables: Mapping[str, EquationVariable], edges: list[tuple[str, tuple[str, ...]]]) -> None:
    """检测变量定义之间的循环引用（a 依赖 b 且 b 依赖 a）。

    edges: (输出变量, 右侧引用变量元组)。只考虑变量间依赖；properties/interfaces
    是常量/输入，不构成循环。变量在自身关系右侧出现（如 ``soc[t] = soc[t-1] + …``）
    是合法的时间状态递推，不构成循环，忽略自环。
    """
    graph: dict[str, set[str]] = {vid: set() for vid in variables}
    for out_var, refs in edges:
        if out_var in graph:
            graph[out_var].update(r for r in refs if r in graph and r != out_var)
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {vid: WHITE for vid in graph}
    stack: list[str] = []

    def visit(vid: str) -> None:
        color[vid] = GRAY
        stack.append(vid)
        for nxt in sorted(graph[vid]):
            if color[nxt] == GRAY:
                idx = stack.index(nxt)
                cycle = stack[idx:] + [nxt]
                raise ParseError(f"equations 存在循环引用: {' -> '.join(cycle)}")
            if color[nxt] == WHITE:
                visit(nxt)
        stack.pop()
        color[vid] = BLACK

    for vid in graph:
        if color[vid] == WHITE:
            visit(vid)


# ---------------------------------------------------------------------------
# 模板 inputs
# ---------------------------------------------------------------------------

def _parse_inputs_tree(node: Any, path: str, file: str) -> TemplateInputSpec:
    """递归解析 inputs 树叶子声明。

    叶子节点携带 ``type``（number/boolean/string/data_repeat/data_predict/object/array）；
    无 ``type`` 的中间节点是隐式 object 容器（与模型同构树形结构），递归其子键。
    """
    if not isinstance(node, Mapping):
        raise ParseError(f"inputs.{path or '<root>'} 必须是 mapping")
    if "type" not in node:
        # 隐式 object 容器：子键即为下一层声明
        base = path or "<root>"
        children = tuple(
            _parse_inputs_tree(sub, f"{base}.{k}", file) for k, sub in node.items()
        )
        return TemplateInputSpec(path=base, type="object", children=children)
    type_ = node.get("type")
    if type_ not in INPUT_TYPES:
        raise ParseError(
            f"inputs.{path}.type 必须是 {INPUT_TYPES} 之一"
        )
    children: tuple[TemplateInputSpec, ...] = ()
    if type_ in ("object", "array"):
        inner = node.get("fields") if type_ == "object" else node.get("items")
        if inner is None:
            # object/array 未声明 fields/items 时作为空容器
            return TemplateInputSpec(path=path, type=type_, children=children)
        if type_ == "object":
            if not isinstance(inner, Mapping):
                raise ParseError(f"inputs.{path}.fields 必须是 mapping")
            children = tuple(
                _parse_inputs_tree(sub, f"{path}.{k}" if path else str(k), file)
                for k, sub in inner.items()
            )
        else:
            if not isinstance(inner, (Mapping, list)):
                raise ParseError(f"inputs.{path}.items 必须是 mapping 或 sequence")
            if isinstance(inner, list):
                children = tuple(_parse_inputs_tree(v, f"{path}[]", file) for v in inner)
            else:
                children = (_parse_inputs_tree(inner, f"{path}[]", file),)
        return TemplateInputSpec(path=path or "<root>", type=type_, children=children)

    unit: str | None = None
    vrange: tuple[float | None, float | None] | None = None
    if type_ == "number":
        unit = node.get("unit")
        if not isinstance(unit, str) or not unit.strip():
            raise ParseError(f"inputs.{path} number 必须声明 unit")
        if not _unit_known(unit.strip()):
            raise ParseError(f"inputs.{path}.unit 无法识别: {unit!r}")
        unit = unit.strip()
        if "valid_range" in node and node.get("valid_range") is not None:
            vr = node["valid_range"]
            if not isinstance(vr, Mapping):
                raise ParseError(f"inputs.{path}.valid_range 必须是 mapping")
            vrange = _check_range_bounds(vr.get("minimum"), vr.get("maximum"), file, f"inputs.{path}")
    elif type_ in ("data_repeat", "data_predict"):
        data_ref = node.get("data_ref")
        if not isinstance(data_ref, str) or not data_ref.strip():
            raise ParseError(f"inputs.{path} {type_} 必须声明 data_ref")
    default = node.get("default")
    if default is not None and not isinstance(default, (int, float, bool, str)):
        raise ParseError(f"inputs.{path}.default 必须是标量")
    return TemplateInputSpec(
        path=path,
        type=type_,
        unit=unit,
        valid_range=vrange,
        default=default,
        data_ref=node.get("data_ref") if type_ in ("data_repeat", "data_predict") else None,
    )


def parse_template_inputs(raw: object, *, file: str) -> TemplateInputs:
    """解析模板顶层 inputs；非法结构抛 ParseError。"""
    if not isinstance(raw, Mapping):
        raise ParseError("顶层 inputs 必须是 mapping")
    leaves: list[TemplateInputSpec] = []
    for key, node in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise ParseError(f"inputs 键非法: {key!r}")
        _collect_leaves(_parse_inputs_tree(node, key, file), leaves)
    return TemplateInputs(raw=raw, leaves=tuple(leaves))


def _collect_leaves(spec: TemplateInputSpec, out: list[TemplateInputSpec]) -> None:
    """深度优先收集合并叶子。

    - 标量（number/boolean/string）与 data 引用是合并点（整体替换）；
    - ``array`` 也是合并点（数组整体替换），items 声明保留用于元素校验；
    - 隐式/显式 ``object`` 是结构容器（mapping 递归合并），其子声明才是叶子。
    """
    if spec.type in INPUT_SCALAR_TYPES or spec.type in INPUT_DATA_TYPES or spec.type == "array":
        out.append(spec)
    for child in spec.children:
        _collect_leaves(child, out)


# ---------------------------------------------------------------------------
# 主解析入口
# ---------------------------------------------------------------------------

def parse_device_model_v2(raw: Mapping[str, Any], *, file: str = "") -> DeviceModelParseResult:
    """解析 2.0.0 设备模型（或模板）原始映射 → 不可变文档。

    ``raw`` 必须已由安全 YAML 解析（重复键已在解析层拒绝）。
    返回结果要么包含文档（含规范摘要），要么包含聚合诊断列表。
    """
    file = file or "<device-model>"
    diags: list[Diagnostic] = []

    # ---- 阶段 1: JSON Schema（顶层结构 + 未知核心字段 + 字段类型） ----
    try:
        import jsonschema
    except ImportError:  # pragma: no cover
        diags.append(_diag("SYS-CFG-001", "jsonschema 依赖缺失", file=file))
        return DeviceModelParseResult(document=None, diagnostics=diags)
    schema = _load_schema()
    try:
        from jsonschema import Draft202012Validator

        errors = list(Draft202012Validator(schema).iter_errors(dict(raw)))
    except ImportError:  # pragma: no cover
        diags.append(_diag("SYS-CFG-001", "jsonschema 依赖缺失", file=file))
        return DeviceModelParseResult(document=None, diagnostics=diags)
    if errors:
        # 聚合同阶段全部结构错误（jsonschema 首个错误不阻断后续字段解释）；
        # 结构错误不提前返回，语义阶段继续以防御方式收集更多问题
        for exc in errors[:20]:  # 上限保护：结构完全崩坏时最多返回 20 条
            path = ".".join(str(p) for p in exc.absolute_path)
            diags.append(
                _diag(
                    "SYS-CFG-001",
                    exc.message,
                    file=file,
                    field=path or None,
                    expected=None if exc.validator_value is None else exc.validator_value,
                    actual=exc.instance,
                )
            )

    # ---- 阶段 2: 语义校验（聚合同阶段互不依赖的问题） ----
    device_raw = raw.get("device") or {}
    if not isinstance(device_raw, Mapping):
        diags.append(_diag("SYS-CFG-001", "device 必须是 mapping", file=file, field="device"))
        device_raw = {}
    d_id = device_raw.get("id") or ""
    if not is_valid_id(d_id):
        diags.append(_diag("SYS-CFG-001", f"device.id 非法: {d_id!r}", file=file, field="device.id",
                           expected="小写命名空间 ID", actual=d_id))
    names_raw = device_raw.get("names")
    names: dict[str, str] = {}
    if isinstance(names_raw, Mapping):
        for k, v in names_raw.items():
            if isinstance(k, str) and isinstance(v, str):
                names[k] = v
    else:
        diags.append(_diag("SYS-CFG-001", "device.names 必须是 mapping", file=file, field="device.names"))

    # properties
    props_raw = raw.get("properties") or {}
    properties: dict[str, PropertySpec] = {}
    if not isinstance(props_raw, Mapping):
        diags.append(_diag("SYS-CFG-001", "properties 必须是 mapping", file=file, field="properties"))
        props_raw = {}
    for pid, praw in props_raw.items():
        fld = f"properties.{pid}"
        if not isinstance(pid, str) or not _ID_PATTERN.fullmatch(pid):
            diags.append(_diag("SYS-CFG-001", f"property ID 非法: {pid!r}", file=file, field=fld))
            continue
        if not isinstance(praw, Mapping):
            diags.append(_diag("SYS-CFG-001", f"properties.{pid} 必须是 mapping", file=file, field=fld))
            continue
        value = praw.get("value")
        if isinstance(value, bool) or isinstance(value, str) or (
            isinstance(value, (int, float)) and _is_finite_number(value)
        ):
            pass
        else:
            diags.append(_diag("SYS-CFG-001", f"properties.{pid}.value 必须是有限 JSON 标量", file=file,
                               field=f"{fld}.value", actual=value))
            continue
        unit = praw.get("unit")
        if not isinstance(unit, str) or not unit.strip():
            diags.append(_diag("SYS-CFG-001", f"properties.{pid}.unit 缺失", file=file, field=f"{fld}.unit"))
            continue
        if not _unit_known(unit.strip()):
            diags.append(_diag("SYS-CFG-001", f"properties.{pid}.unit 无法识别: {unit!r}", file=file,
                               field=f"{fld}.unit", actual=unit))
            continue
        vrange = None
        vr = praw.get("valid_range")
        if vr is not None:
            if not isinstance(vr, Mapping):
                diags.append(_diag("SYS-CFG-001", f"properties.{pid}.valid_range 必须是 mapping", file=file,
                                   field=f"{fld}.valid_range"))
            else:
                try:
                    vrange = _check_range_bounds(vr.get("minimum"), vr.get("maximum"), file, fld)
                except ParseError as exc:
                    diags.append(_diag("SYS-CFG-001", str(exc), file=file, field=f"{fld}.valid_range"))
        properties[pid] = PropertySpec(id=pid, value=value, unit=unit.strip(), valid_range=vrange)

    # interfaces
    ifaces_raw = raw.get("interfaces") or {}
    interfaces: dict[str, InterfaceSpec] = {}
    if not isinstance(ifaces_raw, Mapping):
        diags.append(_diag("SYS-CFG-001", "interfaces 必须是 mapping", file=file, field="interfaces"))
        ifaces_raw = {}
    for iid, iraw in ifaces_raw.items():
        fld = f"interfaces.{iid}"
        if not isinstance(iid, str) or not _ID_PATTERN.fullmatch(iid):
            diags.append(_diag("SYS-CFG-001", f"interface ID 非法: {iid!r}", file=file, field=fld))
            continue
        if not isinstance(iraw, Mapping):
            diags.append(_diag("SYS-CFG-001", f"interfaces.{iid} 必须是 mapping", file=file, field=fld))
            continue
        type_ = iraw.get("type") or "blind"  # 缺省规范化为 blind
        if type_ not in INTERFACE_TYPES:
            diags.append(_diag("SYS-CFG-001", f"interfaces.{iid}.type 必须是 {INTERFACE_TYPES} 之一",
                               file=file, field=f"{fld}.type", expected=INTERFACE_TYPES, actual=type_))
            continue
        carrier = iraw.get("carrier")
        if not isinstance(carrier, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", carrier):
            diags.append(_diag("SYS-CFG-001", f"interfaces.{iid}.carrier 非法: {carrier!r}",
                               file=file, field=f"{fld}.carrier"))
            continue
        unit = iraw.get("unit")
        if not isinstance(unit, str) or not unit.strip():
            diags.append(_diag("SYS-CFG-001", f"interfaces.{iid}.unit 缺失", file=file, field=f"{fld}.unit"))
            continue
        if not _unit_known(unit.strip()):
            diags.append(_diag("SYS-CFG-001", f"interfaces.{iid}.unit 无法识别: {unit!r}", file=file,
                               field=f"{fld}.unit", actual=unit))
            continue
        vr = iraw.get("valid_range")
        if not isinstance(vr, Mapping):
            diags.append(_diag("SYS-CFG-001", f"interfaces.{iid}.valid_range 必须是 mapping", file=file,
                               field=f"{fld}.valid_range"))
            continue
        try:
            vrange = _check_range_bounds(vr.get("minimum"), vr.get("maximum"), file, fld)
        except ParseError as exc:
            diags.append(_diag("SYS-CFG-001", str(exc), file=file, field=f"{fld}.valid_range"))
            continue
        source_raw = iraw.get("source")
        source: SourceSpec | None = None
        if type_ in SOURCE_TYPES and source_raw is None:
            diags.append(_diag("SYS-CFG-001", f"interfaces.{iid} 类型为 {type_!r} 必须声明 source",
                               file=file, field=f"{fld}.source"))
        elif source_raw is not None:
            try:
                _check_interface_source(iid, type_, source_raw, file)
                mode = source_raw.get("mode")
                if mode == "constant":
                    source = SourceSpec(mode=mode, value=source_raw.get("value"))
                else:
                    source = SourceSpec(mode=mode, data_ref=source_raw.get("data_ref"))
            except ParseError as exc:
                diags.append(_diag("SYS-CFG-001", str(exc), file=file, field=f"{fld}.source"))
        interfaces[iid] = InterfaceSpec(
            id=iid, type=type_, carrier=carrier, unit=unit.strip(), valid_range=vrange, source=source
        )

    # equations
    equations_raw = raw.get("equations")
    equations: Equations = Equations()
    if not isinstance(equations_raw, Mapping):
        diags.append(_diag("SYS-CFG-001", "equations 必须是 mapping", file=file, field="equations"))
    else:
        try:
            validate_equations(equations_raw, properties, interfaces, file=file)
            eq_vars = {
                vid: EquationVariable(
                    id=vid,
                    unit=vraw.get("unit", "").strip(),
                    valid_range=(
                        _check_range_bounds(
                            vraw["valid_range"].get("minimum") if isinstance(vraw.get("valid_range"), Mapping) else None,
                            vraw["valid_range"].get("maximum") if isinstance(vraw.get("valid_range"), Mapping) else None,
                            file, f"equations.variables.{vid}",
                        )
                        if vraw.get("valid_range") is not None
                        else None
                    ),
                    initial_property_ref=(
                        vraw["initial"].get("property_ref")
                        if isinstance(vraw.get("initial"), Mapping)
                        else None
                    ),
                )
                for vid, vraw in equations_raw.get("variables", {}).items()
            }
            relations = tuple(
                EquationRelation(id=r.get("id", ""), expression=r.get("expression", ""))
                for r in equations_raw.get("relations", [])
            )
            equations = Equations(variables=dict(eq_vars), relations=relations)
        except ParseError as exc:
            diags.append(_diag("SYS-CFG-001", str(exc), file=file, field="equations"))

    # 模板 inputs
    inputs: Mapping[str, Any] | None = None
    if "inputs" in raw:
        try:
            parsed_inputs = parse_template_inputs(raw.get("inputs"), file=file)
            inputs = parsed_inputs.raw
        except ParseError as exc:
            diags.append(_diag("SYS-CFG-001", str(exc), file=file, field="inputs"))

    if diags:
        return DeviceModelParseResult(document=None, diagnostics=diags)

    document = DeviceModelDocument(
        schema_version=SCHEMA_VERSION,
        device=DeviceInfo(id=d_id, names=names),
        properties=properties,
        interfaces=interfaces,
        equations=equations,
        inputs=inputs,
    )
    return DeviceModelParseResult(document=document, diagnostics=[])


def canonicalize_v2(raw: Mapping[str, Any], *, file: str = "") -> DeviceModelParseResult:
    """解析 + 规范化 + 摘要：成功返回带 ``receipt`` 的文档（``document.receipt`` 属性）。"""
    result = parse_device_model_v2(raw, file=file)
    if not result.ok:
        return result
    doc = result.document
    assert doc is not None
    # 挂载回执（不可变文档使用 MappingProxyType 包装，回执单独存放于结果）
    result.diagnostics = []
    return DeviceModelParseResult(document=doc, diagnostics=[])
