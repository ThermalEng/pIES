"""项目模型 API(切片 dm2-A: 候选校验 / 临时数据文件 / 正式保存 / 清单 / 删除)。

路由清单(prefix /api/projects/{project_id}/models):
- POST   /api/projects/{project_id}/models/validate   候选模型门禁(不保存);
- POST   /api/projects/{project_id}/models/temp-files  配套数据文件临时上传;
- GET    /api/projects/{project_id}/models             项目模型清单(编号可见);
- POST   /api/projects/{project_id}/models             正式保存(校验→编号→规范化→原子保存);
- DELETE /api/projects/{project_id}/models/{model_id}  删除(编号不复用)。

语义(format 标准「进入项目前的候选模型门禁」):
- 候选校验失败: validate 返回 200 + {valid: false, diagnostics}(完整聚合诊断);
  正式保存返回 400 + 标准 8 字段信封(PROJ-MDL-005), 诊断明细入
  params.diagnostics(每条含 code/message_key/location.field/expected/actual),
  不写项目模型目录、不登记清单、不分配编号;
- 请求体结构/语义不可处理(Pydantic 校验失败)返回 422 标准信封(main.py);
- 成功保存 201 + {project_model, receipt}(必要时含 duplicate 幂等重放标志)。

上传说明: temp-files 为 multipart(file + data_ref 表单字段); upload_id 为
服务端生成的 63 位不透明十进制字符串(宪法 §7.2: 对外 ID 不参与算术)。

权限: 读/校验要求项目 view, 上传/保存/删除要求项目 edit(403 标准信封)。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from iesplan.api.auth import CurrentUser
from iesplan.api.limits import (
    QUOTA_CODE,
    QUOTA_MESSAGE_KEY,
    QuotaError,
    check_upload_quota,
)
from iesplan.application.projects import (
    DataFileRef,
    delete_project_model,
    get_project_models,
    new_temp_upload_id,
    save_project_model,
    upload_temp_data_file,
    validate_candidate,
)
from iesplan.core.errors import http_error
from iesplan.db import get_db

router = APIRouter(prefix="/api/projects/{project_id}/models", tags=["project-models"])

#: 临时数据文件大小上限(512 MB, 与 dataset 上传对齐)
_MAX_TEMP_FILE_BYTES: int = 512 * 1024 * 1024

DbSession = Annotated[Session, Depends(get_db)]

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_UPLOAD_ID_PATTERN = r"^[0-9]{1,19}$"


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class DataFileRefDTO(BaseModel):
    """配套数据文件引用(已上传的临时对象 + 声明摘要)。"""

    data_ref: str = Field(
        min_length=1, max_length=200,
        description="数据引用名(对应 interface source.data_ref)",
    )
    upload_id: str = Field(pattern=_UPLOAD_ID_PATTERN, description="临时上传会话标识(不透明十进制字符串)")
    object_id: str = Field(pattern=_UPLOAD_ID_PATTERN, description="临时对象 id(不透明十进制字符串)")
    sha256: str = Field(pattern=_SHA256_PATTERN, description="声明的内容摘要")


class ModelCandidateRequest(BaseModel):
    """候选模型请求体(校验与保存共用)。

    来源判别:
    - ``source=direct_yaml``: ``model_yaml`` 为完整候选模型 YAML(必填);
    - ``source=template``: ``template_id`` + ``template_revision`` +
      ``template_sha256`` 固定精确模板 revision(后端读取权威内容实例化,
      不信任客户端自带的模板字节); ``template_inputs`` 为用户 inputs。
      ``model_yaml`` 可省略(校验端点允许内联模板 YAML 形态)。
    """

    model_yaml: str = Field(default="", max_length=2_000_000, description="候选模型 YAML 文本(直接来源必填)或模板来源的权威模板字节(可省略)")
    source: Literal["direct_yaml", "template"] = "direct_yaml"
    template_id: str | None = Field(
        default=None, description="模板来源: 稳定模板 ID(不透明字符串)",
    )
    template_revision: int | None = Field(
        default=None, ge=1, description="模板来源: 精确发布 revision(固定不可变)",
    )
    template_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN,
        description="模板来源: 精确 revision 的内容摘要(与后端权威内容二次确认)",
    )
    template_inputs: dict[str, Any] | None = Field(
        default=None, description="模板来源时用户提交的 inputs(未声明字段拒绝)"
    )
    data_files: list[DataFileRefDTO] = Field(default_factory=list)

    @model_validator(mode="after")
    def _source_requires_fields(self) -> ModelCandidateRequest:
        if self.source == "direct_yaml" and not self.model_yaml.strip():
            raise ValueError("source=direct_yaml 必须提供 model_yaml")
        if self.source == "template":
            if not self.template_id or not self.template_revision or not self.template_sha256:
                raise ValueError(
                    "source=template 必须携带 template_id、template_revision 与 template_sha256"
                )
            if self.template_inputs is None:
                raise ValueError("source=template 必须携带 template_inputs")
        return self


class ModelSaveRequest(ModelCandidateRequest):
    """正式保存请求体(可重试写操作携带幂等键; 重放返回同一逻辑结果)。"""

    idempotency_key: str | None = Field(
        default=None, min_length=1, max_length=128,
        pattern=r"^[A-Za-z0-9._:-]{1,128}$", description="项目内幂等键",
    )
    expected_revision: int = Field(ge=1, description="预期项目草稿 revision(乐观锁)")


class ModelDeleteRequest(BaseModel):
    """删除项目模型同样推进项目草稿 revision。"""

    expected_revision: int = Field(ge=1)


def _to_data_file_refs(dtos: list[DataFileRefDTO]) -> tuple[DataFileRef, ...]:
    """DTO → 领域值对象(upload_id 由不透明十进制字符串解析)。"""
    return tuple(
        DataFileRef(
            data_ref=d.data_ref,
            upload_id=int(d.upload_id),
            object_id=int(d.object_id),
            sha256=d.sha256,
        )
        for d in dtos
    )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.post("/validate", summary="候选模型门禁(不保存)")
def validate_project_model_candidate(
    project_id: int,
    payload: ModelCandidateRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """候选模型完整校验: 返回聚合诊断与 valid 标志, 不写对象/清单/编号。

    诊断含已登记诊断码、message_key、字段路径(location.field)与
    expected/actual(params); 合法候选返回空诊断列表。
    """
    validation = validate_candidate(
        db, user, project_id,
        model_yaml=payload.model_yaml,
        source=payload.source,
        template_id=payload.template_id,
        template_revision=payload.template_revision,
        template_sha256=payload.template_sha256,
        template_inputs=payload.template_inputs,
        data_files=_to_data_file_refs(payload.data_files),
    )
    return {
        "valid": validation.ok,
        "diagnostics": [d.to_dict() for d in validation.diagnostics],
    }

@router.post("/temp-files", status_code=201, summary="配套数据文件临时上传")
def upload_project_model_temp_file(
    project_id: int,
    db: DbSession,
    user: CurrentUser,
    file: Annotated[UploadFile, File(description="设备配套数据文件(CSV)")],
    data_ref: Annotated[str, Form(description="数据引用名")] = "",
) -> dict[str, Any]:
    """把配套数据文件写入临时隔离区(临时 owner 引用), 返回 upload_id 与摘要。

    保存成功时由 finalize 转为最终 owner; 超龄未保存的临时引用由
    reconciliation 解绑后进入对象回收生命周期。
    """
    data_ref = (data_ref or "").strip()
    if not data_ref:
        raise http_error(400, "API-REQ-001", "ies.error.invalid_request",
                         reason="data_ref_required")
    content = file.file.read(_MAX_TEMP_FILE_BYTES + 1)
    if not content:
        raise http_error(400, "API-REQ-001", "ies.error.empty_file",
                         filename=file.filename or "", data_ref=data_ref)
    if len(content) > _MAX_TEMP_FILE_BYTES:
        raise http_error(413, "API-QUOTA-001", "ies.error.upload_quota_exceeded",
                         reason="temp_file_too_large", data_ref=data_ref,
                         max_bytes=_MAX_TEMP_FILE_BYTES)
    try:
        check_upload_quota(
            db, user_id=user.id, project_id=project_id, incoming_bytes=len(content)
        )
    except QuotaError as exc:
        raise http_error(
            413, QUOTA_CODE, QUOTA_MESSAGE_KEY,
            used_bytes=exc.used_bytes, quota_bytes=exc.quota_bytes,
            scope=exc.scope, owner_id=exc.owner_id,
        ) from exc
    upload_id = new_temp_upload_id()
    result = upload_temp_data_file(
        db, user, project_id, content=content, data_ref=data_ref, upload_id=upload_id
    )
    result["temp_file"]["object_id"] = str(result["temp_file"]["object_id"])
    return {"temp_file": result["temp_file"], "upload_id": str(upload_id)}


@router.get("", summary="项目模型清单")
def list_project_models_endpoint(
    project_id: int,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """项目模型清单(最新在前; 编号对用户可见)。"""
    return {"project_models": get_project_models(db, user, project_id)}


@router.post("", status_code=201, summary="正式保存项目模型")
def save_project_model_endpoint(
    project_id: int,
    payload: ModelSaveRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """正式保存: 完整校验 → 分配 _N 编号 → 规范化/摘要/回执 → 原子保存。

    校验失败 400 + PROJ-MDL-005 信封(聚合诊断); 幂等键重放返回同一逻辑结果
    (duplicate: true), 不重复占号。事务由本端点统一提交(application 拥有
    commit/rollback 边界, 宪法 §5.4)。
    """
    result = save_project_model(
        db, user, project_id,
        model_yaml=payload.model_yaml,
        source=payload.source,
        template_id=payload.template_id,
        template_revision=payload.template_revision,
        template_sha256=payload.template_sha256,
        template_inputs=payload.template_inputs,
        data_files=_to_data_file_refs(payload.data_files),
        idempotency_key=payload.idempotency_key,
        expected_revision=payload.expected_revision,
    )
    return result


@router.delete("/{model_id}", summary="删除项目模型")
def delete_project_model_endpoint(
    project_id: int,
    model_id: int,
    payload: ModelDeleteRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """删除项目模型(硬删除清单行 + 解绑对象引用; 编号不复用)。"""
    revision = delete_project_model(
        db, user, project_id, model_id, expected_revision=payload.expected_revision
    )
    return {"ok": True, "deleted": str(model_id), "project_revision": revision}
