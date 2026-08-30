"""不可变表触发器 DDL（字符串常量，三道防线见 ARCHITECTURE_CONSTITUTION.md §11 数据库与持久化/§16 安全与审计 与 modules/persistence.md）。

三道防线:
1. 应用层只允许该实体的唯一写入单元(U01-U16)发 INSERT;
2. ``REVOKE UPDATE, DELETE ON <table> FROM PUBLIC`` 且不授予任何角色该表 UPDATE/DELETE;
3. 触发器 ``tg_<table>_no_update`` / ``tg_<table>_no_delete`` 在 BEFORE UPDATE|DELETE
   时 RAISE EXCEPTION。

本模块提供 SQL 文本常量, 由 ``db.init_db`` 的 ``_deploy_immutable_triggers`` 在
PostgreSQL 下幂等执行(先 DROP FUNCTION IF EXISTS ... CASCADE 再重建), SQLite
测试库跳过。不可变表清单以本模块常量与迁移为准（见 modules/persistence.md §必须遵循的规范）。
"""

from __future__ import annotations

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
     OR OLD.baseline_scenario_mode IS DISTINCT FROM NEW.baseline_scenario_mode
     OR OLD.baseline_sha256 IS DISTINCT FROM NEW.baseline_sha256 THEN
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
     OR OLD.baseline_scenario_mode IS DISTINCT FROM NEW.baseline_scenario_mode
     OR OLD.baseline_sha256 IS DISTINCT FROM NEW.baseline_sha256 THEN
    RAISE EXCEPTION '项目版本基线固化后不可修改';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER tg_project_versions_baseline_immutable BEFORE UPDATE ON project_versions
  FOR EACH ROW EXECUTE FUNCTION tg_project_versions_baseline_immutable();
"""
