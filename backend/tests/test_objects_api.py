"""对象存储服务与 API 集成测试(U11): 写入/引用/直接清理/管理接口。

运行方式: 内存 SQLite(StaticPool, 跨线程共享) + 临时 data_dir +
app.dependency_overrides 替换 get_request_db 依赖(不触碰真实数据库)。
覆盖: 写入与落盘(每次写入新建对象, 按对象 id 寻址, 无内容去重)、
引用计数、无引用对象清理、业务保留规则、容量状态与管理员权限。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import fields

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from iesplan.api.auth import router as auth_router
from iesplan.api.deps import get_request_db
from iesplan.api.objects import router as objects_router
from iesplan.application import identity
from iesplan.config import settings
from iesplan.core.errors import AppError
from iesplan.db import Base
from iesplan.main import create_app
from iesplan.audit.persistence import AuditLog, RetentionRule
from iesplan.identity.persistence import User
from iesplan.storage import (
    ObjectHandle,
    add_ref,
    get_object,
    list_refs,
    object_info,
    put_object,
    reconcile,
    remove_ref,
    safe_cleanup,
)
from iesplan.storage.persistence import ObjectRef, StoredObject

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
    """测试客户端: 挂载认证 + 对象管理路由, 替换 get_request_db 依赖。"""
    app = create_app()
    app.include_router(auth_router)
    app.include_router(objects_router)

    def _override_request_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_request_db] = _override_request_db
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


def _put(session: Session, content: bytes, **kw) -> ObjectHandle:
    """便捷写入对象(返回公开句柄, 由调用方决定提交)。"""
    return put_object(
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
# 公开契约(0.4.0 存储公开面收窄)
# ---------------------------------------------------------------------------


def test_object_handle_public_field_set() -> None:
    """0.4.0 契约: ObjectHandle 只暴露公开元数据字段, 无适配器/缓存字段。

    存储路径（敏感）与 ref_count（可重建缓存）已从公开句柄移除（见 manual/developer-guide/zh-CN/ARCHITECTURE_CONSTITUTION.md §10.3 引用； manual/developer-guide/zh-CN/modules/storage.md §必须遵循的规范） 删除 §11/§10.3 旧规格编号
    引用/计数状态经 object_info/list_refs 公开门面查询。
    """
    names = {f.name for f in fields(ObjectHandle)}
    assert names == {
        "id", "oid", "size_bytes", "media_type", "status", "created_at",
    }
    assert "storage_path" not in names
    assert "ref_count" not in names


def test_object_info_has_no_storage_path(session: Session, data_dir) -> None:
    """契约：object_info 元数据视图不输出 storage_path（内部路径敏感，见 manual/developer-guide/zh-CN/modules/storage.md §必须遵循的规范； manual/developer-guide/zh-CN/ARCHITECTURE_CONSTITUTION.md §10.2 路径与适配器）
    不得进入 DTO —— 该 dict 会经 /api/admin/objects/restore 响应序列化)。"""
    obj = _put(session, b"no-path-in-info", content_type="text/plain")
    session.commit()
    info = object_info(session, obj.id)
    assert "storage_path" not in info
    assert info["id"] == obj.id and info["oid"] == obj.oid


# ---------------------------------------------------------------------------
# 写入与落盘
# ---------------------------------------------------------------------------


def test_put_object_writes_file_and_record(session: Session, data_dir) -> None:
    """写入: 文件原子落盘到对象 id 路径, 记录含大小/类型/来源审计。"""
    content = b"hello-object-storage" * 10
    obj = _put(session, content, content_type="text/plain", source_category="user_upload")
    session.commit()

    # 对象行: 字段齐全(oid 为随机 64 位 hex, 文件名即 oid)
    assert obj.oid
    assert len(obj.oid) == 64 and all(c in "0123456789abcdef" for c in obj.oid)
    assert obj.size_bytes == len(content)
    assert obj.media_type == "text/plain"
    assert obj.status == "stored"
    # 0.4.0 收窄: 公开句柄不再暴露适配器/缓存字段 storage_path/ref_count
    # （存储路径敏感；引用计数为可重建缓存，经 object_info 查询，见 manual/developer-guide/zh-CN/ARCHITECTURE_CONSTITUTION.md §10.3）
    assert not hasattr(obj, "storage_path")
    assert not hasattr(obj, "ref_count")
    # 文件在最终位置(文件名即 oid), 临时区无残留(原子 rename)
    path = data_dir / "objects" / obj.oid
    assert path.read_bytes() == content
    assert list((data_dir / "objects" / "tmp").iterdir()) == []
    # 存储层不再记录对象创建审计(反向依赖已移除); 需要审计的公开业务用例
    # 在成功路径自行记录。
    # object_info 与行一致
    info = object_info(session, obj.id)
    assert info["oid"] and info["size_bytes"] == len(content)


def test_put_object_no_dedup_same_content(session: Session, data_dir) -> None:
    """无去重: 同内容两次写入新建两个对象行与两个文件(按对象 id 寻址)。"""
    content = b"same-content-bytes" * 5
    obj1 = _put(session, content)
    obj2 = _put(session, content)
    session.commit()
    assert obj1.id != obj2.id  # 各自新建
    assert obj1.oid != obj2.oid
    rows = session.execute(sa.select(StoredObject)).scalars().all()
    assert len(rows) == 2
    assert len(list((data_dir / "objects").glob("*.tmp"))) == 0
    files = [p for p in (data_dir / "objects").iterdir() if p.is_file()]
    assert len(files) == 2  # 磁盘也是两份
    assert get_object(session, obj1.id) == content
    assert get_object(session, obj2.id) == content


def test_put_object_with_business_ref(session: Session, data_dir) -> None:
    """写入可附带业务引用: 引用按 object_refs 单独建立（见 manual/developer-guide/zh-CN/modules/storage.md §写入与引用流程； manual/developer-guide/zh-CN/ARCHITECTURE_CONSTITUTION.md §10.3 引用）"""
    content = b"with-ref" * 3
    obj1 = _put(session, content, ref_type="dataset_file", ref_id=10)
    obj2 = _put(session, content, ref_type="dataset_file", ref_id=11)
    session.commit()
    assert obj1.id != obj2.id  # 无去重: 各自新建对象
    refs = list_refs(session, obj1.id)
    assert len(refs) == 1  # 各自对象的引用互不干扰
    assert refs[0].ref_entity_id == '10'
    assert object_info(session, obj1.id)["ref_count"] == 1
    assert object_info(session, obj2.id)["ref_count"] == 1


def test_get_object_returns_bytes(session: Session, data_dir) -> None:
    """读取: 返回原始字节(不做内容复核); 缺失文件抛 ObjectCorruptError。"""
    content = b"get-me-back" * 20
    obj = _put(session, content)
    session.commit()
    assert get_object(session, obj.id) == content
    assert get_object(session, obj.oid) == content  # 也支持 oid 查询


# ---------------------------------------------------------------------------
# 引用
# ---------------------------------------------------------------------------


def test_add_remove_list_refs(session: Session, data_dir) -> None:
    """引用: add_ref 递增计数 / remove_ref 递减 / list_refs 枚举; 归零置 orphaned。

    put_object 返回不可变 ObjectHandle（快照）；ref_count 状态变化经 object_info 查询（见 manual/developer-guide/zh-CN/modules/storage.md §输出） 删除 STO-05 旧编号
    object_info 查询新视图(不再依赖 ORM identity map 的活对象)。
    """
    obj = _put(session, b"ref-target" * 4)
    session.commit()
    ref = add_ref(session, obj.id, "version_ref", 7, purpose="版本发布")
    session.commit()
    assert ref.ref_entity_type == "project_versions"  # REF_ENTITY_TYPE_MAP 推断
    assert object_info(session, obj.id)["ref_count"] == 1
    assert len(list_refs(session, obj.id)) == 1

    # 重复引用幂等: 不重复计数
    add_ref(session, obj.id, "version_ref", 7)
    session.commit()
    assert object_info(session, obj.id)["ref_count"] == 1

    remove_ref(session, obj.id, "version_ref", 7)
    session.commit()
    assert object_info(session, obj.id)["ref_count"] == 0
    assert object_info(session, obj.id)["status"] == "orphaned"  # 引用归零 → orphaned
    assert list_refs(session, obj.id) == []

    # orphaned 对象重新建立 owner 引用 → 自动恢复为 stored
    # 替换场景: 同内容对象被旧引用解绑后再次 attach, 必须恢复可用状态)
    add_ref(session, obj.id, "version_ref", 8, purpose="重新引用")
    session.commit()
    assert object_info(session, obj.id)["status"] == "stored"
    assert object_info(session, obj.id)["ref_count"] == 1


def test_remove_ref_unknown_raises(session: Session, data_dir) -> None:
    """解除不存在的引用抛 NotFoundError。"""
    obj = _put(session, b"no-ref" * 3)
    session.commit()
    with pytest.raises(AppError) as exc:
        remove_ref(session, obj.id, "report", 1)
    assert exc.value.http_status == 404


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------


def test_safe_cleanup_dry_run_plan(session: Session, data_dir) -> None:
    """清理计划: dry_run 只列出无引用对象, 不删任何数据。"""
    orphan = _put(session, b"orphan-1" * 5)
    kept = _put(session, b"kept-1" * 5)
    add_ref(session, kept.id, "snapshot_ref", 3)
    session.commit()

    plan = safe_cleanup(session, dry_run=True)
    assert plan["dry_run"] is True
    assert plan["count"] == 1
    assert plan["candidates"][0]["id"] == orphan.id
    assert plan["total_bytes"] == orphan.size_bytes
    # 数据未动
    assert (data_dir / "objects" / orphan.oid).exists()
    assert session.get(StoredObject, orphan.id) is not None


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
        add_ref(session, obj.id, ref_type, ref_id)
    session.commit()

    plan = safe_cleanup(session, dry_run=True)
    assert plan["count"] == 0
    assert len(plan["retained"]) == 0  # 被引用对象不是"保留", 而是天然不可清理


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

    plan = safe_cleanup(session, dry_run=True)
    assert plan["count"] == 0
    assert len(plan["retained"]) == 1
    assert plan["retained"][0]["id"] == orphan.id


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
    add_ref(session, kept.id, "report", 9)
    session.commit()

    resp = client.get("/api/admin/storage", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["objects"]["count"] == 2
    assert body["objects"]["total_bytes"] == 2 * 13 * 3
    assert body["objects"]["by_status"].get("stored") == 2
    assert body["refs"]["count"] == 1
    assert body["refs"]["referenced_objects"] == 1
    assert body["capacity"]["free_bytes"] >= 0
    assert body["healthy"] is True


def test_api_cleanup_preview_then_delete(client: TestClient, session: Session, data_dir) -> None:
    seed_admin(session)
    headers = login(client, "admin", ADMIN_PASSWORD)
    orphan = _put(session, b"api-orphan" * 5)
    kept = _put(session, b"api-kept" * 5, ref_type="evidence_package", ref_id=2)
    session.commit()

    url = "/api/admin/objects/cleanup"
    preview = client.post(url, json={"dry_run": True}, headers=headers)
    assert preview.status_code == 200, preview.text
    assert [item["id"] for item in preview.json()["candidates"]] == [orphan.id]
    assert (data_dir / "objects" / orphan.oid).exists()

    done = client.post(url, json={"dry_run": False}, headers=headers)
    assert done.status_code == 200, done.text
    assert [item["id"] for item in done.json()["cleaned"]] == [orphan.id]
    assert done.json()["errors"] == []
    assert not (data_dir / "objects" / orphan.oid).exists()
    assert session.get(StoredObject, orphan.id) is None
    assert get_object(session, kept.id) == b"api-kept" * 5
    assert _count_audit(session, "object_deleted") == 1


class TestTransactionIsolation:
    """唯一键竞争不得回滚调用方事务（存储只 flush，竞争在 savepoint 内处理，见 manual/developer-guide/zh-CN/ARCHITECTURE_CONSTITUTION.md §5.4 事务）"""

    def test_duplicate_ref_conflict_keeps_business_changes(self, session: Session, data_dir) -> None:
        """先修改业务行, 再触发重复引用竞争, 断言业务修改仍在事务中。"""
        obj = _put(session, b"txn-isolation" * 3)
        session.commit()
        # 先建立引用
        add_ref(session, obj.id, "version_ref", 7)
        session.commit()

        # 业务修改(同事务未提交): 设置对象的媒体类型
        row = session.get(StoredObject, obj.id)
        row.media_type = "application/json"

        # 重复引用(唯一键冲突路径) → 幂等返回, 不应回滚外层事务
        ref = add_ref(session, obj.id, "version_ref", 7)
        session.commit()
        assert ref is not None
        # 业务修改仍在: 媒体类型已保存
        row2 = session.get(StoredObject, obj.id)
        assert row2.media_type == "application/json"

    def test_put_object_no_conflict_with_business_changes(self, session: Session, data_dir) -> None:
        """同内容重复写入新建对象, 不干扰调用方同事务内的业务修改。"""
        content = b"repeat-content" * 2
        _put(session, content)
        session.commit()

        # 业务修改(同事务未提交)
        row = session.get(StoredObject, 1)
        row.media_type = "text/plain"

        # 再次 put 同内容 → 新建对象行(无去重/无冲突路径)
        handle = _put(session, content)
        session.commit()
        assert handle.id == 2
        row2 = session.get(StoredObject, 1)
        assert row2.media_type == "text/plain"


class TestReconcile:
    """reconciliation 幂等恢复（临时文件清理/孤儿登记/损坏报告/计数修正，见 manual/developer-guide/zh-CN/modules/storage.md §失败语义）"""

    def test_reconcile_reports_orphan_and_corrupt(self, session: Session, data_dir) -> None:
        """dry_run: 报告磁盘孤儿(有文件无记录)与损坏(有记录无文件), 不修改。"""
        # 孤儿: 直接落一个最终文件(绕过存储服务)
        (data_dir / "objects").mkdir(parents=True, exist_ok=True)
        orphan_path = data_dir / "objects" / ("ab" + "0" * 62)
        orphan_path.write_bytes(b"orphan-bytes")

        # 损坏: 记录存在但文件缺失
        obj = _put(session, b"will-be-missing" * 2)
        session.commit()
        (data_dir / "objects" / obj.oid).unlink()

        report = reconcile(session, dry_run=True)
        assert report["dry_run"] is True
        # 报告只含文件名(即认领后的对象 id), 不含内部路径(§11)
        assert any("ab" in o["oid"] for o in report["orphan_reported"])
        assert all("storage_path" not in o for o in report["orphan_reported"])
        assert any(c["reason"] == "missing_file" for c in report["corrupt_reported"])
        # dry_run 不登记孤儿、不删记录
        assert report["orphan_registered"] == []

    def test_reconcile_execute_registers_orphan(self, session: Session, data_dir) -> None:
        """执行: 为孤儿文件补建元数据行(文件名即对象 id, 不计算内容摘要, 无引用)。"""
        file_oid = "cd" + "1" * 62  # 文件名(手工构造, 即认领后的对象 id)
        (data_dir / "objects").mkdir(parents=True, exist_ok=True)
        (data_dir / "objects" / file_oid).write_bytes(b"orphan-content")
        report = reconcile(session, dry_run=False)
        assert report["orphan_registered"] == [f"objects/{file_oid}"]
        # 登记行的 oid 即文件名(不做内容摘要); 存储层不再记录登记审计
        # (反向依赖已移除, reconcile 执行无生产调用方, 仅巡检 dry-run 对外)。
        row = session.execute(
            sa.select(StoredObject).where(StoredObject.oid == file_oid)
        ).scalar_one()
        assert row.status == "stored" and row.ref_count == 0
        # 幂等: 再次执行不再登记
        report2 = reconcile(session, dry_run=False)
        assert report2["orphan_registered"] == []

    def test_reconcile_fixes_ref_count_drift(self, session: Session, data_dir) -> None:
        """计数漂移修正: 引用清单为权威, ref_count 缓存不一致时修正并置孤儿状态。"""
        obj = _put(session, b"drift-target" * 2)
        add_ref(session, obj.id, "report", 5)
        session.commit()
        # 人为制造漂移: 引用清单为空但 ref_count 残留
        session.execute(
            sa.delete(ObjectRef).where(ObjectRef.object_id == obj.id)
        )
        session.flush()
        report = reconcile(session, dry_run=False)
        assert report["ref_count_fixed"] >= 1
        info = object_info(session, obj.id)
        assert info["ref_count"] == 0
        assert info["status"] == "orphaned"
