"""F2 cross-input checks at the real task submission boundary only."""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal

from iesplan.assembly import financial_dependency_graph
from iesplan.finance import (
    IncomeTaxRule,
    VatRule,
    asset_basis_sources,
    cash_movement_occurrences,
    cost_target_leaves,
    expand_amount_period,
    parse_amount_period,
)
from iesplan.tasks.contracts import InvalidRequestError


def check_financial_relation_inputs(evaluation, artifact, finance) -> None:
    """Resolve evaluation periods and shared spending identities once.

    Future contractual payments are deliberately accepted; they remain
    obligations, not period-end cash. No model or amount is solved here.
    """
    doc = json.loads(artifact.canonical_text)
    years, construction = evaluation.evaluation_years, evaluation.construction_period
    occurrences = {
        identity: cash_movement_occurrences(movement) for identity, movement in finance.cash_movements.items()
    }
    maintenance = defaultdict(int)
    for expense in evaluation.maintenance_expenses:
        maintenance[expense.year] += 1

    def reject(field, reason, **params):
        raise InvalidRequestError(
            reason,
            code="TASK-REQ-015",
            params={"reason": reason, **params},
            location={"object_type": "task", "field": field},
        )

    def period_limit(at, field):
        limit = years * (12 if at.axis == "tax_month" else 1)
        if at.period > limit:
            reject(
                field, "financial_occurrence_outside_evaluation", axis=at.axis, period=at.period, limit=limit
            )

    def reference_period_limit(reference, field):
        period = reference.period
        if period is None or period.kind in ("scalar", "current"):
            return
        if period.kind in ("financial_year", "year_months"):
            axis, last = "financial_year", period.year
        elif period.kind == "tax_month":
            axis, last = "tax_month", period.month
        else:
            axis, last = period.axis, period.last
        limit = years * (12 if axis == "tax_month" else 1)
        if last != "current" and last > limit:
            reject(
                field + ".period",
                "financial_reference_outside_evaluation",
                axis=axis,
                period=last,
                limit=limit,
            )

    for asset_id, asset in finance.assets.items():
        reference_period_limit(asset.basis_from, f"effective_finance.assets.{asset_id}.basis_from")
    for movement_id, movement in finance.cash_movements.items():
        field = f"effective_finance.cash_movements.{movement_id}"
        for at in occurrences[movement_id]:
            period_limit(at, field + ".recognition")
            for index, payment in enumerate(movement.payments):
                paid_at = at.period + payment.offset
                if paid_at < (1 if at.axis == "tax_month" else 0):
                    reject(
                        f"{field}.payments[{index}].offset",
                        "financial_payment_before_axis_start",
                        recognition=at.to_dict(),
                        payment_period=paid_at,
                    )

    for rule_id, rule in finance.tax_rules.items():
        if not isinstance(rule, VatRule):
            continue
        for component in ("movement_input_tax", "movement_output_tax"):
            for index, entry in enumerate(getattr(rule, component)):
                field = f"effective_finance.tax_rules.{rule_id}.{component}[{index}].confirmation"
                expected = {(at.axis, at.period) for at in occurrences[entry.movement]}
                confirmation = entry.confirmation
                if confirmation.method == "recognition_period":
                    if ("financial_year", 0) in expected:
                        reject(
                            field,
                            "year_zero_movement_requires_explicit_vat_schedule",
                            movement=entry.movement,
                        )
                    continue
                actual = [(row.at.axis, row.at.period) for row in confirmation.occurrences]
                if set(actual) != expected or len(actual) != len(expected):
                    reject(
                        field + ".occurrences",
                        "movement_vat_occurrence_coverage_mismatch",
                        movement=entry.movement,
                        missing=sorted(expected - set(actual)),
                        extra=sorted(set(actual) - expected),
                        duplicate=len(actual) != len(set(actual)),
                    )
                for occurrence_index, occurrence in enumerate(confirmation.occurrences):
                    for schedule_index, scheduled in enumerate(occurrence.schedule):
                        if scheduled.period > years * 12:
                            reject(
                                f"{field}.occurrences[{occurrence_index}].schedule[{schedule_index}].period",
                                "movement_vat_confirmation_outside_evaluation",
                                period=scheduled.period,
                                limit=years * 12,
                            )

    def source_atoms(reference, field):
        source = reference.to_dict()
        kind = source["kind"]
        if kind in ("cost", "upfront_capex_net"):
            category = source["finance_type"] if kind == "upfront_capex_net" else source["path"].split(".")[0]
            path = f"{category}.upfront_capex" if kind == "upfront_capex_net" else source["path"]
            bindings = doc["finance_binding"]["instances"]
            matching = {
                identity
                for identity, binding in bindings.items()
                if binding["type"] == "costed"
                and binding["finance_type"] == category
                and doc["devices"][identity]["asset_origin"] == "new"
            }
            scope = source["scope"]
            chosen = matching if scope["kind"] == "all_new_instances" else set(scope["instances"])
            leaves = cost_target_leaves(path, finance.finance_types, allowed_components=("upfront_capex",))
            return tuple(("cost", instance, leaf) for instance in sorted(chosen) for leaf in leaves)
        if kind in ("maintenance", "maintenance_expense_net"):
            if maintenance[source["year"]] < source["position"]:
                reject(
                    field,
                    "capitalized_maintenance_source_missing",
                    year=source["year"],
                    position=source["position"],
                )
            return (("maintenance", source["year"], source["position"]),)
        if kind == "movement":
            axis, selected = expand_amount_period(parse_amount_period(source["period"]), "scalar", 0)
            actual = {(at.axis, at.period) for at in occurrences[source["id"]]}
            missing = {(axis, period) for period in selected} - actual
            if missing:
                reject(
                    field,
                    "movement_reference_occurrence_missing",
                    movement=source["id"],
                    periods=sorted(missing),
                )
            return tuple(("movement", source["id"], axis, period) for period in selected)
        raise AssertionError(f"finance supplied a non-spending capitalized root: {kind}")

    allocated = defaultdict(Decimal)
    ownership = defaultdict(list)
    # Existing direct depreciation claims already passed their own public
    # checks. Include their ownership when new named assets share the space.
    for rule_id, rule in finance.tax_rules.items() if finance.assets else ():
        if not isinstance(rule, IncomeTaxRule):
            continue
        for index, entry in enumerate(rule.depreciation):
            if entry.base_from.kind == "asset":
                continue
            field = f"effective_finance.tax_rules.{rule_id}.depreciation[{index}].base_from"
            for atom in source_atoms(entry.base_from, field):
                allocated[atom] += 1
                ownership[atom].append(field)
    for asset_id, asset in finance.assets.items():
        field = f"effective_finance.assets.{asset_id}"
        available_year = construction + asset.available_from
        if (
            asset.capitalized_at.axis == "financial_year"
            and asset.capitalized_at.period >= available_year
            or asset.capitalized_at.axis == "tax_month"
            and asset.capitalized_at.period > 12 * (available_year - 1)
        ):
            reject(
                field + ".available_from",
                "asset_available_before_capitalization",
                capitalized_at=asset.capitalized_at.to_dict(),
                available_from=asset.available_from,
            )
        for leaf in asset_basis_sources(finance, asset.basis_from):
            for atom in source_atoms(leaf.source, field + ".basis_from"):
                if not leaf.share:
                    continue
                allocated[atom] += leaf.share
                ownership[atom].append(field + ".basis_from")
                if allocated[atom] > 1:
                    reject(
                        field + ".basis_from",
                        "capitalized_source_share_exceeds_one",
                        source=atom,
                        share=str(allocated[atom]),
                        owners=ownership[atom],
                    )

    for rule_id, rule in finance.tax_rules.items():
        if not isinstance(rule, IncomeTaxRule):
            continue
        for item in rule.deductible_maintenance.items:
            atom = ("maintenance", item.year, item.position)
            if item.deductible and allocated[atom]:
                reject(
                    f"effective_finance.tax_rules.{rule_id}.deductible_maintenance.items",
                    "capitalized_maintenance_also_expensed",
                    source=atom,
                    owners=ownership[atom],
                )

    graph = financial_dependency_graph(doc, finance, evaluation)
    for node in graph.nodes:
        for edge in node.edges:
            kind, identity, axis, period, _ = edge.dependency
            if kind == "movement" and (axis, period) not in {
                (at.axis, at.period) for at in occurrences[identity]
            }:
                reject(
                    edge.field_path,
                    "movement_reference_occurrence_missing",
                    movement=identity,
                    axis=axis,
                    period=period,
                )
    if graph.unresolved:
        by_key = {node.key: node for node in graph.nodes}
        unresolved = set(graph.unresolved)
        visited = set()
        for start in graph.unresolved:
            if start in visited:
                continue
            stack = [(start, iter(key for key in by_key[start].dependencies if key in unresolved))]
            positions = {start: 0}
            while stack:
                node, children = stack[-1]
                dependency = next(children, None)
                if dependency is None:
                    visited.add(node)
                    positions.pop(node)
                    stack.pop()
                elif dependency in positions:
                    cycle = [item[0] for item in stack[positions[dependency] :]] + [dependency]
                    fields = [
                        next(edge.field_path for edge in by_key[owner].edges if edge.dependency == target)
                        for owner, target in zip(cycle, cycle[1:], strict=False)
                    ]
                    reject(
                        fields[-1],
                        "financial_period_dependency_cycle",
                        cycle=cycle,
                        fields=fields,
                    )
                elif dependency not in visited:
                    positions[dependency] = len(stack)
                    stack.append(
                        (
                            dependency,
                            iter(key for key in by_key[dependency].dependencies if key in unresolved),
                        )
                    )


__all__ = ["check_financial_relation_inputs"]
