"""Worker 边界用例包(application/worker): 薄 re-export, 无实现。

本包收拢 Worker 对 services/storage 的直接调用, worker 层只经本包推进;
依赖方向: worker → application → (services/storage)。

子模块:
- runner_cases: 快照输入读取转调(版本内容/数据集字节/CSV 解析);
- lease_cases: 领取/进度/完成/失败/槽释放/队列视图/证据存取转调。
"""

from __future__ import annotations

from iesplan.application.worker import lease_cases, runner_cases
from iesplan.application.worker.lease_cases import (
    LEASE_TTL_SECONDS,
    Claim,
    TaskStateError,
    acquire_task,
    attach_result_ref,
    clear_cancel_signal,
    complete_task,
    dequeue_task,
    fail_task,
    publish_heartbeat,
    record_task_progress,
    release_slot,
    store_result_blob,
)
from iesplan.application.worker.runner_cases import (
    load_dataset_blob,
    load_version_content,
    parse_dataset_csv,
)

__all__ = [
    "Claim",
    "LEASE_TTL_SECONDS",
    "TaskStateError",
    "acquire_task",
    "attach_result_ref",
    "clear_cancel_signal",
    "complete_task",
    "dequeue_task",
    "fail_task",
    "lease_cases",
    "load_dataset_blob",
    "load_version_content",
    "parse_dataset_csv",
    "publish_heartbeat",
    "record_task_progress",
    "release_slot",
    "runner_cases",
    "store_result_blob",
]

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
