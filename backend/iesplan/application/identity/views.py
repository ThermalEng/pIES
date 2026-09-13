"""身份展示 read model(application/identity.views)。

UserView: 不可变的身份展示视图, 一次包含 id、username、display_name、
role、status、force_password_change、credential_version、last_login_at。
由 application 身份用例组装后交 API 做纯 DTO 映射(api/auth._user_out
只做 view→Pydantic 字段拷贝, 不接收 Session、不查询)。

组装只经 identity 域公开门面:
- 单用户(user_view): user_roles + 有效密码凭证各一次单发查询;
- 批量(user_views): 一次公开调用内完成全员组装, API 不再逐用户回调
  用例(消除 API 层 N+1 扇出)。角色/凭证取数仍经域单发原语(域批量
  read model 缺失, 后续补足后只需替换本模块内部, API 与用例签名不变)。

主角色规则与旧 api/auth._user_out 一致: admin 优先, 否则取首个,
无角色为空串。last_login_at 为域记录原样透传(ISO 串或 None),
由 API 侧 Pydantic 按既有路径 coerc 为 datetime(响应形状不变)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from iesplan.application.identity.service import (
    get_active_password_credential,
    user_roles,
)
from iesplan.identity.contracts import UserRecord

__all__ = [
    "UserView",
    "primary_role",
    "user_view",
    "user_views",
]


@dataclass(frozen=True, slots=True)
class UserView:
    """身份展示视图(不可变 read model, 与 HTTP 无关)。"""

    id: int
    username: str
    display_name: str
    role: str
    status: str
    force_password_change: bool
    credential_version: int
    last_login_at: str | None = None


def primary_role(roles: Sequence[str]) -> str:
    """主角色: admin 优先, 否则取首个, 无角色为空串。"""
    if "admin" in roles:
        return "admin"
    return roles[0] if roles else ""


def user_view(db: Session, user: UserRecord) -> UserView:
    """组装单用户展示视图(角色 + 有效密码凭证改密状态各一次查询)。"""
    roles = user_roles(db, user)
    cred = get_active_password_credential(db, user)
    return UserView(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=primary_role(roles),
        status=user.status,
        force_password_change=bool(cred is not None and cred.requires_change),
        credential_version=user.credential_version,
        last_login_at=user.last_login_at,
    )


def user_views(db: Session, users: Sequence[UserRecord]) -> list[UserView]:
    """批量组装展示视图: 一次公开调用完成全员组装(API 禁逐用户回调用例)。

    取数仍经域单发原语逐用户组装(域批量 read model 缺失时的过渡形态,
    补足后替换本函数内部即可); 边界上恰为一次调用, 无响应组装交错。
    """
    return [user_view(db, user) for user in users]
