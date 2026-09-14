"""数据库引擎、会话管理与初始化。

- engine / SessionLocal: 全局单例(连接池 + 预检)。
- Base: 全部 ORM 模型的声明基类 + 共享列基元(JSONB/正则 CHECK/主键构造)。
- 不可变表触发器 DDL 常量(部署见 _deploy_immutable_triggers)。
- get_db(): FastAPI 请求级依赖。
- init_db(): 幂等建表(create_all) + 种子管理员。
- seed_admin(): 幂等创建内置管理员(首登强制改密)。

本模块只保留连接 / Session / Base 基础设施;ORM 表定义归各领域 persistence 所有,
init_db 集中导入注册(过渡位, Wave3 迁入 bootstrap)。
"""

from __future__ import annotations

from collections.abc import Generator

import sqlalchemy as sa
from sqlalchemy import create_engine, select
from sqlalchemy import text as sa_text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import DeclarativeBase, MappedColumn, Session, mapped_column, sessionmaker

from iesplan.config import settings

#: SQLAlchemy 引擎: pool_pre_ping 在取连接时先探活, 避免使用失效连接
engine = create_engine(settings.db_url, pool_pre_ping=True)

#: 会话工厂: autoflush=False(显式控制)、expire_on_commit=False(提交后可继续读取)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """全部 ORM 模型的声明基类。"""


# ---------------------------------------------------------------------------
# 共享列基元(Wave2A 由 iesplan.models.common 迁入; Base 基础设施的一部分)。
#
# SQLite(测试)与 PostgreSQL(生产)之间的 DDL 差异在这里统一收口:
# ``JSONB`` / ``BIGINT[]`` / ``INET`` 等 PostgreSQL 专有类型在 SQLite 回退为通用类型;
# 正则 CHECK(``~`` 运算符)用 ``PgRegexCheck`` 包装: PostgreSQL 按文档原样输出,
# SQLite 编译为恒真 ``CHECK (1=1)``;正则语义校验由应用层保证。
# ---------------------------------------------------------------------------

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import MappedColumn, mapped_column

#: 幂等键正则复出(唯一权威: iesplan.tasks.contracts; DDL 经此处取用)。
from iesplan.tasks.contracts import IDEMPOTENCY_KEY_RE as IDEMPOTENCY_KEY_RE

#: 用户名/邮箱正则唯一权威: iesplan.identity.contracts; DDL 经此处取用。
from iesplan.identity.contracts import EMAIL_RE as EMAIL_RE
from iesplan.identity.contracts import USERNAME_RE as USERNAME_RE

#: 64 位小写十六进制(对象 id 等随机标识格式)
HASH64_RE: str = "^[0-9a-f]{64}$"

class JSONB(sa.types.TypeDecorator):
    """JSONB 类型: PostgreSQL 原生 JSONB, SQLite 回退 JSON(仅测试)。"""

    impl = sa.JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.JSONB())
        return dialect.type_descriptor(sa.JSON())


class BigIntArray(sa.types.TypeDecorator):
    """BIGINT[] 类型: PostgreSQL 原生数组, SQLite 回退 JSON 数组(仅测试)。"""

    impl = sa.JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.ARRAY(sa.BigInteger()))
        return dialect.type_descriptor(sa.JSON())


class InetType(sa.types.TypeDecorator):
    """INET 类型: PostgreSQL 原生 INET, SQLite 回退 VARCHAR(仅测试)。"""

    impl = sa.String
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.INET())
        return dialect.type_descriptor(sa.String())


class IdentityBigInt(sa.types.TypeDecorator):
    """BIGINT 自增主键类型: PostgreSQL 用 BIGINT, SQLite 回退 INTEGER。

    SQLite 只有类型精确为 INTEGER 的列才是行号别名(自动自增),
    因此 SQLite 测试环境回退 INTEGER, PostgreSQL 保持文档要求的 BIGINT。
    """

    impl = sa.Integer
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(sa.BigInteger())
        return dialect.type_descriptor(sa.Integer())


class PgRegexCheck(sa.CheckConstraint):
    """PostgreSQL 正则 CHECK 的跨方言包装。

    PostgreSQL 上按文档原样输出 ``col ~ 'regex'``;SQLite 无正则运算符,
    编译为恒真 ``CHECK (1=1)``, 仅保证建表可用。
    """


@compiles(PgRegexCheck, "sqlite")
def _compile_regex_check_sqlite(element: PgRegexCheck, compiler, **kw) -> str:
    """SQLite 下将正则 CHECK 编译为恒真表达式。"""
    name = element.name
    prefix = f"CONSTRAINT {name} " if name else ""
    return f"{prefix}CHECK (1=1)"


def regex_check(sql: str, name: str) -> PgRegexCheck:
    """构造带跨方言回退的正则 CHECK 约束。"""
    return PgRegexCheck(sql, name=name)


def bigint_pk() -> MappedColumn:
    """BIGINT GENERATED ALWAYS AS IDENTITY 主键列(01 第0节: BIGINT PK IDENTITY)。

    PostgreSQL 上按文档渲染 ``BIGINT GENERATED ALWAYS AS IDENTITY``;
    SQLite 上 SQLAlchemy 不渲染 Identity 子句, 且 BIGINT 不是行号别名,
    故通过 IdentityBigInt 回退 INTEGER 主键, 保证测试环境自增可用。
    """
    return mapped_column(IdentityBigInt(), sa.Identity(always=True), primary_key=True)

# ---------------------------------------------------------------------------
# 不可变表触发器 DDL(Wave2A 由 iesplan.models.immutable_triggers 迁入;
# 字符串常量, 由 _deploy_immutable_triggers 在 PostgreSQL 下幂等执行)。
# ---------------------------------------------------------------------------

#: 不可变表清单（仅 INSERT，禁止 UPDATE/DELETE）
IMMUTABLE_TABLES: tuple[str, ...] = (
    "auth_events",
    "admin_maintenance_actions",
    "project_versions",
    "version_refs",
    "dataset_versions",
    "dataset_files",
    "calc_snapshots",
    "task_diagnostics",
    "evidence_packages",
    "result_assessments",
    "uncertainty_snapshots",
    "audit_log",
    "finance_profiles",
    "finance_overrides",
    "effective_finance_revisions",
    "planning_configs",
)


def _immutable_trigger_sql(table: str) -> str:
    """生成单张不可变表的触发器 DDL(函数 + UPDATE/DELETE 两个触发器)。"""
    return f"""\
-- {table}: 不可变表(仅 INSERT), 禁止 UPDATE/DELETE(01 第0节三道防线第3层)
CREATE FUNCTION tg_{table}_immutable() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '{table} 为不可变表, 禁止 %', TG_OP;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER tg_{table}_no_update BEFORE UPDATE ON {table}
  FOR EACH ROW EXECUTE FUNCTION tg_{table}_immutable();
CREATE TRIGGER tg_{table}_no_delete BEFORE DELETE ON {table}
  FOR EACH ROW EXECUTE FUNCTION tg_{table}_immutable();
"""


#: 每张不可变表 -> 触发器 DDL
IMMUTABLE_TRIGGER_SQL: dict[str, str] = {
    table: _immutable_trigger_sql(table) for table in IMMUTABLE_TABLES
}

#: 全部不可变表触发器 DDL 汇总(逐条执行即可)
ALL_IMMUTABLE_TRIGGER_DDL: str = "\n\n".join(IMMUTABLE_TRIGGER_SQL.values())


def _revoke_sql(table: str) -> str:
    """生成单张不可变表的 REVOKE DDL(三道防线第2层: 不授予任何角色 UPDATE/DELETE)。"""
    return f"REVOKE UPDATE, DELETE ON {table} FROM PUBLIC;"


#: 每张不可变表 -> REVOKE DDL
IMMUTABLE_REVOKE_SQL: dict[str, str] = {table: _revoke_sql(table) for table in IMMUTABLE_TABLES}

#: 全部 REVOKE DDL 汇总
ALL_IMMUTABLE_REVOKE_DDL: str = "\n".join(IMMUTABLE_REVOKE_SQL.values())

# ---------------------------------------------------------------------------
# 半不可变表 / 状态机表的专项触发器(01 相关章节)
# ---------------------------------------------------------------------------

#: system_graphs: 版本图(project_version_id 非空)禁止任何 UPDATE(01 §4.1)
SYSTEM_GRAPHS_FROZEN_TRIGGER_SQL: str = """\
-- system_graphs: 版本图不可修改(工作图可改)
CREATE FUNCTION tg_system_graphs_version_frozen() RETURNS trigger AS $$
BEGIN
  IF OLD.project_version_id IS NOT NULL THEN
    RAISE EXCEPTION '版本图不可修改';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER tg_system_graphs_frozen BEFORE UPDATE ON system_graphs
  FOR EACH ROW EXECUTE FUNCTION tg_system_graphs_version_frozen();
"""

#: calc_configs: status='frozen' 的行禁止 UPDATE(01 §6.1);DELETE 由应用层约束
CALC_CONFIGS_FROZEN_TRIGGER_SQL: str = """\
-- calc_configs: 冻结的计算配置不可修改
CREATE FUNCTION tg_calc_configs_frozen() RETURNS trigger AS $$
BEGIN
  IF OLD.status = 'frozen' THEN
    RAISE EXCEPTION '冻结的计算配置不可修改';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER tg_calc_configs_no_update BEFORE UPDATE ON calc_configs
  FOR EACH ROW EXECUTE FUNCTION tg_calc_configs_frozen();
"""

#: tasks: 终态(completed/cancelled/timed_out/failed)禁止再迁移状态(01 §7.2)
TASKS_TERMINAL_TRIGGER_SQL: str = """\
-- tasks: 终态任务不可迁移状态
CREATE FUNCTION tg_tasks_terminal() RETURNS trigger AS $$
BEGIN
  IF OLD.status IN ('completed','cancelled','timed_out','failed') AND NEW.status <> OLD.status THEN
    RAISE EXCEPTION '终态任务不可迁移状态';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER tg_tasks_terminal BEFORE UPDATE ON tasks
  FOR EACH ROW EXECUTE FUNCTION tg_tasks_terminal();
"""

#: projects / project_versions: 项目计算基线(0.6.5 事项 1)创建时一次性固定,
#: 创建后不可修改。行级 UPDATE 若改变任一基线列(含摘要)即拒绝 —— 基线是
#: 序列预备、装配与历史任务解释的权威事实, 不允许运行期篡改。
PROJECT_BASELINE_IMMUTABLE_TRIGGER_SQL: str = """\
-- projects: 基线列不可修改(0.6.5 事项 1)
CREATE FUNCTION tg_projects_baseline_immutable() RETURNS trigger AS $$
BEGIN
  IF OLD.baseline_resolution IS DISTINCT FROM NEW.baseline_resolution
     OR OLD.baseline_leap_year IS DISTINCT FROM NEW.baseline_leap_year
     OR OLD.baseline_scenario_mode IS DISTINCT FROM NEW.baseline_scenario_mode THEN
    RAISE EXCEPTION '项目计算基线创建后不可修改';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER tg_projects_baseline_immutable BEFORE UPDATE ON projects
  FOR EACH ROW EXECUTE FUNCTION tg_projects_baseline_immutable();
-- project_versions: 版本固化基线同样不可修改(版本表整体只 INSERT)
CREATE FUNCTION tg_project_versions_baseline_immutable() RETURNS trigger AS $$
BEGIN
  IF OLD.baseline_resolution IS DISTINCT FROM NEW.baseline_resolution
     OR OLD.baseline_leap_year IS DISTINCT FROM NEW.baseline_leap_year
     OR OLD.baseline_scenario_mode IS DISTINCT FROM NEW.baseline_scenario_mode THEN
    RAISE EXCEPTION '项目版本基线固化后不可修改';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER tg_project_versions_baseline_immutable BEFORE UPDATE ON project_versions
  FOR EACH ROW EXECUTE FUNCTION tg_project_versions_baseline_immutable();
"""



def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖: 提供请求级数据库会话, 请求结束自动关闭。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    """幂等初始化数据库: 建表 + 版本化迁移 + 约束迁移 + 不可变触发器 + 种子管理员。

    - 先导入模型模块, 确保全部表注册到 Base.metadata;
    - create_all 只建不存在的表, 重复调用无副作用;
    - apply_migrations: 在基础 schema 之上执行版本化 schema 迁移(宪法 §11,
      台账幂等)，由各版本明确登记并补充其拥有的当前结构与约束;
    - _migrate_constraints: 既有表约束随模型演进做幂等 ALTER
      (如 ck_tasks_type 增补 'analysis', 03 §9.7);
    - _deploy_immutable_triggers: 不可变表(01 §11)部署"禁 UPDATE/DELETE"触发器
      与 REVOKE(仅 PostgreSQL; SQLite 测试库跳过)。
    """
    # Wave2A: ORM 表真相收归各领域 persistence, 此处集中导入以完成
    # Base.metadata 注册(空库 create_all 全量建表)。过渡位: Wave3 迁入 bootstrap 集中装配。
    from iesplan.audit import persistence as _audit_tables  # noqa: F401
    from iesplan.configuration import persistence as _configuration_tables  # noqa: F401
    from iesplan.dataset import persistence as _dataset_tables  # noqa: F401
    from iesplan.identity import persistence as _identity_tables  # noqa: F401
    from iesplan.model import persistence as _model_tables  # noqa: F401
    from iesplan.package import persistence as _package_tables  # noqa: F401
    from iesplan.project import persistence as _project_tables  # noqa: F401
    from iesplan.results import persistence as _results_tables  # noqa: F401
    from iesplan.storage import persistence as _storage_tables  # noqa: F401
    from iesplan.tasks import persistence as _tasks_tables  # noqa: F401
    from iesplan.migrations import apply_migrations

    Base.metadata.create_all(bind=engine)
    apply_migrations(engine)
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
        # 0.7.0: 计算快照持久化规范装配产物二件套(文本仅校验字头，不做 SHA)。
        # 旧快照不能伪造回执，因此升级列保持可空；Worker 对缺失成员执行阻断，新快照始终完整写入。
        _add_column_if_missing(conn, "calc_snapshots", "canonical_assembly_text", "TEXT")
        _add_column_if_missing(conn, "calc_snapshots", "assembly_receipt", "JSONB")

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
        # 0.8.0: 剔除过度设计(复制项目/转移所有权/共享成员/管理员访问授权)后,
        # projects.admin_access 列不再有语义, 幂等删列(旧库已删则跳过);
        # project_members / ownership_transfers 两张废弃表一并删除
        # (项目权限以 projects.owner_id 为唯一权威, 无历史消费方)。
        conn.execute(sa_text("ALTER TABLE projects DROP COLUMN IF EXISTS admin_access"))
        conn.execute(sa_text("DROP TABLE IF EXISTS project_members"))
        conn.execute(sa_text("DROP TABLE IF EXISTS ownership_transfers"))


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
    # 不可变触发器 DDL 常量见本模块(Wave2A 由 iesplan.models.immutable_triggers 迁入)。

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
    from iesplan.identity.persistence import Credential, Role, User, UserRole

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
