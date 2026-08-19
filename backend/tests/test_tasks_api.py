"""任务与资源调度(U08) API 与服务集成测试。

覆盖: 幂等创建(同键返回同任务)→ 状态推进(注入假执行器)→ 取消(queued 直接 /
running 经 cancelling)→ 重试同快照 → 槽限制(2 并发)→ 存储门禁(模拟空间不足)
→ report(io 队列)与队列/心跳/进度/取消信号读写。

- 数据库: SQLite :memory:(models 全部表 create_all, StaticPool 共享连接);
- 队列: IESPLAN_QUEUE=memory 强制内存后端(单进程, 无外部 Redis 依赖);
- 应用: create_app() + include_router(projects/tasks), dependency_overrides 替换 get_db;
- 假执行器: 测试直接调用 tasks_service.claim_and_run / record_progress /
  complete_task 等服务入口模拟 Worker 行为(Worker 消费端在下一波次实现)。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from hashlib import sha256
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
from iesplan.config import settings  # noqa: E402
from iesplan.db import Base, get_db  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.models.audit import StoredObject  # noqa: E402
from iesplan.models.calc import CalcSnapshot, ComputeSlot, Task, TaskLease, TaskProgress  # noqa: E402
from iesplan.models.dataset import Dataset, DatasetFile, DatasetVersion  # noqa: E402
from iesplan.models.identity import User  # noqa: E402
from iesplan.services import queue  # noqa: E402
from iesplan.services import tasks as tasks_service

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
    """测试客户端: 挂载项目+任务路由, 替换 get_db 依赖, 对象存储指向临时目录。"""
    settings.data_dir = tmp_path
    app = create_app()
    app.include_router(projects_api.router)
    app.include_router(tasks_api.router)

    def _override_get_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
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
    resp = client.post("/api/projects", json={"name": name}, headers=_h(client, user))
    assert resp.status_code == 201, resp.text
    return resp.json()["project"]["id"]


def _seed_dataset(db: Session, *, size_bytes: int = 0, quota_bytes: int = 0, tag: str = "ds") -> int:
    """直接建数据集版本 + 内容寻址对象 + 文件行, 返回 dataset_version_id。

    size_bytes 参与存储门禁的 S_snap 估算; quota_bytes 参与可用空间(配额)估算,
    模拟项目配额/空间不足。
    """
    user = db.execute(select(User).order_by(User.id)).scalars().first()
    assert user is not None
    dataset = Dataset(name=f"{tag}_数据集", created_by=user.id)
    db.add(dataset)
    db.flush()
    version = DatasetVersion(
        dataset_id=dataset.id, version_no=1, timeline="hourly", resolution="1h",
        fixed_utc_offset_minutes=480, fields={}, units={},
        content_hash=sha256(f"{tag}-v1".encode()).hexdigest(), created_by=user.id,
    )
    db.add(version)
    db.flush()
    obj = StoredObject(
        oid=sha256(f"{tag}-obj".encode()).hexdigest(),
        sha256=sha256(f"{tag}-obj".encode()).hexdigest(),
        size_bytes=size_bytes,
        quota_bytes=quota_bytes,
        status="stored",
        ref_count=1,
    )
    db.add(obj)
    db.flush()
    db.add(DatasetFile(
        dataset_version_id=version.id, object_id=obj.id, file_kind="data",
        format="csv", row_count=8760, size_bytes=size_bytes,
    ))
    db.commit()
    return version.id


def _bind_and_freeze(client: TestClient, pid: int, user, dataset_version_id: int | None) -> None:
    """绑定数据集版本(如给定)并从当前草稿创建不可变项目版本。"""
    if dataset_version_id is not None:
        resp = client.put(
            f"/api/projects/{pid}/draft",
            json={"expected_revision": 1, "commands": [{
                "id": "c-bind", "unit": "dataset", "type": "dataset.bind",
                "payload": {"dataset_version_id": dataset_version_id, "role": "main"},
            }]},
            headers=_h(client, user),
        )
        assert resp.status_code == 200, resp.text
    resp = client.post(
        f"/api/projects/{pid}/versions",
        json={"name": "计算基线", "reason": "snapshot_freeze"},
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text


def _prepare_project(
    client: TestClient, db: Session, user, *,
    dataset_size: int = 0, quota_bytes: int = 0, name: str = "任务测试项目",
) -> int:
    """准备可提交计算任务的项目: 建项目 + (可选)数据集 + 绑定 + 固化版本。"""
    pid = _create_project(client, user, name)
    dvid = None
    if dataset_size > 0:
        dvid = _seed_dataset(db, size_bytes=dataset_size, quota_bytes=quota_bytes, tag=f"ds{pid}")
    _bind_and_freeze(client, pid, user, dvid)
    return pid


def _submit_task(
    client: TestClient, pid: int, user, task_type: str = "optimization",
    config: dict[str, Any] | None = None, idempotency_key: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """提交任务, 返回 (HTTP 状态码, 响应体)。"""
    body: dict[str, Any] = {"task_type": task_type}
    if config is not None:
        body["config"] = config
    if idempotency_key is not None:
        body["idempotency_key"] = idempotency_key
    resp = client.post(f"/api/projects/{pid}/tasks", json=body, headers=_h(client, user))
    return resp.status_code, resp.json()


def _claim(db: Session, task_id: int, worker_id: str = "fake-exec-1") -> Any:
    """假执行器: 领取任务(占槽 + 建尝试 + 建租约 + running)。"""
    claim = tasks_service.claim_and_run(db, task_id, worker_id)
    db.commit()
    return claim


def _run_task(
    db: Session, task_id: int, worker_id: str = "fake-exec-1", solver_status: str = "OPTIMAL"
) -> Task:
    """假执行器: 完整推进一次任务(领取 → 进度 → 完成)。"""
    _claim(db, task_id, worker_id)
    tasks_service.record_progress(db, task_id, "solve", 50.0, {"iterations": 1})
    task = tasks_service.complete_task(db, task_id, solver_status=solver_status)
    db.commit()
    return task


# ---------------------------------------------------------------------------
# 幂等创建与快照去重
# ---------------------------------------------------------------------------


def test_idempotent_create_and_snapshot_dedup(client: TestClient, db: Session) -> None:
    """同幂等键返回同一任务; 同快照无键重复提交复用; 不同输入生成新快照。"""
    owner = make_user(db, "owner_idem")
    pid = _prepare_project(client, db, owner)

    # 1) 带幂等键创建 → 201
    status, body = _submit_task(client, pid, owner, idempotency_key="op-20260818-abc")
    assert status == 201
    task_a = body["task"]
    assert task_a["status"] == "queued"
    assert task_a["type"] == "optimization"
    assert body["replayed"] is False
    snapshot_a = task_a["calc_snapshot_id"]
    assert snapshot_a is not None

    # 2) 同键重发(客户端网络重试) → 200 返回同一任务
    status, body = _submit_task(client, pid, owner, idempotency_key="op-20260818-abc")
    assert status == 200
    assert body["replayed"] is True
    assert body["task"]["id"] == task_a["id"]
    assert body["task"]["calc_snapshot_id"] == snapshot_a

    # 3) 无键、同输入重复提交 → 200 复用(重复提交语义, 附提示)
    status, body = _submit_task(client, pid, owner)
    assert status == 200
    assert body["duplicate"] is True
    assert body["task"]["id"] == task_a["id"]
    assert body["hint"]

    # 4) 不同输入(config 不同 → 快照哈希不同) → 201 新任务新快照
    status, body = _submit_task(client, pid, owner, config={"horizon_years": 3})
    assert status == 201
    assert body["task"]["id"] != task_a["id"]
    assert body["task"]["calc_snapshot_id"] != snapshot_a

    # 5) 快照按内容 sha256 去重: 仅 2 个快照(同输入共享 1 个)
    snapshots = db.execute(select(CalcSnapshot)).scalars().all()
    assert len(snapshots) == 2
    by_id = {s.id: s for s in snapshots}
    assert by_id[snapshot_a].content_hash  # 64 位 hex
    assert len(by_id[snapshot_a].content_hash) == 64

    # 6) 列表可见 2 个任务(步骤 2/3 均为既有任务复用; 含摘要与排队位次)
    resp = client.get(f"/api/projects/{pid}/tasks", headers=_h(client, owner))
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    first = items[0]
    assert first["summary"]["queue_position"] is not None
    assert first["summary"]["percent"] == 0.0


# ---------------------------------------------------------------------------
# 状态推进(可注入假执行器)
# ---------------------------------------------------------------------------


def test_state_advance_with_fake_executor(client: TestClient, db: Session) -> None:
    """queued → running(尝试+租约+槽) → 进度 → completed(结局映射)。"""
    owner = make_user(db, "owner_exec")
    pid = _prepare_project(client, db, owner)
    status, body = _submit_task(client, pid, owner, idempotency_key="exec-1")
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
    tasks_service.record_progress(db, task_id, "solve", 45.5, {"iterations": 120})
    db.commit()
    row = db.execute(select(TaskProgress).where(TaskProgress.attempt_id == claim.attempt_id)).scalar_one()
    assert float(row.progress_percent) == 45.5
    live = queue.get_progress(task_id, claim.attempt_no)
    assert float(live["percent"]) == 45.5

    # 完成: OPTIMAL → normal_completion; 尝试 succeeded; 租约 released; 槽释放
    completed = tasks_service.complete_task(db, task_id, solver_status="OPTIMAL")
    db.commit()
    assert completed.status == "completed"
    assert completed.business_outcome == "normal_completion"
    assert db.get(TaskLease, lease.id).status == "released"
    assert db.execute(select(ComputeSlot)).scalars().all()[0].in_use == 0
    # 重复完成幂等
    again = tasks_service.complete_task(db, task_id, solver_status="OPTIMAL")
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
    assert detail["calc_snapshot"]["content_hash"]
    assert detail["current_lease"] is None  # 终态无活跃租约


def test_state_machine_guards(client: TestClient, db: Session) -> None:
    """终态不可迁移: 对已终态任务调用完成/失败/取消均被拒绝。"""
    owner = make_user(db, "owner_guard")
    pid = _prepare_project(client, db, owner)
    _, body = _submit_task(client, pid, owner, idempotency_key="guard-1")
    task_id = body["task"]["id"]
    _run_task(db, task_id)
    with pytest.raises(Exception) as exc_info:
        tasks_service.cancel_task(db, task_id, reason="late-cancel")
    assert exc_info.value.http_status == 409  # CancelDeniedError
    with pytest.raises(Exception) as exc_info:
        tasks_service.fail_task(db, task_id, code="TASK-SOLVE-001", message="late-fail")
    assert exc_info.value.http_status == 409


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------


def test_cancel_flow(client: TestClient, db: Session) -> None:
    """queued 直接取消; running 经 cancelling; 终态取消 409; 信号/出队清理。"""
    owner = make_user(db, "owner_cancel")
    pid = _prepare_project(client, db, owner)

    # queued → 直接 cancelled, 队列消息移除
    _, body = _submit_task(client, pid, owner, idempotency_key="cancel-1")
    queued_id = body["task"]["id"]
    assert queue.queue_position(queued_id, "compute") == 0
    resp = client.post(f"/api/projects/{pid}/tasks/{queued_id}/cancel", headers=_h(client, owner))
    assert resp.status_code == 200
    assert resp.json()["cancel_status"] == "cancelled"
    assert queue.queue_position(queued_id, "compute") is None

    # running → cancelling(发信号) → acknowledge → cancelled
    _, body = _submit_task(client, pid, owner, idempotency_key="cancel-2")
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
    task = tasks_service.acknowledge_cancel(db, running_id)
    db.commit()
    assert task.status == "cancelled"
    assert queue.get_cancel(running_id) is None

    # 终态任务取消 → 409 + cancel_denied 诊断
    _, body = _submit_task(client, pid, owner, idempotency_key="cancel-3")
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
    """终态任务手动重试: 复用同一 calc_snapshot_id, 新尝试编号递增。"""
    owner = make_user(db, "owner_retry")
    pid = _prepare_project(client, db, owner)
    _, body = _submit_task(client, pid, owner, idempotency_key="retry-1")
    task_id = body["task"]["id"]
    snapshot_id = body["task"]["calc_snapshot_id"]
    _run_task(db, task_id)

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
    tasks_service.complete_task(db, task_id, solver_status="TIME_LIMIT_WITH_INCUMBENT")
    db.commit()
    assert db.get(Task, task_id).status == "completed"
    assert db.get(Task, task_id).business_outcome == "restricted_results"
    # 快照始终只有 1 个(复用)
    snapshots = db.execute(select(CalcSnapshot)).scalars().all()
    assert len(snapshots) == 1


def test_retry_denied_for_running(client: TestClient, db: Session) -> None:
    """非终态任务重试被拒。"""
    owner = make_user(db, "owner_retry2")
    pid = _prepare_project(client, db, owner)
    _, body = _submit_task(client, pid, owner, idempotency_key="retry-2")
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
    task_ids: list[int] = []
    for i in range(3):
        _, body = _submit_task(client, pid, owner, idempotency_key=f"slot-{i}")
        task_ids.append(body["task"]["id"])

    # 前两个任务各占一槽 → running
    c1 = _claim(db, task_ids[0], "fake-w1")
    c2 = _claim(db, task_ids[1], "fake-w2")
    assert c1 is not None and c2 is not None
    assert db.get(Task, task_ids[0]).status == "running"
    assert db.get(Task, task_ids[1]).status == "running"

    # 第 3 个: 无空槽 → 领取失败, 保持 queued
    c3 = tasks_service.claim_and_run(db, task_ids[2], "fake-w3")
    db.commit()
    assert c3 is None
    assert db.get(Task, task_ids[2]).status == "queued"

    # 释放一槽(完成 t1)后第 3 个可领取; 池总占用回到 1
    tasks_service.complete_task(db, task_ids[0], solver_status="OPTIMAL")
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

    status, body = _submit_task(client, pid, owner, idempotency_key="gate-1")
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
    obj = db.execute(select(StoredObject).where(StoredObject.quota_bytes == 1)).scalar_one()
    obj.quota_bytes = 10**18
    db.commit()
    status, body = _submit_task(client, pid, owner, idempotency_key="gate-1")
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
