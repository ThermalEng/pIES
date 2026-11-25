"""F1 computation contract models and streaming I/O for primal/engineering solutions and reports."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from jsonschema import Draft202012Validator

from pies_sdk.contracts import (
    MATH_STATUSES,
    check_finite_json,
    read_json_file,
    write_json_file,
)
from pies_sdk.errors import StageIOError

PRIMAL_SOLUTION_SCHEMA = "ies.primal-solution"
PRIMAL_SOLUTION_VERSION = "1.0.0"

ENGINEERING_SOLUTION_SCHEMA = "ies.engineering-solution"
ENGINEERING_SOLUTION_VERSION = "1.0.0"

EVALUATION_REPORT_SCHEMA = "ies.evaluation-report"
EVALUATION_REPORT_VERSION = "1.0.0"

SOLVER_TERMINATION_REASONS = (
    "optimal",
    "time_limit",
    "infeasible",
    "unbounded",
    "not_solved",
)

CONSTRUCTION_UNIT_MODES = ("mandatory", "optional")
CHART_TYPES = ("line", "bar", "area", "scatter")
SOLVE_DOMAINS = ("original", "valid_relaxation", "restricted")

_SCHEMA_DIR = Path(__file__).parent / "schema"
_PROVIDER_REF_RE = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*@[0-9]+\.[0-9]+\.[0-9]+$")
_FEASIBLE_MATH_STATUSES = frozenset(("solved", "feasible"))


def load_primal_solution_schema() -> dict[str, Any]:
    return json.loads((_SCHEMA_DIR / "primal-solution-1.0.0.schema.json").read_text(encoding="utf-8"))


def load_engineering_solution_schema() -> dict[str, Any]:
    return json.loads(
        (_SCHEMA_DIR / "engineering-solution-1.0.0.schema.json").read_text(encoding="utf-8")
    )


def load_evaluation_report_schema() -> dict[str, Any]:
    return json.loads(
        (_SCHEMA_DIR / "evaluation-report-1.0.0.schema.json").read_text(encoding="utf-8")
    )


def _reject_json_constant(token: str) -> NoReturn:
    raise StageIOError(f"Non-finite JSON constant is forbidden: {token!r}")


def _validate_schema(schema: dict[str, Any], payload: dict[str, Any], label: str) -> None:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        loc = "/".join(str(p) for p in first.absolute_path) or "<root>"
        raise StageIOError(f"{label} invalid [{loc}]: {first.message}")


def _require_non_empty_str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise StageIOError(f"{name} must be a non-empty string, got {value!r}")
    return value


def _require_non_bool_int(value: object, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StageIOError(f"{name} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise StageIOError(f"{name} must be >= {minimum}, got {value!r}")
    return value


def _require_finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StageIOError(f"{name} must be a finite number, got {value!r}")
    res = float(value)
    if not math.isfinite(res):
        raise StageIOError(f"{name} forbids non-finite numeric value: {value!r}")
    return res


def _require_optional_finite_number(value: object, name: str) -> float | None:
    if value is None:
        return None
    return _require_finite_number(value, name)


def _check_provider_ref(value: object, name: str) -> str:
    text = _require_non_empty_str(value, name)
    if not _PROVIDER_REF_RE.fullmatch(text):
        raise StageIOError(f"{name} must match '<id>@<semver>', got {value!r}")
    return text


def _validate_primal_receipt(receipt: object) -> dict[str, Any]:
    if not isinstance(receipt, Mapping):
        raise StageIOError("PrimalSolution 'receipt' must be a JSON object")
    allowed = {
        "solver",
        "started_at",
        "ended_at",
        "elapsed_seconds",
        "solve_domain",
        "original_model_verified",
        "solver_diagnostics",
        "extensions",
    }
    extra = sorted(k for k in receipt if k not in allowed)
    if extra:
        raise StageIOError(f"PrimalSolution.receipt contains unknown fields: {extra!r}")
    _check_provider_ref(receipt.get("solver"), "receipt.solver")
    out: dict[str, Any] = {"solver": receipt["solver"]}
    for ts_key in ("started_at", "ended_at"):
        if ts_key in receipt:
            out[ts_key] = _require_non_empty_str(receipt[ts_key], f"receipt.{ts_key}")
    if "elapsed_seconds" in receipt:
        elapsed = _require_finite_number(receipt["elapsed_seconds"], "receipt.elapsed_seconds")
        if elapsed < 0:
            raise StageIOError("receipt.elapsed_seconds must be >= 0")
        out["elapsed_seconds"] = elapsed
    if "solve_domain" in receipt:
        if receipt["solve_domain"] not in SOLVE_DOMAINS:
            raise StageIOError(f"Invalid receipt.solve_domain: {receipt['solve_domain']!r}")
        out["solve_domain"] = receipt["solve_domain"]
    if "original_model_verified" in receipt:
        if not isinstance(receipt["original_model_verified"], bool):
            raise StageIOError("receipt.original_model_verified must be a boolean")
        out["original_model_verified"] = receipt["original_model_verified"]
    for obj_key in ("solver_diagnostics", "extensions"):
        if obj_key in receipt:
            if not isinstance(receipt[obj_key], Mapping):
                raise StageIOError(f"receipt.{obj_key} must be a JSON object")
            check_finite_json(receipt[obj_key], f"receipt.{obj_key}")
            out[obj_key] = dict(receipt[obj_key])
    return out


# ---------------------------------------------------------------------------
# 1. ies.primal-solution@1.0.0
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PrimalSolutionHeader:
    """Metadata header of ies.primal-solution@1.0.0 (excluding values array)."""

    math_status: str
    termination_reason: str
    has_feasible_solution: bool
    column_count: int
    objective_value: float | None
    best_bound: float | None
    relative_gap: float | None
    receipt: Mapping[str, Any]
    schema: str = PRIMAL_SOLUTION_SCHEMA
    schema_version: str = PRIMAL_SOLUTION_VERSION
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema != PRIMAL_SOLUTION_SCHEMA:
            raise StageIOError(f"Invalid PrimalSolution schema: {self.schema!r}")
        if self.schema_version != PRIMAL_SOLUTION_VERSION:
            raise StageIOError(f"Invalid PrimalSolution schema_version: {self.schema_version!r}")
        if self.math_status not in MATH_STATUSES:
            raise StageIOError(f"Invalid PrimalSolution math_status: {self.math_status!r}")
        if self.termination_reason not in SOLVER_TERMINATION_REASONS:
            raise StageIOError(
                f"Invalid PrimalSolution termination_reason: {self.termination_reason!r}"
            )
        if not isinstance(self.has_feasible_solution, bool):
            raise StageIOError("PrimalSolution.has_feasible_solution must be a boolean")
        _require_non_bool_int(self.column_count, "PrimalSolution.column_count", minimum=0)

        obj = _require_optional_finite_number(self.objective_value, "PrimalSolution.objective_value")
        bound = _require_optional_finite_number(self.best_bound, "PrimalSolution.best_bound")
        gap = _require_optional_finite_number(self.relative_gap, "PrimalSolution.relative_gap")
        object.__setattr__(self, "objective_value", obj)
        object.__setattr__(self, "best_bound", bound)
        object.__setattr__(self, "relative_gap", gap)

        if self.has_feasible_solution:
            if self.math_status not in _FEASIBLE_MATH_STATUSES:
                raise StageIOError(
                    "has_feasible_solution=True requires math_status in "
                    f"('solved', 'feasible'), got {self.math_status!r}"
                )
            if obj is None:
                raise StageIOError("has_feasible_solution=True requires non-null objective_value")
        else:
            if self.math_status in _FEASIBLE_MATH_STATUSES:
                raise StageIOError(
                    f"has_feasible_solution=False forbids math_status={self.math_status!r}"
                )

        norm_receipt = _validate_primal_receipt(self.receipt)
        object.__setattr__(self, "receipt", norm_receipt)

        if not isinstance(self.extensions, Mapping):
            raise StageIOError("PrimalSolution.extensions must be a JSON object")
        check_finite_json(self.extensions, "PrimalSolution.extensions")
        object.__setattr__(self, "extensions", dict(self.extensions))

    @property
    def expected_values_count(self) -> int:
        return self.column_count if self.has_feasible_solution else 0

    def to_metadata_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "math_status": self.math_status,
            "termination_reason": self.termination_reason,
            "has_feasible_solution": self.has_feasible_solution,
            "column_count": self.column_count,
            "objective_value": self.objective_value,
            "best_bound": self.best_bound,
            "relative_gap": self.relative_gap,
            "receipt": dict(self.receipt),
        }
        if self.extensions:
            out["extensions"] = dict(self.extensions)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PrimalSolutionHeader:
        if not isinstance(data, Mapping):
            raise StageIOError("PrimalSolution header must be a JSON object")
        allowed = {
            "schema",
            "schema_version",
            "math_status",
            "termination_reason",
            "has_feasible_solution",
            "column_count",
            "objective_value",
            "best_bound",
            "relative_gap",
            "receipt",
            "extensions",
        }
        extra = sorted(k for k in data if k not in allowed)
        if extra:
            raise StageIOError(f"PrimalSolution contains unknown fields: {extra!r}")
        for req in (
            "schema",
            "schema_version",
            "math_status",
            "termination_reason",
            "has_feasible_solution",
            "column_count",
            "objective_value",
            "best_bound",
            "relative_gap",
            "receipt",
        ):
            if req not in data:
                raise StageIOError(f"PrimalSolution missing required field: {req}")
        return cls(
            schema=data["schema"],
            schema_version=data["schema_version"],
            math_status=data["math_status"],
            termination_reason=data["termination_reason"],
            has_feasible_solution=data["has_feasible_solution"],
            column_count=data["column_count"],
            objective_value=data["objective_value"],
            best_bound=data["best_bound"],
            relative_gap=data["relative_gap"],
            receipt=data["receipt"],
            extensions=data.get("extensions", {}),
        )


@dataclass(frozen=True, slots=True)
class PrimalSolutionPayload:
    """Canonical ies.primal-solution@1.0.0 payload in 0-based original input column order."""

    math_status: str
    termination_reason: str
    has_feasible_solution: bool
    column_count: int
    values: tuple[float, ...]
    objective_value: float | None
    best_bound: float | None
    relative_gap: float | None
    receipt: Mapping[str, Any]
    schema: str = PRIMAL_SOLUTION_SCHEMA
    schema_version: str = PRIMAL_SOLUTION_VERSION
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        hdr = PrimalSolutionHeader(
            schema=self.schema,
            schema_version=self.schema_version,
            math_status=self.math_status,
            termination_reason=self.termination_reason,
            has_feasible_solution=self.has_feasible_solution,
            column_count=self.column_count,
            objective_value=self.objective_value,
            best_bound=self.best_bound,
            relative_gap=self.relative_gap,
            receipt=self.receipt,
            extensions=self.extensions,
        )
        object.__setattr__(self, "objective_value", hdr.objective_value)
        object.__setattr__(self, "best_bound", hdr.best_bound)
        object.__setattr__(self, "relative_gap", hdr.relative_gap)
        object.__setattr__(self, "receipt", hdr.receipt)
        object.__setattr__(self, "extensions", hdr.extensions)

        if isinstance(self.values, (str, bytes)) or not isinstance(self.values, (list, tuple)):
            raise StageIOError("PrimalSolution.values must be a list or tuple of finite numbers")
        norm_values = tuple(
            _require_finite_number(val, f"PrimalSolution.values[{idx}]")
            for idx, val in enumerate(self.values)
        )
        expected = hdr.expected_values_count
        if len(norm_values) != expected:
            raise StageIOError(
                f"PrimalSolution.values length ({len(norm_values)}) must equal {expected}"
            )
        object.__setattr__(self, "values", norm_values)

    @property
    def header(self) -> PrimalSolutionHeader:
        return PrimalSolutionHeader(
            schema=self.schema,
            schema_version=self.schema_version,
            math_status=self.math_status,
            termination_reason=self.termination_reason,
            has_feasible_solution=self.has_feasible_solution,
            column_count=self.column_count,
            objective_value=self.objective_value,
            best_bound=self.best_bound,
            relative_gap=self.relative_gap,
            receipt=self.receipt,
            extensions=self.extensions,
        )

    def to_dict(self) -> dict[str, Any]:
        out = self.header.to_metadata_dict()
        out["values"] = list(self.values)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PrimalSolutionPayload:
        if not isinstance(data, Mapping):
            raise StageIOError("PrimalSolution payload must be a JSON object")
        check_finite_json(data, "PrimalSolution")
        _validate_schema(load_primal_solution_schema(), dict(data), "PrimalSolution")
        return cls(
            schema=data["schema"],
            schema_version=data["schema_version"],
            math_status=data["math_status"],
            termination_reason=data["termination_reason"],
            has_feasible_solution=data["has_feasible_solution"],
            column_count=data["column_count"],
            values=tuple(data["values"]),
            objective_value=data["objective_value"],
            best_bound=data["best_bound"],
            relative_gap=data["relative_gap"],
            receipt=data["receipt"],
            extensions=data.get("extensions", {}),
        )


def write_primal_solution_streaming(
    path: Path,
    header: PrimalSolutionHeader | Mapping[str, Any],
    values: Iterable[float],
) -> Path:
    """Stream primal_solution.json to disk without materializing all column values in memory."""
    hdr = header if isinstance(header, PrimalSolutionHeader) else PrimalSolutionHeader.from_dict(header)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_target = target.with_name(f".{target.name}.tmp")
    meta = hdr.to_metadata_dict()
    expected = hdr.expected_values_count
    try:
        with tmp_target.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("{\n")
            for key in sorted(meta):
                encoded_val = json.dumps(
                    meta[key], ensure_ascii=False, sort_keys=True, allow_nan=False
                )
                handle.write(f'  {json.dumps(key)}: {encoded_val},\n')
            handle.write('  "values": [')
            count = 0
            for raw_val in values:
                num = _require_finite_number(raw_val, f"values[{count}]")
                if count > 0:
                    handle.write(", ")
                handle.write(repr(num))
                count += 1
            if count != expected:
                raise StageIOError(
                    f"PrimalSolution streamed values count ({count}) does not match expected ({expected})"
                )
            handle.write("]\n}\n")
        tmp_target.replace(target)
    except Exception:
        tmp_target.unlink(missing_ok=True)
        raise
    return target


def _scan_primal_json_offsets(raw_path: Path) -> tuple[dict[str, Any], int]:
    """Scan primal_solution.json in O(1) memory for metadata and char offset of 'values' array."""
    decoder = json.JSONDecoder(parse_constant=_reject_json_constant)
    header_map: dict[str, Any] = {}
    values_start: int | None = None
    seen_keys: set[str] = set()

    with raw_path.open("r", encoding="utf-8") as handle:
        buf = handle.read(65536)
        eof = not buf
        pos = 0
        consumed = 0

        def _refill(min_avail: int = 1) -> None:
            nonlocal buf, pos, consumed, eof
            if pos > 32768:
                consumed += pos
                buf = buf[pos:]
                pos = 0
            while not eof and len(buf) - pos < min_avail:
                more = handle.read(65536)
                if more:
                    buf += more
                else:
                    eof = True

        def _skip_ws() -> None:
            nonlocal pos
            while True:
                _refill()
                while pos < len(buf) and buf[pos] in " \t\r\n":
                    pos += 1
                if pos < len(buf) or eof:
                    return

        def _decode_next(label: str) -> Any:
            nonlocal buf, pos, eof
            while True:
                _refill()
                try:
                    val, next_pos = decoder.raw_decode(buf, pos)
                    pos = next_pos
                    return val
                except json.JSONDecodeError as exc:
                    if not eof:
                        more = handle.read(65536)
                        if more:
                            buf += more
                            continue
                        eof = True
                    raise StageIOError(f"Invalid PrimalSolution {label}: {exc}") from exc

        def _skip_array() -> None:
            nonlocal pos
            depth = 0
            in_string = False
            escape = False
            while True:
                _refill()
                if pos >= len(buf):
                    raise StageIOError("Unterminated 'values' array in PrimalSolution JSON")
                while pos < len(buf):
                    ch = buf[pos]
                    pos += 1
                    if in_string:
                        if escape:
                            escape = False
                        elif ch == "\\":
                            escape = True
                        elif ch == '"':
                            in_string = False
                    else:
                        if ch == '"':
                            in_string = True
                        elif ch in "[{":
                            depth += 1
                        elif ch in "]}":
                            depth -= 1
                            if depth == 0:
                                return

        _skip_ws()
        if pos >= len(buf) or buf[pos] != "{":
            raise StageIOError("PrimalSolution payload must be a JSON object")
        pos += 1
        _skip_ws()
        if pos < len(buf) and buf[pos] == "}":
            raise StageIOError("PrimalSolution payload cannot be empty")

        while True:
            key = _decode_next("JSON key")
            if not isinstance(key, str):
                raise StageIOError("PrimalSolution JSON object keys must be strings")
            if key in seen_keys:
                raise StageIOError(f"Duplicate key in PrimalSolution JSON: {key!r}")
            seen_keys.add(key)

            _skip_ws()
            if pos >= len(buf) or buf[pos] != ":":
                raise StageIOError(f"Expected ':' after key {key!r} in PrimalSolution JSON")
            pos += 1
            _skip_ws()

            if key == "values":
                if pos >= len(buf) or buf[pos] != "[":
                    raise StageIOError("PrimalSolution.values must be a JSON array")
                values_start = consumed + pos
                _skip_array()
            else:
                header_map[key] = _decode_next(f"field {key!r}")

            _skip_ws()
            if pos < len(buf) and buf[pos] == ",":
                pos += 1
                _skip_ws()
                continue
            if pos < len(buf) and buf[pos] == "}":
                pos += 1
                _skip_ws()
                if pos < len(buf):
                    raise StageIOError("Unexpected trailing content in PrimalSolution JSON")
                break
            raise StageIOError("Expected ',' or '}' in PrimalSolution JSON")

    if values_start is None:
        raise StageIOError("PrimalSolution missing required field: values")
    return header_map, values_start


def iter_primal_solution_values(path: Path) -> tuple[PrimalSolutionHeader, Iterator[float]]:
    """Return validated PrimalSolutionHeader and a lazy streaming iterator over 0-based values."""
    target = Path(path)
    if not target.is_file():
        raise StageIOError(f"PrimalSolution file does not exist: {target}")
    try:
        header_map, values_char_offset = _scan_primal_json_offsets(target)
    except UnicodeDecodeError as exc:
        raise StageIOError(f"Invalid UTF-8 in PrimalSolution file {target}: {exc}") from exc

    header = PrimalSolutionHeader.from_dict(header_map)
    expected = header.expected_values_count

    def _stream() -> Iterator[float]:
        decoder = json.JSONDecoder(parse_constant=_reject_json_constant)
        buf = ""
        count = 0
        with target.open("r", encoding="utf-8") as handle:
            # Skip up to values '[' character
            remaining = values_char_offset
            while remaining > 0:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    raise StageIOError("Unexpected EOF before PrimalSolution.values")
                remaining -= len(chunk)
            buf = handle.read(65536)
            if not buf or buf[0] != "[":
                raise StageIOError("PrimalSolution.values must start with '['")
            pos = 1
            eof = False

            def _ensure_data() -> None:
                nonlocal buf, pos, eof
                if pos > 32768:
                    buf = buf[pos:]
                    pos = 0
                if not eof and len(buf) - pos < 4096:
                    more = handle.read(65536)
                    if more:
                        buf += more
                    else:
                        eof = True

            while True:
                _ensure_data()
                while pos < len(buf) and buf[pos] in " \t\r\n":
                    pos += 1
                    if pos >= len(buf):
                        _ensure_data()
                if pos >= len(buf):
                    raise StageIOError("Unexpected EOF inside PrimalSolution.values")
                if buf[pos] == "]":
                    if count > 0:
                        raise StageIOError("Trailing comma in PrimalSolution.values")
                    break
                while True:
                    try:
                        val, next_pos = decoder.raw_decode(buf, pos)
                        pos = next_pos
                        break
                    except json.JSONDecodeError as exc:
                        if not eof:
                            more = handle.read(65536)
                            if more:
                                buf += more
                                continue
                            eof = True
                        raise StageIOError(
                            f"Malformed number at PrimalSolution.values[{count}]: {exc}"
                        ) from exc
                yield _require_finite_number(val, f"PrimalSolution.values[{count}]")
                count += 1
                _ensure_data()
                while pos < len(buf) and buf[pos] in " \t\r\n":
                    pos += 1
                    if pos >= len(buf):
                        _ensure_data()
                if pos >= len(buf):
                    raise StageIOError("Unexpected EOF inside PrimalSolution.values")
                if buf[pos] == ",":
                    pos += 1
                    # Require an element after comma
                    _ensure_data()
                    while pos < len(buf) and buf[pos] in " \t\r\n":
                        pos += 1
                        if pos >= len(buf):
                            _ensure_data()
                    if pos < len(buf) and buf[pos] == "]":
                        raise StageIOError("Trailing comma in PrimalSolution.values")
                    continue
                if buf[pos] == "]":
                    break
                raise StageIOError(
                    f"Expected ',' or ']' after PrimalSolution.values[{count - 1}]"
                )
        if count != expected:
            raise StageIOError(
                f"PrimalSolution.values length ({count}) must equal {expected}"
            )

    return header, _stream()


# ---------------------------------------------------------------------------
# 2. ies.engineering-solution@1.0.0
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SolverSummary:
    """Canonical solver termination & optimality summary."""

    termination_reason: str
    math_status: str
    has_feasible_solution: bool
    objective_value: float | None
    objective_unit: str | None
    best_bound: float | None
    relative_gap: float | None
    solver_diagnostics: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.termination_reason not in SOLVER_TERMINATION_REASONS:
            raise StageIOError(f"Invalid solver_summary.termination_reason: {self.termination_reason!r}")
        if self.math_status not in MATH_STATUSES:
            raise StageIOError(f"Invalid solver_summary.math_status: {self.math_status!r}")
        if not isinstance(self.has_feasible_solution, bool):
            raise StageIOError("solver_summary.has_feasible_solution must be a boolean")

        obj = _require_optional_finite_number(self.objective_value, "solver_summary.objective_value")
        bound = _require_optional_finite_number(self.best_bound, "solver_summary.best_bound")
        gap = _require_optional_finite_number(self.relative_gap, "solver_summary.relative_gap")
        object.__setattr__(self, "objective_value", obj)
        object.__setattr__(self, "best_bound", bound)
        object.__setattr__(self, "relative_gap", gap)

        if self.objective_unit is not None:
            _require_non_empty_str(self.objective_unit, "solver_summary.objective_unit")

        if self.has_feasible_solution:
            if self.math_status not in _FEASIBLE_MATH_STATUSES:
                raise StageIOError(
                    "has_feasible_solution=True requires math_status in "
                    f"('solved', 'feasible'), got {self.math_status!r}"
                )
            if obj is None:
                raise StageIOError("has_feasible_solution=True requires non-null objective_value")
            if self.objective_unit is None:
                raise StageIOError("has_feasible_solution=True requires non-null objective_unit")
        else:
            if self.math_status in _FEASIBLE_MATH_STATUSES:
                raise StageIOError(
                    f"has_feasible_solution=False forbids math_status={self.math_status!r}"
                )

        if self.solver_diagnostics is not None:
            if not isinstance(self.solver_diagnostics, Mapping):
                raise StageIOError("solver_summary.solver_diagnostics must be a JSON object")
            check_finite_json(self.solver_diagnostics, "solver_summary.solver_diagnostics")
            object.__setattr__(self, "solver_diagnostics", dict(self.solver_diagnostics))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "termination_reason": self.termination_reason,
            "math_status": self.math_status,
            "has_feasible_solution": self.has_feasible_solution,
            "objective_value": self.objective_value,
            "objective_unit": self.objective_unit,
            "best_bound": self.best_bound,
            "relative_gap": self.relative_gap,
        }
        if self.solver_diagnostics is not None:
            out["solver_diagnostics"] = dict(self.solver_diagnostics)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SolverSummary:
        if not isinstance(data, Mapping):
            raise StageIOError("solver_summary must be a JSON object")
        allowed = {
            "termination_reason",
            "math_status",
            "has_feasible_solution",
            "objective_value",
            "objective_unit",
            "best_bound",
            "relative_gap",
            "solver_diagnostics",
        }
        extra = sorted(k for k in data if k not in allowed)
        if extra:
            raise StageIOError(f"solver_summary contains unknown fields: {extra!r}")
        for req in (
            "termination_reason",
            "math_status",
            "has_feasible_solution",
            "objective_value",
            "objective_unit",
            "best_bound",
            "relative_gap",
        ):
            if req not in data:
                raise StageIOError(f"solver_summary missing required field: {req}")
        return cls(
            termination_reason=data["termination_reason"],
            math_status=data["math_status"],
            has_feasible_solution=data["has_feasible_solution"],
            objective_value=data["objective_value"],
            objective_unit=data["objective_unit"],
            best_bound=data["best_bound"],
            relative_gap=data["relative_gap"],
            solver_diagnostics=data.get("solver_diagnostics"),
        )


@dataclass(frozen=True, slots=True)
class ConstructionScale:
    planning_variable_id: str
    value: float
    unit: str

    def __post_init__(self) -> None:
        _require_non_empty_str(self.planning_variable_id, "scale.planning_variable_id")
        object.__setattr__(self, "value", _require_finite_number(self.value, "scale.value"))
        _require_non_empty_str(self.unit, "scale.unit")

    def to_dict(self) -> dict[str, Any]:
        return {
            "planning_variable_id": self.planning_variable_id,
            "value": self.value,
            "unit": self.unit,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ConstructionScale:
        if not isinstance(data, Mapping):
            raise StageIOError("ConstructionScale must be a JSON object")
        if set(data) != {"planning_variable_id", "value", "unit"}:
            raise StageIOError(f"ConstructionScale has invalid keys: {sorted(data)!r}")
        return cls(
            planning_variable_id=data["planning_variable_id"],
            value=data["value"],
            unit=data["unit"],
        )


@dataclass(frozen=True, slots=True)
class ConstructionUnit:
    unit_id: str
    instance_id: str
    mode: str
    built: bool
    scales: tuple[ConstructionScale, ...]

    def __post_init__(self) -> None:
        _require_non_empty_str(self.unit_id, "construction_unit.unit_id")
        _require_non_empty_str(self.instance_id, "construction_unit.instance_id")
        if self.mode not in CONSTRUCTION_UNIT_MODES:
            raise StageIOError(f"Invalid construction_unit.mode: {self.mode!r}")
        if not isinstance(self.built, bool):
            raise StageIOError("construction_unit.built must be a boolean")
        if isinstance(self.scales, (str, bytes)) or not isinstance(self.scales, (list, tuple)):
            raise StageIOError("construction_unit.scales must be a list")
        norm_scales = tuple(
            s if isinstance(s, ConstructionScale) else ConstructionScale.from_dict(s)
            for s in self.scales
        )
        object.__setattr__(self, "scales", norm_scales)

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "instance_id": self.instance_id,
            "mode": self.mode,
            "built": self.built,
            "scales": [s.to_dict() for s in self.scales],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ConstructionUnit:
        if not isinstance(data, Mapping):
            raise StageIOError("ConstructionUnit must be a JSON object")
        if set(data) != {"unit_id", "instance_id", "mode", "built", "scales"}:
            raise StageIOError(f"ConstructionUnit has invalid keys: {sorted(data)!r}")
        return cls(
            unit_id=data["unit_id"],
            instance_id=data["instance_id"],
            mode=data["mode"],
            built=data["built"],
            scales=data["scales"],
        )


@dataclass(frozen=True, slots=True)
class DeviceSeries:
    instance_id: str
    variable_id: str
    unit: str
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        _require_non_empty_str(self.instance_id, "series.instance_id")
        _require_non_empty_str(self.variable_id, "series.variable_id")
        _require_non_empty_str(self.unit, "series.unit")
        if isinstance(self.values, (str, bytes)) or not isinstance(self.values, (list, tuple)):
            raise StageIOError("series.values must be a list of numbers")
        norm = tuple(
            _require_finite_number(v, f"series.values[{idx}]")
            for idx, v in enumerate(self.values)
        )
        object.__setattr__(self, "values", norm)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "variable_id": self.variable_id,
            "unit": self.unit,
            "values": list(self.values),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DeviceSeries:
        if not isinstance(data, Mapping):
            raise StageIOError("DeviceSeries must be a JSON object")
        if set(data) != {"instance_id", "variable_id", "unit", "values"}:
            raise StageIOError(f"DeviceSeries has invalid keys: {sorted(data)!r}")
        return cls(
            instance_id=data["instance_id"],
            variable_id=data["variable_id"],
            unit=data["unit"],
            values=tuple(data["values"]),
        )


@dataclass(frozen=True, slots=True)
class SystemSeries:
    block: str
    variable: str
    unit: str
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        _require_non_empty_str(self.block, "system_series.block")
        _require_non_empty_str(self.variable, "system_series.variable")
        _require_non_empty_str(self.unit, "system_series.unit")
        if isinstance(self.values, (str, bytes)) or not isinstance(self.values, (list, tuple)):
            raise StageIOError("system_series.values must be a list of numbers")
        norm = tuple(
            _require_finite_number(v, f"system_series.values[{idx}]")
            for idx, v in enumerate(self.values)
        )
        object.__setattr__(self, "values", norm)

    def to_dict(self) -> dict[str, Any]:
        return {
            "block": self.block,
            "variable": self.variable,
            "unit": self.unit,
            "values": list(self.values),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SystemSeries:
        if not isinstance(data, Mapping):
            raise StageIOError("SystemSeries must be a JSON object")
        if set(data) != {"block", "variable", "unit", "values"}:
            raise StageIOError(f"SystemSeries has invalid keys: {sorted(data)!r}")
        return cls(
            block=data["block"],
            variable=data["variable"],
            unit=data["unit"],
            values=tuple(data["values"]),
        )


@dataclass(frozen=True, slots=True)
class EngineeringSolutionPayload:
    """Canonical dual-state ies.engineering-solution@1.0.0 contract (solution.json)."""

    solver_summary: SolverSummary
    construction_units: tuple[ConstructionUnit, ...] | None = None
    series: Mapping[str, DeviceSeries] | None = None
    system_series: Mapping[str, SystemSeries] | None = None
    metrics: Mapping[str, Any] | None = None
    model_sources: Mapping[str, Any] | None = None
    schema: str = ENGINEERING_SOLUTION_SCHEMA
    schema_version: str = ENGINEERING_SOLUTION_VERSION
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema != ENGINEERING_SOLUTION_SCHEMA:
            raise StageIOError(f"Invalid EngineeringSolution schema: {self.schema!r}")
        if self.schema_version != ENGINEERING_SOLUTION_VERSION:
            raise StageIOError(
                f"Invalid EngineeringSolution schema_version: {self.schema_version!r}"
            )
        summary = (
            self.solver_summary
            if isinstance(self.solver_summary, SolverSummary)
            else SolverSummary.from_dict(self.solver_summary)
        )
        object.__setattr__(self, "solver_summary", summary)

        norm_units: tuple[ConstructionUnit, ...] | None = None
        if self.construction_units is not None:
            if isinstance(self.construction_units, (str, bytes)) or not isinstance(
                self.construction_units, (list, tuple)
            ):
                raise StageIOError("EngineeringSolution.construction_units must be a list or null")
            norm_units = tuple(
                u if isinstance(u, ConstructionUnit) else ConstructionUnit.from_dict(u)
                for u in self.construction_units
            )
        object.__setattr__(self, "construction_units", norm_units)

        norm_series: dict[str, DeviceSeries] | None = None
        if self.series is not None:
            if not isinstance(self.series, Mapping):
                raise StageIOError("EngineeringSolution.series must be a JSON object or null")
            norm_series = {}
            for sid, item in self.series.items():
                _require_non_empty_str(sid, "EngineeringSolution.series key")
                norm_series[sid] = item if isinstance(item, DeviceSeries) else DeviceSeries.from_dict(item)
        object.__setattr__(self, "series", norm_series)

        norm_sys: dict[str, SystemSeries] | None = None
        if self.system_series is not None:
            if not isinstance(self.system_series, Mapping):
                raise StageIOError("EngineeringSolution.system_series must be a JSON object or null")
            norm_sys = {}
            for sid, item in self.system_series.items():
                _require_non_empty_str(sid, "EngineeringSolution.system_series key")
                norm_sys[sid] = item if isinstance(item, SystemSeries) else SystemSeries.from_dict(item)
        object.__setattr__(self, "system_series", norm_sys)

        if summary.has_feasible_solution:
            if norm_units is None:
                raise StageIOError(
                    "Feasible EngineeringSolution requires 'construction_units'"
                )
            if norm_series is None:
                raise StageIOError("Feasible EngineeringSolution requires 'series'")
            if norm_sys is None:
                raise StageIOError("Feasible EngineeringSolution requires 'system_series'")
            overlap = sorted(set(norm_series) & set(norm_sys))
            if overlap:
                raise StageIOError(
                    f"series and system_series share duplicate series_id keys: {overlap!r}"
                )
        else:
            if norm_units:
                raise StageIOError(
                    "Non-feasible EngineeringSolution cannot contain non-empty 'construction_units'"
                )
            if norm_series:
                raise StageIOError(
                    "Non-feasible EngineeringSolution cannot contain non-empty 'series'"
                )
            if norm_sys:
                raise StageIOError(
                    "Non-feasible EngineeringSolution cannot contain non-empty 'system_series'"
                )

        for opt_name in ("metrics", "model_sources"):
            val = getattr(self, opt_name)
            if val is not None:
                if not isinstance(val, Mapping):
                    raise StageIOError(f"EngineeringSolution.{opt_name} must be a JSON object")
                check_finite_json(val, f"EngineeringSolution.{opt_name}")
                object.__setattr__(self, opt_name, dict(val))

        if not isinstance(self.extensions, Mapping):
            raise StageIOError("EngineeringSolution.extensions must be a JSON object")
        check_finite_json(self.extensions, "EngineeringSolution.extensions")
        object.__setattr__(self, "extensions", dict(self.extensions))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "solver_summary": self.solver_summary.to_dict(),
        }
        if self.construction_units is not None:
            out["construction_units"] = [u.to_dict() for u in self.construction_units]
        if self.series is not None:
            out["series"] = {k: v.to_dict() for k, v in self.series.items()}
        if self.system_series is not None:
            out["system_series"] = {k: v.to_dict() for k, v in self.system_series.items()}
        if self.metrics is not None:
            out["metrics"] = dict(self.metrics)
        if self.model_sources is not None:
            out["model_sources"] = dict(self.model_sources)
        if self.extensions:
            out["extensions"] = dict(self.extensions)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EngineeringSolutionPayload:
        if not isinstance(data, Mapping):
            raise StageIOError("EngineeringSolution payload must be a JSON object")
        check_finite_json(data, "EngineeringSolution")
        _validate_schema(load_engineering_solution_schema(), dict(data), "EngineeringSolution")
        return cls(
            schema=data["schema"],
            schema_version=data["schema_version"],
            solver_summary=data["solver_summary"],
            construction_units=data.get("construction_units"),
            series=data.get("series"),
            system_series=data.get("system_series"),
            metrics=data.get("metrics"),
            model_sources=data.get("model_sources"),
            extensions=data.get("extensions", {}),
        )


# ---------------------------------------------------------------------------
# 3. ies.evaluation-report@1.0.0
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvaluationMetric:
    id: str
    label: str
    value: float | str | None
    unit: str

    def __post_init__(self) -> None:
        _require_non_empty_str(self.id, "metric.id")
        _require_non_empty_str(self.label, "metric.label")
        _require_non_empty_str(self.unit, "metric.unit")
        if isinstance(self.value, bool):
            raise StageIOError(f"metric {self.id!r} value cannot be a boolean")
        if isinstance(self.value, (int, float)):
            object.__setattr__(
                self, "value", _require_finite_number(self.value, f"metric.{self.id}.value")
            )
        elif isinstance(self.value, str):
            _require_non_empty_str(self.value, f"metric.{self.id}.value")
        elif self.value is not None:
            raise StageIOError(
                f"metric {self.id!r} value must be a finite number, string, or null"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "value": self.value, "unit": self.unit}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvaluationMetric:
        if not isinstance(data, Mapping):
            raise StageIOError("EvaluationMetric must be a JSON object")
        if set(data) != {"id", "label", "value", "unit"}:
            raise StageIOError(f"EvaluationMetric has invalid keys: {sorted(data)!r}")
        return cls(id=data["id"], label=data["label"], value=data["value"], unit=data["unit"])


@dataclass(frozen=True, slots=True)
class EvaluationChartSeries:
    id: str
    label: str
    values: tuple[float | None, ...]
    unit: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.id, "chart_series.id")
        _require_non_empty_str(self.label, "chart_series.label")
        if self.unit is not None:
            _require_non_empty_str(self.unit, "chart_series.unit")
        if isinstance(self.values, (str, bytes)) or not isinstance(self.values, (list, tuple)):
            raise StageIOError("chart_series.values must be a list")
        norm = tuple(
            _require_optional_finite_number(v, f"chart_series.{self.id}.values[{idx}]")
            for idx, v in enumerate(self.values)
        )
        object.__setattr__(self, "values", norm)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "label": self.label, "values": list(self.values)}
        if self.unit is not None:
            out["unit"] = self.unit
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvaluationChartSeries:
        if not isinstance(data, Mapping):
            raise StageIOError("EvaluationChartSeries must be a JSON object")
        allowed = {"id", "label", "values", "unit"}
        extra = sorted(k for k in data if k not in allowed)
        if extra:
            raise StageIOError(f"EvaluationChartSeries contains unknown fields: {extra!r}")
        for req in ("id", "label", "values"):
            if req not in data:
                raise StageIOError(f"EvaluationChartSeries missing required field: {req}")
        return cls(
            id=data["id"],
            label=data["label"],
            values=tuple(data["values"]),
            unit=data.get("unit"),
        )


@dataclass(frozen=True, slots=True)
class EvaluationChart:
    id: str
    title: str
    x_label: str
    y_label: str
    y_unit: str
    series: tuple[EvaluationChartSeries, ...]
    x_unit: str | None = None
    chart_type: str | None = None
    x_values: tuple[str | float, ...] | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.id, "chart.id")
        _require_non_empty_str(self.title, "chart.title")
        _require_non_empty_str(self.x_label, "chart.x_label")
        _require_non_empty_str(self.y_label, "chart.y_label")
        _require_non_empty_str(self.y_unit, "chart.y_unit")
        if self.x_unit is not None:
            _require_non_empty_str(self.x_unit, "chart.x_unit")
        if self.chart_type is not None and self.chart_type not in CHART_TYPES:
            raise StageIOError(f"Invalid chart.chart_type: {self.chart_type!r}")
        if (
            isinstance(self.series, (str, bytes))
            or not isinstance(self.series, (list, tuple))
            or not self.series
        ):
            raise StageIOError("chart.series must be a non-empty list")
        norm_series = tuple(
            s if isinstance(s, EvaluationChartSeries) else EvaluationChartSeries.from_dict(s)
            for s in self.series
        )
        seen_ids: set[str] = set()
        for item in norm_series:
            if item.id in seen_ids:
                raise StageIOError(f"Duplicate series id in chart {self.id!r}: {item.id!r}")
            seen_ids.add(item.id)
        object.__setattr__(self, "series", norm_series)

        if self.x_values is not None:
            if isinstance(self.x_values, (str, bytes)) or not isinstance(self.x_values, (list, tuple)):
                raise StageIOError("chart.x_values must be a list")
            norm_x: list[str | float] = []
            for idx, xv in enumerate(self.x_values):
                if isinstance(xv, bool):
                    raise StageIOError(f"chart.{self.id}.x_values[{idx}] cannot be a boolean")
                if isinstance(xv, str):
                    norm_x.append(xv)
                elif isinstance(xv, (int, float)):
                    norm_x.append(_require_finite_number(xv, f"chart.{self.id}.x_values[{idx}]"))
                else:
                    raise StageIOError(f"chart.{self.id}.x_values[{idx}] must be string or number")
            for item in norm_series:
                if len(item.values) != len(norm_x):
                    raise StageIOError(
                        f"chart.{self.id}.series[{item.id}] length ({len(item.values)}) "
                        f"must match x_values length ({len(norm_x)})"
                    )
            object.__setattr__(self, "x_values", tuple(norm_x))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "x_label": self.x_label,
            "y_label": self.y_label,
            "y_unit": self.y_unit,
            "series": [s.to_dict() for s in self.series],
        }
        if self.x_unit is not None:
            out["x_unit"] = self.x_unit
        if self.chart_type is not None:
            out["chart_type"] = self.chart_type
        if self.x_values is not None:
            out["x_values"] = list(self.x_values)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvaluationChart:
        if not isinstance(data, Mapping):
            raise StageIOError("EvaluationChart must be a JSON object")
        allowed = {
            "id",
            "title",
            "x_label",
            "y_label",
            "y_unit",
            "x_unit",
            "chart_type",
            "x_values",
            "series",
        }
        extra = sorted(k for k in data if k not in allowed)
        if extra:
            raise StageIOError(f"EvaluationChart contains unknown fields: {extra!r}")
        for req in ("id", "title", "x_label", "y_label", "y_unit", "series"):
            if req not in data:
                raise StageIOError(f"EvaluationChart missing required field: {req}")
        return cls(
            id=data["id"],
            title=data["title"],
            x_label=data["x_label"],
            y_label=data["y_label"],
            y_unit=data["y_unit"],
            series=data["series"],
            x_unit=data.get("x_unit"),
            chart_type=data.get("chart_type"),
            x_values=data.get("x_values"),
        )


@dataclass(frozen=True, slots=True)
class EvaluationTableColumn:
    id: str
    label: str
    unit: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.id, "table_column.id")
        _require_non_empty_str(self.label, "table_column.label")
        if self.unit is not None:
            _require_non_empty_str(self.unit, "table_column.unit")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "label": self.label}
        if self.unit is not None:
            out["unit"] = self.unit
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvaluationTableColumn:
        if not isinstance(data, Mapping):
            raise StageIOError("EvaluationTableColumn must be a JSON object")
        allowed = {"id", "label", "unit"}
        extra = sorted(k for k in data if k not in allowed)
        if extra:
            raise StageIOError(f"EvaluationTableColumn contains unknown fields: {extra!r}")
        for req in ("id", "label"):
            if req not in data:
                raise StageIOError(f"EvaluationTableColumn missing required field: {req}")
        return cls(id=data["id"], label=data["label"], unit=data.get("unit"))


@dataclass(frozen=True, slots=True)
class EvaluationTable:
    id: str
    title: str
    columns: tuple[EvaluationTableColumn, ...]
    rows: tuple[tuple[str | int | float | bool | None, ...], ...]

    def __post_init__(self) -> None:
        _require_non_empty_str(self.id, "table.id")
        _require_non_empty_str(self.title, "table.title")
        if (
            isinstance(self.columns, (str, bytes))
            or not isinstance(self.columns, (list, tuple))
            or not self.columns
        ):
            raise StageIOError("table.columns must be a non-empty list")
        norm_cols = tuple(
            c if isinstance(c, EvaluationTableColumn) else EvaluationTableColumn.from_dict(c)
            for c in self.columns
        )
        seen_col_ids: set[str] = set()
        for col in norm_cols:
            if col.id in seen_col_ids:
                raise StageIOError(f"Duplicate column id in table {self.id!r}: {col.id!r}")
            seen_col_ids.add(col.id)
        object.__setattr__(self, "columns", norm_cols)

        if isinstance(self.rows, (str, bytes)) or not isinstance(self.rows, (list, tuple)):
            raise StageIOError("table.rows must be a list of row arrays")
        norm_rows: list[tuple[Any, ...]] = []
        col_count = len(norm_cols)
        for r_idx, row in enumerate(self.rows):
            if isinstance(row, (str, bytes)) or not isinstance(row, (list, tuple)):
                raise StageIOError(f"table.{self.id}.rows[{r_idx}] must be an array")
            if len(row) != col_count:
                raise StageIOError(
                    f"table.{self.id}.rows[{r_idx}] length ({len(row)}) does not match columns ({col_count})"
                )
            check_finite_json(row, f"table.{self.id}.rows[{r_idx}]")
            for c_idx, cell in enumerate(row):
                if cell is not None and not isinstance(cell, (str, int, float, bool)):
                    raise StageIOError(
                        f"table.{self.id}.rows[{r_idx}][{c_idx}] has invalid cell type {type(cell).__name__}"
                    )
            norm_rows.append(tuple(row))
        object.__setattr__(self, "rows", tuple(norm_rows))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "columns": [c.to_dict() for c in self.columns],
            "rows": [list(r) for r in self.rows],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvaluationTable:
        if not isinstance(data, Mapping):
            raise StageIOError("EvaluationTable must be a JSON object")
        if set(data) != {"id", "title", "columns", "rows"}:
            raise StageIOError(f"EvaluationTable has invalid keys: {sorted(data)!r}")
        return cls(
            id=data["id"],
            title=data["title"],
            columns=data["columns"],
            rows=data["rows"],
        )


@dataclass(frozen=True, slots=True)
class EvaluationReportPayload:
    """Canonical self-describing ies.evaluation-report@1.0.0 contract (evaluation_report.json)."""

    title: str
    evaluator: str
    metrics: tuple[EvaluationMetric, ...] = ()
    charts: tuple[EvaluationChart, ...] = ()
    tables: tuple[EvaluationTable, ...] = ()
    summary: str | None = None
    schema: str = EVALUATION_REPORT_SCHEMA
    schema_version: str = EVALUATION_REPORT_VERSION
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema != EVALUATION_REPORT_SCHEMA:
            raise StageIOError(f"Invalid EvaluationReport schema: {self.schema!r}")
        if self.schema_version != EVALUATION_REPORT_VERSION:
            raise StageIOError(f"Invalid EvaluationReport schema_version: {self.schema_version!r}")
        _require_non_empty_str(self.title, "EvaluationReport.title")
        _check_provider_ref(self.evaluator, "EvaluationReport.evaluator")
        if self.summary is not None and not isinstance(self.summary, str):
            raise StageIOError("EvaluationReport.summary must be a string or null")

        if isinstance(self.metrics, (str, bytes)) or not isinstance(self.metrics, (list, tuple)):
            raise StageIOError("EvaluationReport.metrics must be a list")
        norm_metrics = tuple(
            m if isinstance(m, EvaluationMetric) else EvaluationMetric.from_dict(m)
            for m in self.metrics
        )
        seen_metric_ids: set[str] = set()
        for m in norm_metrics:
            if m.id in seen_metric_ids:
                raise StageIOError(f"Duplicate metric id in EvaluationReport: {m.id!r}")
            seen_metric_ids.add(m.id)
        object.__setattr__(self, "metrics", norm_metrics)

        if isinstance(self.charts, (str, bytes)) or not isinstance(self.charts, (list, tuple)):
            raise StageIOError("EvaluationReport.charts must be a list")
        norm_charts = tuple(
            c if isinstance(c, EvaluationChart) else EvaluationChart.from_dict(c)
            for c in self.charts
        )
        seen_chart_ids: set[str] = set()
        for c in norm_charts:
            if c.id in seen_chart_ids:
                raise StageIOError(f"Duplicate chart id in EvaluationReport: {c.id!r}")
            seen_chart_ids.add(c.id)
        object.__setattr__(self, "charts", norm_charts)

        if isinstance(self.tables, (str, bytes)) or not isinstance(self.tables, (list, tuple)):
            raise StageIOError("EvaluationReport.tables must be a list")
        norm_tables = tuple(
            t if isinstance(t, EvaluationTable) else EvaluationTable.from_dict(t)
            for t in self.tables
        )
        seen_table_ids: set[str] = set()
        for t in norm_tables:
            if t.id in seen_table_ids:
                raise StageIOError(f"Duplicate table id in EvaluationReport: {t.id!r}")
            seen_table_ids.add(t.id)
        object.__setattr__(self, "tables", norm_tables)

        if not isinstance(self.extensions, Mapping):
            raise StageIOError("EvaluationReport.extensions must be a JSON object")
        check_finite_json(self.extensions, "EvaluationReport.extensions")
        object.__setattr__(self, "extensions", dict(self.extensions))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "title": self.title,
            "evaluator": self.evaluator,
            "metrics": [m.to_dict() for m in self.metrics],
            "charts": [c.to_dict() for c in self.charts],
            "tables": [t.to_dict() for t in self.tables],
        }
        if self.summary is not None:
            out["summary"] = self.summary
        if self.extensions:
            out["extensions"] = dict(self.extensions)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvaluationReportPayload:
        if not isinstance(data, Mapping):
            raise StageIOError("EvaluationReport payload must be a JSON object")
        check_finite_json(data, "EvaluationReport")
        _validate_schema(load_evaluation_report_schema(), dict(data), "EvaluationReport")
        return cls(
            schema=data["schema"],
            schema_version=data["schema_version"],
            title=data["title"],
            evaluator=data["evaluator"],
            summary=data.get("summary"),
            metrics=data["metrics"],
            charts=data["charts"],
            tables=data["tables"],
            extensions=data.get("extensions", {}),
        )


# ---------------------------------------------------------------------------
# Direct JSON file I/O wrappers
# ---------------------------------------------------------------------------


def read_primal_solution(path: Path) -> PrimalSolutionPayload:
    """Read and validate primal_solution.json into PrimalSolutionPayload."""
    target = Path(path)
    header, values_iter = iter_primal_solution_values(target)
    values = tuple(values_iter)
    return PrimalSolutionPayload(
        schema=header.schema,
        schema_version=header.schema_version,
        math_status=header.math_status,
        termination_reason=header.termination_reason,
        has_feasible_solution=header.has_feasible_solution,
        column_count=header.column_count,
        values=values,
        objective_value=header.objective_value,
        best_bound=header.best_bound,
        relative_gap=header.relative_gap,
        receipt=header.receipt,
        extensions=header.extensions,
    )


def write_primal_solution(
    path: Path,
    payload: PrimalSolutionPayload | Mapping[str, Any],
) -> Path:
    """Validate and write primal_solution.json."""
    model = (
        payload
        if isinstance(payload, PrimalSolutionPayload)
        else PrimalSolutionPayload.from_dict(payload)
    )
    return write_primal_solution_streaming(path, model.header, model.values)


def read_engineering_solution(path: Path) -> EngineeringSolutionPayload:
    """Read and validate solution.json (ies.engineering-solution@1.0.0)."""
    return EngineeringSolutionPayload.from_dict(read_json_file(Path(path)))


def write_engineering_solution(
    path: Path,
    payload: EngineeringSolutionPayload | Mapping[str, Any],
) -> Path:
    """Validate and write solution.json (ies.engineering-solution@1.0.0)."""
    model = (
        payload
        if isinstance(payload, EngineeringSolutionPayload)
        else EngineeringSolutionPayload.from_dict(payload)
    )
    return write_json_file(Path(path), model.to_dict())


def read_evaluation_report(path: Path) -> EvaluationReportPayload:
    """Read and validate evaluation_report.json (ies.evaluation-report@1.0.0)."""
    return EvaluationReportPayload.from_dict(read_json_file(Path(path)))


def write_evaluation_report(
    path: Path,
    payload: EvaluationReportPayload | Mapping[str, Any],
) -> Path:
    """Validate and write evaluation_report.json (ies.evaluation-report@1.0.0)."""
    model = (
        payload
        if isinstance(payload, EvaluationReportPayload)
        else EvaluationReportPayload.from_dict(payload)
    )
    return write_json_file(Path(path), model.to_dict())
