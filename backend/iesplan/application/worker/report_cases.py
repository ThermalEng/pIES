"""Worker report 检查分阶段命令(application/worker.report_cases)。

本模块拥有 report/check 的业务规则, Worker 只按运行时序调用、不复制:

- ``locate_report_evidence``: 只读证据定位(显式 id → 任务最新 → 项目最新),
  不提交、不回滚(读完即关由调用方会话负责);
- ``assess_report_stage``: 评估阶段命令。有证据时经
  ``attempt_cases.assess_report_evidence`` 做原子评估写(内含 fencing, 只提交
  本步自己的写入); 无证据时不写库、不提交, 返回显式
  ``no_evidence / insufficient_evidence`` 口径;
- ``ReportCheckResult``: 不可变阶段结果契约(证据 id、有证据时的评估记录与
  outcome、无证据时的 status/outcome/payload 口径, 含可直接上报的 payload)。

Worker(executors.execute_check)只保留"定位 → 进度/检查点 → 评估 → 上报"
运行时序: 调本模块阶段命令、自有 progress/checkpoint、消费显式契约。
长时 attempt 不是一个事务: 本模块不建包住整个 report/check 的单一长事务
handler; 失败/取消后未提交的评估或进度不得被一并提交(各短事务互不包揽)。

依赖方向: worker → application → (results/tasks 领域门面)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from iesplan.application.tasks.submissions import Claim
from iesplan.application.worker import attempt_cases, evidence_cases
from iesplan.results import ResultAssessmentRecord

__all__ = [
    "NO_EVIDENCE_OUTCOME",
    "NO_EVIDENCE_STATUS",
    "REPORT_ASSESSED_STATUS",
    "ReportCheckResult",
    "assess_report_stage",
    "locate_report_evidence",
]

#: report 已评估状态(有证据)。
REPORT_ASSESSED_STATUS = "assessed"
#: report 无证据状态(项目无证据包可检查)。
NO_EVIDENCE_STATUS = "no_evidence"
#: 无证据时的显式业务结局(tasks 域 BUSINESS_OUTCOMES 合法成员)。
NO_EVIDENCE_OUTCOME = "insufficient_evidence"


@dataclass(frozen=True, slots=True)
class ReportCheckResult:
    """report 检查阶段结果契约(不可变; Worker 只消费不复制)。

    有证据时 ``assessment`` 为评估公开记录且 ``outcome`` 经 results 域
    ``check_outcome`` 唯一规则派生; 无证据时 ``assessment`` 为 None 且
    ``status``/``outcome`` 取无证据口径。``payload`` 为可直接上报的
    显式 outcome 载荷(含 assessment DTO 组装)。
    """

    evidence_id: int | None
    status: str
    outcome: str
    payload: dict[str, Any] = field(default_factory=dict)
    assessment: ResultAssessmentRecord | None = None


def locate_report_evidence(
    db: Session,
    *,
    project_id: int,
    evidence_package_id: int | None = None,
    task_id: int | None = None,
) -> int | None:
    """只读定位 report 证据包(优先级: 显式 id → 任务最新 → 项目最新)。

    纯读: 不提交、不写入; 读完即关由调用方(Worker 短会话)负责。
    显式 id 直接采信(缺失与否由评估阶段裁决); 任务最新缺失时回落到项目
    最新; 均无返回 None(调用方走无证据口径)。
    """
    if evidence_package_id is not None:
        return int(evidence_package_id)
    if task_id is not None:
        package_row = evidence_cases.get_latest_evidence_for_task(db, int(task_id))
        if package_row is not None:
            return package_row.id
    package_row = evidence_cases.get_latest_evidence_for_project(db, project_id)
    return package_row.id if package_row is not None else None


def assess_report_stage(
    db: Session, claim: Claim, *, evidence_id: int | None
) -> ReportCheckResult:
    """report 评估阶段命令(定位之后、最终上报之前)。

    - ``evidence_id`` 为 None: 无任何写库与提交, 返回无证据口径契约;
    - 有证据: 经 ``assess_report_evidence`` 原子评估写(内含 fencing 校验,
      只提交本步自己的写入; 租约失效整笔回滚并抛 LeaseRejectedError),
      再组装显式 outcome 载荷返回。

    本函数不包揽定位读与最终上报, 更不建包住整个 report/check 的长事务。
    """
    if evidence_id is None:
        return ReportCheckResult(
            evidence_id=None,
            status=NO_EVIDENCE_STATUS,
            outcome=NO_EVIDENCE_OUTCOME,
            assessment=None,
            payload={
                "schema_version": 1,
                "result_kind": "assessment_report",
                "task_type": "report",
                "status": NO_EVIDENCE_STATUS,
                "evidence_package_id": None,
                "assessment": {},
                "outcome": NO_EVIDENCE_OUTCOME,
                "summary": {"assessed": False, "reason": "项目无证据包可检查"},
            },
        )
    result = attempt_cases.assess_report_evidence(
        db, claim, evidence_package_id=evidence_id
    )
    assessment = result.assessment
    return ReportCheckResult(
        evidence_id=evidence_id,
        status=REPORT_ASSESSED_STATUS,
        outcome=result.outcome,
        assessment=assessment,
        payload={
            "schema_version": 1,
            "result_kind": "assessment_report",
            "task_type": "report",
            "status": REPORT_ASSESSED_STATUS,
            "evidence_package_id": evidence_id,
            "assessment": {
                "dimension_physical": assessment.dimension_physical,
                "dimension_optimality": assessment.dimension_optimality,
                "dimension_financial": assessment.dimension_financial,
                "dimension_reliability": assessment.dimension_reliability,
                "overall_score": assessment.overall_score,
                "comment": assessment.comment,
                "detail": assessment.detail,
            },
            "outcome": result.outcome,
            "summary": {
                "assessed": True,
                "evidence_package_id": evidence_id,
                "assessment_id": assessment.id,
            },
        },
    )
