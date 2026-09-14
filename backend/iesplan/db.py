"""数据库引擎、会话管理与初始化。

- engine / SessionLocal: 全局单例(连接池 + 预检)。
- get_db(): FastAPI 请求级依赖。
- init_db(): 幂等建表(create_all) + 版本化迁移 + 不可变触发器。
  当前 schema 从空库直接建立, 不保留旧库 ALTER/DROP/回填分支;
  种子身份数据归 application.identity.seed_builtin_admin, 本模块不留业务种子。
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy import text as sa_text
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
    """幂等初始化数据库: 建表 + 版本化迁移 + 不可变触发器。

    - 先导入模型模块, 确保全部表注册到 Base.metadata;
    - create_all 只建不存在的表, 重复调用无副作用;
    - apply_migrations: 在基础 schema 之上执行版本化 schema 迁移(宪法 §11,
      台账幂等)，由各版本明确登记并补充其拥有的当前结构与约束;
    - _deploy_immutable_triggers: 不可变表(01 §11)部署"禁 UPDATE/DELETE"触发器
      与 REVOKE(仅 PostgreSQL; SQLite 测试库跳过)。
    - 旧库 ALTER/DROP/回填分支已删除, 当前 schema 从空库直接建立;
      种子管理员改由 application.identity.seed_builtin_admin 负责。
    """
    from iesplan import models  # noqa: F401  (注册全部模型)
    from iesplan.migrations import apply_migrations

    Base.metadata.create_all(bind=engine)
    apply_migrations(engine)
    _deploy_immutable_triggers()


def _deploy_immutable_triggers() -> None:
    """不可变表(仅 INSERT)部署"禁止 UPDATE/DELETE"触发器 + REVOKE(01 第0节)。

    三道防线第 2/3 层(第 1 层应用层只允许唯一写入单元发 INSERT 由代码约定保证):
    1. ``REVOKE UPDATE, DELETE ON <table> FROM PUBLIC``(不授予任何角色该表
       UPDATE/DELETE);
    2. 每张不可变表创建 ``tg_<table>_immutable()`` 函数 + BEFORE UPDATE|DELETE
       触发器, 触发即 RAISE EXCEPTION。

    幂等: 重复执行前先 ``DROP FUNCTION IF EXISTS ... CASCADE`` 清掉同名函数及其
    关联触发器, 再重建, 与新宪法"关键变更保留不可变审计"一致。

    仅 PostgreSQL 执行; SQLite 测试库不解析 plpgsql 触发器语法, 直接跳过
    (create_all 每次全量重建, 无生产数据, 不可变性由应用层唯一写入单元保证)。
    """
    if not settings.db_url.startswith("postgresql"):
        return
    from iesplan.models.immutable_triggers import (
        ALL_IMMUTABLE_REVOKE_DDL,
        ALL_IMMUTABLE_TRIGGER_DDL,
        IMMUTABLE_TABLES,
        PROJECT_BASELINE_IMMUTABLE_TRIGGER_SQL,
    )

    with engine.begin() as conn:
        for table in IMMUTABLE_TABLES:
            conn.execute(
                sa_text(f"DROP FUNCTION IF EXISTS tg_{table}_immutable() CASCADE")
            )
        for stmt in ALL_IMMUTABLE_TRIGGER_DDL.split("\n\n"):
            if stmt.strip():
                conn.execute(sa_text(stmt))
        for stmt in ALL_IMMUTABLE_REVOKE_DDL.split("\n"):
            if stmt.strip():
                conn.execute(sa_text(stmt))
        # 项目计算基线不可变触发器(0.6.5 事项 1): 先 DROP 再重建保证幂等
        for func in ("tg_projects_baseline_immutable", "tg_project_versions_baseline_immutable"):
            conn.execute(sa_text(f"DROP FUNCTION IF EXISTS {func}() CASCADE"))
        for stmt in PROJECT_BASELINE_IMMUTABLE_TRIGGER_SQL.split("\n\n"):
            if stmt.strip():
                conn.execute(sa_text(stmt))
