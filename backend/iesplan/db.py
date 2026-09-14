"""数据库引擎、会话管理与初始化。

- engine / SessionLocal: 全局单例(连接池 + 预检)。
- Base: 全部 ORM 模型的声明基类 + 共享列基元(JSONB/正则 CHECK/主键构造)。
- 通用触发器 DDL 构造与部署执行(纯函数, 不拥有任何业务表名与规则)。
- init_db(): 幂等建表(create_all) + 部署调用方传入的触发器语句
  (未发布前无版本化迁移, 当前 schema 从空库直接建立;
  种子身份数据归 application.identity.seed_builtin_admin, 本模块不留业务种子)。

本模块只保留连接 / Session / Base 无业务状态基础设施:

- 不集中注册任何领域 metadata: 组合根静态导入各领域公开门面，门面加载
  自己的 persistence 后自然完成声明注册；不设无行为的安装钩子。
- 不拥有跨领域表名与触发器规则: 规则归各自领域 persistence 的
  ``install_triggers()`` 钩子所有, 本模块只提供无业务状态的 DDL 构造
  (``immutable_trigger_sql`` / ``immutable_revoke_sql`` /
  ``drop_trigger_function_sql``)与执行(``deploy_trigger_statements``)帮助函数。
- 不从业务领域反向导入任何 contracts: 正则等唯一权威留在各领域
  contracts, 由拥有者 persistence 直接引用(本模块仅保留 ``HASH64_RE``
  这类无业务归属的通用格式)。
"""

from __future__ import annotations

from collections.abc import Iterable

import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy import text as sa_text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import DeclarativeBase, MappedColumn, mapped_column, sessionmaker

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

#: 64 位小写十六进制(对象 id 等随机标识格式; 无业务归属的通用格式)
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
# 通用触发器 DDL 构造与部署执行(无业务状态基础设施)。
#
# 本节函数均为纯构造/执行帮助: 不拥有任何业务表名与规则, 表名与规则由
# 各领域 persistence 的 ``install_triggers()`` 钩子提供, 组合根编排收集后
# 经 ``init_db`` / ``deploy_trigger_statements`` 部署。
# ---------------------------------------------------------------------------


def immutable_trigger_sql(table: str) -> str:
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


def immutable_revoke_sql(table: str) -> str:
    """生成单张不可变表的 REVOKE DDL(三道防线第2层: 不授予任何角色 UPDATE/DELETE)。"""
    return f"REVOKE UPDATE, DELETE ON {table} FROM PUBLIC;"


def drop_trigger_function_sql(func: str) -> str:
    """生成触发器函数的幂等删除语句(重建前先 DROP, 保证重复部署无副作用)。"""
    return f"DROP FUNCTION IF EXISTS {func}() CASCADE"


def deploy_trigger_statements(statements: Iterable[str]) -> None:
    """部署触发器语句(01 第0节三道防线第 2/3 层)。

    调用方(组合根)传入各领域 ``install_triggers()`` 钩子收集的语句,
    按序逐条执行(含各域自带的幂等 DROP, 重复执行无副作用)。

    仅 PostgreSQL 执行; SQLite 测试库不解析 plpgsql 触发器语法, 直接跳过
    (create_all 每次全量重建, 无生产数据, 不可变性由应用层唯一写入单元保证)。
    """
    if not settings.db_url.startswith("postgresql"):
        return
    pending = [stmt for stmt in statements if stmt and stmt.strip()]
    if not pending:
        return
    with engine.begin() as conn:
        for stmt in pending:
            conn.execute(sa_text(stmt))


def init_db(trigger_statements: Iterable[str] | None = None) -> None:
    """幂等初始化数据库: 建表 + 部署触发器。

    - create_all 只建不存在的表, 重复调用无副作用;
    - 本函数不注册领域 metadata；调用前须已由组合根加载领域公开门面；
    - ``trigger_statements`` 为各领域 ``install_triggers()`` 钩子收集的
      部署语句(组合根编排收集后传入); 为空时只建表, 不部署触发器。
    - 项目未发布, 无版本化迁移: 当前 schema 从空库直接建立,
      不保留旧库 ALTER/DROP/回填分支与 schema_migrations 台账;
      正式发布后的 schema 变更走版本化 migration(宪法 §11);
      种子管理员改由 application.identity.seed_builtin_admin 负责。
    """
    Base.metadata.create_all(bind=engine)
    deploy_trigger_statements(trigger_statements or ())
