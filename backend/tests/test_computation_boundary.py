"""computation 边界契约测试。

只覆盖真实边界, 不涉及任何 0.8 算法实现:

- Protocol 形状: 三个边界协议为运行时可检查的 Protocol, 且拒绝
  不完整实现的结构判定; 三协议用各自独立的最小替身验证(不用一个
  同时实现三协议的假对象证明错误形状);
- 宪法 §4.5 方法形状: generate 消费装配产物/已固定资源/公开配置产出
  Bundle; run 消费 Bundle 产出回执与原始输出; adapt 消费三件套产出
  统一结果;
- unavailable 错误语义: ``ComputationUnavailableError`` 为
  ``RuntimeError``, 默认 reason 为 ``"no-provider"``;
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
    SOLVER_BUNDLE_SCHEMA,
    SOLVER_BUNDLE_VERSION,
    AlgorithmSpec,
    CalculationConfig,
    ComputationUnavailableError,
    ComputeResult,
    ExecutionReceipt,
    GeneratorProvider,
    ResultAdapter,
    SolverBundle,
    SolverRuntime,
    get_algorithm,
    list_algorithms,
)
from iesplan.core.errors import NotFoundError


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

    运行期只做表示映射, 直接构造, 缺字段即映射失败, 不补默认值。
    """
    with pytest.raises(ValueError, match="schema"):
        CalculationConfig(schema="ies.other", options={})
    with pytest.raises(ValueError, match="版本"):
        CalculationConfig(schema_version="9.9.9", options={})
    good = CalculationConfig(seed=42, options={"gap_rel": 0.01})
    assert good.seed == 42
    assert good.to_dict()["options"] == {"gap_rel": 0.01}
    with pytest.raises(TypeError):
        CalculationConfig(seed="42", options={})  # type: ignore[arg-type]


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
