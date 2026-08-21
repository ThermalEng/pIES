"""对象存储服务兼容入口(已迁移至 iesplan.storage, STO-01~07)。

本文件保留旧调用签名作为过渡委托层, 全部实现位于 iesplan.storage 包:
- put_object / get_object / object_info / verify_object / add_ref /
  remove_ref / list_refs / safe_cleanup / estimate_storage / check_capacity /
  storage_stats / sample_verify / reconcile;
- 业务模块应改从 iesplan.storage 导入公开门面; 本兼容层只在新边界落地后删除。

新增公开协议(STO-01/05):
- ObjectHandle / ObjectOwner / ObjectId: 不可变值对象, 业务模块不接触 ORM;
- attach / detach: owner 引用成对协议(引用清单为唯一权威, STO-02);
- reconcile: 幂等恢复协议(临时文件清理/孤儿登记/损坏报告/计数修正, STO-04)。

事务纪律(STO-03): 本模块所有函数只 flush, 唯一键竞争在 savepoint 内处理,
绝不调用调用方 Session 的全局 rollback()。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import shutil

from sqlalchemy.orm import Session

from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.core.idgen import sha256_hex
from iesplan.storage import (
    ObjectCorruptError,
    ObjectHandle,
    ObjectOwner,
    StorageQuotaError,
    add_ref,
    attach,
    check_capacity,
    detach,
    estimate_storage,
    find_refs_by_owner,
    get_object,
    list_refs,
    object_by_sha256,
    object_info,
    orphaned_stats,
    usage_summary,
    put_object,
    remove_ref,
    reconcile,
    safe_cleanup,
    sample_verify,
    storage_stats,
    verify_object,
)
from iesplan.storage.contracts import ObjectQuotaError, RefInfo, ReferenceNotFoundError

# 兼容旧导入: 旧对象状态常量
OBJ_STATUS_STORED = "stored"
OBJ_STATUS_ORPHANED = "orphaned"
OBJ_STATUS_PENDING_DELETION = "pending_deletion"
OBJ_STATUS_DELETED = "deleted"

#: 旧路径辅助(仅测试/旧调用方使用; 新代码使用 storage 公开门面)
objects_root = __import__("iesplan.storage.adapters.filesystem", fromlist=["_objects_root"])._objects_root
tmp_root = __import__("iesplan.storage.adapters.filesystem", fromlist=["_tmp_root"])._tmp_root


def utcnow() -> datetime:
    """当前 UTC 时间。"""
    return datetime.now(UTC)


def as_utc(dt: datetime | None) -> datetime | None:
    """规范化为带 UTC 时区的时间。"""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


__all__ = [
    "sha256_hex",
    "OBJ_STATUS_STORED",
    "OBJ_STATUS_ORPHANED",
    "OBJ_STATUS_PENDING_DELETION",
    "OBJ_STATUS_DELETED",
    "ObjectCorruptError",
    "ObjectHandle",
    "ObjectOwner",
    "ObjectQuotaError",
    "RefInfo",
    "ReferenceNotFoundError",
    "shutil",
    "StorageQuotaError",
    "add_ref",
    "as_utc",
    "attach",
    "check_capacity",
    "detach",
    "estimate_storage",
    "find_refs_by_owner",
    "get_object",
    "list_refs",
    "object_by_sha256",
    "object_info",
    "orphaned_stats",
    "usage_summary",
    "objects_root",
    "put_object",
    "reconcile",
    "remove_ref",
    "safe_cleanup",
    "sample_verify",
    "storage_stats",
    "tmp_root",
    "utcnow",
    "verify_object",
]
