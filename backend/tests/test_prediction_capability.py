"""B5-2b 公共预测能力: descriptor/目录/naive 相容 → 物化 → 阶段二 → 证据。

- 纯单元(无数据库): naive 确定性/携载/拒绝、目录原子发布、增量、
  物化配对与相容拒绝、提交相容码 012/013/014；
- 提交→执行→证据链测试(SQLite + 真实 HiGHS，非完整用户端到端)：
  单目标直通完成、多目标阶段二收敛完成、耦合 predictor 逐轮求解、
  训练产物/序列密封与引用、未知算法与上限不足提交拒绝。
版本字节由域级创建(上传尚只收通用时间戳格式，门控只认 device-data
step 格式，见 B5-2a 日志；此处 workaround 仅限脚手架）。上传闭环
待补：以下测试不含公开上传路径，不称完整用户端到端。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")

import pytest  # noqa: E402
from assembly_fixture import declare_compute_device  # noqa: E402
from auth_helpers import make_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from test_tasks_api import (  # noqa: E402
    _CONFIG_BODY,
    _bind_and_freeze,
    _create_project,
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
from iesplan.application.tasks.submissions import (  # noqa: E402
    _check_prediction_inputs,
    submit_task,
)
from iesplan.assembly.contracts import ValidatedAssemblyArtifact, ValidationReceipt  # noqa: E402
from iesplan.bootstrap import assemble_compute_worker  # noqa: E402
from iesplan.computation import (  # noqa: E402
    NAIVE_PREDICTOR,
    NAIVE_PREDICTOR_REF,
    ComputationUnavailableError,
    PredictedTarget,
    PredictorDescriptor,
    build_predictor_registry,
    materialize_predicted_series,
    prediction_delta,
)
from iesplan.db import Base  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.results import persistence as results_store  # noqa: E402
from iesplan.storage import get_object, list_refs, put_object  # noqa: E402
from iesplan.tasks import InvalidRequestError  # noqa: E402
from iesplan.tasks.persistence import Task  # noqa: E402
from iesplan.worker import runner  # noqa: E402


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
    from iesplan.tasks import queue  # noqa: E402

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


@pytest.fixture(scope="module")
def plane():
    return assemble_compute_worker().compute_execution_plane()


class _StrictPredictor:
    """仅拒绝测试用：显式目标类型集合（从不执行预测，只验相容拒绝）。"""

    @property
    def descriptor(self):
        return PredictorDescriptor(
            ref="ies.predictor.strict@1.0.0",
            accepts_any_target_type=False,
            target_types=("load",),
            needs_history=True,
        )

    def predict(self, **kwargs):
        raise AssertionError("strict predictor 不执行预测")


STRICT_REF = "ies.predictor.strict@1.0.0"


CHASE_REF = "test.predictor.chase@1.0.0"


class _ChasePredictor:
    """测试耦合 predictor：首轮携载历史末值，后续轮值随上一轮真实求解目标值变化。

    只验阶段二耦合机制（previous 真实透传、每轮真实求解、新 Bundle/
    最终结果/证据同最终轮输入一致）；非生产算法，不进入公开目录。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    @property
    def descriptor(self):
        return PredictorDescriptor(
            ref=CHASE_REF,
            accepts_any_target_type=True,
            needs_history=True,
            description="测试耦合: 预测值随上一轮求解目标值变化",
        )

    def predict(
        self, *, device, interface, target_type, history, history_object_id,
        parameters, seed, point_count, previous=None,
    ):
        if not history:
            raise ValueError(f"测试耦合预测无历史可携: {device!r} x {interface!r}")
        if previous is None:
            value = float(history[-1])
            previous_objective = None
        else:
            previous_objective = float(previous["solution"]["objective_value"])
            value = 20.0 + (abs(previous_objective) % 4.3)
        values = tuple(value for _ in range(point_count))
        self.calls.append(
            {
                "device": device,
                "interface": interface,
                "previous_objective": previous_objective,
                "value": value,
            }
        )
        artifact = json.dumps(
            {
                "schema": "ies.prediction-artifact",
                "algorithm": CHASE_REF,
                "target": {"device": device, "interface": interface},
                "learned": {"method": "chase_objective", "value": value},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        receipt = {
            "target_type": target_type,
            "algorithm": CHASE_REF,
            "parameters": dict(parameters),
            "feature_order": ["history_value", "previous_objective"],
            "target_order": [interface],
            "training_input_refs": {"history": history_object_id},
            "training_target_refs": {interface: history_object_id},
            "covariate_refs": {},
            "seed": seed,
            "previous_objective": previous_objective,
            "output_series": {
                "device": device,
                "interface": interface,
                "mode": "data_predict",
                "point_count": point_count,
            },
        }
        return PredictedTarget(
            device=device,
            interface=interface,
            target_type=target_type,
            values=values,
            history_object_id=history_object_id,
            artifact=artifact,
            receipt=receipt,
        )


class TestNaivePredictor:
    def test_carry_deterministic_full_horizon(self) -> None:
        history = (48.0, 49.5, 47.0)
        first = NAIVE_PREDICTOR.predict(
            device="load1", interface="electricity_demand", target_type="load",
            history=history, history_object_id="obj-h", parameters={},
            seed=7, point_count=8760,
        )
        second = NAIVE_PREDICTOR.predict(
            device="load1", interface="electricity_demand", target_type="load",
            history=history, history_object_id="obj-h", parameters={},
            seed=7, point_count=8760,
        )
        assert len(first.values) == 8760
        assert set(first.values) == {47.0}
        assert first.values == second.values
        assert first.artifact == second.artifact
        assert json.loads(first.artifact.decode("utf-8"))["learned"] == {
            "method": "naive_carry", "last_value": 47.0,
        }

    def test_accepts_multiple_target_types_opaquely(self) -> None:
        for target_type in ("load", "flex_load"):
            out = NAIVE_PREDICTOR.predict(
                device="d", interface="i", target_type=target_type,
                history=(1.0,), history_object_id="obj-h", parameters={},
                seed=None, point_count=24,
            )
            assert out.receipt["target_type"] == target_type
            assert len(out.values) == 24

    def test_empty_history_rejected(self) -> None:
        with pytest.raises(ValueError, match="无历史可携"):
            NAIVE_PREDICTOR.predict(
                device="d", interface="i", target_type="load",
                history=(), history_object_id="obj-h", parameters={},
                seed=None, point_count=24,
            )


class TestPredictorRegistry:
    def test_exact_resolve_and_snapshot(self) -> None:
        registry = build_predictor_registry((NAIVE_PREDICTOR,))
        assert registry.get(NAIVE_PREDICTOR_REF) is NAIVE_PREDICTOR
        assert [d.ref for d in registry.snapshot()] == [NAIVE_PREDICTOR_REF]

    def test_unknown_ref_unavailable_without_fallback(self) -> None:
        registry = build_predictor_registry((NAIVE_PREDICTOR,))
        with pytest.raises(ComputationUnavailableError):
            registry.get("ies.predictor.missing@9.9.9")

    def test_duplicate_or_bad_descriptor_rejects_atomically(self) -> None:
        with pytest.raises(ComputationUnavailableError):
            build_predictor_registry((NAIVE_PREDICTOR, NAIVE_PREDICTOR))
        with pytest.raises(ComputationUnavailableError):
            build_predictor_registry((object(),))


class TestPredictionDelta:
    def test_equal_series_zero_and_known_diff(self) -> None:
        assert prediction_delta((1.0, 2.0), (1.0, 2.0)) == 0.0
        assert prediction_delta((1.0, 2.0), (1.5, 1.0)) == 1.0

    def test_length_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError, match="点数不一致"):
            prediction_delta((1.0,), (1.0, 2.0))


def _predict_artifact(bindings: dict) -> ValidatedAssemblyArtifact:
    doc = {
        "schema": "ies.assembly",
        "schema_version": "2.0.0",
        "project_baseline": {"resolution": "1h", "leap_year": False},
        "devices": bindings,
    }
    return ValidatedAssemblyArtifact(
        canonical_text=json.dumps(doc), receipt=ValidationReceipt()
    )


def _history_csv_bytes(interface: str, values: list[float]) -> bytes:
    lines = [
        "# schema: ies.device-data",
        "# schema_version: 2.0.0",
        "# dataset_id: hist",
        "# device_id: ies.device.electric_load",
        "# source_mode: data_repeat",
        "# resolution: 1h",
        f"# unit.{interface}: kW",
        f"step,{interface}",
    ]
    lines += [f"{i},{v}" for i, v in enumerate(values)]
    return ("\n".join(lines) + "\n").encode("utf-8")


class TestMaterializePredicted:
    def _bindings(self) -> dict:
        return {
            "load1": {
                "predefined_interfaces": {
                    "electricity_demand": {
                        "mode": "data_predict",
                        "path": "data/ds1.data.csv",
                        "target_type": "load",
                        "content": {"object_id": "obj-h1", "media_type": "text/csv"},
                    }
                }
            },
            "load2": {
                "predefined_interfaces": {
                    "electricity_demand": {
                        "mode": "data_predict",
                        "path": "data/ds2.data.csv",
                        "target_type": "flex_load",
                        "content": {"object_id": "obj-h2", "media_type": "text/csv"},
                    }
                }
            },
        }

    def _predictions(self, *algs: str) -> tuple:
        return tuple(
            {
                "target": {"instance": dev, "interface": "electricity_demand"},
                "algorithm": alg,
                "parameters": {},
            }
            for dev, alg in zip(("load1", "load2"), algs, strict=True)
        )

    def test_two_targets_materialize_with_receipts(self) -> None:
        resources = {
            "obj-h1": _history_csv_bytes("electricity_demand", [48.0, 49.0]),
            "obj-h2": _history_csv_bytes("electricity_demand", [10.0]),
        }
        predicted = materialize_predicted_series(
            _predict_artifact(self._bindings()),
            resources,
            self._predictions(NAIVE_PREDICTOR_REF, NAIVE_PREDICTOR_REF),
            build_predictor_registry((NAIVE_PREDICTOR,)).get,
            7,
        )
        assert [(p.device, p.interface) for p in predicted] == [
            ("load1", "electricity_demand"), ("load2", "electricity_demand"),
        ]
        assert list(predicted[0].values)[:3] == [49.0, 49.0, 49.0]
        assert list(predicted[1].values)[:2] == [10.0, 10.0]
        assert predicted[0].receipt["training_input_refs"] == {"history": "obj-h1"}
        assert predicted[0].receipt["training_target_refs"] == {
            "electricity_demand": "obj-h1"
        }
        assert predicted[0].receipt["seed"] == 7
        assert predicted[0].history_object_id == "obj-h1"

    def test_predicted_series_csv_deterministic(self) -> None:
        item = PredictedTarget(
            device="d", interface="i", target_type="t",
            values=(1.5, 2.0), history_object_id="o", artifact=b"{}", receipt={},
        )
        raw = runner._predicted_series_csv(item).decode("utf-8").splitlines()
        assert raw == ["step,i", "0,1.5", "1,2.0"]

    def test_unknown_algorithm_unavailable(self) -> None:
        with pytest.raises(ComputationUnavailableError):
            materialize_predicted_series(
                _predict_artifact(self._bindings()),
                {"obj-h1": _history_csv_bytes("electricity_demand", [1.0])},
                self._predictions("ies.predictor.missing@9.9.9", NAIVE_PREDICTOR_REF),
                build_predictor_registry((NAIVE_PREDICTOR,)).get,
                7,
            )

class TestSubmitCompat:
    def _doc(self, *devs: str, target_type: str = "load") -> dict:
        devices: dict = {}
        for dev in devs:
            devices[dev] = {
                "predefined_interfaces": {
                    "electricity_demand": {"mode": "data_predict", "target_type": target_type}
                }
            }
        return {"devices": devices}

    def _cfg(self, *entries: dict) -> dict:
        return {"predictions": list(entries), "convergence": None}

    def _entry(self, dev: str, alg: str) -> dict:
        return {
            "target": {"instance": dev, "interface": "electricity_demand"},
            "algorithm": alg,
            "parameters": {},
        }

    def test_unknown_algorithm_rejected_013(self) -> None:
        registry = build_predictor_registry((NAIVE_PREDICTOR,))
        with pytest.raises(InvalidRequestError) as exc_info:
            _check_prediction_inputs(
                self._doc("load1"), self._cfg(self._entry("load1", "ies.predictor.missing@9.9.9")),
                1, predictor_registry=registry,
            )
        assert exc_info.value.code == "TASK-REQ-013"

    def test_unsupported_type_rejected_014(self) -> None:
        registry = build_predictor_registry((NAIVE_PREDICTOR, _StrictPredictor()))
        with pytest.raises(InvalidRequestError) as exc_info:
            _check_prediction_inputs(
                self._doc("load1", target_type="flex_load"),
                self._cfg(self._entry("load1", STRICT_REF)),
                1, predictor_registry=registry,
            )
        assert exc_info.value.code == "TASK-REQ-014"

    def test_naive_accepts_any_type(self) -> None:
        registry = build_predictor_registry((NAIVE_PREDICTOR,))
        assert (
            _check_prediction_inputs(
                self._doc("load1"), self._cfg(self._entry("load1", NAIVE_PREDICTOR_REF)),
                1, predictor_registry=registry,
            )
            == 1
        )


def _predict_history_csv(values: list[float], rows: int = 24) -> bytes:
    """预测历史 device-data 字节（upload 尚不支持此格式，由域级直接建版本）。"""
    lines = [
        "# schema: ies.device-data",
        "# schema_version: 2.0.0",
        "# dataset_id: predict_history",
        "# device_id: ies.device.electric_load",
        "# source_mode: data_predict",
        "# resolution: 1h",
        "# unit.electricity_demand: kW",
        "step,electricity_demand",
    ]
    cycled = [values[i % len(values)] for i in range(rows)]
    lines += [f"{i},{v}" for i, v in enumerate(cycled)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _craft_history_version(
    db: Session, client: TestClient, pid: int, user, name: str, raw: bytes
) -> int:
    """域级创建历史版本（workaround：上传只收通用时间戳格式）+ API 建容器。"""
    from iesplan.dataset import persistence as dataset_store  # noqa: E402

    resp = client.post(
        f"/api/projects/{pid}/datasets",
        json={"name": name, "source_category": "user_upload", "license": "CC-BY-4.0"},
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text
    ds_id = resp.json()["dataset"]["id"]
    handle = put_object(db, raw, "text/csv", source_category="test", purpose="prediction-history")
    version = dataset_store.create_version(
        db, dataset_id=ds_id, timeline="hourly", fixed_utc_offset_minutes=480,
        fields={"electricity_demand": {"unit": "kW"}}, units={"electricity_demand": "kW"},
        created_by=user.id, resolution="1h",
    )
    dataset_store.add_file(
        db, dataset_version_id=version.id, object_id=handle.id,
        file_kind="data", format="csv", row_count=24, size_bytes=len(raw),
    )
    db.commit()
    return version.id


def _draft_put(client: TestClient, pid: int, user, revision: int, commands: list) -> int:
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": revision, "commands": commands},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def _prepare_predict_project(
    client: TestClient, db: Session, user, targets: list[tuple[str, str, list[float]]],
    *, tariff_bindings: dict | None = None, planning_body: dict | None = None,
    version_factory=None,
) -> tuple[int, dict[str, int]]:
    """真实预测项目（B5-2a 声明入口 + 域级历史版本）：返回 (pid, {dev: vid})。

    targets 为 (设备名, 目标类型, 历史值)；每目标独立数据集版本。
    tariff_bindings/planning_body 缺省为无电价绑定 + 恒零目标（目标与负荷
    无关，只验链路）；耦合测试传入真实电价绑定与电价目标以使求解结果
    随预测序列变化。version_factory 缺省为域级历史版本创建；公开上传
    链测试传入 HTTP 上传工厂，不再域级 crafting。
    """
    from test_worker_compute_chain import PLANNING_BODY  # noqa: E402

    from iesplan.application import configuration as config_cases  # noqa: E402

    pid = _create_project(client, user, "预测能力测试项目")
    _declare_finance(db, user, pid)
    make_version = version_factory or _craft_history_version
    vids: dict[str, int] = {}
    for dev, _tt, history in targets:
        raw = _predict_history_csv(history)
        vids[dev] = make_version(db, client, pid, user, f"{dev}历史", raw)
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
    for i, (dev, target_type, _history) in enumerate(targets):
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
                        "peak_power_kw": 100,
                        "electricity_demand": {
                            "mode": "data_predict",
                            "dataset_version_id": vids[dev],
                            "target_type": target_type,
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
    # 规划变量须为额定功率类（容量变量会引入生成器不支持的除法）。
    config_cases.save_planning_config(
        db, pid, dict(planning_body or PLANNING_BODY), None, user.id
    )
    revision = _set_explicit_calc(client, pid, user, revision)
    instances = {device_ref: {"type": "none"}, "grid1": {"type": "none"}}
    instances.update({dev: {"type": "none"} for dev, _tt, _hist in targets})
    view = client.get(f"/api/projects/{pid}", headers=_h(client, user)).json()
    revision = _declare_assembly_sections(client, pid, user, view["draft"]["revision"], instances)
    if tariff_bindings is not None:
        resp = client.put(
            f"/api/projects/{pid}/draft",
            json={
                "expected_revision": revision,
                "commands": [{
                    "id": "c-tariff-bind", "unit": "assembly",
                    "type": "assembly.set_tariff_bindings",
                    "payload": {"bindings": tariff_bindings},
                }],
            },
            headers=_h(client, user),
        )
        assert resp.status_code == 200, resp.text
        revision = resp.json()["revision"]
    _bind_and_freeze(client, pid, user, None, revision=revision)
    return pid, vids


def _save_calc(client: TestClient, pid: int, user, body: dict) -> int:
    resp = client.put(
        f"/api/projects/{pid}/config",
        json={"calculation_config": body, "expected_revision": None},
        headers=_h(client, user),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def _claim(db: Session, task_id: int):
    claim = worker_app.acquire_attempt(db, task_id, "predict-e2e")
    assert claim is not None
    db.commit()
    return claim


def _run(engine: Engine, claim, plane) -> str:
    def factory() -> Session:
        return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()

    try:
        return runner.run_task(factory, claim, worker_id="predict-e2e", compute=plane)
    except worker_app.TaskCancelled:
        return "cancelled"
    except worker_app.LeaseFencedError:
        return "fenced"


def _evidence_payload(db: Session, task_id: int) -> dict:
    evidence = results_store.latest_evidence_for_task(db, task_id)
    assert evidence is not None
    return json.loads(get_object(db, evidence.object_id).decode("utf-8")), evidence


class TestPredictEndToEnd:
    def test_single_target_passthrough_completes(
        self, client: TestClient, db: Session, engine: Engine, plane
    ) -> None:
        """单预测目标提交→执行→证据链：阶段一即权威，训练产物/序列密封可追溯。"""
        user = make_user(db, "predict_owner_1")
        pid, _vids = _prepare_predict_project(
            client, db, user, [("load1", "load", [48.0, 49.0])]
        )
        body = dict(_CONFIG_BODY)
        body["predictions"] = [
            {
                "target": {"instance": "load1", "interface": "electricity_demand"},
                "algorithm": NAIVE_PREDICTOR_REF,
                "parameters": {},
            }
        ]
        rev = _save_calc(client, pid, user, body)
        status, resp = _submit_task(
            client, pid, user, config={"config_revision": rev},
            idempotency_key="predict-e2e-single",
        )
        assert status == 201, resp
        task_id = int(resp["task"]["id"])
        claim = _claim(db, task_id)
        assert _run(engine, claim, plane) == "completed"

        task = db.execute(select(Task).where(Task.id == task_id)).scalar_one()
        assert task.status == "completed"
        assert task.business_outcome == "normal_completion"
        sealed, evidence = _evidence_payload(db, task_id)
        prediction = sealed["prediction"]
        assert prediction["predict_target_count"] == 1
        assert prediction["prediction_rounds"] == 1
        assert prediction["solve_rounds"] == 1
        assert prediction["phase2_solve_rounds"] == 0
        assert prediction["converged"] is True
        assert prediction["final_delta"] is None
        receipt = prediction["receipts"][0]
        assert receipt["algorithm"] == NAIVE_PREDICTOR_REF
        assert receipt["training_input_refs"]["history"]
        training = receipt["training_artifact"]
        assert training["object_id"]
        raw = json.loads(get_object(db, training["object_id"]).decode("utf-8"))
        assert raw["learned"] == {"method": "naive_carry", "last_value": 49.0}
        refs = list_refs(db, training["object_id"])
        assert any(
            r.ref_type == "evidence_package" and r.ref_entity_id == str(evidence.id)
            for r in refs
        )
        assert sealed["prediction_files"][receipt["artifact_path"]]["object_id"] == training["object_id"]
        assert receipt["training_target_refs"] == {
            "electricity_demand": receipt["training_input_refs"]["history"]
        }
        series = receipt["output_series"]
        assert series["device"] == "load1"
        assert series["mode"] == "data_predict"
        assert sealed["prediction_files"][receipt["series_path"]]["object_id"] == series["object_id"]
        series_lines = get_object(db, series["object_id"]).decode("utf-8").splitlines()
        assert series_lines[0] == "step,electricity_demand"
        assert len(series_lines) == series["point_count"] + 1
        assert {line.split(",")[1] for line in series_lines[1:]} == {"49.0"}

    def test_two_targets_phase_two_converges(
        self, client: TestClient, db: Session, engine: Engine, plane
    ) -> None:
        """双预测目标提交→执行→证据链：必进阶段二，每轮真实求解，序列无差即收敛。"""
        user = make_user(db, "predict_owner_2")
        pid, _vids = _prepare_predict_project(
            client, db, user,
            [("load1", "load", [48.0, 49.0]), ("load2", "flex_load", [10.0, 11.0])],
        )
        body = dict(_CONFIG_BODY)
        body["predictions"] = [
            {
                "target": {"instance": dev, "interface": "electricity_demand"},
                "algorithm": NAIVE_PREDICTOR_REF,
                "parameters": {},
            }
            for dev in ("load1", "load2")
        ]
        body["convergence"] = {"tolerance": 0.001, "max_iterations": 50}
        rev = _save_calc(client, pid, user, body)
        status, resp = _submit_task(
            client, pid, user, config={"config_revision": rev},
            idempotency_key="predict-e2e-multi",
        )
        assert status == 201, resp
        task_id = int(resp["task"]["id"])
        claim = _claim(db, task_id)
        assert _run(engine, claim, plane) == "completed"

        task = db.execute(select(Task).where(Task.id == task_id)).scalar_one()
        assert task.status == "completed"
        sealed, _evidence = _evidence_payload(db, task_id)
        prediction = sealed["prediction"]
        assert prediction["predict_target_count"] == 2
        assert prediction["prediction_rounds"] == 2
        assert prediction["solve_rounds"] == 2
        assert prediction["phase2_solve_rounds"] == 1
        assert prediction["converged"] is True
        assert prediction["final_delta"] == 0.0
        assert len(prediction["receipts"]) == 2

    def test_coupled_predictor_solves_each_round(
        self, client: TestClient, db: Session, engine: Engine, plane
    ) -> None:
        """耦合验收：随 previous 变化的 predictor 每一阶段二迭代都真实求解。

        经提交用例直接注入测试目录（公开 HTTP 层只装配生产目录，见
        未知算法测试）；执行面同样注入测试目录。
        断言：轮数 == 求解数 == 上限 3 且未收敛；predictor 逐轮收到真实
        上一轮求解输出；三轮目标值两两不同（每轮都解了新模型）；载荷
        final_delta 与记录值独立重算一致。

        本测试用电价目标（购电费最小化）使求解结果随预测序列变化；
        默认恒零目标的链路测试见单/双目标用例。
        """
        from test_worker_compute_chain import PLANNING_BODY as _BASE_PLANNING  # noqa: E402

        user = make_user(db, "predict_owner_5")
        chase_planning = dict(_BASE_PLANNING)
        chase_planning["objective"] = {
            "sense": "minimize",
            "expression": "grid_import__period_energy_purchase",
        }
        pid, _vids = _prepare_predict_project(
            client, db, user,
            [("load1", "load", [48.0, 49.0]), ("load2", "flex_load", [10.0, 11.0])],
            tariff_bindings={
                "grid_import": {
                    "price": "grid_import",
                    "instance": "grid1",
                    "interface": "electricity_import",
                    "aggregation": "integrate_positive",
                }
            },
            planning_body=chase_planning,
        )
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
        registry = build_predictor_registry((chase,))
        task, _flags = submit_task(
            db, user, pid, "optimization", config={"config_revision": rev},
            idempotency_key="predict-e2e-chase",
            generator_registry=plane.generator_registry,
            predictor_registry=registry,
        )
        chase_plane = worker_app.ComputeExecutionPlane(
            generator_registry=plane.generator_registry,
            executor_registry=plane.executor_registry,
            adapter_registry=plane.adapter_registry,
            runtime=plane.runtime,
            predictor_registry=registry,
        )
        claim = _claim(db, task.id)
        assert _run(engine, claim, chase_plane) == "completed"

        task_row = db.execute(select(Task).where(Task.id == task.id)).scalar_one()
        assert task_row.status == "completed"
        assert task_row.business_outcome == "normal_completion"
        sealed, evidence = _evidence_payload(db, task.id)
        prediction = sealed["prediction"]
        assert prediction["predict_target_count"] == 2
        assert prediction["prediction_rounds"] == 3
        assert prediction["solve_rounds"] == 3
        assert prediction["phase2_solve_rounds"] == 2
        assert prediction["converged"] is False
        final_delta = prediction["final_delta"]
        assert final_delta is not None and final_delta > 0.001

        by_target: dict[str, list[dict]] = {}
        for call in chase.calls:
            by_target.setdefault(call["device"], []).append(call)
        assert set(by_target) == {"load1", "load2"}
        for calls in by_target.values():
            assert len(calls) == 3
            assert calls[0]["previous_objective"] is None
            assert calls[1]["previous_objective"] is not None
            assert calls[2]["previous_objective"] is not None
        # 两目标看到同一轮求解结果（同一次求解驱动两目标重建）。
        assert (
            by_target["load1"][1]["previous_objective"]
            == by_target["load2"][1]["previous_objective"]
        )
        assert (
            by_target["load1"][2]["previous_objective"]
            == by_target["load2"][2]["previous_objective"]
        )
        # 三轮目标值两两不同：每轮都解了由新序列重建的新模型。
        round_objectives = [
            by_target["load1"][1]["previous_objective"],
            by_target["load1"][2]["previous_objective"],
            sealed["compute_result"]["outputs"]["solution"]["objective_value"],
        ]
        assert len(set(round_objectives)) == 3
        # final_delta 与记录的最终两轮值独立重算一致（常量序列，单点即全量差）。
        recomputed = max(
            prediction_delta((calls[1]["value"],), (calls[2]["value"],))
            for calls in by_target.values()
        )
        assert final_delta == recomputed
        assert len(prediction["receipts"]) == 2
        # 封存的最终轮序列字节与记录的第三轮值一致。
        for entry in prediction["receipts"]:
            series_id = entry["output_series"]["object_id"]
            sealed_lines = get_object(db, series_id).decode("utf-8").splitlines()
            assert sealed_lines[0] == "step,electricity_demand"
            assert len(sealed_lines) == entry["output_series"]["point_count"] + 1
            expected_value = by_target[entry["output_series"]["device"]][2]["value"]
            assert {line.split(",")[1] for line in sealed_lines[1:]} == {
                repr(expected_value)
            }
        # 多轮证据：三轮 Bundle/回执/原始输出/训练产物/序列全部可按引用取回；
        # 最终轮不重复封存，其条目引用顶层最终组同一对象。
        history = sealed["round_history"]
        rounds = sealed["rounds"]
        assert [entry["round"] for entry in rounds] == [1, 2, 3]
        assert [entry["phase"] for entry in rounds] == ["phase1", "phase2", "phase2"]
        assert [entry["authoritative"] for entry in rounds] == [False, False, True]
        assert sealed["authoritative_round"] == 3
        assert rounds[0]["sealed_in"] == rounds[1]["sealed_in"] == {
            "bundle": "round_history", "solution": "round_history",
            "prediction": "round_history",
        }
        assert rounds[2]["sealed_in"] == {
            "bundle": "bundle_files", "solution": "solution",
            "prediction": "prediction_files",
        }
        assert not [key for key in history if key.startswith("round-3/")]
        round_receipt_ids = [entry["receipt"]["receipt_id"] for entry in rounds]
        assert len(set(round_receipt_ids)) == 3
        assert sealed["receipt"]["receipt_id"] == round_receipt_ids[2]
        assert sealed["compute_result"]["receipt_id"] == round_receipt_ids[2]
        top_groups = {
            "bundle_files": sealed["bundle_files"],
            "solution": sealed["solution"],
            "prediction_files": sealed["prediction_files"],
            "round_history": history,
        }
        relative_sets = []
        for entry in rounds:
            keys = (
                entry["files"]["bundle"] + entry["files"]["solution"]
                + entry["files"]["prediction"]
            )
            assert keys
            depth = 1 if entry["authoritative"] else 2
            relative_sets.append(sorted(
                "/".join(key.split("/")[depth:]) for key in keys
            ))
            for section in ("bundle", "solution", "prediction"):
                group = top_groups[entry["sealed_in"][section]]
                for key in entry["files"][section]:
                    if entry["authoritative"] and section != "prediction":
                        assert key.startswith(section + "/")
                        descriptor = group[key[len(section) + 1:]]
                    else:
                        descriptor = group[key]
                    refs = list_refs(db, descriptor["object_id"])
                    assert any(
                        r.ref_type == "evidence_package"
                        and r.ref_entity_id == str(evidence.id)
                        for r in refs
                    ), key
        assert relative_sets[0] == relative_sets[1] == relative_sets[2]
        # 最终轮条目经 sealed_in 引用顶层最终组同一对象（无重复封存对象）。
        final_entry = rounds[2]
        for section, group_name in (
            ("bundle", "bundle_files"), ("solution", "solution"),
            ("prediction", "prediction_files"),
        ):
            assert final_entry["sealed_in"][section] == group_name
            for key in final_entry["files"][section]:
                if section == "prediction":
                    assert key in top_groups[group_name]
                else:
                    assert key.startswith(section + "/")
                    assert key[len(section) + 1:] in top_groups[group_name]
        all_ids = [
            descriptor["object_id"]
            for group in top_groups.values()
            for descriptor in group.values()
        ]
        assert len(set(all_ids)) == len(all_ids)

    def test_round_history_seal_failure_rolls_back(
        self, client: TestClient, db: Session, engine: Engine, plane, monkeypatch
    ) -> None:
        """多轮封存/挂接注入失败 → 整笔 DB 回滚，无 completed 与孤立元数据。

        只让输出引用挂接失败（purpose=evidence_output），结果引用保持
        正常：含 round_history 在内的整笔（含已封存对象元数据）必须回滚。
        """
        from iesplan.application.worker import compute_cases  # noqa: E402
        from iesplan.storage.persistence import StoredObject  # noqa: E402

        real_add_ref = compute_cases.add_ref

        def _fail_output_refs(db, object_id, ref_type, ref_id, ref_entity_type=None,
                              purpose=None, *, actor_id=None, actor_type="system"):
            if purpose == "evidence_output":
                raise RuntimeError("injected-ref-failure")
            return real_add_ref(
                db, object_id, ref_type, ref_id, ref_entity_type=ref_entity_type,
                purpose=purpose, actor_id=actor_id, actor_type=actor_type,
            )

        monkeypatch.setattr(compute_cases, "add_ref", _fail_output_refs)
        user = make_user(db, "predict_owner_6")
        pid, _vids = _prepare_predict_project(
            client, db, user,
            [("load1", "load", [48.0]), ("load2", "flex_load", [10.0])],
        )
        body = dict(_CONFIG_BODY)
        body["predictions"] = [
            {
                "target": {"instance": dev, "interface": "electricity_demand"},
                "algorithm": NAIVE_PREDICTOR_REF,
                "parameters": {},
            }
            for dev in ("load1", "load2")
        ]
        body["convergence"] = {"tolerance": 0.001, "max_iterations": 2}
        rev = _save_calc(client, pid, user, body)
        status, resp = _submit_task(
            client, pid, user, config={"config_revision": rev},
            idempotency_key="predict-e2e-rollback",
        )
        assert status == 201, resp
        task_id = int(resp["task"]["id"])
        before = len(db.execute(select(StoredObject)).scalars().all())
        claim = _claim(db, task_id)
        assert _run(engine, claim, plane) == "failed"

        task = db.execute(select(Task).where(Task.id == task_id)).scalar_one()
        assert task.status == "failed"
        assert task.business_outcome is None
        assert results_store.latest_evidence_for_task(db, task_id) is None
        assert len(db.execute(select(StoredObject)).scalars().all()) == before

    def test_unknown_algorithm_rejected_at_submit(
        self, client: TestClient, db: Session
    ) -> None:
        """未注册算法提交即 400 TASK-REQ-013（真目录相容核对）。"""
        user = make_user(db, "predict_owner_3")
        pid, _vids = _prepare_predict_project(
            client, db, user, [("load1", "load", [48.0])]
        )
        body = dict(_CONFIG_BODY)
        body["predictions"] = [
            {
                "target": {"instance": "load1", "interface": "electricity_demand"},
                "algorithm": "ies.predictor.missing@9.9.9",
                "parameters": {},
            }
        ]
        rev = _save_calc(client, pid, user, body)
        status, resp = _submit_task(
            client, pid, user, config={"config_revision": rev},
            idempotency_key="predict-e2e-unknown",
        )
        assert status == 400, resp
        assert resp["error"]["code"] == "TASK-REQ-013"

    def test_max_iterations_one_rejected_at_submit(
        self, client: TestClient, db: Session
    ) -> None:
        """多目标配上限 1 提交即 400 TASK-REQ-012（不许跳过阶段二求解）。"""
        user = make_user(db, "predict_owner_4")
        pid, _vids = _prepare_predict_project(
            client, db, user,
            [("load1", "load", [48.0]), ("load2", "flex_load", [10.0])],
        )
        body = dict(_CONFIG_BODY)
        body["predictions"] = [
            {
                "target": {"instance": dev, "interface": "electricity_demand"},
                "algorithm": NAIVE_PREDICTOR_REF,
                "parameters": {},
            }
            for dev in ("load1", "load2")
        ]
        body["convergence"] = {"tolerance": 0.001, "max_iterations": 1}
        rev = _save_calc(client, pid, user, body)
        status, resp = _submit_task(
            client, pid, user, config={"config_revision": rev},
            idempotency_key="predict-e2e-maxit-one",
        )
        assert status == 400, resp
        assert resp["error"]["code"] == "TASK-REQ-012"
