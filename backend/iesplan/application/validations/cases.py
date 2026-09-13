"""校验面向 API 的完整用例(application/validations/cases)。

每个函数对应 `api/validation.py` 的一个 HTTP 业务动作，接收已认证主体
(`user`)、业务参数与事务会话(`db`)，在内部完成授权、业务步骤与事务，
返回与 HTTP 无关的普通字典；路由只做 DTO、一次调用与错误/响应映射。

吸收的原路由顺序(行为、权限、事务与错误语义与原路由逐一一致，不新增
校验/hash/防御分支)：

- 执行完整预检：view 授权 → 只读预检 → 持久化报告(提交)；
- 财务基准确认：edit 授权 → 追加确认审计(提交) → 确认回执整形；
- 最近报告读取：view 授权 → 读最近持久化报告；缺失时现场执行并返回
  (不落库，与原路由一致)。

依赖方向：api → application.validations.cases → {precheck,
application.projects.authorization} → 领域公开门面。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from iesplan.application.projects.authorization import ensure_access
from iesplan.application.validations.precheck import (
    get_latest_validation_report as _get_latest_report,
)
from iesplan.application.validations.precheck import (
    mark_baseline_confirmed as _mark_baseline_confirmed,
)
from iesplan.application.validations.precheck import (
    store_validation_report as _store_report,
)
from iesplan.application.validations.precheck import (
    validate_project as _validate_project,
)
from iesplan.identity.contracts import UserRecord


def run_validation(db: Session, user: UserRecord, project_id: int) -> dict:
    """执行完整预检并持久化(原路由顺序：view 授权 → 预检 → 存报告)。"""
    ensure_access(db, user, project_id, "view")
    report = _validate_project(db, project_id)
    stored = _store_report(db, project_id, report)
    return {"report": report.to_dict(), "stored": stored}


def confirm_baseline(
    db: Session,
    user: UserRecord,
    project_id: int,
    assumptions: dict[str, Any] | None = None,
) -> dict:
    """记录财务基准确认(原路由顺序：edit 授权 → 追加审计 → 回执整形)。"""
    ensure_access(db, user, project_id, "edit")
    record = _mark_baseline_confirmed(db, project_id, user, assumptions=assumptions)
    return {
        "confirmed": True,
        "confirmed_by": (record.after or {}).get("confirmed_by"),
        "confirmed_at": (record.after or {}).get("confirmed_at"),
    }


def get_validation_report(db: Session, user: UserRecord, project_id: int) -> dict:
    """最近校验报告(原路由顺序：view 授权 → 读持久化；缺失则现场执行不落库)。"""
    ensure_access(db, user, project_id, "view")
    stored = _get_latest_report(db, project_id)
    if stored is not None:
        return {"report": stored, "stored": True}
    report = _validate_project(db, project_id)
    return {"report": report.to_dict(), "stored": False}


__all__ = [
    "confirm_baseline",
    "get_validation_report",
    "run_validation",
]
