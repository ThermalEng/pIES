"""项目包域 repository 实现（import_proposals 表，归属 package）。

实现规则：
- 只做查询、写入、flush；绝不 commit/rollback；
- 状态机：proposed/validated/approved → applied/rejected；终态（applied/
  rejected）不可再迁移；未知状态抛 PackageConflictError；
- 返回 contracts 不可变记录；时间以 ISO 字符串表达。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from iesplan.models.audit import ImportProposal
from iesplan.package.contracts import (
    ImportProposalRecord,
    PackageConflictError,
    PackageNotFoundError,
)

#: 提案终态（不可再迁移）。
TERMINAL_PROPOSAL_STATUSES: frozenset[str] = frozenset({"applied", "rejected"})

#: 提案合法状态集合。
KNOWN_PROPOSAL_STATUSES: frozenset[str] = frozenset(
    {"proposed", "validated", "approved", "applied", "rejected"}
)


def _iso(value: datetime | None) -> str | None:
    """ORM 时间 → 记录字符串（原样 isoformat，不增减时区后缀）。"""
    return value.isoformat() if value is not None else None


def _row_to_proposal(row: ImportProposal) -> ImportProposalRecord:
    """ORM 行 → 公开记录（审计原文 JSON 直通，不做内容重算）。"""
    return ImportProposalRecord(
        id=row.id,
        project_id=row.project_id,
        proposer_id=row.proposer_id,
        source_type=row.source_type,
        status=row.status,
        source_object_id=row.source_object_id,
        source_path=row.source_path,
        review_summary=row.review_summary,
        review_errors=row.review_errors,
        decided_by=row.decided_by,
        decided_at=_iso(row.decided_at),
        created_at=_iso(row.created_at),
    )


def create_proposal(
    db: Session,
    *,
    project_id: int,
    proposer_id: int,
    source_type: str,
    source_object_id: int | None = None,
    source_path: str | None = None,
) -> ImportProposalRecord:
    """创建导入提案（初始状态 proposed）；落库失败抛领域错误。"""
    row = ImportProposal(
        project_id=project_id,
        proposer_id=proposer_id,
        source_type=source_type,
        source_object_id=source_object_id,
        source_path=source_path,
        status="proposed",
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise PackageConflictError("导入提案创建冲突", params={"project_id": project_id}) from exc
    return _row_to_proposal(row)


def get_proposal(db: Session, proposal_id: int) -> ImportProposalRecord | None:
    """按 id 取导入提案；不存在返回 None。"""
    row = db.get(ImportProposal, proposal_id)
    return _row_to_proposal(row) if row is not None else None


def list_proposals(db: Session, project_id: int) -> list[ImportProposalRecord]:
    """列出项目的导入提案（id 升序）。"""
    rows = (
        db.execute(
            select(ImportProposal).where(ImportProposal.project_id == project_id).order_by(ImportProposal.id)
        )
        .scalars()
        .all()
    )
    return [_row_to_proposal(row) for row in rows]


def list_proposals_for_proposer(db: Session, proposer_id: int) -> list[ImportProposalRecord]:
    """列出提议人的导入提案（id 倒序，供幂等键复用检查）。"""
    rows = (
        db.execute(
            select(ImportProposal)
            .where(ImportProposal.proposer_id == proposer_id)
            .order_by(ImportProposal.id.desc())
        )
        .scalars()
        .all()
    )
    return [_row_to_proposal(row) for row in rows]


def set_proposal_review(
    db: Session,
    proposal_id: int,
    *,
    status: str,
    review_summary: dict[str, Any] | None = None,
    review_errors: dict[str, Any] | None = None,
    decided_by: int | None = None,
) -> ImportProposalRecord:
    """推进评审状态机；非法转换抛 PackageConflictError，缺失抛 PackageNotFoundError。

    同状态复写评审内容允许（如创建后补写校验报告）；终态不可再迁移；
    标记 applied 时记录 decided_by 与 decided_at。
    """
    if status not in KNOWN_PROPOSAL_STATUSES:
        raise PackageConflictError("导入提案状态非法", params={"proposal_id": proposal_id, "status": status})
    row = db.get(ImportProposal, proposal_id)
    if row is None:
        raise PackageNotFoundError("导入提案不存在", params={"proposal_id": proposal_id})
    if row.status in TERMINAL_PROPOSAL_STATUSES and row.status != status:
        raise PackageConflictError(
            "导入提案已终态，不可再迁移",
            params={"proposal_id": proposal_id, "status": row.status},
        )
    row.status = status
    if review_summary is not None:
        row.review_summary = review_summary
    if review_errors is not None:
        row.review_errors = review_errors
    if status == "applied":
        row.decided_by = decided_by
        row.decided_at = datetime.now(UTC)
    db.flush()
    return _row_to_proposal(row)
