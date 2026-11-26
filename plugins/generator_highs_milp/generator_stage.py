"""Official built-in `ies.generator.highs_milp@1.0.0` stage plugin entrypoint."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from iesplan.computation.bundle import manifest_to_yaml_bytes
from iesplan.computation.generators.highs_milp import HIGHS_MILP_PROVIDER
from iesplan.computation.protocols import CalculationConfig
from iesplan.finance import EffectiveFinanceConfig, EvaluationInput
from iesplan.modeling import contribution_from_dict
from pies_sdk import StageContext, load_safe_yaml, read_json_file

from pies_builtin_plugins.stage_io import (
    artifact_from_frozen,
    effective_finance_from_frozen,
    materialized_from_frozen,
    materialized_from_frozen_series_inputs,
    stream_content_to_file,
)


def _read_optional_json(path: Path) -> Any | None:
    return read_json_file(path) if path.is_file() else None


def _load_stage_inputs(work_in: Path) -> tuple[
    Mapping[str, Any],
    Mapping[str, Any],
    Mapping[str, Any] | None,
    Mapping[str, Any] | None,
    EffectiveFinanceConfig,
    Mapping[str, Any],
    Mapping[str, Any],
    Mapping[str, Any] | None,
]:
    req_path = work_in / "request.json"
    req: Mapping[str, Any] = read_json_file(req_path) if req_path.is_file() else {}

    raw_artifact = req.get("artifact") or _read_optional_json(work_in / "assembly.json")
    if raw_artifact is None:
        raise KeyError("generator /work/in missing 'artifact' in request.json or assembly.json")

    raw_models = req.get("device_models") or _read_optional_json(work_in / "device_models.json")
    if raw_models is None:
        raise KeyError("generator /work/in missing 'device_models' in request.json or device_models.json")

    raw_frozen_inputs = req.get("frozen_series_inputs")
    raw_materialized: Mapping[str, Any] | None = None
    if raw_frozen_inputs is None:
        raw_materialized = req.get("materialized") or _read_optional_json(work_in / "sequences.json")
        if raw_materialized is None:
            raise KeyError(
                "generator /work/in missing 'frozen_series_inputs' or 'materialized' "
                "in request.json or sequences.json"
            )
    elif not isinstance(raw_frozen_inputs, Mapping):
        raise TypeError("generator /work/in 'frozen_series_inputs' must be a JSON object")

    if "finance" in req:
        finance = effective_finance_from_frozen(req["finance"])
    elif (work_in / "finance.json").is_file():
        finance = effective_finance_from_frozen(read_json_file(work_in / "finance.json"))
    elif (work_in / "effective_finance.yaml").is_file():
        parsed_finance = load_safe_yaml(
            (work_in / "effective_finance.yaml").read_text(encoding="utf-8")
        )
        finance = effective_finance_from_frozen(parsed_finance)
    else:
        raise KeyError(
            "generator /work/in missing 'finance' in request.json, "
            "finance.json, or effective_finance.yaml"
        )

    raw_config = req.get("calculation_config") or _read_optional_json(work_in / "calculation_config.json")
    if raw_config is None:
        raise KeyError(
            "generator /work/in missing 'calculation_config' in request.json or calculation_config.json"
        )

    raw_evaluation = req.get("evaluation") or _read_optional_json(work_in / "evaluation.json")
    if raw_evaluation is None:
        raise KeyError("generator /work/in missing 'evaluation' in request.json or evaluation.json")

    prediction_payload: Mapping[str, Any] | None = req.get("prediction")
    if prediction_payload is None and (work_in / "prediction.json").is_file():
        prediction_payload = read_json_file(work_in / "prediction.json")

    return (
        raw_artifact,
        raw_models,
        raw_materialized,
        raw_frozen_inputs,
        finance,
        raw_config,
        raw_evaluation,
        prediction_payload,
    )


def generate(ctx: StageContext) -> None:
    """Compile frozen stage inputs into `/work/out/bundle/` (`ies.solver-bundle@1.0.0`)."""
    (
        raw_artifact,
        raw_models,
        raw_materialized,
        raw_frozen_inputs,
        finance,
        raw_config,
        raw_evaluation,
        prediction_payload,
    ) = _load_stage_inputs(ctx.work_in)

    artifact = artifact_from_frozen(raw_artifact)
    models = {inst: contribution_from_dict(contrib) for inst, contrib in raw_models.items()}
    if ctx.parameters:
        merged_config = dict(raw_config)
        merged_options = dict(merged_config.get("options") or {})
        merged_options["generator"] = dict(ctx.parameters)
        merged_config["options"] = merged_options
        config = CalculationConfig.from_frozen(merged_config)
    else:
        config = CalculationConfig.from_frozen(raw_config)
    evaluation = EvaluationInput.from_frozen(raw_evaluation)
    resolution = str(json.loads(artifact.canonical_text)["project_baseline"]["resolution"])

    if raw_frozen_inputs is not None:
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
        assert raw_materialized is not None
        materialized = materialized_from_frozen(
            raw_materialized,
            prediction_payload=prediction_payload,
            resolution=resolution,
        )

    bundle = HIGHS_MILP_PROVIDER.generate(
        artifact,
        models,
        materialized,
        finance,
        config,
        evaluation,
    )

    bundle_dir = ctx.work_out / "bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    for rel_path in sorted(bundle.files):
        stream_content_to_file(bundle.files[rel_path], bundle_dir / rel_path)

    (bundle_dir / "bundle.yaml").write_bytes(manifest_to_yaml_bytes(bundle.manifest))
