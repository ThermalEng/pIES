"""Pure forward financial evaluation over resolved engineering solutions.

Consumes already-restored business-unit facts (``ies.engineering-solution@1.0.0``)
alongside frozen assembly, finance, evaluation confirmation, materialized
prices/series, and device metadata to produce ``ies.financial-evaluation@1.0.0``
without importing any computation/modeling/domain-behavior package.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
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
    ConstructionVatRule,
    DerivedTaxRule,
    EffectiveFinanceConfig,
    IncomeTaxRule,
    VatRule,
    amount_dependency_closure,
    cash_movement_occurrences,
    cost_target_leaves,
    expand_amount_period,
)

FINANCIAL_EVALUATION_SCHEMA = "ies.financial-evaluation"
FINANCIAL_EVALUATION_VERSION = "1.0.0"

_COMPONENTS_NEW: tuple[str, ...] = ("upfront_capex", "annual_fixed_om", "period_variable_om")
_COMPONENTS_EXISTING: tuple[str, ...] = ("period_variable_om",)
_DEFAULT_MONTH_SPAN = (0, 0)


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
        return EffectiveFinanceConfig.from_dict(finance)
    raise TypeError(f"finance 须为 EffectiveFinanceConfig 或映射，得到 {type(finance).__name__}")


def _coerce_evaluation(evaluation: EvaluationInput | Mapping[str, Any]) -> EvaluationInput:
    if isinstance(evaluation, EvaluationInput):
        return evaluation
    if isinstance(evaluation, Mapping):
        return EvaluationInput.from_frozen(evaluation)
    raise TypeError(f"evaluation 须为 EvaluationInput 或映射，得到 {type(evaluation).__name__}")


def _attr_or_key(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


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
        self.plan_vars: Mapping[str, Any] = (doc.get("planning") or {}).get("variables") or {}
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

        # Index resolved solution values
        self.unit_by_inst: dict[str, Mapping[str, Any]] = {}
        self.unit_by_id: dict[str, Mapping[str, Any]] = {}
        self.plan_var_si: dict[str, float] = {}
        for unit in solution.get("construction_units") or ():
            self.unit_by_inst[unit["instance_id"]] = unit
            self.unit_by_id[unit["unit_id"]] = unit
            for scale in unit.get("scales") or ():
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

    def _prop_si(self, inst: str, prop: str) -> float:
        bound_var = self.bound_prop.get((inst, prop))
        if bound_var is not None and bound_var in self.plan_var_si:
            return self.plan_var_si[bound_var]
        overrides = (self.devices[inst].get("properties") or {})
        if prop in overrides:
            return _to_internal(float(overrides[prop]["value"]), str(overrides[prop]["unit"]))
        model = self.device_models.get(inst)
        if model is None:
            raise KeyError(f"缺少实例 {inst!r} 的属性 {prop!r} 默认模型定义")
        variables = _attr_or_key(model, "variables", {})
        if prop in variables:
            entry = variables[prop]
            return _to_internal(float(_attr_or_key(entry, "value")), str(_attr_or_key(entry, "unit")))
        props = _attr_or_key(model, "properties", {})
        if prop in props:
            entry = props[prop]
            return _to_internal(float(_attr_or_key(entry, "value")), str(_attr_or_key(entry, "unit")))
        raise KeyError(f"实例 {inst!r} 的属性 {prop!r} 不在装配覆盖或设备模型中")

    def _series_si(self, inst: str, iface: str) -> list[float]:
        key = (inst, iface)
        cached = self._series_si_cache.get(key)
        if cached is not None:
            return cached
        if key in self.predefined_series_si:
            cached = self.predefined_series_si[key]
        elif key in self.solver_series:
            unit, values = self.solver_series[key]
            cached = [_to_internal(float(v), unit) for v in values]
        else:
            raise KeyError(f"缺少实例接口序列 {inst}.{iface}")
        self._series_si_cache[key] = cached
        return cached

    def _system_series_si(self, block: str, variable: str) -> list[float]:
        key = (block, variable)
        cached = self._sys_si_cache.get(key)
        if cached is not None:
            return cached
        if key not in self.sys_series:
            raise KeyError(f"缺少系统序列 system:{block}.{variable}")
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

    def _cost_occurrence_factor(self, inst: str, component: str, leaf: str) -> float:
        conditions = (self.fb_instances.get(inst) or {}).get("cost_occurrence") or {}
        pieces = [component, *leaf.split(".")]
        condition = {"kind": "always"}
        for size in range(1, len(pieces) + 1):
            path = ".".join(pieces[:size])
            if path in conditions:
                condition = conditions[path]
                break
        if condition["kind"] == "always":
            return 1.0
        unit = self.unit_by_inst[inst]
        if condition["unit_ref"] != unit["unit_id"]:
            raise ValueError(
                f"费用发生条件 unit_ref={condition['unit_ref']!r} 与实例建设单元 {unit['unit_id']!r} 不一致"
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

    def _eval_movement(self, identity: str, axis: str, period: int) -> float:
        movement = self.finance.cash_movements[identity]
        raw = self._amount_reference(movement.amount, axis, period)
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
            # deductible_input
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

        # ConstructionVatRule
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
            if loss_res.generated > Decimal("0"):
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
        from iesplan.assembly.financial_relations import financial_dependency_graph

        graph = financial_dependency_graph(self.doc, self.finance, self.evaluation)
        if graph.unresolved:
            raise ValueError(f"财务依赖图存在未解循环: {graph.unresolved!r}")
        for key in graph.order:
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

        # Ensure all VAT rule monthly records and pools/income cover the whole horizon even if no pool exists.
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

        # Construction & payments applied
        construction_total = sum(
            self._cost_paid_net(inst, "upfront_capex")[0]
            for (inst, comp) in sorted(self.cost_specs)
            if comp == "upfront_capex" and self.devices[inst].get("asset_origin") == "new"
        )
        payments_applied: list[dict[str, Any]] = []
        for payment in self.evaluation.construction_payments:
            amount = construction_total * payment.ratio
            self._year_cash[payment.t] = self._year_cash.get(payment.t, 0.0) - amount
            payments_applied.append(
                {
                    "t": payment.t,
                    "ratio": payment.ratio,
                    "amount": _fmt_float(amount, "payments_applied"),
                }
            )

        # Annual operation & operating cash
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

        # Explicit after-tax net cashflows
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

        # Monthly demand rows
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

        # Construction units (exact recovered structure)
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
            for unit in sorted(self.unit_by_id.values(), key=lambda u: u["unit_id"])
        ]

        # Amounts (consumed closure)
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

        # Formatted movements & derived taxes & vat rules
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
