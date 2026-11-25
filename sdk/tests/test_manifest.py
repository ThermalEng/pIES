"""Tests for plugin.yaml (ies.algorithm-plugin@1.0.0) schema and compatibility checks."""

from __future__ import annotations

from copy import deepcopy

import pytest

from pies_sdk.errors import CompatibilityError, PluginValidationError
from pies_sdk.manifest import (
    PLUGIN_KINDS,
    parse_plugin_yaml,
    validate_plugin_manifest,
)


def _base_manifest(kind: str = "algorithm") -> dict:
    return {
        "schema": "ies.algorithm-plugin",
        "schema_version": "1.0.0",
        "plugin_id": f"acme.{kind}.sample",
        "version": "1.0.0",
        "kind": kind,
        "name": {"zh_cn": "示例插件", "en_us": "Sample Plugin"},
        "description": "Sample plugin manifest for tests.",
        "license": "MPL-2.0",
        "compatibility": {"pies": ">=1.1.5,<2.0.0", "sdk": ">=1.1.5,<2.0.0"},
        "runtime": {"language": "python", "python": "3.12"},
        "entrypoint": "main:solve",
        "parameters_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["alpha"],
            "properties": {
                "alpha": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
        "resources": {
            "default": {"cpu": 1, "memory_mb": 256, "timeout_seconds": 60, "process_limit": 8},
            "maximum": {"cpu": 2, "memory_mb": 1024, "timeout_seconds": 300, "process_limit": 16},
        },
        "extensions": {},
    }


@pytest.mark.parametrize("kind", PLUGIN_KINDS)
def test_all_five_kinds_validate(kind: str) -> None:
    manifest = validate_plugin_manifest(_base_manifest(kind))
    assert manifest.kind == kind
    assert manifest.ref == f"acme.{kind}.sample@1.0.0"
    assert manifest.validate_parameters({"alpha": 0.5}) == {"alpha": 0.5}


def test_compatibility_mismatch_raises_explicit_error() -> None:
    data = _base_manifest()
    data["compatibility"] = {"pies": ">=2.0.0"}
    with pytest.raises(CompatibilityError, match="Incompatible pies version"):
        validate_plugin_manifest(data, pies_version="1.1.5")

    data_sdk = _base_manifest()
    data_sdk["compatibility"] = {"pies": ">=1.1.5,<2.0.0", "sdk": "<1.1.0"}
    with pytest.raises(CompatibilityError, match="Incompatible sdk version"):
        validate_plugin_manifest(data_sdk, sdk_version="1.1.5")


def test_schema_version_mismatch_rejected_before_2_0() -> None:
    data = _base_manifest()
    data["schema_version"] = "0.9.0"
    with pytest.raises(CompatibilityError, match="before 2.0.0"):
        validate_plugin_manifest(data)


def test_optional_advanced_and_iteration_sections() -> None:
    data = _base_manifest("solver")
    data["advanced"] = {
        "streaming_bundle": True,
        "supported_modes": ["planning"],
        "supported_precisions": ["medium", "high"],
        "binary": {
            "executable": "ies.solver.highs@1.7.2",
            "arguments_template": ["input/problem.mps"],
            "env_allowlist": ["OMP_NUM_THREADS"],
        },
        "companions": {
            "generator": "ies.generator.highs_milp@1.0.0",
            "result_adapter": "ies.result-adapter.highs@1.0.0",
        },
    }
    data["iteration"] = {
        "enabled": True,
        "max_iterations_default": 25,
        "convergence_metric": "max_relative_delta",
        "progress_channel": "/work/out/iterations.jsonl",
    }
    manifest = validate_plugin_manifest(data)
    assert manifest.advanced is not None
    assert manifest.advanced["streaming_bundle"] is True
    assert manifest.iteration is not None
    assert manifest.iteration.enabled is True
    assert manifest.iteration.max_iterations_default == 25


def test_rejects_unknown_fields_invalid_schema_and_resource_bounds() -> None:
    bad_unknown = _base_manifest()
    bad_unknown["unexpected"] = 1
    with pytest.raises(PluginValidationError, match="Additional properties"):
        validate_plugin_manifest(bad_unknown)

    bad_params_schema = _base_manifest()
    bad_params_schema["parameters_schema"] = {"type": "not-a-json-type"}
    with pytest.raises(PluginValidationError, match="Draft 2020-12"):
        validate_plugin_manifest(bad_params_schema)

    missing_additional_props = _base_manifest()
    missing_additional_props["parameters_schema"] = {"type": "object"}
    with pytest.raises(PluginValidationError, match="additionalProperties: false"):
        validate_plugin_manifest(missing_additional_props)

    nested_missing_additional_props = _base_manifest()
    nested_missing_additional_props["parameters_schema"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"nested": {"type": "object"}},
    }
    with pytest.raises(PluginValidationError, match="additionalProperties: false"):
        validate_plugin_manifest(nested_missing_additional_props)

    bad_res = deepcopy(_base_manifest())
    bad_res["resources"]["default"]["memory_mb"] = 4096
    with pytest.raises(PluginValidationError, match="exceeds resources.maximum"):
        validate_plugin_manifest(bad_res)

    bad_entrypoint = _base_manifest()
    bad_entrypoint["entrypoint"] = "main:_private"
    with pytest.raises(PluginValidationError):
        validate_plugin_manifest(bad_entrypoint)


def test_parameters_validation_rejects_invalid_and_non_finite() -> None:
    manifest = validate_plugin_manifest(_base_manifest())
    with pytest.raises(PluginValidationError, match="required property"):
        manifest.validate_parameters({})
    with pytest.raises(PluginValidationError, match="Additional properties"):
        manifest.validate_parameters({"alpha": 0.2, "extra": 1})
    with pytest.raises(Exception, match="non-finite"):
        manifest.validate_parameters({"alpha": float("nan")})


def test_parse_plugin_yaml_minimal_spec_example() -> None:
    raw_yaml = """
schema: ies.algorithm-plugin
schema_version: "1.0.0"
plugin_id: acme.algorithm.capacity_search
version: "1.2.0"
kind: algorithm
name:
  zh_cn: 容量搜索算法
  en_us: Capacity search
description: Constrained capacity search.
license: MPL-2.0
compatibility:
  pies: ">=1.1.5,<2.0.0"
runtime:
  language: python
  python: "3.12"
  requirements: requirements.txt
entrypoint: main:solve
parameters_schema:
  type: object
  additionalProperties: false
  properties:
    max_steps:
      type: integer
      minimum: 1
      default: 100
resources:
  default:
    cpu: 1
    memory_mb: 512
    timeout_seconds: 300
    process_limit: 8
  maximum:
    cpu: 2
    memory_mb: 2048
    timeout_seconds: 3600
    process_limit: 32
tests:
  - id: minimal
    input: tests/minimal/input.json
    expected: tests/minimal/expected.json
extensions: {}
"""
    manifest = validate_plugin_manifest(parse_plugin_yaml(raw_yaml))
    assert manifest.plugin_id == "acme.algorithm.capacity_search"
    assert manifest.tests[0].id == "minimal"
    assert manifest.tests[0].mode == "file"
    assert manifest.tests[0].is_directory is False


def test_consumes_produces_and_dual_mode_tests() -> None:
    raw_yaml = """
schema: ies.algorithm-plugin
schema_version: "1.0.0"
plugin_id: acme.solver.cbc_mps
version: "1.0.0"
kind: solver
name:
  zh_cn: CBC 求解器
  en_us: CBC Solver
description: Solves MPS models and emits primal_solution.json.
license: MPL-2.0
compatibility:
  pies: ">=1.1.5,<2.0.0"
consumes:
  - ies.solver-bundle.mps@1.0.0
produces:
  - ies.primal-solution@1.0.0
runtime:
  language: python
  python: "3.12"
entrypoint: main:solve
parameters_schema:
  type: object
  additionalProperties: false
  properties: {}
resources:
  default:
    cpu: 1
    memory_mb: 512
    timeout_seconds: 300
    process_limit: 8
  maximum:
    cpu: 2
    memory_mb: 1024
    timeout_seconds: 600
    process_limit: 16
tests:
  - id: simple_json
    input: tests/simple_json/input.json
    expected: tests/simple_json/expected.json
  - id: minimal_dir
    in: tests/minimal_dir/in
    expected: tests/minimal_dir/expected
extensions: {}
"""
    manifest = validate_plugin_manifest(parse_plugin_yaml(raw_yaml))
    assert manifest.consumes == ("ies.solver-bundle.mps@1.0.0",)
    assert manifest.produces == ("ies.primal-solution@1.0.0",)
    assert len(manifest.tests) == 2
    assert manifest.tests[0].mode == "file"
    assert manifest.tests[0].is_directory is False
    assert manifest.tests[0].to_dict() == {
        "id": "simple_json",
        "input": "tests/simple_json/input.json",
        "expected": "tests/simple_json/expected.json",
    }
    assert manifest.tests[1].mode == "directory"
    assert manifest.tests[1].is_directory is True
    assert manifest.tests[1].to_dict() == {
        "id": "minimal_dir",
        "in": "tests/minimal_dir/in",
        "expected": "tests/minimal_dir/expected",
    }
    roundtrip = validate_plugin_manifest(manifest.to_dict())
    assert roundtrip == manifest


def test_rejects_invalid_consumes_produces_and_tests() -> None:
    bad_contract = _base_manifest("solver")
    bad_contract["consumes"] = ["not-a-contract"]
    with pytest.raises(PluginValidationError):
        validate_plugin_manifest(bad_contract)

    dup_produces = _base_manifest("solver")
    dup_produces["produces"] = [
        "ies.primal-solution@1.0.0",
        "ies.primal-solution@1.0.0",
    ]
    with pytest.raises(PluginValidationError):
        validate_plugin_manifest(dup_produces)

    mixed_test_keys = _base_manifest("solver")
    mixed_test_keys["tests"] = [
        {
            "id": "bad",
            "input": "tests/a/input.json",
            "in": "tests/a/in",
            "expected": "tests/a/expected",
        }
    ]
    with pytest.raises(PluginValidationError):
        validate_plugin_manifest(mixed_test_keys)

    dup_test_id = _base_manifest("solver")
    dup_test_id["tests"] = [
        {"id": "dup", "input": "tests/a/input.json", "expected": "tests/a/expected.json"},
        {"id": "dup", "in": "tests/b/in", "expected": "tests/b/expected"},
    ]
    with pytest.raises(PluginValidationError, match="Duplicate plugin test id"):
        validate_plugin_manifest(dup_test_id)
