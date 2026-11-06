"""Small D2 counterexamples; annual benchmarks run explicitly, not in pytest."""
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import pytest
from stage_d_diagnosis import check_original, solve_diagnostic, technical_mutex_pairs

from iesplan.computation import write_mps
from iesplan.computation.generators.linear import (
    LinearConstraint as Row,
)
from iesplan.computation.generators.linear import (
    LinearExpression as Expr,
)
from iesplan.computation.generators.linear import (
    LinearModel as Model,
)
from iesplan.computation.generators.linear import (
    LinearVariable as Var,
)


def expression(**terms):
    return Expr(tuple(((name, 0), a) for name, a in terms.items()))


def sources(*binaries):
    return {"sources": {"user_equation": {"kind": "technical"}},
            "groups": [{"source": "user_equation", "role": "column:bound_relation",
                        "targets": list(binaries)}]}


def solve(model, tmp_path, mode="exact", source=None):
    mps, _ = write_mps(model)
    # Native HiGHS uses a process-wide scheduler. Fresh children match the
    # explicit benchmark and do not inherit other pytest tests' thread counts.
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        return pool.submit(solve_diagnostic, model, mps.encode(), source or sources(), mode=mode,
                           seconds=5, log_path=tmp_path / f"{mode}.log").result(timeout=30)


def test_source_and_algebra_not_column_names_identify_mutex():
    model = Model((Var("switch", 0, 1, "binary"), Var("a", 0, 2), Var("b", 0, 3)),
                  (Row("first", expression(a=1, switch=-2), "le", 0),
                   Row("second", expression(b=1, switch=3), "le", 3)))
    before = model.to_dict()
    pairs = technical_mutex_pairs(model, sources("switch"))
    assert pairs == [{"binary": "switch", "x": "a", "y": "b", "source": "user_equation"}]
    result = check_original(model, {"switch": .5, "a": 1, "b": 1}, pairs)
    assert result["fractional_integer_columns"] == 1
    assert result["positive_both_factors_by_source"] == {"user_equation": 1}
    assert result["raw_row_violations_above_tolerance"] == 0
    no_overlap = check_original(model, {"switch": .5, "a": 1, "b": 0}, pairs)
    assert no_overlap["fractional_integer_columns"] == 1
    assert no_overlap["positive_both_factors_count"] == 0
    assert model.to_dict() == before
    other = sources("switch")
    other["sources"]["user_equation"]["kind"] = "finance"
    assert technical_mutex_pairs(model, other) == []


def test_row_and_bound_residuals_and_fractionality_are_separate():
    model = Model((Var("a", 0, 2), Var("z", 0, 1, "binary")),
                  (Row("equality", expression(a=1), "eq", 1),
                   Row("minimum", expression(a=1), "ge", 1)))
    result = check_original(model, {"a": -.5, "z": .25}, [])
    assert result["maximum_bound_violation"] == .5
    assert result["maximum_raw_row_violation"] == 1.5
    assert result["raw_row_violations_above_tolerance"] == 2
    assert result["fractional_integer_columns"] == 1


def test_fractional_auxiliary_does_not_alone_prove_physical_mutex_violation(tmp_path):
    model = Model((Var("a", 0, 2), Var("b", 0, 3), Var("switch", 0, 1, "binary")),
                  (Row("first", expression(a=1, switch=-2), "le", 0),
                   Row("second", expression(b=1, switch=3), "le", 3),
                   Row("fixed_a", expression(a=1), "eq", 1)), expression(b=1))
    relaxed = solve(model, tmp_path, "relaxation", sources("switch"))
    assert relaxed["model_status"] == "HighsModelStatus.kOptimal"
    assert not relaxed["is_original_milp_incumbent"]
    assert relaxed["original_matrix_checks"]["positive_both_factors_count"] == 0
    assert relaxed["objective_internal_without_offset"] == pytest.approx(0)
    assert (tmp_path / "relaxation.log").read_text()
    exact = solve(model, tmp_path, source=sources("switch"))
    assert exact["is_original_milp_incumbent"]
    assert exact["original_matrix_checks"]["fractional_integer_columns"] == 0


def test_buy_price_above_sell_does_not_override_user_minimum_purchase(tmp_path):
    # User requires buying >=1, demand is zero. Relaxation can resell at a
    # loss (buy price 2 > sell price 1); the exact mutual exclusion is infeasible.
    model = Model((Var("buy", 0, 2), Var("sell", 0, 2), Var("z", 0, 1, "binary")),
                  (Row("buy_mode", expression(buy=1, z=-2), "le", 0),
                   Row("sell_mode", expression(sell=1, z=2), "le", 2),
                   Row("balance", expression(buy=1, sell=-1), "eq", 0),
                   Row("contract", expression(buy=1), "ge", 1)), expression(buy=2, sell=-1))
    relaxed = solve(model, tmp_path, "relaxation", sources("z"))
    exact = solve(model, tmp_path, source=sources("z"))
    assert relaxed["objective_internal_without_offset"] == pytest.approx(1)
    assert relaxed["original_matrix_checks"]["positive_both_factors_count"] == 1
    assert exact["model_status"] == "HighsModelStatus.kInfeasible"
    assert not exact["has_feasible_solution_of_experiment"]
    assert exact["original_matrix_checks"] is None


def test_losses_do_not_make_forced_surplus_and_full_battery_mutex_redundant(tmp_path):
    # Full initial battery, no terminal condition. A rigid generator's surplus
    # .1 must be absorbed; simultaneous charge/discharge burns it in the LP.
    model = Model((Var("charge", 0, 2), Var("discharge", 0, 2),
                   Var("stored", 0, 1), Var("z", 0, 1, "binary")),
                  (Row("charge_mode", expression(charge=1, z=-2), "le", 0),
                   Row("discharge_mode", expression(discharge=1, z=2), "le", 2),
                   Row("surplus", expression(charge=1, discharge=-1), "eq", .1),
                   Row("real_state", expression(stored=1, charge=-.90, discharge=1/.98), "eq", 1)),
                  expression(charge=1))
    relaxed = solve(model, tmp_path, "relaxation", sources("z"))
    exact = solve(model, tmp_path, source=sources("z"))
    assert relaxed["model_status"] == "HighsModelStatus.kOptimal"
    assert relaxed["original_matrix_checks"]["positive_both_factors_count"] == 1
    assert exact["model_status"] == "HighsModelStatus.kInfeasible"


def test_identical_inputs_do_not_allow_reusing_first_period_state_trajectory(tmp_path):
    # Same demand and prices each period, initial energy 1. The real two-period
    # trajectory buys 1 in total. Resetting initial energy each period buys 0.
    def model(reset):
        variables, rows = [], []
        for t in (0, 1):
            variables += [Var(f"g{t}", 0, 1), Var(f"d{t}", 0, 1), Var(f"s{t}", 0, 1)]
            rows.append(Row(f"balance{t}", expression(**{f"g{t}": 1, f"d{t}": 1}), "eq", 1))
            terms = {f"s{t}": 1, f"d{t}": 1}
            if t and not reset:
                terms[f"s{t-1}"] = -1
            rows.append(Row(f"state{t}", expression(**terms), "eq", 1 if reset or not t else 0))
        return Model(tuple(variables), tuple(rows), expression(g0=1, g1=1))
    assert solve(model(False), tmp_path)["objective_internal_without_offset"] == pytest.approx(1)
    assert solve(model(True), tmp_path)["objective_internal_without_offset"] == pytest.approx(0)
