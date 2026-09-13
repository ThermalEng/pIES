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
    input_count BIGINT NOT NULL DEFAULT 0 CHECK (input_count >= 0),
    yaml_object_id BIGINT NOT NULL REFERENCES objects(id),
    receipt_object_id BIGINT NOT NULL REFERENCES objects(id),
    summary_object_id BIGINT NOT NULL REFERENCES objects(id),
    diagnostics_object_id BIGINT REFERENCES objects(id),
    idempotency_key TEXT,
    published_by BIGINT NOT NULL REFERENCES users(id),
    published_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (template_id, revision)
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
    input_count INTEGER NOT NULL DEFAULT 0 CHECK (input_count >= 0),
    yaml_object_id INTEGER NOT NULL REFERENCES objects(id),
    receipt_object_id INTEGER NOT NULL REFERENCES objects(id),
    summary_object_id INTEGER NOT NULL REFERENCES objects(id),
    diagnostics_object_id INTEGER REFERENCES objects(id),
    idempotency_key TEXT,
    published_by INTEGER NOT NULL REFERENCES users(id),
    published_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (template_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_mtr_template ON model_template_revisions (template_id);
CREATE INDEX IF NOT EXISTS idx_mtr_idem_key
    ON model_template_revisions (template_id, idempotency_key);
"""


def _migrate_0002(conn: sa.Connection) -> None:
    """创建用户模型模板主表与不可变发布 revision 表(0002)。

    迁移按版本在台账登记并幂等执行，重复建表由 IF NOT EXISTS 跳过。
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
    model_object_id BIGINT NOT NULL REFERENCES objects(id),
    receipt_object_id BIGINT NOT NULL REFERENCES objects(id),
    source TEXT NOT NULL CHECK (source IN ('direct_yaml','template')),
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
    model_object_id INTEGER NOT NULL REFERENCES objects(id),
    receipt_object_id INTEGER NOT NULL REFERENCES objects(id),
    source TEXT NOT NULL CHECK (source IN ('direct_yaml','template')),
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
    """按方言为当前 schema 补充缺失列(幂等)。

    Postgres: ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``;
    SQLite(<3.35 无 IF NOT EXISTS): 先查 pragma table_info 再逐列 ADD。
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

    FK 依赖 projects/objects/users；初始化流程先建立基线表，再执行本迁移。
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

-- 模板公开身份与命名空间快照。
ALTER TABLE model_templates ADD COLUMN IF NOT EXISTS slug TEXT;
ALTER TABLE model_templates ADD COLUMN IF NOT EXISTS public_namespace TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_model_templates_template_id
    ON model_templates (template_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_model_templates_owner_slug
    ON model_templates (owner_id, slug) WHERE slug IS NOT NULL;

-- 草稿不可变历史表（每次持久化形成新 revision）
CREATE TABLE IF NOT EXISTS model_template_draft_revisions (
    id BIGSERIAL PRIMARY KEY,
    entry_id BIGINT NOT NULL REFERENCES model_templates(id),
    revision BIGINT NOT NULL CHECK (revision >= 1),
    yaml_object_id BIGINT NOT NULL REFERENCES objects(id),
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
"""

_MIGRATION_0003_SQLITE = """
ALTER TABLE users ADD COLUMN public_namespace TEXT;

CREATE TABLE IF NOT EXISTS model_template_draft_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES model_templates(id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    yaml_object_id INTEGER NOT NULL REFERENCES objects(id),
    source TEXT NOT NULL CHECK (source IN ('form','yaml_editor','upload','derived','migration')),
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    diagnostics_object_id INTEGER REFERENCES objects(id),
    UNIQUE (entry_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_mtdr_entry ON model_template_draft_revisions (entry_id);

ALTER TABLE model_templates ADD COLUMN current_draft_revision_id INTEGER
    REFERENCES model_template_draft_revisions(id);
ALTER TABLE model_templates ADD COLUMN current_published_revision_id INTEGER
    REFERENCES model_template_revisions(id);
"""


def _migrate_0003(conn: sa.Connection) -> None:
    """公开命名空间与不可变草稿历史（0003）。

    - users.public_namespace：全局唯一 12 位 Crockford Base32；
    - model_template_draft_revisions：不可变草稿历史；
    - model_templates 指针列：当前草稿/发布 revision。

    幂等：Postgres 用 IF NOT EXISTS；SQLite 用 pragma 检查后 ADD COLUMN。
    不在 db.py 中补列或改表（任务书 §三：禁止启动流程补列）。
    """
    if conn.dialect.name == "postgresql":
        # DO 块含内部分号, 需整块执行; 但必须保持文本顺序(DO 块依赖前面的
        # ADD COLUMN, 先提 DO 块会因列缺失而失败)
        import re
        pg_sql = _MIGRATION_0003_POSTGRES
        for part in re.split(r"(DO \$\$.*?END \$\$;)", pg_sql, flags=re.DOTALL):
            stripped = part.strip()
            if not stripped:
                continue
            if stripped.startswith("DO"):
                conn.execute(sa_text(stripped))
            else:
                for stmt in stripped.split(";"):
                    if stmt.strip():
                        conn.execute(sa_text(stmt.strip()))
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
        # 稳定 ID 全局唯一、同一用户 slug 唯一。
        conn.execute(sa_text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_model_templates_template_id "
            "ON model_templates (template_id)"
        ))
        conn.execute(sa_text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_model_templates_owner_slug "
            "ON model_templates (owner_id, slug) WHERE slug IS NOT NULL"
        ))
# ---------------------------------------------------------------------------
# 迁移 0004: 项目计算基线(0.6.5 前置阶段事项 1)
# ---------------------------------------------------------------------------


def _migrate_0004(conn: sa.Connection) -> None:
    """登记项目计算基线契约版本。

    当前基线字段及数据库约束由项目模型的 schema 定义创建；该版本保留在
    迁移台账中，确保当前迁移链的版本语义稳定。初始化流程在此版本不执行
    数据转换或结构清理。
    """
    return None


# ---------------------------------------------------------------------------
# 迁移 0005: 公共财务配置与规划配置不可变 revision 表(0.6.5 事项 3)
# ---------------------------------------------------------------------------

_MIGRATION_0005_POSTGRES = """
-- 规划配置 revision 表(仅 INSERT, 不可变; 修订号 revision 标识版本,
-- 与 ORM PlanningConfigRevision 同形)
CREATE TABLE IF NOT EXISTS planning_configs (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES projects(id),
    revision BIGINT NOT NULL CHECK (revision >= 1),
    content JSONB NOT NULL,
    created_by BIGINT NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_planning_configs_project
    ON planning_configs (project_id, revision DESC);
"""

_MIGRATION_0005_SQLITE = """
CREATE TABLE IF NOT EXISTS planning_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    content TEXT NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (project_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_planning_configs_project
    ON planning_configs (project_id, revision DESC);
"""


def _migrate_0005(conn: sa.Connection) -> None:
    """规划配置不可变 revision 表(0005, 0.6.5 事项 3)。

    - planning_configs: 仅 INSERT 的 revision 追加表(修订号 revision 标识版本;
      不可变性由 immutable_triggers 部署的禁 UPDATE/DELETE 触发器保证);
    - projects 增加 planning_revision 当前生效指针;
    - 幂等: IF NOT EXISTS + 列守卫。
    """
    ddl = (
        _MIGRATION_0005_POSTGRES
        if conn.dialect.name == "postgresql"
        else _MIGRATION_0005_SQLITE
    )
    for stmt in ddl.split(";"):
        stripped = stmt.strip()
        if stripped:
            conn.execute(sa_text(stripped))
    # 项目当前生效 revision 指针(指向不可变 revision, 指针本身可移动)
    _ensure_columns(
        conn,
        "projects",
        {"planning_revision": "BIGINT"},
    )


# ---------------------------------------------------------------------------
# 迁移 0006: 财务三件套持久化(0.6.5 条目 1-2)
# ---------------------------------------------------------------------------

_MIGRATION_0006_CREATE_POSTGRES = """
-- 地区 FinanceProfile 注册表(已注册、可复用, Profile 主键 + 对象引用追溯;
-- 与 ORM FinanceProfile 同形)
CREATE TABLE IF NOT EXISTS finance_profiles (
    id BIGSERIAL PRIMARY KEY,
    profile_id TEXT NOT NULL,
    region TEXT NOT NULL,
    content JSONB NOT NULL,
    object_id BIGINT NOT NULL REFERENCES objects(id),
    created_by BIGINT NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (profile_id)
);
CREATE INDEX IF NOT EXISTS idx_finance_profiles_id ON finance_profiles (profile_id);

-- 项目 FinanceOverrides 不可变 revision(仅 INSERT; 修订号 revision 标识版本)
CREATE TABLE IF NOT EXISTS finance_overrides (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES projects(id),
    revision BIGINT NOT NULL CHECK (revision >= 1),
    content JSONB NOT NULL,
    profile_id TEXT NOT NULL,
    created_by BIGINT NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_finance_overrides_project
    ON finance_overrides (project_id, revision DESC);

-- 项目 EffectiveFinanceConfig 不可变 revision(合并器产物, 仅 INSERT)
CREATE TABLE IF NOT EXISTS effective_finance_revisions (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES projects(id),
    revision BIGINT NOT NULL CHECK (revision >= 1),
    content JSONB NOT NULL,
    profile_id TEXT NOT NULL,
    created_by BIGINT NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_effective_finance_revisions_project
    ON effective_finance_revisions (project_id, revision DESC);
"""

_MIGRATION_0006_CREATE_SQLITE = """
CREATE TABLE IF NOT EXISTS finance_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT NOT NULL,
    region TEXT NOT NULL,
    content TEXT NOT NULL,
    object_id INTEGER NOT NULL REFERENCES objects(id),
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_finance_profiles_id UNIQUE (profile_id)
);
CREATE INDEX IF NOT EXISTS idx_finance_profiles_id ON finance_profiles (profile_id);

CREATE TABLE IF NOT EXISTS finance_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    content TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_finance_overrides_revision UNIQUE (project_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_finance_overrides_project
    ON finance_overrides (project_id, revision DESC);

CREATE TABLE IF NOT EXISTS effective_finance_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    content TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_effective_finance_revisions_revision UNIQUE (project_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_effective_finance_revisions_project
    ON effective_finance_revisions (project_id, revision DESC);
"""


def _migrate_0006(conn: sa.Connection) -> None:
    """财务三件套持久化(0006, 0.6.5 条目 1-2)。

    - 新建 finance_profiles / finance_overrides / effective_finance_revisions
      (修订号 revision + 对象引用追溯, 与 ORM 同形);
    - projects 增加 finance_profile_id / overrides_revision /
      effective_finance_revision 当前指针;
    - 迁移幂等，结构由当前 schema 定义固定。
    """
    if conn.dialect.name == "postgresql":
        _migrate_0006_postgres(conn)
        return
    _migrate_0006_sqlite(conn)


def _migrate_0006_postgres(conn: sa.Connection) -> None:
    """Postgres: 创建三件套表并增加项目当前指针。"""
    for stmt in _MIGRATION_0006_CREATE_POSTGRES.split(";"):
        stripped = stmt.strip()
        if stripped:
            conn.execute(sa_text(stripped))
    # 项目当前财务指针
    _ensure_columns(
        conn,
        "projects",
        {
            "finance_profile_id": "BIGINT REFERENCES finance_profiles(id)",
            "overrides_revision": "BIGINT",
            "effective_finance_revision": "BIGINT",
        },
    )


def _migrate_0006_sqlite(conn: sa.Connection) -> None:
    """SQLite: 创建三件套表并增加项目当前指针。"""
    for stmt in _MIGRATION_0006_CREATE_SQLITE.split(";"):
        stripped = stmt.strip()
        if stripped:
            conn.execute(sa_text(stripped))
    _ensure_columns(
        conn,
        "projects",
        {
            "finance_profile_id": "INTEGER REFERENCES finance_profiles(id)",
            "overrides_revision": "INTEGER",
            "effective_finance_revision": "INTEGER",
        },
    )


def _migrate_0007(conn: sa.Connection) -> None:
    """配置 revision 可审计回执列(0.6.5 条目 1-2)。

    为 finance_overrides / effective_finance_revisions / planning_configs
    增加 receipt_object_id 列(对象引用, 指向回执 JSON 对象)。
    新建 revision 行由服务层必备回执；迁移幂等补充当前 schema 列。
    迁移失败直接抛出(同一事务回滚, 台账不记录)。
    """
    _ensure_columns(
        conn,
        "finance_overrides",
        {"receipt_object_id": "BIGINT REFERENCES objects(id)"},
    )
    _ensure_columns(
        conn,
        "effective_finance_revisions",
        {"receipt_object_id": "BIGINT REFERENCES objects(id)"},
    )
    _ensure_columns(
        conn,
        "planning_configs",
        {"receipt_object_id": "BIGINT REFERENCES objects(id)"},
    )


#: 有序迁移清单(version, name, upgrade)
MIGRATIONS: list[tuple[str, str, Callable[[sa.Connection], None]]] = [
    ("0001_project_model_manifest", "项目模型清单与编号序列表", _migrate_0001),
    ("0002_model_template_lifecycle", "用户模型模板主表与不可变发布 revision 表", _migrate_0002),
    ("0003_public_namespace_and_draft_history", "公开命名空间与不可变草稿历史", _migrate_0003),
    ("0004_project_baseline", "项目计算基线契约", _migrate_0004),
    ("0005_finance_planning_configs", "公共财务与规划配置不可变 revision 表", _migrate_0005),
    ("0006_finance_triplet_persistence", "财务三件套持久化", _migrate_0006),
    ("0007_config_revision_receipts", "配置 revision 可审计回执列", _migrate_0007),
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
