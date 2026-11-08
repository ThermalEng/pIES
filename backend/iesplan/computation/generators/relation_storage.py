"""Per-build numeric storage for bound analysis, not a second model/contract.

Names and source locations are shared. Per-slot bounds and per-row nonzeros
live in unboxed arrays; querying a row does not retain a Python object graph.
The actual float coefficients, time slots and source indices are preserved.
"""
from __future__ import annotations

import math
from array import array
from collections.abc import Callable, Iterable, Iterator, MutableMapping
from dataclasses import dataclass

from iesplan.computation.generators.compiler_ir import SourceLocation


class BoundTable(MutableMapping):
    """One symbol index and compact mutable endpoints; None stays unknown."""

    def __init__(self) -> None:
        self.slots: dict[str, int] = {}
        self.names: list[str] = []
        self.lower = array("d")
        self.upper = array("d")
        self.lower_sources = array("I")
        self.upper_sources = array("I")
        self.has_sources = bytearray()
        self.sources: list[SourceLocation | None] = [None]
        self._source_slots: dict[SourceLocation | None, int] = {None: 0}

    def source_slot(self, source: SourceLocation | None) -> int:
        slot = self._source_slots.get(source)
        if slot is None:
            slot = len(self.sources)
            self._source_slots[source] = slot
            self.sources.append(source)
        return slot

    def ensure(self, name: str) -> int:
        slot = self.slots.get(name)
        if slot is None:
            slot = len(self.names)
            self.slots[name] = slot
            self.names.append(name)
            # NaN is solely an internal missing-endpoint marker, never an
            # inferred bound, serialized value or operand in propagation.
            self.lower.append(math.nan)
            self.upper.append(math.nan)
            self.lower_sources.append(0)
            self.upper_sources.append(0)
            self.has_sources.append(0)
        return slot

    def endpoints(self, slot: int) -> tuple[float | None, float | None]:
        lo, hi = self.lower[slot], self.upper[slot]
        return (None if math.isnan(lo) else lo, None if math.isnan(hi) else hi)

    def set_endpoints(self, slot: int, lo: float | None, hi: float | None) -> None:
        self.lower[slot] = math.nan if lo is None else lo
        self.upper[slot] = math.nan if hi is None else hi

    def __getitem__(self, name: str) -> tuple[float | None, float | None]:
        return self.endpoints(self.slots[name])

    def __setitem__(self, name: str, value: tuple[float | None, float | None]) -> None:
        self.set_endpoints(self.ensure(name), *value)

    def __delitem__(self, name: str) -> None:
        raise TypeError("analysis symbol slots are append-only")

    def __iter__(self) -> Iterator[str]:
        return iter(self.slots)

    def __len__(self) -> int:
        return len(self.names)

    def snapshot(self) -> tuple[array, array, array, array, bytearray]:
        """Compact rollback of derived bounds after a proven contradiction."""
        return (
            self.lower[:], self.upper[:], self.lower_sources[:], self.upper_sources[:], self.has_sources[:],
        )

    def restore(self, state: tuple[array, array, array, array, bytearray]) -> None:
        self.lower, self.upper, self.lower_sources, self.upper_sources, self.has_sources = state


class BoundSources(MutableMapping):
    """Source view over the same symbol slots; pairs are created only on query."""

    def __init__(self, bounds: BoundTable) -> None:
        self.bounds = bounds

    def __getitem__(self, name: str) -> tuple[SourceLocation | None, SourceLocation | None]:
        slot = self.bounds.slots[name]
        if not self.bounds.has_sources[slot]:
            raise KeyError(name)
        return (self.bounds.sources[self.bounds.lower_sources[slot]],
                self.bounds.sources[self.bounds.upper_sources[slot]])

    def __setitem__(self, name: str, value: tuple[SourceLocation | None, SourceLocation | None]) -> None:
        slot = self.bounds.ensure(name)
        self.bounds.lower_sources[slot] = self.bounds.source_slot(value[0])
        self.bounds.upper_sources[slot] = self.bounds.source_slot(value[1])
        self.bounds.has_sources[slot] = 1

    def __delitem__(self, name: str) -> None:
        raise TypeError("analysis symbol slots are append-only")

    def __iter__(self) -> Iterator[str]:
        return (name for name in self.bounds if self.bounds.has_sources[self.bounds.slots[name]])

    def __len__(self) -> int:
        return self.bounds.has_sources.count(1)


@dataclass(frozen=True, slots=True)
class RelationOrigin:
    source: SourceLocation | None
    source_slot: int
    relation_id: str | None
    axis: str


class AffineRows:
    """CSR rows, with a shared source/family and an explicit actual time index.

    Raw terms retain the binder's stable global-key order (including aliases)
    for the final row consumer. Analysis combines adjacent aliases in exactly
    the former dict-addition order, without retaining another copy of terms.
    """

    SENSES = ("eq", "le", "ge")

    def __init__(self, bounds: BoundTable) -> None:
        self.bounds = bounds
        self.offsets = array("Q", [0])
        self.columns = array("I")
        self.coefficients = array("d")
        self.constants = array("d")
        self.senses = bytearray()
        self.origin_slots = array("I")
        self.indices = array("I")
        self.origins: list[RelationOrigin] = []
        self._origin_slots: dict[tuple, int] = {}

    def __len__(self) -> int:
        return len(self.constants)

    def append(
        self, terms: Iterable[tuple[str, float]], constant: float, sense: str,
        source: SourceLocation | None, relation_id: str | None, axis: str, index: int,
    ) -> int:
        key = (source, relation_id, axis)
        origin = self._origin_slots.get(key)
        if origin is None:
            origin = len(self.origins)
            self._origin_slots[key] = origin
            self.origins.append(RelationOrigin(
                source, self.bounds.source_slot(source), relation_id, axis,
            ))
        row = len(self)
        for name, coefficient in terms:
            self.columns.append(self.bounds.ensure(name))
            self.coefficients.append(coefficient)
        self.offsets.append(len(self.columns))
        self.constants.append(constant)
        self.senses.append(self.SENSES.index(sense))
        self.origin_slots.append(origin)
        self.indices.append(index)
        return row

    def terms(self, row: int) -> Iterator[tuple[int, float]]:
        previous = -1
        total = 0.0
        for at in range(self.offsets[row], self.offsets[row + 1]):
            slot, coefficient = self.columns[at], self.coefficients[at]
            if slot != previous:
                if total:
                    yield previous, total
                previous, total = slot, 0.0
            total += coefficient
        if total:
            yield previous, total

    def named_terms(self, row: int) -> Iterator[tuple[str, float]]:
        for slot, coefficient in self.terms(row):
            yield self.bounds.names[slot], coefficient

    def raw_terms(self, row: int) -> Iterator[tuple[tuple[str, int], float]]:
        for at in range(self.offsets[row], self.offsets[row + 1]):
            yield (self.bounds.names[self.columns[at]], 0), self.coefficients[at]

    def origin(self, row: int) -> RelationOrigin:
        return self.origins[self.origin_slots[row]]


class SparseRowIndex:
    """Compact symbol -> row-ordinal adjacency in the given stable row order."""

    def __init__(
        self, rows: AffineRows, row_ids: array,
        checkpoint: Callable[[], None] | None,
    ) -> None:
        self.offsets = array("Q", [0]) * (len(rows.bounds) + 1)
        for row in row_ids:
            if checkpoint is not None:
                checkpoint()
            for slot, _ in rows.terms(row):
                self.offsets[slot + 1] += 1
        for slot in range(len(rows.bounds)):
            if checkpoint is not None and slot % 4096 == 0:
                checkpoint()
            self.offsets[slot + 1] += self.offsets[slot]
        self.entries = array("I", [0]) * self.offsets[-1]
        cursor = self.offsets[:-1]
        for ordinal, row in enumerate(row_ids):
            if checkpoint is not None:
                checkpoint()
            for slot, _ in rows.terms(row):
                self.entries[cursor[slot]] = ordinal
                cursor[slot] += 1

    def rows_for(self, slot: int) -> Iterator[int]:
        for at in range(self.offsets[slot], self.offsets[slot + 1]):
            yield self.entries[at]


class TechnicalRowCache:
    """Per-family row slots, not a retained tuple/DTO for every time step."""

    def __init__(self, instance: str, relation_id: str) -> None:
        self.instance = instance
        self.relation_id = relation_id
        self.rows = array("q")  # -1: bind later, -2: empty exact row set


def numeric_bytes(arrays: Iterable[array | bytearray]) -> int:
    """Payload metric for regression evidence, excluding shared names/sources."""
    return sum(len(value) * (value.itemsize if isinstance(value, array) else 1)
               for value in arrays)
