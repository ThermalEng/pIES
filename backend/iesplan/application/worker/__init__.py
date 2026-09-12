"""Worker 执行侧薄封装用例(application/worker): 取消信号/结局映射/对象存取。

只做转调, 不新增校验/hash/回退。``executors`` 的领域调用(取消信号读取、
求解器状态映射、逐时/证据对象存取)经由本模块, 执行器只保留任务领取/
派发/进度/状态回写。

依赖方向: worker → application → (services/storage)。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from iesplan.services import queue as queue_service
from iesplan.services import tasks as tasks_service
from iesplan.storage import get_object, put_object


def cancel_requested(task_id: int) -> bool:
    """任务是否存在取消信号(可重建视图)。"""
    return queue_service.get_cancel(task_id) is not None


def map_business_outcome(solver_status: str) -> str:
    """求解器状态 → 业务结局(转调任务域映射)。"""
    return tasks_service.map_business_outcome(solver_status)


def store_worker_object(
    db: Session,
    blob: bytes,
    content_type: str,
    *,
    source_category: str,
    purpose: str,
    actor_id: int | None = None,
) -> int:
    """对象存储写入, 返回对象 id。"""
    return put_object(
        db, blob, content_type, source_category=source_category,
        purpose=purpose, actor_id=actor_id,
    ).id


def load_worker_object(db: Session, object_id: int) -> bytes:
    """对象存储读取。"""
    return get_object(db, object_id)
