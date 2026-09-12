"""结果域 repository SQL 实现（证据/评估/索引/选中/报告表，归属 results）。

实现规则：
- 证据/评估/选中只 INSERT（不可变）；索引 latest 切换与选中 current
  切换在同事务内完成（旧行翻转 + 新行插入）， savepoint 语义由调用方
  事务保证；绝不 commit/rollback；
- 系统评估与人工评估走不同方法，禁止互相转换。
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from iesplan.models.result import (
    EvidencePackage,
    Report,
    ResultAssessment,
    ResultIndex,
    ResultSelection,
)
from iesplan.results.contracts import (
    EvidencePackageRecord,
    ReportRecord,
    ResultAssessmentRecord,
    ResultIndexRecord,
    ResultSelectionRecord,
)


def _iso(value: datetime | None) -> str | None:
    """ORM 时间 → 记录字符串（原样 isoformat，不增减时区后缀）。"""
    return value.isoformat() if value is not None else None


def _row_to_evidence(row: EvidencePackage) -> EvidencePackageRecord:
    return EvidencePackageRecord(
        id=row.id,
        task_id=row.task_id,
        calc_snapshot_id=row.calc_snapshot_id,
        object_id=row.object_id,
        status=row.status,
        attempt_id=row.attempt_id,
        created_by=row.created_by,
        created_at=_iso(row.created_at),
    )


def _row_to_assessment(row: ResultAssessment) -> ResultAssessmentRecord:
    return ResultAssessmentRecord(
        id=row.id,
        evidence_package_id=row.evidence_package_id,
        assessor=row.assessor,
        dimension_physical=row.dimension_physical,
        dimension_optimality=row.dimension_optimality,
        dimension_financial=row.dimension_financial,
        dimension_reliability=row.dimension_reliability,
        assessed_by=row.assessed_by,
        overall_score=float(row.overall_score) if row.overall_score is not None else None,
        comment=row.comment,
        detail=row.detail,
        created_at=_iso(row.created_at),
    )


def _row_to_index(row: ResultIndex) -> ResultIndexRecord:
    return ResultIndexRecord(
        id=row.id,
        project_id=row.project_id,
        project_version_id=row.project_version_id,
        evidence_package_id=row.evidence_package_id,
        assessment_id=row.assessment_id,
        is_latest=row.is_latest,
        created_at=_iso(row.created_at),
    )


def _row_to_selection(row: ResultSelection) -> ResultSelectionRecord:
    return ResultSelectionRecord(
        id=row.id,
        project_id=row.project_id,
        result_index_id=row.result_index_id,
        selected_by=row.selected_by,
        reason=row.reason,
        is_current=row.is_current,
        selected_at=_iso(row.selected_at),
    )


def _row_to_report(row: Report) -> ReportRecord:
    return ReportRecord(
        id=row.id,
        project_id=row.project_id,
        report_type=row.report_type,
        object_id=row.object_id,
        status=row.status,
        generated_by_task_id=row.generated_by_task_id,
        generated_by=row.generated_by,
    )


# ---------------------------------------------------------------------------
# 证据包
# ---------------------------------------------------------------------------


def create_evidence(
    db: Session,
    *,
    task_id: int,
    calc_snapshot_id: int,
    object_id: int,
    status: str,
    attempt_id: int | None = None,
    created_by: int = 0,
) -> EvidencePackageRecord:
    """创建证据包行（不可变，只 INSERT）。"""
    row = EvidencePackage(
        task_id=task_id,
        calc_snapshot_id=calc_snapshot_id,
        object_id=object_id,
        status=status,
        attempt_id=attempt_id,
        created_by=created_by,
    )
    db.add(row)
    db.flush()
    return _row_to_evidence(row)


def get_evidence(db: Session, package_id: int) -> EvidencePackageRecord | None:
    """按主键取证据包；不存在返回 None。"""
    row = db.get(EvidencePackage, package_id)
    return _row_to_evidence(row) if row is not None else None


def latest_evidence_for_task(db: Session, task_id: int) -> EvidencePackageRecord | None:
    """取任务最新证据包（id 倒序首个）；无返回 None。"""
    row = db.execute(
        select(EvidencePackage)
        .where(EvidencePackage.task_id == task_id)
        .order_by(EvidencePackage.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _row_to_evidence(row) if row is not None else None


def list_evidence_for_tasks(db: Session, task_ids: Collection[int]) -> list[EvidencePackageRecord]:
    """多任务的证据包行（id 升序；项目包导出归档用）。

    任务归属由调用方经 tasks 域解析；空输入返回空清单。
    """
    if not task_ids:
        return []
    rows = (
        db.execute(
            select(EvidencePackage).where(EvidencePackage.task_id.in_(task_ids)).order_by(EvidencePackage.id)
        )
        .scalars()
        .all()
    )
    return [_row_to_evidence(row) for row in rows]


def list_package_assessments(db: Session, package_id: int) -> list[ResultAssessmentRecord]:
    """证据包全部评估行（id 升序；项目包导出归档用）。"""
    rows = (
        db.execute(
            select(ResultAssessment)
            .where(ResultAssessment.evidence_package_id == package_id)
            .order_by(ResultAssessment.id)
        )
        .scalars()
        .all()
    )
    return [_row_to_assessment(row) for row in rows]


def list_package_index(db: Session, package_id: int) -> list[ResultIndexRecord]:
    """证据包结果索引行（项目包导出归档用）。"""
    rows = (
        db.execute(select(ResultIndex).where(ResultIndex.evidence_package_id == package_id)).scalars().all()
    )
    return [_row_to_index(row) for row in rows]


def evidence_statuses_for_tasks(db: Session, task_ids: Collection[int]) -> dict[int, str]:
    """批量取任务的证据包状态（每个任务取最新一个；无证据的任务不在返回中）。"""
    rows = (
        db.execute(
            select(EvidencePackage)
            .where(EvidencePackage.task_id.in_(task_ids))
            .order_by(EvidencePackage.id.desc())
        )
        .scalars()
        .all()
    )
    statuses: dict[int, str] = {}
    for row in rows:
        statuses.setdefault(row.task_id, row.status)
    return statuses


# ---------------------------------------------------------------------------
# 评估
# ---------------------------------------------------------------------------


def _insert_assessment(
    db: Session,
    *,
    evidence_package_id: int,
    assessor: str,
    dimensions: dict[str, str],
    overall_score: float | None,
    detail: dict[str, Any] | None,
    assessed_by: int | None,
    comment: str | None,
) -> ResultAssessmentRecord:
    row = ResultAssessment(
        evidence_package_id=evidence_package_id,
        assessor=assessor,
        assessed_by=assessed_by,
        dimension_physical=dimensions["physical"],
        dimension_optimality=dimensions["optimality"],
        dimension_financial=dimensions["financial"],
        dimension_reliability=dimensions["reliability"],
        overall_score=overall_score,
        comment=comment,
        detail=detail,
    )
    db.add(row)
    db.flush()
    return _row_to_assessment(row)


def create_system_assessment(
    db: Session,
    *,
    evidence_package_id: int,
    dimensions: dict[str, str],
    overall_score: float | None = None,
    detail: dict[str, Any] | None = None,
    assessed_by: int | None = None,
    comment: str | None = None,
) -> ResultAssessmentRecord:
    """写入系统评估（assessor='system'）。"""
    return _insert_assessment(
        db,
        evidence_package_id=evidence_package_id,
        assessor="system",
        dimensions=dimensions,
        overall_score=overall_score,
        detail=detail,
        assessed_by=assessed_by,
        comment=comment,
    )


def create_human_assessment(
    db: Session,
    *,
    evidence_package_id: int,
    assessed_by: int,
    dimensions: dict[str, str],
    overall_score: float | None = None,
    comment: str | None = None,
    detail: dict[str, Any] | None = None,
) -> ResultAssessmentRecord:
    """写入人工评估（assessor='human'）。"""
    return _insert_assessment(
        db,
        evidence_package_id=evidence_package_id,
        assessor="human",
        dimensions=dimensions,
        overall_score=overall_score,
        detail=detail,
        assessed_by=assessed_by,
        comment=comment,
    )


def get_assessment(db: Session, assessment_id: int) -> ResultAssessmentRecord | None:
    """按主键取评估；不存在返回 None。"""
    row = db.get(ResultAssessment, assessment_id)
    return _row_to_assessment(row) if row is not None else None


def latest_assessment(db: Session, evidence_package_id: int) -> ResultAssessmentRecord | None:
    """取证据包最新评估（id 倒序首个）；无返回 None。"""
    row = db.execute(
        select(ResultAssessment)
        .where(ResultAssessment.evidence_package_id == evidence_package_id)
        .order_by(ResultAssessment.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _row_to_assessment(row) if row is not None else None


def list_assessments(db: Session, evidence_package_id: int) -> list[ResultAssessmentRecord]:
    """取证据包全部评估（id 倒序）。"""
    rows = (
        db.execute(
            select(ResultAssessment)
            .where(ResultAssessment.evidence_package_id == evidence_package_id)
            .order_by(ResultAssessment.id.desc())
        )
        .scalars()
        .all()
    )
    return [_row_to_assessment(row) for row in rows]


def list_assessments_for_task(db: Session, task_id: int) -> list[ResultAssessmentRecord]:
    """任务全部证据包上的评估历史（id 倒序）。"""
    rows = (
        db.execute(
            select(ResultAssessment)
            .join(EvidencePackage, EvidencePackage.id == ResultAssessment.evidence_package_id)
            .where(EvidencePackage.task_id == task_id)
            .order_by(ResultAssessment.id.desc())
        )
        .scalars()
        .all()
    )
    return [_row_to_assessment(row) for row in rows]


# ---------------------------------------------------------------------------
# 索引 / 选中
# ---------------------------------------------------------------------------


def point_index_latest(
    db: Session,
    *,
    project_id: int,
    project_version_id: int,
    evidence_package_id: int,
    assessment_id: int | None = None,
) -> ResultIndexRecord:
    """将版本索引指向新证据（同证据只更新评估指针；新证据则旧 latest 翻转后插入新行）。"""
    existing = db.execute(
        select(ResultIndex).where(
            ResultIndex.project_version_id == project_version_id,
            ResultIndex.is_latest.is_(True),
        )
    ).scalar_one_or_none()
    if existing is not None and existing.evidence_package_id == evidence_package_id:
        existing.assessment_id = assessment_id
        db.flush()
        return _row_to_index(existing)
    if existing is not None:
        existing.is_latest = False
    row = ResultIndex(
        project_id=project_id,
        project_version_id=project_version_id,
        evidence_package_id=evidence_package_id,
        assessment_id=assessment_id,
        is_latest=True,
    )
    db.add(row)
    db.flush()
    return _row_to_index(row)


def latest_index_for_version(db: Session, project_version_id: int) -> ResultIndexRecord | None:
    """取版本最新索引行；无返回 None。"""
    row = db.execute(
        select(ResultIndex).where(
            ResultIndex.project_version_id == project_version_id,
            ResultIndex.is_latest.is_(True),
        )
    ).scalar_one_or_none()
    return _row_to_index(row) if row is not None else None


def get_index(db: Session, index_id: int) -> ResultIndexRecord | None:
    """按主键取结果索引；不存在返回 None。"""
    row = db.get(ResultIndex, index_id)
    return _row_to_index(row) if row is not None else None


def current_selection(db: Session, project_id: int) -> ResultSelectionRecord | None:
    """取项目当前选中；无返回 None。"""
    row = db.execute(
        select(ResultSelection).where(
            ResultSelection.project_id == project_id,
            ResultSelection.is_current.is_(True),
        )
    ).scalar_one_or_none()
    return _row_to_selection(row) if row is not None else None


def select_result(
    db: Session,
    *,
    project_id: int,
    result_index_id: int,
    selected_by: int,
    reason: str | None = None,
) -> ResultSelectionRecord:
    """选中结果（旧 current 翻转后插入新行，同事务完成）。"""
    old = db.execute(
        select(ResultSelection).where(
            ResultSelection.project_id == project_id,
            ResultSelection.is_current.is_(True),
        )
    ).scalar_one_or_none()
    if old is not None:
        old.is_current = False
    row = ResultSelection(
        project_id=project_id,
        result_index_id=result_index_id,
        selected_by=selected_by,
        reason=reason,
        is_current=True,
    )
    db.add(row)
    db.flush()
    return _row_to_selection(row)


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def create_report(
    db: Session,
    *,
    project_id: int,
    report_type: str,
    object_id: int,
    generated_by: int,
    generated_by_task_id: int | None = None,
) -> ReportRecord:
    """创建报告行。"""
    row = Report(
        project_id=project_id,
        report_type=report_type,
        object_id=object_id,
        generated_by=generated_by,
        generated_by_task_id=generated_by_task_id,
    )
    db.add(row)
    db.flush()
    return _row_to_report(row)


def list_reports(db: Session, project_id: int) -> list[ReportRecord]:
    """取项目全部报告（id 升序）。"""
    rows = (
        db.execute(select(Report).where(Report.project_id == project_id).order_by(Report.id)).scalars().all()
    )
    return [_row_to_report(row) for row in rows]


def count_reports(db: Session) -> int:
    """报告总数。"""
    return int(db.execute(select(func.count(Report.id))).scalar() or 0)
