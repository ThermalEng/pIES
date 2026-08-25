"""安全回归测试(红队修复验证)。

覆盖本次安全修复的回归场景:
- C-01: 对象管理 API 拒绝伪造 X-User-Id(仅接受真实窗口会话 + admin 角色);
- C-02: 强制改密未解除前业务接口一律 403(AUTH-FPC-001), 改密后解除;
- C-03: 模型 API 项目级权限 —— viewer 只读, 写模型 403;
- H-01: takeover_pending 会话业务请求 401, 确认接管后转 active 可用;
- H-05: viewer 取消任务 403;
- H-07: ZIP Bomb 拒绝(PKG-SIZE-001)。

运行方式: SQLite :memory:(StaticPool 共享连接) + IESPLAN_QUEUE=memory,
create_app() 挂载全部业务路由, dependency_overrides 替换 get_db。
"""

from __future__ import annotations

import io
import os
import zipfile
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
from iesplan.services import package as package_service  # noqa: E402

PASSWORD = "Test12345"


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
    """真实登录, 返回窗口凭证(不使用 login_headers 缓存, 便于重复登录/接管)。"""
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_project(client: TestClient, owner_token: str, name: str) -> int:
    resp = client.post(
        "/api/projects", json={"name": name}, headers=_bearer(owner_token)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["project"]["id"]


# ---------------------------------------------------------------------------
# C-01: 对象管理 API 拒绝伪造 X-User-Id
# ---------------------------------------------------------------------------


def test_admin_api_rejects_forged_x_user_id(client: TestClient, db: Session) -> None:
    """伪造 X-User-Id 头不再被接受(仅真实窗口会话 + admin 角色)。"""
    admin = make_user(db, "admin_c01", role="admin")
    make_user(db, "eng_c01")

    # 无会话 + X-User-Id 指向真实管理员 → 401(身份输入不可伪造)
    resp = client.get("/api/admin/storage", headers={"X-User-Id": str(admin.id)})
    assert resp.status_code == 401, resp.text

    # 无会话 + X-User-Id=1(种子管理员常见 id)→ 401
    resp = client.get("/api/admin/health", headers={"X-User-Id": "1"})
    assert resp.status_code == 401, resp.text

    # 普通工程师会话 + X-User-Id 伪装管理员 → 403(以真实会话主体判定)
    eng_token = _login(client, "eng_c01")
    resp = client.get(
        "/api/admin/storage",
        headers={**_bearer(eng_token), "X-User-Id": str(admin.id)},
    )
    assert resp.status_code == 403, resp.text

    # 真实管理员会话 → 200(正例)
    admin_token = _login(client, "admin_c01")
    resp = client.get("/api/admin/storage", headers=_bearer(admin_token))
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# C-02: 强制改密服务端门禁
# ---------------------------------------------------------------------------


def test_force_password_change_gates_business_api(client: TestClient, db: Session) -> None:
    """requires_change=True 时业务接口 403; 改密成功后自动解除。"""
    identity.create_user(
        db, "fpc_user", PASSWORD, role="engineer", force_password_change=True,
        display_name="FPC 用户",
    )
    token = _login(client, "fpc_user")

    # 业务接口(项目列表)→ 403 AUTH-FPC-001
    resp = client.get("/api/projects", headers=_bearer(token))
    assert resp.status_code == 403, resp.text
    err = resp.json()["error"]
    assert err["code"] == "AUTH-FPC-001"
    assert err["message_key"] == "ies.diag.auth.force_password_change"

    # 豁免接口可用: /api/auth/me
    resp = client.get("/api/auth/me", headers=_bearer(token))
    assert resp.status_code == 200, resp.text

    # 改密成功 → 全部旧会话失效 → 新密码重新登录 → 业务接口 200
    resp = client.post(
        "/api/auth/change-password",
        json={"old_password": PASSWORD, "new_password": "NewPass123"},
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    token2 = _login(client, "fpc_user", "NewPass123")
    resp = client.get("/api/projects", headers=_bearer(token2))
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# C-03: 模型 API 项目级权限
# ---------------------------------------------------------------------------


def test_model_api_non_owner_denied_and_owner_allowed(client: TestClient, db: Session) -> None:
    """非所有者读/写模型一律 403(0.8.0 起无共享成员); 所有者可读可写。"""
    make_user(db, "owner_c03")
    other = make_user(db, "other_c03")
    make_user(db, "stranger_c03")
    owner_token = _login(client, "owner_c03")
    pid = _create_project(client, owner_token, "模型权限项目")

    other_token = _login(client, "other_c03")
    # 非所有者读 → 403
    resp = client.get(f"/api/projects/{pid}/model", headers=_bearer(other_token))
    assert resp.status_code == 403, resp.text
    resp = client.get(f"/api/projects/{pid}/model/validate", headers=_bearer(other_token))
    assert resp.status_code == 403, resp.text

    # 非所有者写(设备/连接)→ 403
    body = {"device_type": "ies.device.pv", "name": "PV1", "params": {}}
    resp = client.post(
        f"/api/projects/{pid}/model/devices", json=body, headers=_bearer(other_token)
    )
    assert resp.status_code == 403, resp.text
    resp = client.post(
        f"/api/projects/{pid}/model/connections",
        json={"from_port_id": 1, "to_port_id": 2},
        headers=_bearer(other_token),
    )
    assert resp.status_code == 403, resp.text

    # 非成员写 → 403(未授权用户不可修改模型)
    stranger_token = _login(client, "stranger_c03")
    resp = client.post(
        f"/api/projects/{pid}/model/devices", json=body, headers=_bearer(stranger_token)
    )
    assert resp.status_code == 403, resp.text

    # 所有者写 → 201(正例)
    resp = client.post(
        f"/api/projects/{pid}/model/devices", json=body, headers=_bearer(owner_token)
    )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# H-01: takeover_pending 会话无业务权限, 确认接管后转 active
# ---------------------------------------------------------------------------


def test_pending_session_rejected_until_confirm(client: TestClient, db: Session) -> None:
    """接管确认前新会话业务请求 401; 确认接管后签发正式凭证可用。"""
    make_user(db, "takeover_c01")
    token_a = _login(client, "takeover_c01")
    assert client.get("/api/projects", headers=_bearer(token_a)).status_code == 200

    # 第二窗口登录 → 新会话为 takeover_pending
    resp = client.post(
        "/api/auth/login", json={"username": "takeover_c01", "password": PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["needs_takeover_confirm"] is True
    token_b = resp.json()["token"]

    # pending 会话业务请求 → 401(前端提示接管)
    resp = client.get("/api/projects", headers=_bearer(token_b))
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["params"].get("reason") == "takeover_pending"

    # 确认接管 → 当前待接管会话保留为 active(凭证不变, 立即可用)
    resp = client.post("/api/auth/confirm-takeover", headers=_bearer(token_b))
    assert resp.status_code == 200, resp.text
    token_c = resp.json()["token"]
    assert token_c == token_b
    resp = client.get("/api/projects", headers=_bearer(token_c))
    assert resp.status_code == 200, resp.text

    # 确认后同一凭证(原 pending)即正式活动凭证, 业务请求放行
    resp = client.get("/api/projects", headers=_bearer(token_b))
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# H-05: 非所有者取消/重试任务 403
# ---------------------------------------------------------------------------


def test_non_owner_cannot_cancel_or_retry_task(client: TestClient, db: Session) -> None:
    """非所有者取消/重试任务 → 403(要求项目 edit 能力, 仅所有者具备)。"""
    make_user(db, "owner_c05")
    other = make_user(db, "other_c05")
    owner_token = _login(client, "owner_c05")
    pid = _create_project(client, owner_token, "任务权限项目")

    # 所有者提交任务
    resp = client.post(
        f"/api/projects/{pid}/tasks",
        json={"task_type": "optimization"},
        headers=_bearer(owner_token),
    )
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["task"]["id"]

    other_token = _login(client, "other_c05")
    resp = client.post(
        f"/api/projects/{pid}/tasks/{task_id}/cancel",
        json={"reason": "other-cancel"},
        headers=_bearer(other_token),
    )
    assert resp.status_code == 403, resp.text
    resp = client.post(
        f"/api/projects/{pid}/tasks/{task_id}/retry",
        json={},
        headers=_bearer(other_token),
    )
    assert resp.status_code == 403, resp.text

    # 所有者取消仍可用(正例: 运行前 queued 直接取消)
    resp = client.post(
        f"/api/projects/{pid}/tasks/{task_id}/cancel",
        json={"reason": "owner-cancel"},
        headers=_bearer(owner_token),
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# H-07: ZIP Bomb 拒绝(PKG-SIZE-001)
# ---------------------------------------------------------------------------


def _make_zip(entries: list[tuple[str, bytes]]) -> bytes:
    """构造 zip 字节(条目 [(路径, 内容), ...])。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, data in entries:
            zf.writestr(path, data)
    return buf.getvalue()


def test_zip_bomb_too_many_entries_rejected(client: TestClient, db: Session, monkeypatch) -> None:
    """条目数超限拒绝(PKG-SIZE-001), 不进入解压。"""
    monkeypatch.setattr(package_service, "MAX_PACKAGE_ENTRIES", 10)
    importer = make_user(db, "importer_bomb1")
    zip_bytes = _make_zip([(f"e{i}.json", b"{}") for i in range(20)])
    with pytest.raises(package_service.PackageSizeError) as exc:
        package_service.import_proposal(db, importer, zip_bytes)
    assert exc.value.code == "PKG-SIZE-001"


def test_zip_bomb_single_entry_too_large_rejected(client: TestClient, db: Session, monkeypatch) -> None:
    """单条目解压大小超限拒绝(PKG-SIZE-001)。"""
    monkeypatch.setattr(package_service, "MAX_PACKAGE_ENTRY_BYTES", 32)
    importer = make_user(db, "importer_bomb2")
    zip_bytes = _make_zip([("big.bin", b"x" * 64)])
    with pytest.raises(package_service.PackageSizeError) as exc:
        package_service.import_proposal(db, importer, zip_bytes)
    assert exc.value.code == "PKG-SIZE-001"


def test_zip_bomb_total_uncompressed_rejected(client: TestClient, db: Session, monkeypatch) -> None:
    """总解压大小超限拒绝(PKG-SIZE-001)。"""
    monkeypatch.setattr(package_service, "MAX_PACKAGE_TOTAL_BYTES", 64)
    importer = make_user(db, "importer_bomb3")
    zip_bytes = _make_zip([("a.bin", b"a" * 48), ("b.bin", b"b" * 48)])
    with pytest.raises(package_service.PackageSizeError) as exc:
        package_service.import_proposal(db, importer, zip_bytes)
    assert exc.value.code == "PKG-SIZE-001"


# ---------------------------------------------------------------------------
# C-04: 下载 token 绑定项目与用户(越权下载拒绝)
# ---------------------------------------------------------------------------


def test_download_token_binds_project_and_user(client: TestClient, db: Session) -> None:
    """token 只能下载绑定项目 + 绑定用户的资源; 换项目/换用户一律拒绝。"""
    make_user(db, "owner_a04")
    make_user(db, "owner_b04")
    make_user(db, "other_c04")
    token_a = _login(client, "owner_a04")
    token_b = _login(client, "owner_b04")
    other_token = _login(client, "other_c04")
    pid_a = _create_project(client, token_a, "导出项目 A")
    pid_b = _create_project(client, token_b, "导出项目 B")

    resp = client.post(f"/api/projects/{pid_a}/exports/package", headers=_bearer(token_a))
    assert resp.status_code == 200, resp.text
    dl_token = resp.json()["token"]

    # 绑定用户下载 → 200
    resp = client.get(
        f"/api/projects/{pid_a}/exports/package/download",
        params={"token": dl_token},
        headers=_bearer(token_a),
    )
    assert resp.status_code == 200, resp.text

    # 其他用户持同一 token → 400(用户不匹配)
    resp = client.get(
        f"/api/projects/{pid_a}/exports/package/download",
        params={"token": dl_token},
        headers=_bearer(other_token),
    )
    assert resp.status_code == 400, resp.text

    # 同用户经 B 项目 URL → 400(项目不匹配)
    resp = client.get(
        f"/api/projects/{pid_b}/exports/package/download",
        params={"token": dl_token},
        headers=_bearer(token_a),
    )
    assert resp.status_code == 400, resp.text

    # 匿名(无会话)→ 401(下载必须登录): 用全新 TestClient 避免既有登录 Cookie 残留
    with TestClient(client.app, raise_server_exceptions=False) as anon:
        resp = anon.get(
            f"/api/projects/{pid_a}/exports/package/download", params={"token": dl_token}
        )
        assert resp.status_code == 401, resp.text
