"""用户公开命名空间分配与用户模型（任务书 §一）。

- 使用 CSPRNG 生成 12 位小写 Crockford Base32，全局唯一，碰撞重试；
- 首次需要时分配，终身不变、不可转让、不复用；
- 不包含用户名/显示名/邮箱/数据库 ID 等个人信息；
- 账号改名/停用/重新启用不改变命名空间；
- 客户端只能提交 slug，不能提交或覆盖 public_namespace。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from iesplan.core.namespace import generate_namespace, is_valid_namespace


def ensure_public_namespace(db: Session, user) -> str:
    """确保用户已分配 public_namespace（首次需要时分配，之后只读）。

    - 若 user.public_namespace 已存在且合法则直接返回；
    - 否则在独立 SAVEPOINT 内处理唯一约束碰撞重试；
    - 严禁全局 rollback 调用方会话，确保调用方在会话内的既有改动不被冲掉；
    - 非约束冲突异常正常上浮。
    """
    if getattr(user, "public_namespace", None):
        ns = str(user.public_namespace)
        if is_valid_namespace(ns):
            return ns

    user_id = getattr(user, "id", None)
    if user_id is None:
        raise ValueError("无法为缺少 id 的用户分配命名空间")

    from iesplan.identity.persistence import User

    user_row = user if isinstance(user, User) else db.get(User, user_id)
    if user_row is None:
        raise ValueError(f"用户不存在: id={user_id}")

    if user_row.public_namespace and is_valid_namespace(user_row.public_namespace):
        if hasattr(user, "public_namespace") and not isinstance(user, User):
            try:
                user.public_namespace = user_row.public_namespace
            except (AttributeError, TypeError):
                pass
        return user_row.public_namespace

    for _ in range(20):
        ns = generate_namespace()
        try:
            with db.begin_nested():
                user_row.public_namespace = ns
                db.flush()
            if hasattr(user, "public_namespace") and not isinstance(user, User):
                try:
                    user.public_namespace = ns
                except (AttributeError, TypeError):
                    pass
            return ns
        except sa.exc.IntegrityError:
            # 仅处理 public_namespace 唯一约束冲突；SAVEPOINT 已自动回滚本次改动，调用方既有工作完好
            continue
    raise RuntimeError("无法分配唯一的 public_namespace（多次碰撞）")
