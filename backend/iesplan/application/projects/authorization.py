"""项目授权用例(application/projects/authorization.py)。

跨领域授权缝合契约（Wave 2 切片 A 先行最小实现；切片 2B 落地后以其
实现为准，模块名/函数名/签名/语义保持一致）：
project 只回答项目自身事实（get_project/get_role/OWNER_CAPABILITIES），
identity 只回答身份/角色事实（user_roles），本模块组合两者判定访问。

API 路由只调本契约，不再直调 ``project.ensure_access`` 自组织授权。
签名与语义与 ``project.ensure_access`` 完全一致：
``(db, user, project_id, *capabilities)``。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from iesplan import identity as identity_domain
from iesplan import project as project_domain
from iesplan.core.errors import ForbiddenError, NotFoundError
from iesplan.identity.contracts import UserRecord


def ensure_access(db: Session, user: UserRecord, project_id: int, *capabilities: str) -> None:
    """访问判定(架构宪法 §16、domain-model §身份、权限和审计)：
    用户必须同时具备全部请求能力，否则 ForbiddenError。

    - 仅项目所有者具备全部业务能力;
    - 管理员(全局 admin 角色)始终可查看项目细节与管理生命周期(删除/归档),
      不得业务编辑;
    - 项目不存在或已删除一律按 NotFoundError(不泄露项目存在性细节)。
    """
    project = project_domain.get_project(db, project_id)
    if project is None:
        raise NotFoundError(
            "项目不存在",
            params={"project_id": project_id},
            location={"object_type": "project", "object_id": project_id},
        )
    granted = (
        set(project_domain.OWNER_CAPABILITIES)
        if project_domain.get_role(db, user, project_id) == "owner"
        else set()
    )
    if "admin" in identity_domain.user_roles(db, user.id):
        # 管理员始终可管理项目整体生命周期(删除/归档), 无需授权
        granted |= {"view", "manage_lifecycle"}
    missing = [cap for cap in capabilities if cap not in granted]
    if missing:
        raise ForbiddenError(
            "缺少所需项目权限",
            params={"required": list(capabilities), "missing": missing, "project_id": project_id},
            location={"object_type": "project", "object_id": project_id},
        )


__all__ = ["ensure_access"]
