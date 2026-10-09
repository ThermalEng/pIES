"""首个真实 GeneratorProvider 测试：highs_milp@1.0.0。

只覆盖公开行为，不测私有实现，不启动 solver；正常生成一律走
``registry.resolve(config).generate(...)`` 正式路径：

- 真实 campus 五项输入生成完整 SolverBundle 聚合：manifest、problem.mps
  bytes、元数据 bytes、结构化 command、原始输出声明、unit map/objective
  offset/objective unit、逐字节确定性、不写工作目录；
- provider 特有检查：未知 outputs 与实际绑定 time_series 计费即明确
  拒绝，无 fallback（通用 generator/mode/精度/solver/options 门禁只在
  registry 侧覆盖，不在此重复证明）；
- 注册表注册真实 provider 实例：resolve/get 返回可调用 provider，
  快照与 to_dict 只公开 descriptor 数据；重复/协议不符/引用不一致/
  不可用即整体构建失败。

HiGHS CLI 真实执行验证不在本文件（见 Docker 集成探针记录），本文件
任何测试均不得启动外部进程。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from iesplan.assembly import ValidatedAssemblyArtifact, ValidationReceipt
from iesplan.assembly import validate_assembly_text
from iesplan.computation import (
    CalculationConfig,
    ComputationUnavailableError,
    GeneratorProvider,
    HIGHS_MILP_PROVIDER,
    SolverBundle,
    SolverBundleManifest,
    build_project_model,
    build_registry,
    export_bundle_files,
    materialize_non_predictive_series,
    write_mps,
)
from iesplan.computation.generators.highs_milp import (
    ADAPTER_REF,
    EXECUTOR_REF,
    GENERATOR_REF,
    SOLVER_REF,
)
from iesplan.devices import init_registry, list_devices
from iesplan.finance import effective_from_yaml
from iesplan.modeling import build_math_contribution

SAMPLES = Path(__file__).resolve().parent.parent / "iesplan" / "assembly" / "samples"


def _campus_inputs(*, outputs=("solution", "metrics"), seed=None):
    """真实 campus 五项输入（validator 签发 + 目录贡献 + 物化序列）。"""
    init_registry()
    text = (SAMPLES / "valid" / "campus.assembly.yaml").read_text(encoding="utf-8")
    validated = validate_assembly_text(text, package_dir=SAMPLES)
    assert validated.ok, [
        (d.code, d.params) for d in validated.diagnostics if d.blocking
    ]
    artifact = validated.artifact
    contributions = {}
    for doc in list_devices():
        built = build_math_contribution(doc)
        assert built.ok, (doc.device.id, built.diagnostics)
        contributions[doc.device.id] = built.contribution
    doc = json.loads(artifact.canonical_text)
    models = {
        inst: contributions[dev["definition"]["id"]]
        for inst, dev in doc["devices"].items()
    }
    finance = effective_from_yaml(
        (SAMPLES / "finance" / "campus.effective-finance.yaml").read_text(
            encoding="utf-8"
        )
    )

    def collect(node, found: set):
        if isinstance(node, dict):
            content = node.get("content")
            if isinstance(content, dict) and "object_id" in content:
                found.add(content["object_id"])
            for value in node.values():
                collect(value, found)
        elif isinstance(node, list):
            for value in node:
                collect(value, found)

    object_ids: set = set()
    collect(doc["devices"], object_ids)
    resources = {}
    for object_id in sorted(object_ids):
        rel = object_id[5:] if object_id.startswith("file:") else object_id
        resources[object_id] = (SAMPLES / rel).read_bytes()
    mat = materialize_non_predictive_series(artifact, resources, evaluation_years=1)
    assert not mat.pending_predict
    config = CalculationConfig(
        mode="planning",
        generator=GENERATOR_REF,
        solver=SOLVER_REF,
        precision="medium",
        time_limit_seconds=600,
        outputs=list(outputs),
        seed=seed,
    )
    return artifact, models, mat, finance, config


def _resolved_generate(*args, **kwargs):
    """正式路径：registry 精确选择 + 通用门禁后调用 generate。"""
    artifact, models, materialized, finance, config = _campus_inputs(*args, **kwargs)
    provider = build_registry((HIGHS_MILP_PROVIDER,)).resolve(config)
    assert provider is HIGHS_MILP_PROVIDER
    return (
        provider.generate(artifact, models, materialized, finance, config),
        (artifact, models, materialized, finance, config),
    )


def test_campus_generate_full_bundle() -> None:
    """campus 生成完整聚合：manifest/文件/command/输出声明互相一致。"""
    bundle, _ = _resolved_generate()
    assert isinstance(bundle, SolverBundle)
    manifest = bundle.manifest
    assert isinstance(manifest, SolverBundleManifest)
    assert manifest.bundle["generator"] == GENERATOR_REF
    assert manifest.bundle["solver"] == SOLVER_REF
    assert [e["path"] for e in manifest.inputs] == [
        "input/problem.mps",
        "input/parameters.json",
    ]
    assert [e["media_type"] for e in manifest.inputs] == [
        "application/mps",
        "application/json",
    ]
    assert set(bundle.files) == {"input/problem.mps", "input/parameters.json"}
    command = manifest.command
    assert command["executor"] == EXECUTOR_REF
    assert command["executable"] == SOLVER_REF
    assert list(command["arguments"]) == [
        "input/problem.mps",
        "--solution_file",
        "output/solution.sol",
        "--time_limit",
        "600",
        "--random_seed",
        "0",
    ]
    assert command["working_directory"] == "."
    assert command["timeout_seconds"] == 600
    assert command["network"] is False
    assert list(command["success_exit_codes"]) == [0]
    assert manifest.outputs == (
        {
            "path": "output/solution.sol",
            "media_type": "text/plain",
            "required": True,
        },
    )
    assert manifest.result_adapter == ADAPTER_REF
    assert manifest.extensions == {}
    metadata = json.loads(bundle.files["input/parameters.json"].decode("utf-8"))
    assert metadata["generator"] == GENERATOR_REF
    assert metadata["solver"] == SOLVER_REF
    assert metadata["result_adapter"] == ADAPTER_REF
    assert metadata["precision"] == "medium"
    assert metadata["seed"] == 0
    assert metadata["outputs"] == ["solution", "metrics"]
    assert metadata["assembly"]["id"] == "campus_demo"
    assert isinstance(metadata["unit_map"], dict) and metadata["unit_map"]
    assert isinstance(metadata["objective_offset"], float)
    # campus 目标为金额：目标业务单位取财务币种 CNY。
    assert metadata["objective_unit"] == "CNY"
    # mps 文件字节即 writer 输出；unit_map/offset 与直接构造一致。
    artifact, models, materialized, finance, config = _campus_inputs()
    model, unit_map, objective_unit = build_project_model(
        artifact, models, materialized, finance, config
    )
    mps_text, offset = write_mps(model)
    assert bundle.files["input/problem.mps"] == mps_text.encode("utf-8")
    assert metadata["unit_map"] == dict(sorted(unit_map.items()))
    assert metadata["objective_offset"] == pytest.approx(offset)
    assert metadata["objective_unit"] == objective_unit == "CNY"


def test_generate_is_byte_deterministic_and_writes_nothing(
    tmp_path, monkeypatch
) -> None:
    """同一输入逐字节相同；生成过程不写工作目录。"""
    first, _ = _resolved_generate(seed=7)
    second, _ = _resolved_generate(seed=7)
    assert export_bundle_files(first) == export_bundle_files(second)
    meta = json.loads(first.files["input/parameters.json"].decode("utf-8"))
    assert meta["seed"] == 7
    assert first.manifest.command["arguments"][-1] == "7"
    monkeypatch.chdir(tmp_path)
    _resolved_generate()
    assert list(tmp_path.iterdir()) == []


def _config_with(**kwargs) -> CalculationConfig:
    kwargs.setdefault("mode", "planning")
    kwargs.setdefault("generator", GENERATOR_REF)
    kwargs.setdefault("solver", SOLVER_REF)
    kwargs.setdefault("precision", "medium")
    kwargs.setdefault("time_limit_seconds", 600)
    kwargs.setdefault("outputs", ["solution"])
    return CalculationConfig(**kwargs)


def test_generate_rejects_unknown_outputs() -> None:
    """未知输出请求是明确的用户请求错误，不忽略（门禁后特有检查）。"""
    artifact, models, materialized, finance, config = _campus_inputs(
        outputs=["solution", "trace"]
    )
    provider = build_registry((HIGHS_MILP_PROVIDER,)).resolve(config)
    with pytest.raises(ValueError, match="不支持的输出请求"):
        provider.generate(artifact, models, materialized, finance, config)


def test_generate_rejects_bound_time_series_tariff() -> None:
    """实际绑定的计费价格为时变即拒绝；未绑定的快照条目不触发。"""
    artifact, models, materialized, finance, config = _campus_inputs()
    doc = json.loads(artifact.canonical_text)
    doc["tariff_bindings"]["import_meter"]["price"] = "grid_import_tou"
    rebound = ValidatedAssemblyArtifact(
        canonical_text=json.dumps(doc), receipt=ValidationReceipt(),
    )
    provider = build_registry((HIGHS_MILP_PROVIDER,)).resolve(config)
    with pytest.raises(ComputationUnavailableError) as exc_info:
        provider.generate(rebound, models, materialized, finance, config)
    assert exc_info.value.reason == "unsupported-tariff"


def test_registry_registers_real_provider() -> None:
    """注册表返回真实可调用 provider；快照/to_dict 只公开描述符数据。"""
    registry = build_registry((HIGHS_MILP_PROVIDER,))
    assert len(registry) == 1
    provider = registry.resolve(_config_with())
    assert isinstance(provider, GeneratorProvider)
    assert provider is HIGHS_MILP_PROVIDER
    assert provider.ref == GENERATOR_REF
    assert provider.descriptor.ref == GENERATOR_REF
    assert callable(provider.generate)
    assert registry.get(GENERATOR_REF) is HIGHS_MILP_PROVIDER
    snapshot = registry.snapshot()
    assert [d.ref for d in snapshot] == [GENERATOR_REF]
    dumped = registry.to_dict()
    assert [g["generator_id"] for g in dumped["generators"]] == [
        "ies.generator.highs_milp"
    ]
    assert "generate" not in json.dumps(dumped)


class _FakeProvider:
    """注册表原子性测试替身（可配引用/描述符/可用性）。"""

    def __init__(self, ref, descriptor, available=True):
        self._ref = ref
        self._descriptor = descriptor
        self._available = available

    @property
    def ref(self):
        return self._ref

    @property
    def descriptor(self):
        return self._descriptor

    def available(self):
        return self._available

    def generate(self, artifact, models, materialized, finance, config):
        raise AssertionError("测试替身不应被调用")


def _fake_descriptor(ref: str):
    from iesplan.computation import GeneratorDescriptor

    stable, _, version = ref.partition("@")
    return GeneratorDescriptor(
        generator_id=stable,
        version=version,
        options_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        assembly_schema_version="2.0.0",
        modes=("planning",),
        precisions=("medium",),
        solvers=(SOLVER_REF,),
        executors=(EXECUTOR_REF,),
        result_adapter=ADAPTER_REF,
        resource_limits={},
        unit_system="SI",
        seed_semantics="test",
    )


def test_registry_build_fails_atomically() -> None:
    """重复/协议不符/引用不一致/不可用即整体构建失败，不发布半表。"""
    good = _FakeProvider("t.provider@1.0.0", _fake_descriptor("t.provider@1.0.0"))
    with pytest.raises(ValueError, match="重复注册"):
        build_registry((good, good))
    with pytest.raises(TypeError):
        build_registry((good, object()))
    mismatch = _FakeProvider(
        "t.provider@1.0.0", _fake_descriptor("t.other@1.0.0")
    )
    with pytest.raises(ValueError, match="不一致"):
        build_registry((mismatch,))
    down = _FakeProvider(
        "t.provider@1.0.0", _fake_descriptor("t.provider@1.0.0"),
        available=False,
    )
    with pytest.raises(ComputationUnavailableError):
        build_registry((down,))
    nodescriptor = _FakeProvider("t.provider@1.0.0", None)
    with pytest.raises(TypeError):
        build_registry((nodescriptor,))


def test_bundle_aggregate_requires_exact_manifest_files_match() -> None:
    """聚合检查：清单与文件映射恰好对应；输出只是声明。"""
    bundle, _ = _resolved_generate()
    manifest = bundle.manifest
    with pytest.raises(ValueError, match="恰好对应"):
        SolverBundle(
            manifest=manifest,
            files={"input/problem.mps": b"mps"},
        )
    with pytest.raises(TypeError):
        SolverBundle(
            manifest=manifest,
            files={
                "input/problem.mps": "not-bytes",
                "input/parameters.json": b"{}",
            },
        )
    exported = export_bundle_files(bundle)
    assert set(exported) == {
        "bundle.yaml",
        "input/problem.mps",
        "input/parameters.json",
    }
    assert exported["bundle.yaml"].endswith(b"\n")
    assert "output/" not in exported
