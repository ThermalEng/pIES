"""快照与任务域(U07 任务 / U08 快照写入单元)。

含: calc_configs / calc_snapshots / tasks / task_attempts / task_leases /
task_progress / task_diagnostics / compute_slots。对应 01-db-schema.md 第6、7节。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from iesplan.db import Base
from iesplan.models.common import HASH64_RE, IDEMPOTENCY_KEY_RE, JSONB, BigIntArray, bigint_pk, regex_check

#: 任务状态枚举(01 §7.2, 与契约第3节一致)
TASK_STATUSES: tuple[str, ...] = (
    "queued", "running", "completed", "cancelling", "cancelled", "timed_out", "failed"
)

#: 业务结局枚举(01 §7.2, 与契约第3节一致)
TASK_OUTCOMES: tuple[str, ...] = (
    "normal_completion", "no_recommendation", "no_feasible_multi_objective",
    "partial_batch", "restricted_results", "insufficient_evidence",
)


class CalcConfig(Base):
    """计算配置(参数/变量/目标/约束/算法/容差/种子, 01 §6.1)。"""

    __tablename__ = "calc_configs"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'"))
    variables: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'[]'"))
    objectives: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'[]'"))
    constraints: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'[]'"))
    min_irr: Mapped[float | None] = mapped_column(Numeric(6, 4))
    algorithm: Mapped[str | None] = mapped_column(Text)
    solver: Mapped[str | None] = mapped_column(Text)
    tolerances: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'"))
    random_seed: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("1"))
    updated_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "min_irr IS NULL OR min_irr BETWEEN 0 AND 1", name="ck_calc_configs_min_irr"
        ),
        CheckConstraint(
            "algorithm IS NULL OR algorithm IN ('milp','lp','heuristic','ga','exhaustive','custom')",
            name="ck_calc_configs_algorithm",
        ),
        CheckConstraint("status IN ('draft','frozen')", name="ck_calc_configs_status"),
        Index(
            "uq_calc_configs_name_version", "project_id", "name", "version", unique=True
        ),
        Index("idx_calc_configs_project", "project_id", "name"),
    )


class CalcSnapshot(Base):
    """计算快照(任务唯一输入, 不可变, 01 §7.1)。"""

    __tablename__ = "calc_snapshots"

    id: Mapped[int] = bigint_pk()
    project_version_id: Mapped[int] = mapped_column(ForeignKey("project_versions.id"), nullable=False)
    dataset_version_ids: Mapped[list[int]] = mapped_column(
        BigIntArray, nullable=False, server_default=sa.text("'{}'")
    )
    calc_config_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    program_version: Mapped[str | None] = mapped_column(Text)
    extension_versions: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'"))
    random_seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tolerances: Mapped[dict | None] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    #: 0.7.0 之前的非规范 YAML，仅保留旧快照审计；新快照不得写入/消费。
    assembly_text: Mapped[str | None] = mapped_column(Text)
    #: 规范装配文本 + SHA-256 + 确定性校验回执（三件套）。旧快照升级后可为 NULL，
    #: Worker 必须拒绝缺任一成员的快照；所有新快照由统一校验入口完整写入。
    canonical_assembly_text: Mapped[str | None] = mapped_column(Text)
    assembly_sha256: Mapped[str | None] = mapped_column(Text)
    assembly_receipt: Mapped[dict | None] = mapped_column(JSONB)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        regex_check(f"content_hash ~ '{HASH64_RE}'", name="ck_calc_snapshots_content_hash"),
        regex_check(
            f"assembly_sha256 IS NULL OR assembly_sha256 ~ '{HASH64_RE}'",
            name="ck_calc_snapshots_assembly_sha256",
        ),
        Index("idx_calc_snapshots_version", "project_version_id", sa.text("created_at DESC")),
    )


class Task(Base):
    """任务(状态机、类型、业务结局、幂等键, 01 §7.2)。"""

    __tablename__ = "tasks"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    business_outcome: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    calc_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("calc_snapshots.id"))
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=sa.text("0"))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("3"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "type IN ('calc','optimization','uncertainty','analysis',"
            "'import','export','report','dataset_build')",
            name="ck_tasks_type",
        ),
        CheckConstraint(
            f"status IN {TASK_STATUSES!r}", name="ck_tasks_status"
        ),
        CheckConstraint(
            f"business_outcome IS NULL OR business_outcome IN {TASK_OUTCOMES!r}",
            name="ck_tasks_outcome",
        ),
        regex_check(
            f"idempotency_key IS NULL OR idempotency_key ~ '{IDEMPOTENCY_KEY_RE}'",
            name="ck_tasks_idempotency_key",
        ),
        CheckConstraint("max_attempts BETWEEN 1 AND 10", name="ck_tasks_max_attempts"),
        # RR-P1-05: 幂等键唯一性限定项目范围 —— 前端幂等键由 config+params 哈希
        # 生成, 跨项目相同; 全局唯一会让另一项目同键提交命中他项目任务(replay)
        UniqueConstraint("project_id", "idempotency_key", name="uq_tasks_idempotency_key"),
        Index("idx_tasks_status", "status", sa.text("priority DESC"), "requested_at"),
        Index("idx_tasks_project", "project_id", sa.text("requested_at DESC")),
    )


class TaskAttempt(Base):
    """任务尝试(尝试序号、心跳、停止原因, 01 §7.3)。"""

    __tablename__ = "task_attempts"

    id: Mapped[int] = bigint_pk()
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed','stopped')",
            name="ck_task_attempts_status",
        ),
        UniqueConstraint("task_id", "attempt_no", name="uq_task_attempts_no"),
        Index("idx_task_attempts_task", "task_id", sa.text("attempt_no DESC")),
    )


class TaskLease(Base):
    """任务租约(fencing token, 01 §7.4)。"""

    __tablename__ = "task_leases"

    id: Mapped[int] = bigint_pk()
    attempt_id: Mapped[int] = mapped_column(ForeignKey("task_attempts.id"), nullable=False)
    lease_token: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    acquired_by: Mapped[str | None] = mapped_column(Text)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    renewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('active','expired','released','revoked')", name="ck_task_leases_status"
        ),
        UniqueConstraint("lease_token", name="uq_task_leases_token"),
        Index(
            "uq_task_leases_one_active",
            "attempt_id",
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
            sqlite_where=sa.text("status = 'active'"),
        ),
        Index("idx_task_leases_token", "lease_token"),
        Index(
            "idx_task_leases_expiry",
            "expires_at",
            postgresql_where=sa.text("status = 'active'"),
            sqlite_where=sa.text("status = 'active'"),
        ),
    )


class TaskProgress(Base):
    """任务进度(PG 持久进度, 每尝试至多一行, 01 §7.5)。"""

    __tablename__ = "task_progress"

    id: Mapped[int] = bigint_pk()
    attempt_id: Mapped[int] = mapped_column(ForeignKey("task_attempts.id"), nullable=False)
    progress_percent: Mapped[float] = mapped_column(
        Numeric(5, 2), nullable=False, server_default=sa.text("0")
    )
    stage: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint("progress_percent BETWEEN 0 AND 100", name="ck_task_progress_percent"),
        Index("uq_task_progress_latest", "attempt_id", unique=True),
        Index("idx_task_progress_attempt", "attempt_id"),
    )


class TaskDiagnostic(Base):
    """任务诊断(不可变, 01 §7.6)。"""

    __tablename__ = "task_diagnostics"

    id: Mapped[int] = bigint_pk()
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    attempt_id: Mapped[int | None] = mapped_column(ForeignKey("task_attempts.id"))
    level: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    stack_trace: Mapped[str | None] = mapped_column(Text)
    context: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "level IN ('blocking','error','warning','info')", name="ck_task_diagnostics_level"
        ),
        Index("idx_task_diagnostics_task", "task_id", "created_at"),
        Index("idx_task_diagnostics_level", "level", "created_at"),
    )


class ComputeSlot(Base):
    """计算并发槽(01 §7.7)。"""

    __tablename__ = "compute_slots"

    id: Mapped[int] = bigint_pk()
    pool_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("1"))
    in_use: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    current_attempt_id: Mapped[int | None] = mapped_column(ForeignKey("task_attempts.id"))
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('free','busy','draining','offline')", name="ck_compute_slots_status"
        ),
        CheckConstraint("capacity >= 1", name="ck_compute_slots_capacity"),
        CheckConstraint("in_use >= 0", name="ck_compute_slots_in_use"),
        CheckConstraint("in_use <= capacity", name="ck_compute_slots_capacity_bound"),
        Index(
            "uq_compute_slots_attempt",
            "current_attempt_id",
            unique=True,
            postgresql_where=sa.text("current_attempt_id IS NOT NULL"),
            sqlite_where=sa.text("current_attempt_id IS NOT NULL"),
        ),
        Index("idx_compute_slots_pool", "pool_name", "status"),
    )
