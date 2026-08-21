"""租约与 fencing token 协议(03-task-scheduling.md §4)。

权威事实(尝试/租约/fencing token/槽)全部落 PostgreSQL, Redis 队列/进度/心跳
为可重建视图。本模块是 Worker 端的唯一租约入口:

- acquire_attempt: 领取任务 = 占槽 + 建尝试 + 建租约(发 UUID token) + 任务
  running, 复用 U07 服务(tasks_service.claim_and_run)同事务完成(03 §4.1 ①);
- renew_lease: 续租(15 s 周期); 影响行数 = 0 → 租约失效 → 调用方必须立即
  自毁(终止子进程、停止一切写回, 03 §4.4);
- report_progress: 带 fencing 的进度回写(PG UPSERT + Redis 秒级进度);
- submit_result: 提交结果(证据包 + 四维评估 + 结果索引 + 任务完成 + 释放),
  全程以 token + active 租约为前提, 0 行即整笔回滚 —— 迟到结果永远不入权威库;
- fail_attempt / cancel_attempt: 失败/取消收拢(同样带 fencing);
- slot_available: 槽门禁查询(领取前确认 compute_slots 有空位, 03 §5.2)。

一致性与 03 §1.3 对齐: PG 是权威; 一任务一租约一 token; 写回必带 token;
终态即封闭(状态迁移校验复用 tasks_service)。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from iesplan.core.diagnostics import SEVERITY_ERROR, SEVERITY_INFO, SEVERITY_WARNING, TASK_QUEUED
from iesplan.core.errors import AppError
from iesplan.core.idgen import sha256_hex
from iesplan.models.calc import CalcSnapshot, ComputeSlot, Task, TaskAttempt, TaskDiagnostic, TaskLease
from iesplan.models.result import EvidencePackage, ResultAssessment, ResultIndex
from iesplan.services import queue
from iesplan.services import tasks as tasks_service
from iesplan.storage import add_ref, put_object

logger = logging.getLogger(__name__)

#: 租约 TTL(秒, 03 §4.2 默认 60 s; 与 tasks_service.LEASE_TTL_SECONDS 一致)
LEASE_TTL_SECONDS = tasks_service.LEASE_TTL_SECONDS


class LeaseRejectedError(AppError):
    """租约失效/过期后的迟到写回(03 §6.3 建议登记 TASK-LEASE-001, warning 不阻断)。

    Worker 收到本异常必须立即: 终止子进程 → 停止一切 PG/对象存储写入。
    """

    code = "TASK-LEASE-001"
    severity = SEVERITY_WARNING
    message_key = "ies.diag.task.lease_rejected"


class SlotUnavailableError(AppError):
    """无空闲并发槽(03 §5.2: 无空槽时任务保持 queued, 等待下一轮调度)。"""

    code = "TASK-QUEUE-001"
    severity = SEVERITY_ERROR
    message_key = "ies.diag.task.queue_failed"


# 与 tasks_service.Claim 同构(避免跨模块重复定义, 直接复用其类型)
Claim = tasks_service.Claim


@dataclass(frozen=True, slots=True)
class SubmitReceipt:
    """提交成功回执(证据包/评估/结果索引 id, 供诊断与测试断言)。"""

    evidence_package_id: int | None
    assessment_id: int | None
    result_index_id: int | None
    outcome: str


# ---------------------------------------------------------------------------
# 领取 / 槽门禁
# ---------------------------------------------------------------------------


def acquire_attempt(db: Session, task_id: int, worker_id: str) -> Claim | None:
    """领取任务: 占槽 + 建尝试 + 建租约(发 fencing token) + 任务 running。

    同一 U07 事务内完成(03 §4.1 ①); 无空槽或任务非 queued 返回 None。
    """
    return tasks_service.claim_and_run(db, task_id, worker_id)


def slot_available(db: Session, pool: str) -> bool:
    """槽门禁: 池内是否存在可用槽(领取前确认, 03 §5.2 分配流程第 1 步)。"""
    rows = db.execute(
        select(ComputeSlot).where(
            ComputeSlot.pool_name == pool, ComputeSlot.status.in_(("free", "busy"))
        )
    ).scalars().all()
    if not rows:
        # 槽行尚未初始化(惰性建表由领取路径完成): 视为有空位, 领取时创建
        return True
    return any(slot.in_use < slot.capacity for slot in rows)


# ---------------------------------------------------------------------------
# 续租 / 租约校验 / 进度(带 fencing)
# ---------------------------------------------------------------------------


def verify_lease(db: Session, attempt_id: int, token: UUID) -> TaskLease | None:
    """校验租约有效性: 该尝试 + token 匹配 + status='active'。

    返回 None 表示租约已失效(过期/撤销/释放), 任何写回必须被拒绝(03 §4.4)。
    """
    return db.execute(
        select(TaskLease).where(
            TaskLease.attempt_id == attempt_id,
            TaskLease.lease_token == token,
            TaskLease.status == "active",
        )
    ).scalar_one_or_none()


def renew_lease(db: Session, attempt_id: int, token: UUID) -> bool:
    """续租(03 §4.2: 间隔 15 s, TTL 60 s)。

    返回 False = 租约已失效, 调用方必须立即自毁(终止子进程、停止写回)。
    """
    now = datetime.now(UTC)
    n = db.execute(
        sa.update(TaskLease)
        .where(
            TaskLease.attempt_id == attempt_id,
            TaskLease.lease_token == token,
            TaskLease.status == "active",
        )
        .values(renewed_at=now, expires_at=now + timedelta(seconds=LEASE_TTL_SECONDS))
    ).rowcount
    if n == 1:
        db.commit()  # 续租即独立事务(与执行路径隔离, 失败不影响执行)
        return True
    db.rollback()
    logger.warning("续租失败(租约已失效): attempt=%s token=%s", attempt_id, token)
    return False


def report_progress(
    db: Session, attempt_id: int, token: UUID, task_id: int,
    percent: float, stage: str, detail: dict | None = None,
) -> bool:
    """带 fencing 的进度回写(PG UPSERT + Redis 秒级进度, 03 §7.1)。

    租约无效(0 行)返回 False, 调用方停止写回。
    """
    if verify_lease(db, attempt_id, token) is None:
        return False
    tasks_service.record_progress(db, task_id, stage, percent, detail, attempt_id=attempt_id)
    return True


# ---------------------------------------------------------------------------
# 提交结果(证据包 + 四维评估 + 结果索引 + 完成 + 释放, 03 §4.1 ③ / §11.4)
# ---------------------------------------------------------------------------


def submit_result(
    db: Session,
    claim: Claim,
    *,
    payload: dict,
    outcome: str,
    actor_id: int | None = None,
) -> SubmitReceipt:
    """仅租约持有者可提交(03 §11.4): token 不符/租约过期 → LeaseRejectedError。

    单事务顺序:
        1) fencing 校验(租约 active + token 匹配, 0 行 → 拒绝, 整笔回滚);
        2) 结果序列化 → 内容寻址对象(对象存储, sha256 去重);
        3) 证据包(evidence_packages, 不可变) + 对象引用;
        4) 四维评估(result_assessments, assessor='system');
        5) 结果索引(result_index: 旧行 is_latest=false → 插新行);
        6) 释放: 租约 released + 尝试 succeeded + 槽释放;
        7) 任务 completed + business_outcome(技术状态与业务结局正交, 03 §3.2)。
    """
    attempt_id, token = claim.attempt_id, claim.lease_token
    task = db.get(Task, claim.task_id)
    if task is None:
        raise LeaseRejectedError("任务不存在", params={"task_id": claim.task_id})
    if verify_lease(db, attempt_id, token) is None:
        db.rollback()
        raise LeaseRejectedError(
            "租约失效, 迟到结果拒绝写入",
            params={"task_id": claim.task_id, "attempt_id": attempt_id},
            location={"object_type": "task", "object_id": claim.task_id},
        )
    who = actor_id or task.requested_by

    # 计算类任务: 证据包 + 四维评估 + 结果索引(证据与快照一一绑定, 01 §8.1)
    evidence_id: int | None = None
    assessment_id: int | None = None
    index_id: int | None = None
    snapshot = db.get(CalcSnapshot, task.calc_snapshot_id) if task.calc_snapshot_id else None
    if snapshot is not None:
        blob = _payload_bytes(payload)
        content_hash = sha256_hex(blob)
        obj = put_object(
            db, blob, "application/json", source_category="evidence",
            purpose="evidence_package", actor_id=who,
        )
        evidence = EvidencePackage(
            task_id=task.id, attempt_id=attempt_id, calc_snapshot_id=snapshot.id,
            object_id=obj.id, content_hash=content_hash, status="complete", created_by=who,
        )
        db.add(evidence)
        db.flush()
        evidence_id = evidence.id
        add_ref(db, obj.id, "evidence_package", evidence.id, purpose="evidence_package",
                        actor_id=who)

        assessment = payload.get("assessment") or {}
        assess = ResultAssessment(
            evidence_package_id=evidence.id,
            assessor="system",
            dimension_physical=_dim(assessment.get("dimension_physical")),
            dimension_optimality=_dim(assessment.get("dimension_optimality")),
            dimension_financial=_dim(assessment.get("dimension_financial")),
            dimension_reliability=_dim(assessment.get("dimension_reliability")),
            overall_score=assessment.get("overall_score"),
            comment=assessment.get("comment"),
            detail=assessment.get("detail"),
        )
        db.add(assess)
        db.flush()
        assessment_id = assess.id

        # 结果索引: 每项目版本至多一条 is_latest(01 §8.3; 先置旧行 false 再插新行)
        db.execute(
            sa.update(ResultIndex)
            .where(
                ResultIndex.project_version_id == snapshot.project_version_id,
                ResultIndex.is_latest.is_(True),
            )
            .values(is_latest=False)
        )
        result_hash = sha256_hex((f"{snapshot.content_hash}:{content_hash}").encode())
        index = ResultIndex(
            project_id=task.project_id,
            project_version_id=snapshot.project_version_id,
            evidence_package_id=evidence.id,
            assessment_id=assess.id,
            result_hash=result_hash,
            is_latest=True,
        )
        db.add(index)
        db.flush()
        index_id = index.id

    # 释放(带 token 校验, 0 行 → 整笔回滚)
    release_attempt(db, attempt_id, token, attempt_status="succeeded", stop_reason=None)

    # 任务终态 + 业务结局(正交保存, 03 §3.2)
    if task.status == "completed":  # 幂等: 取消竞态下已由他方完成
        return SubmitReceipt(evidence_id, assessment_id, index_id, outcome)
    tasks_service.complete_task(db, task.id, outcome=outcome)
    _write_diagnostic(
        db, task.id, attempt_id, level=SEVERITY_INFO, code=TASK_QUEUED, message="任务完成",
        context={"business_outcome": outcome, "evidence_package_id": evidence_id,
                 "assessment_id": assessment_id},
    )
    return SubmitReceipt(evidence_id, assessment_id, index_id, outcome)


def release_attempt(
    db: Session, attempt_id: int, token: UUID, *, attempt_status: str, stop_reason: str | None,
) -> TaskAttempt:
    """带 fencing 的尝试收尾: 租约 released/revoked + 尝试终态 + 槽释放(03 §4.1 ③)。

    租约不匹配(0 行)抛 LeaseRejectedError —— 调用方须整体回滚。
    """
    lease_status = "released" if attempt_status == "succeeded" else "revoked"
    n = db.execute(
        sa.update(TaskLease)
        .where(
            TaskLease.attempt_id == attempt_id,
            TaskLease.lease_token == token,
            TaskLease.status == "active",
        )
        .values(status=lease_status)
    ).rowcount
    if n != 1:
        raise LeaseRejectedError(
            "租约失效, 尝试收尾被拒绝",
            params={"attempt_id": attempt_id},
        )
    attempt = db.get(TaskAttempt, attempt_id)
    if attempt is None or attempt.status in ("succeeded", "failed", "stopped"):
        raise LeaseRejectedError("尝试已终态, 不可重复收尾", params={"attempt_id": attempt_id})
    attempt.status = attempt_status
    attempt.stop_reason = stop_reason
    attempt.finished_at = datetime.now(UTC)
    tasks_service.release_slot(db, attempt_id)
    return attempt


def fail_attempt(
    db: Session,
    claim: Claim,
    *,
    code: str,
    message: str,
    stack_trace: str | None = None,
    outcome: str | None = None,
    level: str = SEVERITY_ERROR,
) -> Task:
    """确定性失败收拢(带 fencing): 尝试 failed + 租约 revoked + 槽释放 + 任务 failed。

    03 §6.3: 快照/数据校验失败(TASK-DATA-001/002)自动映射 insufficient_evidence,
    确定性失败不自动重试。
    """
    if verify_lease(db, claim.attempt_id, claim.lease_token) is None:
        db.rollback()
        raise LeaseRejectedError(
            "租约失效, 失败收拢被拒绝", params={"task_id": claim.task_id, "attempt_id": claim.attempt_id}
        )
    release_attempt(db, claim.attempt_id, claim.lease_token, attempt_status="failed", stop_reason=code)
    task = tasks_service.fail_task(
        db, claim.task_id, code=code, message=message, stack_trace=stack_trace,
        level=level, outcome=outcome,
    )
    _write_diagnostic(
        db, task.id, claim.attempt_id, level=level, code=code, message=message,
        context={"outcome": outcome or task.business_outcome},
    )
    return task


def cancel_attempt(
    db: Session, claim: Claim, *, reason: str = "cancelled", outcome: str | None = None,
) -> Task:
    """确认取消收拢(03 §6.1): 尝试 stopped + 租约 revoked + 槽释放 + 任务 cancelled。

    任务不在 cancelling(取消竞态下已终态)时抛 tasks_service.TaskStateError,
    调用方以"先落终态者为准"忽略。
    """
    task = db.get(Task, claim.task_id)
    if task is None:
        raise tasks_service.TaskStateError("任务不存在", params={"task_id": claim.task_id})
    if task.status == "cancelled":
        return task  # 幂等
    if task.status != "cancelling":
        raise tasks_service.TaskStateError(
            "任务不在取消中", params={"task_id": claim.task_id, "status": task.status},
            location={"object_type": "task", "object_id": claim.task_id},
        )
    if verify_lease(db, claim.attempt_id, claim.lease_token) is None:
        db.rollback()
        raise LeaseRejectedError(
            "租约失效, 取消收拢被拒绝", params={"task_id": claim.task_id, "attempt_id": claim.attempt_id}
        )
    release_attempt(db, claim.attempt_id, claim.lease_token, attempt_status="stopped", stop_reason=reason)
    task.status = "cancelled"
    task.business_outcome = outcome
    task.updated_at = datetime.now(UTC)
    queue.clear_cancel(task.id)
    _write_diagnostic(
        db, task.id, claim.attempt_id, level=SEVERITY_INFO, code=TASK_QUEUED, message="任务已取消",
        context={"business_outcome": outcome},
    )
    return task


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _payload_bytes(payload: dict) -> bytes:
    """结果 payload 规范序列化(键排序, 紧凑 JSON; 与快照哈希约定一致)。"""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default
    ).encode("utf-8")


def _json_default(value: object) -> object:
    """JSON 兜底序列化: datetime → ISO 字符串; 其余交给标准编码器。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (set, tuple)):
        return list(value)
    raise TypeError(f"不可序列化类型: {type(value)!r}")


def _dim(value: object) -> str:
    """四维评估值规范化(01 §8.2 CHECK: pass/fail/unknown)。"""
    return str(value) if value in ("pass", "fail", "unknown") else "unknown"


def _write_diagnostic(
    db: Session,
    task_id: int,
    attempt_id: int | None,
    *,
    level: str,
    code: str,
    message: str,
    context: dict | None = None,
) -> None:
    """写入任务诊断(不可变表, 只 INSERT)。"""
    db.add(TaskDiagnostic(
        task_id=task_id, attempt_id=attempt_id, level=level, code=code,
        message=message, context=context,
    ))
