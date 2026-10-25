"""任务与资源调度(U08) API 与服务集成测试。

覆盖: 幂等创建(同键返回同任务)→ 状态推进(注入假执行器)→ 取消(queued 直接 /
running 经 cancelling)→ 重试同快照 → 槽限制(2 并发)→ 存储门禁(模拟空间不足)
→ report(io 队列)与队列/心跳/进度/取消信号读写。

- 数据库: SQLite :memory:(models 全部表 create_all, StaticPool 共享连接);
- 队列: IESPLAN_QUEUE=memory 强制内存后端(单进程, 无外部 Redis 依赖);
- 应用: create_app() + include_router(projects/tasks), dependency_overrides 替换 get_request_db;
- 假执行器: 测试直接调用 tasks 用例 claim_task / tasks 域门面进度·完成·失败推进
  等正典入口模拟 Worker 行为(本文件只覆盖任务 API 与应用层用例的集成边界，不启动真实 Worker 进程)。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# 单文件运行时的安全网: 固定 SQLite + 内存队列, 避免误连部署 Postgres/Redis
os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")

import pytest  # noqa: E402
from auth_helpers import login_headers, make_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan.api import projects as projects_api  # noqa: E402
from iesplan.api import tasks as tasks_api  # noqa: E402
from iesplan.api.config import config_router  # noqa: E402
from iesplan import tasks as tasks_domain  # noqa: E402
from iesplan.api.deps import get_request_db  # noqa: E402
from iesplan.application import tasks as tasks_uc  # noqa: E402
from iesplan.assembly import CANON_ALGORITHM_ID, CANON_ALGORITHM_VERSION, SCHEMA_ID, SCHEMA_VERSION, VALIDATOR_ID, VALIDATOR_VERSION  # noqa: E402
from iesplan.config import settings  # noqa: E402
from iesplan.core.diagnostics import (  # noqa: E402
    SEVERITY_ERROR,
    SEVERITY_INFO,
    TASK_DATA_SNAPSHOT_MISSING,
    TASK_QUEUED,
    TASK_SOLVE_FAILED,
)
from iesplan.core.errors import NotFoundError  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.tasks.persistence import CalcSnapshot, ComputeSlot, Task, TaskLease, TaskProgress  # noqa: E402
from iesplan.dataset.persistence import DatasetVersion  # noqa: E402,F401
from iesplan.identity.persistence import User  # noqa: E402,F401
from iesplan.storage.persistence import StoredObject  # noqa: E402
from iesplan.tasks import queue  # noqa: E402

# ---------------------------------------------------------------------------
# 测试环境
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """会话级 SQLite 内存引擎(StaticPool: 所有会话共享同一连接)。"""
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
    """函数级共享会话(服务与测试共用, 端点内 commit)。"""
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


@pytest.fixture()
def client(engine: Engine, db: Session, tmp_path: Path) -> Iterator[TestClient]:
    """测试客户端: 挂载项目+任务路由, 替换 get_request_db 依赖, 对象存储指向临时目录。"""
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


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _h(client: TestClient, user) -> dict[str, str]:
    """认证头: 以真实窗口会话登录(同一 client 内缓存, 避免多窗口接管)。"""
    return login_headers(client, user)


def _create_project(client: TestClient, user, name: str) -> int:
    """创建项目并返回项目 id。"""
    resp = client.post(
        "/api/projects",
        json={
            "name": name,
            "currency": "CNY",
            "baseline_resolution": "1h",
            "baseline_start_year": 2026,
            "baseline_scenario_mode": "single",
        },
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["project"]["id"]


def _seed_dataset(db: Session, user, pid: int, *, quota_bytes: int = 0) -> int:
    """经公开用例生成内置样例数据版本(真实数据本体 + 可读对象), 返回 version id。

    手造对象行(无 blob)会制造门面必然报错的不可能状态，本函数不用。
    quota_bytes 模拟配额不足：写到真实对象行配额字段(配额管理同语义)。
    """
    from iesplan import dataset as dataset_domain
    from iesplan.application.datasets import lifecycle as datasets_uc

    version = datasets_uc.create_builtin_sample(db, pid, "1h", user_id=user.id)
    if quota_bytes:
        for f in dataset_domain.list_files(db, version.id):
            obj = db.get(StoredObject, f.object_id)
            assert obj is not None
            obj.quota_bytes = quota_bytes
        db.commit()
    return version.id


#: 现行装配边界要求的显式计算声明(无静默默认): mode/generator/solver/
#: time_axis 缺一即阻断(ASM-CONV-001)。夹具必须显式声明, 不得依赖回退。
EXPLICIT_CALC_PATCH = {
    "mode": "fixed_operation",
    "generator": "ies.generator.highs_milp@5.0.0",
    "solver": "ies.solver.highs@1.7.2",
    "time_axis": {"resolution": "1h", "start": "2025-01-01T00:00:00Z"},
}


def _declare_finance(db: Session, user, pid: int) -> None:
    """装配闸门要求项目固定有效财务快照: 登记地区 Profile 并引用
    (原子生成空覆盖 Effective)。夹具必须显式声明, 不得依赖回退。"""
    from test_config_revisions import PROFILE_PAYLOAD

    from iesplan.application import configuration as config_cases

    config_cases.register_finance_profile(db, dict(PROFILE_PAYLOAD), user.id)
    config_cases.set_project_finance_profile(
        db, pid, PROFILE_PAYLOAD["profile"]["id"], user.id
    )


#: 无意图项目的装配声明占位(任务机械测试用；真实项目声明真实目标/绑定)。
#: finance_binding/tariff_bindings 经起草命令显式声明；planning 为冻结规划
#: 修订的确定性投影(见 assembly_fixture)，不再经草稿声明。
def _assembly_commands(instances: dict) -> list:
    return [
        {
            "id": "c-finbind",
            "unit": "assembly",
            "type": "assembly.set_finance_binding",
            "payload": {"instances": instances},
        },
        {
            "id": "c-tariff",
            "unit": "assembly",
            "type": "assembly.set_tariff_bindings",
            "payload": {"bindings": {}},
        },
    ]


def _declare_assembly_sections(
    client: TestClient, pid: int, user, revision: int, instances: dict,
) -> int:
    """经 assembly.* 起草命令显式声明装配节, 返回新修订号。"""
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": revision, "commands": _assembly_commands(instances)},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def _declare_device_and_planning(client: TestClient, db: Session, user, pid: int) -> tuple:
    """夹具电池设备 + 最小规划修订(计算提交前置)，返回(装配实例引用, 草稿修订号)。"""
    from assembly_fixture import declare_compute_device, save_compute_planning

    device_ref, revision = declare_compute_device(client, pid, _h(client, user))
    save_compute_planning(db, pid, user.id, device_ref)
    return device_ref, revision


def _set_explicit_calc(client: TestClient, pid: int, user, revision: int) -> int:
    """经 config.patch 显式声明计算装配字段, 返回新修订号。"""
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": revision, "commands": [{
            "id": "c-calc", "unit": "config", "type": "config.patch",
            "payload": EXPLICIT_CALC_PATCH,
        }]},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def _bind_and_freeze(
    client: TestClient, pid: int, user, dataset_version_id: int | None,
    revision: int = 1,
) -> None:
    """绑定数据集版本(如给定)并从当前草稿创建不可变项目版本。"""
    if dataset_version_id is not None:
        resp = client.put(
            f"/api/projects/{pid}/draft",
            json={"expected_revision": revision, "commands": [{
                "id": "c-bind", "unit": "dataset", "type": "dataset.bind",
                "payload": {"dataset_version_id": dataset_version_id, "role": "main"},
            }]},
            headers=_h(client, user),
        )
        assert resp.status_code == 200, resp.text
    body: dict[str, Any] = {"name": "计算基线", "reason": "snapshot_freeze"}
    if revision != 1:
        # 固化门禁按当前草稿修订校验: 携带显式 expected_revision,
        # 否则 409(上游测试用 _bind_and_freeze 默认 1 即代表无中间修订)。
        body["expected_revision"] = revision
    resp = client.post(
        f"/api/projects/{pid}/versions",
        json=body,
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text


def _prepare_project(
    client: TestClient, db: Session, user, *,
    dataset_size: int = 0, quota_bytes: int = 0, name: str = "任务测试项目",
) -> int:
    """准备可提交计算任务的项目: 建项目 + 显式财务/装配声明 + 显式计算声明 + (可选)数据集 + 绑定 + 固化版本。"""
    pid = _create_project(client, user, name)
    _declare_finance(db, user, pid)
    device_ref, revision = _declare_device_and_planning(client, db, user, pid)
    dvid = None
    if dataset_size > 0:
        dvid = _seed_dataset(db, user, pid, quota_bytes=quota_bytes)
    revision = _set_explicit_calc(client, pid, user, revision)
    revision = _declare_assembly_sections(
        client, pid, user, revision, {device_ref: {"type": "none"}}
    )
    _bind_and_freeze(client, pid, user, dvid, revision=revision)
    return pid


#: 计算配置正文(12 字段契约; 与 test_http_contract._CALC_PAYLOAD 同构)。
_CONFIG_BODY: dict[str, Any] = {
    "schema": "ies.calculation-config",
    "schema_version": "2.0.0",
    "mode": "planning",
    "predictions": [],
    "generator": "ies.generator.highs_milp@5.0.0",
    "solver": "ies.solver.highs@1.7.2",
    "precision": "medium",
    "convergence": None,
    "time_limit_seconds": 600,
    "options": {},
    "seed": None,
    "outputs": ["solution"],
}


def _save_config(client: TestClient, pid: int, user) -> int:
    """保存一份计算配置, 返回 revision(计算任务提交绑定用)。"""
    resp = client.put(
        f"/api/projects/{pid}/config",
        json={"calculation_config": _CONFIG_BODY, "expected_revision": None},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def _submit_task(
    client: TestClient, pid: int, user, task_type: str = "optimization",
    config: dict[str, Any] | None = None, idempotency_key: str | None = None,
    evaluation: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """提交任务, 返回 (HTTP 状态码, 响应体)。

    计算类任务的评价确认以顶层字段发送(缺省为显式零建设期单年块
    K=0/E=1/N=1, 仅测试便利; 产品无默认值)。需要定制评价输入的调用方
    显式传入 ``evaluation``。
    """
    body: dict[str, Any] = {"task_type": task_type}
    if config is not None:
        body["config"] = dict(config)
        body["evaluation"] = (
            evaluation if evaluation is not None
            else {
                "evaluation_years": 1,
                "construction_period": 0,
                "operation_years": 1,
                "discount_rate": 0.08,
                "construction_payments": [{"t": 0, "ratio": 1}],
                "maintenance_expenses": [],
            }
        )
    if idempotency_key is not None:
        body["idempotency_key"] = idempotency_key
    resp = client.post(f"/api/projects/{pid}/tasks", json=body, headers=_h(client, user))
    return resp.status_code, resp.json()


def _claim(db: Session, task_id: int, worker_id: str = "fake-exec-1") -> Any:
    """假执行器: 领取任务(占槽 + 建尝试 + 建租约 + running)。"""
    claim = tasks_uc.claim_task(db, task_id, worker_id)
    db.commit()
    return claim


def _get_task(db: Session, task_id: int) -> Any:
    """按 id 取任务行; 不存在 404(同原 worker 边界语义)。"""
    task = tasks_domain.get_task(db, task_id)
    if task is None:
        raise NotFoundError(
            "任务不存在",
            params={"task_id": task_id},
            location={"object_type": "task", "object_id": task_id},
        )
    return task


def _finish_attempt(db: Session, task: Any, status: str, stop_reason: str | None) -> Any:
    """收尾当前运行尝试: 尝试终态 + 租约释放/吊销 + 槽释放(经 tasks 域门面)。"""
    attempt = tasks_domain.get_running_attempt(db, task.id)
    if attempt is None:
        return None
    finished = tasks_domain.finish_attempt(db, attempt.id, status, stop_reason=stop_reason)
    lease = tasks_domain.get_active_lease_for_attempt(db, attempt.id)
    if lease is not None:
        tasks_domain.release_lease(
            db, attempt.id, lease.lease_token,
            status="released" if status == "succeeded" else "revoked",
        )
    tasks_domain.release_slot(db, attempt.id)
    return finished


def _record_task_progress(
    db: Session, task_id: int, stage: str, percent: float, detail: dict[str, Any] | None = None
) -> Any:
    """假执行器进度: PG UPSERT + 队列秒级进度(经 tasks 域门面)。"""
    task = _get_task(db, task_id)
    attempt = tasks_domain.get_latest_attempt(db, task.id)
    if attempt is None:
        return None
    percent = round(min(max(float(percent), 0.0), 100.0), 2)
    tasks_domain.upsert_progress(
        db, attempt_id=attempt.id, progress_percent=percent, stage=stage, detail=detail
    )
    tasks_domain.set_queue_progress(task.id, attempt.attempt_no, percent, stage, detail)
    return attempt


def _complete_task(db: Session, task_id: int, *, outcome: str) -> Any:
    """假执行器完成: running → completed(幂等), 结局正交保存(经 tasks 域门面)。"""
    if outcome not in tasks_domain.BUSINESS_OUTCOMES:
        raise tasks_domain.InvalidRequestError(
            "任务完成缺少显式合法 outcome",
            params={"task_id": task_id, "outcome": outcome,
                    "allowed": list(tasks_domain.BUSINESS_OUTCOMES)},
            location={"object_type": "task", "object_id": task_id},
        )
    task = _get_task(db, task_id)
    if task.status == "completed":
        return task
    tasks_domain.check_transition(task, "completed")
    attempt = _finish_attempt(db, task, status="succeeded", stop_reason=None)
    task = tasks_domain.set_task_status(db, task.id, "completed", business_outcome=outcome)
    tasks_domain.clear_cancel(task.id)
    tasks_domain.append_diagnostic(
        db, task_id=task.id, level=SEVERITY_INFO, message="任务完成",
        attempt_id=attempt.id if attempt else None, code=TASK_QUEUED,
        context={"business_outcome": outcome},
    )
    return task


def _fail_task(db: Session, task_id: int, *, code: str | None = None, message: str = "") -> Any:
    """假执行器失败: running → failed, 确定性失败不可自动重试(经 tasks 域门面)。"""
    task = _get_task(db, task_id)
    if task.status == "failed":
        return task
    tasks_domain.check_transition(task, "failed")
    outcome = "insufficient_evidence" if code == TASK_DATA_SNAPSHOT_MISSING else None
    attempt = _finish_attempt(db, task, status="failed", stop_reason=code or "error")
    task = tasks_domain.set_task_status(db, task.id, "failed", business_outcome=outcome)
    tasks_domain.append_diagnostic(
        db, task_id=task.id, level=SEVERITY_ERROR, message=message,
        attempt_id=attempt.id if attempt else None, code=code or TASK_SOLVE_FAILED,
        context={"outcome": outcome},
    )
    return task


def _run_task(
    db: Session, task_id: int, worker_id: str = "fake-exec-1", solver_status: str = "OPTIMAL"
) -> Task:
    """假执行器: 完整推进一次任务(领取 → 进度 → 完成)。"""
    _claim(db, task_id, worker_id)
    _record_task_progress(db, task_id, "solve", 50.0, {"iterations": 1})
    task = _complete_task(db, task_id, outcome=tasks_domain.map_business_outcome(solver_status))
    db.commit()
    return task


# ---------------------------------------------------------------------------
# 幂等创建与快照去重
# ---------------------------------------------------------------------------


def test_idempotent_create_and_snapshot_dedup(client: TestClient, db: Session) -> None:
    """同幂等键返回同一任务; 无键提交拒绝(422); 不同输入生成新快照。"""
    owner = make_user(db, "owner_idem")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)

    # 1) 带幂等键创建 → 201
    status, body = _submit_task(
        client, pid, owner, config={"config_revision": rev}, idempotency_key="op-20260818-abc"
    )
    assert status == 201
    task_a = body["task"]
    assert task_a["status"] == "queued"
    assert task_a["type"] == "optimization"
    assert body["replayed"] is False
    snapshot_a = task_a["calc_snapshot_id"]
    assert snapshot_a is not None

    # 2) 同键重发(客户端网络重试) → 200 返回同一任务
    status, body = _submit_task(
        client, pid, owner, config={"config_revision": rev}, idempotency_key="op-20260818-abc"
    )
    assert status == 200
    assert body["replayed"] is True
    assert body["task"]["id"] == task_a["id"]
    assert body["task"]["calc_snapshot_id"] == snapshot_a

    # 3) 无键提交 → 422(宪法 §5.5: Task 请求必须携带幂等键)
    status, body = _submit_task(client, pid, owner)
    assert status == 422

    # 4) 不同输入(config 不同 → 快照输入不同, 带新键) → 201 新任务新快照
    status, body = _submit_task(
        client, pid, owner, config={"config_revision": rev, "horizon_years": 3},
        idempotency_key="op-20260818-def",
    )
    assert status == 201
    assert body["task"]["id"] != task_a["id"]
    assert body["task"]["calc_snapshot_id"] != snapshot_a

    # 5) 快照按内容去重: 仅 2 个快照(同输入共享 1 个)
    snapshots = db.execute(select(CalcSnapshot)).scalars().all()
    assert len(snapshots) == 2
    by_id = {s.id: s for s in snapshots}
    persisted = by_id[snapshot_a]
    assert persisted.random_seed is not None
    assert persisted.canonical_assembly_text
    # 文本仅校验字头，快照去重使用内容字段逐项相等判定（header-only）
    assert isinstance(persisted.assembly_receipt, dict)
    receipt = persisted.assembly_receipt
    assert "issued_at" not in receipt
    assert receipt["schema"] == SCHEMA_ID
    assert receipt["schema_version"] == SCHEMA_VERSION
    assert receipt["validator"] == {"id": VALIDATOR_ID, "version": VALIDATOR_VERSION}
    assert receipt["canonical_algorithm"] == {"id": CANON_ALGORITHM_ID, "version": CANON_ALGORITHM_VERSION}
    assert isinstance(receipt["dependencies"], dict)
    assert isinstance(receipt["resources"], dict)
    assert isinstance(receipt["diagnostics"], list)
    assert all(not item.get("blocking", False) for item in receipt["diagnostics"])

    # 6) 列表可见 2 个任务(步骤 2/3 均为既有任务复用; 含摘要与排队位次)
    resp = client.get(f"/api/projects/{pid}/tasks", headers=_h(client, owner))
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    first = items[0]
    assert first["summary"]["queue_position"] is not None
    assert first["summary"]["percent"] == 0.0


def test_compute_submission_uses_unified_assembly_gate(
    client: TestClient, db: Session
) -> None:
    """未注册设备在起草输入边界即被拒绝, 不得进入草稿, 不得留下任务或快照。"""
    owner = make_user(db, "owner_assembly_gate")
    pid = _create_project(client, owner, "装配闸门反例")
    _declare_finance(db, owner, pid)
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={
            "expected_revision": 1,
            "commands": [
                {
                    "id": "bad-model",
                    "unit": "model",
                    "type": "model.upsert_device",
                    "payload": {
                        "name": "未知设备",
                        "device_type": "ies.device.not_registered",
                        "kind": "existing",
                    },
                }
            ],
        },
        headers=_h(client, owner),
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "PROJ-CMD-005"
    assert db.execute(select(func.count(Task.id))).scalar() == 0
    assert db.execute(select(func.count(CalcSnapshot.id))).scalar() == 0


def test_submit_accepts_tax_rules_with_capable_generator(
    client: TestClient, db: Session
) -> None:
    """真实提交入口(C3)：非空 tax_rules + HiGHS 3.4.0 能力 → 201 并冻结配套引用。

    快照 result_adapter/executor 与所选描述符同一来源；项目财务画像保持
    含税则（不再要求切回空规则）。
    """
    from assembly_fixture import save_compute_planning
    from test_config_revisions import _tax_profile_payload

    from iesplan.application import configuration as config_cases

    owner = make_user(db, "owner_taxgate")
    # 内联 _prepare_project 以保留 device_ref：切换 Profile 按设计使旧
    # Planning 一并失效(_set_project_profile)，必须重存规划后才能提交。
    pid = _create_project(client, owner, "任务测试项目")
    _declare_finance(db, owner, pid)
    device_ref, revision = _declare_device_and_planning(client, db, owner, pid)
    revision = _set_explicit_calc(client, pid, owner, revision)
    revision = _declare_assembly_sections(
        client, pid, owner, revision, {device_ref: {"type": "none"}}
    )
    _bind_and_freeze(client, pid, owner, None, revision=revision)
    tax_payload = _tax_profile_payload()
    config_cases.register_finance_profile(db, dict(tax_payload), owner.id)
    config_cases.set_project_finance_profile(db, pid, "cn-tax-demo", owner.id)
    save_compute_planning(db, pid, owner.id, device_ref)
    rev = _save_config(client, pid, owner)
    status, body = _submit_task(
        client, pid, owner, config={"config_revision": rev}, idempotency_key="op-tax-001")
    assert status == 201, body
    assert body["task"]["status"] == "queued"
    snapshot = db.execute(
        select(CalcSnapshot).where(CalcSnapshot.id == body["task"]["calc_snapshot_id"])
    ).scalar_one()
    assert snapshot.result_adapter == "ies.result-adapter.highs@5.0.0"
    assert snapshot.executor == "ies.executor.local_process@1.0.0"
    assert snapshot.calc_config_snapshot["generator"] == "ies.generator.highs_milp@5.0.0"
    assert db.execute(select(func.count(Task.id))).scalar() == 1
    config_cases.set_project_finance_profile(db, pid, "cn-north-demo", owner.id)
    save_compute_planning(db, pid, owner.id, device_ref)
    status, body = _submit_task(
        client, pid, owner, config={"config_revision": rev}, idempotency_key="op-tax-002")
    assert status == 201, body
    assert body["task"]["status"] == "queued"


def test_submit_accepts_pool_only_with_capable_generator(
    client: TestClient, db: Session
) -> None:
    """真实提交入口(C3)：仅含池（期初留抵/明确退款）+ 池能力 → 201。

    能力判断覆盖无税则的纯池情形，不遗漏；快照冻结同一配套引用。
    """
    from assembly_fixture import save_compute_planning
    from test_config_revisions import _tax_profile_payload

    from iesplan.application import configuration as config_cases

    owner = make_user(db, "owner_poolgate")
    pid = _create_project(client, owner, "任务测试项目")
    _declare_finance(db, owner, pid)
    device_ref, revision = _declare_device_and_planning(client, db, owner, pid)
    revision = _set_explicit_calc(client, pid, owner, revision)
    revision = _declare_assembly_sections(
        client, pid, owner, revision, {device_ref: {"type": "none"}}
    )
    _bind_and_freeze(client, pid, owner, None, revision=revision)
    pool_payload = _tax_profile_payload()
    pool_payload["profile"] = {
        **pool_payload["profile"], "id": "cn-pool-demo", "region": "CN-Pool",
    }
    pool_payload["tax_rules"] = {}
    pool_payload["credit_pools"] = {
        "main_pool": {
            "opening_credit": {"value": "100", "unit": "CNY"},
            "receivable_policy": "end_receivable",
            "refunds": [],
        },
    }
    config_cases.register_finance_profile(db, dict(pool_payload), owner.id)
    config_cases.set_project_finance_profile(db, pid, "cn-pool-demo", owner.id)
    save_compute_planning(db, pid, owner.id, device_ref)
    rev = _save_config(client, pid, owner)
    status, body = _submit_task(
        client, pid, owner, config={"config_revision": rev}, idempotency_key="op-pool-001")
    assert status == 201, body
    assert body["task"]["status"] == "queued"
    snapshot = db.execute(
        select(CalcSnapshot).where(CalcSnapshot.id == body["task"]["calc_snapshot_id"])
    ).scalar_one()
    assert snapshot.result_adapter == "ies.result-adapter.highs@5.0.0"
    assert snapshot.executor == "ies.executor.local_process@1.0.0"
    assert db.execute(select(func.count(Task.id))).scalar() == 1


def test_submit_rejects_tax_rules_without_provider_capabilities(
    db: Session, tmp_path: Path
) -> None:
    """真实提交入口(C3)：合成 provider 缺税务能力标记 → 400 且不建任务/快照。

    经独立 app.state 组合根注入无税务能力的合成生成器目录（只缺标记，
    求解器/执行器声明合法）；同一含税则项目在真实 HiGHS 下本可放行，
    此处必须按缺失能力 007。空税则不受影响（另见空规则正例）。
    """
    from types import SimpleNamespace

    from assembly_fixture import save_compute_planning
    from test_config_revisions import _tax_profile_payload

    from iesplan.application import configuration as config_cases
    from iesplan.computation.errors import ComputationUnavailableError
    from iesplan.computation.generators.descriptor import GeneratorDescriptor

    stub_ref = "ies.generator.stub@1.0.0"
    stub_descriptor = GeneratorDescriptor(
        generator_id="ies.generator.stub",
        version="1.0.0",
        options_schema={"type": "object", "additionalProperties": False},
        assembly_schema_version="2.0",
        modes=("planning",),
        precisions=("medium",),
        solvers=("ies.solver.highs@1.7.2",),
        executors=("ies.executor.local_process@1.0.0",),
        result_adapter="ies.result-adapter.highs@5.0.0",
        unit_system="SI",
        seed_semantics="测试合成 provider，无税务能力标记",
    )
    assert stub_descriptor.capabilities == ()

    def _get(ref: str):
        if ref == stub_ref:
            return SimpleNamespace(descriptor=stub_descriptor)
        raise ComputationUnavailableError(f"未知生成器: {ref}", reason="no-generator")

    settings.data_dir = tmp_path
    app = create_app()
    app.include_router(projects_api.router)
    app.include_router(tasks_api.router)
    app.include_router(config_router)

    def _override_request_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_request_db] = _override_request_db
    with TestClient(app, raise_server_exceptions=False) as stub_client:
        app.state.bootstrap_context = SimpleNamespace(
            generator_registry=SimpleNamespace(get=_get),
            predictor_registry=None,
        )
        owner = make_user(db, "owner_stubgate")
        pid = _create_project(stub_client, owner, "任务测试项目")
        _declare_finance(db, owner, pid)
        device_ref, revision = _declare_device_and_planning(stub_client, db, owner, pid)
        revision = _set_explicit_calc(stub_client, pid, owner, revision)
        revision = _declare_assembly_sections(
            stub_client, pid, owner, revision, {device_ref: {"type": "none"}}
        )
        _bind_and_freeze(stub_client, pid, owner, None, revision=revision)
        tax_payload = _tax_profile_payload()
        config_cases.register_finance_profile(db, dict(tax_payload), owner.id)
        config_cases.set_project_finance_profile(db, pid, "cn-tax-demo", owner.id)
        save_compute_planning(db, pid, owner.id, device_ref)
        stub_config = dict(_CONFIG_BODY)
        stub_config["generator"] = stub_ref
        resp = stub_client.put(
            f"/api/projects/{pid}/config",
            json={"calculation_config": stub_config, "expected_revision": None},
            headers=_h(stub_client, owner),
        )
        assert resp.status_code == 200, resp.text
        rev = resp.json()["revision"]
        status, body = _submit_task(
            stub_client, pid, owner,
            config={"config_revision": rev}, idempotency_key="op-stub-001")
        assert status == 400, body
        assert body["error"]["code"] == "TASK-REQ-007"
        assert body["error"]["params"]["generator"] == stub_ref
        assert body["error"]["params"]["missing_capabilities"] == [
            "finance.credit_pools",
            "finance.financial_facts",
            "finance.income_tax",
            "finance.vat",
        ]
        assert db.execute(select(func.count(Task.id))).scalar() == 0
        assert db.execute(select(func.count(CalcSnapshot.id))).scalar() == 0


def _prepare_taxref_project(client: TestClient, db: Session, owner, profile_payload: dict) -> tuple:
    """建项目 + 登记/引用税则画像 + 保存覆盖 + 规划/计算/装配/固化(返回 pid 与 device_ref)。"""
    pid = _create_project(client, owner, "税则跨输入项目")
    resp = client.post(
        "/api/finance-profiles",
        json={"finance_profile": profile_payload},
        headers=_h(client, owner),
    )
    assert resp.status_code == 200, resp.text
    resp = client.put(
        f"/api/projects/{pid}/finance-profile",
        json={"profile_ref": {"id": profile_payload["profile"]["id"]}},
        headers=_h(client, owner),
    )
    assert resp.status_code == 200, resp.text
    device_ref, revision = _declare_device_and_planning(client, db, owner, pid)
    revision = _set_explicit_calc(client, pid, owner, revision)
    revision = _declare_assembly_sections(
        client, pid, owner, revision, {device_ref: {"type": "none"}}
    )
    _bind_and_freeze(client, pid, owner, None, revision=revision)
    return pid, device_ref


def _put_project_overrides(client: TestClient, pid: int, owner, payload: dict, expected_revision) -> None:
    resp = client.put(
        f"/api/projects/{pid}/finance-overrides",
        json={"finance_overrides": payload, "expected_revision": expected_revision},
        headers=_h(client, owner),
    )
    assert resp.status_code == 200, resp.text


def _evaluation_2y(expenses: list) -> dict:
    return {
        "evaluation_years": 2,
        "construction_period": 0,
        "operation_years": 2,
        "discount_rate": 0.08,
        "construction_payments": [{"t": 0, "ratio": 1}],
        "maintenance_expenses": expenses,
    }


def _maint_ref_overrides(items: list) -> dict:
    return {
        "schema": "ies.finance-overrides",
        "schema_version": "4.0.0",
        "profile_ref": {"id": "cn-tax-demo"},
        "tax_rules": {
            "operating_vat": {
                "maintenance_input_tax": [
                    {
                        "year": year,
                        "position": position,
                        "rate": {"value": "0.13", "unit": "1"},
                        "deductible_ratio": {"value": ratio, "unit": "1"},
                    }
                    for year, position, ratio in [
                        (2, 1, "0"),
                        (2, 2, "1"),
                    ]
                ],
            },
            "income": {
                "deductible_maintenance": {"mode": "explicit_items", "items": items},
            },
        },
    }


def test_submit_tax_refs_two_items_same_year_accepted(client: TestClient, db: Session) -> None:
    """同年两笔合法引用(两维度独立)：跨输入与能力闸门均通过，201 建任务。"""
    from assembly_fixture import save_compute_planning
    from test_config_revisions import _tax_profile_payload

    owner = make_user(db, "owner_taxref_ok")
    pid, device_ref = _prepare_taxref_project(client, db, owner, _tax_profile_payload())
    _put_project_overrides(
        client,
        pid,
        owner,
        _maint_ref_overrides(
            [
                {"year": 2, "position": 1, "deductible": True},
                {"year": 2, "position": 2, "deductible": False},
            ]
        ),
        1,
    )
    save_compute_planning(db, pid, owner.id, device_ref)
    rev = _save_config(client, pid, owner)
    status, body = _submit_task(
        client,
        pid,
        owner,
        config={"config_revision": rev},
        evaluation=_evaluation_2y(
            [
                {"year": 2, "amount": "100", "currency": "CNY"},
                {"year": 2, "amount": "50", "currency": "CNY"},
            ]
        ),
        idempotency_key="op-taxref-ok",
    )
    assert status == 201, body
    assert body["task"]["status"] == "queued"
    assert db.execute(select(func.count(Task.id))).scalar() == 1
    assert db.execute(select(func.count(CalcSnapshot.id))).scalar() == 1


def test_submit_tax_refs_reject_missing_year(client: TestClient, db: Session) -> None:
    """引用清单缺失的评价年 → 400 TASK-REQ-015，不建任务。"""
    from assembly_fixture import save_compute_planning
    from test_config_revisions import _tax_profile_payload

    owner = make_user(db, "owner_taxref_year")
    pid, device_ref = _prepare_taxref_project(client, db, owner, _tax_profile_payload())
    payload = _maint_ref_overrides([{"year": 3, "position": 1, "deductible": True}])
    payload["tax_rules"]["operating_vat"]["maintenance_input_tax"] = []
    _put_project_overrides(client, pid, owner, payload, 1)
    save_compute_planning(db, pid, owner.id, device_ref)
    rev = _save_config(client, pid, owner)
    status, body = _submit_task(
        client,
        pid,
        owner,
        config={"config_revision": rev},
        evaluation=_evaluation_2y(
            [
                {"year": 2, "amount": "100", "currency": "CNY"},
            ]
        ),
        idempotency_key="op-taxref-year",
    )
    assert status == 400, body
    assert body["error"]["code"] == "TASK-REQ-015"
    assert body["error"]["params"]["year"] == 3
    assert body["error"]["params"]["position"] == 1
    assert db.execute(select(func.count(Task.id))).scalar() == 0
    assert db.execute(select(func.count(CalcSnapshot.id))).scalar() == 0


def test_submit_tax_refs_reject_position_overflow(client: TestClient, db: Session) -> None:
    """同年 position 越位 → 400 TASK-REQ-015，不建任务。"""
    from assembly_fixture import save_compute_planning
    from test_config_revisions import _tax_profile_payload

    owner = make_user(db, "owner_taxref_pos")
    pid, device_ref = _prepare_taxref_project(client, db, owner, _tax_profile_payload())
    payload = _maint_ref_overrides([{"year": 2, "position": 2, "deductible": True}])
    payload["tax_rules"]["operating_vat"]["maintenance_input_tax"] = []
    _put_project_overrides(client, pid, owner, payload, 1)
    save_compute_planning(db, pid, owner.id, device_ref)
    rev = _save_config(client, pid, owner)
    status, body = _submit_task(
        client,
        pid,
        owner,
        config={"config_revision": rev},
        evaluation=_evaluation_2y(
            [
                {"year": 2, "amount": "100", "currency": "CNY"},
            ]
        ),
        idempotency_key="op-taxref-pos",
    )
    assert status == 400, body
    assert body["error"]["code"] == "TASK-REQ-015"
    assert body["error"]["params"]["year"] == 2
    assert body["error"]["params"]["position"] == 2
    assert db.execute(select(func.count(Task.id))).scalar() == 0
    assert db.execute(select(func.count(CalcSnapshot.id))).scalar() == 0


def _sched_profile_payload() -> dict:
    from test_config_revisions import _tax_profile_payload

    payload = _tax_profile_payload()
    payload["profile"] = {**payload["profile"], "id": "cn-tax-sched", "region": "CN-Sched"}
    payload["tax_rules"]["building_vat"] = {
        "kind": "construction_input_vat",
        "label_ref": "vat_label",
        "for": "pv_system.upfront_capex",
        "rate": {"value": "0.09", "unit": "1"},
        "deductible_ratio": {"value": "0.8", "unit": "1"},
        "credit_pool": "main_pool",
        "confirmation": {
            "method": "explicit_schedule",
            "schedule": [{"period": 1, "ratio": "0.6"}, {"period": 13, "ratio": "0.4"}],
        },
    }
    return payload


def _sched_refund_overrides(schedule: list | None, refund_period: int, arrival: int) -> dict:
    patch: dict = {
        "schema": "ies.finance-overrides",
        "schema_version": "4.0.0",
        "profile_ref": {"id": "cn-tax-sched"},
        "credit_pools": {
            "main_pool": {
                "refunds": [
                    {
                        "period": refund_period,
                        "amount": {"value": "1500", "unit": "CNY"},
                        "arrival_period": arrival,
                    }
                ],
            },
        },
    }
    if schedule is not None:
        patch["tax_rules"] = {"building_vat": {"confirmation": {"schedule": schedule}}}
    return patch


def test_submit_tax_schedule_and_refund_bounds(client: TestClient, db: Session) -> None:
    """确认期边界：period=12E 合法且晚到账通过本项(201)；超上界 015，不再建任务。"""
    from assembly_fixture import save_compute_planning

    owner = make_user(db, "owner_taxsched")
    pid, device_ref = _prepare_taxref_project(client, db, owner, _sched_profile_payload())
    evaluation = _evaluation_2y([])

    _put_project_overrides(
        client, pid, owner, _sched_refund_overrides([{"period": 24, "ratio": "1"}], 24, 30), 1
    )
    save_compute_planning(db, pid, owner.id, device_ref)
    rev = _save_config(client, pid, owner)
    status, body = _submit_task(
        client,
        pid,
        owner,
        config={"config_revision": rev},
        evaluation=evaluation,
        idempotency_key="op-taxsched-ok",
    )
    assert status == 201, body
    assert body["task"]["status"] == "queued"
    assert db.execute(select(func.count(Task.id))).scalar() == 1
    assert db.execute(select(func.count(CalcSnapshot.id))).scalar() == 1

    _put_project_overrides(
        client, pid, owner, _sched_refund_overrides([{"period": 25, "ratio": "1"}], 24, 30), 2
    )
    save_compute_planning(db, pid, owner.id, device_ref)
    status, body = _submit_task(
        client,
        pid,
        owner,
        config={"config_revision": rev},
        evaluation=evaluation,
        idempotency_key="op-taxsched-period",
    )
    assert status == 400, body
    assert body["error"]["code"] == "TASK-REQ-015"
    assert body["error"]["params"]["period"] == 25
    assert db.execute(select(func.count(Task.id))).scalar() == 1
    assert db.execute(select(func.count(CalcSnapshot.id))).scalar() == 1

    _put_project_overrides(client, pid, owner, _sched_refund_overrides(None, 25, 26), 3)
    save_compute_planning(db, pid, owner.id, device_ref)
    status, body = _submit_task(
        client,
        pid,
        owner,
        config={"config_revision": rev},
        evaluation=evaluation,
        idempotency_key="op-taxsched-refund",
    )
    assert status == 400, body
    assert body["error"]["code"] == "TASK-REQ-015"
    assert body["error"]["params"]["pool"] == "main_pool"
    assert body["error"]["params"]["period"] == 25
    assert db.execute(select(func.count(Task.id))).scalar() == 1
    assert db.execute(select(func.count(CalcSnapshot.id))).scalar() == 1


def test_idempotency_key_scoped_to_project(client: TestClient, db: Session) -> None:
    """幂等键必须限定项目范围(RR-P1-05)。

    同一幂等键在不同项目下必须创建各自任务; 跨项目复用既有任务是
    拒绝服务/数据泄漏语义错误(前端幂等键由 config+params 哈希生成,
    跨项目相同, 命中他项目任务会让列表为空、详情 404)。
    """
    owner = make_user(db, "owner_scope")
    pid_a = _prepare_project(client, db, owner, name="项目A")
    pid_b = _prepare_project(client, db, owner, name="项目B")
    rev_a = _save_config(client, pid_a, owner)
    rev_b = _save_config(client, pid_b, owner)
    key = "sub-calc-shared-key"

    status, body = _submit_task(
        client, pid_a, owner, config={"config_revision": rev_a}, idempotency_key=key
    )
    assert status == 201
    task_a = body["task"]

    # 同键在另一项目提交 → 必须 201 新建, 绝不能 replay 项目 A 的任务
    status, body = _submit_task(
        client, pid_b, owner, config={"config_revision": rev_b}, idempotency_key=key
    )
    assert status == 201, f"跨项目幂等键必须新建任务, 实际 {status}: {body}"
    assert body["replayed"] is False
    task_b = body["task"]
    assert task_b["id"] != task_a["id"]

    # 项目 B 列表只含自己的任务; 项目 A 的详情在 B 下不可见(404)
    resp = client.get(f"/api/projects/{pid_b}/tasks", headers=_h(client, owner))
    assert resp.status_code == 200
    ids = [it["id"] for it in resp.json()["items"]]
    assert task_b["id"] in ids and task_a["id"] not in ids
    resp = client.get(f"/api/projects/{pid_b}/tasks/{task_a['id']}", headers=_h(client, owner))
    assert resp.status_code == 404

    # 同项目内同键仍幂等(replay 语义不回归)
    status, body = _submit_task(
        client, pid_b, owner, config={"config_revision": rev_b}, idempotency_key=key
    )
    assert status == 200
    assert body["replayed"] is True
    assert body["task"]["id"] == task_b["id"]


# ---------------------------------------------------------------------------
# 状态推进(可注入假执行器)
# ---------------------------------------------------------------------------


def test_state_advance_with_fake_executor(client: TestClient, db: Session) -> None:
    """queued → running(尝试+租约+槽) → 进度 → completed(结局映射)。"""
    owner = make_user(db, "owner_exec")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)
    status, body = _submit_task(
        client, pid, owner, config={"config_revision": rev}, idempotency_key="exec-1"
    )
    assert status == 201
    task_id = body["task"]["id"]

    # 领取: 槽占用 + 尝试 running + 租约 active(fencing token)
    claim = _claim(db, task_id, "fake-exec-1")
    assert claim is not None
    task = db.get(Task, task_id)
    assert task.status == "running"
    assert task.attempt_count == 1
    lease = db.execute(select(TaskLease).where(TaskLease.attempt_id == claim.attempt_id)).scalar_one()
    assert lease.status == "active"
    assert str(lease.lease_token) == str(claim.lease_token)
    slot = db.execute(
        select(ComputeSlot).where(ComputeSlot.current_attempt_id == claim.attempt_id)
    ).scalar_one()
    assert slot.in_use == 1

    # 进度: PG 持久进度 + Redis 秒级进度
    _record_task_progress(db, task_id, "solve", 45.5, {"iterations": 120})
    db.commit()
    row = db.execute(select(TaskProgress).where(TaskProgress.attempt_id == claim.attempt_id)).scalar_one()
    assert float(row.progress_percent) == 45.5
    live = queue.get_progress(task_id, claim.attempt_no)
    assert float(live["percent"]) == 45.5

    # 完成: OPTIMAL → normal_completion; 尝试 succeeded; 租约 released; 槽释放
    completed = _complete_task(db, task_id, outcome=tasks_domain.map_business_outcome("OPTIMAL"))
    db.commit()
    assert completed.status == "completed"
    assert completed.business_outcome == "normal_completion"
    assert db.get(TaskLease, lease.id).status == "released"
    assert db.execute(select(ComputeSlot)).scalars().all()[0].in_use == 0
    # 重复完成幂等
    again = _complete_task(db, task_id, outcome=tasks_domain.map_business_outcome("OPTIMAL"))
    db.commit()
    assert again.status == "completed"

    # 详情: 尝试历史 / 进度 / 快照摘要
    resp = client.get(f"/api/projects/{pid}/tasks/{task_id}", headers=_h(client, owner))
    assert resp.status_code == 200
    detail = resp.json()["task"]
    assert detail["status"] == "completed"
    assert detail["business_outcome"] == "normal_completion"
    assert detail["attempts"][0]["status"] == "succeeded"
    assert detail["progress"]["percent"] == 45.5
    assert detail["calc_snapshot"]["id"] == detail["calc_snapshot_id"]
    assert detail["calc_snapshot"]["random_seed"] is not None
    assert detail["current_lease"] is None  # 终态无活跃租约


def test_state_machine_guards(client: TestClient, db: Session) -> None:
    """终态不可迁移: 对已终态任务调用完成/失败/取消均被拒绝。"""
    owner = make_user(db, "owner_guard")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)
    _, body = _submit_task(
        client, pid, owner, config={"config_revision": rev}, idempotency_key="guard-1"
    )
    task_id = body["task"]["id"]
    _run_task(db, task_id)
    with pytest.raises(Exception) as exc_info:
        tasks_uc.cancel_task(db, task_id, reason="late-cancel")
    assert exc_info.value.http_status == 409  # CancelDeniedError
    with pytest.raises(Exception) as exc_info:
        _fail_task(db, task_id, code="TASK-SOLVE-001", message="late-fail")
    assert exc_info.value.http_status == 409


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------


def test_cancel_flow(client: TestClient, db: Session) -> None:
    """queued 直接取消; running 经 cancelling; 终态取消 409; 信号/出队清理。"""
    owner = make_user(db, "owner_cancel")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)

    # queued → 直接 cancelled, 队列消息移除
    _, body = _submit_task(
        client, pid, owner, config={"config_revision": rev}, idempotency_key="cancel-1"
    )
    queued_id = body["task"]["id"]
    assert queue.queue_position(queued_id, "compute") == 0
    resp = client.post(f"/api/projects/{pid}/tasks/{queued_id}/cancel", headers=_h(client, owner))
    assert resp.status_code == 200
    assert resp.json()["cancel_status"] == "cancelled"
    assert queue.queue_position(queued_id, "compute") is None

    # running → cancelling(发信号) → acknowledge → cancelled
    _, body = _submit_task(
        client, pid, owner, config={"config_revision": rev}, idempotency_key="cancel-2"
    )
    running_id = body["task"]["id"]
    _claim(db, running_id)
    resp = client.post(f"/api/projects/{pid}/tasks/{running_id}/cancel", headers=_h(client, owner))
    assert resp.status_code == 200
    assert resp.json()["cancel_status"] == "cancelling"
    assert queue.get_cancel(running_id) == "user_cancel"
    # 重复取消幂等
    resp = client.post(f"/api/projects/{pid}/tasks/{running_id}/cancel", headers=_h(client, owner))
    assert resp.status_code == 200 and resp.json()["cancel_status"] == "cancelling"
    # Worker 收拢确认
    task = tasks_uc.acknowledge_cancel(db, running_id)
    db.commit()
    assert task.status == "cancelled"
    assert queue.get_cancel(running_id) is None

    # 终态任务取消 → 409 + cancel_denied 诊断
    _, body = _submit_task(
        client, pid, owner, config={"config_revision": rev}, idempotency_key="cancel-3"
    )
    done_id = body["task"]["id"]
    _run_task(db, done_id)
    resp = client.post(f"/api/projects/{pid}/tasks/{done_id}/cancel", headers=_h(client, owner))
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "TASK-CANCEL-001"
    assert error["message_key"] == "ies.diag.task.cancel_denied"


# ---------------------------------------------------------------------------
# 重试(复用同一快照)
# ---------------------------------------------------------------------------


def test_retry_reuses_same_snapshot(client: TestClient, db: Session) -> None:
    """失败任务手动重试: 复用同一 calc_snapshot_id, 新尝试编号递增; 已完成任务不可重开。"""
    owner = make_user(db, "owner_retry")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)
    _, body = _submit_task(
        client, pid, owner, config={"config_revision": rev}, idempotency_key="retry-1"
    )
    task_id = body["task"]["id"]
    snapshot_id = body["task"]["calc_snapshot_id"]
    _claim(db, task_id)
    _fail_task(db, task_id)
    db.commit()

    resp = client.post(f"/api/projects/{pid}/tasks/{task_id}/retry", headers=_h(client, owner))
    assert resp.status_code == 200
    retried = resp.json()["task"]
    assert retried["status"] == "queued"
    assert retried["business_outcome"] is None
    assert retried["calc_snapshot_id"] == snapshot_id  # 重试复用同一快照

    # 再次领取: attempt_no/attempt_count 递增
    claim = _claim(db, task_id, "fake-exec-2")
    assert claim.attempt_no == 2
    task = db.get(Task, task_id)
    assert task.attempt_count == 2
    # 第二次完成
    _complete_task(db, task_id, outcome=tasks_domain.map_business_outcome("TIME_LIMIT_WITH_INCUMBENT"))
    db.commit()
    assert db.get(Task, task_id).status == "completed"
    assert db.get(Task, task_id).business_outcome == "restricted_results"
    # 快照始终只有 1 个(复用)
    snapshots = db.execute(select(CalcSnapshot)).scalars().all()
    assert len(snapshots) == 1

    # 已完成任务重试 → 409, 无任何变更(不重开、不入队)
    resp = client.post(f"/api/projects/{pid}/tasks/{task_id}/retry", headers=_h(client, owner))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "TASK-STATE-001"
    done = db.get(Task, task_id)
    assert done.status == "completed"
    assert done.business_outcome == "restricted_results"
    assert queue.queue_position(task_id, "compute") is None


def test_retry_clears_stale_cancel_signal(client: TestClient, db: Session) -> None:
    """重试清残留取消信号: 失败落终态前的未观察取消请求不得取消新尝试。"""
    owner = make_user(db, "owner_retry3")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)
    _, body = _submit_task(
        client, pid, owner, config={"config_revision": rev}, idempotency_key="retry-3"
    )
    task_id = body["task"]["id"]
    _claim(db, task_id)
    queue.set_cancel(task_id, "user_cancel")
    _fail_task(db, task_id)
    db.commit()
    assert queue.get_cancel(task_id) == "user_cancel"

    resp = client.post(f"/api/projects/{pid}/tasks/{task_id}/retry", headers=_h(client, owner))
    assert resp.status_code == 200
    assert resp.json()["task"]["status"] == "queued"
    assert queue.get_cancel(task_id) is None


def test_retry_denied_for_running(client: TestClient, db: Session) -> None:
    """非终态任务重试被拒。"""
    owner = make_user(db, "owner_retry2")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)
    _, body = _submit_task(
        client, pid, owner, config={"config_revision": rev}, idempotency_key="retry-2"
    )
    task_id = body["task"]["id"]
    _claim(db, task_id)
    resp = client.post(f"/api/projects/{pid}/tasks/{task_id}/retry", headers=_h(client, owner))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "TASK-STATE-001"


# ---------------------------------------------------------------------------
# 并发槽限制(compute 默认 2)
# ---------------------------------------------------------------------------


def test_slot_limit_two_concurrent(client: TestClient, db: Session) -> None:
    """默认 2 个计算并发槽: 第 3 个任务保持 queued; 释放后继续领取。"""
    owner = make_user(db, "owner_slot")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)
    task_ids: list[int] = []
    for i in range(3):
        _, body = _submit_task(
            client, pid, owner, config={"config_revision": rev}, idempotency_key=f"slot-{i}"
        )
        task_ids.append(body["task"]["id"])

    # 前两个任务各占一槽 → running
    c1 = _claim(db, task_ids[0], "fake-w1")
    c2 = _claim(db, task_ids[1], "fake-w2")
    assert c1 is not None and c2 is not None
    assert db.get(Task, task_ids[0]).status == "running"
    assert db.get(Task, task_ids[1]).status == "running"

    # 第 3 个: 无空槽 → 领取失败, 保持 queued
    c3 = tasks_uc.claim_task(db, task_ids[2], "fake-w3")
    db.commit()
    assert c3 is None
    assert db.get(Task, task_ids[2]).status == "queued"

    # 释放一槽(完成 t1)后第 3 个可领取; 池总占用回到 1
    _complete_task(db, task_ids[0], outcome=tasks_domain.map_business_outcome("OPTIMAL"))
    db.commit()
    slots = db.execute(select(ComputeSlot)).scalars().all()
    assert sum(s.in_use for s in slots) == 1
    c3 = _claim(db, task_ids[2], "fake-w3")
    assert c3 is not None
    assert db.get(Task, task_ids[2]).status == "running"
    slots = db.execute(select(ComputeSlot)).scalars().all()
    assert sum(s.in_use for s in slots) == 2


# ---------------------------------------------------------------------------
# 存储门禁
# ---------------------------------------------------------------------------


def test_storage_gate_blocks_and_suggests(client: TestClient, db: Session) -> None:
    """存储门禁: 配额不足 → 409 SYS-STORE-003 + 清理建议, 不创建任务/快照。"""
    owner = make_user(db, "owner_gate")
    # 数据集对象: 8 MB 已用, 配额仅 1 字节 → 可用空间 ≈ 0
    pid = _prepare_project(client, db, owner, dataset_size=8_000_000, quota_bytes=1)
    rev = _save_config(client, pid, owner)

    status, body = _submit_task(
        client, pid, owner, config={"config_revision": rev}, idempotency_key="gate-1"
    )
    assert status == 409
    error = body["error"]
    assert error["code"] == "SYS-STORE-003"
    assert error["message_key"] == "ies.diag.store.quota_exceeded"
    assert error["severity"] == "blocking"
    assert error["params"]["need_bytes"] > error["params"]["avail_bytes"]
    suggestions = error["params"]["suggestions"]
    assert suggestions and suggestions[0]["action"] == "cleanup_orphaned_objects"
    # 未创建任务与快照
    assert db.execute(select(func.count(Task.id))).scalar() == 0
    assert db.execute(select(func.count(CalcSnapshot.id))).scalar() == 0

    # 配额放大后同输入可提交(门禁放行; 快照新建)
    objs = db.execute(select(StoredObject).where(StoredObject.quota_bytes == 1)).scalars().all()
    assert objs, "种子样例对象缺失"
    for obj in objs:
        obj.quota_bytes = 10**18
    db.commit()
    status, body = _submit_task(
        client, pid, owner, config={"config_revision": rev}, idempotency_key="gate-1"
    )
    assert status == 201
    assert body["task"]["status"] == "queued"
    assert db.execute(select(func.count(CalcSnapshot.id))).scalar() == 1


# ---------------------------------------------------------------------------
# report(io 池)与队列/心跳/进度/取消信号
# ---------------------------------------------------------------------------


def test_report_task_goes_to_io_queue(client: TestClient, db: Session) -> None:
    """report(结果检查)为 io 类任务: 无快照, 进入 io 队列。"""
    owner = make_user(db, "owner_report")
    pid = _prepare_project(client, db, owner)
    status, body = _submit_task(client, pid, owner, task_type="report", idempotency_key="rpt-1")
    assert status == 201
    task = body["task"]
    assert task["type"] == "report"
    assert task["calc_snapshot_id"] is None
    # 在 io 队列而非 compute 队列
    assert queue.dequeue("io") == task["id"]
    assert queue.dequeue("compute") is None


def test_queue_heartbeat_progress_cancel(db: Session) -> None:
    """队列服务(内存后端): 入队/出队/重入队/位次 + 心跳 + 进度 + 取消信号。"""
    assert queue.queue_status()["backend"] == "memory"

    queue.enqueue(101, "compute", task_type="optimization", priority=5, trace_id="trc-101")
    queue.enqueue(102, "compute")
    assert queue.queue_position(101, "compute") == 0
    assert queue.queue_position(102, "compute") == 1
    assert queue.dequeue("compute") == 101
    queue.requeue(102, "compute")  # 已在队中 → 移到队尾, 不重复
    assert queue.dequeue("compute") == 102
    queue.requeue(101, "compute")  # 已出队 → 重建消息
    assert queue.dequeue("compute") == 101
    assert queue.dequeue("compute") is None

    # Worker 心跳(带 TTL)
    queue.set_heartbeat("cw-1", {"host": "node-a", "pool": "compute", "load": {"cpu": 0.3}}, ttl=15)
    hb = queue.get_heartbeat("cw-1")
    assert hb is not None and hb["host"] == "node-a"

    # 秒级进度
    queue.set_progress(101, 1, 45.5, "solve", {"iterations": 1200})
    live = queue.get_progress(101, 1)
    assert float(live["percent"]) == 45.5
    assert live["stage"] == "solve"

    # 取消信号
    queue.set_cancel(101, "user_cancel")
    assert queue.get_cancel(101) == "user_cancel"
    queue.clear_cancel(101)
    assert queue.get_cancel(101) is None


# ---------------------------------------------------------------------------
# analysis 任务类型(03 §8 / §9.7: 批量分析注册与执行)
# ---------------------------------------------------------------------------


def test_analysis_task_type_registered(client: TestClient, db: Session) -> None:
    """analysis 任务类型: 创建成功、入 compute 队列、快照绑定。"""
    owner = make_user(db, "owner_analysis")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)
    status, body = _submit_task(
        client, pid, owner, task_type="analysis",
        config={"config_revision": rev,
                "sweeps": [{"param_path": "calc_config.irr_floor",
                            "values": [0.06, 0.08], "unit": "-"}]},
        idempotency_key="analysis-1",
    )
    assert status == 201, body
    task = body["task"]
    assert task["type"] == "analysis"
    assert task["calc_snapshot_id"] is not None  # 计算类任务绑定快照
    assert queue.dequeue("compute") == task["id"]


def test_analysis_task_rejected_without_sweeps(client: TestClient, db: Session) -> None:
    """analysis 任务缺 sweeps → 400(参数非法)。"""
    owner = make_user(db, "owner_analysis_bad")
    pid = _prepare_project(client, db, owner)
    status, body = _submit_task(
        client, pid, owner, task_type="analysis", config={}, idempotency_key="analysis-bad"
    )
    assert status == 400
    assert body["error"]["code"] == "TASK-REQ-001"
