"""F4 tests: atomic 1.2.0 contract switch, unidirectional pipeline, parallel evaluators, retry & migration."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from auth_helpers import login_headers, make_user
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from stage_testkit import build_test_compute_plane, build_test_stage_catalog
from test_tasks_api import (
    _submit_task,
)
from test_worker_compute_chain import PLANNING_BODY, _claim, _prepare_with_planning, _run

from iesplan import bootstrap
from iesplan.api import projects as projects_api
from iesplan.api import results as results_api
from iesplan.api import tasks as tasks_api
from iesplan.api.auth import router as auth_router
from iesplan.api.config import config_router
from iesplan.api.deps import get_request_db
from iesplan.application import identity
from iesplan.application import worker as worker_app
from iesplan.computation import (
    CALCULATION_CONFIG_FIELDS,
    EVALUATION_REPORT_CONTRACT,
    HIGHS_MILP_PROVIDER,
    NAIVE_PREDICTOR,
    AlgorithmDescriptor,
    StagePluginSpec,
    build_algorithm_registry,
    build_evaluator_registry,
    build_predictor_registry,
    build_registry,
    build_solver_registry,
)
from iesplan.computation.config import (
    BUILTIN_EVALUATOR_FINANCE_SPEC,
    BUILTIN_SOLVER_HIGHS_SPEC,
)
from iesplan.computation.runtime.stage_runner import (
    BuiltinStageDescriptor,
    build_builtin_stage_catalog,
)
from iesplan.config import settings
from iesplan.db import Base
from iesplan.main import _register_exception_handlers
from iesplan.migrations import (
    MIGRATION_VERSION,
    UpgradeDrainError,
    apply_migrations,
    convert_calc_config_115_to_120,
)
from iesplan.results import persistence as results_store
from iesplan.tasks import persistence as tasks_store
from iesplan.tasks import queue
from iesplan.tasks.persistence import CalcSnapshot, TaskAttempt
from iesplan.worker.executors import RunContext


def _valid_13_config(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "schema": "ies.calculation-config",
        "schema_version": "1.0.0",
        "mode": "planning",
        "predictions": [],
        "generator": "ies.generator.highs_milp@1.0.0",
        "solver": "ies.solver.highs@1.7.2",
        "algorithm": None,
        "evaluators": [{"evaluator": "ies.evaluator.finance@1.0.0", "parameters": {}}],
        "precision": "medium",
        "time_limit_seconds": 60,
        "options": {"generator": {"acceleration": {"mode": "on_demand_mutex"}}, "solver": {}},
        "seed": 7,
        "outputs": ["solution", "metrics"],
    }
    cfg.update(overrides)
    return cfg


def _make_api_app(db: Session, bootstrap_ctx: Any) -> FastAPI:
    app = FastAPI(title="pIES F4 Test")
    for r in (
        auth_router,
        projects_api.router,
        config_router,
        tasks_api.router,
        results_api.router,
    ):
        app.include_router(r)
    app.state.bootstrap_context = bootstrap_ctx
    _register_exception_handlers(app)
    app.dependency_overrides[get_request_db] = lambda: db
    return app


def _new_test_db(tmp_path: Path) -> tuple[Any, sessionmaker[Session]]:
    eng = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'f4.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    return eng, sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)


# ---------------------------------------------------------------------------
# 1. Zero-legacy switch & bootstrap registries
# ---------------------------------------------------------------------------


def test_single_120_contract_and_legacy_modules_removed() -> None:
    """1.2.0 switch removes legacy target_* shims & 1.1.5 result_adapter modules."""
    assert len(CALCULATION_CONFIG_FIELDS) == 13
    assert "convergence" not in CALCULATION_CONFIG_FIELDS
    assert "evaluator" not in CALCULATION_CONFIG_FIELDS
    assert "evaluators" in CALCULATION_CONFIG_FIELDS

    for removed in (
        "iesplan.computation.result_adapter",
        "iesplan.computation.result_adapters.highs",
        "iesplan.computation.result_adapters.declarations",
        "iesplan.computation.generators.target_bundle",
        "iesplan.computation.result_adapters.target_adapter",
        "iesplan.computation.contracts.calculation_config",
        "iesplan.computation.contracts.solver_bundle",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(removed)

    catalog = build_builtin_stage_catalog("pies-builtin-stages:1.2.0")
    refs = [d.ref for d in catalog.snapshot()]
    assert "ies.evaluator.finance@1.0.0" in refs
    assert "ies.result-adapter.highs@1.0.0" not in refs

    eval_desc = catalog.get_by_ref("ies.evaluator.finance@1.0.0")
    assert eval_desc.image == "pies-builtin-stages:1.2.0"
    assert eval_desc.entrypoint == "finance_evaluator_stage:evaluate"
    assert eval_desc.manifest_path == "/app/plugins/evaluator_finance/plugin.yaml"


def test_bootstrap_exposes_all_five_registries(monkeypatch: pytest.MonkeyPatch) -> None:
    """bootstrap exposes Generator/Predictor/Solver/Evaluator/Algorithm registries."""
    from iesplan.config import settings

    monkeypatch.setattr(settings, "job_runner_url", "")
    ctx = bootstrap.assemble_api()
    assert ctx.generator_registry is not None
    assert ctx.predictor_registry is not None
    assert ctx.solver_registry is not None
    assert ctx.evaluator_registry is not None
    assert ctx.algorithm_registry is not None
    assert [s.ref for s in ctx.solver_registry.snapshot()] == ["ies.solver.highs@1.7.2"]
    assert [e.ref for e in ctx.evaluator_registry.snapshot()] == ["ies.evaluator.finance@1.0.0"]


def test_config_api_validates_13_field_evaluators_and_slotted_options(tmp_path: Path) -> None:
    """PUT /config rejects convergence, bad evaluator parameters, and invalid slotted options."""
    eng, sf = _new_test_db(tmp_path)
    try:
        with sf() as db:
            identity.create_user(db, "cfguser", "Pass123456", role="engineer", force_password_change=False)
            owner = db.execute(text("SELECT id FROM users WHERE username='cfguser'")).scalar_one()
            db.execute(
                text(
                    "INSERT INTO projects (name, currency, baseline_resolution, baseline_start_year, "
                    "baseline_scenario_mode, owner_id, created_by) "
                    "VALUES ('p1', 'CNY', '1h', 2026, 'single', :uid, :uid)"
                ),
                {"uid": owner},
            )
            pid = int(db.execute(text("SELECT id FROM projects")).scalar_one())
            db.commit()

            ctx = SimpleNamespace(
                predictor_registry=build_predictor_registry((NAIVE_PREDICTOR,)),
                generator_registry=build_registry((HIGHS_MILP_PROVIDER,)),
                solver_registry=build_solver_registry((BUILTIN_SOLVER_HIGHS_SPEC,)),
                evaluator_registry=build_evaluator_registry((BUILTIN_EVALUATOR_FINANCE_SPEC,)),
                algorithm_registry=build_algorithm_registry(()),
            )
            client = TestClient(_make_api_app(db, ctx), raise_server_exceptions=False)
            login = client.post("/api/auth/login", json={"username": "cfguser", "password": "Pass123456"})
            assert login.status_code == 200

            # Unknown evaluator rejected with location.field == "evaluators".
            bad_eval = _valid_13_config(
                evaluators=[{"evaluator": "acme.missing@1.0.0", "parameters": {}}]
            )
            resp = client.put(
                f"/api/projects/{pid}/config",
                json={"calculation_config": bad_eval, "expected_revision": None},
            )
            assert resp.status_code == 422
            assert resp.json()["error"]["code"] == "CONFIG-VAL-001"
            assert resp.json()["error"]["location"]["field"] == "evaluators"

            # Legacy convergence rejected cleanly.
            with_conv = {
                **_valid_13_config(),
                "convergence": {"tolerance": 0.01, "max_iterations": 5},
            }
            resp2 = client.put(
                f"/api/projects/{pid}/config",
                json={"calculation_config": with_conv, "expected_revision": None},
            )
            assert resp2.status_code == 422

            # Valid 13-field save roundtrips cleanly.
            ok = client.put(
                f"/api/projects/{pid}/config",
                json={"calculation_config": _valid_13_config(), "expected_revision": None},
            )
            assert ok.status_code == 200, ok.text
            assert ok.json()["revision"] == 1
            assert ok.json()["calculation_config"] == _valid_13_config()
    finally:
        eng.dispose()


# ---------------------------------------------------------------------------
# 2. Intent-preserving 1.1.5 -> 1.2.0 migration & drain check
# ---------------------------------------------------------------------------


def test_convert_calc_config_115_to_120_preserves_intent() -> None:
    """Intent-preserving conversion per calculation-config.md:78."""
    base_115 = {
        "schema": "ies.calculation-config",
        "schema_version": "1.0.0",
        "mode": "planning",
        "predictions": [],
        "generator": "ies.generator.highs_milp@1.0.0",
        "solver": "ies.solver.highs@1.7.2",
        "algorithm": None,
        "evaluator": "ies.result-adapter.highs@1.0.0",
        "precision": "medium",
        "convergence": {"tolerance": 0.001, "max_iterations": 20},
        "time_limit_seconds": 300,
        "options": {"acceleration": {"mode": "off"}},
        "seed": 42,
        "outputs": ["solution", "metrics"],
    }
    c1 = convert_calc_config_115_to_120(base_115)
    assert c1["evaluators"] == [{"evaluator": "ies.evaluator.finance@1.0.0", "parameters": {}}]
    assert c1["options"] == {"generator": {"acceleration": {"mode": "off"}}}
    assert "convergence" not in c1 and "evaluator" not in c1

    # Stepped default binding (evaluator: None) with "solution" output maps to finance evaluator.
    c2 = convert_calc_config_115_to_120({**base_115, "evaluator": None})
    assert c2["evaluators"] == [{"evaluator": "ies.evaluator.finance@1.0.0", "parameters": {}}]

    # Stepped evaluator: None without "solution" (metrics-only) preserves empty evaluators.
    c3 = convert_calc_config_115_to_120({**base_115, "evaluator": None, "outputs": ["metrics"]})
    assert c3["evaluators"] == []

    # Unified algorithm evaluator: None preserves evaluators: [] and moves options into options.algorithm.
    c4 = convert_calc_config_115_to_120(
        {
            **base_115,
            "generator": None,
            "solver": None,
            "algorithm": "acme.algo@1.0.0",
            "evaluator": None,
            "options": {"steps": 10},
        }
    )
    assert c4["evaluators"] == []
    assert c4["options"] == {"algorithm": {"steps": 10}}


def test_migration_drain_gate_and_readonly_history(tmp_path: Path) -> None:
    """Migration blocks on active tasks, migrates calc_configs, leaves snapshots untouched."""
    settings.data_dir = tmp_path / "mig_data"
    eng, sf = _new_test_db(tmp_path)
    try:
        with sf() as db:
            from iesplan.storage import put_object

            identity.create_user(
                db, "miguser", "Pass123456", role="engineer", force_password_change=False
            )
            uid = int(db.execute(text("SELECT id FROM users")).scalar_one())
            db.execute(
                text(
                    "INSERT INTO projects (name, currency, baseline_resolution, baseline_start_year, "
                    "baseline_scenario_mode, owner_id, created_by) "
                    "VALUES ('pmig', 'CNY', '1h', 2026, 'single', :uid, :uid)"
                ),
                {"uid": uid},
            )
            pid = int(db.execute(text("SELECT id FROM projects")).scalar_one())
            obj = put_object(
                db, b"{}", content_type="application/json", source_category="project", actor_id=uid
            )
            db.execute(
                text(
                    "INSERT INTO project_versions (project_id, version_no, name, created_by, "
                    "reason, baseline_resolution, baseline_start_year, baseline_scenario_mode, "
                    "schema_version, content_object_id) "
                    "VALUES (:pid, 1, 'v1', :uid, 'freeze', '1h', 2026, 'single', 1, :oid)"
                ),
                {"pid": pid, "uid": uid, "oid": obj.id},
            )
            pvid = int(db.execute(text("SELECT id FROM project_versions")).scalar_one())
            legacy_cfg = {
                "schema": "ies.calculation-config",
                "schema_version": "1.0.0",
                "mode": "planning",
                "predictions": [],
                "generator": "ies.generator.highs_milp@1.0.0",
                "solver": "ies.solver.highs@1.7.2",
                "algorithm": None,
                "evaluator": "ies.result-adapter.highs@1.0.0",
                "precision": "medium",
                "convergence": None,
                "time_limit_seconds": 60,
                "options": {"acceleration": {"mode": "on_demand_mutex"}},
                "seed": 11,
                "outputs": ["solution", "metrics"],
            }
            db.execute(
                text(
                    "INSERT INTO calc_configs (project_id, version, content, created_by) "
                    "VALUES (:pid, 1, :c, :uid)"
                ),
                {"pid": pid, "c": json.dumps(legacy_cfg), "uid": uid},
            )
            db.execute(
                text(
                    "INSERT INTO calc_snapshots (project_version_id, dataset_version_ids, "
                    "calc_config_snapshot, schema_version, result_adapter, executor, "
                    "extension_versions, random_seed, device_models, evaluation, created_by) "
                    "VALUES (:pvid, '[]', :c, '1.0.0', 'ies.result-adapter.highs@1.0.0', "
                    "'ies.executor.local_process@1.0.0', '{}', 11, '{}', '{}', :uid)"
                ),
                {"pvid": pvid, "c": json.dumps(legacy_cfg), "uid": uid},
            )
            snap_id = int(db.execute(text("SELECT id FROM calc_snapshots")).scalar_one())
            db.execute(
                text(
                    "INSERT INTO tasks (project_id, type, status, requested_by, "
                    "idempotency_key, calc_snapshot_id) "
                    "VALUES (:pid, 'calc', 'running', :uid, 'old-running-1', :sid)"
                ),
                {"pid": pid, "uid": uid, "sid": snap_id},
            )
            db.commit()

        # Drain check rejects running task!
        with pytest.raises(UpgradeDrainError, match="running"):
            apply_migrations(eng)

        # Drain task -> completed; re-run migration succeeds and is idempotent.
        with eng.begin() as conn:
            conn.execute(
                text(
                    "UPDATE tasks SET status = 'completed', "
                    "business_outcome = 'normal_completion'"
                )
            )
        applied = apply_migrations(eng)
        assert MIGRATION_VERSION in applied
        assert apply_migrations(eng) == []

        # Verify calc_configs migrated to 13-field, calc_snapshots untouched!
        with eng.begin() as conn:
            cc_raw = json.loads(
                conn.execute(text("SELECT content FROM calc_configs")).scalar_one()
            )
            snap_raw = json.loads(
                conn.execute(text("SELECT calc_config_snapshot FROM calc_snapshots")).scalar_one()
            )
        assert set(cc_raw) == set(CALCULATION_CONFIG_FIELDS)
        assert cc_raw["evaluators"] == [
            {"evaluator": "ies.evaluator.finance@1.0.0", "parameters": {}}
        ]
        assert "convergence" in snap_raw
        assert snap_raw["evaluator"] == "ies.result-adapter.highs@1.0.0"
    finally:
        eng.dispose()


# ---------------------------------------------------------------------------
# 3. Real pipeline, os.link hardlinks, parallel evaluators & single-evaluator retry
# ---------------------------------------------------------------------------


def _setup_pipeline_env(
    tmp_path: Path,
    *,
    extra_evaluators: tuple[StagePluginSpec, ...] = (),
    extra_solvers: tuple[StagePluginSpec, ...] = (),
    extra_algorithms: tuple[Any, ...] = (),
):
    settings.data_dir = tmp_path / "data"
    queue.force_memory()
    eng = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    sf = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    db = sf()
    ctx = SimpleNamespace(
        predictor_registry=build_predictor_registry((NAIVE_PREDICTOR,)),
        generator_registry=build_registry((HIGHS_MILP_PROVIDER,)),
        solver_registry=build_solver_registry((BUILTIN_SOLVER_HIGHS_SPEC, *extra_solvers)),
        evaluator_registry=build_evaluator_registry(
            (BUILTIN_EVALUATOR_FINANCE_SPEC, *extra_evaluators)
        ),
        algorithm_registry=build_algorithm_registry(extra_algorithms),
    )
    client = TestClient(_make_api_app(db, ctx), raise_server_exceptions=False)
    user = make_user(db, f"f4_user_{id(tmp_path)}")
    pid = _prepare_with_planning(client, db, user, dict(PLANNING_BODY))
    return {"engine": eng, "sf": sf, "db": db, "client": client, "user": user, "pid": pid}


def _save_and_submit(
    env: dict[str, Any], cfg: dict[str, Any], *, idempotency_key: str
) -> int:
    headers = login_headers(env["client"], env["user"])
    put_resp = env["client"].put(
        f"/api/projects/{env['pid']}/config",
        json={"calculation_config": cfg, "expected_revision": None},
        headers=headers,
    )
    assert put_resp.status_code == 200, put_resp.text
    rev = int(put_resp.json()["revision"])
    status, body = _submit_task(
        env["client"],
        env["pid"],
        env["user"],
        config={"config_revision": rev},
        idempotency_key=idempotency_key,
    )
    assert status == 201, body
    return int(body["task"]["id"])


def test_parallel_evaluators_partial_failure_single_retry_and_hardlinks(tmp_path: Path) -> None:
    """Real stepped pipeline + os.link + 2 parallel evaluators (1 fails -> retry succeeds)."""
    report_eval_ref = "acme.evaluator.report@1.0.0"
    custom_eval_spec = StagePluginSpec(
        plugin_id="acme.evaluator.report",
        version="1.0.0",
        kind="evaluator",
        consumes=("ies.engineering-solution@1.0.0",),
        produces=(EVALUATION_REPORT_CONTRACT,),
        outputs=("solution", "metrics"),
        parameters_schema={"type": "object", "additionalProperties": False, "properties": {}},
    )
    custom_stage_desc = BuiltinStageDescriptor(
        plugin_id="acme.evaluator.report",
        version="1.0.0",
        kind="evaluator",
        image="pies-builtin-stages:1.2.0",
        entrypoint="report:eval",
        manifest_path="test://acme.evaluator.report@1.0.0",
    )

    fail_finance = {"should_fail": True}
    report_calls = {"count": 0}
    seen_solution_nlinks: list[int] = []

    def _custom_report_handler(work_in: Path, work_out: Path) -> None:
        report_calls["count"] += 1
        sol_file = work_in / "solution.json"
        assert sol_file.is_file()
        seen_solution_nlinks.append(sol_file.stat().st_nlink)
        report_doc = {
            "schema": "ies.evaluation-report",
            "schema_version": "1.0.0",
            "title": "Carbon Intensity Report",
            "evaluator": report_eval_ref,
            "metrics": [{"id": "co2_tons", "label": "CO2 Tons", "value": 12.5, "unit": "t"}],
            "charts": [],
            "tables": [],
        }
        (work_out / "evaluation_report.json").write_text(json.dumps(report_doc), encoding="utf-8")

    def _finance_or_fail_handler(work_in: Path, work_out: Path) -> None:
        if fail_finance["should_fail"]:
            raise RuntimeError("simulated transient finance evaluator failure")
        sol_file = work_in / "solution.json"
        seen_solution_nlinks.append(sol_file.stat().st_nlink)
        from stage_testkit import _artifact_from_json

        from iesplan.finance import (
            EffectiveFinanceConfig,
            EvaluationInput,
            evaluate_forward_finance,
        )

        sol = json.loads(sol_file.read_text(encoding="utf-8"))
        req = json.loads((work_in / "request.json").read_text(encoding="utf-8"))
        fin_eval = evaluate_forward_finance(
            sol,
            artifact=_artifact_from_json(req["artifact"]),
            finance=EffectiveFinanceConfig.from_dict(req["finance"]),
            evaluation=EvaluationInput.from_frozen(req["evaluation"]),
            device_models=req.get("device_models"),
        )
        (work_out / "financial_evaluation.json").write_text(json.dumps(fin_eval), encoding="utf-8")

    env = _setup_pipeline_env(tmp_path, extra_evaluators=(custom_eval_spec,))
    try:
        eval_reg = build_evaluator_registry((BUILTIN_EVALUATOR_FINANCE_SPEC, custom_eval_spec))
        catalog = build_test_stage_catalog(extra_descriptors=(custom_stage_desc,))
        plane = build_test_compute_plane(
            tmp_path / "stages",
            evaluator_registry=eval_reg,
            stage_catalog=catalog,
            evaluator_handlers={
                "/app/plugins/evaluator_finance/plugin.yaml": _finance_or_fail_handler,
                "test://acme.evaluator.report@1.0.0": _custom_report_handler,
            },
        )
        cfg = _valid_13_config(
            evaluators=[
                {"evaluator": "ies.evaluator.finance@1.0.0", "parameters": {}},
                {"evaluator": report_eval_ref, "parameters": {}},
            ]
        )
        task_id = _save_and_submit(env, cfg, idempotency_key="f4-parallel-1")
        claim1 = _claim(env["db"], task_id)
        outcome1 = _run(env["engine"], claim1, plane)
        assert outcome1 == "completed"
        assert report_calls["count"] == 1
        assert any(n >= 2 for n in seen_solution_nlinks), (
            f"Expected os.link hardlink (st_nlink >= 2), observed {seen_solution_nlinks}"
        )

        headers = login_headers(env["client"], env["user"])
        res1 = env["client"].get(
            f"/api/projects/{env['pid']}/tasks/{task_id}/result", headers=headers
        )
        assert res1.status_code == 200, res1.text
        body1 = res1.json()["result"]
        old_ev_id = body1["evidence"]["id"]
        old_obj_id = body1["evidence"]["object_id"]
        assert body1["financial_evaluation"] is None
        assert body1["financial_absence"] == "evaluator_failed"
        assert len(body1["evaluation_reports"]) == 1
        assert body1["evaluation_reports"][0]["evaluator"] == report_eval_ref
        by_eval1 = {e["evaluator"]: e for e in body1["evaluator_results"]}
        assert by_eval1["ies.evaluator.finance@1.0.0"]["status"] == "failed"
        assert by_eval1["ies.evaluator.finance@1.0.0"]["error"] is not None
        assert by_eval1[report_eval_ref]["status"] == "succeeded"

        # Succeeded evaluator is non-retryable -> 409.
        bad_retry = env["client"].post(
            f"/api/projects/{env['pid']}/tasks/{task_id}/result/evaluators/retry",
            json={"evaluator": report_eval_ref, "idempotency_key": "retry-ok-eval"},
            headers=headers,
        )
        assert bad_retry.status_code == 409, bad_retry.text

        # Trigger single-evaluator retry -> 201 child analysis task, replay -> 200.
        retry_resp = env["client"].post(
            f"/api/projects/{env['pid']}/tasks/{task_id}/result/evaluators/retry",
            json={
                "evaluator": "ies.evaluator.finance@1.0.0",
                "idempotency_key": "retry-fin-1",
            },
            headers=headers,
        )
        assert retry_resp.status_code == 201, retry_resp.text
        retry_body = retry_resp.json()
        retry_task_id = retry_body["task"]["id"]
        assert retry_body["replayed"] is False
        assert retry_body["source_task_id"] == task_id
        assert retry_body["evaluator"] == "ies.evaluator.finance@1.0.0"
        assert retry_task_id != task_id
        assert retry_body["task"]["type"] == "analysis"
        assert retry_body["task"]["status"] == "queued"

        # Source task remains completed & usable as baseline while child retry is active.
        source_task_resp = env["client"].get(
            f"/api/projects/{env['pid']}/tasks/{task_id}", headers=headers
        )
        assert source_task_resp.status_code == 200
        assert source_task_resp.json()["task"]["status"] == "completed"
        baseline_during_retry = env["client"].get(
            f"/api/projects/{env['pid']}/tasks/{task_id}/result", headers=headers
        )
        assert baseline_during_retry.status_code == 200
        assert baseline_during_retry.json()["result"]["evidence"]["id"] == old_ev_id

        # Active child retry blocks a different concurrent retry key with 409.
        dup_retry = env["client"].post(
            f"/api/projects/{env['pid']}/tasks/{task_id}/result/evaluators/retry",
            json={
                "evaluator": "ies.evaluator.finance@1.0.0",
                "idempotency_key": "retry-fin-concurrent",
            },
            headers=headers,
        )
        assert dup_retry.status_code == 409, dup_retry.text

        replay_resp = env["client"].post(
            f"/api/projects/{env['pid']}/tasks/{task_id}/result/evaluators/retry",
            json={
                "evaluator": "ies.evaluator.finance@1.0.0",
                "idempotency_key": "retry-fin-1",
            },
            headers=headers,
        )
        assert replay_resp.status_code == 200, replay_resp.text
        assert replay_resp.json()["replayed"] is True
        assert replay_resp.json()["task"]["id"] == retry_task_id
        assert replay_resp.json()["source_task_id"] == task_id

        # Run child analysis retry -> only finance runs; source stays completed.
        fail_finance["should_fail"] = False
        claim2 = _claim(env["db"], retry_task_id)
        outcome2 = _run(env["engine"], claim2, plane)
        assert outcome2 == "completed"
        assert report_calls["count"] == 1

        res2 = env["client"].get(
            f"/api/projects/{env['pid']}/tasks/{task_id}/result", headers=headers
        )
        assert res2.status_code == 200, res2.text
        body2 = res2.json()["result"]
        assert body2["evidence"]["id"] != old_ev_id
        assert body2["evidence"]["object_id"] != old_obj_id
        assert body2["financial_absence"] is None
        assert body2["financial_evaluation"] is not None
        assert len(body2["evaluation_reports"]) == 1
        assert body2["evaluation_reports"][0]["title"] == "Carbon Intensity Report"
        by_eval2 = {e["evaluator"]: e for e in body2["evaluator_results"]}
        assert by_eval2["ies.evaluator.finance@1.0.0"]["status"] == "succeeded"
        assert by_eval2[report_eval_ref]["status"] == "succeeded"

        # Verify old immutable EvidencePackage content was never modified and source task never reopened.
        with env["sf"]() as check_db:
            old_pkg = results_store.get_evidence(check_db, old_ev_id)
            assert old_pkg is not None and old_pkg.object_id == old_obj_id
            src_row = tasks_store.get_task(check_db, task_id)
            child_row = tasks_store.get_task(check_db, retry_task_id)
            assert src_row is not None and src_row.status == "completed" and src_row.attempt_count == 1
            assert child_row is not None and child_row.status == "completed" and child_row.type == "analysis"
    finally:
        env["db"].close()
        env["engine"].dispose()


def test_zero_evaluators_and_infeasible_skip(tmp_path: Path) -> None:
    """0 evaluators yields evaluators_empty; infeasible solution skips evaluators."""
    env = _setup_pipeline_env(tmp_path)
    try:
        plane = build_test_compute_plane(tmp_path / "stages")
        headers = login_headers(env["client"], env["user"])

        # 1. Zero evaluators -> financial_absence == "evaluators_empty"
        tid_zero = _save_and_submit(
            env, _valid_13_config(evaluators=[]), idempotency_key="f4-zero-eval"
        )
        assert _run(env["engine"], _claim(env["db"], tid_zero), plane) == "completed"
        res_zero = env["client"].get(
            f"/api/projects/{env['pid']}/tasks/{tid_zero}/result", headers=headers
        )
        assert res_zero.status_code == 200, res_zero.text
        b_zero = res_zero.json()["result"]
        assert b_zero["financial_absence"] == "evaluators_empty"
        assert b_zero["evaluator_results"] == []
        assert b_zero["evaluation_reports"] == []

        # 2. Infeasible core solution -> evaluators skipped, financial_absence == outcome_not_normal
        inf_script = tmp_path / "infeasible_solver.py"
        inf_script.write_text(
            "import json, pathlib\n"
            "cols = json.loads(pathlib.Path('input/parameters.json').read_text())['column_count']\n"
            "pathlib.Path('output').mkdir(exist_ok=True)\n"
            "doc = {'schema': 'ies.primal-solution', 'schema_version': '1.0.0', "
            "'math_status': 'infeasible', 'termination_reason': 'infeasible', "
            "'has_feasible_solution': False, 'column_count': cols, 'values': [], "
            "'objective_value': None, 'best_bound': None, 'relative_gap': None, "
            "'receipt': {'solver': 'ies.solver.highs@1.7.2'}, 'extensions': {}}\n"
            "pathlib.Path('output/primal_solution.json').write_text(json.dumps(doc))\n",
            encoding="utf-8",
        )
        inf_calls = {"count": 0}

        def _counting_eval(work_in: Path, work_out: Path) -> None:
            inf_calls["count"] += 1

        inf_plane = build_test_compute_plane(
            tmp_path / "stages_inf",
            solver_argv_override=["python", str(inf_script)],
            evaluator_handlers={"/app/plugins/evaluator_finance/plugin.yaml": _counting_eval},
        )
        headers = login_headers(env["client"], env["user"])
        put_r = env["client"].put(
            f"/api/projects/{env['pid']}/config",
            json={"calculation_config": _valid_13_config(), "expected_revision": 1},
            headers=headers,
        )
        assert put_r.status_code == 200, put_r.text
        st, sb = _submit_task(
            env["client"],
            env["pid"],
            env["user"],
            config={"config_revision": 2},
            idempotency_key="f4-infeasible-skip",
        )
        assert st == 201, sb
        tid_inf = int(sb["task"]["id"])
        assert _run(env["engine"], _claim(env["db"], tid_inf), inf_plane) == "completed"
        assert inf_calls["count"] == 0
        res_inf = env["client"].get(
            f"/api/projects/{env['pid']}/tasks/{tid_inf}/result", headers=headers
        )
        assert res_inf.status_code == 200, res_inf.text
        b_inf = res_inf.json()["result"]
        assert b_inf["task"]["business_outcome"] == "no_recommendation"
        assert b_inf["financial_absence"] == "outcome_not_normal"
        assert b_inf["evaluator_results"][0]["status"] == "skipped"
    finally:
        env["db"].close()
        env["engine"].dispose()


def test_unified_algorithm_mode_pipeline_and_checkpoint_recovery(tmp_path: Path) -> None:
    """Unified algorithm mode runs algorithm stage -> parallel evaluator and recovers checkpoint."""

    class _AlgoProvider:
        descriptor = AlgorithmDescriptor(
            algorithm_id="acme.algo.unified",
            version="1.0.0",
            modes=("planning",),
            precisions=("medium",),
            outputs=("solution", "metrics"),
            capabilities=HIGHS_MILP_PROVIDER.descriptor.capabilities,
            evaluators=("ies.evaluator.finance@1.0.0",),
            parameters_schema={"type": "object", "additionalProperties": False, "properties": {}},
        )

        @property
        def ref(self) -> str:
            return self.descriptor.ref

        def available(self) -> bool:
            return True

    algo = _AlgoProvider()
    algo_stage_desc = BuiltinStageDescriptor(
        plugin_id="acme.algo.unified",
        version="1.0.0",
        kind="algorithm",
        image="pies-builtin-stages:1.2.0",
        entrypoint="algo:run",
        manifest_path="test://acme.algo.unified@1.0.0",
    )
    algo_calls = {"count": 0}

    def _algo_handler(work_in: Path, work_out: Path) -> None:
        algo_calls["count"] += 1
        req = json.loads((work_in / "request.json").read_text(encoding="utf-8"))
        assert "parameters" in req
        eng_sol = {
            "schema": "ies.engineering-solution",
            "schema_version": "1.0.0",
            "solver_summary": {
                "math_status": "solved",
                "termination_reason": "optimal",
                "has_feasible_solution": True,
                "objective_value": 100.0,
                "objective_unit": "CNY",
                "best_bound": 100.0,
                "relative_gap": 0.0,
            },
            "construction_units": [],
            "series": {},
            "system_series": {},
            "extensions": {},
        }
        (work_out / "solution.json").write_text(json.dumps(eng_sol), encoding="utf-8")

    env = _setup_pipeline_env(tmp_path, extra_algorithms=(algo,))
    try:
        algo_reg = build_algorithm_registry((algo,))
        catalog = build_test_stage_catalog(extra_descriptors=(algo_stage_desc,))
        plane = build_test_compute_plane(
            tmp_path / "stages_algo",
            algorithm_registry=algo_reg,
            stage_catalog=catalog,
            evaluator_handlers={"test://acme.algo.unified@1.0.0": _algo_handler},
        )
        cfg = _valid_13_config(
            generator=None,
            solver=None,
            algorithm=algo.ref,
            options={"algorithm": {}},
        )
        task_id = _save_and_submit(env, cfg, idempotency_key="f4-unified-1")
        claim = _claim(env["db"], task_id)
        assert _run(env["engine"], claim, plane) == "completed"
        assert algo_calls["count"] == 1

        # Replaying runner with existing checkpoint must NOT re-invoke algorithm stage.
        headers = login_headers(env["client"], env["user"])
        res = env["client"].get(
            f"/api/projects/{env['pid']}/tasks/{task_id}/result", headers=headers
        )
        assert res.status_code == 200, res.text
        assert res.json()["result"]["evaluator_results"][0]["status"] == "succeeded"

        # Also prove load_algorithm_checkpoint hit prevents re-execution on recovery:
        with env["sf"]() as check_db:
            attempt_row = check_db.execute(
                select(TaskAttempt).where(TaskAttempt.task_id == task_id)
            ).scalar_one()
            ckpt = worker_app.load_algorithm_checkpoint(
                check_db,
                worker_app.Claim(
                    task_id=task_id,
                    attempt_id=attempt_row.id,
                    attempt_no=attempt_row.attempt_no,
                    lease_token=claim.lease_token,
                ),
            )
            assert ckpt is not None and ckpt.solution_ref is not None
    finally:
        env["db"].close()
        env["engine"].dispose()


def test_historical_115_evidence_projection(tmp_path: Path) -> None:
    """Historical 1.1.5 EvidencePackage without evaluator_results projects safely on GET /result."""
    env = _setup_pipeline_env(tmp_path)
    try:
        plane = build_test_compute_plane(tmp_path / "stages_hist")
        task_id = _save_and_submit(env, _valid_13_config(), idempotency_key="f4-hist-1")
        claim = _claim(env["db"], task_id)
        assert _run(env["engine"], claim, plane) == "completed"

        # Seed a legacy 1.1.5 EvidencePackage without evaluator_results / evaluation_reports.
        legacy_payload = {
            "compute_result": {
                "schema": "ies.compute-result",
                "schema_version": "1.0.0",
                "bundle_id": "legacy-b1",
                "receipt_id": "legacy-r1",
                "execution_status": "completed",
                "math_status": "solved",
                "outputs": {"solver_summary": {"math_status": "solved", "objective_value": 42.0}},
            },
            "metrics": {"objective_value": 42.0},
            "financial_evaluation": {"irr": 0.12, "npv": 999.0},
        }
        with env["sf"]() as check_db:
            from iesplan import tasks as tasks_domain
            from iesplan.storage import put_object

            task_row = tasks_domain.get_task(check_db, task_id)
            assert task_row is not None
            attempt_row = check_db.execute(
                select(TaskAttempt).where(TaskAttempt.task_id == task_id)
            ).scalar_one()
            stored = put_object(
                check_db,
                json.dumps(legacy_payload).encode("utf-8"),
                content_type="application/json",
                source_category="result",
            )
            results_store.create_evidence(
                check_db,
                task_id=task_id,
                calc_snapshot_id=task_row.calc_snapshot_id or 1,
                object_id=stored.id,
                status="complete",
                attempt_id=attempt_row.id,
                created_by=env["user"].id,
            )
            check_db.commit()

        headers = login_headers(env["client"], env["user"])
        res = env["client"].get(
            f"/api/projects/{env['pid']}/tasks/{task_id}/result", headers=headers
        )
        assert res.status_code == 200, res.text
        body = res.json()["result"]
        assert body["financial_evaluation"] == {"irr": 0.12, "npv": 999.0}
        assert body["evaluator_results"] == []
        assert body["evaluation_reports"] == []
        assert body["financial_absence"] is None
    finally:
        env["db"].close()
        env["engine"].dispose()


def test_grouped_prediction_without_convergence(tmp_path: Path) -> None:
    """Single-pass grouped prediction works in 1.2.0 with no convergence field."""
    from iesplan.assembly.contracts import ValidatedAssemblyArtifact, ValidationReceipt
    from iesplan.core.content import MemoryContent
    from iesplan.core.yamlmini import dump as yaml_dump

    env = _setup_pipeline_env(tmp_path)
    try:
        plane = build_test_compute_plane(tmp_path / "stages_pred")
        csv_bytes = b"timestamp,electricity_demand\n2026-01-01 00:00,10.0\n2026-01-01 01:00,14.0\n"
        with env["sf"]() as db:
            from iesplan.storage import put_object

            obj1 = put_object(
                db, csv_bytes, content_type="text/csv", source_category="dataset"
            )
            obj2 = put_object(
                db, csv_bytes, content_type="text/csv", source_category="dataset"
            )
            doc = {
                "schema": "ies.assembly",
                "schema_version": "1.0.0",
                "assembly": {"id": "pred_asm"},
                "project_baseline": {
                    "resolution": "1h",
                    "start_year": 2026,
                    "scenario_mode": "single",
                },
                "devices": {
                    "load1": {
                        "predefined_interfaces": {
                            "electricity_demand": {
                                "mode": "data_predict",
                                "target_type": "flex_load",
                                "content": {"object_id": str(obj1.id), "media_type": "text/csv"},
                            }
                        }
                    },
                    "load2": {
                        "predefined_interfaces": {
                            "electricity_demand": {
                                "mode": "data_predict",
                                "target_type": "flex_load",
                                "content": {"object_id": str(obj2.id), "media_type": "text/csv"},
                            }
                        }
                    },
                },
            }
            cfg = _valid_13_config(
                predictions=[
                    {
                        "target": {"instance": "load1", "interface": "electricity_demand"},
                        "algorithm": "ies.predictor.naive@1.0.0",
                        "parameters": {},
                    },
                    {
                        "target": {"instance": "load2", "interface": "electricity_demand"},
                        "algorithm": "ies.predictor.naive@1.0.0",
                        "parameters": {},
                    },
                ]
            )
            snap = CalcSnapshot(
                project_version_id=1,
                dataset_version_ids=[],
                calc_config_snapshot={**cfg, "config_ref": {"project_id": env["pid"], "revision": 1}},
                schema_version="1.0.0",
                result_adapter="",
                executor="",
                extension_versions={},
                random_seed=7,
                device_models={},
                evaluation={
                    "evaluation_years": 1,
                    "construction_period": 0,
                    "operation_years": 1,
                    "discount_rate": 0.08,
                    "construction_payments": [],
                    "maintenance_expenses": [],
                },
                canonical_assembly_text=json.dumps(doc),
                assembly_receipt=ValidationReceipt().to_dict(),
                created_by=env["user"].id,
            )
            db.add(snap)
            db.flush()
            task = env["db"].query(type(env["db"].get(CalcSnapshot, snap.id)))  # unused
            from iesplan.tasks.persistence import Task

            t_row = Task(
                project_id=env["pid"],
                type="optimization",
                status="queued",
                requested_by=env["user"].id,
                idempotency_key="f4-pred-1",
                calc_snapshot_id=snap.id,
            )
            db.add(t_row)
            db.commit()
            claim = _claim(db, t_row.id)

        start_count = 0
        orig_start = plane.stage_runner.client.start_stage

        def _counting_start(*args: Any, **kwargs: Any) -> Any:
            nonlocal start_count
            start_count += 1
            return orig_start(*args, **kwargs)

        plane.stage_runner.client.start_stage = _counting_start  # type: ignore[method-assign]
        series, predicted, artifacts = worker_app.predict_round_stage(
            plane=plane,
            task_project_id=env["pid"],
            snapshot=snap,
            claim=claim,
            number=1,
            session_factory=env["sf"],
            save_checkpoint=worker_app.save_prediction_checkpoint,
            save_group_checkpoint=worker_app.save_prediction_group_checkpoint,
            publish_combined_checkpoint=worker_app.publish_prediction_checkpoint_with_artifacts,
        )
        # Grouped same-predictor targets run in a single stage invocation!
        assert start_count == 1
        assert len(predicted) == 2 and len(series) == 2
        assert len(artifacts) == 2
        assert "convergence" not in snap.calc_config_snapshot
        _ = (ValidatedAssemblyArtifact, ValidationReceipt, MemoryContent, yaml_dump, task, RunContext)
    finally:
        env["db"].close()
        env["engine"].dispose()


def test_codex_f4_parameters_slot_third_party_solver_and_metrics_only_evaluators(
    tmp_path: Path,
) -> None:
    """Third-party .mps solver runs without allowlist and metrics-only solves run evaluators."""
    from iesplan.computation import SOLVER_BUNDLE_MPS_CONTRACT, CalculationConfig

    custom_solver_ref = "acme.solver.custom@1.0.0"
    custom_solver_spec = StagePluginSpec(
        plugin_id="acme.solver.custom",
        version="1.0.0",
        kind="solver",
        consumes=(SOLVER_BUNDLE_MPS_CONTRACT,),
        produces=("ies.primal-solution@1.0.0",),
        parameters_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"threads": {"type": "integer", "minimum": 1}},
        },
    )
    custom_solver_desc = BuiltinStageDescriptor(
        plugin_id="acme.solver.custom",
        version="1.0.0",
        kind="solver",
        image="pies-plugin/acme-solver:1.0.0",
        entrypoint="solver:solve",
    )

    seen_requests: dict[str, dict[str, Any]] = {}
    eval_calls = {"count": 0}

    def _report_handler(work_in: Path, work_out: Path) -> None:
        eval_calls["count"] += 1
        seen_requests["eval"] = json.loads((work_in / "request.json").read_text(encoding="utf-8"))
        doc = {
            "schema": "ies.evaluation-report",
            "schema_version": "1.0.0",
            "title": "Metrics Only Evaluator Report",
            "evaluator": "acme.evaluator.metrics_check@1.0.0",
            "metrics": [{"id": "ok", "label": "OK", "value": 1, "unit": "1"}],
            "charts": [],
            "tables": [],
        }
        (work_out / "evaluation_report.json").write_text(json.dumps(doc), encoding="utf-8")

    report_spec = StagePluginSpec(
        plugin_id="acme.evaluator.metrics_check",
        version="1.0.0",
        kind="evaluator",
        consumes=("ies.engineering-solution@1.0.0",),
        produces=(EVALUATION_REPORT_CONTRACT,),
        outputs=("solution", "metrics"),
        parameters_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"tag": {"type": "string"}},
        },
    )
    report_desc = BuiltinStageDescriptor(
        plugin_id="acme.evaluator.metrics_check",
        version="1.0.0",
        kind="evaluator",
        image="pies-plugin/eval:1.0.0",
        entrypoint="eval:run",
        manifest_path="test://acme.evaluator.metrics_check@1.0.0",
    )

    env = _setup_pipeline_env(
        tmp_path,
        extra_evaluators=(report_spec,),
        extra_solvers=(custom_solver_spec,),
    )
    try:
        sol_reg = build_solver_registry((BUILTIN_SOLVER_HIGHS_SPEC, custom_solver_spec))
        eval_reg = build_evaluator_registry((BUILTIN_EVALUATOR_FINANCE_SPEC, report_spec))
        catalog = build_test_stage_catalog(extra_descriptors=(custom_solver_desc, report_desc))
        plane = build_test_compute_plane(
            tmp_path / "stages_custom",
            solver_registry=sol_reg,
            evaluator_registry=eval_reg,
            stage_catalog=catalog,
            evaluator_handlers={"test://acme.evaluator.metrics_check@1.0.0": _report_handler},
        )
        orig_start = plane.stage_runner.client.start_stage

        def _inspect_start(spec: Any) -> Any:
            ws_in = Path(plane.stage_runner.stage_root) / str(spec.workspace_rel) / "in"
            req_path = ws_in / "request.json"
            if req_path.is_file():
                seen_requests[str(spec.stage_id)] = json.loads(req_path.read_text("utf-8"))
            return orig_start(spec)

        plane.stage_runner.client.start_stage = _inspect_start  # type: ignore[method-assign]

        cfg = _valid_13_config(
            solver=custom_solver_ref,
            outputs=["metrics"],
            options={
                "generator": {"acceleration": {"mode": "off"}},
                "solver": {"threads": 2},
            },
            evaluators=[{"evaluator": "acme.evaluator.metrics_check@1.0.0", "parameters": {"tag": "m"}}],
        )
        assert plane.generator_registry.resolve(CalculationConfig(**cfg)).ref == HIGHS_MILP_PROVIDER.ref
        task_id = _save_and_submit(env, cfg, idempotency_key="f4-3p-metrics-only")
        claim = _claim(env["db"], task_id)
        assert _run(env["engine"], claim, plane) == "completed"
        assert seen_requests["generate-r1"]["parameters"] == {"acceleration": {"mode": "off"}}
        assert seen_requests["solve-r1"]["parameters"] == {"threads": 2}
        assert seen_requests["eval"]["parameters"] == {"tag": "m"}
        assert eval_calls["count"] == 1
        res = env["client"].get(
            f"/api/projects/{env['pid']}/tasks/{task_id}/result",
            headers=login_headers(env["client"], env["user"]),
        )
        assert res.status_code == 200
        assert res.json()["result"]["evaluator_results"][0]["status"] == "succeeded"
    finally:
        env["db"].close()
        env["engine"].dispose()


def test_codex_f4_evaluator_protocol_failure_isolated_from_core_and_siblings(
    tmp_path: Path,
) -> None:
    """Missing/invalid evaluator output fails only that evaluator (EVAL-PROTOCOL-001)."""
    report_ref = "acme.evaluator.bad_proto@1.0.0"
    report_spec = StagePluginSpec(
        plugin_id="acme.evaluator.bad_proto",
        version="1.0.0",
        kind="evaluator",
        consumes=("ies.engineering-solution@1.0.0",),
        produces=(EVALUATION_REPORT_CONTRACT,),
        outputs=("solution", "metrics"),
        parameters_schema={"type": "object", "additionalProperties": False, "properties": {}},
    )
    report_desc = BuiltinStageDescriptor(
        plugin_id="acme.evaluator.bad_proto",
        version="1.0.0",
        kind="evaluator",
        image="pies-plugin/bad:1.0.0",
        entrypoint="eval:run",
        manifest_path="test://acme.evaluator.bad_proto@1.0.0",
    )

    def _bad_eval_handler(_work_in: Path, work_out: Path) -> None:
        (work_out / "result.json").write_text(json.dumps({"outputs": {}}), encoding="utf-8")

    env = _setup_pipeline_env(tmp_path, extra_evaluators=(report_spec,))
    try:
        eval_reg = build_evaluator_registry((BUILTIN_EVALUATOR_FINANCE_SPEC, report_spec))
        catalog = build_test_stage_catalog(extra_descriptors=(report_desc,))
        plane = build_test_compute_plane(
            tmp_path / "stages_bad_proto",
            evaluator_registry=eval_reg,
            stage_catalog=catalog,
            evaluator_handlers={"test://acme.evaluator.bad_proto@1.0.0": _bad_eval_handler},
        )
        cfg = _valid_13_config(
            evaluators=[
                {"evaluator": "ies.evaluator.finance@1.0.0", "parameters": {}},
                {"evaluator": report_ref, "parameters": {}},
            ]
        )
        task_id = _save_and_submit(env, cfg, idempotency_key="f4-bad-proto-1")
        claim = _claim(env["db"], task_id)
        assert _run(env["engine"], claim, plane) == "completed"

        res = env["client"].get(
            f"/api/projects/{env['pid']}/tasks/{task_id}/result",
            headers=login_headers(env["client"], env["user"]),
        )
        assert res.status_code == 200
        body = res.json()["result"]
        assert body["financial_evaluation"] is not None
        by_ref = {entry["evaluator"]: entry for entry in body["evaluator_results"]}
        assert by_ref["ies.evaluator.finance@1.0.0"]["status"] == "succeeded"
        assert by_ref[report_ref]["status"] == "failed"
        assert by_ref[report_ref]["contract"] is None
        assert by_ref[report_ref]["error"]["code"] == "EVAL-PROTOCOL-001"
    finally:
        env["db"].close()
        env["engine"].dispose()


def test_codex_f4_predictor_group_checkpoint_recovery_and_retry_cleanup(
    tmp_path: Path,
) -> None:
    """Multi-predictor recovery skips checkpointed group via stable algo_ref and cleans up reuse."""
    from iesplan.application.results import endpoint_cases as results_ep
    from iesplan.tasks.contracts import TaskConflictError

    env = _setup_pipeline_env(tmp_path)
    try:
        from iesplan.assembly import ValidationReceipt
        from iesplan.computation import build_predictor_registry
        from iesplan.computation.predictors import NAIVE_PREDICTOR, PredictorDescriptor
        from iesplan.core.content import MemoryContent
        from iesplan.storage import put_object

        second_pred_ref = "acme.predictor.second@1.0.0"
        second_desc = PredictorDescriptor(
            ref=second_pred_ref,
            depends_on_solve_result=False,
            parameters_schema={"type": "object", "additionalProperties": False},
        )
        second_stage = BuiltinStageDescriptor(
            plugin_id="acme.predictor.second",
            version="1.0.0",
            kind="predictor",
            image="pies-builtin-stages:1.2.0",
            entrypoint="predictor_stage:predict",
            manifest_path="test://acme.predictor.second@1.0.0",
        )

        class _FakeSecondPred:
            ref = second_pred_ref
            descriptor = second_desc

            def validate_parameters(self, parameters: Any) -> dict[str, Any]:
                return dict(parameters or {})

            def available(self) -> bool:
                return True

            def predict(self, **kwargs: Any) -> Any:
                return NAIVE_PREDICTOR.predict(**kwargs)

        pred_reg = build_predictor_registry((NAIVE_PREDICTOR, _FakeSecondPred()))
        catalog = build_test_stage_catalog(
            predictor_registry=pred_reg,
            extra_descriptors=(second_stage,),
        )
        plane = build_test_compute_plane(
            tmp_path / "stages_pred_grp",
            predictor_registry=pred_reg,
            stage_catalog=catalog,
        )

        with env["sf"]() as db:
            csv_body = "hour,electricity_demand\n" + "".join(f"{i},1.0\n" for i in range(8760))
            stored = put_object(db, csv_body.encode("utf-8"), "text/csv", source_category="dataset")
            doc = {
                "project_baseline": {"resolution": "1h", "start_year": 2025},
                "devices": {
                    "load1": {
                        "predefined_interfaces": {
                            "electricity_demand": {
                                "mode": "data_predict",
                                "target_type": "flex_load",
                                "content": {"object_id": str(stored.id), "media_type": "text/csv"},
                            }
                        }
                    },
                    "load2": {
                        "predefined_interfaces": {
                            "electricity_demand": {
                                "mode": "data_predict",
                                "target_type": "flex_load",
                                "content": {"object_id": str(stored.id), "media_type": "text/csv"},
                            }
                        }
                    },
                },
            }
            cfg = _valid_13_config(
                predictions=[
                    {
                        "target": {"instance": "load1", "interface": "electricity_demand"},
                        "algorithm": NAIVE_PREDICTOR.descriptor.ref,
                        "parameters": {},
                    },
                    {
                        "target": {"instance": "load2", "interface": "electricity_demand"},
                        "algorithm": second_pred_ref,
                        "parameters": {},
                    },
                ]
            )
            snap = CalcSnapshot(
                project_version_id=1,
                dataset_version_ids=[],
                calc_config_snapshot={**cfg, "config_ref": {"project_id": env["pid"], "revision": 1}},
                schema_version="1.0.0",
                result_adapter="",
                executor="",
                extension_versions={},
                random_seed=7,
                device_models={},
                evaluation={
                    "evaluation_years": 1,
                    "construction_period": 0,
                    "operation_years": 1,
                    "discount_rate": 0.08,
                    "construction_payments": [],
                    "maintenance_expenses": [],
                },
                canonical_assembly_text=json.dumps(doc),
                assembly_receipt=ValidationReceipt().to_dict(),
                created_by=env["user"].id,
            )
            db.add(snap)
            db.flush()
            from iesplan.tasks.persistence import Task

            t_row = Task(
                project_id=env["pid"],
                type="optimization",
                status="queued",
                requested_by=env["user"].id,
                idempotency_key="f4-pred-grp-1",
                calc_snapshot_id=snap.id,
            )
            db.add(t_row)
            db.commit()
            claim = _claim(db, t_row.id)

            from iesplan.computation import MaterializedSeries, PredictedTarget

            cached_series = (
                MaterializedSeries(
                    device="load1",
                    interface="electricity_demand",
                    mode="data_predict",
                    resolution="1h",
                    point_count=8760,
                    values=(1.0,) * 8760,
                    source={"kind": "data_predict", "object_id": str(stored.id)},
                ),
            )
            cached_pred = (
                PredictedTarget(
                    device="load1",
                    interface="electricity_demand",
                    target_type="flex_load",
                    values=(1.0,) * 8760,
                    history_object_id=str(stored.id),
                    artifact=b"{}",
                ),
            )
            worker_app.save_prediction_group_checkpoint(
                db, claim, 1, NAIVE_PREDICTOR.descriptor.ref, cached_series, cached_pred
            )

        reused_cleanups: list[str] = []
        started_stages: list[str] = []
        orig_start = plane.stage_runner.client.start_stage

        def _tracking_start(spec: Any) -> Any:
            started_stages.append(str(spec.stage_id))
            return orig_start(spec)

        plane.stage_runner.client.start_stage = _tracking_start  # type: ignore[method-assign]
        series, predicted, _artifacts = worker_app.predict_round_stage(
            plane=plane,
            task_project_id=env["pid"],
            snapshot=snap,
            claim=claim,
            number=1,
            session_factory=env["sf"],
            save_checkpoint=worker_app.save_prediction_checkpoint,
            save_group_checkpoint=worker_app.save_prediction_group_checkpoint,
            load_group_checkpoint=worker_app.load_prediction_group_checkpoint,
            publish_combined_checkpoint=worker_app.publish_prediction_checkpoint_with_artifacts,
            cleanup_on_reuse=reused_cleanups.append,
        )
        assert len(series) == 2 and len(predicted) == 2
        assert started_stages == ["predict-r1-0"]
        assert reused_cleanups == ["predict-r1-1"]

        # Verify retry_evaluator rejects conflicting second retry after locking Task row.
        ev_task_id = _save_and_submit(env, _valid_13_config(), idempotency_key="f4-retry-lock")
        ev_claim = _claim(env["db"], ev_task_id)
        with pytest.raises(TaskConflictError):
            results_ep.retry_evaluator_case(
                env["db"],
                env["user"],
                env["pid"],
                ev_task_id,
                evaluator="ies.evaluator.finance@1.0.0",
                idempotency_key="retry-while-running",
            )
        _ = (MemoryContent, ev_claim)
    finally:
        env["db"].close()
        env["engine"].dispose()


def test_codex_r2_rejects_malformed_evaluator_outputs_and_contradictory_solution(
    tmp_path: Path,
) -> None:
    """Codex Round 2 R1/R2: strict canonical evaluator & engineering-solution contracts."""
    from iesplan.computation import ComputationUnavailableError
    from iesplan.computation.contracts.outputs import (
        validate_engineering_solution_doc,
        validate_evaluation_report_doc,
        validate_financial_evaluation_doc,
    )
    from iesplan.computation.result_adapters import project_engineering_solution
    from iesplan.core.content import MemoryContent

    # R1: malformed nested evaluation_report (metrics: [null]) and bare financial_evaluation fail.
    with pytest.raises(ValueError):
        validate_evaluation_report_doc(
            {
                "schema": "ies.evaluation-report",
                "schema_version": "1.0.0",
                "title": "Bad Metrics",
                "evaluator": "acme.evaluator.bad@1.0.0",
                "metrics": [None],
                "charts": [],
                "tables": [],
            },
            expected_evaluator="acme.evaluator.bad@1.0.0",
        )
    with pytest.raises(ValueError):
        validate_financial_evaluation_doc(
            {"schema": "ies.financial-evaluation", "schema_version": "1.0.0", "status": "evaluated"}
        )

    valid_fin_base = {
        "schema": "ies.financial-evaluation",
        "schema_version": "1.0.0",
        "status": "evaluated",
        "evaluation_years": 1,
        "construction_period": 0,
        "operation_years": 1,
        "currency": "CNY",
        "price_basis": "tax_inclusive",
        "base_year": 2025,
        "profile_id": "p",
        "discount_rate": 0.08,
        "construction_total": "0",
        "construction_total_unit": "CNY",
        "annual_fixed_om": "0",
        "payments_applied": [],
        "net_cashflows": [],
        "cost_items": [],
        "amounts": [],
        "cash_movements": [],
        "assets": [],
        "construction_units": [],
        "annual_operation": [],
        "monthly_demand": [],
        "tax_cash": {
            "depreciation": [],
            "derived": [],
            "income": [],
            "loss_balances": [],
            "refunds": [],
            "vat": [],
            "vat_rules": [],
        },
        "annual_net_flows": ["0", "0"],
        "comparison": {"mode": "absolute"},
        "evaluation": {"rows": [], "npv": "0", "irr_status": "no_solution"},
    }
    for bad_fin in (
        {**valid_fin_base, "annual_operation": [None]},
        {**valid_fin_base, "payments_applied": [{"t": 0}]},
        {**valid_fin_base, "tax_cash": {**valid_fin_base["tax_cash"], "vat": [None]}},
        {**valid_fin_base, "evaluation": {"rows": [None], "npv": "0", "irr_status": "no_solution"}},
        {**valid_fin_base, "annual_net_flows": [None]},
        {**valid_fin_base, "construction_total": 1000},
        {**valid_fin_base, "annual_fixed_om": 10},
        {**valid_fin_base, "annual_net_flows": ["1_000", "0"]},
        {
            **valid_fin_base,
            "evaluation": {"rows": [], "npv": "0", "irr_status": "normal", "irr": "NaN"},
        },
        {
            **valid_fin_base,
            "evaluation": {
                "rows": [],
                "npv": "0",
                "irr_status": "no_solution",
                "static_payback": "not-a-number",
            },
        },
        *[
            bad_case
            for bad_token in ("NaN", "Infinity", "-Infinity", "not-a-number", " 1.0 ")
            for bad_case in (
                {**valid_fin_base, "annual_net_flows": [bad_token, "0"]},
                {
                    **valid_fin_base,
                    "annual_operation": [
                        {
                            "financial_year": 1,
                            "energy_net": "0",
                            "variable_om": "0",
                            "fixed_om": "0",
                            "maintenance": "0",
                            "demand_charge": "0",
                            "net": bad_token,
                        }
                    ],
                },
                {**valid_fin_base, "construction_total": bad_token},
                {**valid_fin_base, "annual_fixed_om": bad_token},
                {
                    **valid_fin_base,
                    "evaluation": {"rows": [], "npv": bad_token, "irr_status": "no_solution"},
                },
            )
        ],
    ):
        with pytest.raises(ValueError):
            validate_financial_evaluation_doc(bad_fin)

    # Non-finite decimal string from evaluator container routes to EVAL-PROTOCOL-001.
    from iesplan.application.worker.compute_cases import _parse_evaluator_outputs
    from iesplan.computation import FINANCIAL_EVALUATION_CONTRACT

    with pytest.raises(ValueError, match="annual_net_flows"):
        _parse_evaluator_outputs(
            {
                "financial_evaluation.json": MemoryContent(
                    json.dumps({**valid_fin_base, "annual_net_flows": ["NaN", "0"]}).encode("utf-8")
                )
            },
            evaluator_ref="ies.evaluator.finance@1.0.0",
            declared_produces=(FINANCIAL_EVALUATION_CONTRACT,),
        )

    # R2: contradictory solved + has_feasible_solution=false rejected at both validator and spawn child.
    contradictory = {
        "schema": "ies.engineering-solution",
        "schema_version": "1.0.0",
        "solver_summary": {
            "termination_reason": "optimal",
            "math_status": "solved",
            "has_feasible_solution": False,
            "objective_value": None,
            "objective_unit": None,
            "best_bound": None,
            "relative_gap": None,
        },
        "extensions": {},
    }
    with pytest.raises(ValueError, match="has_feasible_solution=false"):
        validate_engineering_solution_doc(contradictory)

    sol_out = tmp_path / "projected.json"
    with pytest.raises(ComputationUnavailableError) as exc_info:
        project_engineering_solution(
            MemoryContent(json.dumps(contradictory).encode("utf-8")),
            bundle_id="b-r2",
            receipt_id="r-r2",
            outputs=("solution", "metrics"),
            solution_output_path=sol_out,
        )
    assert exc_info.value.reason == "result-protocol-failure"

    # R1 end-to-end isolation: evaluator producing metrics:[null] fails cleanly as EVAL-PROTOCOL-001.
    from iesplan.computation import (
        EVALUATION_REPORT_CONTRACT,
        StagePluginSpec,
        build_evaluator_registry,
    )

    bad_ref = "acme.evaluator.bad_report@1.0.0"

    def _bad_report_handler(_work_in: Path, work_out: Path) -> None:
        (work_out / "evaluation_report.json").write_text(
            json.dumps(
                {
                    "schema": "ies.evaluation-report",
                    "schema_version": "1.0.0",
                    "title": "Malformed Report",
                    "evaluator": bad_ref,
                    "metrics": [None],
                    "charts": [],
                    "tables": [],
                }
            ),
            encoding="utf-8",
        )

    bad_spec = StagePluginSpec(
        plugin_id="acme.evaluator.bad_report",
        version="1.0.0",
        kind="evaluator",
        consumes=("ies.engineering-solution@1.0.0",),
        produces=(EVALUATION_REPORT_CONTRACT,),
        outputs=("solution", "metrics"),
        parameters_schema={"type": "object", "additionalProperties": False},
    )
    bad_stage = BuiltinStageDescriptor(
        plugin_id="acme.evaluator.bad_report",
        version="1.0.0",
        kind="evaluator",
        image="pies-plugin/eval:1.0.0",
        entrypoint="eval:run",
        manifest_path=f"test://{bad_ref}",
    )
    env = _setup_pipeline_env(tmp_path / "e2e_r1", extra_evaluators=(bad_spec,))
    try:
        eval_reg = build_evaluator_registry((BUILTIN_EVALUATOR_FINANCE_SPEC, bad_spec))
        catalog = build_test_stage_catalog(extra_descriptors=(bad_stage,))
        plane = build_test_compute_plane(
            tmp_path / "stages_r1",
            evaluator_registry=eval_reg,
            stage_catalog=catalog,
            evaluator_handlers={f"test://{bad_ref}": _bad_report_handler},
        )
        cfg = _valid_13_config(
            evaluators=[
                {"evaluator": bad_ref, "parameters": {}},
                {"evaluator": "ies.evaluator.finance@1.0.0", "parameters": {}},
            ]
        )
        task_id = _save_and_submit(env, cfg, idempotency_key="f4-r1-malformed-report")
        claim = _claim(env["db"], task_id)
        assert _run(env["engine"], claim, plane) == "completed"
        headers = login_headers(env["client"], env["user"])
        res = env["client"].get(
            f"/api/projects/{env['pid']}/tasks/{task_id}/result",
            headers=headers,
        )
        assert res.status_code == 200
        result_view = res.json()["result"]
        assert result_view["financial_evaluation"] is not None
        assert result_view["evaluation_reports"] == []
        by_eval = {item["evaluator"]: item for item in result_view["evaluator_results"]}
        assert by_eval[bad_ref]["status"] == "failed"
        assert by_eval[bad_ref]["error"]["code"] == "EVAL-PROTOCOL-001"
        assert by_eval["ies.evaluator.finance@1.0.0"]["status"] == "succeeded"
    finally:
        env["db"].close()
        env["engine"].dispose()


def test_p1_02_failed_or_cancelled_retry_preserves_completed_source_and_allows_rerun(
    tmp_path: Path,
) -> None:
    """P1-02: failed/cancelled child analysis retry never reopens source task or drops baseline."""
    fail_mode = {"mode": "fail_eval"}  # fail_eval -> cancel -> crash_runner -> ok

    def _finance_handler(work_in: Path, work_out: Path) -> None:
        mode = fail_mode["mode"]
        if mode == "fail_eval":
            raise RuntimeError("initial finance failure")
        sol = json.loads((work_in / "solution.json").read_text(encoding="utf-8"))
        req = json.loads((work_in / "request.json").read_text(encoding="utf-8"))
        from stage_testkit import _artifact_from_json

        from iesplan.finance import (
            EffectiveFinanceConfig,
            EvaluationInput,
            evaluate_forward_finance,
        )

        fin_eval = evaluate_forward_finance(
            sol,
            artifact=_artifact_from_json(req["artifact"]),
            finance=EffectiveFinanceConfig.from_dict(req["finance"]),
            evaluation=EvaluationInput.from_frozen(req["evaluation"]),
            device_models=req.get("device_models"),
        )
        (work_out / "financial_evaluation.json").write_text(json.dumps(fin_eval), encoding="utf-8")

    env = _setup_pipeline_env(tmp_path)
    try:
        plane = build_test_compute_plane(
            tmp_path / "stages",
            evaluator_handlers={"/app/plugins/evaluator_finance/plugin.yaml": _finance_handler},
        )
        source_id = _save_and_submit(env, _valid_13_config(), idempotency_key="p1-02-src")
        claim1 = _claim(env["db"], source_id)
        assert _run(env["engine"], claim1, plane) == "completed"

        headers = login_headers(env["client"], env["user"])
        res1 = env["client"].get(f"/api/projects/{env['pid']}/tasks/{source_id}/result", headers=headers)
        assert res1.status_code == 200
        old_ev_id = res1.json()["result"]["evidence"]["id"]

        # Start retry #1 -> simulate unexpected child failure before submit.
        r1 = env["client"].post(
            f"/api/projects/{env['pid']}/tasks/{source_id}/result/evaluators/retry",
            json={"evaluator": "ies.evaluator.finance@1.0.0", "idempotency_key": "p1-02-r1"},
            headers=headers,
        )
        assert r1.status_code == 201, r1.text
        retry1_id = r1.json()["task"]["id"]
        orig_run_stage = plane.stage_runner.run_stage

        def _boom_once(spec: Any, **kwargs: Any) -> Any:
            if str(spec.stage_id).startswith("eval-"):
                raise RuntimeError("boom during child retry")
            return orig_run_stage(spec, **kwargs)

        plane.stage_runner.run_stage = _boom_once  # type: ignore[method-assign]
        try:
            assert _run(env["engine"], _claim(env["db"], retry1_id), plane) == "failed"
        finally:
            plane.stage_runner.run_stage = orig_run_stage  # type: ignore[method-assign]

        # Source task stays completed & baseline evidence unchanged.
        with env["sf"]() as check_db:
            src = tasks_store.get_task(check_db, source_id)
            child1 = tasks_store.get_task(check_db, retry1_id)
            latest_ev = results_store.latest_evidence_for_task(check_db, source_id)
            assert src is not None and src.status == "completed"
            assert child1 is not None and child1.status == "failed"
            assert latest_ev is not None and latest_ev.id == old_ev_id

        # Start retry #2 -> cancel child retry -> source still completed & baseline intact.
        r2 = env["client"].post(
            f"/api/projects/{env['pid']}/tasks/{source_id}/result/evaluators/retry",
            json={"evaluator": "ies.evaluator.finance@1.0.0", "idempotency_key": "p1-02-r2"},
            headers=headers,
        )
        assert r2.status_code == 201, r2.text
        retry2_id = r2.json()["task"]["id"]
        from iesplan.worker import runner as worker_runner

        cancel_claim = _claim(env["db"], retry2_id)

        def _cancel_eval(spec: Any, **kwargs: Any) -> Any:
            if str(spec.stage_id).startswith("eval-"):
                cancel_resp = env["client"].post(
                    f"/api/projects/{env['pid']}/tasks/{retry2_id}/cancel",
                    headers=headers,
                )
                assert cancel_resp.status_code == 200, cancel_resp.text
                raise worker_app.StageExecutionError(
                    stage="evaluate",
                    status="cancelled",
                    termination_reason="cancelled",
                    detail="user cancel",
                )
            return orig_run_stage(spec, **kwargs)

        plane.stage_runner.run_stage = _cancel_eval  # type: ignore[method-assign]
        try:
            assert (
                worker_runner.run_task(env["sf"], cancel_claim, worker_id="t", compute=plane)
                == "cancelled"
            )
        finally:
            plane.stage_runner.run_stage = orig_run_stage  # type: ignore[method-assign]

        with env["sf"]() as check_db:
            src = tasks_store.get_task(check_db, source_id)
            child2 = tasks_store.get_task(check_db, retry2_id)
            latest_ev = results_store.latest_evidence_for_task(check_db, source_id)
            assert src is not None and src.status == "completed"
            assert child2 is not None and child2.status == "cancelled"
            assert latest_ev is not None and latest_ev.id == old_ev_id

        # Retry #3 succeeds and updates source task's evidence package.
        fail_mode["mode"] = "ok"
        r3 = env["client"].post(
            f"/api/projects/{env['pid']}/tasks/{source_id}/result/evaluators/retry",
            json={"evaluator": "ies.evaluator.finance@1.0.0", "idempotency_key": "p1-02-r3"},
            headers=headers,
        )
        assert r3.status_code == 201, r3.text
        retry3_id = r3.json()["task"]["id"]
        assert _run(env["engine"], _claim(env["db"], retry3_id), plane) == "completed"

        res3 = env["client"].get(f"/api/projects/{env['pid']}/tasks/{source_id}/result", headers=headers)
        assert res3.status_code == 200
        assert res3.json()["result"]["evidence"]["id"] != old_ev_id
        assert res3.json()["result"]["financial_evaluation"] is not None
    finally:
        env["db"].close()
        env["engine"].dispose()


def test_p2_02_evaluator_max_concurrency_budget_sequential_and_bounded_parallel(
    tmp_path: Path,
) -> None:
    """P2-02: run_evaluators_stage enforces ComputeExecutionPlane.evaluator_max_concurrency."""
    import threading
    import time
    from dataclasses import replace

    specs: list[StagePluginSpec] = []
    descs: list[BuiltinStageDescriptor] = []
    handlers: dict[str, Any] = {}
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    for idx in range(3):
        ref_id = f"acme.evaluator.conc{idx}"
        spec = StagePluginSpec(
            plugin_id=ref_id,
            version="1.0.0",
            kind="evaluator",
            consumes=("ies.engineering-solution@1.0.0",),
            produces=(EVALUATION_REPORT_CONTRACT,),
            outputs=("solution", "metrics"),
            parameters_schema={"type": "object", "additionalProperties": False},
        )
        desc = BuiltinStageDescriptor(
            plugin_id=ref_id,
            version="1.0.0",
            kind="evaluator",
            image="pies-builtin-stages:1.2.0",
            entrypoint="report:eval",
            manifest_path=f"test://{ref_id}@1.0.0",
        )
        specs.append(spec)
        descs.append(desc)

        def _make_handler(eval_ref: str):
            def _handler(_work_in: Path, work_out: Path) -> None:
                with lock:
                    state["active"] += 1
                    state["peak"] = max(state["peak"], state["active"])
                try:
                    time.sleep(0.08)
                finally:
                    with lock:
                        state["active"] -= 1
                (work_out / "evaluation_report.json").write_text(
                    json.dumps(
                        {
                            "schema": "ies.evaluation-report",
                            "schema_version": "1.0.0",
                            "title": eval_ref,
                            "evaluator": eval_ref,
                            "metrics": [],
                            "charts": [],
                            "tables": [],
                        }
                    ),
                    encoding="utf-8",
                )

            return _handler

        handlers[f"test://{ref_id}@1.0.0"] = _make_handler(f"{ref_id}@1.0.0")

    env = _setup_pipeline_env(tmp_path, extra_evaluators=tuple(specs))
    try:
        eval_reg = build_evaluator_registry((BUILTIN_EVALUATOR_FINANCE_SPEC, *specs))
        catalog = build_test_stage_catalog(extra_descriptors=tuple(descs))
        base_plane = build_test_compute_plane(
            tmp_path / "stages",
            evaluator_registry=eval_reg,
            stage_catalog=catalog,
            evaluator_handlers=handlers,
        )
        cfg = _valid_13_config(
            evaluators=[{"evaluator": f"{s.plugin_id}@1.0.0", "parameters": {}} for s in specs]
        )

        # evaluator_max_concurrency == 1 -> strict sequential execution (peak == 1).
        state["active"] = 0
        state["peak"] = 0
        seq_plane = replace(base_plane, evaluator_max_concurrency=1)
        t1 = _save_and_submit(env, cfg, idempotency_key="p2-02-seq")
        assert _run(env["engine"], _claim(env["db"], t1), seq_plane) == "completed"
        assert state["peak"] == 1

        # evaluator_max_concurrency == 2 with 3 evaluators -> bounded concurrency (peak == 2).
        state["active"] = 0
        state["peak"] = 0
        par_plane = replace(base_plane, evaluator_max_concurrency=2)
        sub_status, sub_body = _submit_task(
            env["client"],
            env["pid"],
            env["user"],
            config={"config_revision": 1},
            idempotency_key="p2-02-par",
        )
        assert sub_status == 201, sub_body
        t2 = int(sub_body["task"]["id"])
        assert _run(env["engine"], _claim(env["db"], t2), par_plane) == "completed"
        assert state["peak"] == 2
    finally:
        env["db"].close()
        env["engine"].dispose()


def test_p2_03_evaluator_start_failure_isolated_and_control_signals_propagated(
    tmp_path: Path,
) -> None:
    """P2-03: non-launch rejections isolate to EVAL-START-001; ambiguous/control errors propagate."""
    from iesplan.computation.runtime.job_runner_client import (
        JobRunnerBadRequestError,
        JobRunnerClientError,
        JobRunnerUnavailableError,
    )
    from iesplan.computation.runtime.stage_runner import ControlPlaneDetached
    from iesplan.worker import runner as worker_runner

    start_err_ref = "acme.evaluator.start_err@1.0.0"
    start_err_spec = StagePluginSpec(
        plugin_id="acme.evaluator.start_err",
        version="1.0.0",
        kind="evaluator",
        consumes=("ies.engineering-solution@1.0.0",),
        produces=(EVALUATION_REPORT_CONTRACT,),
        outputs=("solution", "metrics"),
        parameters_schema={"type": "object", "additionalProperties": False},
    )
    start_err_desc = BuiltinStageDescriptor(
        plugin_id="acme.evaluator.start_err",
        version="1.0.0",
        kind="evaluator",
        image="pies-builtin-stages:1.2.0",
        entrypoint="report:eval",
        manifest_path=f"test://{start_err_ref}",
    )

    env = _setup_pipeline_env(tmp_path, extra_evaluators=(start_err_spec,))
    try:
        eval_reg = build_evaluator_registry((BUILTIN_EVALUATOR_FINANCE_SPEC, start_err_spec))
        catalog = build_test_stage_catalog(extra_descriptors=(start_err_desc,))
        plane = build_test_compute_plane(
            tmp_path / "stages",
            evaluator_registry=eval_reg,
            stage_catalog=catalog,
        )
        orig_start = plane.stage_runner.client.start_stage

        def _fail_one_start(spec: Any) -> Any:
            if "acme.evaluator.start_err" in str(spec.stage_id):
                raise JobRunnerBadRequestError("invalid evaluator container spec", status_code=400)
            return orig_start(spec)

        plane.stage_runner.client.start_stage = _fail_one_start  # type: ignore[method-assign]
        cfg = _valid_13_config(
            evaluators=[
                {"evaluator": start_err_ref, "parameters": {}},
                {"evaluator": "ies.evaluator.finance@1.0.0", "parameters": {}},
            ]
        )
        task_id = _save_and_submit(env, cfg, idempotency_key="p2-03-isolate")
        assert _run(env["engine"], _claim(env["db"], task_id), plane) == "completed"

        headers = login_headers(env["client"], env["user"])
        res = env["client"].get(f"/api/projects/{env['pid']}/tasks/{task_id}/result", headers=headers)
        assert res.status_code == 200
        result_view = res.json()["result"]
        assert result_view["financial_evaluation"] is not None
        by_eval = {item["evaluator"]: item for item in result_view["evaluator_results"]}
        assert by_eval["ies.evaluator.finance@1.0.0"]["status"] == "succeeded"
        assert by_eval[start_err_ref]["status"] == "failed"
        assert by_eval[start_err_ref]["error"]["code"] == "EVAL-START-001"

        # Post-persist cleanup_stage JobRunnerClientError is NOT swallowed as EVAL-START-001,
        # and does NOT clobber the already-persisted evaluator checkpoint.
        plane.stage_runner.client.start_stage = orig_start  # type: ignore[method-assign]
        orig_cleanup = plane.stage_runner.client.cleanup_stage

        def _fail_finance_cleanup(stage_execution_id: str, *, force: bool = False) -> Any:
            if "ies.evaluator.finance" in stage_execution_id:
                raise JobRunnerClientError("cleanup failed post-persist", status_code=500)
            return orig_cleanup(stage_execution_id, force=force)

        plane.stage_runner.client.cleanup_stage = _fail_finance_cleanup  # type: ignore[method-assign]
        sub_c_status, sub_c_body = _submit_task(
            env["client"],
            env["pid"],
            env["user"],
            config={"config_revision": 1},
            idempotency_key="p2-03-post-persist-cleanup",
        )
        assert sub_c_status == 201, sub_c_body
        t_cleanup = int(sub_c_body["task"]["id"])
        c_cleanup = _claim(env["db"], t_cleanup)
        try:
            assert worker_runner.run_task(env["sf"], c_cleanup, worker_id="t", compute=plane) == "failed"
            with env["sf"]() as check_db:
                ev_ckpt = worker_app.load_evaluator_checkpoint(check_db, c_cleanup, 1, 1)
                assert ev_ckpt is not None
                assert ev_ckpt["result_entry"]["status"] == "succeeded"
                assert ev_ckpt["result_entry"]["error"] is None
        finally:
            plane.stage_runner.client.cleanup_stage = orig_cleanup  # type: ignore[method-assign]

        # Workspace storage OSError is NOT misclassified as EVAL-START-001.
        orig_prepare = plane.stage_runner._prepare_workspace

        def _fail_workspace_io(workspace_rel: str, spec: Any) -> None:
            if "ies.evaluator.finance" in str(spec.stage_id):
                raise OSError("disk full writing stage input")
            return orig_prepare(workspace_rel, spec)

        plane.stage_runner._prepare_workspace = _fail_workspace_io  # type: ignore[method-assign]
        sub_io_status, sub_io_body = _submit_task(
            env["client"],
            env["pid"],
            env["user"],
            config={"config_revision": 1},
            idempotency_key="p2-03-workspace-oserror",
        )
        assert sub_io_status == 201, sub_io_body
        t_io = int(sub_io_body["task"]["id"])
        c_io = _claim(env["db"], t_io)
        try:
            assert worker_runner.run_task(env["sf"], c_io, worker_id="t", compute=plane) == "failed"
            with env["sf"]() as check_db:
                ev_ckpt = worker_app.load_evaluator_checkpoint(check_db, c_io, 1, 1)
                assert ev_ckpt is None
        finally:
            plane.stage_runner._prepare_workspace = orig_prepare  # type: ignore[method-assign]

        # Ambiguous start_stage JobRunnerUnavailableError (timeout/5xx) and list_stages
        # reconciliation failures must propagate rather than isolating into EVAL-START-001.
        def _ambiguous_unavailable_start(spec: Any) -> Any:
            if "ies.evaluator.finance" in str(spec.stage_id):
                raise JobRunnerUnavailableError("start timeout after dispatch", status_code=503)
            return orig_start(spec)

        plane.stage_runner.client.start_stage = _ambiguous_unavailable_start  # type: ignore[method-assign]
        sub_u_status, sub_u_body = _submit_task(
            env["client"],
            env["pid"],
            env["user"],
            config={"config_revision": 1},
            idempotency_key="p2-03-ambiguous-unavailable",
        )
        assert sub_u_status == 201, sub_u_body
        t_unavail = int(sub_u_body["task"]["id"])
        c_unavail = _claim(env["db"], t_unavail)
        try:
            assert worker_runner.run_task(env["sf"], c_unavail, worker_id="t", compute=plane) == "failed"
            with env["sf"]() as check_db:
                ev_ckpt = worker_app.load_evaluator_checkpoint(check_db, c_unavail, 1, 1)
                assert ev_ckpt is None
        finally:
            plane.stage_runner.client.start_stage = orig_start  # type: ignore[method-assign]

        orig_list_stages = plane.stage_runner.client.list_stages

        def _fail_reconcile_list(**kwargs: Any) -> Any:
            if "ies.evaluator.finance" in str(kwargs.get("stage_id") or ""):
                raise JobRunnerUnavailableError("list_stages unavailable", status_code=503)
            return orig_list_stages(**kwargs)

        plane.stage_runner.client.list_stages = _fail_reconcile_list  # type: ignore[method-assign]
        sub_l_status, sub_l_body = _submit_task(
            env["client"],
            env["pid"],
            env["user"],
            config={"config_revision": 1},
            idempotency_key="p2-03-list-stages-fail",
        )
        assert sub_l_status == 201, sub_l_body
        t_list = int(sub_l_body["task"]["id"])
        c_list = _claim(env["db"], t_list)
        try:
            assert worker_runner.run_task(env["sf"], c_list, worker_id="t", compute=plane) == "failed"
            with env["sf"]() as check_db:
                ev_ckpt = worker_app.load_evaluator_checkpoint(check_db, c_list, 1, 1)
                assert ev_ckpt is None
        finally:
            plane.stage_runner.client.list_stages = orig_list_stages  # type: ignore[method-assign]

        # Control/lease signals are NEVER swallowed into EVAL-START-001.
        orig_run_stage = plane.stage_runner.run_stage
        for exc_obj, expected_outcome in (
            (
                worker_app.StageExecutionError(
                    stage="evaluate", status="cancelled", termination_reason="cancelled", detail="cancel"
                ),
                "cancelled",
            ),
            (ControlPlaneDetached("detached"), "detached"),
            (
                worker_app.StageExecutionError(
                    stage="evaluate", status="fenced", termination_reason="fenced", detail="fenced"
                ),
                "fenced",
            ),
        ):
            sub_status, sub_body = _submit_task(
                env["client"],
                env["pid"],
                env["user"],
                config={"config_revision": 1},
                idempotency_key=f"p2-03-ctrl-{expected_outcome}",
            )
            assert sub_status == 201, sub_body
            t_ctrl = int(sub_body["task"]["id"])
            c_ctrl = _claim(env["db"], t_ctrl)

            def _raise_ctrl(
                spec: Any,
                _exc: BaseException = exc_obj,
                _tid: int = t_ctrl,
                _expected: str = expected_outcome,
                **kwargs: Any,
            ) -> Any:
                if str(spec.stage_id).startswith("eval-"):
                    if _expected == "cancelled":
                        cancel_resp = env["client"].post(
                            f"/api/projects/{env['pid']}/tasks/{_tid}/cancel",
                            headers=headers,
                        )
                        assert cancel_resp.status_code == 200, cancel_resp.text
                    raise _exc
                return orig_run_stage(spec, **kwargs)

            plane.stage_runner.run_stage = _raise_ctrl  # type: ignore[method-assign]
            try:
                assert (
                    worker_runner.run_task(env["sf"], c_ctrl, worker_id="t", compute=plane)
                    == expected_outcome
                )
            finally:
                plane.stage_runner.run_stage = orig_run_stage  # type: ignore[method-assign]
    finally:
        env["db"].close()
        env["engine"].dispose()


def test_evaluator_retry_preserves_newer_version_latest_result_index(tmp_path: Path) -> None:
    """Retry updates ResultIndex.is_latest only when source task still holds version latest."""
    fail_finance = {"should_fail": True}

    def _finance_handler(work_in: Path, work_out: Path) -> None:
        if fail_finance["should_fail"]:
            raise RuntimeError("transient finance error")
        from stage_testkit import _artifact_from_json

        from iesplan.finance import (
            EffectiveFinanceConfig,
            EvaluationInput,
            evaluate_forward_finance,
        )

        sol = json.loads((work_in / "solution.json").read_text(encoding="utf-8"))
        req = json.loads((work_in / "request.json").read_text(encoding="utf-8"))
        fin_eval = evaluate_forward_finance(
            sol,
            artifact=_artifact_from_json(req["artifact"]),
            finance=EffectiveFinanceConfig.from_dict(req["finance"]),
            evaluation=EvaluationInput.from_frozen(req["evaluation"]),
            device_models=req.get("device_models"),
        )
        (work_out / "financial_evaluation.json").write_text(json.dumps(fin_eval), encoding="utf-8")

    env = _setup_pipeline_env(tmp_path)
    try:
        plane = build_test_compute_plane(
            tmp_path / "stages",
            evaluator_handlers={"/app/plugins/evaluator_finance/plugin.yaml": _finance_handler},
        )
        headers = login_headers(env["client"], env["user"])

        # Case (a): Retrying current latest task T1 promotes T1's new evidence to is_latest=True.
        t1_id = _save_and_submit(env, _valid_13_config(), idempotency_key="idx-t1")
        assert _run(env["engine"], _claim(env["db"], t1_id), plane) == "completed"
        with env["sf"]() as db:
            t1_ev1 = results_store.latest_evidence_for_task(db, t1_id)
            assert t1_ev1 is not None
            snap_version_id = tasks_store.get_snapshot(
                db, tasks_store.get_task(db, t1_id).calc_snapshot_id
            ).project_version_id
            idx1 = results_store.latest_index_for_version(db, snap_version_id)
            assert idx1 is not None and idx1.evidence_package_id == t1_ev1.id

        fail_finance["should_fail"] = False
        r1 = env["client"].post(
            f"/api/projects/{env['pid']}/tasks/{t1_id}/result/evaluators/retry",
            json={"evaluator": "ies.evaluator.finance@1.0.0", "idempotency_key": "idx-t1-retry"},
            headers=headers,
        )
        assert r1.status_code == 201, r1.text
        assert _run(env["engine"], _claim(env["db"], r1.json()["task"]["id"]), plane) == "completed"
        with env["sf"]() as db:
            t1_ev2 = results_store.latest_evidence_for_task(db, t1_id)
            assert t1_ev2 is not None and t1_ev2.id != t1_ev1.id
            idx2 = results_store.latest_index_for_version(db, snap_version_id)
            assert idx2 is not None and idx2.evidence_package_id == t1_ev2.id
            assert results_store.list_package_index(db, t1_ev1.id)[0].is_latest is False

        # Case (b): Submit T_old (fails finance) -> newer T2 completes on same project_version_id ->
        # retrying T_old updates T_old's evidence package, with is_latest=False, keeping T2 as latest.
        fail_finance["should_fail"] = True
        t_old_status, t_old_body = _submit_task(
            env["client"],
            env["pid"],
            env["user"],
            config={"config_revision": 1},
            idempotency_key="idx-told",
        )
        assert t_old_status == 201, t_old_body
        t_old_id = int(t_old_body["task"]["id"])
        assert _run(env["engine"], _claim(env["db"], t_old_id), plane) == "completed"

        fail_finance["should_fail"] = False
        t2_status, t2_body = _submit_task(
            env["client"],
            env["pid"],
            env["user"],
            config={"config_revision": 1},
            idempotency_key="idx-t2",
        )
        assert t2_status == 201, t2_body
        t2_id = int(t2_body["task"]["id"])
        assert _run(env["engine"], _claim(env["db"], t2_id), plane) == "completed"
        with env["sf"]() as db:
            t_old_ev1 = results_store.latest_evidence_for_task(db, t_old_id)
            t2_ev = results_store.latest_evidence_for_task(db, t2_id)
            assert t_old_ev1 is not None and t2_ev is not None
            idx_t2 = results_store.latest_index_for_version(db, snap_version_id)
            assert idx_t2 is not None and idx_t2.evidence_package_id == t2_ev.id

        r_old = env["client"].post(
            f"/api/projects/{env['pid']}/tasks/{t_old_id}/result/evaluators/retry",
            json={"evaluator": "ies.evaluator.finance@1.0.0", "idempotency_key": "idx-told-retry"},
            headers=headers,
        )
        assert r_old.status_code == 201, r_old.text
        assert _run(env["engine"], _claim(env["db"], r_old.json()["task"]["id"]), plane) == "completed"

        with env["sf"]() as db:
            t_old_ev2 = results_store.latest_evidence_for_task(db, t_old_id)
            assert t_old_ev2 is not None and t_old_ev2.id != t_old_ev1.id
            idx_after = results_store.latest_index_for_version(db, snap_version_id)
            assert idx_after is not None and idx_after.evidence_package_id == t2_ev.id
            retry_idx_rows = results_store.list_package_index(db, t_old_ev2.id)
            assert len(retry_idx_rows) == 1 and retry_idx_rows[0].is_latest is False

        res_old = env["client"].get(
            f"/api/projects/{env['pid']}/tasks/{t_old_id}/result", headers=headers
        )
        assert res_old.status_code == 200
        assert res_old.json()["result"]["evidence"]["id"] == t_old_ev2.id
        assert res_old.json()["result"]["financial_evaluation"] is not None
    finally:
        env["db"].close()
        env["engine"].dispose()
