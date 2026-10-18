"""项目包项目模型依赖回归: 包内携带 + 导入映射(格式 3.0 files.models)。

原缺陷(修复前红色基线): 导出原样带出内容里的项目模型引用
(``project_models`` 行 id 与设备类型串), 包不携带模型本体; 导入副本继续
消费源项目模型行 — 源项目删除模型即断链, 且副本跨项目共享源项目身份。

覆盖:
- 导出携带: 草稿 ∪ 全部历史版本内容引用的并集(当前草稿未引用但历史版本
  引用的同样携带), 规范 YAML + 清单 base_device_id 元数据; 引用行缺失时
  loudly 拒绝导出(不静默丢弃);
- 导入映射: 经权威保存路径在本项目重建模型资产(新 suffix/新行 id, 源身份
  不复用), 草稿与全部历史版本内容的 ``project_models`` 引用与 ``model`` 节
  ``device_type`` 串改写为新 ID; 源项目删除过模型(suffix 空号)时按真实新
  编号改写, 不产生双后缀;
- 边界拒绝: 内容引用未携带 / 多携带未引用 / 旧主版本(2.x);
- 与消费链一致: 导入副本的已保存模型进入本项目作用域选择器。
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from collections.abc import Iterator
from pathlib import Path

# 单文件运行安全网: 固定 SQLite/内存队列(全量运行时由 conftest 先行覆盖)。
os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")

import pytest  # noqa: E402
from auth_helpers import login_headers, make_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan import identity as identity_domain  # noqa: E402
from iesplan import model as model_domain  # noqa: E402
from iesplan import project as project_domain  # noqa: E402
from iesplan.api.deps import get_request_db  # noqa: E402
from iesplan.application import packages as packages_uc  # noqa: E402
from iesplan.application.projects import load_content_object  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.identity.persistence import User  # noqa: E402,F401
from iesplan.main import create_app  # noqa: E402
from iesplan.storage import get_object  # noqa: E402
from iesplan.storage.persistence import StoredObject  # noqa: E402,F401
from iesplan.tasks import queue  # noqa: E402
from iesplan.tasks.persistence import Task  # noqa: E402,F401

# ---------------------------------------------------------------------------
# 项目模型样例(与 test_project_model_consumption 同构: 属性驱动 sink)
# ---------------------------------------------------------------------------

SINK_BASE = "acme.device.consumption_load"
AUX_BASE = "acme.device.package_aux_load"

SINK_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device:
  id: acme.device.consumption_load
  names:
    zh-CN: 消费链负荷
    en-US: Consumption Load
properties:
  rated_demand_kw:
    value: 10
    unit: kW
    valid_range:
      minimum: 0
      maximum: 100000
interfaces:
  heat_in:
    type: in
    carrier: heat
    unit: kW
    valid_range:
      minimum: 0
      maximum: null
equations:
  variables: {}
  relations:
    - id: serve
      expression: "heat_in[t] = rated_demand_kw"
"""

AUX_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device:
  id: acme.device.package_aux_load
  names:
    zh-CN: 包携带辅助负荷
    en-US: Package Aux Load
properties:
  rated_demand_kw:
    value: 5
    unit: kW
    valid_range:
      minimum: 0
      maximum: 100000
interfaces:
  heat_in:
    type: in
    carrier: heat
    unit: kW
    valid_range:
      minimum: 0
      maximum: null
equations:
  variables: {}
  relations:
    - id: serve
      expression: "heat_in[t] = rated_demand_kw"
"""


# ---------------------------------------------------------------------------
# 测试环境(与 test_project_model_consumption 同模式: 内存 SQLite + 请求级会话)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _clean_state(engine: Engine, db: Session) -> Iterator[None]:
    queue.force_memory()
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def db(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


@pytest.fixture()
def client(engine: Engine, db: Session, tmp_path: Path) -> Iterator[TestClient]:
    from iesplan.config import settings

    settings.data_dir = tmp_path
    app = create_app()

    def _override_request_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_request_db] = _override_request_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _h(client: TestClient, user) -> dict[str, str]:
    return login_headers(client, user)


def _user(db: Session, name: str):
    """真实 UserRecord(供用例直接调用)。"""
    ns = make_user(db, name)
    db.commit()
    rec = identity_domain.get_user(db, ns.id)
    assert rec is not None
    return rec


def _project(client: TestClient, user, name: str) -> int:
    resp = client.post(
        "/api/projects",
        json={
            "name": name,
            "currency": "CNY",
            "baseline_resolution": "1h",
            "baseline_start_year": 2026,
            "baseline_scenario_mode": "single",
        },
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["project"]["id"]


def _revision(client: TestClient, user, pid: int) -> int:
    view = client.get(f"/api/projects/{pid}", headers=_h(client, user)).json()
    return view["draft"]["revision"]


def _save_model(client: TestClient, user, pid: int, model_yaml: str) -> dict:
    resp = client.post(
        f"/api/projects/{pid}/models",
        json={"model_yaml": model_yaml, "expected_revision": _revision(client, user, pid)},
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["project_model"]


def _delete_model(client: TestClient, user, pid: int, model_id: int) -> None:
    resp = client.request(
        "DELETE",
        f"/api/projects/{pid}/models/{model_id}",
        json={"expected_revision": _revision(client, user, pid)},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text


def _upsert_device(client: TestClient, user, pid: int, name: str, device_type: str) -> int:
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={
            "expected_revision": _revision(client, user, pid),
            "commands": [
                {
                    "id": f"pkg-model-{name}",
                    "unit": "model",
                    "type": "model.upsert_device",
                    "payload": {
                        "name": name,
                        "device_type": device_type,
                        "kind": "new",
                        "params": {},
                    },
                },
            ],
        },
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def _create_version(client: TestClient, user, pid: int, name: str) -> int:
    resp = client.post(
        f"/api/projects/{pid}/versions",
        json={"name": name, "reason": "milestone"},
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["version"]["id"]


def _restore_version(client: TestClient, user, pid: int, version_id: int) -> None:
    resp = client.post(
        f"/api/projects/{pid}/versions/{version_id}/restore", headers=_h(client, user)
    )
    assert resp.status_code == 200, resp.text


def _export_bytes(db: Session, user, pid: int) -> bytes:
    exported = packages_uc.export_package(db, user, pid)
    return get_object(db, exported.object_id)


def _load_zip(raw: bytes) -> tuple[dict, dict[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        entries = {info.filename: zf.read(info.filename) for info in zf.infolist()}
    return manifest, entries


def _rebuilt_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, raw in entries.items():
            zf.writestr(path, raw)
    return buf.getvalue()


def _draft_content(db: Session, pid: int) -> dict:
    draft = project_domain.require_current_draft(db, project_domain.get_project(db, pid))
    return load_content_object(db, draft.content_object_id)


def _version_contents(db: Session, pid: int) -> list[dict]:
    contents = []
    for version in project_domain.list_versions(db, pid):
        contents.append(load_content_object(db, version.content_object_id))
    return contents


# ---------------------------------------------------------------------------
# 1) 导出携带: 草稿 ∪ 全部历史版本引用(含仅历史引用的模型)
# ---------------------------------------------------------------------------


def test_export_carries_draft_and_version_referenced_models(
    client: TestClient, db: Session
) -> None:
    owner = _user(db, "pkg_model_owner")
    pid = _project(client, owner, "包模型-导出携带")
    sink = _save_model(client, owner, pid, SINK_YAML)
    assert sink["device_id"] == f"{SINK_BASE}_1"
    v1 = _create_version(client, owner, pid, "V1")
    aux = _save_model(client, owner, pid, AUX_YAML)
    # 编号是项目级单一序列(不按 base 分列): 第二次保存拿到 suffix 2。
    assert aux["device_id"] == f"{AUX_BASE}_2"
    _create_version(client, owner, pid, "V2")
    # 恢复 V1: 当前草稿只引用 sink, aux 仅被 V2 引用(必须仍随包携带)。
    _restore_version(client, owner, pid, v1)

    raw = _export_bytes(db, owner, pid)
    manifest, entries = _load_zip(raw)
    assert manifest["format_version"] == "3.0.0"
    models_meta = manifest["files"]["models"]
    assert [item["device_id"] for item in models_meta] == [
        f"{SINK_BASE}_1",
        f"{AUX_BASE}_2",
    ]
    assert [item["base_device_id"] for item in models_meta] == [SINK_BASE, AUX_BASE]
    object_paths = {entry["path"] for entry in manifest["objects"]}
    for item in models_meta:
        assert item["model_text"] in object_paths
        doc = json.loads(entries[item["model_text"]].decode("utf-8"))
        assert doc["device"]["id"] == item["device_id"]


def test_export_rejects_dangling_version_model_ref(
    client: TestClient, db: Session
) -> None:
    """版本冻结的模型行缺失: 导出 loudly 拒绝(不静默丢弃、不带出空引用)。"""
    owner = _user(db, "pkg_model_owner_dangling")
    pid = _project(client, owner, "包模型-断链导出")
    sink = _save_model(client, owner, pid, SINK_YAML)
    _create_version(client, owner, pid, "V1")
    _delete_model(client, owner, pid, int(sink["id"]))

    with pytest.raises(model_domain.ProjectModelNotFoundError):
        packages_uc.export_package(db, owner, pid)


# ---------------------------------------------------------------------------
# 2) 导入映射: 重建本项目模型资产 + 改写草稿/历史版本引用
# ---------------------------------------------------------------------------


def test_import_rebuilds_project_models_and_remaps_content(
    client: TestClient, db: Session
) -> None:
    owner = _user(db, "pkg_model_owner2")
    pid = _project(client, owner, "包模型-导入映射")
    sink = _save_model(client, owner, pid, SINK_YAML)
    _upsert_device(client, owner, pid, "load2", sink["device_id"])
    v1 = _create_version(client, owner, pid, "V1")
    aux = _save_model(client, owner, pid, AUX_YAML)
    _create_version(client, owner, pid, "V2")
    _restore_version(client, owner, pid, v1)  # 草稿只引用 sink; aux 仅 V2 引用

    raw = _export_bytes(db, owner, pid)
    importer = _user(db, "pkg_model_importer")
    proposal = packages_uc.propose_import(db, importer, raw)
    new_pid = packages_uc.confirm_import(db, importer, proposal.id).id
    assert new_pid != pid

    rows = model_domain.list_project_models(db, new_pid)
    by_base = {row.base_device_id: row for row in rows}
    assert set(by_base) == {SINK_BASE, AUX_BASE}
    assert by_base[SINK_BASE].device_id == f"{SINK_BASE}_1"
    assert by_base[AUX_BASE].device_id == f"{AUX_BASE}_2"
    # 源项目行身份不复用为目标身份。
    assert by_base[SINK_BASE].id != int(sink["id"])
    assert by_base[AUX_BASE].id != int(aux["id"])

    # 草稿(恢复态): 设备类型串与引用改写为新项目身份。
    draft = _draft_content(db, new_pid)
    assert draft["model"]["devices"][0]["device_type"] == by_base[SINK_BASE].device_id
    # 导入重建后草稿引用 = 本项目模型资产集合(保存路径按清单重建引用,
    # 与“保存/删除模型后引用随资产重建”的平台既有语义一致)。
    assert [ref["id"] for ref in draft["project_models"]] == [
        str(by_base[SINK_BASE].id),
        str(by_base[AUX_BASE].id),
    ]
    assert [ref["device_id"] for ref in draft["project_models"]] == [
        by_base[SINK_BASE].device_id,
        by_base[AUX_BASE].device_id,
    ]

    # 历史版本: V2(含 aux)的引用与设备类型串同样映射到新项目身份。
    contents = _version_contents(db, new_pid)
    with_aux = [
        content
        for content in contents
        if {ref["device_id"] for ref in content.get("project_models") or []}
        == {by_base[SINK_BASE].device_id, by_base[AUX_BASE].device_id}
    ]
    assert len(with_aux) == 1
    assert [ref["id"] for ref in with_aux[0]["project_models"]] == [
        str(by_base[SINK_BASE].id),
        str(by_base[AUX_BASE].id),
    ]
    assert with_aux[0]["model"]["devices"][0]["device_type"] == by_base[
        SINK_BASE
    ].device_id

    # 消费链一致: 导入副本的模型进入本项目作用域选择器(导入者可见)。
    resp = client.get(f"/api/projects/{new_pid}/device-types", headers=_h(client, importer))
    assert resp.status_code == 200, resp.text
    items = {item["type_id"] for item in resp.json()["items"]}
    assert by_base[SINK_BASE].device_id in items
    assert by_base[AUX_BASE].device_id in items


def test_import_remaps_suffix_gaps_without_double_suffix(
    client: TestClient, db: Session
) -> None:
    """源项目删除过模型(suffix 空号): 按新编号改写, 不产生双后缀。"""
    owner = _user(db, "pkg_model_owner3")
    pid = _project(client, owner, "包模型-空号导入")
    sink = _save_model(client, owner, pid, SINK_YAML)
    aux = _save_model(client, owner, pid, AUX_YAML)
    _upsert_device(client, owner, pid, "load2", aux["device_id"])
    _delete_model(client, owner, pid, int(sink["id"]))  # 空号: 只剩 _2
    _create_version(client, owner, pid, "V1")

    raw = _export_bytes(db, owner, pid)
    manifest, _ = _load_zip(raw)
    assert [item["device_id"] for item in manifest["files"]["models"]] == [
        f"{AUX_BASE}_2"
    ]

    importer = _user(db, "pkg_model_importer3")
    proposal = packages_uc.propose_import(db, importer, raw)
    new_pid = packages_uc.confirm_import(db, importer, proposal.id).id

    rows = model_domain.list_project_models(db, new_pid)
    assert [row.device_id for row in rows] == [f"{AUX_BASE}_1"]
    draft = _draft_content(db, new_pid)
    assert draft["model"]["devices"][0]["device_type"] == f"{AUX_BASE}_1"
    assert f"{AUX_BASE}_1_1" not in json.dumps(draft, ensure_ascii=False)
    assert [ref["device_id"] for ref in draft["project_models"]] == [f"{AUX_BASE}_1"]


# ---------------------------------------------------------------------------
# 3) 边界拒绝: 依赖携带不完整 / 旧主版本
# ---------------------------------------------------------------------------


def _propose(client: TestClient, user, raw: bytes) -> dict:
    resp = client.post(
        "/api/projects/import",
        files={"file": ("pkg.zip", raw, "application/zip")},
        headers=_h(client, user),
    )
    return resp


def test_import_rejects_missing_and_extra_model_dependencies(
    client: TestClient, db: Session
) -> None:
    owner = _user(db, "pkg_model_owner4")
    pid = _project(client, owner, "包模型-依赖拒绝")
    _save_model(client, owner, pid, SINK_YAML)
    raw = _export_bytes(db, owner, pid)
    importer = _user(db, "pkg_model_importer4")

    # 内容引用未携带: 删掉 files.models 条目。
    manifest, entries = _load_zip(raw)
    manifest["files"]["models"] = []
    entries["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode()
    resp = _propose(client, importer, _rebuilt_zip(entries))
    assert resp.status_code == 400, resp.text
    reasons = resp.json()["error"]["params"]["reasons"]
    assert any("未随包携带" in reason for reason in reasons)

    # 多携带内容未引用的模型: 改掉携带条目的 device_id。
    manifest, entries = _load_zip(raw)
    manifest["files"]["models"][0]["device_id"] = "acme.device.unreferenced_9"
    entries["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode()
    resp = _propose(client, importer, _rebuilt_zip(entries))
    assert resp.status_code == 400, resp.text
    reasons = resp.json()["error"]["params"]["reasons"]
    assert any("内容未引用" in reason for reason in reasons)

    # 旧主版本(2.x)不保留解析。
    manifest, entries = _load_zip(raw)
    manifest["format_version"] = "2.0.0"
    entries["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode()
    resp = _propose(client, importer, _rebuilt_zip(entries))
    assert resp.status_code == 400, resp.text
    reasons = resp.json()["error"]["params"]["reasons"]
    assert any("格式版本不兼容" in reason for reason in reasons)

    # files.models 结构非法: 拒绝而不是按空清单继续。
    manifest, entries = _load_zip(raw)
    manifest["files"]["models"] = "not-a-list"
    entries["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode()
    resp = _propose(client, importer, _rebuilt_zip(entries))
    assert resp.status_code == 400, resp.text
    reasons = resp.json()["error"]["params"]["reasons"]
    assert any("files.models 结构非法" in reason for reason in reasons)
