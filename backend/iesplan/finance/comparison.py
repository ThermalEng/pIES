"""Evaluation comparison: public input and pure selection of sealed facts.

Only the submission application resolves task permissions and storage objects.
These immutable values cross the existing evaluation boundary; no baseline
model, current configuration, solver or second evaluation engine is involved.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType

from iesplan.core.jsonutil import jsonable
from iesplan.core.units import UnitError, dims_of
from iesplan.finance.triplet import (
    CURRENCIES,
    FinanceTripletError,
    Money,
    TaxReferenceSource,
    parse_amount_reference,
)


class ComparisonError(ValueError):
    def __init__(self, field: str, detail: str):
        self.field = field
        super().__init__(f"{field}: {detail}")


@dataclass(frozen=True, slots=True)
class BaselineFact:
    unit: str
    axis: str
    values: tuple[str | int | float, ...]

    def to_dict(self) -> dict:
        return {"unit": self.unit, "axis": self.axis, "values": list(self.values)}


@dataclass(frozen=True, slots=True)
class BaselineSelector:
    kind: str
    id: str | None = None
    measure: str | None = None
    source: TaxReferenceSource | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            **{
                key: value.to_dict() if isinstance(value, TaxReferenceSource) else value
                for key in ("id", "measure", "source")
                if (value := getattr(self, key)) is not None
            },
        }


@dataclass(frozen=True, slots=True)
class Comparability:
    service_basis: str
    differences: str

    def to_dict(self) -> dict:
        return {"service_basis": self.service_basis, "differences": self.differences}


@dataclass(frozen=True, slots=True)
class TaskResultBaseline:
    task_id: int
    evidence_id: int


@dataclass(frozen=True, slots=True)
class DeclaredBaseline:
    currency: str
    evaluation_years: int
    annual_net_flows: tuple[str, ...] | None
    facts: Mapping[str, BaselineFact]


@dataclass(frozen=True, slots=True)
class ComparisonRequest:
    mode: str
    baseline: TaskResultBaseline | DeclaredBaseline | None = None
    facts: Mapping[str, BaselineSelector] = field(default_factory=lambda: MappingProxyType({}))
    comparability: Comparability | None = None


@dataclass(frozen=True, slots=True)
class FrozenComparison:
    mode: str = "absolute"
    baseline_source: Mapping = field(default_factory=lambda: MappingProxyType({}))
    comparability: Comparability | None = None
    facts: Mapping[str, BaselineFact] = field(default_factory=lambda: MappingProxyType({}))
    baseline_net_flows: tuple[str, ...] | None = None

    def to_dict(self) -> dict:
        if self.mode == "absolute":
            return {"mode": "absolute"}
        out = {
            "mode": self.mode,
            "baseline_source": jsonable(self.baseline_source),
            "comparability": self.comparability.to_dict(),
            "facts": {key: value.to_dict() for key, value in sorted(self.facts.items())},
        }
        if self.mode == "difference":
            out["baseline_net_flows"] = list(self.baseline_net_flows)
        return out

    @classmethod
    def from_frozen(cls, value: Mapping) -> FrozenComparison:
        """Trusted snapshot reconstruction, not a second input validation."""
        if value["mode"] == "absolute":
            return cls()
        return cls(
            mode=value["mode"],
            baseline_source=MappingProxyType(dict(value["baseline_source"])),
            comparability=Comparability(**value["comparability"]),
            facts=MappingProxyType(
                {
                    key: BaselineFact(row["unit"], row["axis"], tuple(row["values"]))
                    for key, row in value["facts"].items()
                }
            ),
            baseline_net_flows=tuple(value["baseline_net_flows"]) if value["mode"] == "difference" else None,
        )


def _shape(raw, keys, path):
    if not isinstance(raw, Mapping) or set(raw) != set(keys):
        raise ComparisonError(path, f"required fields: {sorted(keys)}")


def _identity(value, path):
    try:
        return parse_amount_reference(
            {"kind": "baseline", "id": value, "period": {"kind": "scalar"}}, field=path
        ).id
    except FinanceTripletError as exc:
        raise ComparisonError(path, str(exc)) from exc


def _money(value, unit, path):
    try:
        return format(
            Money.from_dict({"value": value, "unit": unit}, allow_negative=True, field=path).value, "f"
        )
    except FinanceTripletError as exc:
        raise ComparisonError(path, str(exc)) from exc


def _parse_fact(raw, years, currency, path):
    _shape(raw, {"unit", "axis", "values"}, path)
    axis, unit = raw["axis"], raw["unit"]
    if axis not in ("scalar", "financial_year", "tax_month"):
        raise ComparisonError(path + ".axis", "expected scalar/financial_year/tax_month")
    if not isinstance(unit, str) or not unit:
        raise ComparisonError(path + ".unit", "expected explicit unit")
    try:
        dimensions = dims_of(unit)
    except (UnitError, TypeError) as exc:
        raise ComparisonError(path + ".unit", str(exc)) from exc
    if dimensions.get("currency") and not unit.startswith(currency):
        raise ComparisonError(path + ".unit", "currency must match baseline; no exchange")
    size = 1 if axis == "scalar" else years + 1 if axis == "financial_year" else 12 * years
    if not isinstance(raw["values"], list) or len(raw["values"]) != size:
        raise ComparisonError(path + ".values", f"expected {size} values on {axis}")
    values = []
    for index, value in enumerate(raw["values"]):
        sub = f"{path}.values[{index}]"
        if dimensions.get("currency"):
            value = _money(value, unit, sub)
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ComparisonError(sub, "physical values must be finite JSON numbers")
        values.append(value)
    return BaselineFact(unit, axis, tuple(values))


def _parse_selector(raw, *, declared, path):
    if not isinstance(raw, Mapping):
        raise ComparisonError(path, "expected fact selector")
    kind = raw.get("kind")
    fields = (
        {"declared_fact": {"id"}}
        if declared
        else {
            "operation": {"measure"},
            "cashflow": {"measure"},
            "amount": {"id"},
            "movement": {"id", "measure"},
            "asset": {"id", "measure"},
            "tax": {"source", "measure"},
        }
    )
    if not isinstance(kind, str) or kind not in fields:
        raise ComparisonError(path + ".kind", "unsupported baseline selector")
    _shape(raw, {"kind", *fields[kind]}, path)
    measure = raw.get("measure")
    allowed = {
        "operation": {"energy_net", "fixed_om", "variable_om", "maintenance", "demand_charge"},
        "cashflow": {"net"},
        "movement": {"gross", "net_basis", "net_revenue", "paid"},
        "asset": {"basis", "depreciation", "book_value"},
        "tax": {
            "output_tax",
            "deductible_input",
            "paid",
            "refund_confirmed",
            "refund_received",
            "credit_balance",
            "receivable",
            "taxable_base",
            "loss_used",
            "assessed",
        },
    }
    if kind in allowed and measure not in allowed[kind]:
        raise ComparisonError(path + ".measure", "unsupported measure")
    source = None
    if kind == "tax":
        try:
            source = parse_amount_reference({**raw, "period": {"kind": "scalar"}}, field=path).source
        except FinanceTripletError as exc:
            raise ComparisonError(path + ".source", str(exc)) from exc
    return BaselineSelector(
        kind, _identity(raw["id"], path + ".id") if "id" in raw else None, measure, source
    )


def parse_comparison(raw, *, years: int) -> ComparisonRequest:
    """Parse only the public comparison input (omission is absolute)."""
    path = "evaluation.comparison"
    if not isinstance(raw, Mapping) or raw.get("mode") not in ("absolute", "reference", "difference"):
        raise ComparisonError(path + ".mode", "expected absolute/reference/difference")
    mode = raw["mode"]
    if mode == "absolute":
        _shape(raw, {"mode"}, path)
        return ComparisonRequest(mode)
    _shape(raw, {"mode", "baseline", "facts", "comparability"}, path)
    _shape(raw["comparability"], {"service_basis", "differences"}, path + ".comparability")
    for key, value in raw["comparability"].items():
        if not isinstance(value, str) or not value.strip():
            raise ComparisonError(path + ".comparability." + key, "expected nonempty confirmation")
    baseline, sub = raw["baseline"], path + ".baseline"
    if not isinstance(baseline, Mapping):
        raise ComparisonError(sub, "expected baseline source")
    if baseline.get("kind") == "task_result":
        _shape(baseline, {"kind", "task_id", "evidence_id"}, sub)
        for key in ("task_id", "evidence_id"):
            if type(baseline[key]) is not int or baseline[key] < 1:
                raise ComparisonError(sub + "." + key, "expected positive public ID")
        source = TaskResultBaseline(baseline["task_id"], baseline["evidence_id"])
    elif baseline.get("kind") == "declared":
        _shape(baseline, {"kind", "currency", "evaluation_years", "annual_net_flows", "facts"}, sub)
        currency = baseline["currency"]
        if not isinstance(currency, str) or currency not in CURRENCIES:
            raise ComparisonError(sub + ".currency", "unsupported currency")
        if type(baseline["evaluation_years"]) is not int or baseline["evaluation_years"] != years:
            raise ComparisonError(sub + ".evaluation_years", "must match candidate evaluation axis")
        flows = baseline["annual_net_flows"]
        if mode == "reference":
            if flows is not None:
                raise ComparisonError(
                    sub + ".annual_net_flows", "reference requires null, not invented cashflows"
                )
        else:
            if not isinstance(flows, list) or len(flows) != years + 1:
                raise ComparisonError(sub + ".annual_net_flows", f"expected {years + 1} signed amounts")
            flows = tuple(_money(v, currency, f"{sub}.annual_net_flows[{i}]") for i, v in enumerate(flows))
        if not isinstance(baseline["facts"], Mapping):
            raise ComparisonError(sub + ".facts", "expected declared fact mapping")
        facts = {
            _identity(key, sub + ".facts"): _parse_fact(value, years, currency, sub + ".facts." + key)
            for key, value in baseline["facts"].items()
        }
        source = DeclaredBaseline(currency, years, flows, MappingProxyType(facts))
    else:
        raise ComparisonError(sub + ".kind", "expected task_result/declared")
    if not isinstance(raw["facts"], Mapping):
        raise ComparisonError(path + ".facts", "expected alias mapping")
    selectors = {
        _identity(key, path + ".facts"): _parse_selector(
            value, declared=isinstance(source, DeclaredBaseline), path=path + ".facts." + key
        )
        for key, value in raw["facts"].items()
    }
    return ComparisonRequest(
        mode, source, MappingProxyType(dict(sorted(selectors.items()))), Comparability(**raw["comparability"])
    )


def _select_fact(fin, selector, years, path):
    """Project published identities, never arbitrary paths or current rules."""
    currency = fin["currency"]
    kind, identity, measure = selector.kind, selector.id, selector.measure

    def indexed(rows, index, key, axis, *, complete=False):
        first, last = (1, 12 * years) if axis == "tax_month" else (0, years)
        values = {period: "0" for period in range(first, last + 1)}
        seen = set()
        for row in rows:
            period = row[index]
            value = row[key]
            seen.add(period)
            if first <= period <= last:
                values[period] = str(Decimal(values[period]) + Decimal(value))
        expected = set(range(1, last + 1))
        if complete and not expected <= seen:
            raise ComparisonError(path, f"missing published periods: {sorted(expected - seen)}")
        return BaselineFact(currency, axis, tuple(values.values()))

    def matched(rows, key, value):
        result = [row for row in rows if row[key] == value]
        if not result:
            raise ComparisonError(path, f"missing published identity: {selector.to_dict()}")
        return result

    try:
        if kind == "cashflow":
            return BaselineFact(currency, "financial_year", tuple(fin["annual_net_flows"]))
        if kind == "operation":
            rows = fin["annual_operation"]
            expected = set(range(fin["construction_period"] + 1, years + 1))
            if not expected <= {row["financial_year"] for row in rows}:
                raise ComparisonError(path, "missing operating financial years")
            return indexed(rows, "financial_year", measure, "financial_year")
        if kind == "amount":
            (row,) = matched(fin["amounts"], "id", identity)
            if row["axis"] == "scalar":
                return BaselineFact(row["unit"], "scalar", (row["values"][0]["value"],))
            fact = indexed(row["values"], "period", "value", row["axis"], complete=True)
            return BaselineFact(row["unit"], fact.axis, fact.values)
        if kind == "movement":
            rows = matched(fin["cash_movements"], "id", identity)
            if measure in ("net_basis", "net_revenue"):
                direction = "outflow" if measure == "net_basis" else "inflow"
                if rows[0]["direction"] != direction:
                    raise ComparisonError(path, f"{measure} incompatible with movement direction")
            axis = rows[0]["axis"]
            if measure == "paid":
                key = "month" if axis == "tax_month" else "year"
                payments = [
                    {"period": p["at"][key], "value": p["amount"]} for row in rows for p in row["payments"]
                ]
                return indexed(payments, "period", "value", axis)
            return indexed(rows, "period", measure, axis)
        if kind == "asset":
            (row,) = matched(fin["assets"], "id", identity)
            if measure == "basis":
                return BaselineFact(currency, "scalar", (row["basis"],))
            if measure == "book_value":
                return indexed(row["book_values"], "year", "value", "financial_year", complete=True)
            rows = [
                row
                for row in fin["tax_cash"]["depreciation"]
                if row["base_from"] == {"kind": "asset", "id": identity}
            ]
            if not rows:
                raise ComparisonError(path, "asset depreciation not published")
            return indexed(rows, "year", "amount", "financial_year")
        tax = fin["tax_cash"]
        identity = selector.source.id
        if selector.source.kind == "pool":
            fields = {
                "paid": "paid",
                "refund_confirmed": "refund_claimed",
                "refund_received": "refund_arrived",
                "credit_balance": "carry",
                "receivable": "receivable",
            }
            rows = matched(tax["vat"], "pool", identity)
            if measure not in fields:
                raise ComparisonError(path, "measure incompatible with VAT pool")
            return indexed(rows, "month", fields[measure], "tax_month", complete=True)
        for directory, allowed in (
            ("vat_rules", {"output_tax", "deductible_input"}),
            ("income", {"taxable_base", "loss_used", "assessed", "paid"}),
            ("derived", {"assessed", "paid"}),
        ):
            rows = [row for row in tax[directory] if row["rule"] == identity]
            if not rows:
                continue
            if measure not in allowed:
                raise ComparisonError(path, "measure incompatible with published rule kind")
            if directory == "vat_rules":
                return indexed(rows, "month", measure, "tax_month", complete=True)
            if directory == "income":
                return indexed(
                    rows, "year", "tax" if measure == "paid" else measure, "financial_year", complete=True
                )
            return indexed(
                rows,
                "payment_period" if measure == "paid" else "period",
                measure,
                rows[0]["axis"],
                complete=measure == "assessed",
            )
        raise ComparisonError(path, f"tax rule not published: {identity}")
    except KeyError as exc:
        raise ComparisonError(path, f"selector {selector.to_dict()} missing published field {exc}") from exc


def freeze_comparison(
    request: ComparisonRequest,
    *,
    years: int,
    currency: str,
    financial: Mapping | None = None,
    source: Mapping | None = None,
) -> FrozenComparison:
    """Resolve only explicitly selected business facts at submission once."""
    if request.mode == "absolute":
        return FrozenComparison()
    baseline = request.baseline
    if isinstance(baseline, DeclaredBaseline):
        actual_currency, actual_years = baseline.currency, baseline.evaluation_years
        flows = baseline.annual_net_flows
        origin = {"kind": "declared"}
    else:
        actual_currency, actual_years = financial["currency"], financial["evaluation_years"]
        flows = financial.get("annual_net_flows") if request.mode == "difference" else None
        origin = dict(source)
    path = "evaluation.comparison"
    if actual_currency != currency:
        raise ComparisonError(path + ".baseline.currency", "currency must match candidate; no exchange")
    if actual_years != years:
        raise ComparisonError(path + ".baseline.evaluation_years", "evaluation axis must match candidate")
    if request.mode == "difference" and (flows is None or len(flows) != years + 1):
        raise ComparisonError(path + ".baseline.annual_net_flows", "complete sealed cashflows required")
    facts = {}
    for alias, selector in request.facts.items():
        field_path = path + ".facts." + alias
        if isinstance(baseline, DeclaredBaseline):
            if selector.id not in baseline.facts:
                raise ComparisonError(field_path, f"declared fact missing: {selector.id}")
            facts[alias] = baseline.facts[selector.id]
        else:
            facts[alias] = _select_fact(financial, selector, years, field_path)
    origin["selectors"] = MappingProxyType(
        {
            alias: MappingProxyType(
                {
                    key: MappingProxyType(value) if isinstance(value, dict) else value
                    for key, value in selector.to_dict().items()
                }
            )
            for alias, selector in request.facts.items()
        }
    )
    return FrozenComparison(
        request.mode,
        MappingProxyType(origin),
        request.comparability,
        MappingProxyType(facts),
        tuple(flows) if flows is not None else None,
    )


def comparison_as_declared(frozen: Mapping, *, years: int, currency: str) -> dict:
    """New-project input from frozen values, without forged task identities."""
    if frozen["mode"] == "absolute":
        return {"mode": "absolute"}
    return {
        "mode": frozen["mode"],
        "comparability": dict(frozen["comparability"]),
        "baseline": {
            "kind": "declared",
            "currency": currency,
            "evaluation_years": years,
            "annual_net_flows": list(frozen["baseline_net_flows"])
            if frozen["mode"] == "difference"
            else None,
            "facts": dict(frozen["facts"]),
        },
        "facts": {alias: {"kind": "declared_fact", "id": alias} for alias in frozen["facts"]},
    }
