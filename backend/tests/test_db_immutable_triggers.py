"""不可变审计触发器部署测试(0.2.0 B4)。

覆盖:
- SQLite 测试库下 ``db._deploy_immutable_triggers`` 直接跳过, 不解析 plpgsql
  语法、不报错(生产 Postgres 才执行);
- 部署 DDL 与不可变表清单完全一致: 每张表都有函数 + BEFORE UPDATE/DELETE
  触发器 + REVOKE(DDL 生成正确性, 供 Postgres 部署前静态校验);
- ``init_db`` 在 SQLite 下仍可安全执行(含触发器部署早退)。

依据: ARCHITECTURE_CONSTITUTION.md §11 数据库与持久化 / §16 安全与审计（三道防线：应用写入单元、REVOKE、触发器）。
"""

from __future__ import annotations

import os

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan.models.immutable_triggers import (  # noqa: E402
    ALL_IMMUTABLE_REVOKE_DDL,
    ALL_IMMUTABLE_TRIGGER_DDL,
    IMMUTABLE_TABLES,
)

#: 每张不可变表在 ALL_IMMUTABLE_TRIGGER_DDL 中的必需片段
_REQUIRED_FRAGMENTS = (
    lambda t: f"CREATE FUNCTION tg_{t}_immutable() RETURNS trigger",
    lambda t: f"CREATE TRIGGER tg_{t}_no_update BEFORE UPDATE ON {t}",
    lambda t: f"CREATE TRIGGER tg_{t}_no_delete BEFORE DELETE ON {t}",
    lambda t: f"REVOKE UPDATE, DELETE ON {t} FROM PUBLIC;",
)


def test_immutable_trigger_ddl_covers_all_tables() -> None:
    """部署 DDL 覆盖不可变表清单中每一张表(函数/两触发器/REVOKE)。"""
    for table in IMMUTABLE_TABLES:
        for fragment in _REQUIRED_FRAGMENTS:
            assert fragment(table) in ALL_IMMUTABLE_TRIGGER_DDL + ALL_IMMUTABLE_REVOKE_DDL, (
                f"{table} 缺少部署片段: {fragment(table)}"
            )


def test_deploy_skipped_on_sqlite() -> None:
    """SQLite 测试库下部署函数直接跳过, 不解析 plpgsql 语法、不报错。"""
    import iesplan.db as db_mod

    assert db_mod.settings.db_url.startswith("sqlite"), "测试环境必须为 SQLite"
    # 幂等: 重复调用不应抛错(内部方言判定早退)
    db_mod._deploy_immutable_triggers()
    db_mod._deploy_immutable_triggers()


def test_init_db_on_sqlite_is_safe() -> None:
    """init_db 在 SQLite 下可安全执行(含触发器部署早退), 不解析 Postgres 语法。"""
    import iesplan.db as db_mod

    # 用 StaticPool 共享连接的 SQLite 内存库替换模块级引擎, 避免每连接独立
    # 内存库导致 seed_admin 读到空库(生产 Postgres 无此问题); 测试后恢复原引擎。
    saved_engine, saved_factory = db_mod.engine, db_mod.SessionLocal
    try:
        eng = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        db_mod.engine = eng
        db_mod.SessionLocal = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
        # 幂等: 重复调用不应抛异常(SQLite 下触发器部署早退)
        db_mod.init_db()
        db_mod.init_db()
    finally:
        db_mod.engine = saved_engine
        db_mod.SessionLocal = saved_factory
