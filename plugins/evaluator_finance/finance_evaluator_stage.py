"""Official built-in `ies.evaluator.finance@1.0.0` forward financial evaluator stage."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

from iesplan.assembly import ValidatedAssemblyArtifact
from iesplan.core.contracts import horizon_point_count
from iesplan.finance import (
    EffectiveFinanceConfig,
    EvaluationInput,
    evaluate_forward_finance,
)
from iesplan.finance.triplet import AmountReference
from pies_sdk import (
    ComputeResultPayload,
    EngineeringSolutionExpectation,
    EngineeringSolutionPayload,
    ExpectedConstructionUnit,
    ExpectedDeviceSeries,
    ExpectedSystemSeries,
    StageContext,
    StageIOError,
    load_safe_yaml,
    read_engineering_solution,
    read_json_file,
    validate_engineering_solution_completeness,
)

from pies_builtin_plugins.stage_io import (
    artifact_from_frozen,
    effective_finance_from_frozen,
    materialized_from_frozen,
    materialized_from_frozen_series_inputs,
)


def _read_optional_json(path: Path) -> Any | None:
    return read_json_file(path) if path.is_file() else None


def _load_artifact(
    work_in: Path, request: Mapping[str, Any]
) -> ValidatedAssemblyArtifact | Mapping[str, Any]:
    raw_artifact = request.get("artifact")
    if raw_artifact is None:
        raw_artifact = _read_optional_json(work_in / "assembly.json")
    if raw_artifact is not None:
        if not isinstance(raw_artifact, Mapping):
            raise StageIOError("evaluator_finance 'artifact' must be a JSON object")
        if "canonical_text" in raw_artifact and "receipt" in raw_artifact:
            return artifact_from_frozen(raw_artifact)
        return raw_artifact

    yaml_path = work_in / "assembly.yaml"
    if yaml_path.is_file():
        parsed = load_safe_yaml(yaml_path.read_text(encoding="utf-8"))
        if not isinstance(parsed, Mapping):
            raise StageIOError("evaluator_finance /work/in assembly.yaml must be a mapping")
        return parsed

    raise KeyError(
        "evaluator_finance /work/in missing 'artifact' in request.json, "
        "assembly.json, or assembly.yaml"
    )


def _artifact_doc(artifact: ValidatedAssemblyArtifact | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(artifact, ValidatedAssemblyArtifact):
        return json.loads(artifact.canonical_text)
    if "canonical_text" in artifact:
        return json.loads(str(artifact["canonical_text"]))
    return artifact


def _artifact_system_equations(
    artifact: ValidatedAssemblyArtifact | Mapping[str, Any],
    doc: Mapping[str, Any],
) -> Mapping[str, Any]:
    if isinstance(artifact, ValidatedAssemblyArtifact):
        return artifact.receipt.system_equations or {}
    if isinstance(artifact, Mapping) and isinstance(artifact.get("receipt"), Mapping):
        sys_eq = artifact["receipt"].get("system_equations")
        if isinstance(sys_eq, Mapping) and sys_eq:
            return sys_eq
    sys_eq = doc.get("system_equations")
    return sys_eq if isinstance(sys_eq, Mapping) else {}


def _system_variable_units(system_equations: Mapping[str, Any]) -> dict[tuple[str, str], str]:
    units: dict[tuple[str, str], str] = {}
    for block_id, block_spec in system_equations.items():
        if not isinstance(block_spec, Mapping):
            continue
        variables = block_spec.get("variables") or {}
        if not isinstance(variables, Mapping):
            continue
        for var_id, var_spec in variables.items():
            if isinstance(var_spec, Mapping) and var_spec.get("unit"):
                units[(str(block_id), str(var_id))] = str(var_spec["unit"])
    return units


def _iter_system_quantity_refs(node: object) -> Iterable[AmountReference]:
    if isinstance(node, AmountReference):
        if node.kind == "system_quantity":
            yield node
        return
    if is_dataclass(node):
        for dc_field in fields(node):
            yield from _iter_system_quantity_refs(getattr(node, dc_field.name))
        return
    if isinstance(node, Mapping):
        for val in node.values():
            yield from _iter_system_quantity_refs(val)
        return
    if isinstance(node, (tuple, list)):
        for item in node:
            yield from _iter_system_quantity_refs(item)


def _predefined_series_keys(
    materialized: Any,
    device_series: Any,
) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    if materialized is not None:
        for entry in getattr(materialized, "series", ()) or ():
            keys.add((str(entry.device), str(entry.interface)))
    if isinstance(device_series, Mapping):
        for key in device_series:
            if isinstance(key, tuple) and len(key) == 2:
                keys.add((str(key[0]), str(key[1])))
    elif isinstance(device_series, Sequence) and not isinstance(device_series, (str, bytes)):
        for entry in device_series:
            if isinstance(entry, Mapping):
                keys.add((str(entry["device"]), str(entry["interface"])))
            else:
                keys.add((str(entry.device), str(entry.interface)))
    return keys


def _device_interface_units(raw_models: Mapping[str, Any] | None) -> dict[tuple[str, str], str]:
    units: dict[tuple[str, str], str] = {}
    if not raw_models:
        return units
    for inst, contrib in raw_models.items():
        if not isinstance(contrib, Mapping):
            continue
        interfaces = contrib.get("interfaces") or {}
        if not isinstance(interfaces, Mapping):
            continue
        for iface, meta in interfaces.items():
            if isinstance(meta, Mapping) and meta.get("unit"):
                units[(str(inst), str(iface))] = str(meta["unit"])
    return units


def _validate_solution_against_frozen_context(
    solution: EngineeringSolutionPayload,
    *,
    doc: Mapping[str, Any],
    system_equations: Mapping[str, Any],
    finance: EffectiveFinanceConfig,
    evaluation: EvaluationInput,
    materialized: Any,
    device_series: Any,
    raw_models: Mapping[str, Any] | None,
) -> None:
    """Check solution completeness once at plugin boundary against frozen domain expectations."""
    baseline = doc["project_baseline"]
    expected_steps = horizon_point_count(
        str(baseline["resolution"]),
        int(baseline["start_year"]),
        evaluation.operation_years,
    )

    planning = doc.get("planning") or {}
    plan_vars = planning.get("variables") or {}
    raw_units = planning.get("construction_units") or {}
    expected_units: list[ExpectedConstructionUnit] = []
    for unit_id, unit_spec in sorted(raw_units.items()):
        raw_scales = unit_spec.get("scales") or ()
        scale_ids = list(raw_scales.keys()) if isinstance(raw_scales, Mapping) else list(raw_scales)
        pvar_units = {str(pvar): str(plan_vars[pvar]["unit"]) for pvar in scale_ids}
        expected_units.append(
            ExpectedConstructionUnit(
                unit_id=str(unit_id),
                instance_id=str(unit_spec["device_ref"]),
                mode=str(unit_spec["mode"]),
                planning_variable_units=pvar_units,
            )
        )

    predefined = _predefined_series_keys(materialized, device_series)
    iface_units = _device_interface_units(raw_models)

    required_device_series: set[tuple[str, str]] = set()
    for binding in (doc.get("tariff_bindings") or {}).values():
        required_device_series.add((str(binding["instance"]), str(binding["interface"])))
    for binding in (doc.get("demand_bindings") or {}).values():
        required_device_series.add((str(binding["instance"]), str(binding["interface"])))
    fb_instances = (doc.get("finance_binding") or {}).get("instances") or {}
    for inst, binding in fb_instances.items():
        if not isinstance(binding, Mapping) or binding.get("type") != "costed":
            continue
        for driver in (binding.get("drivers") or {}).values():
            if isinstance(driver, Mapping) and "interface" in driver:
                required_device_series.add((str(inst), str(driver["interface"])))

    actual_series = solution.series or {}
    by_device_iface = {
        (entry.instance_id, entry.variable_id): entry for entry in actual_series.values()
    }
    for inst, iface in sorted(required_device_series - predefined):
        entry = by_device_iface.get((inst, iface))
        if entry is None:
            raise StageIOError(
                f"EngineeringSolution missing required financial series {inst}.{iface}"
            )
        expected_unit = iface_units.get((inst, iface))
        if expected_unit is not None and entry.unit != expected_unit:
            raise StageIOError(
                f"EngineeringSolution series {inst}.{iface} unit mismatch: "
                f"expected {expected_unit!r}, got {entry.unit!r}"
            )

    actual_sys = solution.system_series or {}
    by_sys_key = {(entry.block, entry.variable): entry for entry in actual_sys.values()}
    sys_var_units = _system_variable_units(system_equations)
    for ref in _iter_system_quantity_refs(finance):
        if ref.block is None or ref.variable is None:
            continue
        expected_sys_unit = sys_var_units.get((ref.block, ref.variable))
        if expected_sys_unit is None:
            raise StageIOError(
                f"Frozen system_equations missing unit declaration for {ref.block}.{ref.variable}"
            )
        entry = by_sys_key.get((ref.block, ref.variable))
        if entry is None:
            raise StageIOError(
                f"EngineeringSolution missing required system_series {ref.block}.{ref.variable}"
            )
        if entry.unit != expected_sys_unit:
            raise StageIOError(
                f"EngineeringSolution system_series {ref.block}.{ref.variable} unit mismatch: "
                f"expected {expected_sys_unit!r}, got {entry.unit!r}"
            )

    exp_series = {
        sid: ExpectedDeviceSeries(
            instance_id=entry.instance_id,
            variable_id=entry.variable_id,
            unit=iface_units.get((entry.instance_id, entry.variable_id), entry.unit),
        )
        for sid, entry in actual_series.items()
    }
    exp_sys = {
        sid: ExpectedSystemSeries(
            block=entry.block,
            variable=entry.variable,
            unit=sys_var_units.get((entry.block, entry.variable), entry.unit),
        )
        for sid, entry in actual_sys.items()
    }
    validate_engineering_solution_completeness(
        solution,
        EngineeringSolutionExpectation(
            expected_steps=expected_steps,
            construction_units=tuple(expected_units),
            series=exp_series,
            system_series=exp_sys,
        ),
    )


def _load_finance(work_in: Path, request: Mapping[str, Any]) -> EffectiveFinanceConfig:
    if request.get("finance") is not None:
        return effective_finance_from_frozen(request["finance"])
    if (work_in / "finance.json").is_file():
        return effective_finance_from_frozen(read_json_file(work_in / "finance.json"))
    if (work_in / "effective_finance.yaml").is_file():
        parsed = load_safe_yaml((work_in / "effective_finance.yaml").read_text(encoding="utf-8"))
        return effective_finance_from_frozen(parsed)
    raise KeyError(
        "evaluator_finance /work/in missing 'finance' in request.json, "
        "finance.json, or effective_finance.yaml"
    )


def _load_evaluation(work_in: Path, request: Mapping[str, Any]) -> EvaluationInput:
    raw_eval = request.get("evaluation")
    if raw_eval is None:
        raw_eval = _read_optional_json(work_in / "evaluation.json")
    if raw_eval is None:
        raise KeyError(
            "evaluator_finance /work/in missing 'evaluation' in request.json or evaluation.json"
        )
    if not isinstance(raw_eval, Mapping):
        raise StageIOError("evaluator_finance 'evaluation' must be a JSON object")

    raw_eval_copy = dict(raw_eval)
    if "comparison" not in raw_eval_copy:
        raw_comp = request.get("comparison")
        if raw_comp is None:
            raw_comp = _read_optional_json(work_in / "comparison.json")
        if raw_comp is not None:
            raw_eval_copy["comparison"] = raw_comp
    return EvaluationInput.from_frozen(raw_eval_copy)


def evaluate(ctx: StageContext) -> None:
    """Evaluate `ies.engineering-solution@1.0.0` via `iesplan.finance.evaluate_forward_finance`."""
    solution_path = ctx.work_in / "solution.json"
    solution = read_engineering_solution(solution_path)
    if not solution.solver_summary.has_feasible_solution:
        raise StageIOError(
            "ies.evaluator.finance cannot evaluate non-feasible engineering solution "
            f"(math_status={solution.solver_summary.math_status!r})"
        )

    req_path = ctx.work_in / "request.json"
    request_raw = read_json_file(req_path) if req_path.is_file() else {}
    if not isinstance(request_raw, Mapping):
        raise StageIOError("evaluator_finance /work/in/request.json must be a JSON object")

    artifact = _load_artifact(ctx.work_in, request_raw)
    finance = _load_finance(ctx.work_in, request_raw)
    evaluation = _load_evaluation(ctx.work_in, request_raw)

    raw_models = request_raw.get("device_models")
    if raw_models is None:
        raw_models = _read_optional_json(ctx.work_in / "device_models.json")
    if raw_models is not None and not isinstance(raw_models, Mapping):
        raise StageIOError("evaluator_finance 'device_models' must be a JSON object")

    prediction_payload = request_raw.get("prediction")
    if prediction_payload is None:
        prediction_payload = _read_optional_json(ctx.work_in / "prediction.json")

    doc = _artifact_doc(artifact)
    resolution = str(doc["project_baseline"]["resolution"])

    materialized = None
    prices = None
    device_series = None

    raw_frozen_inputs = request_raw.get("frozen_series_inputs")
    if raw_frozen_inputs is None:
        raw_frozen_inputs = _read_optional_json(ctx.work_in / "frozen_series_inputs.json")

    if raw_frozen_inputs is not None:
        if not isinstance(raw_frozen_inputs, Mapping):
            raise StageIOError("evaluator_finance 'frozen_series_inputs' must be a JSON object")
        if not isinstance(artifact, ValidatedAssemblyArtifact):
            artifact = artifact_from_frozen(artifact)
        materialized = materialized_from_frozen_series_inputs(
            raw_frozen_inputs,
            work_in=ctx.work_in,
            artifact=artifact,
            finance=finance,
            operation_years=evaluation.operation_years,
            prediction_payload=prediction_payload,
            resolution=resolution,
        )
    else:
        raw_materialized = request_raw.get("materialized")
        if raw_materialized is None:
            raw_materialized = _read_optional_json(ctx.work_in / "sequences.json")
        if raw_materialized is not None:
            if not isinstance(raw_materialized, Mapping):
                raise StageIOError("evaluator_finance 'materialized' must be a JSON object")
            materialized = materialized_from_frozen(
                raw_materialized,
                prediction_payload=prediction_payload,
                resolution=resolution,
            )
        else:
            prices = request_raw.get("prices")
            if prices is None:
                prices = _read_optional_json(ctx.work_in / "prices.json")
            device_series = request_raw.get("device_series")
            if device_series is None:
                device_series = _read_optional_json(ctx.work_in / "device_series.json")

    _validate_solution_against_frozen_context(
        solution,
        doc=doc,
        system_equations=_artifact_system_equations(artifact, doc),
        finance=finance,
        evaluation=evaluation,
        materialized=materialized,
        device_series=device_series,
        raw_models=raw_models,
    )

    fin_eval = evaluate_forward_finance(
        solution.to_dict(),
        artifact=artifact,
        finance=finance,
        evaluation=evaluation,
        materialized=materialized,
        prices=prices,
        device_series=device_series,
        device_models=raw_models,
    )

    receipt_raw = request_raw.get("receipt")
    if receipt_raw is None:
        receipt_raw = _read_optional_json(ctx.work_in / "receipt.json")
    receipt_map = receipt_raw if isinstance(receipt_raw, Mapping) else {}

    bundle_id = str(
        request_raw.get("bundle_id")
        or receipt_map.get("bundle_id")
        or "ies.engineering-solution@1.0.0"
    )
    receipt_id = str(
        request_raw.get("receipt_id")
        or receipt_map.get("receipt_id")
        or ctx.manifest.ref
    )

    ctx.write_result(
        ComputeResultPayload(
            bundle_id=bundle_id,
            receipt_id=receipt_id,
            execution_status="completed",
            math_status=solution.solver_summary.math_status,
            outputs={"financial_evaluation": fin_eval},
        )
    )
