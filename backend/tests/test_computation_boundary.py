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
- 计算配置: ``CalculationConfig`` 固定 generator/solver 精确引用、种子、
  算法选项(含精度)与输出请求；非法引用形态即拒绝；
- 注册表门面: ``iesplan.computation`` 选择性重导出注册表符号,
  ``get_algorithm`` / ``list_algorithms`` 行为与搬迁前一致。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from types import MappingProxyType

import pytest
from worker_testkit import fixture_artifact

from iesplan.assembly import ValidatedAssemblyArtifact
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


@pytest.fixture(scope="module", autouse=True)
def _devices_registry():
    """机械产物依赖锁取设备公开门面实时版本: 初始化设备注册表。"""
    from iesplan.devices import init_registry

    init_registry()
    yield


def _artifact() -> ValidatedAssemblyArtifact:
    """计算边界机械产物: 非空且内部一致的 2.0 二件套(不跑装配校验)。"""
    return fixture_artifact()


_GENERATOR = "acme.generator@1.0.0"
_SOLVER = "ies.solver.highs@1.7.2"


def _config(**kwargs) -> CalculationConfig:
    """最小合法计算配置(默认固定 generator/solver 精确引用)。"""
    kwargs.setdefault("generator", _GENERATOR)
    kwargs.setdefault("solver", _SOLVER)
    return CalculationConfig(**kwargs)


def _bundle() -> SolverBundle:
    return SolverBundle(
        bundle={
            "generator": "acme.generator@1.0.0",
            "solver": "ies.solver.highs@1.7.2",
        },
        inputs=[{"path": "input/problem.mps", "media_type": "application/mps"}],
        command={
            "executor": "ies.executor.local_process@1.0.0",
            "executable": "ies.solver.highs@1.7.2",
            "arguments": ["input/problem.mps"],
        },
        outputs=[{"path": "output/solution.json", "media_type": "application/json", "required": True}],
        result_adapter="acme.result-adapter@1.0.0",
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
    config = _config(seed=42)
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
    config = _config(seed=7, options={"gap": 0.01, "tags": ["a"]}, outputs=["out"])
    assert config.outputs == ("out",)
    receipt = ExecutionReceipt(
        bundle_id="bundle-1", generator_ref="g", solver_ref="s",
        status="succeeded", exit_code=0, detail={"stdout": "ok"},
    )
    for obj in (bundle, result, config, receipt):
        assert dataclasses.is_dataclass(obj)
    with pytest.raises(dataclasses.FrozenInstanceError):
        bundle.result_adapter = "other"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = "failed"  # type: ignore[misc]
    # 嵌套容器只读(映射不可写, 序列不可改, 条目不可写)
    with pytest.raises(TypeError):
        bundle.inputs[0] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        bundle.inputs[0]["path"] = "x"  # type: ignore[index]
    with pytest.raises(TypeError):
        bundle.command["executor"] = "sh -c"  # type: ignore[index]
    with pytest.raises(TypeError):
        result.outputs["k"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        config.options["gap"] = 0.5  # type: ignore[index]
    with pytest.raises(TypeError):
        receipt.detail["stdout"] = "x"  # type: ignore[index]
    assert isinstance(bundle.inputs, tuple)
    assert isinstance(bundle.bundle, MappingProxyType)
    assert isinstance(result.outputs, MappingProxyType)


def test_carriers_ignore_later_caller_mutation() -> None:
    """构造后调用方改写原始结构不影响已冻结载体; to_dict 返回独立副本。"""
    raw_entries = [{"path": "input/a.mps", "media_type": "application/mps"}]
    bundle = SolverBundle(
        bundle={"generator": _GENERATOR, "solver": _SOLVER},
        inputs=raw_entries,
        command={"executor": "e@1.0.0", "executable": "s@1.0.0", "arguments": ["a"]},
        outputs=[{"path": "output/s.json", "media_type": "application/json", "required": True}],
        result_adapter="r@1.0.0",
    )
    raw_entries[0]["path"] = "input/evil.mps"
    raw_entries.append({"path": "input/b.mps", "media_type": "application/mps"})
    assert bundle.to_dict()["inputs"] == [
        {"path": "input/a.mps", "media_type": "application/mps"}
    ]
    assert isinstance(bundle.outputs, tuple)
    dumped = bundle.to_dict()
    dumped["inputs"][0]["path"] = "x"
    dumped["outputs"].clear()
    assert bundle.to_dict()["inputs"] == [
        {"path": "input/a.mps", "media_type": "application/mps"}
    ]
    assert len(bundle.to_dict()["outputs"]) == 1


def test_bundle_rejects_shell_command_and_escaping_paths() -> None:
    """Bundle 只接受结构化命令与 Bundle 内相对路径。"""
    with pytest.raises(TypeError):
        SolverBundle(
            bundle={"generator": _GENERATOR, "solver": _SOLVER},
            inputs=[{"path": "input/a.mps", "media_type": "application/mps"}],
            command="solver input.mps",  # type: ignore[arg-type]
            outputs=[{"path": "output/s.json", "media_type": "application/json", "required": True}],
            result_adapter="r@1.0.0",
        )
    with pytest.raises(ValueError, match="越界"):
        SolverBundle(
            bundle={"generator": _GENERATOR, "solver": _SOLVER},
            inputs=[{"path": "input/a.mps", "media_type": "application/mps"}],
            command={"executor": "e@1.0.0", "executable": "s@1.0.0", "arguments": ["a"]},
            outputs=[{"path": "../evil.json", "media_type": "application/json", "required": True}],
            result_adapter="r@1.0.0",
        )


def test_config_schema_word_checked() -> None:
    """公开 CalculationConfig 字头与选择校验: 非法字头/引用即拒绝。

    运行期只做表示映射, 直接构造, 缺字段即映射失败, 不补默认值。
    """
    with pytest.raises(ValueError, match="schema"):
        CalculationConfig(schema="ies.other", generator=_GENERATOR, solver=_SOLVER)
    with pytest.raises(ValueError, match="版本"):
        CalculationConfig(schema_version="9.9.9", generator=_GENERATOR, solver=_SOLVER)
    good = _config(seed=42, options={"gap_rel": 0.01}, outputs=["solution"])
    assert good.seed == 42
    assert good.to_dict() == {
        "schema": CALCULATION_CONFIG_SCHEMA,
        "schema_version": CALCULATION_CONFIG_VERSION,
        "generator": _GENERATOR,
        "solver": _SOLVER,
        "seed": 42,
        "options": {"gap_rel": 0.01},
        "outputs": ["solution"],
    }
    with pytest.raises(TypeError):
        _config(seed="42")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="generator"):
        CalculationConfig(solver=_SOLVER)
    with pytest.raises(ValueError, match="generator"):
        CalculationConfig(generator="acme.generator", solver=_SOLVER)
    with pytest.raises(ValueError, match="solver"):
        CalculationConfig(generator=_GENERATOR, solver="ies.solver.highs@1.7")
    with pytest.raises(ValueError, match="输出请求"):
        CalculationConfig(generator=_GENERATOR, solver=_SOLVER, outputs=[""])


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
