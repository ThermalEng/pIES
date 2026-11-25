"""Manifest schema, safe-YAML parser, validator, and compatibility checker."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from pies_sdk.contracts import check_finite_json
from pies_sdk.errors import CompatibilityError, PluginValidationError
from pies_sdk.yaml_safe import load_safe_yaml

PLUGIN_SCHEMA = "ies.algorithm-plugin"
PLUGIN_SCHEMA_VERSION = "1.0.0"
PLUGIN_KINDS = ("predictor", "generator", "solver", "evaluator", "algorithm")

CURRENT_PIES_VERSION = "1.1.5"
CURRENT_SDK_VERSION = "1.1.5"

_SCHEMA_PATH = Path(__file__).parent / "schema" / "plugin-1.0.0.schema.json"
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_CLAUSE_RE = re.compile(r"^(>=|<=|>|<|==|!=)?\s*(\d+\.\d+\.\d+)$")


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    cpu: float
    memory_mb: int
    timeout_seconds: int
    process_limit: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu": self.cpu,
            "memory_mb": self.memory_mb,
            "timeout_seconds": self.timeout_seconds,
            "process_limit": self.process_limit,
        }


@dataclass(frozen=True, slots=True)
class ResourcesConfig:
    default: ResourceSpec
    maximum: ResourceSpec

    def to_dict(self) -> dict[str, Any]:
        return {
            "default": self.default.to_dict(),
            "maximum": self.maximum.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class RuntimeSpec:
    language: str
    python: str
    requirements: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"language": self.language, "python": self.python}
        if self.requirements is not None:
            out["requirements"] = self.requirements
        return out


@dataclass(frozen=True, slots=True)
class IterationSpec:
    enabled: bool
    max_iterations_default: int | None = None
    convergence_metric: str | None = None
    progress_channel: str = "/work/out/iterations.jsonl"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "enabled": self.enabled,
            "progress_channel": self.progress_channel,
        }
        if self.max_iterations_default is not None:
            out["max_iterations_default"] = self.max_iterations_default
        if self.convergence_metric is not None:
            out["convergence_metric"] = self.convergence_metric
        return out


@dataclass(frozen=True, slots=True)
class PluginTestSpec:
    id: str
    input: str
    expected: str
    mode: str = "file"

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise PluginValidationError("Plugin test 'id' must be a non-empty string")
        if self.mode not in ("file", "directory"):
            raise PluginValidationError(f"Unsupported plugin test mode: {self.mode!r}")
        if not isinstance(self.input, str) or not self.input:
            raise PluginValidationError(f"Plugin test {self.id!r} requires non-empty input path")
        if not isinstance(self.expected, str) or not self.expected:
            raise PluginValidationError(f"Plugin test {self.id!r} requires non-empty expected path")

    @property
    def is_directory(self) -> bool:
        return self.mode == "directory"

    def to_dict(self) -> dict[str, Any]:
        input_key = "in" if self.mode == "directory" else "input"
        return {"id": self.id, input_key: self.input, "expected": self.expected}


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """Validated, immutable representation of plugin.yaml (ies.algorithm-plugin@1.0.0)."""

    schema: str
    schema_version: str
    plugin_id: str
    version: str
    kind: str
    name: Mapping[str, str]
    description: str
    compatibility: Mapping[str, str]
    runtime: RuntimeSpec
    entrypoint: str
    parameters_schema: Mapping[str, Any]
    resources: ResourcesConfig
    license: str | None = None
    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    advanced: Mapping[str, Any] | None = None
    iteration: IterationSpec | None = None
    tests: tuple[PluginTestSpec, ...] = ()
    extensions: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        """Canonical '<plugin_id>@<version>' plugin reference."""
        return f"{self.plugin_id}@{self.version}"

    def validate_parameters(self, parameters: Mapping[str, Any] | None) -> dict[str, Any]:
        """Validate parameter payload against parameters_schema (Draft 2020-12)."""
        payload = {} if parameters is None else dict(parameters)
        if not isinstance(parameters if parameters is not None else {}, Mapping):
            raise PluginValidationError("Plugin parameters must be a JSON object")
        check_finite_json(payload, "parameters")
        validator = Draft202012Validator(dict(self.parameters_schema))
        errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
        if errors:
            first = errors[0]
            loc = "/".join(str(p) for p in first.absolute_path) or "<root>"
            raise PluginValidationError(
                f"Plugin parameter validation failed [{loc}]: {first.message}"
            )
        return payload

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "plugin_id": self.plugin_id,
            "version": self.version,
            "kind": self.kind,
            "name": dict(self.name),
            "description": self.description,
            "compatibility": dict(self.compatibility),
            "runtime": self.runtime.to_dict(),
            "entrypoint": self.entrypoint,
            "parameters_schema": dict(self.parameters_schema),
            "resources": self.resources.to_dict(),
            "extensions": dict(self.extensions),
        }
        if self.license is not None:
            out["license"] = self.license
        if self.consumes:
            out["consumes"] = list(self.consumes)
        if self.produces:
            out["produces"] = list(self.produces)
        if self.advanced is not None:
            out["advanced"] = dict(self.advanced)
        if self.iteration is not None:
            out["iteration"] = self.iteration.to_dict()
        if self.tests:
            out["tests"] = [item.to_dict() for item in self.tests]
        return out


def load_plugin_schema() -> dict[str, Any]:
    """Load the bundled Draft 2020-12 JSON Schema for ies.algorithm-plugin@1.0.0."""
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def parse_semver(version: str) -> tuple[int, int, int]:
    """Parse strict MAJOR.MINOR.PATCH semantic version string."""
    if not isinstance(version, str):
        raise CompatibilityError(f"Version must be a string, got {type(version).__name__}")
    match = _SEMVER_RE.fullmatch(version.strip())
    if not match:
        raise CompatibilityError(f"Invalid semantic version: {version!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def check_version_constraint(version: str, constraint: str, *, target_name: str) -> None:
    """Check whether a semantic version satisfies a comma-separated constraint.

    Raises CompatibilityError explicitly on syntax error or version mismatch.
    """
    actual = parse_semver(version)
    if not isinstance(constraint, str) or not constraint.strip():
        raise CompatibilityError(f"Compatibility constraint for {target_name} must be non-empty")

    clauses = [part.strip() for part in constraint.split(",")]
    for clause in clauses:
        if not clause:
            raise CompatibilityError(
                f"Invalid empty clause in compatibility.{target_name}: {constraint!r}"
            )
        match = _CLAUSE_RE.fullmatch(clause)
        if not match:
            raise CompatibilityError(
                f"Invalid constraint syntax in compatibility.{target_name}: {clause!r}"
            )
        op = match.group(1) or "=="
        expected = parse_semver(match.group(2))
        ok = (
            (op == "==" and actual == expected)
            or (op == "!=" and actual != expected)
            or (op == ">=" and actual >= expected)
            or (op == "<=" and actual <= expected)
            or (op == ">" and actual > expected)
            or (op == "<" and actual < expected)
        )
        if not ok:
            raise CompatibilityError(
                f"Incompatible {target_name} version {version!r}: "
                f"plugin requires {constraint!r} (failed clause {clause!r})"
            )


def check_compatibility(
    manifest: PluginManifest,
    *,
    pies_version: str = CURRENT_PIES_VERSION,
    sdk_version: str = CURRENT_SDK_VERSION,
) -> None:
    """Verify plugin contract version and compatibility constraints.

    Before pIES 2.0.0, only schema_version '1.0.0' is valid; any mismatch raises
    CompatibilityError explicitly (no multi-version dispatch matrix).
    """
    if manifest.schema != PLUGIN_SCHEMA:
        raise CompatibilityError(
            f"Unsupported plugin schema {manifest.schema!r}; expected {PLUGIN_SCHEMA!r}"
        )
    if manifest.schema_version != PLUGIN_SCHEMA_VERSION:
        raise CompatibilityError(
            f"Unsupported plugin schema_version {manifest.schema_version!r}; "
            f"only {PLUGIN_SCHEMA_VERSION!r} is supported before 2.0.0"
        )
    if "pies" in manifest.compatibility:
        check_version_constraint(
            pies_version, manifest.compatibility["pies"], target_name="pies"
        )
    if "sdk" in manifest.compatibility:
        check_version_constraint(
            sdk_version, manifest.compatibility["sdk"], target_name="sdk"
        )


def parse_plugin_yaml(source: str | Path) -> dict[str, Any]:
    """Parse plugin.yaml text or file path using the YAML 1.2 safe subset."""
    if isinstance(source, Path):
        if not source.is_file():
            raise PluginValidationError(f"Plugin manifest file not found: {source}")
        text = source.read_text(encoding="utf-8")
    elif isinstance(source, str):
        text = source
    else:
        raise TypeError(f"source must be str or Path, got {type(source).__name__}")

    parsed = load_safe_yaml(text)
    if not isinstance(parsed, dict):
        raise PluginValidationError("Plugin manifest root must be a YAML mapping")
    return parsed


def _validate_parameters_schema(params_schema: Any) -> None:
    """Validate Draft 2020-12 parameters_schema and forbid unknown parameter fields."""
    try:
        Draft202012Validator.check_schema(params_schema)
    except SchemaError as exc:
        raise PluginValidationError(
            f"Invalid Draft 2020-12 parameters_schema: {exc.message}"
        ) from exc
    if not isinstance(params_schema, Mapping) or params_schema.get("type") != "object":
        raise PluginValidationError("parameters_schema root must declare type: 'object'")
    _check_object_schemas_forbid_additional_properties(params_schema, "parameters_schema")


def _check_object_schemas_forbid_additional_properties(
    node: Mapping[str, Any], path: str
) -> None:
    if node.get("type") == "object" and node.get("additionalProperties") is not False:
        raise PluginValidationError(
            f"{path} must set 'additionalProperties: false' to forbid unknown fields"
        )
    properties = node.get("properties")
    if isinstance(properties, Mapping):
        for prop_name, prop_schema in properties.items():
            if isinstance(prop_schema, Mapping):
                _check_object_schemas_forbid_additional_properties(
                    prop_schema, f"{path}.properties.{prop_name}"
                )
    for key in ("$defs", "definitions"):
        defs = node.get(key)
        if isinstance(defs, Mapping):
            for def_name, def_schema in defs.items():
                if isinstance(def_schema, Mapping):
                    _check_object_schemas_forbid_additional_properties(
                        def_schema, f"{path}.{key}.{def_name}"
                    )
    items = node.get("items")
    if isinstance(items, Mapping):
        _check_object_schemas_forbid_additional_properties(items, f"{path}.items")
    for comb in ("allOf", "anyOf", "oneOf"):
        branches = node.get(comb)
        if isinstance(branches, list):
            for idx, branch in enumerate(branches):
                if isinstance(branch, Mapping):
                    _check_object_schemas_forbid_additional_properties(
                        branch, f"{path}.{comb}[{idx}]"
                    )
def _build_resource_spec(raw: Mapping[str, Any], label: str) -> ResourceSpec:
    cpu = raw["cpu"]
    if isinstance(cpu, bool) or not isinstance(cpu, (int, float)):
        raise PluginValidationError(f"resources.{label}.cpu must be a positive number")
    for int_field in ("memory_mb", "timeout_seconds", "process_limit"):
        val = raw[int_field]
        if isinstance(val, bool) or not isinstance(val, int):
            raise PluginValidationError(f"resources.{label}.{int_field} must be a positive integer")
    return ResourceSpec(
        cpu=float(cpu),
        memory_mb=raw["memory_mb"],
        timeout_seconds=raw["timeout_seconds"],
        process_limit=raw["process_limit"],
    )


def validate_plugin_manifest(
    data: Mapping[str, Any],
    *,
    pies_version: str = CURRENT_PIES_VERSION,
    sdk_version: str = CURRENT_SDK_VERSION,
    check_compat: bool = True,
) -> PluginManifest:
    """Validate raw manifest mapping against ies.algorithm-plugin@1.0.0 and build PluginManifest."""
    if not isinstance(data, Mapping):
        raise PluginValidationError("Plugin manifest must be a mapping")

    check_finite_json(data, "plugin.yaml")

    schema_val = data.get("schema")
    if schema_val != PLUGIN_SCHEMA:
        raise CompatibilityError(
            f"Unsupported plugin schema {schema_val!r}; expected {PLUGIN_SCHEMA!r}"
        )
    version_val = data.get("schema_version")
    if version_val != PLUGIN_SCHEMA_VERSION:
        raise CompatibilityError(
            f"Unsupported plugin schema_version {version_val!r}; "
            f"only {PLUGIN_SCHEMA_VERSION!r} is supported before 2.0.0"
        )

    schema = load_plugin_schema()
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(dict(data)), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        loc = "/".join(str(p) for p in first.absolute_path) or "<root>"
        raise PluginValidationError(f"Plugin manifest invalid [{loc}]: {first.message}")

    params_schema = data["parameters_schema"]
    _validate_parameters_schema(params_schema)

    default_res = _build_resource_spec(data["resources"]["default"], "default")
    max_res = _build_resource_spec(data["resources"]["maximum"], "maximum")
    for field_name in ("cpu", "memory_mb", "timeout_seconds", "process_limit"):
        if getattr(default_res, field_name) > getattr(max_res, field_name):
            raise PluginValidationError(
                f"resources.default.{field_name} ({getattr(default_res, field_name)}) "
                f"exceeds resources.maximum.{field_name} ({getattr(max_res, field_name)})"
            )

    raw_runtime = data["runtime"]
    runtime = RuntimeSpec(
        language=raw_runtime["language"],
        python=raw_runtime["python"],
        requirements=raw_runtime.get("requirements"),
    )

    iteration: IterationSpec | None = None
    if "iteration" in data:
        raw_iter = data["iteration"]
        enabled = raw_iter["enabled"]
        if enabled:
            if "max_iterations_default" not in raw_iter:
                raise PluginValidationError(
                    "iteration.max_iterations_default is required when iteration.enabled is true"
                )
            if "convergence_metric" not in raw_iter:
                raise PluginValidationError(
                    "iteration.convergence_metric is required when iteration.enabled is true"
                )
        iteration = IterationSpec(
            enabled=enabled,
            max_iterations_default=raw_iter.get("max_iterations_default"),
            convergence_metric=raw_iter.get("convergence_metric"),
            progress_channel=raw_iter.get("progress_channel", "/work/out/iterations.jsonl"),
        )

    raw_tests = data.get("tests", ())
    seen_test_ids: set[str] = set()
    parsed_tests: list[PluginTestSpec] = []
    for item in raw_tests:
        test_id = item["id"]
        if test_id in seen_test_ids:
            raise PluginValidationError(f"Duplicate plugin test id: {test_id!r}")
        seen_test_ids.add(test_id)
        if "in" in item:
            parsed_tests.append(
                PluginTestSpec(
                    id=test_id,
                    input=item["in"],
                    expected=item["expected"],
                    mode="directory",
                )
            )
        else:
            parsed_tests.append(
                PluginTestSpec(
                    id=test_id,
                    input=item["input"],
                    expected=item["expected"],
                    mode="file",
                )
            )
    tests = tuple(parsed_tests)

    manifest = PluginManifest(
        schema=data["schema"],
        schema_version=data["schema_version"],
        plugin_id=data["plugin_id"],
        version=data["version"],
        kind=data["kind"],
        name=dict(data["name"]),
        description=data["description"],
        license=data.get("license"),
        compatibility=dict(data["compatibility"]),
        consumes=tuple(data.get("consumes", ())),
        produces=tuple(data.get("produces", ())),
        runtime=runtime,
        entrypoint=data["entrypoint"],
        parameters_schema=dict(params_schema),
        resources=ResourcesConfig(default=default_res, maximum=max_res),
        advanced=dict(data["advanced"]) if "advanced" in data else None,
        iteration=iteration,
        tests=tests,
        extensions=dict(data.get("extensions", {})),
    )

    if check_compat:
        check_compatibility(manifest, pies_version=pies_version, sdk_version=sdk_version)

    return manifest


def load_plugin_manifest(
    path: str | Path,
    *,
    pies_version: str = CURRENT_PIES_VERSION,
    sdk_version: str = CURRENT_SDK_VERSION,
    check_compat: bool = True,
) -> PluginManifest:
    """Parse and validate a plugin.yaml file."""
    manifest_path = Path(path)
    raw = parse_plugin_yaml(manifest_path)
    return validate_plugin_manifest(
        raw,
        pies_version=pies_version,
        sdk_version=sdk_version,
        check_compat=check_compat,
    )
