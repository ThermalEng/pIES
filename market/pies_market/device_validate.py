"""Standalone `ies.device-model@1.0.0` validator for pIES Market (zero `iesplan` deps)."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pies_sdk import check_finite_json, load_safe_yaml, parse_artifact_id

from pies_market._common import MarketValidationError

DEVICE_MODEL_SCHEMA = "ies.device-model"
DEVICE_MODEL_VERSION = "1.0.0"

_REQUIRED_TOP_KEYS = frozenset(
    {"schema", "schema_version", "device", "properties", "interfaces", "equations"}
)
_ID_RE = re.compile(r"^[a-z0-9]+([._-][a-z0-9]+)*$")
_CARRIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_LOCALE_RE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")
_INTERFACE_TYPES = frozenset({"in", "out", "bidirectional", "predefined", "blind"})
_FORBIDDEN_INTERFACE_KEYS = frozenset({"source", "mode", "value", "data_ref", "target_type"})
_FORBIDDEN_MARKET_KEYS = frozenset(
    {
        "price",
        "prices",
        "cost",
        "costs",
        "tariff",
        "tax",
        "currency",
        "capex",
        "opex",
        "optimizable",
        "stock_or_addition",
        "precision",
        "tolerance",
        "entrypoint",
        "module",
        "package",
        "python",
        "script",
        "code",
        "code_path",
        "eval",
        "exec",
        "subprocess",
    }
)

_BINARY_OPS = frozenset({"+", "-", "*", "/", "%", "^"})
_PRECEDENCE: dict[str, int] = {"+": 1, "-": 1, "*": 2, "/": 2, "%": 2, "^": 3}
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_TIME_INDEX_RE = re.compile(r"\[t\s*(?:([-+])\s*([0-9]+))?\]")

_AFFINE_UNITS = frozenset({"c", "°c", "℃", "摄氏度", "f", "°f", "华氏度"})

# Canonical base-dimension exponents so composite SI equivalents align (kW * h == kWh).
_SIMPLE_UNIT_DIMS: dict[str, dict[str, int]] = {}


def _reg_units(dims: dict[str, int], *names: str) -> None:
    for name in names:
        _SIMPLE_UNIT_DIMS[name.lower()] = dims


_reg_units({}, "-", "1", "dimensionless", "无量纲", "%", "percent", "pct", "百分比")
_reg_units(
    {"power": 1, "time": 1},
    "j",
    "焦耳",
    "kj",
    "千焦",
    "mj",
    "兆焦",
    "gj",
    "吉焦",
    "kwh",
    "千瓦时",
    "度",
    "mwh",
    "兆瓦时",
    "gwh",
    "吉瓦时",
    "kcal",
)
_reg_units({"power": 1}, "w", "瓦", "kw", "千瓦", "kwp", "mw", "兆瓦", "mwp", "gw", "吉瓦")
_reg_units({"temperature": 1}, "k", "开尔文", "c", "°c", "℃", "摄氏度", "f", "°f", "华氏度")
_reg_units({"currency": 1}, "cny", "元", "人民币", "usd", "美元")
_reg_units(
    {"time": 1},
    "s",
    "sec",
    "秒",
    "min",
    "分",
    "分钟",
    "minute",
    "h",
    "hr",
    "时",
    "小时",
    "hour",
    "d",
    "day",
    "天",
    "日",
    "月",
    "month",
    "mo",
    "a",
    "yr",
    "year",
    "年",
)
_reg_units({"angle": 1}, "rad", "弧度", "deg", "°", "degree")
_reg_units({"mass": 1}, "kg", "公斤", "千克", "t", "吨", "tco2", "tco₂", "tco2e", "吨二氧化碳")
_reg_units({"volume": 1}, "m³", "m3", "立方米", "方", "千m³", "千立方米", "万m³")
_reg_units({"voltage": 1}, "v", "伏", "kv", "千伏", "mv", "兆伏")
_reg_units({"area": 1}, "m²", "m2", "平方米", "平米")


@dataclass(frozen=True, slots=True)
class _NumNode:
    value: float


@dataclass(frozen=True, slots=True)
class _RefNode:
    name: str
    offset: int
    indexed: bool


@dataclass(frozen=True, slots=True)
class _UnaryNode:
    op: str
    operand: object


@dataclass(frozen=True, slots=True)
class _BinNode:
    op: str
    left: object
    right: object


def _check_no_forbidden_keys(node: Any, path: str) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            key_str = str(key).lower()
            if key_str in _FORBIDDEN_MARKET_KEYS or key_str.startswith("$price"):
                raise MarketValidationError(
                    f"Device model forbids financial, precision, or code field {key!r} at {path}"
                )
            _check_no_forbidden_keys(value, f"{path}.{key}")
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            _check_no_forbidden_keys(item, f"{path}[{idx}]")


def _is_finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _unit_dims(unit: object, *, path: str = "unit") -> Counter[str]:
    if not isinstance(unit, str) or not unit.strip():
        raise MarketValidationError(f"{path} must be a non-empty unit string")
    raw = unit.strip()
    key = raw.lower()
    if key in _SIMPLE_UNIT_DIMS:
        return Counter(_SIMPLE_UNIT_DIMS[key])

    if "/" in raw:
        halves = raw.split("/")
        if len(halves) != 2 or not halves[0].strip() or not halves[1].strip():
            raise MarketValidationError(f"{path} has malformed composite unit {raw!r}")
        num_parts = [p.strip() for p in re.split(r"[·*]", halves[0]) if p.strip()]
        den_parts = [p.strip() for p in re.split(r"[·*]", halves[1]) if p.strip()]
    else:
        num_parts = [p.strip() for p in re.split(r"[·*]", raw) if p.strip()]
        den_parts = []
    if not num_parts:
        raise MarketValidationError(f"{path} has empty numerator in unit {raw!r}")

    dims: Counter[str] = Counter()
    is_composite = len(num_parts) + len(den_parts) > 1
    for sign, tokens in ((1, num_parts), (-1, den_parts)):
        for tok in tokens:
            t_key = tok.lower()
            if is_composite and t_key in _AFFINE_UNITS:
                raise MarketValidationError(
                    f"{path} cannot use affine temperature unit {tok!r} in composite unit {raw!r}"
                )
            base_dims = _SIMPLE_UNIT_DIMS.get(t_key)
            if base_dims is None:
                raise MarketValidationError(f"{path} uses unknown unit {raw!r}")
            for dim_key, power in base_dims.items():
                dims[dim_key] += sign * power
                if dims[dim_key] == 0:
                    del dims[dim_key]
    return dims


def _validate_range(
    raw_range: object,
    *,
    path: str,
    allow_property_ref: bool,
    prop_specs: Mapping[str, tuple[object, Counter[str]]],
    owner_dims: Counter[str] | None = None,
) -> None:
    if not isinstance(raw_range, Mapping) or set(raw_range) != {"minimum", "maximum"}:
        raise MarketValidationError(f"{path} must be a mapping with exact keys {{minimum, maximum}}")

    numeric_bounds: list[float | None] = []
    for side in ("minimum", "maximum"):
        bound = raw_range[side]
        if bound is None:
            numeric_bounds.append(None)
        elif _is_finite_number(bound):
            numeric_bounds.append(float(bound))
        elif allow_property_ref and isinstance(bound, Mapping) and set(bound) == {"property_ref"}:
            ref = bound["property_ref"]
            if not isinstance(ref, str) or ref not in prop_specs:
                raise MarketValidationError(
                    f"{path}.{side}.property_ref references unknown property: {ref!r}"
                )
            prop_val, prop_dims = prop_specs[ref]
            if not _is_finite_number(prop_val):
                raise MarketValidationError(
                    f"{path}.{side}.property_ref {ref!r} must reference a numeric property"
                )
            if owner_dims is not None and prop_dims != owner_dims:
                raise MarketValidationError(
                    f"{path}.{side}.property_ref {ref!r} dimension mismatch: "
                    f"{dict(prop_dims)} != {dict(owner_dims)}"
                )
            numeric_bounds.append(None)
        else:
            raise MarketValidationError(f"{path}.{side} has invalid range bound: {bound!r}")

    lo, hi = numeric_bounds
    if lo is not None and hi is not None and lo > hi:
        raise MarketValidationError(f"{path}.minimum ({lo}) exceeds maximum ({hi})")


def _split_relation(expr: object, *, path: str) -> tuple[str, str]:
    if not isinstance(expr, str) or not expr.strip():
        raise MarketValidationError(f"{path} must be a non-empty string")
    parts = re.split(r"\s*=\s*", expr, maxsplit=1)
    if len(parts) != 2:
        raise MarketValidationError(f"{path} must be of form 'lhs = rhs'")
    lhs, rhs = parts[0].strip(), parts[1].strip()
    if not lhs or not rhs or "=" in rhs:
        raise MarketValidationError(f"{path} must contain exactly one '=' with non-empty sides")
    return lhs, rhs


def _tokenize_expr(expr: str, *, path: str, allow_time_index: bool) -> list[tuple[str, Any]]:
    tokens: list[tuple[str, Any]] = []
    pos, n = 0, len(expr)
    while pos < n:
        ch = expr[pos]
        if ch.isspace():
            pos += 1
            continue
        if ch.isdigit():
            m = _NUMBER_RE.match(expr, pos)
            if m is None:
                raise MarketValidationError(f"{path} contains invalid number at offset {pos}")
            text = m.group(0)
            end = pos + len(text)
            if end < n and (expr[end].isalnum() or expr[end] in "_."):
                raise MarketValidationError(
                    f"{path} has invalid trailing character {expr[end]!r} after number {text!r}"
                )
            tokens.append(("number", float(text)))
            pos = end
            continue
        if ch in "()":
            tokens.append(("paren", ch))
            pos += 1
            continue
        if ch in _BINARY_OPS:
            if ch == "*" and pos + 1 < n and expr[pos + 1] == "*":
                raise MarketValidationError(
                    f"{path} forbids Python power '**'; use '^' for exponentiation"
                )
            tokens.append(("op", ch))
            pos += 1
            continue
        m = _IDENT_RE.match(expr, pos)
        if m is not None:
            name = m.group(0)
            end = pos + len(name)
            if end < n and expr[end] == "(":
                raise MarketValidationError(
                    f"{path} forbids function call {name!r}(...)"
                )
            if end < n and expr[end] == ".":
                raise MarketValidationError(
                    f"{path} forbids attribute access {name!r}."
                )
            offset = 0
            indexed = False
            if end < n and expr[end] == "[":
                if not allow_time_index:
                    raise MarketValidationError(f"{path} forbids time indexing")
                tm = _TIME_INDEX_RE.match(expr, end)
                if tm is None:
                    raise MarketValidationError(
                        f"{path} uses invalid index on {name!r}; only [t] and [t-1] allowed"
                    )
                indexed = True
                if tm.group(1) is not None:
                    step = int(tm.group(2))
                    offset = -step if tm.group(1) == "-" else step
                    if offset not in (0, -1):
                        raise MarketValidationError(
                            f"{path} uses unsupported time index on {name!r}; only [t] and [t-1] allowed"
                        )
                end = tm.end()
            tokens.append(("ident", (name, offset, indexed)))
            pos = end
            continue
        raise MarketValidationError(f"{path} contains unexpected character {ch!r}")
    return tokens


def _parse_expr_side(expr: object, *, path: str, allow_time_index: bool) -> object:
    if not isinstance(expr, str) or not expr.strip():
        raise MarketValidationError(f"{path} must be a non-empty string")
    tokens = _tokenize_expr(expr.strip(), path=path, allow_time_index=allow_time_index)
    if not tokens:
        raise MarketValidationError(f"{path} cannot be empty")
    idx = 0

    def peek() -> tuple[str, Any] | None:
        return tokens[idx] if idx < len(tokens) else None

    def advance() -> tuple[str, Any]:
        nonlocal idx
        tok = tokens[idx]
        idx += 1
        return tok

    def parse_primary() -> object:
        tok = peek()
        if tok is None:
            raise MarketValidationError(f"{path} has unexpected end of expression")
        kind, value = tok
        if kind == "op" and value in ("+", "-"):
            advance()
            return _UnaryNode(op=value, operand=parse_primary())
        if kind == "number":
            advance()
            return _NumNode(value=value)
        if kind == "ident":
            advance()
            name, offset, indexed = value
            return _RefNode(name=name, offset=offset, indexed=indexed)
        if kind == "paren" and value == "(":
            advance()
            inner = parse_expr(0)
            closing = peek()
            if closing is None or closing != ("paren", ")"):
                raise MarketValidationError(f"{path} is missing closing ')'")
            advance()
            return inner
        raise MarketValidationError(f"{path} has unexpected token {value!r}")

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
            advance()
            right = parse_expr(prec if op == "^" else prec + 1)
            left = _BinNode(op=op, left=left, right=right)
        return left

    root = parse_expr(0)
    if idx != len(tokens):
        raise MarketValidationError(f"{path} has unexpected trailing token {tokens[idx][1]!r}")
    return root


def _collect_expr_refs(node: object) -> list[_RefNode]:
    refs: list[_RefNode] = []
    if isinstance(node, _RefNode):
        refs.append(node)
    elif isinstance(node, _UnaryNode):
        refs.extend(_collect_expr_refs(node.operand))
    elif isinstance(node, _BinNode):
        refs.extend(_collect_expr_refs(node.left))
        refs.extend(_collect_expr_refs(node.right))
    return refs


def _initial_dimensions(
    node: object,
    prop_specs: Mapping[str, tuple[object, Counter[str]]],
    *,
    path: str,
) -> Counter[str]:
    if isinstance(node, _NumNode):
        return Counter()
    if isinstance(node, _RefNode):
        if node.indexed or node.offset != 0 or node.name not in prop_specs:
            raise MarketValidationError(
                f"{path} references unknown or non-property symbol {node.name!r}"
            )
        prop_val, prop_dims = prop_specs[node.name]
        if not _is_finite_number(prop_val):
            raise MarketValidationError(
                f"{path} references non-numeric property {node.name!r}"
            )
        return Counter(prop_dims)
    if isinstance(node, _UnaryNode):
        return _initial_dimensions(node.operand, prop_specs, path=path)
    if isinstance(node, _BinNode):
        left = _initial_dimensions(node.left, prop_specs, path=path)
        right = _initial_dimensions(node.right, prop_specs, path=path)
        if node.op in ("+", "-"):
            if left != right:
                raise MarketValidationError(
                    f"{path} adds/subtracts incompatible dimensions: {dict(left)} vs {dict(right)}"
                )
            return left
        if node.op in ("*", "/"):
            factor = 1 if node.op == "*" else -1
            out = Counter(dict(left))
            for dim_key, power in right.items():
                out[dim_key] += factor * power
                if out[dim_key] == 0:
                    del out[dim_key]
            return out
        if node.op == "%":
            if right and left != right:
                raise MarketValidationError(
                    f"{path} modulo incompatible dimensions: {dict(left)} vs {dict(right)}"
                )
            return left
        if node.op == "^":
            if right:
                raise MarketValidationError(f"{path} exponent must be dimensionless")
            if not isinstance(node.right, _NumNode):
                if left:
                    raise MarketValidationError(
                        f"{path} non-constant exponent requires dimensionless base"
                    )
                return Counter()
            exp = node.right.value
            out = Counter()
            for dim_key, power in left.items():
                scaled = power * exp
                if scaled != 0:
                    out[dim_key] = int(scaled) if float(scaled).is_integer() else scaled  # type: ignore[assignment]
            return out
    raise MarketValidationError(f"{path} contains unsupported initial expression node")


def _check_variable_cycles(var_names: set[str], edges: list[tuple[str, tuple[str, ...]]]) -> None:
    graph: dict[str, set[str]] = {vid: set() for vid in var_names}
    for out_var, refs in edges:
        if out_var in graph:
            graph[out_var].update(r for r in refs if r in graph and r != out_var)
    white, gray, black = 0, 1, 2
    color = {vid: white for vid in graph}
    stack: list[str] = []

    def _visit(vid: str) -> None:
        color[vid] = gray
        stack.append(vid)
        for nxt in sorted(graph[vid]):
            if color[nxt] == gray:
                cycle = stack[stack.index(nxt) :] + [nxt]
                raise MarketValidationError(
                    f"equations contains cyclic variable dependency: {' -> '.join(cycle)}"
                )
            if color[nxt] == white:
                _visit(nxt)
        stack.pop()
        color[vid] = black

    for vid in sorted(graph):
        if color[vid] == white:
            _visit(vid)


def validate_device_model_mapping(
    data: Mapping[str, Any],
    *,
    allow_user_namespace: bool = False,
) -> dict[str, Any]:
    """Validate an `ies.device-model@1.0.0` mapping independently of `backend`."""
    if not isinstance(data, Mapping):
        raise MarketValidationError("Device model must be a YAML mapping")
    check_finite_json(data, "device_model")

    keys = set(data)
    if "inputs" in keys:
        raise MarketValidationError("Published market device model must not contain template 'inputs'")
    if keys != _REQUIRED_TOP_KEYS:
        missing = sorted(_REQUIRED_TOP_KEYS - keys)
        extra = sorted(keys - _REQUIRED_TOP_KEYS)
        raise MarketValidationError(
            f"Device model top-level keys invalid: missing={missing!r}, extra={extra!r}"
        )

    if data["schema"] != DEVICE_MODEL_SCHEMA:
        raise MarketValidationError(f"Invalid device model schema: {data['schema']!r}")
    if data["schema_version"] != DEVICE_MODEL_VERSION:
        raise MarketValidationError(f"Invalid device model schema_version: {data['schema_version']!r}")

    _check_no_forbidden_keys(data, "device_model")

    device = data["device"]
    if not isinstance(device, Mapping) or set(device) != {"id", "names"}:
        raise MarketValidationError("device must be a mapping with exact keys {id, names}")
    parse_artifact_id(device["id"], kind="device_model", allow_user_namespace=allow_user_namespace)

    names = device["names"]
    if not isinstance(names, Mapping) or not names:
        raise MarketValidationError("device.names must be a non-empty mapping")
    for locale, label in names.items():
        if not isinstance(locale, str) or not _LOCALE_RE.fullmatch(locale):
            raise MarketValidationError(f"Invalid device.names locale key: {locale!r}")
        if not isinstance(label, str) or not label.strip():
            raise MarketValidationError(f"device.names[{locale!r}] must be a non-empty string")

    props = data["properties"]
    if not isinstance(props, Mapping):
        raise MarketValidationError("properties must be a mapping")
    prop_specs: dict[str, tuple[object, Counter[str]]] = {}
    for pid, praw in props.items():
        if not isinstance(pid, str) or not _ID_RE.fullmatch(pid):
            raise MarketValidationError(f"Invalid property id: {pid!r}")
        if not isinstance(praw, Mapping):
            raise MarketValidationError(f"properties.{pid} must be a mapping")
        allowed_pkeys = {"value", "unit", "valid_range"}
        if not {"value", "unit"} <= set(praw) or not set(praw) <= allowed_pkeys:
            raise MarketValidationError(f"properties.{pid} has invalid keys: {sorted(praw)!r}")
        val = praw["value"]
        if not (isinstance(val, (bool, str)) or _is_finite_number(val)):
            raise MarketValidationError(f"properties.{pid}.value must be a finite scalar")
        p_dims = _unit_dims(praw["unit"], path=f"properties.{pid}.unit")
        if praw.get("valid_range") is not None:
            _validate_range(
                praw["valid_range"],
                path=f"properties.{pid}.valid_range",
                allow_property_ref=False,
                prop_specs={},
            )
        prop_specs[pid] = (val, p_dims)
    prop_names = set(prop_specs)

    ifaces = data["interfaces"]
    if not isinstance(ifaces, Mapping):
        raise MarketValidationError("interfaces must be a mapping")
    iface_types: dict[str, str] = {}
    for iid, iraw in ifaces.items():
        if not isinstance(iid, str) or not _ID_RE.fullmatch(iid):
            raise MarketValidationError(f"Invalid interface id: {iid!r}")
        if not isinstance(iraw, Mapping):
            raise MarketValidationError(f"interfaces.{iid} must be a mapping")
        bad_src = sorted(_FORBIDDEN_INTERFACE_KEYS & set(iraw))
        if bad_src:
            raise MarketValidationError(
                f"interfaces.{iid} forbids project-level source fields: {bad_src!r}"
            )
        allowed_ikeys = {"type", "carrier", "unit", "valid_range"}
        if not {"carrier", "unit", "valid_range"} <= set(iraw) or not set(iraw) <= allowed_ikeys:
            raise MarketValidationError(f"interfaces.{iid} has invalid keys: {sorted(iraw)!r}")
        iface_type = iraw.get("type", "blind")
        if iface_type not in _INTERFACE_TYPES:
            raise MarketValidationError(f"interfaces.{iid}.type invalid: {iface_type!r}")
        carrier = iraw["carrier"]
        if not isinstance(carrier, str) or not _CARRIER_RE.fullmatch(carrier):
            raise MarketValidationError(f"interfaces.{iid}.carrier invalid: {carrier!r}")
        iface_dims = _unit_dims(iraw["unit"], path=f"interfaces.{iid}.unit")
        _validate_range(
            iraw["valid_range"],
            path=f"interfaces.{iid}.valid_range",
            allow_property_ref=True,
            prop_specs=prop_specs,
            owner_dims=iface_dims,
        )
        iface_types[iid] = iface_type
    iface_names = set(iface_types)

    equations = data["equations"]
    if not isinstance(equations, Mapping) or set(equations) != {"variables", "relations"}:
        raise MarketValidationError("equations must be a mapping with exact keys {variables, relations}")
    variables = equations["variables"]
    if not isinstance(variables, Mapping):
        raise MarketValidationError("equations.variables must be a mapping")
    var_specs: dict[str, tuple[Counter[str], bool]] = {}
    for vid, vraw in variables.items():
        if not isinstance(vid, str) or not _ID_RE.fullmatch(vid):
            raise MarketValidationError(f"Invalid equations.variables key: {vid!r}")
        if not isinstance(vraw, Mapping):
            raise MarketValidationError(f"equations.variables.{vid} must be a mapping")
        if "unit" not in vraw or not set(vraw) <= {"unit", "valid_range", "initial"}:
            raise MarketValidationError(f"equations.variables.{vid} has invalid keys: {sorted(vraw)!r}")
        var_dims = _unit_dims(vraw["unit"], path=f"equations.variables.{vid}.unit")
        if vraw.get("valid_range") is not None:
            _validate_range(
                vraw["valid_range"],
                path=f"equations.variables.{vid}.valid_range",
                allow_property_ref=True,
                prop_specs=prop_specs,
                owner_dims=var_dims,
            )
        has_initial = False
        if vraw.get("initial") is not None:
            init = vraw["initial"]
            if not isinstance(init, Mapping) or set(init) not in ({"property_ref"}, {"expression"}):
                raise MarketValidationError(
                    f"equations.variables.{vid}.initial must specify property_ref or expression"
                )
            if "property_ref" in init:
                pref = init["property_ref"]
                if not isinstance(pref, str) or pref not in prop_specs:
                    raise MarketValidationError(
                        f"equations.variables.{vid}.initial.property_ref unknown: {pref!r}"
                    )
                prop_val, prop_dims = prop_specs[pref]
                if not _is_finite_number(prop_val):
                    raise MarketValidationError(
                        f"equations.variables.{vid}.initial.property_ref {pref!r} must be numeric"
                    )
                if prop_dims != var_dims:
                    raise MarketValidationError(
                        f"equations.variables.{vid}.initial dimension mismatch: "
                        f"{dict(prop_dims)} != {dict(var_dims)}"
                    )
            else:
                init_path = f"equations.variables.{vid}.initial.expression"
                init_root = _parse_expr_side(init["expression"], path=init_path, allow_time_index=False)
                init_dims = _initial_dimensions(init_root, prop_specs, path=init_path)
                if init_dims != var_dims:
                    raise MarketValidationError(
                        f"equations.variables.{vid}.initial dimension mismatch: "
                        f"{dict(init_dims)} != {dict(var_dims)}"
                    )
            has_initial = True
        var_specs[vid] = (var_dims, has_initial)
    var_names = set(var_specs)

    relations = equations["relations"]
    if not isinstance(relations, list):
        raise MarketValidationError("equations.relations must be a list")
    allowed_rel_names = prop_names | iface_names | var_names | {"step_duration"}
    series_names = iface_names | var_names
    seen_rel_ids: set[str] = set()
    defined_outputs: dict[str, str] = {}
    relation_edges: list[tuple[str, tuple[str, ...]]] = []
    vars_with_prev_ref: set[str] = set()

    for idx, rel in enumerate(relations):
        if not isinstance(rel, Mapping) or set(rel) != {"id", "expression"}:
            raise MarketValidationError(
                f"equations.relations[{idx}] must be a mapping with exact keys {{id, expression}}"
            )
        rid = rel["id"]
        if not isinstance(rid, str) or not _ID_RE.fullmatch(rid):
            raise MarketValidationError(f"Invalid equations.relations[{idx}].id: {rid!r}")
        if rid in seen_rel_ids:
            raise MarketValidationError(f"Duplicate relation id: {rid!r}")
        seen_rel_ids.add(rid)

        lhs_text, rhs_text = _split_relation(
            rel["expression"],
            path=f"equations.relations[{idx}].expression",
        )
        lhs_root = _parse_expr_side(
            lhs_text,
            path=f"equations.relations[{idx}].expression(lhs)",
            allow_time_index=True,
        )
        rhs_root = _parse_expr_side(
            rhs_text,
            path=f"equations.relations[{idx}].expression(rhs)",
            allow_time_index=True,
        )
        lhs_refs = _collect_expr_refs(lhs_root)
        rhs_refs = _collect_expr_refs(rhs_root)

        for ref in lhs_refs + rhs_refs:
            if ref.name not in allowed_rel_names:
                raise MarketValidationError(
                    f"equations.relations[{idx}] references undeclared symbol {ref.name!r}"
                )
            if ref.name not in series_names and (ref.indexed or ref.offset != 0):
                raise MarketValidationError(
                    f"equations.relations[{idx}] cannot time-index scalar symbol {ref.name!r}"
                )
            if ref.name in iface_types and iface_types[ref.name] == "blind":
                raise MarketValidationError(
                    f"equations.relations[{idx}] cannot reference blind interface {ref.name!r}"
                )
            if ref.name in var_names and ref.offset == -1:
                vars_with_prev_ref.add(ref.name)

        if len(lhs_refs) not in (1, 2):
            raise MarketValidationError(
                f"equations.relations[{idx}].expression LHS must reference 1 or 2 declared "
                "interfaces/variables"
            )
        if any(ref.name not in series_names for ref in lhs_refs):
            raise MarketValidationError(
                f"equations.relations[{idx}].expression LHS cannot target scalar property or step_duration"
            )
        if len(lhs_refs) == 1:
            output = lhs_refs[0].name
            if iface_types.get(output) == "predefined":
                raise MarketValidationError(
                    f"equations.relations[{idx}] LHS cannot assign predefined interface {output!r}"
                )
            if output in defined_outputs:
                raise MarketValidationError(
                    f"Output {output!r} defined by multiple relations: "
                    f"{defined_outputs[output]!r} and {rid!r}"
                )
            defined_outputs[output] = rid
            if output in var_names:
                relation_edges.append((output, tuple(ref.name for ref in rhs_refs)))

    for vid in sorted(vars_with_prev_ref):
        if not var_specs[vid][1]:
            raise MarketValidationError(
                f"State variable {vid!r} referenced at [t-1] must declare initial"
            )

    _check_variable_cycles(var_names, relation_edges)
    return dict(data)


def validate_device_model_file(
    path: str | Path,
    *,
    allow_user_namespace: bool = False,
) -> dict[str, Any]:
    """Load safe YAML from `path` and validate as `ies.device-model@1.0.0`."""
    target = Path(path)
    if not target.is_file():
        raise MarketValidationError(f"Device model file does not exist: {target}")
    raw = load_safe_yaml(target.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise MarketValidationError(f"Device model {target.name} must contain a YAML mapping")
    return validate_device_model_mapping(raw, allow_user_namespace=allow_user_namespace)
