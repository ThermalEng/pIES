"""Tests for 1.2.0 F1 target CalculationConfig, decoupled SolverBundle, and compatibility contracts."""

from __future__ import annotations

import dataclasses
import json
import pathlib
from typing import Any

import pytest

from iesplan.computation import (
    CALCULATION_CONFIG_FIELDS,
    CALCULATION_CONFIG_SCHEMA,
    CALCULATION_CONFIG_VERSION,
    ComputationUnavailableError,
    SOLVER_BUNDLE_SCHEMA,
    SOLVER_BUNDLE_VERSION,
    CalculationConfig,
    SolverBundle,
    SolverBundleManifest,
    export_bundle_files,
    load_bundle_schema,
    load_calculation_config_schema,
    manifest_to_yaml_bytes,
    normalize_bundle,
    parse_calculation_config_json,
    validate_bundle,
    validate_calculation_config,
)
from iesplan.computation.contracts import (
    ENGINEERING_SOLUTION_CONTRACT,
    EVALUATION_REPORT_CONTRACT,
    FINANCIAL_EVALUATION_CONTRACT,
    PREDICTION_OUTPUT_CONTRACT,
    PRIMAL_SOLUTION_CONTRACT,
    PRIMAL_SOLUTION_OUTPUT_PATH,
    SOLVER_BUNDLE_MPS_CONTRACT,
    STAGE_INPUT_CONTRACT,
    StagePluginSpec,
    validate_stage_compatibility,
)
from iesplan.core.content import MemoryContent, read_content
from iesplan.core.yamlmini import load as yaml_load

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "f1_contracts"
CC_VALID = FIXTURES / "calculation-config" / "valid"
CC_INVALID = FIXTURES / "calculation-config" / "invalid"
SB_VALID = FIXTURES / "solver-bundle" / "valid"
SB_INVALID = FIXTURES / "solver-bundle" / "invalid"


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _empty_object_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


def _stepped_plugin_catalog() -> dict[str, StagePluginSpec]:
    predictor = StagePluginSpec.from_manifest(
        {
            "plugin_id": "ies.predictor.naive",
            "version": "1.0.0",
            "kind": "predictor",
            "consumes": [STAGE_INPUT_CONTRACT],
            "produces": [PREDICTION_OUTPUT_CONTRACT],
            "parameters_schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "properties": {"window": {"type": "integer", "minimum": 1}},
            },
        }
    )
    generator = StagePluginSpec.from_manifest(
        {
            "plugin_id": "ies.generator.highs_milp",
            "version": "1.0.0",
            "kind": "generator",
            "consumes": [STAGE_INPUT_CONTRACT],
            "produces": [SOLVER_BUNDLE_MPS_CONTRACT],
            "parameters_schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "acceleration": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "mode": {"enum": ["off", "on_demand_mutex"]},
                        },
                    }
                },
            },
            "advanced": {
                "supported_modes": ["planning"],
                "supported_precisions": ["low", "medium", "high"],
            },
            "extensions": {"supported_outputs": ["solution", "metrics"]},
        }
    )
    solver = StagePluginSpec.from_manifest(
        {
            "plugin_id": "ies.solver.highs",
            "version": "1.7.2",
            "kind": "solver",
            "consumes": [SOLVER_BUNDLE_MPS_CONTRACT],
            "produces": [PRIMAL_SOLUTION_CONTRACT],
            "parameters_schema": _empty_object_schema(),
        }
    )
    finance_eval = StagePluginSpec.from_manifest(
        {
            "plugin_id": "ies.evaluator.finance",
            "version": "1.0.0",
            "kind": "evaluator",
            "consumes": [ENGINEERING_SOLUTION_CONTRACT],
            "produces": [FINANCIAL_EVALUATION_CONTRACT],
            "parameters_schema": _empty_object_schema(),
            "extensions": {"supported_outputs": ["metrics"]},
        }
    )
    carbon_eval = StagePluginSpec.from_manifest(
        {
            "plugin_id": "acme.evaluator.carbon",
            "version": "1.0.0",
            "kind": "evaluator",
            "consumes": [ENGINEERING_SOLUTION_CONTRACT],
            "produces": [EVALUATION_REPORT_CONTRACT],
            "parameters_schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["grid_factor"],
                "properties": {"grid_factor": {"type": "number", "exclusiveMinimum": 0}},
            },
            "extensions": {"supported_outputs": ["metrics"]},
        }
    )
    algorithm = StagePluginSpec.from_manifest(
        {
            "plugin_id": "acme.algorithm.capacity_search",
            "version": "1.2.0",
            "kind": "algorithm",
            "consumes": [STAGE_INPUT_CONTRACT],
            "produces": [ENGINEERING_SOLUTION_CONTRACT],
            "parameters_schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["max_steps"],
                "properties": {"max_steps": {"type": "integer", "minimum": 1}},
            },
            "advanced": {
                "supported_modes": ["planning"],
                "supported_precisions": ["low", "medium", "high"],
            },
            "extensions": {"supported_outputs": ["solution", "metrics"]},
        }
    )
    return {
        spec.ref: spec
        for spec in (predictor, generator, solver, finance_eval, carbon_eval, algorithm)
    }


def test_target_calculation_config_schema_meta() -> None:
    schema = load_calculation_config_schema()
    assert schema["$id"] == f"{CALCULATION_CONFIG_SCHEMA}/{CALCULATION_CONFIG_VERSION}"
    assert len(CALCULATION_CONFIG_FIELDS) == 13
    assert set(schema["required"]) == set(CALCULATION_CONFIG_FIELDS)
    assert schema["additionalProperties"] is False
    assert "convergence" not in schema["properties"]
    assert "evaluator" not in schema["properties"]
    assert "evaluators" in schema["properties"]
    text = json.dumps(schema).lower()
    for forbidden in ("hash", "digest", "checksum", "sha256", "sha-256"):
        assert forbidden not in text


def test_target_solver_bundle_schema_meta() -> None:
    schema = load_bundle_schema()
    assert schema["$id"] == f"{SOLVER_BUNDLE_SCHEMA}/{SOLVER_BUNDLE_VERSION}"
    assert set(schema["required"]) == {
        "schema",
        "schema_version",
        "bundle",
        "inputs",
        "outputs",
        "extensions",
    }
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]["bundle"]["required"]) == {"generator", "contract"}
    assert "solver" not in schema["properties"]["bundle"]["properties"]
    assert "command" not in schema["properties"]
    assert "result_adapter" not in schema["properties"]
    text = json.dumps(schema).lower()
    for forbidden in ("hash", "digest", "checksum", "sha256", "sha-256"):
        assert forbidden not in text


def test_valid_calculation_config_fixtures() -> None:
    valid_files = sorted(CC_VALID.glob("*.json"))
    assert len(valid_files) == 3
    for path in valid_files:
        raw = _load_json(path)
        cfg = validate_calculation_config(raw)
        assert isinstance(cfg, CalculationConfig)
        assert tuple(cfg.to_dict().keys()) == CALCULATION_CONFIG_FIELDS
        assert cfg.to_dict() == raw
        restored = CalculationConfig.from_frozen(cfg.to_dict())
        assert restored.to_dict() == raw
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.mode = "other"  # type: ignore[misc]


EXPECTED_INVALID_CC = {
    "bad-precision.json": "precision",
    "bool-seed-rejected.json": "seed",
    "both-generator-and-algorithm.json": "null",
    "duplicate-evaluator-ref.json": "重复",
    "duplicate-prediction-target.json": "重复",
    "empty-outputs.json": "outputs",
    "flat-unslotted-options.json": "options",
    "legacy-convergence-rejected.json": "convergence",
    "legacy-singular-evaluator-rejected.json": "evaluator",
    "missing-explicit-null-algorithm.json": "algorithm",
    "missing-explicit-null-seed.json": "seed",
    "neither-generator-nor-algorithm.json": "generator",
    "nonpositive-time-limit.json": "time_limit_seconds",
    "partial-stepped-missing-solver.json": "solver",
    "stepped-with-algorithm-options-slot.json": "options",
    "unified-with-generator-options-slot.json": "options",
    "unknown-top-level-field.json": "discretization",
}


def test_invalid_calculation_config_fixtures_rejected() -> None:
    assert sorted(p.name for p in CC_INVALID.glob("*.json")) == sorted(EXPECTED_INVALID_CC)
    for name, marker in EXPECTED_INVALID_CC.items():
        with pytest.raises(ValueError) as exc_info:
            validate_calculation_config(_load_json(CC_INVALID / name))
        assert marker in str(exc_info.value), f"{name}: {exc_info.value}"


def test_calculation_config_edge_cases_and_immutability() -> None:
    stepped = validate_calculation_config(_load_json(CC_VALID / "stepped-full.json"))
    assert stepped.is_unified is False
    assert stepped.slot_options("generator")["acceleration"] == {"mode": "on_demand_mutex"}
    assert stepped.slot_options("solver") == {}
    with pytest.raises(TypeError):
        stepped.options["generator"]["extra"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        stepped.evaluators[0]["parameters"]["extra"] = 1  # type: ignore[index]

    for bad_json in (
        '{"schema":"ies.calculation-config","schema_version":"1.0.0","mode":"planning",'
        '"predictions":[],"generator":"g.a@1.0.0","solver":"s.a@1.0.0","algorithm":null,'
        '"evaluators":[],"precision":"medium","time_limit_seconds":60,"options":{"generator":{"x":NaN}},'
        '"seed":null,"outputs":["solution"]}',
        '{"schema":"ies.calculation-config","schema_version":"1.0.0","mode":"planning",'
        '"predictions":[],"generator":"g.a@1.0.0","solver":"s.a@1.0.0","algorithm":null,'
        '"evaluators":[],"precision":"medium","time_limit_seconds":60,"options":{"solver":{"x":Infinity}},'
        '"seed":null,"outputs":["solution"]}',
    ):
        with pytest.raises(ValueError, match="非有限"):
            parse_calculation_config_json(bad_json)

    raw = _load_json(CC_VALID / "stepped-minimal.json")
    with pytest.raises(ValueError, match="非有限"):
        validate_calculation_config(
            {
                **raw,
                "evaluators": [
                    {"evaluator": "ies.evaluator.finance@1.0.0", "parameters": {"x": float("-inf")}}
                ],
            }
        )
    with pytest.raises(TypeError, match="布尔"):
        CalculationConfig(
            mode="planning",
            generator="ies.generator.highs_milp@1.0.0",
            solver="ies.solver.highs@1.7.2",
            precision="medium",
            time_limit_seconds=60,
            seed=False,
            outputs=("solution",),
        )
    with pytest.raises(ValueError, match="time_limit_seconds"):
        CalculationConfig(
            mode="planning",
            generator="ies.generator.highs_milp@1.0.0",
            solver="ies.solver.highs@1.7.2",
            precision="medium",
            time_limit_seconds=True,  # type: ignore[arg-type]
            outputs=("solution",),
        )
    with pytest.raises(KeyError):
        CalculationConfig.from_frozen({"schema": "ies.calculation-config"})


def test_valid_solver_bundle_fixture_and_yaml_export() -> None:
    raw = _load_json(SB_VALID / "mps-minimal.json")
    manifest = validate_bundle(raw)
    assert isinstance(manifest, SolverBundleManifest)
    assert manifest.bundle["generator"] == "ies.generator.highs_milp@1.0.0"
    assert manifest.bundle["contract"] == SOLVER_BUNDLE_MPS_CONTRACT
    assert manifest.outputs[0]["path"] == PRIMAL_SOLUTION_OUTPUT_PATH
    assert manifest.to_dict() == raw

    shuffled = dict(reversed(list(raw.items())))
    assert normalize_bundle(shuffled) == normalize_bundle(raw)

    yaml_bytes = manifest_to_yaml_bytes(manifest)
    assert yaml_bytes.endswith(b"\n")
    assert manifest_to_yaml_bytes(manifest) == yaml_bytes
    assert yaml_load(yaml_bytes.decode("utf-8")) == manifest.to_dict()

    bundle = SolverBundle(
        manifest=manifest,
        files={
            "input/problem.mps": MemoryContent(b"NAME DEMO\nENDATA\n"),
            "input/parameters.json": MemoryContent(b'{"column_count": 1}\n'),
            "input/parameters.jsonl": MemoryContent(b'["column_names", ["x0"]]\n'),
        },
    )
    exported = export_bundle_files(bundle)
    assert list(exported.keys()) == [
        "bundle.yaml",
        "input/parameters.json",
        "input/parameters.jsonl",
        "input/problem.mps",
    ]
    assert read_content(exported["bundle.yaml"]) == yaml_bytes
    assert read_content(exported["bundle.yaml"]) == yaml_bytes


EXPECTED_INVALID_SB = {
    "absolute-input-path.json": "inputs/0",
    "duplicate-input-path.json": "重复",
    "escaping-output-path.json": "outputs/0",
    "input-outside-input-dir.json": "inputs/0",
    "legacy-bundle-solver-rejected.json": "solver",
    "legacy-command-rejected.json": "command",
    "legacy-result-adapter-rejected.json": "result_adapter",
    "missing-contract-field.json": "contract",
    "missing-primal-solution-output.json": "primal_solution.json",
    "output-outside-output-dir.json": "outputs/0",
    "reserved-manifest-input-path.json": "inputs/0",
    "unknown-top-level-key.json": "calculation",
}


def test_invalid_solver_bundle_fixtures_rejected() -> None:
    assert sorted(p.name for p in SB_INVALID.glob("*.json")) == sorted(EXPECTED_INVALID_SB)
    for name, marker in EXPECTED_INVALID_SB.items():
        with pytest.raises(ValueError) as exc_info:
            validate_bundle(_load_json(SB_INVALID / name))
        assert marker in str(exc_info.value), f"{name}: {exc_info.value}"

    # Direct dataclass construction must also block reserved manifest collisions and cross-dir paths.
    with pytest.raises(ValueError, match="保留清单路径"):
        SolverBundleManifest(
            bundle={
                "generator": "ies.generator.highs_milp@1.0.0",
                "contract": SOLVER_BUNDLE_MPS_CONTRACT,
            },
            inputs=[{"path": "bundle.yaml", "media_type": "application/yaml"}],
            outputs=[
                {"path": PRIMAL_SOLUTION_OUTPUT_PATH, "media_type": "application/json", "required": True}
            ],
            extensions={},
        )
    with pytest.raises(ValueError, match="'input/'"):
        SolverBundleManifest(
            bundle={
                "generator": "ies.generator.highs_milp@1.0.0",
                "contract": SOLVER_BUNDLE_MPS_CONTRACT,
            },
            inputs=[{"path": "output/problem.mps", "media_type": "application/mps"}],
            outputs=[
                {"path": PRIMAL_SOLUTION_OUTPUT_PATH, "media_type": "application/json", "required": True}
            ],
            extensions={},
        )
    with pytest.raises(ValueError, match="'output/'"):
        SolverBundleManifest(
            bundle={
                "generator": "ies.generator.highs_milp@1.0.0",
                "contract": SOLVER_BUNDLE_MPS_CONTRACT,
            },
            inputs=[{"path": "input/problem.mps", "media_type": "application/mps"}],
            outputs=[
                {"path": "input/primal_solution.json", "media_type": "application/json", "required": True}
            ],
            extensions={},
        )

    # Schema/API parity for immediate traversal segments under input/ and output/.
    from jsonschema import Draft202012Validator

    schema_validator = Draft202012Validator(load_bundle_schema())
    base_bundle = _load_json(SB_VALID / "mps-minimal.json")
    for bad_input in ("input/../escape.mps", "input/..", "input/sub/../escape.mps"):
        payload = {**base_bundle, "inputs": [{"path": bad_input, "media_type": "application/mps"}]}
        assert list(schema_validator.iter_errors(payload)), f"schema missed {bad_input}"
        with pytest.raises(ValueError):
            validate_bundle(payload)
    for bad_output in ("output/../escape.json", "output/..", "output/sub/../escape.json"):
        payload = {
            **base_bundle,
            "outputs": [
                {"path": bad_output, "media_type": "application/json", "required": True},
                {"path": PRIMAL_SOLUTION_OUTPUT_PATH, "media_type": "application/json", "required": True},
            ],
        }
        assert list(schema_validator.iter_errors(payload)), f"schema missed {bad_output}"
        with pytest.raises(ValueError):
            validate_bundle(payload)


def test_solver_bundle_binding_and_file_set_checks() -> None:
    catalog = _stepped_plugin_catalog()
    cfg = validate_calculation_config(_load_json(CC_VALID / "stepped-minimal.json"))
    raw = _load_json(SB_VALID / "mps-minimal.json")

    manifest = validate_bundle(
        raw,
        generator_spec=catalog["ies.generator.highs_milp@1.0.0"],
        solver_spec=catalog["ies.solver.highs@1.7.2"],
        config=cfg,
    )
    assert manifest.bundle["contract"] == SOLVER_BUNDLE_MPS_CONTRACT

    incompatible_solver = StagePluginSpec(
        plugin_id="acme.solver.lp_only",
        version="1.0.0",
        kind="solver",
        consumes=("ies.solver-bundle.lp@1.0.0",),
        produces=(PRIMAL_SOLUTION_CONTRACT,),
        parameters_schema=_empty_object_schema(),
    )
    with pytest.raises(ValueError, match="不兼容 Bundle 契约"):
        validate_bundle(
            raw,
            generator_spec=catalog["ies.generator.highs_milp@1.0.0"],
            solver_spec=incompatible_solver,
        )

    unified_cfg = validate_calculation_config(_load_json(CC_VALID / "unified-minimal.json"))
    with pytest.raises(ValueError, match="一体化配置"):
        validate_bundle(raw, config=unified_cfg)

    with pytest.raises(ValueError, match="必须恰好对应"):
        SolverBundle(
            manifest=manifest,
            files={"input/problem.mps": MemoryContent(b"NAME DEMO\nENDATA\n")},
        )


def test_stage_compatibility_consumes_produces_and_slotted_parameters() -> None:
    catalog = _stepped_plugin_catalog()

    for valid_name in ("stepped-minimal.json", "stepped-full.json", "unified-minimal.json"):
        cfg = validate_calculation_config(_load_json(CC_VALID / valid_name))
        validate_stage_compatibility(cfg, catalog)

    # Incompatible generator -> solver contract
    lp_solver = StagePluginSpec(
        plugin_id="ies.solver.highs",
        version="1.7.2",
        kind="solver",
        consumes=("ies.solver-bundle.lp@1.0.0",),
        produces=(PRIMAL_SOLUTION_CONTRACT,),
        parameters_schema=_empty_object_schema(),
    )
    with pytest.raises(ComputationUnavailableError) as exc_info:
        validate_stage_compatibility(
            validate_calculation_config(_load_json(CC_VALID / "stepped-minimal.json")),
            {**catalog, lp_solver.ref: lp_solver},
        )
    assert exc_info.value.reason == "incompatible-contracts"

    # Solver missing ies.primal-solution@1.0.0 output
    bad_solver = StagePluginSpec(
        plugin_id="ies.solver.highs",
        version="1.7.2",
        kind="solver",
        consumes=(SOLVER_BUNDLE_MPS_CONTRACT,),
        produces=("acme.custom-solution@1.0.0",),
        parameters_schema=_empty_object_schema(),
    )
    with pytest.raises(ComputationUnavailableError, match="ies.primal-solution@1.0.0"):
        validate_stage_compatibility(
            validate_calculation_config(_load_json(CC_VALID / "stepped-minimal.json")),
            {**catalog, bad_solver.ref: bad_solver},
        )

    # Evaluator consumes mismatch
    bad_eval = StagePluginSpec(
        plugin_id="ies.evaluator.finance",
        version="1.0.0",
        kind="evaluator",
        consumes=("acme.other-input@1.0.0",),
        produces=(FINANCIAL_EVALUATION_CONTRACT,),
        parameters_schema=_empty_object_schema(),
    )
    with pytest.raises(ComputationUnavailableError) as exc_info:
        validate_stage_compatibility(
            validate_calculation_config(_load_json(CC_VALID / "unified-minimal.json")),
            {**catalog, bad_eval.ref: bad_eval},
        )
    assert exc_info.value.reason == "incompatible-contracts"

    # Per-stage parameters_schema checks: omitted required options.algorithm fails
    unified_raw = _load_json(CC_VALID / "unified-minimal.json")
    unified_empty_opts = validate_calculation_config({**unified_raw, "options": {}})
    with pytest.raises(ValueError, match="options/algorithm"):
        validate_stage_compatibility(unified_empty_opts, catalog)

    # Unknown field inside options.generator
    stepped_raw = _load_json(CC_VALID / "stepped-full.json")
    bad_gen_opts = validate_calculation_config(
        {**stepped_raw, "options": {"generator": {"unknown_flag": 1}, "solver": {}}}
    )
    with pytest.raises(ValueError, match="options/generator"):
        validate_stage_compatibility(bad_gen_opts, catalog)

    # Invalid evaluator parameter in evaluators[].parameters
    bad_eval_param = validate_calculation_config(
        {
            **stepped_raw,
            "evaluators": [
                {"evaluator": "acme.evaluator.carbon@1.0.0", "parameters": {"grid_factor": -1}}
            ],
        }
    )
    with pytest.raises(ValueError, match="evaluators/0/parameters"):
        validate_stage_compatibility(bad_eval_param, catalog)

    # Invalid prediction parameter in predictions[].parameters
    bad_pred_param = validate_calculation_config(
        {
            **stepped_raw,
            "predictions": [
                {
                    "target": {"instance": "pv1", "interface": "power_out"},
                    "algorithm": "ies.predictor.naive@1.0.0",
                    "parameters": {"window": 0},
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="predictions/0/parameters"):
        validate_stage_compatibility(bad_pred_param, catalog)

    # Companion constraint mismatch
    companion_gen = StagePluginSpec(
        plugin_id="ies.generator.highs_milp",
        version="1.0.0",
        kind="generator",
        consumes=(STAGE_INPUT_CONTRACT,),
        produces=(SOLVER_BUNDLE_MPS_CONTRACT,),
        parameters_schema=_empty_object_schema(),
        modes=("planning",),
        precisions=("low", "medium", "high"),
        outputs=("solution",),
        companions={"solver": "acme.solver.special@1.0.0"},
    )
    with pytest.raises(ComputationUnavailableError) as exc_info:
        validate_stage_compatibility(
            validate_calculation_config(_load_json(CC_VALID / "stepped-minimal.json")),
            {**catalog, companion_gen.ref: companion_gen},
        )
    assert exc_info.value.reason == "incompatible-companion"


def test_stage_compatibility_rejects_missing_capabilities_and_unsupported_contracts() -> None:
    catalog = _stepped_plugin_catalog()
    stepped_full = validate_calculation_config(_load_json(CC_VALID / "stepped-full.json"))
    stepped_minimal = validate_calculation_config(_load_json(CC_VALID / "stepped-minimal.json"))
    unified_minimal = validate_calculation_config(_load_json(CC_VALID / "unified-minimal.json"))

    # Empty consumes must NOT bypass stage-input contract check for predictor / generator / algorithm.
    for role_ref, kwargs in (
        (
            "ies.predictor.naive@1.0.0",
            {
                "plugin_id": "ies.predictor.naive",
                "version": "1.0.0",
                "kind": "predictor",
                "consumes": (),
                "produces": (PREDICTION_OUTPUT_CONTRACT,),
                "parameters_schema": _empty_object_schema(),
            },
        ),
        (
            "ies.generator.highs_milp@1.0.0",
            {
                "plugin_id": "ies.generator.highs_milp",
                "version": "1.0.0",
                "kind": "generator",
                "consumes": (),
                "produces": (SOLVER_BUNDLE_MPS_CONTRACT,),
                "parameters_schema": _empty_object_schema(),
                "modes": ("planning",),
                "precisions": ("medium",),
                "outputs": ("solution",),
            },
        ),
        (
            "acme.algorithm.capacity_search@1.2.0",
            {
                "plugin_id": "acme.algorithm.capacity_search",
                "version": "1.2.0",
                "kind": "algorithm",
                "consumes": (),
                "produces": (ENGINEERING_SOLUTION_CONTRACT,),
                "parameters_schema": catalog["acme.algorithm.capacity_search@1.2.0"].parameters_schema,
                "modes": ("planning",),
                "precisions": ("medium",),
                "outputs": ("solution", "metrics"),
            },
        ),
    ):
        cfg = unified_minimal if kwargs["kind"] == "algorithm" else stepped_full
        bad_spec = StagePluginSpec(**kwargs)
        with pytest.raises(ComputationUnavailableError) as exc_info:
            validate_stage_compatibility(cfg, {**catalog, role_ref: bad_spec})
        assert exc_info.value.reason == "incompatible-contracts"

    # Unsupported predictor / generator / evaluator produces contracts must be rejected.
    bad_pred_out = StagePluginSpec(
        plugin_id="ies.predictor.naive",
        version="1.0.0",
        kind="predictor",
        consumes=(STAGE_INPUT_CONTRACT,),
        produces=("acme.unreadable@1.0.0",),
        parameters_schema=_empty_object_schema(),
    )
    with pytest.raises(ComputationUnavailableError) as exc_info:
        validate_stage_compatibility(stepped_full, {**catalog, bad_pred_out.ref: bad_pred_out})
    assert exc_info.value.reason == "incompatible-contracts"

    bad_eval_out = StagePluginSpec(
        plugin_id="ies.evaluator.finance",
        version="1.0.0",
        kind="evaluator",
        consumes=(ENGINEERING_SOLUTION_CONTRACT,),
        produces=("acme.unrenderable@1.0.0",),
        parameters_schema=_empty_object_schema(),
        outputs=("metrics",),
    )
    with pytest.raises(ComputationUnavailableError) as exc_info:
        validate_stage_compatibility(unified_minimal, {**catalog, bad_eval_out.ref: bad_eval_out})
    assert exc_info.value.reason == "incompatible-contracts"

    # Matching custom namespace bundle contract between generator and solver must be accepted.
    custom_gen = StagePluginSpec(
        plugin_id="ies.generator.highs_milp",
        version="1.0.0",
        kind="generator",
        consumes=(STAGE_INPUT_CONTRACT,),
        produces=("acme.solver-bundle.custom@1.0.0",),
        parameters_schema=_empty_object_schema(),
        modes=("planning",),
        precisions=("medium",),
        outputs=("solution",),
    )
    custom_sol = StagePluginSpec(
        plugin_id="ies.solver.highs",
        version="1.7.2",
        kind="solver",
        consumes=("acme.solver-bundle.custom@1.0.0",),
        produces=(PRIMAL_SOLUTION_CONTRACT,),
        parameters_schema=_empty_object_schema(),
    )
    validate_stage_compatibility(
        stepped_minimal,
        {**catalog, custom_gen.ref: custom_gen, custom_sol.ref: custom_sol},
    )

    # Empty generator produces must still be rejected.
    empty_gen_produces = StagePluginSpec(
        plugin_id="ies.generator.highs_milp",
        version="1.0.0",
        kind="generator",
        consumes=(STAGE_INPUT_CONTRACT,),
        produces=(),
        parameters_schema=_empty_object_schema(),
        modes=("planning",),
        precisions=("medium",),
        outputs=("solution",),
    )
    with pytest.raises(ComputationUnavailableError) as exc_info:
        validate_stage_compatibility(
            stepped_minimal,
            {**catalog, empty_gen_produces.ref: empty_gen_produces},
        )
    assert exc_info.value.reason == "incompatible-contracts"

    # Empty modes, precisions, or outputs declarations must be rejected (no silent bypass).
    for field_name, reason in (
        ("modes", "incompatible-mode"),
        ("precisions", "incompatible-precision"),
        ("outputs", "incompatible-outputs"),
    ):
        base_kwargs: dict[str, Any] = {
            "plugin_id": "ies.generator.highs_milp",
            "version": "1.0.0",
            "kind": "generator",
            "consumes": (STAGE_INPUT_CONTRACT,),
            "produces": (SOLVER_BUNDLE_MPS_CONTRACT,),
            "parameters_schema": _empty_object_schema(),
            "modes": ("planning",),
            "precisions": ("medium",),
            "outputs": ("solution",),
        }
        base_kwargs[field_name] = ()
        with pytest.raises(ComputationUnavailableError) as exc_info:
            validate_stage_compatibility(
                stepped_minimal, {**catalog, "ies.generator.highs_milp@1.0.0": StagePluginSpec(**base_kwargs)}
            )
        assert exc_info.value.reason == reason

    # Evaluator with empty outputs capability must also be rejected.
    empty_eval_outputs = StagePluginSpec(
        plugin_id="ies.evaluator.finance",
        version="1.0.0",
        kind="evaluator",
        consumes=(ENGINEERING_SOLUTION_CONTRACT,),
        produces=(FINANCIAL_EVALUATION_CONTRACT,),
        parameters_schema=_empty_object_schema(),
        outputs=(),
    )
    with pytest.raises(ComputationUnavailableError) as exc_info:
        validate_stage_compatibility(
            unified_minimal, {**catalog, empty_eval_outputs.ref: empty_eval_outputs}
        )
    assert exc_info.value.reason == "incompatible-outputs"


def test_alignment_with_sdk_f1_contract_payloads() -> None:
    """Verify F1 contract IDs and payload shapes align with SDK schemas when repo is mounted."""
    import os

    from jsonschema import Draft202012Validator

    primal_payload = {
        "schema": "ies.primal-solution",
        "schema_version": "1.0.0",
        "math_status": "solved",
        "termination_reason": "optimal",
        "has_feasible_solution": True,
        "column_count": 2,
        "values": [10.0, 25.5],
        "objective_value": 1234.5,
        "best_bound": 1234.5,
        "relative_gap": 0.0,
        "receipt": {"solver": "ies.solver.highs@1.7.2", "elapsed_seconds": 0.12},
        "extensions": {},
    }
    assert f"{primal_payload['schema']}@{primal_payload['schema_version']}" == PRIMAL_SOLUTION_CONTRACT

    eng_feasible = {
        "schema": "ies.engineering-solution",
        "schema_version": "1.0.0",
        "solver_summary": {
            "math_status": "solved",
            "termination_reason": "optimal",
            "has_feasible_solution": True,
            "objective_value": 1234.5,
            "objective_unit": "CNY",
            "best_bound": 1234.5,
            "relative_gap": 0.0,
        },
        "construction_units": [
            {
                "unit_id": "u_pv",
                "instance_id": "pv1",
                "mode": "optional",
                "built": True,
                "scales": [{"planning_variable_id": "cap", "value": 100.0, "unit": "kW"}],
            }
        ],
        "series": {
            "s_pv": {"instance_id": "pv1", "variable_id": "p", "unit": "kW", "values": [10.0, 20.0]}
        },
        "system_series": {},
        "extensions": {},
    }
    eng_infeasible = {
        "schema": "ies.engineering-solution",
        "schema_version": "1.0.0",
        "solver_summary": {
            "math_status": "infeasible",
            "termination_reason": "infeasible",
            "has_feasible_solution": False,
            "objective_value": None,
            "objective_unit": None,
            "best_bound": None,
            "relative_gap": None,
        },
        "extensions": {},
    }
    assert (
        f"{eng_feasible['schema']}@{eng_feasible['schema_version']}" == ENGINEERING_SOLUTION_CONTRACT
    )

    report_payload = {
        "schema": "ies.evaluation-report",
        "schema_version": "1.0.0",
        "title": "Carbon Report",
        "evaluator": "acme.evaluator.carbon@1.0.0",
        "metrics": [{"id": "co2_total", "label": "Total CO2", "value": 42.0, "unit": "t"}],
        "charts": [],
        "tables": [],
        "extensions": {},
    }
    assert f"{report_payload['schema']}@{report_payload['schema_version']}" == EVALUATION_REPORT_CONTRACT

    env_root = os.environ.get("PIES_REPO_ROOT")
    candidates = [pathlib.Path(env_root)] if env_root else []
    candidates.extend([pathlib.Path(__file__).resolve().parents[2], pathlib.Path("/repo")])
    sdk_schema_dir = next(
        (
            c / "sdk" / "pies_sdk" / "schema"
            for c in candidates
            if (c / "sdk" / "pies_sdk" / "schema").is_dir()
        ),
        None,
    )
    if sdk_schema_dir is not None:
        Draft202012Validator(_load_json(sdk_schema_dir / "primal-solution-1.0.0.schema.json")).validate(
            primal_payload
        )
        eng_validator = Draft202012Validator(
            _load_json(sdk_schema_dir / "engineering-solution-1.0.0.schema.json")
        )
        eng_validator.validate(eng_feasible)
        eng_validator.validate(eng_infeasible)
        Draft202012Validator(
            _load_json(sdk_schema_dir / "evaluation-report-1.0.0.schema.json")
        ).validate(report_payload)
        plugin_schema = _load_json(sdk_schema_dir / "plugin-1.0.0.schema.json")
        assert "consumes" in plugin_schema["properties"]
        assert "produces" in plugin_schema["properties"]
