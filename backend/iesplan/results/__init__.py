"""结果域公开门面（证据/评估/索引/选中/报告表，归属 results）。

外部只允许经本门面消费 contract、repository 协议与 repository 实现函数；
不得导入 `iesplan.models`、services 或其他域的内部模块。
"""

from __future__ import annotations

from iesplan.results import persistence
from iesplan.results.contracts import (
    EvidencePackageRecord,
    ReportRecord,
    ResultAssessmentRecord,
    ResultConflictError,
    ResultIndexRecord,
    ResultNotFoundError,
    ResultSelectionRecord,
)
from iesplan.results.repository import ResultsRepository

count_reports = persistence.count_reports
create_evidence = persistence.create_evidence
create_human_assessment = persistence.create_human_assessment
create_report = persistence.create_report
create_system_assessment = persistence.create_system_assessment
current_selection = persistence.current_selection
evidence_statuses_for_tasks = persistence.evidence_statuses_for_tasks
get_assessment = persistence.get_assessment
get_evidence = persistence.get_evidence
get_index = persistence.get_index
latest_assessment = persistence.latest_assessment
latest_evidence_for_task = persistence.latest_evidence_for_task
latest_index_for_version = persistence.latest_index_for_version
list_assessments = persistence.list_assessments
list_assessments_for_task = persistence.list_assessments_for_task
list_evidence_for_tasks = persistence.list_evidence_for_tasks
list_package_assessments = persistence.list_package_assessments
list_package_index = persistence.list_package_index
list_reports = persistence.list_reports
point_index_latest = persistence.point_index_latest
select_result = persistence.select_result

__all__ = [
    "EvidencePackageRecord",
    "ReportRecord",
    "ResultAssessmentRecord",
    "ResultConflictError",
    "ResultIndexRecord",
    "ResultNotFoundError",
    "ResultSelectionRecord",
    "ResultsRepository",
    "count_reports",
    "create_evidence",
    "create_human_assessment",
    "create_report",
    "create_system_assessment",
    "current_selection",
    "evidence_statuses_for_tasks",
    "get_assessment",
    "get_evidence",
    "get_index",
    "latest_assessment",
    "latest_evidence_for_task",
    "latest_index_for_version",
    "list_assessments",
    "list_assessments_for_task",
    "list_evidence_for_tasks",
    "list_package_assessments",
    "list_package_index",
    "list_reports",
    "point_index_latest",
    "select_result",
]
