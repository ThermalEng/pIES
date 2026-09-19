"""Solver Bundle 契约测试(0.8 切片 B)。

只覆盖公开 schema、规范化与校验边界，不涉及真实 generator、
solver 执行或结果适配：

- schema 文件元信息：$id/标题/必需键/结构化命令/未知字段拒绝，
  且 schema 内无任何 hash/摘要/完整性字段；
- 发布样例：合法通过并构造 frozen 载体；6 个非法样例各只违一条，
  逐个被拒绝；
- 规范化：键乱序得同一规范形式，序列顺序保持；
- 引用绑定：descriptor/config 一致通过，不一致（生成器/求解器/
  适配器）即拒绝，不回退。
"""

from __future__ import annotations

import dataclasses
import json
import pathlib

import pytest

from iesplan.computation import (
    SOLVER_BUNDLE_SCHEMA,
    SOLVER_BUNDLE_VERSION,
    CalculationConfig,
    GeneratorDescriptor,
    SolverBundle,
    load_bundle_schema,
    normalize_bundle,
    validate_bundle,
)

SAMPLES = pathlib.Path(__file__).parent.parent / "iesplan" / "computation" / "samples"
VALID = SAMPLES / "valid" / "1.0" / "minimal.json"
INVALID = SAMPLES / "invalid" / "1.0"

GENERATOR = "acme.generator@1.0.0"
SOLVER = "ies.solver.highs@1.7.2"
ADAPTER = "acme.result-adapter@1.0.0"


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _descriptor(**kwargs) -> GeneratorDescriptor:
    kwargs.setdefault("generator_id", "acme.generator")
    kwargs.setdefault("version", "1.0.0")
    kwargs.setdefault("assembly_schema_version", "1.0")
    kwargs.setdefault("solvers", (SOLVER,))
    kwargs.setdefault("options_schema", {})
    kwargs.setdefault("resource_limits", {})
    kwargs.setdefault("unit_system", "SI")
    kwargs.setdefault("seed_semantics", "options.seed 为空时使用固定种子 0")
    kwargs.setdefault("result_adapter", ADAPTER)
    return GeneratorDescriptor(**kwargs)


def _config(**kwargs) -> CalculationConfig:
    kwargs.setdefault("generator", GENERATOR)
    kwargs.setdefault("solver", SOLVER)
    return CalculationConfig(**kwargs)


def test_schema_file_meta() -> None:
    """schema 文件标识与必需结构；无 hash/摘要/完整性字段。"""
    schema = load_bundle_schema()
    assert schema["$id"] == f"{SOLVER_BUNDLE_SCHEMA}/{SOLVER_BUNDLE_VERSION}"
    assert schema["title"] == "ies.solver-bundle 1.0.0 求解交接包契约"
    assert set(schema["required"]) == {
        "schema", "schema_version", "bundle_id", "generator_ref",
        "solver_ref", "config_id", "inputs", "command",
        "declared_outputs", "result_adapter_ref",
    }
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema"] == {"const": SOLVER_BUNDLE_SCHEMA}
    assert schema["properties"]["schema_version"] == {"const": SOLVER_BUNDLE_VERSION}
    command = schema["properties"]["command"]
    assert set(command["required"]) == {"executor", "executable", "arguments"}
    assert command["additionalProperties"] is False
    text = json.dumps(schema)
    for forbidden in ("hash", "digest", "checksum", "sha256", "sha-256"):
        assert forbidden not in text.lower()


def test_valid_sample_builds_frozen_bundle() -> None:
    """合法样例通过校验并构造深层不可变载体。"""
    bundle = validate_bundle(_load(VALID))
    assert isinstance(bundle, SolverBundle)
    assert bundle.generator_ref == GENERATOR
    assert bundle.solver_ref == SOLVER
    assert bundle.result_adapter_ref == ADAPTER
    assert bundle.to_dict() == _load(VALID)
    with pytest.raises(dataclasses.FrozenInstanceError):
        bundle.bundle_id = "other"  # type: ignore[misc]


EXPECTED_INVALID = {
    "bad-schema-word.json": "schema",
    "missing-adapter-ref.json": "result_adapter_ref",
    "absolute-output-path.json": "declared_outputs/0",
    "escaping-output-path.json": "declared_outputs/0",
    "shell-executable.json": "command/executable",
    "extra-top-level-key.json": "extensions",
}


def test_invalid_samples_rejected() -> None:
    """非法样例各只违一条，逐个被拒绝且定位违规位置。"""
    assert sorted(p.name for p in INVALID.glob("*.json")) == sorted(EXPECTED_INVALID)
    for name, location in EXPECTED_INVALID.items():
        with pytest.raises(ValueError) as exc_info:
            validate_bundle(_load(INVALID / name))
        assert location in str(exc_info.value), name


def test_normalize_is_order_insensitive() -> None:
    """键乱序得同一规范形式；序列顺序保持语义。"""
    raw = _load(VALID)
    shuffled = dict(reversed(list(raw.items())))
    assert normalize_bundle(shuffled) == normalize_bundle(raw)
    assert normalize_bundle(raw)["declared_outputs"] == ["output/solution.json"]
    with pytest.raises(TypeError):
        normalize_bundle({"inputs": {"a": b"bytes"}})  # type: ignore[dict-item]
    with pytest.raises(TypeError):
        validate_bundle([("schema", SOLVER_BUNDLE_SCHEMA)])  # type: ignore[arg-type]


def test_binding_against_descriptor_and_config() -> None:
    """descriptor/config 一致通过；引用不一致即拒绝，不回退。"""
    bundle = validate_bundle(_load(VALID), descriptor=_descriptor(), config=_config())
    assert bundle.bundle_id == "bundle-minimal-1"
    with pytest.raises(ValueError, match="不一致"):
        validate_bundle(
            _load(VALID),
            descriptor=_descriptor(generator_id="other.generator"),
            config=_config(),
        )
    with pytest.raises(ValueError, match="不一致"):
        validate_bundle(_load(VALID), descriptor=_descriptor(), config=_config(solver="other.solver@9.9.9"))
    with pytest.raises(ValueError, match="不一致"):
        validate_bundle(
            _load(VALID),
            descriptor=_descriptor(result_adapter="other.adapter@1.0.0"),
            config=_config(),
        )
    with pytest.raises(ValueError, match="不兼容"):
        validate_bundle(
            _load(VALID), descriptor=_descriptor(solvers=("other.solver@9.9.9",)), config=_config()
        )
