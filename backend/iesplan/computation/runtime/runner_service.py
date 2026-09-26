"""隔离 solver runner 服务（运行在部署时预配置的隔离容器内）。

本模块只在 runner 容器内运行：启动时先做隔离自检（非 root 身份、
真实断网、cgroup 可读）与求解链 readiness（精确版本 + 最小离线自检，
复用 ``LocalProcessProvider.available``），任一项失败即拒绝服务；
随后循环处理交换目录中的 attempt 请求，复用 ``LocalProcessProvider``
监督求解并写回响应信封。

runner 不签发 ``ExecutionReceipt``（归属 worker 侧执行层），不碰
数据库/队列/对象存储/项目草稿，不创建容器，不访问网络。容器死亡即
带走被监督进程组，无孤儿。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
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

#: 断网自检的连接目标与超时（秒）：能连上即拒绝服务。
_ISOLATION_PROBE_HOST = "8.8.8.8"
_ISOLATION_PROBE_PORT = 53
_ISOLATION_PROBE_TIMEOUT_SECONDS = 3.0


def check_isolation() -> None:
    """隔离自检：非 root、真实断网、cgroup 可读；失败抛错拒绝服务。"""
    if os.getuid() == 0:
        raise RuntimeError("runner 不得以 root 身份运行")
    try:
        probe = socket.create_connection(
            (_ISOLATION_PROBE_HOST, _ISOLATION_PROBE_PORT),
            timeout=_ISOLATION_PROBE_TIMEOUT_SECONDS,
        )
    except OSError:
        pass
    else:
        probe.close()
        raise RuntimeError(f"runner 网络未隔离：可连接 {_ISOLATION_PROBE_HOST}")
    try:
        with open("/sys/fs/cgroup/memory.events", encoding="utf-8") as handle:
            handle.read(1)
    except OSError as exc:
        raise RuntimeError(f"runner cgroup 不可读：{exc}") from exc


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


def _same_file_bytes(first: str, second: str) -> bool:
    """两文件字节相同即 True；读错即 False（调用方再按写路径处理）。"""
    try:
        if os.path.getsize(first) != os.path.getsize(second):
            return False
        with open(first, "rb") as left, open(second, "rb") as right:
            while True:
                left_chunk = left.read(1 << 20)
                right_chunk = right.read(1 << 20)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True
    except OSError:
        return False


def _materialize(attempt_dir: str, workdir: str) -> None:
    """把请求文件树铺进本次独立工作区（目录结构原样保留）。"""
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


def _collect_back(workdir: str, attempt_dir: str) -> None:
    """把工作区全部产出搬回 attempt 目录（核对归执行层）。

    装配输入字节未变即跳过（worker 侧文件对 runner 只读，不复写）；
    新增或被求解器改写的文件经同目录临时文件原子替换（只需目录写
    权限，不触碰属主即可更新 root 属主文件）。
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
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            if os.path.isfile(target) and _same_file_bytes(full, target):
                continue
            tmp_path = target + ".collecting"
            shutil.copyfile(full, tmp_path)
            os.replace(tmp_path, target)


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
                resource_usage={**dict(outcome.resource_usage), **applied},
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
