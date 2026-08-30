"""CSRF 防护回归测试(切片 A2: Cookie 认证 CSRF 双源校验)。

威胁模型: 浏览器携带 ``ies_session`` Cookie(credentials:'include')调用状态变更
接口; SameSite=Lax 不阻止顶级导航跨站 POST, 第三方站点可自动提交表单触发
注销/改密/接管/创建项目/删除等操作。

防护规则(见 iesplan.api.csrf):
- 只拦截「携带 ies_session Cookie + 状态变更方法(POST/PUT/PATCH/DELETE)」请求;
- Bearer 认证(无 Cookie)不受影响;
- 无 Origin/Referer 的非浏览器客户端(API/TestClient)放行;
- 有来源头 → 规范化后须命中可信来源(静态 app_url+CORS 来源 + 请求 Host 同源),
  否则 403 ``AUTH-CSRF-001``(``ies.diag.auth.csrf_origin_rejected``)。

运行方式: 与 test_security_regression 同构 —— create_app() 挂载全部业务路由,
SQLite :memory: + dependency_overrides 替换 get_db。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")

import pytest  # noqa: E402
from auth_helpers import make_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan.db import Base, get_db  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.services import identity  # noqa: E402

PASSWORD = "Test12345"
#: 伪造的第三方来源(不在可信集合内)
EVIL_ORIGIN = "http://evil.example.com"
#: 可信来源(本地开发 CORS 默认值之一)
TRUSTED_ORIGIN = "http://localhost:3000"


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


@pytest.fixture()
def db(engine: Engine) -> Iterator[Session]:
    """每个测试独立事务起点(共用同一连接, 测试间数据保留)。"""
    with Session(engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture()
def client(db: Session) -> Iterator[TestClient]:
    """挂载全部业务路由的应用测试客户端(get_db 替换为内存 SQLite)。"""
    app = create_app()

    def _override_get_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    identity.reset_login_rate_limit()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _login(client: TestClient, username: str, password: str = PASSWORD) -> str:
    """真实登录, 返回窗口凭证(同时设置客户端 Cookie)。"""
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_project(client: TestClient, owner_token: str, name: str) -> int:
    """创建项目(返回 project_id), 使用 Bearer 认证保证幂等。"""
    resp = client.post(
        "/api/projects",
        json={
            "name": name,
            "baseline_resolution": "1h",
            "baseline_leap_year": False,
            "baseline_scenario_mode": "single",
        },
        headers=_bearer(owner_token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["project"]["id"]


# ---------------------------------------------------------------------------
# 核心规则: Cookie 会话 + 状态变更方法
# ---------------------------------------------------------------------------


def test_cookie_session_cross_origin_state_change_rejected(client: TestClient, db: Session) -> None:
    """跨域 Origin 的状态变更 POST 被拒(403 AUTH-CSRF-001)。"""
    make_user(db, "csrf_cross1")
    _login(client, "csrf_cross1")
    assert client.cookies.get("ies_session"), "登录后应持有会话 Cookie"

    # 跨域 Origin + Cookie 会话的 POST → 403
    resp = client.post(
        "/api/projects",
        json={"name": "跨域项目"},
        headers={"Origin": EVIL_ORIGIN},
    )
    assert resp.status_code == 403, resp.text
    err = resp.json()["error"]
    assert err["code"] == "AUTH-CSRF-001"
    assert err["message_key"] == "ies.diag.auth.csrf_origin_rejected"


def test_cookie_session_cross_origin_put_and_delete_rejected(
    client: TestClient, db: Session
) -> None:
    """跨域 Origin 的 PUT/DELETE 同样被拒(Cookie 会话)。"""
    make_user(db, "csrf_cross2")
    token = _login(client, "csrf_cross2")
    pid = _create_project(client, token, "CSRF PUT/DELETE 项目")

    resp = client.put(
        f"/api/projects/{pid}",
        json={"name": "改名"},
        headers={"Origin": EVIL_ORIGIN},
    )
    assert resp.status_code == 403, resp.text

    resp = client.delete(
        f"/api/projects/{pid}",
        headers={"Origin": EVIL_ORIGIN},
    )
    assert resp.status_code == 403, resp.text


def test_cookie_session_cross_origin_referer_rejected(client: TestClient, db: Session) -> None:
    """跨站 Referer(无 Origin 的旧浏览器/表单导航)同样被拒。"""
    make_user(db, "csrf_ref")
    _login(client, "csrf_ref")

    resp = client.post(
        "/api/projects",
        json={"name": "Referer 跨站项目"},
        headers={"Referer": f"{EVIL_ORIGIN}/steal.html"},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "AUTH-CSRF-001"


# ---------------------------------------------------------------------------
# 正例: 同源 / 可信来源 / 无来源客户端 / Bearer
# ---------------------------------------------------------------------------


def test_same_origin_cookie_state_change_allowed(client: TestClient, db: Session) -> None:
    """请求 Host 推导的同源 Origin 放行(浏览器同源 POST 场景)。"""
    make_user(db, "csrf_same")
    _login(client, "csrf_same")

    resp = client.post(
        "/api/projects",
        json={"name": "同源项目"},
        headers={"Origin": "http://testserver"},  # TestClient 默认 Host: testserver
    )
    assert resp.status_code == 201, resp.text


def test_trusted_origin_cookie_state_change_allowed(client: TestClient, db: Session) -> None:
    """配置的可信来源(CORS 清单内)放行。"""
    make_user(db, "csrf_trusted")
    _login(client, "csrf_trusted")

    resp = client.post(
        "/api/projects",
        json={"name": "可信来源项目"},
        headers={"Origin": TRUSTED_ORIGIN},
    )
    assert resp.status_code == 201, resp.text


def test_no_origin_api_client_allowed(client: TestClient, db: Session) -> None:
    """无 Origin/Referer 的非浏览器客户端(Bearer)放行。"""
    make_user(db, "csrf_noorigin")
    token = _login(client, "csrf_noorigin")

    # 客户端仍持有 Cookie(TestClient 登录后自动保存), 但无来源头 → 放行
    resp = client.post(
        "/api/projects",
        json={"name": "无来源客户端项目"},
        headers=_bearer(token),
    )
    assert resp.status_code == 201, resp.text

    # 纯 Bearer + 无 Cookie 场景(全新客户端, 不共享登录 Cookie)→ 放行
    with TestClient(client.app, raise_server_exceptions=False) as anon:
        anon.headers.update(_bearer(token))
        resp = anon.post("/api/projects", json={"name": "纯 Bearer 项目"})
        assert resp.status_code == 201, resp.text


def test_bearer_auth_not_affected_by_origin(client: TestClient, db: Session) -> None:
    """Bearer 认证请求即使携带跨域 Origin 也不受影响(非 Cookie 会话)。"""
    make_user(db, "csrf_bearer")
    token = _login(client, "csrf_bearer")

    # 携带跨域 Origin, 但认证主体是 Bearer(新客户端无 Cookie)
    with TestClient(client.app, raise_server_exceptions=False) as anon:
        resp = anon.post(
            "/api/projects",
            json={"name": "Bearer 跨域项目"},
            headers={**_bearer(token), "Origin": EVIL_ORIGIN},
        )
        assert resp.status_code == 201, resp.text


def test_get_requests_not_protected(client: TestClient, db: Session) -> None:
    """GET(只读)不校验来源 —— 跨域 Origin 的 GET 正常放行。"""
    make_user(db, "csrf_get")
    token = _login(client, "csrf_get")
    pid = _create_project(client, token, "GET 项目")

    resp = client.get(f"/api/projects/{pid}/model", headers={"Origin": EVIL_ORIGIN})
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# 边界: 请求无会话 Cookie 时不校验来源(仅 Bearer 或匿名)
# ---------------------------------------------------------------------------


def test_no_cookie_origin_not_checked(client: TestClient, db: Session) -> None:
    """未携带会话 Cookie 的请求即使带跨域 Origin 也不被 CSRF 拦截。"""
    make_user(db, "csrf_nocookie")

    # 全新客户端(无登录 Cookie), 即使携带跨域 Origin → 401(未认证), 而非 CSRF 403
    with TestClient(client.app, raise_server_exceptions=False) as anon:
        resp = anon.post(
            "/api/projects",
            json={"name": "未登录跨域"},
            headers={"Origin": EVIL_ORIGIN},
        )
        assert resp.status_code == 401, resp.text
