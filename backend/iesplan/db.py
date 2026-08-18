"""数据库引擎、会话管理与初始化。

- engine / SessionLocal: 全局单例(连接池 + 预检)。
- get_db(): FastAPI 请求级依赖。
- init_db(): 幂等建表(create_all) + 种子管理员。
- seed_admin(): 幂等创建内置管理员(首登强制改密)。
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, select
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from iesplan.config import settings

#: SQLAlchemy 引擎: pool_pre_ping 在取连接时先探活, 避免使用失效连接
engine = create_engine(settings.db_url, pool_pre_ping=True)

#: 会话工厂: autoflush=False(显式控制)、expire_on_commit=False(提交后可继续读取)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """全部 ORM 模型的声明基类。"""


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖: 提供请求级数据库会话, 请求结束自动关闭。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    """幂等初始化数据库: 建表 + 种子管理员。

    - 先导入模型模块, 确保全部表注册到 Base.metadata;
    - create_all 只建不存在的表, 重复调用无副作用。
    """
    from iesplan import models  # noqa: F401  (注册全部模型)

    Base.metadata.create_all(bind=engine)
    seed_admin()


def seed_admin(password: str | None = None) -> None:
    """幂等创建内置管理员(admin)。

    - 若用户表中已存在 admin 角色授权则直接返回;
    - 确保 ``roles`` 中存在 code='admin' 的系统角色;
    - 创建 admin 用户 + password 凭证(requires_change=True, 首登强制改密),
      初始密码取参数, 缺省用 settings.default_admin_password。

    参数:
        password: 初始密码;为 None 时使用配置默认值。
    """
    from iesplan.core.security import check_password_strength, hash_password
    from iesplan.models.identity import Credential, Role, User, UserRole

    with SessionLocal() as session:
        # 已有管理员则跳过(幂等)
        has_admin = session.execute(
            select(User.id)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(Role.code == "admin", UserRole.revoked_at.is_(None))
            .limit(1)
        ).first()
        if has_admin is not None:
            return

        # 确保 admin 系统角色存在
        role = session.execute(select(Role).where(Role.code == "admin")).scalar_one_or_none()
        if role is None:
            role = Role(code="admin", name="管理员", description="系统内置管理员", is_system=True)
            session.add(role)
            session.flush()

        # 创建管理员用户与密码凭证
        pwd = password or settings.default_admin_password
        admin = User(username="admin", display_name="管理员")
        session.add(admin)
        session.flush()
        ok, _ = check_password_strength(pwd)
        session.add(
            Credential(
                user_id=admin.id,
                credential_type="password",
                secret_hash=hash_password(pwd),
                algorithm="bcrypt",
                strength_score=100 if ok else 0,
                requires_change=True,
            )
        )
        # 管理员自授权(种子场景, 授权人即本人)
        session.add(UserRole(user_id=admin.id, role_id=role.id, granted_by=admin.id))
        session.commit()
