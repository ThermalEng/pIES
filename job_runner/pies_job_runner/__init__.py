"""pIES job_runner: controlled Docker proxy for plugin builds and ephemeral stage containers.

Shared Stage Volume Contract (for ``backend`` and ``job_runner``):
    Both ``backend`` and ``job_runner`` mount the same host data directory (for example
    ``./data:/data`` in Docker Compose) and point ``PIES_JOB_RUNNER_STAGE_ROOT`` to the
    shared stage root directory inside their containers (default: ``/data/stages``).

    For each stage execution:
        - ``<STAGE_ROOT>/<workspace_rel>/in`` is populated by ``backend`` with frozen
          read-only inputs before calling ``POST /v1/stages/start``, and is bind-mounted
          into the ephemeral computation container at ``/work/in`` (read-only).
        - ``<STAGE_ROOT>/<workspace_rel>/out`` is bind-mounted into the ephemeral
          container at ``/work/out`` (read-write) with container-writable permissions.
        - ``<STAGE_ROOT>/<workspace_rel>`` also holds job_runner's internal sidecar
          metadata (such as ``stop_reason.json`` and ``stats_snapshot.json``), never
          exposed inside ``/work/in`` or ``/work/out``.
        - ``PIES_JOB_RUNNER_HOST_STAGE_ROOT`` specifies the Docker host path mapping to
          ``PIES_JOB_RUNNER_STAGE_ROOT`` so sibling ephemeral containers bind-mount the
          right directory; when unset, ``job_runner`` resolves it from its own Docker
          container mount table.

    For plugin image builds:
        - ``backend`` places unpacked plugin files (``plugin.yaml``, code, optional
          ``requirements.txt``) under ``PIES_JOB_RUNNER_PLUGIN_SOURCE_ROOT/<source_rel>``
          (default: ``/data/plugin_builds``) and calls ``POST /v1/plugins/build``.
"""

from pies_job_runner.app import create_app

__all__ = ["create_app"]
