"""Ephemeral 6-step container stage runner and built-in stage catalog.

Enforces the ADR-0016 container stage lifecycle through ``JobRunnerClient``:
1. Reconcile existing stage containers by ``(attempt_id, stage_id)`` labels, or
   prepare ``<stage_root>/<workspace_rel>/in`` and call ``start_stage``;
2. Isolated execution (network-disabled, read-only rootfs, non-root, cgroup limits);
3. Wait for natural exit, user cancellation, lease fencing, or deployment/stage timeout;
4. ``inspect_stage`` and collect declared outputs directly from
   ``<stage_root>/<workspace_rel>/out`` plus cgroup v2 resource metrics;
5. Persist checkpoints/artifacts with Fencing Token via required ``persist`` callback;
6. ``cleanup_stage`` ONLY after ``persist`` succeeds.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from iesplan.computation.protocols import StopProbe, check_provider_ref, read_stop_reason
from iesplan.core.content import Content, MemoryContent

if TYPE_CHECKING:
    from iesplan.computation.runtime.job_runner_client import (
        CleanupStageResult,
        JobRunnerClient,
        StageInspection,
    )

_OUTPUT_READ_CHUNK_BYTES = 65_536
_TERMINAL_CONTAINER_STATUSES = frozenset(("exited", "dead"))
_VALID_STAGE_KINDS = frozenset(("predictor", "generator", "solver", "evaluator", "algorithm"))


class ControlPlaneDetached(Exception):
    """Control-plane shutdown detached while waiting on a stage container.

    Control-flow signal: caller must exit without stopping or cleaning up
    the live stage container or writing a terminal state to the active Attempt.
    """


def _is_detached(detach: threading.Event | Callable[[], bool] | None) -> bool:
    if detach is None:
        return False
    if callable(detach):
        return bool(detach())
    return bool(detach.is_set())


def _resolve_stage_subpath(root: Path, rel_path: str, *, field_name: str) -> Path:
    if not isinstance(rel_path, str) or not rel_path or rel_path.startswith("/"):
        raise ValueError(f"{field_name} must be a non-empty relative path: {rel_path!r}")
    normalized = os.path.normpath(rel_path)
    parts = Path(normalized).parts
    if not parts or parts[0] == ".." or ".." in parts:
        raise ValueError(f"{field_name} escapes workspace root: {rel_path!r}")
    root_resolved = root.resolve()
    target = (root_resolved / normalized).resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"{field_name} escapes workspace root: {rel_path!r}") from exc
    return target


@dataclass(frozen=True, slots=True)
class _StageFileContent:
    """Lazy chunk-streaming Content handle backed directly by /work/out without extra copies."""

    path: Path
    size_bytes: int

    def iter_chunks(self) -> Iterator[bytes]:
        with open(self.path, "rb") as handle:
            while chunk := handle.read(_OUTPUT_READ_CHUNK_BYTES):
                yield chunk


@dataclass(frozen=True, slots=True)
class ContainerStagePolicy:
    """Authoritative container stage limits/policy resolved from pies.toml."""

    builtin_image: str
    cpu_cores: float
    memory_mb: int
    pids_limit: int
    tmpfs_size_mb: int
    stop_grace_seconds: int
    max_timeout_seconds: float
    allowed_environment_vars: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BuiltinStageDescriptor:
    """Descriptor for an official built-in container stage plugin."""

    plugin_id: str
    version: str
    kind: str
    image: str
    entrypoint: str
    manifest_path: str = ""

    def __post_init__(self) -> None:
        check_provider_ref(self.ref, "stage_plugin")
        if self.kind not in _VALID_STAGE_KINDS:
            raise ValueError(f"unsupported stage kind: {self.kind!r}")
        if not isinstance(self.image, str) or not self.image.strip():
            raise ValueError("stage image must be a non-empty image reference")
        if not isinstance(self.entrypoint, str) or ":" not in self.entrypoint:
            raise ValueError(f"stage entrypoint must be <module>:<func>: {self.entrypoint!r}")

    @property
    def ref(self) -> str:
        return f"{self.plugin_id}@{self.version}"

    @property
    def command(self) -> tuple[str, ...]:
        """Container command matching pies-builtin-stages:1.1.5 runner contract."""
        if self.manifest_path:
            return ("python", "-m", "pies_sdk.runner", "--manifest", self.manifest_path)
        return ("python", "-m", "pies_sdk.runner")


class BuiltinStageCatalog:
    """Immutable catalog of official built-in stage container descriptors."""

    __slots__ = ("_by_ref", "_by_kind")

    def __init__(self, descriptors: tuple[BuiltinStageDescriptor, ...]) -> None:
        by_ref: dict[str, BuiltinStageDescriptor] = {}
        by_kind: dict[str, BuiltinStageDescriptor] = {}
        for item in descriptors:
            if item.ref in by_ref:
                raise ValueError(f"duplicate built-in stage ref: {item.ref!r}")
            by_ref[item.ref] = item
            by_kind.setdefault(item.kind, item)
        required_kinds = {"predictor", "generator", "solver", "evaluator"}
        missing = required_kinds - set(by_kind)
        if missing:
            raise ValueError(f"built-in stage catalog missing required kinds: {sorted(missing)}")
        self._by_ref = by_ref
        self._by_kind = by_kind

    def snapshot(self) -> tuple[BuiltinStageDescriptor, ...]:
        return tuple(self._by_ref[key] for key in sorted(self._by_ref))

    def get_by_ref(self, ref: str) -> BuiltinStageDescriptor:
        return self._by_ref[ref]

    def get_by_kind(self, kind: str) -> BuiltinStageDescriptor:
        return self._by_kind[kind]


def build_builtin_stage_catalog(builtin_image: str) -> BuiltinStageCatalog:
    """Construct the official 4-stage built-in container descriptor catalog."""
    return BuiltinStageCatalog(
        (
            BuiltinStageDescriptor(
                plugin_id="ies.predictor.naive",
                version="1.0.0",
                kind="predictor",
                image=builtin_image,
                entrypoint="predictor_stage:predict",
                manifest_path="/app/plugins/predictor_naive/plugin.yaml",
            ),
            BuiltinStageDescriptor(
                plugin_id="ies.generator.highs_milp",
                version="1.0.0",
                kind="generator",
                image=builtin_image,
                entrypoint="generator_stage:generate",
                manifest_path="/app/plugins/generator_highs_milp/plugin.yaml",
            ),
            BuiltinStageDescriptor(
                plugin_id="ies.solver.highs",
                version="1.7.2",
                kind="solver",
                image=builtin_image,
                entrypoint="solver_stage:solve",
                manifest_path="/app/plugins/solver_highs/plugin.yaml",
            ),
            BuiltinStageDescriptor(
                plugin_id="ies.result-adapter.highs",
                version="1.0.0",
                kind="evaluator",
                image=builtin_image,
                entrypoint="evaluator_stage:evaluate",
                manifest_path="/app/plugins/evaluator_highs/plugin.yaml",
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class StageExecutionSpec:
    """Caller specification for a single ephemeral container stage execution."""

    attempt_id: str
    stage_id: str
    fencing_token: str
    image: str | None = None
    stage_execution_id: str | None = None
    workspace_rel: str | None = None
    command: tuple[str, ...] | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    input_files: Mapping[str, Content | bytes] = field(default_factory=dict)
    prepare_inputs: Callable[[Path], None] | None = None
    required_outputs: tuple[str, ...] = ()
    optional_outputs: tuple[str, ...] = ()
    collect_all_outputs: bool = False
    timeout_seconds: float | None = None
    cpu_cores: float | None = None
    memory_mb: int | None = None
    pids_limit: int | None = None
    tmpfs_size_mb: int | None = None
    iterations_tail: int = 20


@dataclass(frozen=True, slots=True)
class StageRunResult:
    """Collected stage execution facts, declared outputs, and cleanup status."""

    stage_execution_id: str
    attempt_id: str
    stage_id: str
    fencing_token: str
    workspace_rel: str
    status: str
    reused: bool
    exit_code: int | None
    termination_reason: str | None
    started_at: str | None
    finished_at: str | None
    resources: Mapping[str, Any]
    iterations: Mapping[str, Any]
    outputs: Mapping[str, Content]
    missing_required_outputs: tuple[str, ...]
    output_files: tuple[str, ...]
    cleaned_up: bool = False
    cleanup_result: CleanupStageResult | None = None
    persisted_result: Any = None


class ContainerStageRunner:
    """6-step ephemeral container stage orchestrator backed by job_runner."""

    def __init__(
        self,
        client: JobRunnerClient,
        *,
        stage_root: Path,
        policy: ContainerStagePolicy,
        poll_interval: float = 0.25,
    ) -> None:
        self.client = client
        self.stage_root = Path(stage_root)
        self.policy = policy
        self.poll_interval = float(poll_interval)

    def list_stages(
        self,
        *,
        attempt_id: str | None = None,
        stage_id: str | None = None,
        include_stats: bool = False,
    ) -> tuple[StageInspection, ...]:
        return self.client.list_stages(
            attempt_id=attempt_id,
            stage_id=stage_id,
            include_stats=include_stats,
        )

    def stop_stage(
        self,
        stage_execution_id: str,
        *,
        reason: str = "cancelled",
        grace_seconds: int | None = None,
    ) -> StageInspection:
        grace = self.policy.stop_grace_seconds if grace_seconds is None else grace_seconds
        return self.client.stop_stage(stage_execution_id, reason=reason, grace_seconds=grace)

    def cleanup_stage(self, stage_execution_id: str, *, force: bool = False) -> CleanupStageResult:
        return self.client.cleanup_stage(stage_execution_id, force=force)

    def run_stage(
        self,
        spec: StageExecutionSpec,
        *,
        persist: Callable[[StageRunResult], Any],
        stop: StopProbe | None = None,
        detach: threading.Event | Callable[[], bool] | None = None,
    ) -> StageRunResult:
        """Execute a stage container enforcing persist-before-cleanup and restart reconciliation."""
        if not callable(persist):
            raise TypeError("persist callback is required before stage cleanup")
        if stop is not None and not callable(stop):
            raise TypeError(f"stop probe must be callable, got {type(stop).__name__}")

        from iesplan.computation.runtime.job_runner_client import StartStageSpec

        stage_execution_id = spec.stage_execution_id or f"{spec.attempt_id}--{spec.stage_id}"
        workspace_rel = spec.workspace_rel or stage_execution_id
        # Validate workspace_rel path safety before touching local stage_root.
        _resolve_stage_subpath(self.stage_root, workspace_rel, field_name="workspace_rel")

        resolved_cpu = self._resolve_cpu_cores(spec.cpu_cores)
        resolved_mem = self._resolve_memory_mb(spec.memory_mb)
        resolved_pids = spec.pids_limit if spec.pids_limit is not None else self.policy.pids_limit
        resolved_tmpfs = (
            spec.tmpfs_size_mb if spec.tmpfs_size_mb is not None else self.policy.tmpfs_size_mb
        )
        resolved_timeout = self._resolve_timeout(spec.timeout_seconds)
        resolved_env = self._filter_environment(spec.environment)
        resolved_image = spec.image or self.policy.builtin_image

        # Reconcile existing managed containers by (attempt_id, stage_id) labels FIRST so
        # any cancelled/fenced/timed-out container left running across a restart is stopped,
        # inspected, persisted, and cleaned up rather than skipped by pre-start checks.
        existing_matches = self.client.list_stages(
            attempt_id=spec.attempt_id,
            stage_id=spec.stage_id,
            iterations_tail=spec.iterations_tail,
            include_stats=False,
        )
        reused = False
        wait_timeout = resolved_timeout
        if existing_matches:
            inspection = existing_matches[0]
            reused = True
            stage_execution_id = inspection.stage_execution_id
            workspace_rel = inspection.workspace_rel
            if inspection.status == "created":
                inspection = self.client.start_stage(
                    StartStageSpec(
                        attempt_id=spec.attempt_id,
                        stage_id=spec.stage_id,
                        fencing_token=spec.fencing_token,
                        image=resolved_image,
                        stage_execution_id=stage_execution_id,
                        workspace_rel=workspace_rel,
                        command=spec.command,
                        environment=resolved_env,
                        cpu_cores=resolved_cpu,
                        memory_mb=resolved_mem,
                        pids_limit=resolved_pids,
                        tmpfs_size_mb=resolved_tmpfs,
                    )
                )
            wait_timeout = self._remaining_timeout(resolved_timeout, inspection.started_at)
        else:
            if _is_detached(detach):
                raise ControlPlaneDetached(
                    f"control plane detached before launching stage {stage_execution_id}"
                )
            pre_stop = read_stop_reason(stop)
            if pre_stop is not None:
                return StageRunResult(
                    stage_execution_id=stage_execution_id,
                    attempt_id=str(spec.attempt_id),
                    stage_id=spec.stage_id,
                    fencing_token=str(spec.fencing_token),
                    workspace_rel=workspace_rel,
                    status=pre_stop,
                    reused=False,
                    exit_code=None,
                    termination_reason=pre_stop,
                    started_at=None,
                    finished_at=None,
                    resources={},
                    iterations={"present": False, "count": 0, "tail": []},
                    outputs={},
                    missing_required_outputs=(),
                    output_files=(),
                    cleaned_up=False,
                )

            self._prepare_workspace(workspace_rel, spec)
            inspection = self.client.start_stage(
                StartStageSpec(
                    attempt_id=str(spec.attempt_id),
                    stage_id=spec.stage_id,
                    fencing_token=str(spec.fencing_token),
                    image=resolved_image,
                    stage_execution_id=stage_execution_id,
                    workspace_rel=workspace_rel,
                    command=spec.command,
                    environment=resolved_env,
                    cpu_cores=resolved_cpu,
                    memory_mb=resolved_mem,
                    pids_limit=resolved_pids,
                    tmpfs_size_mb=resolved_tmpfs,
                )
            )
            reused = inspection.reused
            if reused:
                wait_timeout = self._remaining_timeout(resolved_timeout, inspection.started_at)

        # Step 2 & 3: Wait for natural exit, cancel, fence, detach, or remaining timeout.
        inspection = self._wait_for_exit(
            stage_execution_id,
            initial=inspection,
            timeout_seconds=wait_timeout,
            stop=stop,
            detach=detach,
        )

        # Step 4: Inspect final state + collect declared outputs directly from <stage_root>/<rel>/out.
        final_inspection = self.client.inspect_stage(
            stage_execution_id,
            iterations_tail=spec.iterations_tail,
            include_stats=True,
        )
        out_dir = _resolve_stage_subpath(
            self.stage_root, f"{workspace_rel}/out", field_name="workspace_rel"
        )
        collected, missing_required = self._collect_declared_outputs(
            out_dir,
            required_outputs=spec.required_outputs,
            optional_outputs=spec.optional_outputs,
            discovered_files=final_inspection.output_files,
            collect_all_outputs=spec.collect_all_outputs,
        )
        status = self._derive_stage_status(final_inspection, missing_required)

        result = StageRunResult(
            stage_execution_id=final_inspection.stage_execution_id,
            attempt_id=final_inspection.attempt_id,
            stage_id=final_inspection.stage_id,
            fencing_token=final_inspection.fencing_token,
            workspace_rel=workspace_rel,
            status=status,
            reused=reused,
            exit_code=final_inspection.exit_code,
            termination_reason=final_inspection.termination_reason,
            started_at=final_inspection.started_at,
            finished_at=final_inspection.finished_at,
            resources=final_inspection.resources,
            iterations=final_inspection.iterations,
            outputs=collected,
            missing_required_outputs=missing_required,
            output_files=final_inspection.output_files,
        )

        # Step 5: Persist checkpoints / artifacts with Fencing Token BEFORE cleanup.
        persisted_value = persist(result)

        # Step 6: Cleanup container & temporary workspace ONLY after persistence succeeds.
        cleanup_result = self.client.cleanup_stage(stage_execution_id, force=False)
        return StageRunResult(
            stage_execution_id=result.stage_execution_id,
            attempt_id=result.attempt_id,
            stage_id=result.stage_id,
            fencing_token=result.fencing_token,
            workspace_rel=result.workspace_rel,
            status=result.status,
            reused=result.reused,
            exit_code=result.exit_code,
            termination_reason=result.termination_reason,
            started_at=result.started_at,
            finished_at=result.finished_at,
            resources=result.resources,
            iterations=result.iterations,
            outputs=result.outputs,
            missing_required_outputs=result.missing_required_outputs,
            output_files=result.output_files,
            cleaned_up=True,
            cleanup_result=cleanup_result,
            persisted_result=persisted_value,
        )

    def _resolve_cpu_cores(self, requested: float | None) -> float:
        if requested is None:
            return self.policy.cpu_cores
        if requested <= 0 or requested > self.policy.cpu_cores:
            raise ValueError(
                f"cpu_cores={requested!r} exceeds policy limit {self.policy.cpu_cores!r}"
            )
        return float(requested)

    def _resolve_memory_mb(self, requested: int | None) -> int:
        if requested is None:
            return self.policy.memory_mb
        if requested <= 0 or requested > self.policy.memory_mb:
            raise ValueError(
                f"memory_mb={requested!r} exceeds policy limit {self.policy.memory_mb!r}"
            )
        return int(requested)

    def _resolve_timeout(self, requested: float | None) -> float:
        if requested is None:
            return float(self.policy.max_timeout_seconds)
        if requested <= 0 or requested > self.policy.max_timeout_seconds:
            raise ValueError(
                f"timeout_seconds={requested!r} exceeds policy limit "
                f"{self.policy.max_timeout_seconds!r}"
            )
        return float(requested)

    @staticmethod
    def _remaining_timeout(timeout_seconds: float, started_at: str | None) -> float:
        if not started_at:
            return timeout_seconds
        try:
            normalized = started_at[:-1] + "+00:00" if started_at.endswith("Z") else started_at
            started = datetime.fromisoformat(normalized)
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            elapsed = max(0.0, (datetime.now(UTC) - started.astimezone(UTC)).total_seconds())
            return max(0.0, timeout_seconds - elapsed)
        except ValueError:
            return timeout_seconds

    def _filter_environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        denied = sorted(k for k in environment if k not in self.policy.allowed_environment_vars)
        if denied:
            raise ValueError(f"environment variables not in allowlist: {denied!r}")
        return {str(k): str(v) for k, v in environment.items()}

    def _prepare_workspace(self, workspace_rel: str, spec: StageExecutionSpec) -> None:
        workspace_dir = _resolve_stage_subpath(
            self.stage_root, workspace_rel, field_name="workspace_rel"
        )
        in_dir = workspace_dir / "in"
        out_dir = workspace_dir / "out"
        in_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        for rel_path, content in spec.input_files.items():
            target = _resolve_stage_subpath(in_dir, rel_path, field_name="input_files")
            target.parent.mkdir(parents=True, exist_ok=True)
            src_path = getattr(content, "path", None)
            if src_path is not None:
                try:
                    try:
                        os.link(src_path, target)
                    except FileExistsError:
                        target.unlink()
                        os.link(src_path, target)
                    continue
                except OSError:
                    pass
            chunk_source = MemoryContent(content) if isinstance(content, bytes) else content
            with open(target, "wb") as handle:
                for chunk in chunk_source.iter_chunks():
                    handle.write(chunk)
        if spec.prepare_inputs is not None:
            spec.prepare_inputs(in_dir)

    def _wait_for_exit(
        self,
        stage_execution_id: str,
        *,
        initial: StageInspection,
        timeout_seconds: float,
        stop: StopProbe | None,
        detach: threading.Event | Callable[[], bool] | None = None,
    ) -> StageInspection:
        inspection = initial
        deadline = time.monotonic() + timeout_seconds
        while inspection.running or inspection.status not in _TERMINAL_CONTAINER_STATUSES:
            if _is_detached(detach):
                raise ControlPlaneDetached(
                    f"control plane detached while waiting for stage {stage_execution_id}"
                )
            stop_reason = read_stop_reason(stop)
            if stop_reason is not None:
                return self.client.stop_stage(
                    stage_execution_id,
                    reason=stop_reason,
                    grace_seconds=self.policy.stop_grace_seconds,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stop_reason = read_stop_reason(stop)
                reason = stop_reason if stop_reason is not None else "timed_out"
                return self.client.stop_stage(
                    stage_execution_id,
                    reason=reason,
                    grace_seconds=self.policy.stop_grace_seconds,
                )
            wait_duration = min(self.poll_interval, remaining)
            if isinstance(detach, threading.Event):
                if detach.wait(timeout=wait_duration):
                    raise ControlPlaneDetached(
                        f"control plane detached while waiting for stage {stage_execution_id}"
                    )
            else:
                time.sleep(wait_duration)
                if _is_detached(detach):
                    raise ControlPlaneDetached(
                        f"control plane detached while waiting for stage {stage_execution_id}"
                    )
            inspection = self.client.inspect_stage(
                stage_execution_id,
                iterations_tail=0,
                include_stats=False,
            )
        return inspection

    @staticmethod
    def _collect_declared_outputs(
        out_dir: Path,
        *,
        required_outputs: tuple[str, ...],
        optional_outputs: tuple[str, ...],
        discovered_files: tuple[str, ...],
        collect_all_outputs: bool = False,
    ) -> tuple[dict[str, Content], tuple[str, ...]]:
        declared = list(dict.fromkeys((*required_outputs, *optional_outputs)))
        if collect_all_outputs:
            candidates = list(dict.fromkeys((*declared, *discovered_files)))
        else:
            candidates = declared if declared else list(discovered_files)
        collected: dict[str, Content] = {}
        missing_required: list[str] = []
        for rel_path in candidates:
            target = _resolve_stage_subpath(out_dir, rel_path, field_name="output_path")
            if target.is_file():
                collected[rel_path] = _StageFileContent(target, target.stat().st_size)
            elif rel_path in required_outputs:
                missing_required.append(rel_path)
        return collected, tuple(sorted(missing_required))

    @staticmethod
    def _derive_stage_status(
        inspection: StageInspection,
        missing_required: tuple[str, ...],
    ) -> str:
        reason = inspection.termination_reason
        if reason in ("cancelled", "fenced", "timed_out", "oom"):
            return reason
        if reason == "normal" or (reason is None and inspection.exit_code == 0):
            if missing_required:
                return "output_protocol_failed"
            return "succeeded"
        return "process_failed"


__all__ = [
    "BuiltinStageCatalog",
    "BuiltinStageDescriptor",
    "ContainerStagePolicy",
    "ContainerStageRunner",
    "ControlPlaneDetached",
    "StageExecutionSpec",
    "StageRunResult",
    "build_builtin_stage_catalog",
]
