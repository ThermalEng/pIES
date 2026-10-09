"""0.12-F2 全期连续时域真求解验收：N=2 跨闰年、年度输入不同。

背景：F1 把 ``evaluation_years`` 接受并冻结为快照顶层确认块，但若
Worker 仍只解第一年，N=2 任务可“提交成功 + completed”形成假成功路径。
本文件证明一次整体求解真实覆盖全期，而非单年结果冒充：

- 门控级：``validate_project_export`` 全管线签发 data_full 绑定（起始年
  2023 + 已确认 2 年），``materialize_non_predictive_series`` 物化
  17544 点（2023 平年 8760 + 2024 闰年 8784），年界值正确；截短文件在
  提交闸门以 ``csv_full_length`` 阻断（必要输入边界唯一校验）。
- 执行级：真实 Worker 链（HTTP 提交 → 领取 → 本地 HiGHS 求解 → 证据封存）
  跑 N=2 任务至 completed；封存解变量按 ``__t{step}`` 后缀分年汇总，
  第一年与第二年服务能量分别等于各自年度输入（48.0×8760 与 96.0×8784），
  步数跨度 17544。单年求解或首年复制均无法同时满足这两组等式。
- 空序列级：零预定义绑定但有 SoC 时域耦合的项目，N=2 仍建 17544 步
  整体模型/Bundle（集合携年限，不回退单年），跨年耦合约束行可查。

财务逐年归集本 F2 切片未覆盖，仍属 0.12（路线图 0.12-4），本文件不断言它。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest  # type: ignore[import-untyped]
from assembly_fixture import declare_compute_device  # noqa: E402
from auth_helpers import make_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from test_assembly_artifact import (  # noqa: E402
    _EXPORT_FINANCE,
    _EXPORT_PLANNING,
    _EXPORT_TARIFF,
    _finance_snapshots,
)
from test_tasks_api import (  # noqa: E402
    _CONFIG_BODY,
    _bind_and_freeze,
    _declare_assembly_sections,
    _declare_finance,
    _h,
    _set_explicit_calc,
    _submit_task,
)

from iesplan.api import projects as projects_api  # noqa: E402
from iesplan.api import tasks as tasks_api  # noqa: E402
from iesplan.api.config import config_router  # noqa: E402
from iesplan.api.deps import get_request_db  # noqa: E402
from iesplan.application import worker as worker_app  # noqa: E402
from iesplan.assembly.contracts import (  # noqa: E402
    ValidatedAssemblyArtifact,
    ValidationReceipt,
)
from iesplan.assembly.validator import validate_project_export  # noqa: E402
from iesplan.bootstrap import assemble_compute_worker  # noqa: E402
from iesplan.computation import (  # noqa: E402
    HIGHS_MILP_PROVIDER,
    CalculationConfig,
    build_project_model,
)
from iesplan.computation.sequences import (  # noqa: E402
    materialize_non_predictive_series,
)
from iesplan.db import Base  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.results import persistence as results_store  # noqa: E402
from iesplan.storage import get_object, put_object  # noqa: E402
from iesplan.tasks import queue  # noqa: E402
from iesplan.tasks.persistence import Task  # noqa: E402
from iesplan.worker import runner  # noqa: E402

START_YEAR = 2023
YEARS = 2
YEAR1_STEPS = 8760
YEAR2_STEPS = 8784
FULL_STEPS = YEAR1_STEPS + YEAR2_STEPS
YEAR1_KW = 48.0
YEAR2_KW = 96.0


def _full_csv(n1: int, v1: float, n2: int, v2: float) -> bytes:
    """全期 device-data 字节：文件顺序即 step 顺序，年界可区分。"""
    lines = [
        "# schema: ies.device-data",
        "# schema_version: 2.1.0",
        "# dataset_id: load_full_two_years",
        "# device_id: ies.device.electric_load",
        "# source_mode: data_full",
        "# resolution: 1h",
        "# unit.electricity_demand: kW",
        "step,electricity_demand",
    ]
    lines += [f"{i},{v1}" for i in range(n1)]
    lines += [f"{n1 + i},{v2}" for i in range(n2)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _gate_full_artifact(raw: bytes):
    """经全管线签发含 data_full 绑定的产物（起始年 2023，已确认 2 年）。"""
    contents = {
        "data/ds9.data.csv": {
            "object_id": "obj-full-9",
            "media_type": "text/csv",
            "data": raw,
        }
    }
    content = {
        "name": "decl_gate_full",
        "model": {
            "devices": [
                {
                    "name": "grid1",
                    "device_type": "ies.device.grid_connection",
                    "kind": "existing",
                    "params": {"max_import_power_kw": 800, "max_export_power_kw": 200},
                },
                {
                    "name": "hp1",
                    "device_type": "ies.device.heat_pump",
                    "kind": "new",
                    "params": {"rated_heat_kw": 600, "cop": 3.5},
                },
                {
                    "name": "load1",
                    "device_type": "ies.device.electric_load",
                    "kind": "new",
                    "params": {
                        "peak_power_kw": 200,
                        "electricity_demand": {"mode": "data_full", "dataset_version_id": 9},
                    },
                },
            ],
            "connections": [
                {
                    "name": "c-grid-hp",
                    "from_device": "grid1",
                    "from_port": "electricity_import",
                    "to_device": "hp1",
                    "to_port": "electricity_in",
                },
                {
                    "name": "c-grid-load",
                    "from_device": "grid1",
                    "from_port": "electricity_import",
                    "to_device": "load1",
                    "to_port": "electricity_in",
                },
            ],
        },
        "project_baseline": {
            "resolution": "1h",
            "start_year": START_YEAR,
            "scenario_mode": "single",
        },
    }
    return validate_project_export(
        content,
        finance=dict(_EXPORT_FINANCE),
        finance_binding={
            "instances": {
                "grid1": {"type": "none"},
                "hp1": {
                    "type": "costed",
                    "finance_type": "heat_pump_system",
                    "drivers": {
                        "heat_kw": {"property": "rated_heat_kw"},
                        "consumed_kwh": {"interface": "electricity_in", "aggregation": "integrate_positive"},
                    },
                },
                "load1": {"type": "none"},
            }
        },
        tariff_bindings=dict(_EXPORT_TARIFF),
        finance_snapshots=_finance_snapshots(),
        planning=dict(_EXPORT_PLANNING),
        dataset_contents=contents,
        evaluation_years=YEARS,
    )


class TestFullHorizonGate:
    def test_declared_full_binding_freezes_and_materializes(self, devices) -> None:
        """data_full 声明 → 全管线签发 → 物化 17544 点，年界值正确。"""
        raw = _full_csv(YEAR1_STEPS, YEAR1_KW, YEAR2_STEPS, YEAR2_KW)
        result = _gate_full_artifact(raw)
        assert result.ok, [(d.code, d.params) for d in result.diagnostics if d.blocking]
        assert result.artifact is not None
        doc = json.loads(result.artifact.canonical_text)
        binding = doc["devices"]["load1"]["predefined_interfaces"]["electricity_demand"]
        assert binding["mode"] == "data_full"
        assert binding["content"]["object_id"] == "obj-full-9"
        materialized = materialize_non_predictive_series(
            result.artifact, {"obj-full-9": raw}, evaluation_years=YEARS
        )
        assert not materialized.pending_predict
        series = [s for s in materialized.series if s.device == "load1"]
        assert len(series) == 1
        assert series[0].mode == "data_full"
        assert series[0].point_count == FULL_STEPS
        assert series[0].values[YEAR1_STEPS - 1] == YEAR1_KW
        assert series[0].values[YEAR1_STEPS] == YEAR2_KW

    def test_truncated_full_file_blocked_at_gate(self, devices) -> None:
        """截短全期文件（仅首年点数）→ 提交闸门以 csv_full_length 阻断。"""
        raw = _full_csv(YEAR1_STEPS, YEAR1_KW, 0, YEAR2_KW)
        result = _gate_full_artifact(raw)
        assert not result.ok
        blocking = [d for d in result.diagnostics if d.blocking]
        assert any(d.params.get("reason") == "csv_full_length" for d in blocking)


@pytest.fixture()
def devices():
    from iesplan.devices import init_registry  # noqa: E402

    init_registry()
    yield


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """模块级 SQLite 内存引擎（与链测试同一形状）。"""
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
    """真实计算执行面（与 Worker 进程同一构成）。"""
    ctx = assemble_compute_worker()
    plane = ctx.compute_execution_plane()
    assert plane is not None
    return plane


def _craft_full_version(
    db: Session, client: TestClient, pid: int, user, name: str, raw: bytes
) -> int:
    """域级创建全期版本（上传只收通用时间戳格式，由域级直接建版本）。"""
    from iesplan.dataset import persistence as dataset_store  # noqa: E402

    resp = client.post(
        f"/api/projects/{pid}/datasets",
        json={"name": name, "source_category": "user_upload", "license": "CC-BY-4.0"},
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text
    ds_id = resp.json()["dataset"]["id"]
    handle = put_object(db, raw, "text/csv", source_category="test", purpose="full-horizon")
    version = dataset_store.create_version(
        db, dataset_id=ds_id, timeline="hourly", fixed_utc_offset_minutes=480,
        fields={"electricity_demand": {"unit": "kW"}}, units={"electricity_demand": "kW"},
        created_by=user.id, resolution="1h",
    )
    dataset_store.add_file(
        db, dataset_version_id=version.id, object_id=handle.id,
        file_kind="data", format="csv", row_count=FULL_STEPS, size_bytes=len(raw),
    )
    db.commit()
    return version.id


def _create_dated_project(client: TestClient, user, name: str, start_year: int) -> int:
    resp = client.post(
        "/api/projects",
        json={
            "name": name,
            "currency": "CNY",
            "baseline_resolution": "1h",
            "baseline_start_year": start_year,
            "baseline_scenario_mode": "single",
        },
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["project"]["id"]


def _prepare_full_project(client: TestClient, db: Session, user, raw: bytes) -> int:
    """准备 N=2 全期项目：grid1 + data_full 负荷，购电费最小化目标。"""
    from test_worker_compute_chain import PLANNING_BODY  # noqa: E402

    from iesplan.application import configuration as config_cases  # noqa: E402

    pid = _create_dated_project(client, user, "全期求解测试项目", START_YEAR)
    _declare_finance(db, user, pid)
    vid = _craft_full_version(db, client, pid, user, "load1全期", raw)
    view = client.get(f"/api/projects/{pid}", headers=_h(client, user)).json()
    revision = view["draft"]["revision"]
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
                    "electricity_demand": {
                        "mode": "data_full",
                        "dataset_version_id": vid,
                    },
                },
                "position": {"x": 2.0, "y": 0.0},
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
    ]
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": revision, "commands": commands},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    revision = resp.json()["revision"]
    device_ref, revision = declare_compute_device(client, pid, _h(client, user))
    planning = dict(PLANNING_BODY)
    planning["objective"] = {
        "sense": "minimize",
        "expression": "grid_import__period_energy_purchase",
    }
    config_cases.save_planning_config(db, pid, planning, None, user.id)
    revision = _set_explicit_calc(client, pid, user, revision)
    instances = {device_ref: {"type": "none"}, "grid1": {"type": "none"}, "load1": {"type": "none"}}
    view = client.get(f"/api/projects/{pid}", headers=_h(client, user)).json()
    revision = _declare_assembly_sections(client, pid, user, view["draft"]["revision"], instances)
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={
            "expected_revision": revision,
            "commands": [{
                "id": "c-tariff-bind", "unit": "assembly",
                "type": "assembly.set_tariff_bindings",
                "payload": {"bindings": {
                    "grid_import": {
                        "price": "grid_import",
                        "instance": "grid1",
                        "interface": "electricity_import",
                        "aggregation": "integrate_positive",
                    }
                }},
            }],
        },
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    revision = resp.json()["revision"]
    _bind_and_freeze(client, pid, user, None, revision=revision)
    return pid


def _yearly_served(variables: list[dict], prefix: str) -> tuple[float, float, int]:
    """按 __t{step} 后缀分年汇总负荷服务功率；返回 (年一, 年二, 最大步数)。"""
    year1 = 0.0
    year2 = 0.0
    max_step = -1
    hits = 0
    for item in variables:
        name = str(item["name"])
        if not name.startswith(prefix):
            continue
        step = int(name.rsplit("__t", 1)[1])
        max_step = max(max_step, step)
        hits += 1
        if step < YEAR1_STEPS:
            year1 += float(item["value"])
        else:
            year2 += float(item["value"])
    assert hits == FULL_STEPS, f"负荷分步变量数 {hits} != 全期步数 {FULL_STEPS}"
    return year1, year2, max_step + 1


class TestFullHorizonTrueSolve:
    def test_two_year_solve_covers_both_annual_inputs(
        self, client: TestClient, db: Session, engine: Engine, plane
    ) -> None:
        """N=2 跨闰年真求解：一次整体求解同时满足两年各自的年度输入。

        若 Worker 只解第一年（8760 步）或复制首年结果，本测试必败：
        步数跨度不足 17544，或第二年汇总仍等于第一年输入。
        """
        from sqlalchemy import select  # noqa: E402

        user = make_user(db, "full_owner_1")
        raw = _full_csv(YEAR1_STEPS, YEAR1_KW, YEAR2_STEPS, YEAR2_KW)
        pid = _prepare_full_project(client, db, user, raw)
        rev_resp = client.put(
            f"/api/projects/{pid}/config",
            json={"calculation_config": dict(_CONFIG_BODY), "expected_revision": None},
            headers=_h(client, user),
        )
        assert rev_resp.status_code == 200, rev_resp.text
        rev = rev_resp.json()["revision"]
        status, body = _submit_task(
            client, pid, user, config={"config_revision": rev},
            evaluation={"evaluation_years": YEARS, "discount_rate": 0.08},
            idempotency_key="full-horizon-n2",
        )
        assert status == 201, body
        task_id = int(body["task"]["id"])
        claim = worker_app.acquire_attempt(db, task_id, "full-e2e")
        assert claim is not None
        db.commit()

        def factory() -> Session:
            return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()

        assert runner.run_task(factory, claim, worker_id="full-e2e", compute=plane) == "completed"
        task_row = db.execute(select(Task).where(Task.id == task_id)).scalar_one()
        assert task_row.status == "completed"
        assert task_row.business_outcome == "normal_completion"
        evidence = results_store.latest_evidence_for_task(db, task_id)
        assert evidence is not None
        sealed = json.loads(get_object(db, evidence.object_id).decode("utf-8"))
        solution = sealed["compute_result"]["outputs"]["solution"]
        year1, year2, span = _yearly_served(
            solution["variables"], "load1__electricity_in__t"
        )
        assert span == FULL_STEPS
        assert year1 == pytest.approx(YEAR1_KW * YEAR1_STEPS, rel=1e-6)
        assert year2 == pytest.approx(YEAR2_KW * YEAR2_STEPS, rel=1e-6)
        assert year2 != pytest.approx(year1, rel=1e-3)


STORE_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.store, names: {zh-CN: 储能, en-US: Store}}
properties:
  soc0: {value: 200, unit: kWh, valid_range: {minimum: 0, maximum: 1000}}
  cap_kwh: {value: 400, unit: kWh, valid_range: {minimum: 0, maximum: 1000}}
  step_h: {value: 1, unit: h, valid_range: {minimum: 0, maximum: 10}}
interfaces:
  charge: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
  discharge: {type: out, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations:
  variables:
    soc: {unit: kWh, valid_range: {minimum: 0, maximum: 1000}, initial: {property_ref: soc0}}
  relations:
    - id: soc_step
      expression: "soc[t] = soc[t-1] + charge[t] * step_h - discharge[t] * step_h"
"""


def _store_doc() -> dict:
    """无预定义接口的时域项目：储能 SoC 跨步耦合，但零预定义绑定。”
    注：首方目录暂无状态设备，此形态经定制设备模型可达；此处以规范
    canonical 直写（装配 UI 注册路径之外），只验收 Generator 契约。
    """
    return {
        "schema": "ies.assembly",
        "schema_version": "2.0.0",
        "assembly": {"id": "mini-store", "name": "无序列储能"},
        "project_baseline": {
            "resolution": "1h", "start_year": START_YEAR, "scenario_mode": "single",
        },
        "devices": {
            "store1": {
                "definition": {"id": "acme.device.store"},
                "asset_origin": "new",
                "properties": {},
                "predefined_interfaces": {},
            },
        },
        "connections": {},
        "finance": {
            "ref": {
                "kind": "object",
                "object_id": "obj-finance",
                "media_type": "application/yaml",
            },
            "profile_id": "cn-north-demo",
        },
        "finance_binding": {"instances": {"store1": {"type": "none"}}},
        "tariff_bindings": {
            "meter1": {
                "price": "power_import",
                "instance": "store1",
                "interface": "charge",
                "aggregation": "integrate_positive",
            },
        },
        "planning": {
            "objective": {
                "sense": "minimize",
                "expression": "meter1__period_energy_purchase",
            },
            "variables": {
                "pmax": {
                    "device_ref": "store1",
                    "parameter": "cap_kwh",
                    "lower_bound": "0",
                    "upper_bound": "500",
                    "unit": "kWh",
                },
            },
            "constraints": {},
        },
    }


def _step_of(name: str) -> int | None:
    head, sep, tail = name.rpartition("__t")
    return int(tail) if sep and tail.isdigit() else None


class TestEmptySeriesFullHorizon:
    def test_no_series_project_builds_two_year_model_and_bundle(self) -> None:
        """无预定义序列但有时域决策：N=2 仍建 17544 步整体模型/Bundle。

        空序列不再回退单年：SoC 状态变量覆盖末步 t17543，且存在同时
        引用 t8759 与 t8760 的跨年耦合约束行（非首年复制/拼接）。
        """
        from test_project_model import _contribution, _finance_payload  # noqa: E402

        from iesplan.finance import FinanceProfile, merge_effective  # noqa: E402

        artifact = ValidatedAssemblyArtifact(
            canonical_text=json.dumps(_store_doc()), receipt=ValidationReceipt(),
        )
        materialized = materialize_non_predictive_series(
            artifact, {}, evaluation_years=YEARS
        )
        assert materialized.series == ()
        assert materialized.pending_predict == ()
        assert materialized.evaluation_years == YEARS
        models = {"store1": _contribution(STORE_YAML)}
        payload = _finance_payload()
        payload["energy_prices"]["power_import"] = {
            "carrier": "electricity",
            "direction": "purchase",
            "kind": "constant",
            "value": {"value": "0.7", "unit": "CNY/kWh"},
        }
        finance = merge_effective(FinanceProfile.from_dict(payload), None)
        config = CalculationConfig(
            mode="planning",
            generator="ies.generator.highs_milp@1.0.0",
            solver="ies.solver.highs@1.7.2",
            precision="medium",
            time_limit_seconds=600,
            outputs=["solution"],
        )
        model, _units, _ = build_project_model(
            artifact, models, materialized, finance, config
        )
        names = [var.name for var in model.variables]
        steps = sorted(s for s in (_step_of(n) for n in names) if s is not None)
        assert steps[-1] == FULL_STEPS - 1
        assert len([n for n in names if n.startswith("store1__soc__t")]) == FULL_STEPS
        coupled = [
            row for row in model.constraints
            if any(
                _step_of(term[0][0]) == YEAR1_STEPS - 1
                for term in row.expression.terms
            ) and any(
                _step_of(term[0][0]) == YEAR1_STEPS
                for term in row.expression.terms
            )
        ]
        assert coupled, "SoC 跨年耦合约束缺失"
        bundle = HIGHS_MILP_PROVIDER.generate(
            artifact, models, materialized, finance, config
        )
        mps = bytes(bundle.files["input/problem.mps"])
        assert f"store1__soc__t{FULL_STEPS - 1}".encode() in mps
