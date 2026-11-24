"""对象存储服务：对象内容、引用、孤儿清理和磁盘恢复。"""

from __future__ import annotations

import errno
import logging
import secrets
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from iesplan.config import settings
from iesplan.core.content import Content, MemoryContent, SizedContent, read_content
from iesplan.core.errors import NotFoundError
from iesplan.storage.adapters.filesystem import FileSystemBlobStore
from iesplan.storage.contracts import (
    BlobMissingError,
    ObjectCorruptError,
    ObjectHandle,
    ObjectWriteError,
    ReferenceNotFoundError,
    RefInfo,
    RetentionPolicy,
)
from iesplan.storage.persistence import (
    OBJ_STATUS_ORPHANED,
    OBJ_STATUS_STORED,
    ObjectRef,
    StoredObject,
    list_active_retention_rules,
)

logger = logging.getLogger(__name__)

#: 未命中保留规则时孤儿对象的默认保留天数(0 = 引用归零即可清理)
DEFAULT_RETENTION_DAYS: int = 0
#: 单次清理最大对象数(防一次性事务过大)
CLEANUP_BATCH_LIMIT: int = 1000
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
    """按主键 id 或对象 id(oid)解析对象, 不存在抛 NotFoundError。"""
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


def _match_retention_rule(rules: list[RetentionPolicy], obj: StoredObject) -> RetentionPolicy | None:
    """匹配对象保留规则(01 §10.5)。

    仅匹配 entity_type='objects' 且 object_kind 为 '*' 或与对象媒体类型一致的
    active 规则; 取最小保留天数(最严格先满足)。规则为 storage 公开契约值对象,
    不接触保留规则 ORM。
    """
    matched: RetentionPolicy | None = None
    for rule in rules:
        if rule.status != "active" or rule.entity_type != "objects":
            continue
        if rule.object_kind not in ("*", obj.media_type or ""):
            continue
        if matched is None or rule.retention_days < matched.retention_days:
            matched = rule
    return matched


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
    """写入新对象(架构宪法 §10 存储与数据生命周期), 返回公开句柄。

    流程: 生成对象 id → BlobStore 原子落盘 → 新增元数据行 →
    (可选)建立 owner 引用。文件完整落盘后才建对象行, 对象行 flush 后才建引用。

    每次写入新建对象行(无内容去重); 寻址键为对象 id(oid 随机生成,
    文件名即 oid); 不计算内容摘要, 不做完整性复核 —— 二进制分发完整性
    不由业务程序负责。

    参数:
        db: 数据库会话(本函数只 flush, 提交由调用方负责)。
        content: 对象字节内容。
        content_type: 媒体类型(MIME, 记入 objects.media_type)。
        source_category: 来源类别(如 user_upload/evidence/report/export;
            记入对象行来源字段, 需要审计的业务用例自行记录)。
        ref_type/ref_id/ref_entity_type/purpose: 可选初始业务引用(见 attach)。
    返回:
        ObjectHandle(新建对象)。
    """
    return put_content(
        db, MemoryContent(content), content_type, source_category,
        ref_type, ref_id, ref_entity_type, purpose, actor_id=actor_id, actor_type=actor_type,
    )


def put_content(
    db: Session, content: Content, content_type: str, source_category: str,
    ref_type: str | None = None, ref_id: int | None = None,
    ref_entity_type: str | None = None, purpose: str | None = None,
    *, actor_id: int | None = None, actor_type: str = "system",
) -> ObjectHandle:
    """Consume chunks atomically; size is the actual write count, never a second read."""
    # 1. 对象 id(随机 64 位 hex, 满足 ck_objects_oid; 文件名即 oid)
    oid = secrets.token_hex(32)

    # 2. BlobStore 原子落盘(临时区 → fsync → rename)
    store = get_blob_store()
    try:
        storage_path, size = store.put_blob(content, oid)
    except OSError as exc:
        reason = errno.errorcode.get(exc.errno, type(exc).__name__)
        raise ObjectWriteError(
            f"对象写入失败（{reason}）", params={"reason": reason},
        ) from exc

    # 3. 元数据行(本函数只 flush, 提交/回滚由调用方负责)
    obj = StoredObject(
        oid=oid,
        size_bytes=size,
        storage_path=storage_path,
        media_type=content_type,
        status="stored",
        ref_count=0,
    )
    db.add(obj)
    db.flush()

    # 4. 对象创建审计由需要的公开业务用例在成功路径记录; 存储层不反向依赖 audit。
    # 5. 初始业务引用(对象已完整, 此时才允许建立引用)
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


@dataclass(frozen=True, slots=True)
class _ObjectContent:
    source: Content
    object_id: int
    size_bytes: int

    @property
    def path(self) -> Path | None:
        """Expose underlying local file path when backed by a filesystem blob."""
        return getattr(self.source, "path", None)

    def iter_chunks(self):
        try:
            yield from self.source.iter_chunks()
        except BlobMissingError as exc:
            raise ObjectCorruptError(
                "", params={"object_id": self.object_id, "reason": "missing"},
            ) from exc


def get_object_content(db: Session, object_id: int | str) -> SizedContent:
    """返回独立于会话的可重复读取内容；实际读取失败抛 ObjectCorruptError。

    不做内容复核(大小/摘要比对已删除, 二进制分发完整性不由业务程序负责)。
    """
    obj = _resolve_object(db, object_id)
    if not obj.storage_path:
        raise ObjectCorruptError(
            "",
            params={"object_id": obj.id, "oid": obj.oid, "reason": "missing_path"},
        )
    return _ObjectContent(get_blob_store().get_content(obj.storage_path), obj.id, obj.size_bytes)


def get_object(db: Session, object_id: int | str) -> bytes:
    """Explicit small-object convenience, using the same content source."""
    return read_content(get_object_content(db, object_id))


def object_info(db: Session, object_id: int | str) -> dict:
    """对象元数据视图(不含内容)。

    §11 内部路径不得进入 DTO/日志/证据包；ref_count 为
    §10.3 可重建缓存, 仅供管理视图参考, 权威引用清单经 list_refs 查询。
    """
    obj = _resolve_object(db, object_id)
    return {
        "id": obj.id,
        "oid": obj.oid,
        "size_bytes": obj.size_bytes,
        "media_type": obj.media_type,
        "status": obj.status,
        "ref_count": obj.ref_count,
        "created_at": as_utc(obj.created_at).isoformat() if obj.created_at else None,
        "last_referenced_at": (
            as_utc(obj.last_referenced_at).isoformat() if obj.last_referenced_at else None
        ),
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
            obj.status = OBJ_STATUS_STORED
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


def _ref_to_public_dict(r: ObjectRef) -> dict:
    """ObjectRef 行 → 公开视图 dict(与 find_refs_by_owner 同构)。"""
    return {
        "object_id": r.object_id,
        "ref_type": r.ref_type,
        "ref_entity_type": r.ref_entity_type,
        "ref_entity_id": r.ref_entity_id,
        "purpose": r.purpose,
        "created_at": as_utc(r.created_at).isoformat() if r.created_at else None,
    }


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
    return [_ref_to_public_dict(r) for r in rows]


def find_refs_by_entity_type(db: Session, ref_entity_type: str) -> list[dict]:
    """按引用方实体类型列全部引用(公开门面, 供临时 owner reconciliation)。

    STO-05: 调用方(如 application 用例)声明实体类型字符串, 存储只判断
    "是否有引用", 不导入业务模型。返回与 find_refs_by_owner 同构的公开视图。
    """
    rows = db.execute(
        sa.select(ObjectRef)
        .where(ObjectRef.ref_entity_type == ref_entity_type)
        .order_by(ObjectRef.id)
    ).scalars()
    return [_ref_to_public_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 清理(STO-02: 引用清单为权威, 任意存在的 owner 引用阻止清理)
# ---------------------------------------------------------------------------


def _without_refs():
    return ~sa.select(ObjectRef.id).where(ObjectRef.object_id == StoredObject.id).exists()


def _object_summary(obj: StoredObject) -> dict:
    """对象行摘要(清理计划/结果清单条目)。"""
    return {
        "id": obj.id,
        "oid": obj.oid,
        "size_bytes": obj.size_bytes,
        "media_type": obj.media_type,
        "status": obj.status,
        "created_at": as_utc(obj.created_at).isoformat() if obj.created_at else None,
    }


def safe_cleanup(db: Session, dry_run: bool = True, *, limit: int = CLEANUP_BATCH_LIMIT) -> dict:
    """Preview or remove unreferenced objects subject to their explicit retention policy."""
    stmt = (
        sa.select(StoredObject).where(_without_refs())
        .order_by(StoredObject.created_at, StoredObject.id).limit(limit)
    )
    candidates = db.scalars(stmt if dry_run else stmt.with_for_update()).all()
    rules = list_active_retention_rules(db)
    now = utcnow()
    deletable = []
    retained = []
    for obj in candidates:
        age_days = (now - as_utc(obj.created_at)).total_seconds() / 86400
        rule = _match_retention_rule(rules, obj)
        days = rule.retention_days if rule is not None else DEFAULT_RETENTION_DAYS
        if age_days < days:
            retained.append({**_object_summary(obj), "retention_days": days})
        else:
            deletable.append(obj)

    result = {
        "dry_run": dry_run,
        "count": len(deletable),
        "total_bytes": sum(obj.size_bytes for obj in deletable),
        "candidates": [_object_summary(obj) for obj in deletable],
        "retained": retained,
        "cleaned": [],
        "errors": [],
    }
    if dry_run:
        return result

    store = get_blob_store()
    for obj in deletable:
        summary = _object_summary(obj)
        try:
            with db.begin_nested():
                # Flush the deletion first: a real concurrent reference must
                # prevent deleting its file even if it arrived during selection.
                db.delete(obj)
                db.flush()
                if obj.storage_path:
                    store.delete_blob(obj.storage_path)
        except IntegrityError:
            result["errors"].append({"id": obj.id, "oid": obj.oid, "reason": "referenced"})
        except OSError:
            logger.exception("对象文件删除失败: oid=%s", obj.oid)
            result["errors"].append({"id": obj.id, "oid": obj.oid, "reason": "delete_failed"})
        else:
            result["cleaned"].append(summary)
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
        .where(_without_refs())
    ).scalar_one()
    refs_count = db.execute(sa.select(sa.func.count()).select_from(ObjectRef)).scalar_one()
    referenced_objects = db.execute(
        sa.select(sa.func.count(sa.distinct(ObjectRef.object_id))).select_from(ObjectRef)
    ).scalar_one()
    try:
        free_bytes = shutil.disk_usage(settings.data_dir).free
    except OSError:
        free_bytes = None
    return {
        "objects": {
            "count": int(objects_count),
            "total_bytes": int(total_bytes),
            "by_status": {str(k): int(v) for k, v in by_status.items()},
            "orphan_count": int(orphan_count),
        },
        "refs": {"count": int(refs_count), "referenced_objects": int(referenced_objects)},
        "capacity": {"free_bytes": free_bytes},
        "healthy": free_bytes is not None,
    }


# 恢复协议(STO-04: 幂等 reconciliation)
# ---------------------------------------------------------------------------


def reconcile(db: Session, *, dry_run: bool = True) -> dict:
    """对象存储 reconciliation(启动 readiness / 管理员巡检调用, 幂等)。

    1. 清理超龄临时文件(中断残留, 默认保留 1 天);
    2. 登记磁盘孤儿(有最终文件但无元数据记录):
       - dry_run: 只报告; 非 dry_run: 为该文件补建元数据行(stored, 无引用,
         文件名即对象 id, 不计算内容摘要);
    3. 报告损坏(有元数据但文件缺失; 存在性, 不做内容复核);
    4. 修正 ref_count 缓存漂移(以 ObjectRef 清单为权威, STO-02)。

    返回: {dry_run, tmp_cleaned, orphan_registered, orphan_reported,
           corrupt_reported, ref_count_fixed}。
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
                size = store.blob_size(path)
            except BlobMissingError:
                continue
            # 文件名即对象 id(不计算内容摘要); 冲突时跳过(已有记录认领)
            oid = Path(path).name
            clash = db.execute(
                sa.select(StoredObject).where(StoredObject.oid == oid)
            ).scalar_one_or_none()
            if clash is not None:
                continue
            obj = StoredObject(
                oid=oid,
                size_bytes=size,
                storage_path=path,
                media_type="application/octet-stream",
                status="stored",
                ref_count=0,
            )
            db.add(obj)
            db.flush()
            orphan_registered.append(path)
    else:
        # dry-run 只报告文件名(即认领后的对象 id)与大小, 不泄内部路径(§11)
        for path in orphans:
            try:
                size = store.blob_size(path)
                orphan_reported.append({
                    "oid": Path(path).name,
                    "size_bytes": size,
                })
            except BlobMissingError:
                continue

    # 3. 损坏: 有记录但文件缺失(存在性, 不做内容复核)
    corrupt_reported: list[dict] = []
    for obj in db.execute(
        sa.select(StoredObject)
    ).scalars():
        if not obj.storage_path:
            corrupt_reported.append({"object_id": obj.id, "oid": obj.oid, "reason": "missing_path"})
            continue
        if not store.exists(obj.storage_path):
            corrupt_reported.append({"object_id": obj.id, "oid": obj.oid, "reason": "missing_file"})
            continue

    # 4. ref_count 缓存漂移修正(引用清单为权威)
    ref_count_fixed = 0
    for obj in db.execute(
        sa.select(StoredObject)
    ).scalars():
        actual = db.execute(
            sa.select(sa.func.count()).select_from(ObjectRef).where(ObjectRef.object_id == obj.id)
        ).scalar_one()
        if (obj.ref_count or 0) != int(actual):
            obj.ref_count = int(actual)
            if int(actual) == 0:
                obj.status = OBJ_STATUS_ORPHANED
            ref_count_fixed += 1
    if ref_count_fixed:
        db.flush()

    return {
        "dry_run": dry_run,
        "tmp_cleaned": tmp_cleaned,
        "orphan_registered": orphan_registered,
        "orphan_reported": orphan_reported,
        "corrupt_reported": corrupt_reported,
        "ref_count_fixed": ref_count_fixed,
        "healthy": not corrupt_reported,
    }
