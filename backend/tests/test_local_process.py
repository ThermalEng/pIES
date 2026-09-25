"""本地进程执行器测试（真实进程监督；helper 只活在 tests）。

覆盖：executable 精确映射与启动前拒绝；HiGHS argv 与生成器形状一致；
argv 逐项原样传递且 shell 元字符不执行；非点 working_directory；
空白环境不泄密；正常/非零/信号退出；超时 TERM 后退出与忽略 TERM 后
KILL；派生子进程整组回收；stdout/stderr 流式限长且大输出不死锁；
Runtime 超时/信号回执。不注册 bootstrap/Worker，不碰生产隔离断言。
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time

import pytest

from iesplan.computation import (
    AttemptContext,
    ComputationUnavailableError,
    DeploymentPolicy,
    ExecutorDescriptor,
    ExecutorProvider,
    LocalExecutable,
    LocalProcessProvider,
    ProcessOutcome,
    SolverBundle,
    SolverBundleManifest,
    SolverRuntime,
    build_executor_registry,
)
from iesplan.computation.generators.highs_milp import (
    PROBLEM_PATH,
    SOLUTION_PATH,
)
from iesplan.computation.generators.highs_milp import (
    SOLVER_REF as GENERATOR_SOLVER_REF,
)
from iesplan.computation.protocols import TERMINATION_REASONS
from iesplan.computation.runtime.local_process import (
    HIGHS_EXECUTABLE_REF,
    MAX_STREAM_BYTES,
    TERMINATE_GRACE_SECONDS,
    validate_highs_arguments,
)

HELPER = os.path.join(os.path.dirname(__file__), "local_exec_probe.py")
HELPER_REF = "t.helper.exec@1.0.0"
EXECUTOR_ID = "t.local-process"
EXECUTOR_VERSION = "1.0.0"
EXECUTOR_REF = f"{EXECUTOR_ID}@{EXECUTOR_VERSION}"


def _helper_validator(arguments: tuple[str, ...]) -> None:
    """测试 helper 参数门禁：首项须为已知 helper 绝对路径，余项为字符串。"""
    args = tuple(arguments)
    if not args or args[0] != HELPER:
        raise ValueError(f"测试 helper 首参数须为 {HELPER!r}")
    for item in args[1:]:
        if not isinstance(item, str):
            raise ValueError(f"helper 参数须为字符串: {item!r}")


def _binding(path: str = "") -> LocalExecutable:
    """测试绑定：helper 引用到给定二进制，参数门禁收敛在同一绑定。"""
    return LocalExecutable(
        ref=HELPER_REF,
        path=path or sys.executable,
        argv_validator=_helper_validator,
    )


def _descriptor() -> ExecutorDescriptor:
    return ExecutorDescriptor(
        executor_id=EXECUTOR_ID,
        version=EXECUTOR_VERSION,
        executables=(HELPER_REF,),
    )


def _provider(**kwargs) -> LocalProcessProvider:
    """测试 provider：helper 引用到当前解释器绝对路径，无生产默认。"""
    return LocalProcessProvider(
        _descriptor(),
        (_binding(),),
        base_environ=kwargs.get("base_environ", {}),
    )


def _run(provider, arguments, workdir, **command_overrides) -> object:
    command: dict[str, object] = {
        "executor": EXECUTOR_REF,
        "executable": HELPER_REF,
        "arguments": [HELPER, *arguments],
        "timeout_seconds": 30,
        "network": False,
        "success_exit_codes": [0],
    }
    command.update(command_overrides)
    return provider.execute(command, str(workdir))


def test_executable_binding_requires_absolute_path() -> None:
    """二进制映射须为固定绝对路径；引用与生成器声明一致由测试盯防。"""
    assert HIGHS_EXECUTABLE_REF == GENERATOR_SOLVER_REF
    with pytest.raises(ValueError, match="绝对路径"):
        LocalExecutable(ref=HELPER_REF, path="python3")
    with pytest.raises(ValueError, match="形态"):
        LocalExecutable(ref="not-a-ref", path=sys.executable)
    descriptor = ExecutorDescriptor(
        executor_id=EXECUTOR_ID,
        version=EXECUTOR_VERSION,
        executables=(HELPER_REF,),
    )
    with pytest.raises(ValueError, match="恰好一致"):
        LocalProcessProvider(descriptor, ())
    assert isinstance(_provider(), ExecutorProvider)


def test_unmapped_executable_rejected_before_spawn(tmp_path) -> None:
    """未映射引用报基础设施不可用，不起进程。"""
    provider = _provider()
    with pytest.raises(ComputationUnavailableError) as exc_info:
        provider.execute(
            {"executor": EXECUTOR_REF, "executable": "t.other@1.0.0",
             "arguments": []},
            str(tmp_path),
        )
    assert exc_info.value.reason == "unknown-executable"


def test_binding_convergence_and_duplicates() -> None:
    """绑定缺验证函数或引用重复即构造拒绝，不形成错配 provider。"""
    with pytest.raises(ValueError, match="缺参数验证绑定"):
        LocalExecutable(ref=HELPER_REF, path=sys.executable)
    with pytest.raises(ValueError, match="重复"):
        LocalProcessProvider(_descriptor(), (_binding(), _binding()))
    assert _provider().available() is True


def test_available_checks_bound_binaries(tmp_path) -> None:
    """available 逐项检查常规可执行文件；缺失/不可执行即不可用。"""
    missing = LocalProcessProvider(
        _descriptor(),
        (LocalExecutable(
            ref=HELPER_REF, path="/nonexistent/bin-probe",
            argv_validator=_helper_validator,
        ),),
    )
    assert missing.available() is False
    plain = tmp_path / "plain-file"
    plain.write_bytes(b"x")
    plain.chmod(0o644)
    nonexec = LocalProcessProvider(
        _descriptor(),
        (_binding(path=str(plain)),),
    )
    assert nonexec.available() is False


def test_highs_argv_shape_matches_generator() -> None:
    """HiGHS 七参数形状与生成器当前 argv 一致；变体在启动前拒绝。"""
    good = (
        PROBLEM_PATH, "--solution_file", SOLUTION_PATH,
        "--time_limit", "600", "--random_seed", "0",
    )
    validate_highs_arguments(good)
    bad_cases = [
        good[:6],
        (*good, "extra"),
        ("/abs/problem.mps", *good[1:]),
        ("../evil.mps", *good[1:]),
        ("@resp.txt", *good[1:]),
        ("-x", *good[1:]),
        (good[0], "--solve", *good[2:]),
        (good[0], good[1], "/abs/out.sol", *good[3:]),
        (*good[:4], "0", *good[5:]),
        (*good[:4], "x", *good[5:]),
        (*good[:6], "-1"),
    ]
    for bad in bad_cases:
        with pytest.raises(ValueError):
            validate_highs_arguments(bad)


def test_argv_passed_verbatim_without_shell(tmp_path) -> None:
    """argv 逐项原样传递；shell 元字符不执行，不落盘。"""
    evil = "$(touch /tmp/pies-evil-probe)"
    args = [evil, "a;b", "x|y", "`id`", "$HOME", "--flag=value with space"]
    outcome = _run(_provider(), ["echo", *args], tmp_path)
    assert outcome.exit_code == 0
    assert json.loads(outcome.stdout.decode("utf-8")) == args
    assert not os.path.exists("/tmp/pies-evil-probe")


def test_non_dot_working_directory(tmp_path) -> None:
    """子进程工作目录取 Bundle 根下声明目录。"""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outcome = _run(
        _provider(), ["cwd"], tmp_path, working_directory="run",
    )
    assert outcome.exit_code == 0
    assert outcome.stdout.decode("utf-8") == str(run_dir)


def _assert_no_host_leak(env: dict) -> None:
    """宿主环境标记一律不得出现（子进程自身建立的变量不归本层管）。"""
    for leaked in ("PIES_LEAK_SENTINEL", "PATH", "HOME", "IESPLAN_SECRET_KEY"):
        assert leaked not in env


def test_blank_environment_does_not_leak(tmp_path) -> None:
    """环境从空白基线建立，只含显式基线与命令键，不继承宿主环境。"""
    os.environ["PIES_LEAK_SENTINEL"] = "must-not-leak"
    try:
        outcome = _run(
            _provider(base_environ={"LC_ALL": "C"}),
            ["env"],
            tmp_path,
            environment={"T_MODE": "test"},
        )
    finally:
        del os.environ["PIES_LEAK_SENTINEL"]
    assert outcome.exit_code == 0
    env = json.loads(outcome.stdout.decode("utf-8"))
    assert env["LC_ALL"] == "C"
    assert env["T_MODE"] == "test"
    _assert_no_host_leak(env)


def test_production_default_environment_is_blank(tmp_path) -> None:
    """生产默认基线为空；命令无环境即空环境运行（解释器自身变量除外）。"""
    outcome = _run(_provider(), ["env"], tmp_path)
    assert outcome.exit_code == 0
    env = json.loads(outcome.stdout.decode("utf-8"))
    assert set(env) <= {"LC_CTYPE"}
    _assert_no_host_leak(env)


def test_normal_exit_captures_streams(tmp_path) -> None:
    """正常退出如实记录退出码与双流字节。"""
    outcome = _run(_provider(), ["big-output", "100"], tmp_path)
    assert outcome.exit_code == 0
    assert outcome.signal_number is None
    assert outcome.termination_reason == "exited"
    assert outcome.stdout == b"o" * 100
    assert outcome.stderr == b"e" * 100
    assert outcome.stdout_truncated is False
    assert outcome.stderr_truncated is False
    assert outcome.started_at.endswith("+00:00")
    assert outcome.started_at <= outcome.ended_at


def test_nonzero_exit(tmp_path) -> None:
    """非零退出如实记录退出码，不伪装。"""
    outcome = _run(_provider(), ["exit", "3"], tmp_path)
    assert outcome.exit_code == 3
    assert outcome.signal_number is None
    assert outcome.termination_reason == "exited"


def test_signal_termination(tmp_path) -> None:
    """信号终止如实记录信号，退出码为空。"""
    outcome = _run(_provider(), ["sigterm-self"], tmp_path)
    assert outcome.exit_code is None
    assert outcome.signal_number == signal.SIGTERM
    assert outcome.termination_reason == "signaled"


def test_timeout_term_then_exit_is_still_timeout(tmp_path) -> None:
    """超时后进程自行退出（即使 exit 0）仍记 timeout，不伪装成功。"""
    outcome = _run(
        _provider(), ["sleep", "30", "trap-exit=0"], tmp_path,
        timeout_seconds=2,
    )
    assert outcome.exit_code == 0
    assert outcome.termination_reason == "timeout"


def test_timeout_ignore_term_escalates_to_kill(tmp_path) -> None:
    """忽略 TERM 则宽限后 KILL 整组；如实记录 SIGKILL 与超时。"""
    start = time.monotonic()
    outcome = _run(
        _provider(), ["sleep", "30", "ignore-term"], tmp_path,
        timeout_seconds=2,
    )
    elapsed = time.monotonic() - start
    assert outcome.exit_code is None
    assert outcome.signal_number == signal.SIGKILL
    assert outcome.termination_reason == "timeout"
    assert elapsed >= 2.0
    assert elapsed < 2.0 + TERMINATE_GRACE_SECONDS + 10.0


def _assert_pid_gone(pid: int, label: str) -> None:
    """确认进程已消失（可收割的僵尸由本进程回收后确认）。"""
    deadline = time.monotonic() + 10.0
    while True:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        if time.monotonic() > deadline:
            pytest.fail(f"{label}未被回收: {pid}")
        time.sleep(0.05)


def test_spawned_child_reaped_with_group(tmp_path) -> None:
    """派生子进程随整组终止被回收，不留孤儿。"""
    outcome = _run(
        _provider(), ["spawn-child", "30", "60"], tmp_path,
        timeout_seconds=2,
    )
    assert outcome.termination_reason == "timeout"
    child_pid = json.loads(outcome.stdout.decode("utf-8"))["child_pid"]
    _assert_pid_gone(child_pid, "子进程")


def test_parent_exit_leftover_child_closed_before_return(tmp_path) -> None:
    """父正常退出、同组子进程长睡且持有管道：返回前组已收口，无固定等待。"""
    start = time.monotonic()
    outcome = _run(_provider(), ["spawn-and-exit", "30"], tmp_path)
    elapsed = time.monotonic() - start
    assert outcome.exit_code == 0
    assert outcome.termination_reason == "exited"
    child_pid = json.loads(outcome.stdout.decode("utf-8"))["child_pid"]
    _assert_pid_gone(child_pid, "遗留子进程")
    assert elapsed < 15.0


def test_timeout_parent_term_child_ignored_closed_before_return(tmp_path) -> None:
    """超时后父响应 TERM、子忽略 TERM：宽限后子被 KILL，无固定排流等待。"""
    start = time.monotonic()
    outcome = _run(
        _provider(), ["spawn-trap", "30", "60"], tmp_path,
        timeout_seconds=2,
    )
    elapsed = time.monotonic() - start
    assert outcome.termination_reason == "timeout"
    assert outcome.exit_code == 0
    child_pid = json.loads(outcome.stdout.decode("utf-8"))["child_pid"]
    _assert_pid_gone(child_pid, "忽略 TERM 的子进程")
    assert elapsed >= 2.0
    assert elapsed < 2.0 + TERMINATE_GRACE_SECONDS + 10.0


def test_stream_cap_without_deadlock(tmp_path) -> None:
    """双流各 3MiB 输出限长且完成，不死锁。"""
    outcome = _run(_provider(), ["big-output", str(3 * 1024 * 1024)], tmp_path)
    assert outcome.exit_code == 0
    assert len(outcome.stdout) == MAX_STREAM_BYTES
    assert len(outcome.stderr) == MAX_STREAM_BYTES
    assert outcome.stdout == b"o" * MAX_STREAM_BYTES
    assert outcome.stderr == b"e" * MAX_STREAM_BYTES
    assert outcome.stdout_truncated is True
    assert outcome.stderr_truncated is True


def test_start_failure_is_infrastructure_error(tmp_path) -> None:
    """二进制无法启动抛基础设施异常，不伪造回执。"""
    provider = LocalProcessProvider(
        _descriptor(),
        (LocalExecutable(
            ref=HELPER_REF, path="/nonexistent/bin-probe",
            argv_validator=_helper_validator,
        ),),
    )
    with pytest.raises(ComputationUnavailableError) as exc_info:
        _run(provider, ["echo"], tmp_path)
    assert exc_info.value.reason == "executor-start-failed"


def _runtime_bundle(outputs=None, **command_overrides) -> SolverBundle:
    command: dict[str, object] = {
        "executor": EXECUTOR_REF,
        "executable": HELPER_REF,
        "arguments": [HELPER, "sleep", "30"],
        "timeout_seconds": 2,
        "network": False,
        "success_exit_codes": [0],
    }
    command.update(command_overrides)
    manifest = SolverBundleManifest(
        bundle={
            "generator": "acme.generator@1.0.0",
            "solver": "ies.solver.highs@1.7.2",
        },
        inputs=[{"path": "input/problem.mps", "media_type": "application/mps"}],
        command=command,
        outputs=outputs
        if outputs is not None
        else [
            {
                "path": "output/solution.json",
                "media_type": "application/json",
                "required": False,
            }
        ],
        result_adapter="acme.result-adapter@1.0.0",
    )
    return SolverBundle(
        manifest=manifest, files={"input/problem.mps": b"probe"}
    )


def _runtime(provider) -> SolverRuntime:
    policy = DeploymentPolicy(
        executables=(HELPER_REF,),
        environment_vars=(),
        max_timeout_seconds=600,
        max_memory_mib=4096,
        max_cpu_cores=4,
        allow_network=False,
    )
    return SolverRuntime(build_executor_registry([provider]), policy)


def _attempt() -> AttemptContext:
    return AttemptContext(
        attempt_id="attempt-9",
        bundle_id="bundle-9",
        assembly_id="assembly-9",
        calculation_config_id="config-9",
    )


def test_termination_reasons_are_closed_and_consistent() -> None:
    """终止原因封闭且与退出事实一致；伪造 timed_out 即拒绝。"""
    assert set(TERMINATION_REASONS) == {"exited", "signaled", "timeout"}
    base = {
        "started_at": "2026-09-24T00:00:00Z",
        "ended_at": "2026-09-24T00:00:01Z",
    }
    with pytest.raises(ValueError, match="终止原因非法"):
        ProcessOutcome(exit_code=0, termination_reason="cancelled", **base)
    with pytest.raises(ValueError, match="须对应 exit_code"):
        ProcessOutcome(
            exit_code=None, signal_number=9, termination_reason="exited",
            **base,
        )
    with pytest.raises(ValueError, match="须对应 signal_number"):
        ProcessOutcome(exit_code=3, termination_reason="signaled", **base)
    timed = ProcessOutcome(exit_code=0, termination_reason="timeout", **base)
    assert timed.termination_reason == "timeout"


def test_runtime_reports_timeout_receipt() -> None:
    """Runtime 基于真实超时过程签发 timed_out 回执。"""
    receipt, outputs = _runtime(_provider()).run(
        _runtime_bundle(), _attempt()
    )
    assert receipt.status == "timed_out"
    assert receipt.termination_reason == "timeout"
    assert receipt.error
    assert receipt.executor_ref == EXECUTOR_REF
    assert receipt.executable_ref == HELPER_REF
    assert receipt.attempt_id == "attempt-9"
    assert receipt.started_at.endswith("Z") and receipt.ended_at.endswith("Z")
    assert receipt.stdout_truncated is False
    assert receipt.stderr_truncated is False
    assert outputs == {}


def test_runtime_timeout_keeps_priority_over_output_violations() -> None:
    """超时且遗留未声明文件/缺必需输出：主状态仍为 timed_out，问题追加。"""
    receipt, outputs = _runtime(_provider()).run(
        _runtime_bundle(
            arguments=[HELPER, "sleep-with-extra", "output/sneaky.bin", "30"],
            outputs=[
                {
                    "path": "output/solution.json",
                    "media_type": "application/json",
                    "required": True,
                }
            ],
        ),
        _attempt(),
    )
    assert receipt.status == "timed_out"
    assert "超时" in receipt.error
    assert "sneaky.bin" in receipt.error
    assert "必需输出缺失" in receipt.error
    assert outputs == {}


def test_runtime_reports_signal_receipt() -> None:
    """Runtime 基于真实信号过程签发 process_failed 回执。"""
    receipt, _ = _runtime(_provider()).run(
        _runtime_bundle(arguments=[HELPER, "sigterm-self"]), _attempt()
    )
    assert receipt.status == "process_failed"
    assert receipt.exit_code is None
    assert receipt.signal_number == signal.SIGTERM
    assert receipt.termination_reason == "signaled"
