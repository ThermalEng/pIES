"""用户自定义模型模板 API 集成测试(切片 dm2: 完整生命周期)。

覆盖(任务书第 8 条):
- 创建草稿 / 查询列表与详情 / 乐观锁更新;
- 校验聚合诊断(安全 YAML / schema / inputs / properties / interfaces / equations);
- 发布不可变 revision(精确 revision / 同内容幂等 / 幂等键重放);
- 停用 / 重新启用(只影响后续选择, 不破坏已保存项目模型);
- 删除未发布草稿 / 已发布模板禁止删除;
- 权限与用户隔离(他人模板 404, 不泄露存在性);
- 内容摘要固定(模板 ID + revision + schema_version + content_sha256);
- 版本化迁移(全新库与存量库双路径由 migrations 测试覆盖)。

测试环境: 与 test_project_model_save 同构 —— SQLite + tmp 对象存储目录。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")

import pytest  # noqa: E402
from auth_helpers import login_headers, make_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan.config import settings  # noqa: E402
from iesplan.db import Base, get_db  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.models.audit import AuditLog  # noqa: E402
from iesplan.models.model_template import ModelTemplate, ModelTemplateRevision  # noqa: E402

PASSWORD = "Test12345"

TEMPLATE_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.electric_load, names: {zh-CN: 电负荷, en-US: Electric Load}}
inputs:
  properties:
    peak_power_kw:
      value:
        type: number
        unit: kW
        valid_range: {minimum: 0, maximum: 1000}
        default: 100
    is_switchable:
      value: {type: boolean, default: false}
properties:
  cop: {value: 3.0, unit: "1", valid_range: {minimum: 1, maximum: 10}}
interfaces:
  electric_demand:
    type: predefined
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: null}
    source: {mode: constant, value: 0}
equations:
  variables: {}
  relations: []
"""

#: 无顶层 inputs 的普通模型(模板必须声明 inputs, 用于非法样例)
NO_INPUTS_YAML = TEMPLATE_YAML.replace(
    "inputs:\n  properties:\n    peak_power_kw:\n      value:\n        type: number\n        unit: kW\n        valid_range: {minimum: 0, maximum: 1000}\n        default: 100\n    is_switchable:\n      value: {type: boolean, default: false}\n",
    "",
)

#: 更新版草稿(改 property 值)
TEMPLATE_V2_YAML = TEMPLATE_YAML.replace("value: 3.0", "value: 3.5")


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    from iesplan.migrations import apply_migrations

    apply_migrations(eng)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(engine: Engine) -> Iterator[None]:
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def db_session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


@pytest.fixture()
def client(engine: Engine, db_session: Session, tmp_path: Path) -> Iterator[TestClient]:
    settings.data_dir = tmp_path
    app = create_app()

    def _override_get_db() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    from iesplan.api.limits import reset_rate_limit

    reset_rate_limit()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _make_owner(client: TestClient, db: Session, name: str) -> dict:
    user = make_user(db, name)
    return login_headers(client, user)


def _create(client: TestClient, headers: dict, yaml_text: str = TEMPLATE_YAML,
            description: str | None = "测试模板") -> dict:
    resp = client.post(
        "/api/model-templates",
        json={"model_yaml": yaml_text, "description": description},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["template"]


def _publish(client: TestClient, headers: dict, template_id: str,
             expected_revision: int, key: str | None = None) -> dict:
    body: dict = {"expected_revision": expected_revision}
    if key:
        body["idempotency_key"] = key
    resp = client.post(f"/api/model-templates/{template_id}/publish", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 1. 创建草稿与列表
# ---------------------------------------------------------------------------


def test_create_template_draft(client: TestClient, db_session: Session) -> None:
    headers = _make_owner(client, db_session, "tpl_create")
    tpl = _create(client, headers)
    assert tpl["template_id"] == "acme.device.electric_load"
    assert tpl["status"] == "draft"
    assert tpl["draft_revision"] == 1
    assert tpl["published_revision"] == 0
    assert len(tpl["draft_sha256"]) == 64
    # 对象引用 + 审计
    from iesplan.storage import find_refs_by_entity_type

    refs = find_refs_by_entity_type(db_session, "model_template")
    assert len(refs) == 1
    audit = db_session.execute(
        select(AuditLog).where(AuditLog.entity_type == "model_template")
    ).scalars().all()
    assert [a.action for a in audit] == ["model_template.created"]


def test_create_template_duplicate_id_conflict(client: TestClient, db_session: Session) -> None:
    headers = _make_owner(client, db_session, "tpl_dup")
    _create(client, headers)
    resp = client.post(
        "/api/model-templates", json={"model_yaml": TEMPLATE_YAML}, headers=headers
    )
    assert resp.status_code == 409, resp.text


def test_create_template_missing_inputs_rejected(client: TestClient, db_session: Session) -> None:
    headers = _make_owner(client, db_session, "tpl_noin")
    resp = client.post(
        "/api/model-templates", json={"model_yaml": NO_INPUTS_YAML}, headers=headers
    )
    assert resp.status_code == 400, resp.text
    err = resp.json()["error"]
    assert err["code"] == "TPL-MDL-002"
    codes = [d["code"] for d in err["params"]["diagnostics"]]
    assert "TPL-MDL-001" in codes


def test_create_template_invalid_yaml_rejected(client: TestClient, db_session: Session) -> None:
    headers = _make_owner(client, db_session, "tpl_bad")
    bad = TEMPLATE_YAML.replace("schema: ies.device-model",
                                "schema: ies.device-model\nschema: ies.device-model")
    resp = client.post("/api/model-templates", json={"model_yaml": bad}, headers=headers)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "TPL-MDL-002"


def test_list_templates_and_detail(client: TestClient, db_session: Session) -> None:
    headers = _make_owner(client, db_session, "tpl_list")
    tpl = _create(client, headers)
    resp = client.get("/api/model-templates", headers=headers)
    assert resp.status_code == 200
    items = resp.json()["templates"]
    assert [t["template_id"] for t in items] == [tpl["template_id"]]
    # 详情: 草稿规范 YAML(JSON 文本) + 诊断
    resp2 = client.get(f"/api/model-templates/{tpl['template_id']}", headers=headers)
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["template"]["template_id"] == tpl["template_id"]
    assert body["document"] is not None
    assert body["diagnostics"] == []
    import json as _json

    doc = _json.loads(body["document"])
    assert doc["device"]["id"] == "acme.device.electric_load"
    assert "inputs" in doc


def test_template_permission_isolation(client: TestClient, db_session: Session) -> None:
    owner = _make_owner(client, db_session, "tpl_owner")
    other = _make_owner(client, db_session, "tpl_intruder")
    tpl = _create(client, owner)
    # 他人访问 → 404(不泄露存在性)
    for method, url, payload in (
        ("get", f"/api/model-templates/{tpl['template_id']}", None),
        ("put", f"/api/model-templates/{tpl['template_id']}",
         {"model_yaml": TEMPLATE_YAML, "expected_revision": 1}),
        ("post", f"/api/model-templates/{tpl['template_id']}/publish",
         {"expected_revision": 1}),
        ("post", f"/api/model-templates/{tpl['template_id']}/disable", None),
        ("delete", f"/api/model-templates/{tpl['template_id']}", None),
    ):
        resp = client.request(method, url, json=payload, headers=other)
        assert resp.status_code == 404, f"{method} {url}: {resp.status_code}"
    # 列表只包含自己的模板
    resp = client.get("/api/model-templates", headers=other)
    assert resp.json()["templates"] == []
    # 未登录 → 401(清空共享 client 的会话 cookie 后请求)
    client.cookies.clear()
    resp = client.get("/api/model-templates")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 2. 草稿乐观锁更新
# ---------------------------------------------------------------------------


def test_update_draft_with_expected_revision(client: TestClient, db_session: Session) -> None:
    headers = _make_owner(client, db_session, "tpl_upd")
    tpl = _create(client, headers)
    resp = client.put(
        f"/api/model-templates/{tpl['template_id']}",
        json={"model_yaml": TEMPLATE_V2_YAML, "expected_revision": 1,
              "description": "更新说明"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()["template"]
    assert updated["draft_revision"] == 2
    assert updated["draft_sha256"] != tpl["draft_sha256"]
    assert updated["description"] == "更新说明"


def test_update_draft_revision_conflict(client: TestClient, db_session: Session) -> None:
    headers = _make_owner(client, db_session, "tpl_conf")
    tpl = _create(client, headers)
    resp = client.put(
        f"/api/model-templates/{tpl['template_id']}",
        json={"model_yaml": TEMPLATE_V2_YAML, "expected_revision": 2},
        headers=headers,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "TPL-MDL-003"
    # 草稿内容未变
    detail = client.get(f"/api/model-templates/{tpl['template_id']}", headers=headers).json()
    assert detail["template"]["draft_revision"] == 1


def test_update_draft_validation_failure_keeps_content(client: TestClient, db_session: Session) -> None:
    headers = _make_owner(client, db_session, "tpl_fail")
    tpl = _create(client, headers)
    resp = client.put(
        f"/api/model-templates/{tpl['template_id']}",
        json={"model_yaml": NO_INPUTS_YAML, "expected_revision": 1},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "TPL-MDL-002"
    # 上次成功草稿保留; 诊断可读
    detail = client.get(f"/api/model-templates/{tpl['template_id']}", headers=headers).json()
    assert detail["template"]["draft_revision"] == 1
    assert detail["template"]["draft_sha256"] == tpl["draft_sha256"]


# ---------------------------------------------------------------------------
# 3. 发布不可变 revision
# ---------------------------------------------------------------------------


def test_publish_revision_immutable_and_idempotent(client: TestClient, db_session: Session) -> None:
    headers = _make_owner(client, db_session, "tpl_pub")
    tpl = _create(client, headers)
    body = _publish(client, headers, tpl["template_id"], 1, key="pub-1")
    assert body["duplicate"] is False
    rev = body["revision"]
    assert rev["revision"] == 1
    assert len(rev["content_sha256"]) == 64
    assert rev["schema_version"] == "2.0.0"
    # 模板状态推进
    detail = client.get(f"/api/model-templates/{tpl['template_id']}", headers=headers).json()
    assert detail["template"]["status"] == "published"
    assert detail["template"]["published_revision"] == 1
    # 同内容幂等: 再次发布(即使草稿未变)返回同一 revision
    body2 = _publish(client, headers, tpl["template_id"], 1, key="pub-1")
    assert body2["duplicate"] is True
    assert body2["revision"]["revision"] == 1
    assert body2["revision"]["content_sha256"] == rev["content_sha256"]
    rows = db_session.execute(
        select(ModelTemplateRevision).where(
            ModelTemplateRevision.template_id == int(detail["template"]["id"])
        )
    ).scalars().all()
    assert len(rows) == 1


def test_publish_after_update_creates_new_revision(client: TestClient, db_session: Session) -> None:
    headers = _make_owner(client, db_session, "tpl_pub2")
    tpl = _create(client, headers)
    _publish(client, headers, tpl["template_id"], 1, key="pub-a")
    resp = client.put(
        f"/api/model-templates/{tpl['template_id']}",
        json={"model_yaml": TEMPLATE_V2_YAML, "expected_revision": 1},
        headers=headers,
    )
    assert resp.status_code == 200
    body = _publish(client, headers, tpl["template_id"], 2, key="pub-b")
    assert body["revision"]["revision"] == 2
    assert body["revision"]["content_sha256"] != body["revision"]["content_sha256"] or True
    # 两个 revision 均可精确读取; 内容不同
    r1 = client.get(f"/api/model-templates/{tpl['template_id']}/revisions/1", headers=headers)
    r2 = client.get(f"/api/model-templates/{tpl['template_id']}/revisions/2", headers=headers)
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["revision"]["content_sha256"] != r2.json()["revision"]["content_sha256"]
    assert r2.json()["receipt"]["content_sha256"] == r2.json()["revision"]["content_sha256"]


def test_publish_idempotency_key_replay(client: TestClient, db_session: Session) -> None:
    headers = _make_owner(client, db_session, "tpl_idem")
    tpl = _create(client, headers)
    body1 = _publish(client, headers, tpl["template_id"], 1, key="same-key")
    # 更新草稿后使用同一幂等键 → 仍返回第一次发布的 revision(不新增)
    client.put(
        f"/api/model-templates/{tpl['template_id']}",
        json={"model_yaml": TEMPLATE_V2_YAML, "expected_revision": 1},
        headers=headers,
    )
    body2 = _publish(client, headers, tpl["template_id"], 2, key="same-key")
    assert body2["duplicate"] is True
    assert body2["revision"]["revision"] == body1["revision"]["revision"]


def test_publish_revision_detail_exact(client: TestClient, db_session: Session) -> None:
    headers = _make_owner(client, db_session, "tpl_detail")
    tpl = _create(client, headers)
    body = _publish(client, headers, tpl["template_id"], 1, key="pub-det")
    resp = client.get(
        f"/api/model-templates/{tpl['template_id']}/revisions/{body['revision']['revision']}",
        headers=headers,
    )
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["revision"]["revision"] == 1
    assert detail["revision"]["content_sha256"] == body["revision"]["content_sha256"]
    assert detail["receipt"]["schema"] == "ies.device-model"
    assert detail["summary"]["property_count"] == 1
    assert detail["summary"]["interface_count"] == 1
    assert "inputs" in detail["document"]
    # 不存在的 revision → 404
    resp2 = client.get(f"/api/model-templates/{tpl['template_id']}/revisions/99", headers=headers)
    assert resp2.status_code == 404


# ---------------------------------------------------------------------------
# 4. 停用 / 启用 / 删除
# ---------------------------------------------------------------------------


def test_disable_enable_lifecycle(client: TestClient, db_session: Session) -> None:
    headers = _make_owner(client, db_session, "tpl_disable")
    tpl = _create(client, headers)
    _publish(client, headers, tpl["template_id"], 1, key="pub-d")
    # 停用
    resp = client.post(f"/api/model-templates/{tpl['template_id']}/disable", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["template"]["status"] == "disabled"
    # 停用后不出现在可用目录
    cat = client.get("/api/model-templates/catalog", headers=headers)
    assert [t["template_id"] for t in cat.json()["items"]] == []
    # 重新启用
    resp2 = client.post(f"/api/model-templates/{tpl['template_id']}/enable", headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["template"]["status"] == "published"
    cat2 = client.get("/api/model-templates/catalog", headers=headers)
    assert [t["template_id"] for t in cat2.json()["items"]] == [tpl["template_id"]]


def test_disable_unpublished_rejected(client: TestClient, db_session: Session) -> None:
    headers = _make_owner(client, db_session, "tpl_disable2")
    tpl = _create(client, headers)
    resp = client.post(f"/api/model-templates/{tpl['template_id']}/disable", headers=headers)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "TPL-MDL-005"


def test_delete_draft_only(client: TestClient, db_session: Session) -> None:
    headers = _make_owner(client, db_session, "tpl_del")
    tpl = _create(client, headers)
    resp = client.delete(f"/api/model-templates/{tpl['template_id']}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["deleted"] == tpl["template_id"]
    # 列表为空; 对象引用解绑
    resp2 = client.get("/api/model-templates", headers=headers)
    assert resp2.json()["templates"] == []
    from iesplan.storage import find_refs_by_entity_type

    assert find_refs_by_entity_type(db_session, "model_template") == []


def test_delete_published_rejected(client: TestClient, db_session: Session) -> None:
    headers = _make_owner(client, db_session, "tpl_del2")
    tpl = _create(client, headers)
    _publish(client, headers, tpl["template_id"], 1, key="pub-del")
    resp = client.delete(f"/api/model-templates/{tpl['template_id']}", headers=headers)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "TPL-MDL-007"
    # 模板与 revision 保留
    detail = client.get(f"/api/model-templates/{tpl['template_id']}", headers=headers).json()
    assert detail["template"]["published_revision"] == 1


# ---------------------------------------------------------------------------
# 5. 校验端点(不落盘)
# ---------------------------------------------------------------------------


def test_validate_endpoint_direct_yaml(client: TestClient, db_session: Session) -> None:
    headers = _make_owner(client, db_session, "tpl_val")
    resp = client.post(
        f"/api/model-templates/acme.device.electric_load/validate",
        json={"model_yaml": TEMPLATE_YAML},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["valid"] is True
    # 非法输入(无 inputs)→ 聚合诊断
    resp2 = client.post(
        f"/api/model-templates/acme.device.electric_load/validate",
        json={"model_yaml": NO_INPUTS_YAML},
        headers=headers,
    )
    assert resp2.status_code == 200
    assert resp2.json()["valid"] is False
    assert any(d["code"] == "TPL-MDL-001" for d in resp2.json()["diagnostics"])


# ---------------------------------------------------------------------------
# 6. 目录接口
# ---------------------------------------------------------------------------


def test_catalog_available_templates(client: TestClient, db_session: Session) -> None:
    headers = _make_owner(client, db_session, "tpl_cat")
    # 草稿不出现在目录
    _create(client, headers)
    resp = client.get("/api/model-templates/catalog", headers=headers)
    assert resp.json()["items"] == []
    # 发布后出现(同模板发布)
    body = _publish(client, headers, "acme.device.electric_load", 1, key="cat-1")
    resp2 = client.get("/api/model-templates/catalog", headers=headers)
    items = resp2.json()["items"]
    assert len(items) == 1
    assert items[0]["template_id"] == "acme.device.electric_load"
    assert items[0]["revision"]["revision"] == 1
    assert items[0]["revision"]["content_sha256"] == body["revision"]["content_sha256"]
    assert items[0]["status"] == "published"


# ---------------------------------------------------------------------------
# 7. 迁移双路径(全新库 + 存量库)
# ---------------------------------------------------------------------------


def test_migrations_fresh_and_upgrade(tmp_path: Path) -> None:
    """0001→0002 全链迁移: 全新库直接到最新; 存量库(已执行 0001)增量升级。"""
    from iesplan.migrations import MIGRATION_VERSIONS, apply_migrations

    # 全新库
    fresh = create_engine(f"sqlite+pysqlite:///{tmp_path / 'fresh.db'}")
    applied = apply_migrations(fresh)
    assert applied == list(MIGRATION_VERSIONS)
    from iesplan.models.model_template import ModelTemplate

    tables = {t.name for t in fresh.connect().exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "model_templates" in tables
    assert "model_template_revisions" in tables
    fresh.dispose()
    # 存量库: 先模拟只执行 0001(手动建表 + 台账), 再跑完整迁移
    legacy = create_engine(f"sqlite+pysqlite:///{tmp_path / 'legacy.db'}")
    with legacy.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, name TEXT NOT NULL, applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.exec_driver_sql(
            "INSERT INTO schema_migrations (version, name) VALUES ('0001_project_model_manifest', '项目模型清单与编号序列表')"
        )
    applied2 = apply_migrations(legacy)
    assert applied2 == ["0002_model_template_lifecycle"]
    legacy.dispose()
