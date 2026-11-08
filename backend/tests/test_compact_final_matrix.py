"""M2: exact final rows, one shared symbol table and no retained DTO graph."""
from __future__ import annotations

import gc
import weakref
from array import array

import pytest
from test_compact_bound_storage import _chain

from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.generators import SparseModel
from iesplan.computation.generators.bound_inference import BoundInferenceEngine
from iesplan.computation.generators.compiler_ir import CompilerAnalysis, IndexDomain, SourceLocation
from iesplan.computation.generators.linear import (
    LinearConstraint,
    LinearExpression,
    LinearModel,
    LinearVariable,
)
from iesplan.computation.generators.matrix import ColumnEntries, MatrixColumns, MatrixRows
from iesplan.computation.generators.mps import write_mps
from iesplan.computation.generators.relation_storage import AffineRows, BoundTable
from iesplan.computation.generators.sources import SourceTrace


def _public_model():
    return LinearModel(
        variables=(LinearVariable("z", 0.0, 1.0, "binary"),
                   LinearVariable("x"), LinearVariable("unused"),
                   LinearVariable("fixed", 2.0, 2.0),
                   LinearVariable("upper", None, 0.0),
                   LinearVariable("integer", -1.0, 3.0, "binary"),
                   LinearVariable("zero", 0.0, 0.0)),
        constraints=(
            LinearConstraint("r2", LinearExpression(
                ((("z", 0), 1.0), (("x", 0), -2.0)), 0.0), "ge", -4.0),
            LinearConstraint("r10", LinearExpression(
                ((("x", 0), 2.0), (("x", 0), -1.0), (("zero", 0), 0.0)), 2.0), "le", 7.0),
        ),
        objective=LinearExpression(((("x", 0), 0.5), (("z", 0), -3.0)), 1.25),
        sense="maximize",
    )


# Captured from the unchanged writer in pies015clean-backend:21d2f9e.
EXPECTED_MPS = """NAME          IES_MILP
ROWS
 N  OBJ
 L  r10
 G  r2
COLUMNS
    fixed  OBJ  0.0
    INTORG    'MARKER'                 'INTORG'
    integer  OBJ  0.0
    INTEND    'MARKER'                 'INTEND'
    unused  OBJ  0.0
    upper  OBJ  0.0
    x  OBJ  0.5  r10  2.0
    x  r10  -1.0  r2  -2.0
    z  OBJ  -3.0  r2  1.0
    zero  OBJ  0.0
RHS
    RHS1  r10  5.0
    RHS1  r2  -4.0
BOUNDS
 FX       BND1  fixed  2.0
 LO       BND1  integer  -1.0
 UP       BND1  integer  3.0
 FR       BND1  unused
 MI       BND1  upper
 FR       BND1  x
 BV       BND1  z
 FX       BND1  zero  0.0
OBJSENSE
 MAX
ENDATA
"""


def test_exact_mps_order_bounds_alias_terms_and_objective_offset(monkeypatch):
    public = _public_model()
    matrix = SparseModel.from_public(public)
    assert matrix.to_dict() == public.to_dict()
    assert matrix == public
    assert write_mps(public) == (EXPECTED_MPS, 1.25)

    def no_dto(*args):
        raise AssertionError("production writer must consume numeric matrix data")

    monkeypatch.setattr(MatrixRows, "__getitem__", no_dto)
    monkeypatch.setattr(MatrixColumns, "__getitem__", no_dto)
    assert write_mps(matrix) == (EXPECTED_MPS, 1.25)


def test_explicit_solver_positions_and_stable_numeric_transpose():
    matrix = SparseModel.from_public(_public_model())
    assert [matrix.column_position(name) for name in ("z", "x", "fixed")] == [5, 4, 0]
    assert [matrix.rows.names[i] for i in matrix.row_order] == ["r10", "r2"]
    entries = ColumnEntries(matrix, lambda: None)
    assert isinstance(entries.rows, array)
    assert isinstance(entries.values, array)
    lo, hi = entries.offsets[4:6]
    assert [(entries.rows[i], entries.values[i]) for i in range(lo, hi)] == [
        (-1, 0.5), (0, 2.0), (0, -1.0), (1, -2.0),
    ]


def test_analysis_rows_are_reused_without_per_slot_final_objects():
    gc.collect()
    before = len(gc.get_objects())
    count = 20000
    inv, family = _chain(count)
    symbols = BoundTable()
    columns = MatrixColumns(symbols)
    rows = MatrixRows(AffineRows(symbols.symbols))
    engine = BoundInferenceEngine(inv, columns, bounds=symbols, rows=rows.data)
    original_nonzeros = len(engine.rows.coefficients)
    for step, row in enumerate(engine.technical_rows[family.identity].rows):
        columns.declare(f"state_{step}", 0.0, float(count * 2), "continuous")
        rows.append_existing(f"cell__state__t{step}", row)
    matrix = SparseModel.freeze(columns, rows, LinearExpression(), "minimize")
    gc.collect()
    assert len(gc.get_objects()) - before < 200
    assert rows.data is engine.rows
    assert columns.symbols is engine.bounds.symbols
    assert len(engine.rows.coefficients) == original_nonzeros == 2 * count - 1
    assert len(matrix.variables) == len(matrix.constraints) == count
    assert len(rows.names) == count
    assert rows.row_ids[256] == engine.technical_rows[family.identity].rows[256]
    bound_state = weakref.ref(engine.bounds)
    del engine, symbols
    gc.collect()
    assert bound_state() is None  # The final matrix does not own propagation buffers.
    assert matrix.columns.symbols.names[matrix.columns.slots[0]] == "state_0"


def test_real_project_does_not_recover_per_step_analysis_dtos(monkeypatch):
    from test_highs_milp_provider import _campus_inputs

    from iesplan.computation.generators.project import build_project_model

    def no_dto(*args):
        raise AssertionError("project generation must reuse numeric analysis rows")

    monkeypatch.setattr(CompilerAnalysis, "technical_rows", no_dto)
    model, units, unit = build_project_model(*_campus_inputs())
    assert isinstance(model, SparseModel)
    assert len(model.columns) == 131401
    assert len(model.rows) == 122641
    assert len(model.row_order) == len(model.row_positions) == len(model.rows)
    assert len(units) > 0 and unit == "CNY"


def test_post_analysis_auxiliary_symbols_do_not_change_analysis_or_old_slots():
    inv, family = _chain(4)
    symbols = BoundTable()
    columns = MatrixColumns(symbols)
    rows = MatrixRows(AffineRows(symbols.symbols))
    analysis = CompilerAnalysis(inv, columns, bounds=symbols, rows=rows.data)
    old_slots = dict(symbols.slots)
    columns.declare("aux", 0.0, 1.0, "binary")
    assert all(symbols.slots[name] == slot for name, slot in old_slots.items())
    assert analysis.variable_bounds("aux") == (0.0, 1.0)
    assert analysis.inference.bound_affine({"aux": 1.0}, 0.0, side="max") is None
    assert analysis.variable_bounds("state_3") == (3.5, 3.5)
    assert analysis.technical_row_slot(family.identity, 0) >= 0


def test_source_targets_are_numeric_and_resolve_from_same_declarations():
    trace = SourceTrace()
    symbols = BoundTable()
    columns = MatrixColumns(symbols, trace)
    rows = MatrixRows(AffineRows(symbols.symbols), trace)
    trace.targets = {"column": symbols.names, "row": rows.names}
    trace.register("model.state", "technical", SourceLocation("equations.state"), IndexDomain("step", 2))
    for step, name in enumerate(("z", "a")):
        trace.activate("model.state", "declared", "step", step)
        columns.declare(name, 0.0, None, "continuous")
        trace.activate("model.state", "equation", "step", step)
        rows.append(LinearConstraint(f"row_{name}", LinearExpression((((name, 0), 1.0),))))
    matrix = SparseModel.freeze(columns, rows, LinearExpression(), "minimize")
    for group in trace.groups.values():
        assert isinstance(group["targets"], array)
        assert isinstance(group["indices"], array)
    assert [group["targets"] for group in trace.export([])["groups"]] == [["z", "a"], ["row_z", "row_a"]]
    assert [matrix.column_position(name) for name in ("z", "a")] == [1, 0]


def test_analysis_tautology_can_keep_its_explicit_final_row():
    columns = MatrixColumns(BoundTable())
    columns.declare("free", None, None, "continuous")
    rows = MatrixRows(AffineRows(columns.symbols))
    rows.append_semantic("empty", -2, constant=3.0, sense="le", rhs=5.0)
    model = SparseModel.freeze(columns, rows, LinearExpression(), "minimize")
    assert model.constraints[0] == LinearConstraint("empty", LinearExpression((), 3.0), "le", 5.0)
    assert " L  empty\n" in write_mps(model)[0]
    assert "    RHS1  empty  2.0\n" in write_mps(model)[0]


def test_numeric_transpose_and_writer_cooperate_with_stop():
    matrix = SparseModel.from_public(_public_model())
    with pytest.raises(ComputationUnavailableError) as error:
        write_mps(matrix, stop=lambda: "cancelled")
    assert error.value.reason == "cancelled"
    with pytest.raises(RuntimeError, match="stop transpose"):
        ColumnEntries(matrix, lambda: (_ for _ in ()).throw(RuntimeError("stop transpose")))


def test_source_symbol_without_solver_declaration_is_not_last_column():
    symbols = BoundTable()
    symbols.ensure("source_only")
    columns = MatrixColumns(symbols)
    columns.declare("x", 0.0, None, "continuous")
    rows = MatrixRows(AffineRows(symbols.symbols))
    rows.append(LinearConstraint("bad_ref", LinearExpression(((("source_only", 0), 1.0),))))
    matrix = SparseModel.freeze(columns, rows, LinearExpression(), "minimize")
    with pytest.raises(KeyError, match="source_only"):
        write_mps(matrix)
