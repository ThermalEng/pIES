"""项目包携带财务三件套/规划配置 YAML(0.6.5 条目 1-2)导出/导入集成测试。

覆盖:
- 导出: 项目有配置 → 包内含 finance_profile.yaml / finance_overrides.yaml /
  effective_finance.yaml / planning_config.yaml(对象清单逐对象校验值一致,
  manifest.files.configs 登记四键); 无配置 → 不含;
- 导入: 三件套随包重建(revision 由服务层生成), 消费上传边界已接受内容发布;
  规划引用 Effective content 一致; 无配置包导入后无配置(不静默默认);
- 校验拒绝(ImportValidationError, PKG-IMP-001): 三件套缺一 / 对象字节
  校验值被篡改(外部包入口边界, 2.6) / 规划引用与包内有效快照不一致 / 越权覆盖。

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

from iesplan import package as package_domain  # noqa: E402
from iesplan.api import config_revisions as config_api  # noqa: E402
from iesplan.api import exports as exports_api  # noqa: E402
from iesplan.api import projects as projects_api  # noqa: E402
from iesplan.api.deps import get_request_db  # noqa: E402
from iesplan.application.packages import operations as packages_uc  # noqa: E402
from iesplan.config import settings  # noqa: E402
from iesplan.core.contracts import ProjectBaseline  # noqa: E402
from iesplan.core.yamlmini import dump as yaml_dump  # noqa: E402
from iesplan.dataset.persistence import DatasetFile  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.identity.persistence import User  # noqa: E402  # 注册 users 表
# 独立建表需要注册 evidence_packages.task_id 的目标，不依赖其他测试先导入。
from iesplan.tasks.persistence import Task as _TaskForDDL  # noqa: E402,F401
from iesplan.storage import get_object  # noqa: E402
from sqlalchemy import select  # noqa: E402
from test_dataset_api import make_csv  # noqa: E402
from iesplan.finance import (  # noqa: E402
    EffectiveFinanceConfig,
    FinanceOverrides,
    FinanceProfile,
    merge_effective,
)
from iesplan.main import create_app  # noqa: E402

# ---------------------------------------------------------------------------
# 样例(与 test_config_revisions 同源)
# ---------------------------------------------------------------------------

PROFILE_PAYLOAD: dict = {
    "schema": "ies.finance-profile",
    "schema_version": "4.0.0",
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
    "demand_charges": {},
    "taxes": {},
    "credit_pools": {},
    "tax_rules": {},
}


def _overrides_payload(profile: FinanceProfile) -> dict:
    return {
        "schema": "ies.finance-overrides",
        "schema_version": "4.0.0",
        "profile_ref": {"id": profile.profile_id},
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


def _planning_payload() -> dict:
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

    def _override_request_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_request_db] = _override_request_db
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
            "baseline_start_year": 2026,
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
        json={"profile_ref": {"id": profile.profile_id}},
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


def _save_planning(client: TestClient, user, pid: int) -> None:
    resp = client.put(
        f"/api/projects/{pid}/planning-config",
        json={"planning_config": _planning_payload(), "expected_revision": None},
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
    """手工构造项目包 zip(含给定 YAML 配置文件与 files.configs 清单, 对象清单不做内部摘要校验)。"""
    entries: dict[str, bytes] = {
        "project.json": json.dumps(
            {
                "name": "定制包项目",
                "currency": "CNY",
                "project_baseline": ProjectBaseline(
                    resolution="1h", start_year=2026
                ).to_dict(),
            },
            ensure_ascii=False,
        ).encode(),
        "draft.json": json.dumps(
            {"revision": 1, "content": {"model": {"devices": [], "connections": []}}}
        ).encode(),
    }
    for path, raw in extra_entries.items():
        entries[path] = raw

    objects = [
        {
            "path": path,
            "size_bytes": len(raw),
            "media_type": "application/yaml" if path.endswith(".yaml") else "application/json",
        }
        for path, raw in sorted(entries.items())
    ]
    manifest = {
        "format_version": "3.0.0",
        "package_type": "project",
        "project": {
            "name": "定制包项目",
            "currency": "CNY",
            "project_baseline": ProjectBaseline(
                resolution="1h", start_year=2026
            ).to_dict(),
        },
        "files": {"configs": configs_meta},
        "objects": objects,
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
    _save_planning(client, owner, pid)

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
        # 仅校验文件存在，不做内部哈希重算比对
        for entry in manifest["objects"]:
            assert entry["path"] in zf.namelist()
        # 文件内容与契约一致
        from iesplan.core.yamlmini import load as yaml_load

        p = FinanceProfile.from_dict(yaml_load(zf.read("finance_profile.yaml").decode("utf-8")))
        assert p.profile_id == profile.profile_id
        o = FinanceOverrides.from_dict(
            yaml_load(zf.read("finance_overrides.yaml").decode("utf-8")), profile=p
        )
        assert o.profile_ref["id"] == profile.profile_id
        e = EffectiveFinanceConfig.from_dict(
            yaml_load(zf.read("effective_finance.yaml").decode("utf-8"))
        )
        assert e.profile_id == profile.profile_id

    proposal = packages_uc.propose_import(db, importer, zip_bytes)
    summary_configs = proposal.review_summary["configs"]
    assert summary_configs["finance_triplet"]["present"] is True
    assert summary_configs["finance_triplet"]["profile_id"] == profile.profile_id
    assert summary_configs["finance_triplet"]["present"] is True
    new_project = packages_uc.confirm_import(db, importer, proposal.id)
    db.commit()

    # 新项目身份, 配置重建: Effective 血缘一致, 规划引用一致
    assert new_project.id != pid
    resp = client.get(
        f"/api/projects/{new_project.id}/effective-finance", headers=_h(client, importer)
    )
    assert resp.status_code == 200
    assert resp.json()["effective_finance_config"]["profile_id"] == profile.profile_id
    resp = client.get(
        f"/api/projects/{new_project.id}/planning-config", headers=_h(client, importer)
    )
    assert resp.status_code == 200
    assert "planning_config" in resp.json()
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

    proposal = packages_uc.propose_import(db, importer, zip_bytes)
    assert proposal.review_summary["configs"]["finance_triplet"]["present"] is False
    new_project = packages_uc.confirm_import(db, importer, proposal.id)
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
    with pytest.raises(package_domain.ImportValidationError) as excinfo:
        packages_uc.propose_import(db, importer, zip_bytes)
    assert any("effective_finance" in r for r in excinfo.value.reasons)



def test_import_rejects_planning_domain_violation(client: TestClient, db: Session) -> None:
    """包内规划配置领域校验失败 → 拒绝(设备引用格式非法)。"""
    importer = make_user(db, "rej_imp_4")
    entries, configs_meta = _valid_triplet_entries()
    bad_planning = {
        **_planning_payload(),
        "variables": {
            "hp_cap": {**_planning_payload()["variables"]["hp_cap"], "device_ref": "非法引用!"},
        },
    }
    entries["planning_config.yaml"] = yaml_dump(bad_planning).encode("utf-8")
    configs_meta = {**configs_meta, "planning_config": "planning_config.yaml"}
    zip_bytes = _build_package(entries, configs_meta)
    with pytest.raises(package_domain.ImportValidationError) as excinfo:
        packages_uc.propose_import(db, importer, zip_bytes)
    assert any("领域校验失败" in r for r in excinfo.value.reasons)


def _overrides_payload_with_price(profile: FinanceProfile, price: str) -> dict:
    payload = _overrides_payload(profile)
    payload["energy_prices"]["grid_import"]["value"]["value"] = price
    return payload


def _planning_payload_with_bound(upper_bound: str) -> dict:
    payload = _planning_payload()
    payload["variables"]["hp_cap"]["upper_bound"] = upper_bound
    return payload


def _put_overrides(client: TestClient, user, pid: int, profile: FinanceProfile, price: str, expected_revision) -> None:
    if expected_revision is None:
        current = client.get(f"/api/projects/{pid}/finance-overrides", headers=_h(client, user)).json()
        expected_revision = current["revision"]
    overrides = FinanceOverrides.from_dict(_overrides_payload_with_price(profile, price), profile=profile)
    resp = client.put(
        f"/api/projects/{pid}/finance-overrides",
        json={"finance_overrides": overrides.to_dict(), "expected_revision": expected_revision},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text


def _put_planning(client: TestClient, user, pid: int, payload: dict, expected_revision) -> None:
    resp = client.put(
        f"/api/projects/{pid}/planning-config",
        json={"planning_config": payload, "expected_revision": expected_revision},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text


def _create_dataset(client: TestClient, user, pid: int, name: str = "负荷数据") -> int:
    resp = client.post(
        f"/api/projects/{pid}/datasets",
        json={"name": name, "source_category": "user_upload", "license": "CC-BY-4.0"},
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["dataset"]["id"]


def _upload_version(client: TestClient, user, pid: int, dsid: int, csv_bytes: bytes) -> int:
    resp = client.post(
        f"/api/projects/{pid}/datasets/{dsid}/versions",
        data={"resolution": "1h", "utc_offset_minutes": "480"},
        files={"file": ("data.csv", csv_bytes, "text/csv")},
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["dataset_version"]["id"]


def _bind_version(client: TestClient, user, pid: int, dvid: int, expected_revision: int, tag: str) -> int:
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={
            "expected_revision": expected_revision,
            "commands": [{
                "id": f"bind-{tag}", "unit": "dataset", "type": "dataset.bind",
                "payload": {"dataset_version_id": dvid, "role": "main"},
            }],
        },
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def _create_hist_version(client: TestClient, user, pid: int, name: str) -> int:
    resp = client.post(
        f"/api/projects/{pid}/versions",
        json={"name": name, "reason": "manual_save"},
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["version"]["id"]


def _first_e_load(csv_text: str) -> float:
    lines = csv_text.strip().split("\n")
    idx = lines[0].split(",").index("e_load")
    return float(lines[1].split(",")[idx])


def _bound_e_loads(client: TestClient, db: Session, user, pid: int) -> tuple[list[int], list[float]]:
    """当前草稿绑定版本 id 集合与其首行 e_load 值集合(公开视图 + 对象读取)。"""
    view = client.get(f"/api/projects/{pid}", headers=_h(client, user)).json()
    bindings = view["draft"]["content"].get("dataset_bindings") or []
    ids: list[int] = []
    loads: list[float] = []
    for binding in bindings:
        dvid = binding["dataset_version_id"]
        row = db.execute(
            select(DatasetFile).where(DatasetFile.dataset_version_id == dvid)
        ).scalars().first()
        assert row is not None
        ids.append(dvid)
        loads.append(_first_e_load(get_object(db, row.object_id).decode("utf-8")))
    return ids, sorted(loads)


def _import_new_project(client: TestClient, user, zip_bytes: bytes) -> tuple[int, int]:
    """公开上传提案 → 公开确认, 返回 (新项目 id, 提案 id)。"""
    resp = client.post(
        "/api/projects/import",
        files={"file": ("pkg.zip", zip_bytes, "application/zip")},
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text
    prop_id = resp.json()["proposal"]["id"]
    resp = client.post(f"/api/projects/import/{prop_id}/confirm", headers=_h(client, user))
    assert resp.status_code == 201, resp.text
    assert resp.json()["project"]["id"] is not None
    assert resp.json()["my_role"] == "owner"
    return resp.json()["project"]["id"], prop_id


def test_imported_history_versions_restore_original_deps(client: TestClient, db: Session) -> None:
    """导入后历史版本可恢复出原业务依赖(财务/规划值与数据版本), 源项目不变。

    公开流程: 两组覆盖/规划分别固化版本 + 同一数据集两个真实版本分别绑定
    固化 → 公开导出/下载 → 公开上传提案 → 公开确认 → 新项目上恢复历史版本。
    """
    owner = make_user(db, "hist_owner")
    importer = make_user(db, "hist_importer")
    pid = _create_project(client, owner)
    profile, _, _ = _setup_finance(client, owner, pid)  # 覆盖 rev2: 电价 0.75
    _save_planning(client, owner, pid)  # 规划 rev1: 上界 1000
    dsid = _create_dataset(client, owner, pid)
    dvid1 = _upload_version(client, owner, pid, dsid, make_csv("1h", n=8760))
    rev = _bind_version(client, owner, pid, dvid1, 1, "a")
    _create_hist_version(client, owner, pid, "hist-a")
    _put_overrides(client, owner, pid, profile, "0.95", 2)  # 覆盖 rev3(规划指针随之清空)
    _put_planning(client, owner, pid, _planning_payload_with_bound("2000"), None)  # 规划 rev2
    csv_b = make_csv("1h", n=8760, transform=lambda row, i: {**row, "e_load": row["e_load"] + 1000.0})
    dvid2 = _upload_version(client, owner, pid, dsid, csv_b)
    _bind_version(client, owner, pid, dvid2, rev, "b")
    _create_hist_version(client, owner, pid, "hist-b")

    zip_bytes = _export_zip(client, owner, pid)
    new_pid, _ = _import_new_project(client, importer, zip_bytes)
    assert new_pid != pid

    # 导入后当前工作状态保持导出时实际选择(覆盖 0.95/规划 2000/绑定 B), 不隐式切换
    ovr = client.get(f"/api/projects/{new_pid}/finance-overrides", headers=_h(client, importer)).json()
    assert ovr["finance_overrides"]["energy_prices"]["grid_import"]["value"]["value"] == "0.95"
    plan = client.get(f"/api/projects/{new_pid}/planning-config", headers=_h(client, importer)).json()
    assert plan["planning_config"]["variables"]["hp_cap"]["upper_bound"] == "2000"
    ids, loads = _bound_e_loads(client, db, importer, new_pid)
    assert all(i not in (dvid1, dvid2) for i in ids)
    assert loads == [100.0, 1100.0]

    listed = client.get(f"/api/projects/{new_pid}/versions", headers=_h(client, importer)).json()["versions"]
    assert len(listed) == 2
    by_name = {v["name"]: v for v in listed}

    # 恢复 hist-a: 原覆盖值/规划值/数据版本 A
    resp = client.post(
        f"/api/projects/{new_pid}/versions/{by_name['hist-a']['id']}/restore",
        headers=_h(client, importer),
    )
    assert resp.status_code == 200, resp.text
    ovr = client.get(f"/api/projects/{new_pid}/finance-overrides", headers=_h(client, importer)).json()
    assert ovr["finance_overrides"]["energy_prices"]["grid_import"]["value"]["value"] == "0.75"
    plan = client.get(f"/api/projects/{new_pid}/planning-config", headers=_h(client, importer)).json()
    assert plan["planning_config"]["variables"]["hp_cap"]["upper_bound"] == "1000"
    ids, loads = _bound_e_loads(client, db, importer, new_pid)
    assert all(i not in (dvid1, dvid2) for i in ids)  # 重映射到新项目身份, 不沿用原内部 id
    assert loads == [100.0]

    # 恢复 hist-b: 原覆盖值/规划值/数据版本 B(最新不覆盖旧值; 绑定追加语义, 两 vintage 共存)
    resp = client.post(
        f"/api/projects/{new_pid}/versions/{by_name['hist-b']['id']}/restore",
        headers=_h(client, importer),
    )
    assert resp.status_code == 200, resp.text
    ovr = client.get(f"/api/projects/{new_pid}/finance-overrides", headers=_h(client, importer)).json()
    assert ovr["finance_overrides"]["energy_prices"]["grid_import"]["value"]["value"] == "0.95"
    plan = client.get(f"/api/projects/{new_pid}/planning-config", headers=_h(client, importer)).json()
    assert plan["planning_config"]["variables"]["hp_cap"]["upper_bound"] == "2000"
    ids, loads = _bound_e_loads(client, db, importer, new_pid)
    assert all(i not in (dvid1, dvid2) for i in ids)
    assert loads == [100.0, 1100.0]

    # 源项目不变: 版本数、当前配置值、当前绑定
    src_versions = client.get(f"/api/projects/{pid}/versions", headers=_h(client, owner)).json()["versions"]
    assert len(src_versions) == 2
    src_ovr = client.get(f"/api/projects/{pid}/finance-overrides", headers=_h(client, owner)).json()
    assert src_ovr["finance_overrides"]["energy_prices"]["grid_import"]["value"]["value"] == "0.95"
    src_ids, src_loads = _bound_e_loads(client, db, owner, pid)
    assert sorted(src_ids) == sorted([dvid1, dvid2])
    assert src_loads == [100.0, 1100.0]


def test_imported_restore_resolves_version_scoped_config(client: TestClient, db: Session) -> None:
    """导入后恢复历史版本: 按来源映射解析到重建的历史行, 取原业务值。

    源 hist-a 冻结覆盖 rev2(电价 0.75)/规划 rev1(上界 1000); 新项目确认重建
    恰好也有同号当前行(值 0.95/2000), 恢复必须取历史行原值, 不得撞车。
    """
    owner = make_user(db, "silent_owner")
    importer = make_user(db, "silent_importer")
    pid = _create_project(client, owner)
    profile, _, _ = _setup_finance(client, owner, pid)  # 覆盖 rev2: 电价 0.75
    _save_planning(client, owner, pid)  # 规划 rev1: 上界 1000
    _create_hist_version(client, owner, pid, "silent-a")
    _put_overrides(client, owner, pid, profile, "0.95", 2)  # 覆盖 rev3
    _put_planning(client, owner, pid, _planning_payload_with_bound("2000"), None)  # 规划 rev2

    zip_bytes = _export_zip(client, owner, pid)
    new_pid, _ = _import_new_project(client, importer, zip_bytes)
    listed = client.get(f"/api/projects/{new_pid}/versions", headers=_h(client, importer)).json()["versions"]
    assert len(listed) == 1
    resp = client.post(
        f"/api/projects/{new_pid}/versions/{listed[0]['id']}/restore",
        headers=_h(client, importer),
    )
    assert resp.status_code == 200, resp.text
    ovr = client.get(f"/api/projects/{new_pid}/finance-overrides", headers=_h(client, importer)).json()
    assert ovr["finance_overrides"]["energy_prices"]["grid_import"]["value"]["value"] == "0.75"
    plan = client.get(f"/api/projects/{new_pid}/planning-config", headers=_h(client, importer)).json()
    assert plan["planning_config"]["variables"]["hp_cap"]["upper_bound"] == "1000"


def test_imported_restore_recovers_history_planning_despite_absent_current(client: TestClient, db: Session) -> None:
    """导出时当前规划指针已被清空: 包内仍带历史规划依赖, 恢复成功取历史值,
    当前规划缺席真实保持缺席(404), 不回填。"""
    owner = make_user(db, "omit_owner")
    importer = make_user(db, "omit_importer")
    pid = _create_project(client, owner)
    profile, _, _ = _setup_finance(client, owner, pid)
    _save_planning(client, owner, pid)  # 规划 rev1: 上界 1000
    _create_hist_version(client, owner, pid, "omit-a")
    _put_overrides(client, owner, pid, profile, "0.95", 2)  # 规划指针清空, 不重存

    zip_bytes = _export_zip(client, owner, pid)
    new_pid, _ = _import_new_project(client, importer, zip_bytes)
    # 当前规划缺席(包内无当前规划可重建)
    resp = client.get(f"/api/projects/{new_pid}/planning-config", headers=_h(client, importer))
    assert resp.status_code == 404, resp.text
    listed = client.get(f"/api/projects/{new_pid}/versions", headers=_h(client, importer)).json()["versions"]
    assert len(listed) == 1
    resp = client.post(
        f"/api/projects/{new_pid}/versions/{listed[0]['id']}/restore",
        headers=_h(client, importer),
    )
    assert resp.status_code == 200, resp.text
    plan = client.get(f"/api/projects/{new_pid}/planning-config", headers=_h(client, importer)).json()
    assert plan["planning_config"]["variables"]["hp_cap"]["upper_bound"] == "1000"
    ovr = client.get(f"/api/projects/{new_pid}/finance-overrides", headers=_h(client, importer)).json()
    assert ovr["finance_overrides"]["energy_prices"]["grid_import"]["value"]["value"] == "0.75"


def _rezip_with_manifest(zip_bytes: bytes, mutate) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            raw = zin.read(info.filename)
            if info.filename == "manifest.json":
                doc = json.loads(raw.decode("utf-8"))
                mutate(doc)
                raw = json.dumps(doc, ensure_ascii=False).encode()
            zout.writestr(info.filename, raw)
    return buf.getvalue()


def test_import_rejects_legacy_major_version(client: TestClient, db: Session) -> None:
    """旧主版本 1.0 包 → 400 拒绝, reasons 非空指明期望 3.x, 不创建记录。"""
    importer = make_user(db, "legacy_imp")
    entries, configs_meta = _valid_triplet_entries()
    legacy = _rezip_with_manifest(
        _build_package(entries, configs_meta),
        lambda doc: doc.update({"format_version": "1.0"}),
    )
    resp = client.post(
        "/api/projects/import",
        files={"file": ("pkg.zip", legacy, "application/zip")},
        headers=_h(client, importer),
    )
    assert resp.status_code == 400, resp.text
    reasons = resp.json()["error"]["params"]["reasons"]
    assert isinstance(reasons, list) and reasons
    assert any("3.x" in r for r in reasons)


def test_import_rejects_version_without_mapped_deps(client: TestClient, db: Session) -> None:
    """带配置引用的版本缺 version_configs 条目 → 400, reasons 指明版本。"""
    importer = make_user(db, "nodep_imp")
    entries, configs_meta = _valid_triplet_entries()
    entries["versions/0001.json"] = json.dumps({
        "version": {"version_no": 1, "name": "v1", "project_baseline": ProjectBaseline(
            resolution="1h", start_year=2026).to_dict()},
        "content": {
            "effective_finance": {"profile_id": "p", "overrides_revision": 1, "revision": 1},
            "planning_config": {"revision": 1},
        },
    }, ensure_ascii=False).encode()
    configs_meta = dict(configs_meta)
    manifest_files = {"configs": configs_meta, "versions": ["versions/0001.json"]}
    zip_bytes = _build_package(entries, configs_meta)
    zip_bytes = _rezip_with_manifest(
        zip_bytes,
        lambda doc: doc["files"].update(manifest_files),
    )
    resp = client.post(
        "/api/projects/import",
        files={"file": ("pkg.zip", zip_bytes, "application/zip")},
        headers=_h(client, importer),
    )
    assert resp.status_code == 400, resp.text
    reasons = resp.json()["error"]["params"]["reasons"]
    assert any("0001" in r for r in reasons)


PROFILE_B_PAYLOAD: dict = {
    **PROFILE_PAYLOAD,
    "profile": {
        **PROFILE_PAYLOAD["profile"],
        "id": "cn-east-demo",
        "region": "CN-East",
    },
}


def _register_profile(client: TestClient, user, payload: dict) -> None:
    resp = client.post("/api/finance-profiles", json={"finance_profile": payload}, headers=_h(client, user))
    assert resp.status_code == 200, resp.text


def _set_profile(client: TestClient, user, pid: int, profile_id: str) -> None:
    resp = client.put(
        f"/api/projects/{pid}/finance-profile",
        json={"profile_ref": {"id": profile_id}},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text


def _isolated_target(tmp_path_factory):
    """第二套隔离应用/DB(空注册表), 同一 Docker 测试环境内。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    eng = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    settings.data_dir = tmp_path_factory.mktemp("target")
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    session = factory()
    app = create_app()
    app.include_router(projects_api.router)
    app.include_router(config_api.router)
    app.include_router(config_api.profile_router)
    app.include_router(exports_api.router)

    def _override_request_db2():
        yield session

    app.dependency_overrides[get_request_db] = _override_request_db2
    return TestClient(app, raise_server_exceptions=False), session, eng


def test_imported_history_resolves_distinct_profiles_cross_db(
    client: TestClient, db: Session, tmp_path_factory
) -> None:
    """跨库往返: 历史与当前引用不同 Profile, 目标空注册表也能成功并各取原值。"""
    owner = make_user(db, "xdb_owner")
    pid = _create_project(client, owner)
    _register_profile(client, owner, PROFILE_PAYLOAD)
    _set_profile(client, owner, pid, "cn-north-demo")
    profile_a = FinanceProfile.from_dict(PROFILE_PAYLOAD)
    _put_overrides(client, owner, pid, profile_a, "0.75", 1)
    _save_planning(client, owner, pid)
    _create_hist_version(client, owner, pid, "xdb-a")
    _register_profile(client, owner, PROFILE_B_PAYLOAD)
    _set_profile(client, owner, pid, "cn-east-demo")
    profile_b = FinanceProfile.from_dict(PROFILE_B_PAYLOAD)
    _put_overrides(client, owner, pid, profile_b, "0.95", None)
    _put_planning(client, owner, pid, _planning_payload_with_bound("2000"), None)
    _create_hist_version(client, owner, pid, "xdb-b")

    zip_bytes = _export_zip(client, owner, pid)

    prev_data_dir = settings.data_dir
    client2, db2, eng2 = _isolated_target(tmp_path_factory)
    try:
        importer = make_user(db2, "xdb_importer")
        resp = client2.post(
            "/api/projects/import",
            files={"file": ("pkg.zip", zip_bytes, "application/zip")},
            headers=_h(client2, importer),
        )
        assert resp.status_code == 201, resp.text
        prop_id = resp.json()["proposal"]["id"]
        resp = client2.post(f"/api/projects/import/{prop_id}/confirm", headers=_h(client2, importer))
        assert resp.status_code == 201, resp.text
        new_pid = resp.json()["project"]["id"]

        listed = client2.get(f"/api/projects/{new_pid}/versions", headers=_h(client2, importer)).json()["versions"]
        assert len(listed) == 2
        by_name = {v["name"]: v for v in listed}
        resp = client2.post(
            f"/api/projects/{new_pid}/versions/{by_name['xdb-a']['id']}/restore",
            headers=_h(client2, importer),
        )
        assert resp.status_code == 200, resp.text
        eff = client2.get(f"/api/projects/{new_pid}/effective-finance", headers=_h(client2, importer)).json()
        assert eff["effective_finance_config"]["profile_id"] == "cn-north-demo"
        ovr = client2.get(f"/api/projects/{new_pid}/finance-overrides", headers=_h(client2, importer)).json()
        assert ovr["finance_overrides"]["energy_prices"]["grid_import"]["value"]["value"] == "0.75"
        resp = client2.post(
            f"/api/projects/{new_pid}/versions/{by_name['xdb-b']['id']}/restore",
            headers=_h(client2, importer),
        )
        assert resp.status_code == 200, resp.text
        eff = client2.get(f"/api/projects/{new_pid}/effective-finance", headers=_h(client2, importer)).json()
        assert eff["effective_finance_config"]["profile_id"] == "cn-east-demo"
        ovr = client2.get(f"/api/projects/{new_pid}/finance-overrides", headers=_h(client2, importer)).json()
        assert ovr["finance_overrides"]["energy_prices"]["grid_import"]["value"]["value"] == "0.95"
    finally:
        settings.data_dir = prev_data_dir
        eng2.dispose()


def test_import_refuses_conflicting_profile_identity(
    client: TestClient, db: Session, tmp_path_factory
) -> None:
    """目标库同稳定 id 已有不同内容 → 确认 400 身份冲突, 不偷偷沿用。"""
    owner = make_user(db, "conf_owner")
    pid = _create_project(client, owner)
    profile, _, _ = _setup_finance(client, owner, pid)
    _save_planning(client, owner, pid)
    _create_hist_version(client, owner, pid, "conf-a")
    zip_bytes = _export_zip(client, owner, pid)

    prev_data_dir = settings.data_dir
    client2, db2, eng2 = _isolated_target(tmp_path_factory)
    try:
        importer = make_user(db2, "conf_importer")
        other = dict(PROFILE_PAYLOAD)
        other = {**other, "profile": {**other["profile"], "region": "CN-Conflict"}}
        _register_profile(client2, importer, other)
        resp = client2.post(
            "/api/projects/import",
            files={"file": ("pkg.zip", zip_bytes, "application/zip")},
            headers=_h(client2, importer),
        )
        assert resp.status_code == 201, resp.text
        prop_id = resp.json()["proposal"]["id"]
        resp = client2.post(f"/api/projects/import/{prop_id}/confirm", headers=_h(client2, importer))
        assert resp.status_code == 400, resp.text
        reasons = resp.json()["error"]["params"]["reasons"]
        assert any("身份冲突" in r for r in reasons)
    finally:
        settings.data_dir = prev_data_dir
        eng2.dispose()


def _rezip_mutate(zip_bytes: bytes, mutations: dict[str, bytes]) -> bytes:
    """替换包内条目并同步对象清单大小(非法输入变异用, 保持包体自洽)。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            raw = mutations.get(info.filename, zin.read(info.filename))
            if info.filename == "manifest.json":
                doc = json.loads(raw.decode("utf-8"))
                for entry in doc.get("objects", []):
                    if entry.get("path") in mutations:
                        entry["size_bytes"] = len(mutations[entry["path"]])
                raw = json.dumps(doc, ensure_ascii=False).encode()
            zout.writestr(info.filename, raw)
    return buf.getvalue()


def test_import_rejects_illegal_history_overrides(client: TestClient, db: Session) -> None:
    """历史 Overrides 非法字头 → 上传 400, reasons 指明版本与原因。"""
    owner = make_user(db, "ill_owner")
    importer = make_user(db, "ill_importer")
    pid = _create_project(client, owner)
    profile, _, _ = _setup_finance(client, owner, pid)
    _save_planning(client, owner, pid)
    _create_hist_version(client, owner, pid, "ill-a")
    zip_bytes = _export_zip(client, owner, pid)
    bad = _rezip_mutate(zip_bytes, {
        "version_configs/0001/finance_overrides.yaml": b"not: [valid yaml",
    })
    resp = client.post(
        "/api/projects/import",
        files={"file": ("pkg.zip", bad, "application/zip")},
        headers=_h(client, importer),
    )
    assert resp.status_code == 400, resp.text
    reasons = resp.json()["error"]["params"]["reasons"]
    assert any("0001" in r for r in reasons)


def test_import_rejects_mismatched_history_baseline(client: TestClient, db: Session) -> None:
    """历史版本基线与项目基线不一致 → 上传 400, 不等确认才暴露。"""
    owner = make_user(db, "base_owner")
    importer = make_user(db, "base_importer")
    pid = _create_project(client, owner)
    profile, _, _ = _setup_finance(client, owner, pid)
    _save_planning(client, owner, pid)
    _create_hist_version(client, owner, pid, "base-a")
    zip_bytes = _export_zip(client, owner, pid)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        doc = json.loads(zf.read("versions/0001.json").decode("utf-8"))
    doc["version"]["project_baseline"]["start_year"] = 2099
    bad = _rezip_mutate(zip_bytes, {
        "versions/0001.json": json.dumps(doc, ensure_ascii=False).encode(),
    })
    resp = client.post(
        "/api/projects/import",
        files={"file": ("pkg.zip", bad, "application/zip")},
        headers=_h(client, importer),
    )
    assert resp.status_code == 400, resp.text
    reasons = resp.json()["error"]["params"]["reasons"]
    assert any("基线" in r for r in reasons)


def test_import_rejects_version_frozen_profile_mismatch(client: TestClient, db: Session) -> None:
    """版本冻结 Effective 引用与组内 Profile 来源不一致 → 上传 400, 不等确认。

    单项变异: 仅改版本内容块的 profile_id, 依赖文件不变; 上传边界一次核对
    即拒绝并给出 reasons, 确认不会保留错误引用。
    """
    owner = make_user(db, "fpm_owner")
    importer = make_user(db, "fpm_importer")
    pid = _create_project(client, owner)
    _setup_finance(client, owner, pid)
    _save_planning(client, owner, pid)
    _create_hist_version(client, owner, pid, "fpm-a")
    zip_bytes = _export_zip(client, owner, pid)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        doc = json.loads(zf.read("versions/0001.json").decode("utf-8"))
    assert doc["content"]["effective_finance"]["profile_id"] == "cn-north-demo"
    doc["content"]["effective_finance"]["profile_id"] = "cn-east-demo"
    bad = _rezip_mutate(zip_bytes, {
        "versions/0001.json": json.dumps(doc, ensure_ascii=False).encode(),
    })
    resp = client.post(
        "/api/projects/import",
        files={"file": ("pkg.zip", bad, "application/zip")},
        headers=_h(client, importer),
    )
    assert resp.status_code == 400, resp.text
    reasons = resp.json()["error"]["params"]["reasons"]
    assert any("0001" in r for r in reasons)


def test_import_rejects_current_effective_value_mismatch(client: TestClient, db: Session) -> None:
    """当前组 Effective 电价与来源合并结果不一致(profile_id 不变) → 上传 400。

    单项变异: 仅改当前组 effective 文件的电价值, Profile/Overrides 不变;
    上传边界从精确来源合并确定结果并拒绝。
    """
    from iesplan.core.yamlmini import load as yaml_load

    owner = make_user(db, "cvm_owner")
    importer = make_user(db, "cvm_importer")
    pid = _create_project(client, owner)
    _setup_finance(client, owner, pid)  # 覆盖 rev2: 电价 0.75
    zip_bytes = _export_zip(client, owner, pid)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        doc = yaml_load(zf.read("effective_finance.yaml").decode("utf-8"))
    price = doc["energy_prices"]["grid_import"]["value"]
    assert price["value"] == "0.75"
    price["value"] = "0.95"
    bad = _rezip_mutate(zip_bytes, {
        "effective_finance.yaml": yaml_dump(doc).encode("utf-8"),
    })
    resp = client.post(
        "/api/projects/import",
        files={"file": ("pkg.zip", bad, "application/zip")},
        headers=_h(client, importer),
    )
    assert resp.status_code == 400, resp.text
    reasons = resp.json()["error"]["params"]["reasons"]
    assert any("合并结果" in r for r in reasons)


def test_import_rejects_history_effective_value_mismatch(client: TestClient, db: Session) -> None:
    """历史组 Effective 电价与来源合并结果不一致(profile_id 不变) → 上传 400。"""
    from iesplan.core.yamlmini import load as yaml_load

    owner = make_user(db, "hvm_owner")
    importer = make_user(db, "hvm_importer")
    pid = _create_project(client, owner)
    _setup_finance(client, owner, pid)
    _save_planning(client, owner, pid)
    _create_hist_version(client, owner, pid, "hvm-a")
    zip_bytes = _export_zip(client, owner, pid)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        doc = yaml_load(zf.read("version_configs/0001/effective_finance.yaml").decode("utf-8"))
    price = doc["energy_prices"]["grid_import"]["value"]
    assert price["value"] == "0.75"
    price["value"] = "0.95"
    bad = _rezip_mutate(zip_bytes, {
        "version_configs/0001/effective_finance.yaml": yaml_dump(doc).encode("utf-8"),
    })
    resp = client.post(
        "/api/projects/import",
        files={"file": ("pkg.zip", bad, "application/zip")},
        headers=_h(client, importer),
    )
    assert resp.status_code == 400, resp.text
    reasons = resp.json()["error"]["params"]["reasons"]
    assert any("0001" in r for r in reasons)


def test_import_rejects_current_effective_profile_mismatch(client: TestClient, db: Session) -> None:
    """当前组 Effective 与 Profile 来源不一致 → 上传 400, 确认不存错文件。

    单项变异: 仅改当前组 effective 文件的 profile_id, 其余不变; 上传边界一次
    核对即拒绝。确认阶段原样发布已接受文件, 不重新合并, 故该核对必须在边界。
    """
    from iesplan.core.yamlmini import load as yaml_load

    owner = make_user(db, "cpm_owner")
    importer = make_user(db, "cpm_importer")
    pid = _create_project(client, owner)
    _setup_finance(client, owner, pid)
    zip_bytes = _export_zip(client, owner, pid)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        doc = yaml_load(zf.read("effective_finance.yaml").decode("utf-8"))
    assert doc["profile_id"] == "cn-north-demo"
    doc["profile_id"] = "cn-east-demo"
    bad = _rezip_mutate(zip_bytes, {
        "effective_finance.yaml": yaml_dump(doc).encode("utf-8"),
    })
    resp = client.post(
        "/api/projects/import",
        files={"file": ("pkg.zip", bad, "application/zip")},
        headers=_h(client, importer),
    )
    assert resp.status_code == 400, resp.text
    reasons = resp.json()["error"]["params"]["reasons"]
    assert any("Effective" in r for r in reasons)


def test_confirm_does_not_rerun_upload_gate(client: TestClient, db: Session, monkeypatch) -> None:
    """确认消费已接受提案, 不再调用上传边界的作者输入校验, 业务值仍正确发布。

    真实公开上传成功后, 把相关正式解析入口设为失败哨兵(普通 YAML/JSON 解码
    不算重复校验, 不设哨兵); 合并仅允许在上传边界执行, 确认阶段打桩合并器
    证明不再合并。公开确认仍应 201, 且当前业务值正确。
    """
    import iesplan.core.contracts as core_contracts
    import iesplan.finance.triplet as triplet
    import iesplan.planning.contracts as planning_contracts

    owner = make_user(db, "gate_owner")
    importer = make_user(db, "gate_importer")
    pid = _create_project(client, owner)
    _setup_finance(client, owner, pid)  # 覆盖 rev2: 电价 0.75
    _save_planning(client, owner, pid)  # 规划 rev1: 上界 1000
    _create_hist_version(client, owner, pid, "gate-a")
    zip_bytes = _export_zip(client, owner, pid)
    resp = client.post(
        "/api/projects/import",
        files={"file": ("pkg.zip", zip_bytes, "application/zip")},
        headers=_h(client, importer),
    )
    assert resp.status_code == 201, resp.text
    prop_id = resp.json()["proposal"]["id"]

    def _boom(*args, **kwargs):
        raise AssertionError("confirm must not rerun upload gate")

    with monkeypatch.context() as m:
        m.setattr(triplet.FinanceProfile, "from_dict", _boom)
        m.setattr(triplet.FinanceOverrides, "from_dict", _boom)
        m.setattr(triplet.EffectiveFinanceConfig, "from_dict", _boom)
        m.setattr(core_contracts.PlanningConfig, "from_dict", _boom)
        m.setattr(planning_contracts, "validate_planning_domain", _boom)
        # 合并器打桩在真实解析位置(定义处、确认可达模块的引用处与上传
        # 模块的本地绑定处): 确认若再合并即触发哨兵失败, 证明合并仅发生
        # 在上传边界。
        m.setattr(triplet, "merge_effective", _boom)
        import iesplan.application.configuration.revisions as revisions_mod
        m.setattr(revisions_mod, "merge_effective", _boom)
        import iesplan.application.packages.transfers as pkg_transfers
        m.setattr(pkg_transfers, "merge_effective", _boom)
        resp = client.post(f"/api/projects/import/{prop_id}/confirm", headers=_h(client, importer))
        assert resp.status_code == 201, resp.text
    new_pid = resp.json()["project"]["id"]

    ovr = client.get(f"/api/projects/{new_pid}/finance-overrides", headers=_h(client, importer)).json()
    assert ovr["finance_overrides"]["energy_prices"]["grid_import"]["value"]["value"] == "0.75"
    plan = client.get(f"/api/projects/{new_pid}/planning-config", headers=_h(client, importer)).json()
    assert plan["planning_config"]["variables"]["hp_cap"]["upper_bound"] == "1000"


def test_confirm_registers_effective_content_refs(client: TestClient, db: Session) -> None:
    """确认发布的历史/当前 Effective 内容对象均登记引用, 不被清理候选收录。

    公开上传→确认(含一个历史版本与当前工作状态); 经配置公开读口取历史
    rev1 与当前 Effective 行的内容对象 id; 引用公开视图须有
    effective_finance_content 归属; safe_cleanup  dry_run 候选不得包含二者
    (只预览, 不实际清理)。
    """
    import iesplan.configuration as configuration_domain
    import iesplan.storage as storage_domain

    owner = make_user(db, "ref_owner")
    importer = make_user(db, "ref_importer")
    pid = _create_project(client, owner)
    _setup_finance(client, owner, pid)
    _save_planning(client, owner, pid)
    _create_hist_version(client, owner, pid, "ref-a")
    zip_bytes = _export_zip(client, owner, pid)
    resp = client.post(
        "/api/projects/import",
        files={"file": ("pkg.zip", zip_bytes, "application/zip")},
        headers=_h(client, importer),
    )
    assert resp.status_code == 201, resp.text
    prop_id = resp.json()["proposal"]["id"]
    resp = client.post(f"/api/projects/import/{prop_id}/confirm", headers=_h(client, importer))
    assert resp.status_code == 201, resp.text
    new_pid = resp.json()["project"]["id"]

    hist_obj = configuration_domain.get_effective_revision(db, new_pid, 1).content_object_id
    cur_row = configuration_domain.get_current_effective(db, new_pid)
    assert cur_row.revision == 2
    cur_obj = cur_row.content_object_id
    refs = {
        r["object_id"]: r
        for r in storage_domain.find_refs_by_entity_type(db, "effective_finance_revisions")
    }
    assert refs[hist_obj]["purpose"] == "effective_finance_content"
    assert refs[cur_obj]["purpose"] == "effective_finance_content"
    plan = storage_domain.safe_cleanup(db, dry_run=True)
    cand_ids = {c["id"] for c in plan["candidates"]}
    assert hist_obj not in cand_ids
    assert cur_obj not in cand_ids


def test_confirm_failure_rolls_back_all_writes(client: TestClient, db: Session, monkeypatch) -> None:
    """确认写入后半段注入失败 → 本次 DB 写入整体回滚, 预建状态保留, 可重确认。

    版本重建阶段注入失败; 公开观察版本空/当前财务规划缺席/项目仍无草稿;
    晚阶段失败不留半成品项目(导入者列表仍为空); 解除注入后同一提案可重
    确认成功且值正确。外部 Blob 字节不在 DB 原子断言内。
    """
    import iesplan.configuration as configuration_domain
    import iesplan.project as project_domain
    import iesplan.storage as storage_domain

    owner = make_user(db, "rb_owner")
    importer = make_user(db, "rb_importer")
    pid = _create_project(client, owner)
    _setup_finance(client, owner, pid)
    _save_planning(client, owner, pid)
    _create_hist_version(client, owner, pid, "rb-a")
    zip_bytes = _export_zip(client, owner, pid)
    resp = client.post(
        "/api/projects/import",
        files={"file": ("pkg.zip", zip_bytes, "application/zip")},
        headers=_h(client, importer),
    )
    assert resp.status_code == 201, resp.text
    prop_id = resp.json()["proposal"]["id"]
    assert resp.json()["proposal"]["project_id"] is None

    def _content_refs() -> list[dict]:
        return [
            r for r in storage_domain.find_refs_by_entity_type(db, "effective_finance_revisions")
            if r["purpose"] == "effective_finance_content"
        ]

    ref_count_before = len(_content_refs())

    # 注入点记录本次已创建的新项目 id(创建先于版本重建), 用于实质回滚断言。
    recorded: dict = {}

    def _boom(*args, **kwargs):
        recorded["project_id"] = kwargs["project_id"]
        raise RuntimeError("injected late confirm failure")

    with monkeypatch.context() as m:
        m.setattr(project_domain, "create_version", _boom)
        resp = client.post(f"/api/projects/import/{prop_id}/confirm", headers=_h(client, importer))
        assert resp.status_code == 500, resp.text

    # 新增引用随确认写入一同回滚(历史 Effective 内容引用不残留)。
    assert len(_content_refs()) == ref_count_before

    # 实质回滚断言(公开领域读口 + 注入点记录的新项目 id, 不直接跨域 ORM):
    # 中途创建的项目行与其配置行均已消失, 不只列表无新增。
    new_pid = recorded["project_id"]
    assert project_domain.get_project(db, new_pid) is None
    assert configuration_domain.get_current_overrides(db, new_pid) is None
    assert configuration_domain.get_current_effective(db, new_pid) is None
    assert configuration_domain.get_current_planning(db, new_pid) is None
    listed = client.get("/api/projects", headers=_h(client, importer)).json()["projects"]
    assert listed == []

    resp = client.post(f"/api/projects/import/{prop_id}/confirm", headers=_h(client, importer))
    assert resp.status_code == 201, resp.text
    new_pid = resp.json()["project"]["id"]
    ovr = client.get(f"/api/projects/{new_pid}/finance-overrides", headers=_h(client, importer)).json()
    assert ovr["finance_overrides"]["energy_prices"]["grid_import"]["value"]["value"] == "0.75"


def test_unconfirmed_proposal_creates_no_project(client: TestClient, db: Session) -> None:
    """上传提案后、确认前: 只保存提案与暂存, 不创建普通项目。

    提案 project_id 为 null(缺席含义见包域契约); 导入者项目列表无新增,
    无半成品项目; 源包与暂存对象由提案登记引用, safe_cleanup dry_run
    候选不含(只预览, 不实际清理); 确认后项目才出现且详情可用。
    """
    import iesplan.storage as storage_domain

    owner = make_user(db, "life_owner")
    importer = make_user(db, "life_importer")
    pid = _create_project(client, owner)
    _create_hist_version(client, owner, pid, "life-a")
    zip_bytes = _export_zip(client, owner, pid)
    resp = client.post(
        "/api/projects/import",
        files={"file": ("pkg.zip", zip_bytes, "application/zip")},
        headers=_h(client, importer),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()["proposal"]
    assert body["status"] == "proposed"
    assert body["project_id"] is None
    listed = client.get("/api/projects", headers=_h(client, importer)).json()["projects"]
    assert listed == []

    staging = body["review_summary"]["staging"]
    staged_ids = {item["object_id"] for item in staging["objects"]} | {staging["source_object_id"]}
    refs = storage_domain.find_refs_by_entity_type(db, "import_proposals")
    assert staged_ids <= {r["object_id"] for r in refs}
    plan = storage_domain.safe_cleanup(db, dry_run=True)
    cand_ids = {c["id"] for c in plan["candidates"]}
    assert not (staged_ids & cand_ids)

    prop_id = body["id"]
    resp = client.post(f"/api/projects/import/{prop_id}/confirm", headers=_h(client, importer))
    assert resp.status_code == 201, resp.text
    new_pid = resp.json()["project"]["id"]
    listed = client.get("/api/projects", headers=_h(client, importer)).json()["projects"]
    assert any(p["id"] == new_pid and p["status"] == "active" for p in listed)
    view = client.get(f"/api/projects/{new_pid}", headers=_h(client, importer))
    assert view.status_code == 200, view.text


def test_confirm_replay_returns_same_project(client: TestClient, db: Session) -> None:
    """确认重放返回同一已应用项目, 不重复创建项目/版本。"""
    owner = make_user(db, "replay_owner")
    importer = make_user(db, "replay_importer")
    pid = _create_project(client, owner)
    _create_hist_version(client, owner, pid, "replay-a")
    zip_bytes = _export_zip(client, owner, pid)
    resp = client.post(
        "/api/projects/import",
        files={"file": ("pkg.zip", zip_bytes, "application/zip")},
        headers=_h(client, importer),
    )
    assert resp.status_code == 201, resp.text
    prop_id = resp.json()["proposal"]["id"]
    resp = client.post(f"/api/projects/import/{prop_id}/confirm", headers=_h(client, importer))
    assert resp.status_code == 201, resp.text
    new_pid = resp.json()["project"]["id"]
    resp = client.post(f"/api/projects/import/{prop_id}/confirm", headers=_h(client, importer))
    assert resp.status_code == 201, resp.text
    assert resp.json()["project"]["id"] == new_pid
    listed = client.get("/api/projects", headers=_h(client, importer)).json()["projects"]
    assert [p["id"] for p in listed] == [new_pid]
    versions = client.get(
        f"/api/projects/{new_pid}/versions", headers=_h(client, importer)
    ).json()["versions"]
    assert len(versions) == 1


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
    with pytest.raises(package_domain.ImportValidationError) as excinfo:
        packages_uc.propose_import(db, importer, zip_bytes)
    assert any("FinanceOverrides" in r for r in excinfo.value.reasons)


# ---------------------------------------------------------------------------
# 非空税则/池随包携带(B1b): 多税率/维护两维度/退款与期初亏损/显式确认表
# ---------------------------------------------------------------------------


def _tax_package_profile() -> dict:
    """非空税则包画像(合成，不读 0.15)：多税率、维护两维度、显式确认表。"""
    from test_config_revisions import _tax_profile_payload

    payload = _tax_profile_payload()
    vat = payload["tax_rules"]["operating_vat"]
    vat["om_input_tax"] = [
        {
            "for": "pv_system.annual_fixed_om",
            "rate": {"value": "0.06", "unit": "1"},
            "deductible_ratio": {"value": "1", "unit": "1"},
        },
        {
            "for": "pv_system.period_variable_om",
            "rate": {"value": "0.06", "unit": "1"},
            "deductible_ratio": {"value": "0.5", "unit": "1"},
        },
    ]
    vat["maintenance_input_tax"] = [
        {
            "year": 2,
            "position": 1,
            "rate": {"value": "0.13", "unit": "1"},
            "deductible_ratio": {"value": "0", "unit": "1"},
        },
        {
            "year": 2,
            "position": 2,
            "rate": {"value": "0.13", "unit": "1"},
            "deductible_ratio": {"value": "1", "unit": "1"},
        },
    ]
    payload["tax_rules"]["building_vat"] = {
        "kind": "construction_input_vat",
        "label_ref": "vat_label",
        "for": "pv_system.upfront_capex",
        "rate": {"value": "0.09", "unit": "1"},
        "deductible_ratio": {"value": "0.8", "unit": "1"},
        "credit_pool": "main_pool",
        "confirmation": {
            "method": "explicit_schedule",
            "schedule": [{"period": 1, "ratio": "0.6"}, {"period": 13, "ratio": "0.4"}],
        },
    }
    return payload


def _tax_package_overrides(rate: str, refund_amount: str) -> dict:
    """非空税则包覆盖: 所得税率 + 维护两维度 + 期初亏损 + 池期初/退款。"""
    return {
        "schema": "ies.finance-overrides",
        "schema_version": "4.0.0",
        "profile_ref": {"id": "cn-tax-demo"},
        "credit_pools": {
            "main_pool": {
                "opening_credit": {"value": "90000", "unit": "CNY"},
                "refunds": [
                    {
                        "period": 14,
                        "amount": {"value": refund_amount, "unit": "CNY"},
                        "arrival_period": 15,
                    }
                ],
            },
        },
        "tax_rules": {
            "income": {
                "rate": {"value": rate, "unit": "1"},
                "deductible_maintenance": {
                    "mode": "explicit_items",
                    "items": [
                        {"year": 2, "position": 1, "deductible": True},
                        {"year": 2, "position": 2, "deductible": False},
                    ],
                },
                "opening_losses": [{"amount": {"value": "30000", "unit": "CNY"}, "last_usable_year": 6}],
            },
        },
    }


def _setup_tax_finance(client: TestClient, user, pid: int) -> None:
    """登记非空税则画像并由项目引用(空覆盖 rev1)。"""
    resp = client.post(
        "/api/finance-profiles",
        json={"finance_profile": _tax_package_profile()},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    resp = client.put(
        f"/api/projects/{pid}/finance-profile",
        json={"profile_ref": {"id": "cn-tax-demo"}},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text


def _put_tax_overrides(client: TestClient, user, pid: int, payload: dict, expected_revision) -> None:
    resp = client.put(
        f"/api/projects/{pid}/finance-overrides",
        json={"finance_overrides": payload, "expected_revision": expected_revision},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text


def test_export_import_roundtrip_with_nonempty_tax_rules(client: TestClient, db: Session) -> None:
    """非空税则/池随真实包导出→上传提案→确认导入→公开读回，源项目不受影响。"""
    from decimal import Decimal

    from iesplan.core.yamlmini import load as yaml_load

    owner = make_user(db, "taxpkg_owner")
    importer = make_user(db, "taxpkg_importer")
    pid = _create_project(client, owner)
    _setup_tax_finance(client, owner, pid)
    _put_tax_overrides(client, owner, pid, _tax_package_overrides("0.2", "1500"), 1)

    zip_bytes = _export_zip(client, owner, pid)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        profile = FinanceProfile.from_dict(yaml_load(zf.read("finance_profile.yaml").decode("utf-8")))
        assert profile.tax_rules["income"].rate.value == Decimal("0.25")  # type: ignore[union-attr]
        om = profile.tax_rules["operating_vat"].om_input_tax  # type: ignore[union-attr]
        assert [e.deductible_ratio.value for e in om] == [Decimal("1"), Decimal("0.5")]
        sched = profile.tax_rules["building_vat"].confirmation  # type: ignore[union-attr]
        assert sched.method == "explicit_schedule"
        assert [(e.period, e.ratio) for e in sched.schedule] == [(1, Decimal("0.6")), (13, Decimal("0.4"))]
        effective = EffectiveFinanceConfig.from_dict(
            yaml_load(zf.read("effective_finance.yaml").decode("utf-8"))
        )
        assert effective.tax_rules["income"].rate.value == Decimal("0.2")  # type: ignore[union-attr]
        assert effective.credit_pools["main_pool"].opening_credit.value == Decimal("90000")
        assert [(r.period, r.arrival_period) for r in effective.credit_pools["main_pool"].refunds] == [
            (14, 15)
        ]
        income_items = effective.tax_rules["income"].deductible_maintenance.items  # type: ignore[union-attr]
        assert [(i.year, i.position, i.deductible) for i in income_items] == [(2, 1, True), (2, 2, False)]
        vat_items = effective.tax_rules["operating_vat"].maintenance_input_tax  # type: ignore[union-attr]
        assert [(i.year, i.position, i.deductible_ratio.value) for i in vat_items] == [
            (2, 1, Decimal("0")),
            (2, 2, Decimal("1")),
        ]
        losses = effective.tax_rules["income"].opening_losses  # type: ignore[union-attr]
        assert [(o.amount.value, o.last_usable_year) for o in losses] == [(Decimal("30000"), 6)]

    new_pid, _ = _import_new_project(client, importer, zip_bytes)
    assert new_pid != pid
    eff_body = client.get(f"/api/projects/{new_pid}/effective-finance", headers=_h(client, importer)).json()[
        "effective_finance_config"
    ]
    assert EffectiveFinanceConfig.from_dict(eff_body).to_dict() == effective.to_dict()
    assert eff_body["tax_rules"]["income"]["rate"] == {"value": "0.2", "unit": "1"}
    assert eff_body["tax_rules"]["building_vat"]["confirmation"]["schedule"] == [
        {"period": 1, "ratio": "0.6"},
        {"period": 13, "ratio": "0.4"},
    ]
    assert eff_body["credit_pools"]["main_pool"]["opening_credit"] == {"value": "90000", "unit": "CNY"}
    assert eff_body["credit_pools"]["main_pool"]["refunds"][0]["amount"] == {"value": "1500", "unit": "CNY"}
    ovr_body = client.get(f"/api/projects/{new_pid}/finance-overrides", headers=_h(client, importer)).json()[
        "finance_overrides"
    ]
    assert ovr_body["tax_rules"]["income"]["rate"] == {"value": "0.2", "unit": "1"}
    assert ovr_body["tax_rules"]["income"]["opening_losses"] == [
        {"amount": {"value": "30000", "unit": "CNY"}, "last_usable_year": 6}
    ]

    src_eff = client.get(f"/api/projects/{pid}/effective-finance", headers=_h(client, owner)).json()[
        "effective_finance_config"
    ]
    assert EffectiveFinanceConfig.from_dict(src_eff).to_dict() == effective.to_dict()
    assert src_eff["tax_rules"]["income"]["rate"] == {"value": "0.2", "unit": "1"}


def test_imported_history_versions_restore_distinct_tax_overrides(client: TestClient, db: Session) -> None:
    """两份不同合法税则覆盖各自精确依赖：恢复取原值，不被当前覆盖替代。"""
    owner = make_user(db, "taxhist_owner")
    importer = make_user(db, "taxhist_importer")
    pid = _create_project(client, owner)
    _setup_tax_finance(client, owner, pid)
    _put_tax_overrides(client, owner, pid, _tax_package_overrides("0.2", "1500"), 1)
    _create_hist_version(client, owner, pid, "tax-a")
    _put_tax_overrides(client, owner, pid, _tax_package_overrides("0.15", "2500"), 2)
    _create_hist_version(client, owner, pid, "tax-b")

    zip_bytes = _export_zip(client, owner, pid)
    new_pid, _ = _import_new_project(client, importer, zip_bytes)
    listed = client.get(f"/api/projects/{new_pid}/versions", headers=_h(client, importer)).json()["versions"]
    assert len(listed) == 2
    by_name = {v["name"]: v for v in listed}

    resp = client.post(
        f"/api/projects/{new_pid}/versions/{by_name['tax-a']['id']}/restore",
        headers=_h(client, importer),
    )
    assert resp.status_code == 200, resp.text
    ovr = client.get(f"/api/projects/{new_pid}/finance-overrides", headers=_h(client, importer)).json()[
        "finance_overrides"
    ]
    assert ovr["tax_rules"]["income"]["rate"] == {"value": "0.2", "unit": "1"}
    assert ovr["credit_pools"]["main_pool"]["refunds"][0]["amount"] == {"value": "1500", "unit": "CNY"}

    resp = client.post(
        f"/api/projects/{new_pid}/versions/{by_name['tax-b']['id']}/restore",
        headers=_h(client, importer),
    )
    assert resp.status_code == 200, resp.text
    ovr = client.get(f"/api/projects/{new_pid}/finance-overrides", headers=_h(client, importer)).json()[
        "finance_overrides"
    ]
    assert ovr["tax_rules"]["income"]["rate"] == {"value": "0.15", "unit": "1"}
    assert ovr["credit_pools"]["main_pool"]["refunds"][0]["amount"] == {"value": "2500", "unit": "CNY"}


def test_import_rejects_tampered_tax_values(client: TestClient, db: Session) -> None:
    """包内 Effective 税率被改坏 / Overrides 越权加范围 → 上传边界拒绝，不能确认补救。"""
    from iesplan.core.yamlmini import load as yaml_load

    owner = make_user(db, "taxtamper_owner")
    importer = make_user(db, "taxtamper_importer")
    pid = _create_project(client, owner)
    _setup_tax_finance(client, owner, pid)
    _put_tax_overrides(client, owner, pid, _tax_package_overrides("0.2", "1500"), 1)
    zip_bytes = _export_zip(client, owner, pid)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        eff_doc = yaml_load(zf.read("effective_finance.yaml").decode("utf-8"))
        ovr_doc = yaml_load(zf.read("finance_overrides.yaml").decode("utf-8"))
    assert eff_doc["tax_rules"]["income"]["rate"]["value"] == "0.2"
    eff_doc["tax_rules"]["income"]["rate"]["value"] = "0.99"
    bad = _rezip_mutate(zip_bytes, {"effective_finance.yaml": yaml_dump(eff_doc).encode("utf-8")})
    resp = client.post(
        "/api/projects/import",
        files={"file": ("pkg.zip", bad, "application/zip")},
        headers=_h(client, importer),
    )
    assert resp.status_code == 400, resp.text
    assert any("合并结果" in r for r in resp.json()["error"]["params"]["reasons"])

    ovr_doc["tax_rules"]["income"]["revenue"] = []
    bad = _rezip_mutate(zip_bytes, {"finance_overrides.yaml": yaml_dump(ovr_doc).encode("utf-8")})
    resp = client.post(
        "/api/projects/import",
        files={"file": ("pkg.zip", bad, "application/zip")},
        headers=_h(client, importer),
    )
    assert resp.status_code == 400, resp.text


def test_issue04_d1_demand_rate_and_binding_package_roundtrip(client: TestClient, db: Session) -> None:
    """Real package: profile/rate override + draft/version demand bindings survive import."""
    from copy import deepcopy
    from iesplan.core.yamlmini import load as yaml_load

    owner = make_user(db, "d1_pkg_owner")
    importer = make_user(db, "d1_pkg_importer")
    pid = _create_project(client, owner, "D1 synthetic demand package")
    profile_doc = deepcopy(PROFILE_PAYLOAD)
    profile_doc["profile"]["id"] = "d1-rate-profile"
    profile_doc["demand_charges"] = {
        "month_kw": {"rate": {"value": "22", "unit": "CNY/kW"}},
    }
    _register_profile(client, owner, profile_doc)
    _set_profile(client, owner, pid, "d1-rate-profile")
    override = FinanceOverrides.from_dict({
        "schema": "ies.finance-overrides", "schema_version": "4.0.0",
        "profile_ref": {"id": "d1-rate-profile"},
        "demand_charges": {"month_kw": {"rate": {"value": "33", "unit": "CNY/kW"}}},
    }, profile=FinanceProfile.from_dict(profile_doc))
    resp = client.put(
        f"/api/projects/{pid}/finance-overrides",
        json={"finance_overrides": override.to_dict(), "expected_revision": 1},
        headers=_h(client, owner),
    )
    assert resp.status_code == 200, resp.text

    binding = {
        "billing_main": {"charge": "month_kw", "instance": "grid1", "interface": "electricity_import"},
    }
    view = client.get(f"/api/projects/{pid}", headers=_h(client, owner)).json()
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": view["draft"]["revision"], "commands": [{
            "id": "d1-explicit-demand-binding", "unit": "assembly",
            "type": "assembly.set_demand_bindings", "payload": {"bindings": binding},
        }]},
        headers=_h(client, owner),
    )
    assert resp.status_code == 200, resp.text
    _create_hist_version(client, owner, pid, "D1 frozen binding")
    package = _export_zip(client, owner, pid)
    with zipfile.ZipFile(io.BytesIO(package)) as zipped:
        assert json.loads(zipped.read("draft.json"))["content"]["demand_bindings"] == binding
        history = json.loads(zipped.read("versions/0001.json"))
        assert history["content"]["demand_bindings"] == binding
        effective = yaml_load(zipped.read("effective_finance.yaml").decode("utf-8"))
        assert effective["schema_version"] == "4.0.0"
        assert effective["demand_charges"]["month_kw"]["rate"]["value"] == "33"
        overrides = yaml_load(zipped.read("finance_overrides.yaml").decode("utf-8"))
        assert overrides["demand_charges"]["month_kw"]["rate"]["value"] == "33"
    new_pid, _ = _import_new_project(client, importer, package)
    view = client.get(f"/api/projects/{new_pid}", headers=_h(client, importer)).json()
    assert view["draft"]["content"]["demand_bindings"] == binding
    effective = client.get(f"/api/projects/{new_pid}/effective-finance", headers=_h(client, importer))
    assert effective.status_code == 200
    assert effective.json()["effective_finance_config"]["demand_charges"]["month_kw"]["rate"]["value"] == "33"
    versions = client.get(f"/api/projects/{new_pid}/versions", headers=_h(client, importer)).json()["versions"]
    assert len(versions) == 1
    restored = client.post(
        f"/api/projects/{new_pid}/versions/{versions[0]['id']}/restore",
        headers=_h(client, importer),
    )
    assert restored.status_code == 200, restored.text
    view = client.get(f"/api/projects/{new_pid}", headers=_h(client, importer)).json()
    assert view["draft"]["content"]["demand_bindings"] == binding
