"""项目权限(U02)与项目/草稿/版本(U03) API 集成测试。

覆盖流程: 创建→添加查看者→查看者只读(编辑 403)→所有者编辑(修订递增/幂等/冲突)
→版本创建(内容快照)→归档后禁止编辑→删除流程(需显式确认)→所有权转移
(原所有者变 viewer)→审计事件存在; 另覆盖: 恢复版本、应用结果、复制项目、
管理员维护只读、查看者移除、可见列表与无效命令。

测试环境: SQLite :memory:(StaticPool 共享连接) + tmp 对象存储目录,
不依赖部署 Postgres; 通过 app.dependency_overrides 替换 get_db。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# 单文件运行时的安全网: 固定 SQLite, 避免 iesplan.main 启动期误连部署 Postgres
# (全量运行时 iesplan 已被其他测试模块先行导入, 本行不生效但无副作用)
os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan.api import projects as projects_api  # noqa: E402
from iesplan.config import settings  # noqa: E402
from iesplan.db import Base, get_db  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.models.audit import AuditLog  # noqa: E402
from iesplan.models.identity import Role, User, UserRole  # noqa: E402

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
    """每个测试结束后清空全部表(避免测试间数据串扰)。"""
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
def client(engine: Engine, db_session: Session, tmp_path: Path) -> Iterator[TestClient]:
    """测试客户端: 挂载项目路由, 替换 get_db 依赖, 对象存储指向临时目录。"""
    settings.data_dir = tmp_path
    app = create_app()
    app.include_router(projects_api.router)

    def _override_get_db() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _h(user_id: int) -> dict[str, str]:
    """认证头(阶段实现: X-User-Id 模拟认证主体)。"""
    return {"X-User-Id": str(user_id)}


def _make_user(db: Session, username: str) -> User:
    """直接创建测试用户。"""
    user = User(username=username, display_name=username, locale="zh-CN")
    db.add(user)
    db.commit()
    return user


def _make_admin(db: Session, username: str) -> User:
    """创建带全局 admin 角色的用户。"""
    user = User(username=username, display_name=username)
    db.add(user)
    db.flush()
    role = Role(code="admin", name="管理员", description="测试管理员", is_system=True)
    db.add(role)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id, granted_by=user.id))
    db.commit()
    return user


def _create_project(client: TestClient, user_id: int, name: str, **kw: Any) -> int:
    """创建项目并返回项目 id。"""
    resp = client.post("/api/projects", json={"name": name, **kw}, headers=_h(user_id))
    assert resp.status_code == 201, resp.text
    return resp.json()["project"]["id"]


def _device_cmd(cid: str, name: str, device_type: str = "heat_pump", kind: str = "new") -> dict:
    """构造设备 upsert 命令。"""
    return {
        "id": cid,
        "unit": "model",
        "type": "model.upsert_device",
        "payload": {"name": name, "device_type": device_type, "kind": kind},
    }


def _load_content(oid: str) -> dict:
    """从对象存储读取内容文档(测试直读)。"""
    path = Path(settings.data_dir) / "objects" / f"{oid[:2]}/{oid}.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 主流程测试
# ---------------------------------------------------------------------------


def test_project_lifecycle_flow(client: TestClient, db_session: Session) -> None:
    """创建→查看者→只读→编辑→版本→归档→删除→转移→审计 全流程。"""
    owner = _make_user(db_session, "owner1")
    viewer = _make_user(db_session, "viewer1")
    owner_h, viewer_h = _h(owner.id), _h(viewer.id)

    # 1. 创建项目: 创建者=所有者, 初始草稿 revision=1
    resp = client.post(
        "/api/projects",
        json={"name": "园区综合能源", "currency": "CNY", "utc_offset_minutes": 480},
        headers=owner_h,
    )
    assert resp.status_code == 201
    body = resp.json()
    pid = body["project"]["id"]
    assert body["project"]["owner_id"] == owner.id
    assert body["project"]["status"] == "active"
    assert body["project"]["currency"] == "CNY"
    assert body["project"]["fixed_utc_offset_minutes"] == 480
    assert body["my_role"] == "owner"
    view = client.get(f"/api/projects/{pid}", headers=owner_h).json()
    assert view["draft"]["revision"] == 1
    assert view["draft"]["content"]["calc_config"]["params"] == {}
    assert view["versions"] == []

    # 2. 添加查看者
    resp = client.put(
        f"/api/projects/{pid}/viewers",
        json={"user_id": viewer.id, "action": "add"},
        headers=owner_h,
    )
    assert resp.status_code == 200
    members = resp.json()["members"]
    roles = {m["user_id"]: m["role"] for m in members}
    assert roles == {owner.id: "owner", viewer.id: "viewer"}

    # 非成员不可见(403)
    stranger = _make_user(db_session, "stranger1")
    assert client.get(f"/api/projects/{pid}", headers=_h(stranger.id)).status_code == 403

    # 3. 查看者只读: GET 200, 编辑 403
    assert client.get(f"/api/projects/{pid}", headers=viewer_h).status_code == 200
    edit_payload = {
        "expected_revision": 1,
        "commands": [_device_cmd("cmd-x", "热泵X")],
    }
    resp = client.put(f"/api/projects/{pid}/draft", json=edit_payload, headers=viewer_h)
    assert resp.status_code == 403

    # 4. 所有者编辑(语义命令, 乐观锁, 幂等)
    commands = [
        {
            "id": "cmd-1", "project_id": pid, "expected_revision": 1, "session": "win-1",
            "unit": "model", "type": "model.upsert_device",
            "payload": {"name": "热泵1", "device_type": "heat_pump", "kind": "new",
                        "params": {"capacity_kw": 1200}, "position": {"x": 100, "y": 200}},
        },
        {
            "id": "cmd-2", "unit": "config", "type": "config.patch",
            "payload": {"params": {"tariff": 0.6}},
        },
        {
            "id": "cmd-3", "unit": "dataset", "type": "dataset.bind",
            "payload": {"dataset_version_id": 1001, "role": "main"},
        },
    ]
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": 1, "commands": commands},
        headers=owner_h,
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["revision"] == 2
    assert len(result["results"]) == 3
    assert all(item["status"] == "applied" for item in result["results"])

    # 修订冲突: 陈旧预期修订 + 新命令 → 409
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={
            "expected_revision": 1,
            "commands": [_device_cmd("cmd-4", "电制冷机1", "chiller", "existing")],
        },
        headers=owner_h,
    )
    assert resp.status_code == 409

    # 幂等重试: 整批重试返回原结果, 修订不再递增
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": 1, "commands": commands},
        headers=owner_h,
    )
    assert resp.status_code == 200
    retry = resp.json()
    assert retry["revision"] == 2
    assert all(item["status"] == "idempotent" for item in retry["results"])
    assert retry["results"][0]["result"]["device"] == "热泵1"

    # 草稿内容可见
    view = client.get(f"/api/projects/{pid}", headers=owner_h).json()
    assert view["draft"]["revision"] == 2
    assert view["draft"]["content"]["calc_config"]["params"]["tariff"] == 0.6
    assert view["draft"]["content"]["model"]["devices"][0]["name"] == "热泵1"
    assert view["draft"]["content"]["dataset_bindings"][0]["dataset_version_id"] == 1001

    # 5. 版本创建(不可变快照)
    resp = client.post(
        f"/api/projects/{pid}/versions",
        json={"name": "初版", "description": "初始方案", "reason": "milestone"},
        headers=owner_h,
    )
    assert resp.status_code == 201
    version = resp.json()["version"]
    assert version["version_no"] == 1
    assert version["reason"] == "milestone"
    assert version["source_draft_revision"] == 2
    assert version["parent_version_id"] is None
    # 版本内容 = 草稿领域内容 + 项目固化字段(币种/UTC 偏移), 无命令簿记
    vcontent = _load_content(version["content_hash"])
    assert vcontent["currency"] == "CNY"
    assert vcontent["fixed_utc_offset_minutes"] == 480
    assert vcontent["model"]["devices"][0]["name"] == "热泵1"
    assert vcontent["calc_config"]["params"]["tariff"] == 0.6
    assert "applied_commands" not in vcontent
    view = client.get(f"/api/projects/{pid}", headers=owner_h).json()
    assert view["project"]["current_version_id"] == version["id"]
    assert len(view["versions"]) == 1

    # 6. 归档后禁止编辑, 仍可读; 撤销归档恢复
    resp = client.post(f"/api/projects/{pid}/archive", headers=owner_h)
    assert resp.status_code == 200
    assert resp.json()["project"]["status"] == "archived"
    resp = client.put(f"/api/projects/{pid}/draft", json=edit_payload, headers=owner_h)
    assert resp.status_code == 409
    assert client.get(f"/api/projects/{pid}", headers=owner_h).status_code == 200
    resp = client.post(f"/api/projects/{pid}/unarchive", headers=owner_h)
    assert resp.status_code == 200
    assert resp.json()["project"]["status"] == "active"

    # 7. 删除流程: 未确认 → 400; 确认 → 删除; 删除后 404
    # (TestClient.delete 不支持 json 请求体, 用通用 request 发送)
    resp = client.request("DELETE", f"/api/projects/{pid}", json={"confirm": False}, headers=owner_h)
    assert resp.status_code == 400
    resp = client.request("DELETE", f"/api/projects/{pid}", json={"confirm": True}, headers=owner_h)
    assert resp.status_code == 204
    assert client.get(f"/api/projects/{pid}", headers=owner_h).status_code == 404

    # 8. 所有权转移: 原所有者变 viewer
    pid2 = _create_project(client, owner.id, "转移测试项目")
    resp = client.post(
        f"/api/projects/{pid2}/transfer",
        json={"target_user_id": viewer.id},
        headers=owner_h,
    )
    assert resp.status_code == 200
    assert resp.json()["project"]["owner_id"] == viewer.id
    assert resp.json()["my_role"] == "viewer"
    # 原所有者不能再编辑, 新所有者可以
    resp = client.put(f"/api/projects/{pid2}/draft", json=edit_payload, headers=owner_h)
    assert resp.status_code == 403
    resp = client.put(f"/api/projects/{pid2}/draft", json=edit_payload, headers=viewer_h)
    assert resp.status_code == 200

    # 9. 审计事件存在
    rows = db_session.execute(select(AuditLog).order_by(AuditLog.id)).scalars().all()
    actions = [row.action for row in rows]
    for expected in (
        "project.created",
        "project.viewer_added",
        "project.draft_updated",
        "project.version_created",
        "project.archived",
        "project.unarchived",
        "project.deleted",
        "project.transferred",
    ):
        assert expected in actions, f"缺少审计事件 {expected}: {actions}"
    # 转移审计含转移前后双方
    transfer_log = next(row for row in rows if row.action == "project.transferred")
    assert transfer_log.before["from_user_id"] == owner.id
    assert transfer_log.after["to_user_id"] == viewer.id


# ---------------------------------------------------------------------------
# 恢复版本 / 应用结果
# ---------------------------------------------------------------------------


def test_restore_version(client: TestClient, db_session: Session) -> None:
    """恢复历史版本: 创建新版本+新草稿, 不倒写历史。"""
    owner = _make_user(db_session, "owner_restore")
    owner_h = _h(owner.id)
    pid = _create_project(client, owner.id, "恢复测试")

    commands = [
        _device_cmd("r-cmd-1", "光伏1", "pv", "new"),
        {
            "id": "r-cmd-2", "unit": "config", "type": "config.patch",
            "payload": {"params": {"tariff": 0.5}},
        },
    ]
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": 1, "commands": commands},
        headers=owner_h,
    )
    assert resp.status_code == 200 and resp.json()["revision"] == 2
    v1 = client.post(
        f"/api/projects/{pid}/versions",
        json={"name": "V1", "reason": "milestone"},
        headers=owner_h,
    ).json()["version"]

    # 继续编辑(修订 3): 追加锅炉
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": 2, "commands": [_device_cmd("r-cmd-3", "锅炉1", "boiler", "existing")]},
        headers=owner_h,
    )
    assert resp.status_code == 200 and resp.json()["revision"] == 3

    # 恢复 V1 → 新版本 2 + 新草稿 revision 4
    resp = client.post(f"/api/projects/{pid}/versions/{v1['id']}/restore", headers=owner_h)
    assert resp.status_code == 200
    restored = resp.json()
    assert restored["version"]["version_no"] == 2
    assert restored["version"]["parent_version_id"] == v1["id"]
    assert restored["version"]["reason"] == "restore"
    assert restored["draft"]["revision"] == 4

    # 新草稿内容与 V1 一致(不含锅炉)
    view = client.get(f"/api/projects/{pid}", headers=owner_h).json()
    assert view["draft"]["revision"] == 4
    assert view["draft"]["content_hash"] == v1["content_hash"]
    names = [d["name"] for d in view["draft"]["content"]["model"]["devices"]]
    assert names == ["光伏1"]

    # 历史未倒写
    v1_now = client.get(f"/api/projects/{pid}/versions/{v1['id']}", headers=owner_h).json()["version"]
    assert v1_now["version_no"] == 1
    assert v1_now["content_hash"] == v1["content_hash"]


def test_apply_result(client: TestClient, db_session: Session) -> None:
    """应用选定结果: 参数差异补丁→新草稿+新版本, 来源版本不变。"""
    owner = _make_user(db_session, "owner_apply")
    owner_h = _h(owner.id)
    pid = _create_project(client, owner.id, "应用结果测试")
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={
            "expected_revision": 1,
            "commands": [{
                "id": "a-cmd-1", "unit": "config", "type": "config.patch",
                "payload": {"params": {"tariff": 0.5}},
            }],
        },
        headers=owner_h,
    )
    assert resp.status_code == 200 and resp.json()["revision"] == 2
    v1 = client.post(
        f"/api/projects/{pid}/versions",
        json={"name": "V1", "reason": "milestone"},
        headers=owner_h,
    ).json()["version"]

    resp = client.post(
        f"/api/projects/{pid}/apply-result",
        json={
            "diff_patch": {"params": {"tariff": 0.8, "peak_demand": 100}},
            "source_result_id": "res-100",
            "name": "应用优化方案",
        },
        headers=owner_h,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"]["version_no"] == 2
    assert body["version"]["reason"] == "apply_result"
    assert body["version"]["parent_version_id"] == v1["id"]
    assert body["draft"]["revision"] == 3

    # 新草稿已含应用后的参数
    view = client.get(f"/api/projects/{pid}", headers=owner_h).json()
    params = view["draft"]["content"]["calc_config"]["params"]
    assert params["tariff"] == 0.8
    assert params["peak_demand"] == 100

    # 来源版本保持不变
    v1_now = client.get(f"/api/projects/{pid}/versions/{v1['id']}", headers=owner_h).json()["version"]
    assert v1_now["content_hash"] == v1["content_hash"]

    # 审计记录来源结果标识
    rows = db_session.execute(
        select(AuditLog).where(AuditLog.action == "project.version_created")
    ).scalars().all()
    assert any(row.after.get("source_result_id") == "res-100" for row in rows)


# ---------------------------------------------------------------------------
# 复制 / 维护访问 / 列表 / 成员管理 / 命令校验
# ---------------------------------------------------------------------------


def test_duplicate_project(client: TestClient, db_session: Session) -> None:
    """复制项目为独立候选方案(复制者为新所有者, 内容随副本)。"""
    owner = _make_user(db_session, "owner_dup")
    owner_h = _h(owner.id)
    pid = _create_project(client, owner.id, "复制源项目")
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={
            "expected_revision": 1,
            "commands": [_device_cmd("d-cmd-1", "光伏1", "pv", "new")],
        },
        headers=owner_h,
    )
    assert resp.status_code == 200 and resp.json()["revision"] == 2

    resp = client.post(f"/api/projects/{pid}/duplicate", headers=owner_h)
    assert resp.status_code == 201
    dup = resp.json()["project"]
    assert dup["id"] != pid
    assert dup["name"] == "复制源项目 副本"
    assert dup["owner_id"] == owner.id

    # 副本草稿从 revision=1 开始, 领域内容与源一致
    dup_view = client.get(f"/api/projects/{dup['id']}", headers=owner_h).json()
    assert dup_view["draft"]["revision"] == 1
    src_view = client.get(f"/api/projects/{pid}", headers=owner_h).json()
    src_content = {k: v for k, v in src_view["draft"]["content"].items() if k != "applied_commands"}
    dup_content = {k: v for k, v in dup_view["draft"]["content"].items() if k != "applied_commands"}
    assert src_content == dup_content


def test_admin_maintenance_readonly(client: TestClient, db_session: Session) -> None:
    """管理员维护只读: 可读任意项目, 不能业务编辑(RPD 3.2)。"""
    owner = _make_user(db_session, "owner_maint")
    admin = _make_admin(db_session, "admin_maint")
    stranger = _make_user(db_session, "stranger_maint")
    pid = _create_project(client, owner.id, "维护测试")

    assert client.get(f"/api/projects/{pid}", headers=_h(admin.id)).status_code == 200
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": 1, "commands": []},
        headers=_h(admin.id),
    )
    assert resp.status_code == 403
    # 非成员非管理员 → 403
    assert client.get(f"/api/projects/{pid}", headers=_h(stranger.id)).status_code == 403


def test_visible_listing(client: TestClient, db_session: Session) -> None:
    """可见列表: 所有者与查看者可见, 非成员不可见。"""
    owner = _make_user(db_session, "owner_list")
    viewer = _make_user(db_session, "viewer_list")
    stranger = _make_user(db_session, "stranger_list")
    pid = _create_project(client, owner.id, "列表项目")
    resp = client.put(
        f"/api/projects/{pid}/viewers",
        json={"user_id": viewer.id, "action": "add"},
        headers=_h(owner.id),
    )
    assert resp.status_code == 200

    owner_list = client.get("/api/projects", headers=_h(owner.id)).json()["projects"]
    viewer_list = client.get("/api/projects", headers=_h(viewer.id)).json()["projects"]
    stranger_list = client.get("/api/projects", headers=_h(stranger.id)).json()["projects"]
    assert [p["id"] for p in owner_list] == [pid]
    assert [p["id"] for p in viewer_list] == [pid]
    assert stranger_list == []
    assert owner_list[0]["my_role"] == "owner"
    assert viewer_list[0]["my_role"] == "viewer"


def test_remove_viewer(client: TestClient, db_session: Session) -> None:
    """移除查看者后失去读权限; 不能移除所有者。"""
    owner = _make_user(db_session, "owner_rmv")
    viewer = _make_user(db_session, "viewer_rmv")
    pid = _create_project(client, owner.id, "移除查看者")
    client.put(
        f"/api/projects/{pid}/viewers",
        json={"user_id": viewer.id, "action": "add"},
        headers=_h(owner.id),
    )
    resp = client.put(
        f"/api/projects/{pid}/viewers",
        json={"user_id": viewer.id, "action": "remove"},
        headers=_h(owner.id),
    )
    assert resp.status_code == 200
    assert [m["user_id"] for m in resp.json()["members"]] == [owner.id]
    assert client.get(f"/api/projects/{pid}", headers=_h(viewer.id)).status_code == 403
    # 不能移除所有者
    resp = client.put(
        f"/api/projects/{pid}/viewers",
        json={"user_id": owner.id, "action": "remove"},
        headers=_h(owner.id),
    )
    assert resp.status_code == 409


def test_invalid_commands(client: TestClient, db_session: Session) -> None:
    """命令校验: 缺幂等标识/未知类型/陈旧修订。"""
    owner = _make_user(db_session, "owner_inv")
    owner_h = _h(owner.id)
    pid = _create_project(client, owner.id, "命令校验")

    # 缺少幂等标识 → 400
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": 1, "commands": [
            {"unit": "model", "type": "model.upsert_device", "payload": {"name": "x"}},
        ]},
        headers=owner_h,
    )
    assert resp.status_code == 400

    # 未知命令类型 → 400
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": 1, "commands": [
            {"id": "c-bad", "unit": "model", "type": "model.unknown", "payload": {}},
        ]},
        headers=owner_h,
    )
    assert resp.status_code == 400

    # 正常提交后, 陈旧修订 + 新命令 → 409
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": 1, "commands": [
            {"id": "c-1", "unit": "config", "type": "config.patch", "payload": {"params": {"a": 1}}},
        ]},
        headers=owner_h,
    )
    assert resp.status_code == 200 and resp.json()["revision"] == 2
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": 1, "commands": [
            {"id": "c-2", "unit": "config", "type": "config.patch", "payload": {"params": {"b": 2}}},
        ]},
        headers=owner_h,
    )
    assert resp.status_code == 409
