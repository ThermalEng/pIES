"""计算配置 API 测试(0.12-A1: CalculationConfig revision 收敛)。

覆盖: 未保存读 404 → 非法输入 422 信封(不落库) → 保存/读取 revision 链 →
revision 递增 → 乐观锁 409 → 权限 403 → 保存不碰草稿内容对象。

- 数据库: SQLite :memory:(全部表 create_all);
- 应用: 独立 FastAPI 实例挂载 config_router,
  用 dependency_overrides 替换 get_request_db。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from iesplan.api.auth import router as auth_router
from iesplan.api.config import config_router
from iesplan.api.deps import get_request_db
from iesplan.application import identity
from iesplan.db import Base
from iesplan.identity.persistence import User
from iesplan.main import _register_exception_handlers
from iesplan.project.persistence import Draft, Project

# 建表前必须显式导入, 否则 create_all 报 NoReferencedTableError(与其他挂载
# tasks 路由的测试模块行为一致)。
from iesplan.tasks.persistence import CalcSnapshot as _CalcSnapshotForDDL  # noqa: F401

#: 配置域测试所有者(经窗口会话登录)
OWNER_USERNAME = "config_owner"
OWNER_PASSWORD = "Config12345"


@pytest.fixture()
def db() -> Iterator[Session]:
    """内存 SQLite 会话(StaticPool 保证跨连接共享同一库)。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with session_factory() as session:
        yield session
    engine.dispose()


def _make_user(db: Session, username: str) -> None:
    if db.execute(select(User).where(User.username == username)).scalar_one_or_none() is None:
        identity.create_user(
            db, username, OWNER_PASSWORD, role="engineer",
            force_password_change=False, display_name=username,
        )


def seed_project(db: Session) -> Project:
    """种子项目: 所有者 + 项目行(计算配置保存不依赖草稿)。"""
    _make_user(db, OWNER_USERNAME)
    owner = db.execute(select(User).where(User.username == OWNER_USERNAME)).scalar_one()
    proj = Project(
        name="配置测试项目",
        currency="CNY",
        baseline_resolution="1h",
        baseline_leap_year=False,
        baseline_scenario_mode="single",
        owner_id=owner.id,
        created_by=owner.id,
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def make_app(db: Session) -> FastAPI:
    """挂载配置路由的测试应用(get_request_db 覆盖为传入会话)。"""
    application = FastAPI(title="pIES Config API Test")
    application.include_router(auth_router)
    application.include_router(config_router)
    _register_exception_handlers(application)
    application.dependency_overrides[get_request_db] = lambda: db
    return application


@pytest.fixture()
def client(db: Session) -> Iterator[TestClient]:
    """测试客户端: 预置配置所有者并登录, 客户端默认携带窗口会话凭证头。"""
    _make_user(db, OWNER_USERNAME)
    with TestClient(make_app(db), raise_server_exceptions=False) as test_client:
        resp = test_client.post(
            "/api/auth/login",
            json={"username": OWNER_USERNAME, "password": OWNER_PASSWORD},
        )
        assert resp.status_code == 200, resp.text
        test_client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
        yield test_client


def _valid_payload() -> dict[str, Any]:
    """合法 12 字段 CalculationConfig(已发布契约形态)。"""
    return {
        "schema": "ies.calculation-config",
        "schema_version": "1.0.0",
        "mode": "planning",
        "predictions": [],
        "generator": "ies.algo.milp_hybrid@1.0.0",
        "solver": "ies.solver.highs@1.7.2",
        "precision": "medium",
        "convergence": None,
        "time_limit_seconds": 600,
        "options": {},
        "seed": None,
        "outputs": ["solution"],
    }


def _login(client: TestClient, username: str) -> str:
    resp = client.post("/api/auth/login", json={"username": username, "password": OWNER_PASSWORD})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["token"])


def test_get_unsaved_returns_404(client: TestClient, db: Session) -> None:
    """未保存过计算配置的项目读取 → 404(无默认配置可用)。"""
    project = seed_project(db)
    resp = client.get(f"/api/projects/{project.id}/config")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "RES-MISS-003"


def test_save_invalid_rejected_without_persisting(client: TestClient, db: Session) -> None:
    """非法输入逐项 422 标准信封, 且均不落库(读取仍 404)。"""
    project = seed_project(db)
    bad_payloads = [
        ({**_valid_payload(), "generator": "not-a-ref"}, "引用形态非法"),
        ({k: v for k, v in _valid_payload().items() if k != "solver"}, "缺失必填字段"),
        ({**_valid_payload(), "outputs": []}, "输出请求为空"),
        ({**_valid_payload(), "precision": "ultra"}, "精度枚举外"),
        ({**_valid_payload(), "unknown_field": 1}, "未知字段"),
    ]
    for payload, _reason in bad_payloads:
        resp = client.put(
            f"/api/projects/{project.id}/config",
            json={"calculation_config": payload, "expected_revision": None},
        )
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert set(body) == {"error"}, body
        assert body["error"]["code"] == "CONFIG-VAL-001"
    # 全部拒绝后仍无 revision 行
    resp = client.get(f"/api/projects/{project.id}/config")
    assert resp.status_code == 404, resp.text


def test_save_then_get_roundtrip(client: TestClient, db: Session) -> None:
    """保存 → revision 1 → 读取回显一致。"""
    project = seed_project(db)
    payload = _valid_payload()
    resp = client.put(
        f"/api/projects/{project.id}/config",
        json={"calculation_config": payload, "expected_revision": None},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"calculation_config", "revision"}, body
    assert body["revision"] == 1
    assert body["calculation_config"] == payload

    resp = client.get(f"/api/projects/{project.id}/config")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"calculation_config": payload, "revision": 1}


def test_save_second_bumps_revision(client: TestClient, db: Session) -> None:
    """基于最新 revision 再次保存 → revision 递增, 旧 revision 不可变。"""
    project = seed_project(db)
    first = _valid_payload()
    resp = client.put(
        f"/api/projects/{project.id}/config",
        json={"calculation_config": first, "expected_revision": None},
    )
    assert resp.status_code == 200, resp.text
    second = {**first, "precision": "high", "seed": 7}
    resp = client.put(
        f"/api/projects/{project.id}/config",
        json={"calculation_config": second, "expected_revision": 1},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 2
    assert resp.json()["calculation_config"]["precision"] == "high"

    resp = client.get(f"/api/projects/{project.id}/config")
    assert resp.json() == {"calculation_config": second, "revision": 2}


def test_save_stale_expected_revision_conflict(client: TestClient, db: Session) -> None:
    """首保存带非空期望 revision → 409; 旧 revision 保存 → 409。"""
    project = seed_project(db)
    resp = client.put(
        f"/api/projects/{project.id}/config",
        json={"calculation_config": _valid_payload(), "expected_revision": 1},
    )
    assert resp.status_code == 409, resp.text

    resp = client.put(
        f"/api/projects/{project.id}/config",
        json={"calculation_config": _valid_payload(), "expected_revision": None},
    )
    assert resp.status_code == 200, resp.text
    resp = client.put(
        f"/api/projects/{project.id}/config",
        json={"calculation_config": _valid_payload(), "expected_revision": None},
    )
    assert resp.status_code == 409, resp.text


def test_save_requires_edit_permission(client: TestClient, db: Session) -> None:
    """无项目编辑权限的用户保存 → 403; 读取 → 403。"""
    project = seed_project(db)
    _make_user(db, "config_outsider")
    token = _login(client, "config_outsider")
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.put(
        f"/api/projects/{project.id}/config",
        json={"calculation_config": _valid_payload(), "expected_revision": None},
        headers=headers,
    )
    assert resp.status_code == 403, resp.text
    resp = client.get(f"/api/projects/{project.id}/config", headers=headers)
    assert resp.status_code == 403, resp.text


def test_project_not_found(client: TestClient) -> None:
    """不存在的项目读写 → 404。"""
    resp = client.get("/api/projects/999999/config")
    assert resp.status_code == 404, resp.text
    resp = client.put(
        "/api/projects/999999/config",
        json={"calculation_config": _valid_payload(), "expected_revision": None},
    )
    assert resp.status_code == 404, resp.text


def test_save_does_not_touch_draft_content(client: TestClient, db: Session) -> None:
    """保存计算配置不改动草稿内容对象(A1 不改变快照输入源)。"""
    project = seed_project(db)
    draft = db.execute(select(Draft).where(Draft.project_id == project.id)).scalar_one_or_none()
    before = draft.content_object_id if draft is not None else None
    resp = client.put(
        f"/api/projects/{project.id}/config",
        json={"calculation_config": _valid_payload(), "expected_revision": None},
    )
    assert resp.status_code == 200, resp.text
    after = db.execute(select(Draft).where(Draft.project_id == project.id)).scalar_one_or_none()
    after_oid = after.content_object_id if after is not None else None
    assert before == after_oid
