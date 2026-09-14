"""系统模型单元测试(U04): 设备/连接/拓扑校验/图序列化往返。

- API 层: SQLite 内存库(StaticPool 单连接) + FastAPI TestClient, 认证用窗口会话登录;
- 服务层: 直接驱动 iesplan.services.model;
- 数据源: registry 内置 9 类设备(04 §3)。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from iesplan import model as model_domain
from iesplan.api import model as model_api
from iesplan.api.deps import get_request_db
from iesplan.core.diagnostics import (
    CONN_NODE_ORPHAN,
    CONN_TYPE_UNREGISTERED,
    PARAM_CONFLICT,
    PARAM_RNG_OUT,
    PARAM_UNIT_INCONSISTENT,
    PARAM_UNIT_MISMATCH,
)
from iesplan.db import Base
from iesplan.main import create_app
from iesplan.model.persistence import Device, Port, SystemGraph
from iesplan.project.persistence import Project
from iesplan.application import identity
from iesplan.application.models import service as svc

GRID = "ies.device.grid_connection"
PV = "ies.device.pv"
BATTERY = "ies.device.battery"
LOAD = "ies.device.electric_load"
HEAT_LOAD = "ies.device.heat_load"
HP = "ies.device.heat_pump"

#: 种子管理员密码(经 /api/auth/login 真实登录)
ADMIN_PASSWORD = "Admin12345"


def _headers(client: TestClient) -> dict[str, str]:
    """认证头: 以种子管理员窗口会话登录(同一 client 内缓存)。"""
    try:
        token: str | None = client._auth_token  # type: ignore[attr-defined]
    except AttributeError:
        token = None
    if token is None:
        resp = client.post(
            "/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}
        )
        assert resp.status_code == 200, resp.text
        token = resp.json()["token"]
        client._auth_token = token  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def db_factory() -> tuple[sessionmaker, int]:
    """SQLite 内存库(单连接共享, 跨线程) + 种子管理员(首行 id=1)。"""
    engine = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        admin = identity.create_user(
            session, "admin", ADMIN_PASSWORD, role="admin",
            force_password_change=False, display_name="管理员",
        )
        admin_id = admin.id
    return factory, admin_id


def _create_project(factory: sessionmaker, name: str, admin_id: int) -> int:
    """直连会话创建项目(projects.owner_id 记录所有者, 满足 U02 ensure_access; 显式携带基线)。"""
    with factory() as session:
        project = Project(
            name=name, owner_id=admin_id, created_by=admin_id,
            baseline_resolution="1h", baseline_leap_year=False, baseline_scenario_mode="single",
        )
        session.add(project)
        session.flush()
        session.commit()
        return project.id


@pytest.fixture()
def project_id(db_factory: tuple[sessionmaker, int]) -> int:
    """首个测试项目 id。"""
    factory, admin_id = db_factory
    return _create_project(factory, "测试项目", admin_id)


@pytest.fixture()
def client(db_factory: tuple[sessionmaker, int]) -> Iterator[TestClient]:
    """挂载模型路由的独立应用测试客户端(依赖覆盖为 SQLite 会话)。"""
    factory, _ = db_factory
    app = create_app()
    app.include_router(model_api.registry_router)
    app.include_router(model_api.model_router)

    def _override_request_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_request_db] = _override_request_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _create_device(
    client: TestClient,
    project_id: int,
    device_type: str,
    name: str,
    params: dict | None = None,
    **extra: object,
) -> dict:
    """通过 API 创建设备, 断言成功并返回响应体。"""
    resp = client.post(
        f"/api/projects/{project_id}/model/devices",
        json={"device_type": device_type, "name": name, "params": params or {}, **extra},
        headers=_headers(client),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _port(created: dict, name: str) -> dict:
    """从创建设备响应中按端口名取端口。"""
    for p in created["ports"]:
        if p["name"] == name:
            return p
    raise AssertionError(f"端口 {name} 不存在: {created['ports']}")


def _connect(client: TestClient, project_id: int, from_port_id: int, to_port_id: int) -> dict:
    """通过 API 创建连接, 断言成功并返回响应体。"""
    resp = client.post(
        f"/api/projects/{project_id}/model/connections",
        json={"from_port_id": from_port_id, "to_port_id": to_port_id},
        headers=_headers(client),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _get_graph(client: TestClient, project_id: int) -> dict:
    """读取项目系统图。"""
    resp = client.get(f"/api/projects/{project_id}/model", headers=_headers(client))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _get_validate(client: TestClient, project_id: int) -> list[dict]:
    """调用模型校验接口, 返回诊断列表。"""
    resp = client.get(f"/api/projects/{project_id}/model/validate", headers=_headers(client))
    assert resp.status_code == 200, resp.text
    return resp.json()["diagnostics"]


def _codes(diags: list[dict]) -> list[str]:
    """诊断码列表。"""
    return [d["code"] for d in diags]


# ---------------------------------------------------------------------------
# 设备类型注册表(公开)
# ---------------------------------------------------------------------------


def test_device_types_public_endpoint(client: TestClient) -> None:
    """公开注册表: 2.0 设备文档, 含真实端口/参数 schema(单位/范围/默认)。"""
    resp = client.get("/api/registry/device-types")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 10  # RR-P2-05: 含 transport_pipe 管道设备
    hp = next(i for i in body["items"] if i["type_id"] == HP)
    assert hp["names"]["zh-CN"] == "热泵"
    assert hp["schema_version"] == "2.0.0"
    port_names = {p["name"] for p in hp["ports"]}
    assert port_names == {"electricity_in", "heat_out"}
    rated = hp["parameters"]["rated_heat_kw"]
    assert rated["unit"] == "kW"
    assert rated["min"] == 0
    assert rated["max"] == 1_000_000
    assert rated["default"] == 500


# ---------------------------------------------------------------------------
# 设备写入
# ---------------------------------------------------------------------------


def test_create_device_out_of_range_param_rejected(client: TestClient, project_id: int) -> None:
    """参数越界被拒: pv.rated_capacity_kwp 2000000 > max 1000000 → 400, 定位到字段。"""
    resp = client.post(
        f"/api/projects/{project_id}/model/devices",
        json={"device_type": PV, "name": "PV1", "params": {"rated_capacity_kwp": 2000000}},
        headers=_headers(client),
    )
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == PARAM_RNG_OUT
    assert err["location"]["field"] == "rated_capacity_kwp"
    assert err["location"]["object_type"] == "device"


def test_create_device_unknown_type_rejected(client: TestClient, project_id: int) -> None:
    """未注册设备类型被拒(404 CONN-TYPE-002)。"""
    resp = client.post(
        f"/api/projects/{project_id}/model/devices",
        json={"device_type": "ies.device.wind_turbine", "name": "W1"},
        headers=_headers(client),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == CONN_TYPE_UNREGISTERED


def test_create_device_without_profile_param_accepted(client: TestClient, project_id: int) -> None:
    """负荷设备无需曲线参数: electric_load 只带物理参数 → 201。

    2.0 无 load_profile 类引用参数(source.data_ref 为唯一数据路径);
    未声明的参数键被拒(PARAM-UNIT-002)。
    """
    resp = client.post(
        f"/api/projects/{project_id}/model/devices",
        json={"device_type": LOAD, "name": "L1", "params": {"peak_power_kw": 100}},
        headers=_headers(client),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "load_profile" not in body["device"]["params"]
    assert body["device"]["params"]["type_detail"] == LOAD
    # 未声明的引用键直接拒绝(无数据绑定延后机制)
    resp = client.post(
        f"/api/projects/{project_id}/model/devices",
        json={
            "device_type": LOAD,
            "name": "L2",
            "params": {"peak_power_kw": 100, "load_profile": "dataset:e_load"},
        },
        headers=_headers(client),
    )
    assert resp.status_code == 400, resp.text


def test_create_device_param_type_mismatch_rejected(client: TestClient, project_id: int) -> None:
    """参数类型不匹配被拒: cop 传字符串 → 400 PARAM-UNIT-002。"""
    resp = client.post(
        f"/api/projects/{project_id}/model/devices",
        json={"device_type": HP, "name": "HP1", "params": {"cop": "高"}},
        headers=_headers(client),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PARAM-UNIT-002"


def test_create_device_undeclared_param_rejected(client: TestClient, project_id: int) -> None:
    """未声明参数被拒: heat_pump.source_type(2.0 未声明) → 400 PARAM-UNIT-002。"""
    resp = client.post(
        f"/api/projects/{project_id}/model/devices",
        json={"device_type": HP, "name": "HP1", "params": {"source_type": "ocean"}},
        headers=_headers(client),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PARAM-UNIT-002"


def test_create_device_generates_ports_by_carrier(
    db_factory: tuple[sessionmaker, int], project_id: int
) -> None:
    """端口按 2.0 接口生成: heat_pump → electricity_in/heat_out; 负荷需求接口为 in 端口。"""
    factory, admin_id = db_factory
    with factory() as session:
        device = svc.create_device(session, project_id, HP, "HP1", created_by=admin_id)
        ports = {p.name: p for p in svc.get_device_ports(session, device.id)}
        assert set(ports) == {"electricity_in", "heat_out"}
        assert ports["electricity_in"].port_type == "electric"
        assert ports["electricity_in"].direction == "in"
        assert ports["heat_out"].port_type == "thermal"
        assert ports["heat_out"].direction == "out"
        load = svc.create_device(session, project_id, LOAD, "L1", created_by=admin_id)
        load_ports = {p.name: p for p in svc.get_device_ports(session, load.id)}
        assert set(load_ports) == {"electricity_demand"}
        assert load_ports["electricity_demand"].direction == "in"


def test_sync_ports_preserves_same_carrier_multi_port(
    db_factory: tuple[sessionmaker, int], project_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同载能多端口按名同步(codex 复审 N1): 不按 carrier+direction 合并。

    旧实现把端口压缩成 {carrier: direction}, 同 electric 载体的两个不同名输入
    端口会互相覆盖, 服务器只保留一个, 前端真实句柄找不到对应服务器端口 →
    合法连线被判定端口缺失。新实现以 YAML 端口名 (name) 精确匹配, 两个
    electric 输入端口(名称不同)都应保留; 同名端口 id 不变, 既有连接保持。
    """
    from iesplan.devices.contracts2 import DeviceInfo, DeviceModelDocument

    def _doc(device_id: str) -> DeviceModelDocument:
        return DeviceModelDocument(device=DeviceInfo(id=device_id))

    # 假文档: 同 electric 载体两个不同名输入端口 + 一个 heat 输出
    fake_spec = _doc("ies.device.dual_inlet")
    # 源设备: electric out + heat out(供连接)
    source_spec = _doc("ies.device.source_box")

    def _dual_ports(spec, params=None):
        return [
            {"carrier": "electricity", "direction": "in", "name": "electric_a", "capacity_ref": None},
            {"carrier": "electricity", "direction": "in", "name": "electric_b", "capacity_ref": None},
            {"carrier": "heat", "direction": "out", "name": "heat_out", "capacity_ref": None},
        ]

    def _source_ports(spec, params=None):
        return [
            {"carrier": "electricity", "direction": "out", "name": "electric_out", "capacity_ref": None},
            {"carrier": "heat", "direction": "out", "name": "heat_out", "capacity_ref": None},
        ]

    monkeypatch.setattr(svc, "_descriptor_ports", lambda spec, params=None: (
        _source_ports(spec) if spec.device.id == "ies.device.source_box" else _dual_ports(spec)
    ))
    # create_device/validate_device_params 从注册表读文档; 假设备不在注册表,
    # mock 模块级 get_device 返回对应假文档(覆盖创建/校验/同步三路径)
    monkeypatch.setattr(svc, "get_device", lambda type_id: (
        source_spec if type_id == "ies.device.source_box" else fake_spec
    ))
    factory, admin_id = db_factory
    with factory() as session:
        # 创建两个设备: 源(source_box) + 目标(dual_inlet)
        src = svc.create_device(session, project_id, "ies.device.source_box", "Src1", created_by=admin_id)
        dst = svc.create_device(session, project_id, fake_spec.device.id, "Dual1", created_by=admin_id)
        src_ports = {p.name: p for p in svc.get_device_ports(session, src.id)}
        dst_ports = {p.name: p for p in svc.get_device_ports(session, dst.id)}
        assert set(dst_ports) == {"electric_a", "electric_b", "heat_out"}
        assert dst_ports["electric_a"].port_type == "electric"
        assert dst_ports["electric_b"].port_type == "electric"

        # 连接: 源 electric_out → 目标 electric_a(真实端口 id)
        conn = svc.connect(
            session, project_id,
            from_port_id=src_ports["electric_out"].id,
            to_port_id=dst_ports["electric_a"].id,
            attrs={"conn_type": "electric_line"},
        )

        # 更新参数触发 _sync_ports_for_params: 双 electric 端口按名保留(id 不变)
        electric_a_id = dst_ports["electric_a"].id
        electric_b_id = dst_ports["electric_b"].id
        svc.update_device(session, project_id, dst.id, params={})
        ports_after = {p.name: p for p in svc.get_device_ports(session, dst.id)}
        assert set(ports_after) == {"electric_a", "electric_b", "heat_out"}
        assert ports_after["electric_a"].id == electric_a_id  # 同名端口 id 稳定
        assert ports_after["electric_b"].id == electric_b_id

        # 连接仍保留(未裁剪端口不被误删连接)
        conns = model_domain.list_connections(session, dst.graph_id)
        assert any(c.id == conn.id for c in conns)

        # 裁剪: 期望集合移除 electric_b(同载能但名称不同) → 只有 electric_b 被删,
        # electric_a 及其连接保留
        monkeypatch.setattr(
            svc, "_descriptor_ports",
            lambda spec, params=None: [
                {"carrier": "electricity", "direction": "in", "name": "electric_a", "capacity_ref": None},
                {"carrier": "heat", "direction": "out", "name": "heat_out", "capacity_ref": None},
            ],
        )
        svc.update_device(session, project_id, dst.id, params={})
        ports_final = {p.name: p for p in svc.get_device_ports(session, dst.id)}
        assert set(ports_final) == {"electric_a", "heat_out"}
        assert ports_final["electric_a"].id == electric_a_id  # 保留端口 id 不变
        # electric_a 的连接未被裁剪删除
        conn_after = model_domain.list_connections(session, dst.graph_id)
        assert any(c.id == conn.id for c in conn_after)

        # 补建: 期望集合重新包含 electric_b → 按 name 补建新端口
        monkeypatch.setattr(svc, "_descriptor_ports", _dual_ports)
        svc.update_device(session, project_id, dst.id, params={})
        ports_rebuilt = {p.name: p for p in svc.get_device_ports(session, dst.id)}
        assert set(ports_rebuilt) == {"electric_a", "electric_b", "heat_out"}
        assert ports_rebuilt["electric_a"].id == electric_a_id  # 既有端口复用
        assert ports_rebuilt["electric_b"].id != electric_b_id  # 补建为新 id


def test_create_device_kind_and_fidelity(db_factory: tuple[sessionmaker, int], project_id: int) -> None:
    """存量/新增与模型精度落库: is_existing=True → kind=existing, precision=low。"""
    factory, admin_id = db_factory
    with factory() as session:
        device = svc.create_device(
            session,
            project_id,
            PV,
            "PV1",
            is_existing=True,
            model_precision="low",
            created_by=admin_id,
        )
        assert device.kind == "existing"
        assert device.model_fidelity == "low"
        assert device.device_type == "pv"  # 粗分类别(01 §4.2 CHECK)
        assert device.params["type_detail"] == PV  # 细分类别
        # 位置存布局键, 不参与参数校验
        svc.update_device(session, project_id, device.id, position={"x": 12.5, "y": 34})
        fetched = session.get(Device, device.id)
        assert fetched is not None
        assert fetched.params["__layout"]["position"] == {"x": 12.5, "y": 34}


def test_create_device_duplicate_name_rejected(client: TestClient, project_id: int) -> None:
    """同图内设备名唯一(409 冲突)。"""
    _create_device(client, project_id, PV, "PV1")
    resp = client.post(
        f"/api/projects/{project_id}/model/devices",
        json={"device_type": PV, "name": "PV1"},
        headers=_headers(client),
    )
    assert resp.status_code == 409


def test_update_device_name_and_params(client: TestClient, project_id: int) -> None:
    """更新设备名称/参数; 非法参数被拒。"""
    created = _create_device(client, project_id, PV, "PV1", params={"rated_capacity_kwp": 200})
    device_id = created["device"]["id"]
    resp = client.put(
        f"/api/projects/{project_id}/model/devices/{device_id}",
        json={"name": "PV-A", "params": {"rated_capacity_kwp": 300, "temp_coeff": -0.005}},
        headers=_headers(client),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["device"]["name"] == "PV-A"
    assert body["device"]["params"]["rated_capacity_kwp"] == 300
    # 越界参数更新被拒
    resp = client.put(
        f"/api/projects/{project_id}/model/devices/{device_id}",
        json={"params": {"rated_capacity_kwp": 2000000}},
        headers=_headers(client),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == PARAM_RNG_OUT
    # 仅更新位置，布局与模型内容分别保持正确
    resp = client.put(
        f"/api/projects/{project_id}/model/devices/{device_id}",
        json={"position": {"x": 5, "y": 6}},
        headers=_headers(client),
    )
    assert resp.status_code == 200
    graph = _get_graph(client, project_id)
    assert graph["layout"]["devices"][str(device_id)] == {"position": {"x": 5.0, "y": 6.0}}


# ---------------------------------------------------------------------------
# 连接写入
# ---------------------------------------------------------------------------


def _grid_and_load(client: TestClient, project_id: int) -> tuple[dict, dict, dict, dict]:
    """创建电网 + 电负荷, 返回 (grid, grid_out, load, load_in)。"""
    grid = _create_device(client, project_id, GRID, "Grid")
    load = _create_device(client, project_id, LOAD, "Load", params={"peak_power_kw": 800})
    return grid, _port(grid, "electricity_import"), load, _port(load, "electricity_demand")


def test_connect_success(client: TestClient, project_id: int) -> None:
    """源(电网 out)→汇(负荷 in)连接成功, 连接类型由能源类型推导。"""
    grid, grid_out, _load, load_in = _grid_and_load(client, project_id)
    body = _connect(client, project_id, grid_out["id"], load_in["id"])
    conn = body["connection"]
    assert conn["conn_type"] == "electric_line"
    assert conn["from_port_id"] == grid_out["id"]
    assert conn["to_port_id"] == load_in["id"]
    # 图内可读回
    graph = _get_graph(client, project_id)
    assert len(graph["connections"]) == 1
    assert graph["connections"][0]["loss_rate"] == 0.0


def test_connect_energy_mismatch_rejected_with_location(client: TestClient, project_id: int) -> None:
    """能源类型不一致被拒(400, 定位到连接与端口)。"""
    grid = _create_device(client, project_id, GRID, "Grid")
    heat = _create_device(client, project_id, HEAT_LOAD, "Heat", params={"peak_heat_kw": 500})
    resp = client.post(
        f"/api/projects/{project_id}/model/connections",
        json={"from_port_id": _port(grid, "electricity_import")["id"], "to_port_id": _port(heat, "heat_demand")["id"]},
        headers=_headers(client),
    )
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "CONN-PORT-001"
    assert err["location"]["object_type"] == "connection"
    assert err["location"]["from_port_id"] == _port(grid, "electricity_import")["id"]
    assert err["location"]["to_port_id"] == _port(heat, "heat_demand")["id"]
    assert err["params"]["from_port_type"] == "electric"
    assert err["params"]["to_port_type"] == "thermal"


def test_connect_direction_invalid_rejected(client: TestClient, project_id: int) -> None:
    """方向不兼容被拒: 负荷端口(in)不能作为源。"""
    grid = _create_device(client, project_id, GRID, "Grid")
    load = _create_device(client, project_id, LOAD, "Load", params={"peak_power_kw": 800})
    resp = client.post(
        f"/api/projects/{project_id}/model/connections",
        json={
            "from_port_id": _port(load, "electricity_demand")["id"],
            "to_port_id": _port(grid, "electricity_import")["id"],
        },
        headers=_headers(client),
    )
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "CONN-PORT-002"
    assert err["location"]["field"] == "direction"


def test_connect_duplicate_rejected(client: TestClient, project_id: int) -> None:
    """重复连接被拒(同图同两端同类型)。"""
    grid, grid_out, _load, load_in = _grid_and_load(client, project_id)
    _connect(client, project_id, grid_out["id"], load_in["id"])
    resp = client.post(
        f"/api/projects/{project_id}/model/connections",
        json={"from_port_id": grid_out["id"], "to_port_id": load_in["id"]},
        headers=_headers(client),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "CONN-DUP-001"


def test_connect_self_loop_rejected(client: TestClient, project_id: int) -> None:
    """自环被拒: 电池双向端口连自身。"""
    battery = _create_device(client, project_id, BATTERY, "B1")
    port = _port(battery, "electricity")
    resp = client.post(
        f"/api/projects/{project_id}/model/connections",
        json={"from_port_id": port["id"], "to_port_id": port["id"]},
        headers=_headers(client),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "CONN-DUP-002"


def test_connect_cross_project_rejected(
    client: TestClient, db_factory: tuple[sessionmaker, int], project_id: int
) -> None:
    """跨项目连接被拒: 端口不属于同一项目图。"""
    factory, admin_id = db_factory
    other_project = _create_project(factory, "其他项目", admin_id)
    grid, grid_out, _load, _load_in = _grid_and_load(client, project_id)
    other_load = _create_device(
        client, other_project, LOAD, "Load2", params={"peak_power_kw": 800}
    )
    resp = client.post(
        f"/api/projects/{project_id}/model/connections",
        json={
            "from_port_id": grid_out["id"],
            "to_port_id": _port(other_load, "electricity_demand")["id"],
        },
        headers=_headers(client),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "CONN-PORT-003"


def test_connect_bidirectional_battery_to_load(client: TestClient, project_id: int) -> None:
    """双向端口可作源: 电池 → 负荷。"""
    battery = _create_device(client, project_id, BATTERY, "B1")
    load = _create_device(client, project_id, LOAD, "Load", params={"peak_power_kw": 800})
    body = _connect(client, project_id, _port(battery, "electricity")["id"], _port(load, "electricity_demand")["id"])
    assert body["connection"]["conn_type"] == "electric_line"


def test_connect_attrs_and_update_connection(
    db_factory: tuple[sessionmaker, int], project_id: int
) -> None:
    """连接属性(容量/损耗率/扩展参数)落库与更新(服务层)。"""
    factory, admin_id = db_factory
    with factory() as session:
        grid = svc.create_device(session, project_id, GRID, "Grid", created_by=admin_id)
        load = svc.create_device(
            session,
            project_id,
            LOAD,
            "Load",
            params={"peak_power_kw": 800},
            created_by=admin_id,
        )
        grid_port = session.scalars(
            sa.select(Port).where(Port.device_id == grid.id, Port.name == "electricity_import")
        ).one()
        load_port = session.scalars(
            sa.select(Port).where(Port.device_id == load.id, Port.name == "electricity_demand")
        ).one()
        conn = svc.connect(
            session, project_id, grid_port.id, load_port.id, attrs={"capacity": 500, "loss_rate": 0.05}
        )
        assert conn.capacity == 500
        assert conn.loss_rate == 0.05
        # 仅更新损耗率: 容量保留(值对象语义: 以返回记录为准, 不依赖 ORM 原地变更)
        conn = svc.update_connection(session, project_id, conn.id, {"loss_rate": 0.1})
        assert conn.capacity == 500
        assert conn.loss_rate == 0.1


def test_disconnect(client: TestClient, project_id: int) -> None:
    """断开连接并删除连接行。"""
    grid, grid_out, _load, load_in = _grid_and_load(client, project_id)
    conn = _connect(client, project_id, grid_out["id"], load_in["id"])
    conn_id = conn["connection"]["id"]
    resp = client.delete(
        f"/api/projects/{project_id}/model/connections/{conn_id}", headers=_headers(client)
    )
    assert resp.status_code == 200
    graph = _get_graph(client, project_id)
    assert graph["connections"] == []
    # 再删 → 404
    resp = client.delete(
        f"/api/projects/{project_id}/model/connections/{conn_id}", headers=_headers(client)
    )
    assert resp.status_code == 404


def test_delete_device_cascades(client: TestClient, project_id: int) -> None:
    """删除设备级联删除端口与连接。"""
    grid, grid_out, load, load_in = _grid_and_load(client, project_id)
    _connect(client, project_id, grid_out["id"], load_in["id"])
    device_id = load["device"]["id"]
    resp = client.delete(
        f"/api/projects/{project_id}/model/devices/{device_id}", headers=_headers(client)
    )
    assert resp.status_code == 200
    graph = _get_graph(client, project_id)
    assert len(graph["devices"]) == 1
    assert graph["devices"][0]["id"] == grid["device"]["id"]
    assert graph["connections"] == []
    assert all(p["device_id"] != device_id for p in graph["ports"])


# ---------------------------------------------------------------------------
# 拓扑校验
# ---------------------------------------------------------------------------


def test_validate_orphan_device_warning(client: TestClient, project_id: int) -> None:
    """孤立设备(无任何连接)警告: 电网 + 负荷均未连线 → 2 条 CONN-NODE-001。"""
    _grid_and_load(client, project_id)
    diags = _get_validate(client, project_id)
    orphan = [d for d in diags if d["code"] == CONN_NODE_ORPHAN]
    assert len(orphan) == 2
    assert all(d["severity"] == "warning" for d in orphan)
    assert all(d["location"]["object_type"] == "device" for d in orphan)


def test_validate_unconnected_load_warning(client: TestClient, project_id: int) -> None:
    """未连接负荷警告: 负荷端口仅作连接源(异常数据)时发出 CONN-NODE-001。"""
    graph = {
        "devices": [
            {"id": 1, "device_type": LOAD, "name": "L"},
            {"id": 2, "device_type": GRID, "name": "G"},
        ],
        "ports": [
            {
                "id": 10,
                "device_id": 1,
                "port_type": "electric",
                "direction": "in",
                "name": "electric_in",
            },
            {
                "id": 20,
                "device_id": 2,
                "port_type": "electric",
                "direction": "out",
                "name": "electric_out",
            },
        ],
        "connections": [
            {"id": 100, "from_port_id": 10, "to_port_id": 20, "conn_type": "electric_line"}
        ],
    }
    diags = svc.validate_topology(graph)
    incoming = [
        d for d in diags if d.code == CONN_NODE_ORPHAN and d.location.get("field") == "incoming"
    ]
    assert len(incoming) == 1
    assert incoming[0].location["object_id"] == "1"


def test_validate_energy_imbalance_error(client: TestClient, project_id: int) -> None:
    """能源不平衡: 只建热负荷(热载体只有汇无源) → PARAM-UNIT-003 错误。"""
    _create_device(client, project_id, HEAT_LOAD, "Heat", params={"peak_heat_kw": 500})
    diags = _get_validate(client, project_id)
    imbalanced = [d for d in diags if d["code"] == PARAM_UNIT_INCONSISTENT]
    assert len(imbalanced) == 1
    assert imbalanced[0]["severity"] == "error"
    assert imbalanced[0]["location"]["field"] == "carrier:thermal"


def test_validate_balanced_bidirectional_no_imbalance(client: TestClient, project_id: int) -> None:
    """双向端口视为源汇兼具: 仅电池 → 无能源不平衡错误。"""
    _create_device(client, project_id, BATTERY, "B1")
    diags = _get_validate(client, project_id)
    assert PARAM_UNIT_INCONSISTENT not in _codes(diags)


def test_validate_duplicate_connection_error() -> None:
    """重复连接错误(同图同两端同类型多条): PARAM-CONF-001。"""
    graph = {
        "devices": [
            {"id": 1, "device_type": GRID, "name": "G"},
            {"id": 2, "device_type": LOAD, "name": "L"},
        ],
        "ports": [
            {
                "id": 10,
                "device_id": 1,
                "port_type": "electric",
                "direction": "out",
                "name": "electric_out",
            },
            {
                "id": 20,
                "device_id": 2,
                "port_type": "electric",
                "direction": "in",
                "name": "electric_in",
            },
        ],
        "connections": [
            {"id": 100, "from_port_id": 10, "to_port_id": 20, "conn_type": "electric_line"},
            {"id": 101, "from_port_id": 10, "to_port_id": 20, "conn_type": "electric_line"},
        ],
    }
    diags = svc.validate_topology(graph)
    assert any(d.code == PARAM_CONFLICT and d.severity == "error" for d in diags)


def test_validate_valid_system_clean(client: TestClient, project_id: int) -> None:
    """完整可运行系统(电网+负荷连通)校验无错误无警告。"""
    grid, grid_out, _load, load_in = _grid_and_load(client, project_id)
    _connect(client, project_id, grid_out["id"], load_in["id"])
    diags = _get_validate(client, project_id)
    assert diags == []


def test_validate_unregistered_type_diagnostic(
    db_factory: tuple[sessionmaker, int], project_id: int
) -> None:
    """存图内类型未注册 → CONN-TYPE-002 错误(而非 404)。"""
    factory, admin_id = db_factory
    with factory() as session:
        svc.create_device(session, project_id, PV, "PV1", created_by=admin_id)
        device = session.scalars(sa.select(Device)).one()
        device.params = dict(device.params, type_detail="ies.device.wind_turbine")
        session.commit()
    client_app = create_app()
    client_app.include_router(model_api.model_router)

    def _override() -> Iterator[Session]:
        with factory() as session:
            yield session

    client_app.dependency_overrides[get_request_db] = _override
    with TestClient(client_app, raise_server_exceptions=False) as test_client:
        resp = test_client.get(f"/api/projects/{project_id}/model/validate", headers=_headers(test_client))
        assert resp.status_code == 200
        diags = resp.json()["diagnostics"]
    assert any(d["code"] == CONN_TYPE_UNREGISTERED for d in diags)


# ---------------------------------------------------------------------------
# 图序列化
# ---------------------------------------------------------------------------


def test_graph_serialization_roundtrip(client: TestClient, project_id: int) -> None:
    """图序列化往返: 设备+端口+连接+布局结构完整。"""
    grid = _create_device(client, project_id, GRID, "Grid", position={"x": 10, "y": 20})
    load = _create_device(client, project_id, LOAD, "Load", params={"peak_power_kw": 800})
    _connect(client, project_id, _port(grid, "electricity_import")["id"], _port(load, "electricity_demand")["id"])
    graph = _get_graph(client, project_id)
    assert graph["graph_id"] is not None
    assert graph["name"] == "工作图"
    assert len(graph["devices"]) == 2
    assert len(graph["ports"]) == 3  # 电网进出口 2 + 负荷需求 1
    assert len(graph["connections"]) == 1
    dev = graph["devices"][0]
    assert dev["device_type"] == GRID
    assert dev["category"] == "source"
    assert dev["kind"] == "new"
    assert dev["model_fidelity"] == "medium"
    assert dev["params"]["type_detail"] == GRID
    port = graph["ports"][0]
    assert set(port) == {"id", "device_id", "port_type", "direction", "name", "capacity", "params"}
    conn = graph["connections"][0]
    assert conn["loss_rate"] == 0.0
    # 布局对象按设备 id 索引
    assert graph["layout"]["devices"][str(grid["device"]["id"])] == {"position": {"x": 10.0, "y": 20.0}}
    assert str(load["device"]["id"]) not in graph["layout"]["devices"]


def test_empty_graph_for_new_project(client: TestClient, project_id: int) -> None:
    """未建模项目返回显式空态(has_graph=False + graph_id=None), 而非 404。"""
    graph = _get_graph(client, project_id)
    # 0.3.0 C2: 空态语义显式化, 前端据 has_graph 渲染初始画布, 不再从 graph_id 猜测
    assert graph["has_graph"] is False
    assert graph["graph_id"] is None
    assert graph["devices"] == []
    assert graph["layout"] == {"devices": {}}

    # 建模后 has_graph=True(正常流程不受影响)
    _create_device(client, project_id, PV, "PV1")
    graph2 = _get_graph(client, project_id)
    assert graph2["has_graph"] is True
    assert graph2["graph_id"] is not None
    assert len(graph2["devices"]) == 1


def test_get_graph_missing_project_404(client: TestClient) -> None:
    """项目不存在 → 404(带定位)。"""
    resp = client.get("/api/projects/999/model", headers=_headers(client))
    assert resp.status_code == 404
    assert resp.json()["error"]["location"]["object_type"] == "project"


# ---------------------------------------------------------------------------
# 服务层参数校验
# ---------------------------------------------------------------------------


def test_validate_device_params_service() -> None:
    """参数校验(服务层): 合法/未声明拒绝/越界/未注册。"""
    # 缺省键不校验(2.0 无引用参数, source.data_ref 为唯一数据路径)
    diags = svc.validate_device_params(LOAD, {"peak_power_kw": 10}, device_id=7)
    assert diags == []
    # 未声明的引用键 → PARAM-UNIT-002(无数据绑定延后机制)
    diags = svc.validate_device_params(
        LOAD, {"peak_power_kw": 10, "load_profile": "dataset:e_load"}, device_id=7
    )
    assert len(diags) == 1 and diags[0].code == PARAM_UNIT_MISMATCH
    # 越界(两处同时报告)
    diags = svc.validate_device_params(HP, {"cop": 9.0, "rated_heat_kw": -5}, device_id=7)
    assert sum(d.code == PARAM_RNG_OUT for d in diags) == 2
    # 未声明参数 → PARAM-UNIT-002
    diags = svc.validate_device_params(HP, {"mode": "tilt"}, device_id=7)
    assert len(diags) == 1
    # 未注册类型抛 NotFoundError
    with pytest.raises(svc.NotFoundError):
        svc.validate_device_params("ies.device.nope", {}, device_id=7)


def test_working_graph_create_idempotent_and_unique(
    db_factory: tuple[sessionmaker, int]
) -> None:
    """工作图并发防重: 重复取/建返回同一张图, 每项目仅一张工作图(唯一索引兜底)。"""
    factory, admin_id = db_factory
    project_id = _create_project(factory, "竞态项目", admin_id)
    with factory() as session:
        g1 = svc.get_or_create_working_graph(session, project_id, admin_id)
        g2 = svc.get_or_create_working_graph(session, project_id, admin_id)
        assert g1.id == g2.id
        # 项目下仅一张工作图(挂草稿); 版本图为空
        rows = session.scalars(
            sa.select(SystemGraph).where(
                SystemGraph.project_id == project_id, SystemGraph.draft_id.is_not(None)
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].id == g1.id
        # 部分唯一索引 uq_system_graphs_working 已随 create_all 建立(SQLite 方言)
        assert any(
            i.name == "uq_system_graphs_working" for i in Base.metadata.tables["system_graphs"].indexes
        )
