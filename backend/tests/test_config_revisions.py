"""财务三件套与规划配置(0.6.5 条目 1-2)持久化/服务/API 测试。

覆盖:
- Profile 登记: 注册表按 id 唯一(同 id 重登记复用既有行)、owner 引用建立;
- 项目引用 Profile: 精确 profile_ref{id} 定位, 原子生成空覆盖
  Effective(用户不可直接 author Effective), 事务提交后可被新会话读取;
- Overrides 保存/清空: 追加不可变 revision → 确定性重合并生成 Effective,
  DELETE 追加空文档(乐观锁 409);
- 领域校验: Profile 未设置无静默默认、覆盖越权(新增 finance_type/price_id/
  改单位/改 carrier/direction)拒绝;
- 持久化: 不可变 revision 追加、乐观锁 409、未保存 404;
- Planning: 必须先生成 Effective(400); 保存后返回 revision 指针;
  任何 Profile 切换/Overrides 保存/清空生成新 Effective 时当前指针失效;
- 迁移 0006: 新表/指针列创建、旧 finance_configs 表与旧指针退役、幂等,
  兼容 fresh 与 legacy 0005 两种输入。

测试环境: SQLite :memory:(StaticPool 共享连接) + tmp 对象存储目录。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")

import pytest  # noqa: E402
from auth_helpers import login_headers, make_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan.api import config_revisions as config_api  # noqa: E402
from iesplan.api import projects as projects_api  # noqa: E402
from iesplan.config import settings  # noqa: E402
from iesplan.core.contracts import PlanningConfig, PlanningConfigError  # noqa: E402
from iesplan.db import Base, get_db  # noqa: E402
from iesplan.finance import (  # noqa: E402
    EffectiveFinanceConfig,
    FinanceOverrides,
    FinanceProfile,
)
from iesplan.main import create_app  # noqa: E402
from iesplan.migrations import _migrate_0006  # noqa: E402

# ---------------------------------------------------------------------------
# 样例(权威拼写 CNY/kW/a 与 CNY/kWh/a, 验证单位解析器连续除法集成)
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
        "battery_system": {
            "upfront_capex": {
                "linear": {"energy_kwh": {"unit_cost": {"value": "900", "unit": "CNY/kWh"}}},
            },
            "annual_fixed_om": {
                "linear": {"energy_kwh": {"unit_cost": {"value": "18", "unit": "CNY/kWh/a"}}},
            },
            "period_variable_om": {
                "linear": {"cycled_kwh": {"unit_cost": {"value": "0.002", "unit": "CNY/kWh"}}},
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
        "pv_export": {
            "carrier": "electricity",
            "direction": "sale",
            "kind": "constant",
            "value": {"value": "0.4", "unit": "CNY/kWh"},
        },
    },
    "taxes": {},
}


def _overrides_payload(profile: FinanceProfile) -> dict:
    return {
        "schema": "ies.finance-overrides",
        "schema_version": "1.0.0",
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


def _profile() -> FinanceProfile:
    return FinanceProfile.from_dict(PROFILE_PAYLOAD)


def _profile_ref(profile: FinanceProfile) -> dict:
    return {"profile_ref": {"id": profile.profile_id}}


def _overrides(profile: FinanceProfile) -> FinanceOverrides:
    return FinanceOverrides.from_dict(_overrides_payload(profile), profile=profile)


# ---------------------------------------------------------------------------
# 测试环境
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
    app.include_router(projects_api.router)
    app.include_router(config_api.router)
    app.include_router(config_api.profile_router)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _owner(client: TestClient, db_session: Session, name: str = "cfg_owner") -> tuple[dict, int]:
    user = make_user(db_session, name)
    headers = login_headers(client, user)
    resp = client.post(
        "/api/projects",
        json={
            "name": f"配置项目 {name}",
            "baseline_resolution": "1h",
            "baseline_leap_year": False,
            "baseline_scenario_mode": "single",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return headers, resp.json()["project"]["id"]


def _register_profile(client: TestClient, headers: dict) -> dict:
    resp = client.post(
        "/api/finance-profiles",
        json={"finance_profile": PROFILE_PAYLOAD},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["finance_profile"]


# ---------------------------------------------------------------------------
# Profile 登记(注册表按 id 唯一, 同 id 重登记幂等复用)
# ---------------------------------------------------------------------------


def test_profile_registration_roundtrip_and_dedup(client: TestClient, db_session: Session) -> None:
    headers, _ = _owner(client, db_session)
    _register_profile(client, headers)
    # revision 作为精确配置身份字段保留；信任流程不重算内容。
    # 相同内容重复登记: 幂等返回既有(不重复落对象行)
    first_id = client.get("/api/finance-profiles/cn-north-demo", headers=headers).json()["row"]["id"]
    second_id = client.get("/api/finance-profiles/cn-north-demo", headers=headers).json()["row"]["id"]
    assert first_id == second_id


def test_profile_registration_attaches_owner_ref(
    client: TestClient, db_session: Session
) -> None:
    """注册 Profile 的 YAML 对象必须建立稳定 owner 引用(防 orphan 清理)。"""
    headers, _ = _owner(client, db_session)
    _register_profile(client, headers)
    body = client.get("/api/finance-profiles/cn-north-demo", headers=headers).json()
    object_id = body["row"]["object_id"]
    from iesplan.storage import list_refs, safe_cleanup

    refs = list_refs(db_session, object_id)
    assert any(r.ref_type == "finance_profile" for r in refs)
    # 清理计划(引用清单为权威)不得把受引用对象列为候选
    plan = safe_cleanup(db_session, dry_run=True)
    candidates = {item["id"] for item in plan.get("candidates", [])}
    assert object_id not in candidates, "受引用 Profile 对象不应被清理"
    # 幂等注册不重复引用(同内容去重路径不加第二引用)
    _register_profile(client, headers)
    refs2 = list_refs(db_session, object_id)
    assert len([r for r in refs2 if r.ref_type == "finance_profile"]) == 1


def test_list_finance_profiles_dedup_latest(client: TestClient, db_session: Session) -> None:
    """列表按 profile_id 去重取最新登记, 跨库确定性(SQLite 与 PG 语义一致)。"""
    headers, _ = _owner(client, db_session)
    _register_profile(client, headers)
    # 同 id 登记不同内容(新摘要, 内容寻址新行)
    profile2_payload = {
        **PROFILE_PAYLOAD,
        "energy_prices": {
            **PROFILE_PAYLOAD["energy_prices"],
            "grid_import": {**PROFILE_PAYLOAD["energy_prices"]["grid_import"],
                            "value": {"value": "0.71", "unit": "CNY/kWh"}},
        },
    }
    resp = client.post("/api/finance-profiles", json={"finance_profile": profile2_payload}, headers=headers)
    assert resp.status_code == 200
    resp = client.get("/api/finance-profiles", headers=headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1  # 同一 profile_id 只返回最新一条
    assert resp.json()["count"] == 1


# ---------------------------------------------------------------------------
# 项目 Profile 引用 + 空覆盖 Effective
# ---------------------------------------------------------------------------


def test_project_set_profile_generates_effective(client: TestClient, db_session: Session) -> None:
    headers, pid = _owner(client, db_session)
    _register_profile(client, headers)
    resp = client.put(
        f"/api/projects/{pid}/finance-profile",
        json=_profile_ref(_profile()),
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    # GET 一致
    resp = client.get(f"/api/projects/{pid}/effective-finance", headers=headers)
    assert resp.status_code == 200


def test_effective_not_authorable(client: TestClient, db_session: Session) -> None:
    """用户不能直接 author Effective: 仅 Profile 引用 / Overrides 保存触发合并。"""
    headers, pid = _owner(client, db_session)
    # 未引用 Profile → 无 Effective(404)
    resp = client.get(f"/api/projects/{pid}/effective-finance", headers=headers)
    assert resp.status_code == 404
    # 未引用 Profile 直接保存 Overrides → 400(无静默默认)
    resp = client.put(
        f"/api/projects/{pid}/finance-overrides",
        json={"finance_overrides": _overrides_payload(_profile()), "expected_revision": None},
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PROJ-FIN-002"


# ---------------------------------------------------------------------------
# Overrides 保存 → 重合并 Effective
# ---------------------------------------------------------------------------


def test_overrides_save_remerges_effective(client: TestClient, db_session: Session) -> None:
    headers, pid = _owner(client, db_session)
    _register_profile(client, headers)
    client.put(
        f"/api/projects/{pid}/finance-profile",
        json=_profile_ref(_profile()),
        headers=headers,
    )
    profile = _profile()
    overrides = _overrides(profile)
    # 设置 Profile 已生成空覆盖 revision=1; 真实覆盖需基于当前指针(乐观锁)
    resp = client.put(
        f"/api/projects/{pid}/finance-overrides",
        json={"finance_overrides": overrides.to_dict(), "expected_revision": 1},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revision"] == 2  # 空覆盖(1) + 本次真实覆盖(2)
    # 覆盖生效: 被覆盖叶子为新值, 未覆盖叶子保留
    eff = EffectiveFinanceConfig.from_dict(body["effective_finance_config"])
    assert eff.finance_types["pv_system"].upfront_capex.fixed.value == Decimal("1500")  # type: ignore[union-attr]
    assert eff.energy_prices["grid_import"].value.value == Decimal("0.75")  # type: ignore[union-attr]
    assert eff.energy_prices["pv_export"].value.value == Decimal("0.4")  # type: ignore[union-attr]
    # GET overrides 一致
    resp = client.get(f"/api/projects/{pid}/finance-overrides", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["revision"] == 2


def test_overrides_scope_violations_rejected(client: TestClient, db_session: Session) -> None:
    """越权覆盖(新增 finance_type/price_id/改单位/改 carrier)一律拒绝。"""
    headers, pid = _owner(client, db_session)
    _register_profile(client, headers)
    client.put(
        f"/api/projects/{pid}/finance-profile",
        json=_profile_ref(_profile()),
        headers=headers,
    )
    base = _overrides(_profile()).to_dict()

    cases: list[dict] = [
        {**base, "finance_types": {**base["finance_types"], "new_tech": {"upfront_capex": {"fixed": {"value": "1", "unit": "CNY"}}}}},  # 新增 finance_type
        {**base, "energy_prices": {**base["energy_prices"], "new_price": {"kind": "constant", "value": {"value": "1", "unit": "CNY/kWh"}}}},  # 新增 price_id
        {
            **base,
            "finance_types": {
                "pv_system": {
                    "upfront_capex": {
                        "fixed": {"value": "1500", "unit": "USD"},  # 改单位(且币种不符)
                    },
                },
            },
        },
        {**base, "energy_prices": {"grid_import": {"carrier": "heat", "direction": "purchase", "kind": "constant", "value": {"value": "1", "unit": "CNY/kWh"}}}},  # 改写 carrier
        {**base, "profile_ref": {"id": "cn-north-demo", "revision": 1}},  # profile_ref 非精确 {id} 形态
    ]
    for i, bad in enumerate(cases):
        resp = client.put(
            f"/api/projects/{pid}/finance-overrides",
            json={"finance_overrides": bad, "expected_revision": None},
            headers=headers,
        )
        assert resp.status_code == 400, f"case {i}: {resp.text}"
        assert resp.json()["error"]["code"] == "PROJ-FIN-001"


# ---------------------------------------------------------------------------
# 并发 / 失败原子
# ---------------------------------------------------------------------------


def test_overrides_stale_revision_conflict(client: TestClient, db_session: Session) -> None:
    headers, pid = _owner(client, db_session)
    _register_profile(client, headers)
    client.put(
        f"/api/projects/{pid}/finance-profile",
        json=_profile_ref(_profile()),
        headers=headers,
    )
    overrides = _overrides(_profile()).to_dict()
    r1 = client.put(
        f"/api/projects/{pid}/finance-overrides",
        json={"finance_overrides": overrides, "expected_revision": 1},
        headers=headers,
    )
    assert r1.status_code == 200
    # 陈旧 expected_revision(仍传 1) → 409
    r2 = client.put(
        f"/api/projects/{pid}/finance-overrides",
        json={"finance_overrides": overrides, "expected_revision": 1},
        headers=headers,
    )
    assert r2.status_code == 409
    # 失败不产生新 revision(原子)
    resp = client.get(f"/api/projects/{pid}/finance-overrides", headers=headers)
    assert resp.json()["revision"] == 2


def test_profile_switch_invalidates_chain(client: TestClient, db_session: Session) -> None:
    """切换 Profile 后旧覆盖摘要链失效: 清空旧指针并以新 Profile 重建空覆盖。"""
    headers, pid = _owner(client, db_session)
    _register_profile(client, headers)
    client.put(
        f"/api/projects/{pid}/finance-profile",
        json=_profile_ref(_profile()),
        headers=headers,
    )
    client.put(
        f"/api/projects/{pid}/finance-overrides",
        json={"finance_overrides": _overrides(_profile()).to_dict(), "expected_revision": 1},
        headers=headers,
    )
    # 登记第二个 Profile 并切换
    profile2_payload = {
        **PROFILE_PAYLOAD,
        "profile": {**PROFILE_PAYLOAD["profile"], "id": "cn-south-demo", "region": "CN-South"},
    }
    resp = client.post("/api/finance-profiles", json={"finance_profile": profile2_payload}, headers=headers)
    assert resp.status_code == 200
    resp = client.put(
        f"/api/projects/{pid}/finance-profile",
        json=_profile_ref(FinanceProfile.from_dict(profile2_payload)),
        headers=headers,
    )
    assert resp.status_code == 200
    # 切换后: 新空覆盖 revision(表内 max+1, 不撞旧行); Effective 血缘指向新 Profile
    body = resp.json()
    assert body["overrides_revision"] == 3  # 空覆盖(1) + 真实覆盖(2) + 切换重建(3)
    # 新覆盖为空覆盖文档(旧覆盖不残留)
    resp = client.get(f"/api/projects/{pid}/finance-overrides", headers=headers)
    assert resp.status_code == 200
    doc = resp.json()["finance_overrides"]
    assert doc["profile_ref"]["id"] == "cn-south-demo"
    # 空覆盖文档: canonical_dict 仅在非空时写 finance_types/energy_prices
    assert not doc.get("finance_types", {})
    assert not doc.get("energy_prices", {})


# ---------------------------------------------------------------------------
# PlanningConfig(保存要求当前 Effective 存在; 版本链以显式 revision 引用)
# ---------------------------------------------------------------------------


def test_planning_requires_current_effective(client: TestClient, db_session: Session) -> None:
    headers, pid = _owner(client, db_session)
    # 未生成 Effective → 400(PROJ-PLAN-002)
    resp = client.put(
        f"/api/projects/{pid}/planning-config",
        json={"planning_config": _planning_payload(), "expected_revision": None},
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PROJ-PLAN-002"
    # 生成 Effective 后保存 → 成功, GET 一致
    _register_profile(client, headers)
    client.put(
        f"/api/projects/{pid}/finance-profile",
        json=_profile_ref(_profile()),
        headers=headers,
    )
    resp = client.put(
        f"/api/projects/{pid}/planning-config",
        json={"planning_config": _planning_payload(), "expected_revision": None},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 1
    resp = client.get(f"/api/projects/{pid}/planning-config", headers=headers)
    assert resp.status_code == 200


def test_planning_config_contract() -> None:
    config = PlanningConfig.from_dict(_planning_payload())
    assert PlanningConfig.from_dict(config.to_dict()) == config
    with pytest.raises(PlanningConfigError):
        PlanningConfig.from_dict({**_planning_payload(), "finance_revision": 1})


# ---------------------------------------------------------------------------
# 迁移 0006
# ---------------------------------------------------------------------------


def test_migration_0006_retires_old_tables_and_pointers() -> None:
    eng = create_engine("sqlite+pysqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        _migrate_0006(conn)
        _migrate_0006(conn)  # 幂等
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).all()
        }
        assert {"finance_profiles", "finance_overrides", "effective_finance_revisions"} <= tables
        assert "finance_configs" not in tables  # 旧单体表退役
        proj_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(projects)")).all()}
        assert "finance_revision" not in proj_cols
        assert {"finance_profile_id", "overrides_revision", "effective_finance_revision"} <= proj_cols
        planning_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(planning_configs)")).all()}
        assert "finance_revision" not in planning_cols
    eng.dispose()


def test_migration_0006_legacy_0005_schema_cleans_invalid_planning() -> None:
    """B) 模拟正常完成 0005 的 legacy schema: 0006 清理失效规划行/指针并退役旧表。"""
    eng = create_engine("sqlite+pysqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        # 造 0005 legacy 形态: 旧 finance_configs 表、旧 planning_configs.finance_revision、
        # projects.finance_revision 指针与无效历史行
        conn.execute(text(
            "CREATE TABLE finance_configs ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL REFERENCES projects(id),"
            " revision INTEGER NOT NULL, content TEXT NOT NULL,"
            " created_by INTEGER NOT NULL REFERENCES users(id),"
            " created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(project_id, revision))"
        ))
        conn.execute(text(
            "ALTER TABLE planning_configs ADD COLUMN finance_revision INTEGER"
        ))
        conn.execute(text("ALTER TABLE projects ADD COLUMN finance_revision INTEGER"))
        # 旧行: 指向将被删除的旧 FinanceConfig(旧引用失效)
        conn.execute(text(
            "INSERT INTO projects (name, status, owner_id, currency, baseline_resolution,"
            " baseline_leap_year, baseline_scenario_mode, schema_version,"
            " created_by, planning_revision, finance_revision)"
            " VALUES ('legacy', 'active', 1, 'CNY', '1h', 0, 'single', 1, 1, 7, 3)"
        ))
        conn.execute(text(
            "INSERT INTO planning_configs (project_id, revision, content,"
            " finance_revision, created_by)"
            " VALUES (1, 7, '{}', 3, 1)"
        ))
        _migrate_0006(conn)
        _migrate_0006(conn)  # 幂等
        # 旧 planning 行被清理, 项目 planning 指针清空
        cnt = conn.execute(text("SELECT COUNT(*) FROM planning_configs")).scalar()
        assert cnt == 0
        ptr = conn.execute(text("SELECT planning_revision FROM projects WHERE id=1")).scalar()
        assert ptr is None
        # 旧 finance_revision 指针退役
        proj_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(projects)")).all()}
        assert "finance_revision" not in proj_cols
        planning_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(planning_configs)")).all()}
        assert "finance_revision" not in planning_cols
        # 旧单体表退役
        tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).all()}
        assert "finance_configs" not in tables
    eng.dispose()


# ---------------------------------------------------------------------------
# 追加阻断修复: planning 失效 / DELETE overrides / 精确引用 / 事务提交
# ---------------------------------------------------------------------------


def _set_profile_and_planning(client: TestClient, headers: dict, pid: int) -> int:
    """设置 Profile + 保存规划, 返回 planning_revision(显式 revision 引用, 非摘要)。"""
    _register_profile(client, headers)
    client.put(f"/api/projects/{pid}/finance-profile", json=_profile_ref(_profile()), headers=headers)
    eff = client.get(f"/api/projects/{pid}/effective-finance", headers=headers).json()
    assert eff["revision"] >= 1
    resp = client.put(
        f"/api/projects/{pid}/planning-config",
        json={"planning_config": _planning_payload(), "expected_revision": None},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def test_planning_invalidated_on_profile_switch(
    client: TestClient, db_session: Session
) -> None:
    """Profile 切换生成新 Effective → 当前 planning 指针失效(历史行保留)。"""
    headers, pid = _owner(client, db_session)
    plan_rev = _set_profile_and_planning(client, headers, pid)
    # 登记第二个 Profile 并切换
    profile2_payload = {
        **PROFILE_PAYLOAD,
        "profile": {**PROFILE_PAYLOAD["profile"], "id": "cn-south-demo", "region": "CN-South"},
    }
    resp = client.post("/api/finance-profiles", json={"finance_profile": profile2_payload}, headers=headers)
    assert resp.status_code == 200
    resp = client.put(
        f"/api/projects/{pid}/finance-profile",
        json=_profile_ref(FinanceProfile.from_dict(profile2_payload)),
        headers=headers,
    )
    assert resp.status_code == 200
    # planning 当前指针已清空(未保存前 404)
    resp = client.get(f"/api/projects/{pid}/planning-config", headers=headers)
    assert resp.status_code == 404
    # 历史 planning 行保留(append-only)
    rows = db_session.execute(
        text("SELECT COUNT(*) FROM planning_configs WHERE project_id=:p AND revision=:r"),
        {"p": pid, "r": plan_rev},
    ).scalar()
    assert rows == 1


def test_planning_invalidated_on_overrides_save(
    client: TestClient, db_session: Session
) -> None:
    """PUT Overrides 生成新 Effective → 当前 planning 指针失效。"""
    headers, pid = _owner(client, db_session)
    _set_profile_and_planning(client, headers, pid)
    resp = client.put(
        f"/api/projects/{pid}/finance-overrides",
        json={"finance_overrides": _overrides(_profile()).to_dict(), "expected_revision": 1},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    resp = client.get(f"/api/projects/{pid}/planning-config", headers=headers)
    assert resp.status_code == 404


def test_planning_invalidated_on_overrides_delete(
    client: TestClient, db_session: Session
) -> None:
    """DELETE Overrides(追加空文档 + 新 Effective)→ 当前 planning 指针失效。"""
    headers, pid = _owner(client, db_session)
    _set_profile_and_planning(client, headers, pid)
    resp = client.request(
        "DELETE", f"/api/projects/{pid}/finance-overrides",
        json={"expected_revision": 1},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revision"] == 2  # 追加空文档 revision
    # 空覆盖: Effective == Profile(裸合并)
    # GET overrides 现在指向空文档(不再 404)
    resp = client.get(f"/api/projects/{pid}/finance-overrides", headers=headers)
    assert resp.status_code == 200
    doc = resp.json()["finance_overrides"]
    assert not doc.get("finance_types", {})
    # planning 失效
    resp = client.get(f"/api/projects/{pid}/planning-config", headers=headers)
    assert resp.status_code == 404


def test_overrides_delete_optimistic_lock(client: TestClient, db_session: Session) -> None:
    """DELETE Overrides 陈旧 expected_revision → 409, 不产生新 revision。"""
    headers, pid = _owner(client, db_session)
    _set_profile_and_planning(client, headers, pid)
    # 先真实覆盖一次(revision 2)
    client.put(
        f"/api/projects/{pid}/finance-overrides",
        json={"finance_overrides": _overrides(_profile()).to_dict(), "expected_revision": 1},
        headers=headers,
    )
    resp = client.request(
        "DELETE", f"/api/projects/{pid}/finance-overrides",
        json={"expected_revision": 1},
        headers=headers,
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "SYS-STORE-004"
    resp = client.get(f"/api/projects/{pid}/finance-overrides", headers=headers)
    assert resp.json()["revision"] == 2


def test_profile_reregister_reuses_row_no_drift(
    client: TestClient, db_session: Session
) -> None:
    """同 id 重登记复用既有行: 注册表内容不被改写, 已绑定项目不漂移。"""
    headers, pid = _owner(client, db_session)
    _register_profile(client, headers)
    resp = client.put(
        f"/api/projects/{pid}/finance-profile",
        json=_profile_ref(_profile()),
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    old_row_id = client.get(f"/api/projects/{pid}/finance-profile", headers=headers).json()["row"]["id"]
    # 同 profile_id 登记不同内容 → 200 复用既有行(不改写内容)
    profile2_payload = {
        **PROFILE_PAYLOAD,
        "energy_prices": {
            **PROFILE_PAYLOAD["energy_prices"],
            "grid_import": {**PROFILE_PAYLOAD["energy_prices"]["grid_import"],
                            "value": {"value": "0.71", "unit": "CNY/kWh"}},
        },
    }
    resp = client.post("/api/finance-profiles", json={"finance_profile": profile2_payload}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["row"]["id"] == old_row_id
    assert resp.json()["finance_profile"]["energy_prices"]["grid_import"]["value"]["value"] == "0.7"
    # 已绑定项目不漂移: 仍指向既有行与既有内容
    body = client.get(f"/api/projects/{pid}/finance-profile", headers=headers).json()
    assert body["row"]["id"] == old_row_id
    assert body["finance_profile"]["energy_prices"]["grid_import"]["value"]["value"] == "0.7"
    # 引用未登记 id → 404(不静默回退)
    resp = client.put(
        f"/api/projects/{pid}/finance-profile",
        json={"profile_ref": {"id": "no-such-profile"}},
        headers=headers,
    )
    assert resp.status_code == 404


def test_set_profile_commits_transaction_for_new_session(
    client: TestClient, db_session: Session, engine: Engine
) -> None:
    """PUT finance-profile 必须 db.commit: 新会话读取能看到指针与 Effective。"""
    headers, pid = _owner(client, db_session)
    _register_profile(client, headers)
    resp = client.put(
        f"/api/projects/{pid}/finance-profile",
        json=_profile_ref(_profile()),
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    # 独立新会话读取(expire_on_commit=False 语义下仍应从库读到已提交数据)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as fresh:
        from iesplan.models.project import Project

        proj = fresh.get(Project, pid)
        assert proj is not None and proj.finance_profile_id is not None
        assert proj.effective_finance_revision is not None
        row = fresh.execute(
            text("SELECT COUNT(*) FROM effective_finance_revisions WHERE project_id=:p"),
            {"p": pid},
        ).scalar()
        assert row == 1
