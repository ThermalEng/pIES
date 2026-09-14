"""computation 边界契约测试(Wave4-A)。

只覆盖真实边界, 不涉及任何 0.8 算法实现:

- Protocol 形状: 三个边界协议为运行时可检查的 Protocol, 且拒绝
  不完整实现的结构判定;
- unavailable 错误语义: ``ComputationUnavailableError`` 为
  ``RuntimeError``, 默认 reason 为 ``"no-provider"``;
- provider 目录为空: ``available_providers()`` 恒为空, 且返回副本
  (调用方改写不污染门面);
- 注册表门面: ``iesplan.computation`` 选择性重导出注册表符号,
  ``get_algorithm`` / ``list_algorithms`` 行为与搬迁前一致。
"""

from __future__ import annotations

import dataclasses

import pytest

from iesplan.computation import (
    DEFAULT_ALGORITHM,
    AlgorithmSpec,
    ComputationUnavailableError,
    ComputeResult,
    GeneratorProvider,
    ResultAdapter,
    SolverBundle,
    SolverRuntime,
    available_providers,
    get_algorithm,
    list_algorithms,
)
from iesplan.core.errors import NotFoundError


class _EmptyProvider:
    """仅满足形状的最小桩(无任何算法逻辑): available 恒 False,
    实际调用一律抛 unavailable。"""

    @property
    def ref(self) -> str:
        return "ies.algo.milp_hybrid@1.0.0"

    def available(self) -> bool:
        return False

    def generate(self, bundle: SolverBundle) -> ComputeResult:
        raise ComputationUnavailableError(reason="no-provider")

    def run(self, bundle: SolverBundle) -> ComputeResult:  # noqa: ARG002
        raise ComputationUnavailableError(reason="no-provider")

    def adapt(self, result: ComputeResult) -> dict:  # noqa: ARG002
        raise ComputationUnavailableError(reason="no-provider")


def test_protocols_are_runtime_checkable_shapes() -> None:
    """三协议为运行时 Protocol; 完整桩通过三者结构判定, 空对象均不通过。"""
    stub = _EmptyProvider()
    assert isinstance(stub, GeneratorProvider)
    assert isinstance(stub, SolverRuntime)
    assert isinstance(stub, ResultAdapter)
    assert not isinstance(object(), GeneratorProvider)
    assert not isinstance(object(), SolverRuntime)
    assert not isinstance(object(), ResultAdapter)


def test_bundles_are_immutable_value_objects() -> None:
    """SolverBundle / ComputeResult 为不可变 dataclass(属性赋值即失败)。"""
    bundle = SolverBundle(generator_ref="g", solver_ref="s")
    result = ComputeResult(status="ok")
    assert dataclasses.is_dataclass(bundle)
    assert dataclasses.is_dataclass(result)
    with pytest.raises(dataclasses.FrozenInstanceError):
        bundle.generator_ref = "other"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = "failed"  # type: ignore[misc]


def test_unavailable_error_semantics() -> None:
    """unavailable 错误语义: RuntimeError, 默认 reason, 调用一律明确失败。"""
    err = ComputationUnavailableError()
    assert isinstance(err, RuntimeError)
    assert err.reason == "no-provider"
    assert "0.8" in str(err)
    stub = _EmptyProvider()
    bundle = SolverBundle(generator_ref="g", solver_ref="s")
    with pytest.raises(ComputationUnavailableError):
        stub.generate(bundle)
    with pytest.raises(ComputationUnavailableError):
        stub.run(bundle)


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
