"""Worker 租约协议测试(03 §4): 领取/续租/过期/迟到拒绝/双 Worker 竞争/提交/收拢。

- 数据库: SQLite :memory:(StaticPool 共享连接, 见 worker_testkit);
- 队列: IESPLAN_QUEUE=memory 内存后端(无外部 Redis);
- 对象存储: settings.data_dir → tmp_path。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")

import pytest  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from worker_testkit import setup_environment  # noqa: E402

from iesplan.db import Base  # noqa: E402
from iesplan.models.calc import ComputeSlot, Task, TaskAttempt, TaskLease  # noqa: E402
from iesplan.models.result import EvidencePackage, ResultAssessment, ResultIndex  # noqa: E402
from iesplan.services import queue  # noqa: E402
from iesplan.worker import lease  # noqa: E402

# ---------------------------------------------------------------------------
# 测试环境
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """模块级 SQLite 内存引擎(StaticPool: 所有会话共享同一连接)。"""
    eng = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _clean_state(engine: Engine, db: Session) -> Iterator[None]:
    """每个测试前重置内存队列, 结束后清空全部表(避免测试间串扰)。"""
    queue.force_memory()
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def db(engine: Engine) -> Iterator[Session]:
    """函数级共享会话(服务与测试共用, 提交由调用方控制)。"""
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


@pytest.fixture()
def env(db: Session, tmp_path: Path) -> dict[str, Any]:
    """迷你任务环境(calc 任务, 含快照与数据集)。"""
    return setup_environment(db, tmp_path, task_type="calc")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _claim(db: Session, env: dict[str, Any], worker_id: str = "cw-test") -> lease.Claim:
    """领取任务并提交事务(模拟 Worker 领取动作)。"""
    return _claim_task(db, env["task"].id, worker_id)


def _claim_task(db: Session, task_id: int, worker_id: str = "cw-test") -> lease.Claim:
    """按任务 id 领取并提交事务。"""
    claim = lease.acquire_attempt(db, task_id, worker_id)
    assert claim is not None
    db.commit()
    return claim


def _lease_row(db: Session, claim: lease.Claim) -> TaskLease:
    row = db.execute(
        select(TaskLease).where(TaskLease.attempt_id == claim.attempt_id)
    ).scalars().first()
    assert row is not None
    return row


def _slot(db: Session, claim: lease.Claim) -> ComputeSlot:
    """当前绑定尝试的槽(领取后未释放时使用)。"""
    row = db.execute(
        select(ComputeSlot).where(ComputeSlot.current_attempt_id == claim.attempt_id)
    ).scalars().first()
    assert row is not None
    return row


def _slots_used(db: Session, pool: str = "compute") -> int:
    """池内已用槽数(释放后断言用, 03 §5.2 in_use 归零)。"""
    rows = db.execute(
        select(ComputeSlot).where(ComputeSlot.pool_name == pool)
    ).scalars().all()
    return sum(row.in_use for row in rows)


def _payload(outcome: str = "normal_completion") -> dict:
    """最小结果 payload(四维评估完整, 供证据包/评估落库)。"""
    return {
        "result_kind": "test_result",
        "status": "ok",
        "solver_status": "OPTIMAL",
        "assessment": {
            "dimension_physical": "pass",
            "dimension_optimality": "pass",
            "dimension_financial": "pass",
            "dimension_reliability": "unknown",
            "overall_score": 85.0,
            "comment": "测试评估",
            "detail": {"kind": "test"},
        },
        "summary": {"ok": True},
        "outcome": outcome,
    }


# ---------------------------------------------------------------------------
# 领取(槽 + 尝试 + 租约 + token)
# ---------------------------------------------------------------------------


class TestAcquire:
    """领取协议: 03 §4.1 ①(尝试/租约/token/槽/任务状态同事务)。"""

    def test_claim_creates_attempt_lease_and_slot(self, db: Session, env: dict[str, Any]):
        claim = _claim(db, env)
        assert claim.attempt_no == 1
        # 尝试 running
        attempt = db.get(TaskAttempt, claim.attempt_id)
        assert attempt is not None and attempt.status == "running"
        assert attempt.worker_id == "cw-test"
        # 租约 active + token 匹配
        lease_row = _lease_row(db, claim)
        assert lease_row.status == "active"
        assert lease_row.lease_token == claim.lease_token
        assert lease_row.acquired_by == "cw-test"
        # 任务 running + attempt_count +1
        task = db.get(Task, env["task"].id)
        assert task.status == "running"
        assert task.attempt_count == 1
        # 槽占用
        slot = _slot(db, claim)
        assert slot.in_use == 1
        # 出队(可重建视图)
        assert queue.dequeue("compute") is None

    def test_double_claim_rejected(self, db: Session, env: dict[str, Any]):
        """双 Worker 竞争同一任务: 仅第一个领取成功(03 §1.3 一任务一租约一 token)。"""
        first = _claim(db, env, worker_id="cw-01")
        assert first is not None
        second = lease.acquire_attempt(db, env["task"].id, "cw-02")
        assert second is None  # 任务已 running, 第二个 Worker 领不到
        # 第一个 Worker 的租约仍有效
        assert lease.verify_lease(db, first.attempt_id, first.lease_token) is not None

    def test_claim_when_slots_full(self, db: Session, env: dict[str, Any], tmp_path: Path):
        """槽满: 第三个任务保持 queued(03 §5.2 排队等待)。"""
        env2 = setup_environment(db, tmp_path, task_type="calc", config={"tag": "two"})
        env3 = setup_environment(db, tmp_path, task_type="calc", config={"tag": "three"})
        c1 = _claim(db, env)
        c2 = _claim(db, env2)
        assert c1 and c2  # 默认 2 个计算槽
        assert lease.acquire_attempt(db, env3["task"].id, "cw-03") is None
        assert db.get(Task, env3["task"].id).status == "queued"

    def test_claim_non_queued_returns_none(self, db: Session, env: dict[str, Any]):
        task = env["task"]
        task.status = "cancelling"
        db.commit()
        assert lease.acquire_attempt(db, task.id, "cw-01") is None

    def test_slot_gate(self, db: Session, env: dict[str, Any]):
        assert lease.slot_available(db, "compute") is True
        _claim(db, env)
        assert lease.slot_available(db, "compute") is True  # 2 槽, 用 1 仍可领取
        assert lease.slot_available(db, "io") is True


# ---------------------------------------------------------------------------
# 续租与过期判定
# ---------------------------------------------------------------------------


class TestRenew:
    """续租与租约失效(03 §4.2/§4.3: 0 行 → 失效 → 自毁契约)。"""

    def test_renew_with_token(self, db: Session, env: dict[str, Any]):
        claim = _claim(db, env)
        lease_row = _lease_row(db, claim)
        old_expires = lease_row.expires_at
        assert lease.renew_lease(db, claim.attempt_id, claim.lease_token) is True
        db.refresh(lease_row)
        assert lease_row.expires_at > old_expires  # 续租延长 TTL

    def test_renew_wrong_token_rejected(self, db: Session, env: dict[str, Any]):
        claim = _claim(db, env)
        from uuid import uuid4

        assert lease.renew_lease(db, claim.attempt_id, uuid4()) is False

    def test_renew_after_expiry_rejected(self, db: Session, env: dict[str, Any]):
        """租约过期(守护进程置 expired): 旧 token 永久失效, 续租被拒(03 §4.3)。"""
        claim = _claim(db, env)
        lease_row = _lease_row(db, claim)
        lease_row.status = "expired"  # 模拟守护进程过期回收
        db.commit()
        assert lease.renew_lease(db, claim.attempt_id, claim.lease_token) is False

    def test_verify_lease_invalid(self, db: Session, env: dict[str, Any]):
        claim = _claim(db, env)
        from uuid import uuid4

        assert lease.verify_lease(db, claim.attempt_id, claim.lease_token) is not None
        assert lease.verify_lease(db, claim.attempt_id, uuid4()) is None

    def test_report_progress_fenced(self, db: Session, env: dict[str, Any]):
        claim = _claim(db, env)
        assert lease.report_progress(db, claim.attempt_id, claim.lease_token,
                                     env["task"].id, 45.0, "solve", {"it": 1}) is True
        # 错误 token: 拒绝写进度
        from uuid import uuid4

        assert lease.report_progress(db, claim.attempt_id, uuid4(),
                                     env["task"].id, 50.0, "solve") is False


# ---------------------------------------------------------------------------
# 提交(证据包 + 四维评估 + 结果索引)与迟到拒绝
# ---------------------------------------------------------------------------


class TestSubmit:
    """提交协议: 03 §4.1 ③ / §11.4(仅租约持有者可提交)。"""

    def test_submit_completes_task_with_evidence(self, db: Session, env: dict[str, Any]):
        claim = _claim(db, env)
        receipt = lease.submit_result(db, claim, payload=_payload(), outcome="normal_completion",
                                      actor_id=env["user"].id)
        db.commit()
        assert receipt.evidence_package_id is not None
        assert receipt.assessment_id is not None
        assert receipt.result_index_id is not None
        # 任务完成 + 业务结局(正交保存)
        task = db.get(Task, env["task"].id)
        assert task.status == "completed"
        assert task.business_outcome == "normal_completion"
        # 尝试 succeeded + 租约 released + 槽释放
        attempt = db.get(TaskAttempt, claim.attempt_id)
        assert attempt.status == "succeeded"
        assert attempt.finished_at is not None
        assert _lease_row(db, claim).status == "released"
        assert _slots_used(db) == 0  # 槽已释放
        # 证据包 + 评估 + 结果索引
        package = db.get(EvidencePackage, receipt.evidence_package_id)
        assert package is not None
        assert package.task_id == env["task"].id
        assert package.attempt_id == claim.attempt_id
        assert package.calc_snapshot_id == env["snapshot"].id
        assert package.status == "complete"
        assessment = db.get(ResultAssessment, receipt.assessment_id)
        assert assessment is not None
        assert assessment.assessor == "system"
        assert assessment.dimension_physical == "pass"
        index = db.get(ResultIndex, receipt.result_index_id)
        assert index is not None
        assert index.is_latest is True
        assert index.project_version_id == env["version"].id
        assert index.assessment_id == receipt.assessment_id

    def test_submit_outcome_recorded(self, db: Session, env: dict[str, Any]):
        claim = _claim(db, env)
        lease.submit_result(db, claim, payload=_payload(), outcome="restricted_results")
        db.commit()
        task = db.get(Task, env["task"].id)
        assert task.status == "completed"
        assert task.business_outcome == "restricted_results"

    def test_late_submit_after_expiry_rejected(self, db: Session, env: dict[str, Any]):
        """迟到尝试拒绝(03 §4.3): 租约过期后提交 → 整笔拒绝, 结果不入权威库。"""
        claim = _claim(db, env)
        lease_row = _lease_row(db, claim)
        lease_row.status = "expired"  # 守护进程已回收
        db.commit()
        with pytest.raises(lease.LeaseRejectedError):
            lease.submit_result(db, claim, payload=_payload(), outcome="normal_completion")
        task = db.get(Task, env["task"].id)
        assert task.status == "running"  # 状态未被篡改
        assert db.execute(select(EvidencePackage)).scalars().first() is None

    def test_double_submit_rejected(self, db: Session, env: dict[str, Any]):
        """提交后租约已 released: 同一 token 再次提交 → 拒绝(0 行回滚)。"""
        claim = _claim(db, env)
        lease.submit_result(db, claim, payload=_payload(), outcome="normal_completion")
        db.commit()
        with pytest.raises(lease.LeaseRejectedError):
            lease.submit_result(db, claim, payload=_payload(), outcome="normal_completion")

    def test_new_result_flips_old_latest(self, db: Session, env: dict[str, Any]):
        """新结果发布: 旧 result_index 置 is_latest=false(01 §8.3 同事务)。"""
        claim = _claim(db, env)
        lease.submit_result(db, claim, payload=_payload(), outcome="normal_completion")
        db.commit()
        # 同项目版本再提交一个任务(新结果)
        task2 = Task(
            project_id=env["project"].id, type="calc", status="queued",
            calc_snapshot_id=env["snapshot"].id, requested_by=env["user"].id,
        )
        db.add(task2)
        db.flush()
        queue.enqueue(task2.id, "compute", task_type="calc", snapshot_id=env["snapshot"].id)
        db.commit()
        claim2 = _claim_task(db, task2.id, worker_id="cw-02")
        lease.submit_result(db, claim2, payload=_payload(), outcome="normal_completion")
        db.commit()
        latest = db.execute(
            select(ResultIndex).where(
                ResultIndex.project_version_id == env["version"].id, ResultIndex.is_latest.is_(True)
            )
        ).scalars().all()
        assert len(latest) == 1
        assert latest[0].evidence_package_id is not None
        old_index = db.execute(
            select(ResultIndex).where(
                ResultIndex.project_version_id == env["version"].id,
                ResultIndex.evidence_package_id != latest[0].evidence_package_id,
            )
        ).scalars().all()
        assert len(old_index) == 1 and old_index[0].is_latest is False


# ---------------------------------------------------------------------------
# 失败 / 取消收拢
# ---------------------------------------------------------------------------


class TestTerminal:
    """失败与取消收拢(03 §6.1/§6.3: 尝试/租约/槽/任务同事务)。"""

    def test_fail_attempt(self, db: Session, env: dict[str, Any]):
        claim = _claim(db, env)
        lease.fail_attempt(db, claim, code="TASK-SOLVE-001", message="求解失败",
                           outcome="no_recommendation")
        db.commit()
        task = db.get(Task, env["task"].id)
        assert task.status == "failed"
        assert task.business_outcome == "no_recommendation"
        attempt = db.get(TaskAttempt, claim.attempt_id)
        assert attempt.status == "failed"
        assert attempt.stop_reason == "TASK-SOLVE-001"
        assert _lease_row(db, claim).status == "revoked"
        assert _slots_used(db) == 0  # 槽已释放

    def test_fail_snapshot_missing_outcome(self, db: Session, env: dict[str, Any]):
        """快照/数据校验失败 → insufficient_evidence(03 §3.2 表)。"""
        claim = _claim(db, env)
        lease.fail_attempt(db, claim, code="TASK-DATA-001", message="快照缺失")
        db.commit()
        task = db.get(Task, env["task"].id)
        assert task.status == "failed"
        assert task.business_outcome == "insufficient_evidence"

    def test_cancel_attempt(self, db: Session, env: dict[str, Any]):
        """取消收拢(03 §6.1): cancelling → cancelled; 批量部分完成 → partial_batch。"""
        claim = _claim(db, env)
        task = env["task"]
        task.status = "cancelling"  # 模拟 API 已发起取消(权威状态变更)
        db.commit()
        lease.cancel_attempt(db, claim, outcome="partial_batch")
        db.commit()
        assert task.status == "cancelled"
        assert task.business_outcome == "partial_batch"
        attempt = db.get(TaskAttempt, claim.attempt_id)
        assert attempt.status == "stopped"
        assert attempt.stop_reason == "cancelled"
        assert _lease_row(db, claim).status == "revoked"
        assert queue.get_cancel(task.id) is None  # 取消信号已清除

    def test_cancel_denied_when_running(self, db: Session, env: dict[str, Any]):
        """取消竞态: 任务未进入 cancelling 时收拢 → TaskStateError(以终态为准)。"""
        claim = _claim(db, env)
        from iesplan.services.tasks import TaskStateError

        with pytest.raises(TaskStateError):
            lease.cancel_attempt(db, claim)
