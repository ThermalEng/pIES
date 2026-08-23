"""对象存储公开契约(STO-01/05: 类型与协议, 业务模块唯一依赖)。

- 业务模块只消费不可变值对象 ``ObjectHandle`` / ``ObjectOwner`` 与协议
  ``ObjectStore``, 不导入 ORM、不拼接路径;
- 对象 ID 可以是数据库主键(int)或内容寻址 oid(str), 由实现统一解析;
- owner namespace 是调用方声明的稳定标识, 存储只判断"是否有引用",
  不导入任何业务模型(STO-05)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias

from iesplan.core.errors import AppError, ConflictError


ObjectId: TypeAlias = int | str


@dataclass(frozen=True, slots=True)
class ObjectHandle:
    """公开对象句柄(不可变值对象, 不暴露 ORM, 0.4.0 收窄)。

    0.4.0: 移除适配器/缓存字段 storage_path/ref_count —— 存储路径属 §11
    敏感信息, ref_count 是可重建缓存(§10.3), 均不得进入业务层公开对象;
    引用状态经 object_info/list_refs 等公开门面查询。
    保留字段即持久化公开视图(sha256/size_bytes/media_type/status/created_at),
    业务模块可直接消费, 无需接触 ORM。
    """

    id: int
    oid: str
    sha256: str
    size_bytes: int
    media_type: str | None
    status: str
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class ObjectOwner:
    """对象引用方标识(公开协议; namespace 由调用方声明, 存储不解析语义)。

    例如 ObjectOwner(namespace="project", id=42, purpose="draft_content")。
    存储只以 (namespace, id) 唯一判断引用是否存在, 不检查业务表。
    """

    namespace: str
    id: int
    purpose: str | None = None


@dataclass(frozen=True, slots=True)
class RefInfo:
    """对象引用公开视图(不可变值对象; ref_entity_type 为稳定标识字符串)。"""

    id: int
    object_id: int
    ref_type: str
    ref_entity_type: str
    ref_entity_id: str
    purpose: str | None = None
    created_at: str | None = None


class ObjectStore(Protocol):
    """对象存储公开门面(最小公开协议, 10.3)。

    - put: 写入字节 → 句柄(内容去重, 同内容复用记录);
    - get: 按 ID 读取字节(读取时校验大小 + sha256, 损坏抛 ObjectCorruptError);
    - stat: 元数据视图(不含内容);
    - attach/detach: 建立/解除 owner 引用(引用清单为唯一权威, STO-02);
    - list_owners: 列出对象全部引用(调试/审计)。
    """

    def put(self, content: bytes, media_type: str) -> ObjectHandle: ...

    def get(self, object_id: ObjectId) -> bytes: ...

    def stat(self, object_id: ObjectId) -> ObjectHandle: ...

    def attach(self, object_id: ObjectId, owner: ObjectOwner) -> None: ...

    def detach(self, object_id: ObjectId, owner: ObjectOwner) -> None: ...

    def list_owners(self, object_id: ObjectId) -> list[ObjectOwner]: ...


class BlobStore(Protocol):
    """字节存储适配器(10.7: 文件系统 / S3 等 provider 接口)。

    实现只负责字节的可靠存取与完整性报告, 不理解业务引用与生命周期。
    - put_blob: 完整字节原子提交, 返回 (storage_path, size);
    - get_blob: 按 storage_path 读取字节(缺失抛 BlobMissingError);
    - delete_blob: 删除字节(不存在幂等);
    - reconcile: 扫描磁盘孤儿(有文件无记录)与缺失(有记录无文件), 幂等。
    """

    def put_blob(self, content: bytes) -> tuple[str, int]: ...

    def get_blob(self, storage_path: str) -> bytes: ...

    def delete_blob(self, storage_path: str) -> None: ...

    def exists(self, storage_path: str) -> bool: ...


class BlobMissingError(AppError):
    """适配器层: 字节缺失(记录存在但文件不存在, STO-04 损坏类别)。"""

    code = "OBJ-CORRUPT-001"
    message_key = "ies.diag.obj.corrupt"


class ObjectCorruptError(AppError):
    """对象内容与记录不一致(大小或 sha256 不匹配, 或文件缺失)。"""

    code = "OBJ-CORRUPT-001"
    message_key = "ies.diag.obj.corrupt"
    http_status = 500


class StorageQuotaError(ConflictError):
    """磁盘剩余空间低于安全阈值(或容量不可测), 拒绝写入(STO-06)。"""

    code = "SYS-STORE-003"
    message_key = "ies.error.storage_quota"


class ObjectQuotaError(ConflictError):
    """对象配额(quota_bytes)超限, 拒绝写入(01 §10.1)。"""

    code = "SYS-STORE-005"
    message_key = "ies.error.object_quota_exceeded"


class ReferenceNotFoundError(AppError):
    """引用不存在(remove_ref/detach 时; 业务代码可捕获并视为已解绑)。"""

    code = "OBJ-REF-001"
    message_key = "ies.diag.obj.ref_not_found"
    http_status = 404


class ObjectNotPendingDeletionError(AppError):
    """对象不在"待物理回收"状态, 不能执行 undelete(0.2.0-B3 恢复路径)。"""

    code = "OBJ-RESTORE-001"
    message_key = "ies.diag.obj.not_pending_deletion"
    http_status = 409
