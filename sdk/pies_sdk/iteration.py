"""NDJSON writer/reader helpers for /work/out/iterations.jsonl."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from pies_sdk.contracts import check_finite_json
from pies_sdk.errors import StageIOError

ITERATION_LOG_FILENAME = "iterations.jsonl"
DEFAULT_ITERATIONS_PATH = Path("/work/out") / ITERATION_LOG_FILENAME
ITERATION_STATUSES = ("running", "converged", "max_iterations_reached", "stopped")


def _reject_json_constant(token: str) -> NoReturn:
    raise StageIOError(f"Non-finite JSON constant in iterations log: {token!r}")


@dataclass(frozen=True, slots=True)
class IterationRecord:
    """Single NDJSON record emitted per iteration round."""

    iteration: int
    metric: str
    value: float
    unit: str = "1"
    status: str = "running"
    timestamp: str | None = None
    details: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.iteration, bool) or not isinstance(self.iteration, int) or self.iteration < 1:
            raise StageIOError(f"iteration must be a positive integer >= 1, got {self.iteration!r}")
        if not isinstance(self.metric, str) or not self.metric:
            raise StageIOError(f"metric must be a non-empty string, got {self.metric!r}")
        if (
            isinstance(self.value, bool)
            or not isinstance(self.value, (int, float))
            or not math.isfinite(self.value)
            or self.value < 0
        ):
            raise StageIOError(f"value must be a finite non-negative number, got {self.value!r}")
        object.__setattr__(self, "value", float(self.value))
        if self.unit != "1":
            raise StageIOError(f"iteration metric unit must be '1', got {self.unit!r}")
        if self.status not in ITERATION_STATUSES:
            raise StageIOError(f"Invalid iteration status: {self.status!r}")
        if self.timestamp is not None and (not isinstance(self.timestamp, str) or not self.timestamp):
            raise StageIOError(f"timestamp must be a non-empty ISO 8601 string, got {self.timestamp!r}")
        if self.details is not None:
            if not isinstance(self.details, Mapping):
                raise StageIOError("details must be a mapping when provided")
            check_finite_json(self.details, "iteration.details")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "iteration": self.iteration,
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "status": self.status,
        }
        if self.timestamp is not None:
            payload["timestamp"] = self.timestamp
        if self.details is not None:
            payload["details"] = dict(self.details)
        return payload

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> IterationRecord:
        if not isinstance(data, Mapping):
            raise StageIOError("Iteration NDJSON record must be a JSON object")
        allowed = {"iteration", "metric", "value", "unit", "status", "timestamp", "details"}
        extra = sorted(k for k in data if k not in allowed)
        if extra:
            raise StageIOError(f"Iteration record contains unknown fields: {extra!r}")
        for req in ("iteration", "metric", "value", "unit", "status"):
            if req not in data:
                raise StageIOError(f"Iteration record missing required field: {req}")
        return cls(
            iteration=data["iteration"],
            metric=data["metric"],
            value=data["value"],
            unit=data["unit"],
            status=data["status"],
            timestamp=data.get("timestamp"),
            details=data.get("details"),
        )


def utc_timestamp() -> str:
    """Return current UTC ISO 8601 timestamp ending in Z."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class IterationWriter:
    """Streaming NDJSON writer for /work/out/iterations.jsonl."""

    def __init__(self, path: Path = DEFAULT_ITERATIONS_PATH, *, truncate: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        mode = "w" if truncate else "a"
        self._handle = self.path.open(mode, encoding="utf-8", newline="\n")

    def append(self, record: IterationRecord) -> None:
        if not isinstance(record, IterationRecord):
            raise TypeError(f"Expected IterationRecord, got {type(record).__name__}")
        self._handle.write(record.to_json_line() + "\n")
        self._handle.flush()

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    def __enter__(self) -> IterationWriter:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


def iter_iterations(path: Path = DEFAULT_ITERATIONS_PATH) -> Iterator[IterationRecord]:
    """Stream IterationRecord entries from an NDJSON file."""
    target = Path(path)
    if not target.is_file():
        raise StageIOError(f"Iterations NDJSON file does not exist: {target}")
    with target.open("r", encoding="utf-8") as handle:
        for lineno, raw_line in enumerate(handle, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped, parse_constant=_reject_json_constant)
            except json.JSONDecodeError as exc:
                raise StageIOError(f"Malformed JSON at {target}:{lineno}: {exc}") from exc
            yield IterationRecord.from_dict(parsed)


def read_iterations(path: Path = DEFAULT_ITERATIONS_PATH) -> list[IterationRecord]:
    """Read all IterationRecord entries from an NDJSON file."""
    return list(iter_iterations(path))
