"""Pure, period-specific financial dependencies shared by input and generation.

Keys contain business identities only: (kind, id, axis, period, measure).
Technical quantities and original costs are terminal leaves, not expanded
into another operating model. Building this graph never solves or evaluates
an amount. The public submission boundary owns rejection of cyclic inputs.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from dataclasses import dataclass

from iesplan.finance import (
    ConstructionVatRule,
    DerivedTaxRule,
    IncomeTaxRule,
    VatRule,
    cash_movement_occurrences,
    expand_amount_period,
    parse_amount_period,
)

FinancialNodeKey = tuple[str, str, str, int, str]


@dataclass(frozen=True, slots=True)
class FinancialDependencyEdge:
    dependency: FinancialNodeKey
    field_path: str


@dataclass(frozen=True, slots=True)
class FinancialDependencyNode:
    key: FinancialNodeKey
    edges: tuple[FinancialDependencyEdge, ...]
    field_path: str

    @property
    def dependencies(self) -> tuple[FinancialNodeKey, ...]:
        return tuple(sorted({edge.dependency for edge in self.edges}))


@dataclass(frozen=True, slots=True)
class FinancialDependencyGraph:
    nodes: tuple[FinancialDependencyNode, ...]
    roots: tuple[FinancialNodeKey, ...]
    order: tuple[FinancialNodeKey, ...]

    @property
    def unresolved(self) -> tuple[FinancialNodeKey, ...]:
        done = set(self.order)
        return tuple(node.key for node in self.nodes if node.key not in done)


def financial_dependency_graph(doc, finance, evaluation) -> FinancialDependencyGraph:
    """Return immutable selector nodes and deterministic dependency-first order.

    A cyclic graph has a partial order and nonempty ``unresolved``. No
    diagnostics are emitted here: trusted generation can reuse the same
    decomposition without running a second public-input validation pass.
    """
    years = evaluation.evaluation_years
    occurrences = {
        identity: cash_movement_occurrences(movement) for identity, movement in finance.cash_movements.items()
    }
    roots = set()
    for constraint in doc.get("planning", {}).get("constraints", {}).values():
        if constraint["type"] != "amount_relation" or not constraint.get("enabled", True):
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

    def reference_edges(reference, axis, period, field):
        if reference.kind == "maintenance":
            return []
        selected_axis, selected = expand_amount_period(reference.period, axis, period)
        kind = reference.kind
        if kind in ("amount", "movement", "asset"):
            measure = "value" if kind == "amount" else "raw" if kind == "movement" else reference.measure
            return [
                FinancialDependencyEdge((kind, reference.id, selected_axis, p, measure), field)
                for p in selected
            ]
        if kind == "tax":
            owner = "pool" if reference.source.kind == "pool" else "tax_rule"
            return [
                FinancialDependencyEdge(
                    (owner, reference.source.id, selected_axis, p, reference.measure), field
                )
                for p in selected
            ]
        return []

    def confirm_months(entry, at):
        if entry.confirmation.method == "recognition_period":
            return (at.period if at.axis == "tax_month" else 12 * at.period,)
        schedule = next(item for item in entry.confirmation.occurrences if item.at == at)
        return tuple(item.period for item in schedule.schedule if item.ratio)

    def node_edges(key):
        kind, identity, axis, period, measure = key
        edges = []
        if kind == "amount":
            field = f"effective_finance.amounts.{identity}"
            for term_id, term in finance.amounts[identity].terms.items():
                if term.coefficient.value:
                    edges.extend(
                        reference_edges(term.source, axis, period, f"{field}.terms.{term_id}.source")
                    )
        elif kind == "movement":
            field = f"effective_finance.cash_movements.{identity}.amount"
            edges.extend(reference_edges(finance.cash_movements[identity].amount, axis, period, field))
        elif kind == "asset":
            field = f"effective_finance.assets.{identity}.basis_from"
            if measure == "basis":
                edges.extend(reference_edges(finance.assets[identity].basis_from, "scalar", 0, field))
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
                edges.append(FinancialDependencyEdge(("asset", identity, "scalar", 0, "basis"), field))
        elif kind == "pool":
            field = f"effective_finance.credit_pools.{identity}"
            if measure in ("paid", "credit_balance"):
                if period > 1:
                    edges.append(
                        FinancialDependencyEdge(("pool", identity, axis, period - 1, "credit_balance"), field)
                    )
                for rule_id, rule in finance.tax_rules.items():
                    if isinstance(rule, (VatRule, ConstructionVatRule)) and rule.credit_pool == identity:
                        edges.extend(
                            FinancialDependencyEdge(("tax_rule", rule_id, axis, period, value), field)
                            for value in ("output_tax", "deductible_input")
                        )
        else:
            rule = finance.tax_rules[identity]
            field = f"effective_finance.tax_rules.{identity}"
            if isinstance(rule, DerivedTaxRule):
                if measure == "paid":
                    source_period = period - rule.payment_lag
                    if source_period >= 1:
                        edges.append(
                            FinancialDependencyEdge(
                                ("tax_rule", identity, axis, source_period, "assessed"),
                                field + ".payment_lag",
                            )
                        )
                elif rule.rate.value:
                    edges.extend(reference_edges(rule.base_from, axis, period, field + ".base_from"))
            elif isinstance(rule, VatRule):
                component = "movement_output_tax" if measure == "output_tax" else "movement_input_tax"
                entries = getattr(rule, component)
                for index, entry in enumerate(entries):
                    if not entry.rate.value or (measure != "output_tax" and not entry.deductible_ratio.value):
                        continue
                    for at in occurrences[entry.movement]:
                        if period in confirm_months(entry, at):
                            edges.append(
                                FinancialDependencyEdge(
                                    ("movement", entry.movement, at.axis, at.period, "raw"),
                                    f"{field}.{component}[{index}]",
                                )
                            )
            elif isinstance(rule, IncomeTaxRule):
                if measure != "taxable_base":
                    edges.append(
                        FinancialDependencyEdge(("tax_rule", identity, axis, period, "taxable_base"), field)
                    )
                    # Ordered loss usage depends on prior bases, not on their
                    # tax payments. This retains actual forward loss links.
                    for previous in range(max(1, period - rule.loss_carryforward_years), period):
                        edges.append(
                            FinancialDependencyEdge(
                                ("tax_rule", identity, axis, previous, "taxable_base"),
                                field + ".loss_carryforward_years",
                            )
                        )
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
                                    edges.append(
                                        FinancialDependencyEdge(
                                            ("movement", movement_id, at.axis, at.period, "raw"),
                                            f"{field}.{component}",
                                        )
                                    )
                    for tax_id in rule.derived_tax_cost:
                        tax_axis = finance.tax_rules[tax_id].axis
                        selected = (
                            (period,)
                            if tax_axis == "financial_year"
                            else range(12 * (period - 1) + 1, 12 * period + 1)
                        )
                        edges.extend(
                            FinancialDependencyEdge(
                                ("tax_rule", tax_id, tax_axis, p, "assessed"), field + ".derived_tax_cost"
                            )
                            for p in selected
                        )
                    for index, entry in enumerate(rule.depreciation):
                        start = evaluation.construction_period + entry.first_year
                        if entry.base_from.kind == "asset" and start <= period < start + entry.life_years:
                            edges.append(
                                FinancialDependencyEdge(
                                    ("asset", entry.base_from.id, "scalar", 0, "basis"),
                                    f"{field}.depreciation[{index}].base_from",
                                )
                            )
        return FinancialDependencyNode(
            key, tuple(sorted(set(edges), key=lambda edge: (edge.dependency, edge.field_path))), field
        )

    nodes = {}
    pending = list(sorted(roots, reverse=True))
    while pending:
        key = pending.pop()
        if key in nodes:
            continue
        node = node_edges(key)
        nodes[key] = node
        pending.extend(dependency for dependency in reversed(node.dependencies) if dependency not in nodes)
    remaining = {key: len(node.dependencies) for key, node in nodes.items()}
    consumers = defaultdict(list)
    for key, node in nodes.items():
        for dependency in node.dependencies:
            consumers[dependency].append(key)
    ready = [key for key, count in remaining.items() if count == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        key = heapq.heappop(ready)
        order.append(key)
        for consumer in sorted(consumers[key]):
            remaining[consumer] -= 1
            if remaining[consumer] == 0:
                heapq.heappush(ready, consumer)
    return FinancialDependencyGraph(
        tuple(nodes[key] for key in sorted(nodes)), tuple(sorted(roots)), tuple(order)
    )


__all__ = [
    "FinancialNodeKey",
    "FinancialDependencyEdge",
    "FinancialDependencyNode",
    "FinancialDependencyGraph",
    "financial_dependency_graph",
]
