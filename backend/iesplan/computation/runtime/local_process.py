"""本地进程执行器：受信任 executable 到真实受控进程组的映射与监督。

``LocalProcessProvider`` 是首个真实 ``ExecutorProvider``：把稳定
executable 精确引用映射到部署配置给出的固定绝对二进制路径，以参数
数组启动单个受控进程并监督其完整进程组直至结束。职责止于：精确映射、
无 shell 启动、新进程组、空白基线环境、调用方停止探针、真实超时与组
收口（TERM 整组→固定宽限轮询→KILL 残留→确认组消失）、流式限长采集
stdout/stderr、如实返回 ``ProcessOutcome``（含截断事实）。

监督单位是完整进程组：主进程退出后仍有组成员即立即收口；超时后即使
主进程响应 TERM，宽限期内仍观察整个组；运行中观测到停止（取消优先
于超时迟到）即终止整组。主动 setsid 逃逸进程组不在本层处理范围
（网络/容器隔离由部署时预配置的 runner 容器提供），此处不声称能处理。

OOM 只按本进程 cgroup 的 oom_kill 事件证据标定：被监督者死于 SIGKILL
且监督期间 oom_kill 计数增加才记 ``oom``，其余 SIGKILL 一律记
``signaled``，计数不可读时永不标 OOM。oom_kill 基线在子进程启动前读
取，极快 OOM 不漏窗口。``resource_usage`` 只装 ``observed`` 层（有
oom_kill 增量事实才放 ``oom_kill_delta``，无事实即空映射，不伪造任何
峰值）；容器 ``applied`` 边界由 runner 侧从 cgroup 事实另行合并，部署
需求已在不可变 Bundle 命令中，不在此重复。网络/CPU/容器边界由部署实
施，输出文件总量/数量上限由执行层核对。超时需求取命令声明值；命令未
声明超时则不限时等待。
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.protocols import (
    ProcessOutcome,
    StopProbe,
    check_provider_ref,
    read_stop_reason,
)
from iesplan.computation.runtime.descriptor import ExecutorDescriptor

#: 超时/收口时等进程组退出的固定宽限（秒）；到期即强制终止整组。
TERMINATE_GRACE_SECONDS = 2.0

#: stdout/stderr 每流独立字节上限；超限后继续排空丢弃，避免子进程阻塞。
MAX_STREAM_BYTES = 1_048_576

#: 进程组存活轮询间隔（秒）；组消失即立即继续，非固定等待。
_GROUP_POLL_SECONDS = 0.01

#: 运行中等候时的停止/超时轮询间隔（秒）；停止优先于超时迟到。
_STOP_POLL_SECONDS = 0.05

#: 管道排空单次读取块大小。
_DRAIN_CHUNK_BYTES = 65_536

#: 排空线程回收等待上限（秒；进程组收口后排空必读到 EOF，
#: 触及该上限说明证据不完整，直接抛基础设施异常）。
_DRAIN_JOIN_TIMEOUT_SECONDS = 10.0

#: 首个 HiGHS executable 精确引用（须与生成器声明一致，测试负责盯防）。
HIGHS_EXECUTABLE_REF = "ies.solver.highs@1.7.2"

#: HiGHS 精确版本（由 executable 精确引用唯一导出，不另存第二份）。
HIGHS_VERSION = HIGHS_EXECUTABLE_REF.rsplit("@", 1)[1]

#: HiGHS 适配 executable 在镜像内的固定绝对路径（部署约定，测试盯防）。
HIGHS_ADAPTER_PATH = "/usr/local/bin/ies-highs-solver"

#: readiness 版本上报与离线自检共用超时（秒）。
_READINESS_TIMEOUT_SECONDS = 120.0


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
class ExecutableReadiness:
    """executable 部署就绪探针：精确版本证明 + 最小离线自检。

    ``version`` 为 ``--version`` 必须原样报告的精确版本；``self_test``
    为独立临时目录运行的自检参数前缀（目录由调用方追加），自检须
    exit 0 并在该目录生成 ``output_path`` 声明文件。不做规则 DSL：
    只表达这三项固定约定，不访问 DB/对象存储/网络/项目草稿。
    """

    version: str = ""
    self_test: tuple[str, ...] = ()
    output_path: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise ValueError(
                f"readiness 精确版本须为非空字符串: {self.version!r}"
            )
        if (
            not isinstance(self.self_test, tuple)
            or not self.self_test
            or any(
                not isinstance(item, str) or not item
                for item in self.self_test
            )
        ):
            raise ValueError(
                f"readiness 自检参数须为非空字符串元组: {self.self_test!r}"
            )
        _check_bundle_relative_path(self.output_path, "output_path")


@dataclass(frozen=True, slots=True)
class LocalExecutable:
    """单个 executable 的部署绑定：精确引用、固定绝对二进制路径与参数验证。

    四者收敛在同一绑定内，不设平行映射，避免错配；无验证函数即构造
    拒绝，不默认放行任何形状。``readiness`` 为空表示不做版本与自检
    证明（测试 helper 绑定）；生产求解绑定必须持有真实探针。
    """

    ref: str = ""
    path: str = ""
    argv_validator: Callable[[tuple[str, ...]], None] | None = None
    readiness: ExecutableReadiness | None = None

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
        if self.readiness is not None and not isinstance(
            self.readiness, ExecutableReadiness
        ):
            raise ValueError(f"readiness 须为 ExecutableReadiness: {self.ref!r}")


def _drain_pipe(
    fd: int, chunks: list[bytes], cap: int, truncated: list[bool]
) -> None:
    """排空管道：存满上限后继续读取丢弃，避免子进程写阻塞。

    只要当前块有字节被丢弃（``len(keep) < len(data)``）即置
    ``truncated[0]``，包括随后直接 EOF 的情形，调用方如实记录截断事实。
    """
    total = 0
    while True:
        try:
            data = os.read(fd, _DRAIN_CHUNK_BYTES)
        except OSError:
            break
        if not data:
            break
        keep = data[: cap - total] if total < cap else b""
        if keep:
            chunks.append(keep)
            total += len(keep)
        if len(keep) < len(data):
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


def _reap_group(pgid: int) -> dict[int, int]:
    """回收已归属本进程的同组成员，返回 ``{pid: 原始等待状态}``。

    只回收本进程的子进程（``waitpid(-pgid)`` 限定该组），不碰其他
    进程组的进程；无可回收子进程即返回空映射。调用方负责把主进程
    的状态认领进 ``Popen.returncode``，避免后续 ``wait`` 误报无子进程。
    """
    reaped: dict[int, int] = {}
    while True:
        try:
            pid, status = os.waitpid(-pgid, os.WNOHANG)
        except ChildProcessError:
            return reaped
        if pid == 0:
            return reaped
        reaped[pid] = status


def _adopt_reaped(proc: subprocess.Popen[bytes], reaped: dict[int, int]) -> None:
    """把组回收顺带收割的主进程状态认领进 ``Popen``（仅在其未设置时）。"""
    if proc.returncode is None and proc.pid in reaped:
        proc.returncode = os.waitstatus_to_exitcode(reaped[proc.pid])


def _close_group(pgid: int) -> tuple[bool, dict[int, int]]:
    """收口完整进程组，返回 ``(组是否确认无活动成员, 回收状态)``。

    TERM 整组→宽限轮询→KILL 残留→再确认；轮询中同步回收已归属本进程
    的同组僵尸，避免僵尸撑住存活探测；组消失即立即返回，不固定等待。
    KILL 后宽限结束仍有活动成员返回 False，调用方须抛基础设施异常，
    不得静默返回成功。
    """
    reaped: dict[int, int] = {}
    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
    while time.monotonic() < deadline:
        reaped.update(_reap_group(pgid))
        if not _group_alive(pgid):
            return True, reaped
        time.sleep(_GROUP_POLL_SECONDS)
    reaped.update(_reap_group(pgid))
    if not _group_alive(pgid):
        return True, reaped
    _signal_group(pgid, signal.SIGKILL)
    deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
    while time.monotonic() < deadline:
        reaped.update(_reap_group(pgid))
        if not _group_alive(pgid):
            return True, reaped
        time.sleep(_GROUP_POLL_SECONDS)
    reaped.update(_reap_group(pgid))
    return not _group_alive(pgid), reaped


def _now_iso() -> str:
    """当前 UTC 时间戳（ISO 8601，感知时区）。"""
    return datetime.now(timezone.utc).isoformat()


def _read_oom_kills() -> int | None:
    """读本进程 cgroup v2 的 oom_kill 计数；不可读即返回 None。

    只读 v2（``memory.events``）；v1 或不可读环境返回 None，调用方
    永不标 OOM，不做版本猜测。
    """
    try:
        with open(
            "/sys/fs/cgroup/memory.events", encoding="utf-8"
        ) as handle:
            for line in handle:
                name, _, value = line.partition(" ")
                if name == "oom_kill":
                    return int(value.strip())
    except (OSError, ValueError):
        return None
    return None


def _oom_delta(before: int | None) -> int:
    """监督期间的 oom_kill 增量；不可读或回绕即返回 0（不标 OOM）。"""
    if before is None:
        return 0
    after = _read_oom_kills()
    if after is None or after < before:
        return 0
    return after - before


def _check_readiness(path: str, probe: ExecutableReadiness) -> bool:
    """运行就绪探针：版本原样报告 + 独立临时目录离线自检。

    任一步骤失败（不可执行、版本不符、自检非零、声明文件缺失）
    即返回 False，调用方（注册表）原子拒绝，不抛异常。
    """
    try:
        version = subprocess.run(
            [path, "--version"],
            capture_output=True,
            timeout=_READINESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if version.returncode != 0:
        return False
    reported = version.stdout.decode("utf-8", errors="replace").strip()
    if reported != probe.version:
        return False
    tmpdir = tempfile.mkdtemp(prefix="ies-readiness-")
    try:
        try:
            proved = subprocess.run(
                [path, *probe.self_test, tmpdir],
                capture_output=True,
                timeout=_READINESS_TIMEOUT_SECONDS,
                cwd=tmpdir,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if proved.returncode != 0:
            return False
        return os.path.isfile(os.path.join(tmpdir, probe.output_path))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


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
        """绑定二进制可执行，且带探针的绑定通过版本与离线自检。"""
        return all(
            self._binary_ready(item) for item in self._bindings.values()
        )

    @staticmethod
    def _binary_ready(item: LocalExecutable) -> bool:
        """单个绑定就绪：固定路径可执行，探针版本与自检通过。"""
        if not (os.path.isfile(item.path) and os.access(item.path, os.X_OK)):
            return False
        probe = item.readiness
        if probe is None:
            return True
        return _check_readiness(item.path, probe)

    @staticmethod
    def _close_or_raise(
        pgid: int, proc: subprocess.Popen[bytes]
    ) -> None:
        """收口进程组并认领主进程状态；组未消失即抛基础设施异常。"""
        gone, reaped = _close_group(pgid)
        _adopt_reaped(proc, reaped)
        if not gone:
            raise ComputationUnavailableError(
                "进程组未能终止：KILL 后仍有活动成员",
                reason="executor-group-not-terminated",
            )

    def execute(
        self,
        command: Mapping[str, object],
        workdir: str,
        *,
        stop: StopProbe | None = None,
    ) -> ProcessOutcome:
        """启动受控进程组并监督至组消失，返回最小过程结果。

        精确引用查固定二进制（禁止 PATH 发现）；参数验证来自绑定，
        在启动前拒绝。子进程工作目录为 ``workdir`` 下命令声明目录；
        环境只含显式基线与命令环境键，不继承宿主环境。``stop`` 为调用方
        停止探针：启动前已停止即不启动进程；运行中首次观测到停止即终止
        整组并记 ``cancelled``/``fenced``（停止优先于超时迟到；实际
        回收到的退出码或信号如实保留，迟到退出码不改写停止主状态）。
        无 ``timeout_seconds`` 声明时，有停止探针仍有界轮询等待；
        监督单位是完整进程组：主进程退出后仍有成员即
        强制收口并记 ``group_terminated``（保留主进程实际退出码，不伪造
        信号）；超时后即使主进程响应 TERM，宽限期内仍观察整个组。二进制
        无法启动、进程组未能终止、排空证据不完整等无法建立可信执行事实
        的错误抛基础设施异常，不伪造。
        """
        if stop is not None and not callable(stop):
            raise TypeError(
                f"stop 须为停止探针可调用对象，得到 {type(stop).__name__}"
            )
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
        pre_stop = read_stop_reason(stop)
        if pre_stop is not None:
            raise ComputationUnavailableError(
                f"启动前已停止（{pre_stop}），未启动进程",
                reason=pre_stop,
            )
        started_at = _now_iso()
        # oom_kill 基线先于子进程启动读取：极快 OOM 若死在基线之后，
        # 增量仍可观测；读不到即 None，调用链永不标 OOM。
        oom_before = _read_oom_kills()
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
        stopped: str | None = None
        timed_out = False
        group_closed = False
        deadline = (
            time.monotonic() + float(timeout)  # type: ignore[arg-type]
            if timeout is not None
            else None
        )
        while True:
            hit = read_stop_reason(stop)
            if hit is not None:
                stopped = hit
                break
            if deadline is None and stop is None:
                returncode = proc.wait()
                break
            remaining = (
                None if deadline is None else deadline - time.monotonic()
            )
            if remaining is not None and remaining <= 0:
                hit = read_stop_reason(stop)
                if hit is not None:
                    stopped = hit
                else:
                    timed_out = True
                break
            try:
                returncode = proc.wait(
                    timeout=_STOP_POLL_SECONDS
                    if remaining is None or remaining > _STOP_POLL_SECONDS
                    else remaining
                )
                break
            except subprocess.TimeoutExpired:
                continue
        if stopped is not None:
            self._close_or_raise(pgid, proc)
            returncode = proc.wait()
        elif timed_out:
            self._close_or_raise(pgid, proc)
            returncode = proc.wait()
        elif _group_alive(pgid):
            self._close_or_raise(pgid, proc)
            group_closed = True
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
        oom_delta = _oom_delta(oom_before)
        if returncode < 0:
            exit_code: int | None = None
            signal_number: int | None = -returncode
        else:
            exit_code = returncode
            signal_number = None
        if stopped is not None:
            # 运行中停止保留实际回收到的退出码或信号，原因仍记停止；
            # 迟到退出码不改写停止主状态。
            termination_reason = stopped
        elif timed_out:
            termination_reason = "timeout"
        elif group_closed:
            termination_reason = "group_terminated"
        elif exit_code is not None:
            termination_reason = "exited"
        elif signal_number == signal.SIGKILL and oom_delta > 0:
            # 仅凭监督期间的 oom_kill 增量标 OOM；其余 SIGKILL 一律
            # 记 signaled，不猜测。
            termination_reason = "oom"
        else:
            termination_reason = "signaled"
        resource_usage: dict[str, object] = {}
        if oom_delta > 0:
            resource_usage["observed"] = {"oom_kill_delta": oom_delta}
        return ProcessOutcome(
            exit_code=exit_code,
            signal_number=signal_number,
            termination_reason=termination_reason,
            started_at=started_at,
            ended_at=ended_at,
            stdout=b"".join(out_chunks),
            stderr=b"".join(err_chunks),
            stdout_truncated=out_truncated[0],
            stderr_truncated=err_truncated[0],
            resource_usage=resource_usage,
        )


__all__ = [
    "HIGHS_ADAPTER_PATH",
    "HIGHS_EXECUTABLE_REF",
    "HIGHS_VERSION",
    "MAX_STREAM_BYTES",
    "TERMINATE_GRACE_SECONDS",
    "ExecutableReadiness",
    "LocalExecutable",
    "LocalProcessProvider",
    "validate_highs_arguments",
]
