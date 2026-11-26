"""Test-only JobRunnerClient MockTransport + ContainerStageRunner harness.

Used by backend `--no-deps` unit/integration tests that do not have a Docker
daemon or mounted `/var/run/docker.sock`. Production code in `iesplan/` runs
exclusively through `ContainerStageRunner` -> `JobRunnerClient`.
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from iesplan.application.worker import ComputeExecutionPlane
from iesplan.assembly import ValidatedAssemblyArtifact, ValidationReceipt
from iesplan.computation import (
    NAIVE_PREDICTOR,
    PRIMAL_SOLUTION_OUTPUT_PATH,
    CalculationConfig,
    MaterializedPriceSeries,
    MaterializedSeries,
    MaterializedSet,
    build_evaluator_registry,
    build_predictor_registry,
    build_solver_registry,
    thaw_json_value,
)
from iesplan.computation.bundle import manifest_to_yaml_bytes
from iesplan.computation.config import BUILTIN_EVALUATOR_FINANCE_SPEC, BUILTIN_SOLVER_HIGHS_SPEC
from iesplan.computation.generators import HIGHS_MILP_PROVIDER, build_registry
from iesplan.computation.runtime.job_runner_client import JobRunnerClient
from iesplan.computation.runtime.stage_runner import (
    BuiltinStageCatalog,
    BuiltinStageDescriptor,
    ContainerStagePolicy,
    ContainerStageRunner,
    build_builtin_stage_catalog,
)
from iesplan.core.content import Content
from iesplan.core.diagnostics import Diagnostic
from iesplan.core.yamlmini import load as yaml_load
from iesplan.finance import EffectiveFinanceConfig, EvaluationInput, evaluate_forward_finance
from iesplan.modeling import contribution_from_dict


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class _FileContent:
    """Zero-copy Content wrapper over a staged workspace file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.size_bytes = path.stat().st_size

    def iter_chunks(self, chunk_size: int = 65536):
        with open(self._path, "rb") as fh:
            while chunk := fh.read(chunk_size):
                yield chunk


def _project_csv_column(csv_path: Path, interface_id: str) -> tuple[float, ...]:
    lines = csv_path.read_text(encoding="utf-8-sig").splitlines()
    header_idx = next(
        idx for idx, line in enumerate(lines) if line.strip() and not line.strip().startswith("#")
    )
    rows = list(csv.reader(lines[header_idx:]))
    header = [cell.strip().lower() for cell in rows[0]]
    col = header.index(interface_id.lower())
    return tuple(
        float(cells[col].strip())
        for cells in rows[1:]
        if any(cell.strip() for cell in cells)
    )


def _artifact_from_json(raw: Mapping[str, Any]) -> ValidatedAssemblyArtifact:
    r = raw["receipt"]
    receipt = ValidationReceipt(
        schema_id=r["schema"],
        schema_version=r["schema_version"],
        validator_id=r["validator"]["id"],
        validator_version=r["validator"]["version"],
        canonical_algorithm_id=r["canonical_algorithm"]["id"],
        canonical_algorithm_version=r["canonical_algorithm"]["version"],
        dependencies=dict(r["dependencies"]),
        resources=dict(r["resources"]),
        finance_snapshot=dict(r["finance_snapshot"]),
        planning=dict(r["planning"]),
        system_equations=dict(r["system_equations"]),
        diagnostics=tuple(Diagnostic(**dict(entry)) for entry in r["diagnostics"]),
    )
    return ValidatedAssemblyArtifact(canonical_text=raw["canonical_text"], receipt=receipt)


def _materialized_from_json(raw: Mapping[str, Any]) -> MaterializedSet:
    series = tuple(
        MaterializedSeries(
            device=entry["device"],
            interface=entry["interface"],
            mode=entry["mode"],
            resolution=entry["resolution"],
            point_count=entry["point_count"],
            values=tuple(entry["values"]),
            source=dict(entry["source"]),
        )
        for entry in raw.get("series", ())
    )
    pending = tuple(dict(entry) for entry in raw.get("pending_predict", ()))
    prices = tuple(
        MaterializedPriceSeries(
            price_id=entry["price_id"],
            point_count=entry["point_count"],
            resolution=entry["resolution"],
            unit=entry["unit"],
            values=tuple(entry["values"]),
            source=dict(entry["source"]),
        )
        for entry in raw.get("prices", ())
    )
    return MaterializedSet(
        operation_years=int(raw["operation_years"]),
        series=series,
        pending_predict=pending,
        prices=prices,
    )


def _stream_content_to_file(content: Content, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as fh:
        for chunk in content.iter_chunks():
            fh.write(chunk)


class MockJobRunnerBackend:
    """In-memory job_runner HTTP transport executing stage workspaces on disk."""

    def __init__(
        self,
        *,
        stage_root: Path,
        generator_registry: Any,
        predictor_registry: Any,
        stage_overrides: Mapping[str, str] | None = None,
        solver_argv_override: list[str] | None = None,
        evaluator_handlers: Mapping[str, Any] | None = None,
        on_start: Any = None,
    ) -> None:
        self.stage_root = Path(stage_root)
        self.generator_registry = generator_registry
        self.predictor_registry = predictor_registry
        self.stage_overrides = dict(stage_overrides or {})
        self.solver_argv_override = list(solver_argv_override) if solver_argv_override else None
        self.evaluator_handlers = dict(evaluator_handlers or {})
        self.on_start = on_start
        self._lock = threading.Lock()
        self._stages: dict[str, dict[str, Any]] = {}
        self._procs: dict[str, subprocess.Popen[str]] = {}
        self._stop_events: dict[str, threading.Event] = {}

    def _override_for_stage(self, stage_id: str) -> str | None:
        if stage_id in self.stage_overrides:
            return self.stage_overrides[stage_id]
        prefix = stage_id.split("-", 1)[0]
        return self.stage_overrides.get(prefix)

    def _discover_outputs(self, out_dir: Path) -> list[str]:
        if not out_dir.is_dir():
            return []
        return sorted(
            str(path.relative_to(out_dir)).replace("\\", "/")
            for path in out_dir.rglob("*")
            if path.is_file()
        )

    def _run_stage_worker(self, stage_execution_id: str, body: dict[str, Any]) -> None:
        stage_id = str(body["stage_id"])
        workspace_rel = str(body["workspace_rel"])
        work_in = self.stage_root / workspace_rel / "in"
        work_out = self.stage_root / workspace_rel / "out"
        work_out.mkdir(parents=True, exist_ok=True)
        stop_ev = self._stop_events[stage_execution_id]

        override = self._override_for_stage(stage_id)
        if override is not None:
            with self._lock:
                st = self._stages[stage_execution_id]
                st["running"] = False
                st["status"] = "exited"
                st["exit_code"] = 137 if override in ("cancelled", "fenced", "timed_out", "oom") else 1
                st["termination_reason"] = override
                st["finished_at"] = _utc_now()
            return

        kind = stage_id.split("-", 1)[0]
        try:
            if kind == "predict":
                self._exec_predict(work_in, work_out, body)
            elif kind == "generate":
                self._exec_generate(work_in, work_out, stop_ev)
            elif kind == "solve":
                self._exec_solve(stage_execution_id, work_in, work_out, stop_ev)
            elif kind in ("eval", "evaluate", "algorithm"):
                self._exec_evaluate(work_in, work_out, body)
            else:
                raise RuntimeError(f"Unknown mock stage kind: {kind!r}")
        except Exception:
            with self._lock:
                st = self._stages[stage_execution_id]
                if st["termination_reason"] is None:
                    st["running"] = False
                    st["status"] = "exited"
                    st["exit_code"] = 1
                    st["termination_reason"] = "process_failed"
                    st["finished_at"] = _utc_now()
            return

        with self._lock:
            st = self._stages[stage_execution_id]
            if st["termination_reason"] is None:
                st["running"] = False
                st["status"] = "exited"
                st["exit_code"] = 0
                st["termination_reason"] = "normal"
                st["finished_at"] = _utc_now()

    def _exec_predict(self, work_in: Path, work_out: Path, body: Mapping[str, Any]) -> None:
        manifest_path = ""
        cmd = body.get("command") or []
        if "--manifest" in cmd:
            manifest_path = str(cmd[cmd.index("--manifest") + 1])
        algo_ref = "ies.predictor.naive@1.0.0"
        if manifest_path.startswith("test://"):
            algo_ref = manifest_path.removeprefix("test://")
        req = json.loads((work_in / "request.json").read_text(encoding="utf-8"))
        point_count = int(req["point_count"])
        seed = req.get("seed")
        predictor = self.predictor_registry.get(algo_ref)
        artifacts_dir = work_out / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        predictions = []
        artifact_paths = []
        for target in sorted(req["targets"], key=lambda item: (item["instance"], item["interface"])):
            instance = str(target["instance"])
            interface = str(target["interface"])
            history = _project_csv_column(work_in / str(target["history_csv_rel"]), interface)
            predicted = predictor.predict(
                device=instance,
                interface=interface,
                target_type=str(target["target_type"]),
                history=history,
                history_object_id=str(target.get("history_object_id") or ""),
                parameters=dict(target.get("parameters") or {}),
                seed=seed,
                point_count=point_count,
            )
            art_rel = f"artifacts/{instance}__{interface}.json"
            (work_out / art_rel).write_bytes(predicted.artifact)
            artifact_paths.append(art_rel)
            predictions.append(
                {
                    "instance": predicted.device,
                    "interface": predicted.interface,
                    "target_type": predicted.target_type,
                    "history_object_id": predicted.history_object_id,
                    "values": list(predicted.values),
                    "artifact_path": art_rel,
                    "artifact_media_type": predicted.artifact_media_type,
                    "receipt": thaw_json_value(predicted.receipt),
                }
            )
        (work_out / "prediction.json").write_text(
            json.dumps(
                {
                    "schema": "ies.prediction-output",
                    "schema_version": "1.0.0",
                    "predictions": predictions,
                    "artifacts": artifact_paths,
                }
            ),
            encoding="utf-8",
        )

    def _exec_generate(
        self,
        work_in: Path,
        work_out: Path,
        stop_ev: threading.Event,
    ) -> None:
        from iesplan.computation import materialize_non_predictive_series
        from iesplan.finance import expand_monthly_daily_prices, read_trusted_price_values

        req = json.loads((work_in / "request.json").read_text(encoding="utf-8"))
        artifact = _artifact_from_json(req["artifact"])
        models = {inst: contribution_from_dict(c) for inst, c in req["device_models"].items()}
        finance = EffectiveFinanceConfig.from_dict(req["finance"])
        config = CalculationConfig.from_frozen(req["calculation_config"])
        evaluation = EvaluationInput.from_frozen(req["evaluation"])
        if "frozen_series_inputs" in req:
            frozen = req["frozen_series_inputs"]
            resources = {
                str(oid): (work_in / str(rel)).read_bytes()
                for oid, rel in (frozen.get("fixed_resources") or {}).items()
            }
            base = materialize_non_predictive_series(
                artifact,
                resources,
                operation_years=evaluation.operation_years,
            )
            doc = json.loads(artifact.canonical_text)
            baseline = doc["project_baseline"]
            prices = []
            seen_prices: set[str] = set()
            for binding_id in sorted(doc.get("tariff_bindings") or {}):
                price_id = (doc["tariff_bindings"][binding_id] or {}).get("price")
                price = (finance.energy_prices or {}).get(price_id)
                if (
                    price is None
                    or price_id in seen_prices
                    or price.kind not in ("time_series", "monthly_daily")
                ):
                    continue
                seen_prices.add(price_id)
                if price.kind == "time_series":
                    assert price.ref is not None and price.series_meta is not None
                    vals = read_trusted_price_values(resources[price.ref])
                    meta = price.series_meta
                    source: dict[str, Any] = {"kind": "price_series", "object_id": price.ref}
                else:
                    assert price.months is not None
                    curves = {
                        m: read_trusted_price_values(resources[price.months[str(m)].ref])
                        for m in range(1, 13)
                    }
                    vals = expand_monthly_daily_prices(
                        curves,
                        resolution=baseline["resolution"],
                        start_year=baseline["start_year"],
                        operation_years=evaluation.operation_years,
                    )
                    meta = price.months["1"].series_meta
                    source = {
                        "kind": "monthly_daily",
                        "months": {str(m): price.months[str(m)].ref for m in range(1, 13)},
                    }
                prices.append(
                    MaterializedPriceSeries(
                        price_id=price_id,
                        point_count=len(vals),
                        resolution=meta.resolution,
                        unit=meta.unit,
                        values=vals,
                        source=source,
                    )
                )
            predicted = tuple(
                MaterializedSeries(
                    device=entry["device"],
                    interface=entry["interface"],
                    mode=entry["mode"],
                    resolution=entry["resolution"],
                    point_count=entry["point_count"],
                    values=tuple(entry["values"]),
                    source=dict(entry["source"]),
                )
                for entry in frozen.get("predicted_series") or ()
            )
            materialized = MaterializedSet(
                operation_years=base.operation_years,
                series=tuple(base.series) + predicted,
                pending_predict=(),
                prices=tuple(prices),
            )
        else:
            materialized = _materialized_from_json(req["materialized"])
        provider = self.generator_registry.resolve(config)
        gen_with_stop = getattr(provider, "generate_with_stop", None)
        if callable(gen_with_stop):
            bundle = gen_with_stop(
                artifact,
                models,
                materialized,
                finance,
                config,
                evaluation,
                stop=lambda: "cancelled" if stop_ev.is_set() else None,
            )
        else:
            bundle = provider.generate(artifact, models, materialized, finance, config, evaluation)
        bundle_dir = work_out / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        for rel_path in sorted(bundle.files):
            _stream_content_to_file(bundle.files[rel_path], bundle_dir / rel_path)
        (bundle_dir / "bundle.yaml").write_bytes(manifest_to_yaml_bytes(bundle.manifest))

    def _exec_solve(
        self,
        stage_execution_id: str,
        work_in: Path,
        work_out: Path,
        stop_ev: threading.Event,
    ) -> None:
        from test_f2_solver_restoration import _v1_sol_to_primal_json

        bundle_dir = work_in / "bundle"
        manifest = yaml_load((bundle_dir / "bundle.yaml").read_text(encoding="utf-8"))
        outputs = manifest.get("outputs") or []
        primal_rel = str(outputs[0]["path"]) if outputs else PRIMAL_SOLUTION_OUTPUT_PATH
        req_path = work_in / "request.json"
        req = json.loads(req_path.read_text(encoding="utf-8")) if req_path.is_file() else {}
        time_limit = str(int(req.get("time_limit_seconds", 60)))
        seed = str(int(req.get("seed", 0)))

        if self.solver_argv_override is not None:
            argv = list(self.solver_argv_override)
        else:
            argv = [
                "/usr/local/bin/ies-highs-solver",
                "input/problem.mps",
                "--solution_file",
                "output/solution.sol",
                "--time_limit",
                time_limit,
                "--random_seed",
                seed,
            ]
            if (bundle_dir / "input" / "acceleration.json").is_file():
                argv.extend([
                    "--acceleration_mode",
                    "on_demand_mutex",
                    "--acceleration_file",
                    "input/acceleration.json",
                ])
            if (bundle_dir / "input" / "epigraphs.json").is_file():
                argv.extend(["--epigraph_file", "input/epigraphs.json"])

        run_dir = Path(tempfile.mkdtemp(prefix="ies-test-solve-"))
        started_at = _utc_now()
        try:
            in_dir = bundle_dir / "input"
            if in_dir.is_dir():
                (run_dir / "input").symlink_to(in_dir.resolve())
            (run_dir / "output").symlink_to(work_out.resolve())

            proc = subprocess.Popen(
                argv,
                cwd=run_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            with self._lock:
                self._procs[stage_execution_id] = proc
            stdout, stderr = proc.communicate()
            ended_at = _utc_now()
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)
            with self._lock:
                self._procs.pop(stage_execution_id, None)

        if stop_ev.is_set():
            return
        (work_out / "solver_meta.json").write_text(
            json.dumps(
                {
                    "exit_code": proc.returncode,
                    "termination_reason": "exited",
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "stdout": stdout,
                    "stderr": stderr,
                    "executable": "ies.solver.highs@1.7.2",
                }
            ),
            encoding="utf-8",
        )
        if proc.returncode != 0:
            raise RuntimeError(f"solver exited with code {proc.returncode}: {stderr}")
        primal_out = work_out / Path(primal_rel).name
        if not primal_out.is_file():
            sol_file = work_out / "solution.sol"
            if not sol_file.is_file():
                raise RuntimeError("mock solver did not produce solution.sol or primal_solution.json")
            primal_doc = _v1_sol_to_primal_json(sol_file.read_text(encoding="utf-8"))
            primal_out.write_text(json.dumps(primal_doc), encoding="utf-8")

    def _materialize_from_req(
        self,
        work_in: Path,
        req: Mapping[str, Any],
        artifact: ValidatedAssemblyArtifact,
        finance: EffectiveFinanceConfig,
        evaluation: EvaluationInput,
    ) -> MaterializedSet:
        from iesplan.computation import materialize_non_predictive_series
        from iesplan.finance import expand_monthly_daily_prices, read_trusted_price_values

        if "frozen_series_inputs" not in req:
            return _materialized_from_json(req["materialized"])
        frozen = req["frozen_series_inputs"]
        resources = {
            str(oid): (work_in / str(rel)).read_bytes()
            for oid, rel in (frozen.get("fixed_resources") or {}).items()
        }
        base = materialize_non_predictive_series(
            artifact,
            resources,
            operation_years=evaluation.operation_years,
        )
        doc = json.loads(artifact.canonical_text)
        baseline = doc["project_baseline"]
        prices = []
        seen_prices: set[str] = set()
        for binding_id in sorted(doc.get("tariff_bindings") or {}):
            price_id = (doc["tariff_bindings"][binding_id] or {}).get("price")
            price = (finance.energy_prices or {}).get(price_id)
            if (
                price is None
                or price_id in seen_prices
                or price.kind not in ("time_series", "monthly_daily")
            ):
                continue
            seen_prices.add(price_id)
            if price.kind == "time_series":
                assert price.ref is not None and price.series_meta is not None
                vals = read_trusted_price_values(resources[price.ref])
                meta = price.series_meta
                source: dict[str, Any] = {"kind": "price_series", "object_id": price.ref}
            else:
                assert price.months is not None
                curves = {
                    m: read_trusted_price_values(resources[price.months[str(m)].ref])
                    for m in range(1, 13)
                }
                vals = expand_monthly_daily_prices(
                    curves,
                    resolution=baseline["resolution"],
                    start_year=baseline["start_year"],
                    operation_years=evaluation.operation_years,
                )
                meta = price.months["1"].series_meta
                source = {
                    "kind": "monthly_daily",
                    "months": {str(m): price.months[str(m)].ref for m in range(1, 13)},
                }
            prices.append(
                MaterializedPriceSeries(
                    price_id=price_id,
                    point_count=len(vals),
                    resolution=meta.resolution,
                    unit=meta.unit,
                    values=vals,
                    source=source,
                )
            )
        predicted = tuple(
            MaterializedSeries(
                device=entry["device"],
                interface=entry["interface"],
                mode=entry["mode"],
                resolution=entry["resolution"],
                point_count=entry["point_count"],
                values=tuple(entry["values"]),
                source=dict(entry["source"]),
            )
            for entry in frozen.get("predicted_series") or ()
        )
        return MaterializedSet(
            operation_years=base.operation_years,
            series=tuple(base.series) + predicted,
            pending_predict=(),
            prices=tuple(prices),
        )

    def _exec_evaluate(self, work_in: Path, work_out: Path, body: Mapping[str, Any]) -> None:
        cmd = body.get("command") or []
        manifest_path = str(cmd[cmd.index("--manifest") + 1]) if "--manifest" in cmd else ""
        handler = self.evaluator_handlers.get(manifest_path)
        if handler is not None:
            handler(work_in, work_out)
            return

        solution = json.loads((work_in / "solution.json").read_text(encoding="utf-8"))
        req = json.loads((work_in / "request.json").read_text(encoding="utf-8"))
        artifact = _artifact_from_json(req["artifact"])
        finance = EffectiveFinanceConfig.from_dict(req["finance"])
        evaluation = EvaluationInput.from_frozen(req["evaluation"])
        materialized = self._materialize_from_req(work_in, req, artifact, finance, evaluation)
        raw_models = req.get("device_models")
        fin_eval = evaluate_forward_finance(
            solution,
            artifact=artifact,
            finance=finance,
            evaluation=evaluation,
            materialized=materialized,
            device_models=raw_models,
        )
        (work_out / "financial_evaluation.json").write_text(
            json.dumps(fin_eval), encoding="utf-8"
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        path = request.url.path
        if path == "/readyz":
            return httpx.Response(200, json={"status": "ready"})
        if request.method == "GET" and path == "/v1/stages":
            aid = request.url.params.get("attempt_id")
            sid = request.url.params.get("stage_id")
            with self._lock:
                items = [
                    self._payload(st)
                    for st in self._stages.values()
                    if (aid is None or st["attempt_id"] == aid)
                    and (sid is None or st["stage_id"] == sid)
                ]
            return httpx.Response(200, json={"items": items, "total": len(items)})
        if request.method == "POST" and path == "/v1/stages/start":
            seid = str(body["stage_execution_id"])
            if self.on_start is not None:
                self.on_start(body)
            with self._lock:
                if seid in self._stages:
                    payload = self._payload(self._stages[seid], reused=True)
                    return httpx.Response(200, json=payload)
                self._stop_events[seid] = threading.Event()
                st = {
                    "stage_execution_id": seid,
                    "attempt_id": str(body["attempt_id"]),
                    "stage_id": str(body["stage_id"]),
                    "fencing_token": str(body["fencing_token"]),
                    "container_id": f"cid-{seid}",
                    "container_name": f"pies-stage-{seid}",
                    "workspace_rel": str(body["workspace_rel"]),
                    "image": str(body["image"]),
                    "status": "running",
                    "running": True,
                    "exit_code": None,
                    "termination_reason": None,
                    "started_at": _utc_now(),
                    "finished_at": None,
                    "reused": False,
                }
                self._stages[seid] = st
            t = threading.Thread(target=self._run_stage_worker, args=(seid, body), daemon=True)
            t.start()
            # Give fast stages a moment so inspect doesn't wait unnecessarily
            t.join(timeout=0.05)
            with self._lock:
                return httpx.Response(200, json=self._payload(self._stages[seid]))
        if request.method == "GET" and path.startswith("/v1/stages/"):
            seid = path.split("/")[-1]
            with self._lock:
                st = self._stages.get(seid)
                if st is None:
                    return httpx.Response(404, json={"detail": "not found"})
                return httpx.Response(200, json=self._payload(st))
        if request.method == "POST" and path.endswith("/stop"):
            seid = path.split("/")[-2]
            reason = str((body or {}).get("reason") or "cancelled")
            with self._lock:
                proc = self._procs.get(seid)
                ev = self._stop_events.get(seid)
                st = self._stages.get(seid)
            if st is None:
                return httpx.Response(404, json={"detail": "not found"})
            if ev is not None:
                ev.set()
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1.0)
            with self._lock:
                st["running"] = False
                st["status"] = "exited"
                st["exit_code"] = 137
                st["termination_reason"] = reason
                st["finished_at"] = _utc_now()
                return httpx.Response(200, json=self._payload(st))
        if request.method == "POST" and path.endswith("/cleanup"):
            seid = path.split("/")[-2]
            with self._lock:
                st = self._stages.pop(seid, None)
            if st is not None:
                shutil.rmtree(self.stage_root / st["workspace_rel"], ignore_errors=True)
            return httpx.Response(
                200,
                json={
                    "stage_execution_id": seid,
                    "status": "cleaned",
                    "container_removed": True,
                    "workspace_removed": True,
                },
            )
        return httpx.Response(404, json={"detail": f"Unhandled path: {path}"})

    def _payload(self, st: Mapping[str, Any], *, reused: bool | None = None) -> dict[str, Any]:
        out_dir = self.stage_root / st["workspace_rel"] / "out"
        is_oom = st["termination_reason"] == "oom"
        return {
            **st,
            "reused": st["reused"] if reused is None else reused,
            "resources": {
                "source": "docker_and_cgroup_v2",
                "stage_execution_id": st["stage_execution_id"],
                "memory_peak_bytes": {"status": "known", "value": 8_388_608},
                "oom_killed": {"status": "known", "value": is_oom},
                "oom_kill_count": {"status": "known", "value": 1 if is_oom else 0},
            },
            "iterations": {"present": False, "count": 0, "tail": []},
            "output_files": self._discover_outputs(out_dir),
        }


def build_test_stage_catalog(
    *,
    builtin_image: str = "pies-builtin-stages:1.2.0",
    predictor_registry: Any | None = None,
    generator_registry: Any | None = None,
    extra_descriptors: tuple[BuiltinStageDescriptor, ...] = (),
) -> BuiltinStageCatalog:
    base = list(build_builtin_stage_catalog(builtin_image).snapshot())
    seen_refs = {d.ref for d in base}
    for desc in extra_descriptors:
        if desc.ref not in seen_refs:
            base.append(desc)
            seen_refs.add(desc.ref)
    if predictor_registry is not None:
        for d in predictor_registry.snapshot():
            if d.ref not in seen_refs:
                pid, _, ver = d.ref.rpartition("@")
                base.append(
                    BuiltinStageDescriptor(
                        plugin_id=pid,
                        version=ver,
                        kind="predictor",
                        image=builtin_image,
                        entrypoint="predictor_stage:predict",
                        manifest_path=f"test://{d.ref}",
                    )
                )
                seen_refs.add(d.ref)
    if generator_registry is not None:
        for d in generator_registry.snapshot():
            if d.ref not in seen_refs:
                gid, _, ver = d.ref.rpartition("@")
                base.append(
                    BuiltinStageDescriptor(
                        plugin_id=gid,
                        version=ver,
                        kind="generator",
                        image=builtin_image,
                        entrypoint="generator_stage:generate",
                        manifest_path=f"test://{d.ref}",
                    )
                )
                seen_refs.add(d.ref)
    return BuiltinStageCatalog(tuple(base))


def build_test_compute_plane(
    stage_root: Path,
    *,
    generator_registry: Any | None = None,
    predictor_registry: Any | None = None,
    solver_registry: Any | None = None,
    evaluator_registry: Any | None = None,
    algorithm_registry: Any | None = None,
    stage_catalog: BuiltinStageCatalog | None = None,
    stage_overrides: Mapping[str, str] | None = None,
    solver_argv_override: list[str] | None = None,
    evaluator_handlers: Mapping[str, Any] | None = None,
    policy: ContainerStagePolicy | None = None,
    poll_interval: float = 0.01,
    on_start: Any = None,
) -> ComputeExecutionPlane:
    gen_reg = generator_registry or build_registry((HIGHS_MILP_PROVIDER,))
    pred_reg = predictor_registry or build_predictor_registry((NAIVE_PREDICTOR,))
    sol_reg = solver_registry or build_solver_registry((BUILTIN_SOLVER_HIGHS_SPEC,))
    eval_reg = evaluator_registry or build_evaluator_registry((BUILTIN_EVALUATOR_FINANCE_SPEC,))
    catalog = stage_catalog or build_test_stage_catalog(
        predictor_registry=pred_reg,
        generator_registry=gen_reg,
    )
    resolved_policy = policy or ContainerStagePolicy(
        builtin_image="pies-builtin-stages:1.2.0",
        cpu_cores=8.0,
        memory_mb=16384,
        pids_limit=256,
        tmpfs_size_mb=512,
        stop_grace_seconds=2,
        max_timeout_seconds=172800.0,
        allowed_environment_vars=(),
    )
    backend = MockJobRunnerBackend(
        stage_root=stage_root,
        generator_registry=gen_reg,
        predictor_registry=pred_reg,
        stage_overrides=stage_overrides,
        solver_argv_override=solver_argv_override,
        evaluator_handlers=evaluator_handlers,
        on_start=on_start,
    )
    http_client = httpx.Client(
        base_url="http://job_runner.test:8090",
        transport=httpx.MockTransport(backend.handle),
    )
    client = JobRunnerClient("http://job_runner.test:8090", client=http_client)
    runner = ContainerStageRunner(
        client,
        stage_root=stage_root,
        policy=resolved_policy,
        poll_interval=poll_interval,
    )
    return ComputeExecutionPlane(
        generator_registry=gen_reg,
        predictor_registry=pred_reg,
        stage_runner=runner,
        stage_catalog=catalog,
        solver_registry=sol_reg,
        evaluator_registry=eval_reg,
        algorithm_registry=algorithm_registry,
    )


def serve_mock_job_runner(stage_root: Path):
    """Context manager exposing MockJobRunnerBackend over localhost HTTP for subprocess tests."""
    from contextlib import contextmanager
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    gen_reg = build_registry((HIGHS_MILP_PROVIDER,))
    pred_reg = build_predictor_registry((NAIVE_PREDICTOR,))
    backend = MockJobRunnerBackend(
        stage_root=stage_root,
        generator_registry=gen_reg,
        predictor_registry=pred_reg,
    )

    class _Handler(BaseHTTPRequestHandler):
        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            parsed = urlparse(self.path)
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            req = httpx.Request(
                method,
                httpx.URL(f"http://127.0.0.1{parsed.path}", params=params),
                content=body,
            )
            resp = backend.handle(req)
            self.send_response(resp.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp.content)))
            self.end_headers()
            self.wfile.write(resp.content)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return None

    @contextmanager
    def _ctx():
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    return _ctx()
