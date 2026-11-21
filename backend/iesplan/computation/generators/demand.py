"""Sufficient, solver-independent conditions for an exact demand epigraph.

This consumes the existing financial dependency graph, not a second financial
evaluator. Uncertified uses retain the exact maximum formulation.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from fractions import Fraction
from typing import Any

from iesplan.assembly import FinancialDependencyGraph
from iesplan.computation.generators.compiler_ir import CompilerAnalysis
from iesplan.core.contracts import SYSTEM_AFTER_TAX_NPV
from iesplan.finance import AmountReference, DerivedTaxRule, EffectiveFinanceConfig, IncomeTaxRule, VatRule


def monotone_demand_meters(
    doc: Mapping[str, Any], finance: EffectiveFinanceConfig,
    analysis: CompilerAnalysis, financial_graph: FinancialDependencyGraph,
) -> frozenset[tuple[str, str]]:
    """Return meters whose optimum necessarily has peak == max(actual flow).

    A unit of base charge costs g in cash, can save at most c in VAT and
    c*k in VAT-paid surcharges, and h*(g-c) in income tax. With the public
    nonnegative discount rate these savings occur no earlier than the charge.
    Thus g-c*(1+k)-h*(g-c) > 0 proves a strictly adverse NPV effect.

    Refund claims and other consumers of affected monetary facts can constrain
    feasibility or reward a higher charge. Those uses are not certified here.
    """
    objective = doc["planning"]["objective"]
    if (objective["sense"] != "maximize"
            or objective["expression"].strip() != SYSTEM_AFTER_TAX_NPV):
        return frozenset()
    bindings = {op.payload.billing_id: op.payload for op in analysis.relations("demand")}
    if not bindings:
        return frozenset()

    def direct_charges(reference: AmountReference) -> set[str]:
        return ({bindings[name].charge_id for name in reference.bindings}
                if reference.kind == "demand" else set())

    # The existing selector DAG already orders amount/movement/asset/tax
    # dependencies. Only its demand leaves need to be attached to fee IDs.
    nodes = {node.key: node for node in financial_graph.nodes}
    affected = {}
    tax_uses = defaultdict(set)
    blocked = set()
    for key in financial_graph.order:
        kind, identity, _axis, _period, measure = key
        charges = set()
        for dependency in nodes[key].dependencies:
            charges.update(affected[dependency])
        if kind == "amount":
            for term in finance.amounts[identity].terms.values():
                if term.coefficient.value:
                    charges.update(direct_charges(term.source))
        elif kind == "asset" and measure == "basis":
            charges.update(direct_charges(finance.assets[identity].basis_from))
        elif kind == "tax_rule":
            rule = finance.tax_rules[identity]
            if isinstance(rule, VatRule) and measure in ("input_tax", "deductible_input"):
                charges.update(entry.charge for entry in rule.demand_input_tax
                               if entry.rate.value and
                               (measure == "input_tax" or entry.deductible_ratio.value))
            elif isinstance(rule, IncomeTaxRule) and measure == "taxable_base":
                charges.update(rule.demand_cost)
            tax_uses[identity].update(charges)
        affected[key] = charges
        if kind in ("amount", "movement", "asset"):
            blocked.update(charges)

    # A direct planning expression can consume the billing total without a
    # named amount. Use the collected actual symbol uses (not text matching).
    constraints = {op.identity for op in analysis.relations("planning_scalar")}
    for op in analysis.relations("demand"):
        if any(analysis.uses.get(symbol, frozenset()) & constraints for symbol in op.output_symbols):
            blocked.add(op.payload.charge_id)

    surcharge_rates = defaultdict(Fraction)
    income_rates = {}
    factors = {}
    for identity, rule in finance.tax_rules.items():
        if isinstance(rule, DerivedTaxRule) and tax_uses[identity]:
            source = rule.base_from
            # The public rule already limits tax bases to current/current-year
            # periods and nonnegative payment lags. Only VAT-paid surcharges
            # have the c*k savings bound used by this certificate.
            if source.source.kind == "pool" and source.measure == "paid":
                surcharge_rates[source.source.id] += Fraction(rule.rate.value)
            else:
                blocked.update(tax_uses[identity])
        elif isinstance(rule, IncomeTaxRule):
            for charge in rule.demand_cost:
                income_rates[charge] = Fraction(rule.rate.value)
    for rule in finance.tax_rules.values():
        if not isinstance(rule, VatRule):
            continue
        for entry in rule.demand_input_tax:
            rate = Fraction(entry.rate.value)
            gross = 1 + rate if finance.price_basis == "tax_exclusive" else Fraction(1)
            credit = (rate if finance.price_basis == "tax_exclusive" else rate / (1 + rate))
            credit *= Fraction(entry.deductible_ratio.value)
            factors[entry.charge] = (gross, credit, surcharge_rates[rule.credit_pool])
            # A lower input credit can violate a real carry >= refund claim
            # row, even though its cash effect alone would improve NPV.
            refunds = finance.credit_pools[rule.credit_pool].refunds
            if credit and any(refund.amount.value for refund in refunds):
                blocked.add(entry.charge)

    strict = set()
    meters = defaultdict(set)
    for spec in bindings.values():
        meter = (spec.instance, spec.interface)
        meters[meter].add(spec.charge_id)
        gross, credit, surcharge = factors.get(spec.charge_id, (Fraction(1), Fraction(0), Fraction(0)))
        shield = income_rates.get(spec.charge_id, Fraction(0)) * (gross - credit)
        margin = gross - credit * (1 + surcharge) - shield
        if finance.demand_charges[spec.charge_id].rate.value:
            if margin < 0:
                blocked.add(spec.charge_id)
            elif margin > 0:
                strict.add(meter)
    return frozenset(meter for meter, charges in meters.items()
                     if not charges & blocked and meter in strict)
