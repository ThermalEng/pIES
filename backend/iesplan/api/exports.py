"""项目导出 API 路由(U15/U14, prefix /api/projects/{project_id}/exports)。

认证说明: 统一使用 U01 身份单元提供的窗口会话认证
(iesplan.api.auth.CurrentUser; 未认证 401, 权限不足 403)。

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

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from iesplan.api.auth import CurrentUser
from iesplan.db import get_db
from iesplan.services import package as package_service
from iesplan.storage import add_ref, get_object, list_refs, put_object

router = APIRouter(prefix="/api/projects/{project_id}/exports", tags=["exports"])


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
    user: CurrentUser,
) -> dict:
    """生成固定模板 Excel 报告, 返回短期单对象下载授权 token(5 分钟)。"""
    excel_bytes = package_service.export_excel(
        db, user, project_id, payload.evidence_package_id, payload.assessment_id, lang=payload.lang
    )
    obj = put_object(
        db, excel_bytes, package_service.EXCEL_MEDIA_TYPE, source_category="excel_report",
    )
    add_ref(
        db, obj.id, "export_excel", project_id,
        ref_entity_type="projects", purpose="Excel 报告导出",
    )
    db.commit()
    token = package_service.create_download_token(
        obj.id, "excel", project_id=project_id, user_id=user.id
    )
    return {
        "token": token,
        "expires_at_seconds": package_service.DOWNLOAD_TOKEN_TTL_SECONDS,
        "file_name": f"report-{project_id}-{payload.evidence_package_id}.xlsx",
        "sha256": obj.sha256,
        "size_bytes": obj.size_bytes,
    }


def _authorize_download(
    db: Session,
    info: dict,
    project_id: int,
    user: Any,
) -> dict:
    """下载授权校验(C-04): token 绑定的项目/用户必须与当前请求一致。

    防止跨项目对象下载(token 泄漏或伪造场景下仍无法越权读取)。
    额外校验: token 指向的 object 必须确实被该 project 引用(见下),
    封堵"用可预测签名 + 自填 user_id/project_id 伪造 token 下载他项目对象"的路径。
    """
    if info.get("project_id") != project_id or info.get("user_id") != user.id:
        raise package_service.DownloadTokenError(
            "", params={"reason": "project_or_user_mismatch", "project_id": project_id}
        )
    # 归属校验: object 必须存在一条指向该 project 的 owner 引用。
    # 否则即使签名/绑定校验通过, 也说明该对象不属于此项目 → 拒绝。
    refs = list_refs(db, info["object_id"])
    if not any(ref.ref_entity_id == str(project_id) for ref in refs):
        raise package_service.DownloadTokenError(
            "", params={"reason": "object_not_in_project", "project_id": project_id}
        )
    return info


@router.get("/excel/download", summary="下载 Excel 报告")
def download_excel_endpoint(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
    token: str = Query(..., description="短期下载授权 token"),
) -> Response:
    """凭短期授权 token 下载 Excel 报告字节(校验签名/过期 + 项目与用户绑定)。"""
    info = package_service.verify_download_token(token, expected_kind="excel")
    _authorize_download(db, info, project_id, user)
    content = get_object(db, info["object_id"])
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
    user: CurrentUser,
) -> dict:
    """导出完整项目包(模型/配置/版本/数据集/历史证据, 仅所有者), 返回下载授权。"""
    result = package_service.export_package(db, user, project_id)
    db.commit()
    return result.to_dict()


@router.get("/package/download", summary="下载项目包")
def download_package_endpoint(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
    token: str = Query(..., description="短期下载授权 token"),
) -> Response:
    """凭短期授权 token 下载项目包 zip(校验签名/过期 + 项目与用户绑定)。"""
    info = package_service.verify_download_token(token, expected_kind="package")
    _authorize_download(db, info, project_id, user)
    content = get_object(db, info["object_id"])
    return Response(
        content=content,
        media_type=package_service.PACKAGE_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="project-package-{project_id}.zip"'
        },
    )
