"""结果 API 路由(U09/U12/U14, prefix /api/projects/{project_id}/tasks/{task_id}/result)。

路由清单:
- GET    /api/projects/{id}/tasks/{task_id}/result         结果视图(四维结论/业务结局/
                                                           指标摘要/逐时引用/当前选中)
- GET    /api/projects/{id}/tasks/{task_id}/result/assessments   评估历史(不可变, 追加式)
- POST   /api/projects/{id}/tasks/{task_id}/result/assess  触发新评估(每次创建新记录)
- POST   /api/projects/{id}/tasks/{task_id}/result/select  选择结果
- GET    /api/projects/{id}/tasks/{task_id}/result/diff    选中结果的参数差异预览
- GET    /api/projects/{id}/tasks/{task_id}/result/hourly  逐时结果查询(对象存储, 分页)
- POST   /api/projects/{id}/tasks/{task_id}/result/check   对已有证据包创建检查任务

认证与权限: 统一使用 U01 身份单元提供的窗口会话认证(iesplan.api.auth.CurrentUser);
读接口要求项目 view, 写接口(assess/select/check)要求项目 edit。结果应用
(apply_result)由项目单元处理, 本路由只提供数据与差异补丁。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from iesplan.api.auth import CurrentUser
from iesplan.application.results import endpoint_cases as results_app
from iesplan.db import get_db

router = APIRouter(
    prefix="/api/projects/{project_id}/tasks/{task_id}/result", tags=["results"]
)


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class AssessRequest(BaseModel):
    """触发评估请求体(缺省 full=四维全查; 单维=只查该维, 其余记 unknown)。"""

    assessment_type: Literal["full", "physical", "optimality", "financial", "reliability"] = "full"


class SelectRequest(BaseModel):
    """选择结果请求体(01 §8.4)。"""

    solution_id: int = Field(ge=0, description="所选解标识(证据候选索引)")
    selection_type: Literal["adopt", "reference"] = "adopt"
    reference_rule: str | None = Field(default=None, max_length=200, description="参考规则(基准/边界等)")
    reason: str | None = Field(default=None, max_length=2000, description="选择理由")


class CheckRequest(BaseModel):
    """检查任务请求体(evidence_package_id 缺省取任务最新证据包)。"""

    evidence_package_id: int | None = Field(default=None, description="要检查的证据包 id")


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.get("", summary="结果视图")
def get_result_endpoint(
    project_id: int,
    task_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict[str, Any]:
    """结果视图: 四维结论(细粒度 + 派生摘要)/业务结局/指标摘要/逐时结果引用/当前选中。

    展示只读聚合, 不重新计算(domain-model §快照任务结果)。
    """
    return {"result": results_app.result_view(db, user, project_id, task_id)}


@router.get("/assessments", summary="评估历史(不可变)")
def list_assessments_endpoint(
    project_id: int,
    task_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict[str, Any]:
    """评估历史: 全部证据包上的评估记录(追加式不可变, 时间倒序)。"""
    return results_app.list_assessments_case(db, user, project_id, task_id)


@router.post("/assess", status_code=201, summary="触发新评估")
def assess_endpoint(
    project_id: int,
    task_id: int,
    payload: AssessRequest,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict[str, Any]:
    """触发新评估(domain-model §快照任务结果/§对象生命周期): 对任务最新证据包执行四维(或单维)检查, 创建新评估记录
    不覆盖历史; 随后更新结果索引的最新引用(同证据包只挂接指针)。"""
    assessment = results_app.assess_task_evidence(
        db, user, project_id, task_id, payload.assessment_type
    )
    return {"assessment": results_app.assessment_to_dict(db, assessment)}


@router.post("/select", status_code=201, summary="选择结果")
def select_result_endpoint(
    project_id: int,
    task_id: int,
    payload: SelectRequest,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict[str, Any]:
    """选择结果(01 §8.4 追加式): 保存所选解标识/类型/理由 + 差异补丁审计;
    换选=新行 + 旧行 is_current=false。

    A3 越权防御: 入口先 ensure_task_belongs, 对齐 diff/hourly 端点 —— URL
    project_id 与任务真实归属不一致时 404, 防止把选中写入非 URL 项目或读取
    他项目差异(selection_diff 以 URL project_id 读取当前选中)。
    """
    selection, diff = results_app.select_task_result(
        db, user, project_id, task_id, payload.solution_id, payload.selection_type,
        reference_rule=payload.reference_rule, reason=payload.reason,
    )
    return {
        "selection": {
            "id": selection.id,
            "project_id": selection.project_id,
            "result_index_id": selection.result_index_id,
            "selected_by": selection.selected_by,
            "selected_at": selection.selected_at,
            "reason": selection.reason,
            "is_current": selection.is_current,
        },
        "diff": diff,
    }


@router.get("/diff", summary="选中结果参数差异预览")
def diff_endpoint(
    project_id: int,
    task_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict[str, Any]:
    """选中结果的参数差异预览(补丁 + 校验值 + 来源版本), 应用前要求用户确认
    (REQ-RESULT-003); 无选中 → 404。"""
    diff = results_app.get_selection_diff(db, user, project_id, task_id)
    return {"diff": diff}


@router.get("/hourly", summary="逐时结果查询(分页)")
def hourly_endpoint(
    project_id: int,
    task_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
    field: str = Query(description="逐时字段名(如 p_grid_buy)"),
    solution_id: int | None = Query(default=None, description="逐时结果引用所属解(缺省第一份)"),
    start: int = Query(default=0, ge=0, description="起始行号(含)"),
    end: int | None = Query(default=None, ge=0, description="结束行号(不含, 缺省到末尾)"),
    limit: int = Query(default=5000, ge=1, le=50000, description="每页行数"),
) -> dict[str, Any]:
    """逐时结果查询(REQ-RESULT-002): 从对象存储读取, 行号分页
    返回 values + next_start 供翻页。任务尚无证据包 → 404(不再以空内容查询)。"""
    return results_app.read_task_hourly(
        db, user, project_id, task_id, field,
        start=start, end=end, limit=limit, solution_id=solution_id,
    )


@router.post("/check", status_code=201, summary="创建检查任务")
def check_task_endpoint(
    project_id: int,
    task_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
    payload: CheckRequest | None = None,
) -> dict[str, Any]:
    """对已有证据包创建检查任务(report 类型, io 池); Worker 消费后执行四维复查。"""
    package_id = payload.evidence_package_id if payload else None
    return results_app.create_check_task_case(db, user, project_id, task_id, package_id)
