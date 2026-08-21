"""存储模块(公开协议, STO-01~07)。

业务模块(project/dataset/results/package/worker)只经本包公开门面访问对象存储:
- 不导入 ``StoredObject/ObjectRef`` ORM, 不拼接对象路径;
- 只消费 ``ObjectHandle`` / ``ObjectOwner`` 等不可变值对象;
- ``storage_path`` 的解释、分桶、临时文件与哈希校验全部是本模块内部实现。

模块结构(10.10 推荐):
- contracts.py      纯类型与公开协议(对象存储门面 / 值对象 / 错误类型);
- persistence.py    StoredObject/ObjectRef repository(模块内部, 迁移自 models/audit.py);
- service.py        哈希、引用、校验、清理、恢复编排(公开门面实现);
- adapters/         文件系统 BlobStore 实现(未来可替换为 S3 等 provider)。

依赖方向: 业务模块 → storage(公开协议) → configured BlobStore adapter。
"""

from iesplan.storage.contracts import (
    BlobStore,
    ObjectCorruptError,
    ObjectHandle,
    ObjectId,
    ObjectOwner,
    ObjectStore,
    StorageQuotaError,
)
from iesplan.storage.service import (
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

__all__ = [
    "BlobStore",
    "ObjectCorruptError",
    "ObjectHandle",
    "ObjectId",
    "ObjectOwner",
    "ObjectStore",
    "StorageQuotaError",
    "add_ref",
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
    "put_object",
    "remove_ref",
    "reconcile",
    "safe_cleanup",
    "sample_verify",
    "storage_stats",
    "verify_object",
]
