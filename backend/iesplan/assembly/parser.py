"""装配文本解析器:YAML 1.2 子集文本 → AssemblySpec。

阶段 A(语法/结构)检查在此完成:解析失败/未知章节/版本不支持/必填缺失/
类型错误产出 ASM-SYN-* 诊断;存在 error 级诊断时 spec 为 None,不进入阶段 B/C/D。
解析采用严格模式:未知键/未知枚举值一律报错(不静默忽略),保证文本确定性。

YAML 子集说明(与 builder 规范输出互逆,同时兼容 §2.2 手写风格):
- 块映射 `key: value`、块序列 `- item`(缩进任意一致,2 空格为规范);
- 流式映射 `{a: 1, b: 2}` 与流式序列 `[1, 2]`(可嵌套、可含引号字符串);
- 标量:裸字符串 / 单引号 / 双引号 / 整数 / 浮点 / true/false/null;
- 注释:`#` 起至行尾(裸标量内以 " #" 分隔);
- 不支持:多行块标量(|/>)、锚点/别名、制表符缩进、跨行流值。

本模块不依赖第三方 YAML 库(自成体系,可独立导入)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from iesplan.assembly.diags import (
    ASM_SYN_FIELD,
    ASM_SYN_PARSE,
    ASM_SYN_SECTION,
    ASM_SYN_TYPE,
    ASM_SYN_VERSION,
)
from iesplan.assembly.diags import make_asm_diag as make_diag
from iesplan.assembly.schema import (
    CARRIERS,
    DIRECTIONS,
    FORMAT_VERSION,
    MODEL_METHODS,
    NATURES,
    QUANTITIES,
    RESOLUTIONS,
    AssemblyConstraint,
    AssemblyDevice,
    AssemblyEdge,
    AssemblyPipeline,
    AssemblyPort,
    AssemblySpec,
    CalcRequirements,
    DataRef,
    TimeAxisRef,
)
from iesplan.core.diagnostics import SEVERITY_ERROR, Diagnostic

#: 顶层允许章节
_TOP_SECTIONS: tuple[str, ...] = (
    "assembly",
    "time_axis",
    "models",
    "devices",
    "ports",
    "edges",
    "pipelines",
    "constraints",
    "requirements",
)

#: 各对象允许键(严格模式:未知键报 ASM-SYN-001)
_DEVICE_KEYS: tuple[str, ...] = (
    "id",
    "model",
    "kind",
    "model_method",
    "stateful",
    "params",
    "data_refs",
    "meta",
)
_PORT_KEYS: tuple[str, ...] = (
    "device",
    "name",
    "carrier",
    "direction",
    "quantity",
    "unit",
    "nature",
    "delay_steps",
    "capacity",
)
_EDGE_KEYS: tuple[str, ...] = ("id", "from", "to", "capacity", "meta")
_PIPELINE_KEYS: tuple[str, ...] = ("id", "model", "params")
_CONSTRAINT_KEYS: tuple[str, ...] = ("id", "type", "expr", "enabled")
_DATAREF_KEYS: tuple[str, ...] = (
    "key",
    "dataset_version_id",
    "dataset_name",
    "columns",
    "unit",
    "resolution",
)

#: 显式端口声明中必须与注册表推导一致的字段(不一致仅告警,以注册表为准)
PORT_DECL_OVERRIDE_FIELDS: tuple[str, ...] = ("carrier", "direction", "quantity", "unit", "nature")


@dataclass(slots=True)
class ParseResult:
    """解析结果:spec 与诊断列表。"""

    spec: AssemblySpec | None
    diagnostics: list[Diagnostic]

    @property
    def ok(self) -> bool:
        """无 error/blocking 级诊断。"""
        return all(d.severity not in (SEVERITY_ERROR, "blocking") for d in self.diagnostics)


# ---------------------------------------------------------------------------
# YAML 1.2 子集解析器(独立实现,无第三方依赖)
# ---------------------------------------------------------------------------


class _YamlParseError(Exception):
    """YAML 子集语法错误(携带行号)。"""

    def __init__(self, message: str, line: int) -> None:
        super().__init__(message)
        self.message = message
        self.line = line


def _count_indent(line: str) -> int:
    """统计前导空格缩进(制表符一律报错,保证确定性)。"""
    n = 0
    for ch in line:
        if ch == " ":
            n += 1
        elif ch == "\t":
            raise _YamlParseError("不允许制表符缩进", 0)
        else:
            break
    return n


def _strip_comment(text: str) -> str:
    """去掉裸标量行尾注释(流式括号内不剥离,由流解析器处理)。"""
    out: list[str] = []
    in_single = in_double = False
    depth = 0
    for ch in text:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            elif ch == "#" and depth == 0:
                if out and out[-1].isspace():
                    break
        out.append(ch)
    return "".join(out).rstrip()


def _parse_scalar(token: str) -> Any:
    """标量解析:null/布尔/数字/字符串。"""
    t = token.strip()
    low = t.lower()
    if low in ("null", "~", ""):
        return None
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if re.fullmatch(r"[-+]?\d+", t):
        return int(t)
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?", t):
        try:
            return float(t)
        except ValueError:
            pass
    return t


def _parse_quoted(text: str, pos: int) -> tuple[str, int]:
    """解析引号字符串,返回 (值, 消费后位置);未闭合抛 _YamlParseError。"""
    quote = text[pos]
    pos += 1
    buf: list[str] = []
    while pos < len(text):
        ch = text[pos]
        if ch == quote:
            if pos + 1 < len(text) and text[pos + 1] == quote:  # 双写转义
                buf.append(quote)
                pos += 2
                continue
            return "".join(buf), pos + 1
        if quote == '"' and ch == "\\" and pos + 1 < len(text):
            esc = text[pos + 1]
            buf.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(esc, esc))
            pos += 2
            continue
        buf.append(ch)
        pos += 1
    raise _YamlParseError("引号字符串未闭合", 0)


# 裸 token 终止字符(流式值内)
_FLOW_STOP: str = ":{},[]}"


def _parse_flow_value(text: str, pos: int) -> tuple[Any, int]:
    """解析流式值(引号/嵌套容器/裸 token),返回 (值, 消费后位置)。"""
    ch = text[pos]
    if ch in "'\"":
        return _parse_quoted(text, pos)
    if ch in "[{":
        return _parse_flow_container(text, pos)
    start = pos
    while pos < len(text) and text[pos] not in _FLOW_STOP:
        pos += 1
    return _parse_scalar(text[start:pos].strip()), pos


def _parse_flow_container(text: str, pos: int) -> tuple[Any, int]:
    """解析流式容器 {..} / [..](支持嵌套与引号),返回 (值, 消费后位置)。"""
    open_ch = text[pos]
    close_ch = "}" if open_ch == "{" else "]"
    pos += 1
    is_map = open_ch == "{"
    result: Any = {} if is_map else []
    key: Any = None
    expect_key = is_map
    while pos < len(text):
        ch = text[pos]
        if ch in " \t\r\n,":
            pos += 1
            continue
        if ch == close_ch:
            pos += 1
            if is_map and key is not None:
                result[key] = None  # {a:} 简写
            return result, pos
        if expect_key:
            if ch in "'\"":
                key, pos = _parse_quoted(text, pos)
            else:
                start = pos
                while pos < len(text) and text[pos] not in ":{},[]}":
                    pos += 1
                key = _parse_scalar(text[start:pos].strip())
            expect_key = False
            continue
        if is_map and ch == ":":
            pos += 1
            while pos < len(text) and text[pos] in " \t":
                pos += 1
            if pos < len(text) and text[pos] in ",}":
                result[key] = None
            else:
                val, pos = _parse_flow_value(text, pos)
                result[key] = val
            key = None
            expect_key = True
            continue
        val, pos = _parse_flow_value(text, pos)
        if is_map and key is not None:
            result[key] = val
            key = None
            expect_key = True
        else:
            result.append(val)
    raise _YamlParseError("流式容器未闭合", 0)


def _parse_value(text: str) -> Any:
    """解析键后值:空→None;'{'/'['→流式;引号→字符串;其余标量(剥注释)。"""
    text = text.strip()
    if not text:
        return None
    if text[0] in ("{", "["):
        return _parse_flow_container(text, 0)[0]
    if text[0] in ("'", '"'):
        try:
            val, pos = _parse_quoted(text, 0)
        except _YamlParseError:
            pass
        else:
            rest = _strip_comment(text[pos:]).strip()
            if not rest:
                return val
            raise _YamlParseError(f"引号字符串后有非注释内容: {rest!r}", 0)
    return _parse_scalar(_strip_comment(text))


class _LineCursor:
    """行迭代器:预读支持(lookahead),供块解析递归使用。"""

    def __init__(self, lines: list[tuple[int, str]]) -> None:
        self.lines = lines
        self.pos = 0

    def peek(self) -> tuple[int, str] | None:
        if self.pos < len(self.lines):
            return self.lines[self.pos]
        return None

    def next(self) -> tuple[int, str] | None:
        line = self.peek()
        if line is not None:
            self.pos += 1
        return line


def _parse_block(cursor: _LineCursor, min_indent: int) -> Any:
    """递归下降块解析:返回 (映射 | 列表 | None)。

    规则:行缩进 == min_indent 且以 "- " 起 → 序列;形如 "key:" → 映射;
    行缩进 > min_indent → 向上返回(由调用方并入本块)。
    """
    peek = cursor.peek()
    if peek is None or peek[0] != min_indent:
        return None
    if peek[1].startswith("- ") or peek[1] == "-":
        return _parse_sequence(cursor, min_indent)
    return _parse_mapping(cursor, min_indent)


def _deeper_block(cursor: _LineCursor, parent_indent: int) -> Any:
    """解析比 parent_indent 更深的后续块(任意更深缩进,取首行实际缩进)。"""
    peek = cursor.peek()
    if peek is None or peek[0] <= parent_indent:
        return None
    return _parse_block(cursor, peek[0])


def _parse_sequence(cursor: _LineCursor, min_indent: int) -> list[Any]:
    """块序列:每项 "- xxx";"- key: val" 起内联映射,后续更深缩进行并入该项。"""
    items: list[Any] = []
    while True:
        peek = cursor.peek()
        if peek is None or peek[0] != min_indent or not (peek[1].startswith("- ") or peek[1] == "-"):
            break
        indent, content = peek
        cursor.next()
        rest = content[2:].strip() if content.startswith("- ") else ""
        if not rest:
            # "-" 空项:值为嵌套块(下一行更深缩进)
            items.append(_deeper_block(cursor, indent) or None)
            continue
        colon = rest.find(":")
        if colon > 0 and rest[0] not in ("{", "[", "'", '"'):
            # 内联映射项 "- key: value"(后续更深缩进行并入该项映射)
            # 注:流式容器/引号开头的项("− {a: 1}"、"− 'a: b'")按普通值处理
            key = rest[:colon].strip()
            val_text = rest[colon + 1 :].strip()
            item: dict[str, Any] = {}
            if val_text:
                item[key] = _parse_value(val_text)
            child = _deeper_block(cursor, indent)
            if isinstance(child, dict):
                for k, v in child.items():
                    if k in item:
                        raise _YamlParseError(f"重复键 {k!r}", 0)
                    item[k] = v
            elif child is not None:
                raise _YamlParseError(f"序列项内联映射后不允许标量块: {child!r}", 0)
            items.append(item)
            continue
        # "- scalar" / "- {flow}" / "- [flow]"
        items.append(_parse_value(rest))
        child = _deeper_block(cursor, indent)
        if child is not None:
            items.append(child)
    return items


def _parse_mapping(cursor: _LineCursor, min_indent: int) -> dict[str, Any]:
    """块映射:形如 "key:" / "key: value" 的行,值为嵌套块或标量。"""
    result: dict[str, Any] = {}
    while True:
        peek = cursor.peek()
        if peek is None or peek[0] != min_indent or peek[1].startswith("- "):
            break
        indent, content = peek
        colon = content.find(":")
        if colon <= 0:
            raise _YamlParseError(f"映射行缺少冒号: {content!r}", 0)
        key = content[:colon].strip()
        if not key:
            raise _YamlParseError("映射键为空", 0)
        if key in result:
            raise _YamlParseError(f"重复键 {key!r}", 0)
        if len(key) >= 2 and key[0] in ("'", '"') and key[-1] == key[0]:
            key = key[1:-1]
        cursor.next()
        val_text = content[colon + 1 :].strip()
        if val_text:
            result[key] = _parse_value(val_text)
        else:
            child = _deeper_block(cursor, indent)
            result[key] = child if child is not None else None
    return result


def _load_yaml(text: str) -> dict[str, Any]:
    """YAML 子集文本 → 顶层映射(语法错误抛 _YamlParseError)。"""
    raw_lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        stripped = raw.rstrip()
        if not stripped.strip() or stripped.lstrip().startswith("#"):
            continue
        indent = _count_indent(stripped)
        content = stripped[indent:].rstrip()
        if content:
            raw_lines.append((indent, content))
    if not raw_lines:
        return {}
    if raw_lines[0][0] != 0:
        raise _YamlParseError("顶层行不允许缩进", 1)
    tree = _parse_mapping(_LineCursor(raw_lines), 0)
    if not isinstance(tree, dict):
        raise _YamlParseError("顶层必须为映射", 1)
    return tree


# ---------------------------------------------------------------------------
# 结构校验与 AssemblySpec 构建
# ---------------------------------------------------------------------------


def parse_assembly(text: str, *, source_name: str = "assembly.yaml") -> ParseResult:
    """YAML 文本 → AssemblySpec(严格模式)。

    产出 ASM-SYN-001..005 诊断;存在 error 级诊断时 spec 为 None。
    端口推导不在此阶段(检查器阶段 B/C 以注册表为准并合并覆盖 capacity)。
    """
    diags: list[Diagnostic] = []
    try:
        tree = _load_yaml(text)
    except _YamlParseError as exc:
        diags.append(
            make_diag(
                ASM_SYN_PARSE,
                severity="error",
                blocking=True,
                params={"source": source_name, "line": exc.line, "detail": exc.message},
                location={"object_type": "assembly", "field": f"{source_name}:{exc.line}"},
            )
        )
        return ParseResult(spec=None, diagnostics=diags)

    builder = _SpecBuilder(diags)
    spec = builder.build(tree)
    has_error = any(d.severity == SEVERITY_ERROR for d in diags)
    return ParseResult(spec=None if has_error else spec, diagnostics=diags)


def load_assembly_file(path: str) -> ParseResult:
    """从文件读取并解析(供离线 CLI 与测试使用)。"""
    with open(path, encoding="utf-8") as fh:
        return parse_assembly(fh.read(), source_name=path)


class _SpecBuilder:
    """结构校验 + AssemblySpec 构建(定位路径如 "devices[3].id")。"""

    def __init__(self, diags: list[Diagnostic]) -> None:
        self.diags = diags

    def _diag(
        self,
        code: str,
        field: str,
        params: dict | None = None,
        *,
        severity: str = "error",
        blocking: bool = True,
    ) -> None:
        self.diags.append(
            make_diag(
                code,
                severity=severity,
                blocking=blocking,
                params={"field": field, **(params or {})},
                location={"object_type": "assembly", "field": field},
            )
        )

    def _require_map(self, value: Any, field: str) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            self._diag(ASM_SYN_TYPE, field, {"reason": "expected_map", "actual": type(value).__name__})
            return None
        for k in value:
            if not isinstance(k, str):
                self._diag(ASM_SYN_PARSE, f"{field}.<key>", {"reason": "non_string_key"})
                return None
        return value

    def _str(self, obj: dict, key: str, field: str, *, required: bool = False) -> str | None:
        val = obj.get(key)
        if val is None:
            if required:
                self._diag(ASM_SYN_FIELD, field, {"reason": "missing_field", "key": key})
            return None
        if isinstance(val, str):
            return val
        self._diag(ASM_SYN_TYPE, field, {"reason": "expected_string", "actual": type(val).__name__})
        return None

    def _num(self, val: Any, field: str) -> float | None:
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            self._diag(ASM_SYN_TYPE, field, {"reason": "expected_number", "actual": type(val).__name__})
            return None
        return float(val)

    def _int(self, val: Any, field: str) -> int | None:
        if isinstance(val, bool) or not isinstance(val, int):
            self._diag(ASM_SYN_TYPE, field, {"reason": "expected_integer", "actual": type(val).__name__})
            return None
        return val

    def _bool(self, val: Any, field: str) -> bool | None:
        if isinstance(val, bool):
            return val
        self._diag(ASM_SYN_TYPE, field, {"reason": "expected_boolean", "actual": type(val).__name__})
        return None

    def _enum(self, val: Any, allowed: tuple[str, ...], field: str) -> str | None:
        if not isinstance(val, str) or val not in allowed:
            self._diag(
                ASM_SYN_TYPE,
                field,
                {"reason": "invalid_enum", "value": repr(val), "allowed": list(allowed)},
            )
            return None
        return val

    # -- 顶层 ----------------------------------------------------------------

    def build(self, tree: dict[str, Any]) -> AssemblySpec | None:
        spec = AssemblySpec()
        for key in tree:
            if key not in _TOP_SECTIONS:
                self._diag(ASM_SYN_SECTION, key, {"reason": "unknown_section", "section": key})
        assembly = self._require_map(tree.get("assembly"), "assembly")
        if assembly is not None:
            spec.name = self._str(assembly, "name", "assembly.name", required=True) or ""
            ver = self._str(assembly, "format_version", "assembly.format_version", required=True)
            if ver is not None:
                if ver != FORMAT_VERSION:
                    self._diag(
                        ASM_SYN_VERSION,
                        "assembly.format_version",
                        {"expected": FORMAT_VERSION, "actual": ver},
                    )
                else:
                    spec.format_version = ver
            gid = assembly.get("source_graph_id")
            if gid is not None and not isinstance(gid, bool):
                if isinstance(gid, int):
                    spec.source_graph_id = gid
                else:
                    self._diag(ASM_SYN_TYPE, "assembly.source_graph_id", {"reason": "expected_integer"})
            for key in assembly:
                if key not in ("name", "format_version", "source_graph_id"):
                    self._diag(ASM_SYN_PARSE, f"assembly.{key}", {"reason": "unknown_key"})
        elif any(k in tree for k in ("devices", "edges", "time_axis")):
            self._diag(ASM_SYN_FIELD, "assembly", {"reason": "missing_section", "section": "assembly"})

        self._build_time_axis(spec, tree.get("time_axis"))
        self._build_devices(spec, tree.get("devices"))
        self._build_ports(spec, tree.get("ports"))
        self._build_edges(spec, tree.get("edges"))
        self._build_pipelines(spec, tree.get("pipelines"))
        self._build_constraints(spec, tree.get("constraints"))
        self._build_requirements(spec, tree.get("requirements"))
        return spec

    # -- 各章节 --------------------------------------------------------------

    def _build_time_axis(self, spec: AssemblySpec, value: Any) -> None:
        if value is None:
            if spec.name:
                self._diag(ASM_SYN_FIELD, "time_axis", {"reason": "missing_section", "section": "time_axis"})
            return
        m = self._require_map(value, "time_axis")
        if m is None:
            return
        resolution = self._enum(m.get("resolution"), RESOLUTIONS, "time_axis.resolution")
        if resolution is None:
            self._diag(ASM_SYN_FIELD, "time_axis.resolution", {"reason": "missing_or_invalid"})
            return
        start = self._str(m, "start", "time_axis.start") or "2025-01-01T00:00:00Z"
        off = m.get("timezone_offset_min")
        offset = 0 if off is None else (self._int(off, "time_axis.timezone_offset_min") or 0)
        for key in m:
            if key not in ("resolution", "start", "timezone_offset_min"):
                self._diag(ASM_SYN_PARSE, f"time_axis.{key}", {"reason": "unknown_key"})
        spec.time_axis = TimeAxisRef(resolution=resolution, start=start, timezone_offset_min=offset)

    def _build_devices(self, spec: AssemblySpec, value: Any) -> None:
        if value is None:
            return
        if not isinstance(value, list):
            self._diag(ASM_SYN_TYPE, "devices", {"reason": "expected_list", "actual": type(value).__name__})
            return
        for i, item in enumerate(value):
            field = f"devices[{i}]"
            m = self._require_map(item, field)
            if m is None:
                continue
            did = self._str(m, "id", f"{field}.id", required=True)
            model = self._str(m, "model", f"{field}.model", required=True)
            if did is None or model is None:
                continue
            kind = self._enum(m.get("kind", "existing"), ("existing", "new"), f"{field}.kind")
            method = self._enum(m.get("model_method", "mechanism"), MODEL_METHODS, f"{field}.model_method")
            stateful = self._bool(m.get("stateful", False), f"{field}.stateful")
            for key in m:
                if key not in _DEVICE_KEYS:
                    self._diag(ASM_SYN_PARSE, f"{field}.{key}", {"reason": "unknown_key", "key": key})
            spec.devices.append(
                AssemblyDevice(
                    id=did,
                    model=model,
                    kind=kind or "existing",
                    model_method=method or "mechanism",
                    stateful=bool(stateful),
                    params=self._param_map(m, "params"),
                    data_refs=self._build_data_refs(m.get("data_refs"), f"{field}.data_refs"),
                    meta=m.get("meta") if isinstance(m.get("meta"), dict) else {},
                )
            )

    def _build_data_refs(self, value: Any, field: str) -> list[DataRef]:
        refs: list[DataRef] = []
        if value is None:
            return refs
        if not isinstance(value, list):
            self._diag(ASM_SYN_TYPE, field, {"reason": "expected_list", "actual": type(value).__name__})
            return refs
        for i, item in enumerate(value):
            if isinstance(item, int) and not isinstance(item, bool):
                refs.append(DataRef(key=f"data{i}", dataset_version_id=item))
                continue
            m = self._require_map(item, f"{field}[{i}]")
            if m is None:
                continue
            key = self._str(m, "key", f"{field}[{i}].key", required=True)
            vid = m.get("dataset_version_id")
            if vid is None or isinstance(vid, bool) or not isinstance(vid, int):
                self._diag(ASM_SYN_FIELD, f"{field}[{i}].dataset_version_id", {"reason": "missing_field"})
                continue
            cols = m.get("columns")
            if cols is not None and not isinstance(cols, list):
                self._diag(ASM_SYN_TYPE, f"{field}[{i}].columns", {"reason": "expected_list"})
                cols = []
            for k in m:
                if k not in _DATAREF_KEYS:
                    self._diag(ASM_SYN_PARSE, f"{field}[{i}].{k}", {"reason": "unknown_key"})
            refs.append(
                DataRef(
                    key=key or f"data{i}",
                    dataset_version_id=vid,
                    dataset_name=self._str(m, "dataset_name", f"{field}[{i}].dataset_name") or "",
                    columns=[c for c in (cols or []) if isinstance(c, str)],
                    unit=self._str(m, "unit", f"{field}[{i}].unit") or "",
                    resolution=self._str(m, "resolution", f"{field}[{i}].resolution") or "",
                )
            )
        return refs

    def _build_ports(self, spec: AssemblySpec, value: Any) -> None:
        """显式端口声明:设备端口并入 device.ports,管道端口并入 spec.explicit_pipeline_ports。

        仅用于覆盖(容量等);注册表推导由检查器完成,显式声明与推导不一致按 REF-005 告警。
        """
        if value is None:
            return
        if not isinstance(value, list):
            self._diag(ASM_SYN_TYPE, "ports", {"reason": "expected_list", "actual": type(value).__name__})
            return
        for i, item in enumerate(value):
            field = f"ports[{i}]"
            m = self._require_map(item, field)
            if m is None:
                continue
            device = self._str(m, "device", f"{field}.device", required=True)
            name = self._str(m, "name", f"{field}.name", required=True)
            if device is None or name is None:
                continue
            carrier = self._enum(m.get("carrier"), CARRIERS, f"{field}.carrier")
            direction = self._enum(m.get("direction"), DIRECTIONS, f"{field}.direction")
            quantity = self._enum(m.get("quantity"), QUANTITIES, f"{field}.quantity")
            unit = self._str(m, "unit", f"{field}.unit")
            nature = self._enum(m.get("nature", "instantaneous"), NATURES, f"{field}.nature")
            delay = self._int(m.get("delay_steps", 0), f"{field}.delay_steps")
            capacity = None
            if m.get("capacity") is not None:
                capacity = self._num(m["capacity"], f"{field}.capacity")
            for key in m:
                if key not in _PORT_KEYS:
                    self._diag(ASM_SYN_PARSE, f"{field}.{key}", {"reason": "unknown_key", "key": key})
            port = AssemblyPort(
                device=device,
                name=name,
                carrier=carrier or "heat",
                direction=direction or "in",
                quantity=quantity or "power",
                unit=unit or "W",
                nature=nature or "instantaneous",
                delay_steps=delay or 0,
                capacity=capacity,
            )
            device_obj = spec.device_by_id(device)
            if device_obj is not None:
                if any(p.name == name for p in device_obj.ports):
                    self._diag(ASM_SYN_PARSE, f"{field}.name", {"reason": "duplicate_port_declaration"})
                    continue
                device_obj.ports.append(port)  # frozen 数据类的列表字段可追加
            elif spec.pipeline_by_id(device) is not None:
                spec.explicit_pipeline_ports.append(port)
            else:
                self._diag(ASM_SYN_FIELD, f"{field}.device", {"reason": "unknown_device", "device": device})

    def _build_edges(self, spec: AssemblySpec, value: Any) -> None:
        if value is None:
            return
        if not isinstance(value, list):
            self._diag(ASM_SYN_TYPE, "edges", {"reason": "expected_list", "actual": type(value).__name__})
            return
        for i, item in enumerate(value):
            field = f"edges[{i}]"
            m = self._require_map(item, field)
            if m is None:
                continue
            from_port = self._str(m, "from", f"{field}.from", required=True)
            to_port = self._str(m, "to", f"{field}.to", required=True)
            if from_port is None or to_port is None:
                continue
            eid = self._str(m, "id", f"{field}.id") or f"e{i + 1}"
            capacity = None
            if m.get("capacity") is not None:
                capacity = self._num(m["capacity"], f"{field}.capacity")
            for key in m:
                if key not in _EDGE_KEYS:
                    self._diag(ASM_SYN_PARSE, f"{field}.{key}", {"reason": "unknown_key", "key": key})
            spec.edges.append(
                AssemblyEdge(
                    id=eid,
                    from_port=from_port,
                    to_port=to_port,
                    capacity=capacity,
                    meta=m.get("meta") if isinstance(m.get("meta"), dict) else {},
                )
            )

    def _build_pipelines(self, spec: AssemblySpec, value: Any) -> None:
        if value is None:
            return
        if not isinstance(value, list):
            self._diag(ASM_SYN_TYPE, "pipelines", {"reason": "expected_list", "actual": type(value).__name__})
            return
        for i, item in enumerate(value):
            field = f"pipelines[{i}]"
            m = self._require_map(item, field)
            if m is None:
                continue
            pid = self._str(m, "id", f"{field}.id", required=True)
            if pid is None:
                continue
            model = self._str(m, "model", f"{field}.model") or "ies.device.transport_pipe@1.0.0"
            for key in m:
                if key not in _PIPELINE_KEYS:
                    self._diag(ASM_SYN_PARSE, f"{field}.{key}", {"reason": "unknown_key", "key": key})
            spec.pipelines.append(AssemblyPipeline(id=pid, model=model, params=self._param_map(m, "params")))

    def _build_constraints(self, spec: AssemblySpec, value: Any) -> None:
        if value is None:
            return
        if not isinstance(value, list):
            self._diag(
                ASM_SYN_TYPE,
                "constraints",
                {"reason": "expected_list", "actual": type(value).__name__},
            )
            return
        for i, item in enumerate(value):
            field = f"constraints[{i}]"
            m = self._require_map(item, field)
            if m is None:
                continue
            ctype = self._enum(
                m.get("type", "generic"), ("ratio", "capacity", "schedule", "generic"), f"{field}.type"
            )
            expr = self._str(m, "expr", f"{field}.expr", required=True)
            if expr is None:
                continue
            cid = self._str(m, "id", f"{field}.id") or f"c{i + 1}"
            enabled = self._bool(m.get("enabled", True), f"{field}.enabled")
            for key in m:
                if key not in _CONSTRAINT_KEYS:
                    self._diag(ASM_SYN_PARSE, f"{field}.{key}", {"reason": "unknown_key", "key": key})
            spec.constraints.append(
                AssemblyConstraint(id=cid, type=ctype or "generic", expr=expr, enabled=enabled is not False)
            )

    def _build_requirements(self, spec: AssemblySpec, value: Any) -> None:
        if value is None:
            return
        m = self._require_map(value, "requirements")
        if m is None:
            return
        algorithm = self._str(m, "algorithm", "requirements.algorithm") or "ies.algo.milp_hybrid@1.0.0"
        tolerances: dict[str, float] = {}
        tol = m.get("tolerances")
        if tol is not None:
            tm = self._require_map(tol, "requirements.tolerances")
            if tm is not None:
                for k, v in tm.items():
                    num = self._num(v, f"requirements.tolerances.{k}")
                    if num is not None:
                        if num < 0:
                            self._diag(ASM_SYN_TYPE, f"requirements.tolerances.{k}", {"reason": "negative"})
                        else:
                            tolerances[k] = num
        seed = m.get("seed")
        if seed is not None:
            seed = self._int(seed, "requirements.seed")
        for key in m:
            if key not in ("algorithm", "tolerances", "seed", "options"):
                self._diag(ASM_SYN_PARSE, f"requirements.{key}", {"reason": "unknown_key"})
        spec.requirements = CalcRequirements(
            algorithm=algorithm,
            tolerances=tolerances or {"mip_rel_gap": 0.001, "time_limit_s": 600.0},
            seed=seed,
        )

    def _param_map(self, obj: dict, field: str) -> dict[str, object]:
        val = obj.get(field)
        if val is None:
            return {}
        m = self._require_map(val, field)
        if m is None:
            self._diag(ASM_SYN_TYPE, field, {"reason": "expected_map", "actual": type(val).__name__})
            return {}
        return m


__all__ = ["ParseResult", "parse_assembly", "load_assembly_file", "PORT_DECL_OVERRIDE_FIELDS"]
