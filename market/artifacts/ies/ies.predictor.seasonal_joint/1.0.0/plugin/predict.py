"""Reference deterministic multi-target seasonal predictor (`ies.predictor.seasonal_joint@1.0.0`)."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

from pies_sdk import (
    PredictionOutputPayload,
    StageContext,
    StageIOError,
    check_relative_path,
    read_predictor_request,
)


def _read_csv_column(csv_path: Path, column_name: str) -> tuple[float, ...]:
    if not csv_path.is_file():
        raise StageIOError(f"Missing history CSV file: {csv_path}")
    lines = csv_path.read_text(encoding="utf-8-sig").splitlines()
    header_idx = next(
        (idx for idx, line in enumerate(lines) if line.strip() and not line.strip().startswith("#")),
        None,
    )
    if header_idx is None:
        raise StageIOError(f"Empty history CSV file: {csv_path}")
    rows = list(csv.reader(lines[header_idx:]))
    header = [cell.strip().lower() for cell in rows[0]]
    target_key = column_name.lower()
    if target_key not in header:
        raise StageIOError(f"History CSV {csv_path} missing column {column_name!r}")
    col_idx = header.index(target_key)
    values: list[float] = []
    for cells in rows[1:]:
        if not any(cell.strip() for cell in cells):
            continue
        val = float(cells[col_idx].strip())
        if not math.isfinite(val):
            raise StageIOError(f"Non-finite CSV value for {column_name!r}: {val!r}")
        values.append(val)
    return tuple(values)


def _extract_history(ctx: StageContext, target: dict[str, Any], interface: str) -> tuple[float, ...]:
    if "history" in target and target["history"] is not None:
        raw_hist = target["history"]
        if not isinstance(raw_hist, list) or not raw_hist:
            raise StageIOError(f"Target {interface!r} inline history must be a non-empty list")
        vals = tuple(float(v) for v in raw_hist)
    else:
        csv_rel = check_relative_path(target.get("history_csv_rel"), "targets.history_csv_rel")
        vals = _read_csv_column(ctx.work_in / csv_rel, interface)
    if not vals:
        raise StageIOError(f"Empty history series for {interface!r}")
    for val in vals:
        if not math.isfinite(val):
            raise StageIOError(f"Non-finite history value for {interface!r}: {val!r}")
    return vals


def predict(ctx: StageContext) -> None:
    """Run deterministic seasonal moving-average forecast across all requested targets."""
    request = read_predictor_request(ctx.work_in)
    point_count = int(request["point_count"])
    if point_count < 1:
        raise StageIOError(f"point_count must be >= 1, got {point_count!r}")
    seed = request.get("seed")
    raw_targets = request["targets"]
    if not isinstance(raw_targets, list) or not raw_targets:
        raise StageIOError("predictor request requires non-empty 'targets'")

    ordered_targets = sorted(
        raw_targets,
        key=lambda item: (str(item["instance"]), str(item["interface"])),
    )

    loaded: list[tuple[dict[str, Any], tuple[float, ...], float]] = []
    for target in ordered_targets:
        interface = str(target["interface"])
        history = _extract_history(ctx, target, interface)
        mean_val = round(sum(history) / len(history), 6)
        loaded.append((target, history, mean_val))

    peer_targets = [f"{item['instance']}.{item['interface']}" for item, _, _ in loaded]
    peer_means = {
        f"{item['instance']}.{item['interface']}": mean_val for item, _, mean_val in loaded
    }
    algorithm_ref = f"{ctx.manifest.plugin_id}@{ctx.manifest.version}"

    predictions: list[dict[str, Any]] = []
    artifacts: list[str] = []
    artifacts_dir = ctx.work_out / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    for target, history, mean_val in loaded:
        instance = str(target["instance"])
        interface = str(target["interface"])
        target_type = str(target["target_type"])
        history_object_id = str(target.get("history_object_id") or "")
        params = dict(target.get("parameters") or ctx.parameters or {})

        season_length = int(params.get("season_length", 24))
        seasonal_weight = float(params.get("seasonal_weight", 0.7))
        smoothing_window = int(params.get("smoothing_window", 3))
        if season_length < 1:
            raise StageIOError(f"season_length must be >= 1, got {season_length!r}")
        if not (0.0 <= seasonal_weight <= 1.0):
            raise StageIOError(f"seasonal_weight must be within [0, 1], got {seasonal_weight!r}")
        if smoothing_window < 1:
            raise StageIOError(f"smoothing_window must be >= 1, got {smoothing_window!r}")

        slots: list[float] = []
        for slot in range(season_length):
            bucket = [history[idx] for idx in range(slot, len(history), season_length)]
            slots.append(sum(bucket) / len(bucket) if bucket else mean_val)

        tail = history[-min(smoothing_window, len(history)) :]
        recent_ma = sum(tail) / len(tail)
        forecast_values = [
            round(seasonal_weight * slots[step % season_length] + (1.0 - seasonal_weight) * recent_ma, 6)
            for step in range(point_count)
        ]

        joint_summary = {
            "joint_target_count": len(loaded),
            "peer_means": peer_means,
            "peer_targets": peer_targets,
        }
        learned = {
            "method": "seasonal_moving_average_joint",
            "season_length": season_length,
            "seasonal_weight": seasonal_weight,
            "smoothing_window": smoothing_window,
            "history_mean": mean_val,
            "recent_ma": round(recent_ma, 6),
            "seasonal_profile": [round(s, 6) for s in slots],
            "joint_summary": joint_summary,
        }

        artifact_rel = f"artifacts/{instance}__{interface}.json"
        artifact_bytes = json.dumps(
            {
                "schema": "ies.prediction-artifact",
                "algorithm": algorithm_ref,
                "parameters": params,
                "seed": seed,
                "target": {"device": instance, "interface": interface, "target_type": target_type},
                "history": {"object_id": history_object_id, "points": len(history)},
                "learned": learned,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        (ctx.work_out / artifact_rel).write_bytes(artifact_bytes)
        artifacts.append(artifact_rel)

        receipt = {
            "target_type": target_type,
            "algorithm": algorithm_ref,
            "parameters": params,
            "feature_order": ["seasonal_profile", "recent_moving_average", "joint_peers"],
            "target_order": [f"{item['instance']}.{item['interface']}" for item, _, _ in loaded],
            "training_input_refs": {"history": history_object_id},
            "training_target_refs": {interface: history_object_id},
            "covariate_refs": {},
            "seed": seed,
            "joint_summary": joint_summary,
            "output_series": {
                "device": instance,
                "interface": interface,
                "mode": "data_predict",
                "point_count": point_count,
            },
        }
        predictions.append(
            {
                "instance": instance,
                "interface": interface,
                "target_type": target_type,
                "history_object_id": history_object_id,
                "values": forecast_values,
                "artifact_path": artifact_rel,
                "artifact_media_type": "application/json",
                "receipt": receipt,
            }
        )

    ctx.write_prediction(
        PredictionOutputPayload(
            predictions=predictions,
            artifacts=artifacts,
        )
    )
