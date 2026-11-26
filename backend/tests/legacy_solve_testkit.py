"""Shared 1.2.0 test helpers for migrating solver/bundle/restoration tests."""

from __future__ import annotations

import json
import tempfile
from array import array
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bundle_testkit import materialized_file, mps_columns

from iesplan.computation import (
    HIGHS_ADAPTER_PATH,
    PRIMAL_SOLUTION_OUTPUT_PATH,
    SOLVER_BUNDLE_MPS_CONTRACT,
    ExecutionReceipt,
    SolverBundle,
    SolverBundleManifest,
)
from iesplan.computation.generators.highs_milp import (
    EXECUTOR_REF,
    GENERATOR_REF,
    PARAMETERS_PATH,
    SOLVER_REF,
    VARIABLE_MAP_SCHEMA,
    VARIABLE_MAP_VERSION,
)
from iesplan.computation.generators.parameters import RECORDS_PATH
from iesplan.computation.result_adapters import (
    PRIMAL_SOLUTION_SCHEMA,
    PRIMAL_SOLUTION_VERSION,
    ResultAdapter,
)
from iesplan.core.content import MemoryContent, read_content

PROBLEM_PATH = "input/problem.mps"
SOLUTION_PATH = "output/solution.sol"


def parse_highs_solution(raw: bytes | str) -> dict[str, Any]:
    """Parse ``HIGHS_SOLUTION_V1`` text emitted by ``ies-highs-solver``."""
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
    lines = text.splitlines()
    assert lines and lines[0] == "HIGHS_SOLUTION_V1", f"unexpected header: {lines[:1]!r}"
    facts: dict[str, Any] = {}
    primals: array[float] = array("d")
    in_primal = False
    for line in lines[1:]:
        if line == "primal:":
            in_primal = True
            continue
        if in_primal:
            if not line.strip():
                continue
            _, sep, value = line.partition(":")
            assert sep
            primals.append(float(value.strip()))
        else:
            key, sep, value = line.partition(":")
            assert sep
            facts[key.strip()] = value.strip()
    facts["primals"] = primals
    for key, alias in (
        ("objective_value", "objective"),
        ("best_bound", "best_bound_value"),
        ("relative_gap", "relative_gap_value"),
    ):
        val = facts.get(key)
        if val is not None and val != "none":
            facts[alias] = float(val)
    return facts


def v1_sol_to_primal_json(sol_text: str) -> dict[str, Any]:
    """Convert ``HIGHS_SOLUTION_V1`` text into ``ies.primal-solution@1.0.0`` dict."""
    lines = sol_text.splitlines()
    assert lines[0] == "HIGHS_SOLUTION_V1"
    facts: dict[str, str] = {}
    values: list[float] = []
    in_primal = False
    for line in lines[1:]:
        if line == "primal:":
            in_primal = True
            continue
        key, _, val = line.partition(":")
        if not in_primal:
            facts[key.strip()] = val.strip()
        elif line.strip():
            values.append(float(val.strip()))

    status = facts["model_status"]
    has_feasible = facts["has_feasible_solution"] == "true"
    solve_domain = facts.get("solve_domain", "original")
    verified = facts.get("original_model_verified", "true") == "true"
    if facts.get("epigraph_verified") == "false":
        verified = False

    if not verified:
        math_status = "numeric_failure" if status == "HighsModelStatus.kOptimal" else "not_solved"
        term_reason = "not_solved"
        has_feasible = False
    elif solve_domain == "restricted":
        math_status = "feasible" if has_feasible else "not_solved"
        term_reason = "optimal" if status == "HighsModelStatus.kOptimal" else "time_limit"
    elif status == "HighsModelStatus.kOptimal":
        math_status, term_reason = "solved", "optimal"
    elif status == "HighsModelStatus.kTimeLimit":
        math_status, term_reason = ("feasible" if has_feasible else "not_solved"), "time_limit"
    elif status == "HighsModelStatus.kInfeasible":
        math_status, term_reason = "infeasible", "infeasible"
    elif status == "HighsModelStatus.kUnbounded":
        math_status, term_reason = "unbounded", "unbounded"
    else:
        math_status, term_reason = "not_solved", "not_solved"

    def num(key: str) -> float | None:
        return None if facts[key] == "none" else float(facts[key])

    col_count = int(facts.get("model_num_columns", facts["num_columns"]))
    return {
        "schema": PRIMAL_SOLUTION_SCHEMA,
        "schema_version": PRIMAL_SOLUTION_VERSION,
        "math_status": math_status,
        "termination_reason": term_reason,
        "has_feasible_solution": has_feasible,
        "column_count": col_count,
        "values": values if has_feasible else [],
        "objective_value": num("objective_value") if has_feasible else None,
        "best_bound": None if (not has_feasible or solve_domain == "restricted") else num("best_bound"),
        "relative_gap": None if (not has_feasible or solve_domain == "restricted") else num("relative_gap"),
        "receipt": {
            "solver": SOLVER_REF,
            "solve_domain": solve_domain,
            "original_model_verified": verified,
        },
        "extensions": {},
    }


def succeeded_receipt(
    *,
    bundle: SolverBundle | None = None,
    bundle_id: str = "bundle-test",
    receipt_id: str = "receipt-test",
    generator_ref: str = GENERATOR_REF,
    solver_ref: str = SOLVER_REF,
    executor_ref: str = EXECUTOR_REF,
    executable_ref: str = SOLVER_REF,
    result_adapter_ref: str = "ies.backend.result_adapter@1.0.0",
) -> ExecutionReceipt:
    """Create a canonical 1.2.0 succeeded ``ExecutionReceipt``."""
    gen_ref = str(bundle.manifest.bundle["generator"]) if bundle is not None else generator_ref
    return ExecutionReceipt(
        receipt_id=receipt_id,
        bundle_id=bundle_id,
        assembly_id="asm-test",
        calculation_config_id="cfg-test",
        attempt_id="att-test",
        generator_ref=gen_ref,
        solver_ref=solver_ref,
        executor_ref=executor_ref,
        executable_ref=executable_ref,
        result_adapter_ref=result_adapter_ref,
        status="succeeded",
        exit_code=0,
        termination_reason="exited",
        started_at="2026-11-25T10:00:00Z",
        ended_at="2026-11-25T10:00:01Z",
    )


def read_bundle_records(bundle: SolverBundle) -> dict[str, Any]:
    """Decode ``input/parameters.json`` and ``input/parameters.jsonl`` into test-friendly fields."""
    header = json.loads(materialized_file(bundle, PARAMETERS_PATH).decode("utf-8"))
    col_names: list[str] = []
    row_names: list[str] = []
    unit_map: dict[str, str] = {}
    unit_conversions: dict[int, tuple[float, float]] = {}
    step_series: list[dict[str, Any]] = []
    system_series: list[dict[str, Any]] = []
    model_sources: dict[str, Any] = {}
    source_groups: list[dict[str, Any]] = []
    financial_facts: dict[str, Any] = {}
    components: dict[str, Any] = {}
    expressions: dict[int, tuple[float, list[tuple[int, float]]]] = {}
    unit_entries: list[tuple[int, str, float, float]] = []

    if RECORDS_PATH in bundle.files:
        current_series: dict[str, Any] | None = None
        current_cols: list[str] = []
        current_expr_idx: int | None = None
        current_expr_const = 0.0
        current_expr_terms: list[tuple[int, float]] = []
        current_group: dict[str, Any] | None = None
        current_group_targets: list[int] = []
        current_group_indices: list[int] = []
        for line in materialized_file(bundle, RECORDS_PATH).decode("utf-8").splitlines():
            if not line:
                continue
            row = json.loads(line)
            tag = row[0]
            if tag == "column_names":
                col_names.extend(row[1])
            elif tag == "row_names":
                row_names.extend(row[1])
            elif tag == "units":
                for idx, unit, scale, offset in row[1]:
                    col_i = int(idx)
                    sc_f, off_f = float(scale), float(offset)
                    unit_entries.append((col_i, str(unit), sc_f, off_f))
                    unit_conversions[col_i] = (sc_f, off_f)
            elif tag == "expression":
                current_expr_idx = int(row[1])
                current_expr_const = float(row[2])
                current_expr_terms = []
            elif tag == "terms" and current_expr_idx is not None:
                for col_idx, coeff in row[1]:
                    current_expr_terms.append((int(col_idx), float(coeff)))
            elif tag == "expression_end" and current_expr_idx is not None:
                expressions[current_expr_idx] = (current_expr_const, current_expr_terms)
                current_expr_idx = None
            elif tag == "series":
                current_series = dict(row[1])
                current_cols = []
            elif tag == "positions" and current_series is not None:
                current_cols.extend(col_names[int(i)] for i in row[1])
            elif tag == "series_end" and current_series is not None:
                current_series["columns"] = tuple(current_cols)
                if "block" in current_series:
                    system_series.append(current_series)
                else:
                    step_series.append(current_series)
                current_series = None
                current_cols = []
            elif tag == "sources":
                model_sources = dict(row[1])
            elif tag == "source_group":
                current_group = dict(row[1])
                if row[2] is not None:
                    current_group["indices"] = dict(row[2])
                current_group_targets = []
                current_group_indices = []
            elif tag == "targets" and current_group is not None:
                names = col_names if str(current_group.get("role", "")).startswith("column:") else row_names
                current_group_targets.extend(names[int(i)] for i in row[1])
            elif tag == "indices" and current_group is not None:
                current_group_indices.extend(int(i) for i in row[1])
            elif tag == "source_group_end" and current_group is not None:
                current_group["targets"] = current_group_targets
                if "indices" not in current_group:
                    current_group["indices"] = current_group_indices
                source_groups.append(current_group)
                current_group = None
            elif tag == "facts":
                financial_facts = row[1]
            elif tag == "component":
                components[str(row[1])] = {
                    "unit": row[2],
                    "caliber": row[3],
                    "expr_idx": row[4],
                }
        for idx, unit, _scale, _offset in unit_entries:
            unit_map[col_names[idx]] = unit
        for decl in components.values():
            const, terms = expressions[int(decl["expr_idx"])]
            decl["constant"] = const
            decl["terms"] = {col_names[col_i]: coeff for col_i, coeff in terms}
        if model_sources:
            model_sources["groups"] = source_groups

    return {
        **header,
        "column_names": col_names,
        "unit_map": unit_map,
        "unit_conversions": unit_conversions,
        "step_series": step_series,
        "system_series": system_series,
        "model_sources": model_sources,
        "financial_facts": financial_facts,
        "components": components,
        "expressions": expressions,
    }


def evaluate_bundle_expressions(
    bundle: SolverBundle,
    outputs: dict[str, Any] | Any,
) -> dict[str, Any]:
    """Evaluate bundle components and scaled variables from ``primal_solution.json`` or ``solution.sol``."""
    recs = read_bundle_records(bundle)
    if PRIMAL_SOLUTION_OUTPUT_PATH in outputs:
        raw = outputs[PRIMAL_SOLUTION_OUTPUT_PATH]
        raw_bytes = raw if isinstance(raw, (bytes, bytearray)) else read_content(raw)
        primals = json.loads(raw_bytes.decode("utf-8"))["values"]
    else:
        raw = outputs[SOLUTION_PATH]
        raw_bytes = raw if isinstance(raw, (bytes, bytearray)) else read_content(raw)
        primals = list(parse_highs_solution(raw_bytes)["primals"])

    col_names: list[str] = recs["column_names"]
    unit_map: dict[str, str] = recs["unit_map"]
    unit_conv: dict[int, tuple[float, float]] = recs["unit_conversions"]
    variables = []
    for idx, name in enumerate(col_names):
        if name not in unit_map:
            continue
        scale, offset = unit_conv.get(idx, (1.0, 0.0))
        variables.append({
            "name": name,
            "value": float(primals[idx]) * scale + offset,
            "unit": unit_map[name],
        })

    components = {}
    expressions = recs["expressions"]
    for cname, decl in recs["components"].items():
        const, terms = expressions[int(decl["expr_idx"])]
        total = float(const) + sum(float(coeff) * float(primals[idx]) for idx, coeff in terms)
        components[cname] = {
            "value": total,
            "unit": decl["unit"],
            "caliber": decl["caliber"],
        }
    return {"variables": variables, "components": components}


def make_minimal_highs_bundle(
    mps_bytes: bytes,
    *,
    unit_map: dict[str, str] | None = None,
    offset: float = 0.0,
    objective_unit: str = "CNY",
    seed: int = 0,
    output_path: str = PRIMAL_SOLUTION_OUTPUT_PATH,
    media_type: str = "application/json",
) -> SolverBundle:
    """Create a valid 1.2.0 ``SolverBundle`` from raw MPS bytes for solver/runtime tests."""
    cols = mps_columns(mps_bytes)
    eff_units = dict(unit_map) if unit_map is not None else dict.fromkeys(cols, "kW")
    col_pos = {name: idx for idx, name in enumerate(cols)}
    header = {
        "schema": VARIABLE_MAP_SCHEMA,
        "schema_version": VARIABLE_MAP_VERSION,
        "assembly": {"id": "asm-test", "schema_version": "1.0.0"},
        "generator": GENERATOR_REF,
        "records": RECORDS_PATH,
        "column_count": len(cols),
        "objective_offset": float(offset),
        "objective_scale": 1.0,
        "objective_unit": objective_unit,
        "outputs": ["solution", "metrics"],
        "precision": "medium",
        "seed": seed,
        "extensions": {},
    }
    records: list[list[Any]] = []
    if cols:
        records.append(["column_names", list(cols)])
    unit_rows = [
        [col_pos[name], unit, 1.0, 0.0]
        for name, unit in sorted(eff_units.items())
        if name in col_pos
    ]
    if unit_rows:
        records.append(["units", unit_rows])
    records.append([
        "facts",
        {
            "currency": "CNY",
            "construction_total": {"constant": 0.0, "terms": {}},
            "annual_fixed_om": {"constant": 0.0, "terms": {}},
            "payments_applied": [],
            "net_cashflows": [],
            "construction_units": [],
            "cost_items": [],
            "amounts": [],
            "cash_movements": [],
            "assets": [],
            "annual_operation": [],
            "monthly_demand": [],
            "tax_cash": {
                "vat": [],
                "vat_rules": [],
                "refunds": [],
                "income": [],
                "depreciation": [],
                "loss_balances": [],
                "derived": [],
            },
            "annual_net_flows": [{"constant": 0.0, "terms": {}}],
            "comparison": {"mode": "absolute"},
        },
    ])
    records.append([
        "sources",
        {
            "schema": "ies.model-sources",
            "schema_version": "1.0.0",
            "sources": {},
            "outputs": [],
        },
    ])
    jsonl_bytes = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records).encode("utf-8")
    manifest = SolverBundleManifest(
        bundle={"generator": GENERATOR_REF, "contract": SOLVER_BUNDLE_MPS_CONTRACT},
        inputs=[
            {"path": PROBLEM_PATH, "media_type": "application/mps"},
            {"path": PARAMETERS_PATH, "media_type": "application/json"},
            {"path": RECORDS_PATH, "media_type": "application/x-ndjson"},
        ],
        outputs=[{"path": output_path, "media_type": media_type, "required": True}],
        extensions={},
    )
    return SolverBundle(
        manifest=manifest,
        files={
            PROBLEM_PATH: MemoryContent(mps_bytes),
            PARAMETERS_PATH: MemoryContent((json.dumps(header) + "\n").encode("utf-8")),
            RECORDS_PATH: MemoryContent(jsonl_bytes),
        },
    )


def restore_bundle_solution(
    bundle: SolverBundle,
    receipt: ExecutionReceipt,
    outputs: dict[str, Any] | Any,
    *,
    tmp_path: Path | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Restore ``ies.engineering-solution@1.0.0`` via real 1.2.0 ``ResultAdapter``."""
    normalized: dict[str, bytes] = {}
    if outputs and PRIMAL_SOLUTION_OUTPUT_PATH in outputs:
        raw = outputs[PRIMAL_SOLUTION_OUTPUT_PATH]
        raw_bytes = raw if isinstance(raw, (bytes, bytearray)) else read_content(raw)
        try:
            payload = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            normalized[PRIMAL_SOLUTION_OUTPUT_PATH] = raw_bytes
        else:
            if (
                isinstance(payload, dict)
                and not payload.get("has_feasible_solution")
                and payload.get("column_count") == 0
            ):
                header = json.loads(materialized_file(bundle, PARAMETERS_PATH).decode("utf-8"))
                payload["column_count"] = int(header["column_count"])
                normalized[PRIMAL_SOLUTION_OUTPUT_PATH] = json.dumps(payload).encode("utf-8")
            else:
                normalized[PRIMAL_SOLUTION_OUTPUT_PATH] = raw_bytes
    elif outputs and SOLUTION_PATH in outputs:
        raw = outputs[SOLUTION_PATH]
        raw_bytes = raw if isinstance(raw, (bytes, bytearray)) else read_content(raw)
        primal_payload = v1_sol_to_primal_json(raw_bytes.decode("utf-8"))
        if not primal_payload["has_feasible_solution"] and primal_payload["column_count"] == 0:
            header = json.loads(materialized_file(bundle, PARAMETERS_PATH).decode("utf-8"))
            primal_payload["column_count"] = int(header["column_count"])
        normalized[PRIMAL_SOLUTION_OUTPUT_PATH] = json.dumps(primal_payload).encode("utf-8")

    if tmp_path is not None:
        sol_path = tmp_path / f"engineering_solution_{receipt.receipt_id}.json"
        outcome = ResultAdapter().restore_with_outcome(
            bundle, receipt, normalized, solution_output_path=sol_path
        )
        eng_sol = outcome.load_engineering_solution() if sol_path.is_file() else {}
        return outcome.compute_result, eng_sol

    with tempfile.TemporaryDirectory(prefix="ies-test-restore-") as tmp_dir:
        sol_path = Path(tmp_dir) / "solution.json"
        outcome = ResultAdapter().restore_with_outcome(
            bundle, receipt, normalized, solution_output_path=sol_path
        )
        eng_sol = outcome.load_engineering_solution() if sol_path.is_file() else {}
        return outcome.compute_result, eng_sol


def install_highs_stage_wrapper() -> str:
    """Create a test-only wrapper around ``HIGHS_ADAPTER_PATH`` emitting ``primal_solution.json``."""
    wrapper = Path(tempfile.gettempdir()) / "ies_highs_stage_wrapper_f4.py"
    if not wrapper.exists():
        script = f"""#!/usr/local/bin/python
from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

mod_path = {HIGHS_ADAPTER_PATH!r}
args = sys.argv[1:]
if "--version" in args or "--self-test" in args:
    mod = runpy.run_path(mod_path)
    sys.exit(mod["main"](args))

mod = runpy.run_path(mod_path)
rc = mod["main"](args)
if "--solution_file" in args:
    idx = args.index("--solution_file")
    if idx + 1 < len(args):
        sol_file = Path(args[idx + 1])
        if sol_file.is_file():
            lines = sol_file.read_text(encoding="utf-8").splitlines()
            facts = {{}}
            values = []
            in_primal = False
            for line in lines[1:]:
                if line == "primal:":
                    in_primal = True
                    continue
                k, _, v = line.partition(":")
                if not in_primal:
                    facts[k.strip()] = v.strip()
                elif line.strip():
                    values.append(float(v.strip()))
            status = facts["model_status"]
            has_feas = facts["has_feasible_solution"] == "true"
            domain = facts.get("solve_domain", "original")
            verified = facts.get("original_model_verified", "true") == "true"
            if facts.get("epigraph_verified") == "false":
                verified = False
            if not verified:
                ms = "numeric_failure" if status == "HighsModelStatus.kOptimal" else "not_solved"
                tr, has_feas = "not_solved", False
            elif domain == "restricted":
                ms = "feasible" if has_feas else "not_solved"
                tr = "optimal" if status == "HighsModelStatus.kOptimal" else "time_limit"
            elif status == "HighsModelStatus.kOptimal":
                ms, tr = "solved", "optimal"
            elif status == "HighsModelStatus.kTimeLimit":
                ms, tr = ("feasible" if has_feas else "not_solved"), "time_limit"
            elif status == "HighsModelStatus.kInfeasible":
                ms, tr = "infeasible", "infeasible"
            elif status == "HighsModelStatus.kUnbounded":
                ms, tr = "unbounded", "unbounded"
            else:
                ms, tr = "not_solved", "not_solved"
            num = lambda key: None if facts[key] == "none" else float(facts[key])
            col_count = int(facts.get("model_num_columns", facts["num_columns"]))
            primal_doc = {{
                "schema": {PRIMAL_SOLUTION_SCHEMA!r},
                "schema_version": {PRIMAL_SOLUTION_VERSION!r},
                "math_status": ms,
                "termination_reason": tr,
                "has_feasible_solution": has_feas,
                "column_count": col_count,
                "values": values if has_feas else [],
                "objective_value": num("objective_value") if has_feas else None,
                "best_bound": None if (not has_feas or domain == "restricted") else num("best_bound"),
                "relative_gap": None if (not has_feas or domain == "restricted") else num("relative_gap"),
                "receipt": {{
                    "solver": {SOLVER_REF!r},
                    "solve_domain": domain,
                    "original_model_verified": verified,
                }},
                "extensions": {{}},
            }}
            out_path = Path({PRIMAL_SOLUTION_OUTPUT_PATH!r})
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(primal_doc), encoding="utf-8")
sys.exit(rc)
"""
        wrapper.write_text(script, encoding="utf-8")
        wrapper.chmod(0o755)
    return str(wrapper)


def evaluate_builder_forward_finance(
    builder: Any,
    solved_si: dict[str, float],
    *,
    model: Any = None,
    objective_value: float | None = None,
) -> dict[str, Any]:
    """Run real 1.2.0 ``evaluate_forward_finance`` directly on a ``_ProjectBuilder`` solution."""
    from iesplan.computation.generators.highs_milp import linear_conversion_for_unit
    from iesplan.finance import evaluate_forward_finance

    doc_payload = dict(builder.doc)
    doc_payload.setdefault("devices", builder.devices)
    doc_payload.setdefault("connections", {})
    doc_payload.setdefault("tariff_bindings", {})
    doc_payload.setdefault("demand_bindings", {})
    doc_payload.setdefault("finance_binding", {"instances": {}})
    planning = doc_payload.get("planning") or {}
    plan_vars = planning.get("variables") or {}
    decl_units = planning.get("construction_units") or {}

    cu_list: list[dict[str, Any]] = []
    if decl_units:
        for uid, ucfg in sorted(decl_units.items()):
            inst = ucfg["device_ref"]
            mode = ucfg.get("mode", "mandatory")
            built_key = f"built__{uid}"
            built = bool(round(solved_si[built_key])) if built_key in solved_si else True
            scales = []
            for pname in sorted(ucfg.get("scales") or ()):
                unit = (plan_vars.get(pname) or {}).get("unit") or "1"
                sc_val, off_val = linear_conversion_for_unit(str(unit))
                raw_val = float(solved_si.get(pname, 0.0))
                scales.append({
                    "planning_variable_id": pname,
                    "value": raw_val * sc_val + off_val,
                    "unit": str(unit),
                })
            cu_list.append({
                "unit_id": uid,
                "instance_id": inst,
                "mode": mode,
                "built": built,
                "scales": scales,
            })
    elif plan_vars:
        by_inst: dict[str, list[dict[str, Any]]] = {}
        for pname, pcfg in sorted(plan_vars.items()):
            inst = pcfg["device_ref"]
            unit = str(pcfg.get("unit") or "1")
            sc_val, off_val = linear_conversion_for_unit(unit)
            raw_val = float(solved_si.get(pname, 0.0))
            by_inst.setdefault(inst, []).append({
                "planning_variable_id": pname,
                "value": raw_val * sc_val + off_val,
                "unit": unit,
            })
        for inst, scales in sorted(by_inst.items()):
            cu_list.append({
                "unit_id": f"cu_{inst}",
                "instance_id": inst,
                "mode": "mandatory",
                "built": True,
                "scales": scales,
            })

    if objective_value is None and model is not None:
        objective_value = float(model.objective.constant) + sum(
            float(coef) * float(solved_si[name]) for (name, _), coef in model.objective.terms
        )

    series_map: dict[str, Any] = {}
    system_series_map: dict[str, Any] = {}
    if getattr(builder, "point_count", 0) > 0 and hasattr(builder, "step_series"):
        for entry in builder.step_series():
            unit = str(entry["unit"])
            sc_val, off_val = linear_conversion_for_unit(unit)
            vals = [float(solved_si[col]) * sc_val + off_val for col in entry["columns"]]
            if "block" in entry:
                system_series_map[entry["series_id"]] = {
                    "block": entry["block"],
                    "variable": entry["variable"],
                    "unit": unit,
                    "values": vals,
                }
            else:
                series_map[entry["series_id"]] = {
                    "instance_id": entry["instance_id"],
                    "variable_id": entry["variable_id"],
                    "unit": unit,
                    "values": vals,
                }

    eng_sol = {
        "schema": "ies.engineering-solution",
        "schema_version": "1.0.0",
        "solver_summary": {
            "termination_reason": "optimal",
            "math_status": "solved",
            "has_feasible_solution": True,
            "objective_value": float(objective_value or 0.0),
            "objective_unit": builder.finance.currency,
            "best_bound": float(objective_value or 0.0),
            "relative_gap": 0.0,
        },
        "construction_units": cu_list,
        "series": series_map,
        "system_series": system_series_map,
        "extensions": {},
    }
    return evaluate_forward_finance(
        eng_sol,
        artifact={"canonical_text": json.dumps(doc_payload)},
        finance=builder.finance,
        evaluation=builder.evaluation,
        device_models=builder.models,
    )


def with_planning_variable_units(
    eng_sol: Mapping[str, Any],
    artifact: Any,
    bundle: SolverBundle,
    outputs: dict[str, Any] | Any,
) -> dict[str, Any]:
    """Backfill ``construction_units`` from scalar ``planning.variables`` when empty."""
    out = dict(eng_sol)
    if out.get("construction_units"):
        return out
    canonical = artifact["canonical_text"] if isinstance(artifact, Mapping) else artifact.canonical_text
    plan_vars = (json.loads(canonical).get("planning") or {}).get("variables") or {}
    if not plan_vars:
        return out
    var_vals = {
        row["name"]: row
        for row in evaluate_bundle_expressions(bundle, outputs)["variables"]
    }
    by_inst: dict[str, list[dict[str, Any]]] = {}
    for pname, pcfg in sorted(plan_vars.items()):
        if pname in var_vals:
            by_inst.setdefault(pcfg["device_ref"], []).append({
                "planning_variable_id": pname,
                "value": var_vals[pname]["value"],
                "unit": var_vals[pname]["unit"],
            })
    out["construction_units"] = [
        {
            "unit_id": f"cu_{inst}",
            "instance_id": inst,
            "mode": "mandatory",
            "built": True,
            "scales": scales,
        }
        for inst, scales in sorted(by_inst.items())
    ]
    return out


def build_financial_evaluation(
    snapshot: Any,
    finance: Any,
    result: Any,
) -> dict[str, object] | None:
    """Test-only helper assembling ``ies.financial-evaluation@1.0.0`` from precomputed facts."""
    from decimal import Decimal

    from iesplan.computation import thaw_json_value
    from iesplan.finance import evaluate_project_investment

    if result.math_status not in ("solved", "feasible"):
        return None
    if "solution" not in result.outputs:
        return None
    facts = result.outputs["financial_facts"]
    evaluation = snapshot.evaluation
    period = int(evaluation["construction_period"])
    operation = int(evaluation["operation_years"])
    years = int(evaluation["evaluation_years"])
    rate = float(evaluation["discount_rate"])

    currency = finance.currency
    tax_cash = thaw_json_value(facts["tax_cash"])
    flows = [value for value in facts["annual_net_flows"]]
    comparison = thaw_json_value(facts["comparison"])
    evaluation_flows = comparison["evaluation_net_flows"] if comparison["mode"] == "difference" else flows
    assessed = evaluate_project_investment(
        [Decimal(value) for value in evaluation_flows],
        rate,
        years,
        currency=currency,
        price_basis=finance.price_basis,
        base_year=finance.base_year,
    )
    return {
        "schema": "ies.financial-evaluation",
        "schema_version": "1.0.0",
        "status": "evaluated",
        "evaluation_years": years,
        "construction_period": period,
        "operation_years": operation,
        "currency": currency,
        "price_basis": finance.price_basis,
        "base_year": finance.base_year,
        "profile_id": finance.profile_id,
        "discount_rate": rate,
        "construction_total": facts["construction_total"],
        "construction_total_unit": currency,
        "annual_fixed_om": facts["annual_fixed_om"],
        "payments_applied": [
            {"t": entry["t"], "ratio": entry["ratio"], "amount": entry["amount"]}
            for entry in facts["payments_applied"]
        ],
        "net_cashflows": thaw_json_value(facts["net_cashflows"]),
        "cost_items": thaw_json_value(facts["cost_items"]),
        "amounts": thaw_json_value(facts["amounts"]),
        "cash_movements": thaw_json_value(facts["cash_movements"]),
        "assets": thaw_json_value(facts["assets"]),
        "construction_units": thaw_json_value(facts["construction_units"]),
        "annual_operation": [
            {
                "financial_year": entry["financial_year"],
                "energy_net": entry["energy_net"],
                "variable_om": entry["variable_om"],
                "fixed_om": entry["fixed_om"],
                "maintenance": entry["maintenance"],
                "demand_charge": entry["demand_charge"],
                "net": entry["net"],
            }
            for entry in facts["annual_operation"]
        ],
        "monthly_demand": thaw_json_value(facts["monthly_demand"]),
        "tax_cash": tax_cash,
        "annual_net_flows": list(flows),
        "comparison": comparison,
        "evaluation": assessed.to_dict(),
    }
