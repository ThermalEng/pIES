"""本地进程执行器：受信任 executable 到真实受控进程组的映射与监督。

``LocalProcessProvider`` 是首个真实 ``ExecutorProvider``：把稳定
executable 精确引用映射到部署配置给出的固定绝对二进制路径，以参数
数组启动单个受控进程并监督其完整进程组直至结束。职责止于：精确映射、
无 shell 启动、新进程组、空白基线环境、真实超时与组收口（TERM 整组→
固定宽限轮询→KILL 残留→确认组消失）、流式限长采集 stdout/stderr、
如实返回 ``ProcessOutcome``（含截断事实）。

监督单位是完整进程组：主进程退出后仍有组成员即立即收口；超时后即使
主进程响应 TERM，宽限期内仍观察整个组。主动 setsid 逃逸进程组不在
本层处理范围（网络/容器/cgroup 隔离属后续切片），此处不声称能处理。

明确不实施：网络隔离、容器隔离、CPU/内存/OOM 判定、显式取消与
Worker 取消/fencing（``resource_usage`` 为空映射，回执与代码均不声称
已实施）；输出文件总量/数量上限。超时需求取命令声明值；命令未声明
超时则不限时等待。
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.protocols import ProcessOutcome, check_provider_ref
from iesplan.computation.runtime.descriptor import ExecutorDescriptor

#: 超时/收口时等进程组退出的固定宽限（秒）；到期即强制终止整组。
TERMINATE_GRACE_SECONDS = 2.0

#: stdout/stderr 每流独立字节上限；超限后继续排空丢弃，避免子进程阻塞。
MAX_STREAM_BYTES = 1_048_576

#: 进程组存活轮询间隔（秒）；组消失即立即继续，非固定等待。
_GROUP_POLL_SECONDS = 0.01

#: 管道排空单次读取块大小。
_DRAIN_CHUNK_BYTES = 65_536

#: 排空线程回收等待上限（秒；进程组收口后排空必读到 EOF，
#: 触及该上限说明证据不完整，直接抛基础设施异常）。
_DRAIN_JOIN_TIMEOUT_SECONDS = 10.0

#: 首个 HiGHS executable 精确引用（须与生成器声明一致，测试负责盯防）。
HIGHS_EXECUTABLE_REF = "ies.solver.highs@1.7.2"


def _check_bundle_relative_path(value: object, name: str) -> str:
    """校验 Bundle 内规范相对路径（非空、非绝对、无 ``..``、非响应文件）。"""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} 须为非空路径字符串: {value!r}")
    if value.startswith("/") or value.startswith("@") or value.startswith("-"):
        raise ValueError(f"{name} 路径形态非法: {value!r}")
    if ".." in value.split("/"):
        raise ValueError(f"{name} 路径越界: {value!r}")
    return value


def _check_int_text(value: object, name: str, minimum: int) -> None:
    """校验整数文本参数（字符串形态，下限约束）。"""
    if not isinstance(value, str):
        raise ValueError(f"{name} 须为整数文本: {value!r}")
    try:
        number = int(value, 10)
    except ValueError:
        raise ValueError(f"{name} 须为整数文本: {value!r}") from None
    if number < minimum:
        raise ValueError(f"{name} 不得小于 {minimum}: {value!r}")


def validate_highs_arguments(arguments: tuple[str, ...]) -> None:
    """校验 HiGHS argv 与生成器当前形状一致（位置固定的七参数）。

    形状：``[问题路径, --solution_file, 解路径, --time_limit, 秒数,
    --random_seed, 种子]``；路径参数只允许 Bundle 内规范相对路径，
    禁止响应文件形态；非路径参数按当前真实 HiGHS 选项检查。不建立
    通用规则 DSL；生成器形状变化时同步更新本函数与对应测试。
    """
    args = tuple(arguments)
    if len(args) != 7:
        raise ValueError(f"HiGHS 参数须恰为七项，得到 {len(args)} 项")
    _check_bundle_relative_path(args[0], "arguments[0]")
    if args[1] != "--solution_file":
        raise ValueError(f"arguments[1] 须为 --solution_file: {args[1]!r}")
    _check_bundle_relative_path(args[2], "arguments[2]")
    if args[3] != "--time_limit":
        raise ValueError(f"arguments[3] 须为 --time_limit: {args[3]!r}")
    _check_int_text(args[4], "arguments[4]", 1)
    if args[5] != "--random_seed":
        raise ValueError(f"arguments[5] 须为 --random_seed: {args[5]!r}")
    _check_int_text(args[6], "arguments[6]", 0)


@dataclass(frozen=True, slots=True)
class LocalExecutable:
    """单个 executable 的部署绑定：精确引用、固定绝对二进制路径与参数验证。

    三者收敛在同一绑定内，不设平行映射，避免错配；无验证函数即构造
    拒绝，不默认放行任何形状。
    """

    ref: str = ""
    path: str = ""
    argv_validator: Callable[[tuple[str, ...]], None] | None = None

    def __post_init__(self) -> None:
        check_provider_ref(self.ref, "executable")
        if not isinstance(self.path, str) or not self.path:
            raise ValueError(f"二进制路径须为非空字符串: {self.path!r}")
        if not os.path.isabs(self.path):
            raise ValueError(
                f"二进制路径须为固定绝对路径，禁止 PATH 发现: {self.path!r}"
            )
        if self.argv_validator is None or not callable(self.argv_validator):
            raise ValueError(f"executable 缺参数验证绑定: {self.ref!r}")


def _drain_pipe(
    fd: int, chunks: list[bytes], cap: int, truncated: list[bool]
) -> None:
    """排空管道：存满上限后继续读取丢弃，避免子进程写阻塞。

    实际发生丢弃时置 ``truncated[0]``，调用方如实记录截断事实。
    """
    total = 0
    while True:
        try:
            data = os.read(fd, _DRAIN_CHUNK_BYTES)
        except OSError:
            break
        if not data:
            break
        if total < cap:
            keep = data[: cap - total]
            chunks.append(keep)
            total += len(keep)
        else:
            truncated[0] = True


def _signal_group(pgid: int, sig: int) -> bool:
    """向进程组发信号；组已消失返回 False（自行退出，无需终止）。"""
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    return True


def _group_alive(pgid: int) -> bool:
    """进程组是否仍有存活成员（信号 0 探测；无权限即视为存活）。"""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _close_group(pgid: int) -> None:
    """收口完整进程组：TERM 整组→宽限轮询→KILL 残留→确认组消失。

    组消失即立即返回，不固定等待；KILL 后仍存活（如不可杀状态）则
    直接返回，调用方按证据完整性决定是否抛异常。
    """
    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
    while _group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(_GROUP_POLL_SECONDS)
    if _group_alive(pgid):
        _signal_group(pgid, signal.SIGKILL)
        deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
        while _group_alive(pgid) and time.monotonic() < deadline:
            time.sleep(_GROUP_POLL_SECONDS)


def _now_iso() -> str:
    """当前 UTC 时间戳（ISO 8601，感知时区）。"""
    return datetime.now(timezone.utc).isoformat()


class LocalProcessProvider:
    """本地受信任进程执行器（部署配置持有，组合根注册）。

    构造绑定 ``descriptor``、精确引用到固定二进制与参数验证的收敛
    绑定，以及可选空白基线环境。``execute`` 只做本 executable 边界
    必需的门禁，不重复校验已类型化 Bundle，不解释业务语义。
    """

    __slots__ = ("_descriptor", "_bindings", "_base_environ")

    def __init__(
        self,
        descriptor: ExecutorDescriptor,
        executables: tuple[LocalExecutable, ...] = (),
        *,
        base_environ: Mapping[str, str] = {},
    ) -> None:
        if not isinstance(descriptor, ExecutorDescriptor):
            raise TypeError(
                "descriptor 须为 ExecutorDescriptor，"
                f"得到 {type(descriptor).__name__}"
            )
        refs = [item.ref for item in executables]
        if len(set(refs)) != len(refs):
            raise ValueError(f"executable 绑定引用重复: {refs!r}")
        bindings = {item.ref: item for item in executables}
        if set(bindings) != set(descriptor.executables):
            raise ValueError(
                "绑定引用须与描述符 executables 恰好一致: "
                f"绑定 {sorted(bindings)!r} x 描述符 "
                f"{sorted(descriptor.executables)!r}"
            )
        if not isinstance(base_environ, Mapping) or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            for key, value in base_environ.items()
        ):
            raise TypeError("base_environ 须为字符串映射")
        self._descriptor = descriptor
        self._bindings = bindings
        self._base_environ = dict(base_environ)

    @property
    def descriptor(self) -> ExecutorDescriptor:
        """公开能力描述符（与 ``ref`` 单一来源）。"""
        return self._descriptor

    @property
    def ref(self) -> str:
        """执行器精确引用（与描述符单一来源）。"""
        return self._descriptor.ref

    def available(self) -> bool:
        """逐项检查绑定二进制为常规可执行文件（精确版本自检归后续）。"""
        return all(
            os.path.isfile(item.path) and os.access(item.path, os.X_OK)
            for item in self._bindings.values()
        )

    def execute(
        self, command: Mapping[str, object], workdir: str
    ) -> ProcessOutcome:
        """启动受控进程组并监督至组消失，返回最小过程结果。

        精确引用查固定二进制（禁止 PATH 发现）；参数验证来自绑定，
        在启动前拒绝。子进程工作目录为 ``workdir`` 下命令声明目录；
        环境只含显式基线与命令环境键，不继承宿主环境。监督单位是
        完整进程组：主进程退出后仍有成员即收口；超时后即使主进程
        响应 TERM，宽限期内仍观察整个组。二进制无法启动、排空证据
        不完整等无法建立可信执行事实的错误抛基础设施异常，不伪造。
        """
        executable_ref = str(command["executable"])
        try:
            binding = self._bindings[executable_ref]
        except KeyError:
            raise ComputationUnavailableError(
                f"executable 未映射到本地二进制: {executable_ref!r}",
                reason="unknown-executable",
            ) from None
        assert binding.argv_validator is not None
        binding.argv_validator(tuple(command["arguments"]))  # type: ignore[arg-type]
        run_cwd = os.path.normpath(
            os.path.join(workdir, str(command.get("working_directory", ".")))
        )
        environ = dict(self._base_environ)
        environ.update(dict(command.get("environment", {})))  # type: ignore[arg-type]
        argv = [binding.path, *command["arguments"]]  # type: ignore[misc]
        timeout = command.get("timeout_seconds")
        started_at = _now_iso()
        try:
            proc = subprocess.Popen(
                argv,
                cwd=run_cwd,
                env=environ,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise ComputationUnavailableError(
                f"本地进程无法启动: {exc}",
                reason="executor-start-failed",
            ) from exc
        assert proc.stdout is not None and proc.stderr is not None
        pgid = proc.pid
        out_chunks: list[bytes] = []
        err_chunks: list[bytes] = []
        out_truncated: list[bool] = [False]
        err_truncated: list[bool] = [False]
        out_drain = threading.Thread(
            target=_drain_pipe,
            args=(proc.stdout.fileno(), out_chunks, MAX_STREAM_BYTES, out_truncated),
            daemon=True,
        )
        err_drain = threading.Thread(
            target=_drain_pipe,
            args=(proc.stderr.fileno(), err_chunks, MAX_STREAM_BYTES, err_truncated),
            daemon=True,
        )
        out_drain.start()
        err_drain.start()
        timed_out = False
        try:
            returncode = (
                proc.wait(timeout=timeout)  # type: ignore[arg-type]
                if timeout is not None
                else proc.wait()
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            _close_group(pgid)
            returncode = proc.wait()
        else:
            if _group_alive(pgid):
                _close_group(pgid)
        out_drain.join(timeout=_DRAIN_JOIN_TIMEOUT_SECONDS)
        err_drain.join(timeout=_DRAIN_JOIN_TIMEOUT_SECONDS)
        evidence_complete = not (out_drain.is_alive() or err_drain.is_alive())
        try:
            proc.stdout.close()
        except OSError:
            pass
        try:
            proc.stderr.close()
        except OSError:
            pass
        if not evidence_complete:
            raise ComputationUnavailableError(
                "执行证据不完整：日志排空线程未结束",
                reason="executor-evidence-incomplete",
            )
        ended_at = _now_iso()
        if returncode < 0:
            exit_code: int | None = None
            signal_number: int | None = -returncode
        else:
            exit_code = returncode
            signal_number = None
        return ProcessOutcome(
            exit_code=exit_code,
            signal_number=signal_number,
            termination_reason=(
                "timeout"
                if timed_out
                else ("exited" if exit_code is not None else "signaled")
            ),
            started_at=started_at,
            ended_at=ended_at,
            stdout=b"".join(out_chunks),
            stderr=b"".join(err_chunks),
            stdout_truncated=out_truncated[0],
            stderr_truncated=err_truncated[0],
        )


__all__ = [
    "HIGHS_EXECUTABLE_REF",
    "MAX_STREAM_BYTES",
    "TERMINATE_GRACE_SECONDS",
    "LocalExecutable",
    "LocalProcessProvider",
    "validate_highs_arguments",
]
