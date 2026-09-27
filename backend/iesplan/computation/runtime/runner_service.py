"""隔离 solver runner 服务（运行在部署时预配置的隔离容器内）。

本模块只在 runner 容器内运行：启动时先做隔离自检（非 root 身份、
仅 lo 网卡、根只读、cgroup 三边界有限）与求解链 readiness（精确版本
+ 最小离线自检，复用 ``LocalProcessProvider.available``），任一项失败
即拒绝服务；随后循环串行处理交换目录中的 attempt 请求，复用
``LocalProcessProvider`` 监督求解并写回响应信封。

单个长驻串行 runner 复用同一容器 cgroup 硬边界，不声称每 attempt
独立 cgroup：每个 attempt 都受同一部署固定容器硬边界约束，工作目录
按 attempt 独立，用后清理。runner 不签发 ``ExecutionReceipt``（归属
worker 侧执行层），不碰数据库/队列/对象存储/项目草稿，不创建容器，
不访问网络。容器死亡即带走被监督进程组，无孤儿。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime

from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.runtime.descriptor import ExecutorDescriptor
from iesplan.computation.runtime.file_exchange import (
    CANCEL_FILE,
    HEARTBEAT_FILE,
    POLL_SECONDS,
    RECEIPT_FILE,
    REQUEST_FILE,
    read_request,
    write_receipt_envelope,
)
from iesplan.computation.runtime.local_process import (
    HIGHS_ADAPTER_PATH,
    HIGHS_EXECUTABLE_REF,
    HIGHS_VERSION,
    ExecutableReadiness,
    LocalExecutable,
    LocalProcessProvider,
    validate_highs_arguments,
)

#: runner 心跳间隔（秒）；worker 侧失活界限见 ``HEARTBEAT_STALE_AFTER_SECONDS``。
HEARTBEAT_INTERVAL_SECONDS = 1.0

#: 自检要求的 cgroup applied 边界键（与 ``applied_limits`` 同源）。
_REQUIRED_LIMIT_KEYS = (
    "memory_limit_bytes",
    "cpu_limit_cores",
    "pids_limit",
)


def _non_loopback_interfaces() -> list[str]:
    """本网络命名空间内除 lo 外的网卡；命名空间不可读即抛错。"""
    try:
        names = os.listdir("/sys/class/net")
    except OSError as exc:
        raise RuntimeError(f"runner 网络命名空间不可读：{exc}") from exc
    return sorted(name for name in names if name != "lo")


def _root_readonly() -> bool:
    """根挂载是否只读（/proc/mounts 事实；不可读或无根记录即 False）。"""
    try:
        with open("/proc/mounts", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 4 and parts[1] == "/":
                    return "ro" in parts[3].split(",")
    except OSError:
        return False
    return False


def check_isolation() -> None:
    """隔离自检：非 root、仅 lo 网卡、根只读、三 cgroup 边界有限。

    全部基于本命名空间/容器事实，不主动外连；任一事实缺失或不满足
    即抛错拒绝服务。
    """
    if os.getuid() == 0:
        raise RuntimeError("runner 不得以 root 身份运行")
    extra = _non_loopback_interfaces()
    if extra:
        raise RuntimeError(f"runner 网络未隔离：存在非回环网卡 {extra!r}")
    if not _root_readonly():
        raise RuntimeError("runner 根文件系统非只读")
    applied = applied_limits()
    for key in _REQUIRED_LIMIT_KEYS:
        if key not in applied:
            raise RuntimeError(f"runner cgroup 边界缺失或无上限：{key}")


def build_solver_provider() -> LocalProcessProvider:
    """构造求解 provider 并跑通版本与自检 readiness；失败即抛错。"""
    binding = LocalExecutable(
        ref=HIGHS_EXECUTABLE_REF,
        path=HIGHS_ADAPTER_PATH,
        argv_validator=validate_highs_arguments,
        readiness=ExecutableReadiness(
            version=HIGHS_VERSION,
            self_test=("--self-test",),
            output_path="self-test.sol",
        ),
    )
    provider = LocalProcessProvider(
        ExecutorDescriptor(
            executor_id="ies.executor.local_process",
            version="1.0.0",
            executables=(HIGHS_EXECUTABLE_REF,),
        ),
        (binding,),
        base_environ={},
    )
    if not provider.available():
        raise RuntimeError("求解链 readiness 未通过，拒绝服务")
    return provider


def _read_cgroup_text(name: str) -> str | None:
    """读本容器 cgroup v2 单值文件；不可读即返回 None。"""
    try:
        with open(os.path.join("/sys/fs/cgroup", name), encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return None


def applied_limits() -> dict[str, object]:
    """本容器实际施加的资源边界（读 cgroup 事实；读不到即省略）。"""
    applied: dict[str, object] = {}
    memory_max = _read_cgroup_text("memory.max")
    if memory_max is not None and memory_max != "max":
        try:
            applied["memory_limit_bytes"] = int(memory_max)
        except ValueError:
            pass
    cpu_max = _read_cgroup_text("cpu.max")
    if cpu_max is not None:
        quota, _, period = cpu_max.partition(" ")
        try:
            applied["cpu_limit_cores"] = int(quota) / int(period)
        except (ValueError, ZeroDivisionError):
            pass
    pids_max = _read_cgroup_text("pids.max")
    if pids_max is not None and pids_max != "max":
        try:
            applied["pids_limit"] = int(pids_max)
        except ValueError:
            pass
    return applied


def _write_heartbeat(exchange_dir: str) -> None:
    envelope = {
        "schema": "ies.ipc.exchange.v1",
        "updated_at": datetime.now(UTC).isoformat(),
    }
    tmp_path = os.path.join(exchange_dir, HEARTBEAT_FILE + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(envelope, handle)
    os.replace(tmp_path, os.path.join(exchange_dir, HEARTBEAT_FILE))


def _heartbeat_loop(exchange_dir: str, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            _write_heartbeat(exchange_dir)
        except OSError:
            pass
        stop_event.wait(HEARTBEAT_INTERVAL_SECONDS)


def _cancel_probe(attempt_dir: str):
    """读 cancel 文件内容的停止探针（无文件即继续）。"""

    def probe() -> str | None:
        try:
            with open(os.path.join(attempt_dir, CANCEL_FILE), encoding="utf-8") as handle:
                content = handle.read().strip()
        except OSError:
            return None
        return content or None

    return probe


def _materialize(attempt_dir: str, workdir: str) -> None:
    """把请求文件树铺进本次独立工作区（目录结构原样保留）。

    铺入后请求文件置只读（0o444），求解进程误写即失败，不静默污染
    输入；不做内容复核。该位只防误写，不声称强制隔离。
    """
    for dirpath, dirnames, filenames in os.walk(attempt_dir):
        for dirname in dirnames:
            full = os.path.join(dirpath, dirname)
            rel = os.path.relpath(full, attempt_dir)
            os.makedirs(os.path.join(workdir, rel), exist_ok=True)
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, attempt_dir)
            if rel in (REQUEST_FILE, RECEIPT_FILE, CANCEL_FILE):
                continue
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            target = os.path.join(workdir, rel)
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.copyfile(full, target)
            os.chmod(target, 0o444)


def _collect_back(workdir: str, attempt_dir: str) -> None:
    """把工作区产出搬回 attempt 目录（核对归执行层）。

    只搬运 attempt 目录中尚不存在的文件：装配输入原样保留，永不复
    写（输入只读不依赖属主关系）；求解器新产生的文件直接落盘。不做
    字节比对与内容复核。
    """
    for dirpath, dirnames, filenames in os.walk(workdir):
        for dirname in dirnames:
            full = os.path.join(dirpath, dirname)
            rel = os.path.relpath(full, workdir)
            os.makedirs(os.path.join(attempt_dir, rel), exist_ok=True)
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, workdir)
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            target = os.path.join(attempt_dir, rel)
            if os.path.lexists(target):
                continue
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.copyfile(full, target)


def serve_attempt(
    provider: LocalProcessProvider,
    attempt_dir: str,
    work_root: str,
) -> None:
    """处理单个 attempt：独立工作区执行并写回响应，保证清理。"""
    command = read_request(attempt_dir)
    workdir = os.path.join(work_root, uuid.uuid4().hex)
    os.makedirs(workdir, exist_ok=False)
    try:
        _materialize(attempt_dir, workdir)
        try:
            outcome = provider.execute(command, workdir, stop=_cancel_probe(attempt_dir))
        except ComputationUnavailableError as exc:
            if exc.reason not in ("cancelled", "fenced"):
                raise
            write_receipt_envelope(attempt_dir, outcome=None, unexecuted=exc.reason)
            return
        applied = applied_limits()
        if applied:
            outcome = replace(
                outcome,
                resource_usage={
                    "applied": applied,
                    **dict(outcome.resource_usage),
                },
            )
        _collect_back(workdir, attempt_dir)
        write_receipt_envelope(attempt_dir, outcome=outcome)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def serve_forever(
    provider: LocalProcessProvider,
    exchange_dir: str,
    work_root: str,
    *,
    max_attempts: int | None = None,
) -> int:
    """主循环：认领无收据的 attempt 并处理；返回处理计数。

    ``max_attempts`` 只用于测试同步驱动，生产常驻不限次数。
    """
    stop = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat_loop, args=(exchange_dir, stop), daemon=True)
    heartbeat.start()
    served = 0
    try:
        while not stop.is_set():
            try:
                names = sorted(os.listdir(exchange_dir))
            except OSError:
                names = []
            claimed = False
            for name in names:
                if name.startswith("."):
                    continue
                attempt_dir = os.path.join(exchange_dir, name)
                if not os.path.isdir(attempt_dir):
                    continue
                if not os.path.isfile(os.path.join(attempt_dir, REQUEST_FILE)):
                    continue
                if os.path.isfile(os.path.join(attempt_dir, RECEIPT_FILE)):
                    continue
                serve_attempt(provider, attempt_dir, work_root)
                served += 1
                claimed = True
                if max_attempts is not None and served >= max_attempts:
                    return served
            if not claimed:
                stop.wait(POLL_SECONDS)
    finally:
        stop.set()
    return served


def main(argv: list[str]) -> int:
    """入口：``runner_service <交换目录> <工作根>``。"""
    if len(argv) != 3:
        sys.stderr.write("用法：runner_service <交换目录> <工作根>\n")
        return 2
    _prog, exchange_dir, work_root = argv
    for label, path in (("交换目录", exchange_dir), ("工作根", work_root)):
        if not os.path.isabs(path):
            sys.stderr.write(f"{label}须为绝对路径: {path!r}\n")
            return 2
    try:
        check_isolation()
        provider = build_solver_provider()
        os.makedirs(exchange_dir, exist_ok=True)
        os.makedirs(work_root, exist_ok=True)
        # 启动期心跳可写是 readiness 的一部分：写不出即原子拒绝，
        # 不留无声存活的假就绪（运行期心跳仍由失活界限判定）。
        _write_heartbeat(exchange_dir)
        serve_forever(provider, exchange_dir, work_root)
    except (OSError, RuntimeError, ValueError) as exc:
        sys.stderr.write(f"ies-solver-runner: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
