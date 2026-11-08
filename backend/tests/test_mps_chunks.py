"""MPS单一记录writer的分块输出；不代表Bundle全链已迁移。"""

from __future__ import annotations

import pytest
from test_compact_final_matrix import EXPECTED_MPS, _public_model

from iesplan.computation import iter_mps_chunks
from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.generators import mps
from iesplan.computation.generators.linear import (
    LinearConstraint,
    LinearExpression,
    LinearModel,
    LinearVariable,
)
from iesplan.computation.generators.matrix import SparseModel


@pytest.mark.parametrize("sparse", [False, True])
def test_chunks_match_existing_exact_mps_fixture_and_replay(sparse):
    model = _public_model()
    if sparse:
        model = SparseModel.from_public(model)
    assert b"".join(iter_mps_chunks(model)) == EXPECTED_MPS.encode()
    assert b"".join(iter_mps_chunks(model)) == EXPECTED_MPS.encode()
    assert mps.write_mps(model) == (EXPECTED_MPS, 1.25)


def _many_rows():
    expression = LinearExpression(((("x", 0), 1.0),))
    return SparseModel.from_public(LinearModel(
        variables=(LinearVariable("x", 0.0, 1.0),),
        constraints=tuple(
            LinearConstraint(f"row_{index:06}", expression, "le", 1.0)
            for index in range(10_000)
        ),
        objective=expression,
    ))


def test_first_chunk_precedes_numeric_transpose_and_does_not_use_full_text(monkeypatch):
    model = _many_rows()

    def later_only(*args):
        raise AssertionError("COLUMNS must not be built before ROWS are consumed")

    monkeypatch.setattr(mps, "ColumnEntries", later_only)
    monkeypatch.setattr(mps, "write_mps", later_only)
    chunks = iter_mps_chunks(model)
    first = next(chunks)
    assert len(first) == 65_536
    assert first.startswith(b"NAME          IES_MILP\nROWS\n")
    assert b"COLUMNS\n" not in first
    chunks.close()


def test_chunks_write_directly_to_file_with_bounded_buffers(tmp_path):
    model = _many_rows()
    target = tmp_path / "problem.mps"
    size = count = 0
    with target.open("wb") as output:
        for chunk in iter_mps_chunks(model):
            assert 0 < len(chunk) <= 65_536
            output.write(chunk)
            size += len(chunk)
            count += 1
    assert count > 2
    assert target.stat().st_size == size
    assert target.read_text() == mps.write_mps(model)[0]


def test_utf8_record_larger_than_buffer_can_cross_chunk_boundaries():
    name = "量" * 30_000
    model = LinearModel(variables=(LinearVariable(name),), objective=LinearExpression())
    chunks = list(iter_mps_chunks(model))
    assert len(chunks) > 2
    assert all(0 < len(chunk) <= 65_536 for chunk in chunks)
    raw = b"".join(chunks)
    assert raw.decode() == mps.write_mps(model)[0]
    assert f"    {name}  OBJ  0.0\n" in raw.decode()


@pytest.mark.parametrize("reason", ["cancelled", "fenced"])
def test_cancel_and_fencing_remain_visible_during_partial_consumption(reason):
    stopped = False
    chunks = iter_mps_chunks(_many_rows(), stop=lambda: reason if stopped else None)
    assert len(next(chunks)) == 65_536
    stopped = True
    with pytest.raises(ComputationUnavailableError) as error:
        next(chunks)
    assert error.value.reason == reason
