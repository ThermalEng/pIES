"""项目校验 API(U07, prefix /api/projects/{project_id}/validation)。

路由清单:
- POST  /api/projects/{project_id}/validation/run            执行完整预检(模型/数据/配置/财务基准确认/就绪),
                                                             返回 ValidationReport 并持久化最近报告
- POST  /api/projects/{project_id}/validation/baseline-confirm  记录财务基准确认({assumptions: dict},
                                                             确认人/时间/内容校验)
- GET   /api/projects/{project_id}/validation                最近一次校验报告(未持久化时现场执行)

认证说明: 与 iesplan.api.projects 一致, 本阶段以 X-User-Id 请求头模拟认证主体
(集成时以 iesplan.api.auth 的窗口会话凭证校验依赖替换)。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from iesplan.core.diagnostics import SEVERITY_BLOCKING
from iesplan.core.errors import AppError
from iesplan.db import get_db
from iesplan.models.identity import User
from iesplan.services import project as project_service
from iesplan.services import validation as validation_service

#: FastAPI 路由(挂载前缀 /api/projects/{project_id}/validation, 由集成阶段追加)
router = APIRouter(prefix="/api/projects/{project_id}/validation", tags=["validation"])

#: FastAPI 依赖注入的数据库会话
DbSession = Annotated[Session, Depends(get_db)]


def _http_error(status: int, code: str, message_key: str, params: dict[str, Any]) -> AppError:
    """构造带指定 HTTP 状态码的应用错误(状态码在错误实例上设置)。"""
    err = AppError("", code=code, severity=SEVERITY_BLOCKING, message_key=message_key, params=params)
    err.http_status = status
    return err


def get_current_user(request: Request, db: DbSession) -> User:
    """当前认证主体(阶段实现: 从 X-User-Id 请求头读取; 正式会话认证由 U01 提供)。

    集成时以 iesplan.api.auth 的窗口会话凭证校验依赖替换本函数。
    """
    raw = request.headers.get("X-User-Id")
    if not raw:
        raise _http_error(401, "AUTH-REQ-001", "ies.diag.perm.denied", {"reason": "missing_identity"})
    try:
        user_id = int(raw)
    except ValueError as exc:
        raise _http_error(401, "AUTH-REQ-001", "ies.diag.perm.denied", {"reason": "bad_identity"}) from exc
    user = db.get(User, user_id)
    if user is None:
        raise _http_error(401, "AUTH-REQ-001", "ies.diag.perm.denied", {"reason": "unknown_user"})
    return user


#: 当前用户依赖(须在 get_current_user 定义之后声明)
CurrentUser = Annotated[User, Depends(get_current_user)]


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