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

-- 模板公开身份：存量 0002 表没有 slug/命名空间快照，必须显式补列。
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

ALTER TABLE model_templates ADD COLUMN current_draft_revision_id INTEGER REFERENCES model_template_draft_revisions(id);
ALTER TABLE model_templates ADD COLUMN current_published_revision_id INTEGER REFERENCES model_template_revisions(id);
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
        # ADD COLUMN, 先提 DO 块会在新库/旧库上因列缺失而失败)
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
        # 0002 的存量 SQLite 表只有 (owner_id, template_id) 唯一约束；
        # 新 ORM 契约要求稳定 ID 全局唯一、同一用户 slug 唯一。
        conn.execute(sa_text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_model_templates_template_id "
            "ON model_templates (template_id)"
        ))
        conn.execute(sa_text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_model_templates_owner_slug "
            "ON model_templates (owner_id, slug) WHERE slug IS NOT NULL"
        ))
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


# ---------------------------------------------------------------------------
# 迁移 0004: 项目计算基线(0.6.5 前置阶段事项 1)
# ---------------------------------------------------------------------------

#: 旧库回填语句: 仅填充 NULL 行, 幂等(重复执行不覆盖已回填值)。
_BACKFILL_SQL = """
UPDATE projects SET baseline_resolution='1h' WHERE baseline_resolution IS NULL;
UPDATE projects SET baseline_leap_year=false WHERE baseline_leap_year IS NULL;
UPDATE projects SET baseline_scenario_mode='single' WHERE baseline_scenario_mode IS NULL;
UPDATE project_versions SET baseline_resolution='1h' WHERE baseline_resolution IS NULL;
UPDATE project_versions SET baseline_leap_year=false WHERE baseline_leap_year IS NULL;
UPDATE project_versions SET baseline_scenario_mode='single' WHERE baseline_scenario_mode IS NULL;
"""


def _migrate_0004(conn: sa.Connection) -> None:
    """项目计算基线(0004, 0.6.5 事项 1)。

    - projects / project_versions 增加基线三列(resolution/leap_year/
      scenario_mode); 存量行按默认基线(1h/非闰年/single)回填;
      回填后 SET NOT NULL 并补 CHECK 约束;
    - 删除旧 ``fixed_utc_offset_minutes`` 列(projects / project_versions):
      时区语义随项目计算基线废除(宪法 7.5), 不保留兼容别名;
    - 幂等: 全新库由 create_all 先行建列, 本迁移仅处理存量库
      (ADD COLUMN IF NOT EXISTS / PRAGMA 守卫 + NULL 行回填)。

    SQLite 测试库: 全新库经 create_all 重建(无旧列), 本迁移基本为 no-op;
    对含旧列且无 CHECK 引用约束的存量 SQLite 库执行真实 DROP COLUMN。
    """
    if conn.dialect.name == "postgresql":
        _migrate_0004_postgres(conn)
    else:
        _migrate_0004_sqlite(conn)


def _migrate_0004_postgres(conn: sa.Connection) -> None:
    """Postgres 分支: 加列 → 锁表回填 → NOT NULL → CHECK → 删旧列。"""
    for table in ("projects", "project_versions"):
        _ensure_columns(
            conn, table,
            {
                "baseline_resolution": "TEXT",
                "baseline_leap_year": "BOOLEAN",
                "baseline_scenario_mode": "TEXT",
            },
        )
    # 存量库可能已按旧版本部署 project_versions 不可变触发器(BEFORE UPDATE →
    # RAISE), 回填 UPDATE 会被阻断; 先临时卸下(新旧命名), 清理后由 init_db 的
    # _deploy_immutable_triggers 按当前 IMMUTABLE_TABLES 重建(0006 财务迁移同模式)。
    conn.execute(sa_text("DROP FUNCTION IF EXISTS tg_project_versions_immutable() CASCADE"))
    conn.execute(sa_text("DROP TRIGGER IF EXISTS tg_project_versions_no_update ON project_versions"))
    conn.execute(sa_text("DROP TRIGGER IF EXISTS tg_project_versions_no_delete ON project_versions"))
    # 回填与 SET NOT NULL 之间锁表, 杜绝并发插入 NULL 与
    # 回填/加约束之间的原子性窗口。
    conn.execute(sa_text("LOCK TABLE projects IN EXCLUSIVE MODE"))
    conn.execute(sa_text("LOCK TABLE project_versions IN EXCLUSIVE MODE"))
    for stmt in _BACKFILL_SQL.split(";"):
        stripped = stmt.strip()
        if stripped:
            conn.execute(sa_text(stripped))
    for table in ("projects", "project_versions"):
        for column in (
            "baseline_resolution",
            "baseline_leap_year",
            "baseline_scenario_mode",
        ):
            conn.execute(sa_text(f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL"))
        conn.execute(
            sa_text(
                f"DO $$ BEGIN "
                f"IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_{table}_baseline_resolution') THEN "
                f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_baseline_resolution "
                f"CHECK (baseline_resolution IN ('15min','30min','1h')); END IF; END $$;"
            )
        )
        conn.execute(
            sa_text(
                f"DO $$ BEGIN "
                f"IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_{table}_baseline_scenario') THEN "
                f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_baseline_scenario "
                f"CHECK (baseline_scenario_mode IN ('single')); END IF; END $$;"
            )
        )
        # 旧时区列: DROP COLUMN 自动级联删除其 CHECK 约束(pg)。
        conn.execute(
            sa_text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS fixed_utc_offset_minutes")
        )


def _migrate_0004_sqlite(conn: sa.Connection) -> None:
    """SQLite 分支: PRAGMA 守卫加列(带默认值) → 删旧列。

    全新库(create_all 已含基线列、无旧列)为 no-op; 存量库按旧布局补列。
    SQLite ``ADD COLUMN ... NOT NULL`` 必须带 DEFAULT, 存量行即默认值。
    """
    defaults = {
        "baseline_resolution": "TEXT NOT NULL DEFAULT '1h'",
        "baseline_leap_year": "BOOLEAN NOT NULL DEFAULT 0",
        "baseline_scenario_mode": "TEXT NOT NULL DEFAULT 'single'",
    }
    for table in ("projects", "project_versions"):
        _ensure_columns(conn, table, defaults)
        existing = {
            row[1]
            for row in conn.execute(sa_text(f"PRAGMA table_info({table})")).all()
        }
        if "fixed_utc_offset_minutes" in existing:
            # SQLite 3.35+ 支持 DROP COLUMN; 列被 CHECK 约束引用时失败并
            # 抛出 DBAPIError(SQLite 测试库由 create_all 全量重建, 实际
            # 不存在含旧 CHECK 的存量库; 生产为 Postgres, 自动级联删除)。
            conn.execute(
                sa_text(f"ALTER TABLE {table} DROP COLUMN fixed_utc_offset_minutes")
            )


# ---------------------------------------------------------------------------
# 迁移 0005: 公共财务配置与规划配置不可变 revision 表(0.6.5 事项 3)
# ---------------------------------------------------------------------------

_MIGRATION_0005_POSTGRES = """
-- 规划配置 revision 表(仅 INSERT, 不可变; 修订号 revision 标识版本,
-- 与 ORM PlanningConfigRevision 同形, 无内容摘要列)
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
    - 旧单体 finance_configs 表不再创建(0006 直接退役; 存量库由 0006 删除);
    - 旧 finance_revision 列不再创建;
    - projects 增加 planning_revision 当前生效指针(_ensure_columns 守卫:
      全新库随 ORM create_all 已含列时为 no-op, 存量库按需补列; 与 0004 同模式);
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
# 迁移 0006: 财务三件套持久化替换旧单体 FinanceConfig(0.6.5 条目 1-2)
# ---------------------------------------------------------------------------

_MIGRATION_0006_CREATE_POSTGRES = """
-- 地区 FinanceProfile 注册表(已注册、可复用, Profile 主键 + 对象引用追溯;
-- 与 ORM FinanceProfile 同形, 无内容摘要列)
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


def _migrate_0006(conn: sa.Connection) -> None:
    """财务三件套持久化(0006, 0.6.5 条目 1-2)。

    - 新建 finance_profiles / finance_overrides / effective_finance_revisions
      (修订号 revision + 对象引用追溯, 与 ORM 同形, 无内容摘要列);
    - planning_configs 旧 0005 摘要列(finance_revision hash)直接删除,
      同时兼容 fresh(无旧列)与 legacy(0005 旧列)两种基线, 幂等;
    - 按正确性优先清理无效旧 planning revisions(旧 FinanceConfig 摘要链已失效,
      因无法迁移而删除)与项目 planning 指针清空;
    - projects: 删除旧 finance_revision 指针, 新增 finance_profile_id /
      overrides_revision / effective_finance_revision 指针;
    - 删除旧 finance_configs 单体表(0.6.5 纯契约先行, 无运行期消费;
      不保留新旧双轨)。
    确保新列 NOT NULL/check/索引/不可变触发器正确, 迁移幂等。
    全新 SQLite 测试库经 ORM create_all 重建(无旧列/旧表), 本迁移基本 no-op;
    存量库按需补列/删表/清理。
    """
    if conn.dialect.name == "postgresql":
        _migrate_0006_postgres(conn)
        return
    _migrate_0006_sqlite(conn)


def _migrate_0006_postgres(conn: sa.Connection) -> None:
    """Postgres 分支: 仅支持两种真实输入, 幂等, B 清理失效规划。"""
    # 1) 新三件套表
    for stmt in _MIGRATION_0006_CREATE_POSTGRES.split(";"):
        stripped = stmt.strip()
        if stripped:
            conn.execute(sa_text(stripped))
    # 2) planning_configs: 旧 0005 摘要列直接删除(不改名、不制造 hash 列)
    cols = {
        r[0]
        for r in conn.execute(
            sa_text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='planning_configs'"
            )
        ).all()
    }
    if "finance_revision" in cols:
        # B) legacy 0005 schema: 旧规划 revision 因旧 FinanceConfig 摘要链失效
        # 无法迁移, 直接删除旧行并清空项目指针; 旧摘要列直接删除。
        # 旧库已按 0005 部署 planning_configs 不可变触发器(BEFORE DELETE → RAISE),
        # 必须先临时卸下, 清理后由 init_db 的 _deploy_immutable_triggers
        # 按当前 IMMUTABLE_TABLES 重建。
        conn.execute(sa_text("DROP FUNCTION IF EXISTS tg_planning_configs_immutable() CASCADE"))
        conn.execute(sa_text("DELETE FROM planning_configs"))
        conn.execute(sa_text("UPDATE projects SET planning_revision = NULL WHERE planning_revision IS NOT NULL"))
        conn.execute(sa_text("ALTER TABLE planning_configs DROP COLUMN IF EXISTS finance_revision"))
    # A) fresh schema: 无旧摘要列, 保持现状。
    # 3) projects: 新指针列 + 删旧列
    _ensure_columns(
        conn,
        "projects",
        {
            "finance_profile_id": "BIGINT REFERENCES finance_profiles(id)",
            "overrides_revision": "BIGINT",
            "effective_finance_revision": "BIGINT",
        },
    )
    conn.execute(sa_text("ALTER TABLE projects DROP COLUMN IF EXISTS finance_revision"))
    # 4) 旧单体表退役(旧库可能带 finance_configs 不可变触发器函数)
    conn.execute(sa_text("DROP FUNCTION IF EXISTS tg_finance_configs_immutable() CASCADE"))
    conn.execute(sa_text("DROP TABLE IF EXISTS finance_configs"))


def _migrate_0006_sqlite(conn: sa.Connection) -> None:
    """SQLite 分支: 仅支持两种真实输入, B 清理失效规划。"""
    cols = {
        "planning_configs": {
            r[1]
            for r in conn.execute(sa_text("PRAGMA table_info(planning_configs)")).all()
        },
        "projects": {
            r[1]
            for r in conn.execute(sa_text("PRAGMA table_info(projects)")).all()
        },
    }
    if "finance_revision" in cols["planning_configs"]:
        # B) legacy 0005 schema: 旧规划 revision 因无法迁移而删除, 项目指针清空;
        conn.execute(sa_text("DELETE FROM planning_configs"))
        conn.execute(sa_text("UPDATE projects SET planning_revision = NULL WHERE planning_revision IS NOT NULL"))
        conn.execute(sa_text("ALTER TABLE planning_configs DROP COLUMN finance_revision"))
    # A) fresh schema: 无旧摘要列, 保持现状(不重复修改)
    if "finance_revision" in cols["projects"]:
        conn.execute(sa_text("ALTER TABLE projects DROP COLUMN finance_revision"))
    _ensure_columns(
        conn,
        "projects",
        {
            "finance_profile_id": "INTEGER REFERENCES finance_profiles(id)",
            "overrides_revision": "INTEGER",
            "effective_finance_revision": "INTEGER",
        },
    )
    conn.execute(sa_text("DROP TABLE IF EXISTS finance_configs"))


#: 有序迁移清单(version, name, upgrade)
MIGRATIONS: list[tuple[str, str, Callable[[sa.Connection], None]]] = [
    ("0001_project_model_manifest", "项目模型清单与编号序列表", _migrate_0001),
    ("0002_model_template_lifecycle", "用户模型模板主表与不可变发布 revision 表", _migrate_0002),
    ("0003_public_namespace_and_draft_history", "公开命名空间与不可变草稿历史", _migrate_0003),
    ("0004_project_baseline", "项目计算基线固定与旧时区列删除", _migrate_0004),
    ("0005_finance_planning_configs", "公共财务与规划配置不可变 revision 表", _migrate_0005),
    ("0006_finance_triplet_persistence", "财务三件套持久化替换旧单体 FinanceConfig", _migrate_0006),
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
