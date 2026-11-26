"""Pure forward financial evaluation over resolved engineering solutions.

Consumes already-restored business-unit facts (``ies.engineering-solution@1.0.0``)
alongside frozen assembly, finance, evaluation confirmation, materialized
prices/series, and device metadata to produce ``ies.financial-evaluation@1.0.0``
without importing any computation/modeling/assembly/domain-behavior package.
"""

from __future__ import annotations

import heapq
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from iesplan.core.contracts import ProjectBaseline, horizon_point_count
from iesplan.core.units import dims_of, to_si, unit_meta
from iesplan.finance.confirmation import EvaluationInput
from iesplan.finance.evaluation import evaluate_project_investment
from iesplan.finance.tax import (
    LossBalance,
    income_tax_payable,
    settle_vat_period,
    straight_line_period_amount,
    use_losses,
)
from iesplan.finance.timeline import annual_boundaries
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
    amount_dependency_closure,
    cash_movement_occurrences,
    cost_target_leaves,
    expand_amount_period,
    parse_amount_period,
)

FINANCIAL_EVALUATION_SCHEMA = "ies.financial-evaluation"
FINANCIAL_EVALUATION_VERSION = "1.0.0"

_COMPONENTS_NEW: tuple[str, ...] = ("upfront_capex", "annual_fixed_om", "period_variable_om")
_COMPONENTS_EXISTING: tuple[str, ...] = ("period_variable_om",)
_DEFAULT_MONTH_SPAN = (0, 0)

_FinancialNodeKey = tuple[str, str, str, int, str]


# ---------------------------------------------------------------------------
# Unit conversion, formatting, and interval helpers
# ---------------------------------------------------------------------------


def _to_internal(value: float, unit: str) -> float:
    """Convert physical values to SI and currency rates to internal currency/SI denominators."""
    if dims_of(unit).get("currency") == 1:
        return float(value) * float(unit_meta(unit)["to_si"])
    return to_si(float(value), unit)


def _unit_cost_to_internal(value: float, unit: str) -> float:
    """Normalize currency/unit denominator without currency conversion."""
    return float(value) * float(unit_meta(unit)["to_si"])


def _money_to_internal(money: Any) -> float:
    return _to_internal(float(money.value), str(money.unit))


_YEAR_SECONDS = _to_internal(1.0, "a")


def _fmt_float(value: float, label: str) -> str:
    if not math.isfinite(value):
        raise ValueError(f"财务事实 {label} 非有限数值: {value!r}")
    normalized = 0.0 if value == 0.0 else value
    return str(Decimal(str(normalized)))


def _fmt_decimal(value: Decimal, label: str) -> str:
    if not value.is_finite():
        raise ValueError(f"财务事实 {label} 非有限数值: {value!r}")
    return "0" if value == 0 else str(value)


def _to_decimal(value: float, label: str) -> Decimal:
    if not math.isfinite(value):
        raise ValueError(f"财务事实 {label} 非有限数值: {value!r}")
    normalized = 0.0 if value == 0.0 else value
    return Decimal(str(normalized))


def _attr_or_key(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _valid_range_bounds(entry: Any) -> tuple[Any, Any]:
    vrange = _attr_or_key(entry, "valid_range")
    if vrange is None:
        return (None, None)
    if isinstance(vrange, Mapping):
        return (vrange.get("minimum"), vrange.get("maximum"))
    return (vrange[0], vrange[1])


def _interval_scale(bounds: tuple[float, float], coeff: float) -> tuple[float, float]:
    if coeff == 0.0:
        return (0.0, 0.0)
    lo, hi = bounds
    a = 0.0 if lo == 0.0 else coeff * lo
    b = 0.0 if hi == 0.0 else coeff * hi
    return (a, b) if a <= b else (b, a)


def _interval_add(left: tuple[float, float], right: tuple[float, float]) -> tuple[float, float]:
    return (left[0] + right[0], left[1] + right[1])


class _AffineExpr:
    """Pure affine expression over bounded symbol keys, preserving cancellation."""

    __slots__ = ("const", "terms")

    def __init__(self, const: float = 0.0, terms: Mapping[tuple[Any, ...], float] | None = None) -> None:
        self.const = float(const)
        self.terms: dict[tuple[Any, ...], float] = (
            {k: v for k, v in terms.items() if v != 0.0} if terms else {}
        )

    def add_scaled(self, other: _AffineExpr, scale: float = 1.0) -> None:
        if scale == 0.0:
            return
        if other.const != 0.0:
            self.const += other.const * scale
        for sym, coeff in other.terms.items():
            updated = self.terms.get(sym, 0.0) + coeff * scale
            if abs(updated) <= 1e-15:
                self.terms.pop(sym, None)
            else:
                self.terms[sym] = updated

    def scaled(self, scale: float) -> _AffineExpr:
        out = _AffineExpr()
        out.add_scaled(self, scale)
        return out


# ---------------------------------------------------------------------------
# Non-validating frozen reconstruction (Constitution §2.6, AGENTS.md #11)
# ---------------------------------------------------------------------------


def _construct[T](cls: type[T], **attrs: Any) -> T:
    obj = object.__new__(cls)
    for key, value in attrs.items():
        object.__setattr__(obj, key, value)
    return obj


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
    series_meta = _series_meta_from_frozen(raw["series_meta"]) if kind == "time_series" else None
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
        scope = _construct(ReferenceScope, kind=s["kind"], instances=tuple(s.get("instances", ())))
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
                _construct(ConstructionScheduleEntry, period=s["period"], ratio=Decimal(str(s["ratio"])))
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
                _construct(OutTaxEntry, price=item["price"], rate=_money_from_frozen(item["rate"]))
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
                _construct(ConstructionScheduleEntry, period=s["period"], ratio=Decimal(str(s["ratio"])))
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
            _construct(MaintItem, year=item["year"], position=item["position"], deductible=item["deductible"])
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
    return _construct(NetCashflowEntry, at=at, direction=raw["direction"], amount=amount)


def _effective_finance_from_frozen(mapping: Mapping[str, Any]) -> EffectiveFinanceConfig:
    """Reconstruct EffectiveFinanceConfig from trusted frozen dict without rerunning validation."""
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


def _coerce_doc(artifact: Any) -> Mapping[str, Any]:
    if hasattr(artifact, "canonical_text"):
        return json.loads(artifact.canonical_text)
    if isinstance(artifact, Mapping):
        if "canonical_text" in artifact:
            return json.loads(artifact["canonical_text"])
        return artifact
    raise TypeError(f"artifact 须为 ValidatedAssemblyArtifact 或装配字典，得到 {type(artifact).__name__}")


def _coerce_finance(finance: EffectiveFinanceConfig | Mapping[str, Any]) -> EffectiveFinanceConfig:
    if isinstance(finance, EffectiveFinanceConfig):
        return finance
    if isinstance(finance, Mapping):
        return _effective_finance_from_frozen(finance)
    raise TypeError(f"finance 须为 EffectiveFinanceConfig 或映射，得到 {type(finance).__name__}")


def _coerce_evaluation(evaluation: EvaluationInput | Mapping[str, Any]) -> EvaluationInput:
    if isinstance(evaluation, EvaluationInput):
        return evaluation
    if isinstance(evaluation, Mapping):
        return EvaluationInput.from_frozen(evaluation)
    raise TypeError(f"evaluation 须为 EvaluationInput 或映射，得到 {type(evaluation).__name__}")


# ---------------------------------------------------------------------------
# Pure topological dependency ordering (owned inside finance)
# ---------------------------------------------------------------------------


def _financial_dependency_order(
    doc: Mapping[str, Any],
    finance: EffectiveFinanceConfig,
    evaluation: EvaluationInput,
) -> tuple[tuple[_FinancialNodeKey, ...], tuple[_FinancialNodeKey, ...]]:
    """Compute dependency-first evaluation order across amounts, movements, assets, pools, and taxes."""
    years = evaluation.evaluation_years
    occurrences = {
        identity: cash_movement_occurrences(movement) for identity, movement in finance.cash_movements.items()
    }
    roots: set[_FinancialNodeKey] = set()
    for constraint in (doc.get("planning") or {}).get("constraints", {}).values():
        if constraint.get("type") != "amount_relation" or not constraint.get("enabled", True):
            continue
        for reference in constraint["references"].values():
            if reference["kind"] == "amount":
                axis, periods = expand_amount_period(parse_amount_period(reference["period"]), "scalar", 0)
                roots.update(("amount", reference["id"], axis, period, "value") for period in periods)
    roots.update(
        ("amount", cash.amount.id, "scalar", 0, "value")
        for cash in finance.net_cashflows.values()
        if cash.amount.kind == "amount_ref"
    )
    for identity, entries in occurrences.items():
        roots.update(("movement", identity, at.axis, at.period, "raw") for at in entries)
    roots.update(("asset", identity, "scalar", 0, "basis") for identity in finance.assets)
    roots.update(
        ("asset", identity, "financial_year", year, "book_value")
        for identity in finance.assets
        for year in range(1, years + 1)
    )
    for identity, rule in finance.tax_rules.items():
        axis = "financial_year" if isinstance(rule, IncomeTaxRule) else "tax_month"
        if isinstance(rule, DerivedTaxRule):
            axis = rule.axis
        measure = "assessed" if isinstance(rule, (IncomeTaxRule, DerivedTaxRule)) else "output_tax"
        roots.update(
            ("tax_rule", identity, axis, period, measure)
            for period in range(1, years * (12 if axis == "tax_month" else 1) + 1)
        )
    for identity in finance.credit_pools:
        roots.update(("pool", identity, "tax_month", period, "paid") for period in range(1, 12 * years + 1))

    def reference_edges(reference: Any, axis: str, period: int) -> list[_FinancialNodeKey]:
        if reference.kind == "maintenance":
            return []
        selected_axis, selected = expand_amount_period(reference.period, axis, period)
        kind = reference.kind
        if kind in ("amount", "movement", "asset"):
            measure = "value" if kind == "amount" else "raw" if kind == "movement" else reference.measure
            return [(kind, reference.id, selected_axis, p, measure) for p in selected]
        if kind == "tax":
            owner = "pool" if reference.source.kind == "pool" else "tax_rule"
            return [(owner, reference.source.id, selected_axis, p, reference.measure) for p in selected]
        return []

    def confirm_months(entry: Any, at: Any) -> tuple[int, ...]:
        if entry.confirmation.method == "recognition_period":
            return (at.period if at.axis == "tax_month" else 12 * at.period,)
        schedule = next(item for item in entry.confirmation.occurrences if item.at == at)
        return tuple(item.period for item in schedule.schedule if item.ratio)

    def node_deps(key: _FinancialNodeKey) -> tuple[_FinancialNodeKey, ...]:
        kind, identity, axis, period, measure = key
        deps: set[_FinancialNodeKey] = set()
        if kind == "amount":
            for term in finance.amounts[identity].terms.values():
                if term.coefficient.value:
                    deps.update(reference_edges(term.source, axis, period))
        elif kind == "movement":
            deps.update(reference_edges(finance.cash_movements[identity].amount, axis, period))
        elif kind == "asset":
            if measure == "basis":
                deps.update(reference_edges(finance.assets[identity].basis_from, "scalar", 0))
            elif measure == "book_value" or any(
                entry.base_from.kind == "asset"
                and entry.base_from.id == identity
                and evaluation.construction_period + entry.first_year
                <= period
                < evaluation.construction_period + entry.first_year + entry.life_years
                for rule in finance.tax_rules.values()
                if isinstance(rule, IncomeTaxRule)
                for entry in rule.depreciation
            ):
                deps.add(("asset", identity, "scalar", 0, "basis"))
        elif kind == "pool":
            if measure in ("paid", "credit_balance"):
                if period > 1:
                    deps.add(("pool", identity, axis, period - 1, "credit_balance"))
                for rule_id, rule in finance.tax_rules.items():
                    if isinstance(rule, (VatRule, ConstructionVatRule)) and rule.credit_pool == identity:
                        deps.add(("tax_rule", rule_id, axis, period, "output_tax"))
                        deps.add(("tax_rule", rule_id, axis, period, "deductible_input"))
        else:
            rule = finance.tax_rules[identity]
            if isinstance(rule, DerivedTaxRule):
                if measure == "paid":
                    source_period = period - rule.payment_lag
                    if source_period >= 1:
                        deps.add(("tax_rule", identity, axis, source_period, "assessed"))
                elif rule.rate.value:
                    deps.update(reference_edges(rule.base_from, axis, period))
            elif isinstance(rule, VatRule):
                component = "movement_output_tax" if measure == "output_tax" else "movement_input_tax"
                for entry in getattr(rule, component):
                    if not entry.rate.value or (measure != "output_tax" and not entry.deductible_ratio.value):
                        continue
                    for at in occurrences[entry.movement]:
                        if period in confirm_months(entry, at):
                            deps.add(("movement", entry.movement, at.axis, at.period, "raw"))
            elif isinstance(rule, IncomeTaxRule):
                if measure != "taxable_base":
                    deps.add(("tax_rule", identity, axis, period, "taxable_base"))
                    for previous in range(max(1, period - rule.loss_carryforward_years), period):
                        deps.add(("tax_rule", identity, axis, previous, "taxable_base"))
                else:
                    for component in ("movement_revenue", "movement_cost"):
                        for movement_id in getattr(rule, component):
                            for at in occurrences[movement_id]:
                                year = (
                                    max(1, at.period)
                                    if at.axis == "financial_year"
                                    else (at.period + 11) // 12
                                )
                                if year == period:
                                    deps.add(("movement", movement_id, at.axis, at.period, "raw"))
                    for tax_id in rule.derived_tax_cost:
                        tax_axis = finance.tax_rules[tax_id].axis
                        selected = (
                            (period,)
                            if tax_axis == "financial_year"
                            else range(12 * (period - 1) + 1, 12 * period + 1)
                        )
                        deps.update(("tax_rule", tax_id, tax_axis, p, "assessed") for p in selected)
                    for entry in rule.depreciation:
                        start = evaluation.construction_period + entry.first_year
                        if entry.base_from.kind == "asset" and start <= period < start + entry.life_years:
                            deps.add(("asset", entry.base_from.id, "scalar", 0, "basis"))
        return tuple(sorted(deps))

    deps_map: dict[_FinancialNodeKey, tuple[_FinancialNodeKey, ...]] = {}
    pending = sorted(roots, reverse=True)
    while pending:
        key = pending.pop()
        if key in deps_map:
            continue
        deps = node_deps(key)
        deps_map[key] = deps
        pending.extend(dep for dep in reversed(deps) if dep not in deps_map)

    remaining = {key: len(deps) for key, deps in deps_map.items()}
    consumers: dict[_FinancialNodeKey, list[_FinancialNodeKey]] = defaultdict(list)
    for key, deps in deps_map.items():
        for dep in deps:
            consumers[dep].append(key)
    ready = [key for key, count in remaining.items() if count == 0]
    heapq.heapify(ready)
    order: list[_FinancialNodeKey] = []
    while ready:
        key = heapq.heappop(ready)
        order.append(key)
        for consumer in sorted(consumers[key]):
            remaining[consumer] -= 1
            if remaining[consumer] == 0:
                heapq.heappush(ready, consumer)
    done = set(order)
    unresolved = tuple(key for key in sorted(deps_map) if key not in done)
    return tuple(order), unresolved


# ---------------------------------------------------------------------------
# Forward financial evaluator
# ---------------------------------------------------------------------------


def _vat_month_spans(
    start_year: int,
    step_hours: float,
    construction_period: int,
    point_count: int,
) -> tuple[tuple[int, int, int], ...]:
    origin = datetime(start_year, 1, 1)
    spans: dict[int, list[int]] = {}
    for step in range(point_count):
        moment = origin + timedelta(hours=step * step_hours)
        month = 12 * construction_period + (moment.year - start_year) * 12 + moment.month
        slot = spans.setdefault(month, [step, step])
        slot[1] = step
    return tuple((month, bounds[0], bounds[1] + 1) for month, bounds in sorted(spans.items()))


def _amount_basis(raw: float, gross: float, net: float, basis: str) -> float:
    if basis == "raw":
        return raw
    if basis == "gross":
        return gross
    if basis in ("net_basis", "net_revenue"):
        return net
    return gross - net


@dataclass(frozen=True, slots=True)
class _CostTerm:
    driver: str
    local: str
    target_kind: str
    aggregation: str
    coefficient: float
    leaf: str


@dataclass(frozen=True, slots=True)
class _CostSpec:
    instance: str
    component: str
    finance_type: str
    fixed_amount: float | None
    fixed_items: tuple[tuple[str, float], ...]
    terms: tuple[_CostTerm, ...]

    @property
    def leaves(self) -> tuple[str, ...]:
        names = ["fixed"] if self.fixed_amount is not None else []
        names.extend(leaf for leaf, _ in self.fixed_items)
        names.extend(term.leaf for term in self.terms)
        return tuple(names)


class _ForwardFinanceEvaluator:
    """Deterministic forward financial calculator from resolved engineering values."""

    def __init__(
        self,
        *,
        solution: Mapping[str, Any],
        doc: Mapping[str, Any],
        finance: EffectiveFinanceConfig,
        evaluation: EvaluationInput,
        prices_si: Mapping[str, list[float]],
        predefined_series_si: Mapping[tuple[str, str], list[float]],
        device_models: Mapping[str, Any],
    ) -> None:
        self.solution = solution
        self.doc = doc
        self.finance = finance
        self.evaluation = evaluation
        self.prices_si = prices_si
        self.predefined_series_si = predefined_series_si
        self.device_models = device_models

        self.devices: Mapping[str, Any] = doc.get("devices") or {}
        self.fb_instances: Mapping[str, Any] = (doc.get("finance_binding") or {}).get("instances") or {}
        planning = doc.get("planning") or {}
        self.plan_vars: Mapping[str, Any] = planning.get("variables") or {}
        self.declared_units: Mapping[str, Any] = planning.get("construction_units") or {}
        self.bound_prop: dict[tuple[str, str], str] = {
            (var["device_ref"], var["parameter"]): name for name, var in self.plan_vars.items()
        }

        baseline = doc["project_baseline"]
        self.resolution = str(baseline["resolution"])
        self.start_year = int(baseline["start_year"])
        self.point_count = horizon_point_count(
            self.resolution, self.start_year, self.evaluation.operation_years
        )
        step_hours = ProjectBaseline(resolution=self.resolution, start_year=self.start_year).step_duration
        self.step_s = step_hours * 3600.0
        self.operating_spans = annual_boundaries(
            self.resolution, self.start_year, self.evaluation.operation_years
        )
        self.span_by_year: dict[int, tuple[int, int]] = {
            self.evaluation.construction_period + idx + 1: span
            for idx, span in enumerate(self.operating_spans)
        }
        self.month_spans = _vat_month_spans(
            self.start_year,
            step_hours,
            self.evaluation.construction_period,
            self.point_count,
        )
        self.month_span_map: dict[int, tuple[int, int]] = {
            month: (start, end) for month, start, end in self.month_spans
        }

        # Index resolved solution values (public ingestion validates completeness)
        self.unit_by_inst: dict[str, Mapping[str, Any]] = {}
        self.unit_by_id: dict[str, Mapping[str, Any]] = {}
        self.plan_var_si: dict[str, float] = {}
        scale_groups: list[Any] = [solution.get("planning_variables") or ()]
        for unit in solution.get("construction_units") or ():
            self.unit_by_inst[unit["instance_id"]] = unit
            self.unit_by_id[unit["unit_id"]] = unit
            scale_groups.append(unit.get("scales") or ())
        for group in scale_groups:
            for scale in group:
                self.plan_var_si[scale["planning_variable_id"]] = _to_internal(
                    float(scale["value"]), str(scale["unit"])
                )

        self.solver_series: dict[tuple[str, str], tuple[str, Sequence[float]]] = {}
        for entry in (solution.get("series") or {}).values():
            self.solver_series[(entry["instance_id"], entry["variable_id"])] = (
                str(entry["unit"]),
                entry["values"],
            )

        self.sys_series: dict[tuple[str, str], tuple[str, Sequence[float]]] = {}
        for entry in (solution.get("system_series") or {}).values():
            self.sys_series[(entry["block"], entry["variable"])] = (
                str(entry["unit"]),
                entry["values"],
            )

        self._series_si_cache: dict[tuple[str, str], list[float]] = {}
        self._sys_si_cache: dict[tuple[str, str], list[float]] = {}
        self._bound_symbols: dict[tuple[Any, ...], tuple[float, float]] = {}

        self.cost_specs = self._build_cost_specs()
        self.cost_tax_factors = self._build_cost_tax_factors()
        self.demand_tax_factors = self._build_demand_tax_factors()
        self.price_markups, self.sale_tax_factors, self.purchase_tax_factors = (
            self._build_tariff_tax_factors()
        )
        self.maint_credits = self._build_maintenance_credits()

        self._demand_rows: list[dict[str, Any]] = []
        self._maintenance_items: dict[tuple[int, int], float] = {}
        self._maintenance_annual: dict[int, float] = {}
        self._named_amounts: dict[tuple[str, int], float] = {}
        self._movement_values: dict[tuple[str, str, int], dict[str, float]] = {}
        self._financial_values: dict[tuple[Any, ...], Any] = {}
        self._net_sources: dict[str, float] = {}
        self._depreciation_values: dict[tuple[str, int, int], float] = {}
        self._depreciation_by_rule: dict[str, list[tuple[int, Any]]] = {}

        self._year_cash: dict[int, float] = {}
        self._fact_movements: list[dict[str, Any]] = []
        self._fact_assets: list[dict[str, Any]] = []
        self._fact_vat: list[dict[str, Any]] = []
        self._fact_vat_rules: list[dict[str, Any]] = []
        self._fact_refunds: list[dict[str, Any]] = []
        self._fact_income: list[dict[str, Any]] = []
        self._fact_depr: list[dict[str, Any]] = []
        self._fact_balances: list[dict[str, Any]] = []
        self._fact_derived: list[dict[str, Any]] = []
        self._vat_built: dict[str, int] = {}
        self._income_built: dict[str, int] = {}
        self._vat_pool_state: dict[str, Decimal] = {}
        self._income_state: dict[str, tuple[list[tuple[str, LossBalance]], int]] = {}

        for rule_id, rule in sorted(self.finance.tax_rules.items()):
            if isinstance(rule, IncomeTaxRule):
                self._depreciation_by_rule[rule_id] = list(enumerate(rule.depreciation))

    def _model_prop_entry(self, inst: str, prop: str) -> Any | None:
        model = self.device_models.get(inst)
        if model is None:
            return None
        variables = _attr_or_key(model, "variables", {})
        if prop in variables:
            return variables[prop]
        props = _attr_or_key(model, "properties", {})
        return props.get(prop)

    def _prop_si(self, inst: str, prop: str) -> float:
        bound_var = self.bound_prop.get((inst, prop))
        if bound_var is not None:
            return self.plan_var_si[bound_var]
        overrides = self.devices[inst].get("properties") or {}
        if prop in overrides:
            return _to_internal(float(overrides[prop]["value"]), str(overrides[prop]["unit"]))
        entry = self._model_prop_entry(inst, prop)
        if entry is not None:
            return _to_internal(float(_attr_or_key(entry, "value")), str(_attr_or_key(entry, "unit")))
        raise KeyError(f"实例 {inst!r} 的属性 {prop!r} 不在装配覆盖或设备模型中")

    def _prop_interval(self, inst: str, prop: str) -> tuple[float, float]:
        bound_var = self.bound_prop.get((inst, prop))
        if bound_var is None:
            val = self._prop_si(inst, prop)
            return (val, val)
        spec = self.plan_vars[bound_var]
        model_entry = self._model_prop_entry(inst, prop)
        model_unit = str(_attr_or_key(model_entry, "unit")) if model_entry is not None else None
        p_unit = str(spec.get("unit") or model_unit)
        m_lo: float | None = None
        m_hi: float | None = None
        if model_entry is not None:
            vr_lo, vr_hi = _valid_range_bounds(model_entry)
            if vr_lo is not None and model_unit is not None:
                m_lo = _to_internal(float(vr_lo), model_unit)
            if vr_hi is not None and model_unit is not None:
                m_hi = _to_internal(float(vr_hi), model_unit)
        raw_lo = spec.get("lower_bound")
        raw_hi = spec.get("upper_bound")
        p_lo = _to_internal(float(raw_lo), p_unit) if raw_lo is not None else None
        p_hi = _to_internal(float(raw_hi), p_unit) if raw_hi is not None else None

        unit_decl = None
        optional = False
        for ucfg in self.declared_units.values():
            scales = ucfg.get("scales")
            if isinstance(scales, Mapping) and bound_var in scales:
                raw_decl = scales[bound_var]
                unit_decl = raw_decl if isinstance(raw_decl, Mapping) else None
                optional = ucfg.get("mode") == "optional"
                break
        if unit_decl is not None:
            a_lo = unit_decl.get("active_lower_bound")
            a_hi = unit_decl.get("active_upper_bound")
            if a_lo is not None:
                a_lo_si = _to_internal(float(a_lo), p_unit)
                p_lo = a_lo_si if p_lo is None else max(p_lo, a_lo_si)
            if a_hi is not None:
                a_hi_si = _to_internal(float(a_hi), p_unit)
                p_hi = a_hi_si if p_hi is None else min(p_hi, a_hi_si)

        if m_lo is not None and p_lo is not None:
            lo = max(m_lo, p_lo)
        elif m_lo is not None:
            lo = max(m_lo, 0.0)
        elif p_lo is not None:
            lo = p_lo
        else:
            lo = 0.0

        if m_hi is not None and p_hi is not None:
            hi = min(m_hi, p_hi)
        elif m_hi is not None:
            hi = m_hi
        elif p_hi is not None:
            hi = p_hi
        else:
            hi = math.inf

        if optional:
            return (min(lo, 0.0), max(hi, 0.0))
        return (lo, hi)

    def _iface_step_interval(self, inst: str, iface: str, start: int, stop: int) -> tuple[float, float]:
        steps = max(0, stop - start)
        if steps == 0:
            return (0.0, 0.0)
        if (inst, iface) in self.predefined_series_si:
            total = sum(self.predefined_series_si[(inst, iface)][start:stop])
            return (total, total)
        model = self.device_models.get(inst)
        if model is None:
            return (0.0, math.inf)
        interfaces = _attr_or_key(model, "interfaces", {})
        flow = interfaces.get(iface)
        if flow is None:
            return (0.0, math.inf)
        f_unit = str(_attr_or_key(flow, "unit"))
        vr_lo, vr_hi = _valid_range_bounds(flow)
        min_ref = _attr_or_key(flow, "minimum_property_ref")
        max_ref = _attr_or_key(flow, "maximum_property_ref")

        lo_candidates: list[float] = []
        if vr_lo is not None:
            lo_candidates.append(_to_internal(float(vr_lo), f_unit))
        if min_ref is not None:
            lo_candidates.append(self._prop_interval(inst, str(min_ref))[0])
        step_lo = max(lo_candidates) if lo_candidates else (
            -math.inf if _attr_or_key(flow, "type") == "bidirectional" else 0.0
        )

        hi_candidates: list[float] = []
        if vr_hi is not None:
            hi_candidates.append(_to_internal(float(vr_hi), f_unit))
        if max_ref is not None:
            hi_candidates.append(self._prop_interval(inst, str(max_ref))[1])
        step_hi = min(hi_candidates) if hi_candidates else math.inf
        return (step_lo * steps, step_hi * steps)

    def _series_si(self, inst: str, iface: str) -> list[float]:
        key = (inst, iface)
        cached = self._series_si_cache.get(key)
        if cached is not None:
            return cached
        if key in self.predefined_series_si:
            cached = self.predefined_series_si[key]
        else:
            unit, values = self.solver_series[key]
            cached = [_to_internal(float(v), unit) for v in values]
        self._series_si_cache[key] = cached
        return cached

    def _system_series_si(self, block: str, variable: str) -> list[float]:
        key = (block, variable)
        cached = self._sys_si_cache.get(key)
        if cached is not None:
            return cached
        unit, values = self.sys_series[key]
        cached = [_to_internal(float(v), unit) for v in values]
        self._sys_si_cache[key] = cached
        return cached

    def _vat_gross_factor(self, rate: Any) -> float:
        value = float(rate.value)
        if self.finance.price_basis == "tax_exclusive":
            return value
        return value / (1.0 + value)

    @staticmethod
    def _vat_gross_split_factor(rate: Any) -> float:
        value = float(rate.value)
        return value / (1.0 + value)

    def _build_cost_specs(self) -> dict[tuple[str, str], _CostSpec]:
        specs: dict[tuple[str, str], _CostSpec] = {}
        for inst in sorted(self.devices):
            binding = self.fb_instances.get(inst)
            if binding is None or binding.get("type") != "costed":
                continue
            ft = binding["finance_type"]
            ft_entry = self.finance.finance_types[ft]
            kinds = (
                _COMPONENTS_NEW
                if self.devices[inst].get("asset_origin") == "new"
                else _COMPONENTS_EXISTING
            )
            for component in kinds:
                comp = getattr(ft_entry, component)
                if comp is None:
                    continue
                terms: list[_CostTerm] = []
                linear_leaves = [
                    (f"linear.{driver}", driver, cost)
                    for driver, cost in sorted((comp.linear or {}).items())
                ]
                fixed_items: list[tuple[str, float]] = []
                for item_id, item in sorted(comp.items.items()):
                    if item.fixed is not None:
                        fixed_items.append((f"items.{item_id}.fixed", _money_to_internal(item.fixed)))
                    linear_leaves.extend(
                        (f"items.{item_id}.linear.{driver}", driver, cost)
                        for driver, cost in sorted((item.linear or {}).items())
                    )
                for leaf, driver, cost in linear_leaves:
                    target = binding["drivers"][driver]
                    coefficient = (
                        _unit_cost_to_internal(float(cost.unit_cost.value), cost.unit_cost.unit)
                        if component == "upfront_capex"
                        else _to_internal(float(cost.unit_cost.value), cost.unit_cost.unit)
                    )
                    if "property" in target:
                        local = target["property"]
                        target_kind = "property"
                        aggregation = "scalar"
                    else:
                        local = target["interface"]
                        target_kind = "interface"
                        aggregation = target["aggregation"]
                    terms.append(_CostTerm(driver, local, target_kind, aggregation, coefficient, leaf))
                specs[(inst, component)] = _CostSpec(
                    instance=inst,
                    component=component,
                    finance_type=ft,
                    fixed_amount=_money_to_internal(comp.fixed) if comp.fixed is not None else None,
                    fixed_items=tuple(fixed_items),
                    terms=tuple(terms),
                )
        return specs

    def _build_cost_tax_factors(self) -> dict[str, tuple[float, float]]:
        factors: dict[str, tuple[float, float]] = {}
        exclusive = self.finance.price_basis == "tax_exclusive"
        for rule in self.finance.tax_rules.values():
            entries: list[tuple[str, Any, Any]] = []
            if isinstance(rule, VatRule):
                entries = [(item.for_path, item.rate, item.deductible_ratio) for item in rule.om_input_tax]
            elif isinstance(rule, ConstructionVatRule):
                entries = [(rule.for_path, rule.rate, rule.deductible_ratio)]
            for path, rate, ratio in entries:
                gross = 1.0 + float(rate.value) if exclusive else 1.0
                credit = self._vat_gross_factor(rate) * float(ratio.value)
                for leaf in cost_target_leaves(path, self.finance.finance_types):
                    factors[leaf] = (gross, credit)
        return factors

    def _build_demand_tax_factors(self) -> dict[str, tuple[float, float]]:
        out: dict[str, tuple[float, float]] = {}
        exclusive = self.finance.price_basis == "tax_exclusive"
        for rule in self.finance.tax_rules.values():
            if not isinstance(rule, VatRule):
                continue
            for entry in rule.demand_input_tax:
                rate = float(entry.rate.value)
                gross = 1.0 + rate if exclusive else 1.0
                deductible = self._vat_gross_factor(entry.rate) * float(entry.deductible_ratio.value)
                out[entry.charge] = (gross, deductible)
        return out

    def _build_tariff_tax_factors(
        self,
    ) -> tuple[dict[str, float], dict[str, tuple[float, float]], dict[str, tuple[float, float]]]:
        markups: dict[str, float] = {}
        sale: dict[str, tuple[float, float]] = {}
        purchase: dict[str, tuple[float, float]] = {}
        exclusive = self.finance.price_basis == "tax_exclusive"
        for rule in self.finance.tax_rules.values():
            if not isinstance(rule, VatRule):
                continue
            for entry in rule.in_tax:
                rate = float(entry.rate.value)
                markup = 1.0 + rate if exclusive else 1.0
                if exclusive:
                    markups[entry.price] = markup
                purchase[entry.price] = (
                    self._vat_gross_factor(entry.rate) * float(entry.deductible_ratio.value),
                    markup,
                )
            for entry in rule.out_tax:
                rate = float(entry.rate.value)
                markup = 1.0 + rate if exclusive else 1.0
                if exclusive:
                    markups[entry.price] = markup
                sale[entry.price] = (self._vat_gross_factor(entry.rate), markup)
        return markups, sale, purchase

    def _build_maintenance_credits(self) -> dict[tuple[int, int], float]:
        credits: dict[tuple[int, int], float] = {}
        for rule in self.finance.tax_rules.values():
            if not isinstance(rule, VatRule):
                continue
            for item in rule.maintenance_input_tax:
                key = (item.year, item.position)
                credits[key] = credits.get(key, 0.0) + (
                    self._vat_gross_split_factor(item.rate) * float(item.deductible_ratio.value)
                )
        return credits

    def _cost_occurrence_condition(self, inst: str, component: str, leaf: str) -> Mapping[str, Any]:
        conditions = (self.fb_instances.get(inst) or {}).get("cost_occurrence") or {}
        pieces = [component, *leaf.split(".")]
        for size in range(1, len(pieces) + 1):
            path = ".".join(pieces[:size])
            if path in conditions:
                return conditions[path]
        return {"kind": "always"}

    def _cost_occurrence_factor(self, inst: str, component: str, leaf: str) -> float:
        condition = self._cost_occurrence_condition(inst, component, leaf)
        if condition["kind"] == "always":
            return 1.0
        unit = self.unit_by_inst[inst]
        if condition["unit_ref"] != unit["unit_id"]:
            raise ValueError(
                f"费用发生条件 unit_ref={condition['unit_ref']!r} "
                f"与实例建设单元 {unit['unit_id']!r} 不一致"
            )
        if unit["mode"] == "mandatory":
            return 1.0
        return 1.0 if bool(unit["built"]) else 0.0

    def _driver_value(
        self, inst: str, target_kind: str, local: str, aggregation: str, start: int, stop: int
    ) -> float:
        if target_kind == "property":
            return self._prop_si(inst, local)
        values = self._series_si(inst, local)[start:stop]
        if aggregation == "integrate_positive":
            return sum(values) * self.step_s
        return sum(values)

    def _cost_raw(
        self,
        inst: str,
        component: str,
        start: int = 0,
        end: int | None = None,
        *,
        part: str | None = None,
    ) -> float:
        stop = self.point_count if end is None else end
        spec = self.cost_specs[(inst, component)]
        total = 0.0
        fixed_leaves = (
            *((("fixed", spec.fixed_amount),) if spec.fixed_amount is not None else ()),
            *spec.fixed_items,
        )
        for leaf, value in fixed_leaves:
            if part is not None and leaf != part and not leaf.startswith(f"{part}."):
                continue
            total += value * self._cost_occurrence_factor(inst, component, leaf)
        for term in spec.terms:
            leaf = term.leaf
            if part is not None and leaf != part and not leaf.startswith(f"{part}."):
                continue
            occ = self._cost_occurrence_factor(inst, component, leaf)
            if occ == 0.0:
                continue
            val = self._driver_value(inst, term.target_kind, term.local, term.aggregation, start, stop)
            total += val * term.coefficient * occ
        return total

    def _cost_paid_net(
        self,
        inst: str,
        component: str,
        start: int = 0,
        end: int | None = None,
        *,
        part: str | None = None,
        annual_amount: bool = True,
    ) -> tuple[float, float]:
        spec = self.cost_specs[(inst, component)]
        prefix = f"{spec.finance_type}.{component}"
        gross = 0.0
        net = 0.0
        for leaf in spec.leaves:
            if part is not None and leaf != part and not leaf.startswith(f"{part}."):
                continue
            amount = self._cost_raw(inst, component, start, end, part=leaf)
            if component == "annual_fixed_om" and annual_amount:
                amount *= _YEAR_SECONDS
            multiplier, credit = self.cost_tax_factors.get(f"{prefix}.{leaf}", (1.0, 0.0))
            gross += amount * multiplier
            net += amount * (multiplier - credit)
        return gross, net

    def _tariff_value(self, binding_id: str, start: int = 0, end: int | None = None) -> float:
        binding = (self.doc.get("tariff_bindings") or {})[binding_id]
        price_id = binding["price"]
        price = self.finance.energy_prices[price_id]
        sign = 1.0 if price.direction == "purchase" else -1.0
        stop = self.point_count if end is None else end
        values = self._series_si(binding["instance"], binding["interface"])[start:stop]
        factor = self.step_s if binding["aggregation"] == "integrate_positive" else 1.0
        if price.kind == "constant":
            price_si = _to_internal(float(price.value.value), price.value.unit)  # type: ignore[union-attr]
            return sign * price_si * sum(values) * factor
        if price.kind not in ("time_series", "monthly_daily"):
            raise ValueError(f"计费绑定 {binding_id!r} 的价格类型未知: {price.kind!r}")
        p_vals = self.prices_si[price_id]
        total = 0.0
        for offset, flow in enumerate(values):
            total += sign * p_vals[start + offset] * factor * flow
        return total

    def _build_monthly_demand(self) -> None:
        construction = self.evaluation.construction_period
        for billing_id, binding in sorted((self.doc.get("demand_bindings") or {}).items()):
            inst = binding["instance"]
            iface = binding["interface"]
            charge_id = binding["charge"]
            rate = self.finance.demand_charges[charge_id].rate
            rate_si = _money_to_internal(rate)
            series_vals = self._series_si(inst, iface)
            for month, start, end in self.month_spans:
                peak = max(series_vals[start:end])
                base_charge = peak * rate_si
                operating_month = month - 12 * construction - 1
                self._demand_rows.append(
                    {
                        "billing_id": billing_id,
                        "charge_id": charge_id,
                        "calendar_year": self.start_year + operating_month // 12,
                        "calendar_month": operating_month % 12 + 1,
                        "financial_year": (month - 1) // 12 + 1,
                        "month": month,
                        "peak": peak,
                        "base_charge": base_charge,
                        "rate": rate.to_dict(),
                    }
                )

    def _build_maintenance(self) -> None:
        positions: dict[int, int] = {}
        for entry in self.evaluation.maintenance_expenses:
            position = positions.get(entry.year, 0) + 1
            positions[entry.year] = position
            amount = float(entry.amount)
            for term in entry.terms:
                if term.kind == "scale_unit_cost":
                    factor = _unit_cost_to_internal(float(term.unit_cost.value), term.unit_cost.unit)
                    amount += self._prop_si(term.instance_id, term.property) * factor
                elif term.kind == "cost_component_share":
                    amount += self._cost_raw(term.instance_id, "upfront_capex") * float(term.ratio)
            self._maintenance_items[(entry.year, position)] = amount
            self._maintenance_annual[entry.year] = self._maintenance_annual.get(entry.year, 0.0) + amount

    def _maintenance_net(self, year: int, position: int) -> float:
        credit = self.maint_credits.get((year, position), 0.0)
        return self._maintenance_items[(year, position)] * (1.0 - credit)

    def _source_net(self, source: Any) -> float:
        key = source.identity
        if key in self._net_sources:
            return self._net_sources[key]
        if source.kind == "asset":
            val = float(self._financial_values[("asset", source.id, "scalar", 0, "basis")])
            self._net_sources[key] = val
            return val
        if source.kind == "upfront_capex_net":
            instances = self._scoped_instances(source.finance_type, source.scope_kind, source.instances)
            val = sum(
                self._cost_paid_net(inst, "upfront_capex")[1]
                for inst in instances
                if (inst, "upfront_capex") in self.cost_specs
            )
            self._net_sources[key] = val
            return val
        val = self._maintenance_net(source.year, source.position)
        self._net_sources[key] = val
        return val

    def _scoped_instances(
        self, finance_type: str | None, scope_kind: str, instances: Sequence[str]
    ) -> tuple[str, ...]:
        if scope_kind == "instances":
            return tuple(instances)
        return tuple(
            inst
            for inst, binding in sorted(self.fb_instances.items())
            if binding.get("finance_type") == finance_type and self.devices[inst].get("asset_origin") == "new"
        )

    def _amount_span(self, axis: str, period: int) -> tuple[int, int]:
        if axis == "scalar":
            return 0, self.point_count
        if axis == "financial_year":
            idx = period - self.evaluation.construction_period - 1
            if idx < 0 or idx >= len(self.operating_spans):
                return 0, 0
            return self.operating_spans[idx]
        return self.month_span_map.get(period, _DEFAULT_MONTH_SPAN)

    def _amount_reference(
        self, reference: Any, current_axis: str = "scalar", current_period: int = 0
    ) -> float:
        if reference.kind == "maintenance":
            raw = self._maintenance_items[(reference.year, reference.position)]
            net = self._maintenance_net(reference.year, reference.position)
            return _amount_basis(raw, raw, net, reference.basis)
        axis, periods = expand_amount_period(reference.period, current_axis, current_period)
        return sum(self._amount_source(reference, axis, period) for period in periods)

    def _amount_source(self, reference: Any, axis: str, period: int) -> float:
        kind = reference.kind
        if kind == "amount":
            return self._named_amount(reference.id, period)
        if kind == "baseline":
            fact = self.evaluation.comparison.facts[reference.id]
            index = period - 1 if axis == "tax_month" else period
            return _to_internal(float(fact.values[index]), fact.unit)
        if kind == "movement":
            return self._movement_values[(reference.id, axis, period)][reference.basis]
        if kind == "asset":
            return float(self._financial_values[("asset", reference.id, axis, period, reference.measure)])
        if kind == "tax":
            owner = "pool" if reference.source.kind == "pool" else "tax_rule"
            return float(
                self._financial_values[(owner, reference.source.id, axis, period, reference.measure)]
            )
        if kind == "construction_unit":
            return 1.0 if bool(self.unit_by_id[reference.unit_ref]["built"]) else 0.0
        start, end = self._amount_span(axis, period)
        if kind == "driver":
            total = 0.0
            for inst in self._amount_instances(reference):
                target = self.fb_instances[inst]["drivers"][reference.driver]
                local = target.get("property", target.get("interface"))
                target_kind = "property" if "property" in target else "interface"
                agg = target.get("aggregation", "scalar")
                total += self._driver_value(inst, target_kind, local, agg, start, end)
            return total
        if kind == "system_quantity":
            vals = self._system_series_si(reference.block, reference.variable)[start:end]
            factor = self.step_s if reference.aggregation == "integrate_positive" else 1.0
            return sum(vals) * factor
        if kind == "cost":
            _, component, *parts = reference.path.split(".")
            part = ".".join(parts) or None
            total = 0.0
            for inst in self._amount_instances(reference):
                if self.devices[inst].get("asset_origin") != "new" and component not in _COMPONENTS_EXISTING:
                    continue
                if axis != "scalar" and start == end:
                    continue
                raw = self._cost_raw(inst, component, start, end, part=part)
                if component == "annual_fixed_om" and axis != "scalar":
                    raw *= _YEAR_SECONDS
                gross, net = self._cost_paid_net(
                    inst, component, start, end, part=part, annual_amount=axis != "scalar"
                )
                total += _amount_basis(raw, gross, net, reference.basis)
            return total
        if kind == "tariff":
            price = self.finance.energy_prices[reference.price]
            sign = 1.0 if price.direction == "purchase" else -1.0
            credit, markup = (
                self.purchase_tax_factors if price.direction == "purchase" else self.sale_tax_factors
            ).get(reference.price, (0.0, 1.0))
            total = 0.0
            for binding_id in reference.bindings:
                raw = sign * self._tariff_value(binding_id, start, end)
                gross = raw * markup
                net = raw * (markup - credit)
                total += _amount_basis(raw, gross, net, reference.basis)
            return total
        if kind == "demand":
            if start == end:
                return 0.0
            multiplier, credit = self.demand_tax_factors.get(reference.charge, (1.0, 0.0))
            total = 0.0
            for row in self._demand_rows:
                selected = (
                    row["financial_year"] == period if axis == "financial_year" else row["month"] == period
                )
                if row["billing_id"] not in reference.bindings or not selected:
                    continue
                raw = row["base_charge"]
                total += _amount_basis(raw, raw * multiplier, raw * (multiplier - credit), reference.basis)
            return total
        raise ValueError(f"不支持的金额引用来源类型: {kind!r}")

    def _amount_instances(self, reference: Any) -> tuple[str, ...]:
        category = reference.finance_type or (reference.path.split(".")[0] if reference.path else None)
        return self._scoped_instances(category, reference.scope.kind, reference.scope.instances)

    def _named_amount(self, amount_id: str, period: int) -> float:
        key = (amount_id, period)
        if key not in self._named_amounts:
            definition = self.finance.amounts[amount_id]
            total = _money_to_internal(definition.constant)
            for term in definition.terms.values():
                if term.coefficient.value:
                    coeff = _money_to_internal(term.coefficient)
                    total += self._amount_reference(term.source, definition.axis, period) * coeff
            self._named_amounts[key] = total
        return self._named_amounts[key]

    def _movement_vat_factors(self, identity: str) -> tuple[float, float, float]:
        movement = self.finance.cash_movements[identity]
        multiplier, input_factor, output_factor = 1.0, 0.0, 0.0
        for rule in self.finance.tax_rules.values():
            if not isinstance(rule, VatRule):
                continue
            entries = rule.movement_input_tax if movement.direction == "outflow" else rule.movement_output_tax
            for entry in entries:
                if entry.movement != identity:
                    continue
                rate = float(entry.rate.value)
                exclusive = movement.price_basis == "tax_exclusive"
                multiplier = 1.0 + rate if exclusive else 1.0
                tax = rate if exclusive else rate / (1.0 + rate)
                if movement.direction == "outflow":
                    input_factor = tax * float(entry.deductible_ratio.value)
                else:
                    output_factor = tax
        return multiplier, input_factor, output_factor

    def _eval_movement(self, identity: str, axis: str, period: int) -> float:
        movement = self.finance.cash_movements[identity]
        raw = self._amount_reference(movement.amount, axis, period)
        multiplier, input_factor, output_factor = self._movement_vat_factors(identity)
        values = {
            "raw": raw,
            "gross": raw * multiplier,
            "deductible_input": raw * input_factor,
            "output_tax": raw * output_factor,
            "net_basis": raw * (multiplier - input_factor),
            "net_revenue": raw * (multiplier - output_factor),
        }
        limit = self.evaluation.evaluation_years * (12 if axis == "tax_month" else 1)
        payments = []
        unpaid = 0.0
        for payment in movement.payments:
            paid_period = period + payment.offset
            amount = values["gross"] * float(payment.ratio)
            payments.append(
                {
                    "at": {"kind": axis, "year" if axis == "financial_year" else "month": paid_period},
                    "ratio": str(payment.ratio),
                    "amount": amount,
                }
            )
            if paid_period <= limit:
                year = paid_period if axis == "financial_year" else (paid_period + 11) // 12
                sign = 1.0 if movement.direction == "inflow" else -1.0
                self._year_cash[year] = self._year_cash.get(year, 0.0) + amount * sign
            else:
                unpaid += amount
        self._movement_values[(identity, axis, period)] = values
        self._fact_movements.append(
            {
                "id": identity,
                "axis": axis,
                "period": period,
                "direction": movement.direction,
                "price_basis": movement.price_basis,
                **values,
                "payments": payments,
                "end_unpaid": unpaid,
            }
        )
        return raw

    def _depreciation_amount(self, rule_id: str, index: int, entry: Any, year: int) -> float:
        cache_key = (rule_id, index, year)
        if cache_key in self._depreciation_values:
            return self._depreciation_values[cache_key]
        depreciable = self._source_net(entry.base_from)
        residual = float(entry.residual.value)
        if entry.residual.unit == "1":
            residual_amount = depreciable * residual
        elif residual > 0.0 and depreciable > 0.0:
            residual_amount = residual
        else:
            residual_amount = 0.0
        first = self.evaluation.construction_period + entry.first_year
        annual_dec = straight_line_period_amount(
            depreciable_base=_to_decimal(depreciable, f"depreciation.{rule_id}.{index}"),
            residual=_to_decimal(residual_amount, f"depreciation.{rule_id}.{index}.residual"),
            life_years=int(entry.life_years),
            period_index=year,
            first_period=first,
        )
        annual = float(annual_dec)
        self._fact_depr.append(
            {
                "rule": rule_id,
                "base_from": entry.base_from.to_dict(),
                "year": year,
                "amount": _fmt_decimal(annual_dec, f"depreciation.{rule_id}.{index}.y{year}"),
            }
        )
        self._depreciation_values[cache_key] = annual
        return annual

    def _eval_asset(self, identity: str, year: int, measure: str) -> float:
        if measure == "basis":
            basis = self._amount_reference(self.finance.assets[identity].basis_from)
            if basis < -1e-9:
                raise ValueError(f"资产 {identity!r} 计税基础不得为负: {basis}")
            return max(basis, 0.0) if basis < 0.0 else basis
        basis = float(self._financial_values[("asset", identity, "scalar", 0, "basis")])
        value = basis if measure == "book_value" else 0.0
        for rule_id, indexed in self._depreciation_by_rule.items():
            for index, entry in indexed:
                if entry.base_from.kind != "asset" or entry.base_from.id != identity:
                    continue
                first = self.evaluation.construction_period + entry.first_year
                periods = (
                    range(first, min(year, first + entry.life_years - 1) + 1)
                    if measure == "book_value"
                    else (year,)
                )
                for p in periods:
                    if first <= p < first + entry.life_years:
                        annual = self._depreciation_amount(rule_id, index, entry, p)
                        value += -annual if measure == "book_value" else annual
        return value

    def _vat_rule_quantity(self, rule_id: str, month: int, measure: str) -> float:
        rule = self.finance.tax_rules[rule_id]
        span = self.month_span_map.get(month)
        total = 0.0
        if isinstance(rule, VatRule):
            if measure == "output_tax":
                for entry in rule.out_tax:
                    factor = self._vat_gross_factor(entry.rate)
                    if span is not None:
                        s, e = span
                        for binding_id, binding in sorted((self.doc.get("tariff_bindings") or {}).items()):
                            if binding["price"] == entry.price:
                                total += -factor * self._tariff_value(binding_id, s, e)
                for entry in rule.movement_output_tax:
                    total += self._movement_confirmed_vat(entry, month, "output_tax")
                return total
            for entry in rule.demand_input_tax:
                factor = self._vat_gross_factor(entry.rate) * float(entry.deductible_ratio.value)
                for row in self._demand_rows:
                    if row["charge_id"] == entry.charge and row["month"] == month:
                        total += row["base_charge"] * factor
            for entry in rule.in_tax:
                factor = self._vat_gross_factor(entry.rate) * float(entry.deductible_ratio.value)
                if span is not None:
                    s, e = span
                    for binding_id, binding in sorted((self.doc.get("tariff_bindings") or {}).items()):
                        if binding["price"] == entry.price:
                            total += factor * self._tariff_value(binding_id, s, e)
            for entry in rule.om_input_tax:
                finance_type, _, tail = entry.for_path.partition(".")
                component, _, part = tail.partition(".")
                part_arg = part or None
                factor = self._vat_gross_factor(entry.rate) * float(entry.deductible_ratio.value)
                if component == "annual_fixed_om":
                    if month % 12 == 0 and (
                        self.evaluation.construction_period + 1
                        <= month // 12
                        <= self.evaluation.construction_period + self.evaluation.operation_years
                    ):
                        for (inst, comp), spec in sorted(self.cost_specs.items()):
                            if spec.finance_type == finance_type and comp == component:
                                total += self._cost_raw(inst, comp, part=part_arg) * _YEAR_SECONDS * factor
                elif span is not None:
                    s, e = span
                    for (inst, comp), spec in sorted(self.cost_specs.items()):
                        if spec.finance_type == finance_type and comp == component:
                            total += self._cost_raw(inst, comp, s, e, part=part_arg) * factor
            for entry in rule.maintenance_input_tax:
                if 12 * entry.year == month:
                    factor = self._vat_gross_split_factor(entry.rate) * float(entry.deductible_ratio.value)
                    total += self._maintenance_items[(entry.year, entry.position)] * factor
            for entry in rule.movement_input_tax:
                total += self._movement_confirmed_vat(entry, month, "deductible_input")
            return total

        if measure == "output_tax":
            return 0.0
        finance_type, _, tail = rule.for_path.partition(".")
        _, _, part = tail.partition(".")
        part_arg = part or None
        factor = self._vat_gross_factor(rule.rate) * float(rule.deductible_ratio.value)
        if rule.confirmation.method == "payment_period":
            periods = tuple((12 * p.t + 1, float(p.ratio)) for p in self.evaluation.construction_payments)
        else:
            periods = tuple((item.period, float(item.ratio)) for item in rule.confirmation.schedule)
        for confirmed_month, ratio in periods:
            if confirmed_month == month and ratio:
                for (inst, comp), spec in sorted(self.cost_specs.items()):
                    if spec.finance_type == finance_type and comp == "upfront_capex":
                        total += self._cost_raw(inst, comp, part=part_arg) * factor * ratio
        return total

    def _movement_confirmed_vat(self, entry: Any, month: int, field: str) -> float:
        total = 0.0
        movement = self.finance.cash_movements[entry.movement]
        for at in cash_movement_occurrences(movement):
            if entry.confirmation.method == "recognition_period":
                schedule = ((at.period if at.axis == "tax_month" else 12 * at.period, 1.0),)
            else:
                occurrence = next(item for item in entry.confirmation.occurrences if item.at == at)
                schedule = tuple((item.period, float(item.ratio)) for item in occurrence.schedule)
            for confirmed, ratio in schedule:
                if confirmed == month and ratio:
                    total += self._movement_values[(entry.movement, at.axis, at.period)][field] * ratio
        return total

    def _ensure_vat_pool_until(self, pool_id: str, target_month: int) -> None:
        built = self._vat_built.get(pool_id, 0)
        if built >= target_month:
            return
        pool = self.finance.credit_pools[pool_id]
        if pool_id not in self._vat_pool_state:
            self._vat_pool_state[pool_id] = _to_decimal(
                _money_to_internal(pool.opening_credit), f"credit_pools.{pool_id}.opening_credit"
            )
            for refund in pool.refunds:
                self._fact_refunds.append(
                    {
                        "pool": pool_id,
                        "period": refund.period,
                        "arrival_period": refund.arrival_period,
                        "amount": _money_to_internal(refund.amount),
                    }
                )
        max_months = 12 * self.evaluation.evaluation_years
        claims: dict[int, float] = {}
        arrivals: dict[int, float] = {}
        for refund in pool.refunds:
            amt = _money_to_internal(refund.amount)
            claims[refund.period] = claims.get(refund.period, 0.0) + amt
            if refund.arrival_period <= max_months:
                arrivals[refund.arrival_period] = arrivals.get(refund.arrival_period, 0.0) + amt

        for month in range(built + 1, target_month + 1):
            out_val = 0.0
            in_val = 0.0
            for rule_id, rule in self.finance.tax_rules.items():
                if isinstance(rule, (VatRule, ConstructionVatRule)) and rule.credit_pool == pool_id:
                    out_key = ("tax_rule", rule_id, "tax_month", month, "output_tax")
                    in_key = ("tax_rule", rule_id, "tax_month", month, "deductible_input")
                    if out_key not in self._financial_values:
                        self._financial_values[out_key] = self._vat_rule_quantity(
                            rule_id, month, "output_tax"
                        )
                    if in_key not in self._financial_values:
                        self._financial_values[in_key] = self._vat_rule_quantity(
                            rule_id, month, "deductible_input"
                        )
                    out_val += float(self._financial_values[out_key])
                    in_val += float(self._financial_values[in_key])
            claim = claims.get(month, 0.0)
            arrival = arrivals.get(month, 0.0)
            opening_dec = self._vat_pool_state[pool_id]
            out_dec = _to_decimal(out_val, f"vat.{pool_id}.m{month}.output")
            in_dec = _to_decimal(in_val, f"vat.{pool_id}.m{month}.input")
            claim_dec = _to_decimal(claim, f"vat.{pool_id}.m{month}.claim")
            settlement = settle_vat_period(
                opening_credit=opening_dec,
                output_vat=out_dec,
                input_vat=in_dec,
                refund_claim=claim_dec,
            )
            self._vat_pool_state[pool_id] = settlement.carry_next
            year = (month + 11) // 12
            if settlement.paid:
                self._year_cash[year] = self._year_cash.get(year, 0.0) - float(settlement.paid)
            if arrival:
                self._year_cash[year] = self._year_cash.get(year, 0.0) + arrival
            receivable = sum(
                _money_to_internal(r.amount)
                for r in pool.refunds
                if r.period <= month < r.arrival_period
            )
            self._fact_vat.append(
                {
                    "pool": pool_id,
                    "month": month,
                    "paid": _fmt_decimal(settlement.paid, f"vat.{pool_id}.m{month}.paid"),
                    "refund_claimed": _fmt_float(claim, f"vat.{pool_id}.m{month}.claim"),
                    "refund_arrived": _fmt_float(arrival, f"vat.{pool_id}.m{month}.arrival"),
                    "carry": _fmt_decimal(settlement.carry_next, f"vat.{pool_id}.m{month}.carry"),
                    "receivable": _fmt_float(receivable, f"vat.{pool_id}.m{month}.receivable"),
                }
            )
            self._financial_values[("pool", pool_id, "tax_month", month, "paid")] = float(settlement.paid)
            self._financial_values[("pool", pool_id, "tax_month", month, "credit_balance")] = float(
                settlement.carry_next
            )
            self._vat_built[pool_id] = month

    def _eval_pool(self, pool_id: str, month: int, measure: str) -> float:
        if measure in ("paid", "credit_balance"):
            self._ensure_vat_pool_until(pool_id, month)
            return float(self._financial_values[("pool", pool_id, "tax_month", month, measure)])
        pool = self.finance.credit_pools[pool_id]
        total = 0.0
        for refund in pool.refunds:
            if measure == "refund_confirmed" and refund.period == month:
                total += _money_to_internal(refund.amount)
            elif measure == "refund_received" and refund.arrival_period == month:
                total += _money_to_internal(refund.amount)
            elif measure == "receivable" and refund.period <= month < refund.arrival_period:
                total += _money_to_internal(refund.amount)
        return total

    # -- Structural affine lower bounds over frozen context (zero SolverBundle) --

    def _bound_var_affine(self, inst: str, prop: str) -> _AffineExpr:
        bound_var = self.bound_prop.get((inst, prop))
        if bound_var is None:
            return _AffineExpr(const=self._prop_si(inst, prop))
        sym = ("prop", inst, bound_var)
        if sym not in self._bound_symbols:
            self._bound_symbols[sym] = self._prop_interval(inst, prop)
        return _AffineExpr(terms={sym: 1.0})

    def _unit_affine(self, unit_ref: str) -> _AffineExpr:
        unit = self.declared_units.get(unit_ref)
        if unit is not None and unit.get("mode") != "optional":
            return _AffineExpr(const=1.0)
        sym = ("unit", unit_ref)
        self._bound_symbols.setdefault(sym, (0.0, 1.0))
        return _AffineExpr(terms={sym: 1.0})

    def _expr_bounds(self, expr: _AffineExpr) -> tuple[float, float]:
        lo = expr.const
        hi = expr.const
        for sym, coeff in expr.terms.items():
            s_lo, s_hi = self._bound_symbols[sym]
            a, b = _interval_scale((s_lo, s_hi), coeff)
            lo += a
            hi += b
        return (lo, hi)

    def _iface_affine(
        self, inst: str, iface: str, start: int, stop: int, aggregation: str
    ) -> _AffineExpr:
        factor = self.step_s if aggregation == "integrate_positive" else 1.0
        if (inst, iface) in self.predefined_series_si:
            return _AffineExpr(const=sum(self.predefined_series_si[(inst, iface)][start:stop]) * factor)
        sym = ("iface", inst, iface, start, stop, aggregation)
        if sym not in self._bound_symbols:
            self._bound_symbols[sym] = _interval_scale(
                self._iface_step_interval(inst, iface, start, stop), factor
            )
        return _AffineExpr(terms={sym: 1.0})

    def _cost_leaf_affine(self, inst: str, component: str, leaf: str, start: int, stop: int) -> _AffineExpr:
        spec = self.cost_specs[(inst, component)]
        fixed_val = spec.fixed_amount if leaf == "fixed" else dict(spec.fixed_items).get(leaf)
        if fixed_val is not None:
            expr = _AffineExpr(const=fixed_val)
        else:
            term = next(t for t in spec.terms if t.leaf == leaf)
            driver_expr = (
                self._bound_var_affine(inst, term.local)
                if term.target_kind == "property"
                else self._iface_affine(inst, term.local, start, stop, term.aggregation)
            )
            expr = driver_expr.scaled(term.coefficient)

        condition = self._cost_occurrence_condition(inst, component, leaf)
        if condition["kind"] != "always":
            unit_ref = condition["unit_ref"]
            unit = self.declared_units.get(unit_ref)
            if unit is None or unit.get("mode") == "optional":
                if not expr.terms and fixed_val is not None:
                    return self._unit_affine(unit_ref).scaled(fixed_val)
                scales = unit.get("scales") if isinstance(unit, Mapping) else None
                if expr.const == 0.0 and isinstance(scales, Mapping) and all(
                    sym[0] == "prop" and sym[1] == inst and sym[2] in scales for sym in expr.terms
                ):
                    return expr
                lo, hi = self._expr_bounds(expr)
                sym = ("cond_cost", inst, component, leaf, start, stop)
                self._bound_symbols[sym] = (min(lo, 0.0), max(hi, 0.0))
                return _AffineExpr(terms={sym: 1.0})
        return expr

    def _cost_component_affine(
        self,
        inst: str,
        component: str,
        start: int = 0,
        stop: int | None = None,
        *,
        part: str | None = None,
        basis: str = "raw",
        annual_amount: bool = False,
    ) -> _AffineExpr:
        end = self.point_count if stop is None else stop
        spec = self.cost_specs[(inst, component)]
        prefix = f"{spec.finance_type}.{component}"
        acc = _AffineExpr()
        for leaf in spec.leaves:
            if part is not None and leaf != part and not leaf.startswith(f"{part}."):
                continue
            leaf_expr = self._cost_leaf_affine(inst, component, leaf, start, end)
            scale = _YEAR_SECONDS if (component == "annual_fixed_om" and annual_amount) else 1.0
            if basis != "raw":
                mult, cred = self.cost_tax_factors.get(f"{prefix}.{leaf}", (1.0, 0.0))
                scale *= _amount_basis(1.0, mult, mult - cred, basis)
            acc.add_scaled(leaf_expr, scale)
        return acc

    def _tariff_affine(self, binding_id: str, start: int, stop: int) -> _AffineExpr:
        binding = (self.doc.get("tariff_bindings") or {})[binding_id]
        price_id = binding["price"]
        price = self.finance.energy_prices[price_id]
        sign = 1.0 if price.direction == "purchase" else -1.0
        agg = binding["aggregation"]
        inst, iface = binding["instance"], binding["interface"]
        if price.kind == "constant":
            price_si = _to_internal(float(price.value.value), price.value.unit)  # type: ignore[union-attr]
            return self._iface_affine(inst, iface, start, stop, agg).scaled(sign * price_si)
        agg_factor = self.step_s if agg == "integrate_positive" else 1.0
        p_vals = self.prices_si[price_id]
        if (inst, iface) in self.predefined_series_si:
            e_vals = self.predefined_series_si[(inst, iface)][start:stop]
            return _AffineExpr(
                const=sum(sign * p_vals[start + i] * agg_factor * flow for i, flow in enumerate(e_vals))
            )
        sym = ("tariff", binding_id, start, stop)
        if sym not in self._bound_symbols:
            step_bounds = self._iface_step_interval(inst, iface, 0, 1)
            acc = (0.0, 0.0)
            for idx in range(start, stop):
                acc = _interval_add(acc, _interval_scale(step_bounds, sign * p_vals[idx] * agg_factor))
            self._bound_symbols[sym] = acc
        return _AffineExpr(terms={sym: 1.0})

    def _maintenance_affine(self, year: int, position: int) -> _AffineExpr:
        pos = 0
        for entry in self.evaluation.maintenance_expenses:
            if entry.year == year:
                pos += 1
                if pos == position:
                    acc = _AffineExpr(const=float(entry.amount))
                    for term in entry.terms:
                        if term.kind == "scale_unit_cost":
                            factor = _unit_cost_to_internal(float(term.unit_cost.value), term.unit_cost.unit)
                            acc.add_scaled(self._bound_var_affine(term.instance_id, term.property), factor)
                        elif term.kind == "cost_component_share":
                            if (term.instance_id, "upfront_capex") in self.cost_specs:
                                acc.add_scaled(
                                    self._cost_component_affine(term.instance_id, "upfront_capex"),
                                    float(term.ratio),
                                )
                    return acc
        return _AffineExpr()

    def _demand_row_affine(self, row: Mapping[str, Any]) -> _AffineExpr:
        billing_id = row["billing_id"]
        charge_id = row["charge_id"]
        month = row["month"]
        sym = ("demand", billing_id, charge_id, month)
        if sym not in self._bound_symbols:
            binding = (self.doc.get("demand_bindings") or {})[billing_id]
            m_start, m_end = self.month_span_map.get(month, _DEFAULT_MONTH_SPAN)
            self._bound_symbols[sym] = self._iface_step_interval(
                binding["instance"], binding["interface"], m_start, min(m_start + 1, m_end)
            )
        rate_si = _money_to_internal(self.finance.demand_charges[charge_id].rate)
        return _AffineExpr(terms={sym: rate_si})

    def _amount_source_affine(self, reference: Any, axis: str, period: int) -> _AffineExpr:
        kind = reference.kind
        if kind == "amount":
            definition = self.finance.amounts[reference.id]
            acc = _AffineExpr(const=_money_to_internal(definition.constant))
            for term in definition.terms.values():
                if term.coefficient.value:
                    coeff = _money_to_internal(term.coefficient)
                    acc.add_scaled(self._amount_reference_affine(term.source, definition.axis, period), coeff)
            return acc
        if kind == "baseline":
            fact = self.evaluation.comparison.facts[reference.id]
            index = period - 1 if axis == "tax_month" else period
            return _AffineExpr(const=_to_internal(float(fact.values[index]), fact.unit))
        if kind == "movement":
            movement = self.finance.cash_movements[reference.id]
            mult, in_f, out_f = self._movement_vat_factors(reference.id)
            factor = _amount_basis(
                1.0,
                mult,
                mult - (in_f if movement.direction == "outflow" else out_f),
                reference.basis,
            )
            return self._amount_reference_affine(movement.amount, axis, period).scaled(factor)
        if kind == "asset" and reference.measure == "basis":
            basis_expr = self._amount_reference_affine(self.finance.assets[reference.id].basis_from)
            lo, hi = self._expr_bounds(basis_expr)
            if lo >= -1e-9:
                return basis_expr
            sym = ("asset_basis", reference.id)
            self._bound_symbols[sym] = (max(lo, 0.0), max(hi, 0.0))
            return _AffineExpr(terms={sym: 1.0})
        if kind == "construction_unit":
            return self._unit_affine(reference.unit_ref)
        start, end = self._amount_span(axis, period)
        if kind == "cost":
            _, component, *parts = reference.path.split(".")
            part = ".".join(parts) or None
            acc = _AffineExpr()
            for inst in self._amount_instances(reference):
                if self.devices[inst].get("asset_origin") != "new" and component not in _COMPONENTS_EXISTING:
                    continue
                if axis != "scalar" and start == end:
                    continue
                acc.add_scaled(
                    self._cost_component_affine(
                        inst, component, start, end,
                        part=part, basis=reference.basis, annual_amount=axis != "scalar",
                    )
                )
            return acc
        if kind == "tariff":
            price = self.finance.energy_prices[reference.price]
            sign = 1.0 if price.direction == "purchase" else -1.0
            credit, markup = (
                self.purchase_tax_factors if price.direction == "purchase" else self.sale_tax_factors
            ).get(reference.price, (0.0, 1.0))
            factor = _amount_basis(1.0, markup, markup - credit, reference.basis)
            acc = _AffineExpr()
            for binding_id in reference.bindings:
                acc.add_scaled(self._tariff_affine(binding_id, start, end), sign * factor)
            return acc
        if kind == "driver":
            acc = _AffineExpr()
            for inst in self._amount_instances(reference):
                target = self.fb_instances[inst]["drivers"][reference.driver]
                if "property" in target:
                    acc.add_scaled(self._bound_var_affine(inst, str(target["property"])))
                else:
                    acc.add_scaled(
                        self._iface_affine(
                            inst,
                            str(target["interface"]),
                            start,
                            end,
                            str(target.get("aggregation", "scalar")),
                        )
                    )
            return acc
        if kind == "system_quantity":
            if axis != "scalar" and start == end:
                return _AffineExpr()
            sym = (
                "system_quantity",
                reference.block,
                reference.variable,
                reference.aggregation,
                start,
                end,
            )
            self._bound_symbols.setdefault(sym, (-math.inf, math.inf))
            return _AffineExpr(terms={sym: 1.0})
        if kind == "demand":
            if start == end:
                return _AffineExpr()
            multiplier, credit = self.demand_tax_factors.get(reference.charge, (1.0, 0.0))
            factor = _amount_basis(1.0, multiplier, multiplier - credit, reference.basis)
            acc = _AffineExpr()
            for row in self._demand_rows:
                selected = (
                    row["financial_year"] == period if axis == "financial_year" else row["month"] == period
                )
                if row["billing_id"] in reference.bindings and selected:
                    acc.add_scaled(self._demand_row_affine(row), factor)
            return acc
        sym = ("unbounded", kind, json.dumps(reference.to_dict(), sort_keys=True), axis, period)
        self._bound_symbols.setdefault(sym, (-math.inf, math.inf))
        return _AffineExpr(terms={sym: 1.0})

    def _amount_reference_affine(
        self, reference: Any, current_axis: str = "scalar", current_period: int = 0
    ) -> _AffineExpr:
        if reference.kind == "maintenance":
            m_expr = self._maintenance_affine(reference.year, reference.position)
            if reference.basis == "net_basis":
                credit = self.maint_credits.get((reference.year, reference.position), 0.0)
                return m_expr.scaled(1.0 - credit)
            return m_expr
        axis, periods = expand_amount_period(reference.period, current_axis, current_period)
        acc = _AffineExpr()
        for period in periods:
            acc.add_scaled(self._amount_source_affine(reference, axis, period))
        return acc

    def _income_base_lower_bound(self, rule_id: str, rule: IncomeTaxRule, year: int) -> float:
        """Structural lower bound of taxable base in ``year`` preserving shared-expression cancellation."""
        base_expr = _AffineExpr()
        span = self.span_by_year.get(year)
        if span is not None:
            start_step, end_step = span
            for binding_id, binding in sorted((self.doc.get("tariff_bindings") or {}).items()):
                price_id = binding["price"]
                if price_id in rule.revenue:
                    tax, markup = self.sale_tax_factors.get(price_id, (0.0, 1.0))
                    base_expr.add_scaled(
                        self._tariff_affine(binding_id, start_step, end_step), -(markup - tax)
                    )
                elif price_id in rule.energy_cost:
                    credit, markup = self.purchase_tax_factors.get(price_id, (0.0, 1.0))
                    base_expr.add_scaled(
                        self._tariff_affine(binding_id, start_step, end_step), -(markup - credit)
                    )
            demand_refs = set(rule.demand_cost)
            for row in self._demand_rows:
                if row["charge_id"] in demand_refs and row["financial_year"] == year:
                    gross, credit = self.demand_tax_factors.get(row["charge_id"], (1.0, 0.0))
                    base_expr.add_scaled(self._demand_row_affine(row), -(gross - credit))
            for for_path, is_annual in (
                *((p, True) for p in rule.annual_om),
                *((p, False) for p in rule.variable_om),
            ):
                finance_type, component, *_ = for_path.split(".")
                selected = set(cost_target_leaves(for_path, self.finance.finance_types))
                prefix = f"{finance_type}.{component}"
                for (inst, comp), spec in sorted(self.cost_specs.items()):
                    if spec.finance_type != finance_type or comp != component:
                        continue
                    for leaf in spec.leaves:
                        path = f"{prefix}.{leaf}"
                        if path not in selected:
                            continue
                        gross_f, credit_f = self.cost_tax_factors.get(
                            path, self.cost_tax_factors.get(prefix, (1.0, 0.0))
                        )
                        leaf_expr = (
                            self._cost_leaf_affine(inst, comp, leaf, 0, self.point_count).scaled(
                                _YEAR_SECONDS
                            )
                            if is_annual
                            else self._cost_leaf_affine(inst, comp, leaf, start_step, end_step)
                        )
                        base_expr.add_scaled(leaf_expr, -(gross_f - credit_f))

        for item in rule.deductible_maintenance.items:
            if item.year == year and item.deductible:
                credit = self.maint_credits.get((item.year, item.position), 0.0)
                base_expr.add_scaled(self._maintenance_affine(item.year, item.position), -(1.0 - credit))

        construction = self.evaluation.construction_period
        for index, entry in enumerate(rule.depreciation):
            first = construction + entry.first_year
            if first <= year < first + entry.life_years:
                src = entry.base_from
                if src.kind == "asset":
                    src_expr = self._amount_reference_affine(self.finance.assets[src.id].basis_from)
                    s_lo, s_hi = self._expr_bounds(src_expr)
                    if s_lo < -1e-9:
                        sym = ("asset_depr_src", src.id)
                        self._bound_symbols[sym] = (max(s_lo, 0.0), max(s_hi, 0.0))
                        src_expr = _AffineExpr(terms={sym: 1.0})
                        s_lo, s_hi = self._expr_bounds(src_expr)
                elif src.kind == "upfront_capex_net":
                    src_expr = _AffineExpr()
                    for inst in self._scoped_instances(src.finance_type, src.scope_kind, src.instances):
                        if (inst, "upfront_capex") in self.cost_specs:
                            src_expr.add_scaled(
                                self._cost_component_affine(inst, "upfront_capex", basis="net_basis")
                            )
                    s_lo, s_hi = self._expr_bounds(src_expr)
                else:
                    credit = self.maint_credits.get((src.year, src.position), 0.0)
                    src_expr = self._maintenance_affine(src.year, src.position).scaled(1.0 - credit)
                    s_lo, s_hi = self._expr_bounds(src_expr)
                res_val = float(entry.residual.value)
                life = float(entry.life_years)
                if entry.residual.unit == "1":
                    base_expr.add_scaled(src_expr, -(1.0 - res_val) / life)
                elif res_val == 0.0:
                    base_expr.add_scaled(src_expr, -1.0 / life)
                elif s_lo >= res_val:
                    base_expr.add_scaled(src_expr, -1.0 / life)
                    base_expr.const += res_val / life
                else:
                    depr_hi = s_hi / life if s_lo <= 0.0 else max(s_hi - res_val, 0.0) / life
                    sym = ("depr_pw", rule_id, index, year)
                    self._bound_symbols[sym] = (0.0, depr_hi)
                    base_expr.add_scaled(_AffineExpr(terms={sym: 1.0}), -1.0)

        for movement_id in rule.movement_revenue:
            movement = self.finance.cash_movements[movement_id]
            mult, _, out_f = self._movement_vat_factors(movement_id)
            for at in cash_movement_occurrences(movement):
                occ_year = max(1, at.period) if at.axis == "financial_year" else (at.period + 11) // 12
                if occ_year == year:
                    base_expr.add_scaled(
                        self._amount_reference_affine(movement.amount, at.axis, at.period),
                        mult - out_f,
                    )
        for movement_id in rule.movement_cost:
            movement = self.finance.cash_movements[movement_id]
            mult, in_f, _ = self._movement_vat_factors(movement_id)
            for at in cash_movement_occurrences(movement):
                occ_year = max(1, at.period) if at.axis == "financial_year" else (at.period + 11) // 12
                if occ_year == year:
                    base_expr.add_scaled(
                        self._amount_reference_affine(movement.amount, at.axis, at.period),
                        -(mult - in_f),
                    )
        for derived_id in rule.derived_tax_cost:
            d_rule = self.finance.tax_rules[derived_id]
            axis = d_rule.axis
            periods = (year,) if axis == "financial_year" else range(12 * (year - 1) + 1, 12 * year + 1)
            for p in periods:
                assessed_val = float(self._financial_values[("tax_rule", derived_id, axis, p, "assessed")])
                base_expr.const -= max(assessed_val, 0.0)
        return self._expr_bounds(base_expr)[0]

    def _income_taxable_base(self, rule_id: str, rule: IncomeTaxRule, year: int) -> float:
        base = 0.0
        span = self.span_by_year.get(year)
        if span is not None:
            start_step, end_step = span
            for binding_id, binding in sorted((self.doc.get("tariff_bindings") or {}).items()):
                price_id = binding["price"]
                if price_id in rule.revenue:
                    tax, markup = self.sale_tax_factors.get(price_id, (0.0, 1.0))
                    base += -(markup - tax) * self._tariff_value(binding_id, start_step, end_step)
                elif price_id in rule.energy_cost:
                    credit, markup = self.purchase_tax_factors.get(price_id, (0.0, 1.0))
                    base += -(markup - credit) * self._tariff_value(binding_id, start_step, end_step)
            demand_refs = set(rule.demand_cost)
            for row in self._demand_rows:
                if row["charge_id"] in demand_refs and row["financial_year"] == year:
                    gross, credit = self.demand_tax_factors.get(row["charge_id"], (1.0, 0.0))
                    base += -(gross - credit) * row["base_charge"]
            for for_path, is_annual in (
                *((p, True) for p in rule.annual_om),
                *((p, False) for p in rule.variable_om),
            ):
                finance_type, component, *_ = for_path.split(".")
                selected = set(cost_target_leaves(for_path, self.finance.finance_types))
                prefix = f"{finance_type}.{component}"
                for (inst, comp), spec in sorted(self.cost_specs.items()):
                    if spec.finance_type != finance_type or comp != component:
                        continue
                    for leaf in spec.leaves:
                        path = f"{prefix}.{leaf}"
                        if path not in selected:
                            continue
                        gross_factor, credit_factor = self.cost_tax_factors.get(
                            path, self.cost_tax_factors.get(prefix, (1.0, 0.0))
                        )
                        val = (
                            self._cost_raw(inst, comp, part=leaf) * _YEAR_SECONDS
                            if is_annual
                            else self._cost_raw(inst, comp, start_step, end_step, part=leaf)
                        )
                        base += -(gross_factor - credit_factor) * val
        for item in rule.deductible_maintenance.items:
            if item.year == year and item.deductible:
                base += -self._maintenance_net(item.year, item.position)
        construction = self.evaluation.construction_period
        for index, entry in enumerate(rule.depreciation):
            first = construction + entry.first_year
            if first <= year < first + entry.life_years:
                base += -self._depreciation_amount(rule_id, index, entry, year)
        for movement_id in rule.movement_revenue:
            for at in cash_movement_occurrences(self.finance.cash_movements[movement_id]):
                occ_year = max(1, at.period) if at.axis == "financial_year" else (at.period + 11) // 12
                if occ_year == year:
                    base += self._movement_values[(movement_id, at.axis, at.period)]["net_revenue"]
        for movement_id in rule.movement_cost:
            for at in cash_movement_occurrences(self.finance.cash_movements[movement_id]):
                occ_year = max(1, at.period) if at.axis == "financial_year" else (at.period + 11) // 12
                if occ_year == year:
                    base += -self._movement_values[(movement_id, at.axis, at.period)]["net_basis"]
        for derived_id in rule.derived_tax_cost:
            axis = self.finance.tax_rules[derived_id].axis
            periods = (year,) if axis == "financial_year" else range(12 * (year - 1) + 1, 12 * year + 1)
            for p in periods:
                base += -float(self._financial_values[("tax_rule", derived_id, axis, p, "assessed")])
        return base

    def _ensure_income_until(self, rule_id: str, target_year: int) -> None:
        built = self._income_built.get(rule_id, 0)
        if built >= target_year:
            return
        rule = self.finance.tax_rules[rule_id]
        assert isinstance(rule, IncomeTaxRule)
        if rule_id not in self._income_state:
            opening = [
                (
                    f"v{pos}",
                    LossBalance(
                        amount=_to_decimal(float(ol.amount.value), f"income.{rule_id}.opening_losses[{pos}]"),
                        last_usable_year=int(ol.last_usable_year),
                    ),
                )
                for pos, ol in enumerate(rule.opening_losses)
            ]
            self._income_state[rule_id] = (opening, len(opening))

        rate_dec = Decimal(str(rule.rate.value))
        carry_years = int(rule.loss_carryforward_years)
        for year in range(built + 1, target_year + 1):
            active, seq_next = self._income_state[rule_id]
            base_key = ("tax_rule", rule_id, "financial_year", year, "taxable_base")
            if base_key not in self._financial_values:
                self._financial_values[base_key] = self._income_taxable_base(rule_id, rule, year)
            base_val = float(self._financial_values[base_key])
            base_dec = _to_decimal(base_val, f"income.{rule_id}.y{year}.base")
            loss_res = use_losses(
                taxable_base=base_dec,
                balances=tuple(bal for _, bal in active),
                year=year,
                carryforward_years=carry_years,
            )
            assessed = income_tax_payable(
                taxable_base=base_dec, loss_used=loss_res.used_total, rate=rate_dec
            )
            next_active: list[tuple[str, LossBalance]] = []
            for idx, (vid, bal) in enumerate(active):
                if bal.last_usable_year >= year:
                    next_active.append(
                        (
                            vid,
                            LossBalance(
                                amount=bal.amount - loss_res.uses[idx],
                                last_usable_year=bal.last_usable_year,
                            ),
                        )
                    )
            base_lo = self._income_base_lower_bound(rule_id, rule, year)
            if loss_res.generated > Decimal("0") or base_lo < -1e-12:
                next_active.append(
                    (
                        f"v{seq_next}",
                        LossBalance(amount=loss_res.generated, last_usable_year=year + carry_years),
                    )
                )
                seq_next += 1
            self._income_state[rule_id] = (next_active, seq_next)
            self._year_cash[year] = self._year_cash.get(year, 0.0) - float(assessed)
            assessed_str = _fmt_decimal(assessed, f"income.{rule_id}.y{year}.assessed")
            self._fact_income.append(
                {
                    "rule": rule_id,
                    "year": year,
                    "taxable_base": _fmt_decimal(base_dec, f"income.{rule_id}.y{year}.base"),
                    "loss_used": _fmt_decimal(loss_res.used_total, f"income.{rule_id}.y{year}.used"),
                    "loss_generated": _fmt_decimal(loss_res.generated, f"income.{rule_id}.y{year}.gen"),
                    "loss_expired": _fmt_decimal(loss_res.write_off_total, f"income.{rule_id}.y{year}.exp"),
                    "assessed": assessed_str,
                    "tax": assessed_str,
                }
            )
            for vid, bal in next_active:
                self._fact_balances.append(
                    {
                        "rule": rule_id,
                        "vintage_id": vid,
                        "year": year,
                        "last_usable_year": bal.last_usable_year,
                        "amount": _fmt_decimal(bal.amount, f"loss_balances.{rule_id}.{vid}.y{year}"),
                    }
                )
            self._financial_values[("tax_rule", rule_id, "financial_year", year, "loss_used")] = float(
                loss_res.used_total
            )
            self._financial_values[("tax_rule", rule_id, "financial_year", year, "assessed")] = float(
                assessed
            )
            self._financial_values[("tax_rule", rule_id, "financial_year", year, "paid")] = float(
                assessed
            )
            self._income_built[rule_id] = year

    def _eval_tax_rule(self, identity: str, axis: str, period: int, measure: str) -> float:
        rule = self.finance.tax_rules[identity]
        if isinstance(rule, (VatRule, ConstructionVatRule)):
            return self._vat_rule_quantity(identity, period, measure)
        if isinstance(rule, IncomeTaxRule):
            if measure == "taxable_base":
                return self._income_taxable_base(identity, rule, period)
            self._ensure_income_until(identity, period)
            return float(self._financial_values[("tax_rule", identity, axis, period, measure)])
        assert isinstance(rule, DerivedTaxRule)
        if measure == "paid":
            source_period = period - rule.payment_lag
            if source_period < 1:
                return 0.0
            return float(self._financial_values[("tax_rule", identity, axis, source_period, "assessed")])
        assessed = (
            self._amount_reference(rule.base_from, axis, period) * float(rule.rate.value)
            if rule.rate.value
            else 0.0
        )
        payment_period = period + rule.payment_lag
        limit = self.evaluation.evaluation_years * (12 if axis == "tax_month" else 1)
        if payment_period <= limit:
            year = payment_period if axis == "financial_year" else (payment_period + 11) // 12
            self._year_cash[year] = self._year_cash.get(year, 0.0) - assessed
        self._fact_derived.append(
            {
                "rule": identity,
                "axis": axis,
                "period": period,
                "assessed": assessed,
                "payment_period": payment_period,
                "paid": assessed,
            }
        )
        return assessed

    def _consumed_amounts(self) -> tuple[str, ...]:
        roots: set[str] = {
            target["id"]
            for constraint in ((self.doc.get("planning") or {}).get("constraints") or {}).values()
            if constraint.get("type") == "amount_relation" and constraint.get("enabled", True)
            for target in constraint["references"].values()
            if target["kind"] == "amount"
        }
        roots.update(
            cash.amount.id
            for cash in self.finance.net_cashflows.values()
            if cash.amount.kind == "amount_ref"
        )
        roots.update(movement.amount.id for movement in self.finance.cash_movements.values())
        roots.update(
            asset.basis_from.id
            for asset in self.finance.assets.values()
            if asset.basis_from.kind == "amount"
        )
        return amount_dependency_closure(self.finance.amounts, roots)

    def _run_financial_graph(self) -> None:
        order, unresolved = _financial_dependency_order(self.doc, self.finance, self.evaluation)
        if unresolved:
            raise ValueError(f"财务依赖图存在未解循环: {unresolved!r}")
        for key in order:
            if key in self._financial_values:
                continue
            kind, identity, axis, period, measure = key
            if kind == "amount":
                value = self._named_amount(identity, period)
            elif kind == "movement":
                value = self._eval_movement(identity, axis, period)
            elif kind == "asset":
                value = self._eval_asset(identity, period, measure)
            elif kind == "pool":
                value = self._eval_pool(identity, period, measure)
            else:
                value = self._eval_tax_rule(identity, axis, period, measure)
            self._financial_values[key] = value

        max_months = 12 * self.evaluation.evaluation_years
        for rule_id, rule in sorted(self.finance.tax_rules.items()):
            if isinstance(rule, (VatRule, ConstructionVatRule)):
                for month in range(1, max_months + 1):
                    out_key = ("tax_rule", rule_id, "tax_month", month, "output_tax")
                    in_key = ("tax_rule", rule_id, "tax_month", month, "deductible_input")
                    if out_key not in self._financial_values:
                        self._financial_values[out_key] = self._vat_rule_quantity(
                            rule_id, month, "output_tax"
                        )
                    if in_key not in self._financial_values:
                        self._financial_values[in_key] = self._vat_rule_quantity(
                            rule_id, month, "deductible_input"
                        )
                    self._fact_vat_rules.append(
                        {
                            "rule": rule_id,
                            "pool": rule.credit_pool,
                            "month": month,
                            "output_tax": self._financial_values[out_key],
                            "deductible_input": self._financial_values[in_key],
                        }
                    )
        for pool_id in sorted(self.finance.credit_pools):
            self._ensure_vat_pool_until(pool_id, max_months)
        for rule_id, rule in sorted(self.finance.tax_rules.items()):
            if isinstance(rule, IncomeTaxRule):
                self._ensure_income_until(rule_id, self.evaluation.evaluation_years)

        self._fact_assets = [
            {
                "id": identity,
                "basis": _fmt_float(
                    float(self._financial_values[("asset", identity, "scalar", 0, "basis")]),
                    f"assets.{identity}.basis",
                ),
                "capitalized_at": asset.capitalized_at.to_dict(),
                "available_from": asset.available_from,
                "book_values": [
                    {
                        "year": year,
                        "value": _fmt_float(
                            float(
                                self._financial_values[
                                    ("asset", identity, "financial_year", year, "book_value")
                                ]
                            ),
                            f"assets.{identity}.book_values[{year}]",
                        ),
                    }
                    for year in range(1, self.evaluation.evaluation_years + 1)
                ],
            }
            for identity, asset in sorted(self.finance.assets.items())
        ]
        self._fact_movements.sort(key=lambda row: (row["id"], row["axis"], row["period"]))
        self._fact_derived.sort(key=lambda row: (row["rule"], row["period"]))
        self._fact_vat.sort(key=lambda row: (row["pool"], row["month"]))
        self._fact_income.sort(key=lambda row: (row["rule"], row["year"]))
        self._fact_depr.sort(
            key=lambda row: (row["rule"], row["year"], json.dumps(row["base_from"], sort_keys=True))
        )
        self._fact_balances.sort(key=lambda row: (row["rule"], row["vintage_id"], row["year"]))

    def _build_cost_items(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        unit_year_scale = _to_internal(1.0, f"{self.finance.currency}/a")
        for (inst, component), spec in sorted(self.cost_specs.items()):
            comp_def = getattr(self.finance.finance_types[spec.finance_type], component)
            annual = component == "annual_fixed_om"
            unit = f"{self.finance.currency}/a" if annual else self.finance.currency
            scale = unit_year_scale if annual else 1.0
            spans = (
                ((0, 0, self.point_count),)
                if component != "period_variable_om"
                else tuple(
                    (self.evaluation.construction_period + idx + 1, s, e)
                    for idx, (s, e) in enumerate(self.operating_spans)
                )
            )
            axis = "financial_year" if component == "period_variable_om" else "scalar"
            for item_id in comp_def.items:
                part = f"items.{item_id}"
                for period, s, e in spans:
                    raw = self._cost_raw(inst, component, s, e, part=part)
                    gross, net = self._cost_paid_net(inst, component, s, e, part=part, annual_amount=False)
                    credit = gross - net
                    rows.append(
                        {
                            "instance_id": inst,
                            "finance_type": spec.finance_type,
                            "component": component,
                            "item_id": item_id,
                            "axis": axis,
                            "period": period,
                            "unit": unit,
                            "raw": _fmt_float(raw / scale, "cost_items.raw"),
                            "gross": _fmt_float(gross / scale, "cost_items.gross"),
                            "deductible_input": _fmt_float(credit / scale, "cost_items.deductible_input"),
                            "net_basis": _fmt_float(net / scale, "cost_items.net_basis"),
                        }
                    )
        return sorted(
            rows, key=lambda row: (row["instance_id"], row["component"], row["item_id"], row["period"])
        )

    def evaluate(self) -> dict[str, Any]:
        self._build_monthly_demand()
        self._build_maintenance()
        self._run_financial_graph()

        construction_total = sum(
            self._cost_paid_net(inst, "upfront_capex")[0]
            for (inst, comp) in sorted(self.cost_specs)
            if comp == "upfront_capex" and self.devices[inst].get("asset_origin") == "new"
        )
        payments_applied: list[dict[str, Any]] = []
        for payment in self.evaluation.construction_payments:
            amount = construction_total * float(payment.ratio)
            self._year_cash[payment.t] = self._year_cash.get(payment.t, 0.0) - amount
            payments_applied.append(
                {
                    "t": payment.t,
                    "ratio": payment.ratio,
                    "amount": _fmt_float(amount, "payments_applied"),
                }
            )

        annual_fixed_om_total = sum(
            self._cost_paid_net(inst, "annual_fixed_om")[0]
            for (inst, comp) in sorted(self.cost_specs)
            if comp == "annual_fixed_om"
        )
        annual_operation: list[dict[str, Any]] = []
        for year_no in sorted(self.span_by_year):
            start, end = self.span_by_year[year_no]
            energy = 0.0
            for binding_id, binding in sorted((self.doc.get("tariff_bindings") or {}).items()):
                energy += -self.price_markups.get(binding["price"], 1.0) * self._tariff_value(
                    binding_id, start, end
                )
            variable_neg = 0.0
            for (inst, comp) in sorted(self.cost_specs):
                if comp == "period_variable_om":
                    variable_neg += -self._cost_paid_net(inst, comp, start, end)[0]
            fixed_neg = -annual_fixed_om_total
            demand = 0.0
            for row in self._demand_rows:
                if row["financial_year"] == year_no:
                    markup = self.demand_tax_factors.get(row["charge_id"], (1.0, 0.0))[0]
                    demand += row["base_charge"] * markup
            maintenance = self._maintenance_annual.get(year_no, 0.0)
            net = energy + variable_neg + fixed_neg - demand - maintenance
            self._year_cash[year_no] = self._year_cash.get(year_no, 0.0) + net
            annual_operation.append(
                {
                    "financial_year": year_no,
                    "energy_net": _fmt_float(energy, "annual_operation.energy_net"),
                    "variable_om": _fmt_float(-variable_neg, "annual_operation.variable_om"),
                    "fixed_om": _fmt_float(-fixed_neg, "annual_operation.fixed_om"),
                    "maintenance": _fmt_float(maintenance, "annual_operation.maintenance"),
                    "demand_charge": _fmt_float(demand, "annual_operation.demand_charge"),
                    "net": _fmt_float(net, "annual_operation.net"),
                }
            )

        net_cashflows: list[dict[str, Any]] = []
        for cash_id, entry in sorted(self.finance.net_cashflows.items()):
            t = (
                self.evaluation.evaluation_years
                if entry.at.kind == "evaluation_end"
                else entry.at.year
            )
            if entry.amount.kind == "fixed":
                amount = float(entry.amount.value.value)
            elif entry.amount.kind == "amount_ref":
                amount = self._named_amount(entry.amount.id, 0)
            else:
                amount = self._source_net(entry.amount.base_from) * float(entry.amount.ratio.value)
            sign = 1.0 if entry.direction == "inflow" else -1.0
            self._year_cash[t] = self._year_cash.get(t, 0.0) + amount * sign
            net_cashflows.append(
                {
                    "id": cash_id,
                    "t": t,
                    "direction": entry.direction,
                    "amount": _fmt_float(amount, "net_cashflows.amount"),
                }
            )
        net_cashflows.sort(key=lambda row: (row["t"], row["id"]))

        monthly_demand = [
            {
                "billing_id": row["billing_id"],
                "charge_id": row["charge_id"],
                "calendar_year": row["calendar_year"],
                "calendar_month": row["calendar_month"],
                "financial_year": row["financial_year"],
                "peak_power": _fmt_float(row["peak"] * 0.001, "monthly_demand.peak_power"),
                "peak_unit": "kW",
                "rate": dict(row["rate"]),
                "charge": _fmt_float(
                    row["base_charge"] * self.demand_tax_factors.get(row["charge_id"], (1.0, 0.0))[0],
                    "monthly_demand.charge",
                ),
            }
            for row in self._demand_rows
        ]

        declared_or_units = (
            [self.unit_by_id[uid] for uid in sorted(self.declared_units)]
            if self.declared_units
            else sorted(self.unit_by_id.values(), key=lambda u: u["unit_id"])
        )
        construction_units = [
            {
                "unit_id": unit["unit_id"],
                "instance_id": unit["instance_id"],
                "mode": unit["mode"],
                "built": bool(unit["built"]),
                "scales": [
                    {
                        "planning_variable_id": scale["planning_variable_id"],
                        "value": float(scale["value"]),
                        "unit": str(scale["unit"]),
                    }
                    for scale in sorted(unit.get("scales") or (), key=lambda s: s["planning_variable_id"])
                ],
            }
            for unit in declared_or_units
        ]

        consumed = self._consumed_amounts()
        amounts = []
        for amount_id, definition in sorted(self.finance.amounts.items()):
            if amount_id not in consumed:
                continue
            unit_scale = _to_internal(1.0, definition.unit)
            amounts.append(
                {
                    "id": amount_id,
                    "axis": definition.axis,
                    "unit": definition.unit,
                    "values": [
                        {
                            "period": period,
                            "value": _fmt_float(val / unit_scale, f"amounts.{amount_id}"),
                        }
                        for (identity, period), val in sorted(self._named_amounts.items())
                        if identity == amount_id
                    ],
                }
            )

        cash_movements = [
            {
                "id": row["id"],
                "axis": row["axis"],
                "period": row["period"],
                "direction": row["direction"],
                "price_basis": row["price_basis"],
                **{
                    field: _fmt_float(row[field], f"cash_movements.{row['id']}.{field}")
                    for field in (
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
                        "at": dict(payment["at"]),
                        "ratio": payment["ratio"],
                        "amount": _fmt_float(
                            payment["amount"], f"cash_movements.{row['id']}.payments.amount"
                        ),
                    }
                    for payment in row["payments"]
                ],
            }
            for row in self._fact_movements
        ]

        tax_cash = {
            "vat": self._fact_vat,
            "vat_rules": [
                {
                    "rule": row["rule"],
                    "pool": row["pool"],
                    "month": row["month"],
                    "output_tax": _fmt_float(row["output_tax"], "tax_cash.vat_rules.output_tax"),
                    "deductible_input": _fmt_float(
                        row["deductible_input"], "tax_cash.vat_rules.deductible_input"
                    ),
                }
                for row in self._fact_vat_rules
            ],
            "refunds": [
                {
                    "pool": row["pool"],
                    "period": row["period"],
                    "arrival_period": row["arrival_period"],
                    "amount": _fmt_float(row["amount"], "tax_cash.refunds"),
                }
                for row in self._fact_refunds
            ],
            "income": self._fact_income,
            "depreciation": self._fact_depr,
            "loss_balances": self._fact_balances,
            "derived": [
                {
                    "rule": row["rule"],
                    "axis": row["axis"],
                    "period": row["period"],
                    "assessed": _fmt_float(row["assessed"], "tax_cash.derived.assessed"),
                    "payment_period": row["payment_period"],
                    "paid": _fmt_float(row["paid"], "tax_cash.derived.paid"),
                }
                for row in self._fact_derived
            ],
        }

        years = self.evaluation.evaluation_years
        annual_net_flows = [
            _fmt_float(self._year_cash.get(t, 0.0), "annual_net_flows") for t in range(years + 1)
        ]

        comparison = self.evaluation.comparison
        comp_out = comparison.to_dict() if comparison.mode != "absolute" else {"mode": "absolute"}
        if comparison.mode == "difference":
            assert comparison.baseline_net_flows is not None
            eval_flows = [
                _fmt_float(
                    self._year_cash.get(t, 0.0) - float(comparison.baseline_net_flows[t]),
                    "comparison.evaluation_net_flows",
                )
                for t in range(years + 1)
            ]
            comp_out["evaluation_net_flows"] = eval_flows
        else:
            eval_flows = annual_net_flows

        assessed = evaluate_project_investment(
            [Decimal(v) for v in eval_flows],
            self.evaluation.discount_rate,
            years,
            currency=self.finance.currency,
            price_basis=self.finance.price_basis,
            base_year=self.finance.base_year,
        )

        return {
            "schema": FINANCIAL_EVALUATION_SCHEMA,
            "schema_version": FINANCIAL_EVALUATION_VERSION,
            "status": "evaluated",
            "evaluation_years": years,
            "construction_period": self.evaluation.construction_period,
            "operation_years": self.evaluation.operation_years,
            "currency": self.finance.currency,
            "price_basis": self.finance.price_basis,
            "base_year": self.finance.base_year,
            "profile_id": self.finance.profile_id,
            "discount_rate": self.evaluation.discount_rate,
            "construction_total": _fmt_float(construction_total, "construction_total"),
            "construction_total_unit": self.finance.currency,
            "annual_fixed_om": _fmt_float(annual_fixed_om_total, "annual_fixed_om"),
            "payments_applied": payments_applied,
            "net_cashflows": net_cashflows,
            "cost_items": self._build_cost_items(),
            "amounts": amounts,
            "cash_movements": cash_movements,
            "assets": self._fact_assets,
            "construction_units": construction_units,
            "annual_operation": annual_operation,
            "monthly_demand": monthly_demand,
            "tax_cash": tax_cash,
            "annual_net_flows": annual_net_flows,
            "comparison": comp_out,
            "evaluation": assessed.to_dict(),
        }


def _collect_prices_si(
    materialized: Any,
    prices: Mapping[str, Any] | Sequence[Any] | None,
) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}

    def ingest_items(source: Any) -> None:
        if source is None:
            return
        if isinstance(source, Mapping):
            for price_id, item in source.items():
                if isinstance(item, Mapping):
                    unit = str(item["unit"])
                    vals = item["values"]
                else:
                    unit = str(item.unit)
                    vals = item.values
                out[str(price_id)] = [_to_internal(float(v), unit) for v in vals]
        else:
            for item in source:
                pid = str(_attr_or_key(item, "price_id"))
                unit = str(_attr_or_key(item, "unit"))
                vals = _attr_or_key(item, "values")
                out[pid] = [_to_internal(float(v), unit) for v in vals]

    if materialized is not None:
        ingest_items(_attr_or_key(materialized, "prices"))
    if prices is not None:
        ingest_items(prices)
    return out


def _model_interface_unit(device_models: Mapping[str, Any], inst: str, iface: str) -> str:
    model = device_models.get(inst)
    if model is None:
        raise KeyError(f"缺少预定义接口 {inst}.{iface} 的设备模型单位定义")
    interfaces = _attr_or_key(model, "interfaces", {})
    if iface not in interfaces:
        raise KeyError(f"设备模型 {inst!r} 缺少接口 {iface!r}")
    return str(_attr_or_key(interfaces[iface], "unit"))


def _collect_predefined_series_si(
    materialized: Any,
    device_series: Mapping[Any, Any] | Sequence[Any] | None,
    device_models: Mapping[str, Any],
) -> dict[tuple[str, str], list[float]]:
    out: dict[tuple[str, str], list[float]] = {}

    def ingest_entry(inst: str, iface: str, unit: str | None, values: Sequence[float]) -> None:
        resolved_unit = unit if unit else _model_interface_unit(device_models, inst, iface)
        out[(inst, iface)] = [_to_internal(float(v), resolved_unit) for v in values]

    def ingest_source(source: Any) -> None:
        if source is None:
            return
        if isinstance(source, Mapping):
            for key, item in source.items():
                if isinstance(key, tuple) and len(key) == 2:
                    inst, iface = str(key[0]), str(key[1])
                elif isinstance(key, str) and "." in key:
                    inst, _, iface = key.partition(".")
                else:
                    inst = str(_attr_or_key(item, "device"))
                    iface = str(_attr_or_key(item, "interface"))
                if isinstance(item, Mapping):
                    unit = item.get("unit")
                    values = item["values"]
                elif hasattr(item, "values"):
                    unit = getattr(item, "unit", None)
                    values = item.values
                else:
                    unit = None
                    values = item
                ingest_entry(inst, iface, str(unit) if unit is not None else None, values)
        else:
            for item in source:
                inst = str(_attr_or_key(item, "device") or _attr_or_key(item, "instance_id"))
                iface = str(_attr_or_key(item, "interface") or _attr_or_key(item, "variable_id"))
                unit = _attr_or_key(item, "unit")
                values = _attr_or_key(item, "values")
                ingest_entry(inst, iface, str(unit) if unit is not None else None, values)

    if materialized is not None:
        ingest_source(_attr_or_key(materialized, "series"))
    if device_series is not None:
        ingest_source(device_series)
    return out


def evaluate_forward_finance(
    solution: Mapping[str, Any],
    *,
    artifact: Any,
    finance: EffectiveFinanceConfig | Mapping[str, Any],
    evaluation: EvaluationInput | Mapping[str, Any],
    materialized: Any = None,
    prices: Mapping[str, Any] | Sequence[Any] | None = None,
    device_series: Mapping[Any, Any] | Sequence[Any] | None = None,
    device_models: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate complete forward cash flows, taxes, demand charges, and KPIs.

    Consumes a solved ``ies.engineering-solution@1.0.0`` payload directly without
    invoking any solver, bundle, or model generator, and returns canonical
    ``ies.financial-evaluation@1.0.0``.
    """
    summary = solution.get("solver_summary")
    if not isinstance(summary, Mapping):
        raise ValueError("solution 缺少 solver_summary")
    if not summary.get("has_feasible_solution") or summary.get("math_status") not in ("solved", "feasible"):
        raise ValueError(
            f"仅支持可行/已求解工程方案开展财务评价: math_status={summary.get('math_status')!r}"
        )

    doc = _coerce_doc(artifact)
    eff_finance = _coerce_finance(finance)
    eval_input = _coerce_evaluation(evaluation)
    models = device_models or {}
    prices_si = _collect_prices_si(materialized, prices)
    predefined_si = _collect_predefined_series_si(materialized, device_series, models)

    evaluator = _ForwardFinanceEvaluator(
        solution=solution,
        doc=doc,
        finance=eff_finance,
        evaluation=eval_input,
        prices_si=prices_si,
        predefined_series_si=predefined_si,
        device_models=models,
    )
    return evaluator.evaluate()


__all__ = [
    "FINANCIAL_EVALUATION_SCHEMA",
    "FINANCIAL_EVALUATION_VERSION",
    "evaluate_forward_finance",
]
