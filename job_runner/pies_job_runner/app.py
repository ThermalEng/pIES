"""FastAPI application exposing ONLY /healthz, /readyz, and the 5 job_runner operations."""

import logging
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from pies_job_runner.config import JobRunnerSettings
from pies_job_runner.docker_ops import DockerJobRunnerService, JobRunnerError
from pies_job_runner.models import (
    BuildPluginRequest,
    BuildPluginResponse,
    CleanupStageRequest,
    CleanupStageResponse,
    DeletePluginImageRequest,
    DeletePluginImageResponse,
    StageInspectResponse,
    StageListResponse,
    StartStageRequest,
    StopStageRequest,
)

logger = logging.getLogger(__name__)


def _get_service(request: Request) -> DockerJobRunnerService:
    return request.app.state.job_runner_service


ServiceDep = Annotated[DockerJobRunnerService, Depends(_get_service)]


def create_app(
    settings: JobRunnerSettings | None = None,
    service: DockerJobRunnerService | None = None,
) -> FastAPI:
    """Create the thin job_runner HTTP proxy app with strictly fixed endpoints."""
    resolved_settings = settings or JobRunnerSettings()
    resolved_service = service or DockerJobRunnerService(resolved_settings)

    app = FastAPI(
        title="pIES job_runner",
        version="1.1.6",
        description="Controlled Docker proxy for plugin image builds and isolated stage containers.",
    )
    app.state.job_runner_settings = resolved_settings
    app.state.job_runner_service = resolved_service

    @app.exception_handler(JobRunnerError)
    async def _handle_job_runner_error(_request: Request, exc: JobRunnerError) -> JSONResponse:
        logger.warning("job_runner %s: %s", exc.status_code, exc)
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(svc: ServiceDep) -> dict[str, Any]:
        return svc.check_ready()

    @app.post("/v1/plugins/build", response_model=BuildPluginResponse)
    def build_plugin(
        payload: BuildPluginRequest,
        svc: ServiceDep,
    ) -> BuildPluginResponse:
        return svc.build_plugin(payload)

    @app.post("/v1/plugins/images/delete", response_model=DeletePluginImageResponse)
    def delete_plugin_image(
        payload: DeletePluginImageRequest,
        svc: ServiceDep,
    ) -> DeletePluginImageResponse:
        return svc.delete_plugin_image(payload)

    @app.post("/v1/stages/start", response_model=StageInspectResponse)
    def start_stage(
        payload: StartStageRequest,
        svc: ServiceDep,
    ) -> StageInspectResponse:
        return svc.start_stage(payload)

    @app.get("/v1/stages", response_model=StageListResponse)
    def list_stages(
        svc: ServiceDep,
        attempt_id: str | None = Query(default=None),
        stage_id: str | None = Query(default=None),
        iterations_tail: int = Query(default=20, ge=0, le=500),
        include_stats: bool = Query(default=False),
    ) -> StageListResponse:
        return svc.list_stages(
            attempt_id=attempt_id,
            stage_id=stage_id,
            iterations_tail=iterations_tail,
            include_stats=include_stats,
        )

    @app.get("/v1/stages/{stage_execution_id}", response_model=StageInspectResponse)
    def inspect_stage(
        stage_execution_id: str,
        svc: ServiceDep,
        iterations_tail: int = Query(default=20, ge=0, le=500),
        include_stats: bool = Query(default=True),
    ) -> StageInspectResponse:
        return svc.inspect_stage(
            stage_execution_id,
            iterations_tail=iterations_tail,
            include_stats=include_stats,
        )

    @app.post("/v1/stages/{stage_execution_id}/stop", response_model=StageInspectResponse)
    def stop_stage(
        stage_execution_id: str,
        payload: StopStageRequest,
        svc: ServiceDep,
    ) -> StageInspectResponse:
        return svc.stop_stage(
            stage_execution_id,
            reason=payload.reason,
            grace_seconds=payload.grace_seconds,
        )

    @app.post("/v1/stages/{stage_execution_id}/cleanup", response_model=CleanupStageResponse)
    def cleanup_stage(
        stage_execution_id: str,
        svc: ServiceDep,
        payload: CleanupStageRequest | None = None,
    ) -> CleanupStageResponse:
        req = payload or CleanupStageRequest()
        return svc.cleanup_stage(stage_execution_id, force=req.force)

    return app


app = create_app()
