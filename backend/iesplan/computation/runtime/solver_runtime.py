"""通用求解运行时：具体、可调用的 Bundle 执行编排。

``SolverRuntime`` 是通用实现（非 Protocol/ABC/占位）：消费完整 typed
``SolverBundle``、attempt 上下文、部署策略与已发布执行器注册表，拥有
执行编排全过程——精确选择、策略检查、独立临时工作区、输入只读写入、
executor 调用、声明输出核对、唯一回执签发与工作区清理。

信任边界：typed ``SolverBundle`` 是可信内部交接，不重跑 JSON schema，
不算 hash，不重复装配或文件内容校验；只执行本层拥有的真实外部进程
边界门禁（精确 executor/executable、环境 allowlist、资源上限、网络
开关、隔离目录与声明输出范围）。资源需求只做策略上限比对，不做真实
隔离实施；进程启动、取消、超时与回收归 provider 实现。
"""

from __future__ import annotations

import os
import shutil
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
)
from iesplan.computation.runtime.context import AttemptContext
from iesplan.computation.runtime.policy import DeploymentPolicy
from iesplan.computation.runtime.registry import ExecutorRegistry

#: 命令未声明 success_exit_codes 时的协议默认（仅判断进程是否按协议结束）。
_DEFAULT_SUCCESS_EXIT_CODES = (0,)


def _normalize_timestamp(value: str) -> str:
    """把可解析时间戳归一化为 ISO 8601 UTC ``Z`` 字尾（无时区视为 UTC）。"""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class SolverRuntime:
    """通用求解执行编排（组合根持有，Worker 经 application 调用）。

    构造依赖已发布的 ``ExecutorRegistry`` 与 ``DeploymentPolicy``；
    ``run`` 每次调用建立独立临时工作区，结束后清理。执行前拒绝一律
    返回 ``policy_rejected`` 回执且不调用 executor；executor 调用后
    的非零退出、必需输出缺失、未声明输出分别形成明确回执，不回退、
    不切换 executor、不伪造成功；只有无法建立可信回执的基础设施错误
    （工作区不可建、executor 抛异常、清理失败）才向上传播异常。
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
        self, bundle: SolverBundle, attempt: AttemptContext
    ) -> tuple[ExecutionReceipt, dict[str, bytes]]:
        """执行完整 Bundle，返回 ``(唯一回执, 已核对声明输出字节)``。

        状态优先级：输出边界违规（未声明文件/符号链接/不可读）→
        ``output_protocol_failed``；退出码不在成功集合 → ``process_failed``；
        必需输出缺失 → ``output_protocol_failed``；否则 ``succeeded``。
        成功与失败都尽量形成回执；输出映射只含已核对的声明输出。
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
        manifest = bundle.manifest
        command = manifest.command
        executor_ref = command["executor"]
        executable_ref = command["executable"]
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
            outcome = provider.execute(command, root)
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
        目录仅预建，不改变输入/输出定位。输入文件置只读（0o444），
        输入祖先目录（不含根）置只读可遍历（0o555）；该位语义只防误写，
        不声称为完整安全隔离，不可绕过的只读挂载归生产隔离 provider。
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
            os.chmod(target, 0o444)
            ancestor = os.path.dirname(target)
            while ancestor.startswith(root + os.sep):
                os.chmod(ancestor, 0o555)
                ancestor = os.path.dirname(ancestor)
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
        boundary_errors = self._scan_workdir(root, declared, input_paths)
        collected: dict[str, bytes] = {}
        for path in declared:
            full = os.path.join(root, str(path))
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            try:
                with open(full, "rb") as handle:
                    collected[str(path)] = handle.read()
            except OSError:
                boundary_errors.append(f"声明输出不可读: {path!r}")
        success_codes = tuple(
            command.get("success_exit_codes", _DEFAULT_SUCCESS_EXIT_CODES)
        )
        if boundary_errors:
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
        else:
            missing = sorted(
                str(entry["path"])
                for entry in manifest.outputs
                if entry["required"] and str(entry["path"]) not in collected
            )
            if missing:
                status = "output_protocol_failed"
                error = f"必需输出缺失: {missing!r}"
            else:
                status = "succeeded"
                error = ""
        receipt = self._build_receipt(
            bundle, attempt, provider, command, outcome, status, error, collected
        )
        return receipt, collected

    def _scan_workdir(
        self,
        root: str,
        declared: Mapping[str, object],
        input_paths: set[str],
    ) -> list[str]:
        """扫描工作区：符号链接与未声明文件均为输出边界违规。"""
        errors: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            for name in list(dirnames) + filenames:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root)
                if os.path.islink(full):
                    errors.append(f"工作区存在符号链接: {rel!r}")
                    continue
                if os.path.isdir(full):
                    continue
                if rel not in declared and rel not in input_paths:
                    errors.append(f"未声明输出: {rel!r}")
        return errors

    def _unexecuted(
        self,
        bundle: SolverBundle,
        attempt: AttemptContext,
        executor_ref: str,
        executable_ref: str,
        error: str,
    ) -> ExecutionReceipt:
        """执行前拒绝回执（进程未启动，无执行事实，不伪造）。"""
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
            status="policy_rejected",
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
            outputs=entries,
            policy_evaluated=self._policy.to_dict(),
            resource_usage=dict(outcome.resource_usage),
        )


__all__ = ["SolverRuntime"]
