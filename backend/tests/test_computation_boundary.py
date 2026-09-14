"""computation 边界契约测试(Wave4-A, F3 接线后; G1 密封修订)。

只覆盖真实边界, 不涉及任何 0.8 算法实现:

- Protocol 形状: 三个边界协议为运行时可检查的 Protocol, 且拒绝
  不完整实现的结构判定; 三协议用各自独立的最小替身验证(不用一个
  同时实现三协议的假对象证明错误形状);
- 宪法 §4.5 方法形状: generate 消费装配产物/已固定资源/公开配置产出
  Bundle; run 消费 Bundle 产出回执与原始输出; adapt 消费三件套产出
  统一结果;
- 键与组合归属: provider 稳定键与 ``resolve_capabilities`` 只归
  ``iesplan.computation``; application 不知键(无键常量、无解析器、
  无重建器); 判定只用 Protocol ``isinstance``, 不猜方法名;
- 类型化输入契约: 快照已签发事实的运行期表示映射(缺字段即映射失败,
  不重跑装配校验/阻断判断, 不补默认值); 快照记录与资源深层不可变;
- unavailable 错误语义: ``ComputationUnavailableError`` 为
  ``RuntimeError``, 默认 reason 为 ``"no-provider"``;
- provider 目录为空: ``available_providers()`` 恒为空, 且返回副本
  (调用方改写不污染门面);
- 注册表门面: ``iesplan.computation`` 选择性重导出注册表符号,
  ``get_algorithm`` / ``list_algorithms`` 行为与搬迁前一致。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from types import MappingProxyType

import pytest

from iesplan.assembly import ValidatedAssemblyArtifact, ValidationReceipt
from iesplan.computation import (
    CALCULATION_CONFIG_SCHEMA,
    CALCULATION_CONFIG_VERSION,
    COMPUTE_RESULT_SCHEMA,
    COMPUTE_RESULT_VERSION,
    DEFAULT_ALGORITHM,
    GENERATOR_PROVIDER_KEY,
    RESULT_ADAPTER_KEY,
    SOLVER_BUNDLE_SCHEMA,
    SOLVER_BUNDLE_VERSION,
    SOLVER_RUNTIME_KEY,
    AlgorithmSpec,
    CalculationConfig,
    ComputationUnavailableError,
    ComputeCapabilities,
    ComputeInputs,
    ComputeResources,
    ComputeResult,
    ExecutionReceipt,
    GeneratorProvider,
    ResultAdapter,
    SolverBundle,
    SolverRuntime,
    available_providers,
    build_compute_inputs,
    get_algorithm,
    list_algorithms,
    resolve_capabilities,
)
from iesplan.core.diagnostics import DATA_TS_DUP, SEVERITY_WARNING, make_diag
from iesplan.core.errors import NotFoundError
from iesplan.tasks import CalcSnapshotRecord


def _artifact() -> ValidatedAssemblyArtifact:
    return ValidatedAssemblyArtifact(
        canonical_text='{"schema":"ies.assembly","schema_version":"1.0.0"}\n',
        receipt=ValidationReceipt(),
    )


def _bundle() -> SolverBundle:
    return SolverBundle(
        bundle_id="bundle-1",
        generator_ref="acme.generator@1.0.0",
        solver_ref="ies.solver.highs@1.7.2",
        config_id=f"{CALCULATION_CONFIG_SCHEMA}@{CALCULATION_CONFIG_VERSION}",
        inputs={"input/problem.mps": "mps-bytes"},
        command={"executor": "ies.executor.local_process@1.0.0", "arguments": ["input/problem.mps"]},
        declared_outputs=("output/solution.json",),
        result_adapter_ref="acme.result-adapter@1.0.0",
    )


def _snapshot(
    *,
    params: object = {"gap_rel": 0.01},
    tolerances: object | None = {"rel": 1e-6},
    seed: int | None = 42,
    receipt: object = None,
) -> CalcSnapshotRecord:
    """最小合法快照记录(封存表示齐全, receipt 缺省取空回执封存)。"""
    stored = ValidationReceipt().to_dict() if receipt is None else receipt
    config = {"task_params": {"k": 1}, "params": params}
    return CalcSnapshotRecord(
        id=1,
        project_version_id=2,
        dataset_version_ids=[7],
        calc_config_snapshot=config,
        random_seed=seed,
        tolerances=tolerances,
        canonical_assembly_text='{"schema":"ies.assembly","schema_version":"1.0.0"}\n',
        assembly_receipt=stored,
    )


# ---------------------------------------------------------------------------
# 三协议: 各自独立的最小替身
# ---------------------------------------------------------------------------


class _GeneratorOnly:
    """仅实现 GeneratorProvider 的最小替身(无任何算法逻辑)。"""

    @property
    def ref(self) -> str:
        return "acme.generator@1.0.0"

    def available(self) -> bool:
        return False

    def generate(
        self,
        artifact: ValidatedAssemblyArtifact,
        resources: Mapping[str, object],
        config: CalculationConfig,
    ) -> SolverBundle:
        raise ComputationUnavailableError(reason="no-provider")


class _RuntimeOnly:
    """仅实现 SolverRuntime 的最小替身(无任何算法逻辑)。"""

    @property
    def ref(self) -> str:
        return "ies.solver.highs@1.7.2"

    def available(self) -> bool:
        return False

    def run(
        self, bundle: SolverBundle
    ) -> tuple[ExecutionReceipt, Mapping[str, object]]:
        raise ComputationUnavailableError(reason="no-provider")


class _AdapterOnly:
    """仅实现 ResultAdapter 的最小替身(无任何算法逻辑)。"""

    def adapt(
        self,
        bundle: SolverBundle,
        receipt: ExecutionReceipt,
        outputs: Mapping[str, object],
    ) -> ComputeResult:
        raise ComputationUnavailableError(reason="no-provider")


def test_protocols_are_runtime_checkable_shapes() -> None:
    """三协议为运行时 Protocol; 各自独立替身只通过自身结构判定。"""
    gen, runtime, adapter = _GeneratorOnly(), _RuntimeOnly(), _AdapterOnly()
    assert isinstance(gen, GeneratorProvider)
    assert not isinstance(gen, SolverRuntime)
    assert not isinstance(gen, ResultAdapter)
    assert isinstance(runtime, SolverRuntime)
    assert not isinstance(runtime, GeneratorProvider)
    assert not isinstance(runtime, ResultAdapter)
    assert isinstance(adapter, ResultAdapter)
    assert not isinstance(adapter, GeneratorProvider)
    assert not isinstance(adapter, SolverRuntime)
    assert not isinstance(object(), GeneratorProvider)
    assert not isinstance(object(), SolverRuntime)
    assert not isinstance(object(), ResultAdapter)


def test_protocol_methods_match_constitution_boundaries() -> None:
    """宪法 §4.5 方法形状: generate/run/adapt 的参数与产出符合三段边界。"""
    import inspect

    assert list(inspect.signature(_GeneratorOnly.generate).parameters) == [
        "self", "artifact", "resources", "config",
    ]
    assert list(inspect.signature(_RuntimeOnly.run).parameters) == ["self", "bundle"]
    assert list(inspect.signature(_AdapterOnly.adapt).parameters) == [
        "self", "bundle", "receipt", "outputs",
    ]
    assert isinstance(_GeneratorOnly(), GeneratorProvider)
    assert isinstance(_RuntimeOnly(), SolverRuntime)
    assert isinstance(_AdapterOnly(), ResultAdapter)


def test_unavailable_error_semantics() -> None:
    """unavailable 错误语义: RuntimeError, 默认 reason, 调用一律明确失败。"""
    err = ComputationUnavailableError()
    assert isinstance(err, RuntimeError)
    assert err.reason == "no-provider"
    assert "0.8" in str(err)
    gen, runtime, adapter = _GeneratorOnly(), _RuntimeOnly(), _AdapterOnly()
    config = CalculationConfig(seed=42)
    with pytest.raises(ComputationUnavailableError):
        gen.generate(_artifact(), {}, config)
    with pytest.raises(ComputationUnavailableError):
        runtime.run(_bundle())
    receipt = ExecutionReceipt(
        bundle_id="bundle-1", generator_ref="g", solver_ref="s", status="failed",
    )
    with pytest.raises(ComputationUnavailableError):
        adapter.adapt(_bundle(), receipt, {})


def test_bundles_are_deeply_immutable_value_objects() -> None:
    """Bundle/回执/结果/配置深层不可变(顶层赋值与嵌套改写一律失败)。"""
    bundle = _bundle()
    result = ComputeResult(
        bundle_id="bundle-1", status="succeeded",
        business_outcome="normal_completion", outputs={"k": {"nested": [1]}},
    )
    config = CalculationConfig(seed=7, options={"gap": 0.01, "tags": ["a"]})
    receipt = ExecutionReceipt(
        bundle_id="bundle-1", generator_ref="g", solver_ref="s",
        status="succeeded", exit_code=0, detail={"stdout": "ok"},
    )
    for obj in (bundle, result, config, receipt):
        assert dataclasses.is_dataclass(obj)
    with pytest.raises(dataclasses.FrozenInstanceError):
        bundle.bundle_id = "other"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = "failed"  # type: ignore[misc]
    # 嵌套容器只读(映射不可写, 序列不可改)
    with pytest.raises(TypeError):
        bundle.inputs["x"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        bundle.command["executor"] = "sh -c"  # type: ignore[index]
    with pytest.raises(TypeError):
        result.outputs["k"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        config.options["gap"] = 0.5  # type: ignore[index]
    with pytest.raises(TypeError):
        receipt.detail["stdout"] = "x"  # type: ignore[index]
    assert isinstance(bundle.inputs, MappingProxyType)
    assert isinstance(result.outputs, MappingProxyType)


def test_carriers_ignore_later_caller_mutation() -> None:
    """构造后调用方改写原始 dict 不影响已冻结载体; to_dict 返回独立副本。"""
    raw_inputs = {"f": {"deep": [1, 2]}}
    bundle = SolverBundle(
        bundle_id="b", generator_ref="g", solver_ref="s", config_id="c",
        inputs=raw_inputs,
        command={"arguments": ["a"]},
        declared_outputs=["output/solution.json"],
        result_adapter_ref="r",
    )
    raw_inputs["f"] = {"deep": [9]}
    assert bundle.to_dict()["inputs"] == {"f": {"deep": [1, 2]}}
    assert isinstance(bundle.declared_outputs, tuple)
    dumped = bundle.to_dict()
    dumped["inputs"]["f"] = {}
    assert bundle.to_dict()["inputs"] == {"f": {"deep": [1, 2]}}


def test_bundle_rejects_shell_command_and_escaping_paths() -> None:
    """Bundle 只接受结构化命令与 Bundle 内相对声明输出。"""
    with pytest.raises(TypeError):
        SolverBundle(
            bundle_id="b", generator_ref="g", solver_ref="s", config_id="c",
            inputs={}, command="solver input.mps",  # type: ignore[arg-type]
            declared_outputs=("output/solution.json",), result_adapter_ref="r",
        )
    with pytest.raises(ValueError, match="越界"):
        SolverBundle(
            bundle_id="b", generator_ref="g", solver_ref="s", config_id="c",
            inputs={}, command={}, declared_outputs=("../evil.json",),
            result_adapter_ref="r",
        )


def test_config_schema_word_checked() -> None:
    """公开 CalculationConfig 字头校验: 非法 schema/版本即拒绝。

    快照默认修补路径已删除(``from_snapshot`` 不再存在): 运行期只做
    表示映射, 直接构造, 缺字段即映射失败, 不补默认值。
    """
    assert not hasattr(CalculationConfig, "from_snapshot")
    with pytest.raises(ValueError, match="schema"):
        CalculationConfig(schema="ies.other", options={})
    with pytest.raises(ValueError, match="版本"):
        CalculationConfig(schema_version="9.9.9", options={})
    good = CalculationConfig(seed=42, options={"gap_rel": 0.01})
    assert good.seed == 42
    assert good.to_dict()["options"] == {"gap_rel": 0.01}
    with pytest.raises(TypeError):
        CalculationConfig(seed="42", options={})  # type: ignore[arg-type]


def test_provider_directory_is_empty() -> None:
    """provider 目录为空(0.8 未实现, 明确无可用 provider), 且返回副本。"""
    assert available_providers() == {}
    first = available_providers()
    first["x"] = object()
    assert available_providers() == {}


def test_registry_facade_from_computation() -> None:
    """computation 门面重导出注册表: 默认算法可取, 未知算法抛 NotFoundError。"""
    spec = get_algorithm(DEFAULT_ALGORITHM)
    assert isinstance(spec, AlgorithmSpec)
    assert spec.algo_id == DEFAULT_ALGORITHM
    assert spec is get_algorithm("default")
    assert any(s.algo_id == DEFAULT_ALGORITHM for s in list_algorithms())
    with pytest.raises(NotFoundError):
        get_algorithm("ies.algo.not_registered")


def test_result_and_bundle_schema_words() -> None:
    """结果/Bundle 字头为公开 schema, 非法字头即拒绝。"""
    assert _bundle().schema == SOLVER_BUNDLE_SCHEMA
    assert _bundle().schema_version == SOLVER_BUNDLE_VERSION
    result = ComputeResult(
        bundle_id="b", status="succeeded", business_outcome="normal_completion",
    )
    assert result.schema == COMPUTE_RESULT_SCHEMA
    assert result.schema_version == COMPUTE_RESULT_VERSION
    with pytest.raises(ValueError, match="schema"):
        ComputeResult(
            schema="ies.other", bundle_id="b", status="s",
            business_outcome="normal_completion",
        )


# ---------------------------------------------------------------------------
# 键与组合归属 computation(公开契约)
# ---------------------------------------------------------------------------


def test_provider_keys_owned_by_computation_facade() -> None:
    """provider 稳定键只由 computation 门面公开, 取值稳定。"""
    import iesplan.computation as computation

    assert GENERATOR_PROVIDER_KEY == "generator"
    assert SOLVER_RUNTIME_KEY == "solver_runtime"
    assert RESULT_ADAPTER_KEY == "result_adapter"
    for name in (
        "GENERATOR_PROVIDER_KEY",
        "SOLVER_RUNTIME_KEY",
        "RESULT_ADAPTER_KEY",
        "ComputeCapabilities",
        "ComputeInputs",
        "ComputeResources",
        "build_compute_inputs",
        "resolve_capabilities",
    ):
        assert name in computation.__all__, name


def test_application_knows_no_provider_keys() -> None:
    """application 不知键: 无键常量、无组合解析器、无重建器(只编排)。"""
    import iesplan.application.worker as worker_app
    import iesplan.application.worker.compute_cases as compute_gateway

    for name in (
        "GENERATOR_PROVIDER_KEY",
        "SOLVER_RUNTIME_KEY",
        "RESULT_ADAPTER_KEY",
        "_resolve_providers",
        "_rebuild_artifact",
    ):
        assert not hasattr(compute_gateway, name), name
        assert not hasattr(worker_app, name), name
    # 类型化输入契约只归 computation 门面(阶段网关内部调用, 不经本包复出)
    for name in ("ComputeInputs", "build_compute_inputs"):
        assert not hasattr(worker_app, name), name
    import inspect

    source = inspect.getsource(compute_gateway)
    for literal in ('"generator"', '"solver_runtime"', '"result_adapter"'):
        assert literal not in source, literal
    assert worker_app.run_compute_stage is compute_gateway.run_compute_stage


# ---------------------------------------------------------------------------
# resolve_capabilities: 不透明目录 → 类型化能力
# ---------------------------------------------------------------------------


class _AvailableGenerator:
    """可用的生成器替身(透传最小 Bundle, 无算法逻辑)。"""

    @property
    def ref(self) -> str:
        return "test.generator@0.0.0"

    def available(self) -> bool:
        return True

    def generate(self, artifact, resources, config) -> SolverBundle:
        return _bundle()


class _AvailableRuntime:
    """可用的求解运行时替身(成功回执与空原始输出, 无算法逻辑)。"""

    @property
    def ref(self) -> str:
        return "test.solver@0.0.0"

    def available(self) -> bool:
        return True

    def run(self, bundle: SolverBundle):
        receipt = ExecutionReceipt(
            bundle_id=bundle.bundle_id, generator_ref=bundle.generator_ref,
            solver_ref=bundle.solver_ref, status="succeeded", exit_code=0,
        )
        return receipt, {}


class _AvailableAdapter:
    """可用的结果适配器替身(固定映射正常完成, 无算法逻辑)。"""

    def available(self) -> bool:
        return True

    def adapt(self, bundle, receipt, outputs) -> ComputeResult:
        return ComputeResult(
            bundle_id=bundle.bundle_id, status="succeeded",
            business_outcome="normal_completion", outputs={},
        )


def _full_directory() -> dict:
    return {
        GENERATOR_PROVIDER_KEY: _AvailableGenerator(),
        SOLVER_RUNTIME_KEY: _AvailableRuntime(),
        RESULT_ADAPTER_KEY: _AvailableAdapter(),
    }


def test_resolve_capabilities_empty_or_missing_is_no_provider() -> None:
    """目录缺失/为空/缺键/形状不符一律 no-provider(不枚举畸形目录)。"""
    for directory in (None, {}, {GENERATOR_PROVIDER_KEY: _AvailableGenerator()}):
        with pytest.raises(ComputationUnavailableError) as exc_info:
            resolve_capabilities(directory)
        assert exc_info.value.reason == "no-provider"

    class _OnlyGenerateMethod:
        """只有 generate 方法名、非协议形状的对象(方法名猜测必须拒绝)。"""

        def generate(self, artifact, resources, config) -> SolverBundle:
            raise AssertionError("形状不符的能力不得被调用")

    directory = _full_directory()
    directory[GENERATOR_PROVIDER_KEY] = _OnlyGenerateMethod()
    with pytest.raises(ComputationUnavailableError) as exc_info:
        resolve_capabilities(directory)
    assert exc_info.value.reason == "no-provider"


def test_resolve_capabilities_self_reported_unavailable_is_deferred() -> None:
    """能力自报不可用即 deferred-0.8(明确延期, 不伪造成功)。"""
    directory = _full_directory()
    directory[SOLVER_RUNTIME_KEY] = _RuntimeOnly()
    with pytest.raises(ComputationUnavailableError) as exc_info:
        resolve_capabilities(directory)
    assert exc_info.value.reason == "deferred-0.8"


def test_resolve_capabilities_returns_deeply_immutable_typed_caps() -> None:
    """类型化能力深层不可变: 字段协议类型齐备、同一对象、不可重绑。"""
    directory = _full_directory()
    caps = resolve_capabilities(directory)
    assert isinstance(caps, ComputeCapabilities)
    assert dataclasses.is_dataclass(caps)
    assert isinstance(caps.generator, GeneratorProvider)
    assert isinstance(caps.runtime, SolverRuntime)
    assert isinstance(caps.adapter, ResultAdapter)
    assert caps.generator is directory[GENERATOR_PROVIDER_KEY]
    assert caps.runtime is directory[SOLVER_RUNTIME_KEY]
    assert caps.adapter is directory[RESULT_ADAPTER_KEY]
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.generator = _AvailableGenerator()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ComputeResources: 快照真实固定内容的不可变契约
# ---------------------------------------------------------------------------


def test_compute_resources_are_readonly_mapping_of_fixed_content() -> None:
    """固定资源为只读 Mapping(两键), 嵌套容差不可写, 改写原始 dict 不影响。"""
    raw_tol = {"rel": 1e-6, "nested": {"abs": 1e-9}}
    resources = ComputeResources(dataset_version_ids=[7], tolerances=raw_tol)
    assert isinstance(resources, Mapping)
    assert set(resources) == {"dataset_version_ids", "tolerances"}
    assert len(resources) == 2
    assert resources["dataset_version_ids"] == (7,)
    assert resources.dataset_version_ids == (7,)
    raw_tol["rel"] = 0.5
    assert resources["tolerances"] == {"rel": 1e-6, "nested": {"abs": 1e-9}}
    with pytest.raises(TypeError):
        resources["tolerances"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        resources["dataset_version_ids"] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        resources.tolerances["rel"] = 0.5  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        resources.tolerances = {}  # type: ignore[misc]
    dumped = resources.to_dict()
    assert dumped == {
        "dataset_version_ids": [7],
        "tolerances": {"rel": 1e-6, "nested": {"abs": 1e-9}},
    }
    dumped["tolerances"]["rel"] = 0.0
    assert resources.to_dict()["tolerances"]["rel"] == 1e-6


# ---------------------------------------------------------------------------
# build_compute_inputs: 已签发事实的表示映射
# ---------------------------------------------------------------------------


def test_build_inputs_maps_issued_snapshot_structurally() -> None:
    """合法快照直装类型化输入: 产物/资源/配置值与封存一致。"""
    inputs = build_compute_inputs(_snapshot())
    assert isinstance(inputs, ComputeInputs)
    assert inputs.artifact.canonical_text.startswith('{"schema":"ies.assembly"')
    assert isinstance(inputs.resources, ComputeResources)
    assert inputs.resources.dataset_version_ids == (7,)
    assert dict(inputs.resources.tolerances) == {"rel": 1e-6}
    assert inputs.config.seed == 42
    assert inputs.config.to_dict()["options"] == {
        "gap_rel": 0.01, "tolerances": {"rel": 1e-6},
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        inputs.config = CalculationConfig(seed=1)  # type: ignore[misc]


def test_build_inputs_absent_tolerances_means_absent_key() -> None:
    """快照未固定容差时选项不出现该键(缺席语义, 非默认值)。"""
    inputs = build_compute_inputs(_snapshot(tolerances=None))
    assert "tolerances" not in inputs.config.to_dict()["options"]
    assert inputs.resources.tolerances == {}


def test_build_inputs_missing_params_is_mapping_failure_not_default() -> None:
    """params 缺失即表示映射失败(旧静默空映射默认已删除)。"""
    snapshot = _snapshot()
    raw = dict(snapshot.calc_config_snapshot)
    del raw["params"]
    broken = dataclasses.replace(snapshot, calc_config_snapshot=raw)
    with pytest.raises(ValueError, match="表示映射失败"):
        build_compute_inputs(broken)
    with pytest.raises(ValueError, match="表示映射失败"):
        build_compute_inputs(dataclasses.replace(snapshot, calc_config_snapshot=[]))
    with pytest.raises(ValueError, match="表示映射失败"):
        build_compute_inputs(
            dataclasses.replace(snapshot, calc_config_snapshot={"params": [1]})
        )


def test_build_inputs_missing_receipt_fields_is_mapping_failure() -> None:
    """回执缺字段或形状非法即表示映射失败(不重跑装配校验)。"""
    snapshot = _snapshot()
    with pytest.raises(ValueError, match="缺装配回执"):
        build_compute_inputs(dataclasses.replace(snapshot, assembly_receipt=[]))
    stored = dict(snapshot.assembly_receipt)
    del stored["validator"]
    with pytest.raises(ValueError, match="表示映射失败"):
        build_compute_inputs(dataclasses.replace(snapshot, assembly_receipt=stored))
    stored = dict(snapshot.assembly_receipt)
    stored["diagnostics"] = {}
    with pytest.raises(ValueError, match="表示映射失败"):
        build_compute_inputs(dataclasses.replace(snapshot, assembly_receipt=stored))


def test_build_inputs_does_not_rejudge_blocking_receipt() -> None:
    """封存回执含阻断诊断仍直装(密封: 签发即事实, 运行期不复判阻断)。"""
    diag = make_diag(DATA_TS_DUP, severity=SEVERITY_WARNING, blocking=True).to_dict()
    stored = ValidationReceipt().to_dict()
    stored["diagnostics"] = [diag]
    inputs = build_compute_inputs(_snapshot(receipt=stored))
    assert inputs.artifact.receipt.diagnostics[0].blocking is True
    assert inputs.artifact.receipt.diagnostics[0].code == DATA_TS_DUP


def test_build_inputs_rejects_unsigned_snapshot() -> None:
    """快照缺失/未签发装配输入一律 ValueError, 不产生部分产物。"""
    with pytest.raises(ValueError, match="缺快照"):
        build_compute_inputs(None)
    snapshot = _snapshot()
    with pytest.raises(ValueError, match="未签发"):
        build_compute_inputs(
            dataclasses.replace(
                snapshot, canonical_assembly_text="  ", assembly_receipt=None
            )
        )


# ---------------------------------------------------------------------------
# CalcSnapshotRecord: 长期快照字段深度不可变
# ---------------------------------------------------------------------------


def test_snapshot_record_fields_are_deeply_immutable() -> None:
    """快照封存字段构造时冻结(映射只读、序列元组化), 不共享可变 dict。"""
    config = {"params": {"gap_rel": 0.01}, "tags": ["a"]}
    receipt = ValidationReceipt(
        dependencies={"d": [1]}, resources={"r": {"x": 1}},
    ).to_dict()
    record = CalcSnapshotRecord(
        id=1,
        project_version_id=2,
        dataset_version_ids=[7, 8],
        calc_config_snapshot=config,
        random_seed=42,
        extension_versions={"ext": {"v": 1}},
        tolerances={"rel": 1e-6},
        canonical_assembly_text="t",
        assembly_receipt=receipt,
    )
    assert record.dataset_version_ids == (7, 8)
    for field_value in (
        record.calc_config_snapshot,
        record.extension_versions,
        record.tolerances,
        record.assembly_receipt,
    ):
        assert isinstance(field_value, MappingProxyType)
    with pytest.raises(TypeError):
        record.calc_config_snapshot["params"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        record.tolerances["rel"] = 0.0  # type: ignore[index]
    with pytest.raises(TypeError):
        record.assembly_receipt["dependencies"] = {}  # type: ignore[index]
    # 调用方改写原始 dict 不污染已冻结记录(无跨模块共享可变 dict)
    config["params"] = {}
    receipt["dependencies"] = {}
    assert record.calc_config_snapshot["params"] == {"gap_rel": 0.01}
    assert record.assembly_receipt["dependencies"] == {"d": (1,)}
    # 冻结序列不可变, 但与 JSON 列表结构相等(快照去重逐项 == 不受影响)
    assert isinstance(record.calc_config_snapshot["tags"], tuple)
    assert record.calc_config_snapshot["tags"] == ["a"]
    assert ["a"] == record.calc_config_snapshot["tags"]
    assert record.assembly_receipt["dependencies"] == {"d": [1]}
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.random_seed = 1  # type: ignore[misc]
