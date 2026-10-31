"""Per-build source references. No model reconstruction or global registry."""
from __future__ import annotations

from dataclasses import asdict


class SourceTrace:
    def __init__(self):
        self.sources = {}
        self.groups = {}
        self.active = None

    def register(self, identity, kind, location, domain, inputs=(), depends_on=(), outputs=()):
        self.sources[identity] = {
            "kind": kind,
            "location": {k: list(v) if isinstance(v, tuple) else v
                         for k, v in asdict(location).items() if v is not None},
            "domain": asdict(domain),
            "inputs": list(inputs), "depends_on": list(depends_on),
            "outputs": list(outputs),
        }

    def activate(self, identity, role, axis="scalar", index=0):
        self.active = (identity, role, axis, index)

    def record(self, target, kind):
        if self.active is None:
            return
        source, role, axis, index = self.active
        key = (source, f"{kind}:{role}", axis)
        group = self.groups.get(key)
        if group is None:
            group = self.groups[key] = {"targets": [], "indices": []}
        group["targets"].append(target)
        group["indices"].append(index)

    def location(self):
        if self.active is None:
            return None
        source, role, axis, index = self.active
        entry = self.sources[source]
        coefficients = {
            identity: dict(self.sources[f"coefficient:{identity}"]["location"])
            for identity in entry["inputs"] if f"coefficient:{identity}" in self.sources
        }
        return {"source": source, "role": role, "axis": axis, "index": index,
                "location": dict(entry["location"]), "coefficients": coefficients}

    @staticmethod
    def _indices(values):
        stride = values[1] - values[0] if len(values) > 1 else 0
        if all(value == values[0] + i * stride for i, value in enumerate(values)):
            return {"start": values[0], "stride": stride, "count": len(values)}
        return list(values)

    def export(self, outputs):
        return {
            "schema": "ies.model-sources", "schema_version": "1.0.0",
            "sources": dict(sorted(self.sources.items())),
            "groups": [
                {"source": source, "role": role, "axis": axis,
                 "targets": list(group["targets"]),
                 "indices": self._indices(group["indices"])}
                for (source, role, axis), group in sorted(self.groups.items())
            ],
            "outputs": outputs,
        }


class SourceRows(list):
    def __init__(self, trace):
        super().__init__()
        self.trace = trace

    def append(self, row):
        self.trace.record(row.name, "row")
        super().append(row)

    def extend(self, rows):
        for row in rows:
            self.append(row)


class SourceColumns(dict):
    def __init__(self, trace):
        super().__init__()
        self.trace = trace

    def __setitem__(self, name, variable):
        if name not in self:
            self.trace.record(name, "column")
        super().__setitem__(name, variable)
