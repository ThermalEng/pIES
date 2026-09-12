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

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from iesplan.model.contracts import (
    ConnectionRecord,
    DeviceRecord,
    GraphRecord,
    ModelConflictError,
    ModelNotFoundError,
    PortRecord,
)
from iesplan.models.model import Connection, Device, Port, SystemGraph


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
