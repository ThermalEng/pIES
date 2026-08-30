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

    - 若 user.public_namespace 已存在则直接返回；
    - 否则 CSPRNG 生成 12 位标识，碰撞重试，全局唯一；
    - 分配后立即 flush，调用方负责 commit/rollback。
    """
    if getattr(user, "public_namespace", None):
        ns = str(user.public_namespace)
        if is_valid_namespace(ns):
            return ns
    # 需要分配
    for _ in range(20):
        ns = generate_namespace()
        # 尝试写入并检查唯一冲突
        user.public_namespace = ns
        try:
            db.flush()
            return ns
        except Exception:
            db.rollback()
            # 重新读取用户行（避免 detached）
            db.refresh(user)
            if getattr(user, "public_namespace", None):
                return str(user.public_namespace)
            continue
    raise RuntimeError("无法分配唯一的 public_namespace（多次碰撞）")


def get_or_allocate_namespace(db: Session, user_id: int) -> str:
    """按用户 ID 分配/读取命名空间（供离线迁移或后台任务使用）。"""
    from iesplan.models.identity import User

    user = db.get(User, user_id)
    if user is None:
        raise ValueError(f"用户 {user_id} 不存在")
    return ensure_public_namespace(db, user)
