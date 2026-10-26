"""计算 Worker 真实链端到端(B4)：API 提交闸门 → 领取 → 真实执行 → 证据封存。

- 数据库: SQLite :memory:(StaticPool 共享连接);
- 队列: IESPLAN_QUEUE=memory 内存后端;
- 执行: 组合根真实装配(assemble_compute_worker: 生成器/执行器/适配器
  目录 + Settings 派生策略 + 通用运行时) + 镜像内 HiGHS 适配 executable。
提交走任务 API 真实闸门(装配校验/冻结/引用解析)，不手造快照行。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")

import pytest  # noqa: E402
from assembly_fixture import declare_compute_device  # noqa: E402
from auth_helpers import make_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from test_tasks_api import (  # noqa: E402
    _CONFIG_BODY,
    _bind_and_freeze,
    _create_project,
    _declare_assembly_sections,
    _declare_finance,
    _h,
    _set_explicit_calc,
    _submit_task,
)

from iesplan.api import projects as projects_api  # noqa: E402
from iesplan.api import tasks as tasks_api  # noqa: E402
from iesplan.api.config import config_router  # noqa: E402
from iesplan.api.deps import get_request_db  # noqa: E402
from iesplan.application import worker as worker_app  # noqa: E402
from iesplan.bootstrap import assemble_compute_worker  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.results import persistence as results_store  # noqa: E402
from iesplan.storage import get_object, list_refs  # noqa: E402
from iesplan.storage.persistence import StoredObject  # noqa: E402
from iesplan.tasks import queue  # noqa: E402
from iesplan.tasks.persistence import Task, TaskDiagnostic, TaskProgress  # noqa: E402
from iesplan.worker import runner  # noqa: E402

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
def _clean_state(engine: Engine) -> Iterator[None]:
    """每个测试前重置内存队列, 结束后清空全部表(避免测试间串扰)。"""
    queue.force_memory()
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def db(engine: Engine) -> Iterator[Session]:
    """函数级共享会话(服务与测试共用, 端点内 commit)。"""
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


@pytest.fixture()
def client(engine: Engine, db: Session, tmp_path: Path) -> Iterator[TestClient]:
    """测试客户端: 挂载项目+任务路由, 替换 get_request_db 依赖, 对象存储指向临时目录。"""
    from iesplan.config import settings  # noqa: E402

    settings.data_dir = tmp_path

    app = create_app()
    app.include_router(projects_api.router)
    app.include_router(tasks_api.router)
    app.include_router(config_router)

    def _override_request_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_request_db] = _override_request_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture()
def plane():
    """真实计算执行面(组合根装配，与 Worker 进程同一构成)。"""
    ctx = assemble_compute_worker()
    plane = ctx.compute_execution_plane()
    assert plane is not None
    return plane


def _save_config_body(client: TestClient, pid: int, user, body: dict) -> int:
    """保存一份计算配置正文, 返回 revision。"""
    resp = client.put(
        f"/api/projects/{pid}/config",
        json={"calculation_config": body, "expected_revision": None},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def _prepare_with_planning(client: TestClient, db: Session, user, planning_body: dict) -> int:
    """准备可提交计算任务的项目（规划变量目标可定制，电池容量保持固定）。

    步骤同 test_tasks_api._prepare_project：建项目 + 显式财务/装配声明 +
    显式计算声明 + 固化版本；规划修订由调用方给定（额定功率类仿射界
    接管可降，容量变量会引入除法不支持）。
    """
    from iesplan.application import configuration as config_cases  # noqa: E402

    pid = _create_project(client, user, "链测试项目")
    _declare_finance(db, user, pid)
    device_ref, revision = declare_compute_device(client, pid, _h(client, user))
    config_cases.save_planning_config(db, pid, planning_body, None, user.id)
    revision = _set_explicit_calc(client, pid, user, revision)
    revision = _declare_assembly_sections(client, pid, user, revision, {device_ref: {"type": "none"}})
    _bind_and_freeze(client, pid, user, None, revision=revision)
    return pid


def _submit_calc_task(client: TestClient, db: Session, config_body: dict, planning_body: dict) -> int:
    """准备项目→保存配置→提交 optimization 任务，返回 task_id。"""
    user = make_user(db, "chain_owner")
    pid = _prepare_with_planning(client, db, user, planning_body)
    rev = _save_config_body(client, pid, user, config_body)
    status, body = _submit_task(
        client, pid, user, config={"config_revision": rev}, idempotency_key="chain-e2e-1"
    )
    assert status == 201, body
    return int(body["task"]["id"])


#: 规划体：目标常量 + 电池额定功率规划变量（仿射界接管）。
#: device_ref 取装配实例引用（夹具电池名为 bat1）。
PLANNING_BODY = {
    "objective": {"sense": "minimize", "expression": "0"},
    "variables": {
        "cap": {
            "device_ref": "bat1",
            "parameter": "rated_power_kw",
            "lower_bound": "0",
            "upper_bound": "100",
            "unit": "kW",
        },
    },
    "constraints": {},
}

#: 税后规划体：同一电池变量，目标为显式税后 NPV（C1b 装配闸门识别）。
TAX_PLANNING_BODY = {
    "objective": {"sense": "maximize", "expression": "system.after_tax_npv"},
    "variables": {
        "cap": {
            "device_ref": "bat1",
            "parameter": "rated_power_kw",
            "lower_bound": "0",
            "upper_bound": "100",
            "unit": "kW",
        },
    },
    "constraints": {},
}


def _prepare_tax_loop(client: TestClient, db: Session, user) -> int:
    """含税则闭环项目准备：电池 + 税后规划 + 含税画像 + 冻结（返回 pid）。

    顺序沿任务 API 税则用例（画像切换后重存规划），装配/冻结复用既有
    链 helper；评价确认走提交默认块（K=0/E=1/N=1）。
    """
    from test_config_revisions import _tax_profile_payload  # noqa: E402

    from iesplan.application import configuration as config_cases  # noqa: E402

    pid = _create_project(client, user, "链税务测试项目")
    _declare_finance(db, user, pid)
    device_ref, revision = declare_compute_device(client, pid, _h(client, user))
    config_cases.save_planning_config(db, pid, dict(TAX_PLANNING_BODY), None, user.id)
    revision = _set_explicit_calc(client, pid, user, revision)
    revision = _declare_assembly_sections(client, pid, user, revision, {device_ref: {"type": "none"}})
    _bind_and_freeze(client, pid, user, None, revision=revision)
    tax_payload = _tax_profile_payload()
    config_cases.register_finance_profile(db, dict(tax_payload), user.id)
    config_cases.set_project_finance_profile(db, pid, "cn-tax-demo", user.id)
    config_cases.save_planning_config(db, pid, dict(TAX_PLANNING_BODY), None, user.id)
    return pid


def _submit_tax_calc_task(client: TestClient, db: Session) -> int:
    """含税则项目 → 保存配置 → 真实提交，返回 task_id（期望 201）。"""
    user = make_user(db, "chain_tax_owner")
    pid = _prepare_tax_loop(client, db, user)
    rev = _save_config_body(client, pid, user, dict(_CONFIG_BODY))
    status, body = _submit_task(
        client, pid, user, config={"config_revision": rev}, idempotency_key="chain-tax-1"
    )
    assert status == 201, body
    return int(body["task"]["id"])


def _claim(db: Session, task_id: int) -> worker_app.Claim:
    """领取任务并提交事务(模拟 Daemon 领取动作)。"""
    claim = worker_app.acquire_attempt(db, task_id, "cw-e2e")
    assert claim is not None
    db.commit()
    return claim


def _run(engine: Engine, claim: worker_app.Claim, plane) -> str:
    """经 worker.runner 跑真实链（与 Daemon 同一调用形状，各阶段开短会话）。"""

    def factory() -> Session:
        return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()

    return runner.run_task(factory, claim, worker_id="cw-e2e", compute=plane)


class _StubRuntime:
    """桩运行时：返回预设状态的停止回执（只覆盖 fenced/cancelled 分流）。"""

    def __init__(self, status: str) -> None:
        self._status = status

    def run(self, bundle, attempt, *, stop=None):
        from iesplan.computation import ExecutionReceipt  # noqa: E402

        return (
            ExecutionReceipt(
                receipt_id=f"stub-{self._status}",
                bundle_id=attempt.bundle_id,
                attempt_id=attempt.attempt_id,
                assembly_id=attempt.assembly_id,
                calculation_config_id=attempt.calculation_config_id,
                generator_ref="ies.generator.highs_milp@5.0.0",
                solver_ref="ies.solver.highs@1.7.2",
                executor_ref="ies.executor.local_process@1.0.0",
                executable_ref="ies.solver.highs@1.7.2",
                result_adapter_ref="ies.result-adapter.highs@5.0.0",
                status=self._status,
                termination_reason=self._status,
            ),
            {},
        )


def _plane_with_stub_runtime(plane, status: str):
    """复用真实目录，仅把运行时换成预设停止回执的桩。"""
    return worker_app.ComputeExecutionPlane(
        generator_registry=plane.generator_registry,
        executor_registry=plane.executor_registry,
        adapter_registry=plane.adapter_registry,
        runtime=_StubRuntime(status),
        predictor_registry=plane.predictor_registry,
    )


# ---------------------------------------------------------------------------
# 真实链
# ---------------------------------------------------------------------------


class TestComputeChainEndToEnd:
    def test_real_higs_task_completes_with_sealed_evidence(
        self, client: TestClient, db: Session, engine: Engine, plane
    ) -> None:
        """HiGHS 实任务：提交 → 领取 → 真实求解 → completed + 证据封存。

        断言：终态 completed、业务结局正常完成、统一结果 solved、回执
        succeeded 且 executable 为冻结精确引用；Bundle/声明输出字节以
        存储对象无损封存并可按描述子取回；Bundle 使用快照冻结的有效种子
        （配置 seed 缺省时取 snapshot.random_seed，不静默用 0）；回执身份
        追溯真实装配 id 与配置 revision。
        """
        task_id = _submit_calc_task(client, db, dict(_CONFIG_BODY), dict(PLANNING_BODY))
        claim = _claim(db, task_id)
        assert _run(engine, claim, plane) == "completed"

        task = db.execute(select(Task).where(Task.id == task_id)).scalar_one()
        assert task.status == "completed"
        assert task.business_outcome == "normal_completion"

        evidence = results_store.latest_evidence_for_task(db, task_id)
        assert evidence is not None
        sealed = json.loads(get_object(db, evidence.object_id).decode("utf-8"))
        assert sealed["outcome"] == "normal_completion"
        assert sealed["compute_result"]["execution_status"] == "completed"
        assert sealed["compute_result"]["math_status"] == "solved"
        assert sealed["compute_result"]["receipt_id"] == sealed["receipt"]["receipt_id"]
        assert sealed["receipt"]["status"] == "succeeded"
        assert sealed["receipt"]["executable_ref"] == "ies.solver.highs@1.7.2"
        assert sealed["bundle_manifest"]["bundle"]["generator"] == ("ies.generator.highs_milp@5.0.0")
        snapshot = worker_app.get_snapshot_record(db, task.calc_snapshot_id)
        assert snapshot is not None
        args = sealed["bundle_manifest"]["command"]["arguments"]
        assert str(snapshot.random_seed) in [str(a) for a in args]
        assert sealed["receipt"]["executor_ref"] == snapshot.executor
        assert sealed["receipt"]["result_adapter_ref"] == snapshot.result_adapter
        assert sealed["receipt"]["bundle_id"] == f"snapshot-{snapshot.id}:attempt-{claim.attempt_id}"
        assert sealed["receipt"]["assembly_id"] != ""
        assert sealed["receipt"]["calculation_config_id"].startswith("project-")

        solution = sealed["solution"]
        assert set(solution) == {"output/solution.sol"}
        entry = solution["output/solution.sol"]
        raw = get_object(db, entry["object_id"])
        assert raw.startswith(b"HIGHS_SOLUTION_V1")
        assert entry["size_bytes"] == len(raw)
        for _path, descriptor in sorted(sealed["bundle_files"].items()):
            assert get_object(db, descriptor["object_id"]) is not None

    def test_tax_task_completes_with_sealed_financial_evaluation(
        self, client: TestClient, db: Session, engine: Engine, plane
    ) -> None:
        """含税则闭环(C3)：提交 201 → 真实求解 → 封存 financial_evaluation。

        同任务/attempt/快照身份；税后 CF/NPV 与求解目标同源（评价 NPV ==
        同次解目标值）；公开事实形状符合映射表；无交易税基的税额关系
        强制成立（VAT 全零、所得税零税/亏损恒等），不手填电池调度值。
        """
        task_id = _submit_tax_calc_task(client, db)
        claim = _claim(db, task_id)
        assert _run(engine, claim, plane) == "completed"

        task = db.execute(select(Task).where(Task.id == task_id)).scalar_one()
        assert task.status == "completed"
        assert task.business_outcome == "normal_completion"
        snapshot = worker_app.get_snapshot_record(db, task.calc_snapshot_id)
        assert snapshot is not None
        assert snapshot.result_adapter == "ies.result-adapter.highs@5.0.0"

        evidence = results_store.latest_evidence_for_task(db, task_id)
        assert evidence is not None
        sealed = json.loads(get_object(db, evidence.object_id).decode("utf-8"))
        assert sealed["outcome"] == "normal_completion"
        assert sealed["compute_result"]["math_status"] == "solved"
        assert sealed["receipt"]["bundle_id"] == f"snapshot-{snapshot.id}:attempt-{claim.attempt_id}"
        assert sealed["bundle_manifest"]["bundle"]["generator"] == "ies.generator.highs_milp@5.0.0"

        fin = sealed["financial_evaluation"]
        assert fin["schema"] == "ies.financial-evaluation"
        assert fin["schema_version"] == "2.0.0"
        assert fin["currency"] == "CNY"
        assert set(fin) == {
            "schema", "schema_version", "status", "evaluation_years",
            "construction_period", "operation_years", "currency", "price_basis",
            "base_year", "profile_id", "discount_rate", "construction_total",
            "construction_total_unit", "annual_fixed_om", "payments_applied",
            "annual_operation", "monthly_demand", "tax_cash", "annual_net_flows", "evaluation",
        }
        # K=0/E=1/N=1：单池 12 月 VAT 全零（无计费绑定，留抵恒零强制成立）。
        vat = fin["tax_cash"]["vat"]
        assert len(vat) == 12
        for row in vat:
            assert row["pool"] == "main_pool"
            for key in ("paid", "refund_claimed", "refund_arrived", "carry"):
                assert float(row[key]) == pytest.approx(0.0, abs=1e-9)
        # 所得税单规则单年：无利润故税为零；负税基全部转为新生亏损。
        income = fin["tax_cash"]["income"]
        assert [(row["rule"], row["year"]) for row in income] == [("income", 1)]
        only = income[0]
        assert float(only["tax"]) == pytest.approx(0.0, abs=1e-9)
        assert float(only["loss_used"]) == pytest.approx(0.0, abs=1e-9)
        assert float(only["loss_expired"]) == pytest.approx(0.0, abs=1e-9)
        assert float(only["taxable_base"]) <= 0.0
        assert float(only["loss_generated"]) == pytest.approx(
            -float(only["taxable_base"]), rel=1e-6
        )
        assert len(fin["annual_net_flows"]) == 2
        # 评价 NPV 与同次解目标值同源一致（同一模型现金表达式）。
        objective = sealed["compute_result"]["outputs"]["solution"]["objective_value"]
        assert float(fin["evaluation"]["npv"]) == pytest.approx(float(objective), rel=1e-6)

    def test_prediction_required_fails_with_clear_reason(
        self, client: TestClient, db: Session
    ) -> None:
        """预测需求明确拒绝: 无装配绑定的 predictions → 提交即 400，不建任务。

        使用生产 naive 精确引用以通过保存入口参数规则；项目无 data_predict
        绑定，提交闸门按 TASK-REQ-010 拒绝。
        """
        from iesplan.computation import NAIVE_PREDICTOR_REF  # noqa: E402

        body = dict(_CONFIG_BODY)
        body["predictions"] = [
            {
                "target": {"instance": "d1", "interface": "e_load"},
                "algorithm": NAIVE_PREDICTOR_REF,
                "parameters": {},
            }
        ]
        user = make_user(db, "chain_owner_pred")
        pid = _prepare_with_planning(client, db, user, dict(PLANNING_BODY))
        rev = _save_config_body(client, pid, user, body)
        status, resp = _submit_task(
            client, pid, user, config={"config_revision": rev},
            idempotency_key="chain-pred-reject-1",
        )
        assert status == 400, resp
        assert resp["error"]["code"] == "TASK-REQ-010"

    def test_frozen_provider_mismatch_fails_clearly(
        self, client: TestClient, db: Session, engine: Engine
    ) -> None:
        """冻结依赖不可解析明确失败：执行器/适配器目录为空时，求解前即拒绝。"""
        from iesplan.bootstrap import _deployment_policy_from_settings  # noqa: E402
        from iesplan.computation import (  # noqa: E402
            NAIVE_PREDICTOR,
            SolverRuntime,
            build_executor_registry,
            build_predictor_registry,
            build_result_adapter_registry,
        )
        from iesplan.computation.generators import (  # noqa: E402
            HIGHS_MILP_PROVIDER,  # noqa: E402
            build_registry,  # noqa: E402
        )

        task_id = _submit_calc_task(client, db, dict(_CONFIG_BODY), dict(PLANNING_BODY))
        claim = _claim(db, task_id)
        executor_registry = build_executor_registry(())
        plane = worker_app.ComputeExecutionPlane(
            generator_registry=build_registry((HIGHS_MILP_PROVIDER,)),
            executor_registry=executor_registry,
            adapter_registry=build_result_adapter_registry([]),
            runtime=SolverRuntime(executor_registry, _deployment_policy_from_settings()),
            predictor_registry=build_predictor_registry((NAIVE_PREDICTOR,)),
        )
        assert _run(engine, claim, plane) == "failed"
        diagnostics = (
            db.execute(select(TaskDiagnostic).where(TaskDiagnostic.task_id == task_id)).scalars().all()
        )
        assert any("ies.executor.local_process@1.0.0" in (d.message or "") for d in diagnostics)

    def test_fenced_receipt_abandons_without_cancel(
        self, client: TestClient, db: Session, engine: Engine, plane
    ) -> None:
        """fenced 分流：回执证明租约失效 → 返回 fenced，不写终态、不报取消。"""
        task_id = _submit_calc_task(client, db, dict(_CONFIG_BODY), dict(PLANNING_BODY))
        claim = _claim(db, task_id)
        fenced_plane = _plane_with_stub_runtime(plane, status="fenced")
        assert _run(engine, claim, fenced_plane) == "fenced"
        task = db.execute(select(Task).where(Task.id == task_id)).scalar_one()
        assert task.status == "running"
        assert task.business_outcome is None
        diagnostics = (
            db.execute(select(TaskDiagnostic).where(TaskDiagnostic.task_id == task_id)).scalars().all()
        )
        assert all("取消" not in (d.message or "") for d in diagnostics)

    def test_cancelled_receipt_takes_cancel_path(
        self, client: TestClient, db: Session, engine: Engine, plane
    ) -> None:
        """cancelled 分流：回执证明用户取消 → 走取消路径（任务未 cancelling 则保持 running）。"""
        task_id = _submit_calc_task(client, db, dict(_CONFIG_BODY), dict(PLANNING_BODY))
        claim = _claim(db, task_id)
        cancelled_plane = _plane_with_stub_runtime(plane, status="cancelled")
        assert _run(engine, claim, cancelled_plane) == "cancelled"
        task = db.execute(select(Task).where(Task.id == task_id)).scalar_one()
        assert task.status == "running"
        assert task.business_outcome is None


def _stored_object_count(db: Session) -> int:
    """当前已提交的对象元数据行数（单事务回滚与否的直接证据）。"""
    return len(db.execute(select(StoredObject)).scalars().all())


class TestSingleUnitSeal:
    """单一提交单元：封存/证据/引用/完成同一事务（阻断点回归）。

    旧链路分三次提交（封存→提交→挂引用）：引用挂接失败会留下已完成
    却缺引用的任务与已提交孤立对象；fenced 回执还会产生执行进度写回。
    """

    def test_completed_run_refs_all_outputs(
        self, client: TestClient, db: Session, engine: Engine, plane
    ) -> None:
        """成功后全部输出引用存在：每份封存字节都有证据包引用挂接。"""
        task_id = _submit_calc_task(client, db, dict(_CONFIG_BODY), dict(PLANNING_BODY))
        claim = _claim(db, task_id)
        assert _run(engine, claim, plane) == "completed"

        evidence = results_store.latest_evidence_for_task(db, task_id)
        assert evidence is not None
        sealed = json.loads(get_object(db, evidence.object_id).decode("utf-8"))
        for key in ("bundle_files", "solution"):
            group = sealed[key]
            assert group, key
            for path, descriptor in sorted(group.items()):
                refs = list_refs(db, descriptor["object_id"])
                assert any(
                    r.ref_type == "evidence_package"
                    and r.ref_entity_id == str(evidence.id)
                    for r in refs
                ), (key, path)

    def test_output_ref_failure_leaves_no_committed_metadata(
        self, client: TestClient, db: Session, engine: Engine, plane, monkeypatch
    ) -> None:
        """引用挂接注入失败 → 无 completed、无已提交孤立元数据。

        只让输出引用挂接失败（purpose=evidence_output），结果引用保持
        正常：整笔（含已封存对象元数据）必须回滚，不落终态完成。
        """
        from iesplan.application.worker import compute_cases  # noqa: E402

        real_add_ref = compute_cases.add_ref

        def _fail_output_refs(db, object_id, ref_type, ref_id, ref_entity_type=None,
                              purpose=None, *, actor_id=None, actor_type="system"):
            if purpose == "evidence_output":
                raise RuntimeError("injected-ref-failure")
            return real_add_ref(
                db, object_id, ref_type, ref_id, ref_entity_type=ref_entity_type,
                purpose=purpose, actor_id=actor_id, actor_type=actor_type,
            )

        monkeypatch.setattr(compute_cases, "add_ref", _fail_output_refs)
        task_id = _submit_calc_task(client, db, dict(_CONFIG_BODY), dict(PLANNING_BODY))
        claim = _claim(db, task_id)
        before = _stored_object_count(db)
        assert _run(engine, claim, plane) == "failed"

        task = db.execute(select(Task).where(Task.id == task_id)).scalar_one()
        assert task.status == "failed"
        assert task.business_outcome is None
        assert results_store.latest_evidence_for_task(db, task_id) is None
        assert _stored_object_count(db) == before

    def test_hourly_sealed_from_declared_series(
        self, client: TestClient, db: Session, engine: Engine, plane, monkeypatch
    ) -> None:
        """统一序列封存闭环：声明 → 同次求解 → hourly 对象 → 引用。

        全部取自同一真实求解的封存载荷：hourly_refs 的字段/行数/身份与
        parameters.json 内生成器声明一致；对象数据键与字段一致、单位一致；
        每步值等于同次解变量在声明列的值（声明驱动对照，不拆列名）；
        至少一个序列非常恒（真实求解，非存根常量）；对象有证据包引用挂接。
        """
        # 合成输入经原有 HTTP 提交：初始电量为零，首步必须充电至显式
        # 最小 SOC。这样至少一条状态/流量序列必变化，不依赖零目标任意选解。
        from assembly_fixture import FIXTURE_BATTERY_PARAMS
        monkeypatch.setitem(FIXTURE_BATTERY_PARAMS, "initial_soc", 0)
        task_id = _submit_calc_task(client, db, dict(_CONFIG_BODY), dict(PLANNING_BODY))
        claim = _claim(db, task_id)
        assert _run(engine, claim, plane) == "completed"

        evidence = results_store.latest_evidence_for_task(db, task_id)
        assert evidence is not None
        sealed = json.loads(get_object(db, evidence.object_id).decode("utf-8"))
        refs = sealed.get("hourly_refs")
        assert isinstance(refs, list) and len(refs) == 1
        ref = refs[0]
        params = json.loads(
            get_object(db, sealed["bundle_files"]["input/parameters.json"]["object_id"]).decode("utf-8")
        )
        decls = params["step_series"]
        assert decls, "真实求解应有可发布步变量声明"
        assert ref["fields"] == sorted(d["series_id"] for d in decls)
        assert ref["rows"] > 0
        assert [
            {k: m[k] for k in ("series_id", "instance_id", "variable_id", "unit")}
            for m in ref["series"]
        ] == [
            {
                "series_id": d["series_id"],
                "instance_id": d["instance_id"],
                "variable_id": d["variable_id"],
                "unit": d["unit"],
            }
            for d in sorted(decls, key=lambda d: d["series_id"])
        ]
        hourly = json.loads(get_object(db, ref["object_id"]).decode("utf-8"))
        assert set(hourly["data"]) == set(ref["fields"])
        assert hourly["meta"]["units"] == {d["series_id"]: d["unit"] for d in decls}
        assert hourly["meta"]["series"] == {
            d["series_id"]: {"instance_id": d["instance_id"], "variable_id": d["variable_id"]}
            for d in decls
        }
        assert all(len(v) == ref["rows"] for v in hourly["data"].values())
        varmap = {
            v["name"]: v["value"]
            for v in sealed["compute_result"]["outputs"]["solution"]["variables"]
        }
        for d in decls:
            assert hourly["data"][d["series_id"]] == [varmap[c] for c in d["columns"]]
        assert any(len(set(v)) > 1 for v in hourly["data"].values()), "应有非常恒真实序列"
        attached = list_refs(db, ref["object_id"])
        assert any(
            r.ref_type == "evidence_package" and r.ref_entity_id == str(evidence.id)
            for r in attached
        )

    def test_hourly_absent_for_metrics_only(
        self, client: TestClient, db: Session, engine: Engine, plane
    ) -> None:
        """metrics-only：未请求 solution 即无 hourly 对象与引用（缺席，不伪造）。"""
        body = dict(_CONFIG_BODY)
        body["outputs"] = ["metrics"]
        task_id = _submit_calc_task(client, db, body, dict(PLANNING_BODY))
        claim = _claim(db, task_id)
        assert _run(engine, claim, plane) == "completed"

        evidence = results_store.latest_evidence_for_task(db, task_id)
        assert evidence is not None
        sealed = json.loads(get_object(db, evidence.object_id).decode("utf-8"))
        assert "hourly_refs" not in sealed
        assert "series" not in sealed["compute_result"]["outputs"]

    def test_late_failure_rolls_back_hourly_seal(
        self, client: TestClient, db: Session, engine: Engine, plane, monkeypatch
    ) -> None:
        """评估写入失败 → 含 hourly 封存在内整笔回滚，无孤立元数据与引用。"""
        import iesplan.application.worker.attempt_cases as attempt_cases  # noqa: E402

        def _boom(*args, **kwargs):
            raise RuntimeError("injected-assessment-failure")

        monkeypatch.setattr(attempt_cases, "create_assessment_record", _boom)
        task_id = _submit_calc_task(client, db, dict(_CONFIG_BODY), dict(PLANNING_BODY))
        claim = _claim(db, task_id)
        before = _stored_object_count(db)
        assert _run(engine, claim, plane) == "failed"

        task = db.execute(select(Task).where(Task.id == task_id)).scalar_one()
        assert task.status == "failed"
        assert results_store.latest_evidence_for_task(db, task_id) is None
        assert _stored_object_count(db) == before

    def test_submit_fence_rejects_before_seal(
        self, client: TestClient, db: Session
    ) -> None:
        """提交时 fencing → 封存前拒绝，无 completed、无孤立元数据。"""
        import dataclasses  # noqa: E402
        import uuid  # noqa: E402

        task_id = _submit_calc_task(client, db, dict(_CONFIG_BODY), dict(PLANNING_BODY))
        claim = _claim(db, task_id)
        bad_claim = dataclasses.replace(claim, lease_token=uuid.uuid4())
        before = _stored_object_count(db)
        with pytest.raises(worker_app.LeaseRejectedError):
            worker_app.submit_attempt_result(
                db, bad_claim, payload={"outcome": "normal_completion"},
                outcome="normal_completion",
                output_files={"bundle_files": {"a.bin": (b"abc", "application/octet-stream")}},
            )
        task = db.execute(select(Task).where(Task.id == task_id)).scalar_one()
        assert task.status == "running"
        assert results_store.latest_evidence_for_task(db, task_id) is None
        assert _stored_object_count(db) == before

    def test_fenced_receipt_writes_no_execute_progress(
        self, client: TestClient, db: Session, engine: Engine, plane
    ) -> None:
        """fenced 回执判定后无进度写回：执行进度停在判定前的产生点。"""
        task_id = _submit_calc_task(client, db, dict(_CONFIG_BODY), dict(PLANNING_BODY))
        claim = _claim(db, task_id)
        assert _run(engine, claim, _plane_with_stub_runtime(plane, status="fenced")) == "fenced"
        row = db.execute(
            select(TaskProgress).where(TaskProgress.attempt_id == claim.attempt_id)
        ).scalar_one_or_none()
        assert row is not None
        assert (row.stage, float(row.progress_percent)) == ("generate", 40.0)
