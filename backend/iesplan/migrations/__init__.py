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

from collections.abc import Callable

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
# 迁移 0002: 用户模型模板主表与不可变发布 revision 表(切片 dm2)
# ---------------------------------------------------------------------------

_MIGRATION_0002_POSTGRES = """
CREATE TABLE IF NOT EXISTS model_templates (
    id BIGSERIAL PRIMARY KEY,
    template_id TEXT NOT NULL,
    owner_id BIGINT NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','disabled')),
    description TEXT,
    draft_yaml_object_id BIGINT REFERENCES objects(id),
    draft_diagnostics_object_id BIGINT REFERENCES objects(id),
    draft_sha256 TEXT CHECK (draft_sha256 IS NULL OR draft_sha256 ~ '^[0-9a-f]{64}$'),
    draft_has_inputs BOOLEAN,
    draft_revision BIGINT NOT NULL DEFAULT 0 CHECK (draft_revision >= 0),
    draft_updated_at TIMESTAMPTZ,
    published_revision BIGINT NOT NULL DEFAULT 0 CHECK (published_revision >= 0),
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (owner_id, template_id)
);
CREATE INDEX IF NOT EXISTS idx_model_templates_owner ON model_templates (owner_id);

CREATE TABLE IF NOT EXISTS model_template_revisions (
    id BIGSERIAL PRIMARY KEY,
    template_id BIGINT NOT NULL REFERENCES model_templates(id),
    revision BIGINT NOT NULL CHECK (revision >= 1),
    schema_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    inputs_sha256 TEXT CHECK (inputs_sha256 IS NULL OR inputs_sha256 ~ '^[0-9a-f]{64}$'),
    input_count BIGINT NOT NULL DEFAULT 0 CHECK (input_count >= 0),
    yaml_object_id BIGINT NOT NULL REFERENCES objects(id),
    receipt_object_id BIGINT NOT NULL REFERENCES objects(id),
    summary_object_id BIGINT NOT NULL REFERENCES objects(id),
    diagnostics_object_id BIGINT REFERENCES objects(id),
    idempotency_key TEXT,
    published_by BIGINT NOT NULL REFERENCES users(id),
    published_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (template_id, revision),
    UNIQUE (template_id, content_sha256)
);
CREATE INDEX IF NOT EXISTS idx_mtr_template ON model_template_revisions (template_id);
CREATE INDEX IF NOT EXISTS idx_mtr_idem_key
    ON model_template_revisions (template_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
"""

_MIGRATION_0002_SQLITE = """
CREATE TABLE IF NOT EXISTS model_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id TEXT NOT NULL,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','disabled')),
    description TEXT,
    draft_yaml_object_id INTEGER REFERENCES objects(id),
    draft_diagnostics_object_id INTEGER REFERENCES objects(id),
    draft_sha256 TEXT CHECK (length(draft_sha256) = 64),
    draft_has_inputs BOOLEAN,
    draft_revision INTEGER NOT NULL DEFAULT 0 CHECK (draft_revision >= 0),
    draft_updated_at TIMESTAMP,
    published_revision INTEGER NOT NULL DEFAULT 0 CHECK (published_revision >= 0),
    published_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (owner_id, template_id)
);
CREATE INDEX IF NOT EXISTS idx_model_templates_owner ON model_templates (owner_id);

CREATE TABLE IF NOT EXISTS model_template_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL REFERENCES model_templates(id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    schema_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    inputs_sha256 TEXT,
    input_count INTEGER NOT NULL DEFAULT 0 CHECK (input_count >= 0),
    yaml_object_id INTEGER NOT NULL REFERENCES objects(id),
    receipt_object_id INTEGER NOT NULL REFERENCES objects(id),
    summary_object_id INTEGER NOT NULL REFERENCES objects(id),
    diagnostics_object_id INTEGER REFERENCES objects(id),
    idempotency_key TEXT,
    published_by INTEGER NOT NULL REFERENCES users(id),
    published_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (template_id, revision),
    UNIQUE (template_id, content_sha256)
);
CREATE INDEX IF NOT EXISTS idx_mtr_template ON model_template_revisions (template_id);
CREATE INDEX IF NOT EXISTS idx_mtr_idem_key
    ON model_template_revisions (template_id, idempotency_key);
"""


def _migrate_0002(conn: sa.Connection) -> None:
    """创建用户模型模板主表与不可变发布 revision 表(0002)。

    全新数据库与已执行 0001 的存量数据库同一路径: 迁移按版本在台账登记,
    幂等执行; 存量库已有 model_templates 时 IF NOT EXISTS 跳过(仅防重复
    执行, 正常路径由台账版本控制)。
    """
    ddl = (
        _MIGRATION_0002_POSTGRES
        if conn.dialect.name == "postgresql"
        else _MIGRATION_0002_SQLITE
    )
    for stmt in ddl.split(";"):
        stripped = stmt.strip()
        if stripped:
            conn.execute(sa_text(stripped))


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
    project_revision BIGINT NOT NULL CHECK (project_revision >= 2),
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
    project_revision INTEGER NOT NULL CHECK (project_revision >= 2),
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


def _ensure_columns(
    conn: sa.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    """按方言为存量表补充缺失列(幂等)。

    Postgres: ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``;
    SQLite(<3.35 无 IF NOT EXISTS): 先查 pragma table_info 再逐列 ADD。
    全新库随建表语句已含全部列, 本函数仅覆盖「已执行旧迁移的存量库」。
    """
    if conn.dialect.name == "postgresql":
        for name, ddl in columns.items():
            conn.execute(sa_text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {ddl}"))
        return
    existing = {
        row[1]
        for row in conn.execute(sa_text(f"PRAGMA table_info({table})")).all()
    }
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(sa_text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


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
    # 切片 dm2: 存量库(已执行 0001)补充模板来源列; 全新库建表语句已含
    _ensure_columns(
        conn,
        "project_models",
        {"template_id": "TEXT", "template_revision": "BIGINT"},
    )


# ---------------------------------------------------------------------------
# 迁移 0003: 公开命名空间与不可变草稿历史（任务书 §一～§四）
# ---------------------------------------------------------------------------

_MIGRATION_0003_POSTGRES = """
-- 用户公开命名空间（12 位小写 Crockford Base32，60 bit 熵；全局唯一）
ALTER TABLE users ADD COLUMN IF NOT EXISTS public_namespace TEXT;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_users_public_namespace') THEN
        ALTER TABLE users ADD CONSTRAINT ck_users_public_namespace
            CHECK (public_namespace IS NULL OR public_namespace ~ '^[0-9a-hjkmnp-tv-z]{12}$');
    END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_public_namespace
    ON users (public_namespace) WHERE public_namespace IS NOT NULL;

-- 草稿不可变历史表（每次持久化形成新 revision）
CREATE TABLE IF NOT EXISTS model_template_draft_revisions (
    id BIGSERIAL PRIMARY KEY,
    entry_id BIGINT NOT NULL REFERENCES model_templates(id),
    revision BIGINT NOT NULL CHECK (revision >= 1),
    yaml_object_id BIGINT NOT NULL REFERENCES objects(id),
    canonical_sha256 TEXT NOT NULL CHECK (canonical_sha256 ~ '^[0-9a-f]{64}$'),
    inputs_sha256 TEXT CHECK (inputs_sha256 IS NULL OR inputs_sha256 ~ '^[0-9a-f]{64}$'),
    source TEXT NOT NULL CHECK (source IN ('form','yaml_editor','upload','derived','migration')),
    created_by BIGINT NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    diagnostics_object_id BIGINT REFERENCES objects(id),
    UNIQUE (entry_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_mtdr_entry ON model_template_draft_revisions (entry_id);

-- 模板主表：当前草稿 revision 指针与最新发布 revision 指针（不可变历史的索引）
ALTER TABLE model_templates ADD COLUMN IF NOT EXISTS current_draft_revision_id BIGINT
    REFERENCES model_template_draft_revisions(id);
ALTER TABLE model_templates ADD COLUMN IF NOT EXISTS current_published_revision_id BIGINT
    REFERENCES model_template_revisions(id);

-- 已发布模板离线迁移回执（旧 ID → 新 ID 映射、摘要、回执）
CREATE TABLE IF NOT EXISTS template_migration_receipts (
    id BIGSERIAL PRIMARY KEY,
    old_template_id TEXT NOT NULL,
    new_template_id TEXT NOT NULL,
    entry_id BIGINT NOT NULL REFERENCES model_templates(id),
    old_content_sha256 TEXT NOT NULL CHECK (old_content_sha256 ~ '^[0-9a-f]{64}$'),
    new_content_sha256 TEXT NOT NULL CHECK (new_content_sha256 ~ '^[0-9a-f]{64}$'),
    migrated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    migrated_by BIGINT NOT NULL REFERENCES users(id),
    UNIQUE (old_template_id),
    UNIQUE (new_template_id)
);
"""

_MIGRATION_0003_SQLITE = """
ALTER TABLE users ADD COLUMN public_namespace TEXT;

CREATE TABLE IF NOT EXISTS model_template_draft_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES model_templates(id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    yaml_object_id INTEGER NOT NULL REFERENCES objects(id),
    canonical_sha256 TEXT NOT NULL CHECK (length(canonical_sha256) = 64),
    inputs_sha256 TEXT,
    source TEXT NOT NULL CHECK (source IN ('form','yaml_editor','upload','derived','migration')),
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    diagnostics_object_id INTEGER REFERENCES objects(id),
    UNIQUE (entry_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_mtdr_entry ON model_template_draft_revisions (entry_id);

ALTER TABLE model_templates ADD COLUMN current_draft_revision_id INTEGER REFERENCES model_template_draft_revisions(id);
ALTER TABLE model_templates ADD COLUMN current_published_revision_id INTEGER REFERENCES model_template_revisions(id);

CREATE TABLE IF NOT EXISTS template_migration_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    old_template_id TEXT NOT NULL,
    new_template_id TEXT NOT NULL,
    entry_id INTEGER NOT NULL REFERENCES model_templates(id),
    old_content_sha256 TEXT NOT NULL CHECK (length(old_content_sha256) = 64),
    new_content_sha256 TEXT NOT NULL CHECK (length(new_content_sha256) = 64),
    migrated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    migrated_by INTEGER NOT NULL REFERENCES users(id),
    UNIQUE (old_template_id),
    UNIQUE (new_template_id)
);
"""


def _migrate_0003(conn: sa.Connection) -> None:
    """公开命名空间与不可变草稿历史（0003）。

    - users.public_namespace：全局唯一 12 位 Crockford Base32；
    - model_template_draft_revisions：不可变草稿历史；
    - model_templates 指针列：当前草稿/发布 revision；
    - template_migration_receipts：离线迁移回执。

    幂等：Postgres 用 IF NOT EXISTS；SQLite 用 pragma 检查后 ADD COLUMN。
    不在 db.py 中补列或改表（任务书 §三：禁止启动流程补列）。
    """
    if conn.dialect.name == "postgresql":
        # Execute DO block separately, then remaining statements
        # DO block contains semicolons inside, so execute whole postgres DDL with a single execute per statement block
        # Split on ';' outside of $$ blocks
        import re
        pg_sql = _MIGRATION_0003_POSTGRES
        # Extract DO blocks and execute separately
        do_blocks = re.findall(r'DO \$\$.*?END \$\$;', pg_sql, flags=re.DOTALL)
        remaining = re.sub(r'DO \$\$.*?END \$\$;', '', pg_sql, flags=re.DOTALL)
        for block in do_blocks:
            conn.execute(sa_text(block))
        for stmt in remaining.split(";"):
            stripped = stmt.strip()
            if stripped:
                conn.execute(sa_text(stripped))
    else:
        # SQLite: 先用 _ensure_columns 处理 ALTER TABLE（避免 duplicate column）
        _ensure_columns(conn, "users", {"public_namespace": "TEXT"})
        _ensure_columns(conn, "model_templates", {
            "current_draft_revision_id": "INTEGER REFERENCES model_template_draft_revisions(id)",
            "current_published_revision_id": "INTEGER REFERENCES model_template_revisions(id)",
            "slug": "TEXT",
            "public_namespace": "TEXT",
        })
        # 其余 DDL（建表、索引）可直接执行（IF NOT EXISTS 已处理）
        for stmt in _MIGRATION_0003_SQLITE.split(";"):
            stripped = stmt.strip()
            if stripped and "ALTER TABLE" not in stripped:
                conn.execute(sa_text(stripped))
    # 既有用户一次性分配 namespace（CSPRNG + 碰撞重试，任务书 §三）
    _allocate_namespaces_for_existing_users(conn)


def _allocate_namespaces_for_existing_users(conn: sa.Connection) -> None:
    """为既有用户一次性分配 public_namespace（全局唯一，碰撞重试）。"""
    from iesplan.core.namespace import generate_namespace

    # 检查 users 表是否存在（全新库可能尚未 create_all）
    try:
        rows = conn.execute(sa_text("SELECT id FROM users WHERE public_namespace IS NULL")).all()
    except Exception:
        return
    for (uid,) in rows:
        for _ in range(10):
            ns = generate_namespace()
            try:
                conn.execute(
                    sa_text("UPDATE users SET public_namespace = :ns WHERE id = :uid AND public_namespace IS NULL"),
                    {"ns": ns, "uid": uid},
                )
                break
            except Exception:
                continue


#: 有序迁移清单(version, name, upgrade)
MIGRATIONS: list[tuple[str, str, Callable[[sa.Connection], None]]] = [
    ("0001_project_model_manifest", "项目模型清单与编号序列表", _migrate_0001),
    ("0002_model_template_lifecycle", "用户模型模板主表与不可变发布 revision 表", _migrate_0002),
    ("0003_public_namespace_and_draft_history", "公开命名空间与不可变草稿历史", _migrate_0003),
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
            upgrade(conn)  # type: ignore[call-arg]  # 运行时为可调用迁移函数
            conn.execute(
                _LEDGER.insert().values(version=version, name=name)
            )
            applied_now.append(version)
    return applied_now
