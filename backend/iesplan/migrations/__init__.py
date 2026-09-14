"""数据库 schema 版本化迁移包(预留位)。

项目尚未正式发布: 当前数据库只从空库按现行 ORM 定义建立
(``Base.metadata.create_all`` 建现行表 + 约束; 不可变触发器规则归各领域
persistence 的 ``install_triggers()`` 钩子所有, 由组合根编排收集后经
``iesplan.db.deploy_trigger_statements`` 在 PostgreSQL 下部署)。
本包不保留任何历史迁移脚本、``schema_migrations`` 台账、
``ALTER``/``DROP``/回填或重复 ``CREATE TABLE`` SQL。

正式发布后, schema 变更必须通过版本化 migration(架构宪法 §11 与
部署手册升级流程): 新迁移按 ``<版本>_<名称>`` 命名在此登记,
``init_db`` 在建表之后调用运行器执行。
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
