"""文件交换隔离执行 provider：同一执行器引用，隔离部署传输。

``FileExchangeExecutorProvider`` 与 ``LocalProcessProvider`` 是同一
执行器（``ies.executor.local_process@1.0.0``）的隔离部署形态：调用
形状与返回的 ``ProcessOutcome`` 语义完全一致，区别仅在于受控进程
实际运行在部署时预配置的隔离 runner 容器内，双方只经交换目录文件
往来。本模块不含任何求解逻辑，不回退本地执行：交换目录不可用、
runner 心跳失活或收据非法，一律明确失败。

信封（``EXCHANGE_SCHEMA``）为固定键 JSON + 声明相对路径文件：请求
含 ``command.json`` 与输入文件树（含执行层预建的空输出目录）；响应
含 ``receipt.json``（过程结果内含 base64 流字节）与输出文件。跨容器
即真实边界，读写两侧都做固定形状校验；不做内容 hash 与重复业务
校验。取消/fencing 字符串经 ``cancel`` 文件原文往返，主状态不改写。
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import shutil
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.protocols import (
    ProcessOutcome,
    StopProbe,
    read_stop_reason,
    thaw_json_value,
)
from iesplan.computation.runtime.descriptor import ExecutorDescriptor

#: 交换信封版本（读写两侧必须一致，不一致即非法信封）。
EXCHANGE_SCHEMA = "ies.ipc.exchange.v1"

#: 请求/响应固定文件名。
REQUEST_FILE = "command.json"
RECEIPT_FILE = "receipt.json"
CANCEL_FILE = "cancel"
HEARTBEAT_FILE = "heartbeat.json"

#: 收据轮询间隔（秒）与 runner 心跳失活界限（秒）。
POLL_SECONDS = 0.05
HEARTBEAT_STALE_AFTER_SECONDS = 10.0


def _check_rel_path(value: object, name: str) -> str:
    """交换文件相对路径最小形状检查（非空、相对、不越界）"""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} 须为非空路径字符串: {value!r}")
    if os.path.isabs(value) or value.startswith("-"):
        raise ValueError(f"{name} 须为规范相对路径: {value!r}")
    if ".." in value.split("/"):
        raise ValueError(f"{name} 路径越界: {value!r}")
    return value


def _check_envelope(value: object, name: str) -> Mapping[str, object]:
    """信封固定形状检查：顶层映射 + 版本一致。"""
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} 须为映射: {type(value).__name__}")
    if value.get("schema") != EXCHANGE_SCHEMA:
        raise ValueError(f"{name} 版本非法: {value.get('schema')!r}")
    return value


def build_request_bundle(
    command: Mapping[str, object], workdir: str, target: str
) -> None:
    """把命令与工作区文件树装配为请求目录（worker 侧）。

    请求目录树对交换双方可写（目录显式 0o777，仅限交换卷；runner
    以非 root 身份回写收据与输出）；输入文件保持调用方默认权限
    （runner 侧对 root 属主文件只读，天然不可写）。
    """
    os.makedirs(target, exist_ok=False)
    with open(os.path.join(target, REQUEST_FILE), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "schema": EXCHANGE_SCHEMA,
                "command": thaw_json_value(dict(command)),
            },
            handle,
        )
    for dirpath, dirnames, filenames in os.walk(workdir):
        for dirname in dirnames:
            full = os.path.join(dirpath, dirname)
            rel = os.path.relpath(full, workdir)
            os.makedirs(os.path.join(target, rel), exist_ok=True)
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, workdir)
            _check_rel_path(rel, "工作区文件")
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            target_path = os.path.join(target, rel)
            parent = os.path.dirname(target_path) or target
            os.makedirs(parent, exist_ok=True)
            shutil.copyfile(full, target_path)
    for dirpath, dirnames, _filenames in os.walk(target):
        os.chmod(dirpath, 0o777)
        for dirname in dirnames:
            os.chmod(os.path.join(dirpath, dirname), 0o777)


def read_request(request_dir: str) -> Mapping[str, object]:
    """读并校验请求信封，返回结构化命令（runner 侧）。"""
    request_path = os.path.join(request_dir, REQUEST_FILE)
    try:
        with open(request_path, encoding="utf-8") as handle:
            envelope = _check_envelope(json.load(handle), "请求信封")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"请求信封非法: {exc}") from exc
    command = envelope.get("command")
    if not isinstance(command, Mapping):
        raise ValueError("请求信封缺结构化命令")
    return command


def _decode_bytes(value: object, name: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"{name} 须为 base64 字符串")
    try:
        return base64.b64decode(value.encode("ascii"))
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"{name} 非法: {exc}") from exc


def write_receipt_envelope(
    attempt_dir: str,
    *,
    outcome: ProcessOutcome | None,
    unexecuted: str = "",
) -> None:
    """写响应信封（runner 侧）：过程结果与未执行二选一。

    只 transport 过程结果；输出文件原样放回 attempt 目录，核对归
    执行层（不另附清单，避免同一事实两个来源）。
    """
    if (outcome is None) == (not unexecuted):
        raise ValueError("响应须为过程结果与未执行二选一")
    if outcome is not None:
        envelope: dict[str, object] = {
            "schema": EXCHANGE_SCHEMA,
            "outcome": {
                "exit_code": outcome.exit_code,
                "signal_number": outcome.signal_number,
                "termination_reason": outcome.termination_reason,
                "started_at": outcome.started_at,
                "ended_at": outcome.ended_at,
                "stdout": base64.b64encode(outcome.stdout).decode("ascii"),
                "stderr": base64.b64encode(outcome.stderr).decode("ascii"),
                "stdout_truncated": outcome.stdout_truncated,
                "stderr_truncated": outcome.stderr_truncated,
                # 分层映射需递归解冻为普通 dict，信封才可 JSON 序列化。
                "resource_usage": thaw_json_value(dict(outcome.resource_usage)),
            },
        }
    else:
        if unexecuted not in ("cancelled", "fenced"):
            raise ValueError(f"未执行原因非法: {unexecuted!r}")
        envelope = {"schema": EXCHANGE_SCHEMA, "unexecuted": unexecuted}
    tmp_path = os.path.join(attempt_dir, RECEIPT_FILE + ".tmp")
    final_path = os.path.join(attempt_dir, RECEIPT_FILE)
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(envelope, handle)
    os.replace(tmp_path, final_path)


def read_receipt_envelope(
    attempt_dir: str,
) -> tuple[ProcessOutcome | None, str]:
    """读并校验响应信封（worker 侧）。

    返回 ``(过程结果|None, 未执行原因)``；非法信封抛 ``ValueError``。
    输出文件由调用方直接搬运，执行层按 manifest 核对。
    """
    receipt_path = os.path.join(attempt_dir, RECEIPT_FILE)
    try:
        with open(receipt_path, encoding="utf-8") as handle:
            envelope = _check_envelope(json.load(handle), "响应信封")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"响应信封非法: {exc}") from exc
    outcome_raw = envelope.get("outcome")
    unexecuted = envelope.get("unexecuted", "")
    if (outcome_raw is None) == (not unexecuted):
        raise ValueError("响应须为过程结果与未执行二选一")
    if outcome_raw is not None:
        if not isinstance(outcome_raw, Mapping):
            raise ValueError("响应过程结果须为映射")
        usage_raw = outcome_raw.get("resource_usage", {})
        if not isinstance(usage_raw, Mapping):
            raise ValueError("resource_usage 须为映射")
        try:
            outcome = ProcessOutcome(
                exit_code=outcome_raw.get("exit_code"),
                signal_number=outcome_raw.get("signal_number"),
                termination_reason=str(outcome_raw.get("termination_reason")),
                started_at=str(outcome_raw.get("started_at")),
                ended_at=str(outcome_raw.get("ended_at")),
                stdout=_decode_bytes(outcome_raw.get("stdout"), "stdout"),
                stderr=_decode_bytes(outcome_raw.get("stderr"), "stderr"),
                stdout_truncated=outcome_raw.get("stdout_truncated"),
                stderr_truncated=outcome_raw.get("stderr_truncated"),
                resource_usage=dict(usage_raw),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"响应过程结果非法: {exc}") from exc
        return outcome, ""
    if unexecuted not in ("cancelled", "fenced"):
        raise ValueError(f"未执行原因非法: {unexecuted!r}")
    return None, str(unexecuted)


def _relative_files(root: str) -> set[str]:
    """工作区已有相对文件集合（取回时永不覆盖，只搬运新增）。"""
    found: set[str] = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            if os.path.isfile(full) and not os.path.islink(full):
                found.add(os.path.relpath(full, root))
    return found


def read_heartbeat(exchange_dir: str) -> datetime | None:
    """读 runner 心跳时间；缺失或非法即返回 None（失活）。"""
    try:
        with open(
            os.path.join(exchange_dir, HEARTBEAT_FILE), encoding="utf-8"
        ) as handle:
            envelope = _check_envelope(json.load(handle), "心跳")
        updated = datetime.fromisoformat(str(envelope.get("updated_at")))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if updated.tzinfo is None:
        return None
    return updated


def heartbeat_fresh(
    exchange_dir: str, now: datetime | None = None
) -> bool:
    """心跳是否在失活界限内。"""
    updated = read_heartbeat(exchange_dir)
    if updated is None:
        return False
    current = now if now is not None else datetime.now(UTC)
    return (current - updated).total_seconds() <= HEARTBEAT_STALE_AFTER_SECONDS


#: 心跳身份要求的应用边界键（与 runner 启动自检同源，只核对存在性）。
HEARTBEAT_APPLIED_KEYS = ("memory_limit_bytes", "cpu_limit_cores", "pids_limit")


def read_heartbeat_identity(exchange_dir: str) -> Mapping[str, object] | None:
    """读 runner 心跳中的启动身份；缺失或形状非法即返回 None。

    身份是 runner 启动隔离自检通过后写入的固定事实：执行器精确引用、
    支持的可执行文件引用表、已证明有限的三项应用边界。不做能力谈判。
    """
    try:
        with open(
            os.path.join(exchange_dir, HEARTBEAT_FILE), encoding="utf-8"
        ) as handle:
            envelope = _check_envelope(json.load(handle), "心跳")
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    executor_ref = envelope.get("executor_ref")
    executable_refs = envelope.get("executable_refs")
    applied = envelope.get("applied")
    if not isinstance(executor_ref, str) or not executor_ref:
        return None
    if not isinstance(executable_refs, list) or not all(
        isinstance(item, str) for item in executable_refs
    ):
        return None
    if not isinstance(applied, Mapping) or not all(
        key in applied for key in HEARTBEAT_APPLIED_KEYS
    ):
        return None
    return {
        "executor_ref": executor_ref,
        "executable_refs": list(executable_refs),
        "applied": dict(applied),
    }


class FileExchangeExecutorProvider:
    """文件交换隔离执行 provider（与本地进程执行器同引用）。

    ``ref`` 与 ``LocalProcessProvider`` 相同（同一执行器的隔离部署
    形态，由组合根二选一注册，不并存）；``available`` 同时断言交换
    目录已配置可写、runner 心跳新鲜、心跳身份与本描述符精确匹配，
    任一不满足即不可用，注册表据此原子拒绝；``execute`` 递交前再次
    核对目录与心跳身份，把命令与文件树递交给 runner 并取回过程结果
    与输出文件，不解释业务，不回退本地执行。
    """

    __slots__ = ("_descriptor", "_exchange_dir")

    def __init__(
        self,
        descriptor: ExecutorDescriptor,
        exchange_dir: str,
    ) -> None:
        if not isinstance(descriptor, ExecutorDescriptor):
            raise TypeError(
                "descriptor 须为 ExecutorDescriptor，"
                f"得到 {type(descriptor).__name__}"
            )
        if not isinstance(exchange_dir, str) or not exchange_dir:
            raise ValueError(f"交换目录须为非空字符串: {exchange_dir!r}")
        if not os.path.isabs(exchange_dir):
            raise ValueError(
                f"交换目录须为固定绝对路径: {exchange_dir!r}"
            )
        self._descriptor = descriptor
        self._exchange_dir = exchange_dir

    @property
    def descriptor(self) -> ExecutorDescriptor:
        return self._descriptor

    @property
    def ref(self) -> str:
        return self._descriptor.ref

    def available(self) -> bool:
        """交换目录可用、心跳新鲜且身份匹配；任一不满足即不可用。"""
        return (
            self._exchange_ready()
            and heartbeat_fresh(self._exchange_dir)
            and self._runner_identity_ok()
        )

    def _exchange_ready(self) -> bool:
        """交换目录存在且可写（部署事实，不含 runner 存活）。"""
        return os.path.isdir(self._exchange_dir) and os.access(
            self._exchange_dir, os.W_OK | os.X_OK
        )

    def _runner_identity_ok(self) -> bool:
        """心跳身份与本描述符精确匹配：执行器引用一致、可执行文件全支持。"""
        identity = read_heartbeat_identity(self._exchange_dir)
        if identity is None:
            return False
        if identity["executor_ref"] != self._descriptor.ref:
            return False
        supported = set(identity["executable_refs"])
        return set(self._descriptor.executables) <= supported

    def execute(
        self,
        command: Mapping[str, object],
        workdir: str,
        *,
        stop: StopProbe | None = None,
    ) -> ProcessOutcome:
        """经交换目录递交执行并取回过程结果与输出文件。"""
        pre_stop = read_stop_reason(stop)
        if pre_stop is not None:
            raise ComputationUnavailableError(
                f"启动前已停止（{pre_stop}），未递交执行",
                reason=pre_stop,
            )
        if not self._exchange_ready():
            raise ComputationUnavailableError(
                f"交换目录不可用: {self._exchange_dir!r}",
                reason="no-exchange",
            )
        if not heartbeat_fresh(self._exchange_dir):
            raise ComputationUnavailableError(
                "runner 心跳失活，不递交执行",
                reason="runner-unreachable",
            )
        if not self._runner_identity_ok():
            raise ComputationUnavailableError(
                "runner 心跳身份与执行器描述符不匹配，不递交执行",
                reason="runner-unreachable",
            )
        pre_existing = _relative_files(workdir)
        attempt_id = uuid.uuid4().hex
        attempt_dir = os.path.join(self._exchange_dir, attempt_id)
        staging_dir = os.path.join(
            self._exchange_dir, f".staging-{attempt_id}"
        )
        try:
            build_request_bundle(command, workdir, staging_dir)
            os.rename(staging_dir, attempt_dir)
        except OSError as exc:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise ComputationUnavailableError(
                f"请求装配失败: {exc}", reason="exchange-io-error"
            ) from exc
        outcome = self._await_receipt(attempt_dir, stop)
        self._fetch_outputs(attempt_dir, workdir, pre_existing)
        # 成功取回即清理交换侧 attempt 目录（异常路径保留现场供诊断，
        # 由部署按卷策略回收；容器死亡不留进程孤儿）。
        shutil.rmtree(attempt_dir, ignore_errors=True)
        return outcome

    def _await_receipt(
        self, attempt_dir: str, stop: StopProbe | None
    ) -> ProcessOutcome:
        """轮询收据：停止即写 cancel 文件，心跳失活即报错。"""
        receipt_path = os.path.join(attempt_dir, RECEIPT_FILE)
        cancel_path = os.path.join(attempt_dir, CANCEL_FILE)
        cancel_written = False
        while True:
            if not cancel_written:
                hit = read_stop_reason(stop)
                if hit is not None:
                    try:
                        with open(cancel_path, "w", encoding="utf-8") as handle:
                            handle.write(hit)
                    except OSError as exc:
                        raise ComputationUnavailableError(
                            f"取消标记写入失败: {exc}",
                            reason="exchange-io-error",
                        ) from exc
                    cancel_written = True
            if os.path.isfile(receipt_path):
                try:
                    outcome, unexecuted = read_receipt_envelope(attempt_dir)
                except ValueError as exc:
                    raise ComputationUnavailableError(
                        f"响应信封非法: {exc}", reason="bad-envelope"
                    ) from exc
                if outcome is not None:
                    return outcome
                raise ComputationUnavailableError(
                    f"远端启动前已停止（{unexecuted}），未执行",
                    reason=unexecuted,
                )
            if not heartbeat_fresh(self._exchange_dir):
                raise ComputationUnavailableError(
                    "runner 心跳失活，无法建立可信结果",
                    reason="runner-unreachable",
                )
            time.sleep(POLL_SECONDS)

    def _fetch_outputs(
        self,
        attempt_dir: str,
        workdir: str,
        pre_existing: set[str],
    ) -> None:
        """把 runner 新产生的文件搬运进本次工作区（核对归执行层）。

        ``pre_existing`` 是递交前工作区已有的相对文件集合，永不覆盖：
        输入只读语义不依赖 worker 身份，runner 新产生的文件才落盘。
        """
        for dirpath, dirnames, _filenames in os.walk(attempt_dir):
            for dirname in dirnames:
                full = os.path.join(dirpath, dirname)
                rel = os.path.relpath(full, attempt_dir)
                os.makedirs(os.path.join(workdir, rel), exist_ok=True)
        for dirpath, _dirnames, filenames in os.walk(attempt_dir):
            for filename in filenames:
                full = os.path.join(dirpath, filename)
                rel = os.path.relpath(full, attempt_dir)
                if rel in (REQUEST_FILE, RECEIPT_FILE, CANCEL_FILE):
                    continue
                if rel in pre_existing:
                    continue
                target = os.path.join(workdir, rel)
                os.makedirs(os.path.dirname(target) or workdir, exist_ok=True)
                try:
                    shutil.copyfile(full, target)
                except OSError as exc:
                    raise ComputationUnavailableError(
                        f"输出搬运失败: {rel!r}: {exc}",
                        reason="exchange-io-error",
                    ) from exc


__all__ = [
    "EXCHANGE_SCHEMA",
    "REQUEST_FILE",
    "RECEIPT_FILE",
    "CANCEL_FILE",
    "HEARTBEAT_FILE",
    "POLL_SECONDS",
    "HEARTBEAT_STALE_AFTER_SECONDS",
    "HEARTBEAT_APPLIED_KEYS",
    "FileExchangeExecutorProvider",
    "build_request_bundle",
    "read_request",
    "write_receipt_envelope",
    "read_receipt_envelope",
    "read_heartbeat",
    "heartbeat_fresh",
    "read_heartbeat_identity",
]
