"""Standalone `ies.device-model@1.0.0` validator for pIES Market (zero `iesplan` deps)."""

from __future__ import annotations

import ast
import math
import re
from collections.abc import Mapping
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
_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod)
_ALLOWED_UNARYOPS = (ast.UAdd, ast.USub)


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


def _validate_range(
    raw_range: object,
    *,
    path: str,
    allow_property_ref: bool,
    property_names: set[str],
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
            if not isinstance(ref, str) or ref not in property_names:
                raise MarketValidationError(
                    f"{path}.{side}.property_ref references unknown property: {ref!r}"
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


def _is_valid_time_slice(node: ast.AST) -> bool:
    if isinstance(node, ast.Name) and node.id == "t":
        return True
    if (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Sub)
        and isinstance(node.left, ast.Name)
        and node.left.id == "t"
        and isinstance(node.right, ast.Constant)
        and not isinstance(node.right.value, bool)
        and isinstance(node.right.value, int)
        and node.right.value == 1
    ):
        return True
    return False


def _validate_math_ast(
    expr: object,
    *,
    path: str,
    allowed_names: set[str],
    allow_time_index: bool,
    time_indexed_names: set[str],
) -> tuple[str, ...]:
    """Parse expression with restricted AST whitelist and return referenced identifier names."""
    if not isinstance(expr, str) or not expr.strip():
        raise MarketValidationError(f"{path} must be a non-empty string")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as exc:
        raise MarketValidationError(f"{path} has invalid expression syntax: {exc.msg}") from exc

    refs: list[str] = []

    def _visit(node: ast.AST) -> None:
        if isinstance(node, ast.Expression):
            _visit(node.body)
        elif isinstance(node, ast.BinOp):
            if not isinstance(node.op, _ALLOWED_BINOPS):
                raise MarketValidationError(f"{path} uses unsupported operator: {type(node.op).__name__}")
            _visit(node.left)
            _visit(node.right)
        elif isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, _ALLOWED_UNARYOPS):
                raise MarketValidationError(
                    f"{path} uses unsupported unary operator: {type(node.op).__name__}"
                )
            _visit(node.operand)
        elif isinstance(node, ast.Constant):
            if not _is_finite_number(node.value):
                raise MarketValidationError(f"{path} only allows finite numeric literals: {node.value!r}")
        elif isinstance(node, ast.Name):
            if node.id not in allowed_names:
                raise MarketValidationError(f"{path} references undeclared symbol {node.id!r}")
            refs.append(node.id)
        elif isinstance(node, ast.Subscript):
            if not allow_time_index:
                raise MarketValidationError(f"{path} forbids time indexing")
            if not isinstance(node.value, ast.Name):
                raise MarketValidationError(f"{path} subscript target must be a declared identifier")
            target = node.value.id
            if target not in allowed_names:
                raise MarketValidationError(f"{path} references undeclared symbol {target!r}")
            if target not in time_indexed_names:
                raise MarketValidationError(
                    f"{path} cannot time-index scalar symbol {target!r}"
                )
            if not _is_valid_time_slice(node.slice):
                raise MarketValidationError(
                    f"{path} uses unsupported time index on {target!r}; only [t] and [t-1] allowed"
                )
            refs.append(target)
        else:
            raise MarketValidationError(
                f"{path} contains forbidden expression construct: {type(node).__name__}"
            )

    _visit(tree)
    return tuple(refs)


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
    prop_names: set[str] = set()
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
        if not isinstance(praw["unit"], str) or not praw["unit"].strip():
            raise MarketValidationError(f"properties.{pid}.unit must be a non-empty string")
        if praw.get("valid_range") is not None:
            _validate_range(
                praw["valid_range"],
                path=f"properties.{pid}.valid_range",
                allow_property_ref=False,
                property_names=set(),
            )
        prop_names.add(pid)

    ifaces = data["interfaces"]
    if not isinstance(ifaces, Mapping):
        raise MarketValidationError("interfaces must be a mapping")
    iface_names: set[str] = set()
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
        if not isinstance(iraw["unit"], str) or not iraw["unit"].strip():
            raise MarketValidationError(f"interfaces.{iid}.unit must be a non-empty string")
        _validate_range(
            iraw["valid_range"],
            path=f"interfaces.{iid}.valid_range",
            allow_property_ref=True,
            property_names=prop_names,
        )
        iface_names.add(iid)

    equations = data["equations"]
    if not isinstance(equations, Mapping) or set(equations) != {"variables", "relations"}:
        raise MarketValidationError("equations must be a mapping with exact keys {variables, relations}")
    variables = equations["variables"]
    if not isinstance(variables, Mapping):
        raise MarketValidationError("equations.variables must be a mapping")
    var_names: set[str] = set()
    for vid, vraw in variables.items():
        if not isinstance(vid, str) or not _ID_RE.fullmatch(vid):
            raise MarketValidationError(f"Invalid equations.variables key: {vid!r}")
        if not isinstance(vraw, Mapping):
            raise MarketValidationError(f"equations.variables.{vid} must be a mapping")
        if "unit" not in vraw or not set(vraw) <= {"unit", "valid_range", "initial"}:
            raise MarketValidationError(f"equations.variables.{vid} has invalid keys: {sorted(vraw)!r}")
        if not isinstance(vraw["unit"], str) or not vraw["unit"].strip():
            raise MarketValidationError(f"equations.variables.{vid}.unit must be a non-empty string")
        if vraw.get("valid_range") is not None:
            _validate_range(
                vraw["valid_range"],
                path=f"equations.variables.{vid}.valid_range",
                allow_property_ref=True,
                property_names=prop_names,
            )
        if vraw.get("initial") is not None:
            init = vraw["initial"]
            if not isinstance(init, Mapping) or set(init) not in ({"property_ref"}, {"expression"}):
                raise MarketValidationError(
                    f"equations.variables.{vid}.initial must specify property_ref or expression"
                )
            if "property_ref" in init:
                if init["property_ref"] not in prop_names:
                    raise MarketValidationError(
                        f"equations.variables.{vid}.initial.property_ref unknown: "
                        f"{init['property_ref']!r}"
                    )
            else:
                _validate_math_ast(
                    init["expression"],
                    path=f"equations.variables.{vid}.initial.expression",
                    allowed_names=prop_names,
                    allow_time_index=False,
                    time_indexed_names=set(),
                )
        var_names.add(vid)

    relations = equations["relations"]
    if not isinstance(relations, list):
        raise MarketValidationError("equations.relations must be a list")
    allowed_rel_names = prop_names | iface_names | var_names | {"step_duration"}
    time_indexed_names = iface_names | var_names
    seen_rel_ids: set[str] = set()
    relation_edges: list[tuple[str, tuple[str, ...]]] = []
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
        lhs, rhs = _split_relation(
            rel["expression"],
            path=f"equations.relations[{idx}].expression",
        )
        lhs_refs = _validate_math_ast(
            lhs,
            path=f"equations.relations[{idx}].expression(lhs)",
            allowed_names=allowed_rel_names,
            allow_time_index=True,
            time_indexed_names=time_indexed_names,
        )
        lhs_series_refs = [name for name in lhs_refs if name in time_indexed_names]
        if len(lhs_refs) not in (1, 2) or not lhs_series_refs:
            raise MarketValidationError(
                f"equations.relations[{idx}].expression LHS must reference 1 or 2 declared "
                "interfaces/variables"
            )
        rhs_refs = _validate_math_ast(
            rhs,
            path=f"equations.relations[{idx}].expression(rhs)",
            allowed_names=allowed_rel_names,
            allow_time_index=True,
            time_indexed_names=time_indexed_names,
        )
        relation_edges.append((lhs_series_refs[0], rhs_refs))

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
