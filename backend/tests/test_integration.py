"""集成测试：核心业务链（提交、快照与预检）。

链路: 注册/登录(管理员+工程师) → 创建项目 → 添加设备(电网/光伏/热泵/锅炉/
制冷机/电池/负荷)与连接 → 生成内置样例数据集 → 绑定数据集 → 保存计算配置 +
财务基准确认 → 提交方案评价任务 → Worker 领取(租约/尝试状态先行可见)。

另抽查核心语义：草稿乐观锁(409)、归档后禁止编辑、删除需显式确认、任务幂等键复用、导出权限门禁、存储视图权限、财务基准校验门禁（见 manual/developer-guide/zh-CN/contracts.md §HTTP 语义； manual/developer-guide/zh-CN/domain-model.md §项目聚合/对象生命周期； manual/developer-guide/zh-CN/ARCHITECTURE_CONSTITUTION.md §8/§10/§16）。

执行方式: 内存 SQLite(StaticPool) + 临时对象存储目录 + 内存队列,
不依赖部署 Postgres/Redis(与基础层降级路径一致)。
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# 单文件运行时的安全网: 固定 SQLite, 避免 iesplan.main 启动期误连部署 Postgres
os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan.api.deps import get_request_db  # noqa: E402
from iesplan.config import settings  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.identity.persistence import User  # noqa: E402
from iesplan.application import identity  # noqa: E402
from iesplan.application import worker as worker_app  # noqa: E402
from iesplan.tasks import queue  # noqa: E402
# 注册任务侧表: 结果域 persistence 在导入期即登记 evidence_packages(外键指向
# 任务侧表), 而任务域 persistence 为延迟装载; 建表前必须显式导入, 否则
# create_all 报 NoReferencedTableError(单文件运行时必现)。
from iesplan.tasks.persistence import Task as _TaskForDDL  # noqa: E402,F401

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "Admin12345"
ENGINEER_PASSWORD = "Eng12345"

# 设备类型 id(与 core/registry 注册表一致)
D_GRID = "ies.device.grid_connection"
D_PV = "ies.device.pv"
D_BATTERY = "ies.device.battery"
D_HP = "ies.device.heat_pump"
D_BOILER = "ies.device.gas_boiler"
D_CHILLER = "ies.device.electric_chiller"
D_ELOAD = "ies.device.electric_load"
D_HLOAD = "ies.device.heat_load"
D_CLOAD = "ies.device.cooling_load"


# ---------------------------------------------------------------------------
# 测试环境
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """会话级 SQLite 内存引擎(StaticPool: 所有会话共享同一连接)。"""
    eng = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(engine: Engine) -> Iterator[None]:
    """每个测试结束后清空全部表, 队列强制内存后端(避免测试间串扰)。"""
    queue.force_memory()
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def db_session(engine: Engine) -> Iterator[Session]:
    """函数级共享会话(服务与测试共用, 端点内 commit)。"""
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


@pytest.fixture()
def db(db_session: Session) -> Session:
    """fixture 别名(db = db_session, 与测试函数命名习惯一致)。"""
    return db_session


@pytest.fixture()
def client(engine: Engine, db_session: Session, tmp_path: Path) -> Iterator[TestClient]:
    """测试客户端: create_app() 全量路由, 替换 get_request_db, 对象存储指向临时目录。"""
    settings.data_dir = tmp_path
    app = create_app()

    def _override_request_db() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_request_db] = _override_request_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        test_client._test_db = db_session  # type: ignore[attr-defined]  # 供 _h 登录辅助使用
        test_client._auth_tokens = {}  # type: ignore[attr-defined]  # 窗口凭证缓存
        yield test_client


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _h(client: TestClient, user_id: int) -> dict[str, str]:
    """业务端点认证头: 以窗口会话登录(同一 client 内按用户缓存)。

    每用户单活动窗口: 重复登录会使旧凭证降级为 takeover_pending,
    因此与 _login 共享缓存, 同一用户在整个测试中只登录一次。
    """
    db: Session = client._test_db  # type: ignore[attr-defined]
    cache: dict[int, str] = client._auth_tokens  # type: ignore[attr-defined]
    token = cache.get(user_id)
    if token is None:
        user = db.get(User, user_id)
        assert user is not None
        roles = identity.user_roles(db, user)
        password = ADMIN_PASSWORD if "admin" in roles else ENGINEER_PASSWORD
        resp = client.post(
            "/api/auth/login", json={"username": user.username, "password": password}
        )
        assert resp.status_code == 200, resp.text
        token = resp.json()["token"]
        cache[user_id] = token
    return {"Authorization": f"Bearer {token}"}


def _auth(token: str) -> dict[str, str]:
    """会话认证头(auth/objects 端点)。"""
    return {"Authorization": f"Bearer {token}"}


def _seed_admin(db: Session) -> int:
    """创建内置管理员(测试内不再强制改密, 等价于已改密的正式管理员,
    避免 C-02 强制改密门禁阻断管理端点)。"""
    user = identity.create_user(
        db, ADMIN_USERNAME, ADMIN_PASSWORD, role="admin", display_name="管理员",
        force_password_change=False,
    )
    return user.id


def _login(client: TestClient, username: str, password: str) -> dict[str, Any]:
    """登录并返回 {token, user}(同时写入 _h 缓存, 保持单活动窗口)。"""
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    cache: dict[int, str] = client._auth_tokens  # type: ignore[attr-defined]
    cache[body["user"]["id"]] = body["token"]
    return {"token": body["token"], "user": body["user"]}


def _register_engineer(
    client: TestClient, admin_token: str, username: str = "eng_li"
) -> int:
    """管理员开启自助注册 → 注册工程师 → 返回用户 id。"""
    resp = client.put(
        "/api/auth/settings", json={"registration_enabled": True}, headers=_auth(admin_token)
    )
    assert resp.status_code == 200, resp.text
    resp = client.post(
        "/api/auth/register",
        json={"username": username, "password": ENGINEER_PASSWORD, "display_name": f"工程师{username}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _seed_engineer_direct(client: TestClient, db: Session) -> int:
    """直接创建工程师(绕开注册开关, 测试快捷路径)。"""
    user = identity.create_user(
        db, f"eng_{time.time():.0f}", ENGINEER_PASSWORD,
        role="engineer", force_password_change=False,
    )
    return user.id


def _create_project(client: TestClient, user_id: int, name: str = "集成测试项目") -> int:
    """工程师创建项目, 返回 project_id。"""
    resp = client.post(
        "/api/projects",
        json={"name": name, "currency": "CNY", "baseline_resolution": "1h", "baseline_start_year": 2026, "baseline_scenario_mode": "single"},
        headers=_h(client, user_id),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["my_role"] == "owner"
    project = body["project"]
    assert project["status"] == "active"
    return project["id"]


def _add_device(
    client: TestClient, user_id: int, project_id: int, revision: int,
    device_type: str, name: str, params: dict,
) -> int:
    """经草稿语义命令声明 2.0 名键设备实例(设备名即装配引用)，返回新修订号。"""
    resp = client.put(
        f"/api/projects/{project_id}/draft",
        json={
            "expected_revision": revision,
            "commands": [
                {
                    "id": f"add-{name}", "unit": "model", "type": "model.upsert_device",
                    "payload": {
                        "name": name, "device_type": device_type, "kind": "new",
                        "params": params, "position": {"x": 1.0, "y": 1.0},
                    },
                }
            ],
        },
        headers=_h(client, user_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def _add_connection(
    client: TestClient, user_id: int, project_id: int, revision: int, name: str,
    from_device: str, from_port: str, to_device: str, to_port: str,
) -> int:
    """经草稿语义命令创建 2.0 名键连接(端口名为真实 interface ID)，返回新修订号。"""
    resp = client.put(
        f"/api/projects/{project_id}/draft",
        json={
            "expected_revision": revision,
            "commands": [
                {
                    "id": f"conn-{name}", "unit": "model", "type": "model.upsert_connection",
                    "payload": {
                        "name": name, "from_device": from_device, "from_port": from_port,
                        "to_device": to_device, "to_port": to_port,
                    },
                }
            ],
        },
        headers=_h(client, user_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def _create_sample_dataset(client: TestClient, user_id: int, project_id: int) -> tuple[int, int]:
    """创建数据集并生成内置样例版本, 返回 (dataset_id, dataset_version_id)。"""
    resp = client.post(
        f"/api/projects/{project_id}/datasets",
        json={"name": "上海样例", "description": "集成测试内置样例"},
        headers=_h(client, user_id),
    )
    assert resp.status_code == 201, resp.text
    dataset_id = resp.json()["dataset"]["id"]
    resp = client.post(
        f"/api/projects/{project_id}/datasets/{dataset_id}/sample",
        params={"resolution": "1h", "region": "shanghai"},
        headers=_h(client, user_id),
    )
    assert resp.status_code == 201, resp.text
    version = resp.json()["dataset_version"]
    assert version["provenance"]["source_category"] == "builtin_sample"
    assert version["quality_report"]["checks"]["row_count"]["ok"] is True
    return dataset_id, version["id"]


def _bind_dataset(client: TestClient, user_id: int, project_id: int, revision: int, version_id: int) -> int:
    """草稿命令绑定数据集版本, 返回新修订号。"""
    resp = client.put(
        f"/api/projects/{project_id}/draft",
        json={
            "expected_revision": revision,
            "commands": [
                {
                    "id": f"bind-{version_id}",
                    "project_id": project_id,
                    "unit": "dataset",
                    "type": "dataset.bind",
                    "payload": {"dataset_version_id": version_id, "role": "primary"},
                }
            ],
        },
        headers=_h(client, user_id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"][0]["status"] == "applied"
    return body["revision"]


#: 现行装配边界要求的显式计算声明(无静默默认): mode/generator/solver/
#: time_axis 缺一即阻断(ASM-CONV-001)。PUT /config 同步的 calc_config 节不含
#: 这些字段, 夹具必须经 config.patch 显式声明, 不得依赖回退。
EXPLICIT_CALC_PATCH = {
    "mode": "fixed_operation",
    "generator": "ies.generator.highs_milp@1.0.0",
    "solver": "ies.solver.highs@1.7.2",
    "time_axis": {"resolution": "1h", "start": "2025-01-01T00:00:00Z"},
}


def _set_explicit_calc(client: TestClient, user_id: int, project_id: int, revision: int) -> int:
    """经 config.patch 显式声明计算装配字段, 返回新修订号。"""
    resp = client.put(
        f"/api/projects/{project_id}/draft",
        json={
            "expected_revision": revision,
            "commands": [
                {"id": "calc-explicit", "project_id": project_id, "unit": "config",
                 "type": "config.patch", "payload": EXPLICIT_CALC_PATCH}
            ],
        },
        headers=_h(client, user_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def _declare_finance(db: Session, user_id: int, pid: int) -> None:
    """装配闸门要求项目固定有效财务快照: 登记地区 Profile 并引用。"""
    from test_config_revisions import PROFILE_PAYLOAD

    from iesplan.application import configuration as config_cases

    config_cases.register_finance_profile(db, dict(PROFILE_PAYLOAD), user_id)
    config_cases.set_project_finance_profile(
        db, pid, PROFILE_PAYLOAD["profile"]["id"], user_id
    )


def _assembly_commands(instances: dict[str, Any]) -> list[dict[str, Any]]:
    """装配两节起草命令: finance_binding 实例全覆盖(机械测试声明无成本,
    真实项目按成本逐实例声明 costed；tariff 无计费绑定；
    均为显式声明而非系统默认；planning 改走冻结规划修订投影)。"""
    return [
        {
            "id": "c-finbind", "unit": "assembly",
            "type": "assembly.set_finance_binding",
            "payload": {"instances": instances},
        },
        {
            "id": "c-tariff", "unit": "assembly",
            "type": "assembly.set_tariff_bindings",
            "payload": {"bindings": {}},
        },
    ]


def _declare_assembly_sections(
    client: TestClient, user_id: int, project_id: int, revision: int,
    instances: dict[str, Any] | None = None,
) -> int:
    """经 assembly.* 起草命令显式声明装配节, 返回新修订号。"""
    resp = client.put(
        f"/api/projects/{project_id}/draft",
        json={
            "expected_revision": revision,
            "commands": _assembly_commands(instances or {}),
        },
        headers=_h(client, user_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def _finance_instances(names: list[str]) -> dict[str, Any]:
    """装配实例引用(设备名 slug)全覆盖, 显式声明无成本(type:none)。"""
    return {name: {"type": "none"} for name in names}


#: 全链路设备名(即装配实例引用)
_ALL_DEVICES = ["grid", "pv", "battery", "hp", "boiler", "chiller", "eload", "hload", "cload"]


#: 设备预定义接口的显式常量边界(机械测试用；真实项目绑定实测数据文件)。
#: 2.0 契约要求每个 predefined 接口必须声明来源；constant 为一等来源模式，
#: 经与 GUI 同构的 model.upsert_device 起草命令显式声明(非静默默认)。
CONSTANT_BINDINGS = {
    "pv": {"solar_irradiance": 800.0, "ambient_temperature": 25.0},
    "eload": {"electricity_demand": 800.0},
    "hload": {"heat_demand": 800.0},
    "cload": {"cool_demand": 800.0},
}


def _declare_constant_bindings(
    client: TestClient, user_id: int, project_id: int, revision: int,
) -> int:
    """经 model.upsert_device 显式声明设备预定义接口常量来源, 返回新修订号。"""
    resp = client.put(
        f"/api/projects/{project_id}/draft",
        json={
            "expected_revision": revision,
            "commands": [
                {
                    "id": f"bind-const-{idx}",
                    "project_id": project_id,
                    "unit": "model",
                    "type": "model.upsert_device",
                    "payload": {"name": name, "params": dict(params)},
                }
                for idx, (name, params) in enumerate(CONSTANT_BINDINGS.items())
            ],
        },
        headers=_h(client, user_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


#: 合法 12 字段 CalculationConfig(与 test_config_api 同构的最小有效载荷)
CALC_PAYLOAD: dict[str, Any] = {
    "schema": "ies.calculation-config",
    "schema_version": "1.0.0",
    "mode": "planning",
    "predictions": [],
    "generator": "ies.generator.highs_milp@1.0.0",
    "solver": "ies.solver.highs@1.7.2",
    "precision": "medium",
    "convergence": None,
    "time_limit_seconds": 600,
    "options": {},
    "seed": None,
    "outputs": ["solution"],
}


def _save_config(client: TestClient, user_id: int, project_id: int) -> dict[str, Any]:
    """保存计算配置 revision(首次保存), 返回 {calculation_config, revision}。"""
    resp = client.put(
        f"/api/projects/{project_id}/config",
        json={"calculation_config": CALC_PAYLOAD, "expected_revision": None},
        headers=_h(client, user_id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"calculation_config", "revision"}, body
    assert body["revision"] == 1
    return body


#: 显式确认假设(调用方明确提供, 不从计算配置派生)
CONFIRM_ASSUMPTIONS: dict[str, Any] = {
    "discount_rate": 0.08,
    "tax_rate": 0.25,
    "project_years": 20,
    "depreciation_years": 10,
    "currency": "CNY",
    "irr_floor": 0.08,
}


def _baseline_confirm(client: TestClient, user_id: int, project_id: int) -> None:
    """财务基准确认: 调用方显式提供假设原文。"""
    resp = client.post(
        f"/api/projects/{project_id}/validation/baseline-confirm",
        json={"assumptions": CONFIRM_ASSUMPTIONS},
        headers=_h(client, user_id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["confirmed"] is True
    assert body["confirmed_by"] == user_id
    assert body["confirmed_at"]


def _submit_calc_task(
    client: TestClient, user_id: int, project_id: int, idempotency_key: str | None = None,
    *, config_revision: int,
) -> tuple[int, int]:
    """提交方案评价任务(calc), 返回 (task_id, status_code)。"""
    payload: dict[str, Any] = {
        "task_type": "calc", "config": {"config_revision": config_revision},
        "evaluation": {
            "evaluation_years": 1, "construction_period": 0,
            "operation_years": 1, "discount_rate": 0.08,
            "construction_payments": [{"t": 0, "ratio": 1}],
            "maintenance_expenses": [],
        },
    }
    if idempotency_key is not None:
        payload["idempotency_key"] = idempotency_key
    resp = client.post(f"/api/projects/{project_id}/tasks", json=payload, headers=_h(client, user_id))
    if "task" not in resp.json():
        raise AssertionError(f"任务创建失败: {resp.status_code} {resp.text[:1500]}")
    task = resp.json()["task"]
    return task["id"], resp.status_code


def _acquire_task(db: Session, task_id: int) -> Any:
    """Worker 领取: 经 application.worker 用例(领取事务已提交, 租约/尝试状态可见)。"""
    claim = worker_app.acquire_attempt(db, task_id, "it-worker-1")
    assert claim is not None, "任务领取失败"
    return claim


def _get_task(client: TestClient, user_id: int, project_id: int, task_id: int) -> dict[str, Any]:
    """任务详情。"""
    resp = client.get(f"/api/projects/{project_id}/tasks/{task_id}", headers=_h(client, user_id))
    assert resp.status_code == 200, resp.text
    return resp.json()["task"]


# ---------------------------------------------------------------------------
# 全链路准备: 建好一个"可提交任务"的项目
# ---------------------------------------------------------------------------


def _prepare_project(
    client: TestClient, db: Session, user_id: int, *, confirm_baseline: bool = True
) -> dict[str, Any]:
    """全链路准备: 项目 + 7 类设备 + 连接 + 样例数据集绑定 + 配置保存 + 基准确认。"""
    pid = _create_project(client, user_id)

    # 设备名即装配实例引用(2.0 名键事实); 端口名为注册表真实 interface ID
    revision = 1
    revision = _add_device(
        client, user_id, pid, revision, D_GRID, "grid",
        {"max_import_power_kw": 10000, "max_export_power_kw": 0},
    )
    revision = _add_device(client, user_id, pid, revision, D_PV, "pv", {"rated_capacity_kwp": 500})
    revision = _add_device(
        client, user_id, pid, revision, D_BATTERY, "battery",
        {"capacity_kwh": 200, "rated_power_kw": 100, "initial_soc": 0.5,
         "min_soc": 0.1, "max_soc": 0.9, "charge_efficiency": 0.95, "discharge_efficiency": 0.95},
    )
    revision = _add_device(
        client, user_id, pid, revision, D_HP, "hp",
        {"rated_heat_kw": 1000, "cop": 3.0},
    )
    revision = _add_device(
        client, user_id, pid, revision, D_BOILER, "boiler",
        {"rated_heat_kw": 1000, "thermal_efficiency": 0.92},
    )
    revision = _add_device(
        client, user_id, pid, revision, D_CHILLER, "chiller",
        {"rated_cooling_kw": 1000, "cop": 4.0},
    )
    revision = _add_device(
        client, user_id, pid, revision, D_ELOAD, "eload", {"peak_power_kw": 800}
    )
    revision = _add_device(
        client, user_id, pid, revision, D_HLOAD, "hload", {"peak_heat_kw": 800}
    )
    revision = _add_device(
        client, user_id, pid, revision, D_CLOAD, "cload", {"peak_cooling_kw": 800}
    )

    # 连接(2.0 语义: 只有 out/in/bidirectional 接口可进入 connections;
    # predefined 为数据入口永不连边, blind 不得连接。负荷需求经数据绑定,
    # 可连接输入口必须由供电边 feeding；光伏余电经电网出口消纳)
    revision = _add_connection(
        client, user_id, pid, revision, "c1", "grid", "electricity_import", "battery", "electricity",
    )
    revision = _add_connection(
        client, user_id, pid, revision, "c2", "grid", "electricity_import", "hp", "electricity_in",
    )
    revision = _add_connection(
        client, user_id, pid, revision, "c3", "grid", "electricity_import", "chiller", "electricity_in",
    )
    revision = _add_connection(
        client, user_id, pid, revision, "c4", "pv", "electric_out", "grid", "electricity_export",
    )
    revision = _add_connection(
        client, user_id, pid, revision, "c5", "grid", "electricity_import", "eload", "electricity_in",
    )
    revision = _add_connection(
        client, user_id, pid, revision, "c6", "boiler", "heat_out", "hload", "heat_in",
    )
    revision = _add_connection(
        client, user_id, pid, revision, "c7", "chiller", "cool_out", "cload", "cool_in",
    )

    # 草稿内容断言: 设备 9 台(含 3 负荷), 连接 7 条(含 3 条负荷供电边)
    view = client.get(f"/api/projects/{pid}", headers=_h(client, user_id)).json()
    model = view["draft"]["content"]["model"]
    assert len(model["devices"]) == 9
    assert len(model["connections"]) == 7

    # 模型校验: 模型/拓扑/参数类无 error/blocking(拓扑完整; 数据/财务门禁
    # 此时尚未就绪，不在断言范围)
    resp = client.post(f"/api/projects/{pid}/validation/run", headers=_h(client, user_id))
    assert resp.status_code == 200, resp.text
    diags = resp.json()["report"]["diagnostics"]
    model_bad = [
        d for d in diags
        if d["severity"] in ("error", "blocking")
        and (d["code"].startswith("VALID-MODEL") or d["code"].startswith("PARAM") or d["code"].startswith("CONN"))
    ]
    assert not model_bad, model_bad

    # 数据集: 内置样例(1h, 8760 行) + 绑定
    dataset_id, version_id = _create_sample_dataset(client, user_id, pid)
    revision = _bind_dataset(client, user_id, pid, revision, version_id)

    # 保存计算配置 revision + 显式财务引用 + 显式计算声明 +
    # 显式装配两节 + 最小规划修订(储能容量单变量) + 财务基准确认
    # (草稿修订链式递增, 不得复用旧修订号)
    from iesplan.application import configuration as config_cases

    config_body = _save_config(client, user_id, pid)
    config_revision = config_body["revision"]
    _declare_finance(db, user_id, pid)
    bat_ref = "battery"
    config_cases.save_planning_config(
        db,
        pid,
        {
            "objective": {"sense": "minimize", "expression": "0"},
            "variables": {
                "cap": {
                    "device_ref": bat_ref,
                    "parameter": "capacity_kwh",
                    "lower_bound": "0",
                    "upper_bound": "1000",
                    "unit": "kWh",
                },
            },
            "constraints": {},
        },
        None,
        user_id,
    )
    revision = _set_explicit_calc(client, user_id, pid, revision)
    revision = _declare_assembly_sections(
        client, user_id, pid, revision,
        instances=_finance_instances(_ALL_DEVICES),
    )
    revision = _declare_constant_bindings(client, user_id, pid, revision)
    if confirm_baseline:
        _baseline_confirm(client, user_id, pid)

    return {
        "project_id": pid,
        "dataset_id": dataset_id,
        "dataset_version_id": version_id,
        "revision": revision,
        "config_revision": config_revision,
    }


# ---------------------------------------------------------------------------
# 测试 1a: 任务提交/快照/预检正常
# ---------------------------------------------------------------------------


def test_task_submit_snapshot_preguard(client: TestClient, db: Session) -> None:
    """注册/登录 → 项目 → 设备/连接 → 数据集 → 配置/基线 → 提交方案评价任务。

    断言: 空项目预检阻断提交(设备/数据/配置缺失, 0.15 S0 起不再含旧基准确
    认审计); 提交后任务 queued 且计算快照已固化。
    （计算入口显式不可用：本测试止于提交，不期待真实求解；
    执行语义见测试 1b。）
    """
    admin_id = _seed_admin(db)
    admin_login = _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    # 管理员受审操作: 开启自助注册
    eng_id = _register_engineer(client, admin_login["token"])
    assert eng_id != admin_id
    eng_login = _login(client, "eng_li", ENGINEER_PASSWORD)
    assert eng_login["user"]["role"] == "engineer"

    # 预检门禁：空项目(缺设备/数据/配置)阻断提交（见 manual/developer-guide/zh-CN/domain-model.md §规划、财务与计算配置； manual/developer-guide/zh-CN/ARCHITECTURE_CONSTITUTION.md §4.4 assembly）
    pid = _create_project(client, eng_id, name="预检门禁项目")
    resp = client.post(f"/api/projects/{pid}/validation/run", headers=_h(client, eng_id))
    assert resp.status_code == 200, resp.text
    report = resp.json()["report"]
    assert report["blocks_submit"] is True
    codes = {d["code"] for d in report["diagnostics"]}
    assert {"VALID-MODEL-001", "VALID-DATA-001", "VALID-MODEL-002"} <= codes
    assert resp.json()["stored"]["object_id"] > 0  # 报告已持久化为内容寻址对象

    # 全链路准备(含基准确认)
    ctx = _prepare_project(client, db, eng_id)

    # 提交方案评价任务
    task_id, status_code = _submit_calc_task(
        client, eng_id, ctx["project_id"], idempotency_key="it-chain-1",
        config_revision=ctx["config_revision"],
    )
    assert status_code == 201
    task = _get_task(client, eng_id, ctx["project_id"], task_id)
    assert task["status"] == "queued"
    assert task["calc_snapshot_id"] is not None  # 快照已固化

    # 领取: 租约/尝试状态先行可见(领取事务已提交), 任务 running
    claim = _acquire_task(db, task_id)
    task = _get_task(client, eng_id, ctx["project_id"], task_id)
    assert task["status"] == "running", task
    assert worker_app.verify_lease(db, claim.attempt_id, claim.lease_token) is not None


# ---------------------------------------------------------------------------
# 测试 1a-2: 0.15 S0 — 完整输入无需旧基准确认审计即可提交
# ---------------------------------------------------------------------------


def test_submit_without_legacy_confirm_audit(client: TestClient, db: Session) -> None:
    """0.15 S0 入口修复: 未调用旧 baseline-confirm 审计的完整输入可提交。

    评价确认保留在计算提交边界(类型化校验, 服务端记录确认人/时间并冻结);
    预检与提交都不读取旧确认审计证据。评价缺失仍拒绝(400 TASK-REQ-015)。
    """
    eng_id = _seed_engineer_direct(client, db)
    ctx = _prepare_project(client, db, eng_id, confirm_baseline=False)
    pid = ctx["project_id"]

    task_id, status_code = _submit_calc_task(
        client, eng_id, pid, idempotency_key="it-s0-no-confirm",
        config_revision=ctx["config_revision"],
    )
    assert status_code == 201
    task = _get_task(client, eng_id, pid, task_id)
    assert task["status"] == "queued"
    assert task["calc_snapshot_id"] is not None

    # 评价缺失仍拒绝(提交边界类型化校验, 与旧确认审计无关)
    resp = client.post(
        f"/api/projects/{pid}/tasks",
        json={
            "task_type": "calc", "idempotency_key": "it-s0-no-eval",
            "config": {"config_revision": ctx["config_revision"]},
        },
        headers=_h(client, eng_id),
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "TASK-REQ-015"


# ---------------------------------------------------------------------------
# 测试 2: 项目语义（乐观锁/归档/删除确认/版本）（见 manual/developer-guide/zh-CN/contracts.md §HTTP 语义 409 冲突； manual/developer-guide/zh-CN/domain-model.md §项目聚合）
# ---------------------------------------------------------------------------


def test_project_semantics(client: TestClient, db: Session) -> None:
    """草稿乐观锁 409、归档后禁止编辑、删除须 confirm、版本创建/恢复。"""
    eng_id = _seed_engineer_direct(client, db)
    pid = _create_project(client, eng_id)

    # 乐观锁: 期望修订不符 → 409
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": 99, "commands": []},
        headers=_h(client, eng_id),
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["message_key"] == "ies.diag.store.save_conflict"

    # 正常推进一个修订(合法命令: 语言设置)
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={
            "expected_revision": 1,
            "commands": [
                {"id": "lang-1", "project_id": pid, "unit": "project",
                 "type": "project.set_language", "payload": {"language": "zh-CN"}}
            ],
        },
        headers=_h(client, eng_id),
    )
    assert resp.status_code == 200
    assert resp.json()["revision"] == 2

    # 整批重试幂等: 同修订返回原结果且不再递增
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={
            "expected_revision": 1,
            "commands": [
                {"id": "lang-1", "project_id": pid, "unit": "project",
                 "type": "project.set_language", "payload": {"language": "zh-CN"}}
            ],
        },
        headers=_h(client, eng_id),
    )
    assert resp.status_code == 200
    assert resp.json()["revision"] == 2  # 不递增

    # 归档 → 编辑 409
    resp = client.post(f"/api/projects/{pid}/archive", headers=_h(client, eng_id))
    assert resp.status_code == 200
    assert resp.json()["project"]["status"] == "archived"
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": 2, "commands": []},
        headers=_h(client, eng_id),
    )
    assert resp.status_code == 409
    resp = client.post(f"/api/projects/{pid}/unarchive", headers=_h(client, eng_id))
    assert resp.status_code == 200

    # 删除必须显式确认(0.2.0 B4): 未确认 → 400; 空布尔 confirm 不足以确认 → 400;
    # 项目名匹配 → 204
    resp = client.request(
        "DELETE", f"/api/projects/{pid}", json={"confirm": False}, headers=_h(client, eng_id)
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PROJ-DEL-001"
    resp = client.request(
        "DELETE", f"/api/projects/{pid}", json={"confirm": True}, headers=_h(client, eng_id)
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PROJ-DEL-002"
    resp = client.request(
        "DELETE", f"/api/projects/{pid}",
        json={"confirm": True, "name": "集成测试项目"}, headers=_h(client, eng_id),
    )
    assert resp.status_code == 204

    # 版本语义: 创建版本 → 版本列表 → 恢复(不倒写历史)
    pid2 = _create_project(client, eng_id, name="版本语义项目")
    resp = client.post(
        f"/api/projects/{pid2}/versions",
        json={"name": "v1", "reason": "manual_save"},
        headers=_h(client, eng_id),
    )
    assert resp.status_code == 201
    version_id = resp.json()["version"]["id"]
    resp = client.post(
        f"/api/projects/{pid2}/versions/{version_id}/restore",
        json={"name": "恢复副本"},
        headers=_h(client, eng_id),
    )
    assert resp.status_code == 200
    assert resp.json()["version"]["version_no"] == 2
    resp = client.get(f"/api/projects/{pid2}/versions", headers=_h(client, eng_id))
    assert resp.status_code == 200 and len(resp.json()["versions"]) == 2


# ---------------------------------------------------------------------------
# 测试 3: 任务语义（去重/幂等/取消）（见 manual/developer-guide/zh-CN/ARCHITECTURE_CONSTITUTION.md §12 快照、任务与结果； manual/developer-guide/zh-CN/contracts.md §快照与异步契约）
# ---------------------------------------------------------------------------


def test_task_semantics(client: TestClient, db: Session) -> None:
    """无键提交 → 422; 幂等键命中 → 200 replayed; 不同键同输入建新任务; 任务可取消。"""
    eng_id = _seed_engineer_direct(client, db)
    ctx = _prepare_project(client, db, eng_id)

    # 无键提交直接拒绝(宪法 §5.5)
    resp = client.post(
        f"/api/projects/{ctx['project_id']}/tasks",
        json={"task_type": "calc", "config": {"horizon_years": 1}},
        headers=_h(client, eng_id),
    )
    assert resp.status_code == 422

    task_id, status_code = _submit_calc_task(
        client, eng_id, ctx["project_id"], idempotency_key="it-sem-0",
        config_revision=ctx["config_revision"],
    )
    assert status_code == 201

    # 不同键、同输入 → 新任务(不再有快照级去重)
    task_id2, status_code2 = _submit_calc_task(
        client, eng_id, ctx["project_id"], idempotency_key="it-sem-1",
        config_revision=ctx["config_revision"],
    )
    assert task_id2 != task_id and status_code2 == 201

    # 幂等键: 同键命中返回既有任务并标记 replayed
    task_id3, status_code3 = _submit_calc_task(
        client, eng_id, ctx["project_id"], idempotency_key="it-idem-1",
        config_revision=ctx["config_revision"],
    )
    assert status_code3 == 201
    task_id4, status_code4 = _submit_calc_task(
        client, eng_id, ctx["project_id"], idempotency_key="it-idem-1",
        config_revision=ctx["config_revision"],
    )
    assert task_id4 == task_id3 and status_code4 == 200

    # 取消 queued 任务 → cancelled
    resp = client.post(
        f"/api/projects/{ctx['project_id']}/tasks/{task_id}/cancel",
        json={"reason": "测试取消"},
        headers=_h(client, eng_id),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["cancel_status"] == "cancelled"
    # 终态任务再取消 → 409
    resp = client.post(
        f"/api/projects/{ctx['project_id']}/tasks/{task_id}/cancel",
        json={},
        headers=_h(client, eng_id),
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# 测试 4: 权限门禁（见 manual/developer-guide/zh-CN/ARCHITECTURE_CONSTITUTION.md §16 安全与审计； manual/developer-guide/zh-CN/contracts.md §HTTP 语义）
# ---------------------------------------------------------------------------


def test_permission_gates(client: TestClient, db: Session) -> None:
    """非所有者禁止导出项目包(403); 非管理员禁止存储视图(403); 未认证 401。"""
    admin_id = _seed_admin(db)
    admin_login = _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    eng_id = _register_engineer(client, admin_login["token"])
    eng_login = _login(client, "eng_li", ENGINEER_PASSWORD)
    viewer_id = _register_engineer(client, admin_login["token"], username="eng_view")
    ctx = _prepare_project(client, db, eng_id)

    # 查看者(非所有者)导出包 → 403
    resp = client.post(f"/api/projects/{ctx['project_id']}/exports/package", headers=_h(client, viewer_id))
    assert resp.status_code == 403, resp.text

    # 未认证访问业务端点 → 401(先清空 cookie jar, 避免残留会话 Cookie)
    client.cookies.clear()
    resp = client.get(f"/api/projects/{ctx['project_id']}", headers={})
    assert resp.status_code == 401

    # 非管理员访问存储视图 → 403(会话认证)
    resp = client.get("/api/admin/storage", headers=_auth(eng_login["token"]))
    assert resp.status_code == 403
    assert resp.json()["error"]["message_key"] == "ies.diag.perm.denied"

    # 管理员(窗口会话认证)访问存储/健康 → 200
    # (先清除 TestClient cookie jar, 避免工程师会话 cookie 影响后续请求)
    client.cookies.clear()
    resp = client.get("/api/admin/storage", headers=_h(client, admin_id))
    assert resp.status_code == 200
    body = resp.json()
    # 单一 StorageStatusDto（无兼容并集，见 manual/developer-guide/zh-CN/modules/storage.md §边界 及 manual/developer-guide/zh-CN/ARCHITECTURE_CONSTITUTION.md §2.2 正确性优先于兼容） 删除 STO-07 旧存储规格编号
    assert "objects" in body and "refs" in body and "capacity" in body
    resp = client.get("/api/admin/health", headers=_h(client, admin_id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["liveness"]["ok"] is True and body["readiness"]["db"] is True
    assert "queue" in body and "storage" in body and "metrics" in body
    # 存储 health provider 的抽样校验节（独立聚合，见 manual/developer-guide/zh-CN/modules/storage.md §失败语义）
    assert "verify" in body["storage"] and "checked" in body["storage"]["verify"]

    # 管理端审计：项目创建事件可查询（见 manual/developer-guide/zh-CN/ARCHITECTURE_CONSTITUTION.md §16 安全与审计； manual/developer-guide/zh-CN/domain-model.md §身份、权限和审计）
    resp = client.get(
        "/api/admin/audit", params={"action": "project.created"}, headers=_h(client, admin_id)
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert any(item["entity_id"] == ctx["project_id"] for item in items)


# ---------------------------------------------------------------------------
# 测试 5: 配置校验门禁（见 manual/developer-guide/zh-CN/domain-model.md §规划、财务与计算配置； manual/developer-guide/zh-CN/modules/assembly.md）
# ---------------------------------------------------------------------------


def test_config_gate(client: TestClient, db: Session) -> None:
    """非法计算配置保存被拒(422 标准信封), 合法 12 字段配置可保存并读回。"""
    eng_id = _seed_engineer_direct(client, db)
    pid = _create_project(client, eng_id)

    resp = client.put(
        f"/api/projects/{pid}/config",
        json={"calculation_config": {"mode": "planning"}, "expected_revision": None},
        headers=_h(client, eng_id),
    )
    assert resp.status_code == 422
    body = resp.json()
    assert set(body) == {"error"}, body
    assert body["error"]["code"] == "CONFIG-VAL-001"

    # 未保存时读取 404(无默认配置可用)
    resp = client.get(f"/api/projects/{pid}/config", headers=_h(client, eng_id))
    assert resp.status_code == 404

    # 合法保存 → revision 1 → 读回一致
    body = _save_config(client, eng_id, pid)
    assert body["calculation_config"]["generator"] == "ies.generator.highs_milp@1.0.0"
    resp = client.get(f"/api/projects/{pid}/config", headers=_h(client, eng_id))
    assert resp.status_code == 200, resp.text
    assert resp.json() == body


# ---------------------------------------------------------------------------
# 测试 6: 数据集校验语义（见 manual/developer-guide/zh-CN/domain-model.md §数据集； manual/developer-guide/zh-CN/file-formats.md §通用书写规则）
# ---------------------------------------------------------------------------


def test_dataset_validation(client: TestClient, db: Session) -> None:
    """坏数据上传 → 400 + diagnostics 定位(行数); 模板可下载。"""
    eng_id = _seed_engineer_direct(client, db)
    pid = _create_project(client, eng_id)
    resp = client.post(
        f"/api/projects/{pid}/datasets", json={"name": "坏数据"}, headers=_h(client, eng_id)
    )
    dataset_id = resp.json()["dataset"]["id"]

    bad_csv = (
        b"timestamp,e_load,h_load\n"
        b"2025-01-01 00:00,1.0,2.0\n"
        b"2025-01-01 01:00,1.0,2.0\n"  # 行数不足 8760 → DATA-TS-004
    )
    resp = client.post(
        f"/api/projects/{pid}/datasets/{dataset_id}/versions",
        data={"resolution": "1h", "utc_offset_minutes": "480"},
        files={"file": ("bad.csv", bad_csv, "text/csv")},
        headers=_h(client, eng_id),
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["message_key"] == "ies.error.data_validation_failed"
    assert any(d["code"] == "DATA-TS-004" for d in body["error"]["params"]["diagnostics"])

    # 标准模板下载(公开; UTF-8 BOM 起始)
    resp = client.get("/api/datasets/template", params={"resolution": "1h"})
    assert resp.status_code == 200
    assert resp.content.lstrip(b"\xef\xbb\xbf").startswith(b"#")
