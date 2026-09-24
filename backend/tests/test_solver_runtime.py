"""通用 SolverRuntime 编排测试（fake executor 真实可调用）。

测试专属 fake 只活在本文件，不进入生产组合根或能力目录：它被
Runtime 真正调用，在 Runtime 建立的隔离工作区内读写真实文件并返回
真实 ``ProcessOutcome``，证明编排可运行而非固定成功 mock。

覆盖：成功执行与声明输出收集；精确 registry 选择；策略拒绝且
executor 零调用；非零退出；信号终止；必需输出缺失；未声明输出；
工作区每次独立且结束后清理；回执身份/时间/状态/refs 正确且不可变；
无 fallback；executor 抛异常时向上传播且工作区清理。
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat
from datetime import datetime, timezone

import pytest

from iesplan.computation import (
    AttemptContext,
    DeploymentPolicy,
    ExecutionReceipt,
    ExecutorDescriptor,
    ExecutorProvider,
    ProcessOutcome,
    SolverBundle,
    SolverBundleManifest,
    SolverRuntime,
    build_executor_registry,
)

EXECUTOR = "t.executor@1.0.0"
EXE = "t.exe@1.0.0"
OTHER_EXECUTOR = "t.other@2.0.0"
OTHER_EXE = "t.other-exe@2.0.0"
INPUT_BYTES = b"fake-mps-bytes"
SOLUTION_BYTES = b'{"status": "optimal"}'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _outcome(
    exit_code: int | None = 0,
    signal: int | None = None,
    reason: str = "exited",
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> ProcessOutcome:
    return ProcessOutcome(
        exit_code=exit_code,
        signal_number=signal,
        termination_reason=reason,
        started_at=_now(),
        ended_at=_now(),
        stdout=stdout,
        stderr=stderr,
    )


def _manifest(**command_overrides) -> SolverBundleManifest:
    command: dict[str, object] = {
        "executor": EXECUTOR,
        "executable": EXE,
        "arguments": ["input/problem.mps"],
        "environment": {"T_SOLVER_THREADS": "2"},
        "timeout_seconds": 600,
        "memory_mib": 4096,
        "cpu_cores": 4,
        "network": False,
        "success_exit_codes": [0],
    }
    command.update(command_overrides)
    return SolverBundleManifest(
        bundle={
            "generator": "acme.generator@1.0.0",
            "solver": "ies.solver.highs@1.7.2",
        },
        inputs=[{"path": "input/problem.mps", "media_type": "application/mps"}],
        command=command,
        outputs=[
            {
                "path": "output/solution.json",
                "media_type": "application/json",
                "required": True,
            },
            {
                "path": "output/optional.json",
                "media_type": "application/json",
                "required": False,
            },
        ],
        result_adapter="acme.result-adapter@1.0.0",
    )


def _bundle(**command_overrides) -> SolverBundle:
    return SolverBundle(
        manifest=_manifest(**command_overrides),
        files={"input/problem.mps": INPUT_BYTES},
    )


def _policy(**kwargs) -> DeploymentPolicy:
    kwargs.setdefault("executables", (EXE, OTHER_EXE))
    kwargs.setdefault("environment_vars", ("T_SOLVER_THREADS",))
    kwargs.setdefault("max_timeout_seconds", 600)
    kwargs.setdefault("max_memory_mib", 4096)
    kwargs.setdefault("max_cpu_cores", 4)
    kwargs.setdefault("allow_network", False)
    return DeploymentPolicy(**kwargs)


def _attempt(**kwargs) -> AttemptContext:
    kwargs.setdefault("attempt_id", "attempt-7")
    kwargs.setdefault("bundle_id", "bundle-7")
    kwargs.setdefault("assembly_id", "assembly-7")
    kwargs.setdefault("calculation_config_id", "config-7")
    return AttemptContext(**kwargs)


class _FakeExecutor:
    """测试专属 fake：在隔离工作区读写真实文件，返回真实过程结果。"""

    def __init__(
        self,
        executor_id: str = "t.executor",
        version: str = "1.0.0",
        executables: tuple[str, ...] = (EXE,),
        handler=None,
    ):
        self._descriptor = ExecutorDescriptor(
            executor_id=executor_id,
            version=version,
            executables=executables,
        )
        self._handler = handler
        self.calls: list[tuple[dict, str]] = []

    @property
    def descriptor(self) -> ExecutorDescriptor:
        return self._descriptor

    @property
    def ref(self) -> str:
        return self._descriptor.ref

    def available(self) -> bool:
        return True

    def execute(self, command, workdir) -> ProcessOutcome:
        snapshot = dict(command)
        self.calls.append((snapshot, workdir))
        return self._handler(snapshot, workdir)


def _success_handler(probe: dict, write_optional: bool = True):
    """成功 handler：核对输入字节与只读位语义，再写声明输出。"""

    def handle(command, workdir):
        input_path = os.path.join(workdir, "input/problem.mps")
        with open(input_path, "rb") as handle_in:
            probe["input_ok"] = handle_in.read() == INPUT_BYTES
        probe["input_mode"] = stat.S_IMODE(os.stat(input_path).st_mode)
        probe["input_dir_mode"] = stat.S_IMODE(
            os.stat(os.path.join(workdir, "input")).st_mode
        )
        probe["workdir"] = workdir
        probe["saw_command"] = command
        os.makedirs(os.path.join(workdir, "output"), exist_ok=True)
        with open(
            os.path.join(workdir, "output/solution.json"), "wb"
        ) as handle_out:
            handle_out.write(SOLUTION_BYTES)
        if write_optional:
            with open(
                os.path.join(workdir, "output/optional.json"), "wb"
            ) as handle_opt:
                handle_opt.write(b"{}")
        return _outcome(stdout=b"solved", stderr=b"")

    return handle


def _runtime(provider: _FakeExecutor, policy=None) -> SolverRuntime:
    assert isinstance(provider, ExecutorProvider)
    return SolverRuntime(build_executor_registry([provider]), policy or _policy())


def test_success_collects_declared_outputs_and_receipt() -> None:
    """成功执行：声明输出收集完整，回执身份/时间/状态/refs 正确。"""
    probe: dict = {}
    runtime = _runtime(_FakeExecutor(handler=_success_handler(probe)))
    receipt, outputs = runtime.run(_bundle(), _attempt())
    assert probe["input_ok"] is True
    assert probe["input_mode"] == 0o444
    assert probe["input_dir_mode"] == 0o555
    assert outputs == {
        "output/solution.json": SOLUTION_BYTES,
        "output/optional.json": b"{}",
    }
    assert isinstance(receipt, ExecutionReceipt)
    assert receipt.status == "succeeded"
    assert receipt.error == ""
    assert receipt.exit_code == 0
    assert receipt.signal_number is None
    assert receipt.termination_reason == "exited"
    assert receipt.receipt_id
    assert receipt.bundle_id == "bundle-7"
    assert receipt.assembly_id == "assembly-7"
    assert receipt.calculation_config_id == "config-7"
    assert receipt.attempt_id == "attempt-7"
    assert receipt.generator_ref == "acme.generator@1.0.0"
    assert receipt.solver_ref == "ies.solver.highs@1.7.2"
    assert receipt.executor_ref == EXECUTOR
    assert receipt.executable_ref == EXE
    assert receipt.result_adapter_ref == "acme.result-adapter@1.0.0"
    assert receipt.started_at.endswith("Z") and receipt.ended_at.endswith("Z")
    assert receipt.started_at <= receipt.ended_at
    assert receipt.stdout == "solved"
    assert receipt.stderr == ""
    assert receipt.to_dict()["policy_evaluated"] == _policy().to_dict()
    assert "policy_applied" not in receipt.to_dict()
    assert receipt.resource_usage == {}
    assert [e["path"] for e in receipt.outputs] == [
        "output/solution.json",
        "output/optional.json",
    ]
    assert receipt.outputs[0]["size_bytes"] == len(SOLUTION_BYTES)
    assert receipt.outputs[0]["required"] is True
    dumped = receipt.to_dict()
    assert dumped["receipt_id"] == receipt.receipt_id
    assert dumped["outputs"][0]["size_bytes"] == len(SOLUTION_BYTES)
    json.dumps(dumped)


def test_non_dot_working_directory_uses_bundle_root() -> None:
    """非点工作目录：输入/输出仍按 Bundle 根定位，子进程目录已预建。"""
    probe: dict = {}

    def handle(command, workdir):
        assert command["working_directory"] == "run"
        run_dir = os.path.join(workdir, "run")
        assert os.path.isdir(run_dir)
        probe["run_owner_writable"] = bool(
            stat.S_IMODE(os.stat(run_dir).st_mode) & 0o200
        )
        with open(
            os.path.join(workdir, "input/problem.mps"), "rb"
        ) as handle_in:
            probe["input_ok"] = handle_in.read() == INPUT_BYTES
        os.makedirs(os.path.join(workdir, "output"), exist_ok=True)
        with open(
            os.path.join(workdir, "output/solution.json"), "wb"
        ) as handle_out:
            handle_out.write(SOLUTION_BYTES)
        return _outcome()

    receipt, outputs = _runtime(_FakeExecutor(handler=handle)).run(
        _bundle(working_directory="run"), _attempt()
    )
    assert receipt.status == "succeeded"
    assert probe["input_ok"] is True
    assert probe["run_owner_writable"] is True
    assert outputs == {"output/solution.json": SOLUTION_BYTES}


def test_process_outcome_exclusive_exit_and_tz_aware_times() -> None:
    """退出码/信号恰有一个；时间戳须带时区且按 UTC 先后比较。"""
    base = {
        "termination_reason": "exited",
        "started_at": "2026-09-24T00:00:00Z",
        "ended_at": "2026-09-24T00:00:01Z",
    }
    assert ProcessOutcome(exit_code=0, **base).exit_code == 0
    with pytest.raises(ValueError, match="恰有一个有值"):
        ProcessOutcome(exit_code=0, signal_number=9, **base)
    with pytest.raises(ValueError, match="恰有一个有值"):
        ProcessOutcome(exit_code=None, signal_number=None, **base)
    with pytest.raises(ValueError, match="带时区"):
        ProcessOutcome(
            exit_code=0,
            termination_reason="exited",
            started_at="2026-09-24T00:00:00",
            ended_at="2026-09-24T00:00:01Z",
        )
    cross = ProcessOutcome(
        exit_code=0,
        termination_reason="exited",
        started_at="2026-09-24T02:00:00+02:00",
        ended_at="2026-09-24T00:00:01Z",
    )
    assert cross.started_at == "2026-09-24T02:00:00+02:00"
    with pytest.raises(ValueError, match="不得晚于"):
        ProcessOutcome(
            exit_code=0,
            termination_reason="exited",
            started_at="2026-09-24T00:00:02Z",
            ended_at="2026-09-24T02:00:00+02:00",
        )


def test_optional_output_may_be_absent() -> None:
    """可选输出缺失仍成功；输出映射与回执清单只含实际文件。"""
    probe: dict = {}
    runtime = _runtime(
        _FakeExecutor(handler=_success_handler(probe, write_optional=False))
    )
    receipt, outputs = runtime.run(_bundle(), _attempt())
    assert receipt.status == "succeeded"
    assert outputs == {"output/solution.json": SOLUTION_BYTES}
    assert [e["path"] for e in receipt.outputs] == ["output/solution.json"]


def test_registry_selects_exact_executor_without_fallback() -> None:
    """精确选择命中的 executor；不支持的 executable 不切换到另一家。"""
    first = _FakeExecutor(handler=_success_handler({}))
    second = _FakeExecutor(
        executor_id="t.other",
        version="2.0.0",
        executables=(OTHER_EXE,),
        handler=_success_handler({}),
    )
    registry = build_executor_registry([first, second])
    runtime = SolverRuntime(registry, _policy())
    bundle = _bundle(executor=OTHER_EXECUTOR, executable=OTHER_EXE)
    receipt, _ = runtime.run(bundle, _attempt())
    assert receipt.status == "succeeded"
    assert receipt.executor_ref == OTHER_EXECUTOR
    assert len(second.calls) == 1
    assert first.calls == []
    stray = _bundle(executor=EXECUTOR, executable=OTHER_EXE)
    receipt, outputs = runtime.run(stray, _attempt())
    assert receipt.status == "policy_rejected"
    assert outputs == {}
    assert first.calls == []


def test_unknown_executor_is_rejected_without_calls() -> None:
    """未注册 executor 即拒绝，零调用且回执保留命令引用。"""
    fake = _FakeExecutor(handler=_success_handler({}))
    runtime = _runtime(fake)
    receipt, outputs = runtime.run(
        _bundle(executor="t.missing@1.0.0"), _attempt()
    )
    assert receipt.status == "policy_rejected"
    assert receipt.error
    assert receipt.executor_ref == "t.missing@1.0.0"
    assert receipt.exit_code is None
    assert receipt.started_at == "" and receipt.ended_at == ""
    assert outputs == {}
    assert fake.calls == []


def test_policy_gates_reject_without_calls() -> None:
    """allowlist/上限/网络门禁拒绝且 executor 零调用。"""
    cases = [
        (
            {"executable": OTHER_EXE, "executor": OTHER_EXECUTOR},
            _policy(executables=(EXE,)),
            "allowlist",
        ),
        ({"environment": {"EVIL_VAR": "1"}}, _policy(), "allowlist"),
        ({"timeout_seconds": 9999}, _policy(), "上限"),
        ({"memory_mib": 9999}, _policy(), "上限"),
        ({"network": True}, _policy(), "联网"),
    ]
    for command_overrides, policy, _ in cases:
        first = _FakeExecutor(handler=_success_handler({}))
        second = _FakeExecutor(
            executor_id="t.other",
            version="2.0.0",
            executables=(OTHER_EXE,),
            handler=_success_handler({}),
        )
        runtime = SolverRuntime(
            build_executor_registry([first, second]), policy
        )
        receipt, outputs = runtime.run(
            _bundle(**command_overrides), _attempt()
        )
        assert receipt.status == "policy_rejected", command_overrides
        assert receipt.error, command_overrides
        assert outputs == {}
        assert first.calls == [] and second.calls == [], command_overrides


def test_nonzero_exit_is_process_failed_with_existing_outputs() -> None:
    """非零退出即明确失败，已有声明输出仍保存，不伪造成功。"""

    def handle(command, workdir):
        os.makedirs(os.path.join(workdir, "output"), exist_ok=True)
        with open(
            os.path.join(workdir, "output/solution.json"), "wb"
        ) as handle_out:
            handle_out.write(SOLUTION_BYTES)
        return _outcome(exit_code=3, reason="exited", stderr=b"boom")

    receipt, outputs = _runtime(_FakeExecutor(handler=handle)).run(
        _bundle(), _attempt()
    )
    assert receipt.status == "process_failed"
    assert receipt.exit_code == 3
    assert "3" in receipt.error
    assert outputs == {"output/solution.json": SOLUTION_BYTES}
    assert receipt.stderr == "boom"


def test_signaled_process_is_process_failed() -> None:
    """信号终止形成明确失败回执，退出码为空，信号如实记录。"""

    def handle(command, workdir):
        return _outcome(exit_code=None, signal=9, reason="signaled")

    receipt, outputs = _runtime(_FakeExecutor(handler=handle)).run(
        _bundle(), _attempt()
    )
    assert receipt.status == "process_failed"
    assert receipt.exit_code is None
    assert receipt.signal_number == 9
    assert receipt.termination_reason == "signaled"
    assert outputs == {}


def test_missing_required_output_is_protocol_failed() -> None:
    """退出成功但必需输出缺失即输出协议失败。"""

    def handle(command, workdir):
        return _outcome()

    receipt, outputs = _runtime(_FakeExecutor(handler=handle)).run(
        _bundle(), _attempt()
    )
    assert receipt.status == "output_protocol_failed"
    assert "output/solution.json" in receipt.error
    assert outputs == {}


def test_undeclared_output_and_symlink_are_protocol_failed() -> None:
    """未声明文件与符号链接分别形成输出协议失败，不加入结果。"""

    def handle_extra(command, workdir):
        os.makedirs(os.path.join(workdir, "output"), exist_ok=True)
        with open(
            os.path.join(workdir, "output/solution.json"), "wb"
        ) as handle_out:
            handle_out.write(SOLUTION_BYTES)
        with open(
            os.path.join(workdir, "output/sneaky.bin"), "wb"
        ) as handle_extra_out:
            handle_extra_out.write(b"x")
        return _outcome()

    receipt, outputs = _runtime(_FakeExecutor(handler=handle_extra)).run(
        _bundle(), _attempt()
    )
    assert receipt.status == "output_protocol_failed"
    assert "sneaky.bin" in receipt.error
    assert outputs == {"output/solution.json": SOLUTION_BYTES}

    def handle_link(command, workdir):
        os.symlink("solution.json", os.path.join(workdir, "evil-link"))
        return _outcome()

    receipt, _ = _runtime(_FakeExecutor(handler=handle_link)).run(
        _bundle(), _attempt()
    )
    assert receipt.status == "output_protocol_failed"
    assert "evil-link" in receipt.error


def test_workdir_is_unique_per_call_and_cleaned() -> None:
    """每次调用工作区独立，结束后清理；输入字节真实写入。"""
    probe_one: dict = {}
    probe_two: dict = {}
    runtime = _runtime(_FakeExecutor(handler=_success_handler(probe_one)))
    runtime.run(_bundle(), _attempt())
    first_dir = probe_one["workdir"]
    runtime_two = _runtime(_FakeExecutor(handler=_success_handler(probe_two)))
    runtime_two.run(_bundle(), _attempt())
    second_dir = probe_two["workdir"]
    assert first_dir != second_dir
    assert not os.path.exists(first_dir)
    assert not os.path.exists(second_dir)


def test_executor_error_propagates_and_cleans_workdir() -> None:
    """executor 抛异常向上传播（无法建立可信回执），工作区仍清理。"""
    seen: dict = {}

    def handle(command, workdir):
        seen["workdir"] = workdir
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        _runtime(_FakeExecutor(handler=handle)).run(_bundle(), _attempt())
    assert not os.path.exists(seen["workdir"])


def test_bad_outcome_type_propagates() -> None:
    """executor 返回非过程结果即明确失败，不伪造回执。"""

    def handle(command, workdir):
        return {"exit_code": 0}

    with pytest.raises(TypeError, match="ProcessOutcome"):
        _runtime(_FakeExecutor(handler=handle)).run(_bundle(), _attempt())


def test_receipts_are_unique_and_immutable() -> None:
    """回执唯一且深层不可变（顶层赋值与嵌套改写一律失败）。"""
    runtime = _runtime(_FakeExecutor(handler=_success_handler({})))
    first, _ = runtime.run(_bundle(), _attempt())
    second, _ = runtime.run(_bundle(), _attempt())
    assert first.receipt_id != second.receipt_id
    assert dataclasses.is_dataclass(first)
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.status = "succeeded"  # type: ignore[misc]
    with pytest.raises(TypeError):
        first.policy_evaluated["allow_network"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        first.outputs[0]["path"] = "x"  # type: ignore[index]
    with pytest.raises(TypeError):
        first.resource_usage["k"] = 1  # type: ignore[index]


def test_constructor_and_input_types_are_checked() -> None:
    """构造依赖与 run 输入类型错误即拒绝。"""
    policy = _policy()
    registry = build_executor_registry([_FakeExecutor(handler=lambda c, w: _outcome())])
    with pytest.raises(TypeError, match="ExecutorRegistry"):
        SolverRuntime(object(), policy)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="DeploymentPolicy"):
        SolverRuntime(registry, object())  # type: ignore[arg-type]
    runtime = SolverRuntime(registry, policy)
    with pytest.raises(TypeError, match="SolverBundle"):
        runtime.run(object(), _attempt())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="AttemptContext"):
        runtime.run(_bundle(), object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="executables 不得为空"):
        DeploymentPolicy(
            max_timeout_seconds=1, max_memory_mib=1, max_cpu_cores=1
        )
    with pytest.raises(ValueError, match="attempt 上下文"):
        AttemptContext()
    with pytest.raises(ValueError, match="恰有一个有值"):
        ProcessOutcome(
            termination_reason="exited",
            started_at=_now(),
            ended_at=_now(),
        )
