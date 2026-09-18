"""生成器目录契约测试(0.8 切片 A)。

只覆盖公开 descriptor 与原子注册表，不涉及真实 generator 实现：

- descriptor 字段校验：非法 ref 形态/版本、空能力条目、非法 adapter
  ref 即拒绝；``ref`` 为 ``<稳定ID>@<语义化版本>`` 唯一身份；
- 原子发布：重复引用、非 descriptor 候选即整体拒绝，不发布部分表；
- 选择语义：空表/未知引用统一 ``ComputationUnavailableError``；
  solver 不兼容不回退到另一生成器；
- 快照确定性：按稳定 ID 排序；``to_dict`` 为独立副本。
"""

from __future__ import annotations

import pytest

from iesplan.computation import (
    CalculationConfig,
    ComputationUnavailableError,
    GeneratorDescriptor,
    GeneratorRegistry,
    build_registry,
)

GENERATOR = "acme.generator@1.0.0"
SOLVER = "ies.solver.highs@1.7.2"


def _descriptor(**kwargs) -> GeneratorDescriptor:
    kwargs.setdefault("generator_id", "acme.generator")
    kwargs.setdefault("version", "1.0.0")
    kwargs.setdefault("solvers", (SOLVER,))
    return GeneratorDescriptor(**kwargs)


def _config(**kwargs) -> CalculationConfig:
    kwargs.setdefault("generator", GENERATOR)
    kwargs.setdefault("solver", SOLVER)
    return CalculationConfig(**kwargs)


def test_descriptor_ref_and_solver_support() -> None:
    """descriptor ref 为精确引用；solver 兼容可查询。"""
    desc = _descriptor()
    assert desc.ref == GENERATOR
    assert desc.supports_solver(SOLVER) is True
    assert desc.supports_solver("other.solver@9.9.9") is False
    dumped = desc.to_dict()
    assert dumped["generator_id"] == "acme.generator"
    assert dumped["solvers"] == [SOLVER]


def test_descriptor_rejects_bad_identity() -> None:
    """非法 ID/版本/adapter ref 的 descriptor 即拒绝。"""
    with pytest.raises(ValueError, match="generator"):
        _descriptor(generator_id="")
    with pytest.raises(ValueError, match="generator"):
        _descriptor(version="1.0")
    with pytest.raises(ValueError, match="result_adapter"):
        _descriptor(result_adapter="not-a-ref")
    with pytest.raises(ValueError, match="capabilities"):
        _descriptor(capabilities=("",))


def test_build_registry_is_atomic() -> None:
    """重复引用/非候选即整体拒绝；空候选发布空表。"""
    with pytest.raises(ValueError, match="重复注册"):
        build_registry([_descriptor(), _descriptor()])
    with pytest.raises(TypeError, match="GeneratorDescriptor"):
        build_registry([_descriptor(), object()])  # type: ignore[list-item]
    empty = build_registry(())
    assert len(empty) == 0
    assert empty.snapshot() == ()


def test_empty_or_unknown_generator_is_unavailable() -> None:
    """空表/未知引用统一 ComputationUnavailableError，不回退。"""
    empty = build_registry(())
    with pytest.raises(ComputationUnavailableError) as exc_info:
        empty.get(GENERATOR)
    assert exc_info.value.reason == "no-provider"
    with pytest.raises(ComputationUnavailableError):
        empty.resolve(_config())


def test_resolve_checks_solver_compatibility() -> None:
    """选中 provider 后 solver 不兼容即失败，不换另一生成器。"""
    registry = build_registry([_descriptor()])
    assert registry.resolve(_config()).ref == GENERATOR
    bad_solver = _config(solver="other.solver@9.9.9")
    with pytest.raises(ComputationUnavailableError) as exc_info:
        registry.resolve(bad_solver)
    assert exc_info.value.reason == "incompatible-solver"


def test_snapshot_is_deterministic_and_dump_is_copy() -> None:
    """快照按稳定 ID 排序；to_dict 为独立副本。"""
    registry = build_registry([
        _descriptor(generator_id="zeta.generator", solvers=()),
        _descriptor(generator_id="acme.generator", solvers=(SOLVER,)),
    ])
    assert [d.generator_id for d in registry.snapshot()] == [
        "acme.generator", "zeta.generator",
    ]
    dumped = registry.to_dict()
    assert [g["generator_id"] for g in dumped["generators"]] == [
        "acme.generator", "zeta.generator",
    ]
    dumped["generators"].clear()
    assert len(registry) == 2


def test_registry_instances_hold_no_global_state() -> None:
    """注册表实例级持有：两次构建互不影响，无模块全局发布状态。"""
    first = build_registry([_descriptor()])
    second = build_registry(())
    assert len(first) == 1
    assert len(second) == 0
    assert isinstance(first, GeneratorRegistry)
