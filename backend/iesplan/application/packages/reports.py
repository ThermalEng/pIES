"""生成报告与按项目权限读取导出对象。"""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy.orm import Session

from iesplan import package as package_domain
from iesplan.application.packages.transfers import export_excel as _export_excel
from iesplan.core.content import Content
from iesplan.identity.contracts import UserRecord
from iesplan.application.projects import ensure_access
from iesplan.core.errors import NotFoundError
from iesplan.storage import add_ref, get_object_content, list_refs, put_object

EXCEL_MEDIA_TYPE = package_domain.EXCEL_MEDIA_TYPE
PACKAGE_MEDIA_TYPE = package_domain.PACKAGE_MEDIA_TYPE

__all__ = [
    "EXCEL_MEDIA_TYPE",
    "PACKAGE_MEDIA_TYPE",
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
    """生成 Excel 报告并登记对象/引用, 返回导出对象 ID。"""
    excel_bytes = _export_excel(
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
    return {
        "object_id": obj.id,
        "file_name": f"report-{project_id}-{evidence_package_id}.xlsx",
        "size_bytes": obj.size_bytes,
    }


def load_export_download(
    db: Session,
    user: UserRecord,
    project_id: int,
    object_id: int,
    kind: Literal["excel", "package"],
) -> tuple[Content, str, str]:
    """Resolve a project export after checking current project permissions."""
    ref_type = f"export_{kind}"
    ensure_access(db, user, project_id, ref_type)
    if not any(
        ref.ref_type == ref_type
        and ref.ref_entity_type == "projects"
        and ref.ref_entity_id == str(project_id)
        for ref in list_refs(db, object_id)
    ):
        raise NotFoundError("该项目下没有此导出对象")
    content = get_object_content(db, object_id)
    if kind == "excel":
        return content, EXCEL_MEDIA_TYPE, f"report-{project_id}.xlsx"
    return content, PACKAGE_MEDIA_TYPE, f"project-package-{project_id}.zip"
