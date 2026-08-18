"""项目校验单元(U07)测试: 预检报告聚合 / 财务基准确认 / 最近报告 / 阻断不被降级。

- 空项目(缺设备/数据/基准确认)→ blocked;
- 补全设备与数据 → 通过(ok);
- 财务基准确认前后状态变化;
- 阻断错误不被警告降级。
全部测试使用 SQLite :memory:(StaticPool)与临时 data_dir。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from iesplan.config import settings
from iesplan.db import Base, get_db
from iesplan.main import create_app
from iesplan.models.identity import User
from iesplan.services import config as config_service
from iesplan.services import project as project_service
from iesplan.services import validation as validation_service

#: 设备类型常量
GRID = "ies.device.grid_connection"
ELECTRIC_LOAD = "ies.device.electric_load"

#: 测试客户端请求头(X-User-Id = 1 对应种子管理员)
_HEADERS = {"X-User-Id": "1"}

#: 与默认配置一致的财务基准关键假设(预检不产生 VALID-FIN-002 警告)
DEFAULT_ASSUMPTIONS: dict = {
    "discount_rate": 0.08,
    "tax_rate": 0.25,
    "project_years": 20,
    "depreciation_years": 10,
    "currency": "CNY",
    "irr_floor": 0.08,
}


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Iterator[sa.Engine]:
    """SQLite :memory: 引擎(StaticPool: 跨线程共享同一连接)。"""
    eng = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def factory(engine: sa.Engine) -> Iterator[sessionmaker]:
    """会话工厂(expire_on_commit=False) + 种子管理员(首行 id=1)。"""
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        admin = User(username="admin", display_name="管理员")
        session.add(admin)
        session.commit()
    yield factory


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时对象存储根目录(草稿内容/报告/数据集对象落盘)。"""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(settings, "data_dir", d)
    return d


@pytest.fixture()
def client(factory: sessionmaker, data_dir: Path) -> Iterator[TestClient]:
    """挂载校验/模型/数据集/项目路由的测试客户端(DB 依赖覆盖为 SQLite 会话)。"""
    from iesplan.api import model as model_api
    from iesplan.api import projects as project_api
    from iesplan.api import validation as validation_api
    from iesplan.api.datasets import router as datasets_router

    app = create_app()
    app.include_router(model_api.registry_router)
    app.include_router(model_api.model_router)
    app.include_router(datasets_router)
    app.include_router(project_api.router)
    app.include_router(validation_api.router)

    def _override_get_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _create_project(factory: sessionmaker, name: str = "校验测试项目") -> int:
    """经项目服务创建项目(含初始草稿与所有者成员行), 返回项目 id。"""
    with factory() as session:
        admin = session.get(User, 1)
        project = project_service.create_project(session, admin, name)
        session.commit()
        return project.id


def _bind_version(client: TestClient, project_id: int, version_id: int, cmd_id: str = "bind-1") -> dict:
    """经草稿语义命令绑定数据集版本(dataset.bind), 返回响应体。"""
    resp = client.put(
        f"/api/projects/{project_id}/draft",
        json={
            "expected_revision": 1,
            "commands": [
                {
                    "id": cmd_id,
                    "unit": "dataset",
                    "type": "dataset.bind",
                    "payload": {"dataset_version_id": version_id, "role": "main"},
                }
            ],
        },
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _run_report(client: TestClient, project_id: int) -> dict:
    """执行预检并返回报告字典(断言 200)。"""
    resp = client.post(f"/api/projects/{project_id}/validation/run", headers=_HEADERS)
    assert resp.status_code == 200, resp.text
    return resp.json()["report"]


def _diag_codes(diags: list[dict]) -> set[str]:
    """诊断列表 → 诊断码集合。"""
    return {d["code"] for d in diags}


# ---------------------------------------------------------------------------
# 预检聚合
# ---------------------------------------------------------------------------


def test_empty_project_blocked(client: TestClient, factory: sessionmaker) -> None:
    """空项目: 缺设备/数据/基准确认, 应阻断并一次返回全部问题; 未保存配置为警告不阻断。"""
    pid = _create_project(factory)
    report = _run_report(client, pid)
    assert report["status"] == "blocked"
    assert report["blocks_submit"] is True
    codes = _diag_codes(report["diagnostics"])
    # 模型完整性: 缺电网连接 + 缺负荷
    assert "VALID-MODEL-001" in codes
    assert "VALID-MODEL-002" in codes
    # 数据: 未绑定
    assert "VALID-DATA-001" in codes
    # 财务基准确认: 缺失
    assert "VALID-FIN-001" in codes
    # 未保存配置为警告(不降级阻断, 也不阻断)
    assert any(
        d["code"] == "VALID-CONFIG-001" and d["severity"] == "warning" and not d["blocking"]
        for d in report["diagnostics"]
    )
    # summary 计数一致
    summary = report["summary"]
    assert summary["blocking"] >= 4
    assert summary["warning"] >= 1


def test_complete_project_passes(client: TestClient, factory: sessionmaker) -> None:
    """补全设备(电网+负荷连接)、数据(样例)、配置(保存)与基准确认后预检通过。"""
    pid = _create_project(factory)
    # 1) 设备与连接: 电网 → 负荷
    grid = client.post(
        f"/api/projects/{pid}/model/devices",
        json={"device_type": GRID, "name": "电网连接"},
        headers=_HEADERS,
    )
    assert grid.status_code == 201, grid.text
    load = client.post(
        f"/api/projects/{pid}/model/devices",
        json={
            "device_type": ELECTRIC_LOAD,
            "name": "电负荷",
            "params": {"load_profile": "ref:load1"},
        },
        headers=_HEADERS,
    )
    assert load.status_code == 201, load.text
    grid_out = next(p for p in grid.json()["ports"] if p["name"] == "electric_out")
    load_in = next(p for p in load.json()["ports"] if p["name"] == "electric_in")
    conn = client.post(
        f"/api/projects/{pid}/model/connections",
        json={"from_port_id": grid_out["id"], "to_port_id": load_in["id"]},
        headers=_HEADERS,
    )
    assert conn.status_code == 201, conn.text
    # 2) 数据: 内置样例(1h, 上海) + 绑定到草稿(权威绑定来源为 dataset_bindings)
    resp = client.post(
        f"/api/projects/{pid}/datasets", json={"name": "样例数据"}, headers=_HEADERS
    )
    assert resp.status_code == 201, resp.text
    ds_id = resp.json()["dataset"]["id"]
    resp = client.post(
        f"/api/projects/{pid}/datasets/{ds_id}/sample",
        params={"resolution": "1h"},
        headers=_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    version_id = resp.json()["dataset_version"]["id"]
    bind_result = _bind_version(client, pid, version_id)
    # 3) 保存计算配置(消除未保存警告; 绑定后草稿修订已推进)
    with factory() as session:
        current = config_service.get_config(pid, session)
        config_service.save_config(session, pid, current["config"], bind_result["revision"])
        session.commit()
    # 4) 财务基准确认
    resp = client.post(
        f"/api/projects/{pid}/validation/baseline-confirm",
        json={"assumptions": DEFAULT_ASSUMPTIONS},
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["confirmed"] is True
    assert body["assumptions_hash"] == validation_service.hash_assumptions(DEFAULT_ASSUMPTIONS)
    assert body["confirmed_by"] == 1
    # 5) 预检通过
    report = _run_report(client, pid)
    assert report["status"] == "ok", report["diagnostics"]
    assert report["blocks_submit"] is False
    assert report["summary"] == {"blocking": 0, "error": 0, "warning": 0, "info": 0}


# ---------------------------------------------------------------------------
# 财务基准确认
# ---------------------------------------------------------------------------


def test_baseline_confirm_state_change(client: TestClient, factory: sessionmaker) -> None:
    """基准确认前阻断(VFIN-001), 确认后不再因此阻断; 模型/数据仍缺失保持阻断。"""
    pid = _create_project(factory)
    report = _run_report(client, pid)
    assert any(
        d["code"] == "VALID-FIN-001" and d["blocking"]
        for d in report["diagnostics"]
    )
    resp = client.post(
        f"/api/projects/{pid}/validation/baseline-confirm",
        json={"assumptions": DEFAULT_ASSUMPTIONS},
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    report = _run_report(client, pid)
    codes = _diag_codes(report["diagnostics"])
    assert "VALID-FIN-001" not in codes
    # 模型与数据仍缺失, 继续阻断
    assert report["blocks_submit"] is True
    assert {"VALID-MODEL-001", "VALID-MODEL-002", "VALID-DATA-001"} <= codes


def test_baseline_stale_after_config_change(
    client: TestClient, factory: sessionmaker
) -> None:
    """确认内容与当前配置不一致时给出 VALID-FIN-002 警告, 不阻断。"""
    pid = _create_project(factory)
    resp = client.post(
        f"/api/projects/{pid}/validation/baseline-confirm",
        json={"assumptions": {"discount_rate": 0.09}},  # 与默认 0.08 不一致
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    report = _run_report(client, pid)
    stale = [d for d in report["diagnostics"] if d["code"] == "VALID-FIN-002"]
    assert stale, "应产出 VALID-FIN-002 警告"
    assert stale[0]["severity"] == "warning"
    assert stale[0]["blocking"] is False


def test_get_latest_validation_report(
    client: TestClient, factory: sessionmaker
) -> None:
    """GET /validation 返回最近一次持久化的报告; 无记录时现场执行(stored=False)。"""
    pid = _create_project(factory)
    resp = client.get(f"/api/projects/{pid}/validation", headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["stored"] is False
    assert body["report"]["blocks_submit"] is True
    # 执行一次预检并持久化
    _run_report(client, pid)
    resp = client.get(f"/api/projects/{pid}/validation", headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["stored"] is True
    assert body["report"]["blocks_submit"] is True


# ---------------------------------------------------------------------------
# 数据绑定语义(权威来源为草稿内容 dataset_bindings, 不推断最新版本)
# ---------------------------------------------------------------------------


def _create_sample_version(client: TestClient, pid: int, name: str = "样例数据") -> int:
    """创建项目数据集并生成 1h 样例版本, 返回版本 id。"""
    resp = client.post(f"/api/projects/{pid}/datasets", json={"name": name}, headers=_HEADERS)
    assert resp.status_code == 201, resp.text
    ds_id = resp.json()["dataset"]["id"]
    resp = client.post(
        f"/api/projects/{pid}/datasets/{ds_id}/sample",
        params={"resolution": "1h"},
        headers=_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["dataset_version"]["id"]


def test_dataset_without_binding_blocked(client: TestClient, factory: sessionmaker) -> None:
    """项目已有数据集版本但未绑定到草稿 → 仍报 VALID-DATA-001 阻断(不推断最新版本)。"""
    pid = _create_project(factory)
    _create_sample_version(client, pid)
    report = _run_report(client, pid)
    assert report["status"] == "blocked"
    assert "VALID-DATA-001" in _diag_codes(report["diagnostics"])


def test_foreign_dataset_version_binding_blocked(
    client: TestClient, factory: sessionmaker
) -> None:
    """绑定不属于本项目的版本 → VALID-DATA-004 阻断。"""
    pid_a = _create_project(factory, "项目A")
    pid_b = _create_project(factory, "项目B")
    version_b = _create_sample_version(client, pid_b, "B的样例")
    _bind_version(client, pid_a, version_b)
    report = _run_report(client, pid_a)
    codes = _diag_codes(report["diagnostics"])
    assert "VALID-DATA-004" in codes
    assert "VALID-DATA-001" not in codes
    assert report["blocks_submit"] is True


def test_corrupt_quality_report_blocked(
    client: TestClient, factory: sessionmaker
) -> None:
    """绑定版本的质控报告缺失/结构损坏 → fail-closed 阻断, 不产生 500。"""
    pid = _create_project(factory)
    version_id = _create_sample_version(client, pid)
    _bind_version(client, pid, version_id)
    # 直接破坏质控报告结构(模拟数据损坏)
    from iesplan.models.dataset import DatasetVersion

    with factory() as session:
        version = session.get(DatasetVersion, version_id)
        version.quality_report = {"diagnostics": None}
        session.commit()
    report = _run_report(client, pid)
    codes = _diag_codes(report["diagnostics"])
    assert "VALID-DATA-002" in codes
    assert report["blocks_submit"] is True


# ---------------------------------------------------------------------------
# 阻断不被警告降级
# ---------------------------------------------------------------------------


def test_blocking_errors_not_downgraded_by_warnings(
    client: TestClient, factory: sessionmaker
) -> None:
    """存在警告(孤立设备)时阻断错误(能源不平衡/缺负荷)仍保持阻断, 不降级。"""
    pid = _create_project(factory)
    resp = client.post(
        f"/api/projects/{pid}/model/devices",
        json={"device_type": GRID, "name": "孤立的电网连接"},
        headers=_HEADERS,
    )
    assert resp.status_code == 201
    report = _run_report(client, pid)
    assert report["status"] == "blocked"
    assert report["blocks_submit"] is True
    diags = report["diagnostics"]
    # 孤立设备警告(CONN-NODE-001)存在且不阻断
    assert any(
        d["code"] == "CONN-NODE-001" and d["severity"] == "warning" and not d["blocking"]
        for d in diags
    )
    # 阻断诊断保持阻断: 能源不平衡(单边电网)+ 缺负荷
    blockers = [d for d in diags if d["blocking"]]
    assert blockers, "应存在阻断诊断"
    assert any(d["code"] == "PARAM-UNIT-003" and d["blocking"] for d in blockers)
    assert any(d["code"] == "VALID-MODEL-002" and d["blocking"] for d in blockers)
    # 警告与阻断并存, blocks_submit 仍为 True
    assert report["blocks_submit"] is True


# ---------------------------------------------------------------------------
# 财务基准确认内容规范化(哈希可复现, 非法内容拒绝)
# ---------------------------------------------------------------------------


def test_hash_assumptions_canonical_numbers() -> None:
    """数值规范化: 20 与 20.0、Decimal 与 float 同值同哈希; 非法内容拒绝。"""
    from decimal import Decimal

    from iesplan.core.errors import AppError

    assert validation_service.hash_assumptions({"x": 20}) == validation_service.hash_assumptions(
        {"x": 20.0}
    )
    assert validation_service.hash_assumptions({"x": Decimal("0.1")}) == validation_service.hash_assumptions(
        {"x": 0.1}
    )
    assert validation_service.hash_assumptions(
        {"rate": Decimal("0.100000000000000001")}
    ) != validation_service.hash_assumptions({"rate": Decimal("0.1")})
    # 非法内容(非有限数值/不支持类型)拒绝, 且为 400 级输入错误
    with pytest.raises(AppError) as exc:
        validation_service.hash_assumptions({"x": float("nan")})
    assert exc.value.http_status == 400
    with pytest.raises(AppError) as exc:
        validation_service.hash_assumptions({"x": {"y": object()}})
    assert exc.value.http_status == 400


# ---------------------------------------------------------------------------
# 缺失项目
# ---------------------------------------------------------------------------


def test_missing_project_404(client: TestClient) -> None:
    """项目不存在应返回 404(避免泄露存在性细节, 与 U03 行为一致)。"""
    resp = client.post("/api/projects/999999/validation/run", headers=_HEADERS)
    assert resp.status_code == 404