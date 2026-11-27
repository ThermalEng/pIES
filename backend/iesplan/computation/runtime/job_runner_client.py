"""HTTP client for the job_runner container orchestration proxy.

Talks strictly to job_runner's fixed endpoints:
- GET  /readyz
- POST /v1/plugins/build
- POST /v1/plugins/images/delete
- POST /v1/stages/start
- GET  /v1/stages
- GET  /v1/stages/{stage_execution_id}
- POST /v1/stages/{stage_execution_id}/stop
- POST /v1/stages/{stage_execution_id}/cleanup
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx


class JobRunnerClientError(RuntimeError):
    """Base error raised when interacting with job_runner."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class JobRunnerUnavailableError(JobRunnerClientError):
    """job_runner HTTP endpoint or underlying Docker daemon is unavailable."""


class JobRunnerBadRequestError(JobRunnerClientError):
    """job_runner rejected the request payload or path."""


class JobRunnerNotFoundError(JobRunnerClientError):
    """Target stage container was not found."""


class JobRunnerConflictError(JobRunnerClientError):
    """Stage lifecycle conflict (e.g. cleanup requested while container is still running)."""


@dataclass(frozen=True, slots=True)
class BuildPluginResult:
    """Response from POST /v1/plugins/build."""

    image: str
    plugin_id: str
    version: str
    built_at: str
    logs: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BuildPluginResult:
        return cls(
            image=str(data["image"]),
            plugin_id=str(data["plugin_id"]),
            version=str(data["version"]),
            built_at=str(data["built_at"]),
            logs=tuple(str(line) for line in (data.get("logs") or ())),
        )


@dataclass(frozen=True, slots=True)
class DeletePluginImageResult:
    """Response from POST /v1/plugins/images/delete."""

    image: str
    plugin_id: str
    version: str
    deleted: bool
    status: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DeletePluginImageResult:
        return cls(
            image=str(data["image"]),
            plugin_id=str(data["plugin_id"]),
            version=str(data["version"]),
            deleted=bool(data["deleted"]),
            status=str(data["status"]),
        )


@dataclass(frozen=True, slots=True)
class StartStageSpec:
    """Explicit stage launch request passed to POST /v1/stages/start."""

    attempt_id: str
    stage_id: str
    fencing_token: str
    image: str
    cpu_cores: float
    memory_mb: int
    pids_limit: int
    tmpfs_size_mb: int
    stage_execution_id: str | None = None
    workspace_rel: str | None = None
    command: tuple[str, ...] | None = None
    environment: Mapping[str, str] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "attempt_id": self.attempt_id,
            "stage_id": self.stage_id,
            "fencing_token": self.fencing_token,
            "image": self.image,
            "cpu_cores": float(self.cpu_cores),
            "memory_mb": int(self.memory_mb),
            "pids_limit": int(self.pids_limit),
            "tmpfs_size_mb": int(self.tmpfs_size_mb),
            "environment": {str(k): str(v) for k, v in self.environment.items()},
        }
        if self.stage_execution_id is not None:
            payload["stage_execution_id"] = self.stage_execution_id
        if self.workspace_rel is not None:
            payload["workspace_rel"] = self.workspace_rel
        if self.command is not None:
            payload["command"] = [str(part) for part in self.command]
        return payload


@dataclass(frozen=True, slots=True)
class StageInspection:
    """Normalized stage status, cgroup metrics, iterations, and output files."""

    stage_execution_id: str
    attempt_id: str
    stage_id: str
    fencing_token: str
    container_id: str
    container_name: str
    image: str
    workspace_rel: str
    reused: bool
    status: str
    running: bool
    exit_code: int | None
    termination_reason: str | None
    started_at: str | None
    finished_at: str | None
    resources: Mapping[str, Any]
    iterations: Mapping[str, Any]
    output_files: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StageInspection:
        return cls(
            stage_execution_id=str(data["stage_execution_id"]),
            attempt_id=str(data["attempt_id"]),
            stage_id=str(data["stage_id"]),
            fencing_token=str(data["fencing_token"]),
            container_id=str(data["container_id"]),
            container_name=str(data["container_name"]),
            image=str(data["image"]),
            workspace_rel=str(data["workspace_rel"]),
            reused=bool(data.get("reused", False)),
            status=str(data["status"]),
            running=bool(data["running"]),
            exit_code=data.get("exit_code"),
            termination_reason=data.get("termination_reason"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            resources=dict(data["resources"]),
            iterations=dict(data["iterations"]),
            output_files=tuple(str(item) for item in (data.get("output_files") or ())),
        )


@dataclass(frozen=True, slots=True)
class CleanupStageResult:
    """Response from POST /v1/stages/{stage_execution_id}/cleanup."""

    stage_execution_id: str
    status: str
    container_removed: bool
    workspace_removed: bool

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CleanupStageResult:
        return cls(
            stage_execution_id=str(data["stage_execution_id"]),
            status=str(data["status"]),
            container_removed=bool(data["container_removed"]),
            workspace_removed=bool(data["workspace_removed"]),
        )


class JobRunnerClient:
    """Synchronous HTTP client for job_runner's fixed 5-operation proxy API."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 30.0,
        build_timeout_seconds: float = 600.0,
        client: httpx.Client | None = None,
    ) -> None:
        normalized = str(base_url).rstrip("/")
        if not normalized:
            raise ValueError("job_runner base_url must not be empty")
        self.base_url = normalized
        self.timeout_seconds = float(timeout_seconds)
        self.build_timeout_seconds = float(build_timeout_seconds)
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=normalized,
            timeout=self.timeout_seconds,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> JobRunnerClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if json_body is not None:
            kwargs["json"] = dict(json_body)
        if params is not None:
            kwargs["params"] = {k: v for k, v in params.items() if v is not None}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise JobRunnerUnavailableError(
                f"job_runner request failed ({method} {path}): {exc}",
            ) from exc

        if 200 <= response.status_code < 300:
            payload = response.json()
            if not isinstance(payload, dict):
                raise JobRunnerClientError(
                    f"unexpected job_runner response type: {type(payload).__name__}",
                    status_code=response.status_code,
                )
            return payload

        detail = ""
        try:
            body = response.json()
            if isinstance(body, Mapping) and "detail" in body:
                detail = str(body["detail"])
            else:
                detail = str(body)
        except ValueError:
            detail = response.text

        msg = f"job_runner {method} {path} failed ({response.status_code}): {detail}"
        if response.status_code == 404:
            raise JobRunnerNotFoundError(msg, status_code=404, detail=detail)
        if response.status_code == 409:
            raise JobRunnerConflictError(msg, status_code=409, detail=detail)
        if response.status_code in (400, 422):
            raise JobRunnerBadRequestError(msg, status_code=response.status_code, detail=detail)
        if response.status_code >= 500:
            raise JobRunnerUnavailableError(msg, status_code=response.status_code, detail=detail)
        raise JobRunnerClientError(msg, status_code=response.status_code, detail=detail)

    def check_ready(self) -> dict[str, Any]:
        """Call GET /readyz and return readiness metadata."""
        return self._request("GET", "/readyz")

    def build_plugin(
        self,
        plugin_id: str,
        version: str,
        source_rel: str,
        *,
        requirements_rel: str | None = None,
        pip_index_url: str | None = None,
        timeout_seconds: int | None = None,
    ) -> BuildPluginResult:
        """Build a plugin image via POST /v1/plugins/build."""
        body: dict[str, Any] = {
            "plugin_id": plugin_id,
            "version": version,
            "source_rel": source_rel,
        }
        if requirements_rel is not None:
            body["requirements_rel"] = requirements_rel
        if pip_index_url is not None:
            body["pip_index_url"] = pip_index_url
        if timeout_seconds is not None:
            body["timeout_seconds"] = int(timeout_seconds)
        req_timeout = (
            float(timeout_seconds) if timeout_seconds is not None else self.build_timeout_seconds
        )
        data = self._request(
            "POST",
            "/v1/plugins/build",
            json_body=body,
            timeout=req_timeout,
        )
        return BuildPluginResult.from_dict(data)

    def delete_plugin_image(
        self,
        plugin_id: str,
        version: str,
        *,
        force: bool = False,
    ) -> DeletePluginImageResult:
        """Delete a managed plugin image via POST /v1/plugins/images/delete."""
        data = self._request(
            "POST",
            "/v1/plugins/images/delete",
            json_body={
                "plugin_id": plugin_id,
                "version": version,
                "force": bool(force),
            },
        )
        return DeletePluginImageResult.from_dict(data)

    def start_stage(self, spec: StartStageSpec) -> StageInspection:
        """Start an ephemeral isolated stage container via POST /v1/stages/start."""
        data = self._request("POST", "/v1/stages/start", json_body=spec.to_payload())
        return StageInspection.from_dict(data)

    def list_stages(
        self,
        *,
        attempt_id: str | None = None,
        stage_id: str | None = None,
        iterations_tail: int = 20,
        include_stats: bool = False,
    ) -> tuple[StageInspection, ...]:
        """List managed stage containers via GET /v1/stages for restart reconciliation."""
        data = self._request(
            "GET",
            "/v1/stages",
            params={
                "attempt_id": attempt_id,
                "stage_id": stage_id,
                "iterations_tail": iterations_tail,
                "include_stats": include_stats,
            },
        )
        items: Sequence[Mapping[str, Any]] = data.get("items") or ()
        return tuple(StageInspection.from_dict(item) for item in items)

    def inspect_stage(
        self,
        stage_execution_id: str,
        *,
        iterations_tail: int = 20,
        include_stats: bool = True,
    ) -> StageInspection:
        """Inspect a single stage container via GET /v1/stages/{stage_execution_id}."""
        data = self._request(
            "GET",
            f"/v1/stages/{stage_execution_id}",
            params={
                "iterations_tail": iterations_tail,
                "include_stats": include_stats,
            },
        )
        return StageInspection.from_dict(data)

    def stop_stage(
        self,
        stage_execution_id: str,
        *,
        grace_seconds: int,
        reason: str = "cancelled",
    ) -> StageInspection:
        """Stop a stage container via POST /v1/stages/{stage_execution_id}/stop."""
        data = self._request(
            "POST",
            f"/v1/stages/{stage_execution_id}/stop",
            json_body={"reason": reason, "grace_seconds": int(grace_seconds)},
        )
        return StageInspection.from_dict(data)

    def cleanup_stage(
        self,
        stage_execution_id: str,
        *,
        force: bool = False,
    ) -> CleanupStageResult:
        """Clean up a stopped stage container and workspace via POST /v1/stages/{id}/cleanup."""
        data = self._request(
            "POST",
            f"/v1/stages/{stage_execution_id}/cleanup",
            json_body={"force": bool(force)},
        )
        return CleanupStageResult.from_dict(data)


__all__ = [
    "BuildPluginResult",
    "CleanupStageResult",
    "DeletePluginImageResult",
    "JobRunnerBadRequestError",
    "JobRunnerClient",
    "JobRunnerClientError",
    "JobRunnerConflictError",
    "JobRunnerNotFoundError",
    "JobRunnerUnavailableError",
    "StageInspection",
    "StartStageSpec",
]
