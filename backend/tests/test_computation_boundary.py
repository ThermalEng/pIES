"""computation 边界契约测试。

只覆盖真实边界:

- Protocol 形状: 三个边界协议为运行时可检查的 Protocol, 且拒绝
  不完整实现的结构判定; 三协议用各自独立的最小替身验证(不用一个
  同时实现三协议的假对象证明错误形状);
- 宪法 §4.5 方法形状: generate 消费装配产物/设备数学贡献/物化序列/
  有效财务快照/公开配置五项直接输入产出完整 Bundle 聚合(manifest +
  文件字节); execute 消费结构化命令与运行目录产出最小过程结果;
  adapt 消费三件套产出统一结果; manifest 载体为
  ``SolverBundleManifest``; 回执载体为 ``ExecutionReceipt``;
- unavailable 错误语义: ``ComputationUnavailableError`` 为
  ``RuntimeError``, 默认 reason 为 ``"no-provider"``, 默认消息为
  稳定的“计算能力不可用”;
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
    EXECUTION_RECEIPT_SCHEMA,
    EXECUTION_RECEIPT_VERSION,
    SOLVER_BUNDLE_SCHEMA,
    SOLVER_BUNDLE_VERSION,
    AlgorithmSpec,
    CalculationConfig,
    ComputationUnavailableError,
    ComputeResult,
    ExecutionReceipt,
    ExecutorDescriptor,
    ExecutorProvider,
    GeneratorDescriptor,
    GeneratorProvider,
    MaterializedSeries,
    ProcessOutcome,
    RECEIPT_STATUSES,
    ResultAdapter,
    SolverBundle,
    SolverBundleManifest,
    get_algorithm,
    list_algorithms,
)
from iesplan.core.errors import NotFoundError
from iesplan.finance import EffectiveFinanceConfig, FinanceProfile, merge_effective
from iesplan.modeling import DeviceMathContribution


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
    """最小合法计算配置(默认固定 mode/引用/精度/时间限制/输出)。"""
    kwargs.setdefault("mode", "planning")
    kwargs.setdefault("generator", _GENERATOR)
    kwargs.setdefault("solver", _SOLVER)
    kwargs.setdefault("precision", "medium")
    kwargs.setdefault("time_limit_seconds", 600)
    kwargs.setdefault("outputs", ["solution"])
    return CalculationConfig(**kwargs)


def _manifest() -> SolverBundleManifest:
    return SolverBundleManifest(
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


_FINANCE_PAYLOAD = {
    "schema": "ies.finance-profile",
    "schema_version": "1.0.0",
    "profile": {
        "id": "cn-north-demo",
        "region": "CN-North",
        "currency": "CNY",
        "base_year": 2025,
        "price_basis": "tax_inclusive",
        "cost_method": "fixed_plus_linear",
    },
    "finance_types": {
        "pv_system": {
            "upfront_capex": {"fixed": {"value": "0", "unit": "CNY"}},
        }
    },
    "energy_prices": {
        "grid_import": {
            "carrier": "electricity",
            "direction": "purchase",
            "kind": "constant",
            "value": {"value": "0.7", "unit": "CNY/kWh"},
        }
    },
}


def _finance() -> EffectiveFinanceConfig:
    """最小不可变有效财务快照(与装配引用同一合并语义)。"""
    return merge_effective(FinanceProfile.from_dict(_FINANCE_PAYLOAD), None)


class _GeneratorOnly:
    """仅实现 GeneratorProvider 的最小替身(无任何算法逻辑)。"""

    @property
    def descriptor(self):
        return GeneratorDescriptor(
            generator_id="acme.generator",
            version="1.0.0",
            options_schema={
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            assembly_schema_version="1.0",
            modes=("planning",),
            precisions=("medium",),
            solvers=("ies.solver.highs@1.7.2",),
            result_adapter="acme.result-adapter@1.0.0",
            resource_limits={},
            unit_system="SI",
            seed_semantics="test-only",
        )

    @property
    def ref(self) -> str:
        return "acme.generator@1.0.0"

    def available(self) -> bool:
        return False

    def generate(
        self,
        artifact: ValidatedAssemblyArtifact,
        models: Mapping[str, DeviceMathContribution],
        series: tuple[MaterializedSeries, ...],
        finance: EffectiveFinanceConfig,
        config: CalculationConfig,
    ) -> SolverBundle:
        raise ComputationUnavailableError(reason="no-provider")


def _receipt(**kwargs) -> ExecutionReceipt:
    """最小合法回执(默认补齐身份、合法引用与已执行事实字段)。"""
    kwargs.setdefault("receipt_id", "receipt-1")
    kwargs.setdefault("bundle_id", "bundle-1")
    kwargs.setdefault("assembly_id", "assembly-1")
    kwargs.setdefault("calculation_config_id", "config-1")
    kwargs.setdefault("attempt_id", "attempt-1")
    kwargs.setdefault("generator_ref", "acme.generator@1.0.0")
    kwargs.setdefault("solver_ref", "ies.solver.highs@1.7.2")
    kwargs.setdefault("executor_ref", "t.executor@1.0.0")
    kwargs.setdefault("executable_ref", "t.exe@1.0.0")
    kwargs.setdefault("result_adapter_ref", "acme.result-adapter@1.0.0")
    kwargs.setdefault("status", "succeeded")
    kwargs.setdefault("exit_code", 0)
    kwargs.setdefault("termination_reason", "exited")
    kwargs.setdefault("started_at", "2026-09-24T00:00:00Z")
    kwargs.setdefault("ended_at", "2026-09-24T00:00:01Z")
    return ExecutionReceipt(**kwargs)


class _ExecutorOnly:
    """仅实现 ExecutorProvider 的最小替身(无任何算法逻辑)。"""

    @property
    def descriptor(self) -> ExecutorDescriptor:
        return ExecutorDescriptor(
            executor_id="t.executor",
            version="1.0.0",
            executables=("t.exe@1.0.0",),
        )

    @property
    def ref(self) -> str:
        return "t.executor@1.0.0"

    def available(self) -> bool:
        return False

    def execute(
        self, command: Mapping[str, object], workdir: str, *, stop=None
    ) -> ProcessOutcome:
        raise ComputationUnavailableError(reason="no-executor")


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
    gen = _GeneratorOnly()
    exe = _ExecutorOnly()
    adapter = _AdapterOnly()
    assert isinstance(gen, GeneratorProvider)
    assert not isinstance(gen, ExecutorProvider)
    assert not isinstance(gen, ResultAdapter)
    assert isinstance(exe, ExecutorProvider)
    assert not isinstance(exe, GeneratorProvider)
    assert not isinstance(exe, ResultAdapter)
    assert exe.descriptor.ref == exe.ref
    assert isinstance(adapter, ResultAdapter)
    assert not isinstance(adapter, GeneratorProvider)
    assert not isinstance(adapter, ExecutorProvider)
    assert not isinstance(object(), GeneratorProvider)
    assert not isinstance(object(), ExecutorProvider)
    assert not isinstance(object(), ResultAdapter)


def test_protocol_methods_match_constitution_boundaries() -> None:
    """宪法 §4.5 方法形状: generate/execute/adapt 的参数与产出符合边界。"""
    import inspect

    assert list(inspect.signature(GeneratorProvider.generate).parameters) == [
        "self", "artifact", "models", "series", "finance", "config",
    ]
    assert list(inspect.signature(ExecutorProvider.execute).parameters) == [
        "self", "command", "workdir", "stop",
    ]
    assert list(inspect.signature(_AdapterOnly.adapt).parameters) == [
        "self", "bundle", "receipt", "outputs",
    ]
    assert isinstance(_GeneratorOnly(), GeneratorProvider)
    assert isinstance(_ExecutorOnly(), ExecutorProvider)
    assert isinstance(_AdapterOnly(), ResultAdapter)
    assert _GeneratorOnly().descriptor.ref == _GeneratorOnly().ref


def test_unavailable_error_semantics() -> None:
    """unavailable 错误语义: RuntimeError, 默认 reason/消息, 调用一律明确失败。"""
    err = ComputationUnavailableError()
    assert isinstance(err, RuntimeError)
    assert err.reason == "no-provider"
    assert str(err) == "计算能力不可用"
    gen, adapter = _GeneratorOnly(), _AdapterOnly()
    config = _config(seed=42)
    with pytest.raises(ComputationUnavailableError):
        gen.generate(_artifact(), {}, (), _finance(), config)
    receipt = _receipt(status="process_failed")
    with pytest.raises(ComputationUnavailableError):
        adapter.adapt(_manifest(), receipt, {})


def test_bundles_are_deeply_immutable_value_objects() -> None:
    """Bundle/回执/结果/配置深层不可变(顶层赋值与嵌套改写一律失败)。"""
    manifest = _manifest()
    result = ComputeResult(
        bundle_id="bundle-1", status="succeeded",
        business_outcome="normal_completion", outputs={"k": {"nested": [1]}},
    )
    config = _config(seed=7, options={"gap": 0.01, "tags": ["a"]}, outputs=["out"])
    assert config.outputs == ("out",)
    receipt = _receipt(stdout="ok")
    for obj in (manifest, result, config, receipt):
        assert dataclasses.is_dataclass(obj)
    with pytest.raises(dataclasses.FrozenInstanceError):
        manifest.result_adapter = "other"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = "failed"  # type: ignore[misc]
    # 嵌套容器只读(映射不可写, 序列不可改, 条目不可写)
    with pytest.raises(TypeError):
        manifest.inputs[0] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        manifest.inputs[0]["path"] = "x"  # type: ignore[index]
    with pytest.raises(TypeError):
        manifest.command["executor"] = "sh -c"  # type: ignore[index]
    with pytest.raises(TypeError):
        result.outputs["k"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        config.options["gap"] = 0.5  # type: ignore[index]
    with pytest.raises(TypeError):
        receipt.resource_usage["k"] = 1  # type: ignore[index]
    assert isinstance(manifest.inputs, tuple)
    assert isinstance(manifest.bundle, MappingProxyType)
    assert isinstance(result.outputs, MappingProxyType)


def test_carriers_ignore_later_caller_mutation() -> None:
    """构造后调用方改写原始结构不影响已冻结载体; to_dict 返回独立副本。"""
    raw_entries = [{"path": "input/a.mps", "media_type": "application/mps"}]
    manifest = SolverBundleManifest(
        bundle={"generator": _GENERATOR, "solver": _SOLVER},
        inputs=raw_entries,
        command={"executor": "e@1.0.0", "executable": "s@1.0.0", "arguments": ["a"]},
        outputs=[{"path": "output/s.json", "media_type": "application/json", "required": True}],
        result_adapter="r@1.0.0",
    )
    raw_entries[0]["path"] = "input/evil.mps"
    raw_entries.append({"path": "input/b.mps", "media_type": "application/mps"})
    assert manifest.to_dict()["inputs"] == [
        {"path": "input/a.mps", "media_type": "application/mps"}
    ]
    assert isinstance(manifest.outputs, tuple)
    dumped = manifest.to_dict()
    dumped["inputs"][0]["path"] = "x"
    dumped["outputs"].clear()
    assert manifest.to_dict()["inputs"] == [
        {"path": "input/a.mps", "media_type": "application/mps"}
    ]
    assert len(manifest.to_dict()["outputs"]) == 1


def test_bundle_rejects_shell_command_and_escaping_paths() -> None:
    """Bundle 只接受结构化命令与 Bundle 内相对路径。"""
    with pytest.raises(TypeError):
        SolverBundleManifest(
            bundle={"generator": _GENERATOR, "solver": _SOLVER},
            inputs=[{"path": "input/a.mps", "media_type": "application/mps"}],
            command="solver input.mps",  # type: ignore[arg-type]
            outputs=[{"path": "output/s.json", "media_type": "application/json", "required": True}],
            result_adapter="r@1.0.0",
        )
    with pytest.raises(ValueError, match="越界"):
        SolverBundleManifest(
            bundle={"generator": _GENERATOR, "solver": _SOLVER},
            inputs=[{"path": "input/a.mps", "media_type": "application/mps"}],
            command={"executor": "e@1.0.0", "executable": "s@1.0.0", "arguments": ["a"]},
            outputs=[{"path": "../evil.json", "media_type": "application/json", "required": True}],
            result_adapter="r@1.0.0",
        )


def test_config_schema_word_checked() -> None:
    """公开 CalculationConfig 字头与选择校验: 非法字头/引用即拒绝。

    运行期只做表示映射, 直接构造, 缺字段即映射失败, 不补默认值。
    旧形状(无 mode/精度/时间限制)整体删除，不做兼容。
    """
    with pytest.raises(ValueError, match="schema"):
        _config(schema="ies.other")
    with pytest.raises(ValueError, match="版本"):
        _config(schema_version="9.9.9")
    good = _config(seed=42, options={"gap_rel": 0.01})
    assert good.seed == 42
    assert good.to_dict() == {
        "schema": CALCULATION_CONFIG_SCHEMA,
        "schema_version": CALCULATION_CONFIG_VERSION,
        "mode": "planning",
        "predictions": [],
        "generator": _GENERATOR,
        "solver": _SOLVER,
        "precision": "medium",
        "convergence": None,
        "time_limit_seconds": 600,
        "options": {"gap_rel": 0.01},
        "seed": 42,
        "outputs": ["solution"],
    }
    with pytest.raises(TypeError):
        _config(seed="42")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="generator"):
        CalculationConfig(
            mode="planning", solver=_SOLVER, precision="medium",
            time_limit_seconds=600, outputs=["solution"],
        )
    with pytest.raises(ValueError, match="solver"):
        _config(solver="ies.solver.highs@1.7")
    with pytest.raises(ValueError, match="输出请求"):
        _config(outputs=[""])
    with pytest.raises(ValueError, match="precision"):
        _config(precision="ultra")
    with pytest.raises(ValueError, match="time_limit_seconds"):
        _config(time_limit_seconds=0)


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
    """结果/Bundle/回执字头为公开 schema, 非法字头即拒绝。"""
    assert _manifest().schema == SOLVER_BUNDLE_SCHEMA
    assert _manifest().schema_version == SOLVER_BUNDLE_VERSION
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
    receipt = _receipt(bundle_id="b")
    assert receipt.schema == EXECUTION_RECEIPT_SCHEMA
    assert receipt.schema_version == EXECUTION_RECEIPT_VERSION
    assert receipt.to_dict()["schema"] == "ies.execution-receipt"
    assert receipt.to_dict()["schema_version"] == "1.0.0"
    with pytest.raises(ValueError, match="schema"):
        _receipt(schema="ies.other", bundle_id="b")


def test_receipt_refs_status_and_executed_consistency() -> None:
    """回执引用形态、状态封闭集与已执行/未执行一致性，无 detail 残留。"""
    assert set(RECEIPT_STATUSES) == {
        "succeeded", "policy_rejected", "process_failed",
        "output_protocol_failed", "timed_out", "cancelled", "fenced",
    }
    dumped = _receipt().to_dict()
    assert "detail" not in dumped
    assert not hasattr(_receipt(), "detail")
    for field_name in (
        "generator_ref", "solver_ref", "executor_ref", "executable_ref",
        "result_adapter_ref",
    ):
        with pytest.raises(ValueError, match="形态"):
            _receipt(**{field_name: "not-a-ref"})
    with pytest.raises(ValueError, match="状态非法"):
        _receipt(status="timeout")
    rejected = _receipt(
        status="policy_rejected", exit_code=None, termination_reason="",
        started_at="", ended_at="",
    )
    assert rejected.exit_code is None
    assert rejected.started_at == "" and rejected.ended_at == ""
    with pytest.raises(ValueError, match="不得携带"):
        _receipt(status="policy_rejected")
    stopped = _receipt(
        status="cancelled", termination_reason="cancelled", exit_code=None,
        started_at="", ended_at="",
    )
    assert stopped.exit_code is None
    assert stopped.started_at == ""
    midrun = _receipt(
        status="cancelled", termination_reason="cancelled", exit_code=None,
        signal_number=15,
    )
    assert midrun.started_at != "" and midrun.exit_code is None
    with pytest.raises(ValueError, match="启动前停止回执不得携带"):
        _receipt(status="cancelled", termination_reason="cancelled",
                 exit_code=0, started_at="", ended_at="")
    with pytest.raises(ValueError, match="启动前停止回执不得携带"):
        _receipt(status="fenced", termination_reason="fenced",
                 signal_number=9, started_at="", ended_at="")
    with pytest.raises(ValueError, match="运行中停止回执须保留"):
        _receipt(status="cancelled", termination_reason="cancelled",
                 exit_code=None)
    with pytest.raises(ValueError, match="时间戳"):
        _receipt(status="cancelled", termination_reason="cancelled",
                 exit_code=None, signal_number=15, started_at="",
                 ended_at="2026-09-24T00:00:01Z")
    with pytest.raises(ValueError, match="时间戳"):
        _receipt(status="fenced", termination_reason="fenced",
                 exit_code=None, signal_number=15,
                 started_at="not-a-time", ended_at="2026-09-24T00:00:01Z")
    with pytest.raises(ValueError, match="不得晚于"):
        _receipt(status="cancelled", termination_reason="cancelled",
                 exit_code=None, signal_number=15,
                 started_at="2026-09-24T00:00:02Z",
                 ended_at="2026-09-24T00:00:01Z")
    with pytest.raises(ValueError, match="fenced 状态须与 fenced 原因一致"):
        _receipt(status="fenced", termination_reason="cancelled",
                 exit_code=None)
    with pytest.raises(ValueError, match="cancelled 状态须与 cancelled 原因一致"):
        _receipt(status="succeeded", termination_reason="cancelled")
    with pytest.raises(ValueError, match="恰有一个有值"):
        _receipt(exit_code=None, signal_number=None)
    with pytest.raises(ValueError, match="非空时间戳"):
        _receipt(started_at="", ended_at="")
    with pytest.raises(ValueError, match="带时区"):
        _receipt(
            started_at="2026-09-24T00:00:00", ended_at="2026-09-24T00:00:01Z"
        )
