"""Resolve a selected baseline at the real public task submission boundary."""

from __future__ import annotations

import json
from dataclasses import replace

from iesplan import results as results_domain
from iesplan import tasks as tasks_domain
from iesplan.application.projects import ensure_access
from iesplan.assembly import check_amount_relations, consumed_amounts, financial_dependency_graph
from iesplan.core.errors import NotFoundError
from iesplan.core.units import dims_of
from iesplan.finance import (
    ComparisonError,
    TaskResultBaseline,
    expand_amount_period,
    freeze_comparison,
    parse_amount_period,
)
from iesplan.storage import get_object
from iesplan.tasks import InvalidRequestError


def resolve_comparison(db, actor, evaluation, currency):
    """Read one exact accessible result, then keep only selected frozen values."""
    request = evaluation.comparison
    financial, origin = None, None
    if request.mode != "absolute" and isinstance(request.baseline, TaskResultBaseline):
        ref = request.baseline
        task = tasks_domain.get_task(db, ref.task_id)
        if task is None:
            raise NotFoundError("基准任务不存在", params={"task_id": ref.task_id})
        ensure_access(db, actor, task.project_id, "view")
        evidence = results_domain.get_evidence(db, ref.evidence_id)
        field = "evaluation.comparison.baseline"
        if evidence is None or evidence.task_id != ref.task_id:
            raise InvalidRequestError(
                "基准证据不属于指定任务",
                code="TASK-REQ-015",
                params={"detail": "基准证据不属于指定任务"},
                location={"object_type": "task", "field": field + ".evidence_id"},
            )
        if task.status != "completed":
            raise InvalidRequestError(
                "基准须为已完成且有封存财务结果的任务",
                code="TASK-REQ-015",
                params={"detail": "基准须为已完成且有封存财务结果的任务"},
                location={"object_type": "task", "field": field + ".task_id"},
            )
        content = json.loads(get_object(db, evidence.object_id))
        financial = content.get("financial_evaluation")
        if financial is None or financial["status"] != "evaluated":
            raise InvalidRequestError(
                "指定基准证据没有可用的封存财务评价",
                code="TASK-REQ-015",
                params={"detail": "指定基准证据没有可用的封存财务评价"},
                location={"object_type": "task", "field": field + ".evidence_id"},
            )
        origin = {
            "kind": "task_result",
            "task_id": task.id,
            "evidence_id": evidence.id,
            "attempt_id": evidence.attempt_id,
            "object_id": evidence.object_id,
        }
    try:
        frozen = freeze_comparison(
            request, years=evaluation.evaluation_years, currency=currency, financial=financial, source=origin
        )
    except ComparisonError as exc:
        raise InvalidRequestError(
            str(exc),
            code="TASK-REQ-015",
            params={"detail": str(exc)},
            location={"object_type": "task", "field": exc.field},
        ) from exc
    return replace(evaluation, comparison=frozen)


def check_comparison_inputs(evaluation, artifact, finance, generator, capabilities):
    """Bind evaluation-owned aliases/units/periods once, before snapshot creation."""
    frozen = evaluation.comparison
    doc = json.loads(artifact.canonical_text)
    facts = frozen.facts

    def reject(field, detail):
        raise InvalidRequestError(
            detail,
            code="TASK-REQ-015",
            params={"detail": detail},
            location={"object_type": "task", "field": field},
        )

    def reference(ref, axis, period, field):
        if ref["id"] not in facts:
            reject(field + ".id", f"未冻结基准事实别名: {ref['id']}")
        fact = facts[ref["id"]]
        selected_axis, selected = expand_amount_period(parse_amount_period(ref["period"]), axis, period)
        if fact.axis != selected_axis:
            reject(field + ".period", f"基准轴 {fact.axis} 与引用轴 {selected_axis} 不一致")
        minimum = 1 if fact.axis == "tax_month" else 0
        maximum = len(fact.values) - 1 + minimum
        if any(p < minimum or p > maximum for p in selected):
            reject(field + ".period", f"基准引用超出已冻结期间 {minimum}..{maximum}")
        return fact

    baseline_constraints = {}
    for identity, entry in doc["planning"].get("constraints", {}).items():
        if entry["type"] != "amount_relation" or not entry.get("enabled", True):
            continue
        for alias, ref in entry["references"].items():
            if ref["kind"] == "baseline":
                baseline_constraints[identity] = entry
                reference(ref, "scalar", 0, f"planning.constraints.{identity}.references.{alias}")
    diagnostics = []
    check_amount_relations(
        {**doc["planning"], "constraints": baseline_constraints}, finance, diagnostics, baseline_facts=facts
    )
    if diagnostics:
        diagnostic = diagnostics[0]
        reject(diagnostic.location["field"], diagnostic.params["detail"])
    # Check the periods the dependency graph actually consumes (year-0
    # occurrences and ``year_months: current`` reach before period 1).
    for node in financial_dependency_graph(doc, finance, evaluation).nodes:
        kind, identity, axis, period, _ = node.key
        if kind != "amount":
            continue
        for term_id, term in finance.amounts[identity].terms.items():
            if term.source.kind == "baseline":
                field = f"effective_finance.amounts.{identity}.terms.{term_id}.source"
                reference(term.source.to_dict(), axis, period, field)
    for identity in consumed_amounts(doc, finance):
        definition = finance.amounts[identity]
        for term_id, term in definition.terms.items():
            if term.source.kind != "baseline":
                continue
            field = f"effective_finance.amounts.{identity}.terms.{term_id}.source"
            if term.source.id not in facts:
                reject(field + ".id", f"未冻结基准事实别名: {term.source.id}")
            fact = facts[term.source.id]
            dimensions = dims_of(fact.unit).copy()
            dimensions.update(dims_of(term.coefficient.unit))
            dimensions = {key: value for key, value in dimensions.items() if value}
            if dimensions != dict(dims_of(definition.unit)):
                reject(field, f"基准 {term.source.id} × coefficient 与 {definition.unit} 量纲不一致")
    required = set()
    if frozen.mode != "absolute":
        required.update(("finance.frozen_baseline", "finance.financial_facts"))
    if frozen.mode == "difference":
        required.add("finance.cashflow_difference")
    missing = sorted(required - set(capabilities))
    if missing:
        field = "evaluation.comparison"
        raise InvalidRequestError(
            "所选生成器缺冻结基准及同次比较能力",
            code="TASK-REQ-007",
            params={"generator": generator, "missing_capabilities": missing, "fields": [field]},
            location={"object_type": "task", "field": field},
        )
