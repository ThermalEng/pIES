"""CalculationConfig 1.2.0 契约测试 (13 字段 + 并联 evaluators + 分槽 options)。"""

from __future__ import annotations

import dataclasses
import json
import pathlib

import pytest

from iesplan.computation import (
    CALCULATION_CONFIG_FIELDS,
    CALCULATION_CONFIG_SCHEMA,
    CALCULATION_CONFIG_VERSION,
    CalculationConfig,
    load_calculation_config_schema,
    validate_calculation_config,
)

SAMPLES = (
    pathlib.Path(__file__).parent.parent
    / "iesplan"
    / "computation"
    / "samples"
    / "calculation-config"
)
VALID = SAMPLES / "valid" / "2.0" / "minimal.json"
VALID_UNIFIED = SAMPLES / "valid" / "2.0" / "unified-minimal.json"
INVALID = SAMPLES / "invalid" / "2.0"

GENERATOR = "acme.generator@1.0.0"
SOLVER = "ies.solver.highs@1.7.2"
EVALUATOR = "ies.evaluator.finance@1.0.0"


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _config(**kwargs) -> CalculationConfig:
    kwargs.setdefault("mode", "planning")
    kwargs.setdefault("generator", GENERATOR)
    kwargs.setdefault("solver", SOLVER)
    kwargs.setdefault("precision", "medium")
    kwargs.setdefault("time_limit_seconds", 600)
    kwargs.setdefault("outputs", ["solution"])
    return CalculationConfig(**kwargs)


def test_schema_file_meta() -> None:
    """schema 文件标识与 13 字段计算配置必需结构；无 hash/摘要/完整性字段。"""
    schema = load_calculation_config_schema()
    assert schema["$id"] == f"{CALCULATION_CONFIG_SCHEMA}/{CALCULATION_CONFIG_VERSION}"
    assert schema["title"] == "ies.calculation-config 1.0.0 计算配置目标契约"
    assert set(schema["required"]) == set(CALCULATION_CONFIG_FIELDS)
    assert len(CALCULATION_CONFIG_FIELDS) == 13
    assert "convergence" not in schema["properties"]
    assert "evaluator" not in schema["properties"]
    assert "evaluators" in schema["properties"]
    assert schema["properties"]["predictions"]["type"] == "array"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema"] == {"const": CALCULATION_CONFIG_SCHEMA}
    assert schema["properties"]["precision"] == {"enum": ["low", "medium", "high"]}
    assert "discretization" not in schema["properties"]
    text = json.dumps(schema)
    for forbidden in ("hash", "digest", "checksum", "sha256", "sha-256"):
        assert forbidden not in text.lower()


def test_valid_sample_builds_frozen_config() -> None:
    """合法样例（分步与一体化）通过校验并构造深层不可变载体。"""
    config = validate_calculation_config(_load(VALID))
    assert isinstance(config, CalculationConfig)
    assert config.mode == "planning"
    assert config.precision == "medium"
    assert config.algorithm is None
    assert config.evaluators == ()
    assert config.to_dict() == _load(VALID)
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.mode = "other"  # type: ignore[misc]

    unified = validate_calculation_config(_load(VALID_UNIFIED))
    assert isinstance(unified, CalculationConfig)
    assert unified.generator is None
    assert unified.solver is None
    assert unified.algorithm == "acme.algorithm.capacity_search@1.2.0"
    assert unified.evaluators == (
        {"evaluator": EVALUATOR, "parameters": {}},
    )
    assert unified.to_dict() == _load(VALID_UNIFIED)


EXPECTED_INVALID = {
    "bad-precision.json": "ultra",
    "bad-slotted-options.json": "gap_rel",
    "both-generator-and-algorithm.json": "null",
    "discretization-rejected.json": "discretization",
    "duplicate-target.json": "不得重复",
    "empty-outputs.json": "outputs",
    "legacy-convergence-rejected.json": "convergence",
    "missing-mode.json": "mode",
    "neither-generator-nor-algorithm.json": "generator",
}


def test_invalid_samples_rejected() -> None:
    """非法样例各只违一条，逐个被拒绝且定位违规位置。"""
    assert sorted(p.name for p in INVALID.glob("*.json")) == sorted(EXPECTED_INVALID)
    for name, location in EXPECTED_INVALID.items():
        with pytest.raises(ValueError) as exc_info:
            validate_calculation_config(_load(INVALID / name))
        assert location in str(exc_info.value), (name, str(exc_info.value))


def test_predictions_sorted_unique_and_shape_checked() -> None:
    """预测目标去重、to_dict 稳定排序、算法仅形态校验。"""
    config = _config(
        predictions=[
            {
                "target": {"instance": "zeta", "interface": "b"},
                "algorithm": "ies.predictor.naive@1.0.0",
                "parameters": {},
            },
            {
                "target": {"instance": "pv1", "interface": "power_out"},
                "algorithm": "ies.predictor.naive@1.0.0",
                "parameters": {"p": 1},
            },
        ]
    )
    assert [p["target"]["instance"] for p in config.to_dict()["predictions"]] == [
        "zeta", "pv1",
    ]
    with pytest.raises(TypeError):
        config.predictions[0]["parameters"]["p"] = 2  # type: ignore[index]
    with pytest.raises(TypeError, match="None"):
        _config(predictions=None)
    assert _config(predictions=[]).predictions == ()
    with pytest.raises(ValueError, match="非有限"):
        _config(
            predictions=[
                {
                    "target": {"instance": "pv1", "interface": "power_out"},
                    "algorithm": "ies.predictor.naive@1.0.0",
                    "parameters": {"w": float("inf")},
                }
            ]
        )
    with pytest.raises(ValueError, match="不得重复"):
        _config(
            predictions=[
                {
                    "target": {"instance": "pv1", "interface": "power_out"},
                    "algorithm": "ies.predictor.naive@1.0.0",
                    "parameters": {},
                },
                {
                    "target": {"instance": "pv1", "interface": "power_out"},
                    "algorithm": "ies.predictor.naive@1.0.0",
                    "parameters": {},
                },
            ]
        )
    with pytest.raises(ValueError, match="algorithm"):
        _config(
            predictions=[
                {
                    "target": {"instance": "pv1", "interface": "power_out"},
                    "algorithm": "not-a-ref",
                    "parameters": {},
                }
            ]
        )


def test_scalar_selections_and_slotted_options() -> None:
    """mode/精度/时间限制/输出/分槽 options 的选择语义。"""
    with pytest.raises(ValueError, match="mode"):
        _config(mode="")
    with pytest.raises(ValueError, match="precision"):
        _config(precision="ultra")
    with pytest.raises(ValueError, match="time_limit_seconds"):
        _config(time_limit_seconds=0)
    with pytest.raises(ValueError, match="time_limit_seconds"):
        _config(time_limit_seconds=1.5)
    with pytest.raises(ValueError, match="outputs"):
        _config(outputs=[])
    ordered = _config(outputs=["b", "a"])
    assert ordered.to_dict()["outputs"] == ["b", "a"]
    with pytest.raises(TypeError, match="Mapping"):
        _config(options=None)
    assert _config(options={}).options == {}
    slotted = _config(options={"generator": {"k": [1]}, "solver": {"threads": 2}})
    assert slotted.to_dict()["options"] == {"generator": {"k": [1]}, "solver": {"threads": 2}}
    with pytest.raises(ValueError, match="options"):
        _config(options={"algorithm": {}})
    with pytest.raises(ValueError, match="options"):
        _config(options={"flat_key": 1})
    with pytest.raises(ValueError, match="非有限"):
        _config(options={"generator": {"k": float("nan")}})
    with pytest.raises(TypeError, match="布尔"):
        _config(seed=True)


def test_mutually_exclusive_modes_and_evaluators() -> None:
    """分步模式与一体化模式严格互斥；evaluators 在两种模式下均按需多选。"""
    stepped_with_eval = _config(evaluators=[{"evaluator": EVALUATOR, "parameters": {}}])
    assert len(stepped_with_eval.evaluators) == 1
    assert stepped_with_eval.evaluators[0]["evaluator"] == EVALUATOR

    unified = CalculationConfig(
        mode="planning",
        algorithm="acme.algorithm.capacity_search@1.2.0",
        evaluators=[{"evaluator": EVALUATOR, "parameters": {}}],
        precision="medium",
        time_limit_seconds=600,
        options={"algorithm": {"steps": 10}},
        outputs=["solution"],
    )
    assert unified.generator is None
    assert unified.solver is None
    assert unified.algorithm == "acme.algorithm.capacity_search@1.2.0"

    with pytest.raises(ValueError, match="generator 与 solver 必须为 None"):
        CalculationConfig(
            mode="planning",
            generator=GENERATOR,
            solver=None,
            algorithm="acme.algorithm.capacity_search@1.2.0",
            precision="medium",
            time_limit_seconds=600,
            outputs=["solution"],
        )
    with pytest.raises(ValueError, match="generator 与 solver 必须同时提供"):
        CalculationConfig(
            mode="planning",
            generator=GENERATOR,
            solver=None,
            algorithm=None,
            precision="medium",
            time_limit_seconds=600,
            outputs=["solution"],
        )
    with pytest.raises(ValueError, match="evaluators"):
        _config(evaluators=[{"evaluator": "not-a-ref", "parameters": {}}])
    with pytest.raises(ValueError, match="不得重复"):
        _config(
            evaluators=[
                {"evaluator": EVALUATOR, "parameters": {}},
                {"evaluator": EVALUATOR, "parameters": {"x": 1}},
            ]
        )
