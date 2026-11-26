"""Solver Bundle 1.2.0 契约解耦测试 (bundle{generator, contract})。"""

from __future__ import annotations

import dataclasses
import json
import pathlib

import pytest

from iesplan.computation import (
    SOLVER_BUNDLE_SCHEMA,
    SOLVER_BUNDLE_VERSION,
    CalculationConfig,
    SolverBundleManifest,
    StagePluginSpec,
    load_bundle_schema,
    manifest_to_yaml_bytes,
    normalize_bundle,
    validate_bundle,
)

SAMPLES = (
    pathlib.Path(__file__).parent.parent
    / "iesplan"
    / "computation"
    / "samples"
    / "solver-bundle"
)
VALID = SAMPLES / "valid" / "1.0" / "minimal.json"
INVALID = SAMPLES / "invalid" / "1.0"

GENERATOR = "acme.generator@1.0.0"
SOLVER = "ies.solver.highs@1.7.2"
CONTRACT = "ies.solver-bundle.mps@1.0.0"


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _generator_spec(**kwargs) -> StagePluginSpec:
    defaults = {
        "plugin_id": "acme.generator",
        "version": "1.0.0",
        "kind": "generator",
        "consumes": ("ies.stage-input@1.0.0",),
        "produces": (CONTRACT,),
        "modes": ("planning",),
        "precisions": ("low", "medium", "high"),
        "outputs": ("solution",),
        "parameters_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }
    defaults.update(kwargs)
    return StagePluginSpec(**defaults)


def _solver_spec(**kwargs) -> StagePluginSpec:
    defaults = {
        "plugin_id": "ies.solver.highs",
        "version": "1.7.2",
        "kind": "solver",
        "consumes": (CONTRACT,),
        "produces": ("ies.primal-solution@1.0.0",),
        "parameters_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    }
    defaults.update(kwargs)
    return StagePluginSpec(**defaults)


def _config(**kwargs) -> CalculationConfig:
    kwargs.setdefault("mode", "planning")
    kwargs.setdefault("generator", GENERATOR)
    kwargs.setdefault("solver", SOLVER)
    kwargs.setdefault("precision", "medium")
    kwargs.setdefault("time_limit_seconds", 600)
    kwargs.setdefault("outputs", ["solution"])
    return CalculationConfig(**kwargs)


def test_schema_file_meta() -> None:
    """schema 文件标识与解耦 manifest 必需结构；无 hash/摘要/command/result_adapter。"""
    schema = load_bundle_schema()
    assert schema["$id"] == f"{SOLVER_BUNDLE_SCHEMA}/{SOLVER_BUNDLE_VERSION}"
    assert schema["title"] == "ies.solver-bundle 1.0.0 求解交接包目标契约"
    assert set(schema["required"]) == {
        "schema",
        "schema_version",
        "bundle",
        "inputs",
        "outputs",
        "extensions",
    }
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema"] == {"const": SOLVER_BUNDLE_SCHEMA}
    assert schema["properties"]["schema_version"] == {"const": SOLVER_BUNDLE_VERSION}
    bundle = schema["properties"]["bundle"]
    assert set(bundle["required"]) == {"generator", "contract"}
    assert bundle["additionalProperties"] is False
    assert "command" not in schema["properties"]
    assert "result_adapter" not in schema["properties"]
    text = json.dumps(schema)
    for forbidden in ("hash", "digest", "checksum", "sha256", "sha-256"):
        assert forbidden not in text.lower()


def test_valid_sample_builds_frozen_bundle() -> None:
    """合法样例通过校验并构造深层不可变 manifest 载体；字段一一对应。"""
    bundle = validate_bundle(_load(VALID))
    assert isinstance(bundle, SolverBundleManifest)
    assert bundle.bundle["generator"] == GENERATOR
    assert bundle.bundle["contract"] == CONTRACT
    assert bundle.inputs[0]["media_type"] == "application/mps"
    assert bundle.outputs[0]["path"] == "output/primal_solution.json"
    assert bundle.outputs[0]["required"] is True
    assert bundle.extensions == {}
    assert bundle.to_dict() == _load(VALID)
    with pytest.raises(dataclasses.FrozenInstanceError):
        bundle.bundle = {}  # type: ignore[misc]


EXPECTED_INVALID = {
    "absolute-output-path.json": "outputs/0",
    "bad-schema-word.json": "schema",
    "escaping-input-path.json": "inputs/0",
    "extra-top-level-key.json": "calculation",
    "legacy-command-rejected.json": "command",
    "legacy-result-adapter-rejected.json": "result_adapter",
    "missing-primal-output.json": "output/primal_solution.json",
}


def test_invalid_samples_rejected() -> None:
    """非法样例各只违一条，逐个被拒绝且定位违规位置。"""
    assert sorted(p.name for p in INVALID.glob("*.json")) == sorted(EXPECTED_INVALID)
    for name, location in EXPECTED_INVALID.items():
        with pytest.raises(ValueError) as exc_info:
            validate_bundle(_load(INVALID / name))
        assert location in str(exc_info.value), (name, str(exc_info.value))


def test_normalize_is_order_insensitive() -> None:
    """键乱序得同一规范形式；序列顺序保持语义。"""
    raw = _load(VALID)
    shuffled = dict(reversed(list(raw.items())))
    assert normalize_bundle(shuffled) == normalize_bundle(raw)
    assert normalize_bundle(raw)["outputs"][0]["path"] == "output/primal_solution.json"
    with pytest.raises(TypeError):
        normalize_bundle({"inputs": [{"path": "a", "content": b"bytes"}]})
    with pytest.raises(TypeError):
        validate_bundle([("schema", SOLVER_BUNDLE_SCHEMA)])  # type: ignore[arg-type]


def test_manifest_yaml_bytes_are_canonical() -> None:
    """规范字节：语义回读一致、关键字段齐备、末尾换行、逐字节确定。"""
    from iesplan.core.yamlmini import load as yaml_load

    bundle = validate_bundle(_load(VALID))
    first = manifest_to_yaml_bytes(bundle)
    assert manifest_to_yaml_bytes(bundle) == first
    assert first.endswith(b"\n")
    parsed = yaml_load(first.decode("utf-8"))
    assert parsed == bundle.to_dict()
    assert parsed["schema"] == SOLVER_BUNDLE_SCHEMA
    assert parsed["bundle"] == {"generator": GENERATOR, "contract": CONTRACT}
    assert [e["path"] for e in parsed["inputs"]] == [
        "input/problem.mps",
        "input/parameters.json",
    ]
    assert parsed["outputs"][0]["path"] == "output/primal_solution.json"
    with pytest.raises(TypeError):
        manifest_to_yaml_bytes({"schema": "ies.solver-bundle"})  # type: ignore[arg-type]


def test_binding_against_stage_specs_and_config() -> None:
    """generator_spec/solver_spec/config 契约一致通过；不兼容或不一致即拒绝。"""
    bundle = validate_bundle(
        _load(VALID),
        generator_spec=_generator_spec(),
        solver_spec=_solver_spec(),
        config=_config(),
    )
    assert bundle.bundle["contract"] == CONTRACT
    with pytest.raises(ValueError, match="不一致"):
        validate_bundle(
            _load(VALID),
            generator_spec=_generator_spec(plugin_id="other.generator"),
            config=_config(),
        )
    with pytest.raises(ValueError, match="不一致"):
        validate_bundle(
            _load(VALID),
            generator_spec=_generator_spec(),
            config=_config(generator="other.generator@1.0.0"),
        )
    with pytest.raises(ValueError, match="produces"):
        validate_bundle(
            _load(VALID),
            generator_spec=_generator_spec(produces=("acme.other-bundle@1.0.0",)),
            config=_config(),
        )
    with pytest.raises(ValueError, match="consumes"):
        validate_bundle(
            _load(VALID),
            generator_spec=_generator_spec(),
            solver_spec=_solver_spec(consumes=("acme.other-bundle@1.0.0",)),
            config=_config(),
        )
