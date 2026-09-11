"""项目包域 repository 协议（import_proposals）。

实现规则（切片 5 落实）：
- 只做查询、写入、flush；绝不 commit/rollback；
- 导出是只读组合（经其他域公开 repository），不在本协议；
- 确认导入的实际建表写操作归 application 用例编排（切片 6），
  本协议只维护提议状态机。
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.orm import Session

from iesplan.package.contracts import ImportProposalRecord


class PackageRepository(Protocol):
    """项目包聚合 repository 协议（无状态方法组，db 由调用方事务拥有）。"""

    def create_proposal(
        self,
        db: Session,
        *,
        project_id: int,
        proposer_id: int,
        source_type: str,
        source_object_id: int | None = None,
        source_path: str | None = None,
    ) -> ImportProposalRecord: ...

    def get_proposal(self, db: Session, proposal_id: int) -> ImportProposalRecord | None: ...

    def list_proposals(self, db: Session, project_id: int) -> list[ImportProposalRecord]: ...

    def set_proposal_review(
        self,
        db: Session,
        proposal_id: int,
        *,
        status: str,
        review_summary: dict[str, Any] | None = None,
        review_errors: dict[str, Any] | None = None,
        decided_by: int | None = None,
    ) -> ImportProposalRecord:
        """推进评审状态机；非法转换抛 PackageConflictError。"""
        ...
