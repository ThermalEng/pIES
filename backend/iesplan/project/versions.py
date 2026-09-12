"""项目域版本/草稿读取与纯组装（归属 project）。

版本/草稿编排（创建/恢复/应用/冻结/模型清单推进）归属
``application.projects.versions``；本模块仅保留 raising 读取、
纯版本内容组装与序列化。外部经 ``iesplan.project`` 门面消费；
不得导入 ``iesplan.models``、services、storage 或其他域的内部模块。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from iesplan.core.diagnostics import SEVERITY_ERROR, SYS_STORE_CORRUPT
from iesplan.core.errors import AppError, NotFoundError
from iesplan.project import persistence
from iesplan.project.contracts import DraftRecord, ProjectRecord, ProjectVersionRecord


def require_project(db: Session, project_id: int) -> ProjectRecord:
    """按 id 取项目; 不存在或已删除(软删)一律 404(无回收站语义)。"""
    project = persistence.get_project(db, project_id)
    if project is None:
        raise NotFoundError(
            "项目不存在",
            params={"project_id": project_id},
            location={"object_type": "project", "object_id": project_id},
        )
    return project


def require_current_draft(db: Session, project: ProjectRecord) -> DraftRecord:
    """取项目当前草稿(is_current=true 且修订最大者); 缺失视为数据损坏。"""
    draft = persistence.get_current_draft(db, project.id)
    if draft is None:
        raise AppError(
            "项目缺少当前草稿(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
            location={"object_type": "project", "object_id": project.id},
        )
    return draft


def require_version(db: Session, project_id: int, version_id: int) -> ProjectVersionRecord:
    """按 id 获取项目版本(须属于该项目, 否则 404)。"""
    version = persistence.get_version(db, project_id, version_id)
    if version is None:
        raise NotFoundError(
            "版本不存在",
            params={"project_id": project_id, "version_id": version_id},
            location={"object_type": "project_version", "object_id": version_id},
        )
    return version


# ---------------------------------------------------------------------------
# 序列化与内容载体
# ---------------------------------------------------------------------------


def project_to_dict(project: ProjectRecord) -> dict:
    """项目序列化(API 展示；时间已为 ISO 字符串，与既有 JSON 输出一致)。"""
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "status": project.status,
        "owner_id": project.owner_id,
        "currency": project.currency,
        "project_baseline": {
            "resolution": project.baseline_resolution,
            "leap_year": project.baseline_leap_year,
            "scenario_mode": project.baseline_scenario_mode,
        },
        "schema_version": project.schema_version,
        "current_draft_id": project.current_draft_id,
        "current_version_id": project.current_version_id,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "created_by": project.created_by,
    }


def draft_to_dict(draft: DraftRecord) -> dict:
    """草稿摘要序列化。"""
    return {
        "id": draft.id,
        "revision": draft.revision,
        "content_object_id": draft.content_object_id,
        "parent_draft_id": draft.parent_draft_id,
        "updated_by": draft.updated_by,
        "updated_at": draft.updated_at,
        "created_at": draft.created_at,
    }


def version_to_dict(version: ProjectVersionRecord) -> dict:
    """版本序列化(API 展示；时间已为 ISO 字符串，与既有 JSON 输出一致)。"""
    return {
        "id": version.id,
        "project_id": version.project_id,
        "version_no": version.version_no,
        "name": version.name,
        "description": version.description,
        "created_by": version.created_by,
        "created_at": version.created_at,
        "parent_version_id": version.parent_version_id,
        "source_draft_id": version.source_draft_id,
        "source_draft_revision": version.source_draft_revision,
        "reason": version.reason,
        "project_baseline": {
            "resolution": version.baseline_resolution,
            "leap_year": version.baseline_leap_year,
            "scenario_mode": version.baseline_scenario_mode,
        },
        "currency": version.currency,
        "schema_version": version.schema_version,
        "content_object_id": version.content_object_id,
    }


def build_version_content(
    content: dict,
    *,
    currency: str,
    baseline_resolution: str,
    baseline_leap_year: bool,
    scenario_mode: str,
    effective_profile_id: str | None = None,
    effective_revision: int | None = None,
    planning_revision: int | None = None,
) -> dict:
    """版本内容 = 草稿领域内容(去命令簿记) + 项目固化字段(domain-model §项目聚合)。

    纯函数: 调用方(application)负责解析财务/规划引用来源(当前值或行指针),
    本函数只做组装。未提供引用时对应块省略(不静默默认)。
    """
    version_content = {k: v for k, v in content.items() if k != "applied_commands"}
    version_content["currency"] = currency
    version_content["project_baseline"] = {
        "resolution": baseline_resolution,
        "leap_year": baseline_leap_year,
        "scenario_mode": scenario_mode,
    }
    if effective_profile_id is not None and effective_revision is not None:
        version_content["effective_finance"] = {
            "profile_id": effective_profile_id,
            "revision": effective_revision,
        }
    if planning_revision is not None:
        version_content["planning_config"] = {
            "revision": planning_revision,
        }
    return version_content


__all__ = [
    "build_version_content",
    "draft_to_dict",
    "project_to_dict",
    "require_current_draft",
    "require_project",
    "require_version",
    "version_to_dict",
]
