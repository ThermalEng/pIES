"""Project binding for finance's named amounts at the public input boundary.

Finance owns file-local identities and dependency validity. This module binds
only consumed definitions to the project's actual instances, ports and meters;
trusted snapshots and workers do not call these checks again.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping

from iesplan.assembly.diags import ASM_PLAN_DIM, ASM_PLAN_SYNTAX, ASM_PLAN_UNDEF, make_asm_diag
from iesplan.assembly.rules.finance import add_dimensions, driver_dimensions, required_components
from iesplan.core.contracts import compile_planning_expression
from iesplan.core.expression import ExpressionCodeError, ExpressionDimensionError, ExpressionError
from iesplan.core.units import ALIAS_MAP, UNITS, UnitError, dims_of
from iesplan.finance import amount_dependency_closure


def consumed_amounts(doc: Mapping, finance) -> tuple[str, ...]:
    """The one deterministic consumption closure shared by submit and generation."""
    roots = {
        target["id"]
        for constraint in (doc.get("planning", {}).get("constraints") or {}).values()
        if constraint.get("type") == "amount_relation" and constraint.get("enabled", True)
        for target in constraint["references"].values()
        if target["kind"] == "amount"
    }
    roots.update(
        cash.amount.id for cash in finance.net_cashflows.values() if cash.amount.kind == "amount_ref"
    )
    roots.update(movement.amount.id for movement in finance.cash_movements.values())
    roots.update(
        asset.basis_from.id for asset in finance.assets.values() if asset.basis_from.kind == "amount"
    )
    return amount_dependency_closure(finance.amounts, roots)


def _reject(diags, code, field, reason, **extra):
    diags.append(
        make_asm_diag(
            code,
            severity="error",
            blocking=True,
            params={"reason": reason, "detail": reason, **extra},
            location={"object_type": "planning", "field": field},
        )
    )


def _period_axis(period, current_axis):
    kind = period["kind"]
    if kind == "current":
        return current_axis
    if kind == "range":
        return period["axis"]
    if kind == "year_months":
        return "tax_month"
    return kind


def _amount_period(amount, period, current_axis=None):
    return _period_axis(period, current_axis) == amount.axis


def _degree(ir):
    if ir[0] == "num":
        return 0
    if ir[0] == "var":
        return 1
    if ir[0] == "unary":
        return _degree(ir[2])
    if ir[0] == "bin":
        operation, lhs, rhs = ir[1:]
        left, right = _degree(lhs), _degree(rhs)
        if operation in ("add", "sub"):
            return max(left, right)
        if operation == "mul" and left + right <= 1:
            return left + right
        if operation == "div" and right == 0:
            return left
    raise ValueError("amount_relation_requires_affine_expression")


def check_amount_relations(planning: Mapping, finance, diags: list) -> None:
    """Resolve local amount aliases once on planning save or assembly input.

    Evaluation-owned baseline units cannot be inferred at planning-save time;
    their active use remains capability-gated until frozen-baseline delivery.
    """
    reserved = set(planning.get("variables") or {}) | {"step_duration"}
    for constraint_id, entry in sorted((planning.get("constraints") or {}).items()):
        if entry.get("type") != "amount_relation":
            continue
        field = f"planning.constraints.{constraint_id}"
        symbols = {}
        pending_baseline = False
        broken = False
        for alias, target in sorted(entry["references"].items()):
            ref_field = f"{field}.references.{alias}"
            if (
                alias in reserved
                or "__" in alias
                or alias in ALIAS_MAP
                or alias in UNITS
                or re.fullmatch(r"(?:pfin|qconst)\d+", alias)
            ):
                _reject(diags, ASM_PLAN_UNDEF, ref_field, "alias_reserved_collision", alias=alias)
                broken = True
                continue
            if target["kind"] == "baseline":
                pending_baseline = True
                continue
            amount = finance.amounts.get(target["id"])
            if amount is None:
                _reject(diags, ASM_PLAN_UNDEF, ref_field + ".id", "amount_unknown_id", id=target["id"])
                broken = True
                continue
            if not _amount_period(amount, target["period"]):
                _reject(
                    diags,
                    ASM_PLAN_UNDEF,
                    ref_field + ".period",
                    "amount_period_axis_mismatch",
                    axis=amount.axis,
                )
                broken = True
                continue
            symbols[alias] = Counter(dict(dims_of(amount.unit)))
        if broken or pending_baseline:
            continue
        try:
            compiled = compile_planning_expression(entry["expression"], symbols, currency=finance.currency)
            ir = compiled.ir
            if ir[0] != "cmp" or ir[1] not in ("eq", "le", "ge"):
                raise ValueError("amount_relation_requires_equality_or_inequality")
            _degree(ir[2])
            _degree(ir[3])
        except ExpressionDimensionError as exc:
            _reject(diags, ASM_PLAN_DIM, field + ".expression", "dimension_mismatch", detail=str(exc))
        except ExpressionCodeError as exc:
            _reject(diags, ASM_PLAN_UNDEF, field + ".expression", "amount_unknown_symbol", detail=str(exc))
        except (ExpressionError, ValueError) as exc:
            _reject(
                diags, ASM_PLAN_SYNTAX, field + ".expression", "amount_expression_invalid", detail=str(exc)
            )


def _selected_instances(doc, source):
    bindings = doc["finance_binding"]["instances"]
    matching = {
        instance
        for instance, binding in bindings.items()
        if binding["type"] == "costed" and binding["finance_type"] == source["finance_type"]
    }
    scope = source["scope"]
    if scope["kind"] == "all_new_instances":
        return sorted(
            instance for instance in matching if doc["devices"][instance]["asset_origin"] == "new"
        ), set()
    selected = set(scope["instances"])
    return sorted(selected & matching), selected - matching


def check_amount_sources(doc: Mapping, finance, registry: Mapping, diags: list) -> None:
    """Bind actual source ranges/units once, without evaluating money or solving."""
    for amount_id in consumed_amounts(doc, finance):
        amount = finance.amounts[amount_id]
        for term_id, term in sorted(amount.terms.items()):
            source = term.source.to_dict()
            kind = source["kind"]
            field = f"effective_finance.amounts.{amount_id}.terms.{term_id}.source"
            source_dims = []
            broken = False
            period = source.get("period", {"kind": "scalar"})
            axis = _period_axis(period, amount.axis)
            if kind in ("driver", "cost"):
                if kind == "cost":
                    source = {**source, "finance_type": source["path"].split(".", 1)[0]}
                selected, invalid = _selected_instances(doc, source)
                if invalid:
                    _reject(
                        diags,
                        ASM_PLAN_UNDEF,
                        field + ".scope",
                        "amount_scope_wrong_instance",
                        instances=sorted(invalid),
                        finance_type=source["finance_type"],
                    )
                    broken = True
                for instance in selected:
                    binding = doc["finance_binding"]["instances"][instance]
                    if kind == "cost":
                        component = source["path"].split(".")[1]
                        if component not in required_components(doc["devices"][instance]["asset_origin"]):
                            _reject(
                                diags,
                                ASM_PLAN_UNDEF,
                                field + ".scope",
                                "amount_sunk_component_reference",
                                instance=instance,
                            )
                            broken = True
                            continue
                        continue
                    target = binding["drivers"].get(source["driver"])
                    if target is None:
                        _reject(
                            diags,
                            ASM_PLAN_UNDEF,
                            field,
                            "amount_driver_binding_missing",
                            instance=instance,
                            driver=source["driver"],
                        )
                        broken = True
                        continue
                    descriptor = registry[doc["devices"][instance]["definition"]["id"]]
                    if "property" in target:
                        if axis != "scalar":
                            _reject(
                                diags,
                                ASM_PLAN_UNDEF,
                                field + ".period",
                                "amount_property_requires_scalar",
                                instance=instance,
                            )
                            broken = True
                        source_dims.append(dict(dims_of(descriptor.properties[target["property"]].unit)))
                    else:
                        if axis == "scalar":
                            _reject(
                                diags,
                                ASM_PLAN_UNDEF,
                                field + ".period",
                                "amount_interface_requires_period",
                                instance=instance,
                            )
                            broken = True
                        source_dims.append(
                            driver_dimensions(
                                descriptor.interfaces[target["interface"]].unit,
                                target["aggregation"],
                            )
                        )
            elif kind in ("tariff", "demand"):
                binding_table = doc["tariff_bindings" if kind == "tariff" else "demand_bindings"]
                selector = "price" if kind == "tariff" else "charge"
                for binding_id in source["bindings"]:
                    binding = binding_table.get(binding_id)
                    if binding is None or binding[selector] != source[selector]:
                        _reject(
                            diags,
                            ASM_PLAN_UNDEF,
                            field + ".bindings",
                            "amount_binding_wrong_source",
                            binding=binding_id,
                        )
                        broken = True
            elif kind == "construction_unit":
                if source["unit_ref"] not in (doc["planning"].get("construction_units") or {}):
                    _reject(
                        diags,
                        ASM_PLAN_UNDEF,
                        field + ".unit_ref",
                        "amount_unknown_construction_unit",
                        unit_ref=source["unit_ref"],
                    )
                    broken = True
            elif kind == "system_quantity":
                block = doc.get("system_equations", {}).get(source["block"], {})
                variable = block.get("variables", {}).get(source["variable"])
                if variable is None:
                    _reject(
                        diags,
                        ASM_PLAN_UNDEF,
                        field,
                        "amount_unknown_system_variable",
                        block=source["block"],
                        variable=source["variable"],
                    )
                    broken = True
                else:
                    source_dims.append(driver_dimensions(variable["unit"], source["aggregation"]))
            # File-local amount/cost/tax identities, constant units and DAG are
            # finance-owned. Maintenance identity and period limits need the
            # submitted evaluation and are intentionally not guessed here.
            if broken:
                continue
            for dimensions in source_dims:
                try:
                    actual = add_dimensions(dict(dims_of(term.coefficient.unit)), dimensions)
                    expected = dict(dims_of(amount.unit))
                except UnitError as exc:
                    _reject(diags, ASM_PLAN_DIM, field, "amount_unit_invalid", detail=str(exc))
                    break
                if actual != expected:
                    _reject(
                        diags,
                        ASM_PLAN_DIM,
                        field,
                        "amount_source_dimension_mismatch",
                        expected=expected,
                        actual=actual,
                    )
                    break

    # Direct capitalized cost sources have project-owned scopes too; amount
    # combinations above already reach the same checker through the closure.
    for asset_id, asset in finance.assets.items():
        reference = asset.basis_from
        if reference.kind != "cost":
            continue
        source = reference.to_dict()
        source["finance_type"] = source["path"].split(".", 1)[0]
        selected, invalid = _selected_instances(doc, source)
        invalid.update(instance for instance in selected if doc["devices"][instance]["asset_origin"] != "new")
        if invalid:
            _reject(
                diags,
                ASM_PLAN_UNDEF,
                f"effective_finance.assets.{asset_id}.basis_from.scope",
                "asset_scope_wrong_new_instance",
                instances=sorted(invalid),
            )


__all__ = ["consumed_amounts", "check_amount_relations", "check_amount_sources"]
