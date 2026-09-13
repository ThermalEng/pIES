"""结果域公开门面（证据/评估/索引/选中/报告表，归属 results）。

外部只允许经本门面消费 contract、领域规则与 repository 实现函数；
不得导入 `iesplan.models`、services 或其他域的内部模块。
"""

from __future__ import annotations

from iesplan.results import persistence, rules
from iesplan.results.contracts import (
    EvidenceInvalidError,
    EvidencePackageRecord,
    EvidenceWriteDeniedError,
    ReportRecord,
    ResultAssessmentRecord,
    ResultConflictError,
    ResultIndexRecord,
    ResultInvalidRequestError,
    ResultNotFoundError,
    ResultSelectionRecord,
)

count_reports = persistence.count_reports
create_evidence = persistence.create_evidence
create_human_assessment = persistence.create_human_assessment
create_report = persistence.create_report
create_system_assessment = persistence.create_system_assessment
current_selection = persistence.current_selection
evidence_statuses_for_tasks = persistence.evidence_statuses_for_tasks
flip_index_for_version = persistence.flip_index_for_version
get_assessment = persistence.get_assessment
get_evidence = persistence.get_evidence
get_index = persistence.get_index
insert_index = persistence.insert_index
latest_assessment = persistence.latest_assessment
latest_evidence_for_task = persistence.latest_evidence_for_task
latest_index_for_version = persistence.latest_index_for_version
list_assessments = persistence.list_assessments
list_assessments_for_task = persistence.list_assessments_for_task
list_evidence_for_tasks = persistence.list_evidence_for_tasks
list_package_assessments = persistence.list_package_assessments
list_package_index = persistence.list_package_index
list_reports = persistence.list_reports
point_index_assessment = persistence.point_index_assessment
point_index_latest = persistence.point_index_latest
select_result = persistence.select_result

ASSESSMENT_RULE_VERSION = rules.ASSESSMENT_RULE_VERSION
ASSESSMENT_TYPES = rules.ASSESSMENT_TYPES
DEFAULT_GAP_THRESHOLD_PCT = rules.DEFAULT_GAP_THRESHOLD_PCT
DEFAULT_MIN_VALID_SAMPLES = rules.DEFAULT_MIN_VALID_SAMPLES
ENGINE_STATUS_TO_OPTIMALITY = rules.ENGINE_STATUS_TO_OPTIMALITY
EVIDENCE_COMPLETE = rules.EVIDENCE_COMPLETE
EVIDENCE_INVALID = rules.EVIDENCE_INVALID
EVIDENCE_PARTIAL = rules.EVIDENCE_PARTIAL
EVIDENCE_SCHEMA_VERSION = rules.EVIDENCE_SCHEMA_VERSION
OPTIMALITY_BY_SOLVER = rules.OPTIMALITY_BY_SOLVER
REQUIRED_EVIDENCE_KEYS = rules.REQUIRED_EVIDENCE_KEYS
SELECTION_TYPES = rules.SELECTION_TYPES
SystemAssessmentDraft = rules.SystemAssessmentDraft
check_financial = rules.check_financial
check_optimality = rules.check_optimality
check_outcome = rules.check_outcome
check_physical = rules.check_physical
check_reliability = rules.check_reliability
coerce_fine = rules.coerce_fine
evaluate_evidence = rules.evaluate_evidence
evidence_inner = rules.evidence_inner
fine_states = rules.fine_states
fine_to_db = rules.fine_to_db
overall_score = rules.overall_score
summarize_assessment = rules.summarize_assessment
validate_evidence_structure = rules.validate_evidence_structure

__all__ = [
    "ASSESSMENT_RULE_VERSION",
    "ASSESSMENT_TYPES",
    "DEFAULT_GAP_THRESHOLD_PCT",
    "DEFAULT_MIN_VALID_SAMPLES",
    "ENGINE_STATUS_TO_OPTIMALITY",
    "EVIDENCE_COMPLETE",
    "EVIDENCE_INVALID",
    "EVIDENCE_PARTIAL",
    "EVIDENCE_SCHEMA_VERSION",
    "OPTIMALITY_BY_SOLVER",
    "REQUIRED_EVIDENCE_KEYS",
    "SELECTION_TYPES",
    "SystemAssessmentDraft",
    "EvidenceInvalidError",
    "EvidencePackageRecord",
    "EvidenceWriteDeniedError",
    "ReportRecord",
    "ResultAssessmentRecord",
    "ResultConflictError",
    "ResultIndexRecord",
    "ResultInvalidRequestError",
    "ResultNotFoundError",
    "ResultSelectionRecord",
    "check_financial",
    "check_optimality",
    "check_outcome",
    "check_physical",
    "check_reliability",
    "coerce_fine",
    "count_reports",
    "create_evidence",
    "create_human_assessment",
    "create_report",
    "create_system_assessment",
    "current_selection",
    "evaluate_evidence",
    "evidence_inner",
    "evidence_statuses_for_tasks",
    "fine_states",
    "fine_to_db",
    "flip_index_for_version",
    "get_assessment",
    "get_evidence",
    "get_index",
    "insert_index",
    "latest_assessment",
    "latest_evidence_for_task",
    "latest_index_for_version",
    "list_assessments",
    "list_assessments_for_task",
    "list_evidence_for_tasks",
    "list_package_assessments",
    "list_package_index",
    "list_reports",
    "overall_score",
    "point_index_assessment",
    "point_index_latest",
    "select_result",
    "summarize_assessment",
    "validate_evidence_structure",
]
