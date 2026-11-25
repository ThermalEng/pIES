"""F2 target `ies.generator.highs_milp@1.0.0` stage entrypoint emitting TargetSolverBundle."""

from __future__ import annotations

import json

from iesplan.computation.contracts import TargetCalculationConfig, target_manifest_to_yaml_bytes
from iesplan.computation.generators.target_bundle import generate_target_highs_bundle
from iesplan.finance import EvaluationInput
from iesplan.modeling import contribution_from_dict
from pies_sdk import StageContext

from pies_builtin_plugins.stage_io import (
    artifact_from_frozen,
    materialized_from_frozen,
    materialized_from_frozen_series_inputs,
    stream_content_to_file,
)

if __package__:
    from .generator_stage import _load_stage_inputs
else:
    from generator_stage import _load_stage_inputs


def generate_f2(ctx: StageContext) -> None:
    """Compile frozen stage inputs into `/work/out/bundle/` (`TargetSolverBundle`)."""
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
    config = TargetCalculationConfig.from_frozen(raw_config)
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

    bundle = generate_target_highs_bundle(
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

    (bundle_dir / "bundle.yaml").write_bytes(target_manifest_to_yaml_bytes(bundle.manifest))
