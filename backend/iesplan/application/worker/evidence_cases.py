"""Worker 证据/评估/不确定性行写用例(application/worker.evidence_cases)。

本模块收拢 Worker 对 ORM 行的直接访问, 只做转调与行级读写搬运,
不新增校验/hash/回退:

- 证据包查询 → results 域门面(任务最新/项目最新/主键);
- 检查单步评估 → ``application.results`` 公开能力(评估追加) +
  results 域 outcome 规则(显式结局), 只 flush 不提交;
- 不确定性快照/样本取值/样本行创建(含执行态 status 直写) → tasks 域门面。

本模块不导入 ``models.*``。各函数只收不可变输入、返回显式结果契约,
不接受 Worker 的进度/取消回调(运行编排归 Worker 所有)。

依赖方向: worker → application → (results/tasks 领域门面)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from iesplan import results as results_domain
from iesplan import tasks as tasks_domain
from iesplan.application.results import writes as results_writes
from iesplan.application.worker.lease_cases import point_result_assessment
from iesplan.results import EvidencePackageRecord, ResultAssessmentRecord
from iesplan.tasks import SampleTaskRecord, UncertaintySnapshotRecord

__all__ = [
    "CheckAssessment",
    "assess_check_evidence",
    "create_sample_row",
    "create_uncertainty_snapshot_record",
    "get_evidence_record",
    "get_latest_evidence_for_project",
    "get_latest_evidence_for_task",
    "point_result_assessment",
    "record_sample_value",
]


@dataclass(frozen=True, slots=True)
class CheckAssessment:
    """report 检查单步评估结果契约: 评估公开视图 + 显式业务结局。

    ``outcome`` 经 results 域 ``check_outcome`` 唯一规则派生, 为
    tasks 域 ``BUSINESS_OUTCOMES`` 合法成员; Worker 只消费不复制。
    """

    assessment: ResultAssessmentRecord
    outcome: str


def assess_check_evidence(db: Session, *, evidence_package_id: int) -> CheckAssessment:
    """执行一次检查评估: 公开能力评估追加 + 索引指针挂接 + 显式 outcome。

    只做本步行写(flush, 不提交); 进度/取消与跨步顺序由 Worker 编排。
    """
    assessment = results_writes.assess_evidence(db, evidence_package_id)
    point_result_assessment(
        db, evidence_package_id=evidence_package_id, assessment_id=assessment.id
    )
    outcome = results_domain.check_outcome(
        {
            "physical": assessment.dimension_physical,
            "optimality": assessment.dimension_optimality,
            "financial": assessment.dimension_financial,
            "reliability": assessment.dimension_reliability,
        }
    )
    return CheckAssessment(assessment=assessment, outcome=outcome)


def get_latest_evidence_for_task(db: Session, task_id: int) -> EvidencePackageRecord | None:
    """取任务最新证据包(id 倒序首个); 无返回 None。"""
    return results_domain.latest_evidence_for_task(db, task_id)


def get_latest_evidence_for_project(db: Session, project_id: int) -> EvidencePackageRecord | None:
    """取项目最新证据包(项目任务 id 升序枚举后取 id 最大者); 无返回 None。"""
    task_ids = tasks_domain.list_task_ids(db, project_id)
    if not task_ids:
        return None
    packages = results_domain.list_evidence_for_tasks(db, task_ids)
    if not packages:
        return None
    return max(packages, key=lambda package: package.id)


def get_evidence_record(db: Session, evidence_package_id: int) -> EvidencePackageRecord | None:
    """按主键取证据包公开视图; 不存在返回 None。"""
    return results_domain.get_evidence(db, evidence_package_id)


def create_uncertainty_snapshot_record(
    db: Session,
    *,
    calc_snapshot_id: int,
    method: str,
    n_samples: int,
    random_seed: int,
    distributions: dict[str, Any],
    created_by: int,
) -> UncertaintySnapshotRecord:
    """创建不确定性快照(不可变; 只 flush 不提交)。"""
    return tasks_domain.create_uncertainty_snapshot(
        db, calc_snapshot_id=calc_snapshot_id, method=method, n_samples=n_samples,
        random_seed=random_seed, distributions=distributions, created_by=created_by,
    )


def create_sample_row(
    db: Session,
    *,
    uncertainty_snapshot_id: int,
    parent_task_id: int,
    sample_index: int,
    status: str,
    params: dict[str, Any] | None,
) -> SampleTaskRecord:
    """创建样本行(执行态 status 直写; 返回公开视图, 只 flush 不提交)。"""
    return tasks_domain.create_sample_row(
        db,
        uncertainty_snapshot_id=uncertainty_snapshot_id,
        parent_task_id=parent_task_id,
        sample_index=sample_index,
        status=status,
        params=params,
    )


def record_sample_value(
    db: Session,
    *,
    sample_task_id: int,
    variable_name: str,
    value: float,
    unit: str | None,
) -> None:
    """记录样本取值(不可变, 只 INSERT; 只 flush 不提交)。"""
    tasks_domain.record_sample(
        db, sample_task_id=sample_task_id, variable_name=variable_name,
        value=value, unit=unit,
    )
