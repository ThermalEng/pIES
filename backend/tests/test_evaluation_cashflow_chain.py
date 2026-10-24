"""0.12-T2 真实链验收: 提交 → 领取 → 整段 N 年求解 → 封存财务结果。

同一细胞: K=1、N=2、E=3, 付款 0.6/0.4, 年负荷不同致 R1 != R2,
``CF=[-.6C,-.4C,R1,R2]``; 维护只扣发生年; K0 全款 t0; 运行跨闰年。
零固定费(线性热泵)与合法必建固定费(custom 画像)分别真实闭环;
可选正固定费仍拒收(独立缺口, 不在本文件解决)。

独立手算: 测试内用 plain 求和从封存解变量重算年购电/耗电成本,
不断言实现自身的中间函数; NPV 同源复算; 付款金额精确相等。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest  # type: ignore[import-untyped]
from auth_helpers import make_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from test_full_horizon_solve import (  # noqa: E402
    _craft_full_version,
    _create_dated_project,
    _full_csv,
)
from test_tasks_api import (  # noqa: E402
    _bind_and_freeze,
    _declare_assembly_sections,
    _h,
    _set_explicit_calc,
    _submit_task,
)

from iesplan.api import projects as projects_api  # noqa: E402
from iesplan.api import tasks as tasks_api  # noqa: E402
from iesplan.api.config import config_router  # noqa: E402
from iesplan.api.deps import get_request_db  # noqa: E402
from iesplan.application import worker as worker_app  # noqa: E402
from iesplan.bootstrap import assemble_compute_worker  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.results import persistence as results_store  # noqa: E402
from iesplan.storage import get_object  # noqa: E402
from iesplan.tasks import queue  # noqa: E402
from iesplan.tasks.persistence import Task  # noqa: E402
from iesplan.worker import runner  # noqa: E402

RATE = 0.08
PRICE = 0.7

HP_BINDING = {
    "type": "costed",
    "finance_type": "heat_pump_system",
    "drivers": {
        "heat_kw": {"property": "rated_heat_kw"},
        "consumed_kwh": {"interface": "electricity_in", "aggregation": "integrate_positive"},
    },
}

HP_MANDATORY_BINDING = {
    "type": "costed",
    "finance_type": "hp_mandatory",
    "drivers": {
        "heat_kw": {"property": "rated_heat_kw"},
    },
}


def _hp_demo_profile() -> dict:
    """执行画像 = 标准测试画像 + 热泵分量(分量值逐字取样例有效快照)。"""
    import copy

    from test_config_revisions import PROFILE_PAYLOAD  # noqa: E402

    profile = copy.deepcopy(PROFILE_PAYLOAD)
    profile["profile"]["id"] = "t2-hp-demo"
    profile["finance_types"]["heat_pump_system"] = {
        "upfront_capex": {
            "linear": {"heat_kw": {"unit_cost": {"value": "2800", "unit": "CNY/kW"}}},
        },
        "annual_fixed_om": {
            "linear": {"heat_kw": {"unit_cost": {"value": "28", "unit": "CNY/kW·a"}}},
        },
        "period_variable_om": {
            "linear": {"consumed_kwh": {"unit_cost": {"value": "0.008", "unit": "CNY/kWh"}}},
        },
    }
    return profile


FIXED_PROFILE: dict = {
    "schema": "ies.finance-profile",
    "schema_version": "4.0.0",
    "profile": {
        "id": "t2-fixed-demo",
        "region": "CN-North",
        "currency": "CNY",
        "base_year": 2025,
        "price_basis": "tax_inclusive",
        "cost_method": "fixed_plus_linear",
    },
    "finance_types": {
        "hp_mandatory": {
            "upfront_capex": {
                "fixed": {"value": "900000", "unit": "CNY"},
                "linear": {"heat_kw": {"unit_cost": {"value": "5000", "unit": "CNY/kW"}}},
            },
            "annual_fixed_om": {
                "linear": {"heat_kw": {"unit_cost": {"value": "100", "unit": "CNY/kW/a"}}},
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


def _planning(objective: str) -> dict:
    return {
        "objective": {"sense": "minimize", "expression": objective},
        "variables": {
            "hp_cap": {
                "device_ref": "hp1",
                "parameter": "rated_heat_kw",
                "lower_bound": "50",
                "upper_bound": "1000",
                "unit": "kW",
            },
        },
        "constraints": {},
    }


def _prepare_t2_project(
    client: TestClient,
    db: Session,
    user,
    raw: bytes,
    start_year: int,
    hp_binding: dict,
    planning: dict,
    profile: dict | None = None,
    with_existing_hp: bool = False,
    demand_bindings: dict | None = None,
) -> int:
    """T2 执行项目: grid1 + data_full 负荷 + 必建热泵 + 购电计费绑定。

    ``with_existing_hp`` 另加一台存量计费热泵 hp_old(静态 600kW, 其
    FinanceType 含年固定费): 按公开资产口径只计可变费与能源, 不计沉
    没建设及固定 O&M, 运行必须成功。"""
    from test_tasks_api import _declare_finance  # noqa: E402

    from iesplan.application import configuration as config_cases  # noqa: E402

    pid = _create_dated_project(client, user, "T2 现金流测试项目", start_year)
    if profile is None:
        _declare_finance(db, user, pid)
    else:
        config_cases.register_finance_profile(db, dict(profile), user.id)
        config_cases.set_project_finance_profile(db, pid, profile["profile"]["id"], user.id)
    vid = _craft_full_version(db, client, pid, user, "load1全期", raw)
    view = client.get(f"/api/projects/{pid}", headers=_h(client, user)).json()
    commands = [
        {
            "id": "c-bind-load1",
            "unit": "dataset",
            "type": "dataset.bind",
            "payload": {"dataset_version_id": vid, "role": "main"},
        },
        {
            "id": "fixture-declare-grid",
            "unit": "model",
            "type": "model.upsert_device",
            "payload": {
                "name": "grid1",
                "device_type": "ies.device.grid_connection",
                "kind": "existing",
                "params": {"max_import_power_kw": 800, "max_export_power_kw": 200},
                "position": {"x": 0.0, "y": 0.0},
            },
        },
        {
            "id": "fixture-declare-load1",
            "unit": "model",
            "type": "model.upsert_device",
            "payload": {
                "name": "load1",
                "device_type": "ies.device.electric_load",
                "kind": "new",
                "params": {
                    "peak_power_kw": 200,
                    "electricity_demand": {"mode": "data_full", "dataset_version_id": vid},
                },
                "position": {"x": 2.0, "y": 0.0},
            },
        },
        {
            "id": "fixture-declare-hp1",
            "unit": "model",
            "type": "model.upsert_device",
            "payload": {
                "name": "hp1",
                "device_type": "ies.device.heat_pump",
                "kind": "new",
                "params": {"rated_heat_kw": 600, "cop": 3.5},
                "position": {"x": 1.0, "y": 1.0},
            },
        },
        {
            "id": "fixture-connect-load1",
            "unit": "model",
            "type": "model.upsert_connection",
            "payload": {
                "name": "c-grid-load1",
                "from_device": "grid1",
                "from_port": "electricity_import",
                "to_device": "load1",
                "to_port": "electricity_in",
            },
        },
        {
            "id": "fixture-connect-hp1",
            "unit": "model",
            "type": "model.upsert_connection",
            "payload": {
                "name": "c-grid-hp1",
                "from_device": "grid1",
                "from_port": "electricity_import",
                "to_device": "hp1",
                "to_port": "electricity_in",
            },
        },
    ]
    if with_existing_hp:
        commands += [
            {
                "id": "fixture-declare-hp-old",
                "unit": "model",
                "type": "model.upsert_device",
                "payload": {
                    "name": "hp_old",
                    "device_type": "ies.device.heat_pump",
                    "kind": "existing",
                    "params": {"rated_heat_kw": 600, "cop": 3.5},
                    "position": {"x": 1.0, "y": -1.0},
                },
            },
            {
                "id": "fixture-connect-hp-old",
                "unit": "model",
                "type": "model.upsert_connection",
                "payload": {
                    "name": "c-grid-hp-old",
                    "from_device": "grid1",
                    "from_port": "electricity_import",
                    "to_device": "hp_old",
                    "to_port": "electricity_in",
                },
            },
        ]
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": view["draft"]["revision"], "commands": commands},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    config_cases.save_planning_config(db, pid, planning, None, user.id)
    revision = _set_explicit_calc(client, pid, user, resp.json()["revision"])
    instances = {
        "grid1": {"type": "none"},
        "load1": {"type": "none"},
        "hp1": dict(hp_binding),
    }
    if with_existing_hp:
        instances["hp_old"] = dict(hp_binding)
    view = client.get(f"/api/projects/{pid}", headers=_h(client, user)).json()
    revision = _declare_assembly_sections(client, pid, user, view["draft"]["revision"], instances)
    billing_commands = [
        {
            "id": "c-tariff-bind",
            "unit": "assembly",
            "type": "assembly.set_tariff_bindings",
            "payload": {
                "bindings": {
                    "grid_import": {
                        "price": "grid_import",
                        "instance": "grid1",
                        "interface": "electricity_import",
                        "aggregation": "integrate_positive",
                    }
                }
            },
        },
    ]
    if demand_bindings is not None:
        billing_commands.append({
            "id": "c-demand-bind",
            "unit": "assembly",
            "type": "assembly.set_demand_bindings",
            "payload": {"bindings": demand_bindings},
        })
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={
            "expected_revision": revision,
            "commands": billing_commands,
        },
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    _bind_and_freeze(client, pid, user, None, revision=resp.json()["revision"])
    return pid


def _eval_k1n2(maintenance=()):
    return {
        "evaluation_years": 3,
        "construction_period": 1,
        "operation_years": 2,
        "discount_rate": RATE,
        "construction_payments": [{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.4}],
        "maintenance_expenses": list(maintenance),
    }


def _solve(client, db, engine, plane, pid, user, evaluation, key, expected_revision=None):
    from test_task_config_revision import _save_config  # noqa: E402

    rev = _save_config(client, pid, user, expected_revision=expected_revision)
    status, body = _submit_task(
        client,
        pid,
        user,
        config={"config_revision": rev},
        evaluation=evaluation,
        idempotency_key=key,
    )
    assert status == 201, body
    task_id = int(body["task"]["id"])
    claim = worker_app.acquire_attempt(db, task_id, "t2-e2e")
    assert claim is not None
    db.commit()

    def factory() -> Session:
        return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()

    assert runner.run_task(factory, claim, worker_id="t2-e2e", compute=plane) == "completed"
    task_row = db.execute(select(Task).where(Task.id == task_id)).scalar_one()
    assert task_row.status == "completed"
    assert task_row.business_outcome == "normal_completion"
    evidence = results_store.latest_evidence_for_task(db, task_id)
    assert evidence is not None
    return json.loads(get_object(db, evidence.object_id).decode("utf-8"))


def _year_sums(variables, prefix, bounds):
    """独立手算: 指定前缀变量按年界 plain 求和(与实现 SI 路径无关)。"""
    totals = [0.0 for _ in bounds]
    for item in variables:
        name = str(item["name"])
        if not name.startswith(prefix):
            continue
        step = int(name.rsplit("__t", 1)[1])
        for year, (start, end) in enumerate(bounds):
            if start <= step < end:
                totals[year] += float(item["value"])
                break
    return totals


def _check_financial(
    sealed,
    *,
    k,
    n,
    e,
    expected_c,
    fixed_rate,
    maint_by_year,
    payments,
    profile_id,
    bounds,
    variable_rate=None,
):
    """封存财务块的独立核对(手算重算 + 冻结来源 + 时点)。"""
    assert "financial_evaluation" in sealed, "已求解必须封存财务块"
    fin = sealed["financial_evaluation"]
    assert fin["status"] == "evaluated"
    assert fin["evaluation_years"] == e
    assert fin["currency"] == "CNY"
    assert fin["profile_id"] == profile_id
    assert fin["discount_rate"] == RATE
    solution = sealed["compute_result"]["outputs"]["solution"]
    variables = solution["variables"]
    components = solution["components"]

    capacity = next(float(v["value"]) for v in variables if v["name"] == "hp_cap")
    assert capacity == pytest.approx(50.0, rel=1e-6)
    upfront = components["hp1__upfront_capex"]
    assert upfront["unit"] == "CNY"
    assert fin["construction_total"] == str(Decimal(str(upfront["value"])))
    assert float(fin["construction_total"]) == pytest.approx(expected_c, rel=1e-6)
    fixed = components["hp1__annual_fixed_om"]
    assert fixed["unit"] == "CNY/a"
    assert float(fin["annual_fixed_om"]) == pytest.approx(fixed_rate * 50.0, rel=1e-6)

    purchase = [s * PRICE for s in _year_sums(variables, "grid1__electricity_import__t", bounds)]
    consumed = _year_sums(variables, "hp1__electricity_in__t", bounds)
    expected_r = []
    for j in range(n):
        variable = (consumed[j] * variable_rate) if variable_rate else 0.0
        expected_r.append(-purchase[j] - variable - fixed_rate * 50.0 - maint_by_year.get(k + j + 1, 0.0))

    rows = fin["evaluation"]["rows"]
    assert len(rows) == e + 1
    assert [r["year"] for r in rows] == list(range(e + 1))
    for j in range(n):
        net = Decimal(rows[k + j + 1]["net"])
        assert float(net) == pytest.approx(expected_r[j], rel=1e-6)
        assert Decimal(fin["annual_operation"][j]["net"]) == net
    r1, r2 = float(rows[k + 1]["net"]), float(rows[k + 2]["net"])
    assert r1 != pytest.approx(r2, rel=1e-3)

    total = Decimal(fin["construction_total"])
    for index, (t, ratio) in enumerate(payments):
        entry = fin["payments_applied"][index]
        assert (entry["t"], Decimal(entry["ratio"])) == (t, Decimal(str(ratio)))
    for t in range(k + 1):
        paid = [r for (tt, r) in payments if tt == t]
        expect = -sum(Decimal(str(r)) for r in paid) * total if paid else Decimal("0")
        assert float(rows[t]["net"]) == pytest.approx(float(expect), rel=1e-9)

    flows = [Decimal(r["net"]) for r in rows]
    npv = sum(f / (Decimal(str(1 + RATE)) ** t) for t, f in enumerate(flows))
    assert float(fin["evaluation"]["npv"]) == pytest.approx(float(npv), rel=1e-6)
    assert Decimal(rows[-1]["cumulative_discounted"]) == Decimal(fin["evaluation"]["npv"])
    return fin


@pytest.fixture()
def devices():
    from iesplan.devices import init_registry  # noqa: E402

    init_registry()
    yield


@pytest.fixture(scope="module")
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
    from iesplan.config import settings  # noqa: E402

    settings.data_dir = tmp_path
    app = create_app()
    app.include_router(projects_api.router)
    app.include_router(tasks_api.router)
    app.include_router(config_router)

    def _override_request_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_request_db] = _override_request_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture()
def plane():
    ctx = assemble_compute_worker()
    plane = ctx.compute_execution_plane()
    assert plane is not None
    return plane


BASE_PLANNING = _planning(
    "grid_import__period_energy_purchase + hp1__upfront_capex"
    " + 1 year * hp1__annual_fixed_om + hp1__period_variable_om"
)
MANDATORY_PLANNING = _planning(
    "grid_import__period_energy_purchase + hp1__upfront_capex + 1 year * hp1__annual_fixed_om"
)


class TestT2CashflowChain:
    def test_k1n2_construction_offset_cashflow(self, devices, client, db, engine, plane) -> None:
        """K1N2E3 真求解: CF=[-.6C,-.4C,R1,R2], R1 != R2, C=140000 线性必建。"""
        raw = _full_csv(8760, 48.0, 8784, 96.0)
        user = make_user(db, "t2_owner_a")
        pid = _prepare_t2_project(client, db, user, raw, 2023, HP_BINDING, BASE_PLANNING, _hp_demo_profile())
        sealed = _solve(client, db, engine, plane, pid, user, _eval_k1n2(), "t2-k1n2-a")
        fin = _check_financial(
            sealed,
            k=1,
            n=2,
            e=3,
            expected_c=140000.0,
            fixed_rate=28.0,
            maint_by_year={},
            payments=[(0, 0.6), (1, 0.4)],
            profile_id="t2-hp-demo",
            bounds=((0, 8760), (8760, 17544)),
            variable_rate=0.008,
        )
        assert fin["evaluation"]["irr_status"] == "no_solution"
        assert fin["evaluation"]["irr"] is None

    def test_maintenance_deducted_only_at_event_year(self, devices, client, db, engine, plane) -> None:
        """同项目 + t=3 两笔维护(1000+2500.50): 合计只扣 CF[3] 一次,
        CF[2] 与无维护一致, 不限制年份唯一, 不丢项。"""
        raw = _full_csv(8760, 48.0, 8784, 96.0)
        owner = make_user(db, "t2_owner_b")
        pid = _prepare_t2_project(client, db, owner, raw, 2023, HP_BINDING, BASE_PLANNING, _hp_demo_profile())
        base = _solve(client, db, engine, plane, pid, owner, _eval_k1n2(), "t2-k1n2-base")
        maint = _solve(
            client,
            db,
            engine,
            plane,
            pid,
            owner,
            _eval_k1n2(
                [
                    {"year": 3, "amount": "1000.00", "currency": "CNY"},
                    {"year": 3, "amount": "2500.50", "currency": "CNY"},
                ]
            ),
            "t2-k1n2-maint",
            expected_revision=1,
        )
        base_rows = base["financial_evaluation"]["evaluation"]["rows"]
        maint_rows = maint["financial_evaluation"]["evaluation"]["rows"]
        assert float(maint_rows[2]["net"]) == pytest.approx(float(base_rows[2]["net"]), rel=1e-9)
        assert float(maint_rows[3]["net"]) == pytest.approx(float(base_rows[3]["net"]) - 3500.50, rel=1e-9)
        # 快照冻结已按 R2 规范化去尾零("1000.00"→"1000"), 两笔合计等值扣除。
        assert maint["financial_evaluation"]["annual_operation"][1]["maintenance"] == "3500.5"

    def test_k0_leap_first_mandatory_fixed(self, devices, client, db, engine, plane) -> None:
        """K0N2 闰年首年 + 必建固定费: CF=[-1150000,R1,R2], 年界 8784+8760。"""
        raw = _full_csv(8784, 60.0, 8760, 30.0)
        owner = make_user(db, "t2_owner_c")
        pid = _prepare_t2_project(
            client, db, owner, raw, 2024, HP_MANDATORY_BINDING, MANDATORY_PLANNING, FIXED_PROFILE
        )
        evaluation = {
            "evaluation_years": 2,
            "construction_period": 0,
            "operation_years": 2,
            "discount_rate": RATE,
            "construction_payments": [{"t": 0, "ratio": 1}],
            "maintenance_expenses": [],
        }
        sealed = _solve(client, db, engine, plane, pid, owner, evaluation, "t2-k0-fixed")
        _check_financial(
            sealed,
            k=0,
            n=2,
            e=2,
            expected_c=1150000.0,
            fixed_rate=100.0,
            maint_by_year={},
            payments=[(0, 1)],
            profile_id="t2-fixed-demo",
            bounds=((0, 8784), (8784, 17544)),
        )

    def test_existing_costed_counts_no_sunk_cost(self, devices, client, db, engine, plane) -> None:
        """存量计费热泵 hp_old(静态 600kW, 画像含年固定费): 运行成功,
        不计沉没建设(无 hp_old__upfront_capex)与存量固定 O&M(无
        hp_old__annual_fixed_om); C 仍为 140000, 年固定仍为 28×50。
        存量可变费与能源照常归集(空闲≈0, 不报错)。"""
        raw = _full_csv(8760, 48.0, 8784, 96.0)
        owner = make_user(db, "t2_owner_d")
        pid = _prepare_t2_project(
            client,
            db,
            owner,
            raw,
            2023,
            HP_BINDING,
            BASE_PLANNING,
            _hp_demo_profile(),
            with_existing_hp=True,
        )
        sealed = _solve(client, db, engine, plane, pid, owner, _eval_k1n2(), "t2-k1n2-existing")
        components = sealed["compute_result"]["outputs"]["solution"]["components"]
        assert "hp_old__upfront_capex" not in components
        assert "hp_old__annual_fixed_om" not in components
        _check_financial(
            sealed,
            k=1,
            n=2,
            e=3,
            expected_c=140000.0,
            fixed_rate=28.0,
            maint_by_year={},
            payments=[(0, 0.6), (1, 0.4)],
            profile_id="t2-hp-demo",
            bounds=((0, 8760), (8760, 17544)),
            variable_rate=0.008,
        )

    def test_d2_demand_monthly_full_worker_chain(
        self, devices, client, db, engine, plane
    ) -> None:
        """真实提交→Worker→HiGHS→Adapter→封存: K1N2、跨年闰年、月账单、NPV。"""
        profile = _hp_demo_profile()
        profile["profile"]["id"] = "d2-demand-full-chain"
        profile["demand_charges"] = {
            "monthly": {"rate": {"value": "20", "unit": "CNY/kW"}},
        }
        user = make_user(db, "d2_demand_owner")
        # Public data_full CSV: 2023/2024 (leap year), distinct calendar-month
        # peaks in one operating year and alternating non-first peak positions.
        # Month lengths imply candidate counts that are not powers of two.
        import calendar

        csv_lines = _full_csv(8760, 48.0, 8784, 96.0).decode("utf-8").splitlines()
        data_start = csv_lines.index("step,electricity_demand") + 1
        expected_peaks = []
        cursor = 0
        for year, baseline in ((2023, 48.0), (2024, 96.0)):
            for month in range(1, 13):
                steps = calendar.monthrange(year, month)[1] * 24
                assert steps & (steps - 1), "月份候选数必须不是2的幂"
                offset = 30 if month % 2 else steps - 2
                peak = baseline + 3.0 * month
                csv_lines[data_start + cursor + offset] = f"{cursor + offset},{peak}"
                expected_peaks.append(peak)
                cursor += steps
        assert cursor == 8760 + 8784
        variable_month_csv = ("\n".join(csv_lines) + "\n").encode("utf-8")
        pid = _prepare_t2_project(
            client, db, user,
            variable_month_csv,
            2023, HP_BINDING, BASE_PLANNING, profile,
            demand_bindings={
                "peak_meter": {
                    "charge": "monthly",
                    "instance": "grid1",
                    "interface": "electricity_import",
                },
            },
        )
        sealed = _solve(
            client, db, engine, plane, pid, user, _eval_k1n2(), "d2-demand-worker"
        )
        fin = sealed["financial_evaluation"]
        facts = sealed["compute_result"]["outputs"]["financial_facts"]
        assert fin["schema_version"] == "2.0.0"
        assert fin["construction_period"] == 1
        assert fin["operation_years"] == 2
        months = fin["monthly_demand"]
        assert len(months) == 24
        assert facts["monthly_demand"] == months
        assert [(r["calendar_year"], r["calendar_month"]) for r in months] == [
            (year, month) for year in (2023, 2024) for month in range(1, 13)
        ]
        for index, row in enumerate(months):
            peak = expected_peaks[index]
            assert row["billing_id"] == "peak_meter"
            assert row["charge_id"] == "monthly"
            assert row["peak_unit"] == "kW"
            assert row["financial_year"] == (2 if index < 12 else 3)
            assert row["rate"] == {"value": "20", "unit": "CNY/kW"}
            assert float(row["peak_power"]) == pytest.approx(peak, abs=1e-5)
            assert float(row["charge"]) == pytest.approx(peak * 20, abs=1e-5)
        annual_bills = (sum(expected_peaks[:12]) * 20, sum(expected_peaks[12:]) * 20)
        assert annual_bills[0] != annual_bills[1]
        for row, bill in zip(fin["annual_operation"], annual_bills, strict=True):
            assert float(row["demand_charge"]) == pytest.approx(bill, abs=1e-5)
            assert float(row["net"]) == pytest.approx(
                float(row["energy_net"]) - float(row["variable_om"])
                - float(row["fixed_om"]) - float(row["maintenance"]) - bill,
                abs=1e-4,
            )
            assert float(fin["annual_net_flows"][row["financial_year"]]) == pytest.approx(
                float(row["net"]), abs=1e-4,
            )
        assert fin["tax_cash"]["vat"] == []
        assert fin["tax_cash"]["income"] == []
        c = float(fin["construction_total"])
        assert float(fin["annual_net_flows"][0]) == pytest.approx(-0.6 * c)
        assert float(fin["annual_net_flows"][1]) == pytest.approx(-0.4 * c)
        hand_npv = sum(
            float(flow) / (1 + RATE) ** index
            for index, flow in enumerate(fin["annual_net_flows"])
        )
        assert float(fin["evaluation"]["npv"]) == pytest.approx(hand_npv, rel=1e-7)
