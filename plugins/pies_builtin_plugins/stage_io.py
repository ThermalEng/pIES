"""Shared stage I/O and non-validating domain reconstruction helpers for built-in plugins.

All helpers trust already-frozen `/work/in` stage payloads without repeating
input schema validation, domain rechecks, or internal SHA-256 hashing (`AGENTS.md` #11,
Constitution §2.6).
"""

from __future__ import annotations

import csv
from collections.abc import Iterator, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeVar

from iesplan.assembly import ValidatedAssemblyArtifact, ValidationReceipt
from iesplan.computation.protocols import (
    EXECUTION_RECEIPT_SCHEMA,
    EXECUTION_RECEIPT_VERSION,
    SOLVER_BUNDLE_SCHEMA,
    SOLVER_BUNDLE_VERSION,
    ComputeResult,
    ExecutionReceipt,
    SolverBundle,
    SolverBundleManifest,
    freeze_json_value,
    thaw_json_value,
)
from iesplan.computation.sequences import (
    MaterializedPriceSeries,
    MaterializedSeries,
    MaterializedSet,
)
from iesplan.core.content import CHUNK_BYTES, Content
from iesplan.core.diagnostics import Diagnostic
from iesplan.finance import evaluate_project_investment
from iesplan.finance.triplet import (
    AmountDefinition,
    AmountPeriod,
    AmountRefCashflowAmount,
    AmountReference,
    AmountTerm,
    AssetBaseFrom,
    CapitalizedAsset,
    CashMovement,
    ConstructionConfirmation,
    ConstructionScheduleEntry,
    ConstructionVatRule,
    CostComponent,
    CostItem,
    CreditPool,
    DailyPriceSource,
    DeductibleMaintenance,
    DemandCharge,
    DemandInputTaxEntry,
    DepreciationEntry,
    DerivedTaxRule,
    EffectiveFinanceConfig,
    EnergyPrice,
    EvaluationEndAt,
    FinanceTypeEntry,
    FinancialTimePoint,
    FinancialYearAt,
    FixedCashflowAmount,
    IncomeTaxRule,
    InTaxEntry,
    LinearCost,
    MaintenanceExpenseNetBaseFrom,
    MaintInputTaxEntry,
    MaintItem,
    Money,
    MovementConfirmation,
    MovementInputTaxEntry,
    MovementOutputTaxEntry,
    MovementPayment,
    MovementRecognition,
    MovementVatOccurrence,
    NetCashflowEntry,
    OmInputTaxEntry,
    OpeningLoss,
    OutTaxEntry,
    PoolRefund,
    ReferenceScope,
    SeriesMeta,
    SourceShareCashflowAmount,
    TaxEntry,
    TaxReferenceSource,
    UpfrontCapexNetBaseFrom,
    VatRule,
)

T = TypeVar("T")


def _construct[T](cls: type[T], **attrs: Any) -> T:
    """Instantiate a frozen dataclass from trusted frozen data without `__post_init__`."""
    obj = object.__new__(cls)
    for key, value in attrs.items():
        object.__setattr__(obj, key, value)
    return obj


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


def _frozen_mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _frozen_value(v) for k, v in value.items()})
    return MappingProxyType({})


def _frozen_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _frozen_value(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_frozen_value(item) for item in value)
    return value


def receipt_from_frozen(data: Mapping[str, Any]) -> ValidationReceipt:
    """Reconstruct `ValidationReceipt` from trusted frozen data without revalidation."""
    return _construct(
        ValidationReceipt,
        schema_id=data["schema"],
        schema_version=data["schema_version"],
        validator_id=data["validator"]["id"],
        validator_version=data["validator"]["version"],
        canonical_algorithm_id=data["canonical_algorithm"]["id"],
        canonical_algorithm_version=data["canonical_algorithm"]["version"],
        dependencies=_frozen_mapping(data["dependencies"]),
        resources=_frozen_mapping(data["resources"]),
        finance_snapshot=_frozen_mapping(data["finance_snapshot"]),
        planning=_frozen_mapping(data["planning"]),
        system_equations=_frozen_mapping(data["system_equations"]),
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
    """Reconstruct `MaterializedSet` from trusted frozen dict without `__post_init__` checks."""
    series = [
        _construct(
            MaterializedSeries,
            device=entry["device"],
            interface=entry["interface"],
            mode=entry["mode"],
            resolution=entry["resolution"],
            point_count=entry["point_count"],
            values=tuple(entry["values"]),
            source=MappingProxyType(dict(entry["source"])),
        )
        for entry in data.get("series", ())
    ]
    pending = tuple(
        MappingProxyType(dict(entry)) for entry in data.get("pending_predict", ())
    )
    if prediction_payload is not None:
        for pred in prediction_payload["predictions"]:
            values = tuple(pred["values"])
            pred_res = str(pred.get("resolution") or resolution or "")
            series.append(
                _construct(
                    MaterializedSeries,
                    device=pred["instance"],
                    interface=pred["interface"],
                    mode="data_predict",
                    resolution=pred_res,
                    point_count=len(values),
                    values=values,
                    source=MappingProxyType(
                        {
                            "kind": "data_predict",
                            "object_id": str(pred.get("history_object_id") or ""),
                        }
                    ),
                )
            )
        pending = ()

    prices = tuple(
        _construct(
            MaterializedPriceSeries,
            price_id=entry["price_id"],
            point_count=entry["point_count"],
            resolution=entry["resolution"],
            unit=entry["unit"],
            values=tuple(entry["values"]),
            source=MappingProxyType(dict(entry["source"])),
        )
        for entry in data.get("prices", ())
    )
    return _construct(
        MaterializedSet,
        operation_years=int(data["operation_years"]),
        series=tuple(series),
        pending_predict=pending,
        prices=prices,
    )


def bundle_manifest_from_frozen(data: Mapping[str, Any]) -> SolverBundleManifest:
    """Reconstruct `SolverBundleManifest` from trusted frozen YAML without revalidation."""
    return _construct(
        SolverBundleManifest,
        schema=data.get("schema", SOLVER_BUNDLE_SCHEMA),
        schema_version=data.get("schema_version", SOLVER_BUNDLE_VERSION),
        bundle=freeze_json_value(dict(data["bundle"])),
        inputs=tuple(freeze_json_value(dict(item)) for item in data["inputs"]),
        command=freeze_json_value(dict(data["command"])),
        outputs=tuple(freeze_json_value(dict(item)) for item in data["outputs"]),
        result_adapter=data["result_adapter"],
        extensions=freeze_json_value(dict(data.get("extensions", {}))),
    )


def solver_bundle_from_frozen(
    manifest: SolverBundleManifest, files: Mapping[str, Content]
) -> SolverBundle:
    """Reconstruct `SolverBundle` from trusted frozen manifest and streaming files."""
    return _construct(
        SolverBundle,
        manifest=manifest,
        files=MappingProxyType(dict(sorted(files.items()))),
    )


def execution_receipt_from_frozen(data: Mapping[str, Any]) -> ExecutionReceipt:
    """Reconstruct `ExecutionReceipt` from trusted frozen JSON without revalidation."""
    return _construct(
        ExecutionReceipt,
        schema=data.get("schema", EXECUTION_RECEIPT_SCHEMA),
        schema_version=data.get("schema_version", EXECUTION_RECEIPT_VERSION),
        receipt_id=data["receipt_id"],
        bundle_id=data["bundle_id"],
        assembly_id=data["assembly_id"],
        calculation_config_id=data["calculation_config_id"],
        attempt_id=data["attempt_id"],
        generator_ref=data["generator_ref"],
        solver_ref=data["solver_ref"],
        executor_ref=data["executor_ref"],
        executable_ref=data["executable_ref"],
        result_adapter_ref=data["result_adapter_ref"],
        status=data["status"],
        exit_code=data.get("exit_code"),
        signal_number=data.get("signal_number"),
        termination_reason=data.get("termination_reason", ""),
        started_at=data.get("started_at", ""),
        ended_at=data.get("ended_at", ""),
        error=data.get("error", ""),
        stdout=data.get("stdout", ""),
        stderr=data.get("stderr", ""),
        stdout_truncated=bool(data.get("stdout_truncated", False)),
        stderr_truncated=bool(data.get("stderr_truncated", False)),
        outputs=tuple(freeze_json_value(dict(item)) for item in data.get("outputs", ())),
        policy_evaluated=freeze_json_value(dict(data.get("policy_evaluated", {}))),
        resource_usage=freeze_json_value(dict(data.get("resource_usage", {}))),
    )


# ---------------------------------------------------------------------------
# Trusted non-validating reconstruction of EffectiveFinanceConfig
# ---------------------------------------------------------------------------


def _money_from_frozen(raw: Mapping[str, Any]) -> Money:
    return _construct(Money, value=Decimal(str(raw["value"])), unit=raw["unit"])


def _linear_cost_from_frozen(raw: Mapping[str, Any]) -> LinearCost:
    return _construct(LinearCost, unit_cost=_money_from_frozen(raw["unit_cost"]))


def _cost_item_from_frozen(raw: Mapping[str, Any]) -> CostItem:
    fixed = _money_from_frozen(raw["fixed"]) if raw.get("fixed") is not None else None
    linear = MappingProxyType(
        {k: _linear_cost_from_frozen(v) for k, v in sorted((raw.get("linear") or {}).items())}
    )
    return _construct(CostItem, fixed=fixed, linear=linear)


def _cost_component_from_frozen(raw: Mapping[str, Any]) -> CostComponent:
    fixed = _money_from_frozen(raw["fixed"]) if raw.get("fixed") is not None else None
    linear = MappingProxyType(
        {k: _linear_cost_from_frozen(v) for k, v in sorted((raw.get("linear") or {}).items())}
    )
    items = MappingProxyType(
        {k: _cost_item_from_frozen(v) for k, v in sorted((raw.get("items") or {}).items())}
    )
    return _construct(CostComponent, fixed=fixed, linear=linear, items=items)


def _finance_type_from_frozen(raw: Mapping[str, Any]) -> FinanceTypeEntry:
    return _construct(
        FinanceTypeEntry,
        upfront_capex=(
            _cost_component_from_frozen(raw["upfront_capex"])
            if raw.get("upfront_capex") is not None
            else None
        ),
        annual_fixed_om=(
            _cost_component_from_frozen(raw["annual_fixed_om"])
            if raw.get("annual_fixed_om") is not None
            else None
        ),
        period_variable_om=(
            _cost_component_from_frozen(raw["period_variable_om"])
            if raw.get("period_variable_om") is not None
            else None
        ),
    )


def _series_meta_from_frozen(raw: Mapping[str, Any]) -> SeriesMeta:
    return _construct(
        SeriesMeta,
        resolution=raw["resolution"],
        point_count=raw["point_count"],
        unit=raw["unit"],
    )


def _energy_price_from_frozen(raw: Mapping[str, Any]) -> EnergyPrice:
    kind = raw["kind"]
    value = _money_from_frozen(raw["value"]) if kind == "constant" else None
    ref = raw.get("ref") if kind == "time_series" else None
    series_meta = (
        _series_meta_from_frozen(raw["series_meta"]) if kind == "time_series" else None
    )
    months = None
    if kind == "monthly_daily":
        months = MappingProxyType(
            {
                m: _construct(
                    DailyPriceSource,
                    ref=src["ref"],
                    series_meta=_series_meta_from_frozen(src["series_meta"]),
                )
                for m, src in raw["months"].items()
            }
        )
    return _construct(
        EnergyPrice,
        carrier=raw["carrier"],
        direction=raw["direction"],
        kind=kind,
        value=value,
        ref=ref,
        series_meta=series_meta,
        months=months,
    )


def _time_point_from_frozen(raw: Mapping[str, Any]) -> FinancialTimePoint:
    kind = raw["kind"]
    return _construct(
        FinancialTimePoint,
        kind=kind,
        year=raw["year"] if kind == "financial_year" else None,
        month=raw["month"] if kind != "financial_year" else None,
    )


def _amount_period_from_frozen(raw: Mapping[str, Any]) -> AmountPeriod:
    return _construct(
        AmountPeriod,
        kind=raw["kind"],
        year=raw.get("year"),
        month=raw.get("month"),
        axis=raw.get("axis"),
        first=raw.get("first"),
        last=raw.get("last"),
    )


def _amount_reference_from_frozen(raw: Mapping[str, Any]) -> AmountReference:
    scope = None
    if raw.get("scope") is not None:
        s = raw["scope"]
        scope = _construct(
            ReferenceScope,
            kind=s["kind"],
            instances=tuple(s.get("instances", ())),
        )
    period = _amount_period_from_frozen(raw["period"]) if raw.get("period") is not None else None
    bindings = tuple(raw["bindings"]) if raw.get("bindings") is not None else None
    source = (
        _construct(TaxReferenceSource, kind=raw["source"]["kind"], id=raw["source"]["id"])
        if raw.get("source") is not None
        else None
    )
    return _construct(
        AmountReference,
        kind=raw["kind"],
        period=period,
        finance_type=raw.get("finance_type"),
        driver=raw.get("driver"),
        scope=scope,
        path=raw.get("path"),
        basis=raw.get("basis"),
        price=raw.get("price"),
        charge=raw.get("charge"),
        bindings=bindings,
        year=raw.get("year"),
        position=raw.get("position"),
        id=raw.get("id"),
        block=raw.get("block"),
        variable=raw.get("variable"),
        aggregation=raw.get("aggregation"),
        unit_ref=raw.get("unit_ref"),
        measure=raw.get("measure"),
        source=source,
    )


def _amount_base_from_frozen(raw: Mapping[str, Any]) -> Any:
    kind = raw["kind"]
    if kind == "upfront_capex_net":
        scope = raw["scope"]
        return _construct(
            UpfrontCapexNetBaseFrom,
            finance_type=raw["finance_type"],
            scope_kind=scope["kind"],
            instances=tuple(scope.get("instances", ())),
            kind=kind,
        )
    if kind == "maintenance_expense_net":
        return _construct(
            MaintenanceExpenseNetBaseFrom,
            year=raw["year"],
            position=raw["position"],
            kind=kind,
        )
    return _construct(AssetBaseFrom, id=raw["id"], kind=kind)


def _movement_confirmation_from_frozen(raw: Mapping[str, Any]) -> MovementConfirmation:
    occurrences = tuple(
        _construct(
            MovementVatOccurrence,
            at=_time_point_from_frozen(occ["at"]),
            schedule=tuple(
                _construct(
                    ConstructionScheduleEntry,
                    period=s["period"],
                    ratio=Decimal(str(s["ratio"])),
                )
                for s in occ["schedule"]
            ),
        )
        for occ in raw.get("occurrences", ())
    )
    return _construct(MovementConfirmation, method=raw["method"], occurrences=occurrences)


def _tax_rule_from_frozen(raw: Mapping[str, Any]) -> Any:
    kind = raw["kind"]
    if kind == "vat":
        return _construct(
            VatRule,
            label_ref=raw["label_ref"],
            settlement=raw["settlement"],
            in_tax=tuple(
                _construct(
                    InTaxEntry,
                    price=item["price"],
                    rate=_money_from_frozen(item["rate"]),
                    deductible_ratio=_money_from_frozen(item["deductible_ratio"]),
                )
                for item in raw["in_tax"]
            ),
            out_tax=tuple(
                _construct(
                    OutTaxEntry,
                    price=item["price"],
                    rate=_money_from_frozen(item["rate"]),
                )
                for item in raw["out_tax"]
            ),
            om_input_tax=tuple(
                _construct(
                    OmInputTaxEntry,
                    for_path=item["for"],
                    rate=_money_from_frozen(item["rate"]),
                    deductible_ratio=_money_from_frozen(item["deductible_ratio"]),
                )
                for item in raw["om_input_tax"]
            ),
            maintenance_input_tax=tuple(
                _construct(
                    MaintInputTaxEntry,
                    year=item["year"],
                    position=item["position"],
                    rate=_money_from_frozen(item["rate"]),
                    deductible_ratio=_money_from_frozen(item["deductible_ratio"]),
                )
                for item in raw["maintenance_input_tax"]
            ),
            credit_pool=raw["credit_pool"],
            confirmation=raw["confirmation"],
            demand_input_tax=tuple(
                _construct(
                    DemandInputTaxEntry,
                    charge=item["charge"],
                    rate=_money_from_frozen(item["rate"]),
                    deductible_ratio=_money_from_frozen(item["deductible_ratio"]),
                )
                for item in raw.get("demand_input_tax", ())
            ),
            movement_input_tax=tuple(
                _construct(
                    MovementInputTaxEntry,
                    movement=item["movement"],
                    rate=_money_from_frozen(item["rate"]),
                    deductible_ratio=_money_from_frozen(item["deductible_ratio"]),
                    confirmation=_movement_confirmation_from_frozen(item["confirmation"]),
                )
                for item in raw.get("movement_input_tax", ())
            ),
            movement_output_tax=tuple(
                _construct(
                    MovementOutputTaxEntry,
                    movement=item["movement"],
                    rate=_money_from_frozen(item["rate"]),
                    confirmation=_movement_confirmation_from_frozen(item["confirmation"]),
                )
                for item in raw.get("movement_output_tax", ())
            ),
        )
    if kind == "construction_input_vat":
        conf_raw = raw["confirmation"]
        conf = _construct(
            ConstructionConfirmation,
            method=conf_raw["method"],
            schedule=tuple(
                _construct(
                    ConstructionScheduleEntry,
                    period=s["period"],
                    ratio=Decimal(str(s["ratio"])),
                )
                for s in conf_raw.get("schedule", ())
            ),
        )
        return _construct(
            ConstructionVatRule,
            label_ref=raw["label_ref"],
            for_path=raw["for"],
            rate=_money_from_frozen(raw["rate"]),
            deductible_ratio=_money_from_frozen(raw["deductible_ratio"]),
            credit_pool=raw["credit_pool"],
            confirmation=conf,
        )
    if kind == "derived_tax":
        return _construct(
            DerivedTaxRule,
            label_ref=raw["label_ref"],
            axis=raw["axis"],
            base_from=_amount_reference_from_frozen(raw["base_from"]),
            rate=_money_from_frozen(raw["rate"]),
            payment_lag=raw["payment_lag"],
            kind=kind,
        )
    dm_raw = raw["deductible_maintenance"]
    dm = _construct(
        DeductibleMaintenance,
        mode=dm_raw["mode"],
        items=tuple(
            _construct(
                MaintItem,
                year=item["year"],
                position=item["position"],
                deductible=item["deductible"],
            )
            for item in dm_raw["items"]
        ),
    )
    return _construct(
        IncomeTaxRule,
        label_ref=raw["label_ref"],
        rate=_money_from_frozen(raw["rate"]),
        revenue=tuple(raw["revenue"]),
        energy_cost=tuple(raw["energy_cost"]),
        annual_om=tuple(raw["annual_om"]),
        variable_om=tuple(raw["variable_om"]),
        deductible_maintenance=dm,
        depreciation=tuple(
            _construct(
                DepreciationEntry,
                base_from=_amount_base_from_frozen(d["base_from"]),
                method=d["method"],
                life_years=d["life_years"],
                residual=_money_from_frozen(d["residual"]),
                first_year=d["first_year"],
            )
            for d in raw["depreciation"]
        ),
        loss_carryforward_years=raw["loss_carryforward_years"],
        opening_losses=tuple(
            _construct(
                OpeningLoss,
                amount=_money_from_frozen(ol["amount"]),
                last_usable_year=ol["last_usable_year"],
            )
            for ol in raw["opening_losses"]
        ),
        demand_cost=tuple(raw.get("demand_cost", ())),
        movement_revenue=tuple(raw.get("movement_revenue", ())),
        movement_cost=tuple(raw.get("movement_cost", ())),
        derived_tax_cost=tuple(raw.get("derived_tax_cost", ())),
    )


def _net_cashflow_from_frozen(raw: Mapping[str, Any]) -> NetCashflowEntry:
    at_raw = raw["at"]
    at = (
        _construct(EvaluationEndAt, kind="evaluation_end")
        if at_raw["kind"] == "evaluation_end"
        else _construct(FinancialYearAt, year=at_raw["year"], kind="financial_year")
    )
    amt_raw = raw["amount"]
    amt_kind = amt_raw["kind"]
    if amt_kind == "fixed":
        amount: Any = _construct(
            FixedCashflowAmount, value=_money_from_frozen(amt_raw["value"]), kind="fixed"
        )
    elif amt_kind == "source_share":
        amount = _construct(
            SourceShareCashflowAmount,
            base_from=_amount_base_from_frozen(amt_raw["base_from"]),
            ratio=_money_from_frozen(amt_raw["ratio"]),
            kind="source_share",
        )
    else:
        amount = _construct(AmountRefCashflowAmount, id=amt_raw["id"], kind=amt_kind)
    return _construct(
        NetCashflowEntry,
        at=at,
        direction=raw["direction"],
        amount=amount,
    )


def effective_finance_from_frozen(mapping: Mapping[str, Any]) -> EffectiveFinanceConfig:
    """Reconstruct `EffectiveFinanceConfig` from trusted frozen dict without validation."""
    return _construct(
        EffectiveFinanceConfig,
        schema=mapping["schema"],
        schema_version=mapping["schema_version"],
        profile_id=mapping["profile_id"],
        currency=mapping["currency"],
        base_year=mapping["base_year"],
        price_basis=mapping["price_basis"],
        cost_method=mapping["cost_method"],
        finance_types=MappingProxyType(
            {k: _finance_type_from_frozen(v) for k, v in sorted(mapping["finance_types"].items())}
        ),
        energy_prices=MappingProxyType(
            {k: _energy_price_from_frozen(v) for k, v in sorted(mapping["energy_prices"].items())}
        ),
        demand_charges=MappingProxyType(
            {
                k: _construct(DemandCharge, rate=_money_from_frozen(v["rate"]))
                for k, v in sorted(mapping["demand_charges"].items())
            }
        ),
        taxes=MappingProxyType(
            {
                k: _construct(
                    TaxEntry,
                    display_name=v["display_name"],
                    tax_type=v["tax_type"],
                    applies_to=v["applies_to"],
                )
                for k, v in sorted((mapping.get("taxes") or {}).items())
            }
        ),
        credit_pools=MappingProxyType(
            {
                k: _construct(
                    CreditPool,
                    opening_credit=_money_from_frozen(v["opening_credit"]),
                    receivable_policy=v["receivable_policy"],
                    refunds=tuple(
                        _construct(
                            PoolRefund,
                            period=r["period"],
                            amount=_money_from_frozen(r["amount"]),
                            arrival_period=r["arrival_period"],
                        )
                        for r in v["refunds"]
                    ),
                )
                for k, v in sorted(mapping["credit_pools"].items())
            }
        ),
        tax_rules=MappingProxyType(
            {k: _tax_rule_from_frozen(v) for k, v in sorted(mapping["tax_rules"].items())}
        ),
        net_cashflows=MappingProxyType(
            {k: _net_cashflow_from_frozen(v) for k, v in sorted(mapping["net_cashflows"].items())}
        ),
        amounts=MappingProxyType(
            {
                k: _construct(
                    AmountDefinition,
                    unit=v["unit"],
                    axis=v["axis"],
                    constant=_money_from_frozen(v["constant"]),
                    terms=MappingProxyType(
                        {
                            tk: _construct(
                                AmountTerm,
                                coefficient=_money_from_frozen(tv["coefficient"]),
                                source=_amount_reference_from_frozen(tv["source"]),
                            )
                            for tk, tv in sorted(v["terms"].items())
                        }
                    ),
                )
                for k, v in sorted((mapping.get("amounts") or {}).items())
            }
        ),
        cash_movements=MappingProxyType(
            {
                k: _construct(
                    CashMovement,
                    amount=_amount_reference_from_frozen(v["amount"]),
                    price_basis=v["price_basis"],
                    direction=v["direction"],
                    recognition=_construct(
                        MovementRecognition,
                        kind=v["recognition"]["kind"],
                        at=(
                            _time_point_from_frozen(v["recognition"]["at"])
                            if v["recognition"]["kind"] == "at"
                            else None
                        ),
                        axis=v["recognition"].get("axis"),
                        first=v["recognition"].get("first"),
                        last=v["recognition"].get("last"),
                    ),
                    payments=tuple(
                        _construct(
                            MovementPayment,
                            offset=p["offset"],
                            ratio=Decimal(str(p["ratio"])),
                        )
                        for p in v["payments"]
                    ),
                )
                for k, v in sorted((mapping.get("cash_movements") or {}).items())
            }
        ),
        assets=MappingProxyType(
            {
                k: _construct(
                    CapitalizedAsset,
                    basis_from=_amount_reference_from_frozen(v["basis_from"]),
                    capitalized_at=_time_point_from_frozen(v["capitalized_at"]),
                    available_from=v["available_from"],
                )
                for k, v in sorted((mapping.get("assets") or {}).items())
            }
        ),
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
    finance_meta: Mapping[str, Any] | EffectiveFinanceConfig,
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

    if isinstance(finance_meta, Mapping):
        currency = str(finance_meta["currency"])
        price_basis = str(finance_meta["price_basis"])
        base_year = int(finance_meta["base_year"])
        profile_id = str(finance_meta["profile_id"])
    else:
        currency = finance_meta.currency
        price_basis = finance_meta.price_basis
        base_year = finance_meta.base_year
        profile_id = finance_meta.profile_id

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
        price_basis=price_basis,
        base_year=base_year,
    )
    return {
        "schema": "ies.financial-evaluation",
        "schema_version": "1.0.0",
        "status": "evaluated",
        "evaluation_years": years,
        "construction_period": period,
        "operation_years": operation,
        "currency": currency,
        "price_basis": price_basis,
        "base_year": base_year,
        "profile_id": profile_id,
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
