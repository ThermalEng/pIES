"""Shared boundary validation helpers for 1.2.0 computation contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from iesplan.computation.protocols import (
    check_finite_json,
    check_relative_path,
    freeze_json_value,
    thaw_json_value,
)


def validate_parameters_schema(
    schema: object, field_name: str = "parameters_schema"
) -> Mapping[str, object]:
    """Validate and freeze a plugin Draft 2020-12 parameters_schema / options_schema object."""
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


__all__ = [
    "check_finite_json",
    "check_parameters_against_schema",
    "check_relative_path",
    "validate_against_schema",
    "validate_parameters_schema",
]
