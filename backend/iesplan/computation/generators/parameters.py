"""Ordered paired-generator declarations. No whole parameter document or MPS reread."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from itertools import islice

from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.protocols import StopProbe, read_stop_reason

RECORDS_PATH = "input/parameters.jsonl"
RECORD_ITEMS = 512


def _batches(values):
    values = iter(values)
    while batch := list(islice(values, RECORD_ITEMS)):
        yield batch


def parameter_records(model, unit_map, components, series, facts, sources):
    """Identity sharing only: no expression algebra or change to summation order."""
    expressions = []
    identities = {}
    for batch in _batches(model.column_names()):
        yield ["column_names", batch]
    for batch in _batches(model.row_names()):
        yield ["row_names", batch]

    def expression(node):
        identity = (id(node["terms"]), float(node["constant"]).hex())
        if identity not in identities:
            identities[identity] = len(expressions)
            expressions.append(node)
        return identities[identity]

    def refs(node):
        if isinstance(node, Mapping):
            if "constant" in node and "terms" in node:
                return {"expression": expression(node)}
            return {key: refs(value) for key, value in node.items()}
        if isinstance(node, (tuple, list)):
            return [refs(value) for value in node]
        return node

    component_refs = [(name, decl["unit"], decl["caliber"], expression(decl))
                      for name, decl in sorted(components.items())]
    fact_refs = refs(facts)
    for number, node in enumerate(expressions):
        yield ["expression", number, node["constant"]]
        for batch in _batches(node["terms"].items()):
            yield ["terms", [[model.column_position(name), coefficient]
                             for name, coefficient in batch]]
        yield ["expression_end"]
    for batch in _batches((model.column_position(name), unit)
                          for name, unit in sorted(unit_map.items())):
        yield ["units", batch]
    for entry in component_refs:
        yield ["component", *entry]
    yield ["facts", fact_refs]
    for entry in series:
        yield ["series", {key: value for key, value in entry.items() if key != "columns"}]
        for batch in _batches(entry["columns"]):
            yield ["positions", [model.column_position(name) for name in batch]]
        yield ["series_end"]
    yield ["sources", {key: value for key, value in sources.items() if key != "groups"}]
    for group in sources["groups"]:
        indices = group["indices"]
        yield ["source_group", {key: value for key, value in group.items()
                                if key not in ("targets", "indices")},
               indices if isinstance(indices, Mapping) else None]
        for batch in _batches(group["targets"].solver_positions(model, group["role"].partition(":")[0])):
            yield ["targets", batch]
        if not isinstance(indices, Mapping):
            for batch in _batches(indices):
                yield ["indices", batch]
        yield ["source_group_end"]


def parameter_chunks(*args, stop: StopProbe | None = None) -> Iterator[bytes]:
    for record in parameter_records(*args):
        reason = read_stop_reason(stop)
        if reason is not None:
            raise ComputationUnavailableError("生成阶段已停止", reason=reason)
        # Each numeric/name payload is already bounded by RECORD_ITEMS. Encode
        # that small record in C rather than serializing each scalar separately.
        yield json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False,
                         separators=(",", ":")).encode("utf-8") + b"\n"


def read_records(content):
    # Used only by the paired adapter; trusted typed generator output, no schema pass.
    from iesplan.core.content import text_lines

    for line in text_lines(content):
        yield json.loads(line)
