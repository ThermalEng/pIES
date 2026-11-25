"""Shared boundary validation helpers for 1.2.0 target computation contracts."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from iesplan.computation.protocols import freeze_json_value, thaw_json_value


def check_finite_json(value: object, name: str = "value") -> None:
    """Recursively reject non-finite floats, non-string mapping keys, and non-JSON values."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{name} 禁止非有限数值: {value!r}")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{name} 映射键须为字符串，得到 {type(key).__name__}")
            check_finite_json(item, f"{name}.{key}")
    elif isinstance(value, (list, tuple)):
        for idx, item in enumerate(value):
            check_finite_json(item, f"{name}[{idx}]")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise TypeError(f"{name} 须为 JSON 值，得到 {type(value).__name__}")


def check_relative_path(path: object, name: str = "path") -> str:
    """Validate a safe relative bundle path (no root prefix, backslash, empty segments, or '..')."""
    if not isinstance(path, str) or not path:
        raise ValueError(f"{name} 须为非空相对路径: {path!r}")
    if "\\" in path or path.startswith("/") or path.endswith("/"):
        raise ValueError(f"{name} 路径越界: {path!r}")
    parts = path.split("/")
    if any(not part or part == ".." for part in parts):
        raise ValueError(f"{name} 路径越界: {path!r}")
    return path


def validate_parameters_schema(
    schema: object, field_name: str = "parameters_schema"
) -> Mapping[str, object]:
    """Validate and freeze a plugin Draft 2020-12 parameters_schema object."""
    if not isinstance(schema, Mapping):
        raise TypeError(f"{field_name} 须为完整 JSON Schema 对象，得到 {type(schema).__name__}")
    raw = thaw_json_value(schema)
    assert isinstance(raw, dict)
    if raw.get("type") != "object":
        raise ValueError(f"{field_name} 根 schema 必须描述 object")
    if raw.get("additionalProperties") is not False:
        raise ValueError(f"{field_name} 必须显式声明 additionalProperties: false")
    check_finite_json(raw, field_name)
    try:
        Draft202012Validator.check_schema(raw)
    except SchemaError as exc:
        raise ValueError(f"{field_name} 自身非法: {exc}") from exc
    return freeze_json_value(raw)


def check_parameters_against_schema(
    schema: Mapping[str, object],
    parameters: Mapping[str, object],
    *,
    location_label: str,
) -> None:
    """Validate stage parameters against a plugin's parameters_schema."""
    if not isinstance(parameters, Mapping):
        raise TypeError(f"{location_label} 须为 Mapping，得到 {type(parameters).__name__}")
    thawed_params = thaw_json_value(parameters)
    check_finite_json(thawed_params, location_label)
    validator = Draft202012Validator(thaw_json_value(schema))
    errors = sorted(validator.iter_errors(thawed_params), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        path = "/".join(str(p) for p in first.absolute_path)
        loc = f"{location_label}/{path}" if path else location_label
        raise ValueError(f"计算参数非法[{loc}]: {first.message}")


def validate_against_schema(
    schema: dict[str, Any],
    data: Mapping[str, Any],
    *,
    error_prefix: str,
) -> None:
    """Validate a plain mapping against a bundled Draft 2020-12 JSON Schema."""
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(dict(data)), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        location = "/".join(str(p) for p in first.absolute_path) or "<root>"
        raise ValueError(f"{error_prefix}[{location}]: {first.message}")
