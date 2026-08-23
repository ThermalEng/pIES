"""对象存储服务(STO-01~07): 内容寻址写入/读取/引用/清理/门禁/恢复。

对应 RPD 第 23 节与 01-db-schema.md 第 10 节。本模块是对象域唯一写入单元:

- put_object: 经 BlobStore 适配器原子落盘 → upsert 元数据行 → (可选)owner 引用;
- 内容去重(23.1): 相同 sha256 只存一份, 复用既有记录; owner 引用单独建立;
- 读取校验: get_object 读取时校验大小与 sha256, 不一致抛 ObjectCorruptError;
- 引用(STO-02): attach/detach 以 ObjectRef 清单为唯一权威, ref_count 仅作
  可重建缓存(一致性巡检在 reconcile 中执行), 业务引用成对解绑由各业务
  模块在其公开删除/替换流程中显式调用;
- 清理(23.3/23.4): safe_cleanup 按 ObjectRef 清单找无任何引用的对象,
  先计划(dry_run)后执行; 被任意存在的 owner 引用都自然阻止清理
  (删除 REF_ENTITY_TYPE_MAP / PROTECTED_ENTITY_TYPES, STO-02);
- 对象清理恢复路径(0.2.0-B3 软删/保留期): cleanup 执行时不再立即物理删,
  而是把对象标记为 pending_deletion(记 pending_deleted_at / 保留截止
  pending_delete_until), 保留期内可 undelete / 重新 attach 恢复;
  purge_expired 只对已过保留期的对象物理删文件 + 删记录(管理员可显式调用,
  reconcile 巡检时兜底调用), 从而为明显危险误操作保留延迟删除与恢复路径;
- 存储门禁(STO-06): 磁盘容量不可测时拒绝新写入并降级 readiness;
- 恢复(STO-04): reconcile 幂等清理超龄临时文件、登记或删除无元数据最终文件、
  报告有元数据但缺文件的损坏。

事务纪律(STO-03): 本模块只 flush, 提交/回滚由应用用例统一决定; 唯一键
竞争在 savepoint(begin_nested)内处理, 绝不调用调用方 Session 的全局 rollback()。
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from iesplan.config import settings
from iesplan.core.errors import AppError, NotFoundError
from iesplan.core.idgen import sha256_hex
from iesplan.models.audit import AuditLog, RetentionRule
from iesplan.storage.adapters.filesystem import FileSystemBlobStore
from iesplan.storage.contracts import (
    BlobMissingError,
    ObjectCorruptError,
    ObjectHandle,
    ObjectNotPendingDeletionError,
    ObjectOwner,
    ObjectQuotaError,
    RefInfo,
    ReferenceNotFoundError,
    StorageQuotaError,
)
from iesplan.storage.persistence import (
    OBJ_STATUS_DELETED,
    OBJ_STATUS_ORPHANED,
    OBJ_STATUS_PENDING_DELETION,
    OBJ_STATUS_STORED,
    ObjectRef,
    StoredObject,
)

logger = logging.getLogger(__name__)

#: 未命中保留规则时孤儿对象的默认保留天数(0 = 引用归零即可清理)
DEFAULT_RETENTION_DAYS: int = 0
#: 清理后进入软删保留期的默认天数(0.2.0-B3 恢复路径: 保留期内可恢复,
#: 到期才物理回收; 0 = 立即软删并物理回收, 仅显式配置时使用)
DEFAULT_PENDING_DELETE_DAYS: int = 7
#: 单次清理最大对象数(防一次性事务过大)
CLEANUP_BATCH_LIMIT: int = 1000
#: 容量不可测时的降级标志(dict 键, 供 readiness 聚合层读取)
CAPACITY_UNKNOWN_MARKER = "capacity_unknown"

#: 引用类别 → 引用方实体表名(兼容旧契约: 显式 ref_entity_type 优先;
#: 存储只把它当作稳定字符串标识, 不导入业务模型, STO-05)
REF_ENTITY_TYPE_MAP: dict[str, str] = {
    "dataset_file": "dataset_files",  # 数据集版本文件(01 §5.3)
    "version_ref": "project_versions",  # 项目版本(01 §3.3)
    "snapshot_ref": "calc_snapshots",  # 计算快照(01 §7.x)
    "evidence_package": "evidence_packages",  # 证据包(01 §8.1)
    "report": "reports",  # 报告(01 §8.5)
    "result_ref": "eval_results",  # 评估结果(01 §8.x)
}

# 存储估算参数(23.3; 启发式, 只用于门禁判断, 不参与计算)
_FLOAT64_BYTES: int = 8
_HOURLY_FIELDS: int = 20
_SNAPSHOT_BASE_BYTES: int = 1 << 20  # 1 MiB
_SNAPSHOT_PER_HOUR_BYTES: int = 256
_INTERMEDIATE_MULTIPLIER: int = 3
_SAMPLE_POINT_BYTES: int = 32
_ESTIMATE_FLOOR_BYTES: int = 1024


def utcnow() -> datetime:
    """当前 UTC 时间。"""
    return datetime.now(UTC)


def as_utc(dt: datetime | None) -> datetime | None:
    """规范化为带 UTC 时区的时间(无时区按 UTC 处理, SQLite 返回 naive 时间)。"""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# 适配器与内部工具
# ---------------------------------------------------------------------------

_blob_store: FileSystemBlobStore | None = None


def get_blob_store() -> FileSystemBlobStore:
    """进程内 BlobStore 适配器(单例; 测试可用内存适配器替换)。"""
    global _blob_store
    if _blob_store is None:
        _blob_store = FileSystemBlobStore()
    return _blob_store


def _to_handle(obj: StoredObject) -> ObjectHandle:
    """ORM 行 → 公开句柄(业务模块不得接触 ORM)。

    0.4.0: 不再携带 storage_path(§11 内部路径)与 ref_count(§10.3 可重建
    缓存); 引用状态经 object_info/list_refs 公开门面查询。
    """
    return ObjectHandle(
        id=obj.id,
        oid=obj.oid,
        sha256=obj.sha256,
        size_bytes=obj.size_bytes,
        media_type=obj.media_type,
        status=obj.status,
        created_at=as_utc(obj.created_at).isoformat() if obj.created_at else None,
    )


def _to_refinfo(ref: ObjectRef) -> RefInfo:
    """ORM 引用行 → 公开视图。"""
    return RefInfo(
        id=ref.id,
        object_id=ref.object_id,
        ref_type=ref.ref_type,
        ref_entity_type=ref.ref_entity_type,
        ref_entity_id=str(ref.ref_entity_id),
        purpose=ref.purpose,
        created_at=as_utc(ref.created_at).isoformat() if ref.created_at else None,
    )


def _resolve_object(db: Session, object_id: int | str) -> StoredObject:
    """按主键 id 或内容寻址 oid 解析对象, 不存在抛 NotFoundError。"""
    if isinstance(object_id, str):
        obj = db.execute(
            sa.select(StoredObject).where(StoredObject.oid == object_id)
        ).scalar_one_or_none()
    else:
        obj = db.get(StoredObject, int(object_id))
    if obj is None:
        raise NotFoundError(
            "",
            params={"object_type": "objects", "id": str(object_id)},
            location={"object_type": "objects", "object_id": str(object_id), "field": ""},
        )
    return obj


def _audit(
    db: Session,
    entity_type: str,
    entity_id: int,
    action: str,
    *,
    actor_id: int | None = None,
    actor_type: str = "system",
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    """写入不可变审计日志(01 §10.3; 本模块只 INSERT 不修改)。"""
    db.add(
        AuditLog(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            actor_id=actor_id,
            actor_type=actor_type,
            before=before,
            after=after,
        )
    )


def _match_retention_rule(rules: list[RetentionRule], obj: StoredObject) -> RetentionRule | None:
    """匹配对象保留规则(01 §10.5)。

    仅匹配 entity_type='objects' 且 object_kind 为 '*' 或与对象媒体类型一致的
    active 规则; 取最小保留天数(最严格先满足)。
    """
    matched: RetentionRule | None = None
    for rule in rules:
        if rule.status != "active" or rule.entity_type != "objects":
            continue
        if rule.object_kind not in ("*", obj.media_type or ""):
            continue
        if matched is None or rule.retention_days < matched.retention_days:
            matched = rule
    return matched


def _check_quota(obj: StoredObject, size_bytes: int) -> None:
    """对象配额检查: quota_bytes > 0 表示限定额度(0 = 不限)。"""
    if obj.quota_bytes and size_bytes > obj.quota_bytes:
        raise ObjectQuotaError(
            "",
            params={"object_id": obj.id, "size": size_bytes, "quota": obj.quota_bytes},
            location={"object_type": "objects", "object_id": str(obj.id), "field": "quota_bytes"},
        )


def _check_disk_capacity() -> None:
    """磁盘余量门禁(STO-06): 低于安全阈值**或容量不可测**拒绝写入。

    容量状态未知不再静默放行: 拒绝新写入并使 readiness 降级, 保留只读能力。
    """
    try:
        free = shutil.disk_usage(settings.data_dir).free
    except OSError as exc:
        raise StorageQuotaError(
            "",
            params={"free": -1, "safe_threshold": settings.storage_min_free_bytes,
                    "reason": "capacity_unknown"},
        ) from exc
    if free < settings.storage_min_free_bytes:
        raise StorageQuotaError(
            "",
            params={"free": free, "safe_threshold": settings.storage_min_free_bytes},
        )


# ---------------------------------------------------------------------------
# 写入(STO-01/03: 公开门面, 原子落盘 + savepoint 竞争处理)
# ---------------------------------------------------------------------------


def put_object(
    db: Session,
    content: bytes,
    content_type: str,
    source_category: str,
    ref_type: str | None = None,
    ref_id: int | None = None,
    ref_entity_type: str | None = None,
    purpose: str | None = None,
    *,
    actor_id: int | None = None,
    actor_type: str = "system",
) -> ObjectHandle:
    """写入内容寻址对象(01 §10.1 / RPD 23.1), 返回公开句柄。

    流程: 门禁 → BlobStore 原子落盘 → upsert 元数据行(唯一键竞争走
    savepoint 内回退重查, 不触调用方事务) → (可选)建立 owner 引用。
    文件完整落盘后才建对象行, 对象行 flush 后才建引用。

    参数:
        db: 数据库会话(本函数只 flush, 提交由调用方负责)。
        content: 对象字节内容。
        content_type: 媒体类型(MIME, 记入 objects.media_type)。
        source_category: 来源类别(如 user_upload/evidence/report/export;
            记入创建审计事件的 after.source_category)。
        ref_type/ref_id/ref_entity_type/purpose: 可选初始业务引用(见 attach)。
    返回:
        ObjectHandle(新建或按 sha256 复用)。
    """
    digest = sha256_hex(content)
    # 1. 内容去重: 相同 sha256 已存在 → 复用记录(owner 引用仍单独建立)
    existing = db.execute(
        sa.select(StoredObject)
        .where(StoredObject.sha256 == digest, StoredObject.status != OBJ_STATUS_DELETED)
    ).scalar_one_or_none()
    if existing is not None:
        _check_quota(existing, len(content))
        if ref_type is not None and ref_id is not None:
            attach(
                db, existing.id, ref_type, ref_id,
                ref_entity_type=ref_entity_type, purpose=purpose,
                actor_id=actor_id, actor_type=actor_type,
            )
        return _to_handle(existing)

    # 2. 门禁: 磁盘余量(23.3, 含容量不可测拒绝) + 配额
    _check_disk_capacity()

    # 3. BlobStore 原子落盘(临时区 → fsync → rename)
    store = get_blob_store()
    storage_path, _ = store.put_blob(content)

    # 4. 元数据行: 唯一键(oid/sha256)竞争在 savepoint 内回退重查
    obj = StoredObject(
        oid=digest,
        sha256=digest,
        size_bytes=len(content),
        storage_path=storage_path,
        media_type=content_type,
        status="stored",
        ref_count=0,
        quota_bytes=0,
    )
    try:
        with db.begin_nested():  # RR-P1-03: 只回滚嵌套 savepoint, 不触调用方外层事务
            db.add(obj)
            db.flush()
    except IntegrityError:
        # 并发写入去重: 同内容对象已被其他事务建立, 复用即可(文件内容一致)
        # savepoint 已随 begin_nested() 退出自动回滚, 外层事务保持完整
        existing = db.execute(
            sa.select(StoredObject).where(StoredObject.sha256 == digest)
        ).scalar_one_or_none()
        if existing is None:
            raise
        if ref_type is not None and ref_id is not None:
            attach(
                db, existing.id, ref_type, ref_id,
                ref_entity_type=ref_entity_type, purpose=purpose,
                actor_id=actor_id, actor_type=actor_type,
            )
        return _to_handle(existing)

    # 5. 审计: 对象创建(承载来源类别等元信息, 01 §10.3)
    _audit(
        db,
        "objects",
        obj.id,
        "object_created",
        actor_id=actor_id,
        actor_type=actor_type,
        after={
            "oid": obj.oid,
            "sha256": obj.sha256,
            "size_bytes": obj.size_bytes,
            "media_type": obj.media_type,
            "source_category": source_category,
            # 0.4.0: 不再记录 storage_path(§11 内部路径不得进入日志/审计);
            # 内容寻址 oid 即为可追溯标识
        },
    )

    # 6. 初始业务引用(对象已完整, 此时才允许建立引用)
    if ref_type is not None and ref_id is not None:
        attach(
            db, obj.id, ref_type, ref_id,
            ref_entity_type=ref_entity_type, purpose=purpose,
            actor_id=actor_id, actor_type=actor_type,
        )
    return _to_handle(obj)


# ---------------------------------------------------------------------------
# 读取与校验(23.1)
# ---------------------------------------------------------------------------


def get_object(db: Session, object_id: int | str) -> bytes:
    """读取对象字节并校验完整性(大小 + sha256), 不一致抛 ObjectCorruptError。"""
    obj = _resolve_object(db, object_id)
    if not obj.storage_path:
        raise ObjectCorruptError(
            "",
            params={"object_id": obj.id, "oid": obj.oid, "reason": "missing_path"},
        )
    try:
        raw = get_blob_store().get_blob(obj.storage_path)
    except BlobMissingError as exc:
        # §16: 错误响应不得含主机绝对路径; 适配器异常 params 携带的路径只进日志
        logger.warning("对象文件缺失(仅日志): object_id=%s path=%s", obj.id, exc.params)
        raise ObjectCorruptError(
            "",
            params={"object_id": obj.id, "oid": obj.oid, "reason": "missing"},
        ) from exc
    if len(raw) != obj.size_bytes or sha256_hex(raw) != obj.sha256:
        raise ObjectCorruptError(
            "",
            params={
                "object_id": obj.id,
                "oid": obj.oid,
                "expected_size": obj.size_bytes,
                "actual_size": len(raw),
                "expected_sha256": obj.sha256,
            },
        )
    return raw


def object_info(db: Session, object_id: int | str) -> dict:
    """对象元数据视图(不含内容)。

    0.4.0: 不再输出 storage_path(§11 内部路径不得进入 DTO/日志/证据包,
    该 dict 会经 api/objects.py 恢复端点直接序列化为响应); ref_count 为
    §10.3 可重建缓存, 仅供管理视图参考, 权威引用清单经 list_refs 查询。
    """
    obj = _resolve_object(db, object_id)
    return {
        "id": obj.id,
        "oid": obj.oid,
        "sha256": obj.sha256,
        "size_bytes": obj.size_bytes,
        "media_type": obj.media_type,
        "status": obj.status,
        "ref_count": obj.ref_count,
        "quota_bytes": obj.quota_bytes,
        "created_at": as_utc(obj.created_at).isoformat() if obj.created_at else None,
        "last_referenced_at": (
            as_utc(obj.last_referenced_at).isoformat() if obj.last_referenced_at else None
        ),
        "pending_deleted_at": (
            as_utc(obj.pending_deleted_at).isoformat() if obj.pending_deleted_at else None
        ),
        "pending_delete_until": (
            as_utc(obj.pending_delete_until).isoformat() if obj.pending_delete_until else None
        ),
    }


def object_by_sha256(db: Session, digest: str) -> dict:
    """按内容校验值(sha256)查对象元数据视图; 缺失抛 NotFoundError(STO-05)。"""
    obj = db.execute(
        sa.select(StoredObject).where(StoredObject.sha256 == digest)
    ).scalar_one_or_none()
    if obj is None:
        raise NotFoundError(
            "",
            params={"object_type": "objects", "sha256": digest},
            location={"object_type": "objects", "field": "sha256", "value": digest},
        )
    return object_info(db, obj.id)


def verify_object(db: Session, object_id: int | str) -> dict:
    """完整性校验(周期性巡检用): 返回报告不抛错。

    报告字段: ok / size_ok / hash_ok / expected_* / actual_* / error。
    """
    try:
        obj = _resolve_object(db, object_id)
        raw = get_object(db, obj.id)
    except (NotFoundError, ObjectCorruptError) as exc:
        return {
            "object_id": str(object_id),
            "ok": False,
            "size_ok": False,
            "hash_ok": False,
            "error": exc.message_key,
            "params": exc.params,
        }
    size_ok = len(raw) == obj.size_bytes
    hash_ok = sha256_hex(raw) == obj.sha256
    return {
        "object_id": obj.id,
        "oid": obj.oid,
        "ok": size_ok and hash_ok,
        "size_ok": size_ok,
        "hash_ok": hash_ok,
        "expected_size": obj.size_bytes,
        "actual_size": len(raw),
        "expected_sha256": obj.sha256,
        "error": None if (size_ok and hash_ok) else "ies.diag.obj.corrupt",
    }


# ---------------------------------------------------------------------------
# 引用(STO-02/03: 引用清单为唯一权威, attach/detach 成对)
# ---------------------------------------------------------------------------


def attach(
    db: Session,
    object_id: int | str,
    ref_type: str,
    ref_id: int | str,
    ref_entity_type: str | None = None,
    purpose: str | None = None,
    *,
    actor_id: int | None = None,
    actor_type: str = "system",
) -> RefInfo | None:
    """建立 owner 引用(重复引用幂等返回既有行)。

    STO-02: ObjectRef 行是生命周期唯一权威; ref_count 仅作可重建缓存。
    唯一键竞争在 savepoint 内回退, 不触调用方外层事务(STO-03)。
    返回新建(或既有)引用的公开 dict; 重复引用返回 None。
    """
    obj = _resolve_object(db, object_id)
    entity_type = ref_entity_type or REF_ENTITY_TYPE_MAP.get(ref_type, ref_type)
    # 0.2.0-B3 恢复路径: 待物理回收对象重新获得 owner 引用时自动恢复为
    # 可用状态(清除软删标记), 文件仍在磁盘上, 内容可继续访问。
    # attach 必然建立引用, 恢复后 ref_count >= 1, 故置 stored。
    if obj.status == OBJ_STATUS_PENDING_DELETION:
        obj.status = OBJ_STATUS_STORED
        obj.pending_deleted_at = None
        obj.pending_delete_until = None
        _audit(
            db,
            "objects",
            obj.id,
            "object_restored",
            actor_id=actor_id,
            actor_type=actor_type,
            after={"reason": "re-attached", "ref_type": ref_type, "ref_entity_id": str(ref_id)},
        )
    # 幂等: 既有引用直接返回(不重复计数)
    existing = db.execute(
        sa.select(ObjectRef).where(
            ObjectRef.object_id == obj.id,
            ObjectRef.ref_type == ref_type,
            ObjectRef.ref_entity_type == entity_type,
            ObjectRef.ref_entity_id == str(ref_id),
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _to_refinfo(existing)
    ref = ObjectRef(
        object_id=obj.id,
        ref_type=ref_type,
        ref_entity_type=entity_type,
        ref_entity_id=str(ref_id),
        purpose=purpose,
    )
    try:
        with db.begin_nested():  # RR-P1-03: 只回滚嵌套 savepoint, 不触调用方外层事务
            db.add(ref)
            db.flush()
            obj.ref_count = (obj.ref_count or 0) + 1
            obj.last_referenced_at = utcnow()
            db.flush()
    except IntegrityError:
        # 并发去重: savepoint 自动回滚, 返回既有引用(计数不重复累加)
        existing = db.execute(
            sa.select(ObjectRef).where(
                ObjectRef.object_id == obj.id,
                ObjectRef.ref_type == ref_type,
                ObjectRef.ref_entity_type == entity_type,
                ObjectRef.ref_entity_id == str(ref_id),
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        return _to_refinfo(existing)
    _audit(
        db,
        "objects",
        obj.id,
        "object_ref_add",
        actor_id=actor_id,
        actor_type=actor_type,
        after={"ref_type": ref_type, "ref_entity_type": entity_type, "ref_entity_id": str(ref_id)},
    )
    return _to_refinfo(ref)


def detach(
    db: Session,
    object_id: int | str,
    ref_type: str,
    ref_id: int | str,
    ref_entity_type: str | None = None,
    *,
    actor_id: int | None = None,
    actor_type: str = "system",
) -> None:
    """解除 owner 引用并递减 ref_count; 引用不存在抛 ReferenceNotFoundError。

    引用计数归零时对象状态置 orphaned(01 §10.1 删除协议第一跳)。
    业务删除/替换流程必须成对调用 attach/detach(STO-02)。
    """
    obj = _resolve_object(db, object_id)
    entity_type = ref_entity_type or REF_ENTITY_TYPE_MAP.get(ref_type, ref_type)
    ref = db.execute(
        sa.select(ObjectRef).where(
            ObjectRef.object_id == obj.id,
            ObjectRef.ref_type == ref_type,
            ObjectRef.ref_entity_type == entity_type,
            ObjectRef.ref_entity_id == str(ref_id),
        )
    ).scalar_one_or_none()
    if ref is None:
        raise ReferenceNotFoundError(
            "",
            params={"object_type": "object_refs", "object_id": obj.id, "ref_type": ref_type},
        )
    db.delete(ref)
    obj.ref_count = max(0, (obj.ref_count or 0) - 1)
    if obj.ref_count == 0:
        obj.status = OBJ_STATUS_ORPHANED
        obj.last_referenced_at = None
    _audit(
        db,
        "objects",
        obj.id,
        "object_ref_remove",
        actor_id=actor_id,
        actor_type=actor_type,
        before={"ref_type": ref_type, "ref_entity_type": entity_type, "ref_entity_id": str(ref_id)},
    )


def add_ref(
    db: Session,
    object_id: int | str,
    ref_type: str,
    ref_id: int | str,
    ref_entity_type: str | None = None,
    purpose: str | None = None,
    *,
    actor_id: int | None = None,
    actor_type: str = "system",
) -> RefInfo | None:
    """公开门面: 建立 owner 引用(委托 attach, 返回引用公开视图)。"""
    return attach(
        db, object_id, ref_type, ref_id,
        ref_entity_type=ref_entity_type, purpose=purpose,
        actor_id=actor_id, actor_type=actor_type,
    )


def remove_ref(
    db: Session,
    object_id: int | str,
    ref_type: str,
    ref_id: int | str,
    ref_entity_type: str | None = None,
    *,
    actor_id: int | None = None,
    actor_type: str = "system",
) -> None:
    """公开门面: 解除 owner 引用(委托 detach)。"""
    detach(
        db, object_id, ref_type, ref_id,
        ref_entity_type=ref_entity_type, actor_id=actor_id, actor_type=actor_type,
    )


def list_refs(db: Session, object_id: int | str) -> list[RefInfo]:
    """列出对象全部 owner 引用(按建立时间升序), 返回公开视图。"""
    obj = _resolve_object(db, object_id)
    rows = db.execute(
        sa.select(ObjectRef)
        .where(ObjectRef.object_id == obj.id)
        .order_by(ObjectRef.created_at)
    ).scalars()
    return [_to_refinfo(r) for r in rows]


def find_refs_by_owner(
    db: Session, ref_type: str, owner_id: int, ref_entity_type: str | None = None
) -> list[dict]:
    """按 owner 查全部引用(STO-05: 业务模块按公开 owner 标识查询, 不碰 ORM)。

    ref_entity_type 缺省等于 ref_type(公开命名空间即实体类型)。
    返回 [{object_id, ref_type, ref_entity_type, ref_entity_id, purpose, created_at}]。
    """
    entity_type = ref_entity_type or ref_type
    rows = db.execute(
        sa.select(ObjectRef)
        .where(
            ObjectRef.ref_type == ref_type,
            ObjectRef.ref_entity_type == entity_type,
            ObjectRef.ref_entity_id == str(owner_id),
        )
        .order_by(ObjectRef.id.desc())
    ).scalars()
    return [
        {
            "object_id": r.object_id,
            "ref_type": r.ref_type,
            "ref_entity_type": r.ref_entity_type,
            "ref_entity_id": r.ref_entity_id,
            "purpose": r.purpose,
            "created_at": as_utc(r.created_at).isoformat() if r.created_at else None,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 清理(STO-02: 引用清单为权威, 任意存在的 owner 引用阻止清理)
# ---------------------------------------------------------------------------


def _candidate_objects(db: Session, limit: int = CLEANUP_BATCH_LIMIT) -> list[StoredObject]:
    """无任何 owner 引用的对象候选集。

    STO-02: 只以 ObjectRef 清单判断(存在任何引用行即非候选);
    ref_count 缓存不一致时由 reconcile 巡检报告并修正。
    已处于 pending_deletion(待物理回收)的对象不在候选集内(0.2.0-B3):
    它们走 purge_expired 生命周期, 不会被重复软删。
    """
    has_refs = sa.select(ObjectRef.id).where(ObjectRef.object_id == StoredObject.id).exists()
    stmt = (
        sa.select(StoredObject)
        .where(
            ~has_refs,
            StoredObject.status != OBJ_STATUS_DELETED,
            StoredObject.status != OBJ_STATUS_PENDING_DELETION,
        )
        .order_by(StoredObject.created_at)
        .limit(limit)
    )
    return list(db.execute(stmt).scalars())


def _object_summary(obj: StoredObject) -> dict:
    """对象行摘要(清理计划/结果清单条目)。"""
    return {
        "id": obj.id,
        "oid": obj.oid,
        "sha256": obj.sha256,
        "size_bytes": obj.size_bytes,
        "media_type": obj.media_type,
        "status": obj.status,
        "created_at": as_utc(obj.created_at).isoformat() if obj.created_at else None,
        "pending_deleted_at": as_utc(obj.pending_deleted_at).isoformat() if obj.pending_deleted_at else None,
        "pending_delete_until": (
            as_utc(obj.pending_delete_until).isoformat() if obj.pending_delete_until else None
        ),
    }


def safe_cleanup(
    db: Session,
    dry_run: bool = True,
    *,
    actor_id: int | None = None,
    actor_type: str = "admin",
    limit: int = CLEANUP_BATCH_LIMIT,
    expected_plan_id: str | None = None,
    pending_delete_days: int = DEFAULT_PENDING_DELETE_DAYS,
) -> dict:
    """对象清理(23.3/23.4): 先计划后执行, 全程可审计(RR-P2-07 稳定计划标识)。

    - 候选: 无任何 owner 引用的对象(引用清单为唯一权威, STO-02);
    - 保留规则(retention_rules, 01 §10.5): 命中 active 规则时要求对象年龄
      达到 retention_days, 未命中规则默认 DEFAULT_RETENTION_DAYS 天;
    - 被任意 owner 引用的对象天然不可清理(不再需要实体类型白名单);
    - dry_run=True: 只返回清理计划(含 plan_id 与候选摘要), 不删任何数据;
    - dry_run=False: 必须携带 dry-run 返回的 plan_id; 执行前在事务内重新
      计算候选集合并与计划摘要比对, 候选变化(引用/保留/版本变更)则拒绝
      执行并要求重新预览;
    - 0.2.0-B3 软删/保留期: 执行不再立即物理删, 而是把候选对象标记为
      pending_deletion(记 pending_deleted_at 与保留截止 pending_delete_until),
      保留期默认 pending_delete_days 天(默认 7); 保留期内对象可经
      undelete_object / 重新 attach 恢复, 物理回收由 purge_expired 负责
      (到期后删除文件 + 删除记录 + 审计)。文件已缺失的待回收对象同样
      标记软删(保留期内仍可恢复), 由 purge 统一收尾。
      只 flush(提交/回滚由应用用例统一决定, RR-P1-03)。

    返回:
        计划/执行结果字典: plan_id / dry_run / count / total_bytes / candidates /
        retained_count / retained / pending_delete_days /
        (dry_run=False 时) marked_count / marked / errors。
    """
    candidates = _candidate_objects(db, limit=limit)
    rules = list(db.execute(sa.select(RetentionRule).where(RetentionRule.status == "active")).scalars())
    now = utcnow()

    deletable: list[StoredObject] = []
    retained: list[dict] = []
    for obj in candidates:
        created = as_utc(obj.created_at) or now
        age_days = (now - created).total_seconds() / 86400.0
        rule = _match_retention_rule(rules, obj)
        days = rule.retention_days if rule is not None else DEFAULT_RETENTION_DAYS
        if age_days < days:
            retained.append({**_object_summary(obj), "retention_days": days, "age_days": round(age_days, 3)})
        else:
            deletable.append(obj)

    total_bytes = sum(obj.size_bytes or 0 for obj in deletable)
    # RR-P2-07: 稳定计划标识 = 候选 oid 有序串联 + 总字节的 sha256(候选变化即失效)
    # 0.2.0-B3: 保留期天数纳入计划标识(改动保留期即视为新计划, 需重新预览)。
    plan_payload = "|".join(sorted(obj.oid for obj in deletable)) + f"|{total_bytes}|{pending_delete_days}"
    plan_id = sha256_hex(plan_payload.encode("utf-8")) if deletable else "plan-empty"
    if not dry_run:
        if expected_plan_id is None:
            raise AppError(
                "执行清理必须携带 dry-run 返回的 plan_id(先预览后执行)",
                code="OBJ-CLEAN-001",
                message_key="ies.diag.obj.cleanup_plan_required",
            )
        if expected_plan_id != plan_id:
            raise AppError(
                "清理计划已过期: 候选集合或引用状态发生变化, 请重新预览后执行",
                code="OBJ-CLEAN-002",
                message_key="ies.diag.obj.cleanup_plan_stale",
            )
    result: dict = {
        "plan_id": plan_id,
        "dry_run": dry_run,
        "count": len(deletable),
        "total_bytes": total_bytes,
        "candidates": [_object_summary(obj) for obj in deletable],
        "retained_count": len(retained),
        "retained": retained,
        "pending_delete_days": pending_delete_days,
    }
    if dry_run:
        result["message"] = (
            f"清理计划: {len(deletable)} 个无引用对象共 {total_bytes} 字节将标记为待回收, "
            f"保留 {pending_delete_days} 天后物理删除"
        )
        return result

    # 执行(0.2.0-B3 软删/保留期): 只标记待物理回收, 不立即删文件。
    # 到期物理回收由 purge_expired 负责; 保留期内可 undelete / 重新 attach 恢复。
    marked: list[dict] = []
    errors: list[dict] = []
    now = utcnow()
    until = now + timedelta(days=pending_delete_days)
    for obj in deletable:
        obj.status = OBJ_STATUS_PENDING_DELETION
        obj.pending_deleted_at = now
        obj.pending_delete_until = until
        _audit(
            db,
            "objects",
            obj.id,
            "object_marked_pending_deletion",
            actor_id=actor_id,
            actor_type=actor_type,
            before={"oid": obj.oid, "sha256": obj.sha256, "size_bytes": obj.size_bytes},
            after={
                "status": OBJ_STATUS_PENDING_DELETION,
                "pending_delete_until": until.isoformat(),
                "pending_delete_days": pending_delete_days,
            },
        )
        marked.append(_object_summary(obj))
    db.flush()  # RR-P1-03: 存储只 flush, 提交/回滚由应用用例(API 层)统一决定
    result["dry_run"] = False
    result["marked_count"] = len(marked)
    result["marked"] = marked
    result["errors"] = errors
    result["message"] = (
        f"已将 {len(marked)} 个无引用对象标记为待回收, "
        f"保留 {pending_delete_days} 天后物理删除"
    )
    return result


# ---------------------------------------------------------------------------
# 软删/保留期(0.2.0-B3 对象清理恢复路径): 待回收清单 / 物理回收 / 恢复
# ---------------------------------------------------------------------------


def list_pending_deleted(
    db: Session,
    *,
    expired_only: bool = False,
    limit: int = CLEANUP_BATCH_LIMIT,
) -> list[dict]:
    """列出"已删除待回收"对象(管理员查看将被物理回收的对象)。

    返回每个待回收对象的公开摘要(含保留截止 pending_delete_until)。
    expired_only=True 时只列出已过保留期、可以被 purge_expired 物理回收的对象。
    按保留截止时间升序(到期优先)。
    """
    stmt = (
        sa.select(StoredObject)
        .where(StoredObject.status == OBJ_STATUS_PENDING_DELETION)
        .order_by(StoredObject.pending_delete_until, StoredObject.id)
        .limit(limit)
    )
    if expired_only:
        stmt = stmt.where(StoredObject.pending_delete_until < utcnow())
    rows = db.execute(stmt).scalars()
    return [_object_summary(obj) for obj in rows]


def purge_expired(
    db: Session,
    *,
    dry_run: bool = True,
    actor_id: int | None = None,
    actor_type: str = "admin",
    limit: int = CLEANUP_BATCH_LIMIT,
) -> dict:
    """物理回收已过保留期的待回收对象(0.2.0-B3: 延迟物理删除)。

    - 只处理 status = pending_deletion 且 pending_delete_until 已过期的对象;
    - 保留期内对象绝不物理删除(为误操作保留恢复路径);
    - dry_run=True: 只列出可回收对象与预计释放字节, 不删任何数据;
    - dry_run=False: 删除文件(缺失则仅删记录并审计损坏) + 删记录 + 审计,
      只 flush(提交/回滚由应用用例统一决定, RR-P1-03);
    - 文件删除失败的对象跳过并记录 errors(保留记录, 下次重试)。

    返回: {dry_run, count, total_bytes, candidates/purged, errors}。
    """
    now = utcnow()
    stmt = (
        sa.select(StoredObject)
        .where(
            StoredObject.status == OBJ_STATUS_PENDING_DELETION,
            StoredObject.pending_delete_until < now,
        )
        .order_by(StoredObject.pending_delete_until, StoredObject.id)
        .limit(limit)
    )
    expired = list(db.execute(stmt).scalars())
    total_bytes = sum(obj.size_bytes or 0 for obj in expired)
    result: dict = {
        "dry_run": dry_run,
        "count": len(expired),
        "total_bytes": total_bytes,
        "candidates": [_object_summary(obj) for obj in expired],
    }
    if dry_run:
        result["message"] = (
            f"可物理回收 {len(expired)} 个已过保留期的待回收对象, 共 {total_bytes} 字节"
        )
        return result

    purged: list[dict] = []
    errors: list[dict] = []
    store = get_blob_store()
    for obj in expired:
        if obj.storage_path:
            if not store.exists(obj.storage_path):
                errors.append({"id": obj.id, "oid": obj.oid, "reason": "missing_file"})
                continue
            try:
                store.delete_blob(obj.storage_path)
            except OSError as exc:
                errors.append({"id": obj.id, "oid": obj.oid, "reason": str(exc)})
                continue
        _audit(
            db,
            "objects",
            obj.id,
            "object_purged",
            actor_id=actor_id,
            actor_type=actor_type,
            before={
                "oid": obj.oid,
                "sha256": obj.sha256,
                "size_bytes": obj.size_bytes,
                "status": obj.status,
            },
        )
        db.delete(obj)
        purged.append(_object_summary(obj))
    db.flush()
    result["dry_run"] = False
    result["purged_count"] = len(purged)
    result["purged"] = purged
    result["errors"] = errors
    result["message"] = f"已物理回收 {len(purged)} 个对象, {len(errors)} 个失败"
    return result


def undelete_object(
    db: Session,
    object_id: int | str,
    *,
    actor_id: int | None = None,
    actor_type: str = "admin",
) -> dict:
    """恢复误清理对象(0.2.0-B3 恢复路径, 管理员)。

    只允许恢复 status = pending_deletion 且仍在保留期内的对象; 文件保留在
    磁盘上, 恢复后内容立即可访问。已过保留期/已被物理回收的对象不可恢复
    (抛 ObjectNotPendingDeletionError; 物理删除后文件不再存在, 记录已被移除)。

    返回: 恢复后对象的 object_info 视图。
    """
    obj = _resolve_object(db, object_id)
    if obj.status != OBJ_STATUS_PENDING_DELETION:
        raise ObjectNotPendingDeletionError(
            "",
            params={"object_id": obj.id, "oid": obj.oid, "status": obj.status},
            location={"object_type": "objects", "object_id": obj.id, "field": "status"},
        )
    obj.status = OBJ_STATUS_ORPHANED if (obj.ref_count or 0) == 0 else OBJ_STATUS_STORED
    obj.pending_deleted_at = None
    obj.pending_delete_until = None
    _audit(
        db,
        "objects",
        obj.id,
        "object_restored",
        actor_id=actor_id,
        actor_type=actor_type,
        before={"status": "pending_deletion"},
        after={"status": obj.status, "reason": "undelete"},
    )
    db.flush()
    return object_info(db, obj.id)


# ---------------------------------------------------------------------------
# 存储门禁与统计(23.3)
# ---------------------------------------------------------------------------


def estimate_storage(task_type: str, n_hours: int, samples: int) -> int:
    """任务存储需求估算(启发式, 用于任务提交前门禁)。"""
    hours = max(0, int(n_hours))
    n_samples = max(0, int(samples))
    steps = hours * 4  # 按 15min 分辨率步长近似(最坏情况)
    hourly = steps * _HOURLY_FIELDS * _FLOAT64_BYTES

    if task_type in ("calc", "optimization"):
        base = hourly * _INTERMEDIATE_MULTIPLIER + n_samples * steps * _SAMPLE_POINT_BYTES
    elif task_type == "uncertainty":
        base = hourly * max(1, n_samples) + n_samples * _SAMPLE_POINT_BYTES * steps
    elif task_type in ("import", "dataset_build"):
        base = _SNAPSHOT_BASE_BYTES + hours * _SNAPSHOT_PER_HOUR_BYTES + n_samples * _SAMPLE_POINT_BYTES
    elif task_type in ("export", "report"):
        base = hourly * 2 + _SNAPSHOT_BASE_BYTES + n_samples * _SAMPLE_POINT_BYTES
    else:
        raise AppError(
            f"未知任务类型: {task_type}",
            code="SYS-CFG-002",
            message_key="ies.error.task_type_unknown",
            params={
                "task_type": task_type,
                "allowed": "calc,optimization,uncertainty,import,export,report,dataset_build",
            },
        )
    return max(base, _ESTIMATE_FLOOR_BYTES)


def check_capacity(db: Session) -> dict:
    """存储门禁检查: 磁盘剩余空间 vs 安全阈值(23.3)。

    STO-06: 容量不可测(free < 0)时 ok=False 并显式标记 reason, 不静默放行。
    返回: {free_bytes, safe_threshold, ok, message, reason?}。
    """
    try:
        free = int(shutil.disk_usage(settings.data_dir).free)
    except OSError:
        free = -1
    threshold = int(settings.storage_min_free_bytes)
    ok = free > threshold
    result: dict = {"free_bytes": free, "safe_threshold": threshold, "ok": ok}
    if free < 0:
        result["message"] = "无法读取磁盘剩余空间, 新写入将被拒绝"
        result["reason"] = "capacity_unknown"
    elif ok:
        result["message"] = f"可用空间充足: {free / (1 << 30):.2f} GiB > 安全阈值 {threshold / (1 << 30):.2f} GiB"
    else:
        result["message"] = f"可用空间不足: {free / (1 << 30):.2f} GiB ≤ 安全阈值 {threshold / (1 << 30):.2f} GiB"
    return result


def storage_stats(db: Session) -> dict:
    """存储视图(管理接口用): 用量/对象数/引用数/健康。"""
    objects_count = db.execute(
        sa.select(sa.func.count()).select_from(StoredObject)
    ).scalar_one()
    total_bytes = db.execute(
        sa.select(sa.func.coalesce(sa.func.sum(StoredObject.size_bytes), 0))
    ).scalar_one()
    by_status = dict(
        db.execute(
            sa.select(StoredObject.status, sa.func.count())
            .group_by(StoredObject.status)
        ).all()
    )
    orphan_count = db.execute(
        sa.select(sa.func.count())
        .select_from(StoredObject)
        .where(StoredObject.ref_count == 0)
    ).scalar_one()
    pending_count = db.execute(
        sa.select(sa.func.count())
        .select_from(StoredObject)
        .where(StoredObject.status == OBJ_STATUS_PENDING_DELETION)
    ).scalar_one()
    refs_count = db.execute(sa.select(sa.func.count()).select_from(ObjectRef)).scalar_one()
    referenced_objects = db.execute(
        sa.select(sa.func.count(sa.distinct(ObjectRef.object_id))).select_from(ObjectRef)
    ).scalar_one()
    capacity = check_capacity(db)
    return {
        "objects": {
            "count": int(objects_count),
            "total_bytes": int(total_bytes),
            "by_status": {str(k): int(v) for k, v in by_status.items()},
            "orphan_count": int(orphan_count),
            # 0.2.0-B3: 待物理回收对象数(软删/保留期, 供管理视图提示恢复入口)
            "pending_deletion_count": int(pending_count),
        },
        "refs": {"count": int(refs_count), "referenced_objects": int(referenced_objects)},
        "capacity": capacity,
        "healthy": capacity["ok"] and int(objects_count) == int(referenced_objects) + int(orphan_count),
    }


def sample_verify(db: Session, limit: int = 20) -> dict:
    """抽样完整性校验(管理健康接口用): 校验最近 limit 个对象的大小 + sha256。

    返回: {checked, ok_count, failed: [...], capacity}。
    """
    objs = list(
        db.execute(
            sa.select(StoredObject)
            .where(StoredObject.status != OBJ_STATUS_DELETED)
            .order_by(StoredObject.created_at.desc())
            .limit(limit)
        ).scalars()
    )
    failed: list[dict] = []
    ok_count = 0
    for obj in objs:
        report = verify_object(db, obj.id)
        if report["ok"]:
            ok_count += 1
        else:
            failed.append(report)
    return {
        "checked": len(objs),
        "ok_count": ok_count,
        "failed": failed,
        "capacity": check_capacity(db),
    }


def orphaned_stats(db: Session) -> dict:
    """孤儿对象统计(供任务清理建议, STO-05: 业务模块不再直接聚合 StoredObject)。"""
    count, total = db.execute(
        sa.select(
            sa.func.count(StoredObject.id),
            sa.func.coalesce(sa.func.sum(StoredObject.size_bytes), 0),
        ).where(StoredObject.status == OBJ_STATUS_ORPHANED)
    ).one()
    return {"count": int(count or 0), "total_bytes": int(total or 0)}


def usage_summary(db: Session) -> dict:
    """对象用量摘要(供任务提交门禁: 已用/配额, 排除已删除对象)。"""
    used, quota = db.execute(
        sa.select(
            sa.func.coalesce(sa.func.sum(StoredObject.size_bytes), 0),
            sa.func.coalesce(sa.func.sum(StoredObject.quota_bytes), 0),
        ).where(StoredObject.status != OBJ_STATUS_DELETED)
    ).one()
    return {"used_bytes": int(used or 0), "quota_bytes": int(quota or 0)}


# ---------------------------------------------------------------------------
# 恢复协议(STO-04: 幂等 reconciliation)
# ---------------------------------------------------------------------------


def reconcile(db: Session, *, dry_run: bool = True) -> dict:
    """对象存储 reconciliation(启动 readiness / 管理员巡检调用, 幂等)。

    1. 清理超龄临时文件(中断残留, 默认保留 1 天);
    2. 登记磁盘孤儿(有最终文件但无元数据记录):
       - dry_run: 只报告; 非 dry_run: 为该文件补建元数据行(stored, 无引用);
    3. 报告损坏(有元数据但文件缺失或内容与记录不一致);
    4. 修正 ref_count 缓存漂移(以 ObjectRef 清单为权威, STO-02);
    5. 兜底物理回收已过保留期的待回收对象(0.2.0-B3, 非 dry_run 时执行)。

    返回: {dry_run, tmp_cleaned, orphan_registered, orphan_reported,
           corrupt_reported, ref_count_fixed, purged_count}。
    """
    store = get_blob_store()
    tmp_cleaned = store.cleanup_tmp()

    # 2. 磁盘孤儿: 有文件无记录
    known_paths = {
        p for p in db.execute(sa.select(StoredObject.storage_path)).scalars() if p
    }
    final_files = set(store.list_final_files())
    orphans = sorted(final_files - known_paths)
    orphan_registered: list[str] = []
    orphan_reported: list[dict] = []
    if not dry_run:
        for path in orphans:
            try:
                content = store.get_blob(path)
            except BlobMissingError:
                continue
            digest = sha256_hex(content)
            obj = StoredObject(
                oid=digest,
                sha256=digest,
                size_bytes=len(content),
                storage_path=path,
                media_type="application/octet-stream",
                status="stored",
                ref_count=0,
                quota_bytes=0,
            )
            db.add(obj)
            db.flush()
            # §11: 内部路径不入审计; 只记内容摘要 + 大小(可追溯且不泄适配器细节)
            _audit(db, "objects", obj.id, "object_reconciled",
                   after={"sha256": digest, "size_bytes": len(content),
                          "source": "orphan_file"})
            orphan_registered.append(path)
    else:
        # dry-run 同样读文件算摘要: 报告只含内容寻址摘要, 不泄内部路径(§11)
        for path in orphans:
            try:
                content = store.get_blob(path)
                orphan_reported.append({
                    "sha256": sha256_hex(content),
                    "size_bytes": len(content),
                })
            except BlobMissingError:
                continue

    # 3. 损坏: 有记录但文件缺失或内容不一致
    corrupt_reported: list[dict] = []
    for obj in db.execute(
        sa.select(StoredObject).where(StoredObject.status != OBJ_STATUS_DELETED)
    ).scalars():
        if not obj.storage_path:
            corrupt_reported.append({"object_id": obj.id, "oid": obj.oid, "reason": "missing_path"})
            continue
        try:
            raw = store.get_blob(obj.storage_path)
        except BlobMissingError:
            corrupt_reported.append({"object_id": obj.id, "oid": obj.oid, "reason": "missing_file"})
            continue
        if len(raw) != obj.size_bytes or sha256_hex(raw) != obj.sha256:
            corrupt_reported.append(
                {"object_id": obj.id, "oid": obj.oid, "reason": "hash_mismatch"}
            )

    # 4. ref_count 缓存漂移修正(引用清单为权威)
    #    注意: pending_deletion(待物理回收)对象只修 ref_count, 不改变其状态
    #    —— 它们是"已计划回收"生命周期的一部分, 不能被孤儿判定重新置回
    #    orphaned(那会让其脱离 purge 队列, 误清理的恢复路径被意外跳过)。
    ref_count_fixed = 0
    for obj in db.execute(
        sa.select(StoredObject).where(StoredObject.status != OBJ_STATUS_DELETED)
    ).scalars():
        actual = db.execute(
            sa.select(sa.func.count()).select_from(ObjectRef).where(ObjectRef.object_id == obj.id)
        ).scalar_one()
        if (obj.ref_count or 0) != int(actual):
            obj.ref_count = int(actual)
            if int(actual) == 0 and obj.status != OBJ_STATUS_PENDING_DELETION:
                obj.status = OBJ_STATUS_ORPHANED
            ref_count_fixed += 1
    if ref_count_fixed:
        db.flush()

    # 5. 兜底物理回收已过保留期的待回收对象(0.2.0-B3 延迟物理删除;
    #    只处理到期对象, 保留期内绝不物理删)。
    purged_count = 0
    if not dry_run:
        purge = purge_expired(db, dry_run=False, actor_type="reconcile")
        purged_count = int(purge.get("purged_count") or 0)

    return {
        "dry_run": dry_run,
        "tmp_cleaned": tmp_cleaned,
        "orphan_registered": orphan_registered,
        "orphan_reported": orphan_reported,
        "corrupt_reported": corrupt_reported,
        "ref_count_fixed": ref_count_fixed,
        "purged_count": purged_count,
        "healthy": not corrupt_reported,
    }
