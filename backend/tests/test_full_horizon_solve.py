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
- 空序列级（Generator 契约级，非公开项目端到端）：零预定义绑定但有
  SoC 时域耦合的自定义设备，N=2 仍建 17544 步整体模型/Bundle（集合携
  年限，不回退单年），跨年耦合约束行可查；该设备未在首方注册，artifact
  未经公开闸门签发。

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
        "schema_version": "3.0.0",
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
                    "year_offset": 0,
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


class _RecordingGenerator:
    """测试记录型生成器外壳：记录每轮第三项集合时域，委托真实 HiGHS 生成。

    只观测 `materialized`（年限/各序列点数/pending），不改写、不拒收；
    ref/描述符/可用性全部委托，保证 registry 解析走同一引用。
    """

    def __init__(self) -> None:
        from iesplan.computation import HIGHS_MILP_PROVIDER as _inner  # noqa: E402

        self._inner = _inner
        self.calls: list[dict] = []

    @property
    def descriptor(self):
        return self._inner.descriptor

    @property
    def ref(self) -> str:
        return self._inner.ref

    def available(self) -> bool:
        return True

    def generate(self, artifact, models, materialized, finance, config):
        self.calls.append(
            {
                "evaluation_years": materialized.evaluation_years,
                "series_count": len(materialized.series),
                "point_counts": sorted({s.point_count for s in materialized.series}),
                "pending": len(materialized.pending_predict),
            }
        )
        return self._inner.generate(artifact, models, materialized, finance, config)


def _prepare_predict_n2(client: TestClient, db: Session, user) -> int:
    """N=2 预测项目（2023 起始跨闰年）：grid1 + 双 data_predict 负荷。

    复用预测能力测试的域级版本 crafting 与装配流程，只改起始年。
    """
    from test_prediction_capability import (  # noqa: E402
        _craft_history_version,
        _draft_put,
        _predict_history_csv,
    )
    from test_worker_compute_chain import PLANNING_BODY  # noqa: E402

    from iesplan.application import configuration as config_cases  # noqa: E402

    pid = _create_dated_project(client, user, "预测多轮全期测试项目", START_YEAR)
    _declare_finance(db, user, pid)
    vids = {}
    for dev, history in (("load1", [48.0, 49.0]), ("load2", [10.0, 11.0])):
        vids[dev] = _craft_history_version(
            db, client, pid, user, f"{dev}历史", _predict_history_csv(history)
        )
    view = client.get(f"/api/projects/{pid}", headers=_h(client, user)).json()
    revision = view["draft"]["revision"]
    commands = [
        {
            "id": f"c-bind-{dev}",
            "unit": "dataset",
            "type": "dataset.bind",
            "payload": {"dataset_version_id": vid, "role": "main"},
        }
        for dev, vid in vids.items()
    ]
    commands.append(
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
        }
    )
    for i, dev in enumerate(vids):
        commands.append(
            {
                "id": f"fixture-declare-{dev}",
                "unit": "model",
                "type": "model.upsert_device",
                "payload": {
                    "name": dev,
                    "device_type": "ies.device.electric_load",
                    "kind": "new",
                    "params": {
                        "peak_power_kw": 200,
                        "electricity_demand": {
                            "mode": "data_predict",
                            "dataset_version_id": vids[dev],
                            "target_type": "load",
                        },
                    },
                    "position": {"x": 2.0, "y": float(i)},
                },
            }
        )
        commands.append(
            {
                "id": f"fixture-connect-{dev}",
                "unit": "model",
                "type": "model.upsert_connection",
                "payload": {
                    "name": f"c-grid-{dev}",
                    "from_device": "grid1",
                    "from_port": "electricity_import",
                    "to_device": dev,
                    "to_port": "electricity_in",
                },
            }
        )
    revision = _draft_put(client, pid, user, revision, commands)
    device_ref, revision = declare_compute_device(client, pid, _h(client, user))
    planning = dict(PLANNING_BODY)
    planning["objective"] = {
        "sense": "minimize",
        "expression": "grid_import__period_energy_purchase",
    }
    config_cases.save_planning_config(db, pid, planning, None, user.id)
    revision = _set_explicit_calc(client, pid, user, revision)
    instances = {device_ref: {"type": "none"}, "grid1": {"type": "none"}}
    instances.update({dev: {"type": "none"} for dev in vids})
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


class _ShortPredictor:
    """测试短输出 predictor：只产 24 点，验预测公开输出边界不被伪装完成。"""

    SHORT_REF = "test.predictor.short@1.0.0"

    @property
    def descriptor(self):
        from iesplan.computation.predictors import (  # noqa: E402
            PredictorDescriptor,
        )

        return PredictorDescriptor(
            ref=self.SHORT_REF,
            accepts_any_target_type=True,
            needs_history=True,
            description="测试短输出：恒返 24 点",
        )

    def predict(
        self, *, device, interface, target_type, history, history_object_id,
        parameters, seed, point_count, previous=None,
    ):
        from iesplan.computation.predictors import PredictedTarget  # noqa: E402

        return PredictedTarget(
            device=device,
            interface=interface,
            target_type=target_type,
            values=tuple(1.0 for _ in range(24)),
            history_object_id=history_object_id,
            artifact=b"{}",
            receipt={},
        )


class TestPredictPhase2FullHorizon:
    def test_two_year_phase2_solves_full_horizon_each_round(
        self, client: TestClient, db: Session, engine: Engine, plane
    ) -> None:
        """N=2 阶段二多轮：快照评价年限驱动每轮完整 17544 步真求解。

        双预测目标触发阶段二（上限 3 轮）；记录外壳证明三轮 generate
        收到的第三项集合皆为完成态 17544 点；三轮目标值两两不同证明
        每轮都解了新模型；终轮封存解分年汇总等于终轮预测值。
        """
        from sqlalchemy import select  # noqa: E402
        from test_prediction_capability import (  # noqa: E402
            CHASE_REF,
            _ChasePredictor,
            _save_calc,
        )

        from iesplan.application import worker as worker_app  # noqa: E402
        from iesplan.application.tasks.submissions import submit_task  # noqa: E402
        from iesplan.computation import build_registry  # noqa: E402
        from iesplan.computation.predictors import (  # noqa: E402
            build_predictor_registry,
        )

        user = make_user(db, "predict_n2_owner")
        pid = _prepare_predict_n2(client, db, user)
        body = dict(_CONFIG_BODY)
        body["predictions"] = [
            {
                "target": {"instance": dev, "interface": "electricity_demand"},
                "algorithm": CHASE_REF,
                "parameters": {},
            }
            for dev in ("load1", "load2")
        ]
        body["convergence"] = {"tolerance": 0.001, "max_iterations": 3}
        rev = _save_calc(client, pid, user, body)
        chase = _ChasePredictor()
        recorder = _RecordingGenerator()
        gene_registry = build_registry((recorder,))
        pred_registry = build_predictor_registry((chase,))
        task, _flags = submit_task(
            db, user, pid, "optimization", config={"config_revision": rev},
            evaluation={"evaluation_years": YEARS, "discount_rate": 0.08},
            idempotency_key="predict-n2-phase2",
            generator_registry=gene_registry,
            predictor_registry=pred_registry,
        )
        n2_plane = worker_app.ComputeExecutionPlane(
            generator_registry=gene_registry,
            executor_registry=plane.executor_registry,
            adapter_registry=plane.adapter_registry,
            runtime=plane.runtime,
            predictor_registry=pred_registry,
        )
        claim = worker_app.acquire_attempt(db, task.id, "predict-n2-e2e")
        assert claim is not None
        db.commit()

        def factory() -> Session:
            return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()

        assert runner.run_task(factory, claim, worker_id="predict-n2-e2e", compute=n2_plane) == "completed"
        task_row = db.execute(select(Task).where(Task.id == task.id)).scalar_one()
        assert task_row.status == "completed"
        assert task_row.business_outcome == "normal_completion"
        assert len(recorder.calls) == 3
        for call in recorder.calls:
            assert call["evaluation_years"] == YEARS
            assert call["pending"] == 0
            assert call["series_count"] == 2
            assert call["point_counts"] == [FULL_STEPS]
        evidence = results_store.latest_evidence_for_task(db, task.id)
        assert evidence is not None
        sealed = json.loads(get_object(db, evidence.object_id).decode("utf-8"))
        prediction = sealed["prediction"]
        assert prediction["predict_target_count"] == 2
        assert prediction["prediction_rounds"] == 3
        assert prediction["solve_rounds"] == 3
        assert prediction["phase2_solve_rounds"] == 2
        assert prediction["converged"] is False
        solution = sealed["compute_result"]["outputs"]["solution"]
        round_objectives = [
            chase.calls[2]["previous_objective"],
            chase.calls[4]["previous_objective"],
            solution["objective_value"],
        ]
        assert len(set(round_objectives)) == 3
        for dev, call in (("load1", chase.calls[4]), ("load2", chase.calls[5])):
            year1, year2, span = _yearly_served(
                solution["variables"], f"{dev}__electricity_in__t"
            )
            assert span == FULL_STEPS
            assert year1 == pytest.approx(call["value"] * YEAR1_STEPS, rel=1e-6)
            assert year2 == pytest.approx(call["value"] * YEAR2_STEPS, rel=1e-6)


class TestGeneratorEmptySeriesHorizon:
    def test_empty_series_builds_two_year_model_and_bundle(self) -> None:
        """Generator 契约级空序列时域证明（非公开项目端到端）。

        本用例只证明 Generator 第三项输入为完成态空集合时，
        N=2 仍建 17544 步整体模型/Bundle：SoC 状态变量覆盖末步 t17543，
        且存在同时引用 t8759 与 t8760 的跨年耦合约束行（非首年复制/
        拼接）。所用自定义储能设备未在首方注册（公开装配闸门以
        ASM-REF-002 阻断未注册 definition，已探针取证），artifact 以
        规范 canonical 直写、未经公开闸门签发；此处不断言提交/求解链。
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


class TestShortPredictionRejected:
    def test_short_predictor_output_fails_task(
        self, client: TestClient, db: Session, engine: Engine, plane
    ) -> None:
        """短预测输出不得伪装完成：24 点输出进 17544 步全期即明确失败。

        拒绝来自权威全期点数与序列自身长度不变式（非新增校验）；
        失败收拢为 failed 终态，不落成功。
        """
        from sqlalchemy import select  # noqa: E402
        from test_prediction_capability import _save_calc  # noqa: E402

        from iesplan.application import worker as worker_app  # noqa: E402
        from iesplan.application.tasks.submissions import submit_task  # noqa: E402
        from iesplan.computation.predictors import (  # noqa: E402
            build_predictor_registry,
        )

        user = make_user(db, "predict_short_owner")
        pid = _prepare_predict_n2(client, db, user)
        body = dict(_CONFIG_BODY)
        body["predictions"] = [
            {
                "target": {"instance": dev, "interface": "electricity_demand"},
                "algorithm": _ShortPredictor.SHORT_REF,
                "parameters": {},
            }
            for dev in ("load1", "load2")
        ]
        body["convergence"] = {"tolerance": 0.001, "max_iterations": 2}
        rev = _save_calc(client, pid, user, body)
        pred_registry = build_predictor_registry((_ShortPredictor(),))
        task, _flags = submit_task(
            db, user, pid, "optimization", config={"config_revision": rev},
            evaluation={"evaluation_years": YEARS, "discount_rate": 0.08},
            idempotency_key="predict-n2-short",
            generator_registry=plane.generator_registry,
            predictor_registry=pred_registry,
        )
        short_plane = worker_app.ComputeExecutionPlane(
            generator_registry=plane.generator_registry,
            executor_registry=plane.executor_registry,
            adapter_registry=plane.adapter_registry,
            runtime=plane.runtime,
            predictor_registry=pred_registry,
        )
        claim = worker_app.acquire_attempt(db, task.id, "predict-n2-short")
        assert claim is not None
        db.commit()

        def factory() -> Session:
            return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()

        assert runner.run_task(factory, claim, worker_id="predict-n2-short", compute=short_plane) == "failed"
        task_row = db.execute(select(Task).where(Task.id == task.id)).scalar_one()
        assert task_row.status == "failed"


TOU_YEAR1_PRICE = "0.7"
TOU_YEAR2_PRICE = "0.35"


def _tou_price_bytes() -> bytes:
    """两年时变购电文件：2023 年 8760 点 0.7，2024 年 8784 点 0.35。"""
    lines = [
        "# schema: ies.price-series",
        "# schema_version: 1.0.0",
        "# resolution: 1h",
        "# unit: CNY/kWh",
        "step,price",
    ]
    lines += [f"{i},{TOU_YEAR1_PRICE}" for i in range(YEAR1_STEPS)]
    lines += [f"{YEAR1_STEPS + i},{TOU_YEAR2_PRICE}" for i in range(YEAR2_STEPS)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _declare_tou_finance(db: Session, user, pid: int, price_oid: str) -> None:
    """登记购电时变 Profile：grid_import 改 time_series（ref=真实 oid）。"""
    from test_config_revisions import PROFILE_PAYLOAD  # noqa: E402

    from iesplan.application import configuration as config_cases  # noqa: E402

    payload = json.loads(json.dumps(PROFILE_PAYLOAD))
    payload["energy_prices"]["grid_import"] = {
        "carrier": "electricity",
        "direction": "purchase",
        "kind": "time_series",
        "ref": price_oid,
        "series_meta": {
            "resolution": "1h",
            "point_count": FULL_STEPS,
            "unit": "CNY/kWh",
        },
    }
    config_cases.register_finance_profile(db, payload, user.id)
    config_cases.set_project_finance_profile(
        db, pid, payload["profile"]["id"], user.id
    )


def _prepare_tou_project(client: TestClient, db: Session, user, raw: bytes) -> tuple[int, str]:
    """准备 N=2 时变价格项目：购电 0.7/0.35 分年 + 售电 constant（零流量）。

    与 ``_prepare_full_project`` 同一负荷与拓扑，唯两处不同：财务
    Profile 的 grid_import 为时变引用（价格文件经公开上传口落盘，
    oid 填入 ref），计费绑定追加零流量的售电 constant 绑定。售电
    接口无电源：负荷母线平衡与进出口互斥把 export 钉在 0，无虚假收益。
    返回（项目 id，价格对象 oid）。
    """
    from test_device_data_upload import _make_dataset, _upload  # noqa: E402
    from test_worker_compute_chain import PLANNING_BODY  # noqa: E402

    from iesplan.application import configuration as config_cases  # noqa: E402

    pid = _create_dated_project(client, user, "时变价格测试项目", START_YEAR)
    ds_id = _make_dataset(client, pid, user, name="购电时变价格")
    status, resp = _upload(client, pid, user, ds_id, _tou_price_bytes())
    assert status == 201, resp
    price_oid = resp["quality_report"]["price_object_id"]
    _declare_tou_finance(db, user, pid, price_oid)
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
        "expression": (
            "grid_import__period_energy_purchase"
            " + grid_export__period_energy_sale"
        ),
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
                    },
                    "grid_export": {
                        "price": "pv_export",
                        "instance": "grid1",
                        "interface": "electricity_export",
                        "aggregation": "integrate_positive",
                    },
                }},
            }],
        },
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    revision = resp.json()["revision"]
    _bind_and_freeze(client, pid, user, None, revision=revision)
    return pid, price_oid


class TestTimeVaryingPriceTrueSolve:
    def test_two_year_tou_billing_matches_hand_calc(
        self, client: TestClient, db: Session, engine: Engine, plane
    ) -> None:
        """N=2 时变购电真求解：逐点计价两年分量与手算一致。

        购电价按年不同（0.7/0.35），售电 constant 零流量；若实现用
        平均价、单年价或总量先聚合，总目标必偏离手算：
        0.7*E1+0.35*E2。分年能量 E1/E2 取自解变量独立计量。
        """
        from sqlalchemy import select  # noqa: E402

        from iesplan.storage import list_refs  # noqa: E402

        user = make_user(db, "tou_owner_1")
        raw = _full_csv(YEAR1_STEPS, YEAR1_KW, YEAR2_STEPS, YEAR2_KW)
        pid, price_oid = _prepare_tou_project(client, db, user, raw)
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
            idempotency_key="tou-n2-billing",
        )
        assert status == 201, body
        task_id = int(body["task"]["id"])
        # 提交快照对实际使用的价格对象建 storage 引用（与快照同事务）。
        task_row = db.execute(select(Task).where(Task.id == task_id)).scalar_one()
        snapshot_id = task_row.calc_snapshot_id
        assert snapshot_id is not None
        refs = [
            (r.ref_type, r.ref_entity_id) for r in list_refs(db, price_oid)
        ]
        assert ("snapshot_ref", str(snapshot_id)) in refs
        # 相同输入复用快照：引用幂等，不重复计数。
        status2, _ = _submit_task(
            client, pid, user, config={"config_revision": rev},
            evaluation={"evaluation_years": YEARS, "discount_rate": 0.08},
            idempotency_key="tou-n2-billing-reuse",
        )
        assert status2 == 201, body
        refs = [
            (r.ref_type, r.ref_entity_id) for r in list_refs(db, price_oid)
        ]
        assert refs.count(("snapshot_ref", str(snapshot_id))) == 1
        claim = worker_app.acquire_attempt(db, task_id, "tou-e2e")
        assert claim is not None
        db.commit()

        def factory() -> Session:
            return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()

        assert runner.run_task(factory, claim, worker_id="tou-e2e", compute=plane) == "completed"
        # 本会话在提交阶段已读过任务行（快照引用断言），Worker 在独立
        # 会话提交求解完成；显式过期后重读，避免读到已开始事务的旧快照。
        db.expire_all()
        task_row = db.execute(select(Task).where(Task.id == task_id)).scalar_one()
        assert task_row.status == "completed"
        assert task_row.business_outcome == "normal_completion"
        evidence = results_store.latest_evidence_for_task(db, task_id)
        assert evidence is not None
        sealed = json.loads(get_object(db, evidence.object_id).decode("utf-8"))
        solution = sealed["compute_result"]["outputs"]["solution"]
        year1_kw, year2_kw, span = _yearly_served(
            solution["variables"], "grid1__electricity_import__t"
        )
        assert span == FULL_STEPS
        energy1 = year1_kw * 1.0
        energy2 = year2_kw * 1.0
        expect1 = float(TOU_YEAR1_PRICE) * energy1
        expect2 = float(TOU_YEAR2_PRICE) * energy2
        assert expect1 != pytest.approx(expect2, rel=1e-3)
        components = solution["components"]
        purchase = components["grid_import__period_energy_purchase"]["value"]
        sale = components["grid_export__period_energy_sale"]["value"]
        assert purchase == pytest.approx(expect1 + expect2, rel=1e-6)
        assert sale == pytest.approx(0.0, abs=1e-6)
        assert solution["objective_value"] == pytest.approx(
            expect1 + expect2, rel=1e-6
        )
        total_energy = energy1 + energy2
        assert purchase != pytest.approx(float(TOU_YEAR1_PRICE) * total_energy, rel=1e-3)
        assert purchase != pytest.approx(float(TOU_YEAR2_PRICE) * total_energy, rel=1e-3)
        avg_price = (
            float(TOU_YEAR1_PRICE) * YEAR1_STEPS + float(TOU_YEAR2_PRICE) * YEAR2_STEPS
        ) / FULL_STEPS
        assert purchase != pytest.approx(avg_price * total_energy, rel=1e-3)


#: F4b 分年投产真求解场景：光伏自发自用，首年不许建设、次年可建。
#: 负荷两年恒定 50kW；辐照恒 800W/m2、环境 25℃ 下每 kW 容量出力
#: 0.8kW；次年容量上界 50kW（最大出力 40kW < 负荷，恒可行）。
ANNUAL_LOAD_KW = 50.0
ANNUAL_IRR_W_M2 = 800.0
ANNUAL_TEMP_C = 25.0
ANNUAL_CAP_UPPER_KW = 50.0
ANNUAL_PV_KW_PER_KW = ANNUAL_IRR_W_M2 / 1000.0
ANNUAL_Y1_PV_KW = ANNUAL_CAP_UPPER_KW * ANNUAL_PV_KW_PER_KW
ANNUAL_Y0_IMPORT = ANNUAL_LOAD_KW * YEAR1_STEPS
ANNUAL_Y1_IMPORT = (ANNUAL_LOAD_KW - ANNUAL_Y1_PV_KW) * YEAR2_STEPS


def _annual_csv(dataset_id: str, device_id: str, column: str, unit: str, value: float) -> bytes:
    """两年恒定值的全期 device-data 字节（年界可区分，由长度保证）。"""
    lines = [
        "# schema: ies.device-data",
        "# schema_version: 2.1.0",
        f"# dataset_id: {dataset_id}",
        f"# device_id: {device_id}",
        "# source_mode: data_full",
        "# resolution: 1h",
        f"# unit.{column}: {unit}",
        f"step,{column}",
    ]
    lines += [f"{i},{value}" for i in range(FULL_STEPS)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _craft_series_version(
    db: Session, client: TestClient, pid: int, user, name: str,
    raw: bytes, field: str, unit: str,
) -> int:
    """域级创建单列全期版本（_craft_full_version 的通用形态）。"""
    from iesplan.dataset import persistence as dataset_store  # noqa: E402

    resp = client.post(
        f"/api/projects/{pid}/datasets",
        json={"name": name, "source_category": "user_upload", "license": "CC-BY-4.0"},
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text
    ds_id = resp.json()["dataset"]["id"]
    handle = put_object(db, raw, "text/csv", source_category="test", purpose="annual-build")
    version = dataset_store.create_version(
        db, dataset_id=ds_id, timeline="hourly", fixed_utc_offset_minutes=480,
        fields={field: {"unit": unit}}, units={field: unit},
        created_by=user.id, resolution="1h",
    )
    dataset_store.add_file(
        db, dataset_version_id=version.id, object_id=handle.id,
        file_kind="data", format="csv", row_count=FULL_STEPS, size_bytes=len(raw),
    )
    db.commit()
    return version.id


def _prepare_annual_project(client: TestClient, db: Session, user) -> int:
    """准备 F4b 分年项目：pv1 自发自用 + grid1 补足 + data_full 负荷。

    首年容量界 [0,0]、次年 [0,50]kW（取 finance.md 现实示例形态）；
    finance 只有线性成本（固定 upfront 为 0，正固定成本另行拒收）。
    """
    from iesplan.application import configuration as config_cases  # noqa: E402

    pid = _create_dated_project(client, user, "分年投产测试项目", START_YEAR)
    _declare_finance(db, user, pid)
    load_vid = _craft_series_version(
        db, client, pid, user, "load2y",
        _annual_csv("load_two_years", "ies.device.electric_load",
                    "electricity_demand", "kW", ANNUAL_LOAD_KW),
        "electricity_demand", "kW",
    )
    irr_vid = _craft_series_version(
        db, client, pid, user, "irr2y",
        _annual_csv("irr_two_years", "ies.device.pv",
                    "solar_irradiance", "W/m2", ANNUAL_IRR_W_M2),
        "solar_irradiance", "W/m2",
    )
    temp_vid = _craft_series_version(
        db, client, pid, user, "temp2y",
        _annual_csv("temp_two_years", "ies.device.pv",
                    "ambient_temperature", "°C", ANNUAL_TEMP_C),
        "ambient_temperature", "°C",
    )
    view = client.get(f"/api/projects/{pid}", headers=_h(client, user)).json()
    revision = view["draft"]["revision"]
    commands = [
        {
            "id": "c-bind-pv-irr",
            "unit": "dataset",
            "type": "dataset.bind",
            "payload": {"dataset_version_id": irr_vid, "role": "input"},
        },
        {
            "id": "c-bind-pv-temp",
            "unit": "dataset",
            "type": "dataset.bind",
            "payload": {"dataset_version_id": temp_vid, "role": "input"},
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
                        "dataset_version_id": load_vid,
                    },
                },
                "position": {"x": 2.0, "y": 0.0},
            },
        },
        {
            "id": "fixture-declare-pv1",
            "unit": "model",
            "type": "model.upsert_device",
            "payload": {
                "name": "pv1",
                "device_type": "ies.device.pv",
                "kind": "new",
                "params": {
                    # temp_coeff 置 0（无温度折减；属性覆盖取标量，单位从 descriptor）：
                    # 现行 PV 方程按 °C 硬编码“−25”，而降级把环境温度换算
                    # 为 K（25°C→298.15K），恒温 25°C 会得出负发电系数
                    # （既有设备模型问题，与 F4b 无关，见报告残留）；
                    # 本测试只验收分年容量不断言它。
                    "temp_coeff": 0,
                    "solar_irradiance": {
                        "mode": "data_full",
                        "dataset_version_id": irr_vid,
                    },
                    "ambient_temperature": {
                        "mode": "data_full",
                        "dataset_version_id": temp_vid,
                    },
                },
                "position": {"x": 1.0, "y": 1.0},
            },
        },
        {
            "id": "fixture-connect-pv-load",
            "unit": "model",
            "type": "model.upsert_connection",
            "payload": {
                "name": "c-pv-load1",
                "from_device": "pv1",
                "from_port": "electric_out",
                "to_device": "load1",
                "to_port": "electricity_in",
            },
        },
        {
            "id": "fixture-connect-grid-load",
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
    planning = {
        "objective": {
            "sense": "minimize",
            "expression": (
                "pv1__upfront_capex + 1 year * pv1__annual_fixed_om"
                " + pv1__period_variable_om + import_meter__period_energy_purchase"
            ),
        },
        "variables": {
            "pv_cap_y0": {
                "device_ref": "pv1",
                "parameter": "rated_capacity_kwp",
                "year_offset": 0,
                "lower_bound": "0",
                "upper_bound": "0",
                "unit": "kWp",
            },
            "pv_cap_y1": {
                "device_ref": "pv1",
                "parameter": "rated_capacity_kwp",
                "year_offset": 1,
                "lower_bound": "0",
                "upper_bound": str(int(ANNUAL_CAP_UPPER_KW)),
                "unit": "kWp",
            },
        },
        "constraints": {},
    }
    config_cases.save_planning_config(db, pid, planning, None, user.id)
    revision = _set_explicit_calc(client, pid, user, revision)
    instances = {
        "pv1": {
            "type": "costed",
            "finance_type": "pv_system",
            "drivers": {
                "capacity_kw": {"property": "rated_capacity_kwp"},
                "generated_kwh": {
                    "interface": "electric_out",
                    "aggregation": "integrate_positive",
                },
            },
        },
        "grid1": {"type": "none"},
        "load1": {"type": "none"},
    }
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
                    "import_meter": {
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
    _bind_and_freeze(client, pid, user, load_vid, revision=revision)
    return pid


def _solution_scalar(variables: list[dict], name: str) -> float:
    for item in variables:
        if str(item["name"]) == name:
            return float(item["value"])
    raise AssertionError(f"解变量缺失: {name}")


class TestAnnualBuildTrueSolve:
    def test_two_year_build_serves_each_year_capacity(
        self, client: TestClient, db: Session, engine: Engine, plane
    ) -> None:
        """N=2 分年投产真求解：两年在产上限不同，建设变量有解。

        首年容量 [0,0]、次年 [0,50]kW：首年购电必须等于全年负荷，
        次年购电为负荷减去新建 50kW 光伏出力，次年建设变量为 50。
        旧生成器以单值覆盖全年：提交能通过，但首年行必引用次年变量
        （首年购电被低估）或次年行被首年零界锁死（无建设），本测试
        在旧代码上为红。不断言“提交通过”即完成。
        """
        from sqlalchemy import select  # noqa: E402

        user = make_user(db, "annual_owner_1")
        pid = _prepare_annual_project(client, db, user)
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
            idempotency_key="annual-build-n2",
        )
        assert status == 201, body
        task_id = int(body["task"]["id"])
        claim = worker_app.acquire_attempt(db, task_id, "annual-e2e")
        assert claim is not None
        db.commit()

        def factory() -> Session:
            return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()

        assert runner.run_task(factory, claim, worker_id="annual-e2e", compute=plane) == "completed"
        task_row = db.execute(select(Task).where(Task.id == task_id)).scalar_one()
        assert task_row.status == "completed"
        assert task_row.business_outcome == "normal_completion"
        evidence = results_store.latest_evidence_for_task(db, task_id)
        assert evidence is not None
        sealed = json.loads(get_object(db, evidence.object_id).decode("utf-8"))
        solution = sealed["compute_result"]["outputs"]["solution"]
        y0_import, y1_import, span = _yearly_served(
            solution["variables"], "grid1__electricity_import__t"
        )
        assert span == FULL_STEPS
        assert y0_import == pytest.approx(ANNUAL_Y0_IMPORT, rel=1e-6)
        assert y1_import == pytest.approx(ANNUAL_Y1_IMPORT, rel=1e-6)
        assert (y0_import / YEAR1_STEPS) != pytest.approx(
            y1_import / YEAR2_STEPS, rel=1e-3
        )
        assert _solution_scalar(solution["variables"], "pv_cap_y0") == pytest.approx(
            0.0, abs=1e-6
        )
        assert _solution_scalar(solution["variables"], "pv_cap_y1") == pytest.approx(
            ANNUAL_CAP_UPPER_KW, rel=1e-6
        )
