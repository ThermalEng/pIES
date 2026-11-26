"""Standardized /work/in -> /work/out stage I/O helpers for all 5 plugin kinds."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pies_sdk.contracts import (
    SOLVER_BUNDLE_SCHEMA,
    SOLVER_BUNDLE_VERSION,
    ComputeResultPayload,
    PredictionOutputPayload,
    check_finite_json,
    check_relative_path,
    read_json_file,
    write_json_file,
)
from pies_sdk.errors import StageIOError
from pies_sdk.iteration import ITERATION_LOG_FILENAME, IterationWriter
from pies_sdk.manifest import PLUGIN_KINDS, PluginManifest
from pies_sdk.solution_contracts import (
    EVALUATION_REPORT_SCHEMA,
    EVALUATION_REPORT_VERSION,
    EngineeringSolutionPayload,
    EvaluationReportPayload,
    PrimalSolutionPayload,
    iter_primal_solution_values,
    read_engineering_solution,
    read_evaluation_report,
    read_primal_solution,
    write_engineering_solution,
    write_evaluation_report,
    write_primal_solution,
)
from pies_sdk.yaml_safe import dump_safe_yaml, load_safe_yaml

FINANCIAL_EVALUATION_SCHEMA = "ies.financial-evaluation"
FINANCIAL_EVALUATION_VERSION = "1.0.0"

DEFAULT_WORK_IN = Path("/work/in")
DEFAULT_WORK_OUT = Path("/work/out")


def _ensure_work_in(work_in: Path) -> Path:
    path = Path(work_in)
    if not path.is_dir():
        raise StageIOError(f"Input directory does not exist: {path}")
    return path


def _ensure_work_out(work_out: Path) -> Path:
    path = Path(work_out)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_join(root: Path, rel_path: str) -> Path:
    rel = check_relative_path(rel_path)
    resolved_root = root.resolve()
    target = (resolved_root / rel).resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise StageIOError(f"Path escapes stage directory {root}: {rel_path!r}")
    return target


# ---------------------------------------------------------------------------
# Stage 1: predictor (/work/in/request.json -> /work/out/prediction.json)
# ---------------------------------------------------------------------------


def read_predictor_request(work_in: Path = DEFAULT_WORK_IN) -> dict[str, Any]:
    root = _ensure_work_in(work_in)
    data = read_json_file(root / "request.json")
    if not isinstance(data, dict):
        raise StageIOError("predictor /work/in/request.json must be a JSON object")
    return data


def write_prediction_output(
    work_out: Path,
    payload: PredictionOutputPayload | Mapping[str, Any],
) -> Path:
    root = _ensure_work_out(work_out)
    model = (
        payload
        if isinstance(payload, PredictionOutputPayload)
        else PredictionOutputPayload.from_dict(payload)
    )
    return write_json_file(root / "prediction.json", model.to_dict())


def read_prediction_output(work_out: Path = DEFAULT_WORK_OUT) -> PredictionOutputPayload:
    data = read_json_file(Path(work_out) / "prediction.json")
    return PredictionOutputPayload.from_dict(data)


# ---------------------------------------------------------------------------
# Stage 2: generator (/work/in/ -> /work/out/bundle/)
# ---------------------------------------------------------------------------


def read_generator_inputs(work_in: Path = DEFAULT_WORK_IN) -> dict[str, Any]:
    """Load available frozen inputs for a generator stage."""
    root = _ensure_work_in(work_in)
    bundle: dict[str, Any] = {}
    for filename in (
        "request.json",
        "calculation_config.json",
        "assembly.json",
        "sequences.json",
        "finance.json",
        "evaluation.json",
    ):
        candidate = root / filename
        if candidate.is_file():
            bundle[filename.removesuffix(".json")] = read_json_file(candidate)
    for yaml_name in ("assembly.yaml", "effective_finance.yaml"):
        candidate = root / yaml_name
        if candidate.is_file():
            parsed = load_safe_yaml(candidate.read_text(encoding="utf-8"))
            bundle[yaml_name.removesuffix(".yaml")] = parsed
    if "request" not in bundle and "calculation_config" not in bundle:
        raise StageIOError(
            "generator /work/in must contain 'calculation_config.json' or 'request.json'"
        )
    return bundle


def write_solver_bundle(
    work_out: Path,
    manifest: Mapping[str, Any],
    files: Mapping[str, bytes | str],
) -> Path:
    """Write standard Solver Bundle under /work/out/bundle/."""
    root = _ensure_work_out(work_out)
    bundle_dir = root / "bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    if not isinstance(manifest, Mapping):
        raise StageIOError("Solver Bundle manifest must be a mapping")
    if manifest.get("schema") != SOLVER_BUNDLE_SCHEMA:
        raise StageIOError(f"Invalid Solver Bundle schema: {manifest.get('schema')!r}")
    if manifest.get("schema_version") != SOLVER_BUNDLE_VERSION:
        raise StageIOError(
            f"Invalid Solver Bundle schema_version: {manifest.get('schema_version')!r}"
        )
    check_finite_json(manifest, "bundle.yaml")

    inputs = manifest.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise StageIOError("Solver Bundle manifest requires non-empty 'inputs'")
    declared_paths: set[str] = set()
    for entry in inputs:
        if not isinstance(entry, Mapping) or "path" not in entry:
            raise StageIOError("Solver Bundle input entry requires 'path'")
        declared_paths.add(check_relative_path(entry["path"], "bundle.inputs.path"))

    if set(files) != declared_paths:
        raise StageIOError(
            f"Solver Bundle files {sorted(files)!r} do not match declared inputs {sorted(declared_paths)!r}"
        )

    for rel_path, content in files.items():
        target = _safe_join(bundle_dir, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        elif isinstance(content, str):
            target.write_text(content, encoding="utf-8")
        else:
            raise StageIOError(
                f"Solver Bundle file {rel_path!r} must be bytes or str, got {type(content).__name__}"
            )

    manifest_path = bundle_dir / "bundle.yaml"
    manifest_path.write_text(dump_safe_yaml(dict(manifest)), encoding="utf-8")
    return bundle_dir


def validate_solver_bundle_dir(bundle_dir: Path) -> dict[str, Any]:
    """Validate a Solver Bundle directory containing bundle.yaml and declared inputs."""
    manifest_path = bundle_dir / "bundle.yaml"
    if not manifest_path.is_file():
        raise StageIOError(f"Solver Bundle missing bundle.yaml: {manifest_path}")
    manifest = load_safe_yaml(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise StageIOError("bundle.yaml must be a YAML mapping")
    if manifest.get("schema") != SOLVER_BUNDLE_SCHEMA:
        raise StageIOError(f"Invalid Solver Bundle schema: {manifest.get('schema')!r}")
    if manifest.get("schema_version") != SOLVER_BUNDLE_VERSION:
        raise StageIOError(
            f"Invalid Solver Bundle schema_version: {manifest.get('schema_version')!r}"
        )
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise StageIOError("bundle.yaml requires non-empty 'inputs'")
    for entry in inputs:
        if not isinstance(entry, Mapping) or "path" not in entry:
            raise StageIOError("bundle.yaml input entry requires 'path'")
        target = _safe_join(bundle_dir, entry["path"])
        if not target.is_file():
            raise StageIOError(f"Declared Solver Bundle input missing: {entry['path']}")
    return manifest


# ---------------------------------------------------------------------------
# Stage 3: solver (/work/in/bundle/ -> /work/out/solution.*)
# ---------------------------------------------------------------------------


def _read_frozen_bundle_manifest(bundle_dir: Path) -> dict[str, Any]:
    """Read frozen /work/in/bundle/bundle.yaml directly without re-checking inputs."""
    manifest_path = bundle_dir / "bundle.yaml"
    if not manifest_path.is_file():
        raise StageIOError(f"Solver Bundle missing bundle.yaml: {manifest_path}")
    manifest = load_safe_yaml(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise StageIOError("bundle.yaml must be a YAML mapping")
    return manifest


def read_solver_bundle(work_in: Path = DEFAULT_WORK_IN) -> tuple[dict[str, Any], Path]:
    root = _ensure_work_in(work_in)
    bundle_dir = root / "bundle"
    manifest = _read_frozen_bundle_manifest(bundle_dir)
    return manifest, bundle_dir


def write_solver_solution(
    work_out: Path,
    filename: str,
    content: bytes | str | Mapping[str, Any],
) -> Path:
    root = _ensure_work_out(work_out)
    rel = check_relative_path(filename, "solution filename")
    if "/" in rel or not rel.startswith("solution.") or len(rel) <= len("solution."):
        raise StageIOError(
            f"Solver output filename must match 'solution.<ext>', got {filename!r}"
        )
    target = _safe_join(root, rel)
    if isinstance(content, Mapping):
        return write_json_file(target, dict(content))
    if isinstance(content, bytes):
        target.write_bytes(content)
        return target
    if isinstance(content, str):
        target.write_text(content, encoding="utf-8")
        return target
    raise StageIOError(f"Unsupported solution content type: {type(content).__name__}")


def list_solver_solutions(work_out: Path = DEFAULT_WORK_OUT) -> list[Path]:
    root = Path(work_out)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_file() and p.name.startswith("solution."))


# ---------------------------------------------------------------------------
# Stage 4: evaluator (/work/out/{financial_evaluation.json | evaluation_report.json})
# ---------------------------------------------------------------------------


def _find_evaluator_solutions(root: Path) -> list[Path]:
    direct = sorted(p for p in root.iterdir() if p.is_file() and p.name.startswith("solution."))
    if direct:
        return direct
    bundle_out = root / "bundle" / "output"
    if bundle_out.is_dir():
        return sorted(
            p for p in bundle_out.iterdir() if p.is_file() and p.name.startswith("solution.")
        )
    return []


def read_evaluator_inputs(work_in: Path = DEFAULT_WORK_IN) -> dict[str, Any]:
    root = _ensure_work_in(work_in)
    bundle_dir = root / "bundle"
    bundle_manifest = _read_frozen_bundle_manifest(bundle_dir)
    solutions = _find_evaluator_solutions(root)
    if not solutions:
        raise StageIOError("evaluator /work/in missing solution.* output file")
    receipt_path = root / "receipt.json"
    receipt = read_json_file(receipt_path) if receipt_path.is_file() else None
    request_path = root / "request.json"
    request = read_json_file(request_path) if request_path.is_file() else None
    if receipt is None and request is None:
        raise StageIOError("evaluator /work/in requires 'receipt.json' or 'request.json'")

    finance: Any = None
    if (root / "finance.json").is_file():
        finance = read_json_file(root / "finance.json")
    elif (root / "effective_finance.yaml").is_file():
        finance = load_safe_yaml((root / "effective_finance.yaml").read_text(encoding="utf-8"))

    evaluation = (
        read_json_file(root / "evaluation.json") if (root / "evaluation.json").is_file() else None
    )

    return {
        "bundle_manifest": bundle_manifest,
        "bundle_dir": bundle_dir,
        "solution_paths": solutions,
        "receipt": receipt,
        "request": request,
        "finance": finance,
        "evaluation": evaluation,
    }


# ---------------------------------------------------------------------------
# Stage 5: algorithm (/work/in/request.json -> /work/out/solution.json)
# ---------------------------------------------------------------------------


def read_algorithm_request(work_in: Path = DEFAULT_WORK_IN) -> dict[str, Any]:
    root = _ensure_work_in(work_in)
    data = read_json_file(root / "request.json")
    if not isinstance(data, dict):
        raise StageIOError("algorithm /work/in/request.json must be a JSON object")
    return data


_FIN_EVAL_INT_MINS: tuple[tuple[str, int], ...] = (
    ("evaluation_years", 1),
    ("operation_years", 1),
    ("construction_period", 0),
    ("base_year", 1),
)
_FIN_EVAL_STR_FIELDS: tuple[str, ...] = (
    "status",
    "currency",
    "price_basis",
    "construction_total",
    "construction_total_unit",
    "annual_fixed_om",
)
# Row field type specs: "str" (non-empty str), "int" (non-bool int),
# "num" (non-empty numeric/formatted str or finite number), "map" (Mapping)
_FIN_EVAL_ROW_SPECS: dict[str, tuple[tuple[str, str], ...]] = {
    "payments_applied": (("t", "int"), ("ratio", "num"), ("amount", "num")),
    "net_cashflows": (("id", "str"), ("t", "int"), ("direction", "str"), ("amount", "num")),
    "cost_items": (
        ("instance_id", "str"),
        ("finance_type", "str"),
        ("component", "str"),
        ("item_id", "str"),
        ("axis", "str"),
        ("period", "int"),
        ("unit", "str"),
        ("raw", "num"),
        ("gross", "num"),
        ("deductible_input", "num"),
        ("net_basis", "num"),
    ),
    "amounts": (("id", "str"), ("axis", "str"), ("unit", "str")),
    "cash_movements": (
        ("id", "str"),
        ("axis", "str"),
        ("period", "int"),
        ("direction", "str"),
        ("price_basis", "str"),
        ("raw", "num"),
        ("gross", "num"),
        ("deductible_input", "num"),
        ("net_basis", "num"),
        ("output_tax", "num"),
        ("net_revenue", "num"),
        ("end_unpaid", "num"),
    ),
    "assets": (("id", "str"), ("basis", "num"), ("capitalized_at", "map"), ("available_from", "int")),
    "annual_operation": (
        ("financial_year", "int"),
        ("energy_net", "num"),
        ("variable_om", "num"),
        ("fixed_om", "num"),
        ("maintenance", "num"),
        ("demand_charge", "num"),
        ("net", "num"),
    ),
    "monthly_demand": (
        ("billing_id", "str"),
        ("charge_id", "str"),
        ("calendar_year", "int"),
        ("calendar_month", "int"),
        ("financial_year", "int"),
        ("peak_power", "num"),
        ("peak_unit", "str"),
        ("rate", "map"),
        ("charge", "num"),
    ),
}
_FIN_EVAL_TAX_ROW_SPECS: dict[str, tuple[tuple[str, str], ...]] = {
    "vat": (
        ("pool", "str"),
        ("month", "int"),
        ("paid", "num"),
        ("refund_claimed", "num"),
        ("refund_arrived", "num"),
        ("carry", "num"),
        ("receivable", "num"),
    ),
    "vat_rules": (
        ("rule", "str"),
        ("pool", "str"),
        ("month", "int"),
        ("output_tax", "num"),
        ("deductible_input", "num"),
    ),
    "refunds": (("pool", "str"), ("period", "int"), ("arrival_period", "int"), ("amount", "num")),
    "income": (
        ("rule", "str"),
        ("year", "int"),
        ("taxable_base", "num"),
        ("loss_used", "num"),
        ("loss_generated", "num"),
        ("loss_expired", "num"),
        ("assessed", "num"),
        ("tax", "num"),
    ),
    "depreciation": (("rule", "str"), ("base_from", "map"), ("year", "int"), ("amount", "num")),
    "loss_balances": (
        ("rule", "str"),
        ("vintage_id", "str"),
        ("year", "int"),
        ("last_usable_year", "int"),
        ("amount", "num"),
    ),
    "derived": (
        ("rule", "str"),
        ("axis", "str"),
        ("period", "int"),
        ("assessed", "num"),
        ("payment_period", "int"),
        ("paid", "num"),
    ),
}
_FIN_EVAL_ROW_FIELDS: tuple[tuple[str, str], ...] = (
    ("year", "int"),
    ("net", "num"),
    ("discounted", "num"),
    ("cumulative", "num"),
    ("cumulative_discounted", "num"),
)


def _is_scalar_kind(value: Any, kind: str) -> bool:
    if kind == "str":
        return isinstance(value, str) and bool(value)
    if kind == "int":
        return not isinstance(value, bool) and isinstance(value, int)
    if kind == "num":
        return (isinstance(value, str) and bool(value)) or (
            not isinstance(value, bool) and isinstance(value, (int, float))
        )
    if kind == "map":
        return isinstance(value, Mapping)
    return False


def _validate_fin_row_list(
    rows: Any,
    label: str,
    field_specs: tuple[tuple[str, str], ...],
) -> list[Any]:
    if not isinstance(rows, list):
        raise StageIOError(f"{label} requires list")
    for idx, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise StageIOError(f"{label}[{idx}] must be a JSON object")
        for key, kind in field_specs:
            if not _is_scalar_kind(row.get(key), kind):
                raise StageIOError(f"{label}[{idx}] missing or invalid {kind} field {key!r}")
    return rows


def _validate_financial_evaluation_mapping(data: Any) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise StageIOError("FinancialEvaluation payload must be a JSON object")
    if data.get("schema") != FINANCIAL_EVALUATION_SCHEMA:
        raise StageIOError(f"Invalid FinancialEvaluation schema: {data.get('schema')!r}")
    if data.get("schema_version") != FINANCIAL_EVALUATION_VERSION:
        raise StageIOError(
            f"Invalid FinancialEvaluation schema_version: {data.get('schema_version')!r}"
        )

    for key in _FIN_EVAL_STR_FIELDS:
        if not _is_scalar_kind(data.get(key), "str"):
            raise StageIOError(f"FinancialEvaluation requires non-empty string {key!r}")
    if not isinstance(data.get("profile_id"), str):
        raise StageIOError("FinancialEvaluation requires string 'profile_id'")

    for key, minimum in _FIN_EVAL_INT_MINS:
        val = data.get(key)
        if not _is_scalar_kind(val, "int") or val < minimum:
            raise StageIOError(
                f"FinancialEvaluation requires integer {key!r} >= {minimum}, got {val!r}"
            )

    discount_rate = data.get("discount_rate")
    if isinstance(discount_rate, bool) or not isinstance(discount_rate, (int, float)):
        raise StageIOError(
            f"FinancialEvaluation requires finite number 'discount_rate', got {discount_rate!r}"
        )

    for key, specs in _FIN_EVAL_ROW_SPECS.items():
        _validate_fin_row_list(data.get(key), f"FinancialEvaluation.{key}", specs)

    for idx, amt in enumerate(data["amounts"]):
        _validate_fin_row_list(
            amt.get("values"),
            f"FinancialEvaluation.amounts[{idx}].values",
            (("period", "int"), ("value", "num")),
        )
    for idx, mov in enumerate(data["cash_movements"]):
        _validate_fin_row_list(
            mov.get("payments"),
            f"FinancialEvaluation.cash_movements[{idx}].payments",
            (("at", "map"), ("ratio", "num"), ("amount", "num")),
        )
    for idx, asset in enumerate(data["assets"]):
        _validate_fin_row_list(
            asset.get("book_values"),
            f"FinancialEvaluation.assets[{idx}].book_values",
            (("year", "int"), ("value", "num")),
        )

    units = data.get("construction_units")
    if not isinstance(units, list):
        raise StageIOError("FinancialEvaluation.construction_units requires list")
    for idx, unit in enumerate(units):
        if not isinstance(unit, Mapping):
            raise StageIOError(f"FinancialEvaluation.construction_units[{idx}] must be a JSON object")
        for key, kind in (("unit_id", "str"), ("instance_id", "str"), ("mode", "str")):
            if not _is_scalar_kind(unit.get(key), kind):
                raise StageIOError(
                    f"FinancialEvaluation.construction_units[{idx}] invalid field {key!r}"
                )
        if not isinstance(unit.get("built"), bool):
            raise StageIOError(f"FinancialEvaluation.construction_units[{idx}] requires bool 'built'")
        _validate_fin_row_list(
            unit.get("scales"),
            f"FinancialEvaluation.construction_units[{idx}].scales",
            (("planning_variable_id", "str"), ("unit", "str")),
        )
        for s_idx, scale in enumerate(unit["scales"]):
            if not _is_scalar_kind(scale.get("value"), "num"):
                raise StageIOError(
                    f"FinancialEvaluation.construction_units[{idx}].scales[{s_idx}] invalid 'value'"
                )

    annual_flows = data.get("annual_net_flows")
    if not isinstance(annual_flows, list) or not all(
        _is_scalar_kind(v, "num") for v in annual_flows
    ):
        raise StageIOError("FinancialEvaluation.annual_net_flows requires list of numbers/strings")

    comparison = data.get("comparison")
    if not isinstance(comparison, Mapping) or not _is_scalar_kind(comparison.get("mode"), "str"):
        raise StageIOError("FinancialEvaluation requires 'comparison' object with non-empty 'mode'")
    if comparison["mode"] == "difference":
        eval_flows = comparison.get("evaluation_net_flows")
        if not isinstance(eval_flows, list) or not all(
            _is_scalar_kind(v, "num") for v in eval_flows
        ):
            raise StageIOError(
                "FinancialEvaluation.comparison requires 'evaluation_net_flows' in difference mode"
            )

    tax_cash = data.get("tax_cash")
    if not isinstance(tax_cash, Mapping):
        raise StageIOError("FinancialEvaluation requires 'tax_cash' object")
    for key, specs in _FIN_EVAL_TAX_ROW_SPECS.items():
        _validate_fin_row_list(tax_cash.get(key), f"FinancialEvaluation.tax_cash.{key}", specs)

    evaluation = data.get("evaluation")
    if not isinstance(evaluation, Mapping):
        raise StageIOError("FinancialEvaluation requires 'evaluation' object")
    _validate_fin_row_list(
        evaluation.get("rows"),
        "FinancialEvaluation.evaluation.rows",
        _FIN_EVAL_ROW_FIELDS,
    )
    if not _is_scalar_kind(evaluation.get("npv"), "num") or not _is_scalar_kind(
        evaluation.get("irr_status"), "str"
    ):
        raise StageIOError("FinancialEvaluation.evaluation requires 'npv' and non-empty 'irr_status'")

    check_finite_json(data, "FinancialEvaluation")
    return dict(data)


def write_financial_evaluation(path: Path, payload: Mapping[str, Any]) -> Path:
    """Validate and write financial_evaluation.json (ies.financial-evaluation@1.0.0)."""
    validated = _validate_financial_evaluation_mapping(payload)
    return write_json_file(Path(path), validated)


def read_financial_evaluation(path: Path) -> dict[str, Any]:
    """Read and validate financial_evaluation.json (ies.financial-evaluation@1.0.0)."""
    return _validate_financial_evaluation_mapping(read_json_file(Path(path)))


def write_compute_result(
    work_out: Path,
    result: ComputeResultPayload | Mapping[str, Any],
) -> Path:
    root = _ensure_work_out(work_out)
    payload = (
        result if isinstance(result, ComputeResultPayload) else ComputeResultPayload.from_dict(result)
    )
    return write_json_file(root / "result.json", payload.to_dict())


def read_compute_result(work_out: Path = DEFAULT_WORK_OUT) -> ComputeResultPayload:
    data = read_json_file(Path(work_out) / "result.json")
    return ComputeResultPayload.from_dict(data)


# ---------------------------------------------------------------------------
# Unified stage contract validation & StageContext
# ---------------------------------------------------------------------------


def validate_stage_outputs(
    kind: str,
    work_out: Path = DEFAULT_WORK_OUT,
    *,
    produces: Sequence[str] = (),
) -> None:
    """Validate that /work/out satisfies the canonical stage output contract for `kind`."""
    if kind not in PLUGIN_KINDS:
        raise StageIOError(f"Unknown plugin kind: {kind!r}")
    root = Path(work_out)
    if not root.is_dir():
        raise StageIOError(f"Output directory does not exist: {root}")
    if kind == "predictor":
        read_prediction_output(root)
    elif kind == "generator":
        validate_solver_bundle_dir(root / "bundle")
    elif kind == "solver":
        primal_path = root / "primal_solution.json"
        if not primal_path.is_file():
            raise StageIOError("solver /work/out missing 'primal_solution.json' output file")
        _hdr, values_iter = iter_primal_solution_values(primal_path)
        for _ in values_iter:
            pass
    elif kind == "algorithm":
        read_engineering_solution(root / "solution.json")
    elif kind == "evaluator":
        report_path = root / "evaluation_report.json"
        fin_path = root / "financial_evaluation.json"
        fin_contract = f"{FINANCIAL_EVALUATION_SCHEMA}@{FINANCIAL_EVALUATION_VERSION}"
        report_contract = f"{EVALUATION_REPORT_SCHEMA}@{EVALUATION_REPORT_VERSION}"
        if fin_contract in produces:
            if not fin_path.is_file():
                raise StageIOError("evaluator /work/out missing 'financial_evaluation.json'")
            read_financial_evaluation(fin_path)
        elif report_contract in produces:
            if not report_path.is_file():
                raise StageIOError("evaluator /work/out missing 'evaluation_report.json'")
            read_evaluation_report(report_path)
        elif report_path.is_file():
            read_evaluation_report(report_path)
        elif fin_path.is_file():
            read_financial_evaluation(fin_path)
        else:
            raise StageIOError(
                "evaluator /work/out missing 'evaluation_report.json' or 'financial_evaluation.json'"
            )


def extract_stage_parameters(work_in: Path) -> dict[str, Any]:
    """Extract stage parameters mapping from /work/in/request.json['parameters']."""
    root = Path(work_in)
    req_path = root / "request.json"
    if req_path.is_file():
        req = read_json_file(req_path)
        if not isinstance(req, Mapping):
            raise StageIOError("request.json must be a JSON object")
        if "parameters" in req and req["parameters"] is not None:
            params = req["parameters"]
            if not isinstance(params, Mapping):
                raise StageIOError("request.json 'parameters' must be a JSON object")
            return dict(params)
    return {}


@dataclass(frozen=True, slots=True)
class StageContext:
    """Execution context passed to a plugin entrypoint `<module>:<func>(ctx)`."""

    manifest: PluginManifest
    work_in: Path
    work_out: Path
    parameters: Mapping[str, Any]

    @property
    def kind(self) -> str:
        return self.manifest.kind

    def read_request(self) -> dict[str, Any]:
        return read_json_file(self.work_in / "request.json")

    def iteration_writer(self, *, truncate: bool = False) -> IterationWriter:
        return IterationWriter(self.work_out / ITERATION_LOG_FILENAME, truncate=truncate)

    def write_prediction(self, payload: PredictionOutputPayload | Mapping[str, Any]) -> Path:
        return write_prediction_output(self.work_out, payload)

    def write_bundle(
        self, manifest: Mapping[str, Any], files: Mapping[str, bytes | str]
    ) -> Path:
        return write_solver_bundle(self.work_out, manifest, files)

    def write_solution(
        self, filename: str, content: bytes | str | Mapping[str, Any]
    ) -> Path:
        return write_solver_solution(self.work_out, filename, content)

    def write_result(self, result: ComputeResultPayload | Mapping[str, Any]) -> Path:
        return write_compute_result(self.work_out, result)

    def write_primal_solution(
        self, payload: PrimalSolutionPayload | Mapping[str, Any]
    ) -> Path:
        return write_primal_solution(self.work_out / "primal_solution.json", payload)

    def read_primal_solution(self) -> PrimalSolutionPayload:
        return read_primal_solution(self.work_in / "primal_solution.json")

    def write_engineering_solution(
        self, payload: EngineeringSolutionPayload | Mapping[str, Any]
    ) -> Path:
        return write_engineering_solution(self.work_out / "solution.json", payload)

    def read_engineering_solution(self) -> EngineeringSolutionPayload:
        return read_engineering_solution(self.work_in / "solution.json")

    def write_evaluation_report(
        self, payload: EvaluationReportPayload | Mapping[str, Any]
    ) -> Path:
        return write_evaluation_report(self.work_out / "evaluation_report.json", payload)

    def read_evaluation_report(self) -> EvaluationReportPayload:
        return read_evaluation_report(self.work_in / "evaluation_report.json")

    def write_financial_evaluation(self, payload: Mapping[str, Any]) -> Path:
        return write_financial_evaluation(self.work_out / "financial_evaluation.json", payload)

    def read_financial_evaluation(self) -> dict[str, Any]:
        return read_financial_evaluation(self.work_in / "financial_evaluation.json")
