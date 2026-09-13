"""Wave 2 W2-A: application/configuration 用例测试。

覆盖：
- 用例可独立执行并提交事务（新会话可读回写入）；
- 收敛后行为一致（旧 services.config_revisions 已删除，对照基准即用例自身
  在独立项目上的运行：Profile 登记/引用、Overrides 保存、Effective 生成、
  Planning 保存、乐观锁）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")

import pytest  # noqa: E402
from auth_helpers import make_user  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan.application.configuration import revisions as config_uc  # noqa: E402
from iesplan.application.projects import lifecycle as projects_uc  # noqa: E402
from iesplan.config import settings  # noqa: E402
from iesplan.core.errors import ConflictError, NotFoundError  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.finance import FinanceProfile  # noqa: E402


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
def db_session(engine: Engine, tmp_path: Path) -> Iterator[Session]:
    settings.data_dir = tmp_path
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


def _new_session(engine: Engine) -> Session:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return factory()


BASELINE = {
    "baseline_resolution": "1h",
    "baseline_leap_year": False,
    "baseline_scenario_mode": "single",
}

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


def test_register_set_profile_and_commit(engine: Engine, db_session: Session) -> None:
    owner = make_user(db_session, "w2a_cfg_owner1")
    project = projects_uc.create_project(db_session, owner, "W2A 配置项目", **BASELINE)

    row, profile = config_uc.register_finance_profile(db_session, PROFILE_PAYLOAD, owner.id)
    assert profile.profile_id == "cn-north-demo"
    # 同 id 重登记复用既有行
    row2, _ = config_uc.register_finance_profile(db_session, PROFILE_PAYLOAD, owner.id)
    assert row2.id == row.id

    # Planning 在 Effective 生成前拒绝
    with pytest.raises(config_uc.InvalidRequestError):
        config_uc.save_planning_config(db_session, project.id, _planning_payload(), None, owner.id)

    rev, eff_row, effective = config_uc.set_project_finance_profile(
        db_session, project.id, "cn-north-demo", owner.id
    )
    assert rev == 1
    assert effective.profile_id == "cn-north-demo"

    # 独立执行并提交事务：新会话可读回
    with _new_session(engine) as fresh:
        eff, eff_rev, _ = config_uc.get_effective_finance_config(fresh, project.id)
        assert eff_rev == eff_row.revision
        assert eff.to_dict() == effective.to_dict()
        profiles = config_uc.list_finance_profiles(fresh)
        assert [p["profile_id"] for p in profiles] == ["cn-north-demo"]


def test_behavior_repeatable_consistent(engine: Engine, db_session: Session) -> None:
    owner = make_user(db_session, "w2a_cfg_owner2")
    p_new = projects_uc.create_project(db_session, owner, "W2A 配置新", **BASELINE)
    p_old = projects_uc.create_project(db_session, owner, "W2A 配置旧", **BASELINE)

    config_uc.register_finance_profile(db_session, PROFILE_PAYLOAD, owner.id)
    profile = FinanceProfile.from_dict(PROFILE_PAYLOAD)

    new_rev, _new_eff_row, new_eff = config_uc.set_project_finance_profile(
        db_session, p_new.id, "cn-north-demo", owner.id
    )
    old_rev, _old_eff_row, old_eff = config_uc.set_project_finance_profile(
        db_session, p_old.id, "cn-north-demo", owner.id
    )
    db_session.commit()
    assert (new_rev, new_eff.to_dict()) == (old_rev, old_eff.to_dict())

    # Overrides 保存对照
    new_orev, _, new_eff2 = config_uc.save_finance_overrides(
        db_session, p_new.id, _overrides_payload(profile), new_rev, owner.id
    )
    old_orev, _, old_eff2 = config_uc.save_finance_overrides(
        db_session, p_old.id, _overrides_payload(profile), old_rev, owner.id
    )
    db_session.commit()
    assert new_orev == old_orev
    assert new_eff2.to_dict() == old_eff2.to_dict()

    # Planning 保存对照
    new_plan_row, new_plan_rev = config_uc.save_planning_config(
        db_session, p_new.id, _planning_payload(), None, owner.id
    )
    old_plan_row, old_plan_rev = config_uc.save_planning_config(
        db_session, p_old.id, _planning_payload(), None, owner.id
    )
    db_session.commit()
    assert (new_plan_rev, new_plan_row.content) == (old_plan_rev, old_plan_row.content)

    with _new_session(engine) as fresh:
        _, new_got_rev, _ = config_uc.get_planning_config(fresh, p_new.id)
        assert new_got_rev == new_plan_rev


def test_optimistic_lock_and_delete_flow(db_session: Session) -> None:
    owner = make_user(db_session, "w2a_cfg_owner3")
    project = projects_uc.create_project(db_session, owner, "W2A 锁项目", **BASELINE)
    config_uc.register_finance_profile(db_session, PROFILE_PAYLOAD, owner.id)
    rev, _, _ = config_uc.set_project_finance_profile(db_session, project.id, "cn-north-demo", owner.id)
    profile = FinanceProfile.from_dict(PROFILE_PAYLOAD)

    # 过期 expected_revision → 409
    with pytest.raises(ConflictError):
        config_uc.save_finance_overrides(
            db_session, project.id, _overrides_payload(profile), rev + 99, owner.id
        )
    # 清空覆盖追加空文档 revision
    rev2, _, _ = config_uc.delete_finance_overrides(db_session, project.id, rev, owner.id)
    assert rev2 == rev + 1
    overrides, got_rev = config_uc.get_finance_overrides(db_session, project.id)
    assert got_rev == rev2
    assert overrides is not None
    # 未引用 Profile 的项目读 Effective → 404
    project2 = projects_uc.create_project(db_session, owner, "W2A 空项目", **BASELINE)
    with pytest.raises(NotFoundError):
        config_uc.get_effective_finance_config(db_session, project2.id)
