"""项目导出 API 路由(U15/U14, prefix /api/projects/{project_id}/exports)。

认证说明: 正式会话认证由 U01 身份单元提供; 本阶段以 X-User-Id 请求头模拟
认证主体(与 iesplan.api.projects 同一约定), 集成时替换依赖即可。

路由清单(prefix /api/projects/{project_id}/exports):
- POST /excel      生成固定模板 Excel 报告(查看者可导出)→ 短期单对象下载授权
- GET  /excel/download?token=   下载 Excel 报告字节
- POST /package    导出完整项目包(仅所有者)→ 短期单对象下载授权
- GET  /package/download?token= 下载项目包 zip

下载授权: 短期单对象授权(签名 token 含 object_id + 过期, 过期 5 分钟,
services/package.py create_download_token/verify_download_token)。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from iesplan.core.diagnostics import SEVERITY_BLOCKING
from iesplan.core.errors import AppError
from iesplan.db import get_db
from iesplan.models.identity import User
from iesplan.services import objects as objects_service
from iesplan.services import package as package_service

router = APIRouter(prefix="/api/projects/{project_id}/exports", tags=["exports"])


def _http_error(status: int, code: str, message_key: str, params: dict[str, Any]) -> AppError:
    """构造带指定 HTTP 状态码的应用错误(状态码在错误实例上设置)。"""
    err = AppError("", code=code, severity=SEVERITY_BLOCKING, message_key=message_key, params=params)
    err.http_status = status
    return err


def get_current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User:
    """当前认证主体(阶段实现: 从 X-User-Id 请求头读取; 正式会话认证由 U01 提供)。"""
    raw = request.headers.get("X-User-Id")
    if not raw:
        raise _http_error(401, "AUTH-REQ-001", "ies.diag.perm.denied", {"reason": "missing_identity"})
    try:
        user_id = int(raw)
    except ValueError as exc:
        raise _http_error(401, "AUTH-REQ-001", "ies.diag.perm.denied", {"reason": "bad_identity"}) from exc
    user = db.get(User, user_id)
    if user is None:
        raise _http_error(401, "AUTH-REQ-001", "ies.diag.perm.denied", {"reason": "unknown_user"})
    return user


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class ExcelExportRequest(BaseModel):
    """Excel 报告导出请求(固定引用证据包与评估, 不重新求解)。"""

    evidence_package_id: int
    assessment_id: int
    lang: str = Field(default="zh", pattern="^(zh|en)$", description="报告语言(zh 简体中文 / en 英语)")


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.post("/excel", summary="生成 Excel 报告(查看者可导出)")
def export_excel_endpoint(
    project_id: int,
    payload: ExcelExportRequest,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """生成固定模板 Excel 报告, 返回短期单对象下载授权 token(5 分钟)。"""
    excel_bytes = package_service.export_excel(
        db, user, project_id, payload.evidence_package_id, payload.assessment_id, lang=payload.lang
    )
    obj = objects_service.put_object(
        db, excel_bytes, package_service.EXCEL_MEDIA_TYPE, source_category="excel_report",
    )
    objects_service.add_ref(
        db, obj.id, "export_excel", project_id,
        ref_entity_type="projects", purpose="Excel 报告导出",
    )
    db.commit()
    token = package_service.create_download_token(obj.id, "excel")
    return {
        "token": token,
        "expires_at_seconds": package_service.DOWNLOAD_TOKEN_TTL_SECONDS,
        "file_name": f"report-{project_id}-{payload.evidence_package_id}.xlsx",
        "sha256": obj.sha256,
        "size_bytes": obj.size_bytes,
    }


@router.get("/excel/download", summary="下载 Excel 报告")
def download_excel_endpoint(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    token: str = Query(..., description="短期下载授权 token"),
) -> Response:
    """凭短期授权 token 下载 Excel 报告字节(校验签名与过期)。"""
    info = package_service.verify_download_token(token, expected_kind="excel")
    content = objects_service.get_object(db, info["object_id"])
    return Response(
        content=content,
        media_type=package_service.EXCEL_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="report-{project_id}.xlsx"'
        },
    )


@router.post("/package", summary="导出完整项目包(仅所有者)")
def export_package_endpoint(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """导出完整项目包(模型/配置/版本/数据集/历史证据, 仅所有者), 返回下载授权。"""
    result = package_service.export_package(db, user, project_id)
    db.commit()
    return result.to_dict()


@router.get("/package/download", summary="下载项目包")
def download_package_endpoint(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    token: str = Query(..., description="短期下载授权 token"),
) -> Response:
    """凭短期授权 token 下载项目包 zip(校验签名与过期)。"""
    info = package_service.verify_download_token(token, expected_kind="package")
    content = objects_service.get_object(db, info["object_id"])
    return Response(
        content=content,
        media_type=package_service.PACKAGE_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="project-package-{project_id}.zip"'
        },
    )
