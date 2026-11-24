"""Equivalence and path-safety tests for `frozen_series_inputs` direct object reading."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from iesplan.assembly import validate_assembly_text
from iesplan.computation import (
    HIGHS_ADAPTER,
    HIGHS_MILP_PROVIDER,
    NAIVE_PREDICTOR,
    NAIVE_PREDICTOR_REF,
    CalculationConfig,
    MaterializedPriceSeries,
    MaterializedSeries,
    MaterializedSet,
    materialize_non_predictive_series,
    materialize_predicted_series,
)
from iesplan.computation.bundle import export_bundle_files
from iesplan.computation.generators.highs_milp import GENERATOR_REF, SOLVER_REF
from iesplan.core.content import read_content
from iesplan.devices import init_registry, list_devices
from iesplan.finance import (
    EvaluationInput,
    PriceSeriesMeta,
    effective_from_yaml,
    emit_price_series_csv,
    expand_monthly_daily_prices,
    read_trusted_price_values,
)
from iesplan.modeling import build_math_contribution, contribution_to_dict
from pies_sdk import (
    StageIOError,
    dump_safe_yaml,
    load_safe_yaml,
    validate_solver_bundle_dir,
    validate_stage_outputs,
    write_json_file,
)
from pies_sdk.runner import run_plugin

from pies_builtin_plugins.stage_io import (
    complete_materialized,
    safe_work_in_subpath,
)

PLUGINS_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PLUGINS_ROOT.parent
IESPLAN_SAMPLES = REPO_ROOT / "backend" / "iesplan" / "assembly" / "samples"
BUILTIN_STAGE_IMAGE = "pies-builtin-stages:1.1.5"


def _collect_resources(doc: dict[str, Any], samples_dir: Path) -> dict[str, bytes]:
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            content = node.get("content")
            if isinstance(content, dict) and "object_id" in content:
                found.add(str(content["object_id"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(doc.get("devices") or {})
    resources: dict[str, bytes] = {}
    for oid in sorted(found):
        rel = oid[5:] if oid.startswith("file:") else oid
        resources[oid] = (samples_dir / rel).read_bytes()
    return resources


def _load_campus_inputs(*, assembly_text: str | None = None, samples_dir: Path = IESPLAN_SAMPLES):
    init_registry()
    text = assembly_text or (samples_dir / "valid" / "campus.assembly.yaml").read_text(
        encoding="utf-8"
    )
    validated = validate_assembly_text(text, package_dir=samples_dir)
    assert validated.ok, [(d.code, d.params) for d in validated.diagnostics if d.blocking]
    artifact = validated.artifact

    contributions = {}
    for dev in list_devices():
        built = build_math_contribution(dev)
        assert built.ok, (dev.device.id, built.diagnostics)
        contributions[dev.device.id] = built.contribution

    doc = json.loads(artifact.canonical_text)
    models = {
        inst: contributions[dev["definition"]["id"]] for inst, dev in doc["devices"].items()
    }
    finance = effective_from_yaml(
        (samples_dir / "finance" / "campus.effective-finance.yaml").read_text(
            encoding="utf-8"
        )
    )
    resources = _collect_resources(doc, samples_dir)
    evaluation = EvaluationInput(
        evaluation_years=1,
        construction_period=0,
        operation_years=1,
        discount_rate=0.08,
        construction_payments=(),
        maintenance_expenses=(),
    )
    return artifact, models, resources, finance, evaluation


def _evaluation_to_dict(ev: EvaluationInput) -> dict[str, Any]:
    return {
        "evaluation_years": ev.evaluation_years,
        "construction_period": ev.construction_period,
        "operation_years": ev.operation_years,
        "discount_rate": ev.discount_rate,
        "construction_payments": [
            {"t": p.t, "ratio": p.ratio} for p in ev.construction_payments
        ],
        "maintenance_expenses": [],
        "comparison": ev.comparison.to_dict(),
    }


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "image", "inspect", BUILTIN_STAGE_IMAGE],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0


def _host_bind_path(container_path: Path) -> Path:
    c_root = os.environ.get("PIES_TEST_CONTAINER_ROOT")
    h_root = os.environ.get("PIES_TEST_HOST_ROOT")
    if c_root and h_root:
        return Path(h_root) / container_path.relative_to(c_root)
    return container_path


def _stage_hardlinked_objects(
    work_in: Path,
    staging_dir: Path,
    raw_objects: dict[str, bytes],
) -> dict[str, str]:
    staging_dir.mkdir(parents=True, exist_ok=True)
    objects_dir = work_in / "objects"
    objects_dir.mkdir(parents=True, exist_ok=True)
    fixed_resources: dict[str, str] = {}
    for idx, (oid, raw_bytes) in enumerate(sorted(raw_objects.items())):
        src = staging_dir / f"obj_{idx}.dat"
        src.write_bytes(raw_bytes)
        src.chmod(0o644)
        dest_name = f"obj_{idx}_{Path(oid).name}"
        dest = objects_dir / dest_name
        os.link(src, dest)
        fixed_resources[oid] = f"objects/{dest_name}"
    return fixed_resources


def _campus_with_tou_and_predict(
    tmp_path: Path,
) -> tuple[Any, dict[str, Any], dict[str, bytes], Any, Any, bytes]:
    samples_copy = tmp_path / "samples"
    shutil.copytree(IESPLAN_SAMPLES, samples_copy)

    load_csv = samples_copy / "data" / "campus_load.data.csv"
    load_csv.write_text(
        load_csv.read_text(encoding="utf-8").replace(
            "# source_mode: data_repeat", "# source_mode: data_predict", 1
        ),
        encoding="utf-8",
    )

    source_text = (samples_copy / "valid" / "campus.assembly.yaml").read_text(encoding="utf-8")
    updated_text = source_text.replace(
        "      electricity_demand:\n"
        "        mode: data_repeat\n"
        "        path: data/campus_load.data.csv",
        "      electricity_demand:\n"
        "        mode: data_predict\n"
        "        path: data/campus_load.data.csv\n"
        "        target_type: load",
        1,
    ).replace(
        "  import_meter:\n    price: grid_import\n",
        "  import_meter:\n    price: grid_import_tou\n",
        1,
    )
    assert "mode: data_predict" in updated_text
    assert "price: grid_import_tou" in updated_text

    artifact, models, resources, finance, evaluation = _load_campus_inputs(
        assembly_text=updated_text, samples_dir=samples_copy
    )
    tou_values = tuple(Decimal("0.65") if i % 24 < 12 else Decimal("0.95") for i in range(8760))
    tou_csv = emit_price_series_csv(
        PriceSeriesMeta(resolution="1h", unit="CNY/kWh"),
        tou_values,
    )
    return artifact, models, resources, finance, evaluation, tou_csv


def test_frozen_series_inputs_docker_equivalence(tmp_path: Path) -> None:
    """Hardlinked `/work/in/objects/` inside `--network=none --read-only` matches in-process."""
    force_docker = os.environ.get("PIES_RUN_DOCKER_TESTS") == "1"
    if not _docker_available():
        if force_docker:
            pytest.fail(f"Docker daemon or image {BUILTIN_STAGE_IMAGE!r} unavailable")
        pytest.skip(f"Docker daemon or image {BUILTIN_STAGE_IMAGE!r} unavailable")

    artifact, models, resources, finance, evaluation, tou_csv = _campus_with_tou_and_predict(tmp_path)
    tou_price = finance.energy_prices["grid_import_tou"]
    assert tou_price.ref is not None and tou_price.series_meta is not None

    pred_entry = {
        "target": {"instance": "elec_load", "interface": "electricity_demand"},
        "algorithm": NAIVE_PREDICTOR_REF,
        "parameters": {},
    }
    config = CalculationConfig(
        mode="planning",
        predictions=(pred_entry,),
        generator=GENERATOR_REF,
        solver=SOLVER_REF,
        evaluator=HIGHS_ADAPTER.ref,
        precision="medium",
        time_limit_seconds=600,
        outputs=("solution", "metrics"),
        seed=42,
    )

    # 1) Direct in-process materialization + generation
    base_mat = materialize_non_predictive_series(artifact, resources, operation_years=1)
    price_series = (
        MaterializedPriceSeries(
            price_id="grid_import_tou",
            point_count=8760,
            resolution=tou_price.series_meta.resolution,
            unit=tou_price.series_meta.unit,
            values=read_trusted_price_values(tou_csv),
            source={"kind": "price_series", "object_id": tou_price.ref},
        ),
    )
    base_with_prices = MaterializedSet(
        operation_years=base_mat.operation_years,
        series=base_mat.series,
        pending_predict=base_mat.pending_predict,
        prices=price_series,
    )
    predicted_targets = materialize_predicted_series(
        artifact,
        resources,
        config.predictions,
        lambda ref: NAIVE_PREDICTOR if ref == NAIVE_PREDICTOR_REF else None,  # type: ignore[return-value]
        config.seed,
        operation_years=1,
    )
    pred_target = predicted_targets[0]
    predicted_mat_series = (
        MaterializedSeries(
            device=pred_target.device,
            interface=pred_target.interface,
            mode="data_predict",
            resolution="1h",
            point_count=len(pred_target.values),
            values=pred_target.values,
            source={"kind": "data_predict", "object_id": pred_target.history_object_id},
        ),
    )
    direct_mat = complete_materialized(base_with_prices, predicted_mat_series)
    direct_bundle = HIGHS_MILP_PROVIDER.generate(
        artifact, models, direct_mat, finance, config, evaluation
    )

    # 2) Stage hardlinked raw CSV + price objects under /work/in/objects/ (no prediction.json)
    gen_in = tmp_path / "docker_gen_in"
    gen_out = tmp_path / "docker_gen_out"
    staging = tmp_path / "object_store"
    gen_in.mkdir()
    gen_out.mkdir()

    all_objects = dict(resources)
    all_objects[tou_price.ref] = tou_csv
    fixed_resources = _stage_hardlinked_objects(gen_in, staging, all_objects)

    req_path = write_json_file(
        gen_in / "request.json",
        {
            "artifact": artifact.to_dict(),
            "device_models": {inst: contribution_to_dict(c) for inst, c in models.items()},
            "frozen_series_inputs": {
                "fixed_resources": fixed_resources,
                "predicted_series": [item.to_dict() for item in predicted_mat_series],
            },
            "finance": finance.to_dict(),
            "calculation_config": config.to_dict(),
            "evaluation": _evaluation_to_dict(evaluation),
        },
    )
    req_path.chmod(0o644)
    (gen_in / "objects").chmod(0o755)
    gen_in.chmod(0o755)
    gen_out.chmod(0o777)

    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "-v",
            f"{_host_bind_path(gen_in)}:/work/in:ro",
            "-v",
            f"{_host_bind_path(gen_out)}:/work/out:rw",
            BUILTIN_STAGE_IMAGE,
            "python",
            "-m",
            "pies_sdk.runner",
            "--manifest",
            "/app/plugins/generator_highs_milp/plugin.yaml",
            "--work-in",
            "/work/in",
            "--work-out",
            "/work/out",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"

    validate_stage_outputs("generator", gen_out)
    validate_solver_bundle_dir(gen_out / "bundle")

    expected_bundle_files = export_bundle_files(direct_bundle)
    for rel_path, content in expected_bundle_files.items():
        actual = (gen_out / "bundle" / rel_path).read_bytes()
        assert actual == read_content(content), f"Bundle mismatch in {rel_path}"


def test_frozen_series_inputs_with_mounted_prediction_json(tmp_path: Path) -> None:
    """`frozen_series_inputs` reuses mounted `/work/in/prediction.json` over empty `predicted_series`."""
    artifact, models, resources, finance, evaluation, tou_csv = _campus_with_tou_and_predict(tmp_path)
    tou_price = finance.energy_prices["grid_import_tou"]
    assert tou_price.ref is not None and tou_price.series_meta is not None

    pred_entry = {
        "target": {"instance": "elec_load", "interface": "electricity_demand"},
        "algorithm": NAIVE_PREDICTOR_REF,
        "parameters": {},
    }
    config = CalculationConfig(
        mode="planning",
        predictions=(pred_entry,),
        generator=GENERATOR_REF,
        solver=SOLVER_REF,
        evaluator=HIGHS_ADAPTER.ref,
        precision="medium",
        time_limit_seconds=600,
        outputs=("solution", "metrics"),
        seed=42,
    )

    base_mat = materialize_non_predictive_series(artifact, resources, operation_years=1)
    pending = base_mat.pending_predict[0]
    predicted_targets = materialize_predicted_series(
        artifact,
        resources,
        config.predictions,
        lambda ref: NAIVE_PREDICTOR if ref == NAIVE_PREDICTOR_REF else None,  # type: ignore[return-value]
        config.seed,
        operation_years=1,
    )
    pred_target = predicted_targets[0]
    direct_mat = complete_materialized(
        MaterializedSet(
            operation_years=1,
            series=base_mat.series,
            pending_predict=base_mat.pending_predict,
            prices=(
                MaterializedPriceSeries(
                    price_id="grid_import_tou",
                    point_count=8760,
                    resolution=tou_price.series_meta.resolution,
                    unit=tou_price.series_meta.unit,
                    values=read_trusted_price_values(tou_csv),
                    source={"kind": "price_series", "object_id": tou_price.ref},
                ),
            ),
        ),
        (
            MaterializedSeries(
                device=pred_target.device,
                interface=pred_target.interface,
                mode="data_predict",
                resolution="1h",
                point_count=len(pred_target.values),
                values=pred_target.values,
                source={"kind": "data_predict", "object_id": pred_target.history_object_id},
            ),
        ),
    )
    direct_bundle = HIGHS_MILP_PROVIDER.generate(
        artifact, models, direct_mat, finance, config, evaluation
    )

    pred_in = tmp_path / "pred_in"
    pred_out = tmp_path / "pred_out"
    (pred_in / "resources").mkdir(parents=True)
    (pred_in / "resources" / "load.csv").write_bytes(resources[str(pending["object_id"])])
    write_json_file(
        pred_in / "request.json",
        {
            "point_count": 8760,
            "seed": config.seed,
            "targets": [
                {
                    "instance": "elec_load",
                    "interface": "electricity_demand",
                    "target_type": str(pending["target_type"]),
                    "history_object_id": str(pending["object_id"]),
                    "history_csv_rel": "resources/load.csv",
                    "parameters": {},
                }
            ],
        },
    )
    run_plugin(
        manifest_path=PLUGINS_ROOT / "predictor_naive" / "plugin.yaml",
        work_in=pred_in,
        work_out=pred_out,
    )

    gen_in = tmp_path / "gen_in"
    gen_out = tmp_path / "gen_out"
    gen_in.mkdir()
    all_objects = dict(resources)
    all_objects[tou_price.ref] = tou_csv
    fixed_resources = _stage_hardlinked_objects(gen_in, tmp_path / "obj_store", all_objects)
    (gen_in / "prediction.json").symlink_to(pred_out / "prediction.json")
    write_json_file(
        gen_in / "request.json",
        {
            "artifact": artifact.to_dict(),
            "device_models": {inst: contribution_to_dict(c) for inst, c in models.items()},
            "frozen_series_inputs": {
                "fixed_resources": fixed_resources,
                "predicted_series": [],
            },
            "finance": finance.to_dict(),
            "calculation_config": config.to_dict(),
            "evaluation": _evaluation_to_dict(evaluation),
        },
    )
    run_plugin(
        manifest_path=PLUGINS_ROOT / "generator_highs_milp" / "plugin.yaml",
        work_in=gen_in,
        work_out=gen_out,
    )

    for rel_path, content in export_bundle_files(direct_bundle).items():
        assert (gen_out / "bundle" / rel_path).read_bytes() == read_content(content)


def test_frozen_series_inputs_monthly_daily_price_equivalence(tmp_path: Path) -> None:
    """`frozen_series_inputs` expands `monthly_daily` prices identically to in-process worker."""
    samples_copy = tmp_path / "samples"
    shutil.copytree(IESPLAN_SAMPLES, samples_copy)
    month_oids = {str(m): f"{m:064x}" for m in range(1, 13)}
    finance_path = samples_copy / "finance" / "campus.effective-finance.yaml"
    finance_yaml = load_safe_yaml(finance_path.read_text(encoding="utf-8"))
    finance_yaml["energy_prices"]["grid_import_monthly"] = {
        "carrier": "electricity",
        "direction": "purchase",
        "kind": "monthly_daily",
        "months": {
            m: {
                "ref": oid,
                "series_meta": {"resolution": "1h", "point_count": 24, "unit": "CNY/kWh"},
            }
            for m, oid in month_oids.items()
        },
    }
    finance_path.write_text(dump_safe_yaml(finance_yaml), encoding="utf-8")

    assembly_text = (samples_copy / "valid" / "campus.assembly.yaml").read_text(encoding="utf-8")
    assembly_text = assembly_text.replace(
        "  import_meter:\n    price: grid_import\n",
        "  import_meter:\n    price: grid_import_monthly\n",
        1,
    )
    artifact, models, resources, finance, evaluation = _load_campus_inputs(
        assembly_text=assembly_text, samples_dir=samples_copy
    )
    finance_dict = finance.to_dict()
    month_csvs = {
        oid: emit_price_series_csv(
            PriceSeriesMeta(resolution="1h", unit="CNY/kWh"),
            tuple(Decimal(f"0.{50 + int(m):02d}") for _ in range(24)),
        )
        for m, oid in month_oids.items()
    }
    curves = {int(m): read_trusted_price_values(month_csvs[oid]) for m, oid in month_oids.items()}
    expanded = expand_monthly_daily_prices(curves, resolution="1h", start_year=2026, operation_years=1)
    config = CalculationConfig(
        mode="planning",
        generator=GENERATOR_REF,
        solver=SOLVER_REF,
        evaluator=HIGHS_ADAPTER.ref,
        precision="medium",
        time_limit_seconds=600,
        outputs=("solution", "metrics"),
        seed=0,
    )

    base_mat = materialize_non_predictive_series(artifact, resources, operation_years=1)
    direct_mat = MaterializedSet(
        operation_years=1,
        series=base_mat.series,
        pending_predict=(),
        prices=(
            MaterializedPriceSeries(
                price_id="grid_import_monthly",
                point_count=len(expanded),
                resolution="1h",
                unit="CNY/kWh",
                values=expanded,
                source={"kind": "monthly_daily", "months": month_oids},
            ),
        ),
    )
    direct_bundle = HIGHS_MILP_PROVIDER.generate(
        artifact, models, direct_mat, finance, config, evaluation
    )

    gen_in = tmp_path / "gen_in"
    gen_out = tmp_path / "gen_out"
    gen_in.mkdir()
    all_objects = {**resources, **month_csvs}
    fixed_resources = _stage_hardlinked_objects(gen_in, tmp_path / "obj_store", all_objects)
    write_json_file(
        gen_in / "request.json",
        {
            "artifact": artifact.to_dict(),
            "device_models": {inst: contribution_to_dict(c) for inst, c in models.items()},
            "frozen_series_inputs": {
                "fixed_resources": fixed_resources,
                "predicted_series": [],
            },
            "finance": finance_dict,
            "calculation_config": config.to_dict(),
            "evaluation": _evaluation_to_dict(evaluation),
        },
    )
    run_plugin(
        manifest_path=PLUGINS_ROOT / "generator_highs_milp" / "plugin.yaml",
        work_in=gen_in,
        work_out=gen_out,
    )
    for rel_path, content in export_bundle_files(direct_bundle).items():
        assert (gen_out / "bundle" / rel_path).read_bytes() == read_content(content)


@pytest.mark.parametrize(
    "bad_rel",
    ["", ".", "../secret", "/etc/passwd", "objects/../../etc/passwd", "objects\\foo"],
)
def test_frozen_series_inputs_rejects_unsafe_relative_subpath(tmp_path: Path, bad_rel: str) -> None:
    """`safe_work_in_subpath` rejects path traversal, empty, or absolute paths."""
    with pytest.raises(StageIOError):
        safe_work_in_subpath(tmp_path, bad_rel, "fixed_resources['obj']")


def test_frozen_series_inputs_missing_object_fails_visibly(tmp_path: Path) -> None:
    """Missing `fixed_resources` object fails explicitly without fallback."""
    artifact, models, _resources, finance, evaluation = _load_campus_inputs()
    config = CalculationConfig(
        mode="planning",
        generator=GENERATOR_REF,
        solver=SOLVER_REF,
        evaluator=HIGHS_ADAPTER.ref,
        precision="medium",
        time_limit_seconds=600,
        outputs=("solution", "metrics"),
        seed=0,
    )
    gen_in = tmp_path / "gen_in"
    gen_out = tmp_path / "gen_out"
    gen_in.mkdir()
    write_json_file(
        gen_in / "request.json",
        {
            "artifact": artifact.to_dict(),
            "device_models": {inst: contribution_to_dict(c) for inst, c in models.items()},
            "frozen_series_inputs": {
                "fixed_resources": {},
                "predicted_series": [],
            },
            "finance": finance.to_dict(),
            "calculation_config": config.to_dict(),
            "evaluation": _evaluation_to_dict(evaluation),
        },
    )
    with pytest.raises(ValueError, match="固定资源缺失"):
        run_plugin(
            manifest_path=PLUGINS_ROOT / "generator_highs_milp" / "plugin.yaml",
            work_in=gen_in,
            work_out=gen_out,
        )


def test_complete_materialized_preserves_prices_and_clears_pending() -> None:
    """`complete_materialized` appends predicted series, preserves prices, and clears pending."""
    price = MaterializedPriceSeries(
        price_id="p1",
        point_count=2,
        resolution="1h",
        unit="CNY/kWh",
        values=(0.5, 0.6),
        source={"kind": "price_series", "object_id": "oid-p1"},
    )
    base = MaterializedSet(
        operation_years=1,
        series=(),
        pending_predict=(
            {"device": "d1", "interface": "i1", "target_type": "load", "object_id": "oid-1"},
        ),
        prices=(price,),
    )
    pred = MaterializedSeries(
        device="d1",
        interface="i1",
        mode="data_predict",
        resolution="1h",
        point_count=2,
        values=(1.0, 2.0),
        source={"kind": "data_predict", "object_id": "oid-1"},
    )
    completed = complete_materialized(base, (pred,))
    assert completed.operation_years == 1
    assert completed.series == (pred,)
    assert completed.pending_predict == ()
    assert completed.prices == (price,)
    assert json.loads(json.dumps(completed.to_dict()))["prices"][0]["price_id"] == "p1"
