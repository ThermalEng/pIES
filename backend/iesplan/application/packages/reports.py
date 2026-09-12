"""导出面向 API 的薄封装用例(application/packages): 报告/下载授权。

只做转调与路由原有编排的整体下移, 不新增校验/hash/回退:

- ``export_excel_report``: 路由原顺序(生成报告字节 → 对象登记 → 引用 →
  提交 → 签发下载 token)整体下移, 提交由本用例拥有;
- ``authorize_download``/``load_export_download``: 路由原下载授权校验
  (项目/用户绑定 + 对象归属)与对象读取整体下移;
- 常量与 ``DownloadTokenError`` 取自 ``iesplan.package`` 领域公开门面同名符号
  (旧服务 ``services.package`` 已删除，见纠偏 Wave 1 切片 D)。

依赖方向: api → application → (package/storage 域公开门面)。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from iesplan import package as package_domain
from iesplan.identity.contracts import UserRecord
from iesplan.package import DownloadTokenError
from iesplan.storage import add_ref, get_object, list_refs, put_object

EXCEL_MEDIA_TYPE = package_domain.EXCEL_MEDIA_TYPE
PACKAGE_MEDIA_TYPE = package_domain.PACKAGE_MEDIA_TYPE
DOWNLOAD_TOKEN_TTL_SECONDS = package_domain.DOWNLOAD_TOKEN_TTL_SECONDS

__all__ = [
    "DOWNLOAD_TOKEN_TTL_SECONDS",
    "EXCEL_MEDIA_TYPE",
    "PACKAGE_MEDIA_TYPE",
    "DownloadTokenError",
    "authorize_download",
    "export_excel_report",
    "load_export_download",
]


def export_excel_report(
    db: Session,
    user: UserRecord,
    project_id: int,
    evidence_package_id: int,
    assessment_id: int,
    lang: str = "zh",
) -> dict[str, Any]:
    """生成 Excel 报告并登记对象/引用, 返回短期单对象下载授权(路由原顺序)。"""
    excel_bytes = package_domain.export_excel(
        db, user, project_id, evidence_package_id, assessment_id, lang=lang
    )
    try:
        obj = put_object(
            db, excel_bytes, EXCEL_MEDIA_TYPE, source_category="excel_report",
        )
        add_ref(
            db, obj.id, "export_excel", project_id,
            ref_entity_type="projects", purpose="Excel 报告导出",
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    token = package_domain.create_download_token(
        obj.id, "excel", project_id=project_id, user_id=user.id
    )
    return {
        "token": token,
        "expires_at_seconds": DOWNLOAD_TOKEN_TTL_SECONDS,
        "file_name": f"report-{project_id}-{evidence_package_id}.xlsx",
        "size_bytes": obj.size_bytes,
    }


def authorize_download(
    db: Session,
    info: dict,
    project_id: int,
    user_id: int,
) -> dict:
    """下载授权校验(路由原逻辑: 项目/用户绑定 + 对象归属引用)。"""
    if info.get("project_id") != project_id or info.get("user_id") != user_id:
        raise DownloadTokenError(
            "", params={"reason": "project_or_user_mismatch", "project_id": project_id}
        )
    refs = list_refs(db, info["object_id"])
    if not any(ref.ref_entity_id == str(project_id) for ref in refs):
        raise DownloadTokenError(
            "", params={"reason": "object_not_in_project", "project_id": project_id}
        )
    return info


def load_export_download(
    db: Session,
    user: UserRecord,
    project_id: int,
    token: str,
    expected_kind: str,
) -> tuple[bytes, str, str]:
    """校验下载授权并读取对象字节, 返回 (内容, 媒体类型, 文件名)。"""
    info = package_domain.verify_download_token(token, expected_kind=expected_kind)
    authorize_download(db, info, project_id, user.id)
    content = get_object(db, info["object_id"])
    if expected_kind == "excel":
        return content, EXCEL_MEDIA_TYPE, f"report-{project_id}.xlsx"
    return content, PACKAGE_MEDIA_TYPE, f"project-package-{project_id}.zip"
