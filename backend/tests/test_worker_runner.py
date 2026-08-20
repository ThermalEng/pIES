"""Worker 执行器与隔离进程测试: 迷你快照 → 结果对象 → 业务结局。

- 执行器单测(runner.run_task, isolate=False 进程内引擎): 方案评价/规划/
  不确定性/结果检查/取消检查点;
- 隔离求解器子进程(solver_process): 往返序列化/超时终止/内存限制;
- 数据库: SQLite :memory:(StaticPool); 队列: 内存后端; 对象存储: tmp_path。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")

import pytest  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from worker_testkit import setup_environment  # noqa: E402

from iesplan.db import Base  # noqa: E402
from iesplan.models.calc import Task, TaskAttempt  # noqa: E402
from iesplan.models.result import EvidencePackage, ResultAssessment, ResultIndex  # noqa: E402
from iesplan.models.uncertainty import SampleRecord, SampleTask, UncertaintySnapshot  # noqa: E402
from iesplan.services import objects, queue  # noqa: E402
from iesplan.worker import lease, runner  # noqa: E402
from iesplan.worker.solver_process import run_solver_isolated  # noqa: E402

# ---------------------------------------------------------------------------
# 测试环境
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """模块级 SQLite 内存引擎(StaticPool: 所有会话共享同一连接)。"""
    # 计算引擎命令注册(模拟 worker 启动, 03 §9.3; 测试直接跑 runner 不经过
    # worker/main.py 的启动注册流程, 必须自包含注册, 避免全量顺序依赖)
    from iesplan.modeling.command import init_compute_commands

    init_compute_commands()
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
    # 恢复 setup_environment 改动的全局设置(阈值/数据目录),
    # 避免污染后续测试文件(如 test_objects_api 的容量检查)
    from iesplan.config import settings

    settings.storage_min_free_bytes = 2_000_000_000
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def db(engine: Engine) -> Iterator[Session]:
    """函数级共享会话。"""
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _claim_and_run(db: Session, task_id: int, *, worker_id: str = "cw-test") -> str:
    """领取并完整执行一个任务(runner.run_task), 返回终态。"""
    claim = lease.acquire_attempt(db, task_id, worker_id)
    assert claim is not None
    db.commit()
    return runner.run_task(db, claim, worker_id=worker_id, isolate=False)


def _evidence_payload(db: Session, task_id: int) -> dict:
    """读取任务最新证据包内容(JSON)。"""
    package = db.execute(
        select(EvidencePackage).where(EvidencePackage.task_id == task_id)
        .order_by(EvidencePackage.id.desc())
    ).scalars().first()
    assert package is not None, "证据包不存在"
    raw = objects.get_object(db, package.object_id)
    return json.loads(raw.decode("utf-8"))


def _assert_completed(db: Session, task_id: int, outcome: str) -> Task:
    """断言任务终态 + 业务结局。"""
    task = db.get(Task, task_id)
    assert task.status == "completed"
    assert task.business_outcome == outcome
    return task


# ---------------------------------------------------------------------------
# 方案评价(task_type=calc)
# ---------------------------------------------------------------------------


class TestCalc:
    """方案评价: 迷你快照 → 逐时结果对象 → KPI → 业务结局。"""

    def test_run_calc_ok(self, db: Session, tmp_path: Path):
        env = setup_environment(db, tmp_path, task_type="calc")
        status = _claim_and_run(db, env["task"].id)
        assert status == "completed"
        _assert_completed(db, env["task"].id, "normal_completion")

        payload = _evidence_payload(db, env["task"].id)
        assert payload["result_kind"] == "eval_result"
        assert payload["status"] == "ok"
        assert payload["solver_status"] == "OPTIMAL"
        # 逐时流字段存在(02 §8.1 命名)且电平衡成立
        assert "p_grid_buy" in payload["flows"]
        assert len(payload["flows"]["p_grid_buy"]) == 4
        # KPI: 金额 Decimal → str 保精度
        assert float(payload["kpi"]["total_op_cost"]) > 0
        # 四维评估
        assert payload["assessment"]["dimension_physical"] == "pass"
        assert payload["assessment"]["dimension_optimality"] == "pass"
        # 证据包/评估/结果索引落库
        package = db.execute(select(EvidencePackage)).scalars().first()
        assessment = db.execute(select(ResultAssessment)).scalars().first()
        index = db.execute(select(ResultIndex)).scalars().first()
        assert package.task_id == env["task"].id
        assert package.calc_snapshot_id == env["snapshot"].id
        assert assessment.assessor == "system"
        assert index.is_latest is True
        # 尝试 succeeded
        attempt = db.execute(
            select(TaskAttempt).where(TaskAttempt.task_id == env["task"].id)
        ).scalars().first()
        assert attempt.status == "succeeded"

    def test_run_calc_infeasible_outcome(self, db: Session, tmp_path: Path):
        """无可行解(电网购电上限 0): 业务结局 no_recommendation(03 §3.2 表)。"""
        grid_only = [
            {
                "id": 1, "device_type": "ies.device.grid_connection", "kind": "existing",
                "name": "电网", "params": {"max_import_power_kw": 0, "max_export_power_kw": 0},
            },
        ]
        env = setup_environment(db, tmp_path, task_type="calc", devices=grid_only)
        status = _claim_and_run(db, env["task"].id)
        assert status == "completed"
        task = _assert_completed(db, env["task"].id, "no_recommendation")
        assert task.business_outcome == "no_recommendation"
        payload = _evidence_payload(db, env["task"].id)
        assert payload["status"] == "infeasible"
        assert payload["assessment"]["dimension_physical"] == "fail"

    def test_run_calc_snapshot_missing_fails(self, db: Session, tmp_path: Path):
        """快照缺失: blocking 失败 + insufficient_evidence(03 §6.3)。"""
        env = setup_environment(db, tmp_path, task_type="calc")
        task = env["task"]
        task.calc_snapshot_id = None  # 破坏快照绑定(应用层校验之外)
        db.commit()
        status = _claim_and_run(db, task.id)
        assert status == "failed"
        task = db.get(Task, task.id)
        assert task.business_outcome == "insufficient_evidence"
        assert task.attempt_count == 1


# ---------------------------------------------------------------------------
# 规划(task_type=optimization)
# ---------------------------------------------------------------------------


class TestPlan:
    """规划: planning 引擎 → 候选列表 → IRR/NPV → 候选对象。"""

    def test_run_plan_with_candidates(self, db: Session, tmp_path: Path):
        devices = [
            {
                "id": 1, "device_type": "ies.device.grid_connection", "kind": "existing",
                "name": "电网",
                "params": {"max_import_power_kw": 5000, "max_export_power_kw": 0,
                           "export_tariff": 0.35},
            },
            {
                "id": 3, "device_type": "ies.device.pv", "kind": "new",
                "name": "新增光伏",
                "params": {"max_capacity_kwp": 2, "unit_invest_cost": 10,
                           "rated_capacity_kwp": 0, "efficiency": 0.2, "tilt_deg": 30,
                           "azimuth_deg": 180},
            },
        ]
        config = {
            "planning_options": {
                "irr_floor": 0.0,          # 测试算例放宽 IRR 硬约束
                "max_combinations": 10,
                "timeout_per_eval": 20.0,
                "grid_step": {"ies.device.pv": 1.0},
            },
            "seed": 7,
        }
        env = setup_environment(db, tmp_path, task_type="optimization", config=config,
                                devices=devices)
        status = _claim_and_run(db, env["task"].id)
        assert status == "completed"
        _assert_completed(db, env["task"].id, "normal_completion")

        payload = _evidence_payload(db, env["task"].id)
        assert payload["result_kind"] == "planning_result"
        assert payload["solver_status"] == "OPTIMAL"
        assert payload["baseline_cost"] is not None
        assert len(payload["candidates"]) >= 1
        best = payload["best"]
        assert best is not None
        assert best["capacities"]["ies.device.pv"] > 0
        assert best["irr"] is not None and best["irr"] >= 0.0
        assert best["npv"] is not None
        assert best["capex"] > 0
        assert payload["assessment"]["dimension_financial"] == "pass"

    def test_run_plan_no_candidate_outcome(self, db: Session, tmp_path: Path):
        """全部候选被 IRR 硬约束过滤: 业务结局 no_recommendation。"""
        devices = [
            {
                "id": 1, "device_type": "ies.device.grid_connection", "kind": "existing",
                "name": "电网",
                "params": {"max_import_power_kw": 5000, "max_export_power_kw": 0},
            },
            {
                "id": 3, "device_type": "ies.device.pv", "kind": "new",
                "name": "新增光伏",
                "params": {"max_capacity_kwp": 2, "unit_invest_cost": 1_000_000,
                           "rated_capacity_kwp": 0},
            },
        ]
        config = {
            "planning_options": {"irr_floor": 0.08, "max_combinations": 10,
                                 "timeout_per_eval": 20.0,
                                 "grid_step": {"ies.device.pv": 1.0}},
        }
        env = setup_environment(db, tmp_path, task_type="optimization", config=config,
                                devices=devices)
        status = _claim_and_run(db, env["task"].id)
        assert status == "completed"
        _assert_completed(db, env["task"].id, "no_recommendation")
        payload = _evidence_payload(db, env["task"].id)
        assert payload["candidates"] == []


# ---------------------------------------------------------------------------
# 不确定性(task_type=uncertainty)
# ---------------------------------------------------------------------------


class TestUncertainty:
    """不确定性: 父任务样本子任务顺序执行(固定方案可靠性)。"""

    def test_run_uncertainty_fixed_reliability(self, db: Session, tmp_path: Path):
        config = {
            "n_samples": 3,
            "method": "monte_carlo",
            "mode": "fixed_reliability",
            "distributions": {"e_load": {"kind": "normal", "sigma": 0.05}},
        }
        env = setup_environment(db, tmp_path, task_type="uncertainty", config=config)
        status = _claim_and_run(db, env["task"].id)
        assert status == "completed"
        _assert_completed(db, env["task"].id, "normal_completion")

        payload = _evidence_payload(db, env["task"].id)
        assert payload["result_kind"] == "uncertainty_result"
        assert payload["stats"]["valid"] == 3
        assert payload["stats"]["total"] == 3
        assert payload["assessment"]["dimension_reliability"] == "pass"
        # 样本簿记: uncertainty_snapshots + sample_tasks + sample_records
        unc = db.execute(select(UncertaintySnapshot)).scalars().first()
        assert unc is not None and unc.n_samples == 3
        samples = db.execute(select(SampleTask)).scalars().all()
        assert len(samples) == 3
        assert all(s.status == "completed" for s in samples)
        assert all(s.parent_task_id == env["task"].id for s in samples)
        records = db.execute(select(SampleRecord)).scalars().all()
        assert len(records) >= 3  # 每个有效样本至少一条 annual_op_cost
        names = {r.variable_name for r in records}
        assert "annual_op_cost" in names

    def test_run_uncertainty_replan_sensitivity(self, db: Session, tmp_path: Path):
        """重规划敏感性: 每个样本重新优化容量(样本含 irr/capex 指标)。"""
        config = {
            "n_samples": 2,
            "method": "scenario",
            "mode": "replan_sensitivity",
            "distributions": {"e_load": {"kind": "scenario", "multipliers": [0.9, 1.1]}},
            "planning_options": {"irr_floor": 0.0, "max_combinations": 10,
                                 "timeout_per_eval": 20.0,
                                 "grid_step": {"ies.device.pv": 1.0}},
        }
        devices = [
            {
                "id": 1, "device_type": "ies.device.grid_connection", "kind": "existing",
                "name": "电网",
                "params": {"max_import_power_kw": 5000, "max_export_power_kw": 0},
            },
            {
                "id": 3, "device_type": "ies.device.pv", "kind": "new",
                "name": "新增光伏",
                "params": {"max_capacity_kwp": 2, "unit_invest_cost": 10,
                           "rated_capacity_kwp": 0},
            },
        ]
        env = setup_environment(db, tmp_path, task_type="uncertainty", config=config,
                                devices=devices)
        status = _claim_and_run(db, env["task"].id)
        assert status == "completed"
        task = _assert_completed(db, env["task"].id, "normal_completion")
        assert task.business_outcome == "normal_completion"
        payload = _evidence_payload(db, env["task"].id)
        assert payload["mode"] == "replan_sensitivity"
        assert payload["stats"]["valid"] == 2
        irrs = [s["metric"].get("irr") for s in payload["samples"]]
        assert any(v is not None for v in irrs)


# ---------------------------------------------------------------------------
# 结果检查(task_type=report)与取消
# ---------------------------------------------------------------------------


class TestCheckAndCancel:
    """结果检查(四维评估追加)与取消检查点。"""

    def test_run_report_check(self, db: Session, tmp_path: Path):
        env = setup_environment(db, tmp_path, task_type="calc")
        _claim_and_run(db, env["task"].id)
        # 创建 report 任务(io 队列), 检查项目最新证据包
        report_task = Task(
            project_id=env["project"].id, type="report", status="queued",
            requested_by=env["user"].id,
        )
        db.add(report_task)
        db.flush()
        queue.enqueue(report_task.id, "io", task_type="report")
        db.commit()
        status = _claim_and_run(db, report_task.id)
        assert status == "completed"
        _assert_completed(db, report_task.id, "normal_completion")
        # 追加了新的系统评估记录(不覆盖原记录, RPD 11.2)
        assessments = db.execute(
            select(ResultAssessment).order_by(ResultAssessment.id)
        ).scalars().all()
        assert len(assessments) == 2
        assert all(a.assessor == "system" for a in assessments)
        # result_index.assessment_id 挂接最新评估
        index = db.execute(select(ResultIndex)).scalars().first()
        assert index.assessment_id == assessments[-1].id

    def test_cancel_checkpoint(self, db: Session, tmp_path: Path):
        """取消检查点(03 §6.1): cancelling + 取消信号 → 执行器中止 → 收拢。"""
        env = setup_environment(db, tmp_path, task_type="calc")
        claim = lease.acquire_attempt(db, env["task"].id, "cw-test")
        assert claim is not None
        db.commit()
        # 模拟 API 取消: 权威状态 cancelling + 广播取消信号
        task = env["task"]
        task.status = "cancelling"
        queue.set_cancel(task.id, "user_cancel")
        db.commit()
        status = runner.run_task(db, claim, worker_id="cw-test", isolate=False)
        assert status == "cancelled"
        task = db.get(Task, env["task"].id)
        assert task.status == "cancelled"
        attempt = db.execute(
            select(TaskAttempt).where(TaskAttempt.task_id == env["task"].id)
        ).scalars().first()
        assert attempt.status == "stopped"
        assert attempt.stop_reason == "cancelled"
        # 未产出证据包
        assert db.execute(select(EvidencePackage)).scalars().first() is None


# ---------------------------------------------------------------------------
# 隔离求解器子进程(solver_process)
# ---------------------------------------------------------------------------


class TestSolverProcess:
    """隔离子进程: 序列化往返 / 超时终止 / 内存限制(03 §9.4)。"""

    def test_round_trip(self):
        resp = run_solver_isolated(
            "iesplan.worker.solver_process._identity", ({"a": [1, 2, 3], "b": "x"},),
            timeout_sec=30.0,
        )
        assert resp["ok"] is True
        assert resp["result"] == {"a": [1, 2, 3], "b": "x"}
        assert resp["elapsed_s"] < 30.0

    def test_timeout_terminates(self):
        """超时 → SIGTERM → SIGKILL → 清理孤儿; 返回 timed_out(不悬挂)。"""
        resp = run_solver_isolated(
            "iesplan.worker.solver_process._sleep", (300.0,), timeout_sec=1.5,
        )
        assert resp["ok"] is False
        assert resp["timed_out"] is True
        assert resp["elapsed_s"] < 30.0  # 及时返回, 不等待完整 300 s

    def test_mem_limit(self):
        """内存限制(RLIMIT_AS): 超额分配 → MemoryError 返回失败。"""
        resp = run_solver_isolated(
            "iesplan.worker.solver_process._allocate_memory", (300.0,),
            timeout_sec=30.0, mem_limit_mb=96,
        )
        assert resp["ok"] is False
        assert resp["timed_out"] is False
        assert "MemoryError" in resp["error"]

    def test_cancel_event(self):
        """取消事件: 置位后立即终止子进程。"""
        import threading

        cancel = threading.Event()

        def _cancel_later() -> None:
            import time

            time.sleep(0.8)
            cancel.set()

        t = threading.Thread(target=_cancel_later, daemon=True)
        t.start()
        resp = run_solver_isolated(
            "iesplan.worker.solver_process._sleep", (120.0,), timeout_sec=60.0,
            cancel_event=cancel,
        )
        assert resp["ok"] is False
        assert resp["canceled"] is True

    def test_in_process_executor_path(self, db: Session, tmp_path: Path):
        """isolate=False(进程内引擎)与隔离路径产出一致(测试/降级双路径)。"""
        env = setup_environment(db, tmp_path, task_type="calc")
        _claim_and_run(db, env["task"].id)
        payload = _evidence_payload(db, env["task"].id)
        assert payload["status"] == "ok"
