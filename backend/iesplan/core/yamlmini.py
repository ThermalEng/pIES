"""极简 YAML 子集解析器(内置,零第三方依赖)。

背景: pyproject.toml 未声明 PyYAML 依赖;设备目录、装配文档等公开文件契约
统一使用本安全子集。解析器属于 core 纯数据能力,不依赖任何业务域。

支持子集(覆盖设备 YAML、价格表与装配文档所需语法):
- 块映射(缩进式,键值对)与块序列("- 项");
- 流式序列 [a, b, c] 与流式映射 {k: v, ...}(可嵌套);
- 标量: null/bool/int/float/单双引号字符串/普通字符串;
- '#' 注释(引号外)与文档起始标记 '---'(仅取首个文档)。

不支持: 多行字符串、锚点/别名、合并键、多文档。解析失败抛 YamlParseError(含行号)。
"""

from __future__ import annotations

import re

_INT_RE = re.compile(r"[+-]?\d+")
_FLOAT_RE = re.compile(r"[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?")


class YamlParseError(ValueError):
    """yaml 子集解析错误(携带行号)。"""

    def __init__(self, message: str, line: int) -> None:
        self.line = line
        super().__init__(f"第 {line} 行: {message}")


def load(text: str) -> object:
    """解析 YAML 子集文本,返回 dict / list / 标量。"""
    lines = _preprocess(text)
    if not lines:
        return None
    value, _ = _parse_block(lines, 0, lines[0][0])
    return value


# ---------------------------------------------------------------------------
# 预处理: 注释剔除 / 空行剔除 / 缩进计算
# ---------------------------------------------------------------------------


def _strip_comment(line: str) -> str:
    """剔除引号外的 '#' 注释。"""
    in_single = in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:i]
    return line


def _flow_balance(line: str) -> int:
    """引号外流式括号([{ vs ]})的净深度(>0 表示行内括号未闭合)。"""
    balance = 0
    in_single = in_double = False
    for ch in line:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch in "[{":
                balance += 1
            elif ch in "]}":
                balance -= 1
    return balance


def _preprocess(text: str) -> list[tuple[int, str, int]]:
    """返回 [(缩进列数, 内容, 行号), ...];剔除空行/纯注释/文档起始标记。

    多行流式值(如换行书写的 `{a: 1,\n  b: 2}`)在解析前按行合并为单行,
    保持首行缩进;括号始终未闭合(用户 yaml 错误)留待值解析时报"未闭合"。
    """
    out: list[tuple[int, str, int]] = []
    i = 0
    raw_lines = text.splitlines()
    while i < len(raw_lines):
        lineno = i + 1
        line = _strip_comment(raw_lines[i]).rstrip()
        # 流式括号未闭合: 续行合并
        while _flow_balance(line) > 0:
            i += 1
            if i >= len(raw_lines):
                break
            nxt = _strip_comment(raw_lines[i]).strip()
            if nxt:
                line = f"{line} {nxt}"
        if not line.strip():
            i += 1
            continue
        m = re.match(r"^[ \t]*", line)
        leading = m.group(0) if m else ""
        if "\t" in leading:
            raise YamlParseError("缩进不得使用制表符", lineno)
        content = line[len(leading) :].strip()
        if content == "---":
            i += 1
            continue
        out.append((len(leading), content, lineno))
        i += 1
    return out


# ---------------------------------------------------------------------------
# 块级解析
# ---------------------------------------------------------------------------


def _parse_block(
    lines: list[tuple[int, str, int]], idx: int, indent: int
) -> tuple[object, int]:
    """解析从 idx 开始、缩进 >= indent 的块;返回 (值, 下一行索引)。"""
    if idx >= len(lines):
        return None, idx
    ind, content, _ = lines[idx]
    if ind < indent:
        return None, idx
    if content.startswith("- ") or content == "-":
        return _parse_seq(lines, idx, indent)
    return _parse_map(lines, idx, indent)


def _parse_map(
    lines: list[tuple[int, str, int]], idx: int, indent: int
) -> tuple[dict, int]:
    """解析块映射(缩进 == indent 的键值行);返回 (dict, 下一行索引)。

    重复键直接拒绝(YAML 1.2 安全子集要求, 宪法 §7.8);不允许静默覆盖。
    """
    result: dict = {}
    while idx < len(lines):
        ind, content, lineno = lines[idx]
        if ind < indent:
            break
        if ind > indent:
            raise YamlParseError(f"非法缩进: 内容应缩进 {indent} 列", lineno)
        if content.startswith("- "):
            raise YamlParseError("映射中不得出现序列项", lineno)
        key, sep, val = _split_key_value(content)
        # 键按标量语义解析(剥引号; 裸标识符行为不变, 引号键是 YAML 合法形态)
        key = _parse_scalar(key, lineno)
        # 空值键("key:" 或 "key: "), 且下一行缩进更深 → 该键的嵌套块
        if (not sep or not val) and not content.startswith("-"):
            nxt = lines[idx + 1] if idx + 1 < len(lines) else None
            if nxt is not None and nxt[0] > indent:
                value, idx = _parse_block(lines, idx + 1, nxt[0])
            else:
                value, idx = None, idx + 1
        else:
            value, idx = _parse_value(val, lineno), idx + 1
        if key in result:
            raise YamlParseError(f"重复键: {key!r}", lineno)
        result[key] = value
    return result, idx


def _parse_seq(
    lines: list[tuple[int, str, int]], idx: int, indent: int
) -> tuple[list, int]:
    """解析块序列(缩进 == indent 的 '- 项' 行);返回 (list, 下一行索引)。"""
    result: list = []
    while idx < len(lines):
        ind, content, lineno = lines[idx]
        if ind < indent:
            break
        if ind > indent:
            raise YamlParseError(f"非法缩进: 序列项应缩进 {indent} 列", lineno)
        if content == "-":
            rest = ""
        elif content.startswith("- "):
            rest = content[2:].strip()
        else:
            break  # 同级非序列行: 由上层处理
        if not rest:
            nxt = lines[idx + 1] if idx + 1 < len(lines) else None
            if nxt is not None and nxt[0] > indent:
                item, idx = _parse_block(lines, idx + 1, nxt[0])
            else:
                item, idx = None, idx + 1
            result.append(item)
            continue
        if rest.startswith("{") or rest.startswith("["):
            result.append(_parse_value(rest, lineno))
            idx += 1
            continue
        key, sep, val = _split_key_value(rest)
        if sep:
            # "- key: value" 内联块映射;后续同缩进行为同映射续写键
            nxt = lines[idx + 1] if idx + 1 < len(lines) else None
            if val or (nxt is not None and nxt[0] >= ind + 2):
                first = (ind + 2, content[2:].strip(), lineno)
                block = [first, *lines[idx + 1 :]]
                item, next_in_block = _parse_map(block, 0, ind + 2)
                result.append(item)
                idx += next_in_block
            else:
                result.append({key: None})
                idx += 1
            continue
        result.append(_parse_value(rest, lineno))
        idx += 1
    return result, idx


# ---------------------------------------------------------------------------
# 行级 / 流式解析
# ---------------------------------------------------------------------------


def _split_key_value(text: str) -> tuple[str, bool, str]:
    """按引号外的首个 ':' 拆分键值;返回 (键, 是否有分隔符, 值)。"""
    in_single = in_double = False
    for i, ch in enumerate(text):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == ":" and not in_single and not in_double:
            return text[:i].strip(), True, text[i + 1 :].strip()
    return text.strip(), False, ""


def _parse_value(text: str, lineno: int) -> object:
    """解析值: 流式序列 / 流式映射 / 标量。"""
    text = text.strip()
    if not text:
        return None
    if text.startswith("["):
        return _parse_flow_seq(text, lineno)
    if text.startswith("{"):
        return _parse_flow_map(text, lineno)
    return _parse_scalar(text, lineno)


def _parse_flow_seq(text: str, lineno: int) -> list:
    inner = text[1:]
    if not inner.endswith("]"):
        raise YamlParseError(f"流式序列未闭合: {text!r}", lineno)
    parts, depth = _split_flow_items(inner[:-1])
    if depth:
        raise YamlParseError("流式序列括号未闭合", lineno)
    return [_parse_value(part, lineno) for part in parts]


def _parse_flow_map(text: str, lineno: int) -> dict:
    inner = text[1:]
    if not inner.endswith("}"):
        raise YamlParseError(f"流式映射未闭合: {text!r}", lineno)
    parts, depth = _split_flow_items(inner[:-1])
    if depth:
        raise YamlParseError("流式映射括号未闭合", lineno)
    result: dict = {}
    for part in parts:
        key, sep, val = _split_key_value(part)
        if not sep:
            raise YamlParseError(f"流式映射项非法: {part!r}", lineno)
        if key in result:
            raise YamlParseError(f"重复键: {key!r}", lineno)
        result[key] = _parse_value(val, lineno)
    return result


def _split_flow_items(inner: str) -> tuple[list[str], int]:
    """按顶层逗号拆分流式内容(引号与嵌套括号内不拆分);返回 (项列表, 剩余深度)。"""
    items: list[str] = []
    buf: list[str] = []
    depth = 0
    in_single = in_double = False
    for ch in inner:
        if in_single:
            buf.append(ch)
            if ch == "'":
                in_single = False
            continue
        if in_double:
            buf.append(ch)
            if ch == '"':
                in_double = False
            continue
        if ch == "'":
            in_single = True
            buf.append(ch)
        elif ch == '"':
            in_double = True
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        items.append("".join(buf).strip())
    return [it for it in items if it], depth


def _parse_scalar(text: str, lineno: int) -> object:
    """标量解析: null/bool/int/float/引号字符串/普通字符串。"""
    text = text.strip()
    if not text:
        return None
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("''", "'")
    if text.startswith('"') and text.endswith('"'):
        return (
            text[1:-1]
            .replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )
    low = text.lower()
    if low in ("null", "~"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if _INT_RE.fullmatch(text):
        return int(text)
    if _FLOAT_RE.fullmatch(text):
        return float(text)
    return text


# ---------------------------------------------------------------------------
# 序列化(block 风格;与 load 子集互逆, 供项目包/导入导出 YAML 文件使用)
# ---------------------------------------------------------------------------


def _dump_scalar(value: object) -> str:
    """标量序列化: 字符串一律带双引号(保证与 load 解析 round-trip)。"""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    raise YamlParseError(f"不支持的标量类型: {type(value).__name__}", 0)


def dump(value: object) -> str:
    """对象 → block 风格 YAML 文本(与 load 互逆, 稳定键序, 无锚点/别名)。

    支持 dict/list/标量; dict 键按字符串排序输出, 嵌套缩进 2 空格;
    LF 换行; 非 ASCII 字符原样保留。round-trip 保证:
    ``load(dump(x)) == x``。
    """
    lines: list[str] = []

    def _write(item: object, indent: int) -> None:
        pad = " " * indent
        if isinstance(item, dict):
            if not item:
                lines.append(f"{pad}{{}}")
                return
            for key in sorted(item, key=str):
                child = item[key]
                if isinstance(child, dict) and child:
                    lines.append(f"{pad}{_dump_scalar(str(key))}:")
                    _write(child, indent + 2)
                elif isinstance(child, list) and child:
                    lines.append(f"{pad}{_dump_scalar(str(key))}:")
                    _write(child, indent + 2)
                elif isinstance(child, dict) or isinstance(child, list):
                    lines.append(
                        f"{pad}{_dump_scalar(str(key))}: "
                        f"{'{}' if isinstance(child, dict) else '[]'}"
                    )
                else:
                    lines.append(f"{pad}{_dump_scalar(str(key))}: {_dump_scalar(child)}")
        elif isinstance(item, list):
            for child in item:
                if isinstance(child, dict) and child:
                    first = sorted(child, key=str)[0]
                    rest = {k: v for k, v in child.items() if k != first}
                    if isinstance(rest, dict) and not rest:
                        lines.append(
                            f"{pad}- {_dump_scalar(str(first))}: {_dump_scalar(child[first])}"
                        )
                        continue
                    lines.append(
                        f"{pad}- {_dump_scalar(str(first))}: {_dump_scalar(child[first])}"
                    )
                    # 续写键缩进 = list 行缩进 + 2(- 后的映射续写规则)
                    _write(rest, indent + 2)
                elif isinstance(child, dict):
                    lines.append(f"{pad}- {{}}")
                elif isinstance(child, list) and child:
                    lines.append(f"{pad}- ")
                    _write(child, indent + 2)
                elif isinstance(child, list):
                    lines.append(f"{pad}- []")
                else:
                    lines.append(f"{pad}- {_dump_scalar(child)}")
        else:
            lines.append(f"{pad}{_dump_scalar(item)}")

    _write(value, 0)
    return "\n".join(lines) + "\n"
