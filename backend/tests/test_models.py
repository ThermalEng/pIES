"""模型层测试: 模型注册、约束字符串、关键字段与 SQLite create_all 全链路。

领域结构依据: manual/developer-guide/zh-CN/domain-model.md。
SQLite 无法解析 PostgreSQL 正则运算符, 正则 CHECK 在 SQLite 下编译为恒真,
因此约束字符串断言使用 PostgreSQL 方言编译验证。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlalchemy.dialects import postgresql

from iesplan import models
from iesplan.db import Base
from iesplan.models.immutable_triggers import (
    ALL_IMMUTABLE_REVOKE_DDL,
    ALL_IMMUTABLE_TRIGGER_DDL,
    IMMUTABLE_TABLES,
)

#: 01-db-schema.md 全部 39 张表 + 应用级设置表(app_settings, 身份域扩展)
#: (0.8.0 剔除共享成员/所有权转移: project_members / ownership_transfers 已移除)
ALL_TABLES: tuple[str, ...] = (
    "users", "roles", "user_roles", "credentials", "window_sessions", "auth_events",
    "app_settings",
    "admin_maintenance_actions",
    "projects", "drafts", "project_versions", "version_refs",
    "project_models", "project_model_sequences",
    "model_templates", "model_template_revisions",
    "system_graphs", "devices", "ports", "connections",
    "datasets", "dataset_versions", "dataset_files",
    "calc_configs", "calc_snapshots",
    "tasks", "task_attempts", "task_leases", "task_progress", "task_diagnostics", "compute_slots",
    "evidence_packages", "result_assessments", "result_index", "result_selections", "reports",
    "uncertainty_snapshots", "sample_tasks", "sample_records",
    "objects", "object_refs", "audit_log", "import_proposals", "retention_rules",
)

#: 关键列抽查: 表名 -> 必须存在的列(取文档有代表性的列, 含类型敏感列)
KEY_COLUMNS: dict[str, set[str]] = {
    "users": {
        "id",
        "username",
        "display_name",
        "email",
        "status",
        "credential_version",
        "is_system",
        "auth_subject",
    },
    "roles": {"code", "name", "is_system"},
    "user_roles": {"user_id", "role_id", "granted_by", "granted_at", "revoked_at"},
    "credentials": {"user_id", "credential_type", "secret_hash", "strength_score", "requires_change"},
    "window_sessions": {
        "session_token_hash", "user_id", "credential_version_at_issue", "replaced_by_session_id", "expires_at"
    },
    "auth_events": {"user_id", "session_id", "event_type", "occurred_at", "detail"},
    "admin_maintenance_actions": {"action_type", "performed_by", "status", "params", "result"},
    "projects": {
        "name", "owner_id", "currency", "fixed_utc_offset_minutes", "current_draft_id", "current_version_id"
    },
    "drafts": {"project_id", "revision", "content_hash", "parent_draft_id", "is_current"},
    "project_versions": {"project_id", "version_no", "content_hash", "fixed_utc_offset_minutes", "reason"},
    "version_refs": {"project_version_id", "ref_type", "object_id", "ref_hash"},
    "system_graphs": {"project_id", "draft_id", "project_version_id", "graph_hash"},
    "devices": {"graph_id", "device_type", "kind", "params", "model_fidelity"},
    "ports": {"device_id", "port_type", "direction", "capacity"},
    "connections": {"graph_id", "from_port_id", "to_port_id", "conn_type", "loss_rate"},
    "datasets": {"project_id", "name", "status", "default_license"},
    "dataset_versions": {"dataset_id", "version_no", "timeline", "fields", "units", "content_hash"},
    "dataset_files": {"dataset_version_id", "object_id", "file_kind", "format", "row_count", "size_bytes"},
    "calc_configs": {
        "project_id", "name", "params", "variables", "min_irr", "algorithm", "random_seed", "version"
    },
    "calc_snapshots": {
        "project_version_id", "dataset_version_ids", "calc_config_snapshot", "random_seed",
        "content_hash", "canonical_assembly_text", "assembly_sha256", "assembly_receipt",
    },
    "tasks": {"project_id", "type", "status", "business_outcome", "idempotency_key", "max_attempts"},
    "task_attempts": {"task_id", "attempt_no", "worker_id", "status", "stop_reason"},
    "task_leases": {"attempt_id", "lease_token", "renewed_at", "expires_at", "status"},
    "task_progress": {"attempt_id", "progress_percent", "stage", "detail"},
    "task_diagnostics": {"task_id", "attempt_id", "level", "code", "message"},
    "compute_slots": {"pool_name", "status", "capacity", "in_use", "current_attempt_id"},
    "evidence_packages": {"task_id", "attempt_id", "calc_snapshot_id", "object_id", "content_hash"},
    "result_assessments": {"evidence_package_id", "assessor", "dimension_physical", "overall_score"},
    "result_index": {
        "project_id", "project_version_id", "evidence_package_id", "assessment_id", "result_hash", "is_latest"
    },
    "result_selections": {"project_id", "result_index_id", "selected_by", "is_current"},
    "reports": {"project_id", "report_type", "object_id", "content_hash", "generated_by_task_id", "status"},
    "uncertainty_snapshots": {"calc_snapshot_id", "method", "n_samples", "random_seed", "distributions"},
    "sample_tasks": {
        "uncertainty_snapshot_id", "parent_task_id", "parent_sample_id", "sample_index", "depth"
    },
    "sample_records": {"sample_task_id", "variable_name", "value", "unit"},
    "objects": {"oid", "sha256", "size_bytes", "storage_path", "ref_count", "quota_bytes"},
    "object_refs": {"object_id", "ref_type", "ref_entity_type", "ref_entity_id"},
    "audit_log": {"entity_type", "entity_id", "action", "actor_type", "before", "after"},
    "import_proposals": {"project_id", "proposer_id", "source_type", "source_hash", "status"},
    "retention_rules": {"entity_type", "object_kind", "retention_days", "apply_to", "status"},
}

#: 部分唯一索引(唯一 + postgresql_where)抽查
PARTIAL_UNIQUE_INDEXES: dict[str, str] = {
    "user_roles": "uq_user_roles_current",
    "credentials": "uq_credentials_active_password",
    "window_sessions": "uq_window_sessions_one_active",
    "drafts": "uq_drafts_current",
    "task_leases": "uq_task_leases_one_active",
    "compute_slots": "uq_compute_slots_attempt",
    "result_index": "uq_result_index_latest",
    "result_selections": "uq_result_selections_current",
    "sample_tasks": "uq_sample_tasks_top",
}

#: 不可变表清单(01 §11 权威列表)
EXPECTED_IMMUTABLE_TABLES: set[str] = {
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
}


def _find_constraint(table: sa.Table, name: str) -> sa.Constraint | None:
    """按名字在表约束集合中查找(Table.constraints 是 set)。"""
    for constraint in table.constraints:
        if constraint.name == name:
            return constraint
    return None


def _find_index(table: sa.Table, name: str) -> Index | None:
    """按名字在表索引集合中查找(Table.indexes 是 set)。"""
    for index in table.indexes:
        if index.name == name:
            return index
    return None


def _check_sqltext(table: sa.Table, fragment: str) -> bool:
    """断言表内存在 sqltext 含指定片段的 CHECK 约束(忽略空白差异)。"""
    target = fragment.replace(" ", "")
    for constraint in table.constraints:
        if isinstance(constraint, CheckConstraint):
            if str(constraint.sqltext).replace(" ", "") == target:
                return True
    return False


def test_models_import_and_all_tables_registered() -> None:
    """全部 43 张表均注册到 Base.metadata, 无多余/缺失。"""
    registered = set(Base.metadata.tables)
    missing = set(ALL_TABLES) - registered
    extra = registered - set(ALL_TABLES)
    assert set(ALL_TABLES) == registered, f"缺失: {missing}, 多余: {extra}"


def test_key_columns_exist() -> None:
    """每张表的关键列与文档一致。"""
    for table_name, columns in KEY_COLUMNS.items():
        table = Base.metadata.tables[table_name]
        actual = set(table.c.keys())
        missing = columns - actual
        assert not missing, f"{table_name} 缺少列: {missing}"


def test_unique_constraints() -> None:
    """文档规定的唯一约束存在。"""
    cases: dict[tuple[str, str], tuple[str, ...]] = {
        ("users", "uq_users_username"): ("username",),
        ("users", "uq_users_email"): ("email",),
        ("roles", "uq_roles_code"): ("code",),
        ("user_roles", "uq_user_roles_grant"): ("user_id", "role_id", "granted_at"),
        ("window_sessions", "uq_window_sessions_token"): ("session_token_hash",),
        ("drafts", "uq_drafts_revision"): ("project_id", "revision"),
        ("project_versions", "uq_project_versions_version"): ("project_id", "version_no"),
        ("version_refs", "uq_version_refs_ref"): ("project_version_id", "ref_type", "object_id"),
        ("devices", "uq_devices_graph_name"): ("graph_id", "name"),
        ("ports", "uq_ports_device_name"): ("device_id", "name"),
        ("connections", "uq_connections_ends"): ("graph_id", "from_port_id", "to_port_id", "conn_type"),
        ("dataset_versions", "uq_dataset_versions_version"): ("dataset_id", "version_no"),
        ("dataset_files", "uq_dataset_files_object"): ("dataset_version_id", "object_id"),
        ("task_attempts", "uq_task_attempts_no"): ("task_id", "attempt_no"),
        ("task_leases", "uq_task_leases_token"): ("lease_token",),
        ("sample_records", "uq_sample_records_variable"): ("sample_task_id", "variable_name"),
        ("objects", "uq_objects_oid"): ("oid",),
        ("objects", "uq_objects_sha256"): ("sha256",),
        ("object_refs", "uq_object_refs_ref"): ("object_id", "ref_type", "ref_entity_type", "ref_entity_id"),
        ("retention_rules", "uq_retention_rules_key"): ("entity_type", "object_kind", "apply_to"),
    }
    for (table_name, uc_name), cols in cases.items():
        table = Base.metadata.tables[table_name]
        uc = _find_constraint(table, uc_name)
        assert isinstance(uc, UniqueConstraint), f"{table_name}.{uc_name} 缺失或类型错误"
        actual = set(uc.columns.keys())
        assert actual == set(cols), f"{table_name}.{uc_name} 列不匹配: {list(uc.columns.keys())}"


def test_partial_unique_indexes() -> None:
    """部分唯一索引存在、唯一、且带 postgresql_where。"""
    for table_name, index_name in PARTIAL_UNIQUE_INDEXES.items():
        table = Base.metadata.tables[table_name]
        index = _find_index(table, index_name)
        assert isinstance(index, Index), f"{table_name}.{index_name} 索引缺失"
        assert index.unique is True, f"{table_name}.{index_name} 应唯一"
        where = index.dialect_options["postgresql"].get("where")
        assert where is not None, f"{table_name}.{index_name} 缺少 postgresql_where"


def _pg_ddl(table: sa.Table) -> str:
    """用 PostgreSQL 方言编译 CREATE TABLE DDL(供 PG 特有渲染校验)。"""
    return str(sa.schema.CreateTable(table).compile(dialect=postgresql.dialect()))


def test_regex_constraint_strings() -> None:
    """正则 CHECK 的 sqltext 与文档一致。"""
    users = Base.metadata.tables["users"]
    assert _check_sqltext(users, "username ~ '^[a-z0-9_]{3,32}$'")
    assert _check_sqltext(users, "email IS NULL OR email ~ '^[^@\\s]+@[^@\\s]+$'")
    objects = Base.metadata.tables["objects"]
    assert _check_sqltext(objects, "oid ~ '^[0-9a-f]{64}$'")
    assert _check_sqltext(objects, "sha256 ~ '^[0-9a-f]{64}$'")
    tasks = Base.metadata.tables["tasks"]
    assert _check_sqltext(tasks, "idempotency_key IS NULL OR idempotency_key ~ '^[A-Za-z0-9._:-]{1,128}$'")
    snapshots = Base.metadata.tables["calc_snapshots"]
    assert _check_sqltext(snapshots, "content_hash ~ '^[0-9a-f]{64}$'")


def test_enum_check_constraints() -> None:
    """枚举 CHECK 的 sqltext 与文档一致(抽查)。"""
    users = Base.metadata.tables["users"]
    assert _check_sqltext(users, "status IN ('active','disabled','locked')")
    assert _check_sqltext(users, "fixed_utc_offset_minutes BETWEEN -720 AND 840")
    tasks = Base.metadata.tables["tasks"]
    assert _check_sqltext(
        tasks, "status IN ('queued','running','completed','cancelling','cancelled','timed_out','failed')"
    )
    assert _check_sqltext(tasks, "max_attempts BETWEEN 1 AND 10")
    connections = Base.metadata.tables["connections"]
    assert _check_sqltext(
        connections, "conn_type IN ('electric_line','thermal_pipe','cooling_pipe','fuel_pipe','data_link')"
    )
    assert _check_sqltext(connections, "loss_rate BETWEEN 0 AND 1")
    graphs = Base.metadata.tables["system_graphs"]
    assert _check_sqltext(graphs, "(draft_id IS NULL) <> (project_version_id IS NULL)")
    slots = Base.metadata.tables["compute_slots"]
    assert _check_sqltext(slots, "in_use <= capacity")


def test_immutable_tables_and_triggers() -> None:
    """不可变表清单与触发器 DDL 常量完整。"""
    assert set(IMMUTABLE_TABLES) == EXPECTED_IMMUTABLE_TABLES
    for table in IMMUTABLE_TABLES:
        assert f"CREATE TRIGGER tg_{table}_no_update BEFORE UPDATE ON {table}" in ALL_IMMUTABLE_TRIGGER_DDL
        assert f"CREATE TRIGGER tg_{table}_no_delete BEFORE DELETE ON {table}" in ALL_IMMUTABLE_TRIGGER_DDL
        assert f"CREATE FUNCTION tg_{table}_immutable()" in ALL_IMMUTABLE_TRIGGER_DDL
        assert f"REVOKE UPDATE, DELETE ON {table} FROM PUBLIC;" in ALL_IMMUTABLE_REVOKE_DDL
    # 专项触发器(版本图冻结 / 配置冻结 / 任务终态)
    from iesplan.models.immutable_triggers import (
        CALC_CONFIGS_FROZEN_TRIGGER_SQL,
        SYSTEM_GRAPHS_FROZEN_TRIGGER_SQL,
        TASKS_TERMINAL_TRIGGER_SQL,
    )

    assert "BEFORE UPDATE ON system_graphs" in SYSTEM_GRAPHS_FROZEN_TRIGGER_SQL
    assert "BEFORE UPDATE ON calc_configs" in CALC_CONFIGS_FROZEN_TRIGGER_SQL
    assert "BEFORE UPDATE ON tasks" in TASKS_TERMINAL_TRIGGER_SQL


def test_create_all_on_sqlite_and_roundtrip() -> None:
    """SQLite :memory: 上 create_all 全表成功, 关键表可读写。"""
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    registered = sa.inspect(engine).get_table_names()
    assert set(ALL_TABLES) <= set(registered)

    with engine.begin() as conn:
        # users 往返(含服务端默认值)
        conn.execute(
            sa.text(
                "INSERT INTO users (username, display_name) VALUES ('alice', 'Alice')"
            )
        )
        row = conn.execute(sa.text("SELECT username, status, locale, credential_version FROM users")).one()
        assert row.username == "alice"
        assert row.status == "active"
        assert row.locale == "zh-CN"
        assert row.credential_version == 0
        # credentials(JSONB 回退)
        conn.execute(
            sa.text(
                "INSERT INTO credentials (user_id, credential_type, secret_hash, strength_score) "
                "VALUES (1, 'password', 'x', 100)"
            )
        )
        # objects + object_refs
        conn.execute(
            sa.text(
                "INSERT INTO objects (oid, sha256, size_bytes) "
                "VALUES ('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 10)"
            )
        )
        # tasks(枚举/幂等键)
        conn.execute(
            sa.text(
                "INSERT INTO tasks (project_id, type, status, idempotency_key, requested_by) "
                "VALUES (1, 'calc', 'queued', 'key-1', 1)"
            )
        )
        # calc_snapshots(BIGINT[] 回退 JSON)
        conn.execute(
            sa.text(
                "INSERT INTO calc_snapshots (project_version_id, calc_config_snapshot, random_seed, "
                "dataset_version_ids, content_hash, created_by) VALUES (1, '{}', 42, '[1,2]', "
                "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 1)"
            )
        )
        # window_sessions(INET 回退, UUID 用 task_leases 验证)
        conn.execute(
            sa.text(
                "INSERT INTO window_sessions (session_token_hash, user_id, credential_version_at_issue, "
                "expires_at, ip) VALUES ('tok', 1, 0, '2030-01-01T00:00:00Z', '127.0.0.1')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO task_leases (attempt_id, lease_token, expires_at, status) "
                "VALUES (1, :tok, '2030-01-01T00:00:00Z', 'active')"
            ),
            {"tok": str(uuid.uuid4())},
        )


def test_orm_insert_roundtrip() -> None:
    """ORM 方式插入读取(时间列时区、JSON 回退)。"""
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sa.orm.Session(engine) as session:
        user = models.User(username="bob", display_name="Bob")
        session.add(user)
        session.flush()
        obj = models.StoredObject(
            oid="c" * 64,
            sha256="c" * 64,
            size_bytes=1,
            media_type="application/json",
        )
        session.add(obj)
        session.flush()
        event = models.AuthEvent(
            user_id=user.id,
            event_type="login_success",
            ip="127.0.0.1",
            detail={"ok": True},
            occurred_at=datetime.now(UTC),
        )
        session.add(event)
        session.commit()
        # 回读校验
        fetched = session.get(models.User, user.id)
        assert fetched is not None and fetched.username == "bob"
        assert fetched.created_at is not None
        events = session.query(models.AuthEvent).filter_by(user_id=user.id).all()
        assert len(events) == 1
        assert events[0].detail == {"ok": True}


def test_check_constraint_objects_present() -> None:
    """关键表均带文档规定的 CHECK 约束;部分唯一索引在 SQLite 上也带 sqlite_where。"""
    users = Base.metadata.tables["users"]
    assert any(isinstance(c, CheckConstraint) for c in users.constraints)
    tasks = Base.metadata.tables["tasks"]
    assert any(isinstance(c, CheckConstraint) for c in tasks.constraints)
    objects = Base.metadata.tables["objects"]
    assert any(isinstance(c, CheckConstraint) for c in objects.constraints)
    idx = _find_index(Base.metadata.tables["window_sessions"], "uq_window_sessions_one_active")
    assert idx is not None
    assert idx.dialect_options["sqlite"].get("where") is not None
    # PostgreSQL DDL 渲染抽查: 部分唯一索引带 WHERE 子句
    pg_ddl = str(
        sa.schema.CreateIndex(idx).compile(dialect=postgresql.dialect())
    )
    assert "WHERE status = 'active'" in pg_ddl
    # 表达式唯一索引(uq_datasets_name)按文档渲染
    datasets = Base.metadata.tables["datasets"]
    ds_idx = _find_index(datasets, "uq_datasets_name")
    assert ds_idx is not None
    pg_ddl_ds = str(sa.schema.CreateIndex(ds_idx).compile(dialect=postgresql.dialect()))
    assert "CREATE UNIQUE INDEX uq_datasets_name ON datasets (coalesce(project_id, 0), name)" in pg_ddl_ds
