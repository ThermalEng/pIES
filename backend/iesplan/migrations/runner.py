"""版本化数据库迁移运行器（独立于 Base.metadata，不污染冷建表契约）。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from iesplan.migrations.v1_2_0_calc_config import MIGRATION_VERSION, upgrade_calc_configs

MIGRATIONS: tuple[tuple[str, Callable[[Connection], int]], ...] = (
    (MIGRATION_VERSION, upgrade_calc_configs),
)


def _ensure_migrations_table(conn: Connection) -> None:
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, "
            "applied_at TEXT NOT NULL"
            ")"
        )
    )


def applied_versions(conn: Connection) -> set[str]:
    """返回已应用的迁移版本号集合。"""
    _ensure_migrations_table(conn)
    rows = conn.execute(text("SELECT version FROM schema_migrations")).fetchall()
    return {str(row[0]) for row in rows}


def _apply_on_connection(conn: Connection) -> list[str]:
    if conn.dialect.name == "postgresql":
        conn.execute(text("SELECT pg_advisory_xact_lock(174962, 2)"))
    done = applied_versions(conn)
    applied_now: list[str] = []
    for version, fn in MIGRATIONS:
        if version in done:
            continue
        fn(conn)
        conn.execute(
            text("INSERT INTO schema_migrations (version, applied_at) VALUES (:v, :at)"),
            {"v": version, "at": datetime.now(UTC).isoformat()},
        )
        applied_now.append(version)
    return applied_now


def apply_migrations(target: Engine | Connection | None = None) -> list[str]:
    """在指定连接或引擎上按顺序幂等执行未应用的版本化迁移。"""
    if target is None:
        from iesplan.db import engine as default_engine

        target = default_engine
    if isinstance(target, Connection):
        return _apply_on_connection(target)
    with target.begin() as conn:
        return _apply_on_connection(conn)


__all__ = [
    "MIGRATIONS",
    "applied_versions",
    "apply_migrations",
]
