"""Solver Bundle 契约测试(0.8 切片 B 修正版)。

manifest 载体为 ``SolverBundleManifest``（完整聚合为 ``SolverBundle``，
见 provider 测试）；字段唯一依据 ``formats/solver-bundle.md`` 的
manifest，不增删：schema/schema_version、bundle{generator, solver}、
inputs[{path, media_type}]、command、
outputs[{path, media_type, required}]、result_adapter、extensions。
只覆盖公开 schema、规范化、校验与规范字节导出边界，不涉及真实
generator、solver 执行或结果适配：

- schema 文件元信息：$id/标题/必需键/结构化命令/未知字段拒绝，
  且 schema 内无任何 hash/摘要/完整性字段；
- 发布样例：合法通过并构造 frozen manifest 载体；6 个非法样例各只违
  一条，逐个被拒绝；
- 规范化：键乱序得同一规范形式，序列顺序保持；
- 引用绑定：descriptor/config 一致通过，不一致（生成器/求解器/
  适配器）即拒绝，不回退；
- 规范字节：同一 manifest 恒得同一 bundle.yaml 字节，字段顺序固定。
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
    SolverBundleManifest,
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
ADAPTER = "acme.result-adapter@1.0.0"


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _descriptor(**kwargs) -> GeneratorDescriptor:
    kwargs.setdefault("generator_id", "acme.generator")
    kwargs.setdefault("version", "1.0.0")
    kwargs.setdefault("assembly_schema_version", "1.0.0")
    kwargs.setdefault("modes", ("planning",))
    kwargs.setdefault("precisions", ("low", "medium", "high"))
    kwargs.setdefault("outputs", ("solution",))
    kwargs.setdefault("solvers", (SOLVER,))
    kwargs.setdefault(
        "options_schema",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )
    kwargs.setdefault("resource_limits", {})
    kwargs.setdefault("unit_system", "SI")
    kwargs.setdefault("seed_semantics", "options.seed 为空时使用固定种子 0")
    kwargs.setdefault("result_adapter", ADAPTER)
    return GeneratorDescriptor(**kwargs)


def _config(**kwargs) -> CalculationConfig:
    kwargs.setdefault("mode", "planning")
    kwargs.setdefault("generator", GENERATOR)
    kwargs.setdefault("solver", SOLVER)
    kwargs.setdefault("precision", "medium")
    kwargs.setdefault("time_limit_seconds", 600)
    kwargs.setdefault("outputs", ["solution"])
    return CalculationConfig(**kwargs)


def test_schema_file_meta() -> None:
    """schema 文件标识与 manifest 必需结构；无 hash/摘要/完整性字段。"""
    schema = load_bundle_schema()
    assert schema["$id"] == f"{SOLVER_BUNDLE_SCHEMA}/{SOLVER_BUNDLE_VERSION}"
    assert schema["title"] == "ies.solver-bundle 1.0.0 求解交接包契约"
    assert set(schema["required"]) == {
        "schema", "schema_version", "bundle", "inputs", "command",
        "outputs", "result_adapter", "extensions",
    }
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema"] == {"const": SOLVER_BUNDLE_SCHEMA}
    assert schema["properties"]["schema_version"] == {"const": SOLVER_BUNDLE_VERSION}
    bundle = schema["properties"]["bundle"]
    assert set(bundle["required"]) == {"generator", "solver"}
    assert bundle["additionalProperties"] is False
    inputs = schema["properties"]["inputs"]
    assert set(inputs["items"]["required"]) == {"path", "media_type"}
    outputs = schema["properties"]["outputs"]
    assert set(outputs["items"]["required"]) == {"path", "media_type", "required"}
    command = schema["properties"]["command"]
    assert set(command["required"]) == {"executor", "executable", "arguments"}
    assert command["additionalProperties"] is False
    text = json.dumps(schema)
    for forbidden in ("hash", "digest", "checksum", "sha256", "sha-256"):
        assert forbidden not in text.lower()


def test_valid_sample_builds_frozen_bundle() -> None:
    """合法样例通过校验并构造深层不可变 manifest 载体；字段一一对应。"""
    bundle = validate_bundle(_load(VALID))
    assert isinstance(bundle, SolverBundleManifest)
    assert bundle.bundle["generator"] == GENERATOR
    assert bundle.bundle["solver"] == SOLVER
    assert bundle.inputs[0]["media_type"] == "application/mps"
    assert bundle.outputs[0]["required"] is True
    assert bundle.result_adapter == ADAPTER
    assert bundle.extensions == {}
    assert bundle.to_dict() == _load(VALID)
    with pytest.raises(dataclasses.FrozenInstanceError):
        bundle.result_adapter = "other"  # type: ignore[misc]


EXPECTED_INVALID = {
    "bad-schema-word.json": "schema",
    "missing-adapter-ref.json": "result_adapter",
    "absolute-output-path.json": "outputs/0",
    "escaping-input-path.json": "inputs/0",
    "shell-executable.json": "command/executable",
    "extra-top-level-key.json": "calculation",
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
    assert normalize_bundle(raw)["outputs"][0]["path"] == "output/solution.json"
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
    assert yaml_load(first.decode("utf-8")) == bundle.to_dict()
    parsed = yaml_load(first.decode("utf-8"))
    assert parsed["schema"] == SOLVER_BUNDLE_SCHEMA
    assert parsed["bundle"] == {
        "generator": GENERATOR, "solver": SOLVER,
    }
    assert [e["path"] for e in parsed["inputs"]] == [
        "input/problem.mps", "input/parameters.json",
    ]
    assert parsed["command"]["arguments"] == [
        "input/problem.mps", "--solution_file", "output/solution.json",
    ]
    assert parsed["command"]["environment"] == {"OMP_NUM_THREADS": "4"}
    assert parsed["outputs"][0]["required"] is True
    assert parsed["result_adapter"] == ADAPTER
    with pytest.raises(TypeError):
        manifest_to_yaml_bytes({"schema": "ies.solver-bundle"})  # type: ignore[arg-type]


def test_binding_against_descriptor_and_config() -> None:
    """descriptor/config 一致通过；引用不一致即拒绝，不回退。"""
    bundle = validate_bundle(_load(VALID), descriptor=_descriptor(), config=_config())
    assert bundle.bundle["generator"] == GENERATOR
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
