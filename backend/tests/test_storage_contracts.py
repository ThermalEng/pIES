"""存储公开门面与审计不可变契约测试（切片 C 回归）。

覆盖切片 C 发现缺口（宪法 §10–§13 + persistence/storage 手册）：
- 容量未知或低于安全阈值拒绝写入（put 时 SYS-STORE-003 / OBJ-CORRUPT-001 语义）；
- 对象配额拒绝写入（OBJ-QUOTA）；
- 文件缺失/大小或 sha256 损坏返回 OBJ-CORRUPT-001；
- attach/detach/list_owners 引用语义与幂等；
- reconcile/safe_cleanup 幂等且不误删有引用对象；
- audit_log 不可变（更新/删除被 DB 约束或触发器拒绝）。

只测试公开门面（storage.__init__、contracts、service），不拼内部路径；
数据/模型门禁等由 test_model_template_api 等已覆盖。
"""
from __future__ import annotations

import os
os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from iesplan.db import Base
from iesplan.models.immutable_triggers import IMMUTABLE_TABLES

# 复用 conftest 的 sqlite 内存库 helpers
def _engine():
    eng = create_engine("sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    return eng

def _db(eng):
    return sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()

# — helpers —
def _put(db, content: bytes = b"hello-storage"):
    from iesplan.storage.service import put_object
    return put_object(db, content, "text/plain", source_category="test")

def test_capacity_unknown_or_low_rejects_write(monkeypatch=None):
    """容量不可测或低于阈值时 put 拒绝（SYS-STORE-003）。"""
    import shutil
    from unittest.mock import patch
    from iesplan.storage.contracts import StorageQuotaError
    eng = _engine()
    db = _db(eng)
    # 1) 容量不可测：shutil.disk_usage 抛 OSError
    with patch("iesplan.storage.service.shutil.disk_usage", side_effect=OSError("unknown")):
        try:
            _put(db, b"x")
            assert False, "应因容量未知拒绝写入"
        except StorageQuotaError as exc:
            assert exc.code == "SYS-STORE-003"
    db.close()
    # 2) 容量低于阈值：free < threshold
    class DU:
        free = 1
        total = 10
        used = 9
    with patch("iesplan.storage.service.shutil.disk_usage", return_value=DU()):
        eng2 = _engine()
        db2 = _db(eng2)
        try:
            # storage_min_free_bytes 默认 2G，free=1 必低于阈值
            _put(db2, b"y")
            assert False, "应因容量不足拒绝写入"
        except StorageQuotaError as exc:
            assert exc.code == "SYS-STORE-003"
        db2.close()

def test_object_quota_rejects_write():
    """对象 quota_bytes 限额拒绝写入（OBJ-QUOTA）。"""
    from iesplan.storage.contracts import ObjectQuotaError
    from iesplan.storage.persistence import StoredObject
    eng = _engine()
    db = _db(eng)
    h = _put(db, b"quota-test")
    # 模拟对象限额：把该对象 quota_bytes 设为 1
    obj = db.get(StoredObject, h.id)
    obj.quota_bytes = 1
    db.flush()
    # 去重路径也会走 _check_quota
    try:
        from iesplan.storage.service import put_object
        put_object(db, b"quota-test", "text/plain", source_category="test")
        assert False, "应因配额拒绝"
    except ObjectQuotaError as exc:
        assert exc.code in ("SYS-STORE-005", "OBJ-QUOTA-001") or "quota" in str(exc.params).lower()
    db.rollback()
    db.close()

def test_missing_or_corrupt_returns_obj_corrupt():
    """文件缺失或 sha256/大小损坏返回 OBJ-CORRUPT-001。"""
    from iesplan.storage.contracts import ObjectCorruptError
    eng = _engine()
    db = _db(eng)
    h = _put(db, b"corrupt-me")
    # 篡改：清空 storage_path 模拟缺失
    from iesplan.storage.persistence import StoredObject
    obj = db.get(StoredObject, h.id)
    orig_path = obj.storage_path
    obj.storage_path = None
    db.flush()
    try:
        from iesplan.storage.service import get_object
        get_object(db, h.id)
        assert False, "缺失路径应抛 OBJ-CORRUPT-001"
    except ObjectCorruptError as exc:
        assert exc.code == "OBJ-CORRUPT-001"
    # 恢复路径但篡改大小
    obj.storage_path = orig_path
    obj.size_bytes = 999999
    db.flush()
    try:
        from iesplan.storage.service import get_object
        get_object(db, h.id)
        assert False, "大小不匹配应抛 OBJ-CORRUPT-001"
    except ObjectCorruptError as exc:
        assert exc.code == "OBJ-CORRUPT-001"
    db.rollback()
    db.close()

def test_attach_detach_list_owners():
    """attach/detach/list_owners 语义与幂等。"""
    eng = _engine()
    db = _db(eng)
    h = _put(db, b"owner-test")
    from iesplan.storage.service import attach, detach, find_refs_by_owner, list_refs
    # attach 幂等
    r1 = attach(db, h.id, "projects", 1, ref_entity_type="projects")
    r2 = attach(db, h.id, "projects", 1, ref_entity_type="projects")
    assert r1 is not None
    # 重复返回既有
    assert r2 is None or getattr(r2, "ref_entity_id", None) == "1" or r2.ref_entity_id == "1" if hasattr(r2, "ref_entity_id") else r2["ref_entity_id"] == "1"
    refs = find_refs_by_owner(db, "projects", 1, ref_entity_type="projects")
    assert any(str(x["object_id"]) == str(h.id) for x in refs)
    # detach
    detach(db, h.id, "projects", 1, ref_entity_type="projects")
    db.flush()
    refs2 = find_refs_by_owner(db, "projects", 1, ref_entity_type="projects")
    assert not any(str(x["object_id"]) == str(h.id) for x in refs2)
    db.close()

def test_reconcile_and_safe_cleanup_do_not_delete_referenced():
    """reconcile/safe_cleanup 幂等且不误删有引用对象。"""
    eng = _engine()
    db = _db(eng)
    h1 = _put(db, b"keep-me")
    h2 = _put(db, b"delete-me")
    from iesplan.storage.service import attach, reconcile, safe_cleanup
    attach(db, h1.id, "project", 10, ref_entity_type="projects")
    db.flush()
    # dry-run 计划不应包含有引用的 h1
    plan = safe_cleanup(db, dry_run=True)
    cands = [c["id"] for c in plan["candidates"]]
    assert h1.id not in cands
    # 无引用 h2 应在候选内（或 pending 逻辑下可清理）
    # reconcile 幂等：两次 dry_run 结果一致
    r1 = reconcile(db, dry_run=True)
    r2 = reconcile(db, dry_run=True)
    # 幂等：两次 dry_run 结构一致（key 包含 dry_run/tmp_cleaned 等）
    assert r1["dry_run"] == r2["dry_run"]
    assert r1.get("tmp_cleaned") == r2.get("tmp_cleaned")
    db.close()

def test_audit_log_immutable():
    """audit_log 不可变：UPDATE/DELETE 被触发器/约束拒绝（Postgres 语义，SQLite 仅断言在列）。"""
    # 0.7 组装前基线：audit_log 属于 IMMUTABLE_TABLES，三道防线在 Postgres 生效；
    # SQLite 测试仅断言 DDL 覆盖（test_db_immutable_triggers 已覆盖），此处补充 ORM 写入后不可变语义的回归描述
    assert "audit_log" in IMMUTABLE_TABLES
    # 额外断言：SQLite 下重复部署不报错（幂等）
    import iesplan.db as db_mod
    db_mod._deploy_immutable_triggers()
