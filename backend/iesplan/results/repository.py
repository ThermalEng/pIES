"""结果域 repository 协议（evidence_packages/result_assessments/result_index/result_selections/reports）。

实现规则（切片 5 落实）：
- 证据/评估只 INSERT；索引 latest 切换与选中 current 切换用 savepoint；
  绝不 commit/rollback；
- 系统评估与人工评估走不同方法，禁止互相转换（宪法分析命令分离）。
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.orm import Session

from iesplan.results.contracts import (
    EvidencePackageRecord,
    ReportRecord,
    ResultAssessmentRecord,
    ResultIndexRecord,
    ResultSelectionRecord,
)


class ResultsRepository(Protocol):
    """结果聚合 repository 协议（无状态方法组，db 由调用方事务拥有）。"""

    def create_evidence(
        self,
        db: Session,
        *,
        task_id: int,
        calc_snapshot_id: int,
        object_id: int,
        status: str,
        attempt_id: int | None = None,
        created_by: int = 0,
    ) -> EvidencePackageRecord: ...

    def get_evidence(self, db: Session, package_id: int) -> EvidencePackageRecord | None: ...

    def latest_evidence_for_task(self, db: Session, task_id: int) -> EvidencePackageRecord | None: ...

    def create_system_assessment(
        self,
        db: Session,
        *,
        evidence_package_id: int,
        dimensions: dict[str, str],
        overall_score: float | None = None,
        detail: dict[str, Any] | None = None,
    ) -> ResultAssessmentRecord:
        """写入系统评估（assessor='system'）。"""
        ...

    def create_human_assessment(
        self,
        db: Session,
        *,
        evidence_package_id: int,
        assessed_by: int,
        dimensions: dict[str, str],
        overall_score: float | None = None,
        comment: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> ResultAssessmentRecord:
        """写入人工评估（assessor='human'，必须带 assessed_by）。"""
        ...

    def latest_assessment(self, db: Session, evidence_package_id: int) -> ResultAssessmentRecord | None: ...

    def list_assessments(self, db: Session, evidence_package_id: int) -> list[ResultAssessmentRecord]: ...

    def point_index_latest(
        self,
        db: Session,
        *,
        project_id: int,
        project_version_id: int,
        evidence_package_id: int,
        assessment_id: int | None = None,
    ) -> ResultIndexRecord:
        """将版本索引指向新证据（旧 latest 翻转为非 latest，savepoint 内完成）。"""
        ...

    def latest_index_for_version(self, db: Session, project_version_id: int) -> ResultIndexRecord | None: ...

    def current_selection(self, db: Session, project_id: int) -> ResultSelectionRecord | None: ...

    def select_result(
        self, db: Session, *, project_id: int, result_index_id: int, selected_by: int
    ) -> ResultSelectionRecord:
        """选中结果（旧 current 翻转，savepoint 内完成）。"""
        ...

    def create_report(
        self,
        db: Session,
        *,
        project_id: int,
        report_type: str,
        object_id: int,
        generated_by: int,
        generated_by_task_id: int | None = None,
    ) -> ReportRecord: ...

    def list_reports(self, db: Session, project_id: int) -> list[ReportRecord]: ...
