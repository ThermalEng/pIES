"""数据库引擎、会话管理与初始化。

- engine / SessionLocal: 全局单例(连接池 + 预检)。
- Base: 全部 ORM 模型的声明基类 + 共享列基元(JSONB/正则 CHECK/主键构造)。
- 不可变表触发器 DDL 常量(部署见 _deploy_immutable_triggers)。
- get_db(): FastAPI 请求级依赖。
- init_db(): 幂等建表(create_all) + 版本化迁移 + 不可变触发器。
  当前 schema 从空库直接建立, 不保留旧库 ALTER/DROP/回填分支;
  种子身份数据归 application.identity.seed_builtin_admin, 本模块不留业务种子。

本模块只保留连接 / Session / Base 基础设施;ORM 表定义归各领域 persistence 所有,
``register_domain_metadata`` 集中导入注册(由组合根 ``iesplan.bootstrap``
在 ``assemble_*`` 中显式调用, ``init_db`` 内部亦调用以兼容直接调用)。
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
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


def register_domain_metadata() -> None:
    """基础设施函数: 导入各领域 persistence, 完成 Base.metadata 注册。

    只做导入注册, 不建表、不迁移、不种子。由组合根(``iesplan.bootstrap``)
    在 ``assemble_*`` 中显式调用后再做 create_all/迁移/种子;
    ``init_db`` 内部亦调用本函数(测试兼容: 直接调 ``init_db`` 仍全量建表)。
    """
    # ORM 表真相收归各领域 persistence, 此处集中导入以完成
    # Base.metadata 注册(空库 create_all 全量建表)。
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


def init_db() -> None:
    """幂等初始化数据库: 建表 + 版本化迁移 + 不可变触发器。

    - 先经 ``register_domain_metadata`` 确保全部表注册到 Base.metadata;
    - create_all 只建不存在的表, 重复调用无副作用;
    - apply_migrations: 在基础 schema 之上执行版本化 schema 迁移(宪法 §11,
      台账幂等)，由各版本明确登记并补充其拥有的当前结构与约束;
    - _deploy_immutable_triggers: 不可变表(01 §11)部署"禁 UPDATE/DELETE"触发器
      与 REVOKE(仅 PostgreSQL; SQLite 测试库跳过)。
    - 旧库 ALTER/DROP/回填分支已删除, 当前 schema 从空库直接建立;
      种子管理员改由 application.identity.seed_builtin_admin 负责。
    """
    register_domain_metadata()
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
