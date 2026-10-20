"""CalculationConfig 契约测试(0.8 切片 C)。

字段唯一依据 domain-model 与 calculation-config 格式页，不含离散化，
也不含规划目标/变量/约束、设备参数、财务配置、资源或装配内容：

- schema 文件元信息：$id/标题/必需键/精度枚举/未知字段拒绝，
  且 schema 内无任何 hash/摘要/完整性字段；
- 发布样例：合法通过并构造 frozen 载体；7 个非法样例各只违一条，
  逐个被拒绝（含离散化字段直接拒绝）；
- 载体规则：mode 非空、预测目标去重且 to_dict 稳定排序、精度枚举、
  收敛正值可空、时间限制正整数、输出非空保序、options 可空只读。
"""

from __future__ import annotations

import dataclasses
import json
import pathlib

import pytest

from iesplan.computation import (
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
INVALID = SAMPLES / "invalid" / "2.0"

GENERATOR = "acme.generator@1.0.0"
SOLVER = "ies.solver.highs@1.7.2"


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
    """schema 文件标识与计算配置必需结构；无 hash/摘要/完整性字段。"""
    schema = load_calculation_config_schema()
    assert schema["$id"] == f"{CALCULATION_CONFIG_SCHEMA}/{CALCULATION_CONFIG_VERSION}"
    assert schema["title"] == "ies.calculation-config 2.0.0 计算配置契约"
    assert set(schema["required"]) == {
        "schema", "schema_version", "mode", "predictions", "generator",
        "solver", "precision", "convergence", "time_limit_seconds",
        "options", "seed", "outputs",
    }
    assert schema["properties"]["predictions"]["type"] == "array"
    assert schema["properties"]["options"] == {"type": "object"}
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema"] == {"const": CALCULATION_CONFIG_SCHEMA}
    assert schema["properties"]["precision"] == {"enum": ["low", "medium", "high"]}
    assert "discretization" not in schema["properties"]
    text = json.dumps(schema)
    for forbidden in ("hash", "digest", "checksum", "sha256", "sha-256"):
        assert forbidden not in text.lower()


def test_valid_sample_builds_frozen_config() -> None:
    """合法样例通过校验并构造深层不可变载体。"""
    config = validate_calculation_config(_load(VALID))
    assert isinstance(config, CalculationConfig)
    assert config.mode == "planning"
    assert config.precision == "medium"
    assert config.to_dict() == _load(VALID)
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.mode = "other"  # type: ignore[misc]


EXPECTED_INVALID = {
    "bad-precision.json": "ultra",
    "discretization-rejected.json": "discretization",
    "duplicate-target.json": "不得重复",
    "bad-convergence.json": "tolerance",
    "missing-mode.json": "mode",
    "missing-convergence.json": "convergence",
    "empty-outputs.json": "outputs",
}


def test_invalid_samples_rejected() -> None:
    """非法样例各只违一条，逐个被拒绝且定位违规位置。"""
    assert sorted(p.name for p in INVALID.glob("*.json")) == sorted(EXPECTED_INVALID)
    for name, location in EXPECTED_INVALID.items():
        with pytest.raises(ValueError) as exc_info:
            validate_calculation_config(_load(INVALID / name))
        assert location in str(exc_info.value), name


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
        "pv1", "zeta",
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


def test_convergence_optional_but_strict() -> None:
    """收敛可空；给出即恰二键、正有限容差与正整数迭代。"""
    assert _config(convergence=None).convergence is None
    good = _config(convergence={"tolerance": 0.01, "max_iterations": 10})
    assert good.to_dict()["convergence"] == {
        "tolerance": 0.01, "max_iterations": 10,
    }
    for bad in (
        {"tolerance": 0, "max_iterations": 10},
        {"tolerance": float("inf"), "max_iterations": 10},
        {"tolerance": 0.01, "max_iterations": 0},
        {"tolerance": 0.01, "max_iterations": True},
        {"tolerance": 0.01},
    ):
        with pytest.raises(ValueError):
            _config(convergence=bad)


def test_scalar_selections() -> None:
    """mode/精度/时间限制/输出/options 的选择语义。"""
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
    assert _config(options={"k": [1]}).to_dict()["options"] == {"k": [1]}
    with pytest.raises(ValueError, match="非有限"):
        _config(options={"k": float("nan")})
    with pytest.raises(TypeError, match="布尔"):
        _config(seed=True)
