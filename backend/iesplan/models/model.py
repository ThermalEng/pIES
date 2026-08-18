"""系统模型域(U04 模型写入单元): system_graphs / devices / ports / connections。

对应 01-db-schema.md 第4节。
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from iesplan.db import Base
from iesplan.models.common import HASH64_RE, JSONB, bigint_pk, regex_check


class SystemGraph(Base):
    """系统图(工作图可改, 版本图不可变, 01 §4.1)。"""

    __tablename__ = "system_graphs"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    draft_id: Mapped[int | None] = mapped_column(ForeignKey("drafts.id"))
    project_version_id: Mapped[int | None] = mapped_column(ForeignKey("project_versions.id"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    graph_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        # 互斥: 一张图要么是工作图, 要么是版本图
        CheckConstraint(
            "(draft_id IS NULL) <> (project_version_id IS NULL)", name="ck_system_graphs_exclusive"
        ),
        regex_check(f"graph_hash ~ '{HASH64_RE}'", name="ck_system_graphs_hash"),
        Index("idx_system_graphs_draft", "draft_id"),
        Index("idx_system_graphs_version", "project_version_id"),
    )


class Device(Base):
    """设备(类型、存量/新增、参数、模型精度, 01 §4.2)。"""

    __tablename__ = "devices"

    id: Mapped[int] = bigint_pk()
    graph_id: Mapped[int] = mapped_column(ForeignKey("system_graphs.id"), nullable=False)
    device_type: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'"))
    model_fidelity: Mapped[str] = mapped_column(Text, nullable=False, server_default="medium")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "device_type IN ('generator','boiler','chiller','pv','wind','storage','load',"
            "'source','sink','converter','network','other')",
            name="ck_devices_type",
        ),
        CheckConstraint("kind IN ('existing','new')", name="ck_devices_kind"),
        CheckConstraint("model_fidelity IN ('low','medium','high')", name="ck_devices_fidelity"),
        CheckConstraint("status IN ('active','retired')", name="ck_devices_status"),
        UniqueConstraint("graph_id", "name", name="uq_devices_graph_name"),
        Index("idx_devices_graph", "graph_id", "device_type"),
        Index("idx_devices_kind", "graph_id", "kind"),
    )


class Port(Base):
    """端口(01 §4.3)。"""

    __tablename__ = "ports"

    id: Mapped[int] = bigint_pk()
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), nullable=False)
    port_type: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    capacity: Mapped[float | None] = mapped_column(Numeric(18, 4))
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'"))

    __table_args__ = (
        CheckConstraint(
            "port_type IN ('electric','thermal','cooling','fuel','water','data')",
            name="ck_ports_type",
        ),
        CheckConstraint("direction IN ('in','out','bidirectional')", name="ck_ports_direction"),
        UniqueConstraint("device_id", "name", name="uq_ports_device_name"),
        Index("idx_ports_device", "device_id"),
        Index("idx_ports_type", "port_type"),
    )


class Connection(Base):
    """连接(01 §4.4)。"""

    __tablename__ = "connections"

    id: Mapped[int] = bigint_pk()
    graph_id: Mapped[int] = mapped_column(ForeignKey("system_graphs.id"), nullable=False)
    from_port_id: Mapped[int] = mapped_column(ForeignKey("ports.id"), nullable=False)
    to_port_id: Mapped[int] = mapped_column(ForeignKey("ports.id"), nullable=False)
    conn_type: Mapped[str] = mapped_column(Text, nullable=False)
    capacity: Mapped[float | None] = mapped_column(Numeric(18, 4))
    loss_rate: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False, server_default=sa.text("0"))
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'"))

    __table_args__ = (
        CheckConstraint(
            "conn_type IN ('electric_line','thermal_pipe','cooling_pipe','fuel_pipe','data_link')",
            name="ck_connections_type",
        ),
        CheckConstraint("loss_rate BETWEEN 0 AND 1", name="ck_connections_loss_rate"),
        CheckConstraint("from_port_id <> to_port_id", name="ck_connections_no_self_loop"),
        UniqueConstraint(
            "graph_id", "from_port_id", "to_port_id", "conn_type", name="uq_connections_ends"
        ),
        Index("idx_connections_from", "from_port_id"),
        Index("idx_connections_to", "to_port_id"),
        Index("idx_connections_graph", "graph_id"),
    )
