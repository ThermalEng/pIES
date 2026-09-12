"""项目域项目自身事实（项目行、所有者角色、所有者能力集，归属 project）。

收敛自 ``services.project``（纠偏 Wave 1 切片 D），语义与旧服务一致。

跨领域组合授权（管理员判定经 identity 域门面组合）已上收至
``application.projects.authorization``（Wave 2 切片 B）；本模块只保留
项目自身事实，不导入 identity 域。

外部经 ``iesplan.project`` 门面消费；不得导入本域 repository 实现、
``iesplan.models`` 或 services。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from iesplan.core.diagnostics import SEVERITY_ERROR
from iesplan.core.errors import AppError
from iesplan.identity.contracts import UserRecord
from iesplan.project import persistence

# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class InvalidRequestError(AppError):
    """请求/草稿命令校验失败(HTTP 400)。

    code 为项目域内稳定标识(前端按 message_key 渲染文案，见 contracts §成功与错误)。
    """

    code = "PROJ-CMD-001"
    http_status = 400
    severity = SEVERITY_ERROR
    message_key = "ies.diag.param.invalid"


# ---------------------------------------------------------------------------
# 访问控制
# ---------------------------------------------------------------------------

#: 所有者能力集(项目权限以 projects.owner_id 为唯一权威；共享走
#: “导出项目包 → 他人导入”流程，包内不携带账号权限，
#: 见 domain-model §项目聚合/§对象生命周期)。
OWNER_CAPABILITIES: frozenset[str] = frozenset(
    {"view", "edit", "manage_lifecycle", "export_package", "export_excel"}
)


def get_role(db: Session, user: UserRecord, project_id: int) -> str | None:
    """返回用户在项目中的角色: 'owner'(项目所有者) | None(非所有者)。

    项目权限以 owner_id 为唯一权威，非所有者无项目访问能力（管理员除外，
    见 application.projects.authorization.ensure_access；依据 domain-model
    §身份、权限和审计、架构宪法 §16）。
    """
    project = persistence.get_project(db, project_id)
    if project is None:
        return None
    return "owner" if project.owner_id == user.id else None


__all__ = [
    "InvalidRequestError",
    "OWNER_CAPABILITIES",
    "get_role",
]
