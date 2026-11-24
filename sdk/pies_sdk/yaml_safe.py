"""YAML 1.2 safe-subset parser and deterministic serializer.

Zero external dependencies. Enforces pIES safe-YAML rules:
- UTF-8, two-space indentation on output, LF line endings
- Rejects tab indentation, anchors (&), aliases (*), merge keys (<<),
  custom/explicit tags (!), duplicate keys, multi-document streams,
  and non-finite floats (NaN/Infinity).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from pies_sdk.errors import SafeYamlError

_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")
_NON_FINITE_TOKENS = frozenset(
    {
        "nan",
        ".nan",
        "+nan",
        "-nan",
        "inf",
        "+inf",
        "-inf",
        ".inf",
        "+.inf",
        "-.inf",
        "infinity",
        "+infinity",
        "-infinity",
    }
)


def load_safe_yaml(text: str) -> Any:
    """Parse a YAML 1.2 safe-subset document into Python objects."""
    if not isinstance(text, str):
        raise TypeError(f"YAML source must be str, got {type(text).__name__}")
    lines = _preprocess(text)
    if not lines:
        return None
    value, next_idx = _parse_block(lines, 0, lines[0][0])
    if next_idx < len(lines):
        raise SafeYamlError("Unexpected trailing YAML content", lines[next_idx][2])
    return value


def _strip_comment(line: str) -> str:
    """Strip unquoted '#' comments."""
    in_single = False
    in_double = False
    escape = False
    for idx, ch in enumerate(line):
        if in_double:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_double = False
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = True
        elif ch == "#" and not in_single and not in_double:
            if idx == 0 or line[idx - 1].isspace():
                return line[:idx]
    return line


def _flow_balance(line: str) -> int:
    """Return net unquoted bracket depth ([ / { minus ] / })."""
    balance = 0
    in_single = False
    in_double = False
    escape = False
    for ch in line:
        if in_double:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_double = False
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = True
        elif not in_single and not in_double:
            if ch in "[{":
                balance += 1
            elif ch in "]}":
                balance -= 1
    return balance


def _preprocess(text: str) -> list[tuple[int, str, int]]:
    """Return [(indent, content, lineno), ...], validating indentation and markers."""
    out: list[tuple[int, str, int]] = []
    raw_lines = text.splitlines()
    idx = 0
    saw_doc_end = False
    saw_content = False

    while idx < len(raw_lines):
        lineno = idx + 1
        raw_line = raw_lines[idx]
        leading_match = re.match(r"^[ \t]*", raw_line)
        leading = leading_match.group(0) if leading_match else ""
        if "\t" in leading:
            raise SafeYamlError("Tab indentation is forbidden in safe YAML", lineno)

        line = _strip_comment(raw_line).rstrip()
        while _flow_balance(line) > 0:
            idx += 1
            if idx >= len(raw_lines):
                raise SafeYamlError("Unclosed flow sequence or mapping", lineno)
            next_raw = raw_lines[idx]
            next_lead = re.match(r"^[ \t]*", next_raw)
            if next_lead and "\t" in next_lead.group(0):
                raise SafeYamlError("Tab indentation is forbidden in safe YAML", idx + 1)
            nxt = _strip_comment(next_raw).strip()
            if nxt:
                line = f"{line} {nxt}"

        stripped = line.strip()
        if not stripped:
            idx += 1
            continue

        if stripped == "---":
            if saw_content or saw_doc_end:
                raise SafeYamlError("Multiple YAML documents are forbidden", lineno)
            idx += 1
            continue

        if stripped == "...":
            saw_doc_end = True
            idx += 1
            continue

        if saw_doc_end:
            raise SafeYamlError("Content after YAML document end marker is forbidden", lineno)

        saw_content = True
        out.append((len(leading), stripped, lineno))
        idx += 1

    return out


def _parse_block(
    lines: list[tuple[int, str, int]], idx: int, indent: int
) -> tuple[Any, int]:
    if idx >= len(lines):
        return None, idx
    ind, content, _ = lines[idx]
    if ind < indent:
        return None, idx
    if content == "-" or content.startswith("- "):
        return _parse_seq(lines, idx, indent)
    return _parse_map(lines, idx, indent)


def _check_safe_key(key: Any, lineno: int) -> str:
    if not isinstance(key, str):
        raise SafeYamlError(f"Mapping key must be a string, got {type(key).__name__}", lineno)
    if key == "<<":
        raise SafeYamlError("YAML merge key '<<' is forbidden", lineno)
    return key


def _parse_map(
    lines: list[tuple[int, str, int]], idx: int, indent: int
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while idx < len(lines):
        ind, content, lineno = lines[idx]
        if ind < indent:
            break
        if ind > indent:
            raise SafeYamlError(f"Unexpected indentation (expected {indent} spaces)", lineno)
        if content == "-" or content.startswith("- "):
            raise SafeYamlError("Sequence item cannot appear directly inside mapping", lineno)

        raw_key, sep, val = _split_key_value(content)
        if not sep:
            raise SafeYamlError(f"Expected 'key: value' mapping entry, got {content!r}", lineno)
        _reject_unsafe_prefix(raw_key, lineno)
        key = _check_safe_key(_parse_scalar(raw_key, lineno), lineno)

        if not val:
            nxt = lines[idx + 1] if idx + 1 < len(lines) else None
            if nxt is not None and nxt[0] > indent:
                value, idx = _parse_block(lines, idx + 1, nxt[0])
            else:
                value, idx = None, idx + 1
        else:
            value, idx = _parse_value(val, lineno), idx + 1

        if key in result:
            raise SafeYamlError(f"Duplicate mapping key: {key!r}", lineno)
        result[key] = value
    return result, idx


def _parse_seq(
    lines: list[tuple[int, str, int]], idx: int, indent: int
) -> tuple[list[Any], int]:
    result: list[Any] = []
    while idx < len(lines):
        ind, content, lineno = lines[idx]
        if ind < indent:
            break
        if ind > indent:
            raise SafeYamlError(f"Unexpected sequence indentation (expected {indent} spaces)", lineno)
        if content == "-":
            rest = ""
        elif content.startswith("- "):
            rest = content[2:].strip()
        else:
            break

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

        _, sep, val = _split_key_value(rest)
        if sep:
            nxt = lines[idx + 1] if idx + 1 < len(lines) else None
            if val or (nxt is not None and nxt[0] >= ind + 2):
                first = (ind + 2, rest, lineno)
                block = [first, *lines[idx + 1 :]]
                item, consumed = _parse_map(block, 0, ind + 2)
                result.append(item)
                idx += consumed
            else:
                raw_k, _, _ = _split_key_value(rest)
                key = _check_safe_key(_parse_scalar(raw_k, lineno), lineno)
                result.append({key: None})
                idx += 1
            continue

        result.append(_parse_value(rest, lineno))
        idx += 1
    return result, idx


def _split_key_value(text: str) -> tuple[str, bool, str]:
    """Split on the first unquoted ':' followed by whitespace or end-of-string."""
    in_single = False
    in_double = False
    escape = False
    for idx, ch in enumerate(text):
        if in_double:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_double = False
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = True
        elif ch == ":" and not in_single and not in_double:
            if idx + 1 == len(text) or text[idx + 1].isspace():
                return text[:idx].strip(), True, text[idx + 1 :].strip()
    return text.strip(), False, ""


def _reject_unsafe_prefix(token: str, lineno: int) -> None:
    stripped = token.strip()
    if not stripped:
        return
    if stripped.startswith(("'", '"')):
        return
    if stripped.startswith("&"):
        raise SafeYamlError(f"YAML anchor is forbidden: {stripped!r}", lineno)
    if stripped.startswith("*"):
        raise SafeYamlError(f"YAML alias is forbidden: {stripped!r}", lineno)
    if stripped.startswith("!"):
        raise SafeYamlError(f"YAML custom tag is forbidden: {stripped!r}", lineno)
    if stripped in ("|", ">", "|-", ">-", "|+", ">+"):
        raise SafeYamlError(f"Block scalar '{stripped}' is unsupported in safe YAML subset", lineno)


def _parse_value(text: str, lineno: int) -> Any:
    text = text.strip()
    if not text:
        return None
    _reject_unsafe_prefix(text, lineno)
    if text.startswith("["):
        return _parse_flow_seq(text, lineno)
    if text.startswith("{"):
        return _parse_flow_map(text, lineno)
    return _parse_scalar(text, lineno)


def _parse_flow_seq(text: str, lineno: int) -> list[Any]:
    if not text.endswith("]"):
        raise SafeYamlError(f"Unclosed flow sequence: {text!r}", lineno)
    parts, depth = _split_flow_items(text[1:-1], lineno)
    if depth != 0:
        raise SafeYamlError("Unbalanced brackets in flow sequence", lineno)
    return [_parse_value(part, lineno) for part in parts]


def _parse_flow_map(text: str, lineno: int) -> dict[str, Any]:
    if not text.endswith("}"):
        raise SafeYamlError(f"Unclosed flow mapping: {text!r}", lineno)
    parts, depth = _split_flow_items(text[1:-1], lineno)
    if depth != 0:
        raise SafeYamlError("Unbalanced braces in flow mapping", lineno)
    result: dict[str, Any] = {}
    for part in parts:
        raw_key, sep, val = _split_key_value(part)
        if not sep:
            raise SafeYamlError(f"Invalid flow mapping entry: {part!r}", lineno)
        _reject_unsafe_prefix(raw_key, lineno)
        key = _check_safe_key(_parse_scalar(raw_key, lineno), lineno)
        if key in result:
            raise SafeYamlError(f"Duplicate mapping key: {key!r}", lineno)
        result[key] = _parse_value(val, lineno)
    return result


def _split_flow_items(inner: str, lineno: int) -> tuple[list[str], int]:
    items: list[str] = []
    buf: list[str] = []
    depth = 0
    in_single = False
    in_double = False
    escape = False
    for ch in inner:
        if in_double:
            buf.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_double = False
            continue
        if in_single:
            buf.append(ch)
            if ch == "'":
                in_single = False
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
            item = "".join(buf).strip()
            if not item:
                raise SafeYamlError("Empty item in flow collection", lineno)
            items.append(item)
            buf = []
        else:
            buf.append(ch)
    if in_single or in_double:
        raise SafeYamlError("Unclosed quote inside flow collection", lineno)
    trailing = "".join(buf).strip()
    if trailing:
        items.append(trailing)
    return items, depth


def _parse_scalar(text: str, lineno: int) -> Any:
    text = text.strip()
    if not text:
        return None
    _reject_unsafe_prefix(text, lineno)
    if text.startswith("'"):
        if len(text) < 2 or not text.endswith("'"):
            raise SafeYamlError(f"Unclosed single-quoted string: {text!r}", lineno)
        return text[1:-1].replace("''", "'")
    if text.startswith('"'):
        if len(text) < 2 or not text.endswith('"'):
            raise SafeYamlError(f"Unclosed double-quoted string: {text!r}", lineno)
        return _unescape_double_quoted(text[1:-1], lineno)

    low = text.lower()
    if low in ("null", "~"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if low in _NON_FINITE_TOKENS:
        raise SafeYamlError(f"Non-finite numeric value is forbidden: {text!r}", lineno)
    if _INT_RE.fullmatch(text):
        return int(text)
    if _FLOAT_RE.fullmatch(text):
        val = float(text)
        if not math.isfinite(val):
            raise SafeYamlError(f"Non-finite float value is forbidden: {text!r}", lineno)
        return val
    return text


def _unescape_double_quoted(raw: str, lineno: int) -> str:
    out: list[str] = []
    idx = 0
    while idx < len(raw):
        ch = raw[idx]
        if ch != "\\":
            out.append(ch)
            idx += 1
            continue
        idx += 1
        if idx >= len(raw):
            raise SafeYamlError("Trailing backslash in double-quoted string", lineno)
        esc = raw[idx]
        if esc == "n":
            out.append("\n")
        elif esc == "t":
            out.append("\t")
        elif esc == "r":
            out.append("\r")
        elif esc == '"':
            out.append('"')
        elif esc == "\\":
            out.append("\\")
        elif esc == "/":
            out.append("/")
        else:
            raise SafeYamlError(f"Unsupported escape sequence '\\{esc}'", lineno)
        idx += 1
    return "".join(out)


def _dump_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SafeYamlError(f"Non-finite float cannot be serialized: {value!r}")
        return repr(value)
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\t", "\\t")
            .replace("\r", "\\r")
        )
        return f'"{escaped}"'
    raise SafeYamlError(f"Unsupported YAML scalar type: {type(value).__name__}")


def dump_safe_yaml(value: Any) -> str:
    """Serialize Python mappings/lists/scalars into deterministic block-style safe YAML."""
    lines: list[str] = []

    def _write(item: Any, indent: int) -> None:
        pad = " " * indent
        if isinstance(item, Mapping):
            if not item:
                lines.append(f"{pad}{{}}")
                return
            for key in sorted(item, key=str):
                if not isinstance(key, str):
                    raise SafeYamlError(f"Mapping key must be str, got {type(key).__name__}")
                child = item[key]
                key_str = _dump_scalar(key)
                if isinstance(child, Mapping) and child:
                    lines.append(f"{pad}{key_str}:")
                    _write(child, indent + 2)
                elif isinstance(child, (list, tuple)) and child:
                    lines.append(f"{pad}{key_str}:")
                    _write(child, indent + 2)
                elif isinstance(child, Mapping):
                    lines.append(f"{pad}{key_str}: {{}}")
                elif isinstance(child, (list, tuple)):
                    lines.append(f"{pad}{key_str}: []")
                else:
                    lines.append(f"{pad}{key_str}: {_dump_scalar(child)}")
        elif isinstance(item, (list, tuple)):
            if not item:
                lines.append(f"{pad}[]")
                return
            for child in item:
                if isinstance(child, Mapping) and child:
                    first_key = sorted(child, key=str)[0]
                    rest = {k: v for k, v in child.items() if k != first_key}
                    first_val = child[first_key]
                    first_key_str = _dump_scalar(str(first_key))
                    if isinstance(first_val, Mapping) and first_val:
                        lines.append(f"{pad}- {first_key_str}:")
                        _write(first_val, indent + 4)
                    elif isinstance(first_val, (list, tuple)) and first_val:
                        lines.append(f"{pad}- {first_key_str}:")
                        _write(first_val, indent + 4)
                    elif isinstance(first_val, Mapping):
                        lines.append(f"{pad}- {first_key_str}: {{}}")
                    elif isinstance(first_val, (list, tuple)):
                        lines.append(f"{pad}- {first_key_str}: []")
                    else:
                        lines.append(f"{pad}- {first_key_str}: {_dump_scalar(first_val)}")
                    if rest:
                        _write(rest, indent + 2)
                elif isinstance(child, Mapping):
                    lines.append(f"{pad}- {{}}")
                elif isinstance(child, (list, tuple)) and child:
                    lines.append(f"{pad}-")
                    _write(child, indent + 2)
                elif isinstance(child, (list, tuple)):
                    lines.append(f"{pad}- []")
                else:
                    lines.append(f"{pad}- {_dump_scalar(child)}")
        else:
            lines.append(f"{pad}{_dump_scalar(item)}")

    _write(value, 0)
    return "\n".join(lines) + "\n"
