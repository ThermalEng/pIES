"""项目域访问控制（项目所有者能力判定，归属 project）。

收敛自 ``services.project``（纠偏 Wave 1 切片 D）：访问判定只依赖项目域
公开门面与 identity 域公开门面；管理员判定经 identity 域门面组合，
语义与旧服务一致。

外部经 ``iesplan.project`` 门面消费；不得导入本域 repository 实现、
``iesplan.models`` 或 services。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from iesplan import identity as identity_domain
from iesplan.core.diagnostics import SEVERITY_ERROR
from iesplan.core.errors import AppError, ForbiddenError, NotFoundError
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
    见 ensure_access；依据 domain-model §身份、权限和审计、架构宪法 §16）。
    """
    project = persistence.get_project(db, project_id)
    if project is None:
        return None
    return "owner" if project.owner_id == user.id else None


def _is_admin(db: Session, user: UserRecord) -> bool:
    """用户是否持有全局 admin 角色(经 identity 域公开门面判定，不直接查表)。"""
    return "admin" in identity_domain.user_roles(db, user.id)


def ensure_access(db: Session, user: UserRecord, project_id: int, *capabilities: str) -> None:
    """访问判定(架构宪法 §16、domain-model §身份、权限和审计)：
    用户必须同时具备全部请求能力，否则 ForbiddenError。

    - 仅项目所有者具备全部业务能力;
    - 管理员(全局 admin 角色)始终可查看项目细节与管理生命周期(删除/归档),
      不得业务编辑;
    - 项目不存在或已删除一律按 NotFoundError(不泄露项目存在性细节)。
    """
    project = persistence.get_project(db, project_id)
    if project is None:
        raise NotFoundError(
            "项目不存在",
            params={"project_id": project_id},
            location={"object_type": "project", "object_id": project_id},
        )
    granted = set(OWNER_CAPABILITIES) if get_role(db, user, project_id) == "owner" else set()
    if _is_admin(db, user):
        # 管理员始终可管理项目整体生命周期(删除/归档), 无需授权
        granted |= {"view", "manage_lifecycle"}
    missing = [cap for cap in capabilities if cap not in granted]
    if missing:
        raise ForbiddenError(
            "缺少所需项目权限",
            params={"required": list(capabilities), "missing": missing, "project_id": project_id},
            location={"object_type": "project", "object_id": project_id},
        )


__all__ = [
    "InvalidRequestError",
    "OWNER_CAPABILITIES",
    "ensure_access",
    "get_role",
]
