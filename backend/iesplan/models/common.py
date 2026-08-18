"""模型层共享工具: 跨方言类型、正则常量与通用构造。

SQLite(测试)与 PostgreSQL(生产)之间的 DDL 差异在这里统一收口:

- ``JSONB`` / ``BIGINT[]`` / ``INET`` 等 PostgreSQL 专有类型在 SQLite 回退为通用类型;
- 正则 CHECK(``~`` 运算符)是 PostgreSQL 语法, SQLite 解析即报错,
  因此用 ``PgRegexCheck`` 包装: PostgreSQL 按文档原样输出, SQLite 编译为恒真
  ``CHECK (1=1)``, 保证 create_all 可用;正则语义校验由应用层保证。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import MappedColumn, mapped_column

#: 内容寻址哈希(sha256 十六进制, 64 位)
HASH64_RE: str = "^[0-9a-f]{64}$"
#: 用户名(小写字母/数字/下划线, 3-32 位)
USERNAME_RE: str = "^[a-z0-9_]{3,32}$"
#: 邮箱格式
EMAIL_RE: str = r"^[^@\s]+@[^@\s]+$"
#: 幂等键(字母/数字/._:-)
IDEMPOTENCY_KEY_RE: str = "^[A-Za-z0-9._:-]{1,128}$"


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
