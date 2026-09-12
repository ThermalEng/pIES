"""Worker 尝试级事务用例(application/worker.attempt_cases): 完整尝试事务所有者。

纠偏 Wave 3: ``iesplan.worker`` 层不调用 commit/rollback; 领取/续租/提交/
失败/取消各自的完整事务由本模块用例拥有:

- acquire_attempt: 占槽 + 建尝试 + 建租约(fencing token) + 任务 running,
  同事务提交 —— 领取先形成正确可见的租约/尝试状态, 后续执行回滚不再
  丢失租约(旧链路回滚后租约丢失曾被误判为 lease_rejected);
- renew_attempt_lease: 续租行级更新, 独立事务(与执行路径隔离);
- submit_attempt_result: 结果提交全序列(证据包 + 四维评估 + 结果索引 +
  尝试成功 + 任务完成), 同事务提交; fencing 失败整笔回滚并抛
  LeaseRejectedError(迟到结果永远不入权威库);
- fail_attempt: 确定性失败收拢(尝试 failed + 租约 revoked + 槽释放 +
  任务 failed + 诊断), 同事务提交;
- cancel_attempt: 取消收拢(尝试 stopped + 租约 revoked + 槽释放 +
  任务 cancelled), 同事务提交。

行级读写全部经 lease_cases(flush-only 步骤)与 tasks/results/dataset/
project/storage 领域公开门面, 不声明裸表, 不导入 ``iesplan.services``
与 ``iesplan.models``。

依赖方向: worker → application → (storage/领域门面)。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from iesplan.application.tasks.submissions import (
    LEASE_TTL_SECONDS,
    Claim,
    TaskStateError,
)
from iesplan.application.worker.lease_cases import (
    LeaseRejectedError,
    acquire_task,
    attach_result_ref,
    cancel_task_record,
    clear_cancel_signal,
    complete_task,
    create_assessment_record,
    create_evidence_record,
    fail_task,
    fenced_release_attempt,
    flip_result_index,
    get_snapshot_record,
    get_task_record,
    insert_result_index,
    renew_lease_once,
    store_result_blob,
    verify_lease,
    write_diagnostic,
)
from iesplan.core.diagnostics import SEVERITY_ERROR, SEVERITY_INFO, TASK_QUEUED
from iesplan.tasks import TaskRecord

__all__ = [
    "LeaseRejectedError",
    "SubmitReceipt",
    "acquire_attempt",
    "cancel_attempt",
    "fail_attempt",
    "renew_attempt_lease",
    "submit_attempt_result",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SubmitReceipt:
    """提交成功回执(证据包/评估/结果索引 id, 供诊断与测试断言)。"""

    evidence_package_id: int | None
    assessment_id: int | None
    result_index_id: int | None
    outcome: str


# ---------------------------------------------------------------------------
# 领取(事务所有者: 同事务提交, 形成可见租约/尝试状态)
# ---------------------------------------------------------------------------


def acquire_attempt(db: Session, task_id: int, worker_id: str) -> Claim | None:
    """领取任务并提交: 占槽 + 建尝试 + 建租约 + 任务 running。

    提交后租约/尝试状态即刻可见; 无空槽或任务非 queued 返回 None(任务保持
    排队)。失败整笔回滚并原样抛异常。
    """
    try:
        claim = acquire_task(db, task_id, worker_id)
        db.commit()
        return claim
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------------------
# 续租(独立事务, 与执行路径隔离)
# ---------------------------------------------------------------------------


def renew_attempt_lease(
    db: Session, attempt_id: int, token: UUID | str, *, ttl_seconds: int = LEASE_TTL_SECONDS
) -> bool:
    """续租(03 §4.2): 成功提交并返回 True; 0 行 → 回滚并返回 False。

    返回 False = 租约已失效, 调用方必须立即自毁(终止子进程、停止写回)。
    """
    try:
        n = renew_lease_once(db, attempt_id, token, ttl_seconds=ttl_seconds)
    except Exception:
        db.rollback()
        raise
    if n == 1:
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
        return True
    db.rollback()
    logger.warning("续租失败(租约已失效): attempt=%s token=%s", attempt_id, token)
    return False


# ---------------------------------------------------------------------------
# 提交结果(事务所有者)
# ---------------------------------------------------------------------------


def submit_attempt_result(
    db: Session,
    claim: Claim,
    *,
    payload: dict,
    outcome: str,
    actor_id: int | None = None,
) -> SubmitReceipt:
    """仅租约持有者可提交(03 §11.4): token 不符/租约过期 → 整笔回滚并抛 LeaseRejectedError。

    单事务顺序:
        1) fencing 校验(租约 active + token 匹配);
        2) 结果序列化 → 对象存储;
        3) 证据包(evidence_packages, 不可变) + 对象引用;
        4) 四维评估(result_assessments, assessor='system');
        5) 结果索引(result_index: 旧行 is_latest=false → 插新行);
        6) 释放: 租约 released + 尝试 succeeded + 槽释放;
        7) 任务 completed + business_outcome(技术状态与业务结局正交, 03 §3.2)。
    """
    try:
        attempt_id, token = claim.attempt_id, claim.lease_token
        task = get_task_record(db, claim.task_id)
        if task is None:
            raise LeaseRejectedError("任务不存在", params={"task_id": claim.task_id})
        if verify_lease(db, attempt_id, token) is None:
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
        snapshot = get_snapshot_record(db, task.calc_snapshot_id) if task.calc_snapshot_id else None
        if snapshot is not None:
            blob = _payload_bytes(payload)
            object_id = store_result_blob(db, blob, actor_id=who)
            evidence_id = create_evidence_record(
                db, task_id=task.id, attempt_id=attempt_id, snapshot_id=snapshot.id,
                object_id=object_id, created_by=who,
            )
            attach_result_ref(db, object_id, evidence_id, actor_id=who)

            assessment = payload.get("assessment") or {}
            assessment_id = create_assessment_record(
                db,
                evidence_package_id=evidence_id,
                dimensions={
                    "physical": _dim(assessment.get("dimension_physical")),
                    "optimality": _dim(assessment.get("dimension_optimality")),
                    "financial": _dim(assessment.get("dimension_financial")),
                    "reliability": _dim(assessment.get("dimension_reliability")),
                },
                overall_score=assessment.get("overall_score"),
                comment=assessment.get("comment"),
                detail=assessment.get("detail"),
            )

            # 结果索引: 每项目版本至多一条 is_latest(01 §8.3; 先置旧行 false 再插新行)
            flip_result_index(db, project_version_id=snapshot.project_version_id)
            index_id = insert_result_index(
                db,
                project_id=task.project_id,
                project_version_id=snapshot.project_version_id,
                evidence_package_id=evidence_id,
                assessment_id=assessment_id,
            )

        # 释放(带 token 校验, 0 行 → 整笔回滚)
        fenced_release_attempt(db, attempt_id, token, attempt_status="succeeded", stop_reason=None)

        # 任务终态 + 业务结局(正交保存, 03 §3.2)
        if task.status != "completed":  # 幂等: 取消竞态下已由他方完成则只收尾尝试
            complete_task(db, task.id, outcome=outcome)
            write_diagnostic(
                db, task.id, attempt_id, level=SEVERITY_INFO, code=TASK_QUEUED,
                message="任务完成",
                context={"business_outcome": outcome, "evidence_package_id": evidence_id,
                         "assessment_id": assessment_id},
            )
        receipt = SubmitReceipt(evidence_id, assessment_id, index_id, outcome)
        db.commit()
        logger.info("任务完成: task=%s outcome=%s evidence=%s",
                    task.id, outcome, receipt.evidence_package_id)
        return receipt
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------------------
# 失败 / 取消收拢(事务所有者)
# ---------------------------------------------------------------------------


def fail_attempt(
    db: Session,
    claim: Claim,
    *,
    code: str,
    message: str,
    stack_trace: str | None = None,
    outcome: str | None = None,
    level: str = SEVERITY_ERROR,
) -> TaskRecord:
    """确定性失败收拢(带 fencing): 尝试 failed + 租约 revoked + 槽释放 + 任务 failed。

    03 §6.3: 快照/数据校验失败(TASK-DATA-001/002)自动映射 insufficient_evidence,
    确定性失败不自动重试。租约已失效 → 整笔回滚并抛 LeaseRejectedError。
    """
    try:
        if verify_lease(db, claim.attempt_id, claim.lease_token) is None:
            raise LeaseRejectedError(
                "租约失效, 失败收拢被拒绝",
                params={"task_id": claim.task_id, "attempt_id": claim.attempt_id},
            )
        fenced_release_attempt(
            db, claim.attempt_id, claim.lease_token, attempt_status="failed", stop_reason=code
        )
        task = fail_task(
            db, claim.task_id, code=code, message=message, stack_trace=stack_trace,
            level=level, outcome=outcome,
        )
        write_diagnostic(
            db, task.id, claim.attempt_id, level=level, code=code, message=message,
            context={"outcome": outcome or task.business_outcome},
        )
        db.commit()
        return task
    except Exception:
        db.rollback()
        raise


def cancel_attempt(
    db: Session, claim: Claim, *, reason: str = "cancelled", outcome: str | None = None,
) -> TaskRecord:
    """确认取消收拢(03 §6.1): 尝试 stopped + 租约 revoked + 槽释放 + 任务 cancelled。

    任务不在 cancelling(取消竞态下已终态)时抛 TaskStateError,
    调用方以"先落终态者为准"忽略。租约已失效 → 整笔回滚并抛 LeaseRejectedError。
    """
    try:
        task = get_task_record(db, claim.task_id)
        if task is None:
            raise TaskStateError("任务不存在", params={"task_id": claim.task_id})
        if task.status == "cancelled":
            db.commit()
            return task  # 幂等
        if task.status != "cancelling":
            raise TaskStateError(
                "任务不在取消中", params={"task_id": claim.task_id, "status": task.status},
                location={"object_type": "task", "object_id": claim.task_id},
            )
        if verify_lease(db, claim.attempt_id, claim.lease_token) is None:
            raise LeaseRejectedError(
                "租约失效, 取消收拢被拒绝",
                params={"task_id": claim.task_id, "attempt_id": claim.attempt_id},
            )
        fenced_release_attempt(
            db, claim.attempt_id, claim.lease_token, attempt_status="stopped", stop_reason=reason
        )
        cancelled = cancel_task_record(db, task.id, outcome=outcome)
        clear_cancel_signal(task.id)
        write_diagnostic(
            db, task.id, claim.attempt_id, level=SEVERITY_INFO, code=TASK_QUEUED,
            message="任务已取消",
            context={"business_outcome": outcome},
        )
        db.commit()
        return cancelled
    except Exception:
        db.rollback()
        raise


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
