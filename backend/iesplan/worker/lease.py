"""租约与 fencing token 协议(03-task-scheduling.md §4, Worker 端唯一租约入口)。

权威事实(尝试/租约/fencing token/槽)全部落 PostgreSQL, Redis 队列/进度/心跳
为可重建视图。本模块是 Worker 端的唯一租约入口, 自身为薄门面:

- 领取/续租/进度/提交/失败/取消全部转调 application.worker 事务用例;
- 每个原子步骤的短事务(提交/回滚)由 application.worker 用例拥有(03 §4.1);
- 本模块不调用 commit/rollback, 不接收或操作领域记录, 不直连
  ``iesplan.services`` / ``iesplan.models`` / 裸 SQL。

一致性与 03 §1.3 对齐: PG 是权威; 一任务一租约一 token; 写回必带 token;
终态即封闭(状态迁移校验经 application.worker 用例复用任务服务)。
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.orm import Session

from iesplan.application import worker as worker_app
from iesplan.core.diagnostics import SEVERITY_ERROR
from iesplan.core.errors import AppError

logger = logging.getLogger(__name__)

#: 租约 TTL(秒, 03 §4.2 默认 60 s; 经 application.worker 用例取用)
LEASE_TTL_SECONDS = worker_app.LEASE_TTL_SECONDS

#: 租约失效错误语义由 application.worker 拥有, 本模块仅复出(同一类对象)。
LeaseRejectedError = worker_app.LeaseRejectedError

#: 提交回执类型由 application.worker 拥有, 本模块仅复出。
SubmitReceipt = worker_app.SubmitReceipt


class SlotUnavailableError(AppError):
    """无空闲并发槽(03 §5.2: 无空槽时任务保持 queued, 等待下一轮调度)。"""

    code = "TASK-QUEUE-001"
    severity = SEVERITY_ERROR
    message_key = "ies.diag.task.queue_failed"


# 与 tasks 提交用例 Claim 同构(经 application.worker 用例复用其类型)
Claim = worker_app.Claim


# ---------------------------------------------------------------------------
# 领取 / 槽门禁
# ---------------------------------------------------------------------------


def acquire_attempt(db: Session, task_id: int, worker_id: str) -> Claim | None:
    """领取任务: 占槽 + 建尝试 + 建租约(发 fencing token) + 任务 running。

    领取事务由 application.worker 用例提交(03 §4.1 ①); 返回后租约/尝试
    状态即刻可见。无空槽或任务非 queued 返回 None(任务保持排队)。
    """
    return worker_app.acquire_attempt(db, task_id, worker_id)


def slot_available(db: Session, pool: str) -> bool:
    """槽门禁: 池内是否存在可用槽(领取前确认, 03 §5.2 分配流程第 1 步)。"""
    return worker_app.slot_available(db, pool)


# ---------------------------------------------------------------------------
# 续租 / 租约校验 / 进度(带 fencing)
# ---------------------------------------------------------------------------


def verify_lease(
    db: Session, attempt_id: int, token: UUID
) -> worker_app.TaskLeaseRecord | None:
    """校验租约有效性: 该尝试 + token 匹配 + status='active'。

    返回 None 表示租约已失效(过期/撤销/释放), 任何写回必须被拒绝(03 §4.4)。
    """
    return worker_app.verify_lease(db, attempt_id, token)


def renew_lease(db: Session, attempt_id: int, token: UUID) -> bool:
    """续租(03 §4.2: 间隔 15 s, TTL 60 s)。

    续租即独立事务(由 application.worker 用例提交, 与执行路径隔离)。
    返回 False = 租约已失效, 调用方必须立即自毁(终止子进程、停止写回)。
    """
    return worker_app.renew_attempt_lease(db, attempt_id, token)


def report_progress(
    db: Session, attempt_id: int, token: UUID, task_id: int,
    percent: float, stage: str, detail: dict | None = None,
) -> bool:
    """带 fencing 的进度回写(PG UPSERT + Redis 秒级进度, 03 §7.1)。

    本函数只转调 application.worker 短事务用例: 租约有效则记录进度并提交,
    提交后即刻对其他会话可见; 租约无效(0 行)返回 False, 调用方停止写回。
    本模块不调用 commit/rollback。
    """
    return worker_app.report_attempt_progress(
        db, attempt_id, token, task_id, percent, stage, detail
    )


# ---------------------------------------------------------------------------
# 提交结果 / 失败 / 取消收拢(事务由 application.worker 用例拥有)
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

    提交全序列(证据包 + 四维评估 + 结果索引 + 任务完成 + 释放)由
    application.worker 用例同事务提交; 迟到的写回整笔回滚, 永远不入权威库。
    """
    return worker_app.submit_attempt_result(
        db, claim, payload=payload, outcome=outcome, actor_id=actor_id
    )


def fail_attempt(
    db: Session,
    claim: Claim,
    *,
    code: str,
    message: str,
    stack_trace: str | None = None,
    outcome: str | None = None,
    level: str = SEVERITY_ERROR,
) -> worker_app.TaskRecord:
    """确定性失败收拢(带 fencing): 尝试 failed + 租约 revoked + 槽释放 + 任务 failed。

    收拢全序列由 application.worker 用例同事务提交。03 §6.3: 快照缺失
    (TASK-DATA-001)自动映射 insufficient_evidence, 确定性失败不自动重试。
    """
    return worker_app.fail_attempt(
        db, claim, code=code, message=message, stack_trace=stack_trace,
        outcome=outcome, level=level,
    )


def cancel_attempt(
    db: Session, claim: Claim, *, reason: str = "cancelled", outcome: str | None = None,
) -> worker_app.TaskRecord:
    """确认取消收拢(03 §6.1): 尝试 stopped + 租约 revoked + 槽释放 + 任务 cancelled。

    收拢全序列由 application.worker 用例同事务提交。任务不在 cancelling
    (取消竞态下已终态)时抛 TaskStateError, 调用方以"先落终态者为准"忽略。
    """
    return worker_app.cancel_attempt(db, claim, reason=reason, outcome=outcome)
