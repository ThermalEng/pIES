"""Canonical runtime output contract validators for engineering solutions and evaluator outputs."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any, NoReturn

from iesplan.computation.protocols import MATH_STATUSES, check_finite_json, check_provider_ref

ENGINEERING_SOLUTION_SCHEMA = "ies.engineering-solution"
ENGINEERING_SOLUTION_VERSION = "1.0.0"

FINANCIAL_EVALUATION_SCHEMA = "ies.financial-evaluation"
FINANCIAL_EVALUATION_VERSION = "1.0.0"

EVALUATION_REPORT_SCHEMA = "ies.evaluation-report"
EVALUATION_REPORT_VERSION = "1.0.0"

_SOLVER_TERMINATION_REASONS = frozenset(
    ("optimal", "time_limit", "infeasible", "unbounded", "not_solved")
)
_FEASIBLE_MATH_STATUSES = frozenset(("solved", "feasible"))
_CONSTRUCTION_UNIT_MODES = frozenset(("mandatory", "optional"))
_CHART_TYPES = frozenset(("line", "bar", "area", "scatter"))

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
_SOLVER_SUMMARY_REQUIRED_KEYS = frozenset(
    (
        "termination_reason",
        "math_status",
        "has_feasible_solution",
        "objective_value",
        "objective_unit",
        "best_bound",
        "relative_gap",
    )
)
_SOLVER_SUMMARY_ALLOWED_KEYS = _SOLVER_SUMMARY_REQUIRED_KEYS | frozenset(("solver_diagnostics",))

_REPORT_REQUIRED_KEYS = frozenset(
    ("schema", "schema_version", "title", "evaluator", "metrics", "charts", "tables")
)
_REPORT_ALLOWED_KEYS = _REPORT_REQUIRED_KEYS | frozenset(("summary", "extensions"))

_FIN_EVAL_STR_FIELDS = (
    "status",
    "currency",
    "price_basis",
    "construction_total",
    "construction_total_unit",
    "annual_fixed_om",
)
_FIN_EVAL_INT_MINS = (
    ("evaluation_years", 1),
    ("operation_years", 1),
    ("construction_period", 0),
    ("base_year", 1),
)
_FIN_EVAL_LIST_FIELDS = (
    "payments_applied",
    "net_cashflows",
    "cost_items",
    "amounts",
    "cash_movements",
    "assets",
    "construction_units",
    "annual_operation",
    "monthly_demand",
    "annual_net_flows",
)
_FIN_EVAL_TAX_LIST_FIELDS = (
    "depreciation",
    "derived",
    "income",
    "loss_balances",
    "refunds",
    "vat",
    "vat_rules",
)


def _default_raise(message: str) -> NoReturn:
    raise ValueError(message)


def _require_str(value: object, label: str, fail: Callable[[str], NoReturn]) -> str:
    if not isinstance(value, str) or not value:
        fail(f"{label} 须为非空字符串，得到 {value!r}")
    return value


def _require_finite_num(value: object, label: str, fail: Callable[[str], NoReturn]) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(f"{label} 须为有限数值，得到 {value!r}")
    num = float(value)
    if not math.isfinite(num):
        fail(f"{label} 非有限数值: {value!r}")
    return num


def _require_opt_finite_num(
    value: object, label: str, fail: Callable[[str], NoReturn]
) -> float | None:
    if value is None:
        return None
    return _require_finite_num(value, label, fail)


def _require_list(value: object, label: str, fail: Callable[[str], NoReturn]) -> list[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, list):
        fail(f"{label} 须为数组")
    return value


def _check_keys(
    data: Mapping[str, Any],
    *,
    required: frozenset[str],
    allowed: frozenset[str],
    label: str,
    fail: Callable[[str], NoReturn],
) -> None:
    extra = sorted(k for k in data if k not in allowed)
    if extra:
        fail(f"{label} 包含未知字段: {extra!r}")
    missing = sorted(k for k in required if k not in data)
    if missing:
        fail(f"{label} 缺少必需字段: {missing!r}")


def _validate_scale(item: Any, label: str, fail: Callable[[str], NoReturn]) -> str:
    if not isinstance(item, Mapping):
        fail(f"{label} 须为 JSON 对象")
    if set(item) != {"planning_variable_id", "value", "unit"}:
        fail(f"{label} 字段集合非法: {sorted(item)!r}")
    pvar_id = _require_str(item["planning_variable_id"], f"{label}.planning_variable_id", fail)
    _require_finite_num(item["value"], f"{label}.value", fail)
    _require_str(item["unit"], f"{label}.unit", fail)
    return pvar_id


def _validate_solver_summary(
    summary: Any, fail: Callable[[str], NoReturn]
) -> tuple[str, bool]:
    if not isinstance(summary, Mapping):
        fail("solution.json 缺少 solver_summary 对象")
    _check_keys(
        summary,
        required=_SOLVER_SUMMARY_REQUIRED_KEYS,
        allowed=_SOLVER_SUMMARY_ALLOWED_KEYS,
        label="solver_summary",
        fail=fail,
    )
    term = summary["termination_reason"]
    if term not in _SOLVER_TERMINATION_REASONS:
        fail(f"solver_summary.termination_reason 非法: {term!r}")
    math_status = summary["math_status"]
    if math_status not in MATH_STATUSES:
        fail(f"solver_summary.math_status 非法: {math_status!r}")
    has_feasible = summary["has_feasible_solution"]
    if not isinstance(has_feasible, bool):
        fail("solver_summary.has_feasible_solution 须为布尔值")

    obj = _require_opt_finite_num(summary["objective_value"], "solver_summary.objective_value", fail)
    _require_opt_finite_num(summary["best_bound"], "solver_summary.best_bound", fail)
    _require_opt_finite_num(summary["relative_gap"], "solver_summary.relative_gap", fail)
    obj_unit = summary["objective_unit"]
    if obj_unit is not None:
        _require_str(obj_unit, "solver_summary.objective_unit", fail)

    if has_feasible:
        if math_status not in _FEASIBLE_MATH_STATUSES:
            fail(
                "solver_summary.has_feasible_solution=true 要求 math_status 为 solved/feasible, "
                f"实际为 {math_status!r}"
            )
        if obj is None or obj_unit is None:
            fail("solver_summary.has_feasible_solution=true 要求非空 objective_value 与 objective_unit")
    elif math_status in _FEASIBLE_MATH_STATUSES:
        fail(f"solver_summary.has_feasible_solution=false 禁止 math_status={math_status!r}")

    diag = summary.get("solver_diagnostics")
    if diag is not None and not isinstance(diag, Mapping):
        fail("solver_summary.solver_diagnostics 须为 JSON 对象")
    return str(math_status), has_feasible


def _validate_series_map(
    raw: Mapping[str, Any],
    *,
    required_keys: set[str],
    label: str,
    fail: Callable[[str], NoReturn],
) -> dict[str, int]:
    lengths: dict[str, int] = {}
    for sid, entry in raw.items():
        _require_str(sid, f"{label} key", fail)
        if not isinstance(entry, Mapping):
            fail(f"{label}[{sid!r}] 须为 JSON 对象")
        if set(entry) != required_keys:
            fail(f"{label}[{sid!r}] 字段集合非法: {sorted(entry)!r}")
        for str_key in required_keys - {"values"}:
            _require_str(entry[str_key], f"{label}[{sid!r}].{str_key}", fail)
        values = _require_list(entry["values"], f"{label}[{sid!r}].values", fail)
        if not values:
            fail(f"{label}[{sid!r}].values 不得为空")
        for idx, val in enumerate(values):
            _require_finite_num(val, f"{label}[{sid!r}].values[{idx}]", fail)
        lengths[sid] = len(values)
    return lengths


def validate_engineering_solution_doc(
    data: Any,
    *,
    on_error: Callable[[str], NoReturn] | None = None,
) -> Mapping[str, Any]:
    """Validate canonical ``ies.engineering-solution@1.0.0`` structure and dual-state invariants."""
    fail = on_error or _default_raise
    if not isinstance(data, Mapping):
        fail("solution.json 须为 JSON 对象")
    _check_keys(
        data,
        required=_ENG_REQUIRED_KEYS,
        allowed=_ENG_ALLOWED_KEYS,
        label="solution.json",
        fail=fail,
    )
    if (
        data["schema"] != ENGINEERING_SOLUTION_SCHEMA
        or data["schema_version"] != ENGINEERING_SOLUTION_VERSION
    ):
        fail(f"solution.json schema 非法: {data.get('schema')!r}@{data.get('schema_version')!r}")

    _validate_solver_summary(data["solver_summary"], fail)

    raw_units = data.get("construction_units")
    if raw_units is not None:
        units = _require_list(raw_units, "solution.construction_units", fail)
        seen_units: set[str] = set()
        for idx, unit in enumerate(units):
            if not isinstance(unit, Mapping):
                fail(f"construction_units[{idx}] 须为 JSON 对象")
            if set(unit) != {"unit_id", "instance_id", "mode", "built", "scales"}:
                fail(f"construction_units[{idx}] 字段集合非法: {sorted(unit)!r}")
            uid = _require_str(unit["unit_id"], f"construction_units[{idx}].unit_id", fail)
            if uid in seen_units:
                fail(f"construction_units 存在重复 unit_id: {uid!r}")
            seen_units.add(uid)
            _require_str(unit["instance_id"], f"construction_units[{idx}].instance_id", fail)
            if unit["mode"] not in _CONSTRUCTION_UNIT_MODES:
                fail(f"construction_units[{idx}].mode 非法: {unit['mode']!r}")
            if not isinstance(unit["built"], bool):
                fail(f"construction_units[{idx}].built 须为布尔值")
            scales = _require_list(unit["scales"], f"construction_units[{idx}].scales", fail)
            seen_unit_pvars: set[str] = set()
            for s_idx, scale in enumerate(scales):
                pvar_id = _validate_scale(
                    scale, f"construction_units[{idx}].scales[{s_idx}]", fail
                )
                if pvar_id in seen_unit_pvars:
                    fail(f"construction_units[{uid!r}] 存在重复 planning_variable_id: {pvar_id!r}")
                seen_unit_pvars.add(pvar_id)

    raw_series = data.get("series")
    if raw_series is not None:
        if not isinstance(raw_series, Mapping):
            fail("solution.series 须为 JSON 对象或 null")
        series_lengths = _validate_series_map(
            raw_series,
            required_keys={"instance_id", "variable_id", "unit", "values"},
            label="solution.series",
            fail=fail,
        )
    else:
        series_lengths = {}

    raw_sys = data.get("system_series")
    if raw_sys is not None:
        if not isinstance(raw_sys, Mapping):
            fail("solution.system_series 须为 JSON 对象或 null")
        sys_lengths = _validate_series_map(
            raw_sys,
            required_keys={"block", "variable", "unit", "values"},
            label="solution.system_series",
            fail=fail,
        )
    else:
        sys_lengths = {}

    raw_pvars = data.get("planning_variables")
    if raw_pvars is not None:
        pvars = _require_list(raw_pvars, "solution.planning_variables", fail)
        seen_pvars: set[str] = set()
        for idx, scale in enumerate(pvars):
            pvar_id = _validate_scale(scale, f"solution.planning_variables[{idx}]", fail)
            if pvar_id in seen_pvars:
                fail(f"planning_variables 存在重复 planning_variable_id: {pvar_id!r}")
            seen_pvars.add(pvar_id)

    has_feasible = bool(data["solver_summary"]["has_feasible_solution"])
    if has_feasible:
        if raw_units is None or raw_series is None or raw_sys is None:
            fail("有可行解时 solution.json 缺少 construction_units/series/system_series")
        overlap = sorted(set(raw_series) & set(raw_sys))
        if overlap:
            fail(f"series 与 system_series 存在重复键: {overlap!r}")
        step_lengths = {**series_lengths, **sys_lengths}
        if len(set(step_lengths.values())) > 1:
            fail(f"series 与 system_series 时步长度不一致: {step_lengths!r}")
    else:
        if raw_units:
            fail("无可行解时 construction_units 不得非空")
        if raw_series:
            fail("无可行解时 series 不得非空")
        if raw_sys:
            fail("无可行解时 system_series 不得非空")
        if raw_pvars:
            fail("无可行解时 planning_variables 不得非空")

    for opt_key in ("metrics", "model_sources"):
        val = data.get(opt_key)
        if val is not None and not isinstance(val, Mapping):
            fail(f"solution.{opt_key} 须为 JSON 对象或 null")
    if "extensions" in data and not isinstance(data["extensions"], Mapping):
        fail("solution.extensions 须为 JSON 对象")

    try:
        check_finite_json(data, "solution.json")
    except ValueError as exc:
        fail(str(exc))
    return data


def validate_evaluation_report_doc(
    data: Any,
    *,
    expected_evaluator: str | None = None,
) -> dict[str, Any]:
    """Validate canonical ``ies.evaluation-report@1.0.0`` structure and nested items."""
    fail = _default_raise
    if not isinstance(data, Mapping):
        fail("evaluation_report.json 须为 JSON 对象")
    _check_keys(
        data,
        required=_REPORT_REQUIRED_KEYS,
        allowed=_REPORT_ALLOWED_KEYS,
        label="evaluation_report.json",
        fail=fail,
    )
    if data["schema"] != EVALUATION_REPORT_SCHEMA or data["schema_version"] != EVALUATION_REPORT_VERSION:
        fail(
            f"evaluation_report.json schema 非法: {data.get('schema')!r}@{data.get('schema_version')!r}"
        )
    _require_str(data["title"], "evaluation_report.title", fail)
    evaluator = check_provider_ref(data["evaluator"], "evaluation_report.evaluator")
    if expected_evaluator is not None and evaluator != expected_evaluator:
        fail(f"evaluation_report.json evaluator 不匹配: {evaluator!r} != {expected_evaluator!r}")
    if "summary" in data and data["summary"] is not None and not isinstance(data["summary"], str):
        fail("evaluation_report.summary 须为字符串或 null")
    if "extensions" in data and not isinstance(data["extensions"], Mapping):
        fail("evaluation_report.extensions 须为 JSON 对象")

    metrics = _require_list(data["metrics"], "evaluation_report.metrics", fail)
    seen_metrics: set[str] = set()
    for idx, metric in enumerate(metrics):
        if not isinstance(metric, Mapping):
            fail(f"evaluation_report.metrics[{idx}] 须为 JSON 对象")
        if set(metric) != {"id", "label", "value", "unit"}:
            fail(f"evaluation_report.metrics[{idx}] 字段集合非法: {sorted(metric)!r}")
        mid = _require_str(metric["id"], f"metrics[{idx}].id", fail)
        if mid in seen_metrics:
            fail(f"evaluation_report.metrics 存在重复 id: {mid!r}")
        seen_metrics.add(mid)
        _require_str(metric["label"], f"metrics[{idx}].label", fail)
        _require_str(metric["unit"], f"metrics[{idx}].unit", fail)
        val = metric["value"]
        if isinstance(val, bool):
            fail(f"metrics[{mid!r}].value 不得为布尔值")
        elif isinstance(val, (int, float)):
            _require_finite_num(val, f"metrics[{mid!r}].value", fail)
        elif isinstance(val, str):
            _require_str(val, f"metrics[{mid!r}].value", fail)
        elif val is not None:
            fail(f"metrics[{mid!r}].value 须为有限数值、非空字符串或 null")

    charts = _require_list(data["charts"], "evaluation_report.charts", fail)
    seen_charts: set[str] = set()
    for idx, chart in enumerate(charts):
        if not isinstance(chart, Mapping):
            fail(f"evaluation_report.charts[{idx}] 须为 JSON 对象")
        _check_keys(
            chart,
            required=frozenset(("id", "title", "x_label", "y_label", "y_unit", "series")),
            allowed=frozenset(
                ("id", "title", "x_label", "y_label", "y_unit", "x_unit", "chart_type", "x_values", "series")
            ),
            label=f"charts[{idx}]",
            fail=fail,
        )
        cid = _require_str(chart["id"], f"charts[{idx}].id", fail)
        if cid in seen_charts:
            fail(f"evaluation_report.charts 存在重复 id: {cid!r}")
        seen_charts.add(cid)
        for key in ("title", "x_label", "y_label", "y_unit"):
            _require_str(chart[key], f"charts[{cid!r}].{key}", fail)
        if "x_unit" in chart and chart["x_unit"] is not None:
            _require_str(chart["x_unit"], f"charts[{cid!r}].x_unit", fail)
        if "chart_type" in chart and chart["chart_type"] not in _CHART_TYPES:
            fail(f"charts[{cid!r}].chart_type 非法: {chart['chart_type']!r}")
        series = _require_list(chart["series"], f"charts[{cid!r}].series", fail)
        if not series:
            fail(f"charts[{cid!r}].series 不得为空")
        seen_series: set[str] = set()
        series_lengths: list[int] = []
        for s_idx, item in enumerate(series):
            if not isinstance(item, Mapping):
                fail(f"charts[{cid!r}].series[{s_idx}] 须为 JSON 对象")
            _check_keys(
                item,
                required=frozenset(("id", "label", "values")),
                allowed=frozenset(("id", "label", "values", "unit")),
                label=f"charts[{cid!r}].series[{s_idx}]",
                fail=fail,
            )
            sid = _require_str(item["id"], f"charts[{cid!r}].series[{s_idx}].id", fail)
            if sid in seen_series:
                fail(f"charts[{cid!r}].series 存在重复 id: {sid!r}")
            seen_series.add(sid)
            _require_str(item["label"], f"charts[{cid!r}].series[{sid!r}].label", fail)
            if "unit" in item and item["unit"] is not None:
                _require_str(item["unit"], f"charts[{cid!r}].series[{sid!r}].unit", fail)
            vals = _require_list(item["values"], f"charts[{cid!r}].series[{sid!r}].values", fail)
            for v_idx, val in enumerate(vals):
                _require_opt_finite_num(val, f"charts[{cid!r}].series[{sid!r}].values[{v_idx}]", fail)
            series_lengths.append(len(vals))
        if "x_values" in chart:
            x_vals = _require_list(chart["x_values"], f"charts[{cid!r}].x_values", fail)
            for x_idx, xv in enumerate(x_vals):
                if isinstance(xv, bool):
                    fail(f"charts[{cid!r}].x_values[{x_idx}] 不得为布尔值")
                elif isinstance(xv, (int, float)):
                    _require_finite_num(xv, f"charts[{cid!r}].x_values[{x_idx}]", fail)
                elif not isinstance(xv, str):
                    fail(f"charts[{cid!r}].x_values[{x_idx}] 须为字符串或有限数值")
            for s_len in series_lengths:
                if s_len != len(x_vals):
                    fail(f"charts[{cid!r}] series 长度 {s_len} 与 x_values 长度 {len(x_vals)} 不一致")

    tables = _require_list(data["tables"], "evaluation_report.tables", fail)
    seen_tables: set[str] = set()
    for idx, table in enumerate(tables):
        if not isinstance(table, Mapping):
            fail(f"evaluation_report.tables[{idx}] 须为 JSON 对象")
        if set(table) != {"id", "title", "columns", "rows"}:
            fail(f"evaluation_report.tables[{idx}] 字段集合非法: {sorted(table)!r}")
        tid = _require_str(table["id"], f"tables[{idx}].id", fail)
        if tid in seen_tables:
            fail(f"evaluation_report.tables 存在重复 id: {tid!r}")
        seen_tables.add(tid)
        _require_str(table["title"], f"tables[{tid!r}].title", fail)
        cols = _require_list(table["columns"], f"tables[{tid!r}].columns", fail)
        if not cols:
            fail(f"tables[{tid!r}].columns 不得为空")
        seen_cols: set[str] = set()
        for c_idx, col in enumerate(cols):
            if not isinstance(col, Mapping):
                fail(f"tables[{tid!r}].columns[{c_idx}] 须为 JSON 对象")
            _check_keys(
                col,
                required=frozenset(("id", "label")),
                allowed=frozenset(("id", "label", "unit")),
                label=f"tables[{tid!r}].columns[{c_idx}]",
                fail=fail,
            )
            col_id = _require_str(col["id"], f"tables[{tid!r}].columns[{c_idx}].id", fail)
            if col_id in seen_cols:
                fail(f"tables[{tid!r}].columns 存在重复 id: {col_id!r}")
            seen_cols.add(col_id)
            _require_str(col["label"], f"tables[{tid!r}].columns[{col_id!r}].label", fail)
            if "unit" in col and col["unit"] is not None:
                _require_str(col["unit"], f"tables[{tid!r}].columns[{col_id!r}].unit", fail)
        rows = _require_list(table["rows"], f"tables[{tid!r}].rows", fail)
        for r_idx, row in enumerate(rows):
            row_list = _require_list(row, f"tables[{tid!r}].rows[{r_idx}]", fail)
            if len(row_list) != len(cols):
                fail(
                    f"tables[{tid!r}].rows[{r_idx}] 列数 {len(row_list)} 与 columns 数量 {len(cols)} 不一致"
                )
            for c_idx, cell in enumerate(row_list):
                if cell is not None and not isinstance(cell, (str, int, float, bool)):
                    fail(f"tables[{tid!r}].rows[{r_idx}][{c_idx}] 单元格类型非法")

    check_finite_json(data, "evaluation_report.json")
    return dict(data)


def validate_financial_evaluation_doc(data: Any) -> dict[str, Any]:
    """Validate canonical ``ies.financial-evaluation@1.0.0`` structure and required facts."""
    fail = _default_raise
    if not isinstance(data, Mapping):
        fail("financial_evaluation.json 须为 JSON 对象")
    if data.get("schema") != FINANCIAL_EVALUATION_SCHEMA:
        fail(f"financial_evaluation.json schema 非法: {data.get('schema')!r}")
    if data.get("schema_version") != FINANCIAL_EVALUATION_VERSION:
        fail(f"financial_evaluation.json schema_version 非法: {data.get('schema_version')!r}")

    for key in _FIN_EVAL_STR_FIELDS:
        _require_str(data.get(key), f"financial_evaluation.{key}", fail)
    if not isinstance(data.get("profile_id"), str):
        fail("financial_evaluation.profile_id 须为字符串")

    for key, minimum in _FIN_EVAL_INT_MINS:
        val = data.get(key)
        if isinstance(val, bool) or not isinstance(val, int) or val < minimum:
            fail(f"financial_evaluation.{key} 须为 >= {minimum} 的整数，得到 {val!r}")

    _require_finite_num(data.get("discount_rate"), "financial_evaluation.discount_rate", fail)

    for key in _FIN_EVAL_LIST_FIELDS:
        _require_list(data.get(key), f"financial_evaluation.{key}", fail)

    comparison = data.get("comparison")
    if (
        not isinstance(comparison, Mapping)
        or not isinstance(comparison.get("mode"), str)
        or not comparison["mode"]
    ):
        fail("financial_evaluation.comparison 须包含非空字符串 mode")

    tax_cash = data.get("tax_cash")
    if not isinstance(tax_cash, Mapping):
        fail("financial_evaluation.tax_cash 须为 JSON 对象")
    for key in _FIN_EVAL_TAX_LIST_FIELDS:
        _require_list(tax_cash.get(key), f"financial_evaluation.tax_cash.{key}", fail)

    evaluation = data.get("evaluation")
    if not isinstance(evaluation, Mapping):
        fail("financial_evaluation.evaluation 须为 JSON 对象")
    _require_list(evaluation.get("rows"), "financial_evaluation.evaluation.rows", fail)
    if (
        "npv" not in evaluation
        or not isinstance(evaluation.get("irr_status"), str)
        or not evaluation["irr_status"]
    ):
        fail("financial_evaluation.evaluation 须包含 npv 与非空字符串 irr_status")

    check_finite_json(data, "financial_evaluation.json")
    return dict(data)


__all__ = [
    "ENGINEERING_SOLUTION_SCHEMA",
    "ENGINEERING_SOLUTION_VERSION",
    "EVALUATION_REPORT_SCHEMA",
    "EVALUATION_REPORT_VERSION",
    "FINANCIAL_EVALUATION_SCHEMA",
    "FINANCIAL_EVALUATION_VERSION",
    "validate_engineering_solution_doc",
    "validate_evaluation_report_doc",
    "validate_financial_evaluation_doc",
]
