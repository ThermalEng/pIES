"""项目包携带财务三件套/规划配置 YAML(0.6.5 条目 1-2)导出/导入集成测试。

覆盖:
- 导出: 项目有配置 → 包内含 finance_profile.yaml / finance_overrides.yaml /
  effective_finance.yaml / planning_config.yaml(对象清单逐对象校验值一致,
  manifest.files.configs 登记四键); 无配置 → 不含;
- 导入: 三件套随包重建(revision 由服务层生成), 导入时从精确来源重新合并
  验证三摘要(effective_from_sources); 规划引用 Effective content 一致;
  无配置包导入后无配置(不静默默认);
- 校验拒绝(ImportValidationError, PKG-IMP-001): 三件套缺一 / 声明
  content_sha256 被篡改 / Effective 与 Profile+Overrides 重新合并不一致 /
  规划引用与包内有效快照不一致 / 越权覆盖。

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

from iesplan.api import config_revisions as config_api  # noqa: E402
from iesplan.api import exports as exports_api  # noqa: E402
from iesplan.api import projects as projects_api  # noqa: E402
from iesplan.config import settings  # noqa: E402
from iesplan.core.contracts import PlanningConfig, ProjectBaseline  # noqa: E402
from iesplan.core.idgen import sha256_hex  # noqa: E402
from iesplan.core.yamlmini import dump as yaml_dump  # noqa: E402
from iesplan.db import Base, get_db  # noqa: E402
from iesplan.finance import (  # noqa: E402
    EffectiveFinanceConfig,
    FinanceOverrides,
    FinanceProfile,
    merge_effective,
)
from iesplan.main import create_app  # noqa: E402
from iesplan.services import package as package_service  # noqa: E402

# ---------------------------------------------------------------------------
# 样例(与 test_config_revisions 同源)
# ---------------------------------------------------------------------------

PROFILE_PAYLOAD: dict = {
    "schema": "ies.finance-profile",
    "schema_version": "1.0.0",
    "profile": {
        "id": "cn-north-demo",
        "region": "CN-North",
        "currency": "CNY",
        "base_year": 2025,
        "price_basis": "tax_inclusive",
        "cost_method": "fixed_plus_linear",
    },
    "finance_types": {
        "pv_system": {
            "upfront_capex": {
                "fixed": {"value": "0", "unit": "CNY"},
                "linear": {"capacity_kw": {"unit_cost": {"value": "3500", "unit": "CNY/kW"}}},
            },
            "annual_fixed_om": {
                "linear": {"capacity_kw": {"unit_cost": {"value": "35", "unit": "CNY/kW/a"}}},
            },
            "period_variable_om": {
                "linear": {"generated_kwh": {"unit_cost": {"value": "0.01", "unit": "CNY/kWh"}}},
            },
        },
    },
    "energy_prices": {
        "grid_import": {
            "carrier": "electricity",
            "direction": "purchase",
            "kind": "constant",
            "value": {"value": "0.7", "unit": "CNY/kWh"},
        },
    },
    "taxes": {},
}


def _overrides_payload(profile: FinanceProfile) -> dict:
    return {
        "schema": "ies.finance-overrides",
        "schema_version": "1.0.0",
        "profile_ref": {"id": profile.profile_id, "content_sha256": profile.content_sha256},
        "finance_types": {
            "pv_system": {
                "upfront_capex": {
                    "fixed": {"value": "1500", "unit": "CNY"},
                    "linear": {"capacity_kw": {"unit_cost": {"value": "3200", "unit": "CNY/kW"}}},
                },
            },
        },
        "energy_prices": {
            "grid_import": {"kind": "constant", "value": {"value": "0.75", "unit": "CNY/kWh"}},
        },
    }


def _planning_payload(effective_sha: str) -> dict:
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
        "constraints": {},
        "finance_content_sha256": effective_sha,
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
    app.include_router(config_api.router)
    app.include_router(config_api.profile_router)
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


def _setup_finance(client: TestClient, user, pid: int) -> tuple[FinanceProfile, FinanceOverrides, EffectiveFinanceConfig]:
    """登记 Profile → 项目引用(空覆盖 rev1) → 保存真实覆盖(rev2)。"""
    profile = FinanceProfile.from_dict(PROFILE_PAYLOAD)
    resp = client.post(
        "/api/finance-profiles", json={"finance_profile": PROFILE_PAYLOAD}, headers=_h(client, user)
    )
    assert resp.status_code == 200, resp.text
    resp = client.put(
        f"/api/projects/{pid}/finance-profile",
        json={"finance_profile": PROFILE_PAYLOAD},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    overrides = FinanceOverrides.from_dict(_overrides_payload(profile), profile=profile)
    resp = client.put(
        f"/api/projects/{pid}/finance-overrides",
        json={"finance_overrides": overrides.to_dict(), "expected_revision": 1},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    effective = merge_effective(profile, overrides)
    return profile, overrides, effective


def _save_planning(client: TestClient, user, pid: int, effective_sha: str) -> None:
    resp = client.put(
        f"/api/projects/{pid}/planning-config",
        json={"planning_config": _planning_payload(effective_sha), "expected_revision": None},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text


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


def _build_package(extra_entries: dict[str, bytes], configs_meta: dict) -> bytes:
    """手工构造项目包 zip(含给定 YAML 配置文件与 files.configs 清单, 完整对象清单)。"""
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
    for path, raw in extra_entries.items():
        entries[path] = raw

    objects = [
        {
            "path": path,
            "sha256": sha256_hex(raw),
            "size_bytes": len(raw),
            "media_type": "application/yaml" if path.endswith(".yaml") else "application/json",
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


def _valid_triplet_entries() -> tuple[dict[str, bytes], dict]:
    """合法三件套 YAML 条目 + configs_meta(供负例篡改基底)。"""
    profile = FinanceProfile.from_dict(PROFILE_PAYLOAD)
    overrides = FinanceOverrides.from_dict(_overrides_payload(profile), profile=profile)
    effective = merge_effective(profile, overrides)
    entries = {
        "finance_profile.yaml": yaml_dump(profile.to_dict()).encode("utf-8"),
        "finance_overrides.yaml": yaml_dump(overrides.to_dict()).encode("utf-8"),
        "effective_finance.yaml": yaml_dump(effective.to_dict()).encode("utf-8"),
    }
    configs_meta = {
        "finance_profile": "finance_profile.yaml",
        "finance_overrides": "finance_overrides.yaml",
        "effective_finance": "effective_finance.yaml",
    }
    return entries, configs_meta


# ---------------------------------------------------------------------------
# 导出/导入往返
# ---------------------------------------------------------------------------


def test_export_and_import_roundtrip_with_configs(client: TestClient, db: Session) -> None:
    """有财务三件套+规划配置的项目包: 导出含四 YAML 与清单登记; 导入重建。"""
    owner = make_user(db, "pkg_owner")
    importer = make_user(db, "pkg_importer")
    pid = _create_project(client, owner)
    profile, overrides, effective = _setup_finance(client, owner, pid)
    _save_planning(client, owner, pid, effective.content_sha256)

    zip_bytes = _export_zip(client, owner, pid)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["files"]["configs"] == {
            "finance_profile": "finance_profile.yaml",
            "finance_overrides": "finance_overrides.yaml",
            "effective_finance": "effective_finance.yaml",
            "planning_config": "planning_config.yaml",
        }
        names = set(zf.namelist())
        assert {
            "finance_profile.yaml",
            "finance_overrides.yaml",
            "effective_finance.yaml",
            "planning_config.yaml",
        } <= names
        # 逐对象校验值一致
        for entry in manifest["objects"]:
            raw = zf.read(entry["path"])
            assert len(raw) == entry["size_bytes"]
            assert sha256_hex(raw) == entry["sha256"]
        # 文件内容摘要与契约一致
        from iesplan.core.yamlmini import load as yaml_load

        p = FinanceProfile.from_dict(yaml_load(zf.read("finance_profile.yaml").decode("utf-8")))
        assert p.content_sha256 == profile.content_sha256
        o = FinanceOverrides.from_dict(
            yaml_load(zf.read("finance_overrides.yaml").decode("utf-8")), profile=p
        )
        assert o.content_sha256 == overrides.content_sha256
        e = EffectiveFinanceConfig.from_dict(
            yaml_load(zf.read("effective_finance.yaml").decode("utf-8"))
        )
        assert e.content_sha256 == effective.content_sha256

    proposal = package_service.import_proposal(db, importer, zip_bytes)
    summary_configs = proposal.review_summary["configs"]
    assert summary_configs["finance_triplet"]["present"] is True
    assert summary_configs["finance_triplet"]["profile_sha256"] == profile.content_sha256
    assert summary_configs["finance_triplet"]["overrides_sha256"] == overrides.content_sha256
    assert summary_configs["finance_triplet"]["content_sha256"] == effective.content_sha256
    new_project = package_service.confirm_import(db, importer, proposal.id)
    db.commit()

    # 新项目身份, 配置重建: Effective 血缘一致, 规划引用一致
    assert new_project.id != pid
    resp = client.get(
        f"/api/projects/{new_project.id}/effective-finance", headers=_h(client, importer)
    )
    assert resp.status_code == 200
    assert resp.json()["effective_finance_config"]["content_sha256"] == effective.content_sha256
    resp = client.get(
        f"/api/projects/{new_project.id}/planning-config", headers=_h(client, importer)
    )
    assert resp.status_code == 200
    assert resp.json()["planning_config"]["finance_content_sha256"] == effective.content_sha256
    assert summary_configs["planning"]["present"] is True


def test_export_without_configs(client: TestClient, db: Session) -> None:
    """无配置项目: 包不含 YAML; 导入后无配置(不静默默认)。"""
    owner = make_user(db, "none_owner")
    importer = make_user(db, "none_importer")
    pid = _create_project(client, owner)

    zip_bytes = _export_zip(client, owner, pid)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["files"]["configs"] == {}
        assert "finance_profile.yaml" not in zf.namelist()

    proposal = package_service.import_proposal(db, importer, zip_bytes)
    assert proposal.review_summary["configs"]["finance_triplet"]["present"] is False
    new_project = package_service.confirm_import(db, importer, proposal.id)
    db.commit()
    resp = client.get(
        f"/api/projects/{new_project.id}/effective-finance", headers=_h(client, importer)
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 导入校验拒绝(严格校验, 无静默默认)
# ---------------------------------------------------------------------------


def test_import_rejects_incomplete_triplet(client: TestClient, db: Session) -> None:
    """三件套缺一(有效快照缺失) → 拒绝。"""
    importer = make_user(db, "rej_imp_1")
    entries, configs_meta = _valid_triplet_entries()
    del entries["effective_finance.yaml"]
    configs_meta = {k: v for k, v in configs_meta.items() if k != "effective_finance"}
    zip_bytes = _build_package(entries, configs_meta)
    with pytest.raises(package_service.ImportValidationError) as excinfo:
        package_service.import_proposal(db, importer, zip_bytes)
    assert any("effective_finance" in r for r in excinfo.value.reasons)


def test_import_rejects_tampered_profile_sha(client: TestClient, db: Session) -> None:
    """Profile 声明 content_sha256 被篡改 → 拒绝。"""
    importer = make_user(db, "rej_imp_2")
    entries, configs_meta = _valid_triplet_entries()
    profile = FinanceProfile.from_dict(PROFILE_PAYLOAD)
    bad = {**profile.to_dict(), "content_sha256": "0" * 64}
    entries["finance_profile.yaml"] = yaml_dump(bad).encode("utf-8")
    zip_bytes = _build_package(entries, configs_meta)
    with pytest.raises(package_service.ImportValidationError) as excinfo:
        package_service.import_proposal(db, importer, zip_bytes)
    assert any("content_sha256" in r for r in excinfo.value.reasons)


def test_import_rejects_effective_remerge_mismatch(client: TestClient, db: Session) -> None:
    """Effective 与 Profile+Overrides 重新合并不一致(篡改 Effective 叶子) → 拒绝。"""
    importer = make_user(db, "rej_imp_3")
    entries, configs_meta = _valid_triplet_entries()
    from iesplan.core.yamlmini import load as yaml_load

    effective_doc = yaml_load(entries["effective_finance.yaml"].decode("utf-8"))
    # 篡改被覆盖叶子金额(与 Overrides 的 1500 不一致), 但保留声明 content_sha256
    effective_doc["finance_types"]["pv_system"]["upfront_capex"]["fixed"]["value"] = "9999"
    entries["effective_finance.yaml"] = yaml_dump(effective_doc).encode("utf-8")
    zip_bytes = _build_package(entries, configs_meta)
    with pytest.raises(package_service.ImportValidationError) as excinfo:
        package_service.import_proposal(db, importer, zip_bytes)
    # 篡改叶子 → 自身摘要重算不一致或与来源重新合并不一致, 两者都拒绝
    assert any(
        "Effective" in r and ("不一致" in r or "重新合并" in r)
        for r in excinfo.value.reasons
    )


def test_import_rejects_effective_remerge_bloodline(client: TestClient, db: Session) -> None:
    """篡改 Effective 叶子并同步声明摘要: from_dict 通过, 但来源重合并血缘拒绝。"""
    importer = make_user(db, "rej_imp_6")
    entries, configs_meta = _valid_triplet_entries()
    from iesplan.core.yamlmini import load as yaml_load

    effective_doc = yaml_load(entries["effective_finance.yaml"].decode("utf-8"))
    effective_doc["finance_types"]["pv_system"]["upfront_capex"]["fixed"]["value"] = "9999"
    # 移除声明摘要后重建(重算新摘要), 使结构校验通过
    effective_doc.pop("content_sha256", None)
    tampered = EffectiveFinanceConfig.from_dict(effective_doc)
    entries["effective_finance.yaml"] = yaml_dump(tampered.to_dict()).encode("utf-8")
    zip_bytes = _build_package(entries, configs_meta)
    with pytest.raises(package_service.ImportValidationError) as excinfo:
        package_service.import_proposal(db, importer, zip_bytes)
    assert any("重新合并" in r for r in excinfo.value.reasons)


def test_import_rejects_planning_finance_mismatch(client: TestClient, db: Session) -> None:
    """规划引用的 Effective content_sha256 与包内有效快照不一致 → 拒绝。"""
    importer = make_user(db, "rej_imp_4")
    entries, configs_meta = _valid_triplet_entries()
    planning = PlanningConfig.from_dict(_planning_payload("0" * 64))
    entries["planning_config.yaml"] = yaml_dump(planning.to_dict()).encode("utf-8")
    configs_meta = {**configs_meta, "planning_config": "planning_config.yaml"}
    zip_bytes = _build_package(entries, configs_meta)
    with pytest.raises(package_service.ImportValidationError) as excinfo:
        package_service.import_proposal(db, importer, zip_bytes)
    assert any("finance_content_sha256" in r or "不一致" in r for r in excinfo.value.reasons)


def test_import_rejects_override_scope_violation(client: TestClient, db: Session) -> None:
    """包内 Overrides 越权(新增 finance_type) → 拒绝。"""
    importer = make_user(db, "rej_imp_5")
    entries, configs_meta = _valid_triplet_entries()
    from iesplan.core.yamlmini import load as yaml_load

    overrides_doc = yaml_load(entries["finance_overrides.yaml"].decode("utf-8"))
    overrides_doc["finance_types"]["new_tech"] = {
        "upfront_capex": {"fixed": {"value": "1", "unit": "CNY"}}
    }
    entries["finance_overrides.yaml"] = yaml_dump(overrides_doc).encode("utf-8")
    zip_bytes = _build_package(entries, configs_meta)
    with pytest.raises(package_service.ImportValidationError) as excinfo:
        package_service.import_proposal(db, importer, zip_bytes)
    assert any("FinanceOverrides" in r for r in excinfo.value.reasons)
