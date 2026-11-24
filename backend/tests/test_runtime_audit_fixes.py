"""Focused regression tests for 1.1.5 runtime audit items 2, 3, 4 (backend), 5, and 6."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from auth_helpers import make_user
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from stage_testkit import build_test_compute_plane

from iesplan import project as project_domain
from iesplan import tasks as tasks_domain
from iesplan.application import worker as worker_app
from iesplan.application.worker import compute_cases
from iesplan.application.worker.checkpoints import (
    load_algorithm_checkpoint,
    load_evaluation_checkpoint,
    save_algorithm_checkpoint,
    save_evaluation_checkpoint,
)
from iesplan.bootstrap import collect_immutable_tables
from iesplan.computation import (
    NAIVE_PREDICTOR,
    AttemptContext,
    CalculationConfig,
    ExecutionReceipt,
    MaterializedSeries,
    MaterializedSet,
    SolverBundle,
    SolverBundleManifest,
    build_predictor_registry,
)
from iesplan.computation.generators import HIGHS_MILP_PROVIDER, build_registry
from iesplan.computation.runtime.job_runner_client import JobRunnerClient
from iesplan.computation.runtime.stage_runner import (
    BuiltinStageCatalog,
    BuiltinStageDescriptor,
    ContainerStagePolicy,
    ContainerStageRunner,
    ControlPlaneDetached,
    StageExecutionSpec,
    build_builtin_stage_catalog,
)
from iesplan.config import settings
from iesplan.core.content import MemoryContent
from iesplan.db import Base
from iesplan.storage import get_object_content, put_object
from iesplan.worker import runner
from iesplan.worker.scheduler import EmbeddedScheduler


@pytest.fixture()
def memory_db() -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    collect_immutable_tables()
    eng = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    try:
        yield eng, factory
    finally:
        eng.dispose()


def _stage_payload(
    *,
    stage_execution_id: str,
    attempt_id: str = "10",
    stage_id: str = "solver",
    fencing_token: str = "tok-10",
    workspace_rel: str | None = None,
    status: str = "exited",
    running: bool = False,
    exit_code: int | None = 0,
    termination_reason: str | None = "normal",
    output_files: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "stage_execution_id": stage_execution_id,
        "attempt_id": attempt_id,
        "stage_id": stage_id,
        "fencing_token": fencing_token,
        "container_id": f"cid-{stage_execution_id}",
        "container_name": f"pies-stage-{stage_execution_id}",
        "image": "pies-builtin-stages:1.1.5",
        "workspace_rel": workspace_rel or stage_execution_id,
        "reused": False,
        "status": status,
        "running": running,
        "exit_code": exit_code,
        "termination_reason": termination_reason,
        "started_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "finished_at": None if running else "2026-11-24T08:00:01Z",
        "resources": {
            "source": "docker_and_cgroup_v2",
            "memory_peak_bytes": {"status": "known", "value": 1_048_576},
        },
        "iterations": {"present": False, "count": 0, "tail": []},
        "output_files": output_files or [],
    }


def _default_policy() -> ContainerStagePolicy:
    return ContainerStagePolicy(
        builtin_image="pies-builtin-stages:1.1.5",
        cpu_cores=2.0,
        memory_mb=512,
        pids_limit=256,
        tmpfs_size_mb=128,
        stop_grace_seconds=3,
        max_timeout_seconds=60.0,
    )


def _create_project_and_task(
    db: Session,
    *,
    username: str,
    task_type: str = "calc",
    idempotency_key: str = "k-1",
    snapshot_id: int | None = None,
) -> tuple[Any, Any, Any]:
    user = make_user(db, username, role="admin")
    proj = project_domain.create_project(
        db,
        name=f"P-{username}",
        description="",
        owner_id=user.id,
        created_by=user.id,
        baseline_start_year=2026,
    )
    task = tasks_domain.create_task(
        db,
        project_id=proj.id,
        type=task_type,
        idempotency_key=idempotency_key,
        requested_by=user.id,
        calc_snapshot_id=snapshot_id,
    )
    db.flush()
    return user, proj, task


# ---------------------------------------------------------------------------
# Item 2: Authoritative DB-driven restart reconciliation
# ---------------------------------------------------------------------------


def test_list_active_reconcilable_claims_and_containerless_gap_reconciliation(
    memory_db: tuple[Engine, sessionmaker[Session]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _eng, factory = memory_db
    with factory() as db:
        _user, proj, compute_task = _create_project_and_task(
            db, username="recon_db_user", task_type="calc", idempotency_key="recon-calc"
        )
        io_task = tasks_domain.create_task(
            db, project_id=proj.id, type="report", idempotency_key="recon-io", requested_by=_user.id
        )
        finished_task = tasks_domain.create_task(
            db, project_id=proj.id, type="calc", idempotency_key="recon-done", requested_by=_user.id
        )
        db.flush()

        # 1. Active compute attempt with zero containers in job_runner (e.g. inter-stage gap)
        a_comp = tasks_domain.create_attempt(db, task_id=compute_task.id, worker_id="old-1")
        t_comp = str(uuid4())
        tasks_domain.acquire_lease(db, attempt_id=a_comp.id, lease_token=t_comp, acquired_by="old-1")

        # 2. Active IO attempt with zero containers in job_runner
        a_io = tasks_domain.create_attempt(db, task_id=io_task.id, worker_id="old-1")
        t_io = str(uuid4())
        tasks_domain.acquire_lease(db, attempt_id=a_io.id, lease_token=t_io, acquired_by="old-1")

        # 3. Released / finished attempt MUST NOT be returned
        a_done = tasks_domain.create_attempt(db, task_id=finished_task.id, worker_id="old-1")
        t_done = str(uuid4())
        tasks_domain.acquire_lease(db, attempt_id=a_done.id, lease_token=t_done, acquired_by="old-1")
        tasks_domain.release_lease(db, a_done.id, t_done, status="released")
        tasks_domain.finish_attempt(db, a_done.id, "succeeded")
        db.commit()

        claims = worker_app.list_active_reconcilable_claims(db)
        assert [c.attempt_id for c in claims] == [a_comp.id, a_io.id]
        assert worker_app.get_task_pool(db, compute_task.id) == "compute"
        assert worker_app.get_task_pool(db, io_task.id) == "io"

    stopped_ids: list[str] = []
    cleaned_ids: list[str] = []
    launched: list[tuple[int, str]] = []
    both_launched = threading.Event()

    class FakeStage:
        def __init__(self, seid: str, attempt_id: str, fencing_token: str) -> None:
            self.stage_execution_id = seid
            self.attempt_id = attempt_id
            self.stage_id = "solve-r1"
            self.fencing_token = fencing_token
            self.running = True
            self.status = "running"

    class FakeStageRunner:
        def list_stages(self, **_kwargs: Any) -> list[FakeStage]:
            # Only a mismatched/fenced stale container exists; active DB claims have 0 containers.
            return [
                FakeStage(f"{a_comp.id}--solve-r1", str(a_comp.id), "wrong-token"),
                FakeStage(f"{a_done.id}--solve-r1", str(a_done.id), t_done),
            ]

        def stop_stage(self, seid: str, **_kwargs: Any) -> None:
            stopped_ids.append(seid)

        def cleanup_stage(self, seid: str, **_kwargs: Any) -> None:
            cleaned_ids.append(seid)

    def fake_run_task(_factory: Any, claim: worker_app.Claim, **kwargs: Any) -> str:
        pool = "compute" if kwargs.get("compute") is not None else "io"
        launched.append((claim.attempt_id, pool))
        if len(launched) == 2:
            both_launched.set()
        return "completed"

    monkeypatch.setattr("iesplan.worker.scheduler.runner.run_task", fake_run_task)
    scheduler = EmbeddedScheduler(
        session_factory=factory,
        compute_slots=1,
        io_slots=1,
        build_slots=1,
        compute=object(),
        stage_runner=FakeStageRunner(),
        poll_interval=0.5,
    )
    try:
        actions = scheduler.reconcile_on_startup()
        assert both_launched.wait(timeout=3.0)
    finally:
        scheduler.stop(timeout=3.0)

    assert stopped_ids == [f"{a_comp.id}--solve-r1", f"{a_done.id}--solve-r1"]
    assert cleaned_ids == [f"{a_comp.id}--solve-r1", f"{a_done.id}--solve-r1"]
    assert set(launched) == {(a_comp.id, "compute"), (a_io.id, "io")}
    assert f"adopted:attempt-{a_comp.id}" in actions
    assert f"adopted:attempt-{a_io.id}" in actions


# ---------------------------------------------------------------------------
# Item 3: Consume evaluation and algorithm checkpoints
# ---------------------------------------------------------------------------


def test_load_evaluation_and_algorithm_checkpoints_consumed_by_runner(
    memory_db: tuple[Engine, sessionmaker[Session]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _eng, factory = memory_db
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from test_config_revisions import PROFILE_PAYLOAD

    from iesplan import configuration as config_domain
    from iesplan.application import configuration as config_cases

    # 1. Direct checkpoint load & algorithm runner bypass
    with factory() as db:
        user, proj, algo_task = _create_project_and_task(
            db, username="algo_ckpt_user", task_type="calc", idempotency_key="algo-ckpt"
        )
        # Create minimal calc snapshot for algorithm mode
        cfg_body = {
            "schema": "ies.calculation-config",
            "schema_version": "1.0.0",
            "mode": "planning",
            "predictions": [],
            "generator": None,
            "solver": None,
            "algorithm": "acme.algorithm.demo@1.0.0",
            "evaluator": None,
            "precision": "medium",
            "convergence": None,
            "time_limit_seconds": 30,
            "options": {},
            "seed": 7,
            "outputs": ["solution"],
        }
        cfg = config_domain.create_calculation_config(
            db,
            project_id=proj.id,
            content=cfg_body,
            created_by=user.id,
        )
        config_cases.register_finance_profile(db, dict(PROFILE_PAYLOAD), user.id)
        _, eff, _ = config_cases.set_project_finance_profile(
            db, proj.id, PROFILE_PAYLOAD["profile"]["id"], user.id
        )
        ver = project_domain.create_version(
            db,
            project_id=proj.id,
            content_object_id=put_object(db, b"{}", "application/json", "project").id,
            name="v1",
            reason="manual",
            created_by=user.id,
        )
        canonical_doc = {
            "schema": "ies.assembly",
            "schema_version": "1.0.0",
            "assembly": {"id": "asm-1"},
            "project_baseline": {"resolution": "1h", "start_year": 2026},
            "devices": {},
        }
        receipt_doc = {
            "schema": "ies.validation-receipt",
            "schema_version": "1.0.0",
            "validator": {"id": "v", "version": "1.0.0"},
            "canonical_algorithm": {"id": "c", "version": "1.0.0"},
            "dependencies": {},
            "resources": {},
            "finance_snapshot": {"profile_id": eff.profile_id, "revision": eff.revision},
            "planning": {},
            "system_equations": {},
            "diagnostics": [],
        }
        eval_block = {
            "evaluation_years": 1,
            "construction_period": 0,
            "operation_years": 1,
            "discount_rate": 0.08,
            "construction_payments": [],
            "maintenance_expenses": [],
        }
        snap = tasks_domain.create_snapshot(
            db,
            project_version_id=ver.id,
            dataset_version_ids=[],
            calc_config_snapshot={
                **cfg.content,
                "config_ref": {"project_id": proj.id, "revision": cfg.version},
            },
            random_seed=7,
            schema_version="1.0.0",
            result_adapter="ies.result-adapter.highs@1.0.0",
            executor="ies.executor.local_process@1.0.0",
            device_models={},
            evaluation=eval_block,
            canonical_assembly_text=json.dumps(canonical_doc),
            assembly_receipt=receipt_doc,
            created_by=user.id,
        )
        algo_task = tasks_domain.create_task(
            db,
            project_id=proj.id,
            type="calc",
            idempotency_key="algo-ckpt-2",
            requested_by=user.id,
            calc_snapshot_id=snap.id,
        )
        db.commit()
        claim = worker_app.acquire_attempt(db, algo_task.id, "w-1")
        assert claim is not None

        raw_algo_result = {
            "schema": "ies.compute-result",
            "schema_version": "1.0.0",
            "bundle_id": f"snapshot-{snap.id}:attempt-{claim.attempt_id}",
            "receipt_id": "rcpt-algo-1",
            "execution_status": "completed",
            "math_status": "solved",
            "outputs": {
                "solution": {"objective_value": 12.5, "variables": []},
                "series": {
                    "dev.p": {
                        "device": "dev",
                        "interface": "p",
                        "unit": "kW",
                        "values": [1.0],
                    }
                },
                "hourly_series": {"data": {"dev.p": [1.0]}, "meta": {"units": {"dev.p": "kW"}, "series": {}}},
                "financial_evaluation": {"schema": "ies.financial-evaluation", "status": "evaluated"},
            },
        }
        save_algorithm_checkpoint(db, claim, raw_algo_result)

        loaded_algo = load_algorithm_checkpoint(db, claim)
        assert loaded_algo is not None
        assert loaded_algo.result.receipt_id == "rcpt-algo-1"
        assert loaded_algo.hourly_series == raw_algo_result["outputs"]["hourly_series"]
        assert loaded_algo.financial_evaluation == raw_algo_result["outputs"]["financial_evaluation"]

        save_evaluation_checkpoint(db, claim, 1, raw_algo_result)
        loaded_eval = load_evaluation_checkpoint(db, claim, 1)
        assert loaded_eval is not None
        assert loaded_eval.result.bundle_id == raw_algo_result["bundle_id"]

    # Runner consumes algorithm checkpoint without invoking run_algorithm_stage container
    plane = build_test_compute_plane(tmp_path / "stages")
    monkeypatch.setattr(
        worker_app,
        "run_algorithm_stage",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("algorithm container must not re-run")),
    )
    assert runner.run_task(factory, claim, worker_id="w-1", compute=plane) == "completed"
    with factory() as db:
        t = worker_app.get_task_record(db, algo_task.id)
        assert t is not None and t.status == "completed" and t.business_outcome == "normal_completion"


# ---------------------------------------------------------------------------
# Item 4: Zero-copy os.link & direct frozen object handoff
# ---------------------------------------------------------------------------


def test_stage_runner_prepare_workspace_hardlinks_and_falls_back_on_oserror(
    memory_db: tuple[Engine, sessionmaker[Session]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _eng, factory = memory_db
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    with factory() as db:
        handle = put_object(db, b"step,p\n0,1.5\n", "text/csv", "dataset_file")
        db.commit()
        content = get_object_content(db, handle.id)
        src_path = getattr(content, "path", None)
        assert isinstance(src_path, Path) and src_path.is_file()

    stage_root = tmp_path / "stages"
    client = JobRunnerClient(
        "http://job_runner:8090",
        client=httpx.Client(
            base_url="http://job_runner:8090",
            transport=httpx.MockTransport(lambda _req: httpx.Response(200, json={"items": []})),
        ),
    )
    sr = ContainerStageRunner(client, stage_root=stage_root, policy=_default_policy(), poll_interval=0.01)

    # Pre-create stale target to verify safe replacement via os.link
    pre_target = stage_root / "10--gen" / "in" / "objects" / f"{handle.id}"
    pre_target.parent.mkdir(parents=True, exist_ok=True)
    pre_target.write_bytes(b"stale")

    spec = StageExecutionSpec(
        attempt_id="10",
        stage_id="gen",
        fencing_token="tok-10",
        input_files={
            f"objects/{handle.id}": content,
            "inline.txt": MemoryContent(b"hello"),
        },
    )
    sr._prepare_workspace("10--gen", spec)
    target = stage_root / "10--gen" / "in" / "objects" / f"{handle.id}"
    assert target.read_bytes() == b"step,p\n0,1.5\n"
    assert os.stat(target).st_ino == os.stat(src_path).st_ino
    assert (stage_root / "10--gen" / "in" / "inline.txt").read_bytes() == b"hello"

    # Cross-device OSError fallback to chunk streaming
    def fail_link(_src: Any, _dst: Any) -> None:
        raise OSError("EXDEV simulated")

    monkeypatch.setattr(os, "link", fail_link)
    sr._prepare_workspace("11--gen", spec)
    fallback_target = stage_root / "11--gen" / "in" / "objects" / f"{handle.id}"
    assert fallback_target.read_bytes() == b"step,p\n0,1.5\n"
    assert os.stat(fallback_target).st_ino != os.stat(src_path).st_ino


def test_load_compute_inputs_deferred_passes_frozen_series_inputs_and_hardlinks(
    memory_db: tuple[Engine, sessionmaker[Session]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_config_revisions import PROFILE_PAYLOAD

    from iesplan import configuration as config_domain
    from iesplan.application import configuration as config_cases

    _eng, factory = memory_db
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    with factory() as db:
        user = make_user(db, "frozen_user", role="admin")
        proj = project_domain.create_project(
            db, name="Frozen", description="", owner_id=user.id, created_by=user.id, baseline_start_year=2026
        )
        csv_handle = put_object(db, b"step,e_load\n0,10.0\n1,20.0\n", "text/csv", "dataset_file")
        price_bytes = (
            b"# schema: ies.price-series\n"
            b"# schema_version: 1.0.0\n"
            b"# resolution: 1h\n"
            b"# unit: CNY/kWh\n"
            b"step,price\n0,0.5\n"
        )
        price_handle = put_object(db, price_bytes, "text/csv", "dataset_file")
        profile_payload = {
            **PROFILE_PAYLOAD,
            "profile": {**PROFILE_PAYLOAD["profile"], "id": "cn-ts"},
            "energy_prices": {
                "grid": {
                    "carrier": "electricity",
                    "direction": "purchase",
                    "kind": "time_series",
                    "ref": price_handle.oid,
                    "series_meta": {"resolution": "1h", "point_count": 1, "unit": "CNY/kWh"},
                }
            },
        }
        config_cases.register_finance_profile(db, profile_payload, user.id)
        _, eff, _ = config_cases.set_project_finance_profile(db, proj.id, "cn-ts", user.id)
        cfg = config_domain.create_calculation_config(
            db,
            project_id=proj.id,
            content={
                "schema": "ies.calculation-config",
                "schema_version": "1.0.0",
                "mode": "planning",
                "predictions": [],
                "generator": "ies.generator.highs_milp@1.0.0",
                "solver": "ies.solver.highs@1.7.2",
                "algorithm": None,
                "evaluator": None,
                "precision": "medium",
                "convergence": None,
                "time_limit_seconds": 30,
                "options": {},
                "seed": 1,
                "outputs": ["solution"],
            },
            created_by=user.id,
        )
        ver = project_domain.create_version(
            db,
            project_id=proj.id,
            content_object_id=put_object(db, b"{}", "application/json", "project").id,
            name="v1",
            reason="manual",
            created_by=user.id,
        )
        canonical_doc = {
            "schema": "ies.assembly",
            "schema_version": "1.0.0",
            "assembly": {"id": "asm-f"},
            "project_baseline": {"resolution": "1h", "start_year": 2026},
            "devices": {
                "load1": {
                    "predefined_interfaces": {
                        "e_load": {
                            "mode": "data_repeat",
                            "content": {"object_id": str(csv_handle.id)},
                        }
                    }
                }
            },
            "tariff_bindings": {"b1": {"price": "grid"}},
        }
        receipt_doc = {
            "schema": "ies.validation-receipt",
            "schema_version": "1.0.0",
            "validator": {"id": "v", "version": "1.0.0"},
            "canonical_algorithm": {"id": "c", "version": "1.0.0"},
            "dependencies": {},
            "resources": {},
            "finance_snapshot": {"profile_id": eff.profile_id, "revision": eff.revision},
            "planning": {},
            "system_equations": {},
            "diagnostics": [],
        }
        snap = tasks_domain.create_snapshot(
            db,
            project_version_id=ver.id,
            dataset_version_ids=[],
            calc_config_snapshot={
                **cfg.content,
                "config_ref": {"project_id": proj.id, "revision": cfg.version},
            },
            random_seed=1,
            schema_version="1.0.0",
            result_adapter="ies.result-adapter.highs@1.0.0",
            executor="ies.executor.local_process@1.0.0",
            device_models={},
            evaluation={
                "evaluation_years": 1,
                "construction_period": 0,
                "operation_years": 1,
                "discount_rate": 0.08,
                "construction_payments": [],
                "maintenance_expenses": [],
            },
            canonical_assembly_text=json.dumps(canonical_doc),
            assembly_receipt=receipt_doc,
            created_by=user.id,
        )
        task = tasks_domain.create_task(
            db,
            project_id=proj.id,
            type="calc",
            idempotency_key="frz-1",
            requested_by=user.id,
            calc_snapshot_id=snap.id,
        )
        db.commit()
        claim = worker_app.acquire_attempt(db, task.id, "w-frz")
        assert claim is not None

        monkeypatch.setattr(
            compute_cases,
            "materialize_non_predictive_series",
            lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not materialize in backend")),
        )
        artifact, models, deferred, finance, config, evaluation = worker_app.load_compute_inputs(
            db, proj.id, snap, materialize_series=False
        )
        assert isinstance(deferred, worker_app.FrozenSeriesInputs)
        assert set(deferred.fixed_resources) == {str(csv_handle.id), price_handle.oid}
        pred_series = (
            MaterializedSeries(
                device="d_pred",
                interface="e_load",
                mode="data_predict",
                resolution="1h",
                point_count=2,
                values=(1.0, 2.0),
                source={"kind": "data_predict", "object_id": "999"},
            ),
        )
        completed = worker_app.complete_materialized(deferred, pred_series)

    seen_specs: list[StageExecutionSpec] = []

    class CaptureStageRunner:
        def __init__(self, stage_root: Path) -> None:
            self.stage_runner = ContainerStageRunner(
                JobRunnerClient(
                    "http://jr:8090",
                    client=httpx.Client(
                        base_url="http://jr:8090",
                        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={})),
                    ),
                ),
                stage_root=stage_root,
                policy=_default_policy(),
            )

        def run_stage(self, spec: StageExecutionSpec, *, persist: Any, stop: Any = None, detach: Any = None):
            seen_specs.append(spec)
            self.stage_runner._prepare_workspace(f"{spec.attempt_id}--{spec.stage_id}", spec)
            raise worker_app.LeaseRejectedError("stop after workspace staging")

    plane = worker_app.ComputeExecutionPlane(
        generator_registry=build_registry((HIGHS_MILP_PROVIDER,)),
        predictor_registry=build_predictor_registry((NAIVE_PREDICTOR,)),
        stage_runner=CaptureStageRunner(tmp_path / "stages"),
        stage_catalog=build_builtin_stage_catalog("pies-builtin-stages:1.1.5"),
    )
    attempt = AttemptContext(
        attempt_id=str(claim.attempt_id),
        bundle_id="b-1",
        assembly_id="asm-f",
        calculation_config_id="c-1",
    )
    with pytest.raises(worker_app.StageExecutionError, match="fenced"):
        worker_app.generate_bundle(
            plane,
            snap,
            artifact,
            models,
            completed,
            finance,
            config,
            evaluation,
            attempt=attempt,
            claim=claim,
            number=1,
            session_factory=factory,
            save_checkpoint=worker_app.save_bundle_checkpoint,
        )

    assert len(seen_specs) == 1
    spec = seen_specs[0]
    assert f"objects/{csv_handle.id}" in spec.input_files
    assert f"objects/{price_handle.oid}" in spec.input_files
    req_doc = json.loads(spec.input_files["request.json"].decode("utf-8"))
    assert "materialized" not in req_doc
    assert req_doc["frozen_series_inputs"] == {
        "fixed_resources": {
            str(csv_handle.id): f"objects/{csv_handle.id}",
            price_handle.oid: f"objects/{price_handle.oid}",
        },
        "predicted_series": [pred_series[0].to_dict()],
    }
    in_dir = tmp_path / "stages" / f"{claim.attempt_id}--generate-r1" / "in"
    assert (in_dir / "objects" / str(csv_handle.id)).is_file()
    assert (in_dir / "objects" / price_handle.oid).is_file()


# ---------------------------------------------------------------------------
# Item 5: Explicit schema & schema_version validation at result.json boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad_payload", "exc_type"),
    [
        (
            {
                "schema_version": "1.0.0",
                "bundle_id": "b-1",
                "receipt_id": "r-1",
                "execution_status": "completed",
                "math_status": "solved",
                "outputs": {},
            },
            KeyError,
        ),
        (
            {
                "schema": "ies.wrong",
                "schema_version": "1.0.0",
                "bundle_id": "b-1",
                "receipt_id": "r-1",
                "execution_status": "completed",
                "math_status": "solved",
                "outputs": {},
            },
            ValueError,
        ),
        (
            {
                "schema": "ies.compute-result",
                "schema_version": "9.9.9",
                "bundle_id": "b-1",
                "receipt_id": "r-1",
                "execution_status": "completed",
                "math_status": "solved",
                "outputs": {},
            },
            ValueError,
        ),
    ],
)
def test_result_json_boundary_requires_valid_schema_and_version(
    memory_db: tuple[Engine, sessionmaker[Session]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_payload: dict[str, Any],
    exc_type: type[Exception],
) -> None:
    _eng, factory = memory_db
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    with factory() as db:
        _user, _proj, task = _create_project_and_task(
            db, username=f"schema_{exc_type.__name__}", idempotency_key=f"s-{exc_type.__name__}"
        )
        db.commit()
        claim = worker_app.acquire_attempt(db, task.id, "w-s")
        assert claim is not None

    stage_root = tmp_path / "stages"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        if request.method == "GET" and request.url.path == "/v1/stages":
            return httpx.Response(200, json={"items": [], "total": 0})
        if request.method == "POST" and request.url.path == "/v1/stages/start":
            out_dir = stage_root / body["workspace_rel"] / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "result.json").write_text(json.dumps(bad_payload), encoding="utf-8")
            return httpx.Response(
                200,
                json=_stage_payload(
                    stage_execution_id=body["stage_execution_id"],
                    attempt_id=body["attempt_id"],
                    stage_id=body["stage_id"],
                    fencing_token=body["fencing_token"],
                    status="exited",
                    running=False,
                    exit_code=0,
                    termination_reason="normal",
                    output_files=["result.json"],
                ),
            )
        if request.method == "GET" and request.url.path.startswith("/v1/stages/"):
            seid = request.url.path.split("/")[-1]
            return httpx.Response(
                200,
                json=_stage_payload(
                    stage_execution_id=seid,
                    attempt_id=str(claim.attempt_id),
                    stage_id=seid.split("--", 1)[-1],
                    fencing_token=str(claim.lease_token),
                    status="exited",
                    running=False,
                    exit_code=0,
                    termination_reason="normal",
                    output_files=["result.json"],
                ),
            )
        raise AssertionError(
            f"Unexpected call (cleanup must not run on bad result): {request.method} {request.url}"
        )

    client = JobRunnerClient(
        "http://job_runner:8090",
        client=httpx.Client(base_url="http://job_runner:8090", transport=httpx.MockTransport(handler)),
    )
    sr = ContainerStageRunner(client, stage_root=stage_root, policy=_default_policy(), poll_interval=0.01)
    base_descs = list(build_builtin_stage_catalog("pies-builtin-stages:1.1.5").snapshot())
    base_descs.append(
        BuiltinStageDescriptor(
            plugin_id="acme.algorithm.demo",
            version="1.0.0",
            kind="algorithm",
            image="pies-plugin/acme:1.0.0",
            entrypoint="algo:run",
        )
    )
    plane = worker_app.ComputeExecutionPlane(
        generator_registry=build_registry((HIGHS_MILP_PROVIDER,)),
        predictor_registry=build_predictor_registry((NAIVE_PREDICTOR,)),
        stage_runner=sr,
        stage_catalog=BuiltinStageCatalog(tuple(base_descs)),
    )
    manifest = SolverBundleManifest(
        bundle={"generator": "ies.generator.highs_milp@1.0.0", "solver": "ies.solver.highs@1.7.2"},
        inputs=[{"path": "input/problem.mps", "media_type": "text/plain"}],
        command={
            "executor": "ies.executor.local_process@1.0.0",
            "executable": "ies.solver.highs@1.7.2",
            "arguments": ["input/problem.mps"],
            "timeout_seconds": 30,
            "network": False,
        },
        outputs=[{"path": "output/solution.sol", "media_type": "text/plain", "required": True}],
        result_adapter="ies.result-adapter.highs@1.0.0",
    )
    bundle = SolverBundle(manifest, {"input/problem.mps": MemoryContent(b"NAME X\nENDATA\n")})
    receipt = ExecutionReceipt(
        receipt_id="r-1",
        bundle_id="b-1",
        assembly_id="a-1",
        calculation_config_id="c-1",
        attempt_id=str(claim.attempt_id),
        generator_ref="ies.generator.highs_milp@1.0.0",
        solver_ref="ies.solver.highs@1.7.2",
        executor_ref="ies.executor.local_process@1.0.0",
        executable_ref="ies.solver.highs@1.7.2",
        result_adapter_ref="ies.result-adapter.highs@1.0.0",
        status="succeeded",
        exit_code=0,
        termination_reason="exited",
        started_at="2026-11-24T08:00:00Z",
        ended_at="2026-11-24T08:00:01Z",
    )
    fake_finance = SimpleNamespace(to_dict=lambda: {"profile_id": "p"})
    fake_snapshot = SimpleNamespace(
        assembly_receipt={"schema": "ies.validation-receipt"},
        device_models={},
        evaluation={"evaluation_years": 1},
    )

    with pytest.raises(exc_type):
        worker_app.adapt_result(
            plane,
            bundle,
            receipt,
            {"output/solution.sol": MemoryContent(b"HIGHS_SOLUTION_V1\n")},
            claim=claim,
            number=1,
            finance=fake_finance,  # type: ignore[arg-type]
            snapshot=fake_snapshot,
            session_factory=factory,
            save_checkpoint=save_evaluation_checkpoint,
        )

    algo_cfg = CalculationConfig(
        mode="planning",
        algorithm="acme.algorithm.demo@1.0.0",
        precision="medium",
        time_limit_seconds=60,
        outputs=("solution",),
    )
    with pytest.raises(exc_type):
        worker_app.run_algorithm_stage(
            plane=plane,
            attempt=AttemptContext(
                attempt_id=str(claim.attempt_id),
                bundle_id="b-1",
                assembly_id="a-1",
                calculation_config_id="c-1",
            ),
            claim=claim,
            snapshot=fake_snapshot,
            artifact=SimpleNamespace(canonical_text="{}"),  # type: ignore[arg-type]
            materialized=MaterializedSet(operation_years=1, series=(), pending_predict=(), prices=()),
            finance=fake_finance,  # type: ignore[arg-type]
            config=algo_cfg,
            session_factory=factory,
            save_checkpoint=save_algorithm_checkpoint,
        )


# ---------------------------------------------------------------------------
# Item 6: Non-blocking control-plane detach on shutdown
# ---------------------------------------------------------------------------


def test_control_plane_detach_preserves_live_container_and_active_attempt(
    memory_db: tuple[Engine, sessionmaker[Session]],
    tmp_path: Path,
) -> None:
    _eng, factory = memory_db
    with factory() as db:
        _user, _proj, task = _create_project_and_task(
            db, username="detach_user", idempotency_key="detach-1"
        )
        db.commit()
        claim = worker_app.acquire_attempt(db, task.id, "w-detach")
        assert claim is not None

    stage_root = tmp_path / "stages"
    stop_calls: list[str] = []
    cleanup_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/stages":
            return httpx.Response(200, json={"items": [], "total": 0})
        if request.method == "POST" and request.url.path == "/v1/stages/start":
            body = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json=_stage_payload(
                    stage_execution_id=body["stage_execution_id"],
                    attempt_id=body["attempt_id"],
                    stage_id=body["stage_id"],
                    fencing_token=body["fencing_token"],
                    status="running",
                    running=True,
                    exit_code=None,
                    termination_reason=None,
                ),
            )
        if request.method == "GET" and request.url.path.startswith("/v1/stages/"):
            seid = request.url.path.split("/")[-1]
            return httpx.Response(
                200,
                json=_stage_payload(
                    stage_execution_id=seid,
                    attempt_id=str(claim.attempt_id),
                    stage_id="solve-r1",
                    fencing_token=str(claim.lease_token),
                    status="running",
                    running=True,
                    exit_code=None,
                    termination_reason=None,
                ),
            )
        if request.method == "POST" and request.url.path.endswith("/stop"):
            stop_calls.append(request.url.path)
            return httpx.Response(200, json={})
        if request.method == "POST" and request.url.path.endswith("/cleanup"):
            cleanup_calls.append(request.url.path)
            return httpx.Response(200, json={})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = JobRunnerClient(
        "http://job_runner:8090",
        client=httpx.Client(base_url="http://job_runner:8090", transport=httpx.MockTransport(handler)),
    )
    sr = ContainerStageRunner(client, stage_root=stage_root, policy=_default_policy(), poll_interval=5.0)

    detach_ev = threading.Event()
    timer = threading.Timer(0.05, detach_ev.set)
    timer.start()
    try:
        with pytest.raises(ControlPlaneDetached):
            sr.run_stage(
                StageExecutionSpec(
                    attempt_id=str(claim.attempt_id),
                    stage_id="solve-r1",
                    fencing_token=str(claim.lease_token),
                    timeout_seconds=30.0,
                ),
                persist=lambda _r: None,
                detach=detach_ev,
            )
    finally:
        timer.cancel()

    assert stop_calls == []
    assert cleanup_calls == []

    # Also verify runner.run_task translates ControlPlaneDetached -> "detached" without touching Attempt
    def fake_dispatch(ctx: Any) -> tuple[dict, dict | None]:
        assert ctx.detach_event is detach_ev
        raise ControlPlaneDetached("detaching")

    original_dispatch = runner.dispatch
    runner.dispatch = fake_dispatch  # type: ignore[assignment]
    try:
        outcome = runner.run_task(factory, claim, worker_id="w-detach", detach_event=detach_ev)
    finally:
        runner.dispatch = original_dispatch  # type: ignore[assignment]

    assert outcome == "detached"
    with factory() as db:
        reconcilable = worker_app.list_active_reconcilable_claims(db)
        assert [c.attempt_id for c in reconcilable] == [claim.attempt_id]


def test_embedded_scheduler_stop_never_blocks_past_timeout(
    memory_db: tuple[Engine, sessionmaker[Session]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _eng, factory = memory_db
    tasks_domain.force_memory()
    with factory() as db:
        _user, _proj, task = _create_project_and_task(
            db, username="stop_timeout_user", task_type="report", idempotency_key="stop-to-1"
        )
        tasks_domain.enqueue(task.id, "io")
        db.commit()

    task_entered = threading.Event()
    unblock_task = threading.Event()

    def stuck_run_task(_factory: Any, _claim: Any, **kwargs: Any) -> str:
        assert isinstance(kwargs.get("detach_event"), threading.Event)
        task_entered.set()
        unblock_task.wait(timeout=10.0)
        return "completed"

    monkeypatch.setattr("iesplan.worker.scheduler.runner.run_task", stuck_run_task)
    scheduler = EmbeddedScheduler(
        session_factory=factory,
        compute_slots=1,
        io_slots=1,
        build_slots=1,
        poll_interval=0.02,
    )
    scheduler.start()
    try:
        assert task_entered.wait(timeout=3.0)
        start = time.monotonic()
        scheduler.stop(timeout=0.25)
        elapsed = time.monotonic() - start
        assert scheduler.shutdown_event.is_set()
        assert elapsed < 1.0, f"scheduler.stop blocked past timeout: {elapsed:.2f}s"
    finally:
        unblock_task.set()
