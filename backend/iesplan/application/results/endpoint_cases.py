"""结果面向 API 的薄封装用例(application/results): 视图/评估/选中/逐时/检查。

只做转调与路由原有编排的整体下移, 不新增校验/hash/回退:

- 只读(``result_view``/``assessment_to_dict``/``list_assessments``/
  ``latest_evidence``/``evidence_content``/``selection_diff``)直调
  ``writes`` 同名函数, 不拥有事务;
- 写编排(``assess_task_evidence``/``select_task_result``/
  ``create_check_task``)把路由原来的“多次服务调用 + 一次提交”整体下移为
  单个用例拥有的一次事务(失败回滚), 路由层不再提交;
- 读编排(``get_selection_diff``/``read_task_hourly``)把路由原来的
  “归属/权限/缺失处理”整体下移, 行为与原路由一致。

依赖方向: api → application → 领域门面。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from iesplan.application.results import writes as results_writes
from iesplan.application.tasks.submissions import ensure_project_access, ensure_task_belongs
from iesplan.core.errors import NotFoundError
from iesplan.identity.contracts import UserRecord
from iesplan.results.contracts import ResultAssessmentRecord, ResultSelectionRecord
from iesplan.tasks.contracts import TaskRecord


def result_view(
    db: Session, user: UserRecord, project_id: int, task_id: int
) -> dict[str, Any]:
    """结果视图(只读转调)。"""
    return results_writes.result_view(db, user, project_id, task_id)


def list_assessments(db: Session, task_id: int) -> list[ResultAssessmentRecord]:
    """任务评估历史(只读转调)。"""
    return results_writes.list_assessments(db, task_id)


def assessment_to_dict(db: Session, assessment: ResultAssessmentRecord) -> dict[str, Any]:
    """评估记录序列化(只读转调)。"""
    return results_writes.assessment_to_dict(db, assessment)


def latest_evidence(db: Session, task_id: int):
    """任务最新证据包(只读转调)。"""
    return results_writes.latest_evidence(db, task_id)


def evidence_content(db: Session, package) -> dict[str, Any]:
    """证据包内容(只读转调)。"""
    return results_writes.evidence_content(db, package)


def assess_task_evidence(
    db: Session, user: UserRecord, project_id: int, task_id: int, assessment_type: str = "full",
) -> ResultAssessmentRecord:
    """触发新评估(路由原顺序: 归属 → 最新证据包 → edit 权限 → 评估 → 索引)。

    原路由一次提交覆盖评估 + 索引两步写; 本用例同样一次提交覆盖两步,
    失败整体回滚。
    """
    task = ensure_task_belongs(db, project_id, task_id)
    package = results_writes.latest_evidence(db, task_id)
    if package is None:
        raise NotFoundError(
            "任务尚无证据包, 无法评估",
            params={"task_id": task_id},
            location={"object_type": "evidence_package", "object_id": None},
        )
    ensure_project_access(db, user, project_id, "edit")
    try:
        assessment = results_writes.assess_evidence(
            db, package.id, assessment_type, user=user
        )
        results_writes.refresh_result_index(
            db, task_id, assessment.id, business_outcome=task.business_outcome
        )
        db.commit()
        return assessment
    except Exception:
        db.rollback()
        raise


def select_task_result(
    db: Session,
    user: UserRecord,
    project_id: int,
    task_id: int,
    solution_id: int,
    selection_type: str,
    reference_rule: str | None = None,
    reason: str | None = None,
) -> tuple[ResultSelectionRecord, dict[str, Any] | None]:
    """选择结果(路由原顺序: 归属 → 选中写(提交) → 差异预览)。"""
    ensure_task_belongs(db, project_id, task_id)
    try:
        selection = results_writes.select_result(
            db, user, task_id, solution_id, selection_type,
            reference_rule=reference_rule, reason=reason,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return selection, results_writes.selection_diff(db, project_id)


def get_selection_diff(
    db: Session, user: UserRecord, project_id: int, task_id: int
) -> dict[str, Any]:
    """选中差异预览(路由原顺序: 归属 → view 权限 → 差异; 无选中 → 404)。"""
    ensure_task_belongs(db, project_id, task_id)
    ensure_project_access(db, user, project_id, "view")
    diff = results_writes.selection_diff(db, project_id)
    if diff is None:
        raise NotFoundError(
            "项目尚无当前选中的结果", params={"project_id": project_id},
            location={"object_type": "result_selection", "object_id": None},
        )
    return diff


def read_task_hourly(
    db: Session,
    user: UserRecord,
    project_id: int,
    task_id: int,
    field: str,
    start: int = 0,
    end: int | None = None,
    limit: int = 5000,
    solution_id: int | None = None,
) -> dict[str, Any]:
    """逐时查询(路由原顺序: view 权限 → 归属 → 最新证据包 → 内容 → 分页读)。"""
    ensure_project_access(db, user, project_id, "view")
    ensure_task_belongs(db, project_id, task_id)
    package = results_writes.latest_evidence(db, task_id)
    if package is None:
        raise NotFoundError(
            "任务尚无证据包, 无逐时结果可查",
            params={"task_id": task_id},
            location={"object_type": "evidence_package", "object_id": None},
        )
    content = results_writes.evidence_content(db, package)
    return results_writes.read_hourly(
        db, content, field, start=start, end=end, limit=limit, solution_id=solution_id
    )


def create_check_task(
    db: Session,
    user: UserRecord,
    project_id: int,
    task_id: int,
    evidence_package_id: int | None = None,
) -> TaskRecord:
    """创建检查任务(report 类型; 提交由任务提交用例拥有)。"""
    return results_writes.run_check_task(
        db, user, project_id, task_id, evidence_package_id=evidence_package_id
    )
