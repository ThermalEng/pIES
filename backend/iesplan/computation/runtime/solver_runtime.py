"""通用求解运行时：具体、可调用的 Bundle 执行编排。

``SolverRuntime`` 是通用实现（非 Protocol/ABC/占位）：消费完整 typed
``SolverBundle``、attempt 上下文、部署策略、已发布执行器注册表与调用方
停止探针，拥有执行编排全过程——启动前停止检查、精确选择、策略检查、
独立临时工作区、输入只读写入、executor 调用、声明输出核对（含数量/
总字节上限与特殊文件边界）、唯一回执签发与工作区清理。

信任边界：typed ``SolverBundle`` 是可信内部交接，不重跑 JSON schema，
不算 hash，不重复装配或文件内容校验；只执行本层拥有的真实外部进程
边界门禁（精确 executor/executable、环境 allowlist、资源上限、网络
开关、隔离目录与声明输出范围）。资源需求只做策略上限比对，不做真实
隔离实施；进程启动、运行中停止、超时与回收归 provider 实现。
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone

from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.protocols import (
    ExecutionReceipt,
    ExecutorProvider,
    ProcessOutcome,
    SolverBundle,
    SolverBundleManifest,
    StopProbe,
    read_stop_reason,
)
from iesplan.computation.runtime.context import AttemptContext
from iesplan.computation.runtime.policy import DeploymentPolicy
from iesplan.computation.runtime.registry import ExecutorRegistry

#: 命令未声明 success_exit_codes 时的协议默认（仅判断进程是否按协议结束）。
_DEFAULT_SUCCESS_EXIT_CODES = (0,)

#: 声明输出单次读取块大小（字节；总预算内分块读，超限不完整读入）。
_OUTPUT_READ_CHUNK_BYTES = 65_536


def _normalize_timestamp(value: str) -> str:
    """把带时区时间戳归一化为 ISO 8601 UTC ``Z`` 字尾。"""
    return (
        datetime.fromisoformat(value)
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class SolverRuntime:
    """通用求解执行编排（组合根持有，Worker 经 application 调用）。

    构造依赖已发布的 ``ExecutorRegistry`` 与 ``DeploymentPolicy``；
    ``run`` 每次调用建立独立临时工作区，结束后清理。门禁或执行器解析
    失败返回 ``policy_rejected`` 回执且不调用 executor；启动前停止
    （含 provider 启动瞬间观测到的停止，此时进程必然未启动）返回
    ``cancelled``/``fenced`` 未执行回执；executor 调用后的非零退出、
    必需输出缺失、未声明输出分别形成明确回执，不回退、不切换
    executor、不伪造成功；只有无法建立可信回执的基础设施错误
    （工作区不可建、executor 抛非停止异常、清理失败）才向上传播异常。
    """

    __slots__ = ("_registry", "_policy")

    def __init__(
        self, registry: ExecutorRegistry, policy: DeploymentPolicy
    ) -> None:
        if not isinstance(registry, ExecutorRegistry):
            raise TypeError(
                "registry 须为 ExecutorRegistry，"
                f"得到 {type(registry).__name__}"
            )
        if not isinstance(policy, DeploymentPolicy):
            raise TypeError(
                f"policy 须为 DeploymentPolicy，得到 {type(policy).__name__}"
            )
        self._registry = registry
        self._policy = policy

    def run(
        self,
        bundle: SolverBundle,
        attempt: AttemptContext,
        *,
        stop: StopProbe | None = None,
    ) -> tuple[ExecutionReceipt, dict[str, bytes]]:
        """执行完整 Bundle，返回 ``(唯一回执, 已核对声明输出字节)``。

        ``stop`` 为调用方停止探针：启动前已停止即不启动进程，直接签发
        ``cancelled``/``fenced`` 回执（含 provider 启动瞬间观测到的停止，
        此时进程必然未启动，收口为同一未执行回执而非异常）；运行中停止
        由 provider 观测并终止进程组。状态优先级：真实超时 → ``timed_out``
        为主状态；主进程
        退出后仍有组成员被强制收口 → ``process_failed``
        （``group_terminated`` 不得签发 ``succeeded``）；运行中停止 →
        ``cancelled``/``fenced`` 为主状态；输出边界问题追加进 ``error``
        供解释，不改写上述主状态。非停止执行的输出边界违规（未声明文件/
        特殊文件/符号链接/不可读/数量超限/总量超限）→
        ``output_protocol_failed``；退出码不在成功集合 →
        ``process_failed``；必需输出缺失 →
        ``output_protocol_failed``；否则 ``succeeded``。成功与失败都
        尽量形成回执；输出映射只含已核对的声明输出。
        """
        if not isinstance(bundle, SolverBundle):
            raise TypeError(
                f"bundle 须为 SolverBundle，得到 {type(bundle).__name__}"
            )
        if not isinstance(attempt, AttemptContext):
            raise TypeError(
                "attempt 须为 AttemptContext，"
                f"得到 {type(attempt).__name__}"
            )
        if stop is not None and not callable(stop):
            raise TypeError(
                f"stop 须为停止探针可调用对象，得到 {type(stop).__name__}"
            )
        manifest = bundle.manifest
        command = manifest.command
        executor_ref = command["executor"]
        executable_ref = command["executable"]
        stop_reason = read_stop_reason(stop)
        if stop_reason is not None:
            return self._unexecuted(
                bundle,
                attempt,
                str(executor_ref),
                str(executable_ref),
                (
                    "启动前已取消，未启动进程"
                    if stop_reason == "cancelled"
                    else "启动前租约失效，未启动进程"
                ),
                status=stop_reason,
            ), {}
        try:
            provider = self._registry.resolve(
                str(executor_ref), str(executable_ref)
            )
        except ComputationUnavailableError as exc:
            return self._unexecuted(
                bundle,
                attempt,
                str(executor_ref),
                str(executable_ref),
                str(exc),
            ), {}
        gate_error = self._check_policy(command)
        if gate_error is not None:
            return self._unexecuted(
                bundle, attempt, provider.ref, str(executable_ref), gate_error
            ), {}
        root = tempfile.mkdtemp(prefix="ies-runtime-")
        try:
            self._prepare_workdir(root, bundle, command)
            try:
                outcome = provider.execute(command, root, stop=stop)
            except ComputationUnavailableError as exc:
                if exc.reason not in ("cancelled", "fenced"):
                    raise
                return self._unexecuted(
                    bundle,
                    attempt,
                    provider.ref,
                    str(executable_ref),
                    str(exc),
                    status=exc.reason,
                ), {}
            if not isinstance(outcome, ProcessOutcome):
                raise TypeError(
                    "executor 须返回 ProcessOutcome，"
                    f"得到 {type(outcome).__name__}"
                )
            return self._finish(
                bundle, attempt, provider, command, outcome, root
            )
        finally:
            shutil.rmtree(root)

    def _check_policy(self, command: Mapping[str, object]) -> str | None:
        """部署策略门禁；通过返回 None，否则返回明确拒绝原因。"""
        executable_ref = str(command["executable"])
        if not self._policy.allows_executable(executable_ref):
            return f"executable 不在部署 allowlist: {executable_ref!r}"
        environment = command.get("environment", {})
        assert isinstance(environment, Mapping)
        denied = sorted(
            key for key in environment if key not in self._policy.environment_vars
        )
        if denied:
            return f"环境变量不在 allowlist: {denied!r}"
        for key, limit in (
            ("timeout_seconds", self._policy.max_timeout_seconds),
            ("memory_mib", self._policy.max_memory_mib),
            ("cpu_cores", self._policy.max_cpu_cores),
        ):
            if key in command and float(command[key]) > limit:  # type: ignore[arg-type]
                return (
                    f"资源需求超过部署上限: {key}={command[key]!r} > {limit!r}"
                )
        if bool(command.get("network", False)) and not self._policy.allow_network:
            return "命令声明联网但部署策略拒绝"
        return None

    def _prepare_workdir(
        self,
        root: str,
        bundle: SolverBundle,
        command: Mapping[str, object],
    ) -> str:
        """建立 Bundle 根工作区：输入只读写入，输出目录保持可写。

        返回 Bundle 根目录（即 executor 可见的 ``workdir``；manifest
        内全部路径均按 Bundle 根定位）。``command`` 声明的子进程工作
        目录仅预建，不改变输入/输出定位。全部输入正常写完后，仅把
        输入文件置只读（0o444）；输入目录保持本进程可写以便清理。
        该位语义只防误写，不声称为完整安全隔离，不可绕过的只读挂载
        归生产隔离 provider。
        """
        run_sub = os.path.normpath(
            os.path.join(root, str(command.get("working_directory", ".")))
        )
        os.makedirs(run_sub, exist_ok=True)
        for path, content in bundle.files.items():
            target = os.path.join(root, path)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(content)
        for path in bundle.files:
            os.chmod(os.path.join(root, path), 0o444)
        return root

    def _finish(
        self,
        bundle: SolverBundle,
        attempt: AttemptContext,
        provider: ExecutorProvider,
        command: Mapping[str, object],
        outcome: ProcessOutcome,
        root: str,
    ) -> tuple[ExecutionReceipt, dict[str, bytes]]:
        """核对输出边界并签发回执（见 ``run`` 状态优先级）。"""
        manifest = bundle.manifest
        declared = {entry["path"]: entry for entry in manifest.outputs}
        input_paths = {entry["path"] for entry in manifest.inputs}
        nodes, scan_errors = self._scan_output_nodes(
            root, declared, input_paths
        )
        boundary_errors = scan_errors
        collected, collect_errors = self._collect_declared(
            root, manifest, nodes
        )
        boundary_errors.extend(collect_errors)
        success_codes = tuple(
            command.get("success_exit_codes", _DEFAULT_SUCCESS_EXIT_CODES)
        )
        missing = sorted(
            str(entry["path"])
            for entry in manifest.outputs
            if entry["required"] and str(entry["path"]) not in collected
        )
        reason = outcome.termination_reason
        if reason == "timeout":
            status = "timed_out"
            parts = [
                f"执行超时终止: exit_code={outcome.exit_code!r} "
                f"signal={outcome.signal_number!r}"
            ]
            parts.extend(boundary_errors)
            if missing:
                parts.append(f"必需输出缺失: {missing!r}")
            error = "; ".join(parts)
        elif reason == "group_terminated":
            status = "process_failed"
            parts = [
                "主进程退出后仍有组成员、已强制收口: "
                f"exit_code={outcome.exit_code!r} "
                f"signal={outcome.signal_number!r}"
            ]
            parts.extend(boundary_errors)
            if missing:
                parts.append(f"必需输出缺失: {missing!r}")
            error = "; ".join(parts)
        elif reason in ("cancelled", "fenced"):
            status = reason
            lead = (
                "执行被取消"
                if reason == "cancelled"
                else "执行被隔离（租约失效）"
            )
            parts = [
                f"{lead}: exit_code={outcome.exit_code!r} "
                f"signal={outcome.signal_number!r}"
            ]
            parts.extend(boundary_errors)
            if missing:
                parts.append(f"必需输出缺失: {missing!r}")
            error = "; ".join(parts)
        elif boundary_errors:
            status = "output_protocol_failed"
            error = "; ".join(boundary_errors)
        elif outcome.exit_code is None:
            status = "process_failed"
            error = (
                f"进程被信号终止: signal={outcome.signal_number!r} "
                f"reason={outcome.termination_reason!r}"
            )
        elif outcome.exit_code not in success_codes:
            status = "process_failed"
            error = (
                f"进程未按协议结束: exit_code={outcome.exit_code!r} "
                f"reason={outcome.termination_reason!r}"
            )
        elif missing:
            status = "output_protocol_failed"
            error = f"必需输出缺失: {missing!r}"
        else:
            status = "succeeded"
            error = ""
        receipt = self._build_receipt(
            bundle, attempt, provider, command, outcome, status, error, collected
        )
        return receipt, collected

    def _scan_output_nodes(
        self,
        root: str,
        declared: Mapping[str, object],
        input_paths: set[str],
    ) -> tuple[list[tuple[str, str, int]], list[str]]:
        """扫描工作区输出节点事实。

        返回 ``([(相对路径, 类型, 字节数)], 违规)``；类型为
        ``"regular"``/``"special"``/``"symlink"``，字节数仅普通文件取
        lstat 事实。目录与输入跳过（不计配额）。符号链接、特殊文件与
        未声明文件记违规，但节点仍保留供配额统计。
        """
        nodes: list[tuple[str, str, int]] = []
        errors: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            for name in list(dirnames) + filenames:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root)
                try:
                    info = os.lstat(full)
                except OSError:
                    continue
                mode = info.st_mode
                if stat.S_ISDIR(mode) or rel in input_paths:
                    continue
                if stat.S_ISLNK(mode):
                    nodes.append((rel, "symlink", 0))
                    errors.append(f"工作区存在符号链接: {rel!r}")
                elif not stat.S_ISREG(mode):
                    nodes.append((rel, "special", 0))
                    errors.append(f"工作区存在特殊文件: {rel!r}")
                else:
                    nodes.append((rel, "regular", info.st_size))
                    if rel not in declared:
                        errors.append(f"未声明输出: {rel!r}")
        return nodes, errors

    def _collect_declared(
        self,
        root: str,
        manifest: SolverBundleManifest,
        nodes: list[tuple[str, str, int]],
    ) -> tuple[dict[str, bytes], list[str]]:
        """按 manifest 顺序有界收集声明输出。

        配额按扫描到的实际节点事实判定：文件数覆盖全部非输入、非目录
        节点，字节总量按全部普通文件的 lstat 大小累计；未声明、不可读
        或超限未收录的文件同样占配额，不腾出计数。只收集普通文件
        （符号链接、目录与特殊文件跳过，缺必需项由 ``missing`` 判定）；
        每个文件至多按其 stat 大小读取，大文件不读入内存。
        """
        collected: dict[str, bytes] = {}
        errors: list[str] = []
        max_files = self._policy.max_output_files
        budget = self._policy.max_output_bytes
        by_path = {rel: (kind, size) for rel, kind, size in nodes}
        total_bytes = sum(
            size for _, kind, size in nodes if kind == "regular"
        )
        if len(nodes) > max_files:
            errors.append(
                f"输出文件数超过上限 {max_files}（实际 {len(nodes)}）"
            )
        if total_bytes > budget:
            errors.append(
                f"输出总字节超过上限 {budget}（实际 {total_bytes}）"
            )
        remaining = budget
        for entry in manifest.outputs:
            path = str(entry["path"])
            node = by_path.get(path)
            if node is None or node[0] != "regular":
                continue
            if len(collected) >= max_files:
                continue
            if node[1] > remaining:
                continue
            try:
                data = self._read_sized(os.path.join(root, path), node[1])
            except OSError:
                errors.append(f"声明输出不可读: {path!r}")
                continue
            collected[path] = data
            remaining -= len(data)
        return collected, errors

    @staticmethod
    def _read_sized(full: str, size: int) -> bytes:
        """按 stat 大小有界读取文件内容；IO 错误抛 ``OSError``。"""
        chunks: list[bytes] = []
        total = 0
        with open(full, "rb") as handle:
            while total < size:
                piece = handle.read(
                    min(_OUTPUT_READ_CHUNK_BYTES, size - total)
                )
                if not piece:
                    break
                chunks.append(piece)
                total += len(piece)
        return b"".join(chunks)

    def _unexecuted(
        self,
        bundle: SolverBundle,
        attempt: AttemptContext,
        executor_ref: str,
        executable_ref: str,
        error: str,
        status: str = "policy_rejected",
    ) -> ExecutionReceipt:
        """执行前拒绝回执（进程未启动，无执行事实，不伪造）。

        ``status`` 只取 ``policy_rejected``/``cancelled``/``fenced``；
        停止类回执只带停止原因，不带退出事实与起止时间。
        """
        manifest = bundle.manifest
        return ExecutionReceipt(
            receipt_id=uuid.uuid4().hex,
            bundle_id=attempt.bundle_id,
            assembly_id=attempt.assembly_id,
            calculation_config_id=attempt.calculation_config_id,
            attempt_id=attempt.attempt_id,
            generator_ref=str(manifest.bundle["generator"]),
            solver_ref=str(manifest.bundle["solver"]),
            executor_ref=executor_ref,
            executable_ref=executable_ref,
            result_adapter_ref=manifest.result_adapter,
            status=status,
            termination_reason=(
                status if status in ("cancelled", "fenced") else ""
            ),
            error=error,
            policy_evaluated=self._policy.to_dict(),
        )

    def _build_receipt(
        self,
        bundle: SolverBundle,
        attempt: AttemptContext,
        provider: ExecutorProvider,
        command: Mapping[str, object],
        outcome: ProcessOutcome,
        status: str,
        error: str,
        collected: Mapping[str, bytes],
    ) -> ExecutionReceipt:
        """由过程结果与已核对输出签发唯一回执。"""
        manifest = bundle.manifest
        entries = []
        for entry in manifest.outputs:
            path = str(entry["path"])
            if path not in collected:
                continue
            entries.append(
                {
                    "path": path,
                    "media_type": str(entry["media_type"]),
                    "size_bytes": len(collected[path]),
                    "required": bool(entry["required"]),
                }
            )
        return ExecutionReceipt(
            receipt_id=uuid.uuid4().hex,
            bundle_id=attempt.bundle_id,
            assembly_id=attempt.assembly_id,
            calculation_config_id=attempt.calculation_config_id,
            attempt_id=attempt.attempt_id,
            generator_ref=str(manifest.bundle["generator"]),
            solver_ref=str(manifest.bundle["solver"]),
            executor_ref=provider.ref,
            executable_ref=str(command["executable"]),
            result_adapter_ref=manifest.result_adapter,
            status=status,
            exit_code=outcome.exit_code,
            signal_number=outcome.signal_number,
            termination_reason=outcome.termination_reason,
            started_at=_normalize_timestamp(outcome.started_at),
            ended_at=_normalize_timestamp(outcome.ended_at),
            error=error,
            stdout=outcome.stdout.decode("utf-8", errors="replace"),
            stderr=outcome.stderr.decode("utf-8", errors="replace"),
            stdout_truncated=outcome.stdout_truncated,
            stderr_truncated=outcome.stderr_truncated,
            outputs=entries,
            policy_evaluated=self._policy.to_dict(),
            resource_usage=dict(outcome.resource_usage),
        )


__all__ = ["SolverRuntime"]
