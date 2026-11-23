"""S2 求解器内存按需技术互斥恢复契约测试（仅在 Docker 内运行真实 highspy 1.7.2 与 ies-highs-solver）。

覆盖：
1. 0 冲突 1 轮无损收敛：单次 readModel、valid_relaxation + original_model_verified=True、
   HighsResultAdapter 发布 solved、与原 MILP 最优值一致。
2. 真实多轮双正冲突恢复：Round 0 冲突对 A -> 原位恢复 A 暴露对 B 新冲突 -> Round 1
   原位恢复 B -> Round 2 0 冲突收敛，剩余松弛对精确重构 z 并通过原模型核查。
3. 熔断回退原 MILP（max_rounds、round_idx>=2 冲突不收敛、pairs>=100 恢复超 50%）：
   同句柄原位恢复全部剩余互斥对并回退原 MILP 达到精确最优。
4. 恢复中途单调预算耗尽：不续发求解轮次，保全已有已核查最优可行解且不泄漏冲突松弛解。
5. CLI 子进程 --acceleration_mode on_demand_mutex --acceleration_file input/acceleration.json --threads 2
   及 pIES campus 真实模型同模型对照端到端闭环（绝不在 pytest 进程内改全局 threads）。
6. 元数据校验、安全回退（空清单/非零成本）、松弛不可行证明与 IPM 残差超限 Simplex 精修。
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import math
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import highspy
import pytest
from bundle_testkit import fixture_files
from test_highs_milp_provider import _campus_inputs
from test_highs_result_adapter import PARAMETERS_PATH, PROBLEM_PATH, _adapt, _parameters, _receipt

from iesplan.computation import HIGHS_ADAPTER_PATH
from iesplan.computation.generators.linear import (
    LinearConstraint,
    LinearExpression,
    LinearModel,
    LinearVariable,
    lower_mutex,
)
from iesplan.computation.generators.matrix import SparseModel
from iesplan.computation.generators.mps import write_mps
from iesplan.core.content import read_content


def _load_solver_module():
    name = "ies_highs_solver_s2_under_test"
    loader = importlib.machinery.SourceFileLoader(name, HIGHS_ADAPTER_PATH)
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def _parse_solution(text: str) -> dict[str, object]:
    lines = text.splitlines()
    assert lines[0] == "HIGHS_SOLUTION_V1"
    facts: dict[str, object] = {}
    primals: list[float] = []
    in_primal = False
    for line in lines[1:]:
        if line == "primal:":
            in_primal = True
            continue
        key, sep, value = line.partition(":")
        assert sep
        if in_primal:
            primals.append(float(value.strip()))
        else:
            facts[key.strip()] = value.strip()
    facts["primals"] = primals
    return facts


def _build_chain_mutex_model(
    pair_costs: list[tuple[float, float]],
    *,
    cap: float | Sequence[float] = 5.0,
    demand: float,
    slack_cost: float = 100.0,
) -> tuple[LinearModel, list[tuple[str, str, str, str, str]]]:
    """Build deterministic multi-round mutex MILP and symbolic mutex tuples.

    Each pair i has continuous x_i, y_i in [0, cap_i] and mutex binary z_i:
      x_i - cap_i * z_i <= 0
      y_i + cap_i * z_i <= cap_i
    Global demand: sum_i (x_i + y_i) + slack >= demand.
    """
    caps = [float(cap)] * len(pair_costs) if isinstance(cap, (int, float)) else [float(c) for c in cap]
    assert len(caps) == len(pair_costs)

    variables: list[LinearVariable] = []
    constraints: list[LinearConstraint] = []
    obj_terms: list[tuple[tuple[str, int], float]] = []
    demand_terms: list[tuple[tuple[str, int], float]] = []
    symbols: dict[str, tuple[str, float, float, str]] = {}

    for idx, ((cx, cy), c_cap) in enumerate(zip(pair_costs, caps, strict=True)):
        vx = LinearVariable(name=f"x_{idx}", lower=0.0, upper=c_cap, kind="continuous")
        vy = LinearVariable(name=f"y_{idx}", lower=0.0, upper=c_cap, kind="continuous")
        variables.extend((vx, vy))
        symbols[vx.name] = ("var", 0.0, c_cap, "continuous")
        symbols[vy.name] = ("var", 0.0, c_cap, "continuous")
        obj_terms.append(((vx.name, 0), cx))
        obj_terms.append(((vy.name, 0), cy))
        demand_terms.append(((vx.name, 0), 1.0))
        demand_terms.append(((vy.name, 0), 1.0))

    slack = LinearVariable(name="slack", lower=0.0, upper=1000.0, kind="continuous")
    variables.append(slack)
    obj_terms.append(((slack.name, 0), slack_cost))
    demand_terms.append(((slack.name, 0), 1.0))

    constraints.append(
        LinearConstraint(
            name="demand",
            expression=LinearExpression(terms=tuple(demand_terms)),
            sense="ge",
            rhs=demand,
        )
    )

    pair_names: list[tuple[str, str, str, str, str]] = []
    for idx in range(len(pair_costs)):
        lowered = lower_mutex(f"m_{idx}", (f"x_{idx}", 0), (f"y_{idx}", 0), symbols)
        b_var = lowered.variables[0]
        row_x, row_y = lowered.constraints
        variables.append(b_var)
        constraints.extend((row_x, row_y))
        pair_names.append((b_var.name, f"x_{idx}", f"y_{idx}", row_x.name, row_y.name))

    model = LinearModel(
        variables=tuple(variables),
        constraints=tuple(constraints),
        objective=LinearExpression(terms=tuple(obj_terms)),
        sense="minimize",
    )
    return model, pair_names


def _resolve_mps_pair_indices(
    model: LinearModel,
    pair_names: Sequence[tuple[str, str, str, str, str]],
) -> tuple[int, int, list[tuple[int, int, int, int, int]]]:
    """Resolve exact sorted MPS column/row indices matching ``write_mps``."""
    sparse = SparseModel.from_public(model)
    col_index = {
        sparse.columns.values_at(decl)[0]: pos
        for pos, decl in enumerate(sparse.column_order)
    }
    row_index = {
        sparse.rows.names[decl]: pos for pos, decl in enumerate(sparse.row_order)
    }
    pairs = [
        (col_index[b], col_index[x], col_index[y], row_index[rx], row_index[ry])
        for b, x, y, rx, ry in pair_names
    ]
    return len(sparse.column_order), len(sparse.row_order), pairs


def _write_mps_and_accel(
    tmp_path: Path,
    model: LinearModel,
    pair_names: Sequence[tuple[str, str, str, str, str]],
    *,
    mps_name: str = "problem.mps",
    accel_name: str = "acceleration.json",
) -> tuple[Path, Path, bytes, list[tuple[int, int, int, int, int]]]:
    num_col, num_row, pairs = _resolve_mps_pair_indices(model, pair_names)
    mps_text, _ = write_mps(model)
    mps_bytes = mps_text.encode("utf-8")
    mps_path = tmp_path / mps_name
    mps_path.write_bytes(mps_bytes)
    accel_doc = {
        "schema": "ies.solver-acceleration",
        "schema_version": "1.0.0",
        "num_columns": num_col,
        "num_rows": num_row,
        "technical_mutex_pairs": [list(p) for p in pairs],
    }
    accel_path = tmp_path / accel_name
    accel_path.write_text(json.dumps(accel_doc), encoding="utf-8")
    return mps_path, accel_path, mps_bytes, pairs


def _solve_original_cli(tmp_path: Path, mps_name: str, sol_name: str) -> dict[str, object]:
    proc = subprocess.run(
        [
            HIGHS_ADAPTER_PATH,
            mps_name,
            "--solution_file",
            sol_name,
            "--time_limit",
            "30",
            "--random_seed",
            "0",
            "--acceleration_mode",
            "off",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return _parse_solution((tmp_path / sol_name).read_text(encoding="utf-8"))


def test_zero_conflict_one_round_lossless_convergence(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """0 冲突 1 轮无损收敛：单次 readModel、精确最优、valid_relaxation + ResultAdapter 发布 solved。"""
    mod = _load_solver_module()
    # y_0 costs 10 > x_1 cost 5 -> Round 0 picks x_0=5, x_1=1 (0 conflicts in Round 0, obj=10)
    model, pair_names = _build_chain_mutex_model([(1.0, 10.0), (5.0, 11.0)], cap=5.0, demand=6.0)
    _, _, mps_bytes, _ = _write_mps_and_accel(tmp_path, model, pair_names)
    orig_facts = _solve_original_cli(tmp_path, "problem.mps", "orig.sol")
    assert float(orig_facts["objective_value"]) == pytest.approx(10.0)

    read_calls = 0
    real_highs = highspy.Highs

    class _ReadSpy:
        def __init__(self) -> None:
            self._inner = real_highs()

        def readModel(self, path: str):
            nonlocal read_calls
            read_calls += 1
            return self._inner.readModel(path)

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    monkeypatch.setattr(highspy, "Highs", _ReadSpy)
    capsys.readouterr()
    monkeypatch.chdir(tmp_path)
    rc = mod._cmd_solve(
        [
            "problem.mps",
            "--solution_file",
            "accel.sol",
            "--time_limit",
            "30",
            "--random_seed",
            "0",
            "--acceleration_mode",
            "on_demand_mutex",
            "--acceleration_file",
            "acceleration.json",
        ]
    )
    assert rc == 0
    assert read_calls == 1
    stderr = capsys.readouterr().err
    assert "stage=mutex_round event=end status=round=0,conflicts=0,added=0,rss_kib=" in stderr
    assert (
        "stage=acceleration event=applied "
        "status=requested=on_demand_mutex,applied=on_demand_mutex,pairs=2,rounds=1,restored=0"
    ) in stderr

    sol_bytes = (tmp_path / "accel.sol").read_bytes()
    facts = _parse_solution(sol_bytes.decode("utf-8"))
    assert facts["model_status"] == "HighsModelStatus.kOptimal"
    assert facts["has_feasible_solution"] == "true"
    assert facts["solve_domain"] == "valid_relaxation"
    assert facts["original_model_verified"] == "true"
    assert float(facts["objective_value"]) == pytest.approx(float(orig_facts["objective_value"]))

    # Verify reconstructed primal against original MPS LP
    h_orig = real_highs()
    h_orig.setOptionValue("output_flag", False)
    assert h_orig.readModel(str(tmp_path / "problem.mps")) == highspy.HighsStatus.kOk
    assert mod.verify_primal_against_lp(h_orig.getLp(), facts["primals"])

    # Verify ResultAdapter maps valid_relaxation + kOptimal + verified -> solved
    bundle_files = fixture_files(
        {
            PROBLEM_PATH: mps_bytes,
            PARAMETERS_PATH: _parameters(unit_map={}),
        }
    )
    result = _adapt(bundle_files, _receipt(), sol_bytes)
    assert result.execution_status == "completed"
    assert result.math_status == "solved"
    assert result.outputs["solver_summary"]["termination_reason"] == "optimal"
    assert result.outputs["solver_summary"]["best_bound"] == pytest.approx(11.5)
    assert result.outputs["solver_summary"]["relative_gap"] == pytest.approx(0.0)


def test_multi_round_double_positive_conflict_recovery(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """真实多轮双正冲突恢复：Round 0 冲突 A -> 恢复 A 暴露 B 新冲突 -> Round 1 恢复 B -> Round 2 收敛。"""
    mod = _load_solver_module()
    # Pairs A=(1,2), B=(5,6), C=(20,21), cap=5, demand=12:
    # Round 0: x_A=5,y_A=5,x_B=2 -> conflict on A (1) -> restore A
    # Round 1: x_A=5, x_B=5,y_B=2 -> conflict on B (1) -> restore B
    # Round 2: x_A=5, x_B=5, x_C=2 -> 0 conflicts, C stays relaxed -> obj=70.0
    model, pair_names = _build_chain_mutex_model(
        [(1.0, 2.0), (5.0, 6.0), (20.0, 21.0)], cap=5.0, demand=12.0
    )
    _, _, mps_bytes, pairs = _write_mps_and_accel(tmp_path, model, pair_names)
    orig_facts = _solve_original_cli(tmp_path, "problem.mps", "orig.sol")
    assert float(orig_facts["objective_value"]) == pytest.approx(70.0)

    read_calls = 0
    real_highs = highspy.Highs

    class _ReadSpy:
        def __init__(self) -> None:
            self._inner = real_highs()

        def readModel(self, path: str):
            nonlocal read_calls
            read_calls += 1
            return self._inner.readModel(path)

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    monkeypatch.setattr(highspy, "Highs", _ReadSpy)
    capsys.readouterr()
    monkeypatch.chdir(tmp_path)
    rc = mod._cmd_solve(
        [
            "problem.mps",
            "--solution_file",
            "multi.sol",
            "--time_limit",
            "30",
            "--random_seed",
            "0",
            "--acceleration_mode",
            "on_demand_mutex",
            "--acceleration_file",
            "acceleration.json",
        ]
    )
    assert rc == 0
    assert read_calls == 1
    stderr = capsys.readouterr().err
    assert "stage=mutex_round event=end status=round=0,conflicts=1,added=1,rss_kib=" in stderr
    assert "stage=mutex_round event=end status=round=1,conflicts=1,added=1,rss_kib=" in stderr
    assert "stage=mutex_round event=end status=round=2,conflicts=0,added=0,rss_kib=" in stderr
    assert (
        "stage=acceleration event=applied "
        "status=requested=on_demand_mutex,applied=on_demand_mutex,pairs=3,rounds=3,restored=2"
    ) in stderr

    sol_bytes = (tmp_path / "multi.sol").read_bytes()
    facts = _parse_solution(sol_bytes.decode("utf-8"))
    assert facts["model_status"] == "HighsModelStatus.kOptimal"
    assert facts["solve_domain"] == "valid_relaxation"
    assert facts["original_model_verified"] == "true"
    assert float(facts["objective_value"]) == pytest.approx(70.0)

    # Reconstructed z_C == 1.0 (x_C=2, y_C=0) and full primal passes original LP check
    bin_c = pairs[2][0]
    assert facts["primals"][bin_c] == pytest.approx(1.0)
    h_orig = real_highs()
    h_orig.setOptionValue("output_flag", False)
    assert h_orig.readModel(str(tmp_path / "problem.mps")) == highspy.HighsStatus.kOk
    assert mod.verify_primal_against_lp(h_orig.getLp(), facts["primals"])

    bundle_files = fixture_files(
        {
            PROBLEM_PATH: mps_bytes,
            PARAMETERS_PATH: _parameters(unit_map={}),
        }
    )
    result = _adapt(bundle_files, _receipt(), sol_bytes)
    assert result.execution_status == "completed"
    assert result.math_status == "solved"


def test_bailout_and_inplace_fallback_to_original_milp(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """熔断回退：max_rounds、round_idx>=2 冲突未收敛与 pairs>=100 恢复超半数均原位恢复全原 MILP。"""
    mod = _load_solver_module()

    # 1. max_rounds=2 bailout on 3-pair chain -> Round 0 restores A, Round 1 bails out & solves fallback
    model3, names3 = _build_chain_mutex_model(
        [(1.0, 2.0), (5.0, 6.0), (20.0, 21.0)], cap=5.0, demand=12.0
    )
    mps3, accel3, _, pairs3 = _write_mps_and_accel(
        tmp_path, model3, names3, mps_name="m3.mps", accel_name="a3.json"
    )

    read_calls = 0
    real_highs = highspy.Highs

    class _ReadSpy:
        def __init__(self) -> None:
            self._inner = real_highs()

        def readModel(self, path: str):
            nonlocal read_calls
            read_calls += 1
            return self._inner.readModel(path)

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    monkeypatch.setattr(highspy, "Highs", _ReadSpy)
    capsys.readouterr()
    sol_maxr = tmp_path / "maxr.sol"
    handle, status, slot = mod._run_accelerated_solve(
        str(mps3),
        str(sol_maxr),
        30.0,
        0,
        acceleration_mode="on_demand_mutex",
        acceleration_file=str(accel3),
        max_rounds=2,
    )
    assert read_calls == 1
    assert status == highspy.HighsModelStatus.kOptimal
    assert slot.incumbent is not None
    assert slot.incumbent.objective_value == pytest.approx(70.0)
    assert slot.incumbent.solve_domain == "original"
    lp_after = handle.getLp()
    for bin_col, _, _, _, _ in pairs3:
        assert lp_after.integrality_[bin_col] == highspy.HighsVarType.kInteger
        assert lp_after.col_lower_[bin_col] == 0.0
        assert lp_after.col_upper_[bin_col] == 1.0
    stderr1 = capsys.readouterr().err
    assert (
        "stage=acceleration event=fallback "
        "status=requested=on_demand_mutex,applied=none,rounds=2,restored=3"
    ) in stderr1

    # 2. Non-shrinking conflict bailout at round_idx == 2 (caps=[10,5,2,5], demand=18 -> opt=75)
    # Round 0: x0=10,y0=8 -> conflict=1 -> restore 0
    # Round 1: x0=10, x1=5,y1=3 -> conflict=1 -> restore 1
    # Round 2: x0=10,x1=5, x2=2,y2=1 -> conflict=1 >= prev(1) -> bailout -> restore 2,3 -> obj=75
    model4, names4 = _build_chain_mutex_model(
        [(1.0, 2.0), (5.0, 6.0), (10.0, 11.0), (20.0, 21.0)],
        cap=[10.0, 5.0, 2.0, 5.0],
        demand=18.0,
    )
    mps4, accel4, _, pairs4 = _write_mps_and_accel(
        tmp_path, model4, names4, mps_name="m4.mps", accel_name="a4.json"
    )
    sol_nonconv = tmp_path / "nonconv.sol"
    read_calls = 0
    capsys.readouterr()
    handle4, status4, slot4 = mod._run_accelerated_solve(
        str(mps4),
        str(sol_nonconv),
        30.0,
        0,
        acceleration_mode="on_demand_mutex",
        acceleration_file=str(accel4),
        max_rounds=5,
    )
    assert read_calls == 1
    assert status4 == highspy.HighsModelStatus.kOptimal
    assert slot4.incumbent is not None
    assert slot4.incumbent.objective_value == pytest.approx(75.0)
    assert slot4.incumbent.solve_domain == "original"
    lp_after4 = handle4.getLp()
    for bin_col, _, _, _, _ in pairs4:
        assert lp_after4.integrality_[bin_col] == highspy.HighsVarType.kInteger
    stderr2 = capsys.readouterr().err
    assert "round=2,conflicts=1,added=2" in stderr2
    assert (
        "stage=acceleration event=fallback "
        "status=requested=on_demand_mutex,applied=none,rounds=3,restored=4"
    ) in stderr2

    # 3. >50% restored fraction bailout when len(pairs) >= 100
    # 100 pairs with costs (1, 1.5), cap=1.0, demand=151 -> 51 pairs conflict in Round 0.
    model100, names100 = _build_chain_mutex_model([(1.0, 1.5)] * 100, cap=1.0, demand=151.0)
    mps100, accel100, _, _ = _write_mps_and_accel(
        tmp_path, model100, names100, mps_name="m100.mps", accel_name="a100.json"
    )
    capsys.readouterr()
    sol100 = tmp_path / "m100.sol"
    _, status100, slot100 = mod._run_accelerated_solve(
        str(mps100),
        str(sol100),
        30.0,
        0,
        acceleration_mode="on_demand_mutex",
        acceleration_file=str(accel100),
        max_rounds=5,
    )
    assert status100 == highspy.HighsModelStatus.kOptimal
    assert slot100.incumbent is not None
    assert slot100.incumbent.solve_domain == "original"
    assert slot100.incumbent.objective_value == pytest.approx(5200.0)
    stderr3 = capsys.readouterr().err
    assert "round=0,conflicts=" in stderr3
    assert "rounds=1,restored=100" in stderr3


def test_budget_exhaustion_mid_recovery_preserves_verified_incumbent(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """恢复中途预算耗尽：不启动额外轮次或回退求解，保全已核查可行解且不发布冲突松弛解。"""
    mod = _load_solver_module()
    model, pair_names = _build_chain_mutex_model(
        [(1.0, 2.0), (5.0, 6.0), (20.0, 21.0)], cap=5.0, demand=12.0
    )
    mps_path, accel_path, _, pairs = _write_mps_and_accel(tmp_path, model, pair_names)

    # Case A: Round 0 has conflicts=1 and exhausts budget -> bailout sees None, does NOT run fallback MILP!
    clock_a = [1000.0]
    run_calls_a = 0
    real_highs = highspy.Highs

    class _ExhaustAfterRound0:
        def __init__(self) -> None:
            self._inner = real_highs()

        def run(self):
            nonlocal run_calls_a
            run_calls_a += 1
            res = self._inner.run()
            clock_a[0] += 9.95  # exhaust 10s deadline right after Round 0
            return res

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    monkeypatch.setattr(mod.time, "monotonic", lambda: clock_a[0])
    monkeypatch.setattr(highspy, "Highs", _ExhaustAfterRound0)
    capsys.readouterr()
    sol_a = tmp_path / "exhaust_a.sol"
    _, status_a, slot_a = mod._run_accelerated_solve(
        str(mps_path),
        str(sol_a),
        10.0,
        0,
        acceleration_mode="on_demand_mutex",
        acceleration_file=str(accel_path),
    )
    assert run_calls_a == 1
    assert status_a == highspy.HighsModelStatus.kTimeLimit
    assert slot_a.incumbent is None
    facts_a = _parse_solution(sol_a.read_text(encoding="utf-8"))
    assert facts_a["model_status"] == "HighsModelStatus.kTimeLimit"
    assert facts_a["has_feasible_solution"] == "false"
    assert facts_a["primals"] == []

    # Case B: Prior verified incumbent already in sol_path / slot, next round skipped on budget exhaustion
    clock_b = [2000.0]
    run_calls_b = 0

    class _ExhaustBeforeRound0:
        def __init__(self) -> None:
            self._inner = real_highs()

        def readModel(self, path: str):
            res = self._inner.readModel(path)
            clock_b[0] += 5.0  # readModel consumes entire 5s budget
            return res

        def run(self):
            nonlocal run_calls_b
            run_calls_b += 1
            return self._inner.run()

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    monkeypatch.setattr(mod.time, "monotonic", lambda: clock_b[0])
    monkeypatch.setattr(highspy, "Highs", _ExhaustBeforeRound0)
    h_pre = real_highs()
    h_pre.setOptionValue("output_flag", False)
    assert h_pre.readModel(str(mps_path)) == highspy.HighsStatus.kOk
    orig_lp = h_pre.getLp()
    assert h_pre.run() == highspy.HighsStatus.kOk
    sol_b = tmp_path / "preserve_b.sol"
    slot_b = mod.VerifiedIncumbentSlot(sense="minimize", sol_path=str(sol_b))
    assert slot_b.record_from_handle(
        h_pre,
        highspy.HighsModelStatus.kTimeLimit,
        original_model_verified=True,
        original_lp=orig_lp,
        solve_domain="valid_relaxation",
    )
    saved_bytes = sol_b.read_bytes()

    h_b = _ExhaustBeforeRound0()
    assert h_b.readModel(str(mps_path)) == highspy.HighsStatus.kOk
    lp_b = h_b.getLp()
    row_bounds = mod._validate_and_snapshot_mutex_pairs(lp_b, pairs)
    assert row_bounds is not None
    del lp_b
    capsys.readouterr()
    mod._run_on_demand_mutex_recovery(
        h_b,
        str(sol_b),
        2000.0 + 5.0,
        "on_demand_mutex",
        pairs,
        row_bounds,
        slot_b,
        min_guard=0.05,
        io_margin=0.05,
        max_rounds=5,
    )
    assert run_calls_b == 0
    assert sol_b.read_bytes() == saved_bytes
    assert "stage=run event=skipped status=budget_exhausted" in capsys.readouterr().err


def test_cli_subprocess_and_campus_model_integration(tmp_path: Path) -> None:
    """CLI 子进程 --threads 2 与 pIES campus 真实模型同模型对照端到端闭环。"""
    # 1. Multi-round CLI subprocess with --threads 2 (isolated subprocess, never alters pytest threads)
    model, pair_names = _build_chain_mutex_model(
        [(1.0, 2.0), (5.0, 6.0), (20.0, 21.0)], cap=5.0, demand=12.0
    )
    _write_mps_and_accel(
        tmp_path, model, pair_names, mps_name="chain.mps", accel_name="chain_accel.json"
    )
    proc_chain = subprocess.run(
        [
            HIGHS_ADAPTER_PATH,
            "chain.mps",
            "--solution_file",
            "chain.sol",
            "--time_limit",
            "30",
            "--random_seed",
            "0",
            "--acceleration_mode",
            "on_demand_mutex",
            "--acceleration_file",
            "chain_accel.json",
            "--threads",
            "2",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc_chain.returncode == 0, proc_chain.stderr
    assert "applied=on_demand_mutex,pairs=3,rounds=3,restored=2" in proc_chain.stderr
    chain_facts = _parse_solution((tmp_path / "chain.sol").read_text(encoding="utf-8"))
    assert float(chain_facts["objective_value"]) == pytest.approx(70.0)

    # 2. Real pIES campus fixture via HighsMilpProvider: off vs on_demand_mutex + ResultAdapter
    from iesplan.computation.generators import HIGHS_MILP_PROVIDER

    artifact, models, materialized, finance, base_config, evaluation = _campus_inputs(seed=0)
    config_off = replace(base_config, time_limit_seconds=60, options={"acceleration": {"mode": "off"}})
    config_on = replace(
        base_config, time_limit_seconds=60, options={"acceleration": {"mode": "on_demand_mutex"}}
    )
    bundle_off = HIGHS_MILP_PROVIDER.generate(
        artifact, models, materialized, finance, config_off, evaluation
    )
    bundle_on = HIGHS_MILP_PROVIDER.generate(
        artifact, models, materialized, finance, config_on, evaluation
    )

    campus_mps = read_content(bundle_off.files["input/problem.mps"])
    assert read_content(bundle_on.files["input/problem.mps"]) == campus_mps
    campus_accel = read_content(bundle_on.files["input/acceleration.json"])
    campus_params = read_content(bundle_on.files["input/parameters.json"])
    campus_records = read_content(bundle_on.files["input/parameters.jsonl"])

    campus_dir = tmp_path / "campus"
    (campus_dir / "input").mkdir(parents=True)
    (campus_dir / "output").mkdir(parents=True)
    (campus_dir / "input" / "problem.mps").write_bytes(campus_mps)
    (campus_dir / "input" / "acceleration.json").write_bytes(campus_accel)

    proc_off = subprocess.run(
        [
            HIGHS_ADAPTER_PATH,
            *bundle_off.manifest.command["arguments"],
            "--threads",
            "2",
        ],
        cwd=campus_dir,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc_off.returncode == 0, proc_off.stderr
    off_bytes = (campus_dir / "output" / "solution.sol").read_bytes()

    proc_on = subprocess.run(
        [
            HIGHS_ADAPTER_PATH,
            *bundle_on.manifest.command["arguments"],
            "--threads",
            "2",
        ],
        cwd=campus_dir,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc_on.returncode == 0, proc_on.stderr
    assert "stage=acceleration event=applied" in proc_on.stderr
    on_bytes = (campus_dir / "output" / "solution.sol").read_bytes()

    campus_bundle_files = fixture_files(
        {
            PROBLEM_PATH: campus_mps,
            PARAMETERS_PATH: campus_params,
            "input/parameters.jsonl": campus_records,
        }
    )
    res_off = _adapt(campus_bundle_files, _receipt(), off_bytes)
    res_on = _adapt(campus_bundle_files, _receipt(), on_bytes)
    assert res_off.execution_status == "completed"
    assert res_on.execution_status == "completed"
    assert res_off.math_status == "solved"
    assert res_on.math_status == "solved"
    assert res_on.outputs["solution"]["objective_value"] == pytest.approx(
        res_off.outputs["solution"]["objective_value"],
        rel=1e-6,
    )

    mod = _load_solver_module()
    h_campus = highspy.Highs()
    h_campus.setOptionValue("output_flag", False)
    assert h_campus.readModel(str(campus_dir / "input" / "problem.mps")) == highspy.HighsStatus.kOk
    on_facts = _parse_solution(on_bytes.decode("utf-8"))
    assert mod.verify_primal_against_lp(h_campus.getLp(), on_facts["primals"])


def test_metadata_validation_fallback_infeasible_and_simplex_refine(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """校验非法/重复 --acceleration_file 退出码 2、空清单/非零成本安全回退、不可行证明与 Simplex 精修。"""
    mod = _load_solver_module()
    model, pair_names = _build_chain_mutex_model([(1.0, 2.0)], cap=5.0, demand=3.0)
    mps_path, _, _, pairs = _write_mps_and_accel(tmp_path, model, pair_names)

    # 1. CLI invalid / duplicate --acceleration_file flags exit with 2
    for bad_args in (
        ["--acceleration_file", "/abs/accel.json"],
        ["--acceleration_file", "../accel.json"],
        ["--acceleration_file", "-accel.json"],
        ["--acceleration_file", "a.json", "--acceleration_file", "b.json"],
        ["--acceleration_mode", "on_demand_mutex", "--acceleration_file", "missing.json"],
    ):
        proc = subprocess.run(
            [
                HIGHS_ADAPTER_PATH,
                "problem.mps",
                "--solution_file",
                "bad.sol",
                "--time_limit",
                "10",
                "--random_seed",
                "0",
                *bad_args,
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 2
        assert not (tmp_path / "bad.sol").exists()

    # 2. Invalid metadata contents rejected by _load_acceleration_metadata
    base_meta = {
        "schema": "ies.solver-acceleration",
        "schema_version": "1.0.0",
        "num_columns": 4,
        "num_rows": 3,
        "technical_mutex_pairs": [],
    }
    bad_docs = [
        {**base_meta, "schema": "wrong"},
        {**base_meta, "schema_version": "2.0.0"},
        {**base_meta, "num_columns": 99},
        {**base_meta, "num_rows": 99},
        {**base_meta, "technical_mutex_pairs": [[99, 0, 1, 1, 2]]},
        {**base_meta, "technical_mutex_pairs": [list(pairs[0]), list(pairs[0])]},
    ]
    for idx, doc in enumerate(bad_docs):
        p = tmp_path / f"bad_{idx}.json"
        p.write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(ValueError):
            mod._load_acceleration_metadata(
                str(p), expected_num_col=len(model.variables), expected_num_row=len(model.constraints)
            )

    # 3. Empty pairs safely falls back to original solve (applied=none)
    empty_accel = tmp_path / "empty_accel.json"
    empty_accel.write_text(
        json.dumps(
            {
                "schema": "ies.solver-acceleration",
                "schema_version": "1.0.0",
                "num_columns": len(model.variables),
                "num_rows": len(model.constraints),
                "technical_mutex_pairs": [],
            }
        ),
        encoding="utf-8",
    )
    capsys.readouterr()
    _, st_empty, slot_empty = mod._run_accelerated_solve(
        str(mps_path),
        str(tmp_path / "empty.sol"),
        20.0,
        0,
        acceleration_mode="on_demand_mutex",
        acceleration_file=str(empty_accel),
    )
    assert st_empty == highspy.HighsModelStatus.kOptimal
    assert slot_empty.incumbent is not None
    assert slot_empty.incumbent.solve_domain == "original"
    assert (
        "stage=acceleration event=fallback status=requested=on_demand_mutex,applied=none"
        in capsys.readouterr().err
    )

    # 4. Non-zero bin_col cost fails pre-relaxation LP check -> safely falls back to original solve
    nonzero_model = LinearModel(
        variables=model.variables,
        constraints=model.constraints,
        objective=LinearExpression(
            terms=(*model.objective.terms, ((pair_names[0][0], 0), 2.5))
        ),
        sense="minimize",
    )
    mps_nz, accel_nz, _, _ = _write_mps_and_accel(
        tmp_path, nonzero_model, pair_names, mps_name="nz.mps", accel_name="nz.json"
    )
    capsys.readouterr()
    _, st_nz, slot_nz = mod._run_accelerated_solve(
        str(mps_nz),
        str(tmp_path / "nz.sol"),
        20.0,
        0,
        acceleration_mode="on_demand_mutex",
        acceleration_file=str(accel_nz),
    )
    assert st_nz == highspy.HighsModelStatus.kOptimal
    assert slot_nz.incumbent is not None
    assert slot_nz.incumbent.solve_domain == "original"
    assert (
        "stage=acceleration event=fallback status=requested=on_demand_mutex,applied=none"
        in capsys.readouterr().err
    )

    # 5. Relaxation infeasibility proves original infeasibility
    infeas_model = LinearModel(
        variables=model.variables,
        constraints=(
            *model.constraints,
            LinearConstraint(
                name="impossible",
                expression=LinearExpression(terms=((("x_0", 0), 1.0), (("y_0", 0), 1.0))),
                sense="le",
                rhs=-1.0,
            ),
        ),
        objective=model.objective,
        sense="minimize",
    )
    mps_inf, accel_inf, mps_inf_bytes, _ = _write_mps_and_accel(
        tmp_path, infeas_model, pair_names, mps_name="infeas.mps", accel_name="infeas.json"
    )
    sol_inf = tmp_path / "infeas.sol"
    _, st_inf, slot_inf = mod._run_accelerated_solve(
        str(mps_inf),
        str(sol_inf),
        20.0,
        0,
        acceleration_mode="on_demand_mutex",
        acceleration_file=str(accel_inf),
    )
    assert st_inf == highspy.HighsModelStatus.kInfeasible
    assert slot_inf.incumbent is None
    inf_bytes = sol_inf.read_bytes()
    inf_facts = _parse_solution(inf_bytes.decode("utf-8"))
    assert inf_facts["model_status"] == "HighsModelStatus.kInfeasible"
    assert inf_facts["solve_domain"] == "valid_relaxation"
    assert inf_facts["original_model_verified"] == "true"
    bundle_inf = fixture_files(
        {
            PROBLEM_PATH: mps_inf_bytes,
            PARAMETERS_PATH: _parameters(unit_map={}),
        }
    )
    res_inf = _adapt(bundle_inf, _receipt(), inf_bytes)
    assert res_inf.execution_status == "completed"
    assert res_inf.math_status == "infeasible"

    # 6. Verification failure on first check triggers Simplex refinement in same handle and succeeds
    verify_calls = 0
    orig_verify = mod.verify_primal_against_lp
    solver_options_seen: list[object] = []
    presolve_options_seen: list[object] = []
    real_set_opt = mod._set_highs_option

    def _spy_set_opt(handle, key, value):
        if key == "solver":
            solver_options_seen.append(value)
        elif key == "presolve":
            presolve_options_seen.append(value)
        return real_set_opt(handle, key, value)

    def _fail_first_verify(*args, **kwargs):
        nonlocal verify_calls
        verify_calls += 1
        if verify_calls == 1:
            return False
        return orig_verify(*args, **kwargs)

    monkeypatch.setattr(mod, "verify_primal_against_lp", _fail_first_verify)
    monkeypatch.setattr(mod, "_set_highs_option", _spy_set_opt)
    sol_ref = tmp_path / "refine.sol"
    _, st_ref, slot_ref = mod._run_accelerated_solve(
        str(mps_path),
        str(sol_ref),
        20.0,
        0,
        acceleration_mode="on_demand_mutex",
        acceleration_file=str(tmp_path / "acceleration.json"),
    )
    assert st_ref == highspy.HighsModelStatus.kOptimal
    assert verify_calls == 2
    assert solver_options_seen == ["simplex", "choose"]
    assert presolve_options_seen == ["off", "choose"]
    assert slot_ref.incumbent is not None
    assert slot_ref.incumbent.objective_value == pytest.approx(3.0)
    assert slot_ref.incumbent.solve_domain == "valid_relaxation"


def test_verify_primal_against_lp_scale_aware_si_joule_tolerance() -> None:
    """高量级 SI 焦耳等式与变量边界允许 0.5-ULP 舍入残差，同时拒绝真实违背与单位尺度超差。"""
    mod = _load_solver_module()

    def _build_lp(rowwise: bool):
        lp = highspy.HighsLp()
        lp.num_col_ = 4
        lp.num_row_ = 2
        lp.col_lower_ = [0.0, 0.0, 0.0, 0.0]
        lp.col_upper_ = [1e5, 1.5e9, 1.5e9, 10.0]
        lp.row_lower_ = [0.0, 1.0]
        lp.row_upper_ = [0.0, 1.0]
        lp.a_matrix_.format_ = (
            highspy.MatrixFormat.kRowwise if rowwise else highspy.MatrixFormat.kColwise
        )
        if rowwise:
            lp.a_matrix_.start_ = [0, 3, 4]
            lp.a_matrix_.index_ = [0, 1, 2, 3]
            lp.a_matrix_.value_ = [-3420.0, -1.0, 1.0, 1.0]
        else:
            lp.a_matrix_.start_ = [0, 1, 2, 3, 4]
            lp.a_matrix_.index_ = [0, 0, 0, 1]
            lp.a_matrix_.value_ = [-3420.0, -1.0, 1.0, 1.0]
        return lp

    # Mirror campus_5y_bat_on SI Joule magnitudes where exact math.fsum
    # residual is float64 0.5 ULP (2**-23 > 1e-7)
    charge = 41267.90403340236
    prev_energy = 1154863768.2057638
    next_energy = 1296000000.0
    half_ulp = 2.0**-23
    assert abs(math.fsum((-3420.0 * charge, -prev_energy, next_energy)) - half_ulp) == 0.0

    for rowwise in (False, True):
        lp = _build_lp(rowwise)
        # 1. Accept 0.5-ULP row residual on ~1.296e9 row and 0.5-ULP rounding on 1.5e9 col bound
        assert mod.verify_primal_against_lp(lp, [charge, prev_energy, next_energy, 1.0])
        assert mod.verify_primal_against_lp(lp, [0.0, 1.5e9 + half_ulp, 1.5e9 + half_ulp, 1.0])
        # 2. Reject genuine violation (~1e-2) on high-magnitude row and column bound
        assert not mod.verify_primal_against_lp(lp, [charge, prev_energy, next_energy + 1e-2, 1.0])
        assert not mod.verify_primal_against_lp(lp, [0.0, 1.5e9 + 1e-2, 1.5e9 + 1e-2, 1.0])
        # 3. Reject 2e-7 violation on unit-scale row
        assert not mod.verify_primal_against_lp(
            lp, [charge, prev_energy, next_energy, 1.0 + 2e-7]
        )
