"""对象存储服务与 API 集成测试(U11): 写入/去重/引用/清理/校验/门禁/管理接口。

运行方式: 内存 SQLite(StaticPool, 跨线程共享) + 临时 data_dir +
app.dependency_overrides 替换 get_db 依赖(不触碰真实数据库)。
覆盖: 写入与落盘、同内容去重、引用计数、无引用清理、被引用不可清理、
哈希校验失败报错、容量估算与管理 API(仅管理员)。
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from iesplan.api.auth import router as auth_router
from iesplan.api.objects import router as objects_router
from iesplan.config import settings
from iesplan.core.errors import AppError
from iesplan.db import Base, get_db
from iesplan.main import create_app
from iesplan.models.audit import AuditLog, RetentionRule, StoredObject
from iesplan.models.identity import User
from iesplan.services import identity, objects

ADMIN_PASSWORD = "Admin12345"
USER_PASSWORD = "Alice12345"


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Iterator[sa.Engine]:
    """SQLite :memory: 引擎(StaticPool: 跨线程共享同一连接)。"""
    eng = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine) -> Iterator[Session]:
    """共享会话(expire_on_commit=False: 提交后属性保持可读)。"""
    with Session(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture()
def data_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """临时对象存储根目录(monkeypatch 替换配置, 不触碰真实 /data)。"""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(settings, "data_dir", d)
    return d


@pytest.fixture()
def client(session: Session, data_dir) -> Iterator[TestClient]:
    """测试客户端: 挂载认证 + 对象管理路由, 替换 get_db 依赖。"""
    app = create_app()
    app.include_router(auth_router)
    app.include_router(objects_router)

    def _override_get_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    identity.reset_login_rate_limit()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def seed_admin(session: Session) -> User:
    """创建内置管理员(与 seed_admin 语义一致; 测试内不再强制改密,
    等价于已改密的正式管理员, 避免 C-02 强制改密门禁阻断管理端点)。"""
    return identity.create_user(
        session, "admin", ADMIN_PASSWORD, role="admin", display_name="管理员",
        force_password_change=False,
    )


def seed_engineer(session: Session, username: str = "alice") -> User:
    """创建普通工程师(首登不强制改密)。"""
    return identity.create_user(
        session, username, USER_PASSWORD, role="engineer", force_password_change=False
    )


def login(client: TestClient, username: str, password: str):
    """登录并返回 Authorization 头。"""
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _put(session: Session, content: bytes, **kw) -> StoredObject:
    """便捷写入对象(返回对象, 由调用方决定提交)。"""
    return objects.put_object(
        session, content, content_type=kw.pop("content_type", "application/octet-stream"),
        source_category=kw.pop("source_category", "test"), **kw,
    )


def _count_audit(session: Session, action: str, entity_type: str = "objects") -> int:
    """统计指定审计动作条数。"""
    return session.execute(
        sa.select(sa.func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == action, AuditLog.entity_type == entity_type)
    ).scalar_one()


# ---------------------------------------------------------------------------
# 写入与落盘
# ---------------------------------------------------------------------------


def test_put_object_writes_file_and_record(session: Session, data_dir) -> None:
    """写入: 文件原子落盘到 data_dir/objects/{sha256}, 记录含大小/哈希/类型/来源审计。"""
    content = b"hello-object-storage" * 10
    obj = _put(session, content, content_type="text/plain", source_category="user_upload")
    session.commit()

    digest = objects.sha256_hex(content)
    # 对象行: 内容寻址字段齐全
    assert obj.oid == digest
    assert obj.sha256 == digest
    assert obj.size_bytes == len(content)
    assert obj.media_type == "text/plain"
    assert obj.status == "stored"
    assert obj.ref_count == 0
    # 文件在最终位置, 临时区无残留(原子 rename)
    path = data_dir / "objects" / digest
    assert path.read_bytes() == content
    assert list((data_dir / "objects" / "tmp").iterdir()) == []
    # 来源类别记入创建审计
    assert _count_audit(session, "object_created") == 1
    audit = session.execute(
        sa.select(AuditLog).where(AuditLog.action == "object_created")
    ).scalar_one()
    assert audit.after["source_category"] == "user_upload"
    # object_info 与行一致
    info = objects.object_info(session, obj.id)
    assert info["oid"] == digest and info["size_bytes"] == len(content)


def test_put_object_dedup_same_content(session: Session, data_dir) -> None:
    """去重: 同内容同 sha256 只存一份, 复用既有对象记录与文件。"""
    content = b"same-content-bytes" * 5
    obj1 = _put(session, content)
    obj2 = _put(session, content)
    session.commit()
    assert obj1.id == obj2.id  # 复用同一行
    rows = session.execute(sa.select(StoredObject)).scalars().all()
    assert len(rows) == 1
    assert len(list((data_dir / "objects").glob("*.tmp"))) == 0
    files = [p for p in (data_dir / "objects").iterdir() if p.is_file()]
    assert len(files) == 1  # 磁盘也仅一份


def test_put_object_dedup_still_records_business_ref(session: Session, data_dir) -> None:
    """去重: 复用对象记录, 但传入的业务引用仍按 object_refs 单独建立(RPD 23.1)。"""
    content = b"dedup-with-ref" * 3
    obj1 = _put(session, content, ref_type="dataset_file", ref_id=10)
    obj2 = _put(session, content, ref_type="dataset_file", ref_id=11)
    session.commit()
    assert obj1.id == obj2.id
    refs = objects.list_refs(session, obj1.id)
    assert len(refs) == 2  # 两个业务实体分别引用
    assert {r.ref_entity_id for r in refs} == {10, 11}
    assert obj1.ref_count == 2


def test_get_object_returns_bytes(session: Session, data_dir) -> None:
    """读取: 返回原始字节(读取时校验大小与哈希)。"""
    content = b"get-me-back" * 20
    obj = _put(session, content)
    session.commit()
    assert objects.get_object(session, obj.id) == content
    assert objects.get_object(session, obj.oid) == content  # 也支持 oid 查询


# ---------------------------------------------------------------------------
# 引用
# ---------------------------------------------------------------------------


def test_add_remove_list_refs(session: Session, data_dir) -> None:
    """引用: add_ref 递增计数 / remove_ref 递减 / list_refs 枚举; 归零置 orphaned。"""
    obj = _put(session, b"ref-target" * 4)
    session.commit()
    ref = objects.add_ref(session, obj.id, "version_ref", 7, purpose="版本发布")
    session.commit()
    assert ref.ref_entity_type == "project_versions"  # REF_ENTITY_TYPE_MAP 推断
    assert obj.ref_count == 1
    assert len(objects.list_refs(session, obj.id)) == 1

    # 重复引用幂等: 不重复计数
    objects.add_ref(session, obj.id, "version_ref", 7)
    session.commit()
    assert obj.ref_count == 1

    objects.remove_ref(session, obj.id, "version_ref", 7)
    session.commit()
    assert obj.ref_count == 0
    assert obj.status == "orphaned"  # 引用归零 → orphaned(01 §10.1)
    assert objects.list_refs(session, obj.id) == []


def test_remove_ref_unknown_raises(session: Session, data_dir) -> None:
    """解除不存在的引用抛 NotFoundError。"""
    obj = _put(session, b"no-ref" * 3)
    session.commit()
    with pytest.raises(AppError) as exc:
        objects.remove_ref(session, obj.id, "report", 1)
    assert exc.value.http_status == 404


# ---------------------------------------------------------------------------
# 完整性校验
# ---------------------------------------------------------------------------


def test_get_object_corrupt_hash_raises(session: Session, data_dir) -> None:
    """哈希校验失败报错: 文件被篡改后读取抛 ObjectCorruptError。"""
    content = b"pristine-content" * 8
    obj = _put(session, content)
    session.commit()
    # 篡改磁盘文件(记录哈希不变)
    path = data_dir / "objects" / obj.sha256
    path.write_bytes(b"tampered!" + content[9:])
    with pytest.raises(objects.ObjectCorruptError) as exc:
        objects.get_object(session, obj.id)
    assert exc.value.code == "OBJ-CORRUPT-001"


def test_get_object_missing_file_raises(session: Session, data_dir) -> None:
    """文件缺失读取报错(视作完整性失败)。"""
    obj = _put(session, b"gone-soon" * 6)
    session.commit()
    (data_dir / "objects" / obj.sha256).unlink()
    with pytest.raises(objects.ObjectCorruptError):
        objects.get_object(session, obj.id)


def test_verify_object_reports_corruption(session: Session, data_dir) -> None:
    """verify_object 不抛错, 返回 ok=False 报告(周期性巡检用)。"""
    content = b"verify-me" * 12
    obj = _put(session, content)
    session.commit()
    good = objects.verify_object(session, obj.id)
    assert good["ok"] is True and good["hash_ok"] is True and good["size_ok"] is True

    (data_dir / "objects" / obj.sha256).write_bytes(b"xx")
    bad = objects.verify_object(session, obj.id)
    assert bad["ok"] is False and bad["error"] == "ies.diag.obj.corrupt"

    (data_dir / "objects" / obj.sha256).unlink()
    missing = objects.verify_object(session, obj.id)
    assert missing["ok"] is False


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------


def test_safe_cleanup_dry_run_plan(session: Session, data_dir) -> None:
    """清理计划: dry_run 只列出无引用对象, 不删任何数据。"""
    orphan = _put(session, b"orphan-1" * 5)
    kept = _put(session, b"kept-1" * 5)
    objects.add_ref(session, kept.id, "snapshot_ref", 3)
    session.commit()

    plan = objects.safe_cleanup(session, dry_run=True)
    assert plan["dry_run"] is True
    assert plan["count"] == 1
    assert plan["candidates"][0]["id"] == orphan.id
    assert plan["total_bytes"] == orphan.size_bytes
    # 数据未动
    assert (data_dir / "objects" / orphan.sha256).exists()
    assert session.get(StoredObject, orphan.id) is not None


def test_safe_cleanup_execute_removes_unreferenced(session: Session, data_dir) -> None:
    """执行清理: 删文件 + 删记录 + 审计; 被引用对象(项目版本引用)不可清理。"""
    orphan = _put(session, b"orphan-2" * 7)
    kept = _put(session, b"kept-version" * 7, ref_type="version_ref", ref_id=42)  # project_versions
    session.commit()

    result = objects.safe_cleanup(session, dry_run=False)
    assert result["removed_count"] == 1
    assert result["errors"] == []
    # 无引用对象: 文件与记录均已删除
    assert not (data_dir / "objects" / orphan.sha256).exists()
    assert session.get(StoredObject, orphan.id) is None
    # 被项目版本引用的对象保留(23.2)
    assert (data_dir / "objects" / kept.sha256).exists()
    assert session.get(StoredObject, kept.id) is not None
    assert kept.ref_count == 1
    # 清理审计(01 §10.3)
    assert _count_audit(session, "object_cleanup") == 1
    audit = session.execute(
        sa.select(AuditLog).where(AuditLog.action == "object_cleanup")
    ).scalar_one()
    assert audit.entity_id == orphan.id and audit.before["sha256"] == orphan.sha256


def test_safe_cleanup_keeps_referenced_and_protected(session: Session, data_dir) -> None:
    """被引用对象不可清理: 项目版本/快照/证据包/报告引用的对象均不在计划中。"""
    objects_to_ref = [
        ("version_ref", 1, "project_versions"),
        ("snapshot_ref", 2, "calc_snapshots"),
        ("evidence_package", 3, "evidence_packages"),
        ("report", 4, "reports"),
        ("dataset_file", 5, "dataset_files"),
    ]
    for ref_type, ref_id, _entity in objects_to_ref:
        obj = _put(session, f"protected-{ref_type}-{ref_id}".encode() * 3)
        objects.add_ref(session, obj.id, ref_type, ref_id)
    session.commit()

    plan = objects.safe_cleanup(session, dry_run=True)
    assert plan["count"] == 0
    assert plan["retained_count"] == 0  # 被引用对象不是"保留", 而是天然不可清理


def test_safe_cleanup_retention_rule_holds(session: Session, data_dir) -> None:
    """保留规则: 命中 active 的 retention_rules 时, 孤儿对象在保留期内不可清理。"""
    sys_user = User(username="sysop", display_name="系统")
    session.add(sys_user)
    session.flush()
    session.add(
        RetentionRule(
            entity_type="objects",
            object_kind="*",
            retention_days=36500,
            apply_to="orphaned",
            status="active",
            created_by=sys_user.id,
        )
    )
    orphan = _put(session, b"young-orphan" * 4)
    session.commit()

    plan = objects.safe_cleanup(session, dry_run=True)
    assert plan["count"] == 0
    assert plan["retained_count"] == 1
    assert plan["retained"][0]["id"] == orphan.id


def test_safe_cleanup_missing_file_skipped(session: Session, data_dir) -> None:
    """文件已缺失的对象跳过删除, 记录保留以便重试。"""
    orphan = _put(session, b"no-file" * 3)
    session.commit()
    (data_dir / "objects" / orphan.sha256).unlink()
    result = objects.safe_cleanup(session, dry_run=False)
    assert result["removed_count"] == 0
    assert len(result["errors"]) == 1
    assert session.get(StoredObject, orphan.id) is not None


# ---------------------------------------------------------------------------
# 存储门禁
# ---------------------------------------------------------------------------


def test_estimate_storage_scales_with_inputs() -> None:
    """容量估算: 随时长/样本数单调不减, 且不小于下限。"""
    base = objects.estimate_storage("calc", 24, 1)
    assert base > 0
    assert objects.estimate_storage("calc", 48, 1) > base  # 时长加倍
    assert objects.estimate_storage("calc", 24, 100) >= base  # 样本增加
    assert objects.estimate_storage("calc", 0, 0) >= 1024  # 下限
    # 各任务类型均有估算值
    for task_type in ("calc", "optimization", "uncertainty", "import", "export", "report", "dataset_build"):
        assert objects.estimate_storage(task_type, 24, 10) > 0


def test_estimate_storage_unknown_type_raises() -> None:
    """未知任务类型抛 AppError。"""
    with pytest.raises(AppError):
        objects.estimate_storage("nope", 1, 1)


def test_check_capacity(session: Session, data_dir, monkeypatch: pytest.MonkeyPatch) -> None:
    """容量检查: free > 阈值 → ok; 不足 → ok=False 并给出提示。"""
    stub = SimpleNamespace(
        disk_usage=lambda _p: SimpleNamespace(free=settings.storage_min_free_bytes + 1_000_000)
    )
    monkeypatch.setattr(objects, "shutil", stub)
    res = objects.check_capacity(session)
    assert res["ok"] is True
    assert res["free_bytes"] > res["safe_threshold"]

    stub2 = SimpleNamespace(disk_usage=lambda _p: SimpleNamespace(free=100))
    monkeypatch.setattr(objects, "shutil", stub2)
    res2 = objects.check_capacity(session)
    assert res2["ok"] is False
    assert "不足" in res2["message"]


# ---------------------------------------------------------------------------
# 管理 API(仅管理员)
# ---------------------------------------------------------------------------


def test_api_storage_requires_auth(client: TestClient, session: Session) -> None:
    """未认证访问存储视图 → 401。"""
    resp = client.get("/api/admin/storage")
    assert resp.status_code == 401


def test_api_storage_forbidden_for_engineer(client: TestClient, session: Session) -> None:
    """非管理员访问存储视图 → 403。"""
    seed_engineer(session)
    headers = login(client, "alice", USER_PASSWORD)
    resp = client.get("/api/admin/storage", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["message_key"] == "ies.diag.perm.denied"


def test_api_storage_view(client: TestClient, session: Session, data_dir) -> None:
    """存储视图: 用量/对象数/引用数/健康(管理员)。"""
    seed_admin(session)
    headers = login(client, "admin", ADMIN_PASSWORD)
    _put(session, b"api-storage-1" * 3)
    kept = _put(session, b"api-storage-2" * 3)
    objects.add_ref(session, kept.id, "report", 9)
    session.commit()

    resp = client.get("/api/admin/storage", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["objects"]["count"] == 2
    assert body["objects"]["total_bytes"] == 2 * 13 * 3
    assert body["objects"]["by_status"].get("stored") == 2
    assert body["refs"]["count"] == 1
    assert body["refs"]["referenced_objects"] == 1
    assert body["capacity"]["ok"] is True
    assert body["healthy"] is True


def test_api_cleanup_plan_then_execute(client: TestClient, session: Session, data_dir) -> None:
    """两阶段清理: 先 dry_run 出计划, 再执行; 被引用对象不受影响。"""
    seed_admin(session)
    headers = login(client, "admin", ADMIN_PASSWORD)
    orphan = _put(session, b"api-orphan" * 5)
    kept = _put(session, b"api-kept" * 5, ref_type="evidence_package", ref_id=2)
    session.commit()

    # 阶段 1: 计划
    plan = client.post("/api/admin/objects/cleanup", json={"dry_run": True}, headers=headers)
    assert plan.status_code == 200
    p = plan.json()
    assert p["dry_run"] is True and p["count"] == 1
    assert p["candidates"][0]["id"] == orphan.id
    assert (data_dir / "objects" / orphan.sha256).exists()

    # 阶段 2: 执行
    done = client.post("/api/admin/objects/cleanup", json={"dry_run": False}, headers=headers)
    assert done.status_code == 200
    d = done.json()
    assert d["dry_run"] is False and d["removed_count"] == 1
    assert not (data_dir / "objects" / orphan.sha256).exists()
    assert (data_dir / "objects" / kept.sha256).exists()  # 被证据包引用, 保留


def test_api_health_reports_corruption(client: TestClient, session: Session, data_dir) -> None:
    """健康接口: 抽样校验发现被篡改对象。"""
    seed_admin(session)
    headers = login(client, "admin", ADMIN_PASSWORD)
    obj = _put(session, b"health-check" * 10)
    session.commit()

    healthy = client.get("/api/admin/health", headers=headers)
    assert healthy.status_code == 200
    assert healthy.json()["checked"] == 1 and healthy.json()["ok_count"] == 1

    (data_dir / "objects" / obj.sha256).write_bytes(b"corrupted-content!!!")
    broken = client.get("/api/admin/health", headers=headers)
    assert broken.status_code == 200
    body = broken.json()
    assert body["ok_count"] == 0
    assert len(body["failed"]) == 1
    assert body["failed"][0]["ok"] is False
