"""解耦重构切片 7: model 域 persistence 与 services.model 迁移测试。

- 直接覆盖 `iesplan.model` 门面: 工作图创建/查找、设备增删改查、端口批量
  建/按名重同步、连接增删改查与重复校验；
- 覆盖迁移后的 `iesplan.services.model` 图/设备/连接写入面（经 model 域）：
  建图幂等、设备创建（端口随类型生成）、改名冲突、参数更新触发端口重同步、
  连接创建/重复拒绝/更新/断开、设备级联删除。
运行环境与切片 6 一致：SQLite 内存库 + 临时 data_dir（对象存储）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from iesplan import model as model_domain
from iesplan import project as project_domain
from iesplan.config import settings
from iesplan.core.errors import ConflictError, NotFoundError
from iesplan.db import Base
from iesplan.model.contracts import ModelConflictError
from iesplan.services import model as model_service

LOAD = "ies.device.electric_load"
HP = "ies.device.heat_pump"
GRID = "ies.device.grid_connection"


@pytest.fixture()
def engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine: sa.Engine) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture(autouse=True)
def _clean_tables(engine: sa.Engine) -> Iterator[None]:
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(settings, "data_dir", d)
    return d


def _project(db: Session, name: str = "slice7-proj"):
    return project_domain.create_project(db, name=name, owner_id=7, created_by=7)


def test_model_domain_graph_device_port_connection(db: Session) -> None:
    project = _project(db)
    assert model_domain.find_working_graph(db, project.id) is None

    graph = model_domain.create_graph(db, project_id=project.id, draft_id=1, name="g", created_by=7)
    assert model_domain.find_working_graph(db, project.id).id == graph.id
    assert model_domain.get_graph(db, 999999) is None

    dev = model_domain.create_device(
        db, graph_id=graph.id, device_type="load", kind="new", name="L1", params={"a": 1}
    )
    assert model_domain.find_device_by_name(db, graph.id, "L1").id == dev.id
    assert model_domain.find_device_by_name(db, graph.id, "L1", exclude_id=dev.id) is None
    assert model_domain.find_device_by_name(db, graph.id, "nope") is None
    db.commit()  # 先落盘搭建行，再做冲突探针（探针失败需回滚，不能丢弃搭建）
    with pytest.raises(ModelConflictError):
        model_domain.create_device(
            db, graph_id=graph.id, device_type="load", kind="new", name="L1", params={}
        )
    # savepoint 隔离：冲突探针失败不污染调用方事务，会话可继续使用
    assert model_domain.find_device_by_name(db, graph.id, "L1").id == dev.id
    db.rollback()  # 丢弃探针残留，继续后续步骤

    ports = model_domain.create_ports(
        db,
        dev.id,
        [
            {"name": "electric_out", "port_type": "electric", "direction": "out"},
            {"name": "electric_in", "port_type": "electric", "direction": "in"},
        ],
    )
    db.commit()  # 落盘端口行，其后连接冲突探针回滚不影响它们
    assert [p.name for p in model_domain.list_ports_by_device(db, dev.id)] == [
        "electric_out",
        "electric_in",
    ]
    assert len(model_domain.list_ports(db, graph.id)) == 2

    # 按名重同步：删 electric_in（含其连接），补 heat_out
    conn = model_domain.create_connection(
        db,
        graph_id=graph.id,
        from_port_id=ports[0].id,
        to_port_id=ports[1].id,
        conn_type="electric_line",
    )
    assert (
        model_domain.find_connection(
            db,
            graph_id=graph.id,
            from_port_id=ports[0].id,
            to_port_id=ports[1].id,
            conn_type="electric_line",
        ).id
        == conn.id
    )
    with pytest.raises(ModelConflictError):
        model_domain.create_connection(
            db,
            graph_id=graph.id,
            from_port_id=ports[0].id,
            to_port_id=ports[1].id,
            conn_type="electric_line",
        )
    db.rollback()  # 同上：flush 冲突后回滚再继续

    synced = model_domain.resync_device_ports(
        db,
        dev.id,
        [
            {"name": "electric_out", "port_type": "electric", "direction": "out"},
            {"name": "heat_out", "port_type": "thermal", "direction": "out"},
        ],
    )
    assert sorted(p.name for p in synced) == ["electric_out", "heat_out"]
    # 被删端口的连接一并消失
    assert model_domain.list_connections(db, graph.id) == []
    assert model_domain.get_connection(db, conn.id) is None

    updated = model_domain.update_device(db, dev.id, params={"a": 2})
    assert updated.params == {"a": 2} and updated.updated_at is not None
    renamed = model_domain.update_device(db, dev.id, name="L2")
    assert renamed.name == "L2"

    model_domain.delete_device_cascade(db, dev.id)
    assert model_domain.get_device(db, dev.id) is None
    assert model_domain.list_ports_by_device(db, dev.id) == []
    with pytest.raises(NotFoundError):
        model_domain.update_device(db, dev.id, name="x")


def test_model_service_device_connection_flow(db: Session, data_dir: Path) -> None:
    project = _project(db, "slice7-flow")

    load = model_service.create_device(db, project.id, LOAD, "L1", created_by=7)
    assert load.graph_id is not None
    ports = {p.name: p for p in model_service.get_device_ports(db, load.id)}
    assert ports, "负荷应按类型声明生成端口"

    # 重名拒绝（经域门面查询）
    with pytest.raises(ConflictError):
        model_service.create_device(db, project.id, LOAD, "L1", created_by=7)

    # 改名冲突
    other = model_service.create_device(db, project.id, LOAD, "L2", created_by=7)
    with pytest.raises(ConflictError):
        model_service.update_device(db, project.id, other.id, name="L1")

    # 跨项目拒绝（归属校验先于方向/自环检查，与方向无关可确定性断言）
    other_project = _project(db, "slice7-other")
    other = model_service.create_device(db, other_project.id, LOAD, "LX", created_by=7)
    other_ports = list(model_service.get_device_ports(db, other.id))
    names = list(ports)
    if names and other_ports:
        with pytest.raises(model_service.ModelValidationError) as exc:
            model_service.connect(db, project.id, ports[names[0]].id, other_ports[0].id)
        assert exc.value.code == model_service.CONN_CROSS_PROJECT


def test_model_service_connect_update_disconnect(db: Session, data_dir: Path) -> None:
    project = _project(db, "slice7-conn")
    src = model_service.create_device(db, project.id, GRID, "G1", created_by=7)
    dst = model_service.create_device(db, project.id, LOAD, "L1", created_by=7)
    src_ports = {p.name: p for p in model_service.get_device_ports(db, src.id)}
    dst_ports = {p.name: p for p in model_service.get_device_ports(db, dst.id)}
    # 按能源类型配对（同载体才能连接）
    pair = next(
        (
            (o, i)
            for o in src_ports.values()
            if o.direction == "out"
            for i in dst_ports.values()
            if i.direction == "in" and i.port_type == o.port_type
        ),
        None,
    )
    assert pair is not None, "需要一对同载体源/汇端口构造连接"
    out_ports, in_ports = [pair[0]], [pair[1]]

    conn = model_service.connect(db, project.id, out_ports[0].id, in_ports[0].id)
    assert conn.graph_id == src.graph_id
    # 重复连接拒绝
    with pytest.raises(model_service.ModelValidationError) as exc:
        model_service.connect(db, project.id, out_ports[0].id, in_ports[0].id)
    assert exc.value.code == model_service.CONN_DUPLICATE

    updated = model_service.update_connection(db, project.id, conn.id, {"loss_rate": 0.05})
    assert updated.loss_rate == 0.05

    # 显式空值清空容量（与迁移前 ORM 直接赋值语义一致）
    cleared = model_service.update_connection(db, project.id, conn.id, {"capacity": None})
    assert cleared.capacity is None
    assert cleared.loss_rate == 0.05  # 未提供字段保留

    view = model_service.get_graph(db, project.id)
    assert view["has_graph"] is True
    assert any(c["id"] == conn.id for c in view["connections"])

    model_service.disconnect(db, project.id, conn.id)
    assert model_service.get_graph(db, project.id)["connections"] == []

    model_service.delete_device(db, project.id, src.id)
    with pytest.raises(NotFoundError):
        model_service.delete_device(db, project.id, src.id)
