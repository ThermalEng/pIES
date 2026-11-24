"""Canonical stage I/O contracts for pIES computation plugins."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from pies_sdk.errors import StageIOError

COMPUTE_RESULT_SCHEMA = "ies.compute-result"
COMPUTE_RESULT_VERSION = "1.0.0"

PREDICTION_OUTPUT_SCHEMA = "ies.prediction-output"
PREDICTION_OUTPUT_VERSION = "1.0.0"

SOLVER_BUNDLE_SCHEMA = "ies.solver-bundle"
SOLVER_BUNDLE_VERSION = "1.0.0"

EXECUTION_STATUSES = (
    "completed",
    "rejected",
    "failed",
    "timed_out",
    "cancelled",
    "fenced",
    "resource_exhausted",
    "protocol_failed",
)

MATH_STATUSES = (
    "solved",
    "feasible",
    "infeasible",
    "unbounded",
    "numeric_failure",
    "not_solved",
)


def check_finite_json(value: object, name: str = "value") -> None:
    """Recursively reject non-finite floats (NaN, Infinity, -Infinity)."""
    if isinstance(value, float) and not math.isfinite(value):
        raise StageIOError(f"{name} forbids non-finite numeric value: {value!r}")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise StageIOError(f"{name} keys must be strings, got {type(key).__name__}")
            check_finite_json(item, f"{name}.{key}")
    elif isinstance(value, (list, tuple)):
        for idx, item in enumerate(value):
            check_finite_json(item, f"{name}[{idx}]")
    elif value is not None and not isinstance(value, (str, int, bool, float)):
        raise StageIOError(f"{name} contains non-JSON type: {type(value).__name__}")


def check_relative_path(path: object, name: str = "path") -> str:
    """Validate a safe relative path without traversal or drive/root prefixes."""
    if not isinstance(path, str) or not path:
        raise StageIOError(f"{name} must be a non-empty relative path: {path!r}")
    if "\\" in path or path.startswith("/") or ".." in path.split("/"):
        raise StageIOError(f"{name} escapes allowed directory: {path!r}")
    return path


def _reject_json_constant(token: str) -> NoReturn:
    raise StageIOError(f"Non-finite JSON constant is forbidden: {token!r}")


def read_json_file(path: Path) -> Any:
    """Read UTF-8 JSON file, rejecting non-finite constants and malformed JSON."""
    if not path.is_file():
        raise StageIOError(f"Required JSON file does not exist: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw, parse_constant=_reject_json_constant)
    except StageIOError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StageIOError(f"Failed to read JSON file {path}: {exc}") from exc
    check_finite_json(data, path.name)
    return data


def write_json_file(path: Path, data: Any) -> Path:
    """Write UTF-8 deterministic JSON file, rejecting non-finite floats."""
    check_finite_json(data, path.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


@dataclass(frozen=True, slots=True)
class ComputeResultPayload:
    """Canonical /work/out/result.json contract for algorithm and evaluator plugins."""

    bundle_id: str
    receipt_id: str
    execution_status: str
    math_status: str
    outputs: Mapping[str, Any] = field(default_factory=dict)
    schema: str = COMPUTE_RESULT_SCHEMA
    schema_version: str = COMPUTE_RESULT_VERSION

    def __post_init__(self) -> None:
        if self.schema != COMPUTE_RESULT_SCHEMA:
            raise StageIOError(f"Invalid ComputeResult schema: {self.schema!r}")
        if self.schema_version != COMPUTE_RESULT_VERSION:
            raise StageIOError(f"Invalid ComputeResult schema_version: {self.schema_version!r}")
        if not isinstance(self.bundle_id, str) or not self.bundle_id:
            raise StageIOError("ComputeResult requires non-empty 'bundle_id'")
        if not isinstance(self.receipt_id, str) or not self.receipt_id:
            raise StageIOError("ComputeResult requires non-empty 'receipt_id'")
        if self.execution_status not in EXECUTION_STATUSES:
            raise StageIOError(f"Invalid execution_status: {self.execution_status!r}")
        if self.math_status not in MATH_STATUSES:
            raise StageIOError(f"Invalid math_status: {self.math_status!r}")
        if not isinstance(self.outputs, Mapping):
            raise StageIOError("ComputeResult 'outputs' must be a mapping")
        check_finite_json(self.outputs, "outputs")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "bundle_id": self.bundle_id,
            "receipt_id": self.receipt_id,
            "execution_status": self.execution_status,
            "math_status": self.math_status,
            "outputs": dict(self.outputs),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ComputeResultPayload:
        if not isinstance(data, Mapping):
            raise StageIOError("ComputeResult payload must be a JSON object")
        expected_keys = {
            "schema",
            "schema_version",
            "bundle_id",
            "receipt_id",
            "execution_status",
            "math_status",
            "outputs",
        }
        extra = sorted(k for k in data if k not in expected_keys)
        if extra:
            raise StageIOError(f"ComputeResult contains unknown fields: {extra!r}")
        missing = sorted(k for k in expected_keys if k not in data)
        if missing:
            raise StageIOError(f"ComputeResult missing required fields: {missing!r}")
        return cls(
            schema=data["schema"],
            schema_version=data["schema_version"],
            bundle_id=data["bundle_id"],
            receipt_id=data["receipt_id"],
            execution_status=data["execution_status"],
            math_status=data["math_status"],
            outputs=data["outputs"],
        )


@dataclass(frozen=True, slots=True)
class PredictionOutputPayload:
    """Canonical /work/out/prediction.json contract for predictor plugins."""

    predictions: list[dict[str, Any]]
    artifacts: list[str] = field(default_factory=list)
    schema: str = PREDICTION_OUTPUT_SCHEMA
    schema_version: str = PREDICTION_OUTPUT_VERSION

    def __post_init__(self) -> None:
        if self.schema != PREDICTION_OUTPUT_SCHEMA:
            raise StageIOError(f"Invalid PredictionOutput schema: {self.schema!r}")
        if self.schema_version != PREDICTION_OUTPUT_VERSION:
            raise StageIOError(f"Invalid PredictionOutput schema_version: {self.schema_version!r}")
        if not isinstance(self.predictions, list):
            raise StageIOError("PredictionOutput 'predictions' must be a list")
        check_finite_json(self.predictions, "predictions")
        if not isinstance(self.artifacts, list):
            raise StageIOError("PredictionOutput 'artifacts' must be a list")
        for idx, rel in enumerate(self.artifacts):
            check_relative_path(rel, f"artifacts[{idx}]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "predictions": [dict(item) for item in self.predictions],
            "artifacts": list(self.artifacts),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PredictionOutputPayload:
        if not isinstance(data, Mapping):
            raise StageIOError("PredictionOutput payload must be a JSON object")
        allowed = {"schema", "schema_version", "predictions", "artifacts"}
        extra = sorted(k for k in data if k not in allowed)
        if extra:
            raise StageIOError(f"PredictionOutput contains unknown fields: {extra!r}")
        for req in ("schema", "schema_version", "predictions"):
            if req not in data:
                raise StageIOError(f"PredictionOutput missing required field: {req}")
        return cls(
            schema=data["schema"],
            schema_version=data["schema_version"],
            predictions=list(data["predictions"]),
            artifacts=list(data.get("artifacts", [])),
        )
