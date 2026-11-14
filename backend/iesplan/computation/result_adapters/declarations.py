"""Consume paired ordered records once, directly against the compact primal vector."""

from __future__ import annotations

import math
from array import array
from collections.abc import Mapping

from iesplan.computation.generators.parameters import read_records
from iesplan.core.units import UnitError, from_si


def recover_declarations(content, primals, fail, *, include_solution=True):
    expressions = []
    variables = []
    components = {}
    device_series = {}
    system_series = {}
    facts = None
    sources = None
    total = 0.0
    group = None
    series = None
    column_names = []
    row_names = []
    epigraph_peak = None
    epigraph_max = None

    def value(position, unit):
        try:
            result = from_si(primals[position], unit)
        except (IndexError, UnitError) as exc:
            fail(f"声明列位置 {position!r} 反向换算失败 →{unit!r}: {exc}")
        if not math.isfinite(result):
            fail(f"声明列位置 {position!r} 换算后非有限")
        return result

    def resolve(node):
        if isinstance(node, Mapping):
            if "expression" in node:
                return expressions[node["expression"]]
            return {key: resolve(item) for key, item in node.items()}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    for record in read_records(content):
        kind = record[0]
        if kind == "column_names":
            column_names.extend(record[1])
        elif kind == "row_names":
            if include_solution:
                row_names.extend(record[1])
        elif kind == "epigraph":
            try:
                epigraph_peak = primals[record[1]]
            except IndexError as exc:
                fail(f"需量包络峰值列位置不存在: {record[1]!r}: {exc}")
            if not math.isfinite(epigraph_peak):
                fail(f"需量包络峰值列位置 {record[1]!r} 非有限")
            epigraph_max = None
        elif kind == "epigraph_flows":
            for position in record[1]:
                try:
                    flow = primals[position]
                except IndexError as exc:
                    fail(f"需量包络流量列位置不存在: {position!r}: {exc}")
                if not math.isfinite(flow):
                    fail(f"需量包络流量列位置 {position!r} 非有限")
                if epigraph_max is None or flow > epigraph_max:
                    epigraph_max = flow
        elif kind == "epigraph_end":
            if epigraph_max is not None:
                tol = max(1e-4, 1e-7 * max(1.0, abs(epigraph_max), abs(epigraph_peak)))
                if epigraph_peak - epigraph_max > tol:
                    return {"epigraph_inflated": True}
            epigraph_peak = None
            epigraph_max = None
        elif kind == "expression":
            total = float(record[2])
        elif kind == "terms":
            for position, coefficient in record[1]:
                try:
                    total += float(coefficient) * primals[position]
                except IndexError as exc:
                    fail(f"表达式的求解列位置不存在: {position!r}: {exc}")
        elif kind == "expression_end":
            expressions.append(total)
        elif kind == "units":
            for position, unit in record[1]:
                variables.append({"name": column_names[position], "unit": unit,
                                  "value": value(position, unit)})
        elif kind == "component":
            _, identity, unit, caliber, expression = record
            components[identity] = {"unit": unit, "caliber": caliber,
                                    "constant": expressions[expression], "terms": {}}
        elif kind == "facts":
            if include_solution:
                facts = resolve(record[1])
        elif kind == "series":
            series = dict(record[1], values=array("d"))
        elif kind == "positions":
            if include_solution:
                series["values"].extend(value(position, series["unit"]) for position in record[1])
        elif kind == "series_end":
            target = system_series if "block" in series else device_series
            identity = series.pop("series_id")
            target[identity] = series
            series = None
        elif kind == "sources":
            sources = dict(record[1], groups=[])
        elif kind == "source_group":
            group = dict(record[1], targets=[], indices=record[2] if record[2] is not None else [])
        elif kind == "targets":
            if include_solution:
                names = column_names if group["role"].startswith("column:") else row_names
                group["targets"].extend(names[position] for position in record[1])
        elif kind == "indices":
            if include_solution:
                group["indices"].extend(record[1])
        elif kind == "source_group_end":
            sources["groups"].append(group)
            group = None
        else:
            raise ValueError(f"未知参数记录: {kind!r}")
    return {"variables": variables, "components": components,
            "financial_facts": facts, "model_sources": sources,
            "series": device_series, "system_series": system_series,
            "epigraph_inflated": False}
