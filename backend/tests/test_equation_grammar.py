"""core.equation_grammar 公共语法契约测试（devices 与 modeling 共享）。

覆盖：lhs/rhs 拆分、引用原子与时间索引提取、未来引用拒绝、
非法字符、循环引用检测与自环递推合法性。
"""

from __future__ import annotations

import pytest

from iesplan.core.equation_grammar import (
    EquationSyntaxError,
    check_cycles,
    reference_atoms,
    split_relation,
)


class TestSplitRelation:
    def test_basic_split(self):
        assert split_relation("a[t] = b[t] * 2") == ("a[t]", "b[t] * 2")

    def test_multiple_equals_rejected(self):
        with pytest.raises(EquationSyntaxError):
            split_relation("a = b = c")

    def test_no_equals_rejected(self):
        with pytest.raises(EquationSyntaxError):
            split_relation("a[t] + b[t]")

    def test_empty_side_rejected(self):
        with pytest.raises(EquationSyntaxError):
            split_relation("a[t] = ")


class TestReferenceAtoms:
    def test_plain_atoms(self):
        atoms, offsets = reference_atoms("a + b * 2 - c", "r1")
        assert atoms == ("a", "b", "c")
        assert offsets == (0, 0, 0)

    def test_time_indices(self):
        atoms, offsets = reference_atoms("soc[t] = soc[t-1] + charge[t]", "r1")
        assert atoms == ("soc", "soc", "charge")
        assert offsets == (0, -1, 0)

    def test_past_offset_only(self):
        with pytest.raises(EquationSyntaxError) as exc:
            reference_atoms("a[t+1] + b[t]", "r1")
        assert "禁止未来引用" in str(exc.value)

    def test_arbitrary_index_rejected(self):
        with pytest.raises(EquationSyntaxError):
            reference_atoms("a[x] + b", "r1")

    def test_illegal_char_rejected(self):
        with pytest.raises(EquationSyntaxError):
            reference_atoms("a; import os", "r1")

    def test_function_call_rejected(self):
        with pytest.raises(EquationSyntaxError):
            reference_atoms("f(x)", "r1")

    def test_numbers_and_ops_ok(self):
        atoms, offsets = reference_atoms("1 + 2 * a - (b / 4)", "r1")
        assert atoms == ("a", "b")


class TestCheckCycles:
    def test_no_cycle(self):
        edges = [("a", ("b", "c")), ("b", ("c",)), ("c", ())]
        check_cycles(("a", "b", "c"), edges)  # 不抛异常

    def test_cycle_detected(self):
        edges = [("a", ("b",)), ("b", ("a",))]
        with pytest.raises(EquationSyntaxError) as exc:
            check_cycles(("a", "b"), edges)
        assert "循环引用" in str(exc.value)

    def test_self_reference_is_state_transition(self):
        # soc[t] = soc[t-1] + ... 自环是合法状态递推，不构成循环
        edges = [("soc", ("soc", "charge"))]
        check_cycles(("soc",), edges)

    def test_transitive_cycle(self):
        edges = [("a", ("b",)), ("b", ("c",)), ("c", ("a",))]
        with pytest.raises(EquationSyntaxError):
            check_cycles(("a", "b", "c"), edges)
