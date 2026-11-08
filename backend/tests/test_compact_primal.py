"""M4 主解读取起步：原文法及二进制精度不变，避免文本／行／浮点对象叠加。"""

from __future__ import annotations

from array import array

import pytest
from test_highs_result_adapter import SHIM_OPTIMAL, _adapt, _receipt

from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.result_adapters.highs import _parse_solution


class NoWholeText(bytes):
    def decode(self, *args, **kwargs):
        raise AssertionError("不得完整解码主解")

    def splitlines(self, *args, **kwargs):
        raise AssertionError("不得物化完整主解行列表")


def _solution(values):
    return (
        "HIGHS_SOLUTION_V1\n"
        "model_status: HighsModelStatus.kOptimal\n"
        "has_feasible_solution: true\n"
        "objective_value: 0.0\n"
        "best_bound: 0.0\n"
        "relative_gap: 0.0\n"
        f"num_columns: {len(values)}\n"
        "primal:\n"
        + "".join(f"{index}: {value!r}\n" for index, value in enumerate(values))
    ).encode()


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_incremental_solution_keeps_public_outputs_and_line_endings(ending):
    expected = _adapt(None, _receipt(), SHIM_OPTIMAL.encode())
    raw = NoWholeText(SHIM_OPTIMAL.rstrip("\n").replace("\n", ending).encode())
    actual = _adapt(None, _receipt(), raw)
    assert actual == expected


def test_compact_primals_keep_column_order_and_exact_binary64_values():
    values = [0.0, -0.0, 5e-324, -5e-324, 1.0 / 3.0, -1.7e308, 1.7e308]
    parsed = _parse_solution(NoWholeText(_solution(values)))
    primals = parsed["primals"]
    assert isinstance(primals, array)
    assert primals.typecode == "d"
    assert primals.itemsize == 8
    assert [value.hex() for value in primals] == [value.hex() for value in values]
    assert parsed["objective"] == 0.0
    assert parsed["best_bound"] == 0.0
    assert parsed["relative_gap"] == 0.0
    assert parsed["has_feasible_solution"] is True


@pytest.mark.parametrize("count", [0, 1, 100_000])
def test_solution_retains_one_unboxed_value_per_column(count):
    values = [float(index) for index in range(count)]
    parsed = _parse_solution(NoWholeText(_solution(values)))
    primals = parsed["primals"]
    assert isinstance(primals, array)
    assert len(primals) == count
    assert primals.buffer_info()[1] * primals.itemsize == 8 * count
    assert all(actual == expected for actual, expected in zip(primals, values, strict=True))


def test_invalid_utf8_after_several_read_buffers_is_still_protocol_failure():
    raw = _solution([1.0] * 20_000) + b"\xff\n"
    with pytest.raises(ComputationUnavailableError, match="UTF-8") as error:
        _parse_solution(raw)
    assert error.value.reason == "result-protocol-failure"
