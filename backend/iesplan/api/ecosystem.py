"""本地模型与算法生态 API 路由（/api/ecosystem）。

本层仅负责 HTTP 参数校验、会话认证与调用 application.ecosystem 公开门面；
绝不导入 ORM/db、绝不直接导入 job_runner_client/stage_runner/worker。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from iesplan.api.auth import CurrentAdmin, CurrentUser
from iesplan.api.deps import get_request_db
from iesplan.application import ecosystem as ecosystem_app
from iesplan.core.errors import http_error

router = APIRouter(prefix="/api/ecosystem", tags=["ecosystem"])
_MAX_ECOSYSTEM_UPLOAD_BYTES = 50 * 1024 * 1024
_UPLOAD_CHUNK_BYTES = 1024 * 1024


async def _read_bounded_upload(
    file: UploadFile, *, max_bytes: int | None = None
) -> bytes:
    """分块读取上传文件并硬限制体积上限（超出立即返回 413，防无界缓冲）。"""
    limit = _MAX_ECOSYSTEM_UPLOAD_BYTES if max_bytes is None else max_bytes
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise http_error(
                413,
                "ECO-UPLOAD-TOO-LARGE",
                "ies.diag.ecosystem.upload_too_large",
                max_bytes=limit,
            )
        chunks.append(chunk)
    return b"".join(chunks)


class EcosystemSettingsUpdateRequest(BaseModel):
    market_mode: str = Field(description="市场模式：online | local")
    local_upload_requires_approval: bool = Field(description="非管理员本地上传/离线导入是否需审批")
    pip_index_url: str | None = Field(default=None, description="可选单源 HTTP(S) pip index URL")
    market_index_url: str | None = Field(default=None, description="可选公共市场 index.json URL")
    submission_gateway_url: str | None = Field(default=None, description="可选无状态投稿网关 URL")


class DeviceModelImportRequest(BaseModel):
    yaml_text: str = Field(description="ies.device-model@1.0.0 YAML 文本")
    expected_sha256: str | None = Field(default=None, description="可选外部期望 64 位 SHA-256")
    revision: int = Field(default=1, ge=1, description="发布正整数修订号")
    source_origin: Literal["local_upload", "offline_import"] = Field(
        default="local_upload",
        description="来源：local_upload | offline_import（公共市场下载不经本上传端点）",
    )


class RejectArtifactRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


class UninstallImageRequest(BaseModel):
    force: bool = Field(default=False)


class MarketInstallRequest(BaseModel):
    artifact_id: str = Field(min_length=1, description="市场制品稳定 ID")
    kind: str = Field(
        description="制品类型：device_model | predictor | generator | solver | evaluator | algorithm"
    )
    version_or_revision: str | int | None = Field(default=None, description="可选版本或修订号")


class MarketOtpRequest(BaseModel):
    email: str = Field(min_length=3, description="投稿人验证邮箱（无状态透传，不持久化）")


class MarketOtpVerifyRequest(BaseModel):
    email: str = Field(min_length=3, description="投稿人验证邮箱（无状态透传，不持久化）")
    otp_code: str = Field(min_length=1, description="6 位邮箱验证码")
    challenge_token: str = Field(min_length=1, description="OTP 请求返回的挑战令牌")


def _runtime_handles(request: Request) -> tuple[Any, Any, Any]:
    bootstrap = getattr(request.app.state, "bootstrap_context", None)
    scheduler = getattr(request.app.state, "scheduler", None)
    job_runner_client = getattr(bootstrap, "job_runner_client", None) if bootstrap is not None else None
    session_factory = getattr(bootstrap, "session_factory", None) if bootstrap is not None else None
    return scheduler, job_runner_client, session_factory


def _stage_registries(request: Request) -> dict[str, Any]:
    bootstrap = getattr(request.app.state, "bootstrap_context", None)
    if bootstrap is None:
        return {
            "predictor_registry": None,
            "generator_registry": None,
            "solver_registry": None,
            "evaluator_registry": None,
            "algorithm_registry": None,
        }
    return {
        "predictor_registry": getattr(bootstrap, "predictor_registry", None),
        "generator_registry": getattr(bootstrap, "generator_registry", None),
        "solver_registry": getattr(bootstrap, "solver_registry", None),
        "evaluator_registry": getattr(bootstrap, "evaluator_registry", None),
        "algorithm_registry": getattr(bootstrap, "algorithm_registry", None),
    }


@router.get("/settings", summary="读取实例级生态模式与审批开关")
def get_ecosystem_settings_endpoint(
    db: Annotated[Session, Depends(get_request_db)],
    user: CurrentUser,
) -> dict[str, Any]:
    return ecosystem_app.get_ecosystem_settings_case(db, user)


@router.put("/settings", summary="管理员更新实例级生态设置")
def update_ecosystem_settings_endpoint(
    body: EcosystemSettingsUpdateRequest,
    db: Annotated[Session, Depends(get_request_db)],
    admin: CurrentAdmin,
) -> dict[str, Any]:
    return ecosystem_app.update_ecosystem_settings_case(
        db,
        admin,
        market_mode=body.market_mode,
        local_upload_requires_approval=body.local_upload_requires_approval,
        pip_index_url=body.pip_index_url,
        market_index_url=body.market_index_url,
        submission_gateway_url=body.submission_gateway_url,
    )


@router.get("/artifacts", summary="列出本地生态目录条目（含成员安装态）")
def list_artifacts_endpoint(
    db: Annotated[Session, Depends(get_request_db)],
    user: CurrentUser,
    kind: str | None = None,
    approval_status: str | None = None,
) -> dict[str, Any]:
    return ecosystem_app.list_catalog_artifacts_case(
        db, user, kind=kind, approval_status=approval_status
    )


@router.post("/device-models/import", status_code=201, summary="导入或上传本地设备模型 YAML")
def import_device_model_endpoint(
    body: DeviceModelImportRequest,
    db: Annotated[Session, Depends(get_request_db)],
    user: CurrentUser,
) -> dict[str, Any]:
    return ecosystem_app.import_device_model_case(
        db,
        user,
        yaml_text=body.yaml_text,
        expected_sha256=body.expected_sha256,
        revision=body.revision,
        source_origin=body.source_origin,
    )


@router.get("/device-models/{artifact_id}/export", summary="导出纯技术设备模型 YAML")
def export_device_model_endpoint(
    artifact_id: int,
    db: Annotated[Session, Depends(get_request_db)],
    user: CurrentUser,
) -> dict[str, Any]:
    return ecosystem_app.export_device_model_case(db, user, artifact_id=artifact_id)


@router.post("/plugins/upload", status_code=201, summary="上传算法插件 ZIP")
async def upload_plugin_endpoint(
    request: Request,
    db: Annotated[Session, Depends(get_request_db)],
    user: CurrentUser,
    file: Annotated[UploadFile, File(...)],
    expected_sha256: Annotated[str | None, Form()] = None,
    source_origin: Annotated[Literal["local_upload", "offline_import"], Form()] = "local_upload",
) -> dict[str, Any]:
    zip_bytes = await _read_bounded_upload(file)
    scheduler, job_runner_client, session_factory = _runtime_handles(request)
    return await run_in_threadpool(
        ecosystem_app.upload_plugin_zip_case,
        db,
        user,
        zip_bytes=zip_bytes,
        expected_sha256=expected_sha256,
        source_origin=source_origin,
        scheduler=scheduler,
        job_runner_client=job_runner_client,
        session_factory=session_factory,
        wait_for_build=scheduler is None,
    )


@router.post("/artifacts/{artifact_id}/approve", summary="管理员审批通过生态制品")
def approve_artifact_endpoint(
    artifact_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_request_db)],
    admin: CurrentAdmin,
) -> dict[str, Any]:
    scheduler, job_runner_client, session_factory = _runtime_handles(request)
    return ecosystem_app.approve_artifact_case(
        db,
        admin,
        artifact_id,
        scheduler=scheduler,
        job_runner_client=job_runner_client,
        session_factory=session_factory,
        wait_for_build=scheduler is None,
    )


@router.post("/artifacts/{artifact_id}/reject", summary="管理员拒绝生态制品")
def reject_artifact_endpoint(
    artifact_id: int,
    body: RejectArtifactRequest,
    db: Annotated[Session, Depends(get_request_db)],
    admin: CurrentAdmin,
) -> dict[str, Any]:
    return ecosystem_app.reject_artifact_case(db, admin, artifact_id, reason=body.reason)


@router.post("/artifacts/{artifact_id}/install", summary="当前成员安装并启用精确版本制品")
def install_artifact_endpoint(
    artifact_id: int,
    db: Annotated[Session, Depends(get_request_db)],
    user: CurrentUser,
) -> dict[str, Any]:
    return ecosystem_app.install_artifact_for_user_case(db, user, artifact_id, enabled=True)


@router.post("/artifacts/{artifact_id}/enable", summary="当前成员启用已安装制品")
def enable_artifact_endpoint(
    artifact_id: int,
    db: Annotated[Session, Depends(get_request_db)],
    user: CurrentUser,
) -> dict[str, Any]:
    return ecosystem_app.set_member_artifact_enabled_case(db, user, artifact_id, enabled=True)


@router.post("/artifacts/{artifact_id}/disable", summary="当前成员停用已安装制品")
def disable_artifact_endpoint(
    artifact_id: int,
    db: Annotated[Session, Depends(get_request_db)],
    user: CurrentUser,
) -> dict[str, Any]:
    return ecosystem_app.set_member_artifact_enabled_case(db, user, artifact_id, enabled=False)


@router.post("/artifacts/{artifact_id}/uninstall", summary="当前成员卸载个人安装记录")
def uninstall_member_artifact_endpoint(
    artifact_id: int,
    db: Annotated[Session, Depends(get_request_db)],
    user: CurrentUser,
) -> dict[str, Any]:
    return ecosystem_app.uninstall_member_artifact_case(db, user, artifact_id)


@router.post("/admin/artifacts/{artifact_id}/uninstall-image", summary="管理员并发安全下架并清理插件镜像")
def uninstall_artifact_image_endpoint(
    artifact_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_request_db)],
    admin: CurrentAdmin,
    body: UninstallImageRequest | None = None,
) -> dict[str, Any]:
    _, job_runner_client, _ = _runtime_handles(request)
    return ecosystem_app.uninstall_artifact_image_case(
        db,
        admin,
        artifact_id,
        job_runner_client=job_runner_client,
        force=bool(body.force) if body is not None else False,
    )


@router.get("/stage-catalog", summary="读取当前成员可用的动态阶段算法目录")
def get_stage_catalog_endpoint(
    request: Request,
    db: Annotated[Session, Depends(get_request_db)],
    user: CurrentUser,
) -> dict[str, Any]:
    return ecosystem_app.get_stage_catalog_case(db, user, **_stage_registries(request))


@router.get("/market/index", summary="在线模式拉取远端公共市场索引与成员安装三态")
def get_market_index_endpoint(
    db: Annotated[Session, Depends(get_request_db)],
    user: CurrentUser,
) -> dict[str, Any]:
    return ecosystem_app.get_market_index_case(db, user)


@router.post("/market/install", summary="从在线公共市场下载并安装制品")
def install_from_market_endpoint(
    body: MarketInstallRequest,
    request: Request,
    db: Annotated[Session, Depends(get_request_db)],
    user: CurrentUser,
) -> dict[str, Any]:
    scheduler, job_runner_client, session_factory = _runtime_handles(request)
    return ecosystem_app.install_from_market_case(
        db,
        user,
        artifact_id=body.artifact_id,
        kind=body.kind,
        version_or_revision=body.version_or_revision,
        scheduler=scheduler,
        job_runner_client=job_runner_client,
        session_factory=session_factory,
        wait_for_build=scheduler is None,
    )


@router.post("/market/submit/otp-request", summary="无状态代理向投稿网关申请邮箱 OTP")
def market_otp_request_endpoint(
    body: MarketOtpRequest,
    db: Annotated[Session, Depends(get_request_db)],
    user: CurrentUser,
) -> dict[str, Any]:
    return ecosystem_app.request_market_otp_case(db, user, email=body.email)


@router.post("/market/submit/otp-verify", summary="无状态代理向投稿网关核验邮箱 OTP")
def market_otp_verify_endpoint(
    body: MarketOtpVerifyRequest,
    db: Annotated[Session, Depends(get_request_db)],
    user: CurrentUser,
) -> dict[str, Any]:
    return ecosystem_app.verify_market_otp_case(
        db,
        user,
        email=body.email,
        otp_code=body.otp_code,
        challenge_token=body.challenge_token,
    )


@router.post("/market/submit", summary="无状态代理持令牌提交模型或算法插件至公共市场")
async def submit_to_market_endpoint(
    db: Annotated[Session, Depends(get_request_db)],
    user: CurrentUser,
    submission_token: Annotated[str, Form()],
    kind: Annotated[str, Form()],
    file: Annotated[UploadFile, File(...)],
    artifact_id: Annotated[str | None, Form()] = None,
    version_or_revision: Annotated[str | None, Form()] = None,
    publisher: Annotated[str | None, Form()] = None,
    license: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
    readme_markdown: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    file_bytes = await _read_bounded_upload(file)
    return await run_in_threadpool(
        ecosystem_app.submit_to_market_case,
        db,
        user,
        submission_token=submission_token,
        kind=kind,
        file_bytes=file_bytes,
        filename=file.filename or "",
        artifact_id=artifact_id,
        version_or_revision=version_or_revision,
        publisher=publisher,
        license_name=license,
        description=description,
        readme_markdown=readme_markdown,
    )
