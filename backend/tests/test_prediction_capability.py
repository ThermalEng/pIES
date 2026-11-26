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
import math
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
from worker_testkit import claim_for_direct_run  # noqa: E402

from iesplan.api import projects as projects_api  # noqa: E402
from iesplan.api import tasks as tasks_api  # noqa: E402
from iesplan.api.config import config_router  # noqa: E402
from iesplan.api.deps import get_request_db  # noqa: E402
from iesplan.application.tasks.submissions import (  # noqa: E402
    _check_prediction_inputs,
)
from iesplan.assembly.contracts import ValidatedAssemblyArtifact, ValidationReceipt  # noqa: E402
from iesplan.computation import (  # noqa: E402
    NAIVE_PREDICTOR,
    NAIVE_PREDICTOR_REF,
    ComputationUnavailableError,
    PredictedTarget,
    PredictorDescriptor,
    build_predictor_registry,
    materialize_predicted_series,
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


@pytest.fixture()
def plane(tmp_path: Path):
    from stage_testkit import build_test_compute_plane

    return build_test_compute_plane(tmp_path / "stages")


class _StrictPredictor:
    """仅拒绝测试用：显式目标类型集合（从不执行预测，只验相容拒绝）。"""

    @property
    def descriptor(self):
        return PredictorDescriptor(
            ref="ies.predictor.strict@1.0.0",
            depends_on_solve_result=False,
            accepts_any_target_type=False,
            target_types=("load",),
            needs_history=True,
        )

    def validate_parameters(self, parameters):
        return None

    def predict(self, **kwargs):
        raise AssertionError("strict predictor 不执行预测")


STRICT_REF = "ies.predictor.strict@1.0.0"


PARAM_PREDICTOR_REF = "test.predictor.param@1.0.0"


class _ParamPredictor:
    """参数化测试 predictor: 验证边界参数规则(scale > 0)。"""

    @property
    def descriptor(self):
        return PredictorDescriptor(
            ref=PARAM_PREDICTOR_REF,
            depends_on_solve_result=False,
            accepts_any_target_type=True,
            needs_history=True,
            description="测试参数化独立预测: scale 为正有限数",
            parameters_schema={
                "type": "object",
                "properties": {"scale": {"type": "number", "exclusiveMinimum": 0}},
                "additionalProperties": False,
            },
        )

    def validate_parameters(self, parameters):
        scale = parameters.get("scale") if isinstance(parameters, dict) else None
        if (
            isinstance(scale, bool)
            or not isinstance(scale, (int, float))
            or not math.isfinite(scale)
            or scale <= 0
        ):
            raise ValueError("测试参数预测要求 parameters.scale 为正有限数")

    def predict(
        self, *, device, interface, target_type, history, history_object_id,
        parameters, seed, point_count,
    ):
        return NAIVE_PREDICTOR.predict(
            device=device, interface=interface, target_type=target_type,
            history=history, history_object_id=history_object_id,
            parameters=parameters, seed=seed, point_count=point_count,
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

    def test_dependency_declaration_required_and_round_tripped(self) -> None:
        assert NAIVE_PREDICTOR.descriptor.depends_on_solve_result is False
        registry = build_predictor_registry((NAIVE_PREDICTOR, _ParamPredictor()))
        snapshot = {d.ref: d.depends_on_solve_result for d in registry.snapshot()}
        assert snapshot == {NAIVE_PREDICTOR_REF: False, PARAM_PREDICTOR_REF: False}
        with pytest.raises(TypeError):
            PredictorDescriptor(ref="ies.predictor.missing@1.0.0")
        with pytest.raises(TypeError):
            PredictorDescriptor(
                ref="ies.predictor.missing@1.0.0",
                depends_on_solve_result=0,
            )

    def test_unknown_ref_unavailable_without_fallback(self) -> None:
        registry = build_predictor_registry((NAIVE_PREDICTOR,))
        with pytest.raises(ComputationUnavailableError):
            registry.get("ies.predictor.missing@9.9.9")

    def test_duplicate_or_bad_descriptor_rejects_atomically(self) -> None:
        with pytest.raises(ComputationUnavailableError):
            build_predictor_registry((NAIVE_PREDICTOR, NAIVE_PREDICTOR))
        with pytest.raises(ComputationUnavailableError):
            build_predictor_registry((_NoValidatePredictor(),))
        with pytest.raises(ComputationUnavailableError):
            build_predictor_registry((object(),))


class _NoValidatePredictor:
    """注册拒绝用：缺少参数校验。"""

    @property
    def descriptor(self):
        return PredictorDescriptor(
            ref="test.predictor.no-validate@1.0.0",
            depends_on_solve_result=False,
        )


def _predict_artifact(bindings: dict) -> ValidatedAssemblyArtifact:
    doc = {
        "schema": "ies.assembly",
        "schema_version": "1.0.0",
        "project_baseline": {"resolution": "1h", "start_year": 2026},
        "devices": bindings,
    }
    return ValidatedAssemblyArtifact(
        canonical_text=json.dumps(doc), receipt=ValidationReceipt()
    )


def _history_csv_bytes(interface: str, values: list[float]) -> bytes:
    lines = [
        "# schema: ies.device-data",
        "# schema_version: 1.0.0",
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
            7, operation_years=1,
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
                7, operation_years=1,
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
        return {"predictions": list(entries)}

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

    def test_two_independent_targets_pass_compat(self) -> None:
        registry = build_predictor_registry((NAIVE_PREDICTOR,))
        assert (
            _check_prediction_inputs(
                self._doc("load1", "load2"),
                self._cfg(
                    self._entry("load1", NAIVE_PREDICTOR_REF),
                    self._entry("load2", NAIVE_PREDICTOR_REF),
                ),
                1, predictor_registry=registry,
            )
            == 2
        )


def _predict_history_csv(values: list[float], rows: int = 24) -> bytes:
    """预测历史 device-data 字节（upload 尚不支持此格式，由域级直接建版本）。"""
    lines = [
        "# schema: ies.device-data",
        "# schema_version: 1.0.0",
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


def _generator_registry():
    from iesplan.computation.generators import HIGHS_MILP_PROVIDER, build_registry

    return build_registry((HIGHS_MILP_PROVIDER,))


def _registry_kwargs() -> dict:
    from iesplan.computation import build_evaluator_registry, build_solver_registry
    from iesplan.computation.config import (
        BUILTIN_EVALUATOR_FINANCE_SPEC,
        BUILTIN_SOLVER_HIGHS_SPEC,
    )

    return {
        "generator_registry": _generator_registry(),
        "solver_registry": build_solver_registry((BUILTIN_SOLVER_HIGHS_SPEC,)),
        "evaluator_registry": build_evaluator_registry((BUILTIN_EVALUATOR_FINANCE_SPEC,)),
    }


def _save_calc_case(db: Session, user, pid: int, body: dict, registry) -> int:
    """经保存用例直接落配置（合成算法在保存入口真实可用，带目录注入）。

    公开 HTTP 层只装配生产目录，合成算法经此入口证明参数规则接线。
    """
    from iesplan.application.configuration.calc_config import (  # noqa: E402
        save_calculation_config_case,
    )

    saved = save_calculation_config_case(
        db, user, pid, body, None, predictor_registry=registry,
        **_registry_kwargs(),
    )
    return saved["revision"]


def _claim(db: Session, task_id: int):
    claim = claim_for_direct_run(db, task_id, "predict-e2e")
    assert claim is not None
    db.commit()
    return claim


def _run(engine: Engine, claim, plane) -> str:
    def factory() -> Session:
        return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()

    return runner.run_task(factory, claim, worker_id="predict-e2e", compute=plane)


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

    def test_two_independent_targets_single_solve(
        self, client: TestClient, db: Session, engine: Engine, plane
    ) -> None:
        """多独立目标提交→执行→证据链：无耦合声明即一次完整求解，不迭代。

        convergence 必须为 null（不适用不得携带）；分流只看依赖声明，
        不看目标数量。
        """
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
        assert prediction["prediction_rounds"] == 1
        assert prediction["solve_rounds"] == 1
        assert prediction["phase2_solve_rounds"] == 0
        assert len(prediction["receipts"]) == 2
        assert "rounds" not in sealed

    def test_unknown_algorithm_rejected_at_save(
        self, client: TestClient, db: Session
    ) -> None:
        """配置引用未注册预测算法 → 保存即 422 CONFIG-VAL-001，不落库。

        未知算法不能冒充已校验配置落库；提交闸门的 TASK-REQ-013 仍保留
        给注册表发生变更的直接提交路径（见 TestSubmitCompat）。
        """
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
        resp = client.put(
            f"/api/projects/{pid}/config",
            json={"calculation_config": body, "expected_revision": None},
            headers=_h(client, user),
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "CONFIG-VAL-001"

    def test_save_parameter_boundary(
        self, client: TestClient, db: Session
    ) -> None:
        """保存输入边界调用算法参数规则：缺尺度 422，通过与未知算法直通。"""
        from iesplan.application.configuration.calc_config import (  # noqa: E402
            save_calculation_config_case,
        )
        from iesplan.core.errors import AppError  # noqa: E402

        user = make_user(db, "predict_owner_9")
        pid = _create_project(client, user, "参数边界项目")
        registry = build_predictor_registry((_ParamPredictor(),))

        def _body(parameters: dict) -> dict:
            config = dict(_CONFIG_BODY)
            config["predictions"] = [
                {
                    "target": {"instance": "load1", "interface": "electricity_demand"},
                    "algorithm": PARAM_PREDICTOR_REF,
                    "parameters": parameters,
                }
            ]
            return config

        with pytest.raises(AppError) as exc_info:
            save_calculation_config_case(
                db, user, pid, _body({}), None, predictor_registry=registry,
                **_registry_kwargs(),
            )
        assert exc_info.value.code == "CONFIG-VAL-001"
        with pytest.raises(AppError):
            save_calculation_config_case(
                db, user, pid, _body({"scale": 0}), None, predictor_registry=registry,
                **_registry_kwargs(),
            )
        saved = save_calculation_config_case(
            db, user, pid, _body({"scale": 5.0}), None, predictor_registry=registry,
            **_registry_kwargs(),
        )
        assert saved["revision"] == 1
        # 未注册算法同样保存拒绝（不能冒充已校验配置落库）。
        unknown = dict(_CONFIG_BODY)
        unknown["predictions"] = [
            {
                "target": {"instance": "load1", "interface": "electricity_demand"},
                "algorithm": "ies.predictor.missing@9.9.9",
                "parameters": {},
            }
        ]
        with pytest.raises(AppError) as exc_info:
            save_calculation_config_case(
                db, user, pid, unknown, 1, predictor_registry=registry,
                **_registry_kwargs(),
            )
        assert exc_info.value.code == "CONFIG-VAL-001"
        # 目录缺席而含预测目标同样不可用。
        with pytest.raises(AppError):
            save_calculation_config_case(
                db, user, pid, _body({"scale": 5.0}), 1, predictor_registry=None,
                **_registry_kwargs(),
            )
