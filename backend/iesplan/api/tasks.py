"""任务 API 路由(U08, prefix /api/projects/{project_id}/tasks)。

路由清单:
- POST   /api/projects/{id}/tasks               提交任务(方案评价/规划/不确定性/检查;
                                                幂等键命中或同快照去重复用 → 200)
- GET    /api/projects/{id}/tasks               任务列表(状态/结局/进度摘要, 游标分页)
- GET    /api/projects/{id}/tasks/{task_id}     任务详情(进度/尝试/租约/诊断/快照)
- POST   /api/projects/{id}/tasks/{task_id}/cancel   取消(传播批量子任务)
- POST   /api/projects/{id}/tasks/{task_id}/retry    手动重试(复用同一快照)

认证与权限: 统一使用 U01 身份单元提供的窗口会话认证(iesplan.api.auth.CurrentUser);
提交/取消/重试要求项目 edit 能力, 列表/详情要求 view 能力。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from iesplan.api.auth import CurrentUser
from iesplan.db import get_db
from iesplan.models.common import IDEMPOTENCY_KEY_RE
from iesplan.services import project as project_service
from iesplan.services import tasks as tasks_service

router = APIRouter(prefix="/api/projects/{project_id}/tasks", tags=["tasks"])


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class TaskCreateRequest(BaseModel):
    """提交任务请求体(01 §7.2)。

    task_type: calc(方案评价)/optimization(规划)/uncertainty(不确定性)/
        analysis(批量分析)/report(结果检查);
    config: 任务级参数(存储估算、快照内容哈希; 如 horizon_years/n_samples/priority/deadline);
    idempotency_key: 客户端重试去重(格式 ^[A-Za-z0-9._:-]{1,128}$, 规格 2.2);
    parent_task_id: 批量父任务(uncertainty 样本子任务, 规格 5.4)。
    """

    task_type: Literal["calc", "optimization", "uncertainty", "analysis", "report"]
    config: dict[str, Any] | None = Field(default=None, description="任务级参数")
    idempotency_key: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=IDEMPOTENCY_KEY_RE,
        description="幂等键(命中返回既有任务)",
    )
    parent_task_id: int | None = Field(default=None, description="批量父任务 id")


class CancelRequest(BaseModel):
    """取消请求体(原因可选, 缺省 user_cancel)。"""

    reason: str | None = Field(default=None, max_length=200)


class RetryRequest(BaseModel):
    """重试请求体(当前无必填字段, 保留扩展位)。"""

    note: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.post("", status_code=201, summary="提交任务")
def create_task_endpoint(
    project_id: int,
    payload: TaskCreateRequest,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict[str, Any]:
    """提交任务(规格 2.2): 幂等键命中或同快照去重复用返回既有任务并附标记。

    幂等/去重复用 → 200(附 replayed/duplicate 标记与提示); 新建 → 201。
    """
    task = tasks_service.create_task(
        db, user, project_id, payload.task_type,
        config=payload.config,
        idempotency_key=payload.idempotency_key,
        parent_task_id=payload.parent_task_id,
    )
    replayed = bool(getattr(task, "replay", False))
    duplicate = bool(getattr(task, "duplicate", False))
    db.commit()
    if replayed or duplicate:
        response.status_code = 200
    return {
        "task": tasks_service.task_summary(db, task),
        "replayed": replayed,
        "duplicate": duplicate,
        "hint": "已复用既有任务(输入相同, 未重复计算)" if replayed or duplicate else None,
    }


@router.get("", summary="任务列表")
def list_tasks_endpoint(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
    task_type: str | None = Query(default=None, alias="type"),
    status: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    cursor: int | None = Query(default=None, description="上一页最后一条任务 id"),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """任务列表(规格 9.1): 状态/结局过滤 + 游标分页, 含进度摘要与排队位次。"""
    return tasks_service.list_tasks(
        db, user, project_id,
        task_type=task_type, status=status, outcome=outcome, cursor=cursor, limit=limit,
    )


@router.get("/{task_id}", summary="任务详情")
def get_task_endpoint(
    project_id: int,
    task_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict[str, Any]:
    """任务详情(规格 9.2): 状态/结局 + 尝试历史 + 当前租约(不含 token) + 进度 +
    诊断 + 快照摘要 + 批量关系。"""
    return {"task": tasks_service.task_detail(db, user, project_id, task_id)}


@router.post("/{task_id}/cancel", summary="取消任务")
def cancel_task_endpoint(
    project_id: int,
    task_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
    payload: CancelRequest | None = None,
) -> dict[str, Any]:
    """取消任务(规格 6.1): queued 直接取消; running → cancelling 并传播批量子任务;
    终态 → 409(ies.diag.task.cancel_denied)。取消为写操作, 要求项目 edit 能力(H-05)。"""
    project_service.ensure_access(db, user, project_id, "edit")
    tasks_service.ensure_task_belongs(db, project_id, task_id)
    reason = payload.reason if payload and payload.reason else "user_cancel"
    task = tasks_service.cancel_task(db, task_id, reason=reason, actor_id=user.id)
    db.commit()
    return {
        "task": tasks_service.task_summary(db, task),
        "cancel_status": task.status,
        "diagnostic": "cancel_ok",
    }


@router.post("/{task_id}/retry", summary="手动重试任务")
def retry_task_endpoint(
    project_id: int,
    task_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
    payload: RetryRequest | None = None,
) -> dict[str, Any]:
    """手动重试(规格 6.4): 仅终态任务; 复用同一 calc_snapshot_id(输入含义不变);
    计算类快照缺失 → 409(TASK-DATA-001)。重试为写操作, 要求项目 edit 能力(H-05)。"""
    project_service.ensure_access(db, user, project_id, "edit")
    tasks_service.ensure_task_belongs(db, project_id, task_id)
    task = tasks_service.retry_task(db, user, task_id)
    db.commit()
    return {"task": tasks_service.task_summary(db, task)}
