"""Configuration settings for job_runner loaded from PIES_JOB_RUNNER_* environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class JobRunnerSettings(BaseSettings):
    """Runtime settings for the job_runner proxy service.

    Contract notes for ``backend`` orchestrator integration:
        - ``stage_root`` (``PIES_JOB_RUNNER_STAGE_ROOT``): Container-local path to the
          shared stages directory (default ``/data/stages``). Must be shared between
          ``backend`` and ``job_runner``.
        - ``host_stage_root`` (``PIES_JOB_RUNNER_HOST_STAGE_ROOT``): Optional Docker-host
          path corresponding to ``stage_root`` for sibling container bind mounts. When
          omitted, ``job_runner`` inspects its own container mounts via Docker API.
        - ``plugin_source_root`` (``PIES_JOB_RUNNER_PLUGIN_SOURCE_ROOT``): Container-local
          path where ``backend`` stages unpacked plugin build directories (default
          ``/data/plugin_builds``).
        - ``plugin_base_image`` (``PIES_JOB_RUNNER_PLUGIN_BASE_IMAGE``): Official base
          image for ``build_plugin`` (default ``pies-plugin-base:latest``).
        - ``plugin_image_prefix`` (``PIES_JOB_RUNNER_PLUGIN_IMAGE_PREFIX``): Tag prefix
          for built plugin images (default ``pies-plugin``).
    """

    model_config = SettingsConfigDict(
        env_prefix="PIES_JOB_RUNNER_",
        extra="ignore",
    )

    stage_root: Path = Field(default=Path("/data/stages"))
    host_stage_root: Path | None = Field(default=None)
    plugin_source_root: Path = Field(default=Path("/data/plugin_builds"))
    plugin_base_image: str = Field(default="pies-plugin-base:latest")
    plugin_image_prefix: str = Field(default="pies-plugin")
    docker_host: str | None = Field(default=None)
    cgroup_root: Path = Field(default=Path("/sys/fs/cgroup"))
    container_user: str = Field(default="65534:65534")
    default_pids_limit: int = Field(default=64, ge=1)
    default_tmpfs_size_mb: int = Field(default=256, ge=1)
    stop_grace_seconds: int = Field(default=5, ge=0, le=300)
