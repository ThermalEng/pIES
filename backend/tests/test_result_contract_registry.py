"""0.10-A:ComputeResult 双层状态契约与 ResultAdapterProvider 原子注册。

只覆盖本切片真实契约: 封闭枚举、深层不可变、描述符校验、原子发布/
精确解析/失败语义与架构边界。不含任何求解器行为映射与端到端算例
(后续切片)。
"""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Mapping

import pytest

from iesplan.computation import (
    COMPUTE_RESULT_SCHEMA,
    COMPUTE_RESULT_VERSION,
    EXECUTION_STATUSES,
    MATH_STATUSES,
    ComputationUnavailableError,
    ComputeResult,
    ExecutionReceipt,
    ResultAdapter,
    ResultAdapterDescriptor,
    ResultAdapterRegistry,
    SolverBundle,
    build_result_adapter_registry,
)


class _Adapter:
    """最小真实适配器替身: 身份真实, adapt 只做结构回显(无算法逻辑)。"""

    def __init__(self, adapter_id: str = "acme.adapter", available: bool = True) -> None:
        self._descriptor = ResultAdapterDescriptor(adapter_id=adapter_id, version="1.0.0")
        self._available = available

    @property
    def descriptor(self) -> ResultAdapterDescriptor:
        return self._descriptor

    @property
    def ref(self) -> str:
        return self._descriptor.ref

    def available(self) -> bool:
        return self._available

    def adapt(
        self,
        bundle: SolverBundle,
        receipt: ExecutionReceipt,
        outputs: Mapping[str, object],
    ) -> ComputeResult:
        return ComputeResult(
            bundle_id=bundle.bundle_id,
            attempt_id=receipt.attempt_id,
            execution_status="completed",
            math_status="solved",
            outputs={"echo": sorted(outputs)},
        )


def _result(**kwargs) -> ComputeResult:
    kwargs.setdefault("bundle_id", "bundle-1")
    kwargs.setdefault("attempt_id", "attempt-1")
    kwargs.setdefault("execution_status", "completed")
    kwargs.setdefault("math_status", "solved")
    return ComputeResult(**kwargs)


#: 回执→技术状态映射测试数据(与 ComputeResult 契约规则同源; 本层只做
#: 覆盖断言, 不实现生产映射)。
RECEIPT_TO_EXECUTION = (
    ({"status": "succeeded"}, "completed"),
    ({"status": "policy_rejected"}, "rejected"),
    ({"status": "process_failed", "termination_reason": "oom"}, "resource_exhausted"),
    ({"status": "process_failed", "termination_reason": "exited"}, "failed"),
    ({"status": "process_failed", "termination_reason": ""}, "failed"),
    ({"status": "output_protocol_failed"}, "protocol_failed"),
    ({"status": "timed_out"}, "timed_out"),
    ({"status": "cancelled"}, "cancelled"),
    ({"status": "fenced"}, "fenced"),
)


def test_status_layers_are_closed_and_orthogonal() -> None:
    """技术/数学状态分层封闭, 互不混写, 非法与缺失一律拒绝。"""
    assert set(EXECUTION_STATUSES) == {
        "completed",
        "rejected",
        "failed",
        "timed_out",
        "cancelled",
        "fenced",
        "resource_exhausted",
        "protocol_failed",
    }
    assert set(MATH_STATUSES) == {
        "solved",
        "infeasible",
        "unbounded",
        "numeric_failure",
        "not_solved",
    }
    assert isinstance(_Adapter(), ResultAdapter)
    for bad in ("succeeded", "normal_completion", "", "COMPLETED"):
        with pytest.raises(ValueError, match="execution_status"):
            _result(execution_status=bad)
    for bad in ("optimal", "failed", "", "SOLVED"):
        with pytest.raises(ValueError, match="math_status"):
            _result(math_status=bad)
    with pytest.raises(ValueError, match="bundle_id"):
        _result(bundle_id="")
    with pytest.raises(ValueError, match="attempt_id"):
        _result(attempt_id="")
    with pytest.raises(ValueError, match="schema"):
        _result(schema="ies.other")
    # 无业务结局字段: 旧模糊形态不再接受。
    with pytest.raises(TypeError):
        ComputeResult(bundle_id="b", status="succeeded")  # type: ignore[call-arg]


def test_receipt_statuses_all_have_execution_home() -> None:
    """七种真实回执状态各有准确技术归属, OOM 归资源耗尽且与其余
    process_failed 区分, 策略拒绝/租约隔离不误报为协议失败或取消。"""
    seen_receipts = set()
    for facts, expected in RECEIPT_TO_EXECUTION:
        assert expected in EXECUTION_STATUSES
        seen_receipts.add(facts["status"])
    assert seen_receipts == {
        "succeeded",
        "policy_rejected",
        "process_failed",
        "output_protocol_failed",
        "timed_out",
        "cancelled",
        "fenced",
    }
    by_receipt = {}
    for facts, expected in RECEIPT_TO_EXECUTION:
        by_receipt.setdefault(facts["status"], set()).add(expected)
    assert by_receipt["process_failed"] == {"resource_exhausted", "failed"}
    for single in (
        "succeeded",
        "policy_rejected",
        "output_protocol_failed",
        "timed_out",
        "cancelled",
        "fenced",
    ):
        assert len(by_receipt[single]) == 1
    assert by_receipt["policy_rejected"] != {"protocol_failed", "cancelled"}
    assert by_receipt["fenced"] != {"cancelled"}


def test_result_is_deeply_immutable_and_versioned() -> None:
    """回执式深层不可变与版本字头, to_dict 返回独立副本。

    2.0.0 为破坏性 schema 变更(双层状态 + attempt_id 追溯, 无旧字段
    兼容), 版本号钉死以防无声回退。
    """
    assert COMPUTE_RESULT_VERSION == "2.0.0"
    result = _result(outputs={"k": {"nested": [1]}})
    assert dataclasses.is_dataclass(result)
    assert result.schema == COMPUTE_RESULT_SCHEMA
    assert result.schema_version == COMPUTE_RESULT_VERSION
    assert result.attempt_id == "attempt-1"
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.math_status = "infeasible"  # type: ignore[misc]
    with pytest.raises(TypeError):
        result.outputs["k"] = {}  # type: ignore[index]
    plain = result.to_dict()
    assert plain["execution_status"] == "completed"
    assert plain["math_status"] == "solved"
    assert plain["attempt_id"] == "attempt-1"
    assert plain["outputs"] == {"k": {"nested": [1]}}
    plain["outputs"]["k"]["nested"].append(2)
    assert result.outputs["k"]["nested"] == (1,)


def test_descriptor_is_minimal_and_exact() -> None:
    """描述符只含精确选择所需的身份二元组, 非法引用当场拒绝。"""
    descriptor = ResultAdapterDescriptor(adapter_id="acme.adapter", version="1.0.0")
    assert descriptor.ref == "acme.adapter@1.0.0"
    assert descriptor.to_dict() == {"adapter_id": "acme.adapter", "version": "1.0.0"}
    with pytest.raises(ValueError, match="稳定 ID"):
        ResultAdapterDescriptor(adapter_id="bad id", version="1.0.0")
    with pytest.raises(ValueError, match="版本非法"):
        ResultAdapterDescriptor(adapter_id="acme.adapter", version="1.0")


def test_registry_publishes_atomically_and_resolves_exactly() -> None:
    """原子发布 + 精确解析: 未知/重复/不可用/不一致明确失败, 不回退。"""
    adapter = _Adapter()
    registry = build_result_adapter_registry([adapter])
    assert isinstance(registry, ResultAdapterRegistry)
    assert len(registry) == 1
    assert registry.get("acme.adapter@1.0.0") is adapter
    assert registry.resolve("acme.adapter@1.0.0") is adapter
    assert [d.ref for d in registry.snapshot()] == ["acme.adapter@1.0.0"]
    assert registry.to_dict() == {"result_adapters": [{"adapter_id": "acme.adapter", "version": "1.0.0"}]}
    with pytest.raises(ComputationUnavailableError) as exc_info:
        registry.resolve("acme.missing@1.0.0")
    assert exc_info.value.reason == "no-result-adapter"
    with pytest.raises(ComputationUnavailableError) as exc_info:
        build_result_adapter_registry([_Adapter(available=False)])
    assert exc_info.value.reason == "no-result-adapter"
    with pytest.raises(ValueError, match="重复注册"):
        build_result_adapter_registry([_Adapter(), _Adapter()])

    class _Mismatched(_Adapter):
        @property
        def ref(self) -> str:
            return "acme.other@1.0.0"

    with pytest.raises(ValueError, match="不一致"):
        build_result_adapter_registry([_Mismatched()])
    with pytest.raises(TypeError, match="协议"):
        build_result_adapter_registry([object()])  # type: ignore[list-item]
    empty = build_result_adapter_registry([])
    assert len(empty) == 0
    with pytest.raises(ComputationUnavailableError) as exc_info:
        empty.resolve("acme.adapter@1.0.0")
    assert exc_info.value.reason == "no-result-adapter"


def test_adapt_call_shape_only() -> None:
    """adapt 唯一调用形状: 只读 Bundle + 回执 + 声明输出 → 结果。"""
    assert list(inspect.signature(ResultAdapter.adapt).parameters) == [
        "self",
        "bundle",
        "receipt",
        "outputs",
    ]
