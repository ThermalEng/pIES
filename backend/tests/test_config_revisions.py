"""公共财务配置与规划配置(0.6.5 事项 3)契约/领域/持久化/API 测试。

覆盖:
- core 契约: FinanceConfig/PlanningConfig 严格恢复、Decimal 纪律、revision
  确定性摘要、roundtrip(to_dict→from_dict 含 revision 校验)、validate 诊断;
- 领域规则: 币种一致性、规划引用 finance revision 一致性、设备引用格式;
- 持久化: 不可变 revision 追加、乐观锁 409、未保存 404、规划必须先有财务配置;
- API: GET/PUT 闭环、冲突/校验失败错误信封;
- 迁移 0005: 表与指针列创建、幂等。

测试环境: SQLite :memory:(StaticPool 共享连接) + tmp 对象存储目录。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
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
from iesplan.core.contracts import (  # noqa: E402
    FinanceConfig,
    FinanceConfigError,
    PlanningConfig,
    PlanningConfigError,
)
from iesplan.db import Base, get_db  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.migrations import _migrate_0005  # noqa: E402

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


# ---------------------------------------------------------------------------
# 契约: FinanceConfig
# ---------------------------------------------------------------------------


def test_finance_config_roundtrip_and_revision() -> None:
    config = FinanceConfig.from_dict(FINANCE_PAYLOAD)
    assert FinanceConfig.from_dict(config.to_dict()) == config
    assert len(config.revision) == 64
    again = FinanceConfig.from_dict(FINANCE_PAYLOAD)
    assert config.revision == again.revision
    assert config.revision != FinanceConfig.from_dict(
        {**FINANCE_PAYLOAD, "tax_rate": "0.3"}
    ).revision


def test_finance_config_rejects_float_and_nan() -> None:
    with pytest.raises(FinanceConfigError):
        FinanceConfig.from_dict({**FINANCE_PAYLOAD, "tax_rate": 0.25})
    with pytest.raises(FinanceConfigError):
        FinanceConfig.from_dict({**FINANCE_PAYLOAD, "tax_rate": "NaN"})


def test_finance_config_rejects_unknown_and_bad_values() -> None:
    with pytest.raises(FinanceConfigError):
        FinanceConfig.from_dict({**FINANCE_PAYLOAD, "objective": "minimize"})
    with pytest.raises(FinanceConfigError):
        FinanceConfig.from_dict({**FINANCE_PAYLOAD, "tax_rate": "1.5"})
    with pytest.raises(FinanceConfigError):
        FinanceConfig.from_dict({**FINANCE_PAYLOAD, "energy_prices": {"crypto": {"value": "1", "unit": "CNY/kWh"}}})
    with pytest.raises(FinanceConfigError):
        FinanceConfig.from_dict({**FINANCE_PAYLOAD, "currency": "EUR"})


def test_finance_config_rejects_tampered_revision() -> None:
    mapping = FinanceConfig.from_dict(FINANCE_PAYLOAD).to_dict()
    mapping["revision"] = "0" * 64
    with pytest.raises(FinanceConfigError):
        FinanceConfig.from_dict(mapping)


def test_finance_config_validate_diagnostics() -> None:
    diags = FinanceConfig.validate({**FINANCE_PAYLOAD, "currency": "EUR"})
    assert len(diags) == 1
    assert diags[0].code == "PROJ-FIN-001"
    assert FinanceConfig.validate(FINANCE_PAYLOAD) == []


def test_finance_domain_currency_mismatch() -> None:
    from iesplan.finance.contracts import validate_finance_domain

    config = FinanceConfig.from_dict(FINANCE_PAYLOAD)
    bad = FinanceConfig.from_dict(
        {
            **FINANCE_PAYLOAD,
            "devices": {
                "heat_pump_1": {
                    "unit_investment": {"value": "1800", "unit": "USD/kW"},
                    "fixed_om_rate": "0.02",
                    "variable_om": {"value": "0.03", "unit": "CNY/kWh"},
                },
            },
        }
    )
    diags = validate_finance_domain(bad)
    assert any("USD/kW" in (d.params.get("detail") or "") for d in diags)
    assert validate_finance_domain(config) == []


# ---------------------------------------------------------------------------
# 契约: PlanningConfig
# ---------------------------------------------------------------------------


def test_planning_config_roundtrip_and_revision() -> None:
    finance_rev = FinanceConfig.from_dict(FINANCE_PAYLOAD).revision
    config = PlanningConfig.from_dict(_planning_payload(finance_rev))
    assert PlanningConfig.from_dict(config.to_dict()) == config
    assert len(config.revision) == 64


def test_planning_config_rejects_invalid_shapes() -> None:
    finance_rev = FinanceConfig.from_dict(FINANCE_PAYLOAD).revision
    with pytest.raises(PlanningConfigError):
        PlanningConfig.from_dict(
            {**_planning_payload(finance_rev), "finance_revision": "zz"}
        )
    with pytest.raises(PlanningConfigError):
        PlanningConfig.from_dict(
            {**_planning_payload(finance_rev), "objective": {"sense": "max", "expression": "x"}}
        )
    with pytest.raises(PlanningConfigError):
        PlanningConfig.from_dict(
            {**_planning_payload(finance_rev), "variables": {}}
        )
    with pytest.raises(PlanningConfigError):
        PlanningConfig.from_dict(
            {
                **_planning_payload(finance_rev),
                "constraints": {
                    "c1": {"type": "magic", "expression": "x", "enabled": True}
                },
            }
        )


def test_planning_domain_device_ref_and_consistency() -> None:
    from iesplan.finance.contracts import check_finance_revision
    from iesplan.planning.contracts import validate_planning_domain

    finance = FinanceConfig.from_dict(FINANCE_PAYLOAD)
    config = PlanningConfig.from_dict(_planning_payload(finance.revision))
    assert validate_planning_domain(config) == []
    assert check_finance_revision(config, finance) == []

    bad = PlanningConfig.from_dict(
        {**_planning_payload(finance.revision), "finance_revision": "0" * 64}
    )
    assert check_finance_revision(bad, finance)
    bad_ref = PlanningConfig.from_dict(
        {
            **_planning_payload(finance.revision),
            "variables": {"v": {"device_ref": "Bad Ref!", "parameter": "cap"}},
        }
    )
    assert validate_planning_domain(bad_ref)


# ---------------------------------------------------------------------------
# 持久化/服务
# ---------------------------------------------------------------------------


def test_service_save_and_get_finance_config(db_session: Session) -> None:
    from iesplan.services import config_revisions as svc

    user = make_user(db_session, "cfg_svc")
    project = svc._get_project(db_session, _mk_project(db_session, user, "svc-项目"))
    row, revision = svc.save_finance_config(
        db_session, project.id, FINANCE_PAYLOAD, None, user.id
    )
    assert revision == 1
    assert row.content_sha256 == FinanceConfig.from_dict(FINANCE_PAYLOAD).revision
    config, cur, _ = svc.get_finance_config(db_session, project.id)
    assert cur == 1 and config.currency == "CNY"


def _mk_project(db_session: Session, user, name: str) -> int:
    from iesplan.core.contracts import ProjectBaseline
    from iesplan.models.project import Project

    project = Project(
        name=name,
        owner_id=user.id,
        created_by=user.id,
        baseline_resolution="1h",
        baseline_leap_year=False,
        baseline_scenario_mode="single",
        baseline_sha256=ProjectBaseline(
            resolution="1h", leap_year=False, scenario_mode="single"
        ).digest(),
    )
    db_session.add(project)
    db_session.flush()
    return project.id


def test_service_stale_revision_conflict(db_session: Session) -> None:
    from iesplan.core.errors import ConflictError
    from iesplan.services import config_revisions as svc

    user = make_user(db_session, "cfg_conflict")
    pid = _mk_project(db_session, user, "conflict-项目")
    svc.save_finance_config(db_session, pid, FINANCE_PAYLOAD, None, user.id)
    with pytest.raises(ConflictError):
        svc.save_finance_config(db_session, pid, FINANCE_PAYLOAD, None, user.id)


def test_service_planning_requires_finance_and_same_revision(db_session: Session) -> None:
    from iesplan.core.errors import AppError
    from iesplan.services import config_revisions as svc

    user = make_user(db_session, "cfg_plan")
    pid = _mk_project(db_session, user, "plan-项目")
    # 未保存财务配置 → 400(PROJ-PLAN-002)
    with pytest.raises(AppError) as excinfo:
        svc.save_planning_config(
            db_session, pid, _planning_payload("0" * 64), None, user.id
        )
    assert excinfo.value.code == "PROJ-PLAN-002"
    # 保存财务配置后, 引用错误 revision → 400(PROJ-PLAN-002)
    svc.save_finance_config(db_session, pid, FINANCE_PAYLOAD, None, user.id)
    with pytest.raises(AppError) as excinfo:
        svc.save_planning_config(
            db_session, pid, _planning_payload("0" * 64), None, user.id
        )
    assert excinfo.value.code == "PROJ-PLAN-002"
    # 引用当前 revision → 成功
    finance, _, _ = svc.get_finance_config(db_session, pid)
    row, revision = svc.save_planning_config(
        db_session, pid, _planning_payload(finance.revision), None, user.id
    )
    assert revision == 1
    assert row.finance_revision == finance.revision


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_api_finance_config_flow(client: TestClient, db_session: Session) -> None:
    headers, pid = _owner(client, db_session)
    # 未保存 → 404
    resp = client.get(f"/api/projects/{pid}/finance-config", headers=headers)
    assert resp.status_code == 404
    # 保存 revision 1
    resp = client.put(
        f"/api/projects/{pid}/finance-config",
        json={"finance_config": FINANCE_PAYLOAD, "expected_revision": None},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revision"] == 1
    assert body["finance_config"]["revision"] == FinanceConfig.from_dict(
        FINANCE_PAYLOAD
    ).revision
    # GET 一致
    resp = client.get(f"/api/projects/{pid}/finance-config", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["revision"] == 1
    # 非法配置 → 400 标准错误信封
    resp = client.put(
        f"/api/projects/{pid}/finance-config",
        json={"finance_config": {**FINANCE_PAYLOAD, "currency": "EUR"}, "expected_revision": 1},
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PROJ-FIN-001"
    # 陈旧 revision → 409
    resp = client.put(
        f"/api/projects/{pid}/finance-config",
        json={"finance_config": FINANCE_PAYLOAD, "expected_revision": 1},
        headers=headers,
    )
    assert resp.status_code == 200  # 正常追加 revision 2
    assert resp.json()["revision"] == 2
    resp = client.put(
        f"/api/projects/{pid}/finance-config",
        json={"finance_config": FINANCE_PAYLOAD, "expected_revision": 1},
        headers=headers,
    )
    assert resp.status_code == 409


def test_api_planning_config_flow(client: TestClient, db_session: Session) -> None:
    headers, pid = _owner(client, db_session)
    # 无财务配置 → 400(PROJ-PLAN-002)
    resp = client.put(
        f"/api/projects/{pid}/planning-config",
        json={"planning_config": _planning_payload("0" * 64), "expected_revision": None},
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PROJ-PLAN-002"
    # 保存财务配置后引用错误 revision → 400(PROJ-PLAN-002)
    client.put(
        f"/api/projects/{pid}/finance-config",
        json={"finance_config": FINANCE_PAYLOAD, "expected_revision": None},
        headers=headers,
    )
    finance = client.get(f"/api/projects/{pid}/finance-config", headers=headers).json()
    resp = client.put(
        f"/api/projects/{pid}/planning-config",
        json={"planning_config": _planning_payload("0" * 64), "expected_revision": None},
        headers=headers,
    )
    assert resp.status_code == 400
    # 正确引用 → 成功, GET 一致
    resp = client.put(
        f"/api/projects/{pid}/planning-config",
        json={
            "planning_config": _planning_payload(finance["finance_config"]["revision"]),
            "expected_revision": None,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 1
    resp = client.get(f"/api/projects/{pid}/planning-config", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["planning_config"]["finance_revision"] == finance["finance_config"]["revision"]


# ---------------------------------------------------------------------------
# 迁移 0005
# ---------------------------------------------------------------------------


def test_migration_0005_creates_tables_and_pointers() -> None:
    eng = create_engine("sqlite+pysqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        _migrate_0005(conn)
        _migrate_0005(conn)  # 幂等
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).all()
        }
        assert {"finance_configs", "planning_configs"} <= tables
        proj_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(projects)")).all()}
        assert {"finance_revision", "planning_revision"} <= proj_cols
    eng.dispose()
