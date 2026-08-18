"""全链路集成测试: 覆盖 11 个业务单元端到端流程(RPD 关键语义抽查)。

链路: 注册/登录(管理员+工程师) → 创建项目 → 添加设备(电网/光伏/热泵/锅炉/
制冷机/电池/负荷)与连接 → 生成内置样例数据集 → 绑定数据集 → 保存计算配置 +
财务基准确认 → 提交方案评价任务 → Worker 真实执行(evaluate_plan 全算例,
8760 步) → 任务完成 → 四维评估 → 选择结果 → Excel 导出 → 项目包导出/导入。

另抽查 RPD 语义: 草稿乐观锁(409)、归档后禁止编辑、删除需显式确认、
任务同快照去重(200 duplicate)、非所有者禁止导出包(403)、非管理员禁止
存储视图(403)、财务基准确认门禁(未确认 → 预检阻断)。

执行方式: 内存 SQLite(StaticPool) + 临时对象存储目录 + 内存队列,
不依赖部署 Postgres/Redis(与基础层降级路径一致)。
"""

from __future__ import annotations

import io
import os
import time
import zipfile
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

from iesplan.config import settings  # noqa: E402
from iesplan.db import Base, get_db  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.models.identity import User  # noqa: E402
from iesplan.services import identity, queue  # noqa: E402
from iesplan.services import package as package_service  # noqa: E402
from iesplan.services import tasks as tasks_service  # noqa: E402
from iesplan.worker import runner  # noqa: E402

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
    """测试客户端: create_app() 全量路由, 替换 get_db, 对象存储指向临时目录。"""
    settings.data_dir = tmp_path
    app = create_app()

    def _override_get_db() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _h(user_id: int) -> dict[str, str]:
    """业务端点认证头(阶段实现: X-User-Id 模拟认证主体)。"""
    return {"X-User-Id": str(user_id)}


def _auth(token: str) -> dict[str, str]:
    """会话认证头(auth/objects 端点)。"""
    return {"Authorization": f"Bearer {token}"}


def _seed_admin(db: Session) -> int:
    """创建内置管理员(首登强制改密, 与 seed_admin 语义一致)。"""
    user = identity.create_user(db, ADMIN_USERNAME, ADMIN_PASSWORD, role="admin", display_name="管理员")
    return user.id


def _login(client: TestClient, username: str, password: str) -> dict[str, Any]:
    """登录并返回 {token, user}。"""
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    body = resp.json()
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
        json={"name": name, "currency": "CNY", "utc_offset_minutes": 480},
        headers=_h(user_id),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["my_role"] == "owner"
    project = body["project"]
    assert project["status"] == "active"
    return project["id"]


def _add_device(
    client: TestClient, user_id: int, project_id: int, device_type: str, name: str, params: dict
) -> dict[str, Any]:
    """创建设备并返回 {device, ports}。"""
    resp = client.post(
        f"/api/projects/{project_id}/model/devices",
        json={"device_type": device_type, "name": name, "params": params, "position": {"x": 1.0, "y": 1.0}},
        headers=_h(user_id),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _add_connection(
    client: TestClient, user_id: int, project_id: int, from_port_id: int, to_port_id: int
) -> None:
    """创建连接(源端口 → 汇端口)。"""
    resp = client.post(
        f"/api/projects/{project_id}/model/connections",
        json={"from_port_id": from_port_id, "to_port_id": to_port_id, "attrs": {}},
        headers=_h(user_id),
    )
    assert resp.status_code == 201, resp.text


def _port(entry: dict[str, Any], port_type: str) -> int:
    """按能源类型取设备端口 id(每设备每载体至多一个端口)。"""
    for p in entry["ports"]:
        if p["port_type"] == port_type:
            return p["id"]
    raise AssertionError(f"设备缺少 {port_type} 端口: {entry}")


def _create_sample_dataset(client: TestClient, user_id: int, project_id: int) -> tuple[int, int]:
    """创建数据集并生成内置样例版本, 返回 (dataset_id, dataset_version_id)。"""
    resp = client.post(
        f"/api/projects/{project_id}/datasets",
        json={"name": "上海样例", "description": "集成测试内置样例"},
        headers=_h(user_id),
    )
    assert resp.status_code == 201, resp.text
    dataset_id = resp.json()["dataset"]["id"]
    resp = client.post(
        f"/api/projects/{project_id}/datasets/{dataset_id}/sample",
        params={"resolution": "1h", "region": "shanghai"},
        headers=_h(user_id),
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
        headers=_h(user_id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"][0]["status"] == "applied"
    return body["revision"]


def _save_config(client: TestClient, user_id: int, project_id: int, revision: int) -> dict[str, Any]:
    """读取默认配置并保存(带乐观锁修订), 返回 {config, version}。"""
    resp = client.get(f"/api/projects/{project_id}/config", headers=_h(user_id))
    assert resp.status_code == 200, resp.text
    default_config = resp.json()["config"]
    assert default_config["irr_floor"] == 0.08  # RPD: 最低 IRR 硬约束默认 8%
    resp = client.put(
        f"/api/projects/{project_id}/config",
        json={"config": default_config, "expected_revision": revision},
        headers=_h(user_id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] is not None and body["diagnostics"] == []
    return body


def _baseline_confirm(client: TestClient, user_id: int, project_id: int, config: dict[str, Any]) -> None:
    """财务基准确认: 假设取当前配置导出的关键假设(与预检口径一致)。"""
    econ = (config.get("parameters") or {}).get("economic") or {}
    assumptions = {
        "discount_rate": econ.get("discount_rate"),
        "tax_rate": econ.get("tax_rate"),
        "project_years": econ.get("project_years"),
        "depreciation_years": econ.get("depreciation_years"),
        "currency": "CNY",
        "irr_floor": config.get("irr_floor"),
    }
    resp = client.post(
        f"/api/projects/{project_id}/validation/baseline-confirm",
        json={"assumptions": assumptions},
        headers=_h(user_id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["confirmed"] is True and len(body["assumptions_hash"]) == 64
    assert body["confirmed_by"] == user_id


def _submit_calc_task(
    client: TestClient, user_id: int, project_id: int, idempotency_key: str | None = None
) -> tuple[int, int]:
    """提交方案评价任务(calc), 返回 (task_id, status_code)。"""
    payload: dict[str, Any] = {"task_type": "calc", "config": {"horizon_years": 1}}
    if idempotency_key is not None:
        payload["idempotency_key"] = idempotency_key
    resp = client.post(f"/api/projects/{project_id}/tasks", json=payload, headers=_h(user_id))
    task = resp.json()["task"]
    return task["id"], resp.status_code


def _run_task_to_completion(db: Session, task_id: int) -> None:
    """Worker 真实执行: 领取 → runner.run_task(引擎进程内, 快照输入真实)。"""
    claim = tasks_service.claim_and_run(db, task_id, "it-worker-1")
    assert claim is not None, "任务领取失败"
    status = runner.run_task(db, claim, worker_id="it-worker-1", isolate=False)
    assert status == "completed", f"任务执行失败: {status}"


def _get_task(client: TestClient, user_id: int, project_id: int, task_id: int) -> dict[str, Any]:
    """任务详情。"""
    resp = client.get(f"/api/projects/{project_id}/tasks/{task_id}", headers=_h(user_id))
    assert resp.status_code == 200, resp.text
    return resp.json()["task"]


def _result_view(client: TestClient, user_id: int, project_id: int, task_id: int) -> dict[str, Any]:
    """结果视图。"""
    resp = client.get(
        f"/api/projects/{project_id}/tasks/{task_id}/result", headers=_h(user_id)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


# ---------------------------------------------------------------------------
# 全链路准备: 建好一个"可提交任务"的项目
# ---------------------------------------------------------------------------


def _prepare_project(
    client: TestClient, db: Session, user_id: int, *, confirm_baseline: bool = True
) -> dict[str, Any]:
    """全链路准备: 项目 + 7 类设备 + 连接 + 样例数据集绑定 + 配置保存 + 基准确认。"""
    pid = _create_project(client, user_id)

    devices: dict[str, Any] = {}
    devices["grid"] = _add_device(
        client, user_id, pid, D_GRID, "市电电网",
        {"max_import_power_kw": 10000, "max_export_power_kw": 0, "demand_charge": 40},
    )
    devices["pv"] = _add_device(client, user_id, pid, D_PV, "屋顶光伏", {"rated_capacity_kwp": 500})
    devices["battery"] = _add_device(
        client, user_id, pid, D_BATTERY, "储能电池",
        {"capacity_kwh": 200, "rated_power_kw": 100, "initial_soc": 0.5,
         "min_soc": 0.1, "max_soc": 0.9, "charge_efficiency": 0.95, "discharge_efficiency": 0.95},
    )
    devices["hp"] = _add_device(
        client, user_id, pid, D_HP, "热泵",
        {"rated_heat_kw": 1000, "cop": 3.0, "mode": "both"},
    )
    devices["boiler"] = _add_device(
        client, user_id, pid, D_BOILER, "燃气锅炉",
        {"rated_heat_kw": 1000, "thermal_efficiency": 0.92, "gas_price": 3.2},
    )
    devices["chiller"] = _add_device(
        client, user_id, pid, D_CHILLER, "电制冷机",
        {"rated_cooling_kw": 1000, "cop": 4.0},
    )
    devices["eload"] = _add_device(
        client, user_id, pid, D_ELOAD, "电负荷", {"peak_power_kw": 800, "load_profile": "dataset:e_load"}
    )
    devices["hload"] = _add_device(
        client, user_id, pid, D_HLOAD, "热负荷", {"heat_profile": "dataset:h_load"}
    )
    devices["cload"] = _add_device(
        client, user_id, pid, D_CLOAD, "冷负荷", {"cooling_profile": "dataset:c_load"}
    )

    # 连接(电/热/冷 总线拓扑; 电池双向端口作源或汇)
    grid_e = _port(devices["grid"], "electric")
    eload_e = _port(devices["eload"], "electric")
    pv_e = _port(devices["pv"], "electric")
    bat_e = _port(devices["battery"], "electric")
    hp_e = _port(devices["hp"], "electric")
    ch_e = _port(devices["chiller"], "electric")
    _add_connection(client, user_id, pid, grid_e, eload_e)
    _add_connection(client, user_id, pid, grid_e, bat_e)
    _add_connection(client, user_id, pid, bat_e, eload_e)
    _add_connection(client, user_id, pid, pv_e, eload_e)
    _add_connection(client, user_id, pid, grid_e, hp_e)
    _add_connection(client, user_id, pid, grid_e, ch_e)
    _add_connection(client, user_id, pid, _port(devices["hp"], "thermal"),
                    _port(devices["hload"], "thermal"))
    _add_connection(client, user_id, pid, _port(devices["boiler"], "thermal"),
                    _port(devices["hload"], "thermal"))
    _add_connection(client, user_id, pid, _port(devices["hp"], "cooling"),
                    _port(devices["cload"], "cooling"))
    _add_connection(client, user_id, pid, _port(devices["chiller"], "cooling"),
                    _port(devices["cload"], "cooling"))

    # 系统图断言: 设备 9 台(含 3 负荷), 连接 10 条
    resp = client.get(f"/api/projects/{pid}/model", headers=_h(user_id))
    assert resp.status_code == 200, resp.text
    graph = resp.json()
    assert len(graph["devices"]) == 9
    assert len(graph["connections"]) == 10

    # 模型校验: 无 error/blocking(拓扑完整)
    resp = client.get(f"/api/projects/{pid}/model/validate", headers=_h(user_id))
    assert resp.status_code == 200, resp.text
    diags = resp.json()["diagnostics"]
    assert not any(d["severity"] in ("error", "blocking") for d in diags), diags

    # 数据集: 内置样例(1h, 8760 行) + 绑定
    dataset_id, version_id = _create_sample_dataset(client, user_id, pid)
    revision = _bind_dataset(client, user_id, pid, 1, version_id)

    # 保存配置(乐观锁修订) + 财务基准确认
    config_body = _save_config(client, user_id, pid, revision)
    if confirm_baseline:
        _baseline_confirm(client, user_id, pid, config_body["config"])

    return {
        "project_id": pid,
        "dataset_id": dataset_id,
        "dataset_version_id": version_id,
        "config": config_body["config"],
        "revision": revision,
    }


# ---------------------------------------------------------------------------
# 测试 1: 全链路端到端
# ---------------------------------------------------------------------------


def test_full_business_chain(client: TestClient, db: Session) -> None:
    """注册/登录 → 项目 → 设备/连接 → 数据集 → 配置/基线 → 任务 → 结果 → 导出。"""
    admin_id = _seed_admin(db)
    admin_login = _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    # 管理员受审操作: 开启自助注册
    eng_id = _register_engineer(client, admin_login["token"])
    assert eng_id != admin_id
    eng_login = _login(client, "eng_li", ENGINEER_PASSWORD)
    assert eng_login["user"]["role"] == "engineer"

    # 预检门禁: 未确认基线时阻断提交(财务基准确认门禁 RPD 10.2)
    pid = _create_project(client, eng_id, name="预检门禁项目")
    resp = client.post(f"/api/projects/{pid}/validation/run", headers=_h(eng_id))
    assert resp.status_code == 200, resp.text
    report = resp.json()["report"]
    assert report["blocks_submit"] is True
    assert any(d["code"] == "VALID-FIN-001" for d in report["diagnostics"])
    assert resp.json()["stored"]["object_id"] > 0  # 报告已持久化为内容寻址对象

    # 全链路准备(含基准确认)
    ctx = _prepare_project(client, db, eng_id)

    # 提交方案评价任务
    task_id, status_code = _submit_calc_task(client, eng_id, ctx["project_id"], idempotency_key="it-chain-1")
    assert status_code == 201
    task = _get_task(client, eng_id, ctx["project_id"], task_id)
    assert task["status"] == "queued"
    assert task["calc_snapshot_id"] is not None  # 快照已固化

    # Worker 真实执行(evaluate_plan 全算例)
    _run_task_to_completion(db, task_id)
    task = _get_task(client, eng_id, ctx["project_id"], task_id)
    assert task["status"] == "completed", task
    assert task["business_outcome"] == "normal_completion"
    assert len(task["attempts"]) >= 1

    # 结果视图: 四维评估存在 + 逐时引用存在
    result = _result_view(client, eng_id, ctx["project_id"], task_id)
    assert result["task"]["business_outcome"] == "normal_completion"
    assert result["evidence"] is not None and result["evidence"]["status"] == "complete"
    assert result["assessment"] is not None
    four = result["assessment"]["dimensions"]
    assert set(four) == {"physical", "optimality", "financial", "reliability"}
    assert four["physical"] == "pass"
    assert four["optimality"] == "pass"
    assert four["financial"] == "pass"
    assert four["reliability"] in ("unknown", "pass")
    assert result["assessment"]["summary"]  # 派生摘要
    assert result["hourly_refs"], "逐时结果引用缺失"
    assert result["hourly_refs"][0]["fields"] == sorted(result["hourly_refs"][0]["fields"])
    assert "p_grid_buy" in result["hourly_refs"][0]["fields"]
    assert result["selection"] is None  # 尚未选择
    evidence_package_id = result["evidence"]["id"]

    # 逐时结果读取(从对象存储, 行号分页)
    resp = client.get(
        f"/api/projects/{ctx['project_id']}/tasks/{task_id}/result/hourly",
        params={"field": "p_grid_buy", "limit": 48},
        headers=_h(eng_id),
    )
    assert resp.status_code == 200, resp.text
    hourly = resp.json()
    assert len(hourly["values"]) == 48
    assert hourly["values"][0] >= 0  # 购电功率非负
    assert hourly["total_rows"] == 8760

    # 触发新评估(四维, 追加式)
    resp = client.post(
        f"/api/projects/{ctx['project_id']}/tasks/{task_id}/result/assess",
        json={"assessment_type": "full"},
        headers=_h(eng_id),
    )
    assert resp.status_code == 201, resp.text
    assessment = resp.json()["assessment"]
    assert assessment["dimensions"]["physical"] in ("pass", "fail", "unknown")
    assert assessment["evidence_package_id"] == evidence_package_id

    # 评估历史不可变追加
    resp = client.get(
        f"/api/projects/{ctx['project_id']}/tasks/{task_id}/result/assessments", headers=_h(eng_id)
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) >= 2
    ids = [i["id"] for i in items]
    assert len(set(ids)) == len(ids)  # 不可覆盖

    # 选择结果(带差异补丁审计)
    resp = client.post(
        f"/api/projects/{ctx['project_id']}/tasks/{task_id}/result/select",
        json={"solution_id": 0, "selection_type": "adopt", "reason": "集成测试采纳"},
        headers=_h(eng_id),
    )
    assert resp.status_code == 201, resp.text
    selection = resp.json()
    assert selection["selection"]["is_current"] is True
    diff = selection["diff"]
    assert diff is not None
    assert diff["diff_patch"]["params"]["result_adoption"]["solution_index"] == 0
    assert diff["preview_checksum"] is not None  # RPD: 差异补丁带校验值

    # 差异预览(应用前确认, RPD REQ-RESULT-003)
    resp = client.get(
        f"/api/projects/{ctx['project_id']}/tasks/{task_id}/result/diff", headers=_h(eng_id)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["diff"]["preview_checksum"] == diff["preview_checksum"]

    # Excel 报告导出 → 短期授权下载
    resp = client.post(
        f"/api/projects/{ctx['project_id']}/exports/excel",
        json={"evidence_package_id": evidence_package_id, "assessment_id": assessment["id"], "lang": "zh"},
        headers=_h(eng_id),
    )
    assert resp.status_code == 200, resp.text
    excel_meta = resp.json()
    assert excel_meta["size_bytes"] > 0 and excel_meta["sha256"]
    resp = client.get(
        f"/api/projects/{ctx['project_id']}/exports/excel/download",
        params={"token": excel_meta["token"]},
        headers=_h(eng_id),
    )
    assert resp.status_code == 200 and resp.content[:2] == b"PK"  # xlsx 为 zip 容器
    assert len(resp.content) == excel_meta["size_bytes"]

    # 项目包导出(仅所有者) → 下载 → 导入(新项目) → 确认
    resp = client.post(
        f"/api/projects/{ctx['project_id']}/exports/package", headers=_h(eng_id)
    )
    assert resp.status_code == 200, resp.text
    pkg_meta = resp.json()
    assert pkg_meta["object_id"] and pkg_meta["sha256"]
    resp = client.get(
        f"/api/projects/{ctx['project_id']}/exports/package/download",
        params={"token": pkg_meta["token"]},
        headers=_h(eng_id),
    )
    assert resp.status_code == 200
    pkg_bytes = resp.content
    with zipfile.ZipFile(io.BytesIO(pkg_bytes)) as zf:
        names = zf.namelist()
        assert any(n.endswith("manifest.json") for n in names)

    # 导入: 创建提案 → 确认导入(导入者成为新项目所有者)
    importer_id = _register_engineer(client, admin_login["token"], username="eng_wang")
    proposal = package_service.import_proposal(db, db.get(User, importer_id), pkg_bytes)
    assert proposal.review_errors == {}
    imported = package_service.confirm_import(db, db.get(User, importer_id), proposal.id)
    db.commit()
    assert imported.id > ctx["project_id"]
    # 导入项目可读取: 设备/连接/配置/数据集绑定完整迁移
    imported_pid = imported.id
    resp = client.get(f"/api/projects/{imported_pid}", headers=_h(importer_id))
    assert resp.status_code == 200, resp.text
    view = resp.json()
    assert len(view["draft"]["content"]["model"]["devices"]) == 9
    assert len(view["draft"]["content"]["dataset_bindings"]) == 1
    assert view["draft"]["content"]["calc_config"]["params"] != {}

    # 应用选中结果到新草稿(参数差异补丁, RPD 20.12)
    resp = client.post(
        f"/api/projects/{imported_pid}/apply-result",
        json={"diff_patch": diff["diff_patch"], "source_result_id": str(task_id)},
        headers=_h(importer_id),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"]["version_no"] == 2


# ---------------------------------------------------------------------------
# 测试 2: RPD 项目语义(乐观锁 / 归档 / 删除确认 / 版本)
# ---------------------------------------------------------------------------


def test_rpd_project_semantics(client: TestClient, db: Session) -> None:
    """草稿乐观锁 409、归档后禁止编辑、删除须 confirm、版本创建/恢复。"""
    eng_id = _seed_engineer_direct(client, db)
    pid = _create_project(client, eng_id)

    # 乐观锁: 期望修订不符 → 409
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": 99, "commands": []},
        headers=_h(eng_id),
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
        headers=_h(eng_id),
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
        headers=_h(eng_id),
    )
    assert resp.status_code == 200
    assert resp.json()["revision"] == 2  # 不递增

    # 归档 → 编辑 409
    resp = client.post(f"/api/projects/{pid}/archive", headers=_h(eng_id))
    assert resp.status_code == 200
    assert resp.json()["project"]["status"] == "archived"
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": 2, "commands": []},
        headers=_h(eng_id),
    )
    assert resp.status_code == 409
    resp = client.post(f"/api/projects/{pid}/unarchive", headers=_h(eng_id))
    assert resp.status_code == 200

    # 删除必须显式确认: 未确认 → 400; 确认 → 204
    resp = client.request("DELETE", f"/api/projects/{pid}", json={"confirm": False}, headers=_h(eng_id))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PROJ-DEL-001"
    resp = client.request("DELETE", f"/api/projects/{pid}", json={"confirm": True}, headers=_h(eng_id))
    assert resp.status_code == 204

    # 版本语义: 创建版本 → 版本列表 → 恢复(不倒写历史)
    pid2 = _create_project(client, eng_id, name="版本语义项目")
    resp = client.post(
        f"/api/projects/{pid2}/versions",
        json={"name": "v1", "reason": "manual_save"},
        headers=_h(eng_id),
    )
    assert resp.status_code == 201
    version_id = resp.json()["version"]["id"]
    resp = client.post(
        f"/api/projects/{pid2}/versions/{version_id}/restore",
        json={"name": "恢复副本"},
        headers=_h(eng_id),
    )
    assert resp.status_code == 200
    assert resp.json()["version"]["version_no"] == 2
    resp = client.get(f"/api/projects/{pid2}/versions", headers=_h(eng_id))
    assert resp.status_code == 200 and len(resp.json()["versions"]) == 2


# ---------------------------------------------------------------------------
# 测试 3: RPD 任务语义(去重 / 幂等 / 取消)
# ---------------------------------------------------------------------------


def test_rpd_task_semantics(client: TestClient, db: Session) -> None:
    """同快照重复提交 → 200 duplicate; 幂等键命中 → 200 replayed; 任务可取消。"""
    eng_id = _seed_engineer_direct(client, db)
    ctx = _prepare_project(client, db, eng_id)

    task_id, status_code = _submit_calc_task(client, eng_id, ctx["project_id"])
    assert status_code == 201

    # 同快照非终态去重: 返回既有任务并标记 duplicate
    task_id2, status_code2 = _submit_calc_task(client, eng_id, ctx["project_id"])
    assert task_id2 == task_id and status_code2 == 200

    # 幂等键: 同键命中返回既有任务并标记 replayed
    task_id3, status_code3 = _submit_calc_task(
        client, eng_id, ctx["project_id"], idempotency_key="it-idem-1"
    )
    assert status_code3 == 201
    task_id4, status_code4 = _submit_calc_task(
        client, eng_id, ctx["project_id"], idempotency_key="it-idem-1"
    )
    assert task_id4 == task_id3 and status_code4 == 200

    # 取消 queued 任务 → cancelled
    resp = client.post(
        f"/api/projects/{ctx['project_id']}/tasks/{task_id}/cancel",
        json={"reason": "测试取消"},
        headers=_h(eng_id),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["cancel_status"] == "cancelled"
    # 终态任务再取消 → 409
    resp = client.post(
        f"/api/projects/{ctx['project_id']}/tasks/{task_id}/cancel",
        json={},
        headers=_h(eng_id),
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# 测试 4: RPD 权限门禁
# ---------------------------------------------------------------------------


def test_rpd_permission_gates(client: TestClient, db: Session) -> None:
    """非所有者禁止导出项目包(403); 非管理员禁止存储视图(403); 未认证 401。"""
    admin_id = _seed_admin(db)
    admin_login = _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    eng_id = _register_engineer(client, admin_login["token"])
    eng_login = _login(client, "eng_li", ENGINEER_PASSWORD)
    viewer_id = _register_engineer(client, admin_login["token"], username="eng_view")
    ctx = _prepare_project(client, db, eng_id)

    # 查看者(非所有者)导出包 → 403
    resp = client.post(f"/api/projects/{ctx['project_id']}/exports/package", headers=_h(viewer_id))
    assert resp.status_code == 403, resp.text

    # 未认证访问业务端点 → 401
    resp = client.get(f"/api/projects/{ctx['project_id']}", headers={})
    assert resp.status_code == 401

    # 非管理员访问存储视图 → 403(会话认证)
    resp = client.get("/api/admin/storage", headers=_auth(eng_login["token"]))
    assert resp.status_code == 403
    assert resp.json()["error"]["message_key"] == "ies.diag.perm.denied"

    # 管理员(X-User-Id 阶段认证兼容)访问存储/健康 → 200
    # (先清除 TestClient cookie jar, 避免工程师会话 cookie 优先于 X-User-Id)
    client.cookies.clear()
    resp = client.get("/api/admin/storage", headers=_h(admin_id))
    assert resp.status_code == 200
    body = resp.json()
    assert "stats" in body and "sample_verify" in body and "objects" in body
    resp = client.get("/api/admin/health", headers=_h(admin_id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["liveness"]["ok"] is True and body["readiness"]["db"] is True
    assert "queue" in body and "storage" in body and "metrics" in body
    assert "checked" in body  # 存储抽样完整性(并集视图)

    # 管理端审计: 项目创建事件可查询(RPD 13.2)
    resp = client.get(
        "/api/admin/audit", params={"action": "project.created"}, headers=_h(admin_id)
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert any(item["entity_id"] == ctx["project_id"] for item in items)


# ---------------------------------------------------------------------------
# 测试 5: 配置校验门禁(RPD 17.5 REQ-CALC-007)
# ---------------------------------------------------------------------------


def test_rpd_config_gate(client: TestClient, db: Session) -> None:
    """非法配置保存被拒(422 + diagnostics), 合法默认配置可保存。"""
    eng_id = _seed_engineer_direct(client, db)
    pid = _create_project(client, eng_id)

    resp = client.put(
        f"/api/projects/{pid}/config",
        json={"config": {"parameters": {}}, "expected_revision": 1},
        headers=_h(eng_id),
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["count"] >= 1 and any(d["severity"] in ("error", "blocking") for d in body["diagnostics"])

    resp = client.get(f"/api/projects/{pid}/config/default", headers=_h(eng_id))
    assert resp.status_code == 200
    config = resp.json()["config"]
    assert config["algorithm"]["mode"] == "auto"
    assert config["irr_floor"] == 0.08

    # 算法注册表(公开)
    resp = client.get("/api/registry/algorithms")
    assert resp.status_code == 200
    assert resp.json()["algorithms"]


# ---------------------------------------------------------------------------
# 测试 6: 数据集校验语义(RPD 8.3)
# ---------------------------------------------------------------------------


def test_rpd_dataset_validation(client: TestClient, db: Session) -> None:
    """坏数据上传 → 400 + diagnostics 定位(行数); 模板可下载。"""
    eng_id = _seed_engineer_direct(client, db)
    pid = _create_project(client, eng_id)
    resp = client.post(
        f"/api/projects/{pid}/datasets", json={"name": "坏数据"}, headers=_h(eng_id)
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
        headers=_h(eng_id),
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["message_key"] == "ies.error.data_validation_failed"
    assert any(d["code"] == "DATA-TS-004" for d in body["diagnostics"])

    # 标准模板下载(公开; UTF-8 BOM 起始)
    resp = client.get("/api/datasets/template", params={"resolution": "1h"})
    assert resp.status_code == 200
    assert resp.content.lstrip(b"\xef\xbb\xbf").startswith(b"#")
