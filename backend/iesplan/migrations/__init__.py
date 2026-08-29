"""版本化 schema 迁移运行器(宪法 §11: schema 变化必须通过版本化 migration,
不依赖运行时 create_all 作为发布机制)。

结构:
- ``schema_migrations`` 台账表(version/name/applied_at)由运行器自举创建
  (SQLAlchemy Core 建表, 跨方言可移植);
- ``MIGRATIONS`` 为有序迁移列表, 每项 (version, name, upgrade(conn));
- ``apply_migrations(engine)`` 在事务内应用未执行的迁移并登记台账, 幂等
  (重复调用跳过已应用版本)。

迁移 DDL 按方言分支提供(SQLite 测试 / PostgreSQL 生产), 与 ORM 模型
(models/project_model.py)语义一致; ORM 模型仅作测试基建(create_all)与
运行期读写载体, 发布机制以本版本化迁移为准。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import MetaData, Table
from sqlalchemy import text as sa_text

__all__ = ["apply_migrations", "MIGRATIONS", "MIGRATION_VERSIONS"]


def _build_ledger() -> Table:
    """schema_migrations 台账表(SQLAlchemy Core 定义, 方言可移植)。"""
    return Table(
        "schema_migrations",
        MetaData(),
        sa.Column("version", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )


_LEDGER = _build_ledger()


# ---------------------------------------------------------------------------
# 迁移 0001: 项目模型清单与编号序列表(切片 dm2-A)
# ---------------------------------------------------------------------------

_MIGRATION_0001_POSTGRES = """
CREATE TABLE IF NOT EXISTS project_model_sequences (
    project_id BIGINT PRIMARY KEY REFERENCES projects(id),
    next_suffix BIGINT NOT NULL DEFAULT 1 CHECK (next_suffix >= 1)
);

CREATE TABLE IF NOT EXISTS project_models (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES projects(id),
    suffix BIGINT NOT NULL CHECK (suffix >= 1),
    base_device_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision >= 1),
    content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    model_object_id BIGINT NOT NULL REFERENCES objects(id),
    receipt_object_id BIGINT NOT NULL REFERENCES objects(id),
    source TEXT NOT NULL CHECK (source IN ('direct_yaml','template')),
    template_sha256 TEXT CHECK (template_sha256 IS NULL OR template_sha256 ~ '^[0-9a-f]{64}$'),
    inputs_sha256 TEXT CHECK (inputs_sha256 IS NULL OR inputs_sha256 ~ '^[0-9a-f]{64}$'),
    idempotency_key TEXT,
    created_by BIGINT NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, suffix),
    UNIQUE (project_id, device_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_project_models_idem_key
    ON project_models (project_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_project_models_project ON project_models (project_id);
CREATE INDEX IF NOT EXISTS idx_project_models_object ON project_models (model_object_id);
"""

_MIGRATION_0001_SQLITE = """
CREATE TABLE IF NOT EXISTS project_model_sequences (
    project_id INTEGER PRIMARY KEY REFERENCES projects(id),
    next_suffix INTEGER NOT NULL DEFAULT 1 CHECK (next_suffix >= 1)
);

CREATE TABLE IF NOT EXISTS project_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    suffix INTEGER NOT NULL CHECK (suffix >= 1),
    base_device_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    model_object_id INTEGER NOT NULL REFERENCES objects(id),
    receipt_object_id INTEGER NOT NULL REFERENCES objects(id),
    source TEXT NOT NULL CHECK (source IN ('direct_yaml','template')),
    template_sha256 TEXT,
    inputs_sha256 TEXT,
    idempotency_key TEXT,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (project_id, suffix),
    UNIQUE (project_id, device_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_project_models_idem_key
    ON project_models (project_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_project_models_project ON project_models (project_id);
CREATE INDEX IF NOT EXISTS idx_project_models_object ON project_models (model_object_id);
"""


def _migrate_0001(conn: sa.Connection) -> None:
    """创建项目模型清单表与编号序列表(0001, 切片 dm2-A)。

    FK 依赖 projects/objects/users —— 基线表由 init_db 的 create_all 先行
    建立(既有发布机制), 迁移在其后执行; 对仅缺本表的存量库同样成立。
    """
    ddl = (
        _MIGRATION_0001_POSTGRES
        if conn.dialect.name == "postgresql"
        else _MIGRATION_0001_SQLITE
    )
    for stmt in ddl.split(";"):
        stripped = stmt.strip()
        if stripped:
            conn.execute(sa_text(stripped))


#: 有序迁移清单(version, name, upgrade)
MIGRATIONS: list[tuple[str, str, object]] = [
    ("0001_project_model_manifest", "项目模型清单与编号序列表", _migrate_0001),
]

MIGRATION_VERSIONS: tuple[str, ...] = tuple(m[0] for m in MIGRATIONS)


def apply_migrations(engine: sa.Engine) -> list[str]:
    """应用未执行的版本化迁移, 返回本次应用的版本列表。

    台账表自举创建(IF NOT EXISTS); 每个迁移与台账登记在同一事务内
    (``engine.begin()``), 迁移中途失败整体回滚, 不会留下半迁移状态。
    重复调用幂等: 已应用版本直接跳过。
    """
    applied_now: list[str] = []
    with engine.begin() as conn:
        _LEDGER.create(conn, checkfirst=True)
        existing = {
            r[0] for r in conn.execute(sa.select(_LEDGER.c.version)).all()
        }
        for version, name, upgrade in MIGRATIONS:
            if version in existing:
                continue
            upgrade(conn)
            conn.execute(
                _LEDGER.insert().values(version=version, name=name)
            )
            applied_now.append(version)
    return applied_now
