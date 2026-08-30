"""规划/财务配置 revision 服务(0.6.5 事项 3)。

职责:
- 保存 = 追加新的不可变 revision 行(finance_configs / planning_configs,
  仅 INSERT)并更新 projects 当前生效指针; revision 号单调递增;
- 并发保护: 保存必须携带 expected_revision(当前指针值), 不匹配 → 409
  (宪法 8.4 乐观锁, 禁止最后写入静默覆盖);
- 规划配置保存时强制其 finance_revision 与项目**当前** FinanceConfig
  revision 一致(宪法 4.6: 规划与结果财务计算固定同一 revision);
- 领域校验失败 → InvalidRequestError(携带 PROJ-FIN/PROJ-PLAN 诊断),
  不落任何行。

本层不主动 commit, 事务边界由 API 层控制。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from iesplan.core.contracts import FinanceConfig, PlanningConfig
from iesplan.core.diagnostics import SEVERITY_ERROR
from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.finance.contracts import (
    check_finance_revision,
    validate_finance_domain,
)
from iesplan.models.config_revision import (
    FinanceConfigRevision,
    PlanningConfigRevision,
)
from iesplan.models.project import Project
from iesplan.planning.contracts import validate_planning_domain


class InvalidRequestError(AppError):
    """配置校验失败(HTTP 400; code 为 PROJ-FIN/PROJ-PLAN 领域码)。"""

    code = "PROJ-FIN-001"
    http_status = 400
    severity = SEVERITY_ERROR
    message_key = "ies.diag.param.invalid"


def _get_project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None or project.status == "deleted":
        raise NotFoundError(
            "项目不存在",
            params={"project_id": project_id},
            location={"object_type": "project", "object_id": project_id},
        )
    return project


def _diag_params(diags: Sequence) -> dict:
    """领域诊断 → 错误信封 params(诊断明细, 供前端按 message_key 渲染)。"""
    return {
        "count": len(diags),
        "diagnostics": [
            {
                "code": d.code,
                "detail": d.params.get("detail") or "",
                "field": (d.location or {}).get("field") or "",
            }
            for d in diags
        ],
    }


# ---------------------------------------------------------------------------
# 财务配置
# ---------------------------------------------------------------------------


def get_finance_config(
    db: Session, project_id: int
) -> tuple[FinanceConfig, int, FinanceConfigRevision]:
    """读取项目当前生效财务配置; 未保存过 → 404(无静默默认, 宪法 2.2)。"""
    project = _get_project(db, project_id)
    if project.finance_revision is None:
        raise NotFoundError(
            "项目尚未保存公共财务配置",
            params={"project_id": project_id},
            location={"object_type": "finance_config", "object_id": project_id},
        )
    row = db.execute(
        select(FinanceConfigRevision)
        .where(
            FinanceConfigRevision.project_id == project_id,
            FinanceConfigRevision.revision == project.finance_revision,
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError(
            "项目财务配置指针损坏(指向不存在的 revision)",
            code="PROJ-FIN-003",
            params={"project_id": project_id, "revision": project.finance_revision},
        )
    config = FinanceConfig.from_dict(row.content)
    if config.revision != row.content_sha256:
        raise AppError(
            "财务配置内容摘要与持久化摘要不一致(数据损坏)",
            code="PROJ-FIN-003",
            params={"project_id": project_id, "revision": row.revision},
        )
    return config, row.revision, row


def save_finance_config(
    db: Session,
    project_id: int,
    payload: object,
    expected_revision: int | None,
    user_id: int,
) -> tuple[FinanceConfigRevision, int]:
    """保存财务配置: 追加不可变 revision + 更新项目指针。

    参数:
        payload: 请求中的 finance_config 字典(严格恢复, 含派生 revision
            字段时校验一致性)。
        expected_revision: 当前生效 revision(首次保存传 None);
            不匹配 → 409 ConflictError。
    """
    project = _get_project(db, project_id)
    try:
        config = FinanceConfig.from_dict(payload)
    except Exception as exc:  # FinanceConfigError
        raise InvalidRequestError(
            f"公共财务配置非法: {exc}", code="PROJ-FIN-001", params={"detail": str(exc)}
        ) from exc
    diags = validate_finance_domain(config)
    if diags:
        raise InvalidRequestError(
            "公共财务配置领域校验失败",
            code="PROJ-FIN-002",
            params=_diag_params(diags),
        )
    if project.finance_revision != expected_revision:
        raise ConflictError(
            "财务配置已在其他会话中修改, 请基于最新 revision 重试",
            code="SYS-STORE-004",
            params={"expected_revision": expected_revision, "current": project.finance_revision},
        )
    next_revision = (project.finance_revision or 0) + 1
    row = FinanceConfigRevision(
        project_id=project_id,
        revision=next_revision,
        content=config.to_dict(),
        content_sha256=config.revision,
        created_by=user_id,
    )
    db.add(row)
    project.finance_revision = next_revision
    db.flush()
    return row, next_revision


# ---------------------------------------------------------------------------
# 规划配置
# ---------------------------------------------------------------------------


def get_planning_config(
    db: Session, project_id: int
) -> tuple[PlanningConfig, int, PlanningConfigRevision]:
    """读取项目当前生效规划配置; 未保存过 → 404。"""
    project = _get_project(db, project_id)
    if project.planning_revision is None:
        raise NotFoundError(
            "项目尚未保存规划配置",
            params={"project_id": project_id},
            location={"object_type": "planning_config", "object_id": project_id},
        )
    row = db.execute(
        select(PlanningConfigRevision)
        .where(
            PlanningConfigRevision.project_id == project_id,
            PlanningConfigRevision.revision == project.planning_revision,
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError(
            "项目规划配置指针损坏(指向不存在的 revision)",
            code="PROJ-PLAN-004",
            params={"project_id": project_id, "revision": project.planning_revision},
        )
    config = PlanningConfig.from_dict(row.content)
    if config.revision != row.content_sha256:
        raise AppError(
            "规划配置内容摘要与持久化摘要不一致(数据损坏)",
            code="PROJ-PLAN-004",
            params={"project_id": project_id, "revision": row.revision},
        )
    return config, row.revision, row


def save_planning_config(
    db: Session,
    project_id: int,
    payload: object,
    expected_revision: int | None,
    user_id: int,
) -> tuple[PlanningConfigRevision, int]:
    """保存规划配置: 强制 finance_revision 与当前财务配置 revision 一致。

    - 项目未保存财务配置 → 422(规划必须引用已存在的 FinanceConfig revision);
    - payload.finance_revision ≠ 当前财务配置 revision → 422(PROJ-PLAN-002),
      不落任何行。
    """
    project = _get_project(db, project_id)
    try:
        config = PlanningConfig.from_dict(payload)
    except Exception as exc:  # PlanningConfigError
        raise InvalidRequestError(
            f"规划配置非法: {exc}", code="PROJ-PLAN-001", params={"detail": str(exc)}
        ) from exc
    diags = validate_planning_domain(config)
    if diags:
        raise InvalidRequestError(
            "规划配置领域校验失败",
            code="PROJ-PLAN-003",
            params=_diag_params(diags),
        )
    if project.finance_revision is None:
        raise InvalidRequestError(
            "规划配置必须引用已保存的公共财务配置(请先保存 FinanceConfig)",
            code="PROJ-PLAN-002",
            params={"detail": "项目未保存财务配置"},
        )
    finance, _, _ = get_finance_config(db, project_id)
    revision_diags = check_finance_revision(config, finance)
    if revision_diags:
        raise InvalidRequestError(
            "规划配置引用的 FinanceConfig revision 与当前配置不一致",
            code="PROJ-PLAN-002",
            params=_diag_params(revision_diags),
        )
    if project.planning_revision != expected_revision:
        raise ConflictError(
            "规划配置已在其他会话中修改, 请基于最新 revision 重试",
            code="SYS-STORE-004",
            params={"expected_revision": expected_revision, "current": project.planning_revision},
        )
    next_revision = (project.planning_revision or 0) + 1
    row = PlanningConfigRevision(
        project_id=project_id,
        revision=next_revision,
        content=config.to_dict(),
        content_sha256=config.revision,
        finance_revision=config.finance_revision,
        created_by=user_id,
    )
    db.add(row)
    project.planning_revision = next_revision
    db.flush()
    return row, next_revision
