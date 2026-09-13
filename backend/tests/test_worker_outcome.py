"""R2 Wave 2 切片 B: Worker 职责纠偏行为测试。

契约依据: docs/development/backend-decoupling-finalization.md §C / Wave 2。
只断言公共行为、权限与错误语义, 不复制实现常量、不绑定行号与私有布局:

- 未实现的 I/O 任务(dataset_build/export/import)不得返回成功 outcome;
  占位成功与缺字段默认成功必须失败闭环, 且不得借用求解失败码描述 I/O 不可用;
- 完成路径必须要求显式、合法的业务 outcome, 缺字段不得默认成功;
- report 检查由 application.worker 用例完成: 证据解释/评分与业务 outcome
  归 results 公开能力所有, Worker 只传递不可变输入并消费显式结果契约。

本文件分两组:

- 契约组(TestUnimplementedIoNeverSucceeds / TestCompletionRequiresExplicitOutcome /
  TestReportCheckDelegatedToResultsCapability): 新语义断言, 在纠偏前生产上
  主要以违规形式失败(TDD 红), 实现切片(correction/r2w2-worker)落地后转绿;
  其中非法 outcome 值一例已由任务结局 CHECK 约束失败闭环, 同为回归;
- 回归组(TestReportNoEvidencePath / TestWorkerExecutionGuards): 纠偏前后
  均应通过, 锁定运行编排(领取/租约/取消/收拢)不被本轮改动破坏。

数据库: SQLite :memory:(StaticPool 共享连接); 队列: IESPLAN_QUEUE=memory;
对象存储: settings.data_dir → tmp_path(均见 worker_testkit)。
"""

from __future__ import annotations

import json
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

from iesplan.application import worker as worker_app  # noqa: E402
from iesplan.core.diagnostics import TASK_SOLVE_FAILED  # noqa: E402
from iesplan.core.errors import AppError  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.models.calc import Task, TaskDiagnostic, TaskLease  # noqa: E402
from iesplan.results import (  # noqa: E402
    ASSESSMENT_RULE_VERSION,
    EVIDENCE_COMPLETE,
    create_evidence,
    evaluate_evidence,
    evidence_inner,
    latest_assessment,
)
from iesplan.tasks import queue  # noqa: E402
from iesplan.worker import lease, runner  # noqa: E402

#: 未实现的 I/O 任务类型(0.8 真实实现落地前不得产生成功回执)。
IO_TASK_TYPES: tuple[str, ...] = ("dataset_build", "export", "import")


# ---------------------------------------------------------------------------
# 测试环境(同 test_worker_lease 形态: 模块级内存引擎 + 函数级会话)
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
    """迷你任务环境(calc 任务, 含快照与数据集; 项目下可挂 I/O 与 report 任务)。"""
    return setup_environment(db, tmp_path, task_type="calc")


# ---------------------------------------------------------------------------
# 工具(仅经公共入口: 领取门面 / application.worker 用例 / results 域门面)
# ---------------------------------------------------------------------------


def _claim_task(db: Session, task_id: int, worker_id: str = "w-outcome") -> lease.Claim:
    """领取任务(占槽 + 建尝试 + 建租约 + running, 事务已提交)。"""
    claim = lease.acquire_attempt(db, task_id, worker_id)
    assert claim is not None
    db.commit()
    return claim


def _add_task(db: Session, env: dict[str, Any], task_type: str) -> Task:
    """在环境项目下新增一个 queued 任务并入 io 队列(计算快照无绑定)。"""
    task = Task(
        project_id=env["project"].id, type=task_type, status="queued",
        calc_snapshot_id=None, requested_by=env["user"].id,
    )
    db.add(task)
    db.flush()
    queue.enqueue(task.id, "io", task_type=task_type, snapshot_id=None)
    db.commit()
    return task


def _store_evidence(db: Session, env: dict[str, Any], payload: dict[str, Any]):
    """经 application.worker 对象写入 + results 域门面存一条 complete 证据包。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    object_id = worker_app.store_result_blob(db, blob, actor_id=env["user"].id)
    package = create_evidence(
        db, task_id=env["task"].id, calc_snapshot_id=env["snapshot"].id,
        object_id=object_id, status=EVIDENCE_COMPLETE, created_by=env["user"].id,
    )
    db.commit()
    return package


def _task_view(db: Session, task_id: int):
    """经 application.worker 公开读取任务视图(状态 + 业务结局)。"""
    return worker_app.get_task_record(db, task_id)


def _task_diagnostics(db: Session, task_id: int) -> list[TaskDiagnostic]:
    """任务全部诊断行(按 id 升序)。"""
    return db.execute(
        select(TaskDiagnostic).where(TaskDiagnostic.task_id == task_id).order_by(TaskDiagnostic.id)
    ).scalars().all()


# ---------------------------------------------------------------------------
# 契约组一: 未实现的 I/O 不得返回成功 outcome
# ---------------------------------------------------------------------------


class TestUnimplementedIoNeverSucceeds:
    """未实现 I/O 失败闭环: 三种 io 任务都不得完成为成功。

    纠偏前生产经占位执行器返回成功回执, 以下断言以违规形式失败;
    纠偏后(删除分派入口或结构化不可用失败)应全部通过。
    """

    @pytest.mark.parametrize("task_type", IO_TASK_TYPES)
    def test_unimplemented_io_fails_closed(self, db: Session, env: dict[str, Any], task_type: str):
        task = _add_task(db, env, task_type)
        claim = _claim_task(db, task.id)

        status = runner.run_task(db, claim, worker_id="w-io", isolate=False)

        assert status == "failed", (task_type, status)
        view = _task_view(db, task.id)
        assert view is not None and view.status == "failed", (task_type, view)
        assert view.business_outcome != "normal_completion", (task_type, view.business_outcome)
        # 失败有诊断可查, 且不得借用求解失败码描述 I/O 不可用。
        diags = _task_diagnostics(db, task.id)
        assert diags, task_type
        assert all(d.code != TASK_SOLVE_FAILED for d in diags), [
            (d.code, d.message) for d in diags
        ]


# ---------------------------------------------------------------------------
# 契约组二: 完成路径必须要求显式、合法的业务 outcome
# ---------------------------------------------------------------------------


class TestCompletionRequiresExplicitOutcome:
    """缺 outcome 不得默认成功, 非法 outcome 不得完成。

    缺字段/缺参在纠偏前默认成功, 对应用例以违规形式失败,
    纠偏后(显式校验 + 失败闭环 / 调用方错误)应通过;
    非法 outcome 值已由任务结局 CHECK 约束失败闭环, 此处锁定为回归。
    """

    def test_outcome_less_handler_result_fails_closed(
        self, db: Session, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    ):
        """公开执行器返回缺 outcome 载荷 → runner 不得默认成功完成。"""
        task = _add_task(db, env, "report")
        claim = _claim_task(db, task.id)
        monkeypatch.setattr(
            "iesplan.worker.executors.execute_check",
            lambda ctx: {"result_kind": "external", "status": "ok"},
        )

        status = runner.run_task(db, claim, worker_id="w-outcome", isolate=False)

        assert status == "failed", status
        view = _task_view(db, task.id)
        assert view is not None and view.status == "failed", view
        assert view.business_outcome != "normal_completion", view.business_outcome

    def test_unknown_outcome_value_fails_closed(
        self, db: Session, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    ):
        """公开执行器返回非法 outcome → 不得以该值完成(结局 CHECK 约束兜底)。"""
        task = _add_task(db, env, "report")
        claim = _claim_task(db, task.id)
        monkeypatch.setattr(
            "iesplan.worker.executors.execute_check",
            lambda ctx: {"result_kind": "external", "status": "ok", "outcome": "not_a_real_outcome"},
        )

        status = runner.run_task(db, claim, worker_id="w-outcome", isolate=False)

        assert status == "failed", status
        view = _task_view(db, task.id)
        assert view is not None and view.status == "failed", view
        assert view.business_outcome != "not_a_real_outcome", view.business_outcome

    def test_complete_task_without_outcome_raises(self, db: Session, env: dict[str, Any]):
        """application.worker 完成用例缺结局参数 → 调用方错误, 不得静默成功。"""
        claim = _claim_task(db, env["task"].id)
        assert claim is not None

        with pytest.raises((AppError, ValueError, TypeError)):
            worker_app.complete_task(db, env["task"].id)
        db.rollback()
        view = _task_view(db, env["task"].id)
        assert view is not None and view.business_outcome != "normal_completion", view


# ---------------------------------------------------------------------------
# 契约组三: report 检查由 application.worker 用例完成
# ---------------------------------------------------------------------------


def _divergent_evidence_payload() -> dict[str, Any]:
    """分歧证据: 载荷自带全通过评估, 但内容空无可评估依据。

    Worker 本地解释(透传载荷评估)与 results 公开能力(评估内容文档)在此分歧:
    前者判全通过, 后者判证据不足。纠偏后 Worker 必须以后者为准。
    """
    return {
        "result_kind": "external_check",
        "content": {},
        "assessment": {
            "dimension_physical": "pass",
            "dimension_optimality": "pass",
            "dimension_financial": "pass",
            "dimension_reliability": "pass",
        },
    }


class TestReportCheckDelegatedToResultsCapability:
    """report 检查的证据解释/评分/业务 outcome 归 results 公开能力所有。

    Worker 只传递不可变输入并消费显式结果契约, 不在本地复制规则。
    纠偏前 Worker 本地透传载荷评估并自算综合得分, 以下断言以违规形式失败。
    """

    def test_report_verdict_matches_results_capability(self, db: Session, env: dict[str, Any]):
        payload = _divergent_evidence_payload()
        package = _store_evidence(db, env, payload)
        task = _add_task(db, env, "report")
        claim = _claim_task(db, task.id)

        status = runner.run_task(db, claim, worker_id="w-report", isolate=False)

        # 证据不足是显式、合法的业务结局, 不得判成功。
        assert status == "completed", status
        view = _task_view(db, task.id)
        assert view is not None and view.status == "completed", view
        assert view.business_outcome == "insufficient_evidence", view.business_outcome
        # 落库评估必须与公开能力对同一证据的裁决一致(维度/得分/规则版本)。
        persisted = latest_assessment(db, package.id)
        assert persisted is not None and persisted.assessor == "system", persisted
        draft = evaluate_evidence(evidence_inner(payload), evidence_status=package.status)
        assert persisted.dimension_physical == draft.dimensions["physical"], persisted
        assert persisted.dimension_optimality == draft.dimensions["optimality"], persisted
        assert persisted.dimension_financial == draft.dimensions["financial"], persisted
        assert persisted.dimension_reliability == draft.dimensions["reliability"], persisted
        assert persisted.overall_score == draft.overall_score, persisted
        assert (persisted.detail or {}).get("definition_version") == ASSESSMENT_RULE_VERSION, (
            persisted.detail
        )


class TestReportNoEvidencePath:
    """回归: 项目无证据包时 report 显式判证据不足(纠偏前后均成立)。"""

    def test_report_without_evidence_is_insufficient(self, db: Session, env: dict[str, Any]):
        task = _add_task(db, env, "report")
        claim = _claim_task(db, task.id)

        status = runner.run_task(db, claim, worker_id="w-report", isolate=False)

        assert status == "completed", status
        view = _task_view(db, task.id)
        assert view is not None and view.status == "completed", view
        assert view.business_outcome == "insufficient_evidence", view.business_outcome


# ---------------------------------------------------------------------------
# 回归组: 运行编排(领取/租约/取消/收拢)不受本轮纠偏破坏
# ---------------------------------------------------------------------------


class TestWorkerExecutionGuards:
    """权限与错误语义回归: 非持有者写回被拒、取消信号中止执行。"""

    def test_expired_lease_rejects_run(self, db: Session, env: dict[str, Any]):
        """租约过期后执行: 迟到写回整笔拒绝, 任务保持 running。"""
        task = _add_task(db, env, "report")
        claim = _claim_task(db, task.id)
        row = db.execute(
            select(TaskLease).where(TaskLease.attempt_id == claim.attempt_id)
        ).scalars().first()
        assert row is not None
        row.status = "expired"  # 模拟守护进程过期回收
        db.commit()

        status = runner.run_task(db, claim, worker_id="w-report", isolate=False)

        assert status == "lease_rejected", status
        assert worker_app.verify_lease(db, claim.attempt_id, claim.lease_token) is None
        view = _task_view(db, task.id)
        assert view is not None and view.status == "running", view

    def test_cancel_signal_closes_run_as_cancelled(self, db: Session, env: dict[str, Any]):
        """取消信号 + cancelling 状态下执行: 收拢为 cancelled。"""
        task = _add_task(db, env, "report")
        claim = _claim_task(db, task.id)
        task.status = "cancelling"  # 模拟 API 已发起取消(权威状态变更)
        queue.set_cancel(task.id, "test-cancel")
        db.commit()

        status = runner.run_task(db, claim, worker_id="w-report", isolate=False)

        assert status == "cancelled", status
        view = _task_view(db, task.id)
        assert view is not None and view.status == "cancelled", view
        assert queue.get_cancel(task.id) is None  # 取消信号已清除
