"""Final matrix over the same numeric rows/symbols used by analysis.

Small public relation DTOs are accepted at the append boundary. Production
retains numeric data, not the DTO graph; inspection views create one DTO at a
time. Source slots, declaration order and solver positions remain distinct.
"""
from __future__ import annotations

import math
from array import array
from collections.abc import Iterator, MutableMapping, Sequence
from dataclasses import dataclass

from iesplan.computation.generators.linear import (
    LinearConstraint,
    LinearExpression,
    LinearModel,
    LinearVariable,
)
from iesplan.computation.generators.relation_storage import AffineRows, BoundTable


class MatrixColumns(MutableMapping[str, LinearVariable]):
    def __init__(self, symbols: BoundTable, trace=None) -> None:
        self.symbols = symbols.symbols
        self.trace = trace
        self.slots = array("I")
        self.declarations = array("q")
        self.lower = array("d")
        self.upper = array("d")
        self.kinds = bytearray()

    def declare(self, name, lower, upper, kind) -> str:
        slot = self.symbols.ensure(name)
        missing = len(self.symbols) - len(self.declarations)
        if missing == 1:
            self.declarations.append(-1)
        elif missing > 1:
            self.declarations.extend(array("q", [-1]) * missing)
        position = self.declarations[slot]
        if position < 0:
            position = len(self.slots)
            self.declarations[slot] = position
            self.slots.append(slot)
            self.lower.append(math.nan)
            self.upper.append(math.nan)
            self.kinds.append(0)
            if self.trace is not None:
                self.trace.record(slot, "column")
        self.lower[position] = math.nan if lower is None else lower
        self.upper[position] = math.nan if upper is None else upper
        self.kinds[position] = kind == "binary"
        return self.symbols.names[slot]

    def values_at(self, position):
        lo, hi = self.lower[position], self.upper[position]
        return (self.symbols.names[self.slots[position]],
                None if math.isnan(lo) else lo, None if math.isnan(hi) else hi,
                "binary" if self.kinds[position] else "continuous")

    def __getitem__(self, name):
        slot = self.symbols.slots[name]
        return LinearVariable(*self.values_at(self.declaration(slot)))

    def declaration(self, slot):
        if slot >= len(self.declarations) or self.declarations[slot] < 0:
            raise KeyError(self.symbols.names[slot])
        return self.declarations[slot]

    def __contains__(self, name):
        slot = self.symbols.slots.get(name)
        return (slot is not None and slot < len(self.declarations)
                and self.declarations[slot] >= 0)

    def __setitem__(self, name, variable):
        self.declare(name, variable.lower, variable.upper, variable.kind)

    def __delitem__(self, name):
        raise TypeError("matrix columns are append-only")

    def __iter__(self):
        return (self.symbols.names[slot] for slot in self.slots)

    def __len__(self):
        return len(self.slots)


class MatrixRows(Sequence[LinearConstraint]):
    def __init__(self, data: AffineRows, trace=None) -> None:
        self.data = data
        self.trace = trace
        self.names: list[str] = []
        self.row_ids = array("I")
        self.constants = array("d")
        self.rhs = array("d")

    def append_existing(self, name, row, *, constant=None, rhs=0.0):
        if self.trace is not None:
            self.trace.record(len(self.names), "row")
        self.names.append(name)
        self.row_ids.append(row)
        self.constants.append(self.data.constants[row] if constant is None else constant)
        self.rhs.append(rhs)

    def append(self, row):
        index = self.data.append(
            ((name, value) for (name, _), value in row.expression.terms),
            row.expression.constant, row.sense, None, None, "scalar", 0,
        )
        self.append_existing(row.name, index, rhs=row.rhs)

    def append_semantic(self, name, row, *, constant=0.0, sense="eq", rhs=0.0):
        # A source tautology is not an analysis operation, but an explicit
        # planning row still occupies its original final row/name position.
        if row == -2:
            row = self.data.append((), constant, sense, None, None, "scalar", 0)
        self.append_existing(name, row, constant=constant, rhs=rhs)

    def extend(self, rows):
        for row in rows:
            self.append(row)

    def __len__(self):
        return len(self.names)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(len(self))))
        row = self.row_ids[index]
        return LinearConstraint(
            self.names[index], LinearExpression(tuple(self.data.raw_terms(row)), self.constants[index]),
            self.data.SENSES[self.data.senses[row]], self.rhs[index],
        )


class _OrderedView(Sequence):
    def __init__(self, values, order):
        self.values, self.order = values, order

    def __len__(self):
        return len(self.order)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(len(self))))
        return self.values[self.order[index]]

    def __eq__(self, other):
        return (isinstance(other, Sequence) and len(self) == len(other)
                and all(a == b for a, b in zip(self, other, strict=False)))


class _VariableView(Sequence):
    def __init__(self, columns):
        self.columns = columns

    def __len__(self):
        return len(self.columns)

    def __getitem__(self, index):
        return LinearVariable(*self.columns.values_at(index))


@dataclass(frozen=True, slots=True, eq=False)
class SparseModel:
    columns: MatrixColumns
    rows: MatrixRows
    column_order: array
    row_order: array
    column_positions: array
    row_positions: array
    objective: LinearExpression
    sense: str

    @classmethod
    def freeze(cls, columns, rows, objective, sense, checkpoint=None):
        # One normative lexical ordering, not per-consumer name reconstruction.
        if checkpoint is not None:
            checkpoint()
        column_order = array("I", sorted(
            range(len(columns)), key=lambda i: columns.symbols.names[columns.slots[i]],
        ))
        row_order = array("I", sorted(range(len(rows)), key=rows.names.__getitem__))
        column_positions = array("I", [0]) * len(columns)
        row_positions = array("I", [0]) * len(rows)
        for order, positions in ((column_order, column_positions), (row_order, row_positions)):
            for position, slot in enumerate(order):
                if checkpoint is not None and position % 4096 == 0:
                    checkpoint()
                positions[slot] = position
        return cls(columns, rows, column_order, row_order,
                   column_positions, row_positions, objective, sense)

    @classmethod
    def from_public(cls, model: LinearModel):
        columns = MatrixColumns(BoundTable())
        rows = MatrixRows(AffineRows(columns.symbols))
        for var in model.variables:
            columns[var.name] = var
        rows.extend(model.constraints)
        return cls.freeze(columns, rows, model.objective, model.sense)

    @property
    def variables(self):
        return _OrderedView(_VariableView(self.columns), self.column_order)

    @property
    def constraints(self):
        return _OrderedView(self.rows, self.row_order)

    def column_position(self, name):
        return self.column_positions[self.columns.declaration(self.columns.symbols.slots[name])]

    def to_dict(self):
        return {"variables": [v.to_dict() for v in self.variables],
                "constraints": [r.to_dict() for r in self.constraints],
                "objective": self.objective.to_dict(), "sense": self.sense}

    def __eq__(self, other):
        if not isinstance(other, (SparseModel, LinearModel)):
            return NotImplemented
        return (self.sense == other.sense and self.objective == other.objective
                and self.variables == other.variables and self.constraints == other.constraints)

    def row_terms(self, declaration: int) -> Iterator[tuple[int, float]]:
        row = self.rows.row_ids[declaration]
        data = self.rows.data
        for at in range(data.offsets[row], data.offsets[row + 1]):
            slot = data.columns[at]
            yield self.column_positions[self.columns.declaration(slot)], data.coefficients[at]


class ColumnEntries:
    """Stable numeric transpose; no Python tuple/list per matrix nonzero."""

    def __init__(self, model: SparseModel, checkpoint):
        self.offsets = array("Q", [0]) * (len(model.columns) + 1)
        for (name, _), value in model.objective.terms:
            if value:
                self.offsets[model.column_position(name) + 1] += 1
        for position, declaration in enumerate(model.row_order):
            if position % 128 == 0:
                checkpoint()
            for column, value in model.row_terms(declaration):
                if value:
                    self.offsets[column + 1] += 1
        for column in range(len(model.columns)):
            if column % 4096 == 0:
                checkpoint()
            self.offsets[column + 1] += self.offsets[column]
        self.rows = array("q", [0]) * self.offsets[-1]  # -1 denotes the objective row
        self.values = array("d", [0.0]) * self.offsets[-1]
        cursor = self.offsets[:-1]

        def append(column, row, value):
            at = cursor[column]
            self.rows[at], self.values[at] = row, value
            cursor[column] += 1

        for (name, _), value in model.objective.terms:
            if value:
                append(model.column_position(name), -1, value)
        for position, declaration in enumerate(model.row_order):
            if position % 128 == 0:
                checkpoint()
            for column, value in model.row_terms(declaration):
                if value:
                    append(column, declaration, value)
