"""Universal F2 ResultAdapter running streaming restoration in an isolated spawn subprocess."""

from __future__ import annotations

import json
import math
import multiprocessing
import resource
import tempfile
import time
from array import array
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn

from iesplan.computation.contracts import (
    PRIMAL_SOLUTION_OUTPUT_PATH,
    TargetSolverBundle,
    check_finite_json,
)
from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.generators.highs_milp import PARAMETERS_PATH
from iesplan.computation.generators.target_bundle import (
    VARIABLE_MAP_SCHEMA,
    VARIABLE_MAP_VERSION,
    linear_conversion_for_unit,
)
from iesplan.computation.protocols import (
    MATH_STATUSES,
    ComputeResult,
    ExecutionReceipt,
    StopProbe,
    read_stop_reason,
)
from iesplan.core.content import Content, MemoryContent, freeze_owned_json

PRIMAL_SOLUTION_SCHEMA = "ies.primal-solution"
PRIMAL_SOLUTION_VERSION = "1.0.0"

ENGINEERING_SOLUTION_SCHEMA = "ies.engineering-solution"
ENGINEERING_SOLUTION_VERSION = "1.0.0"

_SOLVER_TERMINATION_REASONS = frozenset(
    ("optimal", "time_limit", "infeasible", "unbounded", "not_solved")
)
_FULL_FINANCIAL_KEYS = frozenset(
    (
        "currency",
        "construction_total",
        "annual_fixed_om",
        "payments_applied",
        "net_cashflows",
        "annual_operation",
        "tax_cash",
        "monthly_demand",
        "annual_net_flows",
        "construction_units",
        "cost_items",
        "amounts",
        "cash_movements",
        "assets",
        "comparison",
    )
)
_STREAM_CHUNK_BYTES = 65_536


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


def _require_finite_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{label} 须为有限数值，得到 {value!r}")
    num = float(value)
    if not math.isfinite(num):
        _fail(f"{label} 非有限数值: {value!r}")
    return num


def _require_nonzero_scale(value: object, label: str) -> float:
    scale = _require_finite_float(value, label)
    if scale == 0.0:
        _fail(f"{label} 不能为 0")
    return scale


def _apply_affine(raw_value: float, *, scale: float, offset: float, label: str) -> float:
    val = scale * raw_value + offset
    if not math.isfinite(val):
        _fail(f"{label} 线性换算后非有限")
    return val


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


def _read_header(parameters_path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(parameters_path.read_text(encoding="utf-8"), parse_constant=_reject_json_constant)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"读取参数头 {parameters_path} 失败: {exc}")
    if not isinstance(doc, Mapping):
        _fail("parameters.json 须为 JSON 对象")
    required = {
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
    }
    missing = sorted(required - set(doc))
    if missing:
        _fail(f"parameters.json 缺必需字段: {missing!r}")
    if doc["schema"] != VARIABLE_MAP_SCHEMA or doc["schema_version"] != VARIABLE_MAP_VERSION:
        _fail(f"parameters.json schema 非法: {doc.get('schema')!r}@{doc.get('schema_version')!r}")
    col_count = _require_non_bool_int(doc["column_count"], "column_count", minimum=0)
    obj_offset = _require_finite_float(doc["objective_offset"], "objective_offset")
    obj_scale = _require_nonzero_scale(doc["objective_scale"], "objective_scale")
    obj_unit = doc["objective_unit"]
    if not isinstance(obj_unit, str) or not obj_unit:
        _fail("parameters.json.objective_unit 须为非空字符串")
    records_rel = doc["records"]
    if (
        not isinstance(records_rel, str)
        or not records_rel
        or records_rel.startswith("/")
        or ".." in records_rel.split("/")
    ):
        _fail(f"parameters.json.records 路径非法: {records_rel!r}")
    outputs = doc["outputs"]
    if not isinstance(outputs, list) or not outputs or any(not isinstance(o, str) or not o for o in outputs):
        _fail("parameters.json.outputs 须为非空字符串数组")
    return {
        "column_count": col_count,
        "objective_offset": obj_offset,
        "objective_scale": obj_scale,
        "objective_unit": obj_unit,
        "outputs": outputs,
        "records": records_rel,
    }


_JSON_NUM_CHARS = frozenset("-+0123456789.eE")
_JSON_WORD_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _stream_primal_solution(primal_path: Path, expected_columns: int) -> tuple[dict[str, Any], array]:
    """Stream ``primal_solution.json`` without double-copying large values arrays."""
    try:
        raw_size = primal_path.stat().st_size
    except OSError as exc:
        _fail(f"原变量解文件不可读: {exc}")
    if raw_size <= 0:
        _fail("primal_solution.json 不能为空")

    # Stream token-by-token through json.JSONDecoder over a sliding buffer so multi-million
    # values arrays land directly in compact array('d') rather than a Python float list.
    decoder = json.JSONDecoder(parse_constant=_reject_json_constant)
    meta: dict[str, Any] = {}
    primals = array("d")
    saw_values = False

    def _refill(handle: Any, buf: str, pos: int, eof: bool, min_avail: int = 4096) -> tuple[str, int, bool]:
        if pos > 32768:
            buf = buf[pos:]
            pos = 0
        while not eof and len(buf) - pos < min_avail:
            more = handle.read(_STREAM_CHUNK_BYTES)
            if more:
                buf += more
            else:
                eof = True
        return buf, pos, eof

    def _decode_token(handle: Any, buf: str, pos: int, eof: bool, label: str) -> tuple[Any, str, int, bool]:
        while True:
            if pos >= len(buf):
                if not eof:
                    more = handle.read(_STREAM_CHUNK_BYTES)
                    if more:
                        buf += more
                        continue
                    eof = True
                _fail(f"{label} 遇到意外文件结束")
            first = buf[pos]
            if first in _JSON_NUM_CHARS or first in _JSON_WORD_CHARS:
                token_chars = _JSON_NUM_CHARS if first in _JSON_NUM_CHARS else _JSON_WORD_CHARS
                end = pos + 1
                while True:
                    while end < len(buf) and buf[end] in token_chars:
                        end += 1
                    if end == len(buf) and not eof:
                        more = handle.read(_STREAM_CHUNK_BYTES)
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
                    more = handle.read(_STREAM_CHUNK_BYTES)
                    if more:
                        buf += more
                        continue
                    eof = True
                    continue
                _fail(f"{label} JSON 解析失败: {exc}")

    try:
        with primal_path.open("r", encoding="utf-8") as handle:
            buf = handle.read(_STREAM_CHUNK_BYTES)
            pos = 0
            eof = not buf

            def skip_ws() -> None:
                nonlocal buf, pos, eof
                while True:
                    buf, pos, eof = _refill(handle, buf, pos, eof)
                    while pos < len(buf) and buf[pos] in " \t\r\n":
                        pos += 1
                    if pos < len(buf) or eof:
                        return

            skip_ws()
            if pos >= len(buf) or buf[pos] != "{":
                _fail("primal_solution.json 须为 JSON 对象")
            pos += 1
            skip_ws()
            if pos < len(buf) and buf[pos] == "}":
                _fail("primal_solution.json 不能为空对象")

            while True:
                key, buf, pos, eof = _decode_token(handle, buf, pos, eof, "primal_solution key")
                if not isinstance(key, str):
                    _fail("primal_solution.json 键须为字符串")
                if key in meta or (key == "values" and saw_values):
                    _fail(f"primal_solution.json 存在重复键: {key!r}")
                skip_ws()
                if pos >= len(buf) or buf[pos] != ":":
                    _fail(f"primal_solution.json 键 {key!r} 后缺少 ':'")
                pos += 1
                skip_ws()
                if key == "values":
                    saw_values = True
                    if pos >= len(buf) or buf[pos] != "[":
                        _fail("primal_solution.values 须为 JSON 数组")
                    pos += 1
                    skip_ws()
                    if pos < len(buf) and buf[pos] == "]":
                        pos += 1
                    else:
                        while True:
                            val, buf, pos, eof = _decode_token(
                                handle, buf, pos, eof, f"primal_solution.values[{len(primals)}]"
                            )
                            primals.append(
                                _require_finite_float(val, f"primal_solution.values[{len(primals)}]")
                            )
                            skip_ws()
                            if pos < len(buf) and buf[pos] == ",":
                                pos += 1
                                skip_ws()
                                continue
                            if pos < len(buf) and buf[pos] == "]":
                                pos += 1
                                break
                            _fail("primal_solution.values 期望 ',' 或 ']'")
                else:
                    val, buf, pos, eof = _decode_token(handle, buf, pos, eof, f"primal_solution.{key}")
                    meta[key] = val

                skip_ws()
                if pos < len(buf) and buf[pos] == ",":
                    pos += 1
                    skip_ws()
                    continue
                if pos < len(buf) and buf[pos] == "}":
                    pos += 1
                    skip_ws()
                    if pos < len(buf):
                        _fail("primal_solution.json 存在尾随内容")
                    break
                _fail("primal_solution.json 期望 ',' 或 '}'")
    except (OSError, UnicodeDecodeError) as exc:
        _fail(f"读取 primal_solution.json 失败: {exc}")

    if not saw_values:
        _fail("primal_solution.json 缺少必需字段: 'values'")
    required = {
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
    }
    allowed = required | {"extensions"}
    extra = sorted(set(meta) - allowed)
    if extra:
        _fail(f"primal_solution.json 含未知字段: {extra!r}")
    missing = sorted(required - set(meta))
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

    check_finite_json(meta, "primal_solution")
    receipt_obj = meta["receipt"]
    if (
        not isinstance(receipt_obj, Mapping)
        or not isinstance(receipt_obj.get("solver"), str)
        or not receipt_obj["solver"]
    ):
        _fail("primal_solution.receipt 缺少合法 solver 引用")
    return meta, primals


def _recover_variable_map_records(
    records_path: Path,
    primals: array,
    *,
    include_solution: bool,
) -> dict[str, Any]:
    expressions: list[float] = []
    expr_single_col: dict[int, int] = {}
    variables: list[dict[str, Any]] = []
    col_conv: dict[int, tuple[str, float, float]] = {}
    unit_conv_by_name: dict[str, tuple[float, float]] = {}
    components: dict[str, dict[str, Any]] = {}
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
    seen_series_ids: set[str] = set()

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

    def resolve_scale_expr(raw_node: Any, unit: str, label: str) -> tuple[float, float]:
        if isinstance(raw_node, Mapping) and "expression" in raw_node and len(raw_node) == 1:
            eid = _require_non_bool_int(raw_node["expression"], f"{label}.expression", minimum=0)
            if eid >= len(expressions):
                _fail(f"{label} 引用未定义表达式 ID: {eid}")
            raw_val = expressions[eid]
            col = expr_single_col.get(eid)
            if col is not None and col in col_conv:
                _, scale, offset = col_conv[col]
                return _apply_affine(raw_val, scale=scale, offset=offset, label=label), raw_val
        else:
            raw_val = _require_finite_float(resolve_fact_node(raw_node), label)
        if unit in unit_conv_by_name:
            scale, offset = unit_conv_by_name[unit]
        else:
            scale, offset = linear_conversion_for_unit(unit)
        return _apply_affine(raw_val, scale=scale, offset=offset, label=label), raw_val

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
                if tag == "column_names":
                    column_names.extend(record[1])
                elif tag == "row_names":
                    if include_solution:
                        row_names.extend(record[1])
                elif tag in ("epigraph", "epigraph_flows", "epigraph_end"):
                    # ResultAdapter does not re-verify solver math or epigraph tightness.
                    continue
                elif tag == "expression":
                    current_expr_id = _require_non_bool_int(record[1], "expression.id", minimum=0)
                    if current_expr_id != len(expressions):
                        _fail(f"expression.id 须从 0 递增: 期望 {len(expressions)}, 得到 {current_expr_id}")
                    total = _require_finite_float(record[2], "expression.constant")
                    term_count = 0
                    single_col = None
                    single_coeff = 0.0
                elif tag == "terms":
                    if current_expr_id is None:
                        _fail("terms 缺少前置 expression")
                    for pos, coeff in record[1]:
                        col = _require_non_bool_int(pos, "terms.col", minimum=0)
                        try:
                            raw_x = primals[col]
                        except IndexError:
                            _fail(f"表达式列索引越界: {col}")
                        c_val = _require_finite_float(coeff, "terms.coeff")
                        total += c_val * raw_x
                        if term_count == 0:
                            single_col = col
                            single_coeff = c_val
                        term_count += 1
                elif tag == "expression_end":
                    if current_expr_id is None:
                        _fail("expression_end 缺少前置 expression")
                    if not math.isfinite(total):
                        _fail(f"表达式 {current_expr_id} 求值非有限")
                    if term_count == 1 and single_col is not None and single_coeff == 1.0:
                        expr_single_col[current_expr_id] = single_col
                    expressions.append(total)
                    current_expr_id = None
                elif tag == "units":
                    for entry in record[1]:
                        if not isinstance(entry, list) or len(entry) != 4:
                            _fail(f"units 条目须为 [col, unit, scale, offset] 四元组: {entry!r}")
                        col = _require_non_bool_int(entry[0], "units.col", minimum=0)
                        unit = entry[1]
                        if not isinstance(unit, str) or not unit:
                            _fail(f"units.unit 须为非空字符串: {unit!r}")
                        scale = _require_nonzero_scale(entry[2], "units.scale")
                        offset = _require_finite_float(entry[3], "units.offset")
                        try:
                            raw_x = primals[col]
                            col_name = column_names[col]
                        except IndexError:
                            _fail(f"units 列索引越界: {col}")
                        val = _apply_affine(raw_x, scale=scale, offset=offset, label=f"units[{col}]")
                        col_conv[col] = (unit, scale, offset)
                        unit_conv_by_name.setdefault(unit, (scale, offset))
                        variables.append({"name": col_name, "unit": unit, "value": val})
                elif tag == "component":
                    _, identity, unit, caliber, expr_id = record
                    eid = _require_non_bool_int(expr_id, "component.expression_id", minimum=0)
                    if eid >= len(expressions):
                        _fail(f"component {identity!r} 引用未定义表达式 {eid}")
                    components[identity] = {
                        "unit": unit,
                        "caliber": caliber,
                        "raw_total": expressions[eid],
                    }
                elif tag == "facts":
                    if include_solution:
                        raw_facts = record[1]
                        if not isinstance(raw_facts, Mapping):
                            _fail("facts 须为 JSON 对象")
                        facts = _resolve_facts_payload(
                            raw_facts,
                            resolve_fact_node=resolve_fact_node,
                            resolve_scale_expr=resolve_scale_expr,
                        )
                elif tag == "series":
                    meta = record[1]
                    if not isinstance(meta, Mapping):
                        _fail("series 元数据须为 JSON 对象")
                    sid = meta.get("series_id")
                    if not isinstance(sid, str) or not sid or sid in seen_series_ids:
                        _fail(f"series_id 非法或重复: {sid!r}")
                    seen_series_ids.add(sid)
                    scale = _require_nonzero_scale(meta.get("scale"), f"series.{sid}.scale")
                    offset = _require_finite_float(meta.get("offset"), f"series.{sid}.offset")
                    unit = meta.get("unit")
                    if not isinstance(unit, str) or not unit:
                        _fail(f"series.{sid}.unit 须为非空字符串")
                    is_system = "block" in meta or "variable" in meta
                    if is_system:
                        block = meta.get("block")
                        variable = meta.get("variable")
                        if (
                            not isinstance(block, str)
                            or not block
                            or not isinstance(variable, str)
                            or not variable
                        ):
                            _fail(f"系统序列 {sid!r} 缺少 block/variable")
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
                        inst = meta.get("instance_id")
                        var = meta.get("variable_id")
                        if not isinstance(inst, str) or not inst or not isinstance(var, str) or not var:
                            _fail(f"设备序列 {sid!r} 缺少 instance_id/variable_id")
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
                elif tag == "positions":
                    if current_series is None:
                        _fail("positions 缺少前置 series")
                    if include_solution:
                        sc = current_series["scale"]
                        off = current_series["offset"]
                        sid = current_series["series_id"]
                        vals = current_series["values"]
                        for pos in record[1]:
                            col = _require_non_bool_int(pos, f"series.{sid}.positions", minimum=0)
                            try:
                                raw_x = primals[col]
                            except IndexError:
                                _fail(f"序列 {sid!r} 列索引越界: {col}")
                            vals.append(
                                _apply_affine(raw_x, scale=sc, offset=off, label=f"series.{sid}[{col}]")
                            )
                elif tag == "series_end":
                    if current_series is None:
                        _fail("series_end 缺少前置 series")
                    sid = current_series["series_id"]
                    values_list = list(current_series["values"])
                    if include_solution and not values_list:
                        _fail(f"序列 {sid!r} 不能为空")
                    if current_series["kind"] == "system":
                        system_series[sid] = {
                            "block": current_series["block"],
                            "variable": current_series["variable"],
                            "unit": current_series["unit"],
                            "values": values_list,
                        }
                    else:
                        device_series[sid] = {
                            "instance_id": current_series["instance_id"],
                            "variable_id": current_series["variable_id"],
                            "unit": current_series["unit"],
                            "values": values_list,
                        }
                    current_series = None
                elif tag == "sources":
                    if include_solution:
                        sources = dict(record[1], groups=[])
                elif tag == "source_group":
                    if include_solution:
                        current_group = dict(
                            record[1], targets=[], indices=record[2] if record[2] is not None else []
                        )
                elif tag == "targets":
                    if include_solution and current_group is not None:
                        names = column_names if current_group["role"].startswith("column:") else row_names
                        try:
                            current_group["targets"].extend(names[pos] for pos in record[1])
                        except IndexError:
                            _fail("source_group targets 索引越界")
                elif tag == "indices":
                    if include_solution and current_group is not None:
                        current_group["indices"].extend(record[1])
                elif tag == "source_group_end":
                    if include_solution and sources is not None and current_group is not None:
                        sources["groups"].append(current_group)
                    current_group = None
                else:
                    _fail(f"未知参数记录标签: {tag!r}")
    except (OSError, UnicodeDecodeError) as exc:
        _fail(f"读取 {records_path} 失败: {exc}")

    if current_expr_id is not None or current_series is not None or current_group is not None:
        _fail("parameters.jsonl 存在未闭合块")

    if include_solution:
        lengths = {
            **{sid: len(entry["values"]) for sid, entry in device_series.items()},
            **{sid: len(entry["values"]) for sid, entry in system_series.items()},
        }
        if len(set(lengths.values())) > 1:
            _fail(f"所有设备与系统步序列长度必须一致: {lengths!r}")

    return {
        "variables": variables,
        "components": components,
        "facts": facts,
        "series": device_series,
        "system_series": system_series,
        "model_sources": sources,
    }


def _resolve_facts_payload(
    raw_facts: Mapping[str, Any],
    *,
    resolve_fact_node: Any,
    resolve_scale_expr: Any,
) -> dict[str, Any]:
    if "construction_units" not in raw_facts or not isinstance(raw_facts["construction_units"], list):
        _fail("facts 缺少必需字段 construction_units")

    resolved_units: list[dict[str, Any]] = []
    seen_unit_ids: set[str] = set()
    for unit_entry in raw_facts["construction_units"]:
        if not isinstance(unit_entry, Mapping):
            _fail("facts.construction_units 条目须为 JSON 对象")
        uid = unit_entry.get("unit_id")
        inst = unit_entry.get("instance_id")
        mode = unit_entry.get("mode")
        if not isinstance(uid, str) or not uid or uid in seen_unit_ids:
            _fail(f"construction_units unit_id 非法或重复: {uid!r}")
        seen_unit_ids.add(uid)
        if not isinstance(inst, str) or not inst or mode not in ("mandatory", "optional"):
            _fail(f"construction_units[{uid!r}] instance_id 或 mode 非法")
        built_raw = _require_finite_float(resolve_fact_node(unit_entry["built"]), f"{uid}.built")
        rounded = round(built_raw)
        if rounded not in (0, 1) or abs(built_raw - rounded) > 1e-6:
            _fail(f"建设状态 {uid!r}.built 不属于整数 0/1: {built_raw!r}")

        if not isinstance(unit_entry.get("scales"), list):
            _fail(f"construction_units[{uid!r}].scales 须为数组")
        scales_out: list[dict[str, Any]] = []
        seen_pvars: set[str] = set()
        for scale_entry in unit_entry["scales"]:
            if not isinstance(scale_entry, Mapping):
                _fail(f"construction_units[{uid!r}].scales 条目须为对象")
            pname = scale_entry.get("planning_variable_id")
            unit = scale_entry.get("unit")
            if not isinstance(pname, str) or not pname or pname in seen_pvars:
                _fail(f"construction_units[{uid!r}] planning_variable_id 非法或重复: {pname!r}")
            seen_pvars.add(pname)
            if not isinstance(unit, str) or not unit:
                _fail(f"construction_units[{uid!r}].{pname}.unit 须为非空字符串")
            if "scale" in scale_entry and "offset" in scale_entry:
                raw_val = _require_finite_float(
                    resolve_fact_node(scale_entry["value"]), f"{uid}.{pname}"
                )
                sc = _require_nonzero_scale(scale_entry["scale"], f"{uid}.{pname}.scale")
                off = _require_finite_float(scale_entry["offset"], f"{uid}.{pname}.offset")
                biz_val = _apply_affine(raw_val, scale=sc, offset=off, label=f"{uid}.{pname}")
            else:
                biz_val, _ = resolve_scale_expr(scale_entry["value"], unit, f"{uid}.{pname}")
            scales_out.append({"planning_variable_id": pname, "value": biz_val, "unit": unit})

        resolved_units.append(
            {
                "unit_id": uid,
                "instance_id": inst,
                "mode": mode,
                "built": bool(rounded),
                "scales": scales_out,
            }
        )

    out: dict[str, Any] = {"construction_units": resolved_units}
    if _FULL_FINANCIAL_KEYS <= set(raw_facts):
        resolved_full = resolve_fact_node(raw_facts)
        out["financial_facts"] = _recover_full_financial_facts(resolved_full, resolved_units)
    elif "currency" in raw_facts:
        missing = sorted(_FULL_FINANCIAL_KEYS - set(raw_facts))
        _fail(f"完整财务事实缺少必需字段: {missing!r}")
    return out


def _facts_amount(total: float, label: str) -> str:
    if not math.isfinite(total):
        _fail(f"财务事实 {label} 非有限")
    return str(Decimal(str(total)))


def _recover_full_financial_facts(
    decl: Mapping[str, Any],
    construction_units: list[dict[str, Any]],
) -> dict[str, Any]:
    def amt(val: float, label: str) -> str:
        return _facts_amount(float(val), label)

    out: dict[str, Any] = {
        "currency": decl["currency"],
        "construction_total": amt(decl["construction_total"], "construction_total"),
        "annual_fixed_om": amt(decl["annual_fixed_om"], "annual_fixed_om"),
        "payments_applied": [
            {"t": e["t"], "ratio": e["ratio"], "amount": amt(e["amount"], "payments_applied")}
            for e in decl["payments_applied"]
        ],
        "net_cashflows": [
            {
                "id": e["id"],
                "t": e["t"],
                "direction": e["direction"],
                "amount": amt(e["amount"], "net_cashflows"),
            }
            for e in decl["net_cashflows"]
        ],
        "annual_operation": [
            {
                "financial_year": e["financial_year"],
                "energy_net": amt(e["energy_net"], "annual_operation.energy_net"),
                "variable_om": amt(e["variable_om"], "annual_operation.variable_om"),
                "fixed_om": amt(e["fixed_om"], "annual_operation.fixed_om"),
                "maintenance": amt(e["maintenance"], "annual_operation.maintenance"),
                "demand_charge": amt(e["demand_charge"], "annual_operation.demand_charge"),
                "net": amt(e["net"], "annual_operation.net"),
            }
            for e in decl["annual_operation"]
        ],
        "tax_cash": {
            "vat": [
                {
                    "pool": e["pool"],
                    "month": e["month"],
                    "paid": amt(e["paid"], "tax_cash.vat"),
                    "refund_claimed": amt(e["refund_claimed"], "tax_cash.vat"),
                    "refund_arrived": amt(e["refund_arrived"], "tax_cash.vat"),
                    "carry": amt(e["carry"], "tax_cash.vat"),
                    "receivable": amt(e["receivable"], "tax_cash.vat"),
                }
                for e in decl["tax_cash"]["vat"]
            ],
            "vat_rules": [
                {
                    "rule": e["rule"],
                    "pool": e["pool"],
                    "month": e["month"],
                    "output_tax": amt(e["output_tax"], "tax_cash.vat_rules"),
                    "deductible_input": amt(e["deductible_input"], "tax_cash.vat_rules"),
                }
                for e in decl["tax_cash"]["vat_rules"]
            ],
            "refunds": [
                {
                    "pool": e["pool"],
                    "period": e["period"],
                    "arrival_period": e["arrival_period"],
                    "amount": amt(e["amount"], "tax_cash.refunds"),
                }
                for e in decl["tax_cash"]["refunds"]
            ],
            "income": [
                {
                    "rule": e["rule"],
                    "year": e["year"],
                    "taxable_base": amt(e["taxable_base"], "tax_cash.income"),
                    "loss_used": amt(e["loss_used"], "tax_cash.income"),
                    "loss_generated": amt(e["loss_generated"], "tax_cash.income"),
                    "loss_expired": amt(e["loss_expired"], "tax_cash.income"),
                    "assessed": amt(e["assessed"], "tax_cash.income"),
                    "tax": amt(e["tax"], "tax_cash.income"),
                }
                for e in decl["tax_cash"]["income"]
            ],
            "depreciation": [
                {
                    "rule": e["rule"],
                    "base_from": dict(e["base_from"]),
                    "year": e["year"],
                    "amount": amt(e["amount"], "tax_cash.depreciation"),
                }
                for e in decl["tax_cash"]["depreciation"]
            ],
            "loss_balances": [
                {
                    "rule": e["rule"],
                    "vintage_id": e["vintage_id"],
                    "year": e["year"],
                    "last_usable_year": e["last_usable_year"],
                    "amount": amt(e["amount"], "tax_cash.loss_balances"),
                }
                for e in decl["tax_cash"]["loss_balances"]
            ],
            "derived": [
                {
                    "rule": e["rule"],
                    "axis": e["axis"],
                    "period": e["period"],
                    "assessed": amt(e["assessed"], "tax_cash.derived"),
                    "payment_period": e["payment_period"],
                    "paid": amt(e["paid"], "tax_cash.derived"),
                }
                for e in decl["tax_cash"]["derived"]
            ],
        },
        "monthly_demand": [
            {
                "billing_id": e["billing_id"],
                "charge_id": e["charge_id"],
                "calendar_year": e["calendar_year"],
                "calendar_month": e["calendar_month"],
                "financial_year": e["financial_year"],
                "peak_power": amt(e["peak_power"], "monthly_demand.peak_power"),
                "peak_unit": e["peak_unit"],
                "rate": dict(e["rate"]),
                "charge": amt(e["charge"], "monthly_demand.charge"),
            }
            for e in decl["monthly_demand"]
        ],
        "annual_net_flows": [amt(node, "annual_net_flows") for node in decl["annual_net_flows"]],
        "construction_units": construction_units,
        "cost_items": [
            {
                **{
                    k: e[k]
                    for k in ("instance_id", "finance_type", "component", "item_id", "axis", "period", "unit")
                },
                **{
                    k: amt(e[k], f"cost_items.{k}")
                    for k in ("raw", "gross", "deductible_input", "net_basis")
                },
            }
            for e in decl["cost_items"]
        ],
        "amounts": [
            {
                "id": e["id"],
                "axis": e["axis"],
                "unit": e["unit"],
                "values": [
                    {"period": item["period"], "value": amt(item["value"], f"amounts.{e['id']}")}
                    for item in e["values"]
                ],
            }
            for e in decl["amounts"]
        ],
        "cash_movements": [
            {
                **{k: e[k] for k in ("id", "axis", "period", "direction", "price_basis")},
                **{
                    k: amt(e[k], f"cash_movements.{e['id']}.{k}")
                    for k in (
                        "raw",
                        "gross",
                        "deductible_input",
                        "net_basis",
                        "output_tax",
                        "net_revenue",
                        "end_unpaid",
                    )
                },
                "payments": [
                    {
                        "at": dict(p["at"]),
                        "ratio": p["ratio"],
                        "amount": amt(p["amount"], "cash_movements.payment"),
                    }
                    for p in e["payments"]
                ],
            }
            for e in decl["cash_movements"]
        ],
        "assets": [
            {
                "id": e["id"],
                "basis": amt(e["basis"], f"assets.{e['id']}.basis"),
                "capitalized_at": dict(e["capitalized_at"]),
                "available_from": e["available_from"],
                "book_values": [
                    {"year": v["year"], "value": amt(v["value"], f"assets.{e['id']}.book_values")}
                    for v in e["book_values"]
                ],
            }
            for e in decl["assets"]
        ],
    }
    comparison = dict(decl["comparison"])
    if comparison["mode"] == "difference":
        comparison["evaluation_net_flows"] = [
            amt(node, "comparison.evaluation_net_flows") for node in comparison["evaluation_net_flows"]
        ]
    out["comparison"] = comparison
    return out


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
        solution_out_path.write_text(
            json.dumps(eng_sol, ensure_ascii=False, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        result_meta_path.write_text(
            json.dumps(
                {
                    "math_status": math_status,
                    "has_feasible_solution": False,
                    "extra_outputs": {"solver_summary": eng_sol["solver_summary"]},
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

    solver_summary: dict[str, Any] = {
        "termination_reason": termination_reason,
        "math_status": math_status,
        "has_feasible_solution": True,
        "objective_value": business_obj,
        "objective_unit": header["objective_unit"],
        "best_bound": business_bound,
        "relative_gap": relative_gap,
    }
    if diagnostics:
        solver_summary["solver_diagnostics"] = diagnostics

    include_solution = "solution" in header["outputs"]
    recovered = _recover_variable_map_records(records_path, primals, include_solution=True)
    facts_payload = recovered["facts"]
    if not isinstance(facts_payload, Mapping) or "construction_units" not in facts_payload:
        _fail("有可行解时 variable_map 缺少 facts.construction_units")

    components_out: dict[str, Any] = {}
    for ident, decl in recovered["components"].items():
        scale, offset = linear_conversion_for_unit(decl["unit"])
        val = _apply_affine(decl["raw_total"], scale=scale, offset=offset, label=f"component.{ident}")
        components_out[ident] = {"value": val, "unit": decl["unit"], "caliber": decl["caliber"]}

    eng_sol: dict[str, Any] = {
        "schema": ENGINEERING_SOLUTION_SCHEMA,
        "schema_version": ENGINEERING_SOLUTION_VERSION,
        "solver_summary": solver_summary,
        "construction_units": facts_payload["construction_units"],
        "series": recovered["series"],
        "system_series": recovered["system_series"],
        "extensions": {},
    }
    if recovered["model_sources"] is not None:
        eng_sol["model_sources"] = recovered["model_sources"]

    extra_outputs: dict[str, Any] = {"solver_summary": solver_summary}
    if "metrics" in header["outputs"]:
        metrics = {
            "termination_reason": termination_reason,
            "math_status": math_status,
            "objective_value": business_obj,
            "objective_unit": header["objective_unit"],
            "num_variables": len(recovered["variables"]),
        }
        eng_sol["metrics"] = metrics
        extra_outputs["metrics"] = metrics

    if include_solution:
        extra_outputs["solution"] = {
            "objective_value": business_obj,
            "objective_unit": header["objective_unit"],
            "variables": recovered["variables"],
            "components": components_out,
        }
        extra_outputs["series"] = recovered["series"]
        extra_outputs["system_series"] = recovered["system_series"]
        if recovered["model_sources"] is not None:
            extra_outputs["model_sources"] = recovered["model_sources"]
        if "financial_facts" in facts_payload:
            extra_outputs["financial_facts"] = facts_payload["financial_facts"]

    solution_out_path.write_text(
        json.dumps(eng_sol, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    result_meta_path.write_text(
        json.dumps(
            {
                "math_status": math_status,
                "has_feasible_solution": True,
                "extra_outputs": extra_outputs,
            },
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n",
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
    memory_limit_mb: int | None,
) -> None:
    """Spawned child-process entrypoint (only lightweight path strings cross IPC)."""
    if memory_limit_mb is not None and memory_limit_mb > 0:
        limit_bytes = int(memory_limit_mb) * 1024 * 1024
        # Add limit_bytes above pre-mapped extension virtual arenas (e.g. OpenBLAS/HiGHS)
        # so RLIMIT_AS bounds restoration growth rather than failing at import baseline.
        baseline_vm = _current_vm_bytes() or 0
        requested_as = baseline_vm + limit_bytes
        try:
            _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            target = requested_as if hard == resource.RLIM_INFINITY else min(requested_as, hard)
            resource.setrlimit(resource.RLIMIT_AS, (target, hard))
        except (ValueError, OSError):
            pass
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


@dataclass(frozen=True, slots=True)
class RestorationOutcome:
    """Outcome from bounded-subprocess restoration including child PID for isolation tests."""

    compute_result: ComputeResult
    engineering_solution: Mapping[str, Any]
    child_pid: int | None


def _stream_content_to_path(content: Content | bytes, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = MemoryContent(content) if isinstance(content, bytes) else content
    with dest.open("wb") as handle:
        for chunk in raw.iter_chunks():
            handle.write(chunk)


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
    bundle_id = receipt["bundle_id"] if isinstance(receipt, Mapping) else receipt.bundle_id
    receipt_id = receipt["receipt_id"] if isinstance(receipt, Mapping) else receipt.receipt_id
    receipt_status = receipt["status"] if isinstance(receipt, Mapping) else receipt.status
    term_reason = (
        str(receipt.get("termination_reason", ""))
        if isinstance(receipt, Mapping)
        else receipt.termination_reason
    )

    exec_status = _execution_status_from_receipt(str(receipt_status), term_reason)
    if receipt_status != "succeeded":
        eng_sol = _non_feasible_solution(
            math_status="not_solved",
            termination_reason="not_solved",
            objective_unit=None,
        )
        if solution_output_path is not None:
            solution_output_path.parent.mkdir(parents=True, exist_ok=True)
            solution_output_path.write_text(
                json.dumps(eng_sol, ensure_ascii=False, allow_nan=False) + "\n",
                encoding="utf-8",
            )
        return RestorationOutcome(
            compute_result=ComputeResult(
                bundle_id=str(bundle_id),
                receipt_id=str(receipt_id),
                execution_status=exec_status,
                math_status="not_solved",
                outputs=freeze_owned_json({"solver_summary": dict(eng_sol["solver_summary"])}),
            ),
            engineering_solution=eng_sol,
            child_pid=None,
        )

    pre_stop = read_stop_reason(stop)
    if pre_stop is not None:
        raise ComputationUnavailableError(f"结果还原启动前已停止: {pre_stop}", reason=pre_stop)

    with tempfile.TemporaryDirectory(prefix="ies-restore-") as tmp_dir:
        tmp = Path(tmp_dir)
        sol_path = solution_output_path or (tmp / "solution.json")
        sol_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path = tmp / "restore_meta.json"

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
        started = time.monotonic()
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
                if timeout_seconds is not None and (time.monotonic() - started) > timeout_seconds:
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
            _fail(
                f"受限子进程异常退出 (exitcode={proc.exitcode})",
                reason=reason,
            )

        sol_text = sol_path.read_text(encoding="utf-8")
        eng_sol = json.loads(sol_text)
        extra_outputs = dict(meta_doc["extra_outputs"])
        extra_outputs["engineering_solution"] = json.loads(sol_text)
        return RestorationOutcome(
            compute_result=ComputeResult(
                bundle_id=str(bundle_id),
                receipt_id=str(receipt_id),
                execution_status="completed",
                math_status=str(meta_doc["math_status"]),
                outputs=freeze_owned_json(extra_outputs),
            ),
            engineering_solution=eng_sol,
            child_pid=child_pid,
        )


class TargetResultAdapter:
    """Universal F2 ResultAdapter restoring ``ies.primal-solution@1.0.0`` via spawn subprocess."""

    __slots__ = ()

    def restore_with_outcome(
        self,
        bundle: TargetSolverBundle,
        receipt: ExecutionReceipt | Mapping[str, Any],
        outputs: Mapping[str, object],
        *,
        memory_limit_mb: int | None = None,
        timeout_seconds: float | None = None,
        stop: StopProbe | None = None,
    ) -> RestorationOutcome:
        """Materialize streamed bundle inputs/outputs to temp dir and run bounded spawn restoration."""
        if not isinstance(bundle, TargetSolverBundle):
            raise TypeError(f"bundle 须为 TargetSolverBundle，得到 {type(bundle).__name__}")
        receipt_status = receipt["status"] if isinstance(receipt, Mapping) else receipt.status
        if receipt_status != "succeeded":
            return restore_from_bundle_dir(
                Path("."),
                Path("."),
                receipt,
                memory_limit_mb=memory_limit_mb,
                timeout_seconds=timeout_seconds,
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
                _stream_content_to_path(content, root / rel_path)
            primal_path = root / PRIMAL_SOLUTION_OUTPUT_PATH
            _stream_content_to_path(primal_raw, primal_path)
            return restore_from_bundle_dir(
                root,
                primal_path,
                receipt,
                memory_limit_mb=memory_limit_mb,
                timeout_seconds=timeout_seconds,
                stop=stop,
            )

    def restore(
        self,
        bundle: TargetSolverBundle,
        receipt: ExecutionReceipt | Mapping[str, Any],
        outputs: Mapping[str, object],
        *,
        memory_limit_mb: int | None = None,
        timeout_seconds: float | None = None,
        stop: StopProbe | None = None,
    ) -> ComputeResult:
        """Restore ``ComputeResult`` containing ``ies.engineering-solution@1.0.0``."""
        return self.restore_with_outcome(
            bundle,
            receipt,
            outputs,
            memory_limit_mb=memory_limit_mb,
            timeout_seconds=timeout_seconds,
            stop=stop,
        ).compute_result


__all__ = [
    "ENGINEERING_SOLUTION_SCHEMA",
    "ENGINEERING_SOLUTION_VERSION",
    "PRIMAL_SOLUTION_SCHEMA",
    "PRIMAL_SOLUTION_VERSION",
    "RestorationOutcome",
    "TargetResultAdapter",
    "restore_from_bundle_dir",
]
