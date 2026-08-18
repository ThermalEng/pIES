"""对象存储服务(U11): 内容寻址对象写入/读取/校验/引用/清理与存储门禁。

对应 RPD 第 23 节(存储与对象生命周期)与 01-db-schema.md 第 10 节(审计与对象)。
本模块是对象域(U11)的唯一写入单元:

- put_object: 临时区(data_dir/objects/tmp)写入 → 计算 sha256 → 原子
  rename 提交到 data_dir/objects/{sha256} → 建立 objects 表记录
  (大小/哈希/类型/创建时间; "来源"即 source_category, objects 表无来源列,
  以审计事件承载, 见 _audit);
- 内容去重(23.1): 相同 sha256 只存一份, 复用既有对象记录; 业务引用仍按
  object_refs 单独判断与计数;
- 任何业务记录不得引用半成品对象(23.1): 文件先完整落盘并 rename 成功,
  对象行 flush 成功, 之后才允许建立业务引用;
- 读取校验: get_object 读取时校验大小与 sha256, 不一致抛 ObjectCorruptError;
  verify_object 供周期性完整性巡检, 返回报告不抛错;
- 引用(01 §10.2): add_ref / remove_ref / list_refs, ref_count 原子增减,
  归零时状态置 orphaned;
- 清理(23.3/23.4): safe_cleanup 按 object_refs 与 retention_rules 找出无任何
  业务引用的对象, 先返回计划(dry_run), 确认后执行: 删文件 + 删记录 + 审计;
  被项目版本/快照/证据包/报告引用的对象不可清理(23.2);
- 存储门禁(23.3): estimate_storage 任务存储估算, check_capacity 磁盘余量检查。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from iesplan.config import settings
from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.core.idgen import sha256_hex
from iesplan.models.audit import AuditLog, ObjectRef, RetentionRule, StoredObject

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 对象状态(01 §10.1 CHECK 枚举)
OBJ_STATUS_STORED = "stored"
OBJ_STATUS_ORPHANED = "orphaned"
OBJ_STATUS_PENDING_DELETION = "pending_deletion"
OBJ_STATUS_DELETED = "deleted"

#: 引用类别 → 引用方实体表名(01 §10.2 ref_entity_type; 未命中时按 ref_type 原样入库)
REF_ENTITY_TYPE_MAP: dict[str, str] = {
    "dataset_file": "dataset_files",  # 数据集版本文件(01 §5.3)
    "version_ref": "project_versions",  # 项目版本(01 §3.3)
    "snapshot_ref": "calc_snapshots",  # 计算快照(01 §7.x)
    "evidence_package": "evidence_packages",  # 证据包(01 §8.1)
    "report": "reports",  # 报告(01 §8.5)
    "result_ref": "eval_results",  # 评估结果(01 §8.x)
}

#: 引用期间不可清理的引用方实体类型(RPD 23.2; 与 ref_count 构成双保险)
PROTECTED_ENTITY_TYPES: frozenset[str] = frozenset(
    {"project_versions", "calc_snapshots", "evidence_packages", "reports", "dataset_files"}
)

#: 未命中保留规则时孤儿对象的默认保留天数(0 = 引用归零即可清理)
DEFAULT_RETENTION_DAYS: int = 0
#: 单次清理最大对象数(防一次性事务过大)
CLEANUP_BATCH_LIMIT: int = 1000

# 存储估算参数(23.3; 启发式, 只用于门禁判断, 不参与计算)
#: float64 字节数
_FLOAT64_BYTES: int = 8
#: 逐时结果典型字段数(02 §8: 电/热/冷负荷、光伏、储能、热泵、锅炉、冷水机等)
_HOURLY_FIELDS: int = 20
#: 快照基础体积(模型/配置/数据引用的序列化估计)
_SNAPSHOT_BASE_BYTES: int = 1 << 20  # 1 MiB
#: 快照每小时附加体积(抽样数据)
_SNAPSHOT_PER_HOUR_BYTES: int = 256
#: 中间结果放大倍数(多工况/迭代展开)
_INTERMEDIATE_MULTIPLIER: int = 3
#: 样本单点体积(统计摘要, 23.3: 样本任务可只保留摘要)
_SAMPLE_POINT_BYTES: int = 32
#: 估算下限(避免零长度估算)
_ESTIMATE_FLOOR_BYTES: int = 1024


# ---------------------------------------------------------------------------
# 业务异常
# ---------------------------------------------------------------------------


class ObjectCorruptError(AppError):
    """对象内容与记录不一致(大小或 sha256 不匹配, 或文件缺失)。"""

    code = "OBJ-CORRUPT-001"
    message_key = "ies.diag.obj.corrupt"
    http_status = 500


class StorageQuotaError(ConflictError):
    """磁盘剩余空间低于安全阈值, 拒绝写入(23.3)。"""

    code = "SYS-STORE-003"
    message_key = "ies.error.storage_quota"


class ObjectQuotaError(ConflictError):
    """对象配额(quota_bytes)超限, 拒绝写入(01 §10.1)。"""

    code = "SYS-STORE-005"
    message_key = "ies.error.object_quota_exceeded"


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    """当前 UTC 时间。"""
    return datetime.now(UTC)


def as_utc(dt: datetime | None) -> datetime | None:
    """规范化为带 UTC 时区的时间(无时区按 UTC 处理, SQLite 返回 naive 时间)。"""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def objects_root() -> Path:
    """对象存储根目录(settings.data_dir/objects), 不存在则自动创建。"""
    root = settings.data_dir / "objects"
    root.mkdir(parents=True, exist_ok=True)
    return root


def tmp_root() -> Path:
    """对象临时区目录(settings.data_dir/objects/tmp), 不存在则自动创建。"""
    root = objects_root() / "tmp"
    root.mkdir(parents=True, exist_ok=True)
    return root


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


def _object_path(obj: StoredObject) -> Path:
    """对象磁盘绝对路径(storage_path 为相对 data_dir 的路径)。"""
    return settings.data_dir / (obj.storage_path or f"objects/{obj.sha256}")


def _read_file(obj: StoredObject) -> bytes:
    """读取对象文件内容, 文件缺失抛 ObjectCorruptError。"""
    path = _object_path(obj)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ObjectCorruptError(
            "",
            params={
                "object_id": obj.id,
                "oid": obj.oid,
                "reason": "missing",
                "path": str(path),
            },
        ) from exc


def _check_quota(obj: StoredObject, size_bytes: int) -> None:
    """对象配额检查: quota_bytes > 0 表示限定额度(0 = 不限)。"""
    if obj.quota_bytes and size_bytes > obj.quota_bytes:
        raise ObjectQuotaError(
            "",
            params={"object_id": obj.id, "size": size_bytes, "quota": obj.quota_bytes},
            location={"object_type": "objects", "object_id": str(obj.id), "field": "quota_bytes"},
        )


def _check_disk_capacity() -> None:
    """磁盘余量门禁: 低于安全阈值拒绝写入(23.3)。"""
    try:
        free = shutil.disk_usage(settings.data_dir).free
    except OSError:
        # 磁盘信息不可得时放行, 由 check_capacity 上报不可用状态
        return
    if free < settings.storage_min_free_bytes:
        raise StorageQuotaError(
            "",
            params={"free": free, "safe_threshold": settings.storage_min_free_bytes},
        )


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


def _ref_entity_type(ref_type: str, ref_entity_type: str | None) -> str:
    """引用方实体表名: 显式指定优先, 否则按 REF_ENTITY_TYPE_MAP 推断。"""
    return ref_entity_type or REF_ENTITY_TYPE_MAP.get(ref_type, ref_type)


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


# ---------------------------------------------------------------------------
# 写入(23.1)
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
) -> StoredObject:
    """写入内容寻址对象(01 §10.1 / RPD 23.1)。

    流程: 写临时区 → 计算 sha256 → 原子 rename 到 data_dir/objects/{sha256}
    → 建立 objects 表记录 → (可选)建立业务引用。任何一步失败都不会让业务记录
    引用到半成品对象: 文件完整落盘后才建对象行, 对象行提交后才建引用。

    内容去重: 相同 sha256 已存在则复用对象记录, 但传入的 ref_type/ref_id
    业务引用仍按 object_refs 单独建立(去重不影响业务引用语义)。

    参数:
        db: 数据库会话(本函数只 flush, 提交由调用方负责)。
        content: 对象字节内容。
        content_type: 媒体类型(MIME, 记入 objects.media_type)。
        source_category: 来源类别(如 user_upload/evidence/report/export;
            objects 表无来源列, 记入创建审计事件的 after.source_category)。
        ref_type/ref_id: 可选初始业务引用(如 dataset_file/version_ref)。
        ref_entity_type: 引用方实体表名, 缺省按 REF_ENTITY_TYPE_MAP 推断。
        actor_id/actor_type: 审计操作者(缺省 system)。
    返回:
        StoredObject 记录(新建或按 sha256 复用)。
    """
    digest = sha256_hex(content)
    # 1. 内容去重: 相同 sha256 已存在 → 复用记录(业务引用仍单独建立)
    existing = db.execute(
        sa.select(StoredObject)
        .where(StoredObject.sha256 == digest, StoredObject.status != OBJ_STATUS_DELETED)
    ).scalar_one_or_none()
    if existing is not None:
        _check_quota(existing, len(content))
        if ref_type is not None and ref_id is not None:
            add_ref(
                db,
                existing.id,
                ref_type,
                ref_id,
                ref_entity_type=ref_entity_type,
                purpose=purpose,
                actor_id=actor_id,
                actor_type=actor_type,
            )
        return existing

    # 2. 门禁: 磁盘余量(23.3) + 配额
    _check_disk_capacity()

    # 3. 临时区写入 → 原子 rename 提交(同盘 rename 保证文件要么完整要么不存在)
    final_path = objects_root() / digest
    fd, tmp_name = tempfile.mkstemp(dir=tmp_root(), prefix="put-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, final_path)
    except BaseException:
        # 失败时清理临时文件, 不留下半成品
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    # 4. 建立对象行(文件已完整落盘, 此时才允许进入引用体系)
    obj = StoredObject(
        oid=digest,
        sha256=digest,
        size_bytes=len(content),
        storage_path=f"objects/{digest}",
        media_type=content_type,
        status=OBJ_STATUS_STORED,
        ref_count=0,
        quota_bytes=0,
    )
    db.add(obj)
    try:
        db.flush()
    except IntegrityError:
        # 并发写入去重: 同内容对象已被其他事务建立, 复用即可(文件内容一致)
        db.rollback()
        existing = db.execute(
            sa.select(StoredObject).where(StoredObject.sha256 == digest)
        ).scalar_one_or_none()
        if existing is None:
            raise
        if ref_type is not None and ref_id is not None:
            add_ref(
                db,
                existing.id,
                ref_type,
                ref_id,
                ref_entity_type=ref_entity_type,
                purpose=purpose,
                actor_id=actor_id,
                actor_type=actor_type,
            )
        return existing

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
            "storage_path": obj.storage_path,
        },
    )

    # 6. 初始业务引用(对象已完整, 此时才允许建立引用)
    if ref_type is not None and ref_id is not None:
        add_ref(
            db,
            obj.id,
            ref_type,
            ref_id,
            ref_entity_type=ref_entity_type,
            purpose=purpose,
            actor_id=actor_id,
            actor_type=actor_type,
        )
    return obj


# ---------------------------------------------------------------------------
# 读取与校验(23.1)
# ---------------------------------------------------------------------------


def get_object(db: Session, object_id: int | str) -> bytes:
    """读取对象字节并校验完整性(大小 + sha256), 不一致抛 ObjectCorruptError。"""
    obj = _resolve_object(db, object_id)
    raw = _read_file(obj)
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
    """对象元数据视图(不含内容)。"""
    obj = _resolve_object(db, object_id)
    return {
        "id": obj.id,
        "oid": obj.oid,
        "sha256": obj.sha256,
        "size_bytes": obj.size_bytes,
        "storage_path": obj.storage_path,
        "media_type": obj.media_type,
        "status": obj.status,
        "ref_count": obj.ref_count,
        "quota_bytes": obj.quota_bytes,
        "created_at": as_utc(obj.created_at).isoformat() if obj.created_at else None,
        "last_referenced_at": (
            as_utc(obj.last_referenced_at).isoformat() if obj.last_referenced_at else None
        ),
    }


def verify_object(db: Session, object_id: int | str) -> dict:
    """完整性校验(周期性巡检用): 返回报告不抛错。

    报告字段: ok / size_ok / hash_ok / expected_* / actual_* / error。
    """
    try:
        obj = _resolve_object(db, object_id)
        raw = _read_file(obj)
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
# 引用(01 §10.2)
# ---------------------------------------------------------------------------


def add_ref(
    db: Session,
    object_id: int | str,
    ref_type: str,
    ref_id: int,
    ref_entity_type: str | None = None,
    purpose: str | None = None,
    *,
    actor_id: int | None = None,
    actor_type: str = "system",
) -> ObjectRef:
    """建立业务引用并原子递增 ref_count(重复引用幂等返回既有引用)。"""
    obj = _resolve_object(db, object_id)
    entity_type = _ref_entity_type(ref_type, ref_entity_type)
    ref = ObjectRef(
        object_id=obj.id,
        ref_type=ref_type,
        ref_entity_type=entity_type,
        ref_entity_id=int(ref_id),
        purpose=purpose,
    )
    db.add(ref)
    obj.ref_count = (obj.ref_count or 0) + 1
    obj.last_referenced_at = utcnow()
    try:
        db.flush()
    except IntegrityError:
        # 唯一约束 (object_id, ref_type, ref_entity_type, ref_entity_id):
        # 重复引用幂等返回既有行(计数不重复累加)
        db.rollback()
        existing = db.execute(
            sa.select(ObjectRef).where(
                ObjectRef.object_id == obj.id,
                ObjectRef.ref_type == ref_type,
                ObjectRef.ref_entity_type == entity_type,
                ObjectRef.ref_entity_id == int(ref_id),
            )
        ).scalar_one()
        return existing
    _audit(
        db,
        "objects",
        obj.id,
        "object_ref_add",
        actor_id=actor_id,
        actor_type=actor_type,
        after={"ref_type": ref_type, "ref_entity_type": entity_type, "ref_entity_id": int(ref_id)},
    )
    return ref


def remove_ref(
    db: Session,
    object_id: int | str,
    ref_type: str,
    ref_id: int,
    ref_entity_type: str | None = None,
    *,
    actor_id: int | None = None,
    actor_type: str = "system",
) -> None:
    """解除业务引用并递减 ref_count; 引用不存在抛 NotFoundError。

    引用计数归零时对象状态置 orphaned(01 §10.1 删除协议第一跳)。
    """
    obj = _resolve_object(db, object_id)
    entity_type = _ref_entity_type(ref_type, ref_entity_type)
    ref = db.execute(
        sa.select(ObjectRef).where(
            ObjectRef.object_id == obj.id,
            ObjectRef.ref_type == ref_type,
            ObjectRef.ref_entity_type == entity_type,
            ObjectRef.ref_entity_id == int(ref_id),
        )
    ).scalar_one_or_none()
    if ref is None:
        raise NotFoundError(
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
        before={"ref_type": ref_type, "ref_entity_type": entity_type, "ref_entity_id": int(ref_id)},
    )


def list_refs(db: Session, object_id: int | str) -> list[ObjectRef]:
    """列出对象全部业务引用(按建立时间升序)。"""
    obj = _resolve_object(db, object_id)
    return list(
        db.execute(
            sa.select(ObjectRef)
            .where(ObjectRef.object_id == obj.id)
            .order_by(ObjectRef.created_at)
        ).scalars()
    )


# ---------------------------------------------------------------------------
# 清理(23.3/23.4)
# ---------------------------------------------------------------------------


def _candidate_objects(db: Session, limit: int = CLEANUP_BATCH_LIMIT) -> list[StoredObject]:
    """无任何业务引用的对象候选集。

    双条件判断: ref_count == 0 且 object_refs 无任何行(防计数漂移),
    且状态未删除(01 §10.1 删除协议起点)。
    """
    no_refs = sa.select(ObjectRef.id).where(ObjectRef.object_id == StoredObject.id).exists()
    # 双保险(23.2): 即使引用计数漂移, 被项目版本/快照/证据包/报告等引用的对象
    # 也通过实体类型过滤直接排除, 绝不允许进入清理候选
    protected_refs = sa.select(ObjectRef.id).where(
        ObjectRef.object_id == StoredObject.id,
        ObjectRef.ref_entity_type.in_(PROTECTED_ENTITY_TYPES),
    ).exists()
    stmt = (
        sa.select(StoredObject)
        .where(
            StoredObject.ref_count == 0,
            ~no_refs,
            ~protected_refs,
            StoredObject.status != OBJ_STATUS_DELETED,
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
    }


def safe_cleanup(
    db: Session,
    dry_run: bool = True,
    *,
    actor_id: int | None = None,
    actor_type: str = "admin",
    limit: int = CLEANUP_BATCH_LIMIT,
) -> dict:
    """对象清理(23.3/23.4): 先计划后执行, 全程可审计。

    - 候选: 无任何业务引用(ref_count == 0 且 object_refs 无行)且未删除的对象;
    - 保留规则(retention_rules, 01 §10.5): 命中 active 规则时要求对象年龄
      达到 retention_days, 未命中规则默认 DEFAULT_RETENTION_DAYS 天;
    - 被引用对象(含项目版本/快照/证据包/报告引用的对象, 23.2)天然不可清理;
    - dry_run=True: 只返回清理计划, 不删任何数据;
    - dry_run=False: 执行清理 — 删文件 + 删记录 + 审计, 单事务提交;
      文件删除失败的对象跳过(保留记录, 便于下次重试)。

    返回:
        计划/执行结果字典: dry_run / count / total_bytes / candidates /
        retained_count / retained / (dry_run=False 时) removed_count / removed / errors。
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
    result: dict = {
        "dry_run": dry_run,
        "count": len(deletable),
        "total_bytes": total_bytes,
        "candidates": [_object_summary(obj) for obj in deletable],
        "retained_count": len(retained),
        "retained": retained,
    }
    if dry_run:
        result["message"] = (
            f"清理计划: {len(deletable)} 个无引用对象共 {total_bytes} 字节可清理"
        )
        return result

    # 执行: 删文件 + 删记录 + 审计(23.4 第 6 步: 重试清理无引用对象)
    removed: list[dict] = []
    errors: list[dict] = []
    for obj in deletable:
        try:
            _object_path(obj).unlink()
        except OSError as exc:
            # 文件已不存在或删除失败: 跳过本对象, 保留记录便于重试
            errors.append({"id": obj.id, "oid": obj.oid, "reason": str(exc)})
            continue
        # 双保险: 清除残留引用行(正常情况无)
        db.execute(sa.delete(ObjectRef).where(ObjectRef.object_id == obj.id))
        _audit(
            db,
            "objects",
            obj.id,
            "object_cleanup",
            actor_id=actor_id,
            actor_type=actor_type,
            before={"oid": obj.oid, "sha256": obj.sha256, "size_bytes": obj.size_bytes},
        )
        db.delete(obj)
        removed.append(_object_summary(obj))
    db.commit()
    result["dry_run"] = False
    result["removed_count"] = len(removed)
    result["removed"] = removed
    result["errors"] = errors
    result["message"] = f"已清理 {len(removed)} 个无引用对象, {len(errors)} 个失败"
    return result


# ---------------------------------------------------------------------------
# 存储门禁(23.3)
# ---------------------------------------------------------------------------


def estimate_storage(task_type: str, n_hours: int, samples: int) -> int:
    """任务存储需求估算(启发式, 用于任务提交前门禁)。

    - calc/optimization: 逐时结果(20 字段 float64) × 中间结果放大 + 样本点;
    - uncertainty: 逐时结果 × 样本数(Monte Carlo 展开);
    - import/dataset_build: 快照基础体积 + 每小时抽样附加;
    - export/report: 逐时结果两份(原文 + 汇总) + 快照基础体积。

    参数:
        task_type: 任务类型(01 §7.2: calc/optimization/uncertainty/import/export/report/dataset_build)。
        n_hours: 计算时长(小时; 15min 分辨率按 4 倍步长近似)。
        samples: 样本数(uncertainty 任务为采样次数, 其余为输出对象数)。
    返回:
        估算字节数(不小于下限 _ESTIMATE_FLOOR_BYTES)。
    """
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

    返回: {free_bytes, safe_threshold, ok, message}。
    """
    try:
        free = int(shutil.disk_usage(settings.data_dir).free)
    except OSError:
        free = -1
    threshold = int(settings.storage_min_free_bytes)
    ok = free > threshold
    if free < 0:
        message = "无法读取磁盘剩余空间"
    elif ok:
        message = f"可用空间充足: {free / (1 << 30):.2f} GiB > 安全阈值 {threshold / (1 << 30):.2f} GiB"
    else:
        message = f"可用空间不足: {free / (1 << 30):.2f} GiB ≤ 安全阈值 {threshold / (1 << 30):.2f} GiB"
    return {"free_bytes": free, "safe_threshold": threshold, "ok": ok, "message": message}


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
