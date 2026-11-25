"""Universal variable_map (input/parameters.json + streaming input/parameters.jsonl)."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from jsonschema import Draft202012Validator

from pies_sdk.contracts import check_finite_json, check_relative_path, read_json_file, write_json_file
from pies_sdk.errors import StageIOError

VARIABLE_MAP_SCHEMA = "ies.variable-map"
VARIABLE_MAP_VERSION = "1.0.0"
VARIABLE_MAP_HEADER_FILENAME = "parameters.json"
VARIABLE_MAP_RECORDS_PATH = "input/parameters.jsonl"

VARIABLE_MAP_RECORD_TAGS = (
    "column_names",
    "row_names",
    "epigraph",
    "epigraph_flows",
    "epigraph_end",
    "expression",
    "terms",
    "expression_end",
    "units",
    "component",
    "facts",
    "series",
    "positions",
    "series_end",
    "sources",
    "source_group",
    "targets",
    "indices",
    "source_group_end",
)

_HEADER_SCHEMA_PATH = Path(__file__).parent / "schema" / "variable-map-header-1.0.0.schema.json"
_PRECISIONS = frozenset(("low", "medium", "high"))


def load_variable_map_header_schema() -> dict[str, Any]:
    """Load the bundled Draft 2020-12 JSON Schema for ies.variable-map header."""
    return json.loads(_HEADER_SCHEMA_PATH.read_text(encoding="utf-8"))


def _reject_json_constant(token: str) -> NoReturn:
    raise StageIOError(f"Non-finite JSON constant in variable_map: {token!r}")


def _require_non_bool_int(value: object, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StageIOError(f"{name} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise StageIOError(f"{name} must be >= {minimum}, got {value!r}")
    return value


def _require_finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StageIOError(f"{name} must be a finite number, got {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise StageIOError(f"{name} forbids non-finite numeric value: {value!r}")
    return result


@dataclass(frozen=True, slots=True)
class VariableMapHeader:
    """Metadata header (input/parameters.json) for universal variable_map."""

    assembly: Mapping[str, Any]
    generator: str
    column_count: int
    objective_offset: float
    objective_unit: str
    outputs: tuple[str, ...]
    precision: str
    seed: int | None
    records: str = VARIABLE_MAP_RECORDS_PATH
    schema: str | None = None
    schema_version: str | None = None
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema is not None and self.schema != VARIABLE_MAP_SCHEMA:
            raise StageIOError(f"Invalid variable_map schema: {self.schema!r}")
        if self.schema_version is not None and self.schema_version != VARIABLE_MAP_VERSION:
            raise StageIOError(f"Invalid variable_map schema_version: {self.schema_version!r}")
        if not isinstance(self.assembly, Mapping):
            raise StageIOError("variable_map 'assembly' must be a JSON object")
        asm_keys = set(self.assembly)
        if asm_keys != {"id", "schema_version"}:
            raise StageIOError(
                "variable_map.assembly must have exact keys ['id', 'schema_version'], "
                f"got {sorted(asm_keys)!r}"
            )
        asm_id = self.assembly["id"]
        if asm_id is not None and (not isinstance(asm_id, str) or not asm_id):
            raise StageIOError("variable_map.assembly.id must be a non-empty string or null")
        asm_ver = self.assembly["schema_version"]
        if not isinstance(asm_ver, str) or not asm_ver:
            raise StageIOError("variable_map.assembly.schema_version must be a non-empty string")
        object.__setattr__(self, "assembly", {"id": asm_id, "schema_version": asm_ver})

        if not isinstance(self.generator, str) or not self.generator:
            raise StageIOError("variable_map requires non-empty 'generator'")
        check_relative_path(self.records, "variable_map.records")
        _require_non_bool_int(self.column_count, "variable_map.column_count", minimum=0)
        offset = _require_finite_number(self.objective_offset, "variable_map.objective_offset")
        object.__setattr__(self, "objective_offset", offset)
        if not isinstance(self.objective_unit, str) or not self.objective_unit:
            raise StageIOError("variable_map requires non-empty 'objective_unit'")

        if isinstance(self.outputs, str) or not isinstance(self.outputs, (list, tuple)) or not self.outputs:
            raise StageIOError("variable_map requires non-empty 'outputs' list")
        norm_outputs: list[str] = []
        for idx, item in enumerate(self.outputs):
            if not isinstance(item, str) or not item:
                raise StageIOError(f"variable_map.outputs[{idx}] must be a non-empty string")
            norm_outputs.append(item)
        object.__setattr__(self, "outputs", tuple(norm_outputs))

        if self.precision not in _PRECISIONS:
            raise StageIOError(f"Invalid variable_map.precision: {self.precision!r}")
        if self.seed is not None:
            _require_non_bool_int(self.seed, "variable_map.seed")

        if not isinstance(self.extensions, Mapping):
            raise StageIOError("variable_map.extensions must be a JSON object")
        check_finite_json(self.extensions, "variable_map.extensions")
        object.__setattr__(self, "extensions", dict(self.extensions))

        raw_dict = self.to_dict()
        validator = Draft202012Validator(load_variable_map_header_schema())
        errors = sorted(validator.iter_errors(raw_dict), key=lambda e: list(e.absolute_path))
        if errors:
            first = errors[0]
            loc = "/".join(str(p) for p in first.absolute_path) or "<root>"
            raise StageIOError(f"variable_map header invalid [{loc}]: {first.message}")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "assembly": dict(self.assembly),
            "generator": self.generator,
            "records": self.records,
            "column_count": self.column_count,
            "objective_offset": self.objective_offset,
            "objective_unit": self.objective_unit,
            "outputs": list(self.outputs),
            "precision": self.precision,
            "seed": self.seed,
        }
        if self.schema is not None:
            out["schema"] = self.schema
        if self.schema_version is not None:
            out["schema_version"] = self.schema_version
        if self.extensions:
            out["extensions"] = dict(self.extensions)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VariableMapHeader:
        if not isinstance(data, Mapping):
            raise StageIOError("variable_map header must be a JSON object")
        check_finite_json(data, "variable_map")
        validator = Draft202012Validator(load_variable_map_header_schema())
        errors = sorted(validator.iter_errors(dict(data)), key=lambda e: list(e.absolute_path))
        if errors:
            first = errors[0]
            loc = "/".join(str(p) for p in first.absolute_path) or "<root>"
            raise StageIOError(f"variable_map header invalid [{loc}]: {first.message}")
        return cls(
            assembly=data["assembly"],
            generator=data["generator"],
            records=data["records"],
            column_count=data["column_count"],
            objective_offset=data["objective_offset"],
            objective_unit=data["objective_unit"],
            outputs=tuple(data["outputs"]),
            precision=data["precision"],
            seed=data["seed"],
            schema=data.get("schema"),
            schema_version=data.get("schema_version"),
            extensions=data.get("extensions", {}),
        )


class _RecordStreamValidator:
    """O(1)-memory state machine validating NDJSON record order and 0-based indices."""

    def __init__(self, *, column_count: int | None = None) -> None:
        if column_count is not None:
            _require_non_bool_int(column_count, "column_count", minimum=0)
        self.column_count = column_count
        self.stage = 0
        self.block: str | None = None
        self.col_names_count = 0
        self.row_names_count = 0
        self.expr_count = 0
        self.facts_seen = False
        self.sources_seen = False
        self.seen_series_ids: set[str] = set()
        self.group_role: str | None = None
        self.group_compressed = False

    def _check_col(self, value: object, context: str) -> int:
        col = _require_non_bool_int(value, context, minimum=0)
        if self.column_count is not None and col >= self.column_count:
            raise StageIOError(
                f"{context} column index {col} out of range [0, {self.column_count})"
            )
        return col

    def _check_row(self, value: object, context: str) -> int:
        row = _require_non_bool_int(value, context, minimum=0)
        if self.row_names_count > 0 and row >= self.row_names_count:
            raise StageIOError(
                f"{context} row index {row} out of range [0, {self.row_names_count})"
            )
        return row

    def _advance_stage(self, target_stage: int, tag: str) -> None:
        if self.block is not None:
            raise StageIOError(f"Record {tag!r} cannot appear inside open {self.block!r} block")
        if target_stage < self.stage:
            raise StageIOError(f"Record {tag!r} appears out of documented section order")
        self.stage = target_stage

    def validate(self, record: Any) -> list[Any]:
        if not isinstance(record, (list, tuple)) or not record:
            raise StageIOError("variable_map NDJSON record must be a non-empty JSON array")
        tag = record[0]
        if not isinstance(tag, str) or tag not in VARIABLE_MAP_RECORD_TAGS:
            raise StageIOError(f"Unknown variable_map record tag: {tag!r}")
        check_finite_json(record, f"variable_map[{tag}]")

        if tag == "column_names":
            self._advance_stage(0, tag)
            self._expect_len(record, 2, tag)
            names = self._require_str_list(record[1], "column_names")
            self.col_names_count += len(names)
            return ["column_names", names]

        if tag == "row_names":
            self._advance_stage(1, tag)
            self._expect_len(record, 2, tag)
            names = self._require_str_list(record[1], "row_names")
            self.row_names_count += len(names)
            return ["row_names", names]

        if tag == "epigraph":
            self._advance_stage(2, tag)
            self._expect_len(record, 2, tag)
            peak = self._check_col(record[1], "epigraph.peak_position")
            self.block = "epigraph"
            return ["epigraph", peak]

        if tag == "epigraph_flows":
            if self.block != "epigraph":
                raise StageIOError("'epigraph_flows' must appear inside an open 'epigraph' block")
            self._expect_len(record, 2, tag)
            cols = self._check_col_list(record[1], "epigraph_flows")
            return ["epigraph_flows", cols]

        if tag == "epigraph_end":
            if self.block != "epigraph":
                raise StageIOError("'epigraph_end' without matching 'epigraph'")
            self._expect_len(record, 1, tag)
            self.block = None
            return ["epigraph_end"]

        if tag == "expression":
            self._advance_stage(3, tag)
            self._expect_len(record, 3, tag)
            expr_id = _require_non_bool_int(record[1], "expression.id", minimum=0)
            if expr_id != self.expr_count:
                raise StageIOError(
                    "expression.id must be strictly 0-based sequential "
                    f"(expected {self.expr_count}, got {expr_id})"
                )
            constant = _require_finite_number(record[2], "expression.constant")
            self.block = "expression"
            return ["expression", expr_id, constant]

        if tag == "terms":
            if self.block != "expression":
                raise StageIOError("'terms' must appear inside an open 'expression' block")
            self._expect_len(record, 2, tag)
            if not isinstance(record[1], list):
                raise StageIOError("terms payload must be a list of [col, coeff] pairs")
            norm_terms: list[list[Any]] = []
            for idx, pair in enumerate(record[1]):
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    raise StageIOError(f"terms[{idx}] must be a [col, coeff] pair")
                col = self._check_col(pair[0], f"terms[{idx}][0]")
                coeff = _require_finite_number(pair[1], f"terms[{idx}][1]")
                norm_terms.append([col, coeff])
            return ["terms", norm_terms]

        if tag == "expression_end":
            if self.block != "expression":
                raise StageIOError("'expression_end' without matching 'expression'")
            self._expect_len(record, 1, tag)
            self.block = None
            self.expr_count += 1
            return ["expression_end"]

        if tag == "units":
            self._advance_stage(4, tag)
            self._expect_len(record, 2, tag)
            if not isinstance(record[1], list):
                raise StageIOError("units payload must be a list of [col, unit] pairs")
            norm_units: list[list[Any]] = []
            for idx, pair in enumerate(record[1]):
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    raise StageIOError(f"units[{idx}] must be a [col, unit] pair")
                col = self._check_col(pair[0], f"units[{idx}][0]")
                unit = pair[1]
                if not isinstance(unit, str) or not unit:
                    raise StageIOError(f"units[{idx}][1] must be a non-empty unit string")
                norm_units.append([col, unit])
            return ["units", norm_units]

        if tag == "component":
            self._advance_stage(5, tag)
            self._expect_len(record, 5, tag)
            identity, unit, caliber, expr_id = record[1], record[2], record[3], record[4]
            for label, val in (("identity", identity), ("unit", unit), ("caliber", caliber)):
                if not isinstance(val, str) or not val:
                    raise StageIOError(f"component.{label} must be a non-empty string")
            eid = _require_non_bool_int(expr_id, "component.expression_id", minimum=0)
            if eid >= self.expr_count:
                raise StageIOError(
                    f"component.expression_id {eid} references undefined expression (count={self.expr_count})"
                )
            return ["component", identity, unit, caliber, eid]

        if tag == "facts":
            self._advance_stage(6, tag)
            if self.facts_seen:
                raise StageIOError("Multiple 'facts' records are forbidden")
            self._expect_len(record, 2, tag)
            if not isinstance(record[1], Mapping):
                raise StageIOError("'facts' payload must be a JSON object")
            self.facts_seen = True
            return ["facts", dict(record[1])]

        if tag == "series":
            self._advance_stage(7, tag)
            self._expect_len(record, 2, tag)
            meta = record[1]
            if not isinstance(meta, Mapping):
                raise StageIOError("'series' metadata must be a JSON object")
            sid = meta.get("series_id")
            if not isinstance(sid, str) or not sid:
                raise StageIOError("series.series_id must be a non-empty string")
            if sid in self.seen_series_ids:
                raise StageIOError(f"Duplicate series_id in variable_map: {sid!r}")
            unit = meta.get("unit")
            if not isinstance(unit, str) or not unit:
                raise StageIOError(f"series {sid!r} requires non-empty 'unit'")
            is_system = "block" in meta or "variable" in meta
            is_device = "instance_id" in meta or "variable_id" in meta
            if is_system == is_device:
                raise StageIOError(
                    f"series {sid!r} must declare either (instance_id, variable_id) or (block, variable)"
                )
            req = ("block", "variable") if is_system else ("instance_id", "variable_id")
            for k in req:
                if not isinstance(meta.get(k), str) or not meta[k]:
                    raise StageIOError(f"series {sid!r} requires non-empty {k!r}")
            self.seen_series_ids.add(sid)
            self.block = "series"
            return ["series", dict(meta)]

        if tag == "positions":
            if self.block != "series":
                raise StageIOError("'positions' must appear inside an open 'series' block")
            self._expect_len(record, 2, tag)
            cols = self._check_col_list(record[1], "positions")
            return ["positions", cols]

        if tag == "series_end":
            if self.block != "series":
                raise StageIOError("'series_end' without matching 'series'")
            self._expect_len(record, 1, tag)
            self.block = None
            return ["series_end"]

        if tag == "sources":
            self._advance_stage(8, tag)
            if self.sources_seen:
                raise StageIOError("Multiple 'sources' records are forbidden")
            self._expect_len(record, 2, tag)
            if not isinstance(record[1], Mapping):
                raise StageIOError("'sources' payload must be a JSON object")
            self.sources_seen = True
            return ["sources", dict(record[1])]

        if tag == "source_group":
            self._advance_stage(8, tag)
            if not self.sources_seen:
                raise StageIOError("'source_group' requires a preceding 'sources' record")
            self._expect_len(record, 3, tag)
            meta, compressed = record[1], record[2]
            if not isinstance(meta, Mapping):
                raise StageIOError("source_group metadata must be a JSON object")
            for req_key in ("source", "role", "axis"):
                val = meta.get(req_key)
                if not isinstance(val, str) or not val:
                    raise StageIOError(f"source_group.{req_key} must be a non-empty string")
            role = meta["role"]
            if not role.startswith(("column:", "row:")):
                raise StageIOError(f"source_group.role must start with 'column:' or 'row:', got {role!r}")
            if compressed is not None:
                if not isinstance(compressed, Mapping) or set(compressed) != {"start", "stride", "count"}:
                    raise StageIOError(
                        "source_group compressed indices must be null or {start, stride, count}"
                    )
                _require_non_bool_int(compressed["start"], "source_group.indices.start")
                _require_non_bool_int(compressed["stride"], "source_group.indices.stride")
                _require_non_bool_int(compressed["count"], "source_group.indices.count", minimum=0)
            self.block = "source_group"
            self.group_role = role
            self.group_compressed = compressed is not None
            return ["source_group", dict(meta), dict(compressed) if compressed is not None else None]

        if tag == "targets":
            if self.block != "source_group" or self.group_role is None:
                raise StageIOError("'targets' must appear inside an open 'source_group' block")
            self._expect_len(record, 2, tag)
            if not isinstance(record[1], list):
                raise StageIOError("targets payload must be a list of 0-based positions")
            if self.group_role.startswith("column:"):
                positions = self._check_col_list(record[1], "targets")
            else:
                positions = [self._check_row(item, f"targets[{i}]") for i, item in enumerate(record[1])]
            return ["targets", positions]

        if tag == "indices":
            if self.block != "source_group":
                raise StageIOError("'indices' must appear inside an open 'source_group' block")
            if self.group_compressed:
                raise StageIOError("'indices' cannot appear when source_group uses compressed indices")
            self._expect_len(record, 2, tag)
            if not isinstance(record[1], list):
                raise StageIOError("indices payload must be a list of integers")
            indices = [_require_non_bool_int(item, f"indices[{i}]") for i, item in enumerate(record[1])]
            return ["indices", indices]

        if tag == "source_group_end":
            if self.block != "source_group":
                raise StageIOError("'source_group_end' without matching 'source_group'")
            self._expect_len(record, 1, tag)
            self.block = None
            self.group_role = None
            self.group_compressed = False
            return ["source_group_end"]

        raise StageIOError(f"Unhandled variable_map record tag: {tag!r}")

    def finalize(self) -> None:
        if self.block is not None:
            raise StageIOError(f"Unclosed {self.block!r} block at end of variable_map records")
        if self.column_count is not None and self.col_names_count > 0:
            if self.col_names_count != self.column_count:
                raise StageIOError(
                    f"variable_map column_names count ({self.col_names_count}) "
                    f"does not match header column_count ({self.column_count})"
                )

    @staticmethod
    def _expect_len(record: Sequence[Any], expected: int, tag: str) -> None:
        if len(record) != expected:
            raise StageIOError(f"Record {tag!r} expects {expected} elements, got {len(record)}")

    @staticmethod
    def _require_str_list(val: object, label: str) -> list[str]:
        if not isinstance(val, list):
            raise StageIOError(f"{label} payload must be a list of strings")
        out: list[str] = []
        for idx, item in enumerate(val):
            if not isinstance(item, str) or not item:
                raise StageIOError(f"{label}[{idx}] must be a non-empty string")
            out.append(item)
        return out

    def _check_col_list(self, val: object, label: str) -> list[int]:
        if not isinstance(val, list):
            raise StageIOError(f"{label} payload must be a list of 0-based column indices")
        return [self._check_col(item, f"{label}[{idx}]") for idx, item in enumerate(val)]


class VariableMapWriter:
    """Streaming NDJSON writer for variable_map (input/parameters.jsonl)."""

    def __init__(
        self,
        path: Path,
        *,
        column_count: int | None = None,
        truncate: bool = True,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._validator = _RecordStreamValidator(column_count=column_count)
        mode = "w" if truncate else "a"
        self._handle = self.path.open(mode, encoding="utf-8", newline="\n")
        self._closed = False

    def write_record(self, record: Sequence[Any]) -> None:
        if self._closed:
            raise StageIOError("Cannot write to closed VariableMapWriter")
        validated = self._validator.validate(record)
        line = json.dumps(
            validated, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
        )
        self._handle.write(line + "\n")

    def write_records(self, records: Iterable[Sequence[Any]]) -> None:
        for record in records:
            self.write_record(record)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._handle.flush()
            finally:
                self._handle.close()
            self._validator.finalize()

    def __enter__(self) -> VariableMapWriter:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._handle.flush()
            finally:
                self._handle.close()
            if exc_type is None:
                self._validator.finalize()


def iter_variable_map_records(
    path: Path,
    *,
    column_count: int | None = None,
) -> Iterator[list[Any]]:
    """Stream and validate variable_map NDJSON records (input/parameters.jsonl)."""
    target = Path(path)
    if not target.is_file():
        raise StageIOError(f"variable_map records file does not exist: {target}")
    validator = _RecordStreamValidator(column_count=column_count)
    with target.open("r", encoding="utf-8") as handle:
        for lineno, raw_line in enumerate(handle, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped, parse_constant=_reject_json_constant)
            except json.JSONDecodeError as exc:
                raise StageIOError(f"Malformed JSON in {target}:{lineno}: {exc}") from exc
            yield validator.validate(parsed)
    validator.finalize()


def read_variable_map_header(path: Path) -> VariableMapHeader:
    """Read and validate input/parameters.json metadata header."""
    return VariableMapHeader.from_dict(read_json_file(Path(path)))


def write_variable_map_header(
    path: Path,
    header: VariableMapHeader | Mapping[str, Any],
) -> Path:
    """Validate and write input/parameters.json metadata header."""
    model = header if isinstance(header, VariableMapHeader) else VariableMapHeader.from_dict(header)
    return write_json_file(Path(path), model.to_dict())


def read_variable_map(bundle_dir: Path) -> tuple[VariableMapHeader, list[list[Any]]]:
    """Read variable_map header + validated NDJSON records from a bundle directory."""
    root = Path(bundle_dir)
    header_path = root / "input" / VARIABLE_MAP_HEADER_FILENAME
    if not header_path.is_file():
        header_path = root / VARIABLE_MAP_HEADER_FILENAME
    header = read_variable_map_header(header_path)
    records_path = root / header.records
    if not records_path.is_file() and (root / Path(header.records).name).is_file():
        records_path = root / Path(header.records).name
    records = list(iter_variable_map_records(records_path, column_count=header.column_count))
    return header, records


def write_variable_map(
    bundle_dir: Path,
    header: VariableMapHeader | Mapping[str, Any],
    records: Iterable[Sequence[Any]],
) -> tuple[Path, Path]:
    """Write variable_map header (input/parameters.json) and stream NDJSON records."""
    root = Path(bundle_dir)
    model = header if isinstance(header, VariableMapHeader) else VariableMapHeader.from_dict(header)
    header_path = write_variable_map_header(root / "input" / VARIABLE_MAP_HEADER_FILENAME, model)
    records_rel = Path(model.records)
    records_path = root / records_rel
    with VariableMapWriter(records_path, column_count=model.column_count) as writer:
        writer.write_records(records)
    return header_path, records_path
