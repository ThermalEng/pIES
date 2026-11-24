"""Pydantic v2 request and response contracts for job_runner's 5 fixed operations."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_IMAGE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

TerminationReason = Literal[
    "normal",
    "cancelled",
    "timed_out",
    "oom",
    "fenced",
    "failed",
]
StopReason = Literal["cancelled", "timed_out", "fenced"]


def _check_safe_id(value: str, field_name: str) -> str:
    if not _SAFE_ID_RE.match(value) or ".." in value:
        raise ValueError(f"{field_name} must match {_SAFE_ID_RE.pattern} without '..'")
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BuildPluginRequest(_StrictModel):
    plugin_id: str
    version: str
    source_rel: str = Field(min_length=1, max_length=256)
    requirements_rel: str | None = Field(default=None, min_length=1, max_length=256)
    install_requirements: bool = True

    @field_validator("plugin_id")
    @classmethod
    def _validate_plugin_id(cls, value: str) -> str:
        if not _PLUGIN_ID_RE.match(value) or ".." in value:
            raise ValueError("plugin_id must be a lowercase namespace identifier without '..'")
        return value

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        if not _VERSION_RE.match(value) or ".." in value:
            raise ValueError("version must be a valid image tag segment without '..'")
        return value


class BuildPluginResponse(_StrictModel):
    image: str
    plugin_id: str
    version: str
    built_at: str
    logs: list[str] = Field(default_factory=list)


class StartStageRequest(_StrictModel):
    attempt_id: str
    stage_id: str
    fencing_token: str
    image: str
    stage_execution_id: str | None = None
    workspace_rel: str | None = None
    command: list[str] | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    cpu_cores: float = Field(gt=0)
    memory_mb: int = Field(gt=0)
    pids_limit: int = Field(gt=0)
    tmpfs_size_mb: int = Field(gt=0)

    @field_validator("attempt_id", "stage_id", "fencing_token")
    @classmethod
    def _validate_ids(cls, value: str, info: Any) -> str:
        return _check_safe_id(value, info.field_name)

    @field_validator("stage_execution_id")
    @classmethod
    def _validate_stage_execution_id(cls, value: str | None) -> str | None:
        if value is not None:
            return _check_safe_id(value, "stage_execution_id")
        return None

    @field_validator("image")
    @classmethod
    def _validate_image(cls, value: str) -> str:
        if not _IMAGE_REF_RE.match(value) or ".." in value:
            raise ValueError("image must be a valid Docker image reference")
        return value

    @field_validator("environment")
    @classmethod
    def _validate_environment(cls, value: dict[str, str]) -> dict[str, str]:
        for key in value:
            if not _ENV_KEY_RE.match(key):
                raise ValueError(f"invalid environment variable name: {key!r}")
        return value


class MetricValue(_StrictModel):
    status: Literal["known", "unknown"]
    value: int | float | bool | None = None


class LimitValue(_StrictModel):
    status: Literal["limited", "unlimited", "unknown"]
    value: int | float | None = None


class StageResources(_StrictModel):
    source: str = "docker_and_cgroup_v2"
    memory_limit_bytes: LimitValue
    memory_current_bytes: MetricValue
    memory_peak_bytes: MetricValue
    cpu_limit_cores: LimitValue
    cpu_usage_ns: MetricValue
    pids_limit: LimitValue
    oom_killed: MetricValue
    oom_kill_count: MetricValue


class IterationsSummary(_StrictModel):
    present: bool
    count: int
    tail: list[Any]


class StageInspectResponse(_StrictModel):
    stage_execution_id: str
    attempt_id: str
    stage_id: str
    fencing_token: str
    container_id: str
    container_name: str
    image: str
    workspace_rel: str
    reused: bool = False
    status: str
    running: bool
    exit_code: int | None = None
    termination_reason: TerminationReason | None = None
    started_at: str | None = None
    finished_at: str | None = None
    resources: StageResources
    iterations: IterationsSummary
    output_files: list[str] = Field(default_factory=list)


class StageListResponse(_StrictModel):
    items: list[StageInspectResponse]
    total: int


class StopStageRequest(_StrictModel):
    reason: StopReason = "cancelled"
    grace_seconds: int = Field(ge=0, le=300)


class CleanupStageRequest(_StrictModel):
    force: bool = False


class CleanupStageResponse(_StrictModel):
    stage_execution_id: str
    status: Literal["cleaned"] = "cleaned"
    container_removed: bool
    workspace_removed: bool
