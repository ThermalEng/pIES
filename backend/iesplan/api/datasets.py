"""数据集 API(U05): /api/datasets 与 /api/projects/{id}/datasets 路由。

- GET    /api/datasets/template?resolution=1h          标准 CSV 模板下载(公开);
- POST   /api/projects/{project_id}/datasets           创建数据集;
- POST   /api/projects/{project_id}/datasets/{dataset_id}/versions
                                                       multipart 上传版本(校验通过→版本+质量报告+诊断);
- GET    /api/projects/{project_id}/datasets           数据集列表+最新版本;
- GET    /api/projects/{project_id}/datasets/{dataset_id}  版本列表+质量报告;
- POST   /api/projects/{project_id}/datasets/{dataset_id}/sample  生成内置样例数据;
- GET    /api/projects/{project_id}/datasets/{dataset_id}/versions/{version_no}
                                                       版本元数据+溯源(不返回数据文件)。

错误统一 AppError + 标准错误信封(宪法 §8.3); 数据校验失败返回
400 + 信封, 诊断明细入 params.diagnostics(字段/行号定位, file-formats §设备数据 CSV)。

认证与权限: 除模板下载(公开)外, 全部端点要求窗口会话认证
(iesplan.api.auth.CurrentUser, 未认证 401); 读接口要求项目 view,
写接口(创建/上传/样例)要求项目 edit(查看者只读, 所有者可上传, 403)。

挂载方式(由 main.py 后续阶段追加):
    from iesplan.api.datasets import router
    application.include_router(router)
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from iesplan.api.auth import CurrentUser
from iesplan.api.limits import (
    META_CODE,
    META_MESSAGE_KEY,
    QUOTA_CODE,
    QUOTA_MESSAGE_KEY,
    QuotaError,
    validate_upload_fields,
    validate_upload_meta,
)
from iesplan.application import datasets as dataset_ops
from iesplan.application.datasets import DataValidationError
from iesplan.core.errors import error_envelope, http_error
from iesplan.core.timeaxis import RESOLUTIONS
from iesplan.db import get_db

#: 路由: 统一前缀 /api, 各端点自带路径
router = APIRouter(prefix="/api", tags=["datasets"])

#: 上传文件大小上限(512 MB, 防御性限制)
_MAX_UPLOAD_BYTES: int = 512 * 1024 * 1024

#: 会话依赖(Annotated 风格, 规避 B008)
DbSession = Annotated[Session, Depends(get_db)]

# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class DatasetCreate(BaseModel):
    """创建数据集请求(source_category/provenance 归属版本, 见服务文档)。"""

    name: str = Field(min_length=1, max_length=200, description="数据集名称")
    description: str | None = Field(default=None, max_length=2000, description="说明")
    source_category: str | None = Field(default=None, description="数据来源类别(默认 user_upload)")
    license: str | None = Field(default=None, description="默认许可证")
    provenance: dict | None = Field(default=None, description="默认溯源(版本未显式给出时继承)")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _iso_ts(value):
    """时间戳归一化为 ISO 字符串(ORM 行给 datetime, 域记录给 ISO 字符串)。"""
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else value


def _version_dict(v, *, with_report: bool = True) -> dict:
    """DatasetVersion → JSON 字典。"""
    out = {
        "id": v.id,
        "dataset_id": v.dataset_id,
        "version_no": v.version_no,
        "timeline": v.timeline,
        "resolution": v.resolution,
        "fixed_utc_offset_minutes": v.fixed_utc_offset_minutes,
        "fields": v.fields,
        "units": v.units,
        "provenance": v.provenance,
        "license": v.license,
        "created_by": v.created_by,
        "created_at": _iso_ts(v.created_at),
        "created_reason": v.created_reason,
    }
    if with_report:
        out["quality_report"] = v.quality_report
    return out


def _dataset_dict(ds) -> dict:
    """Dataset → JSON 字典。"""
    return {
        "id": ds.id,
        "project_id": ds.project_id,
        "name": ds.name,
        "description": ds.description,
        "status": ds.status,
        "default_license": ds.default_license,
        "created_by": ds.created_by,
        "created_at": _iso_ts(ds.created_at),
        "updated_at": _iso_ts(ds.updated_at),
    }


def _validation_response(exc: DataValidationError) -> JSONResponse:
    """校验失败响应: 400 + 标准错误信封, 诊断明细入 params.diagnostics(宪法 §8.3)。"""
    return JSONResponse(
        status_code=400,
        content=error_envelope(
            code=exc.code,
            message_key=exc.message_key,
            severity=exc.severity,
            blocking=exc.blocking,
            params={
                "diagnostics": [d.to_dict() for d in exc.diagnostics],
                "count": len(exc.diagnostics),
            },
        ),
    )


def _parse_json_field(raw: str | None, name: str) -> dict:
    """解析 multipart JSON 字段; 非法 JSON 返回 400 错误。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise http_error(400, "API-REQ-001", "ies.error.invalid_json", field=name) from exc
    if not isinstance(value, dict):
        raise http_error(400, "API-REQ-001", "ies.error.invalid_json", field=name)
    return value


# ---------------------------------------------------------------------------
# 模板(公开)
# ---------------------------------------------------------------------------


@router.get("/datasets/template", summary="标准 CSV 模板下载")
def download_template(resolution: str = Query(default="1h")) -> Response:
    """下载标准 CSV 模板(字段说明/单位/示例, 双语注释行, REQ-DATA-002)。"""
    if resolution not in RESOLUTIONS:
        raise http_error(400, "API-REQ-001", "ies.error.invalid_resolution", resolution=resolution)
    content = dataset_ops.get_template(resolution)
    headers = {"Content-Disposition": f'attachment; filename="iesplan_dataset_template_{resolution}.csv"'}
    return Response(content=content, media_type="text/csv; charset=utf-8", headers=headers)


# ---------------------------------------------------------------------------
# 数据集 CRUD
# ---------------------------------------------------------------------------


@router.post("/projects/{project_id}/datasets", status_code=201, summary="创建数据集")
def create_dataset(
    project_id: int,
    body: DatasetCreate,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """创建数据集元数据(名称在项目内唯一, 冲突返回 409; 需项目 edit 能力)。"""
    ds = dataset_ops.create_dataset_case(
        db,
        user,
        project_id=project_id,
        name=body.name,
        source_category=body.source_category,
        license=body.license,
        provenance=body.provenance,
        description=body.description,
    )
    return {"dataset": _dataset_dict(ds)}


@router.get("/projects/{project_id}/datasets", summary="数据集列表+最新版本")
def list_datasets(project_id: int, db: DbSession, user: CurrentUser) -> dict:
    """项目数据集列表, 每个附带最新版本摘要(含质量报告; 需项目 view 能力)。"""
    items = dataset_ops.list_datasets_case(db, user, project_id=project_id)
    return {
        "datasets": [
            {
                "dataset": _dataset_dict(item["dataset"]),
                "latest_version": (_version_dict(item["latest_version"]) if item["latest_version"] else None),
            }
            for item in items
        ]
    }


@router.get("/projects/{project_id}/datasets/{dataset_id}", summary="数据集详情(版本列表+质量报告)")
def get_dataset(project_id: int, dataset_id: int, db: DbSession, user: CurrentUser) -> dict:
    """数据集详情: 元数据 + 全部版本(含质量报告与文件摘要; 需项目 view 能力)。"""
    result = dataset_ops.get_dataset_case(db, user, project_id=project_id, dataset_id=dataset_id)
    return {
        "dataset": _dataset_dict(result["dataset"]),
        "versions": [
            {**_version_dict(item["version"]), "files": item["files"]}
            for item in result["versions"]
        ],
    }


# ---------------------------------------------------------------------------
# 版本上传
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/datasets/{dataset_id}/versions",
    status_code=201,
    summary="上传数据集版本(multipart)",
)
def upload_version(
    project_id: int,
    dataset_id: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    file: Annotated[UploadFile, File(description="CSV 数据文件")],
    resolution: Annotated[str, Form(description="分辨率: 15min | 30min | 1h")],
    utc_offset_minutes: Annotated[int, Form(description="固定 UTC 偏移(分钟)")] = 480,
    fields: Annotated[str | None, Form(description='字段描述 JSON, 如 {"e_load": {"unit": "kWh"}}')] = None,
    meta: Annotated[
        str | None, Form(description="元信息 JSON: {source_category, license, provenance, created_reason}")
    ] = None,
):
    """上传并校验数据集版本(需项目 edit 能力)。

    大小门禁(H-08): 先按 Content-Length 头预检, 再以 (上限+1) 字节封顶流式读取,
    超限立即拒绝 —— 任何情况下都不会把超大文件完整读入内存。
    校验通过: 201 + {dataset_version, quality_report, diagnostics};
    存在阻断性诊断(行数/时间戳/缺失/范围等): 400 + 标准错误信封
    (诊断明细入 params.diagnostics, 字段/行号定位)。

    授权 → 归属 → 配额 → 保存的业务顺序由完整用例拥有; 本路由只做传输适配、
    输入 DTO 校验与错误/响应映射。
    """
    if resolution not in RESOLUTIONS:
        raise http_error(400, "API-REQ-001", "ies.error.invalid_resolution", resolution=resolution)
    if not isinstance(utc_offset_minutes, int) or not (-720 <= utc_offset_minutes <= 840):
        raise http_error(400, "API-REQ-001", "ies.error.invalid_utc_offset", value=utc_offset_minutes)

    # Content-Length 预检(存在时; multipart 包含其他字段, 仅作快速拒绝)
    try:
        raw_len = int(request.headers.get("content-length", "0"))
    except ValueError:
        raw_len = 0
    if raw_len > _MAX_UPLOAD_BYTES:
        raise http_error(400, "API-REQ-001", "ies.error.file_too_large", max_bytes=_MAX_UPLOAD_BYTES)

    # 封顶流式读取: 最多读 (上限+1) 字节, 超出即拒绝(内存占用有界)
    data = file.file.read(_MAX_UPLOAD_BYTES + 1)
    if not data:
        raise http_error(400, "API-REQ-001", "ies.error.empty_file", filename=file.filename or "")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise http_error(400, "API-REQ-001", "ies.error.file_too_large", max_bytes=_MAX_UPLOAD_BYTES)

    fields_dict = _parse_json_field(fields, "fields")
    meta_dict = _parse_json_field(meta, "meta")

    # 0.2.0 A4: meta/fields schema 白名单 —— 拒绝未知键与畸形结构(不破坏合法上传)
    meta_errors = validate_upload_meta(meta_dict)
    fields_errors = validate_upload_fields(fields_dict)
    if meta_errors or fields_errors:
        raise http_error(
            400, META_CODE, META_MESSAGE_KEY,
            errors=meta_errors + fields_errors,
        )

    # 完整用例(授权 → 归属 → 配额 → 保存); 配额超限 413, 阻断性诊断 400 信封。
    # 0.2.0 A4 配额门禁语义不变(超配额 413; 默认不启用, 本地开发宽松)。
    try:
        version = dataset_ops.upload_dataset_version_case(
            db, user,
            project_id=project_id, dataset_id=dataset_id,
            resolution=resolution, utc_offset_minutes=utc_offset_minutes,
            fields=fields_dict, data=data, meta=meta_dict,
        )
    except QuotaError as exc:
        raise http_error(
            413, QUOTA_CODE, QUOTA_MESSAGE_KEY,
            used_bytes=exc.used_bytes, quota_bytes=exc.quota_bytes,
            scope=exc.scope, owner_id=exc.owner_id,
        ) from exc
    except DataValidationError as exc:
        return _validation_response(exc)
    return {
        "dataset_version": _version_dict(version),
        "quality_report": version.quality_report,
        "diagnostics": (version.quality_report or {}).get("diagnostics", []),
    }


# ---------------------------------------------------------------------------
# 版本查询(元数据+溯源, 数据文件不直接返回)
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_id}/datasets/{dataset_id}/versions/{version_no}",
    summary="版本元数据+溯源",
)
def get_version(project_id: int, dataset_id: int, version_no: int, db: DbSession, user: CurrentUser) -> dict:
    """版本详情: 元数据 + 溯源 + 许可证 + 文件引用(对象哈希/大小), 不返回数据本体。"""
    result = dataset_ops.get_dataset_version_case(
        db, user, project_id=project_id, dataset_id=dataset_id, version_no=version_no
    )
    version = result["version"]
    return {
        "dataset_version": _version_dict(version),
        "provenance": version.provenance,
        "license": version.license,
        "files": result["files"],
        "data": result["data"],
    }


# ---------------------------------------------------------------------------
# 内置样例
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/datasets/{dataset_id}/sample",
    status_code=201,
    summary="生成内置样例数据版本",
)
def create_sample(
    project_id: int,
    dataset_id: int,
    db: DbSession,
    user: CurrentUser,
    resolution: Annotated[str, Query(description="分辨率: 15min | 30min | 1h")] = "1h",
    region: Annotated[str, Query(description="地区: shanghai | beijing | guangzhou")] = "shanghai",
) -> dict:
    """为目标数据集生成确定性内置样例数据版本(REQ-DATA-003/004; 需项目 edit 能力)。"""
    if resolution not in RESOLUTIONS:
        raise http_error(400, "API-REQ-001", "ies.error.invalid_resolution", resolution=resolution)
    version = dataset_ops.create_sample_case(
        db, user, project_id=project_id, dataset_id=dataset_id,
        resolution=resolution, region=region,
    )
    return {
        "dataset_version": _version_dict(version),
        "quality_report": version.quality_report,
    }
