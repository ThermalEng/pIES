"""Bounded JSON projection over repeatable Content, without a second document tree.

Selectors describe the values a reader needs; skipped values are parsed but never
materialized. Keys are actual JSON keys, not dot-separated parser prefixes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import ijson
from ijson import JSONError as JsonReadError

from iesplan.core.content import Content

__all__ = ["ArrayReader", "JsonReadError", "ObjectFields", "project_json", "project_value"]


@dataclass(frozen=True)
class ObjectFields:
    fields: Mapping[str, Any] = field(default_factory=dict)
    other: Any = False


@dataclass(frozen=True)
class ArrayReader:
    """Consume array items incrementally (a filter, page, or disk spool)."""

    consume: Callable[[Iterator[Any]], Any]


class ContentReader:
    """The small file-like read interface expected by incremental parsers."""

    def __init__(self, content: Content):
        self.chunks = iter(content.iter_chunks())
        self.pending = memoryview(b"")

    def read(self, size: int) -> bytes:
        parts = []
        while size > 0:
            if not self.pending:
                self.pending = memoryview(next(self.chunks, b""))
                if not self.pending:
                    break
            part = self.pending[:size]
            parts.append(part)
            self.pending = self.pending[len(part) :]
            size -= len(part)
        return b"".join(parts)


def project_json(content: Content, selection: ObjectFields) -> dict:
    """Read only selected subtrees; RAM is selected output + parser chunk + depth."""
    events = iter(ijson.basic_parse(ContentReader(content), use_float=True))

    def skip(event):
        depth = int(event in ("start_map", "start_array"))
        while depth:
            event, _ = next(events)
            depth += (event in ("start_map", "start_array")) - (event in ("end_map", "end_array"))

    def value(event, scalar, spec=True):
        if spec is False:
            skip(event)
            return None
        if event == "start_map":
            result = {}
            for event, key in events:
                if event == "end_map":
                    return result
                child = spec.fields.get(key, spec.other) if isinstance(spec, ObjectFields) else True
                event, scalar = next(events)
                selected = value(event, scalar, child)
                if child is not False:
                    result[key] = selected
        elif event == "start_array":

            def items():
                for event, scalar in events:
                    if event == "end_array":
                        return
                    yield value(event, scalar)

            return spec.consume(items()) if isinstance(spec, ArrayReader) else list(items())
        else:
            return scalar

    result = value(*next(events), selection)
    # Finish parsing: JSON errors remain visible, without a separate validation pass.
    for _ in events:
        pass
    return result


def project_value(value: Any, selection: Any) -> Any:
    """The same projection over an already-owned tree, before evidence is sealed."""
    if isinstance(selection, ObjectFields) and isinstance(value, Mapping):
        return {
            key: project_value(item, spec)
            for key, item in value.items()
            if (spec := selection.fields.get(key, selection.other)) is not False
        }
    if isinstance(selection, ArrayReader):
        return selection.consume(iter(value))
    return value
