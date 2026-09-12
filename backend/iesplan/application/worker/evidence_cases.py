"""Worker 证据/评估/不确定性行写用例(application/worker.evidence_cases)。

本模块收拢 ``iesplan.worker.executors`` 原先对 ORM 行的直接访问, 只做
转调与行级读写搬运, 不新增校验/hash/回退:

- 证据包查询 → results 域门面(任务最新/项目最新/主键);
- 结果检查评估追加 + 索引评估指针 → results 域门面;
- 不确定性快照/样本取值/样本行创建(含执行态 status 直写) → tasks 域门面。

本模块不导入 ``models.*``。

依赖方向: worker → application → (results/tasks 领域门面)。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from iesplan import results as results_domain
from iesplan import tasks as tasks_domain
from iesplan.application.worker.lease_cases import point_result_assessment
from iesplan.results import EvidencePackageRecord
from iesplan.tasks import SampleTaskRecord, UncertaintySnapshotRecord

__all__ = [
    "append_check_assessment",
    "create_sample_row",
    "create_uncertainty_snapshot_record",
    "get_evidence_record",
    "get_latest_evidence_for_project",
    "get_latest_evidence_for_task",
    "point_result_assessment",
    "record_sample_value",
]


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


def append_check_assessment(
    db: Session, *, evidence_package_id: int, assessment: dict[str, Any]
) -> int:
    """结果检查评估追加(assessor='system')并挂接索引评估指针; 返回评估 id。

    四维键由调用方保证完备(与搬入前 ``executors.execute_check`` 同口径)。
    """
    record = results_domain.create_system_assessment(
        db,
        evidence_package_id=evidence_package_id,
        dimensions={
            "physical": assessment["dimension_physical"],
            "optimality": assessment["dimension_optimality"],
            "financial": assessment["dimension_financial"],
            "reliability": assessment["dimension_reliability"],
        },
        overall_score=assessment["overall_score"],
        detail=assessment["detail"],
        comment=assessment["comment"],
    )
    point_result_assessment(
        db, evidence_package_id=evidence_package_id, assessment_id=record.id
    )
    return record.id


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
