"""Docker Engine operations for job_runner's 5 fixed business operations."""

from __future__ import annotations

import io
import json
import os
import queue
import shutil
import socket
import tarfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic as _monotonic
from typing import Any

import docker
from docker.errors import APIError, DockerException, ImageNotFound
from docker.types import Mount

from pies_job_runner.config import JobRunnerSettings
from pies_job_runner.metrics import collect_stage_resources, read_iterations_summary, save_stats_snapshot
from pies_job_runner.models import (
    BuildPluginRequest,
    BuildPluginResponse,
    CleanupStageResponse,
    DeletePluginImageRequest,
    DeletePluginImageResponse,
    StageInspectResponse,
    StageListResponse,
    StartStageRequest,
    StopReason,
    TerminationReason,
)
from pies_job_runner.paths import UnsafePathError, resolve_relative_subpath

LABEL_MANAGED = "pies.managed"
LABEL_PLUGIN_ID = "pies.plugin_id"
LABEL_PLUGIN_VERSION = "pies.plugin_version"
LABEL_ATTEMPT_ID = "pies.attempt_id"
LABEL_STAGE_ID = "pies.stage_id"
LABEL_STAGE_EXECUTION_ID = "pies.stage_execution_id"
LABEL_FENCING_TOKEN = "pies.fencing_token"
LABEL_WORKSPACE_REL = "pies.workspace_rel"

_STOP_REASON_FILE = ".job_runner_stop.json"
_ZERO_DOCKER_TIME = "0001-01-01T00:00:00Z"


class JobRunnerError(Exception):
    """Base domain/proxy error mapped to HTTP status codes in app.py."""

    status_code: int = 500


class BadRequestError(JobRunnerError):
    status_code = 400


class StageNotFoundError(JobRunnerError):
    status_code = 404


class ConflictError(JobRunnerError):
    status_code = 409


class DockerUnavailableError(JobRunnerError):
    status_code = 503


class PluginBuildError(JobRunnerError):
    status_code = 400


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _normalize_docker_timestamp(value: str | None) -> str | None:
    if not value or value.startswith("0001-01-01"):
        return None
    if value.endswith("Z"):
        # Trim sub-microsecond nanoseconds to standard ISO 8601 UTC Z if needed
        if "." in value:
            prefix, frac = value[:-1].split(".", 1)
            return f"{prefix}.{frac[:6]}Z"
        return value
    return value


def render_plugin_dockerfile(
    *,
    base_image: str,
    plugin_id: str,
    version: str,
    requirements_rel: str | None,
    pip_index_url: str | None = None,
    runtime_user: str = "10001:10001",
) -> str:
    """Render the controlled official Dockerfile template atop ``pies-plugin-base``."""
    lines = [
        f"FROM {base_image}",
        "USER root",
        "WORKDIR /plugin",
    ]
    if requirements_rel:
        target_req = f"/plugin/{requirements_rel}"
        pip_cmd = ["pip", "install", "--no-cache-dir"]
        if pip_index_url:
            pip_cmd.extend(["--index-url", pip_index_url])
        pip_cmd.extend(["-r", target_req])
        lines.extend(
            [
                f"COPY {json.dumps([requirements_rel, target_req])}",
                f"RUN {json.dumps(pip_cmd)}",
            ]
        )
    lines.extend(
        [
            "COPY . /plugin",
            "RUN chmod -R a+rX /plugin",
            "ENV PYTHONPATH=/plugin",
            (
                f'LABEL {LABEL_MANAGED}="true" '
                f'{LABEL_PLUGIN_ID}="{plugin_id}" '
                f'{LABEL_PLUGIN_VERSION}="{version}"'
            ),
            f"USER {runtime_user}",
            'ENTRYPOINT ["python", "-m", "pies_sdk.runner"]',
        ]
    )
    return "\n".join(lines) + "\n"


def _build_plugin_tar_context(source_dir: Path, dockerfile_content: str) -> io.BytesIO:
    """Pack plugin directory into an in-memory tar stream with our controlled Dockerfile."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for root_str, dirs, files in os.walk(source_dir, followlinks=False):
            dirs.sort()
            files.sort()
            root_path = Path(root_str)
            rel_root = root_path.relative_to(source_dir)
            for filename in files:
                file_path = root_path / filename
                if file_path.is_symlink():
                    raise BadRequestError(f"plugin build directory must not contain symlinks: {filename}")
                rel_path = (rel_root / filename).as_posix() if rel_root != Path(".") else filename
                # Never allow a plugin package to supply its own Dockerfile.
                if rel_path in ("Dockerfile", ".dockerignore"):
                    continue
                tar.add(file_path, arcname=rel_path, recursive=False)

        df_bytes = dockerfile_content.encode("utf-8")
        info = tarfile.TarInfo(name="Dockerfile")
        info.size = len(df_bytes)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(df_bytes))

    buf.seek(0)
    return buf


def _close_stream_best_effort(stream: Any) -> None:
    """Close a Docker build stream and any underlying HTTP response to abort blocked reads."""
    if stream is None:
        return
    frame = getattr(stream, "gi_frame", None)
    resp = frame.f_locals.get("response") if frame is not None else None
    if hasattr(resp, "close"):
        try:
            resp.close()
        except Exception:
            pass
    if hasattr(stream, "close"):
        try:
            stream.close()
        except Exception:
            pass


class DockerJobRunnerService:
    """Stateless Docker container proxy implementing the 5 fixed pIES job_runner operations."""

    def __init__(self, settings: JobRunnerSettings, client: docker.DockerClient | None = None) -> None:
        self.settings = settings
        self._client = client
        self._lock_guard = threading.Lock()
        self._stage_locks: dict[tuple[str, str], threading.Lock] = {}
        self._resolved_host_stage_root: Path | None = None
        self._host_ncpu: int | None = None

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            try:
                if self.settings.docker_host:
                    self._client = docker.DockerClient(base_url=self.settings.docker_host)
                else:
                    self._client = docker.from_env()
            except DockerException as exc:
                raise DockerUnavailableError(f"failed to initialize Docker client: {exc}") from exc
        return self._client

    def _get_host_ncpu(self) -> int | None:
        if self._host_ncpu is not None:
            return self._host_ncpu
        client = self._get_client()
        try:
            info = client.info()
        except DockerException:
            return None
        ncpu = info.get("NCPU") if isinstance(info, dict) else None
        if isinstance(ncpu, int) and ncpu > 0:
            self._host_ncpu = ncpu
            return ncpu
        return None

    def _get_stage_lock(self, attempt_id: str, stage_id: str) -> threading.Lock:
        key = (attempt_id, stage_id)
        with self._lock_guard:
            lock = self._stage_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._stage_locks[key] = lock
            return lock

    def check_ready(self) -> dict[str, Any]:
        """Verify Docker daemon ping, stage root, and required local images for ``GET /readyz``."""
        client = self._get_client()
        try:
            client.ping()
        except DockerException as exc:
            raise DockerUnavailableError(f"docker daemon unreachable: {exc}") from exc

        stage_root = self.settings.stage_root
        try:
            stage_root.mkdir(parents=True, exist_ok=True)
            if not stage_root.is_dir() or not os.access(stage_root, os.W_OK | os.X_OK):
                raise OSError(f"stage_root not writable: {stage_root}")
        except OSError as exc:
            raise DockerUnavailableError(f"stage_root unavailable: {exc}") from exc

        image_statuses: dict[str, str] = {}
        missing_images: list[str] = []
        for image_ref in self.settings.parsed_required_images():
            try:
                client.images.get(image_ref)
            except ImageNotFound:
                missing_images.append(image_ref)
            except DockerException as exc:
                raise DockerUnavailableError(
                    f"failed to inspect required image {image_ref}: {exc}"
                ) from exc
            else:
                image_statuses[image_ref] = "ok"

        if missing_images:
            raise DockerUnavailableError(
                f"required images missing locally: {', '.join(missing_images)}"
            )

        return {
            "status": "ready",
            "docker": "ok",
            "stage_root": str(stage_root),
            "required_images": image_statuses,
        }

    def _resolve_host_stage_root(self) -> Path:
        """Translate container-local ``stage_root`` to the Docker host bind-mount path."""
        if self.settings.host_stage_root is not None:
            return self.settings.host_stage_root
        if self._resolved_host_stage_root is not None:
            return self._resolved_host_stage_root

        stage_root = self.settings.stage_root.resolve()
        try:
            client = self._get_client()
            self_container = client.containers.get(socket.gethostname())
            mounts = self_container.attrs.get("Mounts") or []
            best_dest: Path | None = None
            best_source: Path | None = None
            for mount in mounts:
                if mount.get("Type") != "bind":
                    continue
                dest = Path(mount["Destination"]).resolve()
                try:
                    stage_root.relative_to(dest)
                except ValueError:
                    continue
                if best_dest is None or len(dest.parts) > len(best_dest.parts):
                    best_dest = dest
                    best_source = Path(mount["Source"])
            if best_dest is not None and best_source is not None:
                rel = stage_root.relative_to(best_dest)
                self._resolved_host_stage_root = best_source / rel
                return self._resolved_host_stage_root
        except (DockerException, OSError, KeyError, ValueError):
            pass

        self._resolved_host_stage_root = stage_root
        return self._resolved_host_stage_root

    def _host_path_for(self, workspace_rel: str, subdir: str) -> str:
        host_root = self._resolve_host_stage_root()
        return str(host_root / workspace_rel / subdir)

    def _workspace_dir_for(self, workspace_rel: str | None) -> Path | None:
        if not workspace_rel:
            return None
        try:
            _, path = resolve_relative_subpath(
                self.settings.stage_root, workspace_rel, field_name="workspace_rel"
            )
            return path
        except UnsafePathError:
            return None

    def _read_stop_reason(self, workspace_dir: Path | None) -> StopReason | None:
        if workspace_dir is None:
            return None
        stop_file = workspace_dir / _STOP_REASON_FILE
        try:
            data = json.loads(stop_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        reason = data.get("reason") if isinstance(data, dict) else None
        if reason in ("cancelled", "timed_out", "fenced"):
            return reason
        return None

    def _write_stop_reason(self, workspace_dir: Path | None, reason: StopReason) -> None:
        if workspace_dir is None or not workspace_dir.is_dir():
            return
        tmp = workspace_dir / f"{_STOP_REASON_FILE}.tmp.{os.getpid()}"
        try:
            tmp.write_text(json.dumps({"reason": reason, "recorded_at": _utc_now_iso()}), encoding="utf-8")
            tmp.replace(workspace_dir / _STOP_REASON_FILE)
        except OSError:
            tmp.unlink(missing_ok=True)

    def _find_container_by_execution_id(self, stage_execution_id: str) -> Any | None:
        client = self._get_client()
        try:
            matches = client.containers.list(
                all=True,
                filters={
                    "label": [
                        f"{LABEL_MANAGED}=true",
                        f"{LABEL_STAGE_EXECUTION_ID}={stage_execution_id}",
                    ]
                },
            )
        except DockerException as exc:
            raise DockerUnavailableError(f"docker list failed: {exc}") from exc
        return matches[0] if matches else None

    def _find_container_by_attempt_stage(self, attempt_id: str, stage_id: str) -> Any | None:
        client = self._get_client()
        try:
            matches = client.containers.list(
                all=True,
                filters={
                    "label": [
                        f"{LABEL_MANAGED}=true",
                        f"{LABEL_ATTEMPT_ID}={attempt_id}",
                        f"{LABEL_STAGE_ID}={stage_id}",
                    ]
                },
            )
        except DockerException as exc:
            raise DockerUnavailableError(f"docker list failed: {exc}") from exc
        return matches[0] if matches else None

    def _build_inspect_response(
        self,
        container: Any,
        *,
        reused: bool = False,
        iterations_tail: int = 20,
        include_stats: bool = True,
    ) -> StageInspectResponse:
        try:
            container.reload()
        except DockerException as exc:
            raise DockerUnavailableError(f"docker inspect failed: {exc}") from exc

        attrs = container.attrs or {}
        config = attrs.get("Config") or {}
        labels = config.get("Labels") or {}
        state = attrs.get("State") or {}

        status = str(state.get("Status") or container.status or "unknown")
        running = bool(state.get("Running", False))
        raw_exit_code = state.get("ExitCode")
        exit_code: int | None = (
            int(raw_exit_code)
            if (not running and status in ("exited", "dead") and isinstance(raw_exit_code, int))
            else None
        )

        workspace_rel = str(labels.get(LABEL_WORKSPACE_REL) or labels.get(LABEL_STAGE_EXECUTION_ID) or "")
        workspace_dir = self._workspace_dir_for(workspace_rel)
        out_dir = workspace_dir / "out" if workspace_dir is not None else None

        resources = collect_stage_resources(
            container,
            attrs,
            cgroup_root=self.settings.cgroup_root,
            workspace_dir=workspace_dir,
            running=running,
            include_stats=include_stats,
        )

        termination_reason: TerminationReason | None = None
        if not running and status in ("exited", "dead"):
            stop_reason = self._read_stop_reason(workspace_dir)
            oom_killed = resources.oom_killed.value is True or (
                isinstance(resources.oom_kill_count.value, int) and resources.oom_kill_count.value > 0
            )
            if oom_killed:
                termination_reason = "oom"
            elif stop_reason is not None:
                termination_reason = stop_reason
            elif exit_code == 0:
                termination_reason = "normal"
            else:
                termination_reason = "failed"

        if out_dir is not None and out_dir.is_dir():
            iterations = read_iterations_summary(out_dir, tail_limit=iterations_tail)
            output_files = sorted(
                p.relative_to(out_dir).as_posix()
                for p in out_dir.rglob("*")
                if p.is_file()
            )
        else:
            iterations = read_iterations_summary(Path("/nonexistent-out-dir"), tail_limit=0)
            output_files = []

        return StageInspectResponse(
            stage_execution_id=str(labels.get(LABEL_STAGE_EXECUTION_ID) or ""),
            attempt_id=str(labels.get(LABEL_ATTEMPT_ID) or ""),
            stage_id=str(labels.get(LABEL_STAGE_ID) or ""),
            fencing_token=str(labels.get(LABEL_FENCING_TOKEN) or ""),
            container_id=str(container.id),
            container_name=str(container.name),
            image=str(config.get("Image") or ""),
            workspace_rel=workspace_rel,
            reused=reused,
            status=status,
            running=running,
            exit_code=exit_code,
            termination_reason=termination_reason,
            started_at=_normalize_docker_timestamp(state.get("StartedAt")),
            finished_at=_normalize_docker_timestamp(state.get("FinishedAt")),
            resources=resources,
            iterations=iterations,
            output_files=output_files,
        )

    def build_plugin(self, request: BuildPluginRequest) -> BuildPluginResponse:
        """Build ``pies-plugin/<plugin_id>:<version>`` from the official Dockerfile template."""
        try:
            _, source_dir = resolve_relative_subpath(
                self.settings.plugin_source_root,
                request.source_rel,
                field_name="source_rel",
            )
        except UnsafePathError as exc:
            raise BadRequestError(str(exc)) from exc

        if not source_dir.is_dir():
            raise BadRequestError(f"plugin source directory does not exist: {request.source_rel}")

        requirements_rel: str | None = None
        if request.requirements_rel is not None:
            try:
                requirements_rel, _ = resolve_relative_subpath(
                    source_dir,
                    request.requirements_rel,
                    field_name="requirements_rel",
                )
            except UnsafePathError as exc:
                raise BadRequestError(str(exc)) from exc

        effective_timeout = (
            request.timeout_seconds
            if "timeout_seconds" in request.model_fields_set
            else self.settings.default_build_timeout_seconds
        )
        dockerfile_content = render_plugin_dockerfile(
            base_image=self.settings.plugin_base_image,
            plugin_id=request.plugin_id,
            version=request.version,
            requirements_rel=requirements_rel,
            pip_index_url=request.pip_index_url,
            runtime_user=self.settings.container_user,
        )
        tar_ctx = _build_plugin_tar_context(source_dir, dockerfile_content)
        image_tag = f"{self.settings.plugin_image_prefix}/{request.plugin_id}:{request.version}"

        client = self._get_client()
        logs: list[str] = []
        build_kwargs: dict[str, Any] = {
            "fileobj": tar_ctx,
            "custom_context": True,
            "tag": image_tag,
            "rm": True,
            "forcerm": True,
            "pull": False,
            "decode": True,
            "timeout": effective_timeout,
        }
        if requirements_rel is None:
            build_kwargs["network_mode"] = "none"

        deadline = _monotonic() + float(effective_timeout)
        chunk_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        cancelled = threading.Event()
        stream_holder: list[Any] = []

        def _drain_build_stream() -> None:
            stream: Any = None
            try:
                stream = client.api.build(**build_kwargs)
                stream_holder.append(stream)
                for item in stream:
                    if cancelled.is_set():
                        break
                    chunk_queue.put(("chunk", item))
            except Exception as exc:
                chunk_queue.put(("error", exc))
            finally:
                _close_stream_best_effort(stream)
                chunk_queue.put(("eof", None))

        worker = threading.Thread(
            target=_drain_build_stream,
            name=f"pies-build-{request.plugin_id}",
            daemon=True,
        )
        worker.start()
        try:
            while True:
                remaining = deadline - _monotonic()
                if remaining <= 0:
                    raise PluginBuildError(f"plugin image build timed out after {effective_timeout}s")
                try:
                    kind, payload = chunk_queue.get(timeout=remaining)
                except queue.Empty as exc:
                    raise PluginBuildError(
                        f"plugin image build timed out after {effective_timeout}s"
                    ) from exc
                if _monotonic() > deadline:
                    raise PluginBuildError(f"plugin image build timed out after {effective_timeout}s")
                if kind == "eof":
                    break
                if kind == "error":
                    raise payload
                if "error" in payload:
                    raise PluginBuildError(f"plugin image build failed: {payload['error']}")
                line = payload.get("stream")
                if isinstance(line, str) and line.strip():
                    logs.append(line.strip())
        except TimeoutError as exc:
            raise PluginBuildError(
                f"plugin image build timed out after {effective_timeout}s"
            ) from exc
        except DockerException as exc:
            raise DockerUnavailableError(f"docker build failed: {exc}") from exc
        finally:
            cancelled.set()
            for active_stream in stream_holder:
                _close_stream_best_effort(active_stream)

        return BuildPluginResponse(
            image=image_tag,
            plugin_id=request.plugin_id,
            version=request.version,
            built_at=_utc_now_iso(),
            logs=logs[-50:],
        )

    def delete_plugin_image(self, request: DeletePluginImageRequest) -> DeletePluginImageResponse:
        """Delete a managed plugin image strictly matching prefix and managed labels."""
        prefix = self.settings.plugin_image_prefix.strip("/")
        if not prefix or prefix.startswith(("pies-builtin-stages", "pies-plugin-base")):
            raise BadRequestError(f"invalid plugin image prefix: {self.settings.plugin_image_prefix!r}")
        image_tag = f"{prefix}/{request.plugin_id}:{request.version}"

        client = self._get_client()
        try:
            image = client.images.get(image_tag)
        except ImageNotFound:
            return DeletePluginImageResponse(
                image=image_tag,
                plugin_id=request.plugin_id,
                version=request.version,
                deleted=False,
                status="not_found",
            )
        except DockerException as exc:
            raise DockerUnavailableError(f"docker inspect image failed: {exc}") from exc

        attrs = getattr(image, "attrs", None)
        if not isinstance(attrs, dict):
            attrs = image if isinstance(image, dict) else {}
        labels = (attrs.get("Config") or {}).get("Labels") or getattr(image, "labels", None) or {}
        if (
            labels.get(LABEL_MANAGED) != "true"
            or labels.get(LABEL_PLUGIN_ID) != request.plugin_id
            or labels.get(LABEL_PLUGIN_VERSION) != request.version
        ):
            raise BadRequestError(
                f"refusing to delete unmanaged or label-mismatched image: {image_tag}"
            )

        try:
            client.images.remove(image=image_tag, force=bool(request.force))
        except ImageNotFound:
            return DeletePluginImageResponse(
                image=image_tag,
                plugin_id=request.plugin_id,
                version=request.version,
                deleted=False,
                status="not_found",
            )
        except APIError as exc:
            if getattr(exc, "status_code", None) == 409:
                raise ConflictError(f"plugin image is still in use: {image_tag}") from exc
            raise DockerUnavailableError(f"docker remove image failed: {exc}") from exc
        except DockerException as exc:
            raise DockerUnavailableError(f"docker remove image failed: {exc}") from exc

        return DeletePluginImageResponse(
            image=image_tag,
            plugin_id=request.plugin_id,
            version=request.version,
            deleted=True,
            status="deleted",
        )

    def start_stage(self, request: StartStageRequest) -> StageInspectResponse:
        """Start an isolated ephemeral stage container idempotently per ``(attempt_id, stage_id)``."""
        lock = self._get_stage_lock(request.attempt_id, request.stage_id)
        with lock:
            existing = self._find_container_by_attempt_stage(request.attempt_id, request.stage_id)
            if existing is not None:
                try:
                    existing.reload()
                except DockerException as exc:
                    raise DockerUnavailableError(f"docker inspect failed: {exc}") from exc
                existing_state = (existing.attrs or {}).get("State") or {}
                if existing_state.get("Status") == "created":
                    try:
                        existing.start()
                    except DockerException as exc:
                        raise DockerUnavailableError(
                            f"failed to start existing created stage container: {exc}"
                        ) from exc
                return self._build_inspect_response(existing, reused=True)

            stage_execution_id = request.stage_execution_id or f"{request.attempt_id}--{request.stage_id}"
            raw_workspace_rel = request.workspace_rel or stage_execution_id
            try:
                workspace_rel, workspace_dir = resolve_relative_subpath(
                    self.settings.stage_root,
                    raw_workspace_rel,
                    field_name="workspace_rel",
                )
            except UnsafePathError as exc:
                raise BadRequestError(str(exc)) from exc

            in_dir = workspace_dir / "in"
            out_dir = workspace_dir / "out"
            if not in_dir.is_dir():
                raise BadRequestError(
                    f"stage input directory missing under stage_root: {workspace_rel}/in"
                )
            out_dir.mkdir(parents=True, exist_ok=True)
            # Ephemeral containers run as a non-root user and must be able to write /work/out.
            os.chmod(out_dir, 0o777)

            effective_cpu = request.cpu_cores
            host_ncpu = self._get_host_ncpu()
            if host_ncpu is not None and host_ncpu > 0:
                effective_cpu = min(effective_cpu, float(host_ncpu))
            nano_cpus = int(effective_cpu * 1_000_000_000)
            if nano_cpus < 1_000_000:
                raise BadRequestError("cpu_cores must be at least 0.001")
            mem_spec = f"{request.memory_mb}m"

            run_kwargs: dict[str, Any] = {
                "image": request.image,
                "name": f"pies-stage-{stage_execution_id}",
                "detach": True,
                "network_mode": "none",
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "user": self.settings.container_user,
                "mounts": [
                    Mount(
                        target="/work/in",
                        source=self._host_path_for(workspace_rel, "in"),
                        type="bind",
                        read_only=True,
                    ),
                    Mount(
                        target="/work/out",
                        source=self._host_path_for(workspace_rel, "out"),
                        type="bind",
                        read_only=False,
                    ),
                ],
                "tmpfs": {"/tmp": f"rw,nosuid,nodev,size={request.tmpfs_size_mb}m"},
                "nano_cpus": nano_cpus,
                "mem_limit": mem_spec,
                "memswap_limit": mem_spec,
                "pids_limit": request.pids_limit,
                "environment": {"HOME": "/tmp", "PYTHONUNBUFFERED": "1", **request.environment},
                "labels": {
                    LABEL_MANAGED: "true",
                    LABEL_ATTEMPT_ID: request.attempt_id,
                    LABEL_STAGE_ID: request.stage_id,
                    LABEL_STAGE_EXECUTION_ID: stage_execution_id,
                    LABEL_FENCING_TOKEN: request.fencing_token,
                    LABEL_WORKSPACE_REL: workspace_rel,
                },
            }
            if request.command is not None:
                run_kwargs["command"] = request.command

            client = self._get_client()
            try:
                container = client.containers.run(**run_kwargs)
            except ImageNotFound as exc:
                raise BadRequestError(f"stage image not found locally: {request.image}") from exc
            except APIError as exc:
                if getattr(exc, "status_code", None) == 409:
                    existing_after = self._find_container_by_attempt_stage(
                        request.attempt_id, request.stage_id
                    )
                    if existing_after is not None:
                        return self._build_inspect_response(existing_after, reused=True)
                    raise ConflictError(
                        f"container conflict for stage_execution_id={stage_execution_id}"
                    ) from exc
                raise DockerUnavailableError(f"docker run failed: {exc}") from exc
            except DockerException as exc:
                raise DockerUnavailableError(f"docker run failed: {exc}") from exc

            return self._build_inspect_response(container, reused=False)

    def inspect_stage(
        self,
        stage_execution_id: str,
        *,
        iterations_tail: int = 20,
        include_stats: bool = True,
    ) -> StageInspectResponse:
        """Inspect a single managed stage container by ``stage_execution_id``."""
        container = self._find_container_by_execution_id(stage_execution_id)
        if container is None:
            raise StageNotFoundError(f"stage container not found: {stage_execution_id}")
        return self._build_inspect_response(
            container,
            reused=False,
            iterations_tail=iterations_tail,
            include_stats=include_stats,
        )

    def list_stages(
        self,
        *,
        attempt_id: str | None = None,
        stage_id: str | None = None,
        iterations_tail: int = 20,
        include_stats: bool = False,
    ) -> StageListResponse:
        """List managed stage containers for restart reconciliation, optionally filtered by labels."""
        label_filters = [f"{LABEL_MANAGED}=true"]
        if attempt_id is not None:
            label_filters.append(f"{LABEL_ATTEMPT_ID}={attempt_id}")
        if stage_id is not None:
            label_filters.append(f"{LABEL_STAGE_ID}={stage_id}")

        client = self._get_client()
        try:
            containers = client.containers.list(all=True, filters={"label": label_filters})
        except DockerException as exc:
            raise DockerUnavailableError(f"docker list failed: {exc}") from exc

        items = [
            self._build_inspect_response(
                container,
                reused=False,
                iterations_tail=iterations_tail,
                include_stats=include_stats,
            )
            for container in containers
        ]
        items.sort(key=lambda item: (item.started_at or "", item.stage_execution_id))
        return StageListResponse(items=items, total=len(items))

    def stop_stage(
        self,
        stage_execution_id: str,
        *,
        reason: StopReason = "cancelled",
        grace_seconds: int,
    ) -> StageInspectResponse:
        """Terminate a running stage container immediately WITHOUT deleting container or /work/out."""
        container = self._find_container_by_execution_id(stage_execution_id)
        if container is None:
            raise StageNotFoundError(f"stage container not found: {stage_execution_id}")

        try:
            container.reload()
        except DockerException as exc:
            raise DockerUnavailableError(f"docker inspect failed: {exc}") from exc

        attrs = container.attrs or {}
        labels = (attrs.get("Config") or {}).get("Labels") or {}
        state = attrs.get("State") or {}
        workspace_rel = str(labels.get(LABEL_WORKSPACE_REL) or stage_execution_id)
        workspace_dir = self._workspace_dir_for(workspace_rel)

        if state.get("Status") in ("created", "running", "paused", "restarting"):
            pre_resources = collect_stage_resources(
                container,
                attrs,
                cgroup_root=self.settings.cgroup_root,
                workspace_dir=workspace_dir,
                running=bool(state.get("Running", False)),
                include_stats=True,
            )
            save_stats_snapshot(workspace_dir, pre_resources)
            self._write_stop_reason(workspace_dir, reason)
            try:
                if grace_seconds == 0:
                    container.kill()
                else:
                    container.stop(timeout=grace_seconds)
            except DockerException as exc:
                raise DockerUnavailableError(f"docker stop failed: {exc}") from exc

        return self._build_inspect_response(container, reused=False)

    def cleanup_stage(
        self,
        stage_execution_id: str,
        *,
        force: bool = False,
    ) -> CleanupStageResponse:
        """Remove container and temporary workspace only after backend persists outputs."""
        container = self._find_container_by_execution_id(stage_execution_id)
        workspace_rel: str | None = stage_execution_id
        container_removed = False

        if container is not None:
            try:
                container.reload()
            except DockerException as exc:
                raise DockerUnavailableError(f"docker inspect failed: {exc}") from exc
            attrs = container.attrs or {}
            labels = (attrs.get("Config") or {}).get("Labels") or {}
            workspace_rel = str(labels.get(LABEL_WORKSPACE_REL) or stage_execution_id)
            state = attrs.get("State") or {}
            if state.get("Running") and not force:
                raise ConflictError(
                    "stage container is still running; stop_stage and persist outputs before cleanup_stage"
                )
            try:
                container.remove(force=True)
                container_removed = True
            except DockerException as exc:
                raise DockerUnavailableError(f"docker remove failed: {exc}") from exc

        workspace_dir = self._workspace_dir_for(workspace_rel)
        workspace_removed = False
        if workspace_dir is not None and workspace_dir.exists():
            shutil.rmtree(workspace_dir)
            workspace_removed = True

        return CleanupStageResponse(
            stage_execution_id=stage_execution_id,
            container_removed=container_removed,
            workspace_removed=workspace_removed,
        )
