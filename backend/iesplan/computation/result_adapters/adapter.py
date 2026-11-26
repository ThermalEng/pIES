"""Universal F2 ResultAdapter running streaming restoration in an isolated spawn subprocess."""

from __future__ import annotations

import json
import math
import multiprocessing
import resource
import shutil
import tempfile
import time
from array import array
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from iesplan.computation.bundle import PRIMAL_SOLUTION_OUTPUT_PATH
from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.generators.highs_milp import (
    PARAMETERS_PATH,
    VARIABLE_MAP_SCHEMA,
    VARIABLE_MAP_VERSION,
)
from iesplan.computation.protocols import (
    MATH_STATUSES,
    ComputeResult,
    ExecutionReceipt,
    SolverBundle,
    StopProbe,
    check_finite_json,
    check_provider_ref,
    read_stop_reason,
)
from iesplan.core.content import CHUNK_BYTES, Content, MemoryContent, SizedContent, json_chunks

PRIMAL_SOLUTION_SCHEMA = "ies.primal-solution"
PRIMAL_SOLUTION_VERSION = "1.0.0"

ENGINEERING_SOLUTION_SCHEMA = "ies.engineering-solution"
ENGINEERING_SOLUTION_VERSION = "1.0.0"

DEFAULT_MEMORY_LIMIT_MB = 1024
DEFAULT_TIMEOUT_SECONDS = 300.0

_PRECISIONS = frozenset(("low", "medium", "high"))
_SOLVE_DOMAINS = frozenset(("original", "valid_relaxation", "restricted"))
_SOLVER_TERMINATION_REASONS = frozenset(
    ("optimal", "time_limit", "infeasible", "unbounded", "not_solved")
)
_VARIABLE_MAP_RECORD_TAGS = frozenset(
    (
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
)
_HEADER_REQUIRED_KEYS = frozenset(
    (
        "schema",
        "schema_version",
        "assembly",
        "generator",
        "records",
        "column_count",
        "objective_offset",
        "objective_scale",
        "objective_unit",
        "outputs",
        "precision",
        "seed",
    )
)
_HEADER_ALLOWED_KEYS = _HEADER_REQUIRED_KEYS | frozenset(("extensions",))
_PRIMAL_REQUIRED_KEYS = frozenset(
    (
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
    )
)
_PRIMAL_ALLOWED_KEYS = _PRIMAL_REQUIRED_KEYS | frozenset(("extensions",))
_PRIMAL_RECEIPT_ALLOWED_KEYS = frozenset(
    (
        "solver",
        "started_at",
        "ended_at",
        "elapsed_seconds",
        "solve_domain",
        "original_model_verified",
        "solver_diagnostics",
        "extensions",
    )
)
_DEVICE_SERIES_CORE_KEYS = frozenset(
    ("series_id", "instance_id", "variable_id", "unit", "scale", "offset")
)
_SYSTEM_SERIES_CORE_KEYS = frozenset(
    ("series_id", "block", "variable", "unit", "scale", "offset")
)
_UNIT_ENTRY_ALLOWED_KEYS = frozenset(("unit_id", "instance_id", "mode", "built", "scales"))
_SCALE_ENTRY_ALLOWED_KEYS = frozenset(("planning_variable_id", "value", "unit", "scale", "offset"))
_STREAM_CHUNK_BYTES = CHUNK_BYTES
_JSON_NUM_CHARS = frozenset("-+0123456789.eE")
_JSON_WORD_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


@dataclass(frozen=True, slots=True)
class FileContent:
    """Lazy chunk-streaming ``SizedContent`` backed by a durable filesystem path."""

    path: Path
    size_bytes: int

    def iter_chunks(self) -> Iterator[bytes]:
        with self.path.open("rb") as handle:
            while chunk := handle.read(CHUNK_BYTES):
                yield chunk


@dataclass(frozen=True, slots=True)
class RestorationOutcome:
    """Outcome from bounded-subprocess restoration carrying durable references."""

    compute_result: ComputeResult
    solution_path: Path
    solution_content: SizedContent
    solver_summary: Mapping[str, Any]
    child_pid: int | None

    def load_engineering_solution(self) -> dict[str, Any]:
        """Explicit convenience reader for tests/callers inspecting small solutions."""
        return json.loads(self.solution_path.read_text(encoding="utf-8"))


def _fail(message: str, *, reason: str = "result-protocol-failure") -> NoReturn:
    raise ComputationUnavailableError(f"结果还原失败: {message}", reason=reason)


def _reject_json_constant(token: str) -> NoReturn:
    _fail(f"禁止非有限 JSON 常量 {token!r}")


def _require_non_bool_int(value: object, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{label} 须为整数，得到 {value!r}")
    if minimum is not None and value < minimum:
        _fail(f"{label} 须 >= {minimum}，得到 {value!r}")
    return value


def _require_non_empty_str(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{label} 须为非空字符串，得到 {value!r}")
    return value


def _require_finite_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{label} 须为有限数值，得到 {value!r}")
    num = float(value)
    if not math.isfinite(num):
        _fail(f"{label} 非有限数值: {value!r}")
    return num


def _require_optional_finite_float(value: object, label: str) -> float | None:
    if value is None:
        return None
    return _require_finite_float(value, label)


def _require_nonzero_scale(value: object, label: str) -> float:
    scale = _require_finite_float(value, label)
    if scale == 0.0:
        _fail(f"{label} 不能为 0")
    return scale


def _require_provider_ref(value: object, label: str) -> str:
    try:
        return check_provider_ref(value, label)
    except ValueError as exc:
        _fail(str(exc))


def _check_relative_path(path: object, label: str) -> str:
    text = _require_non_empty_str(path, label)
    if text.startswith("/") or "\\" in text:
        _fail(f"{label} 路径非法: {path!r}")
    segments = text.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        _fail(f"{label} 路径非法: {path!r}")
    return text


def _apply_affine(raw_value: float, *, scale: float, offset: float, label: str) -> float:
    val = scale * raw_value + offset
    if not math.isfinite(val):
        _fail(f"{label} 线性换算后非有限")
    return val


def _validate_resource_budgets(
    memory_limit_mb: int | None,
    timeout_seconds: float | None,
) -> tuple[int, float]:
    mem = DEFAULT_MEMORY_LIMIT_MB if memory_limit_mb is None else memory_limit_mb
    if isinstance(mem, bool) or not isinstance(mem, int) or mem <= 0:
        raise ValueError(f"memory_limit_mb 须为正整数，得到 {memory_limit_mb!r}")
    tmo = DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    if (
        isinstance(tmo, bool)
        or not isinstance(tmo, (int, float))
        or not math.isfinite(float(tmo))
        or float(tmo) <= 0.0
    ):
        raise ValueError(f"timeout_seconds 须为有限正数，得到 {timeout_seconds!r}")
    return int(mem), float(tmo)


def _execution_status_from_receipt(status: str, termination_reason: str) -> str:
    if status == "succeeded":
        return "completed"
    if status == "process_failed":
        return "resource_exhausted" if termination_reason == "oom" else "failed"
    mapping = {
        "policy_rejected": "rejected",
        "rejected": "rejected",
        "failed": "failed",
        "resource_exhausted": "resource_exhausted",
        "output_protocol_failed": "protocol_failed",
        "protocol_failed": "protocol_failed",
        "timed_out": "timed_out",
        "cancelled": "cancelled",
        "fenced": "fenced",
    }
    if status not in mapping:
        _fail(f"未知回执状态: {status!r}")
    return mapping[status]


def _non_feasible_solution(
    *,
    math_status: str,
    termination_reason: str,
    objective_unit: str | None,
    solver_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "termination_reason": termination_reason,
        "math_status": math_status,
        "has_feasible_solution": False,
        "objective_value": None,
        "objective_unit": objective_unit,
        "best_bound": None,
        "relative_gap": None,
    }
    if solver_diagnostics:
        summary["solver_diagnostics"] = solver_diagnostics
    return {
        "schema": ENGINEERING_SOLUTION_SCHEMA,
        "schema_version": ENGINEERING_SOLUTION_VERSION,
        "solver_summary": summary,
        "extensions": {},
    }


def _check_json_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} 须为 JSON 对象")
    try:
        check_finite_json(value, label)
    except ValueError as exc:
        _fail(str(exc))
    return dict(value)


def _read_header(parameters_path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(parameters_path.read_text(encoding="utf-8"), parse_constant=_reject_json_constant)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"读取参数头 {parameters_path} 失败: {exc}")
    if not isinstance(doc, Mapping):
        _fail("parameters.json 须为 JSON 对象")

    extra = sorted(set(doc) - _HEADER_ALLOWED_KEYS)
    if extra:
        _fail(f"parameters.json 含未知字段: {extra!r}")
    missing = sorted(_HEADER_REQUIRED_KEYS - set(doc))
    if missing:
        _fail(f"parameters.json 缺必需字段: {missing!r}")
    if doc["schema"] != VARIABLE_MAP_SCHEMA or doc["schema_version"] != VARIABLE_MAP_VERSION:
        _fail(f"parameters.json schema 非法: {doc.get('schema')!r}@{doc.get('schema_version')!r}")

    asm = doc["assembly"]
    if not isinstance(asm, Mapping) or set(asm) != {"id", "schema_version"}:
        _fail("parameters.json.assembly 须仅含 ['id', 'schema_version']")
    if asm["id"] is not None:
        _require_non_empty_str(asm["id"], "parameters.json.assembly.id")
    _require_non_empty_str(asm["schema_version"], "parameters.json.assembly.schema_version")

    _require_provider_ref(doc["generator"], "parameters.json.generator")
    records_rel = _check_relative_path(doc["records"], "parameters.json.records")
    col_count = _require_non_bool_int(doc["column_count"], "parameters.json.column_count", minimum=0)
    obj_offset = _require_finite_float(doc["objective_offset"], "parameters.json.objective_offset")
    obj_scale = _require_nonzero_scale(doc["objective_scale"], "parameters.json.objective_scale")
    obj_unit = _require_non_empty_str(doc["objective_unit"], "parameters.json.objective_unit")

    outputs = doc["outputs"]
    if (
        not isinstance(outputs, list)
        or not outputs
        or any(not isinstance(item, str) or not item for item in outputs)
    ):
        _fail("parameters.json.outputs 须为非空字符串数组")
    if doc["precision"] not in _PRECISIONS:
        _fail(f"parameters.json.precision 非法: {doc['precision']!r}")
    if doc["seed"] is not None:
        _require_non_bool_int(doc["seed"], "parameters.json.seed")
    if "extensions" in doc:
        _check_json_mapping(doc["extensions"], "parameters.json.extensions")

    return {
        "column_count": col_count,
        "objective_offset": obj_offset,
        "objective_scale": obj_scale,
        "objective_unit": obj_unit,
        "outputs": outputs,
        "records": records_rel,
    }


def _refill_stream(
    handle: Any,
    buf: str,
    pos: int,
    eof: bool,
    *,
    min_avail: int = 4096,
) -> tuple[str, int, bool]:
    if pos > 32768:
        buf = buf[pos:]
        pos = 0
    while not eof and len(buf) - pos < min_avail:
        chunk = handle.read(_STREAM_CHUNK_BYTES)
        if not chunk:
            eof = True
            break
        buf += chunk
    return buf, pos, eof


def _skip_ws(handle: Any, buf: str, pos: int, eof: bool) -> tuple[str, int, bool]:
    while True:
        buf, pos, eof = _refill_stream(handle, buf, pos, eof, min_avail=1)
        while pos < len(buf) and buf[pos] in " \t\r\n":
            pos += 1
        if pos < len(buf) or eof:
            return buf, pos, eof


def _decode_token(
    handle: Any,
    buf: str,
    pos: int,
    eof: bool,
    label: str,
) -> tuple[Any, str, int, bool]:
    buf, pos, eof = _skip_ws(handle, buf, pos, eof)
    if pos >= len(buf):
        _fail(f"{label} 遇到意外文件结束")
    decoder = json.JSONDecoder(parse_constant=_reject_json_constant)
    if buf[pos] in _JSON_NUM_CHARS or buf[pos] in _JSON_WORD_CHARS:
        token_chars = _JSON_NUM_CHARS if buf[pos] in _JSON_NUM_CHARS else _JSON_WORD_CHARS
        scan = pos
        while True:
            while scan < len(buf) and buf[scan] in token_chars:
                scan += 1
            if scan < len(buf) or eof:
                break
            chunk = handle.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                eof = True
                break
            buf += chunk
        try:
            val, end = decoder.raw_decode(buf, pos)
            return val, buf, end, eof
        except json.JSONDecodeError as exc:
            _fail(f"{label} JSON 解析失败: {exc}")
    while True:
        try:
            val, end = decoder.raw_decode(buf, pos)
            return val, buf, end, eof
        except json.JSONDecodeError as exc:
            if eof:
                _fail(f"{label} JSON 解析失败: {exc}")
            chunk = handle.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                eof = True
                _fail(f"{label} JSON 解析失败: {exc}")
            buf += chunk


def _validate_primal_receipt(receipt_obj: object) -> Mapping[str, Any]:
    if not isinstance(receipt_obj, Mapping):
        _fail("primal_solution.receipt 须为 JSON 对象")
    extra = sorted(set(receipt_obj) - _PRIMAL_RECEIPT_ALLOWED_KEYS)
    if extra:
        _fail(f"primal_solution.receipt 含未知字段: {extra!r}")
    _require_provider_ref(receipt_obj.get("solver"), "primal_solution.receipt.solver")

    for ts_key in ("started_at", "ended_at"):
        if ts_key in receipt_obj:
            _require_non_empty_str(receipt_obj[ts_key], f"primal_solution.receipt.{ts_key}")
    if "elapsed_seconds" in receipt_obj:
        elapsed = _require_finite_float(
            receipt_obj["elapsed_seconds"],
            "primal_solution.receipt.elapsed_seconds",
        )
        if elapsed < 0.0:
            _fail("primal_solution.receipt.elapsed_seconds 须 >= 0")
    if "solve_domain" in receipt_obj and receipt_obj["solve_domain"] not in _SOLVE_DOMAINS:
        _fail(f"primal_solution.receipt.solve_domain 非法: {receipt_obj['solve_domain']!r}")
    if "original_model_verified" in receipt_obj and not isinstance(
        receipt_obj["original_model_verified"], bool
    ):
        _fail("primal_solution.receipt.original_model_verified 须为布尔值")
    for obj_key in ("solver_diagnostics", "extensions"):
        if obj_key in receipt_obj:
            _check_json_mapping(receipt_obj[obj_key], f"primal_solution.receipt.{obj_key}")
    return receipt_obj


def _stream_primal_solution(primal_path: Path, expected_columns: int) -> tuple[dict[str, Any], array]:
    """Stream ``primal_solution.json`` directly into compact ``array('d')``."""
    try:
        raw_size = primal_path.stat().st_size
    except OSError as exc:
        _fail(f"原变量解文件不可读: {exc}")
    if raw_size <= 0:
        _fail("primal_solution.json 不能为空")

    meta: dict[str, Any] = {}
    primals = array("d")
    saw_values = False

    try:
        with primal_path.open("r", encoding="utf-8") as handle:
            buf, pos, eof = "", 0, False
            buf, pos, eof = _skip_ws(handle, buf, pos, eof)
            if pos >= len(buf) or buf[pos] != "{":
                _fail("primal_solution.json 须为 JSON 对象")
            pos += 1
            buf, pos, eof = _skip_ws(handle, buf, pos, eof)
            if pos < len(buf) and buf[pos] == "}":
                _fail("primal_solution.json 不能为空对象")

            while True:
                key, buf, pos, eof = _decode_token(handle, buf, pos, eof, "primal_solution.key")
                if not isinstance(key, str):
                    _fail(f"primal_solution.json 键须为字符串: {key!r}")
                buf, pos, eof = _skip_ws(handle, buf, pos, eof)
                if pos >= len(buf) or buf[pos] != ":":
                    _fail(f"primal_solution.json 键 {key!r} 后缺少 ':'")
                pos += 1
                buf, pos, eof = _skip_ws(handle, buf, pos, eof)

                if key == "values":
                    if saw_values:
                        _fail("primal_solution.json 存在重复 'values'")
                    saw_values = True
                    if pos >= len(buf) or buf[pos] != "[":
                        _fail("primal_solution.values 须为数组")
                    pos += 1
                    buf, pos, eof = _skip_ws(handle, buf, pos, eof)
                    if pos < len(buf) and buf[pos] == "]":
                        pos += 1
                    else:
                        while True:
                            num, buf, pos, eof = _decode_token(
                                handle, buf, pos, eof, f"primal_solution.values[{len(primals)}]"
                            )
                            primals.append(
                                _require_finite_float(num, f"primal_solution.values[{len(primals)}]")
                            )
                            buf, pos, eof = _skip_ws(handle, buf, pos, eof)
                            if pos < len(buf) and buf[pos] == ",":
                                pos += 1
                                buf, pos, eof = _skip_ws(handle, buf, pos, eof)
                                continue
                            if pos < len(buf) and buf[pos] == "]":
                                pos += 1
                                break
                            _fail("primal_solution.values 期望 ',' 或 ']'")
                else:
                    if key in meta:
                        _fail(f"primal_solution.json 存在重复字段 {key!r}")
                    val, buf, pos, eof = _decode_token(handle, buf, pos, eof, f"primal_solution.{key}")
                    meta[key] = val

                buf, pos, eof = _skip_ws(handle, buf, pos, eof)
                if pos < len(buf) and buf[pos] == ",":
                    pos += 1
                    buf, pos, eof = _skip_ws(handle, buf, pos, eof)
                    continue
                if pos < len(buf) and buf[pos] == "}":
                    pos += 1
                    buf, pos, eof = _skip_ws(handle, buf, pos, eof)
                    if pos < len(buf):
                        _fail("primal_solution.json 存在尾随内容")
                    break
                _fail("primal_solution.json 期望 ',' 或 '}'")
    except (OSError, UnicodeDecodeError) as exc:
        _fail(f"读取 primal_solution.json 失败: {exc}")

    if not saw_values:
        _fail("primal_solution.json 缺少必需字段: 'values'")
    extra = sorted(set(meta) - _PRIMAL_ALLOWED_KEYS)
    if extra:
        _fail(f"primal_solution.json 含未知字段: {extra!r}")
    missing = sorted(_PRIMAL_REQUIRED_KEYS - set(meta))
    if missing:
        _fail(f"primal_solution.json 缺必需字段: {missing!r}")
    if meta["schema"] != PRIMAL_SOLUTION_SCHEMA or meta["schema_version"] != PRIMAL_SOLUTION_VERSION:
        _fail(f"primal_solution schema 非法: {meta.get('schema')!r}@{meta.get('schema_version')!r}")
    if meta["math_status"] not in MATH_STATUSES:
        _fail(f"primal_solution.math_status 非法: {meta['math_status']!r}")
    if meta["termination_reason"] not in _SOLVER_TERMINATION_REASONS:
        _fail(f"primal_solution.termination_reason 非法: {meta['termination_reason']!r}")
    if not isinstance(meta["has_feasible_solution"], bool):
        _fail("primal_solution.has_feasible_solution 须为布尔值")
    primal_cols = _require_non_bool_int(meta["column_count"], "primal_solution.column_count", minimum=0)
    if primal_cols != expected_columns:
        _fail(f"primal_solution.column_count ({primal_cols}) 与 variable_map ({expected_columns}) 不一致")

    meta["objective_value"] = _require_optional_finite_float(
        meta["objective_value"], "primal_solution.objective_value"
    )
    meta["best_bound"] = _require_optional_finite_float(
        meta["best_bound"], "primal_solution.best_bound"
    )
    meta["relative_gap"] = _require_optional_finite_float(
        meta["relative_gap"], "primal_solution.relative_gap"
    )

    has_feasible = meta["has_feasible_solution"]
    if has_feasible != (meta["math_status"] in ("solved", "feasible")):
        _fail("primal_solution.has_feasible_solution 与 math_status 矛盾")
    if has_feasible:
        if len(primals) != expected_columns:
            _fail(f"primal_solution.values 长度 ({len(primals)}) 与 column_count ({expected_columns}) 不一致")
        if meta["objective_value"] is None:
            _fail("有可行解时 primal_solution.objective_value 不得为 null")
    elif len(primals) != 0:
        _fail("无可行解时 primal_solution.values 须为空数组")

    _validate_primal_receipt(meta["receipt"])
    if "extensions" in meta:
        _check_json_mapping(meta["extensions"], "primal_solution.extensions")
    return meta, primals


class _RecordStreamValidator:
    """Canonical F1 NDJSON state machine validating section order, field sets, and 0-based bounds."""

    def __init__(self, column_count: int) -> None:
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

    def check_col(self, value: object, context: str) -> int:
        col = _require_non_bool_int(value, context, minimum=0)
        if col >= self.column_count:
            _fail(f"{context} 列索引越界: {col} >= {self.column_count}")
        return col

    def check_row(self, value: object, context: str) -> int:
        row = _require_non_bool_int(value, context, minimum=0)
        if self.row_names_count > 0 and row >= self.row_names_count:
            _fail(f"{context} 行索引越界: {row} >= {self.row_names_count}")
        return row

    def advance_stage(self, target_stage: int, tag: str) -> None:
        if self.block is not None:
            _fail(f"记录 {tag!r} 不得出现在未闭合块 {self.block!r} 内")
        if target_stage < self.stage:
            _fail(f"记录 {tag!r} 违反 F1 规范节顺序")
        self.stage = target_stage

    @staticmethod
    def expect_len(record: Sequence[Any], expected: int, tag: str) -> None:
        if len(record) != expected:
            _fail(f"记录 {tag!r} 期望 {expected} 个元素，得到 {len(record)}")

    def finalize(self) -> None:
        if self.block is not None:
            _fail(f"parameters.jsonl 存在未闭合块: {self.block!r}")
        if self.col_names_count > 0 and self.col_names_count != self.column_count:
            _fail(
                f"variable_map column_names 数量 ({self.col_names_count}) "
                f"与 header column_count ({self.column_count}) 不一致"
            )


def _recover_variable_map_records(
    records_path: Path,
    primals: array,
    *,
    expected_columns: int,
) -> dict[str, Any]:
    validator = _RecordStreamValidator(expected_columns)
    expressions: list[float] = []
    expr_single_col: dict[int, int] = {}
    col_conv: dict[int, tuple[str, float, float]] = {}
    unit_var_count = 0
    device_series: dict[str, dict[str, Any]] = {}
    system_series: dict[str, dict[str, Any]] = {}
    facts: Any = None
    sources: dict[str, Any] | None = None
    column_names: list[str] = []
    row_names: list[str] = []

    current_expr_id: int | None = None
    total = 0.0
    term_count = 0
    single_col: int | None = None
    single_coeff = 0.0
    current_series: dict[str, Any] | None = None
    current_group: dict[str, Any] | None = None

    def resolve_fact_node(node: Any) -> Any:
        if isinstance(node, Mapping):
            if "expression" in node and len(node) == 1:
                eid = _require_non_bool_int(node["expression"], "facts.expression", minimum=0)
                if eid >= len(expressions):
                    _fail(f"facts 引用未定义表达式 ID: {eid}")
                return expressions[eid]
            return {key: resolve_fact_node(val) for key, val in node.items()}
        if isinstance(node, list):
            return [resolve_fact_node(item) for item in node]
        return node

    def resolve_scale_value(scale_entry: Mapping[str, Any], unit: str, label: str) -> float:
        raw_node = scale_entry["value"]
        has_scale = "scale" in scale_entry
        has_offset = "offset" in scale_entry
        if has_scale != has_offset:
            _fail(f"{label} 须同时提供 scale 与 offset")
        if has_scale:
            raw_val = _require_finite_float(resolve_fact_node(raw_node), label)
            sc = _require_nonzero_scale(scale_entry["scale"], f"{label}.scale")
            off = _require_finite_float(scale_entry["offset"], f"{label}.offset")
            return _apply_affine(raw_val, scale=sc, offset=off, label=label)

        # Without explicit scale/offset on the fact, only a single-column reference
        # whose declared variable unit strictly matches the requested unit is valid.
        if isinstance(raw_node, Mapping) and "expression" in raw_node and len(raw_node) == 1:
            eid = _require_non_bool_int(raw_node["expression"], f"{label}.expression", minimum=0)
            if eid >= len(expressions):
                _fail(f"{label} 引用未定义表达式 ID: {eid}")
            col = expr_single_col.get(eid)
            if col is not None and col in col_conv and col_conv[col][0] == unit:
                _, sc, off = col_conv[col]
                return _apply_affine(expressions[eid], scale=sc, offset=off, label=label)

        _fail(f"{label} 缺少匹配目标单位 {unit!r} 的显式 scale/offset")

    try:
        with records_path.open("r", encoding="utf-8") as handle:
            for lineno, raw_line in enumerate(handle, start=1):
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped, parse_constant=_reject_json_constant)
                except json.JSONDecodeError as exc:
                    _fail(f"{records_path}:{lineno} JSON 解析失败: {exc}")
                if not isinstance(record, list) or not record:
                    _fail(f"{records_path}:{lineno} 记录须为非空数组")
                tag = record[0]
                if not isinstance(tag, str) or tag not in _VARIABLE_MAP_RECORD_TAGS:
                    _fail(f"未知参数记录标签: {tag!r}")
                try:
                    check_finite_json(record, f"variable_map[{tag}]")
                except ValueError as exc:
                    _fail(str(exc))

                if tag == "column_names":
                    validator.advance_stage(0, tag)
                    validator.expect_len(record, 2, tag)
                    if not isinstance(record[1], list) or any(
                        not isinstance(n, str) or not n for n in record[1]
                    ):
                        _fail("column_names 须为非空字符串数组")
                    validator.col_names_count += len(record[1])
                    column_names.extend(record[1])
                elif tag == "row_names":
                    validator.advance_stage(1, tag)
                    validator.expect_len(record, 2, tag)
                    if not isinstance(record[1], list) or any(
                        not isinstance(n, str) or not n for n in record[1]
                    ):
                        _fail("row_names 须为非空字符串数组")
                    validator.row_names_count += len(record[1])
                    row_names.extend(record[1])
                elif tag == "epigraph":
                    validator.advance_stage(2, tag)
                    validator.expect_len(record, 2, tag)
                    validator.check_col(record[1], "epigraph.peak_position")
                    validator.block = "epigraph"
                elif tag == "epigraph_flows":
                    if validator.block != "epigraph":
                        _fail("epigraph_flows 缺少前置 epigraph")
                    validator.expect_len(record, 2, tag)
                    if not isinstance(record[1], list):
                        _fail("epigraph_flows 须为数组")
                    for idx, pos in enumerate(record[1]):
                        validator.check_col(pos, f"epigraph_flows[{idx}]")
                elif tag == "epigraph_end":
                    if validator.block != "epigraph":
                        _fail("epigraph_end 缺少前置 epigraph")
                    validator.expect_len(record, 1, tag)
                    validator.block = None
                elif tag == "expression":
                    validator.advance_stage(3, tag)
                    validator.expect_len(record, 3, tag)
                    current_expr_id = _require_non_bool_int(record[1], "expression.id", minimum=0)
                    if current_expr_id != validator.expr_count:
                        _fail(
                            "expression.id 须从 0 递增: "
                            f"期望 {validator.expr_count}, 得到 {current_expr_id}"
                        )
                    total = _require_finite_float(record[2], "expression.constant")
                    term_count = 0
                    single_col = None
                    single_coeff = 0.0
                    validator.block = "expression"
                elif tag == "terms":
                    if validator.block != "expression" or current_expr_id is None:
                        _fail("terms 缺少前置 expression")
                    validator.expect_len(record, 2, tag)
                    if not isinstance(record[1], list):
                        _fail("terms 须为 [col, coeff] 对数组")
                    for idx, pair in enumerate(record[1]):
                        if not isinstance(pair, list) or len(pair) != 2:
                            _fail(f"terms[{idx}] 须为 [col, coeff] 二元组")
                        col = validator.check_col(pair[0], f"terms[{idx}][0]")
                        c_val = _require_finite_float(pair[1], f"terms[{idx}][1]")
                        total += c_val * primals[col]
                        if term_count == 0:
                            single_col = col
                            single_coeff = c_val
                        term_count += 1
                elif tag == "expression_end":
                    if validator.block != "expression" or current_expr_id is None:
                        _fail("expression_end 缺少前置 expression")
                    validator.expect_len(record, 1, tag)
                    if not math.isfinite(total):
                        _fail(f"表达式 {current_expr_id} 求值非有限")
                    if term_count == 1 and single_col is not None and single_coeff == 1.0:
                        expr_single_col[current_expr_id] = single_col
                    expressions.append(total)
                    validator.expr_count += 1
                    validator.block = None
                    current_expr_id = None
                elif tag == "units":
                    validator.advance_stage(4, tag)
                    validator.expect_len(record, 2, tag)
                    if not isinstance(record[1], list):
                        _fail("units 须为 [col, unit, scale, offset] 四元组数组")
                    for idx, entry in enumerate(record[1]):
                        if not isinstance(entry, list) or len(entry) != 4:
                            _fail(f"units[{idx}] 须为 [col, unit, scale, offset] 四元组: {entry!r}")
                        col = validator.check_col(entry[0], f"units[{idx}].col")
                        unit = _require_non_empty_str(entry[1], f"units[{idx}].unit")
                        scale = _require_nonzero_scale(entry[2], f"units[{idx}].scale")
                        offset = _require_finite_float(entry[3], f"units[{idx}].offset")
                        _apply_affine(primals[col], scale=scale, offset=offset, label=f"units[{col}]")
                        col_conv[col] = (unit, scale, offset)
                        unit_var_count += 1
                elif tag == "component":
                    validator.advance_stage(5, tag)
                    validator.expect_len(record, 5, tag)
                    for idx, label in ((1, "identity"), (2, "unit"), (3, "caliber")):
                        _require_non_empty_str(record[idx], f"component.{label}")
                    eid = _require_non_bool_int(record[4], "component.expression_id", minimum=0)
                    if eid >= len(expressions):
                        _fail(f"component {record[1]!r} 引用未定义表达式 {eid}")
                elif tag == "facts":
                    validator.advance_stage(6, tag)
                    if validator.facts_seen:
                        _fail("parameters.jsonl 不得包含多个 facts 记录")
                    validator.expect_len(record, 2, tag)
                    validator.facts_seen = True
                    raw_facts = record[1]
                    if not isinstance(raw_facts, Mapping):
                        _fail("facts 须为 JSON 对象")
                    facts = _resolve_facts_payload(
                        raw_facts,
                        resolve_fact_node=resolve_fact_node,
                        resolve_scale_value=resolve_scale_value,
                    )
                elif tag == "series":
                    validator.advance_stage(7, tag)
                    validator.expect_len(record, 2, tag)
                    meta = record[1]
                    if not isinstance(meta, Mapping):
                        _fail("series 元数据须为 JSON 对象")
                    sid = _require_non_empty_str(meta.get("series_id"), "series.series_id")
                    if sid in validator.seen_series_ids:
                        _fail(f"series_id 重复: {sid!r}")
                    has_system = "block" in meta or "variable" in meta
                    has_device = "instance_id" in meta or "variable_id" in meta
                    if has_system == has_device:
                        _fail(
                            f"序列 {sid!r} 须严格声明设备身份(instance_id, variable_id)"
                            "或系统身份(block, variable)之一"
                        )
                    expected_core = _SYSTEM_SERIES_CORE_KEYS if has_system else _DEVICE_SERIES_CORE_KEYS
                    allowed_keys = expected_core | {"extensions"}
                    extra_keys = sorted(set(meta) - allowed_keys)
                    if extra_keys:
                        _fail(f"序列 {sid!r} 含未知核心字段: {extra_keys!r}")
                    missing_keys = sorted(expected_core - set(meta))
                    if missing_keys:
                        _fail(f"序列 {sid!r} 缺少必需字段: {missing_keys!r}")
                    unit = _require_non_empty_str(meta["unit"], f"series.{sid}.unit")
                    scale = _require_nonzero_scale(meta["scale"], f"series.{sid}.scale")
                    offset = _require_finite_float(meta["offset"], f"series.{sid}.offset")
                    if "extensions" in meta:
                        ext = _check_json_mapping(meta["extensions"], f"series.{sid}.extensions")
                        if any(not ext_k for ext_k in ext):
                            _fail(f"series.{sid}.extensions 键须为非空字符串")
                    if has_system:
                        block = _require_non_empty_str(meta["block"], f"series.{sid}.block")
                        variable = _require_non_empty_str(meta["variable"], f"series.{sid}.variable")
                        current_series = {
                            "kind": "system",
                            "series_id": sid,
                            "block": block,
                            "variable": variable,
                            "unit": unit,
                            "scale": scale,
                            "offset": offset,
                            "values": array("d"),
                        }
                    else:
                        inst = _require_non_empty_str(meta["instance_id"], f"series.{sid}.instance_id")
                        var = _require_non_empty_str(meta["variable_id"], f"series.{sid}.variable_id")
                        current_series = {
                            "kind": "device",
                            "series_id": sid,
                            "instance_id": inst,
                            "variable_id": var,
                            "unit": unit,
                            "scale": scale,
                            "offset": offset,
                            "values": array("d"),
                        }
                    validator.seen_series_ids.add(sid)
                    validator.block = "series"
                elif tag == "positions":
                    if validator.block != "series" or current_series is None:
                        _fail("positions 缺少前置 series")
                    validator.expect_len(record, 2, tag)
                    if not isinstance(record[1], list):
                        _fail("positions 须为列索引数组")
                    sc = current_series["scale"]
                    off = current_series["offset"]
                    sid = current_series["series_id"]
                    vals: array = current_series["values"]
                    for idx, pos in enumerate(record[1]):
                        col = validator.check_col(pos, f"series.{sid}.positions[{idx}]")
                        vals.append(
                            _apply_affine(primals[col], scale=sc, offset=off, label=f"series.{sid}[{col}]")
                        )
                elif tag == "series_end":
                    if validator.block != "series" or current_series is None:
                        _fail("series_end 缺少前置 series")
                    validator.expect_len(record, 1, tag)
                    sid = current_series["series_id"]
                    vals_arr: array = current_series["values"]
                    if not vals_arr:
                        _fail(f"序列 {sid!r} 不能为空")
                    if current_series["kind"] == "system":
                        system_series[sid] = {
                            "block": current_series["block"],
                            "variable": current_series["variable"],
                            "unit": current_series["unit"],
                            "values": vals_arr,
                        }
                    else:
                        device_series[sid] = {
                            "instance_id": current_series["instance_id"],
                            "variable_id": current_series["variable_id"],
                            "unit": current_series["unit"],
                            "values": vals_arr,
                        }
                    validator.block = None
                    current_series = None
                elif tag == "sources":
                    validator.advance_stage(8, tag)
                    if validator.sources_seen:
                        _fail("parameters.jsonl 不得包含多个 sources 记录")
                    validator.expect_len(record, 2, tag)
                    if not isinstance(record[1], Mapping):
                        _fail("sources 须为 JSON 对象")
                    validator.sources_seen = True
                    sources = dict(record[1], groups=[])
                elif tag == "source_group":
                    validator.advance_stage(8, tag)
                    if not validator.sources_seen or sources is None:
                        _fail("source_group 缺少前置 sources")
                    validator.expect_len(record, 3, tag)
                    meta, compressed = record[1], record[2]
                    if not isinstance(meta, Mapping):
                        _fail("source_group 元数据须为 JSON 对象")
                    for req_key in ("source", "role", "axis"):
                        _require_non_empty_str(meta.get(req_key), f"source_group.{req_key}")
                    role = str(meta["role"])
                    if not role.startswith(("column:", "row:")):
                        _fail(f"source_group.role 须以 column:/row: 开头: {role!r}")
                    if compressed is not None:
                        if not isinstance(compressed, Mapping) or set(compressed) != {
                            "start",
                            "stride",
                            "count",
                        }:
                            _fail("source_group compressed indices 非法")
                        _require_non_bool_int(compressed["start"], "source_group.indices.start")
                        _require_non_bool_int(compressed["stride"], "source_group.indices.stride")
                        _require_non_bool_int(
                            compressed["count"], "source_group.indices.count", minimum=0
                        )
                    validator.block = "source_group"
                    validator.group_role = role
                    validator.group_compressed = compressed is not None
                    current_group = dict(
                        meta,
                        targets=[],
                        indices=dict(compressed) if compressed is not None else [],
                    )
                elif tag == "targets":
                    if (
                        validator.block != "source_group"
                        or current_group is None
                        or validator.group_role is None
                    ):
                        _fail("targets 缺少前置 source_group")
                    validator.expect_len(record, 2, tag)
                    if not isinstance(record[1], list):
                        _fail("targets 须为索引数组")
                    names = column_names if validator.group_role.startswith("column:") else row_names
                    is_col_role = validator.group_role.startswith("column:")
                    for idx, pos in enumerate(record[1]):
                        checked = (
                            validator.check_col(pos, f"targets[{idx}]")
                            if is_col_role
                            else validator.check_row(pos, f"targets[{idx}]")
                        )
                        try:
                            current_group["targets"].append(names[checked])
                        except IndexError:
                            _fail(f"source_group targets 索引越界: {checked}")
                elif tag == "indices":
                    if validator.block != "source_group" or current_group is None:
                        _fail("indices 缺少前置 source_group")
                    if validator.group_compressed:
                        _fail("压缩索引 source_group 不得出现 indices 记录")
                    validator.expect_len(record, 2, tag)
                    if not isinstance(record[1], list):
                        _fail("indices 须为整数数组")
                    for idx, item in enumerate(record[1]):
                        current_group["indices"].append(
                            _require_non_bool_int(item, f"indices[{idx}]")
                        )
                elif tag == "source_group_end":
                    if validator.block != "source_group" or current_group is None or sources is None:
                        _fail("source_group_end 缺少前置 source_group")
                    validator.expect_len(record, 1, tag)
                    sources["groups"].append(current_group)
                    validator.block = None
                    validator.group_role = None
                    validator.group_compressed = False
                    current_group = None
    except (OSError, UnicodeDecodeError) as exc:
        _fail(f"读取 {records_path} 失败: {exc}")

    validator.finalize()

    lengths = {
        **{sid: len(entry["values"]) for sid, entry in device_series.items()},
        **{sid: len(entry["values"]) for sid, entry in system_series.items()},
    }
    if len(set(lengths.values())) > 1:
        _fail(f"所有设备与系统步序列长度必须一致: {lengths!r}")

    return {
        "unit_var_count": unit_var_count,
        "facts": facts,
        "series": device_series,
        "system_series": system_series,
        "model_sources": sources,
    }


def _resolve_scale_entries(
    raw_entries: Sequence[Any],
    *,
    label: str,
    value_prefix: str,
    resolve_scale_value: Any,
) -> list[dict[str, Any]]:
    """Validate and convert scale entries into business-unit EngineeringSolution items."""
    scales_out: list[dict[str, Any]] = []
    seen_pvars: set[str] = set()
    for scale_entry in raw_entries:
        if not isinstance(scale_entry, Mapping):
            _fail(f"{label} 条目须为对象")
        extra_s = sorted(set(scale_entry) - _SCALE_ENTRY_ALLOWED_KEYS)
        if extra_s:
            _fail(f"{label} 含未知字段: {extra_s!r}")
        for req in ("planning_variable_id", "value", "unit"):
            if req not in scale_entry:
                _fail(f"{label} 缺少字段: {req!r}")
        pname = _require_non_empty_str(
            scale_entry["planning_variable_id"],
            f"{label}.planning_variable_id",
        )
        if pname in seen_pvars:
            _fail(f"{label} planning_variable_id 重复: {pname!r}")
        seen_pvars.add(pname)
        unit = _require_non_empty_str(scale_entry["unit"], f"{label}.{pname}.unit")
        biz_val = resolve_scale_value(scale_entry, unit, f"{value_prefix}.{pname}")
        scales_out.append({"planning_variable_id": pname, "value": biz_val, "unit": unit})
    return scales_out


def _resolve_facts_payload(
    raw_facts: Mapping[str, Any],
    *,
    resolve_fact_node: Any,
    resolve_scale_value: Any,
) -> dict[str, Any]:
    if "construction_units" not in raw_facts or not isinstance(raw_facts["construction_units"], list):
        _fail("facts 缺少必需字段 construction_units")

    resolved_units: list[dict[str, Any]] = []
    seen_unit_ids: set[str] = set()
    for unit_entry in raw_facts["construction_units"]:
        if not isinstance(unit_entry, Mapping):
            _fail("facts.construction_units 条目须为 JSON 对象")
        extra_u = sorted(set(unit_entry) - _UNIT_ENTRY_ALLOWED_KEYS)
        if extra_u:
            _fail(f"construction_units 含未知字段: {extra_u!r}")
        for req in ("unit_id", "instance_id", "mode", "built", "scales"):
            if req not in unit_entry:
                _fail(f"construction_units 缺少必需字段: {req!r}")
        uid = _require_non_empty_str(unit_entry["unit_id"], "construction_units.unit_id")
        if uid in seen_unit_ids:
            _fail(f"construction_units unit_id 重复: {uid!r}")
        seen_unit_ids.add(uid)
        inst = _require_non_empty_str(
            unit_entry["instance_id"], f"construction_units[{uid!r}].instance_id"
        )
        mode = unit_entry["mode"]
        if mode not in ("mandatory", "optional"):
            _fail(f"construction_units[{uid!r}].mode 非法: {mode!r}")

        built_raw = _require_finite_float(resolve_fact_node(unit_entry["built"]), f"{uid}.built")
        rounded = round(built_raw)
        if rounded not in (0, 1) or abs(built_raw - rounded) > 1e-6:
            _fail(f"建设状态 {uid!r}.built 不属于整数 0/1: {built_raw!r}")

        if not isinstance(unit_entry["scales"], list):
            _fail(f"construction_units[{uid!r}].scales 须为数组")
        scales_out = _resolve_scale_entries(
            unit_entry["scales"],
            label=f"construction_units[{uid!r}].scales",
            value_prefix=uid,
            resolve_scale_value=resolve_scale_value,
        )

        resolved_units.append(
            {
                "unit_id": uid,
                "instance_id": inst,
                "mode": mode,
                "built": bool(rounded),
                "scales": scales_out,
            }
        )

    resolved_out: dict[str, Any] = {"construction_units": resolved_units}
    if "planning_variables" in raw_facts and raw_facts["planning_variables"] is not None:
        if not isinstance(raw_facts["planning_variables"], list):
            _fail("facts.planning_variables 须为数组")
        resolved_out["planning_variables"] = _resolve_scale_entries(
            raw_facts["planning_variables"],
            label="planning_variables",
            value_prefix="planning_variables",
            resolve_scale_value=resolve_scale_value,
        )
    return resolved_out


def _stream_engineering_solution_to_path(eng_sol: Mapping[str, Any], path: Path) -> int:
    """Write ``eng_sol`` (including ``array('d')`` series) directly to disk in 64KiB chunks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    size_bytes = 0
    with path.open("wb") as handle:
        for chunk in json_chunks(eng_sol):
            handle.write(chunk)
            size_bytes += len(chunk)
        handle.write(b"\n")
        size_bytes += 1
    return size_bytes


def _restore_in_current_process(
    bundle_dir: Path,
    primal_path: Path,
    solution_out_path: Path,
    result_meta_path: Path,
) -> None:
    """Execute streaming variable_map + primal_solution restoration and write solution.json."""
    header = _read_header(bundle_dir / PARAMETERS_PATH)
    records_path = (bundle_dir / header["records"]).resolve()
    bundle_root = bundle_dir.resolve()
    if records_path != bundle_root and bundle_root not in records_path.parents:
        _fail(f"records 路径越界: {header['records']!r}")
    if not records_path.is_file():
        _fail(f"缺失参数记录文件: {header['records']!r}")

    primal_meta, primals = _stream_primal_solution(primal_path, header["column_count"])
    math_status = str(primal_meta["math_status"])
    termination_reason = str(primal_meta["termination_reason"])
    has_feasible = bool(primal_meta["has_feasible_solution"])
    primal_receipt = primal_meta["receipt"]
    solve_domain = str(primal_receipt.get("solve_domain", "original"))

    diagnostics: dict[str, Any] = {}
    for key in ("solve_domain", "original_model_verified"):
        if key in primal_receipt:
            diagnostics[key] = primal_receipt[key]
    if isinstance(primal_receipt.get("solver_diagnostics"), Mapping):
        diagnostics.update(primal_receipt["solver_diagnostics"])

    if not has_feasible:
        eng_sol = _non_feasible_solution(
            math_status=math_status,
            termination_reason=termination_reason,
            objective_unit=header["objective_unit"],
            solver_diagnostics=diagnostics or None,
        )
        size_bytes = _stream_engineering_solution_to_path(eng_sol, solution_out_path)
        meta_summary = {
            k: v for k, v in eng_sol["solver_summary"].items() if k != "solver_diagnostics"
        }
        result_meta_path.write_text(
            json.dumps(
                {
                    "math_status": math_status,
                    "has_feasible_solution": False,
                    "solver_summary": meta_summary,
                    "solution_size_bytes": size_bytes,
                },
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
        return

    obj_scale = header["objective_scale"]
    obj_offset = header["objective_offset"]
    business_obj = _apply_affine(
        float(primal_meta["objective_value"]),
        scale=obj_scale,
        offset=obj_offset,
        label="objective_value",
    )
    if solve_domain == "restricted":
        business_bound: float | None = None
        relative_gap: float | None = None
    else:
        raw_bound = primal_meta["best_bound"]
        business_bound = (
            _apply_affine(float(raw_bound), scale=obj_scale, offset=obj_offset, label="best_bound")
            if raw_bound is not None
            else None
        )
        raw_gap = primal_meta["relative_gap"]
        relative_gap = float(raw_gap) if raw_gap is not None else None

    scalar_summary: dict[str, Any] = {
        "termination_reason": termination_reason,
        "math_status": math_status,
        "has_feasible_solution": True,
        "objective_value": business_obj,
        "objective_unit": header["objective_unit"],
        "best_bound": business_bound,
        "relative_gap": relative_gap,
    }
    solver_summary = (
        {**scalar_summary, "solver_diagnostics": diagnostics} if diagnostics else dict(scalar_summary)
    )

    recovered = _recover_variable_map_records(
        records_path,
        primals,
        expected_columns=header["column_count"],
    )
    facts_payload = recovered["facts"]
    if not isinstance(facts_payload, Mapping) or "construction_units" not in facts_payload:
        _fail("有可行解时 variable_map 缺少 facts.construction_units")

    eng_sol: dict[str, Any] = {
        "schema": ENGINEERING_SOLUTION_SCHEMA,
        "schema_version": ENGINEERING_SOLUTION_VERSION,
        "solver_summary": solver_summary,
        "construction_units": facts_payload["construction_units"],
        "series": recovered["series"],
        "system_series": recovered["system_series"],
        "extensions": {},
    }
    if "planning_variables" in facts_payload:
        eng_sol["planning_variables"] = facts_payload["planning_variables"]
    if recovered["model_sources"] is not None:
        eng_sol["model_sources"] = recovered["model_sources"]

    metrics_out: dict[str, Any] | None = None
    if "metrics" in header["outputs"]:
        metrics_out = {
            "termination_reason": termination_reason,
            "math_status": math_status,
            "objective_value": business_obj,
            "objective_unit": header["objective_unit"],
            "num_variables": recovered["unit_var_count"],
        }
        eng_sol["metrics"] = metrics_out

    size_bytes = _stream_engineering_solution_to_path(eng_sol, solution_out_path)
    meta_out: dict[str, Any] = {
        "math_status": math_status,
        "has_feasible_solution": True,
        "solver_summary": scalar_summary,
        "solution_size_bytes": size_bytes,
    }
    if metrics_out is not None:
        meta_out["metrics"] = metrics_out
    result_meta_path.write_text(
        json.dumps(meta_out, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _current_vm_bytes() -> int | None:
    """Return current process VmSize in bytes on Linux, or None when unavailable."""
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmSize:"):
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


def _subprocess_restore_entry(
    bundle_dir_str: str,
    primal_path_str: str,
    solution_out_str: str,
    result_meta_str: str,
    memory_limit_mb: int,
) -> None:
    """Spawned child-process entrypoint enforcing ``RLIMIT_AS`` strictly."""
    limit_bytes = int(memory_limit_mb) * 1024 * 1024
    baseline_vm = _current_vm_bytes() or 0
    requested_as = baseline_vm + limit_bytes
    try:
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY and requested_as > hard:
            raise OSError(f"requested RLIMIT_AS ({requested_as}) exceeds hard limit ({hard})")
        resource.setrlimit(resource.RLIMIT_AS, (requested_as, hard))
    except (ValueError, OSError) as exc:
        Path(result_meta_str).write_text(
            json.dumps(
                {"error": f"建立受限子进程内存配额失败: {exc}", "reason": "resource-limit-failed"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return

    try:
        _restore_in_current_process(
            Path(bundle_dir_str),
            Path(primal_path_str),
            Path(solution_out_str),
            Path(result_meta_str),
        )
    except ComputationUnavailableError as exc:
        Path(result_meta_str).write_text(
            json.dumps({"error": str(exc), "reason": exc.reason}, ensure_ascii=False),
            encoding="utf-8",
        )
    except MemoryError as exc:
        Path(result_meta_str).write_text(
            json.dumps(
                {"error": f"受限子进程内存超限: {exc}", "reason": "resource_exhausted"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


def _terminate_process(proc: multiprocessing.Process) -> None:
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=0.5)
    if proc.is_alive():
        proc.kill()
        proc.join(timeout=1.0)


def _check_stop_and_deadline(
    stop: StopProbe | None,
    deadline: float,
    phase: str,
) -> None:
    stop_reason = read_stop_reason(stop)
    if stop_reason is not None:
        raise ComputationUnavailableError(f"结果还原{phase}已停止: {stop_reason}", reason=stop_reason)
    if time.monotonic() > deadline:
        raise ComputationUnavailableError(f"结果还原{phase}超时", reason="timed_out")


def _stream_content_to_path(
    content: Content | bytes,
    dest: Path,
    *,
    stop: StopProbe | None,
    deadline: float,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = MemoryContent(content) if isinstance(content, bytes) else content
    with dest.open("wb") as handle:
        for chunk in raw.iter_chunks():
            _check_stop_and_deadline(stop, deadline, "输入准备阶段")
            handle.write(chunk)
    _check_stop_and_deadline(stop, deadline, "输入准备阶段")


def _allocate_solution_path(solution_output_path: Path | None) -> tuple[Path, Path | None]:
    if solution_output_path is not None:
        sol_path = Path(solution_output_path)
        sol_path.parent.mkdir(parents=True, exist_ok=True)
        return sol_path, None
    out_dir = Path(tempfile.mkdtemp(prefix="ies-eng-solution-"))
    return out_dir / "engineering_solution.json", out_dir


def _restore_from_bundle_dir_validated(
    bundle_dir: Path,
    primal_path: Path,
    receipt: ExecutionReceipt | Mapping[str, Any],
    *,
    solution_output_path: Path | None,
    memory_limit_mb: int,
    deadline: float,
    stop: StopProbe | None,
) -> RestorationOutcome:
    bundle_id = receipt["bundle_id"] if isinstance(receipt, Mapping) else receipt.bundle_id
    receipt_id = receipt["receipt_id"] if isinstance(receipt, Mapping) else receipt.receipt_id
    receipt_status = receipt["status"] if isinstance(receipt, Mapping) else receipt.status
    term_reason = (
        str(receipt.get("termination_reason", ""))
        if isinstance(receipt, Mapping)
        else receipt.termination_reason
    )
    exec_status = _execution_status_from_receipt(str(receipt_status), term_reason)

    _check_stop_and_deadline(stop, deadline, "启动前")

    sol_path, owned_tmp_dir = _allocate_solution_path(solution_output_path)
    try:
        if receipt_status != "succeeded":
            eng_sol = _non_feasible_solution(
                math_status="not_solved",
                termination_reason="not_solved",
                objective_unit=None,
            )
            size_bytes = _stream_engineering_solution_to_path(eng_sol, sol_path)
            outputs_meta: dict[str, Any] = {
                "solver_summary": dict(eng_sol["solver_summary"]),
                "engineering_solution_path": str(sol_path),
                "engineering_solution_size_bytes": size_bytes,
            }
            return RestorationOutcome(
                compute_result=ComputeResult(
                    bundle_id=str(bundle_id),
                    receipt_id=str(receipt_id),
                    execution_status=exec_status,
                    math_status="not_solved",
                    outputs=outputs_meta,
                ),
                solution_path=sol_path,
                solution_content=FileContent(sol_path, size_bytes),
                solver_summary=dict(eng_sol["solver_summary"]),
                child_pid=None,
            )

        with tempfile.TemporaryDirectory(prefix="ies-restore-") as tmp_dir:
            meta_path = Path(tmp_dir) / "restore_meta.json"
            ctx = multiprocessing.get_context("spawn")
            proc = ctx.Process(
                target=_subprocess_restore_entry,
                args=(
                    str(bundle_dir),
                    str(primal_path),
                    str(sol_path),
                    str(meta_path),
                    memory_limit_mb,
                ),
                daemon=False,
            )
            proc.start()
            child_pid = proc.pid
            try:
                while proc.is_alive():
                    stop_reason = read_stop_reason(stop)
                    if stop_reason is not None:
                        _terminate_process(proc)
                        raise ComputationUnavailableError(
                            f"结果还原运行中被停止: {stop_reason}",
                            reason=stop_reason,
                        )
                    if time.monotonic() > deadline:
                        _terminate_process(proc)
                        raise ComputationUnavailableError("结果还原受限子进程超时", reason="timed_out")
                    proc.join(timeout=0.02)
            except BaseException:
                _terminate_process(proc)
                raise

            meta_doc: dict[str, Any] | None = None
            if meta_path.is_file():
                meta_doc = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta_doc is not None and "error" in meta_doc:
                err_msg = str(meta_doc["error"])
                err_reason = str(meta_doc.get("reason") or "result-protocol-failure")
                if err_msg.startswith("结果还原失败: "):
                    raise ComputationUnavailableError(err_msg, reason=err_reason)
                _fail(err_msg, reason=err_reason)
            if proc.exitcode != 0 or meta_doc is None or not sol_path.is_file():
                reason = "resource_exhausted" if proc.exitcode in (-9, -11) else "result-protocol-failure"
                _fail(f"受限子进程异常退出 (exitcode={proc.exitcode})", reason=reason)

            sol_size = int(meta_doc.get("solution_size_bytes", sol_path.stat().st_size))
            outputs_meta = {
                "solver_summary": dict(meta_doc["solver_summary"]),
                "engineering_solution_path": str(sol_path),
                "engineering_solution_size_bytes": sol_size,
            }
            if "metrics" in meta_doc:
                outputs_meta["metrics"] = dict(meta_doc["metrics"])
            return RestorationOutcome(
                compute_result=ComputeResult(
                    bundle_id=str(bundle_id),
                    receipt_id=str(receipt_id),
                    execution_status="completed",
                    math_status=str(meta_doc["math_status"]),
                    outputs=outputs_meta,
                ),
                solution_path=sol_path,
                solution_content=FileContent(sol_path, sol_size),
                solver_summary=dict(meta_doc["solver_summary"]),
                child_pid=child_pid,
            )
    except BaseException:
        if owned_tmp_dir is not None:
            shutil.rmtree(owned_tmp_dir, ignore_errors=True)
        raise


def restore_from_bundle_dir(
    bundle_dir: Path,
    primal_path: Path,
    receipt: ExecutionReceipt | Mapping[str, Any],
    *,
    solution_output_path: Path | None = None,
    memory_limit_mb: int | None = None,
    timeout_seconds: float | None = None,
    stop: StopProbe | None = None,
) -> RestorationOutcome:
    """Restore ``ies.engineering-solution@1.0.0`` inside a bounded ``spawn`` child subprocess."""
    eff_mem_mb, eff_timeout_s = _validate_resource_budgets(memory_limit_mb, timeout_seconds)
    deadline = time.monotonic() + eff_timeout_s
    return _restore_from_bundle_dir_validated(
        bundle_dir,
        primal_path,
        receipt,
        solution_output_path=solution_output_path,
        memory_limit_mb=eff_mem_mb,
        deadline=deadline,
        stop=stop,
    )


class ResultAdapter:
    """Universal 1.2.0 ResultAdapter restoring ``ies.primal-solution@1.0.0`` via spawn subprocess."""

    __slots__ = ()

    def restore_with_outcome(
        self,
        bundle: SolverBundle,
        receipt: ExecutionReceipt | Mapping[str, Any],
        outputs: Mapping[str, object],
        *,
        solution_output_path: Path | None = None,
        memory_limit_mb: int | None = None,
        timeout_seconds: float | None = None,
        stop: StopProbe | None = None,
    ) -> RestorationOutcome:
        """Materialize streamed inputs/outputs under one budget and run bounded spawn restoration."""
        if not isinstance(bundle, SolverBundle):
            raise TypeError(f"bundle 须为 SolverBundle，得到 {type(bundle).__name__}")
        eff_mem_mb, eff_timeout_s = _validate_resource_budgets(memory_limit_mb, timeout_seconds)
        deadline = time.monotonic() + eff_timeout_s
        _check_stop_and_deadline(stop, deadline, "启动前")

        receipt_status = receipt["status"] if isinstance(receipt, Mapping) else receipt.status
        if receipt_status != "succeeded":
            return _restore_from_bundle_dir_validated(
                Path("."),
                Path("."),
                receipt,
                solution_output_path=solution_output_path,
                memory_limit_mb=eff_mem_mb,
                deadline=deadline,
                stop=stop,
            )

        primal_raw = outputs.get(PRIMAL_SOLUTION_OUTPUT_PATH)
        if not isinstance(primal_raw, (Content, bytes)):
            _fail(f"声明输出缺失: {PRIMAL_SOLUTION_OUTPUT_PATH}")

        with tempfile.TemporaryDirectory(prefix="ies-bundle-restore-") as tmp_dir:
            root = Path(tmp_dir)
            for rel_path, content in bundle.files.items():
                if rel_path.endswith((".mps", "acceleration.json", "epigraphs.json")):
                    continue
                _check_stop_and_deadline(stop, deadline, "输入准备阶段")
                _stream_content_to_path(content, root / rel_path, stop=stop, deadline=deadline)
            primal_path = root / PRIMAL_SOLUTION_OUTPUT_PATH
            _stream_content_to_path(primal_raw, primal_path, stop=stop, deadline=deadline)
            return _restore_from_bundle_dir_validated(
                root,
                primal_path,
                receipt,
                solution_output_path=solution_output_path,
                memory_limit_mb=eff_mem_mb,
                deadline=deadline,
                stop=stop,
            )

    def restore(
        self,
        bundle: SolverBundle,
        receipt: ExecutionReceipt | Mapping[str, Any],
        outputs: Mapping[str, object],
        *,
        solution_output_path: Path | None = None,
        memory_limit_mb: int | None = None,
        timeout_seconds: float | None = None,
        stop: StopProbe | None = None,
    ) -> ComputeResult:
        """Restore ``ComputeResult`` referencing durable ``ies.engineering-solution@1.0.0``."""
        return self.restore_with_outcome(
            bundle,
            receipt,
            outputs,
            solution_output_path=solution_output_path,
            memory_limit_mb=memory_limit_mb,
            timeout_seconds=timeout_seconds,
            stop=stop,
        ).compute_result


def restore_bundle_outputs(
    bundle: SolverBundle,
    receipt: ExecutionReceipt | Mapping[str, Any],
    outputs: Mapping[str, object],
    *,
    solution_output_path: Path | None = None,
    memory_limit_mb: int | None = None,
    timeout_seconds: float | None = None,
    stop: StopProbe | None = None,
) -> RestorationOutcome:
    """Convenience entry calling ``ResultAdapter().restore_with_outcome``."""
    return ResultAdapter().restore_with_outcome(
        bundle,
        receipt,
        outputs,
        solution_output_path=solution_output_path,
        memory_limit_mb=memory_limit_mb,
        timeout_seconds=timeout_seconds,
        stop=stop,
    )


__all__ = [
    "DEFAULT_MEMORY_LIMIT_MB",
    "DEFAULT_TIMEOUT_SECONDS",
    "ENGINEERING_SOLUTION_SCHEMA",
    "ENGINEERING_SOLUTION_VERSION",
    "FileContent",
    "PRIMAL_SOLUTION_SCHEMA",
    "PRIMAL_SOLUTION_VERSION",
    "RestorationOutcome",
    "ResultAdapter",
    "restore_bundle_outputs",
    "restore_from_bundle_dir",
]
