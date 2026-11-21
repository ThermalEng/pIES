"""项目导出与已认证的对象流式下载。"""

from __future__ import annotations

from itertools import chain
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from iesplan.api.auth import CurrentUser
from iesplan.api.deps import get_request_db
from iesplan.application import packages as packages_app
from iesplan.core.content import Content

router = APIRouter(prefix="/api/projects/{project_id}/exports", tags=["exports"])


def _download_response(content: Content, media_type: str, file_name: str) -> StreamingResponse:
    # Start the actual read before HTTP headers, so storage errors retain their status.
    chunks = content.iter_chunks()
    first = next(chunks, b"")
    return StreamingResponse(
        content=chain((first,), chunks),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )


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
    db: Annotated[Session, Depends(get_request_db)],
    user: CurrentUser,
) -> dict:
    """生成固定模板 Excel 报告, 返回导出对象 ID。"""
    return packages_app.export_excel_report(
        db, user, project_id, payload.evidence_package_id, payload.assessment_id,
        lang=payload.lang,
    )


@router.get("/excel/{object_id}/download", summary="下载 Excel 报告")
def download_excel_endpoint(
    project_id: int,
    object_id: int,
    db: Annotated[Session, Depends(get_request_db)],
    user: CurrentUser,
) -> Response:
    """下载当前项目的 Excel 报告。"""
    content, media_type, file_name = packages_app.load_export_download(
        db, user, project_id, object_id, "excel"
    )
    return _download_response(content, media_type, file_name)


@router.post("/package", summary="导出完整项目包(仅所有者)")
def export_package_endpoint(
    project_id: int,
    db: Annotated[Session, Depends(get_request_db)],
    user: CurrentUser,
) -> dict:
    """导出完整项目包(模型/配置/版本/数据集/历史证据, 仅所有者), 返回导出对象 ID。"""
    result = packages_app.export_package(db, user, project_id)
    return result.to_dict()


@router.get("/package/{object_id}/download", summary="下载项目包")
def download_package_endpoint(
    project_id: int,
    object_id: int,
    db: Annotated[Session, Depends(get_request_db)],
    user: CurrentUser,
) -> Response:
    """下载当前项目的 ZIP 文件。"""
    content, media_type, file_name = packages_app.load_export_download(
        db, user, project_id, object_id, "package"
    )
    return _download_response(content, media_type, file_name)
