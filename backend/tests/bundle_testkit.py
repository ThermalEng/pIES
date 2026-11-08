"""Small-model inspection/fixtures only; production never materializes these documents."""
from __future__ import annotations

import json
from collections.abc import Mapping
from types import SimpleNamespace

from iesplan.computation.generators.parameters import RECORDS_PATH, parameter_chunks
from iesplan.core.content import MemoryContent, read_content, text_lines


def content_bytes(files):
    """Explicit small-fixture inspection, including every actual emitted file."""
    return {path: read_content(content) for path, content in files.items()}


def stored_parameters(db, files):
    from iesplan.storage import get_object_content

    bundle = SimpleNamespace(files={
        name: get_object_content(db, entry["object_id"]) for name, entry in files.items()
    })
    return json.loads(materialized_file(bundle, "input/parameters.json"))


def evaluate_fact_expressions(node, values):
    """Independent small-fixture affine evaluator, before scalar financial publication."""
    if isinstance(node, Mapping):
        if "constant" in node and "terms" in node:
            total = float(node["constant"])
            for name, coefficient in node["terms"].items():
                total += float(coefficient) * values[name]
            return total
        return {key: evaluate_fact_expressions(value, values) for key, value in node.items()}
    if isinstance(node, (tuple, list)):
        return [evaluate_fact_expressions(value, values) for value in node]
    return node


def mps_columns(raw):
    if isinstance(raw, bytes):
        raw = MemoryContent(raw)
    lines = iter(text_lines(raw))
    for line in lines:
        if line == "COLUMNS":
            break
    names = []
    for line in lines:
        parts = line.split()
        if parts[0] == "RHS":
            break
        if parts[0] not in ("INTORG", "INTEND") and (not names or names[-1] != parts[0]):
            names.append(parts[0])
    return names


def fixture_files(files):
    """Compile small hand-written mathematical test declarations into current records."""
    files = dict(files)
    head = json.loads(files["input/parameters.json"])
    if "records" in head:
        return files
    names = mps_columns(files["input/problem.mps"])
    raw = files["input/problem.mps"]
    if isinstance(raw, bytes):
        raw = MemoryContent(raw)
    rows = []
    in_rows = False
    for line in text_lines(raw):
        if line == "COLUMNS":
            break
        if line == "ROWS":
            in_rows = True
        elif in_rows and line.split()[0] != "N":
            rows.append(line.split()[1])

    column_positions = {name: index for index, name in enumerate(names)}
    row_positions = {name: index for index, name in enumerate(rows)}

    class Positions:
        def column_position(self, name):
            return column_positions[name]

        def column_names(self):
            return iter(names)

        def row_names(self):
            return iter(rows)

    class Targets(list):
        def solver_positions(self, model, kind):
            positions = column_positions if kind == "column" else row_positions
            return (positions[name] for name in self)

    for group in head["model_sources"]["groups"]:
        group["targets"] = Targets(group["targets"])

    files[RECORDS_PATH] = MemoryContent(b"".join(parameter_chunks(
        Positions(), head.pop("unit_map"), head.pop("components"),
        head.pop("step_series") + head.pop("system_series"),
        head.pop("financial_facts"), head.pop("model_sources"),
    )))
    head["records"] = RECORDS_PATH
    head["column_count"] = len(names)
    files["input/parameters.json"] = MemoryContent(json.dumps(head).encode())
    return files


def materialized_file(bundle, path):
    raw = read_content(bundle.files[path])
    if path != "input/parameters.json":
        return raw
    head = json.loads(raw)
    if "records" not in head:
        return raw
    names = []
    row_names = []
    expressions = []
    doc = dict(head, unit_map={}, components={}, step_series=[], system_series=[])
    group = None
    entry = None

    def refs(node):
        if isinstance(node, dict):
            if "expression" in node:
                return expressions[node["expression"]]
            return {key: refs(value) for key, value in node.items()}
        if isinstance(node, list):
            return [refs(value) for value in node]
        return node

    for line in text_lines(bundle.files[head["records"]]):
        record = json.loads(line)
        kind = record[0]
        if kind == "column_names":
            names.extend(record[1])
        elif kind == "row_names":
            row_names.extend(record[1])
        elif kind == "expression":
            entry = {"constant": record[2], "terms": {}}
        elif kind == "terms":
            entry["terms"].update((names[index], value) for index, value in record[1])
        elif kind == "expression_end":
            expressions.append(entry)
        elif kind == "units":
            doc["unit_map"].update((names[position], unit) for position, unit in record[1])
        elif kind == "component":
            doc["components"][record[1]] = dict(expressions[record[4]], unit=record[2], caliber=record[3])
        elif kind == "facts":
            doc["financial_facts"] = refs(record[1])
        elif kind == "series":
            entry = dict(record[1], columns=[])
        elif kind == "positions":
            entry["columns"].extend(names[index] for index in record[1])
        elif kind == "series_end":
            doc["system_series" if "block" in entry else "step_series"].append(entry)
        elif kind == "sources":
            doc["model_sources"] = dict(record[1], groups=[])
        elif kind == "source_group":
            group = dict(record[1], targets=[], indices=[] if record[2] is None else record[2])
        elif kind == "targets":
            entries = names if group["role"].startswith("column:") else row_names
            group["targets"].extend(entries[position] for position in record[1])
        elif kind == "indices":
            group["indices"].extend(record[1])
        elif kind == "source_group_end":
            doc["model_sources"]["groups"].append(group)
    doc.pop("records")
    return json.dumps(doc).encode()
