"""Configuration settings for job_runner loaded from PIES_JOB_RUNNER_* environment variables.

Runtime resource limits and stop grace policy are NOT defaulted here; ``backend``
resolves them from the authoritative TOML deployment configuration and passes them
explicitly on each stage request.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class JobRunnerSettings(BaseSettings):
    """Orchestration-only settings for the job_runner proxy service.

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
        - ``container_user`` (``PIES_JOB_RUNNER_CONTAINER_USER``): Non-root UID:GID
          used for ephemeral containers and plugin image runtime (default ``10001:10001``,
          matching ``pies`` in ``pies-plugin-base``).
        - ``required_images`` (``PIES_JOB_RUNNER_REQUIRED_IMAGES``): Optional
          comma-separated Docker image references that must exist locally for
          ``/readyz`` to report ready (default ``""``).
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
    container_user: str = Field(default="10001:10001")
    required_images: str = Field(default="")

    def parsed_required_images(self) -> tuple[str, ...]:
        """Parse comma-separated ``required_images`` into deduplicated non-empty image refs."""
        return tuple(
            dict.fromkeys(part.strip() for part in self.required_images.split(",") if part.strip())
        )
