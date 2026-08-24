"""数据库引擎、会话管理与初始化。

- engine / SessionLocal: 全局单例(连接池 + 预检)。
- get_db(): FastAPI 请求级依赖。
- init_db(): 幂等建表(create_all) + 种子管理员。
- seed_admin(): 幂等创建内置管理员(首登强制改密)。
"""

from __future__ import annotations

from collections.abc import Generator

import sqlalchemy as sa
from sqlalchemy import create_engine, select
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
    """幂等初始化数据库: 建表 + 约束迁移 + 不可变触发器 + 种子管理员。

    - 先导入模型模块, 确保全部表注册到 Base.metadata;
    - create_all 只建不存在的表, 重复调用无副作用;
    - _migrate_constraints: 既有表约束随模型演进做幂等 ALTER
      (如 ck_tasks_type 增补 'analysis', 03 §9.7);
    - _deploy_immutable_triggers: 不可变表(01 §11)部署"禁 UPDATE/DELETE"触发器
      与 REVOKE(仅 PostgreSQL; SQLite 测试库跳过)。
    """
    from iesplan import models  # noqa: F401  (注册全部模型)

    Base.metadata.create_all(bind=engine)
    _migrate_constraints()
    _deploy_immutable_triggers()
    seed_admin()


def _migrate_constraints() -> None:
    """既有表约束幂等迁移(Postgres; SQLite 测试库由 create_all 全量重建)。

    处理两类演进:
    1. 新增枚举取值类约束(如 ck_tasks_type 增补 'analysis', 03 §9.7);
    2. 唯一索引语义变化(RR-P1-05: uq_tasks_idempotency_key 由全局唯一改为
       (project_id, idempotency_key) 复合 —— 幂等键由前端 config+params 哈希
       生成, 跨项目相同, 全局唯一会让另一项目同键提交命中他项目任务)。
    约束完全不存在(旧库手工删过/从未建过)也补建, 不能放任 CHECK 约束缺失。
    """
    if not settings.db_url.startswith("postgresql"):
        return
    with engine.begin() as conn:
        row = conn.execute(
            sa_text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_tasks_type' AND conrelid = 'tasks'::regclass"
            )
        ).first()
        if row is not None and "'analysis'" in row[0]:
            pass  # 已含新值, 无需迁移
        else:
            # 缺失或旧定义: 先删后建(缺失时 DROP IF EXISTS 幂等)
            conn.execute(sa_text("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS ck_tasks_type"))
            conn.execute(
                sa_text(
                    "ALTER TABLE tasks ADD CONSTRAINT ck_tasks_type CHECK "
                    "(type IN ('calc','optimization','uncertainty','analysis','import',"
                    "'export','report','dataset_build'))"
                )
            )
        # RR-P1-05: 幂等键唯一索引改为项目复合(旧全局唯一约束名相同, 先删后建)
        # Postgres 唯一约束由 backing index 实现, 约束的索引不能直接 DROP INDEX
        # (报 "cannot drop index ... because constraint ... requires it"),
        # 必须先用 ALTER TABLE DROP CONSTRAINT(索引随之删除); 若旧库是手工
        # 建的独立索引则再补 DROP INDEX IF EXISTS(幂等)。
        idx = conn.execute(
            sa_text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'uq_tasks_idempotency_key' AND tablename = 'tasks'"
            )
        ).first()
        needs_rebuild = (
            idx is None
            or "project_id" not in idx[0]
            or "idempotency_key" not in idx[0]
        )
        if needs_rebuild:
            conn.execute(sa_text("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS uq_tasks_idempotency_key"))
            conn.execute(sa_text("DROP INDEX IF EXISTS uq_tasks_idempotency_key"))
            conn.execute(
                sa_text(
                    "CREATE UNIQUE INDEX uq_tasks_idempotency_key ON tasks "
                    "(project_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
                )
            )
        # 0.2.0-B3: objects 新增软删/保留期列(pending_deleted_at / pending_delete_until),
        # 旧库 create_all 不会为既有表补列, 这里幂等 ALTER 补列并建到期索引。
        # 0.7.0: 计算快照持久化唯一规范装配产物三件套。旧快照不能伪造回执，
        # 因此升级列保持可空；Worker 对缺失成员执行阻断，新快照始终完整写入。
        _add_column_if_missing(conn, "calc_snapshots", "canonical_assembly_text", "TEXT")
        _add_column_if_missing(conn, "calc_snapshots", "assembly_sha256", "TEXT")
        _add_column_if_missing(conn, "calc_snapshots", "assembly_receipt", "JSONB")
        assembly_hash_check = conn.execute(
            sa_text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = 'ck_calc_snapshots_assembly_sha256'"
            )
        ).first()
        if assembly_hash_check is None:
            conn.execute(
                sa_text(
                    "ALTER TABLE calc_snapshots ADD CONSTRAINT "
                    "ck_calc_snapshots_assembly_sha256 CHECK "
                    "(assembly_sha256 IS NULL OR assembly_sha256 ~ '^[0-9a-f]{64}$')"
                )
            )

        _add_column_if_missing(conn, "objects", "pending_deleted_at", "TIMESTAMPTZ")
        _add_column_if_missing(conn, "objects", "pending_delete_until", "TIMESTAMPTZ")
        conn.execute(
            sa_text(
                "CREATE INDEX IF NOT EXISTS idx_objects_pending_until "
                "ON objects (pending_delete_until)"
            )
        )
        # 既有库补建软删日期 CHECK 约束(新库由 create_all 建, 旧库升级需幂等补):
        # pending 状态必须同时有 de/until 且 until >= deleted_at, 防负保留期等畸形数据。
        exists = conn.execute(
            sa_text(
                "SELECT 1 FROM pg_constraint WHERE conname = 'ck_objects_pending_deletion_dates'"
            )
        ).first()
        if exists is None:
            conn.execute(
                sa_text(
                    "ALTER TABLE objects ADD CONSTRAINT ck_objects_pending_deletion_dates "
                    "CHECK (status <> 'pending_deletion' OR (pending_deleted_at IS NOT NULL "
                    "AND pending_delete_until IS NOT NULL "
                    "AND pending_delete_until >= pending_deleted_at))"
                )
            )


def _add_column_if_missing(
    conn: sa.Connection, table: str, column: str, col_type: str
) -> None:
    """Postgres 幂等补列: 列不存在才 ALTER TABLE ADD COLUMN(IF NOT EXISTS 不支持
    ADD COLUMN, 故先查 information_schema 再执行)。"""
    exists = conn.execute(
        sa_text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).first()
    if exists is None:
        conn.execute(sa_text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))


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
