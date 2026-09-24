"""ExecutorProvider 独立注册 contract 测试(0.9 切片 A)。

只覆盖公开描述符与原子注册表，不涉及真实 executor 实现，不启动进程：

- descriptor 字段校验：非法 ref 形态、空 executables、非法 executable
  引用即拒绝；
- 原子发布：重复引用、非 provider 候选、描述符缺失/类型不符、ref 与
  描述符不一致、不可用候选即整体拒绝，不发布部分表；
- 选择语义：精确 executor 选择 + executable 受支持确认；未知引用与不
  受支持 executable 统一 ``ComputationUnavailableError``，不回退；
- resolve/get 返回真实可调用 provider；快照与 to_dict 只公开描述符数据。
"""

from __future__ import annotations

import pytest

from iesplan.computation import (
    ComputationUnavailableError,
    ExecutorDescriptor,
    ExecutorProvider,
    ExecutorRegistry,
    build_executor_registry,
)

EXECUTOR = "t.executor@1.0.0"
EXE = "t.exe@1.0.0"


def _descriptor(**kwargs) -> ExecutorDescriptor:
    kwargs.setdefault("executor_id", "t.executor")
    kwargs.setdefault("version", "1.0.0")
    kwargs.setdefault("executables", (EXE,))
    return ExecutorDescriptor(**kwargs)


class _FakeProvider:
    """注册表测试替身：符合协议的最小真实形状（引用/描述符/可用性）。"""

    def __init__(
        self,
        descriptor: ExecutorDescriptor,
        available: bool = True,
        ref: str | None = None,
    ):
        self._descriptor = descriptor
        self._available = available
        self._ref = ref

    @property
    def ref(self) -> str:
        return self._ref if self._ref is not None else self._descriptor.ref

    @property
    def descriptor(self) -> ExecutorDescriptor:
        return self._descriptor

    def available(self) -> bool:
        return self._available

    def execute(self, command, workdir):
        raise AssertionError("注册表测试替身不应被调用")


def _provider(**kwargs) -> _FakeProvider:
    return _FakeProvider(_descriptor(**kwargs))


def test_descriptor_rejects_bad_identity_and_empty_executables() -> None:
    """非法引用形态与空 executable 集即拒绝发布。"""
    assert _descriptor().ref == EXECUTOR
    assert _descriptor().supports_executable(EXE)
    assert not _descriptor().supports_executable("t.other@1.0.0")
    with pytest.raises(ValueError, match="executables 不得为空"):
        _descriptor(executables=())
    with pytest.raises(ValueError, match="稳定 ID 非法"):
        _descriptor(executor_id="bad id")
    with pytest.raises(ValueError, match="形态"):
        _descriptor(executables=("not-a-ref",))
    dumped = _descriptor().to_dict()
    assert dumped == {
        "executor_id": "t.executor",
        "version": "1.0.0",
        "executables": [EXE],
    }


def test_build_registry_is_atomic() -> None:
    """重复引用/非 provider 候选/描述符不符/不可用即整体拒绝。"""
    with pytest.raises(ValueError, match="重复注册"):
        build_executor_registry([_provider(), _provider()])
    with pytest.raises(TypeError, match="ExecutorProvider"):
        build_executor_registry([_provider(), object()])  # type: ignore[list-item]
    nodescriptor = _FakeProvider(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ExecutorDescriptor"):
        build_executor_registry([nodescriptor])
    mismatch = _FakeProvider(
        _descriptor(executor_id="t.other"), ref="t.executor@1.0.0"
    )
    with pytest.raises(ValueError, match="不一致"):
        build_executor_registry([mismatch])
    down = _FakeProvider(_descriptor(), available=False)
    with pytest.raises(ComputationUnavailableError):
        build_executor_registry([down])
    empty = build_executor_registry(())
    assert len(empty) == 0
    assert empty.snapshot() == ()


def test_resolve_selects_exact_executor_and_executable() -> None:
    """精确选择 + executable 确认后返回真实可调用 provider。"""
    provider = _provider()
    registry = build_executor_registry([provider])
    resolved = registry.resolve(EXECUTOR, EXE)
    assert resolved is provider
    assert isinstance(resolved, ExecutorProvider)
    assert resolved.descriptor.ref == EXECUTOR
    assert callable(resolved.execute)
    assert registry.get(EXECUTOR) is provider


def test_unknown_or_unsupported_is_unavailable_without_fallback() -> None:
    """未知引用与不受支持 executable 统一不可用，不换另一 executor。"""
    registry = build_executor_registry([_provider()])
    with pytest.raises(ComputationUnavailableError) as exc_info:
        registry.resolve("t.missing@1.0.0", EXE)
    assert exc_info.value.reason == "no-executor"
    with pytest.raises(ComputationUnavailableError) as exc_info:
        registry.resolve(EXECUTOR, "t.other@9.9.9")
    assert exc_info.value.reason == "unsupported-executable"
    with pytest.raises(ComputationUnavailableError):
        registry.get("t.missing@1.0.0")


def test_snapshot_and_dump_expose_descriptors_only() -> None:
    """快照确定性排序；to_dict 只含描述符数据，不含实现。"""
    import json

    registry = build_executor_registry([
        _provider(executor_id="zeta.executor"),
        _provider(executor_id="acme.executor"),
    ])
    assert [d.ref for d in registry.snapshot()] == [
        "acme.executor@1.0.0", "zeta.executor@1.0.0",
    ]
    dumped = registry.to_dict()
    assert [e["executor_id"] for e in dumped["executors"]] == [
        "acme.executor", "zeta.executor",
    ]
    assert "execute" not in json.dumps(dumped)


def test_registry_instances_hold_no_global_state() -> None:
    """注册表实例级持有；空表选择即不可用。"""
    first = build_executor_registry([_provider()])
    second = build_executor_registry(())
    assert len(first) == 1
    assert len(second) == 0
    with pytest.raises(ComputationUnavailableError):
        second.resolve(EXECUTOR, EXE)
