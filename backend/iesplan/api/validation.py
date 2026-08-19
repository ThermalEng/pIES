"""项目校验 API(U07, prefix /api/projects/{project_id}/validation)。

路由清单:
- POST  /api/projects/{project_id}/validation/run            执行完整预检(模型/数据/配置/财务基准确认/就绪),
                                                             返回 ValidationReport 并持久化最近报告
- POST  /api/projects/{project_id}/validation/baseline-confirm  记录财务基准确认({assumptions: dict},
                                                             确认人/时间/内容校验)
- GET   /api/projects/{project_id}/validation                最近一次校验报告(未持久化时现场执行)

认证说明: 统一使用 U01 身份单元提供的窗口会话认证
(iesplan.api.auth.CurrentUser; 未认证 401, 权限不足 403)。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from iesplan.api.auth import CurrentUser
from iesplan.db import get_db
from iesplan.services import project as project_service
from iesplan.services import validation as validation_service

#: FastAPI 路由(挂载前缀 /api/projects/{project_id}/validation, 由集成阶段追加)
router = APIRouter(prefix="/api/projects/{project_id}/validation", tags=["validation"])

#: FastAPI 依赖注入的数据库会话
DbSession = Annotated[Session, Depends(get_db)]


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class BaselineConfirmRequest(BaseModel):
    """财务基准确认请求体(RPD 10.2: 关键假设, 服务端计算内容完整性校验值)。"""

    assumptions: dict[str, Any] = Field(
        default_factory=dict,
        description="财务基准关键假设内容(经济参数/币种/IRR 下限等)",
    )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.post("/run", summary="执行完整预检")
def run_validation(project_id: int, db: DbSession, user: CurrentUser) -> dict:
    """执行完整预检(REQ-CALC-007 校验门禁): 一次返回全部诊断, 阻断则 blocks_submit=true。

    报告持久化为内容寻址对象(01 §10.2 ref_type='report'), GET /validation 可读取最近报告。
    """
    project_service.ensure_access(db, user, project_id, "view")
    report = validation_service.validate_project(db, project_id)
    stored = validation_service.store_validation_report(db, project_id, report)
    db.commit()
    return {"report": report.to_dict(), "stored": stored}


@router.post("/baseline-confirm", summary="记录财务基准确认")
def baseline_confirm(
    project_id: int,
    body: BaselineConfirmRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """记录财务基准确认(确认人/时间/内容完整性校验, 追加式审计, 不可覆盖)。

    服务端计算假设内容的 sha256 校验值并连同确认人/时间写入审计日志。
    """
    project_service.ensure_access(db, user, project_id, "edit")
    digest = validation_service.hash_assumptions(body.assumptions)
    record = validation_service.mark_baseline_confirmed(
        db, project_id, user, digest, assumptions=body.assumptions
    )
    db.commit()
    return {
        "confirmed": True,
        "assumptions_hash": digest,
        "confirmed_by": (record.after or {}).get("confirmed_by"),
        "confirmed_at": (record.after or {}).get("confirmed_at"),
    }


@router.get("", summary="最近校验报告")
def get_validation_report(project_id: int, db: DbSession, user: CurrentUser) -> dict:
    """最近一次持久化的校验报告; 尚无记录时现场执行并返回(不落库)。"""
    project_service.ensure_access(db, user, project_id, "view")
    stored = validation_service.get_latest_validation_report(db, project_id)
    if stored is not None:
        return {"report": stored, "stored": True}
    report = validation_service.validate_project(db, project_id)
    return {"report": report.to_dict(), "stored": False}