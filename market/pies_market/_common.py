"""Shared strict YAML loading and market validation error types (zero backend deps)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pies_sdk import PluginValidationError, check_finite_json


class MarketValidationError(PluginValidationError):
    """Raised when market registry, device model, plugin artifact, or metadata fails validation."""


class _StrictSafeLoader(yaml.SafeLoader):
    """YAML SafeLoader rejecting duplicate mapping keys and anchors/aliases."""


def _no_anchors_compose_node(self: _StrictSafeLoader, parent: Any, index: Any) -> Any:
    event = self.peek_event()
    if getattr(event, "anchor", None):
        raise MarketValidationError(f"YAML anchors are forbidden: &{event.anchor}")
    if isinstance(event, yaml.AliasEvent):
        raise MarketValidationError(f"YAML aliases are forbidden: *{event.anchor}")
    return yaml.SafeLoader.compose_node(self, parent, index)


def _strict_construct_mapping(
    loader: _StrictSafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key == "<<":
            raise MarketValidationError("YAML merge key '<<' is forbidden")
        if key in mapping:
            raise MarketValidationError(f"Duplicate YAML mapping key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.compose_node = _no_anchors_compose_node  # type: ignore[method-assign]
_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _strict_construct_mapping,
)


def load_strict_yaml(path: str | Path, *, label: str = "yaml") -> Mapping[str, Any]:
    """Load a YAML file using PyYAML SafeLoader with duplicate/anchor/alias/NaN checks."""
    target = Path(path)
    if not target.is_file():
        raise MarketValidationError(f"Missing required {label} file: {target}")
    raw_text = target.read_text(encoding="utf-8")
    try:
        docs = list(yaml.load_all(raw_text, Loader=_StrictSafeLoader))
    except MarketValidationError:
        raise
    except yaml.YAMLError as exc:
        raise MarketValidationError(f"Invalid YAML in {target.name}: {exc}") from exc
    if len(docs) != 1 or not isinstance(docs[0], Mapping):
        raise MarketValidationError(f"{target.name} must contain a single YAML mapping")
    data = dict(docs[0])
    check_finite_json(data, label)
    return data
