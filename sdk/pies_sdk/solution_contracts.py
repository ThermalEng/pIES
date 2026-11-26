"""F1 computation contract models and streaming I/O for primal/engineering solutions and reports."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

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

_PRIMAL_REQUIRED_KEYS = frozenset(
    (
        "schema",
        "schema_version",
        "math_status",
        "termination_reason",
        "has_feasible_solution",
        "column_count",
        "values",
        "objective_value",
        "best_bound",
        "relative_gap",
        "receipt",
    )
)
_PRIMAL_HEADER_REQUIRED_KEYS = _PRIMAL_REQUIRED_KEYS - frozenset(("values",))
_PRIMAL_HEADER_ALLOWED_KEYS = _PRIMAL_HEADER_REQUIRED_KEYS | frozenset(("extensions",))
_PRIMAL_ALLOWED_KEYS = _PRIMAL_REQUIRED_KEYS | frozenset(("extensions",))

_ENG_REQUIRED_KEYS = frozenset(("schema", "schema_version", "solver_summary"))
_ENG_ALLOWED_KEYS = _ENG_REQUIRED_KEYS | frozenset(
    (
        "construction_units",
        "series",
        "system_series",
        "metrics",
        "model_sources",
        "planning_variables",
        "extensions",
    )
)

_REPORT_REQUIRED_KEYS = frozenset(
    ("schema", "schema_version", "title", "evaluator", "metrics", "charts", "tables")
)
_REPORT_ALLOWED_KEYS = _REPORT_REQUIRED_KEYS | frozenset(("summary", "extensions"))


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


def _validate_primal_metadata(
    *,
    schema: str,
    schema_version: str,
    math_status: str,
    termination_reason: str,
    has_feasible_solution: bool,
    column_count: int,
    objective_value: float | None,
    best_bound: float | None,
    relative_gap: float | None,
    receipt: Mapping[str, Any],
    extensions: Mapping[str, Any],
) -> tuple[float | None, float | None, float | None, dict[str, Any], dict[str, Any]]:
    if schema != PRIMAL_SOLUTION_SCHEMA:
        raise StageIOError(f"Invalid PrimalSolution schema: {schema!r}")
    if schema_version != PRIMAL_SOLUTION_VERSION:
        raise StageIOError(f"Invalid PrimalSolution schema_version: {schema_version!r}")
    if math_status not in MATH_STATUSES:
        raise StageIOError(f"Invalid PrimalSolution math_status: {math_status!r}")
    if termination_reason not in SOLVER_TERMINATION_REASONS:
        raise StageIOError(f"Invalid PrimalSolution termination_reason: {termination_reason!r}")
    if not isinstance(has_feasible_solution, bool):
        raise StageIOError("PrimalSolution.has_feasible_solution must be a boolean")
    _require_non_bool_int(column_count, "PrimalSolution.column_count", minimum=0)

    obj = _require_optional_finite_number(objective_value, "PrimalSolution.objective_value")
    bound = _require_optional_finite_number(best_bound, "PrimalSolution.best_bound")
    gap = _require_optional_finite_number(relative_gap, "PrimalSolution.relative_gap")

    if has_feasible_solution:
        if math_status not in _FEASIBLE_MATH_STATUSES:
            raise StageIOError(
                "has_feasible_solution=True requires math_status in "
                f"('solved', 'feasible'), got {math_status!r}"
            )
        if obj is None:
            raise StageIOError("has_feasible_solution=True requires non-null objective_value")
    elif math_status in _FEASIBLE_MATH_STATUSES:
        raise StageIOError(f"has_feasible_solution=False forbids math_status={math_status!r}")

    norm_receipt = _validate_primal_receipt(receipt)
    if not isinstance(extensions, Mapping):
        raise StageIOError("PrimalSolution.extensions must be a JSON object")
    check_finite_json(extensions, "PrimalSolution.extensions")
    return obj, bound, gap, norm_receipt, dict(extensions)


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
        obj, bound, gap, norm_receipt, norm_ext = _validate_primal_metadata(
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
        object.__setattr__(self, "objective_value", obj)
        object.__setattr__(self, "best_bound", bound)
        object.__setattr__(self, "relative_gap", gap)
        object.__setattr__(self, "receipt", norm_receipt)
        object.__setattr__(self, "extensions", norm_ext)

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
        extra = sorted(k for k in data if k not in _PRIMAL_HEADER_ALLOWED_KEYS)
        if extra:
            raise StageIOError(f"PrimalSolution contains unknown fields: {extra!r}")
        missing = sorted(k for k in _PRIMAL_HEADER_REQUIRED_KEYS if k not in data)
        if missing:
            raise StageIOError(f"PrimalSolution missing required fields: {missing!r}")
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
        obj, bound, gap, norm_receipt, norm_ext = _validate_primal_metadata(
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
        object.__setattr__(self, "objective_value", obj)
        object.__setattr__(self, "best_bound", bound)
        object.__setattr__(self, "relative_gap", gap)
        object.__setattr__(self, "receipt", norm_receipt)
        object.__setattr__(self, "extensions", norm_ext)

        if isinstance(self.values, (str, bytes)) or not isinstance(self.values, (list, tuple)):
            raise StageIOError("PrimalSolution.values must be a list or tuple of finite numbers")
        norm_values = tuple(
            _require_finite_number(val, f"PrimalSolution.values[{idx}]")
            for idx, val in enumerate(self.values)
        )
        expected = self.column_count if self.has_feasible_solution else 0
        if len(norm_values) != expected:
            raise StageIOError(
                f"PrimalSolution.values length ({len(norm_values)}) must equal {expected}"
            )
        object.__setattr__(self, "values", norm_values)

    @classmethod
    def _from_validated(
        cls, header: PrimalSolutionHeader, values: tuple[float, ...]
    ) -> PrimalSolutionPayload:
        """Assemble PrimalSolutionPayload from already-validated header and streamed values."""
        obj = object.__new__(cls)
        object.__setattr__(obj, "schema", header.schema)
        object.__setattr__(obj, "schema_version", header.schema_version)
        object.__setattr__(obj, "math_status", header.math_status)
        object.__setattr__(obj, "termination_reason", header.termination_reason)
        object.__setattr__(obj, "has_feasible_solution", header.has_feasible_solution)
        object.__setattr__(obj, "column_count", header.column_count)
        object.__setattr__(obj, "values", values)
        object.__setattr__(obj, "objective_value", header.objective_value)
        object.__setattr__(obj, "best_bound", header.best_bound)
        object.__setattr__(obj, "relative_gap", header.relative_gap)
        object.__setattr__(obj, "receipt", header.receipt)
        object.__setattr__(obj, "extensions", header.extensions)
        return obj

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

    def to_dict(self) -> dict[str, Any]:
        out = self.to_metadata_dict()
        out["values"] = list(self.values)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PrimalSolutionPayload:
        if not isinstance(data, Mapping):
            raise StageIOError("PrimalSolution payload must be a JSON object")
        extra = sorted(k for k in data if k not in _PRIMAL_ALLOWED_KEYS)
        if extra:
            raise StageIOError(f"PrimalSolution contains unknown fields: {extra!r}")
        missing = sorted(k for k in _PRIMAL_REQUIRED_KEYS if k not in data)
        if missing:
            raise StageIOError(f"PrimalSolution missing required fields: {missing!r}")
        return cls(
            schema=data["schema"],
            schema_version=data["schema_version"],
            math_status=data["math_status"],
            termination_reason=data["termination_reason"],
            has_feasible_solution=data["has_feasible_solution"],
            column_count=data["column_count"],
            values=data["values"],
            objective_value=data["objective_value"],
            best_bound=data["best_bound"],
            relative_gap=data["relative_gap"],
            receipt=data["receipt"],
            extensions=data.get("extensions", {}),
        )


def _write_primal_stream(
    path: Path,
    meta: Mapping[str, Any],
    expected_count: int,
    values: Iterable[float],
    *,
    validate_values: bool,
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_target = target.with_name(f".{target.name}.tmp")
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
                num = (
                    _require_finite_number(raw_val, f"values[{count}]")
                    if validate_values
                    else raw_val
                )
                if count > 0:
                    handle.write(", ")
                handle.write(repr(num))
                count += 1
            if validate_values and count != expected_count:
                raise StageIOError(
                    f"PrimalSolution streamed values count ({count}) "
                    f"does not match expected ({expected_count})"
                )
            handle.write("]\n}\n")
        tmp_target.replace(target)
    except Exception:
        tmp_target.unlink(missing_ok=True)
        raise
    return target


def write_primal_solution_streaming(
    path: Path,
    header: PrimalSolutionHeader | Mapping[str, Any],
    values: Iterable[float],
) -> Path:
    """Stream primal_solution.json to disk without materializing all column values in memory."""
    hdr = header if isinstance(header, PrimalSolutionHeader) else PrimalSolutionHeader.from_dict(header)
    return _write_primal_stream(
        path, hdr.to_metadata_dict(), hdr.expected_values_count, values, validate_values=True
    )


_JSON_NUM_CHARS = frozenset("-+0123456789.eE")
_JSON_WORD_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _decode_complete_json_token(
    decoder: json.JSONDecoder,
    handle: Any,
    buf: str,
    pos: int,
    eof: bool,
    label: str,
) -> tuple[Any, str, int, bool]:
    """Decode one JSON token, ensuring numbers/literals are complete across chunk boundaries."""
    while True:
        if pos >= len(buf):
            if not eof:
                more = handle.read(65536)
                if more:
                    buf += more
                    continue
                eof = True
            raise StageIOError(f"Unexpected EOF while reading {label}")
        first = buf[pos]
        if first in _JSON_NUM_CHARS or first in _JSON_WORD_CHARS:
            token_chars = _JSON_NUM_CHARS if first in _JSON_NUM_CHARS else _JSON_WORD_CHARS
            end = pos + 1
            while True:
                while end < len(buf) and buf[end] in token_chars:
                    end += 1
                if end == len(buf) and not eof:
                    more = handle.read(65536)
                    if more:
                        buf += more
                        continue
                    eof = True
                break
        try:
            val, next_pos = decoder.raw_decode(buf, pos)
            return val, buf, next_pos, eof
        except json.JSONDecodeError as exc:
            if not eof:
                more = handle.read(65536)
                if more:
                    buf += more
                    continue
                eof = True
            raise StageIOError(f"Invalid {label}: {exc}") from exc


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
            _refill()
            val, buf, pos, eof = _decode_complete_json_token(
                decoder, handle, buf, pos, eof, f"PrimalSolution {label}"
            )
            return val

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
        count = 0
        with target.open("r", encoding="utf-8") as handle:
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
                val, buf, pos, eof = _decode_complete_json_token(
                    decoder,
                    handle,
                    buf,
                    pos,
                    eof,
                    f"PrimalSolution.values[{count}]",
                )
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
            raise StageIOError(
                f"Invalid solver_summary.termination_reason: {self.termination_reason!r}"
            )
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
        elif self.math_status in _FEASIBLE_MATH_STATUSES:
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
        seen_pvars: set[str] = set()
        for scale in norm_scales:
            if scale.planning_variable_id in seen_pvars:
                raise StageIOError(
                    f"Duplicate planning_variable_id {scale.planning_variable_id!r} "
                    f"in construction_unit {self.unit_id!r}"
                )
            seen_pvars.add(scale.planning_variable_id)
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
        if (
            isinstance(self.values, (str, bytes))
            or not isinstance(self.values, (list, tuple))
            or not self.values
        ):
            raise StageIOError("series.values must be a non-empty list of numbers")
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
            values=data["values"],
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
        if (
            isinstance(self.values, (str, bytes))
            or not isinstance(self.values, (list, tuple))
            or not self.values
        ):
            raise StageIOError("system_series.values must be a non-empty list of numbers")
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
            values=data["values"],
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
    planning_variables: tuple[ConstructionScale, ...] | None = None
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
            seen_units: set[str] = set()
            for unit in norm_units:
                if unit.unit_id in seen_units:
                    raise StageIOError(
                        f"Duplicate unit_id in construction_units: {unit.unit_id!r}"
                    )
                seen_units.add(unit.unit_id)
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

        norm_pvars: tuple[ConstructionScale, ...] | None = None
        if self.planning_variables is not None:
            if isinstance(self.planning_variables, (str, bytes)) or not isinstance(
                self.planning_variables, (list, tuple)
            ):
                raise StageIOError("EngineeringSolution.planning_variables must be a list or null")
            norm_pvars = tuple(
                item if isinstance(item, ConstructionScale) else ConstructionScale.from_dict(item)
                for item in self.planning_variables
            )
            seen_pvars: set[str] = set()
            for item in norm_pvars:
                if item.planning_variable_id in seen_pvars:
                    raise StageIOError(
                        "Duplicate planning_variable_id in planning_variables: "
                        f"{item.planning_variable_id!r}"
                    )
                seen_pvars.add(item.planning_variable_id)
        object.__setattr__(self, "planning_variables", norm_pvars)

        if summary.has_feasible_solution:
            if norm_units is None:
                raise StageIOError("Feasible EngineeringSolution requires 'construction_units'")
            if norm_series is None:
                raise StageIOError("Feasible EngineeringSolution requires 'series'")
            if norm_sys is None:
                raise StageIOError("Feasible EngineeringSolution requires 'system_series'")
            overlap = sorted(set(norm_series) & set(norm_sys))
            if overlap:
                raise StageIOError(
                    f"series and system_series share duplicate series_id keys: {overlap!r}"
                )
            step_lengths: dict[str, int] = {
                **{sid: len(entry.values) for sid, entry in norm_series.items()},
                **{sid: len(entry.values) for sid, entry in norm_sys.items()},
            }
            unique_lengths = set(step_lengths.values())
            if len(unique_lengths) > 1:
                raise StageIOError(
                    "Feasible EngineeringSolution requires equal continuous step count across all "
                    f"series and system_series, got {step_lengths!r}"
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
            if norm_pvars:
                raise StageIOError(
                    "Non-feasible EngineeringSolution cannot contain non-empty 'planning_variables'"
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
        if self.planning_variables is not None:
            out["planning_variables"] = [p.to_dict() for p in self.planning_variables]
        if self.extensions:
            out["extensions"] = dict(self.extensions)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EngineeringSolutionPayload:
        if not isinstance(data, Mapping):
            raise StageIOError("EngineeringSolution payload must be a JSON object")
        extra = sorted(k for k in data if k not in _ENG_ALLOWED_KEYS)
        if extra:
            raise StageIOError(f"EngineeringSolution contains unknown fields: {extra!r}")
        missing = sorted(k for k in _ENG_REQUIRED_KEYS if k not in data)
        if missing:
            raise StageIOError(f"EngineeringSolution missing required fields: {missing!r}")
        return cls(
            schema=data["schema"],
            schema_version=data["schema_version"],
            solver_summary=data["solver_summary"],
            construction_units=data.get("construction_units"),
            series=data.get("series"),
            system_series=data.get("system_series"),
            metrics=data.get("metrics"),
            model_sources=data.get("model_sources"),
            planning_variables=data.get("planning_variables"),
            extensions=data.get("extensions", {}),
        )


@dataclass(frozen=True, slots=True)
class ExpectedConstructionUnit:
    """Frozen expected construction unit and planning variables for completeness checks."""

    unit_id: str
    instance_id: str
    mode: str
    planning_variable_units: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty_str(self.unit_id, "expected_unit.unit_id")
        _require_non_empty_str(self.instance_id, "expected_unit.instance_id")
        if self.mode not in CONSTRUCTION_UNIT_MODES:
            raise StageIOError(f"Invalid expected_unit.mode: {self.mode!r}")
        if not isinstance(self.planning_variable_units, Mapping):
            raise StageIOError("expected_unit.planning_variable_units must be a mapping")
        for pvar, unit in self.planning_variable_units.items():
            _require_non_empty_str(pvar, "expected_unit.planning_variable_id")
            _require_non_empty_str(unit, f"expected_unit.{pvar}.unit")
        object.__setattr__(self, "planning_variable_units", dict(self.planning_variable_units))


@dataclass(frozen=True, slots=True)
class ExpectedDeviceSeries:
    """Frozen expected device step series identity and business unit."""

    instance_id: str
    variable_id: str
    unit: str

    def __post_init__(self) -> None:
        _require_non_empty_str(self.instance_id, "expected_series.instance_id")
        _require_non_empty_str(self.variable_id, "expected_series.variable_id")
        _require_non_empty_str(self.unit, "expected_series.unit")


@dataclass(frozen=True, slots=True)
class ExpectedSystemSeries:
    """Frozen expected system equation series identity and business unit."""

    block: str
    variable: str
    unit: str

    def __post_init__(self) -> None:
        _require_non_empty_str(self.block, "expected_system_series.block")
        _require_non_empty_str(self.variable, "expected_system_series.variable")
        _require_non_empty_str(self.unit, "expected_system_series.unit")


@dataclass(frozen=True, slots=True)
class EngineeringSolutionExpectation:
    """Frozen expectation against which a feasible EngineeringSolutionPayload is checked."""

    expected_steps: int
    construction_units: tuple[ExpectedConstructionUnit, ...] = ()
    series: Mapping[str, ExpectedDeviceSeries] = field(default_factory=dict)
    system_series: Mapping[str, ExpectedSystemSeries] = field(default_factory=dict)
    planning_variables: Mapping[str, str] = field(default_factory=dict)
    objective_unit: str | None = None

    def __post_init__(self) -> None:
        _require_non_bool_int(self.expected_steps, "expectation.expected_steps", minimum=1)
        if self.objective_unit is not None:
            _require_non_empty_str(self.objective_unit, "expectation.objective_unit")
        seen_units: set[str] = set()
        for unit in self.construction_units:
            if unit.unit_id in seen_units:
                raise StageIOError(f"Duplicate expected construction unit_id: {unit.unit_id!r}")
            seen_units.add(unit.unit_id)
        overlap = sorted(set(self.series) & set(self.system_series))
        if overlap:
            raise StageIOError(f"Expected series and system_series overlap: {overlap!r}")
        if not isinstance(self.planning_variables, Mapping):
            raise StageIOError("expectation.planning_variables must be a mapping")
        for pvar, unit in self.planning_variables.items():
            _require_non_empty_str(pvar, "expectation.planning_variable_id")
            _require_non_empty_str(unit, f"expectation.planning_variables.{pvar}.unit")
        object.__setattr__(self, "planning_variables", dict(self.planning_variables))


def validate_engineering_solution_completeness(
    solution: EngineeringSolutionPayload,
    expectation: EngineeringSolutionExpectation,
) -> EngineeringSolutionPayload:
    """Validate feasible EngineeringSolutionPayload completeness against frozen expectations.

    Preserves the ``has_feasible_solution: false`` exemption without requiring
    construction_units or series.
    """
    if not isinstance(solution, EngineeringSolutionPayload):
        raise StageIOError("solution must be EngineeringSolutionPayload")
    if not isinstance(expectation, EngineeringSolutionExpectation):
        raise StageIOError("expectation must be EngineeringSolutionExpectation")
    if not solution.solver_summary.has_feasible_solution:
        return solution

    if (
        expectation.objective_unit is not None
        and solution.solver_summary.objective_unit != expectation.objective_unit
    ):
        raise StageIOError(
            f"Expected objective_unit {expectation.objective_unit!r}, "
            f"got {solution.solver_summary.objective_unit!r}"
        )

    actual_units = {u.unit_id: u for u in (solution.construction_units or ())}
    expected_units = {u.unit_id: u for u in expectation.construction_units}
    if set(actual_units) != set(expected_units):
        missing_u = sorted(set(expected_units) - set(actual_units))
        extra_u = sorted(set(actual_units) - set(expected_units))
        raise StageIOError(
            f"construction_units mismatch: missing={missing_u!r}, extra={extra_u!r}"
        )
    for uid, exp_u in expected_units.items():
        act_u = actual_units[uid]
        if act_u.instance_id != exp_u.instance_id or act_u.mode != exp_u.mode:
            raise StageIOError(
                f"construction_unit {uid!r} metadata mismatch: "
                f"expected ({exp_u.instance_id!r}, {exp_u.mode!r}), "
                f"got ({act_u.instance_id!r}, {act_u.mode!r})"
            )
        act_scales = {s.planning_variable_id: s for s in act_u.scales}
        if set(act_scales) != set(exp_u.planning_variable_units):
            missing_p = sorted(set(exp_u.planning_variable_units) - set(act_scales))
            extra_p = sorted(set(act_scales) - set(exp_u.planning_variable_units))
            raise StageIOError(
                f"construction_unit {uid!r} planning variables mismatch: "
                f"missing={missing_p!r}, extra={extra_p!r}"
            )
        for pvar, exp_punit in exp_u.planning_variable_units.items():
            if act_scales[pvar].unit != exp_punit:
                raise StageIOError(
                    f"construction_unit {uid!r} variable {pvar!r} unit mismatch: "
                    f"expected {exp_punit!r}, got {act_scales[pvar].unit!r}"
                )

    resolved_pvars: dict[str, str] = {
        sc.planning_variable_id: sc.unit
        for act_u in actual_units.values()
        for sc in act_u.scales
    }
    for pvar_entry in solution.planning_variables or ():
        resolved_pvars[pvar_entry.planning_variable_id] = pvar_entry.unit
    for pname, exp_punit in sorted(expectation.planning_variables.items()):
        actual_punit = resolved_pvars.get(pname)
        if actual_punit is None:
            raise StageIOError(
                f"EngineeringSolution missing required planning variable {pname!r}"
            )
        if actual_punit != exp_punit:
            raise StageIOError(
                f"planning variable {pname!r} unit mismatch: "
                f"expected {exp_punit!r}, got {actual_punit!r}"
            )

    actual_series = dict(solution.series or {})
    if set(actual_series) != set(expectation.series):
        missing_s = sorted(set(expectation.series) - set(actual_series))
        extra_s = sorted(set(actual_series) - set(expectation.series))
        raise StageIOError(f"series mismatch: missing={missing_s!r}, extra={extra_s!r}")
    for sid, exp_s in expectation.series.items():
        act_s = actual_series[sid]
        if (
            act_s.instance_id != exp_s.instance_id
            or act_s.variable_id != exp_s.variable_id
            or act_s.unit != exp_s.unit
        ):
            raise StageIOError(f"series {sid!r} identity/unit mismatch against frozen expectation")
        if len(act_s.values) != expectation.expected_steps:
            raise StageIOError(
                f"series {sid!r} length ({len(act_s.values)}) "
                f"does not match expected_steps ({expectation.expected_steps})"
            )

    actual_sys = dict(solution.system_series or {})
    if set(actual_sys) != set(expectation.system_series):
        missing_ss = sorted(set(expectation.system_series) - set(actual_sys))
        extra_ss = sorted(set(actual_sys) - set(expectation.system_series))
        raise StageIOError(
            f"system_series mismatch: missing={missing_ss!r}, extra={extra_ss!r}"
        )
    for sid, exp_ss in expectation.system_series.items():
        act_ss = actual_sys[sid]
        if (
            act_ss.block != exp_ss.block
            or act_ss.variable != exp_ss.variable
            or act_ss.unit != exp_ss.unit
        ):
            raise StageIOError(
                f"system_series {sid!r} identity/unit mismatch against frozen expectation"
            )
        if len(act_ss.values) != expectation.expected_steps:
            raise StageIOError(
                f"system_series {sid!r} length ({len(act_ss.values)}) "
                f"does not match expected_steps ({expectation.expected_steps})"
            )

    return solution


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
            values=data["values"],
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
        if "chart_type" in data and data["chart_type"] is None:
            raise StageIOError("chart.chart_type cannot be null when present")
        if "x_values" in data and data["x_values"] is None:
            raise StageIOError("chart.x_values cannot be null when present")
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
        extra = sorted(k for k in data if k not in _REPORT_ALLOWED_KEYS)
        if extra:
            raise StageIOError(f"EvaluationReport contains unknown fields: {extra!r}")
        missing = sorted(k for k in _REPORT_REQUIRED_KEYS if k not in data)
        if missing:
            raise StageIOError(f"EvaluationReport missing required fields: {missing!r}")
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
    return PrimalSolutionPayload._from_validated(header, values)


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
    expected = model.column_count if model.has_feasible_solution else 0
    return _write_primal_stream(
        path, model.to_metadata_dict(), expected, model.values, validate_values=False
    )


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
