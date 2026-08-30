"""身份认证 API 集成测试(U01): 登录/限速/改密/接管/登出/管理员操作/注册。

运行方式: 内存 SQLite + app.dependency_overrides 替换 get_db 依赖
(不触碰真实数据库, 与 CONTRACT 第 4 节一致)。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from iesplan.api.auth import SESSION_COOKIE_NAME
from iesplan.api.auth import router as auth_router
from iesplan.db import Base, get_db
from iesplan.main import create_app
from iesplan.models.identity import AuthEvent, WindowSession
from iesplan.services import identity

ADMIN_PASSWORD = "Admin12345"
USER_PASSWORD = "Alice12345"


@pytest.fixture()
def db_session() -> Iterator[Session]:
    """内存 SQLite 会话(每次测试独立建库)。

    StaticPool + check_same_thread=False: TestClient 在独立线程执行应用逻辑,
    单连接跨线程共享(内存库标准做法)。
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with TestingSession() as session:
        yield session
    engine.dispose()


@pytest.fixture()
def client(db_session: Session) -> Iterator[TestClient]:
    """挂载 /api/auth 路由的应用测试客户端(get_db 替换为内存 SQLite)。"""
    app = create_app()
    app.include_router(auth_router)

    def override_get_db() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    identity.reset_login_rate_limit()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def seed_admin(db: Session, force_change: bool = True) -> identity.User:
    """创建内置管理员(默认首登强制改密, 与 seed_admin 语义一致)。

    参数:
        force_change: 需要管理员执行业务/管理操作的测试传 False(等价于
            已改密后的正式管理员状态, 避免强制改密门禁(C-02)阻断管理端点)。
    """
    return identity.create_user(
        db, "admin", ADMIN_PASSWORD, role="admin", display_name="管理员",
        force_password_change=force_change,
    )


def seed_engineer(db: Session, username: str = "alice", password: str = USER_PASSWORD) -> identity.User:
    """创建普通工程师用户(首登不强制改密, 便于直接测试业务操作)。"""
    return identity.create_user(
        db, username, password, role="engineer", force_password_change=False, display_name=username.title()
    )


def login(
    client: TestClient, username: str, password: str, device: str | None = None
) -> Response:
    """便捷登录请求。"""
    return client.post(
        "/api/auth/login", json={"username": username, "password": password, "device": device}
    )


def bearer(token: str) -> dict[str, str]:
    """构造 Authorization 头。"""
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------


def test_login_success_returns_token_and_cookie(client: TestClient, db_session: Session) -> None:
    """登录成功: 返回窗口凭证、用户信息并写入 HttpOnly Cookie。"""
    seed_admin(db_session)
    resp = login(client, "admin", ADMIN_PASSWORD, device="window-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"]
    assert body["token_type"] == "bearer"
    assert body["needs_takeover_confirm"] is False
    user = body["user"]
    assert user["username"] == "admin"
    assert user["role"] == "admin"
    assert user["force_password_change"] is True  # 种子管理员首登强制改密
    assert client.cookies.get(SESSION_COOKIE_NAME) == body["token"]


def test_login_failure_message_unified(client: TestClient, db_session: Session) -> None:
    """登录失败统一文案: 不区分用户不存在/密码错误(均 401 + login_failed)。"""
    seed_admin(db_session)
    r1 = login(client, "nobody", "Wrong12345")
    r2 = login(client, "admin", "Wrong12345")
    assert r1.status_code == 401
    assert r2.status_code == 401
    e1 = r1.json()["error"]
    e2 = r2.json()["error"]
    assert e1["message_key"] == e2["message_key"] == "ies.diag.auth.login_failed"
    assert "Wrong12345" not in r1.text  # 响应不泄露密码


def test_login_rate_limit_locks_after_five_failures(client: TestClient, db_session: Session) -> None:
    """登录限速: 5 次失败锁定 15 分钟, 锁定期间即使密码正确也拒绝(429)。"""
    seed_admin(db_session)
    for _ in range(5):
        assert login(client, "admin", "Wrong12345").status_code == 401
    resp = login(client, "admin", "Wrong12345")
    assert resp.status_code == 429
    assert resp.json()["error"]["message_key"] == "ies.diag.auth.locked"
    # 锁定期间正确密码同样被拒
    resp = login(client, "admin", ADMIN_PASSWORD)
    assert resp.status_code == 429
    assert resp.json()["error"]["message_key"] == "ies.diag.auth.locked"
    # 限速按用户名隔离: 其他用户名不受影响
    assert login(client, "alice", USER_PASSWORD).status_code == 401  # 用户不存在但未被锁定


def test_login_success_resets_rate_limit(client: TestClient, db_session: Session) -> None:
    """限速计数: 4 次失败后成功登录, 计数清零(再失败 4 次不触发锁定)。"""
    seed_admin(db_session)
    for _ in range(4):
        login(client, "admin", "Wrong12345")
    assert login(client, "admin", ADMIN_PASSWORD).status_code == 200
    for _ in range(4):
        assert login(client, "admin", "Wrong12345").status_code == 401
    # 未达 5 次不锁定
    assert login(client, "admin", "Wrong12345").status_code == 401


# ---------------------------------------------------------------------------
# 修改密码(首登强制改密)
# ---------------------------------------------------------------------------


def test_change_password_flow(client: TestClient, db_session: Session) -> None:
    """改密: 旧密码错误/强度不足被拒; 成功后旧会话失效、强制改密标记清除。"""
    seed_admin(db_session)
    r = login(client, "admin", ADMIN_PASSWORD)
    token = r.json()["token"]
    headers = bearer(token)

    # 旧密码错误
    resp = client.post(
        "/api/auth/change-password",
        json={"old_password": "Nope12345", "new_password": "NewAdmin123"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["message_key"] == "ies.diag.auth.bad_old_password"

    # 强度不足
    resp = client.post(
        "/api/auth/change-password",
        json={"old_password": ADMIN_PASSWORD, "new_password": "short"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["message_key"] == "ies.diag.auth.weak_password"

    # 新旧密码相同
    resp = client.post(
        "/api/auth/change-password",
        json={"old_password": ADMIN_PASSWORD, "new_password": ADMIN_PASSWORD},
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["message_key"] == "ies.diag.auth.same_password"

    # 改密成功
    resp = client.post(
        "/api/auth/change-password",
        json={"old_password": ADMIN_PASSWORD, "new_password": "NewAdmin123"},
        headers=headers,
    )
    assert resp.status_code == 200

    # 凭证版本递增 → 旧会话立即失效
    assert client.post("/api/auth/refresh", headers=headers).status_code == 401

    # 新密码登录, 强制改密标记已清除
    r2 = login(client, "admin", "NewAdmin123")
    assert r2.status_code == 200
    assert r2.json()["user"]["force_password_change"] is False


# ---------------------------------------------------------------------------
# 窗口接管(单活动窗口, RPD 3.3)
# ---------------------------------------------------------------------------


def test_window_takeover_and_confirm(client: TestClient, db_session: Session) -> None:
    """接管: 新登录使旧窗口撤销, 新窗口 pending(确认前无业务权限); 确认后转 active。"""
    seed_admin(db_session, force_change=False)
    r1 = login(client, "admin", ADMIN_PASSWORD, device="window-A")
    token_a = r1.json()["token"]
    assert r1.json()["needs_takeover_confirm"] is False

    # 第二窗口登录 → 提示确认接管
    r2 = login(client, "admin", ADMIN_PASSWORD, device="window-B")
    token_b = r2.json()["token"]
    assert r2.json()["needs_takeover_confirm"] is True

    # 旧窗口凭证立即失效(取消防止新操作)
    assert client.post("/api/auth/refresh", headers=bearer(token_a)).status_code == 401

    # 新窗口确认接管 → 当前待接管会话保留为 active(凭证不变)
    r3 = client.post("/api/auth/confirm-takeover", headers=bearer(token_b))
    assert r3.status_code == 200
    token_c = r3.json()["token"]
    assert token_c == token_b

    # B 凭证确认后转 active 可用(不轮换: 客户端既有凭证立即生效)
    assert client.post("/api/auth/refresh", headers=bearer(token_b)).status_code == 200
    assert client.post("/api/auth/refresh", headers=bearer(token_c)).status_code == 200

    # 数据库状态: A revoked → B active(保留), 且 A 的 replaced_by 指向 B
    # (B 为保留会话, 不再派生新会话)
    sessions = list(db_session.execute(select(WindowSession).order_by(WindowSession.id)).scalars())
    assert [s.status for s in sessions] == ["revoked", "active"]
    assert sessions[0].replaced_by_session_id == sessions[1].id
    assert sessions[1].replaced_by_session_id is None


def test_takeover_pending_session_revoked_on_next_login(client: TestClient, db_session: Session) -> None:
    """连续接管(H-01): 接管未确认前再次登录, 更早的 pending 会话被撤销,
    新会话仍为 takeover_pending(每用户至多一条 pending, 无 active 被绕过)。"""
    seed_admin(db_session, force_change=False)
    r1 = login(client, "admin", ADMIN_PASSWORD)
    assert r1.status_code == 200
    r2 = login(client, "admin", ADMIN_PASSWORD)
    assert r2.status_code == 200
    r3 = login(client, "admin", ADMIN_PASSWORD)
    assert r3.status_code == 200
    assert r3.json()["needs_takeover_confirm"] is True
    sessions = list(db_session.execute(select(WindowSession).order_by(WindowSession.id)).scalars())
    statuses = [s.status for s in sessions]
    # 三次登录: 前两次会话均被撤销, 当前会话为 takeover_pending(等待确认)
    assert statuses == ["revoked", "revoked", "takeover_pending"]
    assert statuses.count("active") == 0
    assert statuses.count("takeover_pending") == 1
    assert statuses[0] == "revoked"


# ---------------------------------------------------------------------------
# 登出 / 续期 / 过期
# ---------------------------------------------------------------------------


def test_logout_revokes_session(client: TestClient, db_session: Session) -> None:
    """登出: 会话撤销、Cookie 清除, 凭证不再可用, 写 logout 审计。"""
    seed_admin(db_session)
    r = login(client, "admin", ADMIN_PASSWORD)
    token = r.json()["token"]
    headers = bearer(token)
    resp = client.post("/api/auth/logout", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # 服务端会话已撤销, 且浏览器 Cookie 被清除
    assert client.post("/api/auth/refresh", headers=headers).status_code == 401
    assert client.cookies.get(SESSION_COOKIE_NAME) is None
    events = list(db_session.execute(select(AuthEvent)).scalars())
    assert any(e.event_type == "logout" for e in events)


def test_refresh_extends_session(client: TestClient, db_session: Session) -> None:
    """会话续期: 返回新过期时刻且原会话仍可用。"""
    seed_admin(db_session, force_change=False)
    r = login(client, "admin", ADMIN_PASSWORD)
    token = r.json()["token"]
    resp = client.post("/api/auth/refresh", headers=bearer(token))
    assert resp.status_code == 200
    assert resp.json()["expires_at"]
    assert client.post("/api/auth/refresh", headers=bearer(token)).status_code == 200


def test_expired_session_rejected(client: TestClient, db_session: Session) -> None:
    """过期校验: 会话过期后请求被拒(401)且会话置 expired 终态。"""
    seed_admin(db_session)
    r = login(client, "admin", ADMIN_PASSWORD)
    token = r.json()["token"]
    session = db_session.execute(select(WindowSession)).scalar_one()
    session.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()
    resp = client.post("/api/auth/refresh", headers=bearer(token))
    assert resp.status_code == 401
    assert resp.json()["error"]["message_key"] == "ies.diag.auth.session_invalid"
    db_session.refresh(session)
    assert session.status == "expired"


def test_missing_or_invalid_token_rejected(client: TestClient, db_session: Session) -> None:
    """缺少/伪造凭证: 一律 401 session_invalid/required。"""
    seed_admin(db_session)
    assert client.post("/api/auth/refresh").status_code == 401
    resp = client.post("/api/auth/refresh", headers=bearer("forged-token"))
    assert resp.status_code == 401
    assert resp.json()["error"]["message_key"] == "ies.diag.auth.session_invalid"


# ---------------------------------------------------------------------------
# 管理员: 重置密码 / 停用 / 启用 / 用户列表 / 权限校验
# ---------------------------------------------------------------------------


def test_admin_reset_password_invalidates_sessions(client: TestClient, db_session: Session) -> None:
    """管理员重置密码: 目标用户全部会话失效, 临时密码首登强制改密。"""
    seed_admin(db_session, force_change=False)
    alice = seed_engineer(db_session)
    r = login(client, "alice", USER_PASSWORD)
    token = r.json()["token"]
    headers = bearer(token)
    assert client.post("/api/auth/refresh", headers=headers).status_code == 200

    r2 = login(client, "admin", ADMIN_PASSWORD)
    admin_headers = bearer(r2.json()["token"])
    resp = client.post(
        f"/api/auth/users/{alice.id}/reset-password",
        json={"new_password": "Temp12345"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # 旧会话已失效
    assert client.post("/api/auth/refresh", headers=headers).status_code == 401

    # 临时密码登录 → 强制改密
    r3 = login(client, "alice", "Temp12345")
    assert r3.status_code == 200
    assert r3.json()["user"]["force_password_change"] is True

    # 原密码已不可用
    assert login(client, "alice", USER_PASSWORD).status_code == 401


def test_admin_deactivate_reactivate(client: TestClient, db_session: Session) -> None:
    """停用: 会话立即失效且登录被拒; 重新启用后恢复登录。"""
    seed_admin(db_session, force_change=False)
    alice = seed_engineer(db_session)
    r = login(client, "alice", USER_PASSWORD)
    headers = bearer(r.json()["token"])
    r2 = login(client, "admin", ADMIN_PASSWORD)
    admin_headers = bearer(r2.json()["token"])

    resp = client.post(f"/api/auth/users/{alice.id}/deactivate", headers=admin_headers)
    assert resp.status_code == 200
    # 停用后会话立即失效
    assert client.post("/api/auth/refresh", headers=headers).status_code == 401
    # 登录被拒(统一文案, 不区分原因)
    r3 = login(client, "alice", USER_PASSWORD)
    assert r3.status_code == 401
    assert r3.json()["error"]["message_key"] == "ies.diag.auth.login_failed"

    resp = client.post(f"/api/auth/users/{alice.id}/reactivate", headers=admin_headers)
    assert resp.status_code == 200
    assert login(client, "alice", USER_PASSWORD).status_code == 200


def test_admin_cannot_deactivate_self(client: TestClient, db_session: Session) -> None:
    """管理员不能停用自己的账号(避免管理员自锁)。"""
    seed_admin(db_session, force_change=False)
    r = login(client, "admin", ADMIN_PASSWORD)
    admin_headers = bearer(r.json()["token"])
    admin = db_session.execute(select(identity.User).where(identity.User.username == "admin")).scalar_one()
    resp = client.post(f"/api/auth/users/{admin.id}/deactivate", headers=admin_headers)
    assert resp.status_code == 403


def test_admin_users_list_and_permission(client: TestClient, db_session: Session) -> None:
    """用户列表: 管理员可见全部; 普通用户访问被拒(403)。"""
    seed_admin(db_session, force_change=False)
    seed_engineer(db_session)
    r = login(client, "admin", ADMIN_PASSWORD)
    resp = client.get("/api/auth/users", headers=bearer(r.json()["token"]))
    assert resp.status_code == 200
    names = [u["username"] for u in resp.json()["users"]]
    assert names == ["admin", "alice"]

    r2 = login(client, "alice", USER_PASSWORD)
    alice_headers = bearer(r2.json()["token"])
    assert client.get("/api/auth/users", headers=alice_headers).status_code == 403


# ---------------------------------------------------------------------------
# 管理员用户列表: 项目数(账号管理展示, 前端 4345872 配套)
# ---------------------------------------------------------------------------


def _create_project_for(client: TestClient, headers: dict[str, str], name: str) -> int:
    """以窗口凭证创建项目并返回 project_id(测试造数)。"""
    resp = client.post(
        "/api/projects", json={"name": name, "currency": "CNY", "utc_offset_minutes": 480},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["project"]["id"]


def test_admin_users_list_returns_project_count(client: TestClient, db_session: Session) -> None:
    """用户列表: 每个用户返回 project_count(active+archived 计入, deleted 不计)。"""
    seed_admin(db_session, force_change=False)
    seed_engineer(db_session, "alice")
    seed_engineer(db_session, "bob")
    admin_headers = bearer(login(client, "admin", ADMIN_PASSWORD).json()["token"])
    alice_headers = bearer(login(client, "alice", USER_PASSWORD).json()["token"])

    # alice: 2 active + 1 archived = 3; bob/admin: 无项目 = 0
    p1 = _create_project_for(client, alice_headers, "alice-active-1")
    _create_project_for(client, alice_headers, "alice-active-2")
    p3 = _create_project_for(client, alice_headers, "alice-archived")
    assert client.post(f"/api/projects/{p3}/archive", headers=alice_headers).status_code == 200

    resp = client.get("/api/auth/users", headers=admin_headers)
    assert resp.status_code == 200
    by_name = {u["username"]: u for u in resp.json()["users"]}
    assert by_name["alice"]["project_count"] == 3
    assert by_name["bob"]["project_count"] == 0
    assert by_name["admin"]["project_count"] == 0

    # deleted 项目不计入
    assert client.request(
        "DELETE", f"/api/projects/{p1}", headers=alice_headers,
        json={"confirm": True, "reason": "清理测试项目"},
    ).status_code == 204
    resp = client.get("/api/auth/users", headers=admin_headers)
    by_name = {u["username"]: u for u in resp.json()["users"]}
    assert by_name["alice"]["project_count"] == 2
    assert by_name["bob"]["project_count"] == 0


def test_login_and_me_keep_user_semantics(client: TestClient, db_session: Session) -> None:
    """登录与 /me 保持原有普通用户响应语义(不含 project_count)。

    project_count 仅服务管理员用户列表(GET /api/auth/users); 登录/注册/
    /me 继续使用原有 UserOut 字段集。
    """
    seed_admin(db_session, force_change=False)
    seed_engineer(db_session)
    r = login(client, "admin", ADMIN_PASSWORD)
    user = r.json()["user"]
    assert "project_count" not in user
    assert set(user) == {
        "id", "username", "display_name", "role", "status",
        "force_password_change", "credential_version", "last_login_at",
    }

    me = client.get("/api/auth/me", headers=bearer(r.json()["token"])).json()
    assert "project_count" not in me
    assert set(me) == set(user)

    # 仅管理员用户列表项携带 project_count
    users = client.get("/api/auth/users", headers=bearer(r.json()["token"])).json()["users"]
    assert all("project_count" in u for u in users)


def test_admin_delete_user_cascades_projects(client: TestClient, db_session: Session) -> None:
    """删除账号(0.2.0 B1 误操作防护): 先预览取得确认令牌, 携带 confirm+令牌删除成功,
    该账号拥有的项目一并软删, 账号停用且会话失效。"""
    seed_admin(db_session, force_change=False)
    alice = seed_engineer(db_session)
    r = login(client, "admin", ADMIN_PASSWORD)
    admin_headers = bearer(r.json()["token"])
    alice_headers = bearer(login(client, "alice", USER_PASSWORD).json()["token"])

    # alice 创建两个项目
    p1 = client.post(
        "/api/projects", json={"name": "级联删除1", "currency": "CNY", "utc_offset_minutes": 480},
        headers=alice_headers,
    )
    assert p1.status_code == 201
    p2 = client.post(
        "/api/projects", json={"name": "级联删除2", "currency": "CNY", "utc_offset_minutes": 480},
        headers=alice_headers,
    )
    assert p2.status_code == 201
    pid1 = p1.json()["project"]["id"]
    pid2 = p2.json()["project"]["id"]

    # 第一步: 删除预告 → 返回将受影响项目清单 + 确认令牌
    preview = client.post(f"/api/auth/users/{alice.id}/delete-preview", headers=admin_headers)
    assert preview.status_code == 200
    prev = preview.json()
    assert prev["user_id"] == alice.id
    assert prev["username"] == "alice"
    assert prev["project_count"] == 2
    assert [p["id"] for p in prev["projects"]] == sorted([pid1, pid2])
    assert prev["confirm_token"]

    # 第二步: 携带 confirm + 令牌删除 → 两个项目一并软删
    resp = client.request(
        "DELETE",
        f"/api/auth/users/{alice.id}",
        json={"confirm": True, "confirm_token": prev["confirm_token"]},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["deleted_projects"] == 2

    # alice 会话失效、登录被拒
    assert client.post("/api/auth/refresh", headers=alice_headers).status_code == 401
    assert login(client, "alice", USER_PASSWORD).status_code == 401
    # 项目已删除(整体视图仍可见, status=deleted; 细节视图 404)
    admin_view = client.get("/api/projects/admin-visible", headers=admin_headers)
    assert admin_view.status_code == 200
    by_id = {p["id"]: p for p in admin_view.json()["projects"]}
    assert by_id[pid1]["status"] == "deleted"
    assert by_id[pid2]["status"] == "deleted"
    assert client.get(f"/api/projects/{pid1}", headers=admin_headers).status_code == 404


def test_admin_delete_user_requires_confirm_and_token(client: TestClient, db_session: Session) -> None:
    """删除账号(0.2.0 B1): 无 confirm / 无令牌 / 令牌无效 / 清单变化均被拒(400),
    账号与项目均不受影响。"""
    seed_admin(db_session, force_change=False)
    alice = seed_engineer(db_session)
    r = login(client, "admin", ADMIN_PASSWORD)
    admin_headers = bearer(r.json()["token"])
    alice_headers = bearer(login(client, "alice", USER_PASSWORD).json()["token"])

    p1 = client.post(
        "/api/projects", json={"name": "需确认项目", "currency": "CNY", "utc_offset_minutes": 480},
        headers=alice_headers,
    )
    assert p1.status_code == 201
    pid1 = p1.json()["project"]["id"]

    # 1) 无 confirm(裸 DELETE)→ 400
    resp = client.delete(f"/api/auth/users/{alice.id}", headers=admin_headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["message_key"] == "ies.diag.auth.delete_confirm_required"

    # 2) confirm=true 但无令牌 → 400
    resp = client.request(
        "DELETE", f"/api/auth/users/{alice.id}", json={"confirm": True}, headers=admin_headers
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["message_key"] == "ies.diag.auth.delete_confirm_required"

    # 3) confirm=true + 伪造令牌 → 400
    resp = client.request(
        "DELETE",
        f"/api/auth/users/{alice.id}",
        json={"confirm": True, "confirm_token": "forged-token"},
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["message_key"] == "ies.diag.auth.delete_confirm_required"

    # 4) 预览后项目清单变化(新增项目)→ 旧令牌拒绝(需重新预览)
    preview = client.post(f"/api/auth/users/{alice.id}/delete-preview", headers=admin_headers)
    assert preview.status_code == 200
    old_token = preview.json()["confirm_token"]
    p2 = client.post(
        "/api/projects", json={"name": "清单变化项目", "currency": "CNY", "utc_offset_minutes": 480},
        headers=alice_headers,
    )
    assert p2.status_code == 201
    resp = client.request(
        "DELETE",
        f"/api/auth/users/{alice.id}",
        json={"confirm": True, "confirm_token": old_token},
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["message_key"] == "ies.diag.auth.delete_confirm_required"

    # 全部被拒后账号与项目均不受影响
    assert client.post("/api/auth/refresh", headers=alice_headers).status_code == 200
    assert login(client, "alice", USER_PASSWORD).status_code == 200
    # 重复登录触发接管, 确认接管后取得可用凭证再访问项目细节
    alice_tok = login(client, "alice", USER_PASSWORD).json()["token"]
    alice2_headers = bearer(
        client.post("/api/auth/confirm-takeover", headers=bearer(alice_tok)).json()["token"]
    )
    admin_view = client.get("/api/projects/admin-visible", headers=admin_headers)
    by_id = {p["id"]: p for p in admin_view.json()["projects"]}
    assert by_id[pid1]["status"] == "active"
    assert client.get(f"/api/projects/{pid1}", headers=alice2_headers).status_code == 200


def test_admin_delete_user_preview_zero_projects(client: TestClient, db_session: Session) -> None:
    """删除预告: 无项目账号返回空清单 + 可正常确认删除。"""
    seed_admin(db_session, force_change=False)
    alice = seed_engineer(db_session)
    r = login(client, "admin", ADMIN_PASSWORD)
    admin_headers = bearer(r.json()["token"])

    preview = client.post(f"/api/auth/users/{alice.id}/delete-preview", headers=admin_headers)
    assert preview.status_code == 200
    prev = preview.json()
    assert prev["project_count"] == 0
    assert prev["projects"] == []
    assert prev["confirm_token"]

    resp = client.request(
        "DELETE",
        f"/api/auth/users/{alice.id}",
        json={"confirm": True, "confirm_token": prev["confirm_token"]},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["deleted_projects"] == 0


def test_admin_cannot_delete_self_or_system(client: TestClient, db_session: Session) -> None:
    """管理员不能删除自己; 系统账号不可删除。"""
    seed_admin(db_session, force_change=False)
    r = login(client, "admin", ADMIN_PASSWORD)
    admin_headers = bearer(r.json()["token"])
    admin = db_session.execute(select(identity.User).where(identity.User.username == "admin")).scalar_one()
    assert client.delete(f"/api/auth/users/{admin.id}", headers=admin_headers).status_code == 403
    # 预告同样被拒(不自锁/不预告系统账号)
    assert client.post(f"/api/auth/users/{admin.id}/delete-preview", headers=admin_headers).status_code == 403
    sys_user = db_session.execute(
        select(identity.User).where(identity.User.is_system.is_(True))
    ).scalars().all()
    for u in sys_user:
        assert client.delete(f"/api/auth/users/{u.id}", headers=admin_headers).status_code == 403
        assert client.post(f"/api/auth/users/{u.id}/delete-preview", headers=admin_headers).status_code == 403


# ---------------------------------------------------------------------------
# 自助注册开关
# ---------------------------------------------------------------------------


def test_register_toggle(client: TestClient, db_session: Session) -> None:
    """注册开关: 默认关闭(403); 管理员开启后只能注册工程师; 重名冲突 409。

    开关持久化到数据库(app_settings): 同一会话内读取与写入一致;
    公开设置端点无需认证即可感知开关状态(登录页渲染条件)。
    """
    seed_admin(db_session, force_change=False)
    # 默认关闭(公开设置端点可感知, 无需认证)
    resp = client.get("/api/auth/public-settings")
    assert resp.status_code == 200
    assert resp.json()["registration_enabled"] is False
    assert resp.json()["sso_enabled"] is False
    resp = client.post("/api/auth/register", json={"username": "bob", "password": "Bob12345"})
    assert resp.status_code == 403
    assert resp.json()["error"]["message_key"] == "ies.diag.auth.registration_disabled"

    # 管理员开启(写审计 + 持久化到 app_settings)
    r = login(client, "admin", ADMIN_PASSWORD)
    admin_headers = bearer(r.json()["token"])
    resp = client.put("/api/auth/settings", json={"registration_enabled": True}, headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["registration_enabled"] is True
    resp = client.get("/api/auth/settings", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["registration_enabled"] is True
    # 公开设置同步生效(登录页无需登录即可见注册按钮)
    assert client.get("/api/auth/public-settings").json()["registration_enabled"] is True
    # 持久化: app_settings 表已写入
    from iesplan.models.identity import AppSetting

    row = db_session.execute(
        select(AppSetting).where(AppSetting.key == "registration_enabled")
    ).scalar_one()
    assert row.value["value"] is True

    # 注册成功且角色为工程师
    resp = client.post("/api/auth/register", json={"username": "bob", "password": "Bob12345"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "engineer"
    assert body["force_password_change"] is False

    # 重名冲突
    resp = client.post("/api/auth/register", json={"username": "bob", "password": "Bob23456"})
    assert resp.status_code == 409
    assert resp.json()["error"]["message_key"] == "ies.diag.auth.username_taken"

    # 注册用户可登录
    assert login(client, "bob", "Bob12345").status_code == 200

    # 关闭后再次拒绝
    resp = client.put("/api/auth/settings", json={"registration_enabled": False}, headers=admin_headers)
    assert resp.status_code == 200
    resp = client.post("/api/auth/register", json={"username": "carol", "password": "Carol12345"})
    assert resp.status_code == 403
    assert client.get("/api/auth/public-settings").json()["registration_enabled"] is False


# ---------------------------------------------------------------------------
# 会话服务: 批量过期 / 撤销其他会话(API 测试未直接覆盖的服务函数)
# ---------------------------------------------------------------------------


def test_service_expire_sessions(client: TestClient, db_session: Session) -> None:
    """expire_sessions: 过期的活动会话批量置为 expired, 之后可正常重新登录。"""
    seed_admin(db_session)
    r = login(client, "admin", ADMIN_PASSWORD)
    token = r.json()["token"]
    session = db_session.execute(select(WindowSession)).scalar_one()
    session.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()
    assert identity.expire_sessions(db_session) == 1
    db_session.refresh(session)
    assert session.status == "expired"
    assert client.post("/api/auth/refresh", headers=bearer(token)).status_code == 401
    assert login(client, "admin", ADMIN_PASSWORD).status_code == 200


def test_service_revoke_other_sessions(client: TestClient, db_session: Session) -> None:
    """revoke_other_sessions: 撤销指定会话以外的全部活动/待接管会话。

    单活动窗口语义(H-01)下每用户至多一条 active/pending: 第二次登录时旧
    active 已被撤销, 因此通常无可撤销的"其他"会话; 保留会话确认接管后可用。
    """
    seed_admin(db_session, force_change=False)
    r1 = login(client, "admin", ADMIN_PASSWORD)
    token_a = r1.json()["token"]
    r2 = login(client, "admin", ADMIN_PASSWORD)
    token_b = r2.json()["token"]
    user = db_session.execute(select(identity.User).where(identity.User.username == "admin")).scalar_one()
    keep = db_session.execute(
        select(WindowSession).where(WindowSession.session_token_hash == identity.token_hash(token_b))
    ).scalar_one()
    assert identity.revoke_other_sessions(db_session, user, keep_session_id=keep.id) == 0
    # 保留的会话仍为待接管(不因 revoke_other_sessions 变化): 确认接管后可用
    r3 = client.post("/api/auth/confirm-takeover", headers=bearer(token_b))
    assert r3.status_code == 200, r3.text
    token_c = r3.json()["token"]
    assert client.post("/api/auth/refresh", headers=bearer(token_c)).status_code == 200
    # 被接管撤销的旧会话不可用
    assert client.post("/api/auth/refresh", headers=bearer(token_a)).status_code == 401


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


def test_auth_events_recorded(client: TestClient, db_session: Session) -> None:
    """登录成功/失败/登出/改密/接管/重置/停用均写入 auth_events 审计。"""
    seed_admin(db_session, force_change=False)
    alice = seed_engineer(db_session)
    login(client, "admin", ADMIN_PASSWORD, device="audit-window")
    login(client, "admin", "Wrong12345")
    r = login(client, "alice", USER_PASSWORD)
    token = r.json()["token"]
    headers = bearer(token)
    client.post(
        "/api/auth/change-password",
        json={"old_password": USER_PASSWORD, "new_password": "Alice9999"},
        headers=headers,
    )
    # 改密后旧凭证已失效, 需重新登录后再登出
    r1b = login(client, "alice", "Alice9999")
    token_b = r1b.json()["token"]
    client.post("/api/auth/logout", headers=bearer(token_b))

    r2 = login(client, "admin", ADMIN_PASSWORD)
    admin_token = r2.json()["token"]
    # 第二次登录的新窗口为待接管(H-01), 确认接管后取得正式凭证再执行管理操作
    r2c = client.post("/api/auth/confirm-takeover", headers=bearer(admin_token))
    assert r2c.status_code == 200, r2c.text
    admin_headers = bearer(r2c.json()["token"])
    client.post(f"/api/auth/users/{alice.id}/deactivate", headers=admin_headers)

    types = [e.event_type for e in db_session.execute(select(AuthEvent)).scalars()]
    for expected in (
        "login_success",
        "login_failure",
        "logout",
        "password_change",
        "account_disabled",
        "session_takeover",
    ):
        assert expected in types, f"缺少审计事件: {expected}"


# ---------------------------------------------------------------------------
# 公开设置(登录页)与外部认证(OIDC/SSO)
# ---------------------------------------------------------------------------


def test_public_settings_unauthenticated(client: TestClient, db_session: Session) -> None:
    """公开设置端点: 无需认证即可读取(登录页渲染前置条件)。"""
    resp = client.get("/api/auth/public-settings")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"registration_enabled", "sso_enabled", "sso_provider_name"}
    assert body["registration_enabled"] is False
    assert body["sso_enabled"] is False
    # 不应暴露内部细节
    assert "secret" not in body


def test_oidc_disabled_by_default(client: TestClient, db_session: Session) -> None:
    """OIDC 默认关闭: 登录入口返回 404, 公开设置 sso_enabled=False。"""
    resp = client.get("/api/auth/oidc/login")
    assert resp.status_code == 404
    assert client.get("/api/auth/public-settings").json()["sso_enabled"] is False


def test_oidc_state_roundtrip(client: TestClient, db_session: Session) -> None:
    """state 签名令牌: 签发→校验往返; 篡改/过期均被拒。"""
    import time as _time
    from unittest.mock import patch

    from iesplan.services import external_auth
    from iesplan.services.external_auth import ExternalAuthError

    state = external_auth.build_state(nonce="n1", code_verifier="v1")
    payload = external_auth.verify_state(state)
    assert payload == {"nonce": "n1", "verifier": "v1"}

    # 篡改 state → 拒签(BadSignature → ExternalAuthError)
    with pytest.raises(ExternalAuthError):
        external_auth.verify_state(state + "x")
    # 伪造内容(不同盐/密钥)→ 拒签
    from itsdangerous import URLSafeTimedSerializer

    forged = URLSafeTimedSerializer(
        "different-secret", salt="oidc-pkce-state", signer_kwargs={"key_derivation": "hmac"}
    ).dumps({"nonce": "n", "verifier": "v"})
    with pytest.raises(ExternalAuthError):
        external_auth.verify_state(forged)
    # 过期 → 拒绝(收窄安全窗口为 1s 后休眠触发; itsdangerous 秒级精度, 需 >1s 差)
    with patch.object(external_auth, "AUTH_CODE_WINDOW_SECONDS", 1):
        stale = external_auth.build_state(nonce="n2", code_verifier="v2")
        _time.sleep(2.1)
        with pytest.raises(ExternalAuthError):
            external_auth.verify_state(stale)


def test_oidc_provision_user_jit(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JIT 建号: 首次外部主体登录自动创建 engineer 账号并绑定 subject。

    同一 subject 再次登录返回同一用户(不重复建号); 绑定关系落库。
    """
    from iesplan.services import external_auth

    # 即使随机部分碰巧不含大小写/数字，内部凭证也必须稳定通过复杂度门禁。
    monkeypatch.setattr(external_auth.secrets, "token_urlsafe", lambda _size: "_" * 24)

    claims = {"sub": "oidc-user-001", "name": "外部用户", "email": "oidc@example.com"}
    user = external_auth.provision_user(db_session, claims)
    assert user.username == "oidc_user_001"
    assert user.auth_subject == "oidc-user-001"
    assert user.email == "oidc@example.com"
    assert "engineer" in identity.user_roles(db_session, user)

    # 同 subject 再次登录: 返回同一用户(不重复建号)
    user2 = external_auth.provision_user(db_session, claims)
    assert user2.id == user.id
    assert db_session.execute(select(identity.User)).scalars().all() == [user]

    # 用户名规则冲突 → 追加序号: 不同 subject 清洗后用户名恰好与既有账号同名
    claims2 = {"sub": "OIDC_USER.001", "name": "x"}  # 小写+点→下划线 → oidc_user_001
    user3 = external_auth.provision_user(db_session, claims2)
    assert user3.username == "oidc_user_001_2"
    assert user3.auth_subject == "OIDC_USER.001"


def test_oidc_callback_disabled(client: TestClient, db_session: Session) -> None:
    """回调在 OIDC 未启用时返回 404。"""
    resp = client.get("/api/auth/oidc/callback?code=x&state=y")
    assert resp.status_code == 404
