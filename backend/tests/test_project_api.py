"""项目权限(U02)与项目/草稿/版本(U03) API 集成测试。

覆盖流程: 创建→查看者只读(编辑 403)→所有者编辑(修订递增/幂等/冲突)
→版本创建(内容快照)→归档后禁止编辑→删除流程(需显式确认)→审计事件存在;
另覆盖: 恢复版本、应用结果、管理员维护只读、可见列表与无效命令。

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
from auth_helpers import login_headers, make_user  # noqa: E402
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


def _h(client: TestClient, user) -> dict[str, str]:
    """认证头: 以真实窗口会话登录(同一 client 内缓存, 避免多窗口接管)。"""
    return login_headers(client, user)


def _create_project(client: TestClient, user, name: str, **kw: Any) -> int:
    """创建项目并返回项目 id。"""
    resp = client.post("/api/projects", json={"name": name, **kw}, headers=_h(client, user))
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


def _load_content(db: Session, oid: str) -> dict:
    """从对象存储读取内容文档(STO-01: 经公开门面, 不拼路径)。"""
    from iesplan.storage import get_object

    raw = get_object(db, oid)
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# 主流程测试
# ---------------------------------------------------------------------------


def test_project_lifecycle_flow(client: TestClient, db_session: Session) -> None:
    """创建→非所有者不可见→编辑→版本→归档→删除→审计 全流程。"""
    owner = make_user(db_session, "owner1")
    stranger = make_user(db_session, "stranger1")
    owner_h = _h(client, owner)

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

    # 2. 非所有者不可见/不可编辑(0.8.0 起无共享成员, 权限仅属所有者)
    assert client.get(f"/api/projects/{pid}", headers=_h(client, stranger)).status_code == 403
    edit_payload = {
        "expected_revision": 1,
        "commands": [_device_cmd("cmd-x", "热泵X")],
    }
    resp = client.put(f"/api/projects/{pid}/draft", json=edit_payload, headers=_h(client, stranger))
    assert resp.status_code == 403

    # 3. 所有者编辑(语义命令, 乐观锁, 幂等)
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
    vcontent = _load_content(db_session, version["content_hash"])
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

    # 7. 删除流程(0.2.0 B4 强化确认): 未确认 → 400;
    #    空布尔 confirm 不足以确认(须项目名或原因) → 400;
    #    项目名不匹配 → 400; 项目名匹配 → 删除; 删除后 404
    # (TestClient.delete 不支持 json 请求体, 用通用 request 发送)
    resp = client.request("DELETE", f"/api/projects/{pid}", json={"confirm": False}, headers=owner_h)
    assert resp.status_code == 400
    resp = client.request("DELETE", f"/api/projects/{pid}", json={"confirm": True}, headers=owner_h)
    assert resp.status_code == 400
    resp = client.request(
        "DELETE", f"/api/projects/{pid}", json={"confirm": True, "name": "错误项目名"}, headers=owner_h
    )
    assert resp.status_code == 400
    resp = client.request(
        "DELETE", f"/api/projects/{pid}", json={"confirm": True, "name": "园区综合能源"}, headers=owner_h
    )
    assert resp.status_code == 204
    assert client.get(f"/api/projects/{pid}", headers=owner_h).status_code == 404

    # 8. 审计事件存在
    rows = db_session.execute(select(AuditLog).order_by(AuditLog.id)).scalars().all()
    actions = [row.action for row in rows]
    for expected in (
        "project.created",
        "project.draft_updated",
        "project.version_created",
        "project.archived",
        "project.unarchived",
        "project.deleted",
    ):
        assert expected in actions, f"缺少审计事件 {expected}: {actions}"
    # 删除审计含确认方式与原因(0.2.0 B4)
    deleted_log = next(row for row in rows if row.action == "project.deleted")
    assert deleted_log.after["confirm"] == "name"
    assert deleted_log.after["reason"] is None


# ---------------------------------------------------------------------------
# 恢复版本 / 应用结果
# ---------------------------------------------------------------------------


def test_restore_version(client: TestClient, db_session: Session) -> None:
    """恢复历史版本: 创建新版本+新草稿, 不倒写历史。"""
    owner = make_user(db_session, "owner_restore")
    owner_h = _h(client, owner)
    pid = _create_project(client, owner, "恢复测试")

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
    owner = make_user(db_session, "owner_apply")
    owner_h = _h(client, owner)
    pid = _create_project(client, owner, "应用结果测试")
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
# 维护访问 / 列表 / 命令校验
# ---------------------------------------------------------------------------


def test_admin_maintenance_readonly(client: TestClient, db_session: Session) -> None:
    """管理员维护访问: 可读/可删/可转移(admin.py 维护入口), 不能业务编辑; 非成员 403。"""
    owner = make_user(db_session, "owner_maint")
    admin = make_user(db_session, "admin_maint", role="admin")
    stranger = make_user(db_session, "stranger_maint")
    pid = _create_project(client, owner, "维护测试")

    # 管理员可读项目细节(维护只读), 可删除(整体管理, 0.2.0 B4 需项目名/原因确认)
    assert client.get(f"/api/projects/{pid}", headers=_h(client, admin)).status_code == 200
    resp = client.request(
        "DELETE", f"/api/projects/{pid}", headers=_h(client, admin),
        json={"confirm": True, "name": "维护测试"},
    )
    assert resp.status_code == 204

    # 管理员不能业务编辑(维护只读)
    pid2 = _create_project(client, owner, "维护测试2")
    resp = client.put(
        f"/api/projects/{pid2}/draft",
        json={"expected_revision": 1, "commands": []},
        headers=_h(client, admin),
    )
    assert resp.status_code == 403
    # 非成员非管理员 → 403
    assert client.get(f"/api/projects/{pid2}", headers=_h(client, stranger)).status_code == 403


def test_visible_listing(client: TestClient, db_session: Session) -> None:
    """可见列表: 仅所有者可见(0.8.0 起无共享成员), 非所有者不可见。"""
    owner = make_user(db_session, "owner_list")
    other = make_user(db_session, "other_list")
    stranger = make_user(db_session, "stranger_list")
    pid = _create_project(client, owner, "列表项目")

    owner_list = client.get("/api/projects", headers=_h(client, owner)).json()["projects"]
    other_list = client.get("/api/projects", headers=_h(client, other)).json()["projects"]
    stranger_list = client.get("/api/projects", headers=_h(client, stranger)).json()["projects"]
    assert [p["id"] for p in owner_list] == [pid]
    assert other_list == []
    assert stranger_list == []
    assert owner_list[0]["my_role"] == "owner"


def test_invalid_commands(client: TestClient, db_session: Session) -> None:
    """命令校验: 缺幂等标识/未知类型/陈旧修订。"""
    owner = make_user(db_session, "owner_inv")
    owner_h = _h(client, owner)
    pid = _create_project(client, owner, "命令校验")

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


def test_delete_requires_name_or_reason(client: TestClient, db_session: Session) -> None:
    """0.2.0 B4: 删除确认强化 —— 空布尔 confirm 不足以确认, 须项目名或删除原因。"""
    owner = make_user(db_session, "owner_del2")
    owner_h = _h(client, owner)
    pid = _create_project(client, owner, "删除确认项目")

    # 空布尔 confirm 单独 → 400 PROJ-DEL-002
    resp = client.request("DELETE", f"/api/projects/{pid}", json={"confirm": True}, headers=owner_h)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PROJ-DEL-002"
    # 项目名不匹配 → 400 PROJ-DEL-003
    resp = client.request(
        "DELETE", f"/api/projects/{pid}", json={"confirm": True, "name": "别的项目"}, headers=owner_h
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PROJ-DEL-003"
    # 只提供原因 → 删除成功, 审计含 confirm=reason
    resp = client.request(
        "DELETE", f"/api/projects/{pid}",
        json={"confirm": True, "reason": "误建的测试项目,需移除"}, headers=owner_h,
    )
    assert resp.status_code == 204
    assert client.get(f"/api/projects/{pid}", headers=owner_h).status_code == 404
    deleted = db_session.execute(
        select(AuditLog).where(AuditLog.action == "project.deleted").order_by(AuditLog.id.desc())
    ).scalars().first()
    assert deleted is not None
    assert deleted.after["confirm"] == "reason"
    assert deleted.after["reason"] == "误建的测试项目,需移除"


def test_anonymous_and_xuserid_rejected(client: TestClient, db_session: Session) -> None:
    """匿名访问一律 401; 伪造 X-User-Id 头不再被接受(已由窗口会话认证取代)。"""
    # 匿名 GET / 列表 → 401
    resp = client.get("/api/projects")
    assert resp.status_code == 401
    assert resp.json()["error"]["message_key"] in ("ies.diag.auth.required", "ies.diag.auth.session_invalid")
    # 匿名 POST / 创建 → 401
    resp = client.post("/api/projects", json={"name": "匿名项目"})
    assert resp.status_code == 401
    # 伪造 X-User-Id 头(旧模拟鉴权) → 401
    resp = client.get("/api/projects", headers={"X-User-Id": "1"})
    assert resp.status_code == 401
    # 登录后(窗口凭证) → 200
    owner = make_user(db_session, "owner_anon")
    resp = client.get("/api/projects", headers=_h(client, owner))
    assert resp.status_code == 200
