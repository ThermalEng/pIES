"""模型域 repository SQL 实现（system_graphs/devices/ports/connections，归属 model）。

实现规则：
- 查询、写入、flush 由本模块完成；绝不 commit/rollback；
- 唯一冲突（工作图并发建图/设备重名/连接重复）转为 ModelConflictError，
  由调用方决定重试或拒绝；
- 端口按名重同步与设备级联删除在同事务内完成。
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from iesplan.model.contracts import (
    ConnectionRecord,
    DeviceRecord,
    GraphRecord,
    ModelConflictError,
    ModelNotFoundError,
    ModelTemplateRecord,
    ModelTemplateRevisionRecord,
    PortRecord,
    ProjectModelRecord,
    TemplateDraftRevisionRecord,
)
from iesplan.models.draft_revision import ModelTemplateDraftRevision
from iesplan.models.model import Connection, Device, Port, SystemGraph
from iesplan.models.model_template import ModelTemplate, ModelTemplateRevision
from iesplan.models.project_model import ProjectModel


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    """ORM 时间 → 记录字符串（原样 isoformat，不增减时区后缀）。"""
    return value.isoformat() if value is not None else None


#: 更新函数"字段缺省"哨兵（与显式 None 区分：None 表示合法清空）。
_UNSET: Any = object()


def _row_to_graph(row: SystemGraph) -> GraphRecord:
    return GraphRecord(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        draft_id=row.draft_id,
        project_version_id=row.project_version_id,
        created_by=row.created_by,
        created_at=_iso(row.created_at),
    )


def _row_to_device(row: Device) -> DeviceRecord:
    return DeviceRecord(
        id=row.id,
        graph_id=row.graph_id,
        device_type=row.device_type,
        kind=row.kind,
        name=row.name,
        params=dict(row.params or {}),
        model_fidelity=row.model_fidelity,
        status=row.status,
        description=row.description,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def _row_to_port(row: Port) -> PortRecord:
    return PortRecord(
        id=row.id,
        device_id=row.device_id,
        port_type=row.port_type,
        direction=row.direction,
        name=row.name,
        capacity=float(row.capacity) if row.capacity is not None else None,
        params=dict(row.params or {}),
    )


def _row_to_connection(row: Connection) -> ConnectionRecord:
    return ConnectionRecord(
        id=row.id,
        graph_id=row.graph_id,
        from_port_id=row.from_port_id,
        to_port_id=row.to_port_id,
        conn_type=row.conn_type,
        loss_rate=float(row.loss_rate),
        capacity=float(row.capacity) if row.capacity is not None else None,
        params=dict(row.params or {}),
    )


# ---------------------------------------------------------------------------
# 系统图
# ---------------------------------------------------------------------------


def find_working_graph(db: Session, project_id: int) -> GraphRecord | None:
    """项目工作图（挂草稿，按 id 升序取最早一张）；无返回 None。"""
    row = db.execute(
        select(SystemGraph)
        .where(SystemGraph.project_id == project_id, SystemGraph.draft_id.is_not(None))
        .order_by(SystemGraph.id)
    ).scalar_one_or_none()
    return _row_to_graph(row) if row is not None else None


def get_graph(db: Session, graph_id: int) -> GraphRecord | None:
    """按主键取图；不存在返回 None。"""
    row = db.get(SystemGraph, graph_id)
    return _row_to_graph(row) if row is not None else None


def find_graph_by_draft(db: Session, project_id: int, draft_id: int) -> GraphRecord | None:
    """取挂指定草稿的工作图；无返回 None。"""
    row = db.execute(
        select(SystemGraph).where(SystemGraph.project_id == project_id, SystemGraph.draft_id == draft_id)
    ).scalar_one_or_none()
    return _row_to_graph(row) if row is not None else None


def find_latest_working_graph(db: Session, project_id: int) -> GraphRecord | None:
    """项目最近一张工作图（id 降序）；无返回 None。"""
    row = db.execute(
        select(SystemGraph)
        .where(SystemGraph.project_id == project_id, SystemGraph.draft_id.is_not(None))
        .order_by(SystemGraph.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _row_to_graph(row) if row is not None else None


def create_graph(db: Session, *, project_id: int, draft_id: int, name: str, created_by: int) -> GraphRecord:
    """创建工作图；唯一冲突抛 ModelConflictError。"""
    row = SystemGraph(
        project_id=project_id,
        draft_id=draft_id,
        name=name,
        created_by=created_by,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        raise ModelConflictError("工作图创建冲突", params={"project_id": project_id}) from exc
    return _row_to_graph(row)


# ---------------------------------------------------------------------------
# 设备
# ---------------------------------------------------------------------------


def get_device(db: Session, device_id: int) -> DeviceRecord | None:
    """按主键取设备；不存在返回 None。"""
    row = db.get(Device, device_id)
    return _row_to_device(row) if row is not None else None


def find_device_by_name(
    db: Session, graph_id: int, name: str, exclude_id: int | None = None
) -> DeviceRecord | None:
    """图内按名取设备（重名校验用）；无返回 None。"""
    stmt = select(Device).where(Device.graph_id == graph_id, Device.name == name)
    if exclude_id is not None:
        stmt = stmt.where(Device.id != exclude_id)
    row = db.execute(stmt).scalar_one_or_none()
    return _row_to_device(row) if row is not None else None


def list_devices(db: Session, graph_id: int) -> list[DeviceRecord]:
    """图内设备（id 升序）。"""
    rows = db.execute(select(Device).where(Device.graph_id == graph_id).order_by(Device.id)).scalars().all()
    return [_row_to_device(row) for row in rows]


def create_device(
    db: Session,
    *,
    graph_id: int,
    device_type: str,
    kind: str,
    name: str,
    params: dict[str, Any],
    model_fidelity: str = "medium",
    status: str = "active",
) -> DeviceRecord:
    """创建设备行；重名冲突抛 ModelConflictError。"""
    row = Device(
        graph_id=graph_id,
        device_type=device_type,
        kind=kind,
        name=name,
        params=dict(params),
        model_fidelity=model_fidelity,
        status=status,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        raise ModelConflictError("设备名称重复", params={"graph_id": graph_id, "name": name}) from exc
    return _row_to_device(row)


def update_device(
    db: Session,
    device_id: int,
    *,
    name: str | None = None,
    params: dict[str, Any] | None = None,
) -> DeviceRecord:
    """更新设备名称/参数（仅提供字段）并刷新 updated_at；缺失抛 ModelNotFoundError。"""
    row = db.get(Device, device_id)
    if row is None:
        raise ModelNotFoundError("设备不存在", params={"device_id": device_id})
    if name is not None:
        row.name = name
    if params is not None:
        row.params = dict(params)
    row.updated_at = _now()
    db.flush()
    return _row_to_device(row)


def delete_device_cascade(db: Session, device_id: int) -> None:
    """删除设备及其端口与连接（级联）；设备缺失抛 ModelNotFoundError。"""
    row = db.get(Device, device_id)
    if row is None:
        raise ModelNotFoundError("设备不存在", params={"device_id": device_id})
    port_ids = list(db.execute(select(Port.id).where(Port.device_id == device_id)).scalars())
    if port_ids:
        db.execute(delete(Connection).where(Connection.from_port_id.in_(port_ids)))
        db.execute(delete(Connection).where(Connection.to_port_id.in_(port_ids)))
        db.execute(delete(Port).where(Port.id.in_(port_ids)))
    db.delete(row)
    db.flush()


# ---------------------------------------------------------------------------
# 端口
# ---------------------------------------------------------------------------


def get_port(db: Session, port_id: int) -> PortRecord | None:
    """按主键取端口；不存在返回 None。"""
    row = db.get(Port, port_id)
    return _row_to_port(row) if row is not None else None


def list_ports(db: Session, graph_id: int) -> list[PortRecord]:
    """图内端口（经设备归属，id 升序）。"""
    rows = (
        db.execute(
            select(Port)
            .join(Device, Port.device_id == Device.id)
            .where(Device.graph_id == graph_id)
            .order_by(Port.id)
        )
        .scalars()
        .all()
    )
    return [_row_to_port(row) for row in rows]


def list_ports_by_device(db: Session, device_id: int) -> list[PortRecord]:
    """设备端口（id 升序）。"""
    rows = db.execute(select(Port).where(Port.device_id == device_id).order_by(Port.id)).scalars().all()
    return [_row_to_port(row) for row in rows]


def create_ports(db: Session, device_id: int, specs: Collection[dict[str, Any]]) -> list[PortRecord]:
    """批量建端口；spec 含 name/port_type/direction，params 缺省为空对象。"""
    rows: list[Port] = []
    for spec in specs:
        row = Port(
            device_id=device_id,
            port_type=spec["port_type"],
            direction=spec["direction"],
            name=spec["name"],
            params=dict(spec.get("params") or {}),
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return [_row_to_port(row) for row in rows]


def resync_device_ports(db: Session, device_id: int, wanted: Collection[dict[str, Any]]) -> list[PortRecord]:
    """按名对齐设备端口：删除多余端口及其连接，补齐缺失端口；返回当前端口。"""
    want_by_name = {spec["name"]: spec for spec in wanted}
    existing = list(db.execute(select(Port).where(Port.device_id == device_id)).scalars())
    for port in existing:
        if port.name not in want_by_name:
            db.execute(delete(Connection).where(Connection.from_port_id == port.id))
            db.execute(delete(Connection).where(Connection.to_port_id == port.id))
            db.delete(port)
    existing_names = {port.name for port in existing}
    for name, spec in want_by_name.items():
        if name not in existing_names:
            db.add(
                Port(
                    device_id=device_id,
                    port_type=spec["port_type"],
                    direction=spec["direction"],
                    name=name,
                    params={},
                )
            )
    db.flush()
    return list_ports_by_device(db, device_id)


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------


def get_connection(db: Session, conn_id: int) -> ConnectionRecord | None:
    """按主键取连接；不存在返回 None。"""
    row = db.get(Connection, conn_id)
    return _row_to_connection(row) if row is not None else None


def list_connections(db: Session, graph_id: int) -> list[ConnectionRecord]:
    """图内连接（id 升序）。"""
    rows = (
        db.execute(select(Connection).where(Connection.graph_id == graph_id).order_by(Connection.id))
        .scalars()
        .all()
    )
    return [_row_to_connection(row) for row in rows]


def find_connection(
    db: Session,
    *,
    graph_id: int,
    from_port_id: int,
    to_port_id: int,
    conn_type: str,
) -> ConnectionRecord | None:
    """按（图，两端，类型）取连接（重复校验用）；无返回 None。"""
    row = db.execute(
        select(Connection).where(
            Connection.graph_id == graph_id,
            Connection.from_port_id == from_port_id,
            Connection.to_port_id == to_port_id,
            Connection.conn_type == conn_type,
        )
    ).scalar_one_or_none()
    return _row_to_connection(row) if row is not None else None


def create_connection(
    db: Session,
    *,
    graph_id: int,
    from_port_id: int,
    to_port_id: int,
    conn_type: str,
    capacity: float | None = None,
    loss_rate: float = 0.0,
    params: dict[str, Any] | None = None,
) -> ConnectionRecord:
    """创建连接行；冲突抛 ModelConflictError。"""
    row = Connection(
        graph_id=graph_id,
        from_port_id=from_port_id,
        to_port_id=to_port_id,
        conn_type=conn_type,
        capacity=capacity,
        loss_rate=loss_rate,
        params=dict(params or {}),
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        raise ModelConflictError(
            "连接已存在(同图同两端同类型)",
            params={"graph_id": graph_id, "from_port_id": from_port_id},
        ) from exc
    return _row_to_connection(row)


def update_connection(
    db: Session,
    conn_id: int,
    *,
    capacity: float | None | Any = _UNSET,
    loss_rate: float | None | Any = _UNSET,
    params: dict[str, Any] | None | Any = _UNSET,
) -> ConnectionRecord:
    """更新连接属性（仅提供字段；显式 None 表示合法清空）；缺失抛 ModelNotFoundError。"""
    row = db.get(Connection, conn_id)
    if row is None:
        raise ModelNotFoundError("连接不存在", params={"connection_id": conn_id})
    if capacity is not _UNSET:
        row.capacity = capacity
    if loss_rate is not _UNSET:
        row.loss_rate = loss_rate
    if params is not _UNSET:
        row.params = dict(params) if params is not None else None
    db.flush()
    return _row_to_connection(row)


def delete_connection(db: Session, conn_id: int) -> None:
    """删除连接行；缺失抛 ModelNotFoundError。"""
    row = db.get(Connection, conn_id)
    if row is None:
        raise ModelNotFoundError("连接不存在", params={"connection_id": conn_id})
    db.delete(row)
    db.flush()


# ---------------------------------------------------------------------------
# 模板主表
# ---------------------------------------------------------------------------


def _row_to_template(row: ModelTemplate) -> ModelTemplateRecord:
    return ModelTemplateRecord(
        id=row.id,
        template_id=row.template_id,
        owner_id=row.owner_id,
        status=row.status,
        description=row.description,
        slug=row.slug,
        public_namespace=row.public_namespace,
        draft_yaml_object_id=row.draft_yaml_object_id,
        draft_diagnostics_object_id=row.draft_diagnostics_object_id,
        draft_has_inputs=row.draft_has_inputs,
        draft_revision=row.draft_revision,
        draft_updated_at=_iso(row.draft_updated_at),
        current_draft_revision_id=row.current_draft_revision_id,
        current_published_revision_id=row.current_published_revision_id,
        published_revision=row.published_revision,
        published_at=_iso(row.published_at),
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def _row_to_template_revision(row: ModelTemplateRevision) -> ModelTemplateRevisionRecord:
    return ModelTemplateRevisionRecord(
        id=row.id,
        template_id=row.template_id,
        revision=row.revision,
        schema_version=row.schema_version,
        input_count=row.input_count,
        diagnostics_object_id=row.diagnostics_object_id,
        idempotency_key=row.idempotency_key,
        yaml_object_id=row.yaml_object_id,
        receipt_object_id=row.receipt_object_id,
        summary_object_id=row.summary_object_id,
        published_by=row.published_by,
        published_at=_iso(row.published_at),
    )


def _row_to_draft_revision(row: ModelTemplateDraftRevision) -> TemplateDraftRevisionRecord:
    return TemplateDraftRevisionRecord(
        id=row.id,
        entry_id=row.entry_id,
        revision=row.revision,
        yaml_object_id=row.yaml_object_id,
        source=row.source,
        created_by=row.created_by,
        created_at=_iso(row.created_at),
        diagnostics_object_id=row.diagnostics_object_id,
    )


def _row_to_project_model(row: ProjectModel) -> ProjectModelRecord:
    return ProjectModelRecord(
        id=row.id,
        project_id=row.project_id,
        suffix=row.suffix,
        base_device_id=row.base_device_id,
        device_id=row.device_id,
        revision=row.revision,
        project_revision=row.project_revision,
        model_object_id=row.model_object_id,
        receipt_object_id=row.receipt_object_id,
        source=row.source,
        template_id=row.template_id,
        template_revision=row.template_revision,
        idempotency_key=row.idempotency_key,
        created_by=row.created_by,
        created_at=_iso(row.created_at),
    )


def get_owned_template(db: Session, owner_id: int, template_id: str) -> ModelTemplateRecord | None:
    """按稳定模板 ID 取当前用户的模板；无/非属返回 None。"""
    row = db.execute(
        select(ModelTemplate).where(
            ModelTemplate.owner_id == owner_id,
            ModelTemplate.template_id == template_id,
        )
    ).scalar_one_or_none()
    return _row_to_template(row) if row is not None else None


def list_owned_templates(db: Session, owner_id: int) -> list[ModelTemplateRecord]:
    """当前用户模板（更新时间降序，id 降序）。"""
    rows = (
        db.execute(
            select(ModelTemplate)
            .where(ModelTemplate.owner_id == owner_id)
            .order_by(ModelTemplate.updated_at.desc(), ModelTemplate.id.desc())
        )
        .scalars()
        .all()
    )
    return [_row_to_template(row) for row in rows]


def list_owned_templates_by_status(
    db: Session, owner_id: int, status: str
) -> list[ModelTemplateRecord]:
    """当前用户指定状态模板（更新时间降序，id 降序）。"""
    rows = (
        db.execute(
            select(ModelTemplate)
            .where(ModelTemplate.owner_id == owner_id, ModelTemplate.status == status)
            .order_by(ModelTemplate.updated_at.desc(), ModelTemplate.id.desc())
        )
        .scalars()
        .all()
    )
    return [_row_to_template(row) for row in rows]


def create_template(
    db: Session,
    *,
    template_id: str,
    slug: str,
    public_namespace: str,
    owner_id: int,
    status: str,
    description: str | None,
    draft_yaml_object_id: int,
    draft_has_inputs: bool,
    draft_revision: int,
    draft_updated_at: Any,
) -> ModelTemplateRecord:
    """创建模板主表行；稳定 ID/用户 slug 冲突抛 ModelConflictError。"""
    row = ModelTemplate(
        template_id=template_id,
        slug=slug,
        public_namespace=public_namespace,
        owner_id=owner_id,
        status=status,
        description=description,
        draft_yaml_object_id=draft_yaml_object_id,
        draft_has_inputs=draft_has_inputs,
        draft_revision=draft_revision,
        draft_updated_at=draft_updated_at,
        published_revision=0,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        raise ModelConflictError(
            "模板已存在(同一用户模板 ID 唯一)",
            params={"template_id": template_id},
        ) from exc
    return _row_to_template(row)


def update_template(db: Session, template_row_id: int, **fields: Any) -> ModelTemplateRecord:
    """更新模板主表字段（仅提供字段）并返回新视图；缺失抛 ModelNotFoundError。"""
    row = db.get(ModelTemplate, template_row_id)
    if row is None:
        raise ModelNotFoundError("模板不存在", params={"template_row_id": template_row_id})
    for name, value in fields.items():
        setattr(row, name, value)
    db.flush()
    return _row_to_template(row)


def delete_template(db: Session, template_row_id: int) -> None:
    """硬删除模板主表行；缺失抛 ModelNotFoundError。"""
    row = db.get(ModelTemplate, template_row_id)
    if row is None:
        raise ModelNotFoundError("模板不存在", params={"template_row_id": template_row_id})
    db.delete(row)
    db.flush()


# ---------------------------------------------------------------------------
# 模板草稿/发布 revision
# ---------------------------------------------------------------------------


def create_draft_revision(
    db: Session,
    *,
    entry_id: int,
    revision: int,
    yaml_object_id: int,
    source: str,
    created_by: int,
    diagnostics_object_id: int | None,
) -> TemplateDraftRevisionRecord:
    """新增不可变草稿 revision 行；冲突抛 ModelConflictError。"""
    row = ModelTemplateDraftRevision(
        entry_id=entry_id,
        revision=revision,
        yaml_object_id=yaml_object_id,
        source=source,
        created_by=created_by,
        diagnostics_object_id=diagnostics_object_id,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        raise ModelConflictError(
            "草稿 revision 冲突",
            params={"entry_id": entry_id, "revision": revision},
        ) from exc
    return _row_to_draft_revision(row)


def list_draft_revisions(db: Session, entry_id: int) -> list[TemplateDraftRevisionRecord]:
    """模板草稿 revision 历史（revision 升序）。"""
    rows = (
        db.execute(
            select(ModelTemplateDraftRevision)
            .where(ModelTemplateDraftRevision.entry_id == entry_id)
            .order_by(ModelTemplateDraftRevision.revision)
        )
        .scalars()
        .all()
    )
    return [_row_to_draft_revision(row) for row in rows]


def get_draft_revision(
    db: Session, entry_id: int, revision: int
) -> TemplateDraftRevisionRecord | None:
    """取精确草稿 revision；无返回 None。"""
    row = db.execute(
        select(ModelTemplateDraftRevision).where(
            ModelTemplateDraftRevision.entry_id == entry_id,
            ModelTemplateDraftRevision.revision == revision,
        )
    ).scalar_one_or_none()
    return _row_to_draft_revision(row) if row is not None else None


def get_published_revision(
    db: Session, template_row_id: int, revision: int
) -> ModelTemplateRevisionRecord | None:
    """取精确发布 revision；无返回 None。"""
    row = db.execute(
        select(ModelTemplateRevision).where(
            ModelTemplateRevision.template_id == template_row_id,
            ModelTemplateRevision.revision == revision,
        )
    ).scalar_one_or_none()
    return _row_to_template_revision(row) if row is not None else None


def find_revision_by_idempotency(
    db: Session, template_row_id: int, idempotency_key: str
) -> ModelTemplateRevisionRecord | None:
    """按幂等键取发布 revision（重放用）；无返回 None。"""
    row = db.execute(
        select(ModelTemplateRevision).where(
            ModelTemplateRevision.template_id == template_row_id,
            ModelTemplateRevision.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    return _row_to_template_revision(row) if row is not None else None


def create_published_revision(
    db: Session,
    *,
    template_row_id: int,
    revision: int,
    schema_version: str,
    input_count: int,
    yaml_object_id: int,
    receipt_object_id: int,
    summary_object_id: int,
    diagnostics_object_id: int | None,
    idempotency_key: str | None,
    published_by: int,
) -> ModelTemplateRevisionRecord:
    """新增不可变发布 revision 行；冲突抛 ModelConflictError。"""
    row = ModelTemplateRevision(
        template_id=template_row_id,
        revision=revision,
        schema_version=schema_version,
        input_count=input_count,
        yaml_object_id=yaml_object_id,
        receipt_object_id=receipt_object_id,
        summary_object_id=summary_object_id,
        diagnostics_object_id=diagnostics_object_id,
        idempotency_key=idempotency_key,
        published_by=published_by,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        raise ModelConflictError(
            "模板发布冲突(并发)",
            params={"template_row_id": template_row_id, "revision": revision},
        ) from exc
    return _row_to_template_revision(row)


# ---------------------------------------------------------------------------
# 项目模型清单
# ---------------------------------------------------------------------------


def get_project_model(db: Session, model_id: int) -> ProjectModelRecord | None:
    """按主键取清单行；不存在返回 None。"""
    row = db.get(ProjectModel, model_id)
    return _row_to_project_model(row) if row is not None else None


def find_project_model_by_idempotency(
    db: Session, project_id: int, idempotency_key: str
) -> ProjectModelRecord | None:
    """按幂等键取清单行（重放用）；无返回 None。"""
    row = db.execute(
        select(ProjectModel).where(
            ProjectModel.project_id == project_id,
            ProjectModel.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    return _row_to_project_model(row) if row is not None else None


def create_project_model(
    db: Session,
    *,
    project_id: int,
    suffix: int,
    base_device_id: str,
    device_id: str,
    project_revision: int,
    model_object_id: int,
    receipt_object_id: int,
    source: str,
    template_id: str | None,
    template_revision: int | None,
    idempotency_key: str | None,
    created_by: int,
) -> ProjectModelRecord:
    """新增清单行；编号/最终 ID 冲突抛 ModelConflictError。"""
    row = ProjectModel(
        project_id=project_id,
        suffix=suffix,
        base_device_id=base_device_id,
        device_id=device_id,
        revision=1,
        project_revision=project_revision,
        model_object_id=model_object_id,
        receipt_object_id=receipt_object_id,
        source=source,
        template_id=template_id,
        template_revision=template_revision,
        idempotency_key=idempotency_key,
        created_by=created_by,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        raise ModelConflictError(
            "项目模型保存冲突(编号或最终 ID 唯一性)",
            params={"project_id": project_id, "device_id": device_id},
        ) from exc
    return _row_to_project_model(row)


def update_project_model(db: Session, model_id: int, **fields: Any) -> ProjectModelRecord:
    """更新清单行字段（仅提供字段）并返回新视图；缺失抛 ModelNotFoundError。"""
    row = db.get(ProjectModel, model_id)
    if row is None:
        raise ModelNotFoundError("项目模型不存在", params={"model_id": model_id})
    for name, value in fields.items():
        setattr(row, name, value)
    db.flush()
    return _row_to_project_model(row)


def delete_project_model(db: Session, model_id: int) -> ProjectModelRecord:
    """硬删除清单行并返回删除前视图；缺失抛 ModelNotFoundError。"""
    row = db.get(ProjectModel, model_id)
    if row is None:
        raise ModelNotFoundError("项目模型不存在", params={"model_id": model_id})
    snapshot = _row_to_project_model(row)
    db.delete(row)
    db.flush()
    return snapshot


def list_project_models(
    db: Session, project_id: int, *, newest_first: bool = False
) -> list[ProjectModelRecord]:
    """项目清单行（默认编号升序；newest_first 时编号降序）。"""
    order = (
        (ProjectModel.suffix.desc(), ProjectModel.id.desc())
        if newest_first
        else (ProjectModel.suffix, ProjectModel.id)
    )
    rows = (
        db.execute(
            select(ProjectModel).where(ProjectModel.project_id == project_id).order_by(*order)
        )
        .scalars()
        .all()
    )
    return [_row_to_project_model(row) for row in rows]


def allocate_project_model_suffix(db: Session, project_id: int) -> int:
    """项目内分配下一个 _N 编号（只递增、删除不复用）。

    ``UPDATE ... RETURNING`` 在数据库内原子完成（PostgreSQL 行锁 +
    SQLite 写锁串行化）；计数器行缺失时以 savepoint 插入并重查；
    重试耗尽抛 ModelConflictError（即 ConflictError，由调用方决定重试）。
    """

    for _attempt in range(3):
        row = db.execute(
            text(
                "UPDATE project_model_sequences SET next_suffix = next_suffix + 1 "
                "WHERE project_id = :pid RETURNING next_suffix - 1"
            ),
            {"pid": project_id},
        ).first()
        if row is not None:
            return int(row[0])
        try:
            with db.begin_nested():  # 只回滚嵌套 savepoint, 不触调用方外层事务
                db.execute(
                    text(
                        "INSERT INTO project_model_sequences (project_id, next_suffix) "
                        "VALUES (:pid, 2)"
                    ),
                    {"pid": project_id},
                )
                db.flush()
            return 1
        except IntegrityError:
            continue  # 并发竞争者已插入计数器行: 下一轮 UPDATE 原子重试
    raise ModelConflictError(
        "项目模型编号分配失败(并发冲突), 请重试",
        params={"project_id": project_id},
        location={"object_type": "project_model", "project_id": str(project_id)},
    )
