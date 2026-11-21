"""通用求解运行时：具体、可调用的 Bundle 执行编排。

``SolverRuntime`` 是通用实现（非 Protocol/ABC/占位）：消费完整 typed
``SolverBundle``、attempt 上下文、部署策略、已发布执行器注册表与调用方
停止探针，拥有执行编排全过程——启动前停止检查、精确选择、策略检查、
独立临时工作区、声明输出的分块交接、回执签发与工作区清理。

执行器选择及部署策略在运行时应用。进程启动、运行中停止、超时与回收归 provider 实现。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
import weakref
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.protocols import (
    ExecutionReceipt,
    ExecutorProvider,
    ProcessOutcome,
    SolverBundle,
    StopProbe,
    read_stop_reason,
)
from iesplan.computation.runtime.context import AttemptContext
from iesplan.computation.runtime.policy import DeploymentPolicy
from iesplan.computation.runtime.registry import ExecutorRegistry
from iesplan.core.content import Content

#: 命令未声明 success_exit_codes 时的协议默认（仅判断进程是否按协议结束）。
_DEFAULT_SUCCESS_EXIT_CODES = (0,)

#: 声明输出单次读取块大小（字节）。
_OUTPUT_READ_CHUNK_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class _OutputContent:
    filename: str
    size_bytes: int

    def iter_chunks(self) -> Iterator[bytes]:
        with open(self.filename, "rb") as source:
            while chunk := source.read(_OUTPUT_READ_CHUNK_BYTES):
                yield chunk


class RuntimeOutputs(dict[str, Content]):
    """Runtime-owned output lifetime. Read before close; no paths escape the runtime."""

    def __init__(self, values: Mapping[str, Content], root: str):
        super().__init__(values)
        self._cleanup = weakref.finalize(self, shutil.rmtree, root)

    def close(self) -> None:
        self._cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _normalize_timestamp(value: str) -> str:
    """把带时区时间戳归一化为 ISO 8601 UTC ``Z`` 字尾。"""
    return (
        datetime.fromisoformat(value)
        .astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


class SolverRuntime:
    """通用求解执行编排（组合根持有，Worker 经 application 调用）。

    构造依赖已发布的 ``ExecutorRegistry`` 与 ``DeploymentPolicy``；
    ``run`` 每次调用建立独立临时工作区，输出交接完成后清理。门禁或执行器解析
    失败返回 ``policy_rejected`` 回执且不调用 executor；启动前停止
    （含 provider 启动瞬间观测到的停止，此时进程必然未启动）返回
    ``cancelled``/``fenced`` 未执行回执；executor 调用后的非零退出、
    必需输出缺失分别形成明确回执；只有无法建立可信回执的基础设施错误
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
        handoff: Callable[[ExecutionReceipt, Mapping[str, Content]], Mapping[str, Content]] | None = None,
    ) -> tuple[ExecutionReceipt, Mapping[str, Content]]:
        """执行完整 Bundle，返回 ``(唯一回执, 声明输出内容)``。

        ``stop`` 为调用方停止探针：启动前已停止即不启动进程，直接签发
        ``cancelled``/``fenced`` 回执（含 provider 启动瞬间观测到的停止，
        此时进程必然未启动，收口为同一未执行回执而非异常）；运行中停止
        由 provider 观测并终止进程组。状态优先级：真实超时 → ``timed_out``
        为主状态；主进程
        退出后仍有组成员被强制收口 → ``process_failed``
        （``group_terminated`` 不得签发 ``succeeded``）；运行中停止 →
        ``cancelled``/``fenced`` 为主状态；凭 cgroup oom_kill 证据标定的
        OOM → ``process_failed``（``oom``）；退出码不在成功集合 →
        ``process_failed``；必需输出缺失 →
        ``output_protocol_failed``；否则 ``succeeded``。成功与失败都
        尽量形成回执；输出映射只含声明文件。
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
        delivered = True
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
            receipt, outputs = self._finish(
                bundle, attempt, provider, command, outcome, root
            )
            if handoff is not None:
                delivered = False
                outputs = handoff(receipt, outputs)
                delivered = True
            else:
                outputs = RuntimeOutputs(outputs, root)
                delivered = False
            return receipt, outputs
        finally:
            if delivered:
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
        """在独立工作区写入输入并建立声明输出的父目录。"""
        run_sub = os.path.normpath(
            os.path.join(root, str(command.get("working_directory", ".")))
        )
        os.makedirs(run_sub, exist_ok=True)
        for path, content in bundle.files.items():
            target = os.path.join(root, path)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as handle:
                for chunk in content.iter_chunks():
                    handle.write(chunk)
        for entry in bundle.manifest.outputs:
            parent = os.path.dirname(str(entry["path"]))
            if parent:
                os.makedirs(os.path.join(root, parent), exist_ok=True)
        return root

    def _finish(
        self,
        bundle: SolverBundle,
        attempt: AttemptContext,
        provider: ExecutorProvider,
        command: Mapping[str, object],
        outcome: ProcessOutcome,
        root: str,
    ) -> tuple[ExecutionReceipt, dict[str, Content]]:
        """核对输出边界并签发回执（见 ``run`` 状态优先级）。"""
        manifest = bundle.manifest
        collected = self._collect_declared(root, manifest.outputs)
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
            if missing:
                parts.append(f"必需输出缺失: {missing!r}")
            error = "; ".join(parts)
        elif reason == "oom":
            status = "process_failed"
            usage = outcome.resource_usage
            applied = usage.get("applied")
            observed = usage.get("observed")
            parts = [
                "求解器被 OOM 终结: "
                f"signal={outcome.signal_number!r} "
                "oom_kill_delta="
                f"{observed.get('oom_kill_delta') if isinstance(observed, Mapping) else None!r} "
                "memory_limit_bytes="
                f"{applied.get('memory_limit_bytes') if isinstance(applied, Mapping) else None!r}"
            ]
            if missing:
                parts.append(f"必需输出缺失: {missing!r}")
            error = "; ".join(parts)
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

    @staticmethod
    def _collect_declared(root: str, outputs: tuple) -> dict[str, Content]:
        collected: dict[str, Content] = {}
        for entry in outputs:
            path = str(entry["path"])
            filename = os.path.join(root, path)
            try:
                size = os.lstat(filename).st_size
            except FileNotFoundError:
                continue
            collected[path] = _OutputContent(filename, size)
        return collected

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
        collected: Mapping[str, Content],
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
                    "size_bytes": collected[path].size_bytes,
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
