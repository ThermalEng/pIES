"""结果面向 API 的薄封装用例(application/results): 视图/评估/选中/逐时/检查。

只做转调与路由原有编排的整体下移, 不新增校验/hash/回退:

- 只读(``result_view``/``assessment_to_dict``/``list_assessments``/
  ``latest_evidence``/``evidence_content``/``selection_diff``)直调
  ``writes`` 同名函数, 不拥有事务;
- 写编排(``assess_task_evidence``/``select_task_result``/
  ``create_check_task_case``)把路由原来的“多次服务调用 + 一次提交”整体下移为
  单个用例拥有的一次事务(失败回滚), 路由层不再提交;
- 读编排(``get_selection_diff``/``read_task_hourly``)把路由原来的
  “归属/权限/缺失处理”整体下移, 行为与原路由一致;
- 端点完整用例(``list_assessments_case``/``create_check_task_case``)把路由
  原来的“业务调用 + 序列化/跨包摘要”收进单个 handler, 路由层每个动作只
  转交其中一个(``assess`` 端点的纯序列化与 ``select`` 端点的内联选择字典
  经复核为纯映射, 保留在 API)。

依赖方向: api → application → 领域门面。application 内跨域组合允许:
``create_check_task_case`` 复用 ``application.tasks`` 的任务摘要, 不在
API 跨包二次调用。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from iesplan import tasks as tasks_domain
from iesplan.application.projects import ensure_access
from iesplan.application.results import writes as results_writes
from iesplan.application.tasks import ensure_task_belongs, task_summary
from iesplan.core.errors import ConflictError, NotFoundError
from iesplan.identity.contracts import UserRecord
from iesplan.results.contracts import ResultAssessmentRecord, ResultSelectionRecord
from iesplan.storage import get_object
from iesplan.tasks.contracts import (
    IDEMPOTENCY_KEY_RE,
    POOL_BY_TYPE,
    InvalidRequestError,
    TaskConflictError,
)


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
    ensure_access(db, user, project_id, "edit")
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
    ensure_access(db, user, project_id, "view")
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
    ensure_access(db, user, project_id, "view")
    ensure_task_belongs(db, project_id, task_id)
    package = results_writes.latest_evidence(db, task_id)
    if package is None:
        raise NotFoundError(
            "任务尚无证据包, 无逐时结果可查",
            params={"task_id": task_id},
            location={"object_type": "evidence_package", "object_id": None},
        )
    content = results_writes.evidence_content(db, package)
    prepared = results_writes.prepare_hourly_read(db, content, field, solution_id)
    # 本用例只有读操作；身份活跃时间已由认证用例单独提交。
    # 内容源独立于 Session，大对象扫描不占用请求的数据库连接。
    db.rollback()
    return results_writes.read_hourly(prepared, start=start, end=end, limit=limit)


def create_check_task_case(
    db: Session,
    user: UserRecord,
    project_id: int,
    task_id: int,
    evidence_package_id: int | None = None,
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    """创建检查任务完整用例(路由原顺序: 创建检查任务 → 跨包任务摘要)。

    幂等键为必填 keyword-only 参数(调用方提供, 宪法 §5.5); 空值由任务提交
    权威用例按 TASK-REQ-003 拒绝。同键重试返回同一逻辑任务。
    application 内跨域组合: 复用 ``application.tasks`` 的任务摘要实现,
    API 层不再跨包二次调用。提交由任务提交用例拥有。返回响应就绪字典。
    """
    task, flags = results_writes.run_check_task(
        db, user, project_id, task_id,
        evidence_package_id=evidence_package_id, idempotency_key=idempotency_key,
    )
    return {"task": task_summary(db, task), "replayed": bool(flags.get("replay", False))}


def list_assessments_case(
    db: Session, user: UserRecord, project_id: int, task_id: int
) -> dict[str, Any]:
    """评估历史完整用例(路由原顺序: view 权限 + 归属 → 列表 → 逐项序列化)。"""
    ensure_access(db, user, project_id, "view")
    ensure_task_belongs(db, project_id, task_id)
    items = [
        results_writes.assessment_to_dict(db, a)
        for a in results_writes.list_assessments(db, task_id)
    ]
    return {"items": items, "total": len(items)}


def retry_evaluator_case(
    db: Session,
    user: UserRecord,
    project_id: int,
    task_id: int,
    *,
    evaluator: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """单项评价器重试用例：校验失败评价与密封工程解，幂等重排现有任务执行单项评价。"""
    from iesplan.application.worker import (
        list_evaluator_retry_requests,
        save_evaluator_retry_request,
    )

    ensure_access(db, user, project_id, "edit")
    task = ensure_task_belongs(db, project_id, task_id)
    if not idempotency_key or not re.fullmatch(IDEMPOTENCY_KEY_RE, idempotency_key):
        raise InvalidRequestError(
            "单项评价器重试必须提供合法幂等键",
            code="TASK-REQ-003",
            params={"idempotency_key": idempotency_key},
        )
    existing_reqs = list_evaluator_retry_requests(db, task.id)
    for req in existing_reqs:
        if req.get("idempotency_key") == idempotency_key:
            if req.get("evaluator") != evaluator:
                raise ConflictError(
                    "同一幂等键已绑定其他评价器重试请求",
                    params={"idempotency_key": idempotency_key, "evaluator": evaluator},
                )
            return {
                "task": task_summary(db, task),
                "evaluator": evaluator,
                "replayed": True,
            }

    if task.status in ("queued", "running", "cancelling"):
        raise TaskConflictError(
            "任务正在排队或执行中，暂不可发起单项评价器重试",
            params={"task_id": task_id, "status": task.status},
        )
    package = results_writes.latest_evidence(db, task_id)
    if package is None:
        raise NotFoundError("任务尚无证据包，无法发起单项评价器重试", params={"task_id": task_id})

    snapshot = tasks_domain.get_snapshot(db, task.calc_snapshot_id) if task.calc_snapshot_id else None
    if snapshot is None:
        raise InvalidRequestError("任务缺少冻结计算快照，无法重试评价器", params={"task_id": task_id})
    configured_evals = {
        str(ev["evaluator"])
        for ev in (snapshot.calc_config_snapshot.get("evaluators") or ())
        if isinstance(ev, Mapping) and "evaluator" in ev
    }
    if evaluator not in configured_evals:
        raise InvalidRequestError(
            "指定评价器不在任务冻结配置中",
            params={"task_id": task_id, "evaluator": evaluator},
        )

    evidence_payload = json.loads(get_object(db, package.object_id))
    eng_group = evidence_payload.get("engineering_solution") or {}
    if not isinstance(eng_group, Mapping) or "solution.json" not in eng_group:
        raise InvalidRequestError(
            "当前证据包缺少可复用的密封工程解，无法执行单项评价器重试",
            params={"task_id": task_id, "evidence_package_id": package.id},
        )
    eval_entries = evidence_payload.get("evaluator_results") or []
    matching_entry = next(
        (e for e in eval_entries if isinstance(e, dict) and e.get("evaluator") == evaluator),
        None,
    )
    if matching_entry is None or matching_entry.get("status") not in ("failed", "timed_out", "cancelled"):
        raise TaskConflictError(
            "仅失败、超时或已取消的评价器支持单项重试",
            params={
                "task_id": task_id,
                "evaluator": evaluator,
                "status": matching_entry.get("status") if matching_entry else None,
            },
        )

    attempts = tasks_domain.list_attempts(db, task.id)
    if not attempts:
        raise TaskConflictError("任务无历史执行尝试，无法绑定单项重试", params={"task_id": task_id})
    try:
        save_evaluator_retry_request(
            db,
            attempt_id=attempts[-1].id,
            evaluator=evaluator,
            base_evidence_id=package.id,
            idempotency_key=idempotency_key,
            min_attempt_no=task.attempt_count + 1,
        )
        updated_task = tasks_domain.set_task_status(
            db, task.id, "queued", business_outcome=task.business_outcome
        )
        tasks_domain.clear_cancel(task.id)
        tasks_domain.append_diagnostic(
            db,
            task_id=task.id,
            level="info",
            code="TASK_QUEUED",
            message=f"单项评价器重试已排队: {evaluator}",
            context={"evaluator": evaluator, "base_evidence_id": package.id},
        )
        pool = POOL_BY_TYPE[task.type]
        tasks_domain.enqueue(
            task.id,
            pool,
            task_type=task.type,
            snapshot_id=task.calc_snapshot_id,
            priority=task.priority,
        )
        summary = task_summary(db, updated_task)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"task": summary, "evaluator": evaluator, "replayed": False}
