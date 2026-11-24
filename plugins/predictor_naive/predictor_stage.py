"""Official built-in `ies.predictor.naive@1.0.0` stage plugin entrypoint."""

from __future__ import annotations

from typing import Any

from iesplan.computation.predictors import NAIVE_PREDICTOR
from pies_sdk import (
    PredictionOutputPayload,
    StageContext,
    check_relative_path,
    read_predictor_request,
)

from pies_builtin_plugins.stage_io import project_csv_column


def predict(ctx: StageContext) -> None:
    """Run deterministic naive carry-forward prediction from `/work/in/request.json`."""
    request = read_predictor_request(ctx.work_in)
    point_count = int(request["point_count"])
    seed = request.get("seed")
    previous = request.get("previous")
    raw_targets = request["targets"]

    ordered_targets = sorted(
        raw_targets,
        key=lambda item: (str(item["instance"]), str(item["interface"])),
    )

    predictions: list[dict[str, Any]] = []
    artifacts: list[str] = []
    artifacts_dir = ctx.work_out / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    for target in ordered_targets:
        instance = str(target["instance"])
        interface = str(target["interface"])
        target_type = str(target["target_type"])
        history_object_id = str(target.get("history_object_id") or "")
        parameters = dict(target.get("parameters") or ctx.parameters or {})

        if "history" in target and target["history"] is not None:
            history = tuple(float(v) for v in target["history"])
        else:
            csv_rel = check_relative_path(target["history_csv_rel"], "targets.history_csv_rel")
            history = project_csv_column(ctx.work_in / csv_rel, interface)

        predicted = NAIVE_PREDICTOR.predict(
            device=instance,
            interface=interface,
            target_type=target_type,
            history=history,
            history_object_id=history_object_id,
            parameters=parameters,
            seed=seed,
            point_count=point_count,
            previous=previous,
        )

        artifact_rel = f"artifacts/{instance}__{interface}.json"
        (ctx.work_out / artifact_rel).write_bytes(predicted.artifact)
        artifacts.append(artifact_rel)

        predictions.append(
            {
                "instance": predicted.device,
                "interface": predicted.interface,
                "target_type": predicted.target_type,
                "history_object_id": predicted.history_object_id,
                "values": list(predicted.values),
                "artifact_path": artifact_rel,
                "artifact_media_type": predicted.artifact_media_type,
                "receipt": dict(predicted.receipt),
            }
        )

    ctx.write_prediction(
        PredictionOutputPayload(
            predictions=predictions,
            artifacts=artifacts,
        )
    )
