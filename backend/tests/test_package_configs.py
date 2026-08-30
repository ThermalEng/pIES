"""项目包携带规划/财务配置 revision(0.6.5 事项 3)导出/导入集成测试。

覆盖:
- 导出: 项目有配置 → 包内含 finance_config.json / planning_config.json
  (对象清单逐对象校验值一致, manifest.files.configs 登记); 无配置 → 不含;
- 导入: 配置随包重建为 revision=1(不覆盖既有项目, 新项目身份, 所有者=导入者);
  规划配置 finance_revision 与财务配置摘要一致; 无配置包导入后无配置(不静默默认);
- 校验拒绝(ImportValidationError, PKG-IMP-001): 规划缺财务引用 / 文件级
  content_sha256 不一致 / 声明 revision 被篡改 / 币种领域校验失败 /
  规划引用的财务 revision 与包内不一致。

测试环境: SQLite :memory:(StaticPool 共享连接) + tmp 对象存储目录。
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from collections.abc import Iterator
from pathlib import Path

# 单文件运行时的安全网: 固定 SQLite, 避免误连部署 Postgres
os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")

import pytest  # noqa: E402
from auth_helpers import login_headers, make_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan.api import exports as exports_api  # noqa: E402
from iesplan.api import projects as projects_api  # noqa: E402
from iesplan.config import settings  # noqa: E402
from iesplan.core.contracts import (  # noqa: E402
    FinanceConfig,
    PlanningConfig,
    ProjectBaseline,
)
from iesplan.core.idgen import sha256_hex  # noqa: E402
from iesplan.db import Base, get_db  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.services import package as package_service  # noqa: E402

# ---------------------------------------------------------------------------
# 样例
# ---------------------------------------------------------------------------

FINANCE_PAYLOAD: dict = {
    "currency": "CNY",
    "base_year": 2025,
    "devices": {
        "heat_pump_1": {
            "unit_investment": {"value": "1800", "unit": "CNY/kW"},
            "fixed_om_rate": "0.02",
            "variable_om": {"value": "0.03", "unit": "CNY/kWh"},
        },
    },
    "energy_prices": {
        "electricity_purchase": {"value": "0.6", "unit": "CNY/kWh"},
    },
    "tax_rate": "0.25",
    "capital_time_cost": "0.08",
}


def _planning_payload(finance_revision: str) -> dict:
    return {
        "objective": {"sense": "minimize", "expression": "system.total_financial_cost"},
        "variables": {
            "hp_cap": {
                "device_ref": "heat_pump_1",
                "parameter": "capacity",
                "lower_bound": "0",
                "upper_bound": "1000",
                "unit": "kW",
            },
        },
        "constraints": {
            "c1": {
                "type": "ratio",
                "expression": "hp1.electricity_in[t] <= 0.8 * grid.electricity_out[t]",
                "enabled": True,
            },
        },
        "finance_revision": finance_revision,
    }


# ---------------------------------------------------------------------------
# 测试环境(与 test_package_api.py 同构)
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
def _clean_state(engine: Engine) -> Iterator[None]:
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def db(engine: Engine, tmp_path: Path) -> Iterator[Session]:
    settings.data_dir = tmp_path
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


@pytest.fixture()
def client(engine: Engine, db: Session, tmp_path: Path) -> Iterator[TestClient]:
    settings.data_dir = tmp_path
    app = create_app()
    app.include_router(projects_api.router)
    app.include_router(exports_api.router)

    def _override_get_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _h(client: TestClient, user) -> dict[str, str]:
    return login_headers(client, user)


def _create_project(client: TestClient, user, name: str = "配置包项目") -> int:
    resp = client.post(
        "/api/projects",
        json={
            "name": name,
            "baseline_resolution": "1h",
            "baseline_leap_year": False,
            "baseline_scenario_mode": "single",
        },
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["project"]["id"]


def _save_finance(client: TestClient, user, pid: int) -> str:
    """保存财务配置, 返回其规范摘要(revision)。"""
    resp = client.put(
        f"/api/projects/{pid}/finance-config",
        json={"finance_config": FINANCE_PAYLOAD, "expected_revision": None},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["finance_config"]["revision"]


def _save_planning(client: TestClient, user, pid: int, finance_revision: str) -> str:
    resp = client.put(
        f"/api/projects/{pid}/planning-config",
        json={
            "planning_config": _planning_payload(finance_revision),
            "expected_revision": None,
        },
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["planning_config"]["revision"]


def _export_zip(client: TestClient, user, pid: int) -> bytes:
    """导出项目包并下载字节(仅所有者)。"""
    resp = client.post(f"/api/projects/{pid}/exports/package", headers=_h(client, user))
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    resp = client.get(
        f"/api/projects/{pid}/exports/package/download",
        params={"token": token},
        headers=_h(client, user),
    )
    assert resp.status_code == 200
    return resp.content


def _config_doc(payload: dict, digest: str) -> dict:
    """构造包内配置文件文档形态: {revision, content_sha256, <field>: payload}。"""
    return {
        "revision": 1,
        "content_sha256": digest,
        **payload,
    }


def _build_package(extra_entries: dict[str, dict], configs_meta: dict) -> bytes:
    """手工构造项目包 zip(含给定配置文件与 files.configs 清单, 完整对象清单)。

    供负例测试使用: 先经 _config_doc 构造合法配置文档, 测试再篡改字段。
    """
    entries: dict[str, bytes] = {
        "project.json": json.dumps(
            {
                "name": "定制包项目",
                "currency": "CNY",
                "project_baseline": ProjectBaseline(
                    resolution="1h", leap_year=False
                ).to_dict(),
            },
            ensure_ascii=False,
        ).encode(),
        "draft.json": json.dumps(
            {"revision": 1, "content_hash": "0" * 64, "content": {}}
        ).encode(),
    }
    for path, doc in extra_entries.items():
        entries[path] = json.dumps(doc, ensure_ascii=False).encode()

    objects = [
        {
            "path": path,
            "sha256": sha256_hex(raw),
            "size_bytes": len(raw),
            "media_type": "application/json",
        }
        for path, raw in sorted(entries.items())
    ]
    aggregate = sha256_hex(
        "".join(f"{e['path']}\0{e['sha256']}\0" for e in objects).encode("utf-8")
    )
    manifest = {
        "format_version": "1.0",
        "package_type": "project",
        "project": {
            "name": "定制包项目",
            "currency": "CNY",
            "project_baseline": ProjectBaseline(
                resolution="1h", leap_year=False
            ).to_dict(),
        },
        "files": {"configs": configs_meta},
        "objects": objects,
        "checksums": {"entry_count": len(objects), "aggregate_sha256": aggregate},
    }
    entries["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, raw in sorted(entries.items()):
            zf.writestr(path, raw)
    return buf.getvalue()


def _valid_finance_doc() -> tuple[dict, str]:
    """合法财务配置文档 + 摘要(供负例篡改基底)。"""
    config = FinanceConfig.from_dict(FINANCE_PAYLOAD)
    return _config_doc({"finance_config": config.to_dict()}, config.revision), config.revision


def _valid_planning_doc(finance_revision: str) -> tuple[dict, str]:
    config = PlanningConfig.from_dict(_planning_payload(finance_revision))
    return _config_doc({"planning_config": config.to_dict()}, config.revision), config.revision


# ---------------------------------------------------------------------------
# 导出/导入往返
# ---------------------------------------------------------------------------


def test_export_and_import_roundtrip_with_configs(client: TestClient, db: Session) -> None:
    """有财务+规划配置的项目包: 导出含两文件与清单登记; 导入重建 revision=1。"""
    owner = make_user(db, "pkg_owner")
    importer = make_user(db, "pkg_importer")
    pid = _create_project(client, owner)
    finance_rev = _save_finance(client, owner, pid)
    planning_rev = _save_planning(client, owner, pid, finance_rev)

    zip_bytes = _export_zip(client, owner, pid)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["files"]["configs"] == {
            "finance_config": "finance_config.json",
            "planning_config": "planning_config.json",
        }
        names = set(zf.namelist())
        assert {"finance_config.json", "planning_config.json"} <= names
        # 逐对象校验值一致
        for entry in manifest["objects"]:
            raw = zf.read(entry["path"])
            assert len(raw) == entry["size_bytes"]
            assert sha256_hex(raw) == entry["sha256"]
        finance_doc = json.loads(zf.read("finance_config.json").decode("utf-8"))
        assert finance_doc["content_sha256"] == finance_rev
        planning_doc = json.loads(zf.read("planning_config.json").decode("utf-8"))
        assert planning_doc["content_sha256"] == planning_rev

    proposal = package_service.import_proposal(db, importer, zip_bytes)
    assert proposal.review_summary["configs"]["finance"]["present"] is True
    assert proposal.review_summary["configs"]["finance"]["content_sha256"] == finance_rev
    new_project = package_service.confirm_import(db, importer, proposal.id)
    db.commit()

    # 新项目身份, 配置重建为 revision=1, 规划/财务同 revision
    assert new_project.id != pid
    resp = client.get(
        f"/api/projects/{new_project.id}/finance-config", headers=_h(client, importer)
    )
    assert resp.status_code == 200
    assert resp.json()["revision"] == 1
    assert resp.json()["finance_config"]["revision"] == finance_rev
    resp = client.get(
        f"/api/projects/{new_project.id}/planning-config", headers=_h(client, importer)
    )
    assert resp.status_code == 200
    assert resp.json()["revision"] == 1
    assert resp.json()["planning_config"]["finance_revision"] == finance_rev
    assert proposal.review_summary["configs"]["planning"]["present"] is True


def test_export_import_finance_only(client: TestClient, db: Session) -> None:
    """仅有财务配置: 包内只含 finance_config.json; 导入后规划配置不存在(404)。"""
    owner = make_user(db, "fin_owner")
    importer = make_user(db, "fin_importer")
    pid = _create_project(client, owner)
    finance_rev = _save_finance(client, owner, pid)

    zip_bytes = _export_zip(client, owner, pid)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["files"]["configs"] == {"finance_config": "finance_config.json"}
        assert "planning_config.json" not in zf.namelist()

    proposal = package_service.import_proposal(db, importer, zip_bytes)
    new_project = package_service.confirm_import(db, importer, proposal.id)
    db.commit()
    resp = client.get(
        f"/api/projects/{new_project.id}/finance-config", headers=_h(client, importer)
    )
    assert resp.status_code == 200
    assert resp.json()["finance_config"]["revision"] == finance_rev
    resp = client.get(
        f"/api/projects/{new_project.id}/planning-config", headers=_h(client, importer)
    )
    assert resp.status_code == 404


def test_export_import_without_configs(client: TestClient, db: Session) -> None:
    """无配置项目: 包不含配置文件; 导入后无配置(不静默默认)。"""
    owner = make_user(db, "none_owner")
    importer = make_user(db, "none_importer")
    pid = _create_project(client, owner)

    zip_bytes = _export_zip(client, owner, pid)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["files"]["configs"] == {}
        assert "finance_config.json" not in zf.namelist()

    proposal = package_service.import_proposal(db, importer, zip_bytes)
    assert proposal.review_summary["configs"]["finance"]["present"] is False
    new_project = package_service.confirm_import(db, importer, proposal.id)
    db.commit()
    resp = client.get(
        f"/api/projects/{new_project.id}/finance-config", headers=_h(client, importer)
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 导入校验拒绝(严格校验, 无静默默认)
# ---------------------------------------------------------------------------


def test_import_rejects_planning_without_finance(client: TestClient, db: Session) -> None:
    importer = make_user(db, "rej_imp_1")
    planning_doc, _ = _valid_planning_doc("0" * 64)
    zip_bytes = _build_package(
        {"planning_config.json": planning_doc},
        {"planning_config": "planning_config.json"},
    )
    with pytest.raises(package_service.ImportValidationError) as excinfo:
        package_service.import_proposal(db, importer, zip_bytes)
    assert any("财务配置" in r for r in excinfo.value.reasons)


def test_import_rejects_tampered_content_sha256(client: TestClient, db: Session) -> None:
    importer = make_user(db, "rej_imp_2")
    finance_doc, _ = _valid_finance_doc()
    finance_doc["content_sha256"] = "0" * 64  # 文件级摘要与内容不一致
    zip_bytes = _build_package(
        {"finance_config.json": finance_doc},
        {"finance_config": "finance_config.json"},
    )
    with pytest.raises(package_service.ImportValidationError) as excinfo:
        package_service.import_proposal(db, importer, zip_bytes)
    assert any("content_sha256" in r for r in excinfo.value.reasons)


def test_import_rejects_tampered_declared_revision(client: TestClient, db: Session) -> None:
    importer = make_user(db, "rej_imp_3")
    config = FinanceConfig.from_dict(FINANCE_PAYLOAD)
    doc = _config_doc({"finance_config": {**config.to_dict(), "revision": "0" * 64}}, config.revision)
    zip_bytes = _build_package(
        {"finance_config.json": doc},
        {"finance_config": "finance_config.json"},
    )
    with pytest.raises(package_service.ImportValidationError) as excinfo:
        package_service.import_proposal(db, importer, zip_bytes)
    assert any("财务配置非法" in r for r in excinfo.value.reasons)


def test_import_rejects_currency_domain_violation(client: TestClient, db: Session) -> None:
    importer = make_user(db, "rej_imp_4")
    bad_payload = {
        **FINANCE_PAYLOAD,
        "devices": {
            "heat_pump_1": {
                "unit_investment": {"value": "1800", "unit": "USD/kW"},
                "fixed_om_rate": "0.02",
                "variable_om": {"value": "0.03", "unit": "CNY/kWh"},
            },
        },
    }
    config = FinanceConfig.from_dict(bad_payload)
    doc = _config_doc({"finance_config": config.to_dict()}, config.revision)
    zip_bytes = _build_package(
        {"finance_config.json": doc},
        {"finance_config": "finance_config.json"},
    )
    with pytest.raises(package_service.ImportValidationError) as excinfo:
        package_service.import_proposal(db, importer, zip_bytes)
    assert any("领域校验失败" in r for r in excinfo.value.reasons)


def test_import_rejects_planning_finance_revision_mismatch(
    client: TestClient, db: Session
) -> None:
    importer = make_user(db, "rej_imp_5")
    finance_doc, finance_rev = _valid_finance_doc()
    planning_doc, _ = _valid_planning_doc("0" * 64)  # 引用其他摘要
    zip_bytes = _build_package(
        {"finance_config.json": finance_doc, "planning_config.json": planning_doc},
        {
            "finance_config": "finance_config.json",
            "planning_config": "planning_config.json",
        },
    )
    with pytest.raises(package_service.ImportValidationError) as excinfo:
        package_service.import_proposal(db, importer, zip_bytes)
    assert any("finance_revision" in r or "不一致" in r for r in excinfo.value.reasons)
    assert finance_rev  # 摘要本身有效, 失败来自引用不一致
