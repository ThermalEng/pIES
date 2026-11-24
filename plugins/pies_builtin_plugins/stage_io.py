"""Shared stage I/O and domain reconstruction helpers for built-in stage plugins.

All helpers trust already-frozen `/work/in` stage payloads without repeating
input schema validation or internal SHA-256 hashing (`AGENTS.md` #11,
Constitution §2.6).
"""

from __future__ import annotations

import csv
from collections.abc import Iterator, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from iesplan.assembly import ValidatedAssemblyArtifact, ValidationReceipt
from iesplan.computation.protocols import ComputeResult, thaw_json_value
from iesplan.computation.sequences import (
    MaterializedPriceSeries,
    MaterializedSeries,
    MaterializedSet,
)
from iesplan.core.content import CHUNK_BYTES, Content
from iesplan.core.diagnostics import Diagnostic
from iesplan.finance import EffectiveFinanceConfig, evaluate_project_investment


class FileContent:
    """Zero-buffer streaming Content backed directly by a stage file on disk."""

    __slots__ = ("_path",)

    def __init__(self, path: Path) -> None:
        self._path = path

    def iter_chunks(self) -> Iterator[bytes]:
        with self._path.open("rb") as handle:
            while chunk := handle.read(CHUNK_BYTES):
                yield chunk


def stream_content_to_file(content: Content, dest: Path) -> None:
    """Write a streaming `Content` object directly to `dest` without whole-file buffering."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as handle:
        for chunk in content.iter_chunks():
            handle.write(chunk)


def project_csv_column(csv_path: Path, interface_id: str) -> tuple[float, ...]:
    """Deterministically extract a single column from a trusted device data CSV."""
    lines = csv_path.read_bytes().decode("utf-8-sig").splitlines()
    header_index = next(
        index
        for index, line in enumerate(lines)
        if line.strip() and not line.strip().startswith("#")
    )
    rows = list(csv.reader(lines[header_index:]))
    header = [cell.strip().lower() for cell in rows[0]]
    column = header.index(interface_id.lower())
    return tuple(
        float(cells[column].strip())
        for cells in rows[1:]
        if any(cell.strip() for cell in cells)
    )


def receipt_from_frozen(data: Mapping[str, Any]) -> ValidationReceipt:
    """Reconstruct `ValidationReceipt` from a trusted frozen dictionary."""
    return ValidationReceipt(
        schema_id=data["schema"],
        schema_version=data["schema_version"],
        validator_id=data["validator"]["id"],
        validator_version=data["validator"]["version"],
        canonical_algorithm_id=data["canonical_algorithm"]["id"],
        canonical_algorithm_version=data["canonical_algorithm"]["version"],
        dependencies=dict(data["dependencies"]),
        resources=dict(data["resources"]),
        finance_snapshot=dict(data["finance_snapshot"]),
        planning=dict(data["planning"]),
        system_equations=dict(data["system_equations"]),
        diagnostics=tuple(Diagnostic(**dict(entry)) for entry in data["diagnostics"]),
    )


def artifact_from_frozen(data: Mapping[str, Any]) -> ValidatedAssemblyArtifact:
    """Reconstruct `ValidatedAssemblyArtifact` from a trusted frozen dictionary."""
    return ValidatedAssemblyArtifact(
        canonical_text=data["canonical_text"],
        receipt=receipt_from_frozen(data["receipt"]),
    )


def materialized_from_frozen(
    data: Mapping[str, Any],
    *,
    prediction_payload: Mapping[str, Any] | None = None,
    resolution: str | None = None,
) -> MaterializedSet:
    """Reconstruct `MaterializedSet` from frozen dict and optional Stage-1 `prediction.json`."""
    series = [
        MaterializedSeries(
            device=entry["device"],
            interface=entry["interface"],
            mode=entry["mode"],
            resolution=entry["resolution"],
            point_count=entry["point_count"],
            values=tuple(entry["values"]),
            source=dict(entry["source"]),
        )
        for entry in data.get("series", ())
    ]
    pending = tuple(data.get("pending_predict", ()))
    if prediction_payload is not None:
        for pred in prediction_payload["predictions"]:
            values = tuple(pred["values"])
            pred_res = str(pred.get("resolution") or resolution or "")
            series.append(
                MaterializedSeries(
                    device=pred["instance"],
                    interface=pred["interface"],
                    mode="data_predict",
                    resolution=pred_res,
                    point_count=len(values),
                    values=values,
                    source={
                        "kind": "data_predict",
                        "object_id": str(pred.get("history_object_id") or ""),
                    },
                )
            )
        pending = ()

    prices = tuple(
        MaterializedPriceSeries(
            price_id=entry["price_id"],
            point_count=entry["point_count"],
            resolution=entry["resolution"],
            unit=entry["unit"],
            values=tuple(entry["values"]),
            source=dict(entry["source"]),
        )
        for entry in data.get("prices", ())
    )
    return MaterializedSet(
        operation_years=int(data["operation_years"]),
        series=tuple(series),
        pending_predict=pending,
        prices=prices,
    )


def build_hourly_series_payload(outputs: Mapping[str, Any]) -> dict[str, Any] | None:
    """Build the canonical `hourly/series.json` payload from `ComputeResult.outputs`."""
    merged: dict[str, dict[str, Any]] = {}
    for kind, key in (("device", "series"), ("system", "system_series")):
        group = outputs.get(key)
        if not isinstance(group, Mapping):
            continue
        for identity, entry in group.items():
            merged[str(identity)] = {"series_id": str(identity), "kind": kind, **dict(entry)}
    if not merged:
        return None

    data: dict[str, list[float]] = {}
    units: dict[str, str] = {}
    identities: dict[str, dict[str, Any]] = {}
    for series_id in sorted(merged):
        entry = merged[series_id]
        raw_values = entry["values"]
        data[series_id] = list(raw_values) if isinstance(raw_values, Sequence) else raw_values
        units[series_id] = str(entry["unit"])
        identities[series_id] = {
            k: thaw_json_value(v) for k, v in entry.items() if k != "values"
        }
    return {"data": data, "meta": {"units": units, "series": identities}}


def build_financial_evaluation_payload(
    evaluation_data: Mapping[str, Any],
    finance: EffectiveFinanceConfig,
    result: ComputeResult,
) -> dict[str, Any] | None:
    """Project same-run financial facts and run `evaluate_project_investment`."""
    if result.math_status not in ("solved", "feasible"):
        return None
    if "solution" not in result.outputs:
        return None

    facts = result.outputs["financial_facts"]
    period = int(evaluation_data["construction_period"])
    operation = int(evaluation_data["operation_years"])
    years = int(evaluation_data["evaluation_years"])
    rate = float(evaluation_data["discount_rate"])

    currency = finance.currency
    tax_cash = thaw_json_value(facts["tax_cash"])
    flows = list(facts["annual_net_flows"])
    comparison = thaw_json_value(facts["comparison"])
    evaluation_flows = (
        comparison["evaluation_net_flows"] if comparison["mode"] == "difference" else flows
    )
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
        "annual_net_flows": flows,
        "comparison": comparison,
        "evaluation": assessed.to_dict(),
    }
