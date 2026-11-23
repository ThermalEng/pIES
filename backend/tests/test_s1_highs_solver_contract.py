"""S1 求解器可执行契约测试（仅在 Docker 内运行真实 highspy 1.7.2 与 ies-highs-solver）。

覆盖：
1. CLI 向后兼容（7 参数位置形式）与可选尾部结构化参数（--acceleration_mode / --threads），
   默认/off 输出与 HIGHS_SOLUTION_V1 逐字节一致，非法参数退出码 2。
2. readModel 后单调截止预算 helper remaining_solve_budget（耗尽返回 None，严禁 max 重发）。
3. highspy 1.7.2 setSolution 四态闭环与有限占位值契约（完整合法解、部分整数 + 有限占位值
   触发内置 LP 修复、NaN/Inf 在调用 setSolution 前被拒绝、不可行离散赋值无缝回退原 MILP、
   紧凑 time_limit 干净退出不误报可行）。
4. 单槽位 VerifiedIncumbentSlot + 临时文件 + os.replace 原子保解与跨阶段预算耗尽交接。
5. 可选附加 HIGHS_SOLUTION_V1 头行仅在非默认时输出。
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import highspy
import numpy as np
import pytest

from iesplan.computation import HIGHS_ADAPTER_PATH
from iesplan.computation.generators.linear import (
    LinearConstraint,
    LinearExpression,
    LinearModel,
    LinearVariable,
)
from iesplan.computation.generators.mps import write_mps


def _load_solver_module():
    name = "ies_highs_solver_s1_under_test"
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


def _sample_milp_mps() -> str:
    """最小 0-1 混合整数模型（有唯一最优解 x=1, y=2.0, obj=7.0）。"""
    model = LinearModel(
        variables=(
            LinearVariable(name="x", lower=0.0, upper=1.0, kind="binary"),
            LinearVariable(name="y", lower=0.0, upper=10.0, kind="continuous"),
        ),
        constraints=(
            LinearConstraint(
                name="link",
                expression=LinearExpression(terms=((("y", 0), 1.0), (("x", 0), -2.0))),
                sense="ge",
                rhs=0.0,
            ),
            LinearConstraint(
                name="min_supply",
                expression=LinearExpression(terms=((("y", 0), 1.0), (("x", 0), 2.0))),
                sense="ge",
                rhs=4.0,
            ),
        ),
        objective=LinearExpression(terms=((("x", 0), 3.0), (("y", 0), 2.0))),
        sense="minimize",
    )
    text, _ = write_mps(model)
    return text


def _build_warm_start_milp(*, log_file: str | None = None) -> highspy.Highs:
    """构造含多种变量上下界的真实 MILP：
    min 10*x0 + 20*x1 + 3*c0 + 5*c1 + 1*c2 + 0.5*c3
    s.t.
      x0 + x1 <= 1
      2*x0 + c0 >= 2
      -3*x1 + c1 >= 0
      c0 + c1 >= 2
      c2 >= -4
    变量：
      0: x0 binary [0, 1]
      1: x1 binary [0, 1]
      2: c0 continuous [0, 10]      -> clamp(0) = 0.0
      3: c1 continuous [2, 10]      -> clamp(0) = 2.0
      4: c2 continuous [-5, -1]     -> clamp(0) = -1.0
      5: c3 continuous [-inf, inf]  -> clamp(0) = 0.0
    最优解：x0=0, x1=0, c0=2, c1=2, c2=-4, c3=0? Wait: c3 obj=+0.5 and free -> unbounded if no lower!
    让 c3 系数为 0 且约束 c3 >= c0 - 2，或下界 [-inf, -1] / 目标 0.0。
    这里对 c3 设约束 c3 - c0 >= -2 且 c3 <= 5, 目标 +0.5*c3 -> 最优 c3 = 0.0, c2 = -1.0 (obj=10? wait c2 coef=-1? 设 c2 coef=-1 则最优 c2=-1, obj=17)。
    """
    handle = highspy.Highs()
    if log_file is not None:
        handle.setOptionValue("output_flag", True)
        handle.setOptionValue("log_to_console", False)
        handle.setOptionValue("log_file", log_file)
    else:
        handle.setOptionValue("output_flag", False)

    inf = float("inf")
    handle.addVar(0.0, 1.0)
    handle.addVar(0.0, 1.0)
    handle.addVar(0.0, 10.0)
    handle.addVar(2.0, 10.0)
    handle.addVar(-5.0, -1.0)
    handle.addVar(-inf, inf)
    handle.changeColsIntegrality(
        2,
        [0, 1],
        [highspy.HighsVarType.kInteger, highspy.HighsVarType.kInteger],
    )
    handle.changeColsCost(6, [0, 1, 2, 3, 4, 5], [10.0, 20.0, 3.0, 5.0, -1.0, 1.0])
    handle.addRow(-inf, 1.0, 2, [0, 1], [1.0, 1.0])
    handle.addRow(2.0, inf, 2, [0, 2], [2.0, 1.0])
    handle.addRow(0.0, inf, 2, [1, 3], [-3.0, 1.0])
    handle.addRow(2.0, inf, 2, [2, 3], [1.0, 1.0])
    handle.addRow(-2.0, inf, 2, [5, 2], [1.0, -1.0])
    return handle


def _hard_knapsack_mps(*, n: int = 240, m: int = 80, seed: int = 11) -> str:
    """生成紧约束 0-1 多维背包问题（全 0 不可行：覆盖需求 >= 10，使极短时限下无可行解）。"""
    rng = np.random.default_rng(seed)
    vars_ = tuple(
        LinearVariable(name=f"x{i:03d}", lower=0.0, upper=1.0, kind="binary")
        for i in range(n)
    )
    rows = []
    for row_idx in range(m):
        weights = rng.integers(1, 10, size=n)
        terms = tuple(((f"x{i:03d}", 0), float(weights[i])) for i in range(n))
        sense = "ge" if row_idx < 10 else "le"
        rhs = 200.0 if sense == "ge" else 180.0
        rows.append(
            LinearConstraint(
                name=f"row_{row_idx:03d}",
                expression=LinearExpression(terms=terms),
                sense=sense,
                rhs=rhs,
            )
        )
    obj_terms = tuple(((f"x{i:03d}", 0), float(i + 1)) for i in range(n))
    model = LinearModel(
        variables=vars_,
        constraints=tuple(rows),
        objective=LinearExpression(terms=obj_terms),
        sense="minimize",
    )
    text, _ = write_mps(model)
    return text


def test_cli_default_and_off_byte_identical_and_on_demand_fallback(tmp_path: Path) -> None:
    """7 参数默认调用与 --acceleration_mode off 逐字节一致；on_demand_mutex 干净回退原路径。"""
    mps_path = tmp_path / "problem.mps"
    mps_path.write_text(_sample_milp_mps(), encoding="utf-8")

    proc_default = subprocess.run(
        [
            HIGHS_ADAPTER_PATH,
            "problem.mps",
            "--solution_file",
            "default.sol",
            "--time_limit",
            "30",
            "--random_seed",
            "0",
        ],
        cwd=tmp_path,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc_default.returncode == 0, proc_default.stderr
    default_bytes = (tmp_path / "default.sol").read_bytes()

    proc_off = subprocess.run(
        [
            HIGHS_ADAPTER_PATH,
            "problem.mps",
            "--solution_file",
            "off.sol",
            "--time_limit",
            "30",
            "--random_seed",
            "0",
            "--acceleration_mode",
            "off",
        ],
        cwd=tmp_path,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc_off.returncode == 0, proc_off.stderr
    assert (tmp_path / "off.sol").read_bytes() == default_bytes

    proc_mutex = subprocess.run(
        [
            HIGHS_ADAPTER_PATH,
            "problem.mps",
            "--solution_file",
            "mutex.sol",
            "--time_limit",
            "30",
            "--random_seed",
            "0",
            "--acceleration_mode",
            "on_demand_mutex",
            "--threads",
            "2",
        ],
        cwd=tmp_path,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc_mutex.returncode == 0, proc_mutex.stderr
    facts = _parse_solution((tmp_path / "mutex.sol").read_text(encoding="utf-8"))
    assert facts["model_status"] == "HighsModelStatus.kOptimal"
    assert facts["has_feasible_solution"] == "true"
    assert float(facts["objective_value"]) == pytest.approx(7.0)
    assert "solve_domain" not in facts
    assert "original_model_verified" not in facts


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--acceleration_mode", "bogus"],
        ["--acceleration_mode"],
        ["--threads", "0"],
        ["--threads", "-2"],
        ["--threads", "1.5"],
        ["--threads", "+2"],
        ["--threads", "abc"],
        ["--threads"],
        ["--unknown", "1"],
        ["--threads", "1", "--threads", "2"],
        ["--acceleration_mode", "off", "--acceleration_mode", "off"],
    ],
)
def test_cli_rejects_invalid_structured_flags_with_exit_2(
    tmp_path: Path,
    extra_args: list[str],
) -> None:
    """非法或重复结构化参数严格以退出码 2 拒绝。"""
    mps_path = tmp_path / "problem.mps"
    mps_path.write_text(_sample_milp_mps(), encoding="utf-8")
    proc = subprocess.run(
        [
            HIGHS_ADAPTER_PATH,
            "problem.mps",
            "--solution_file",
            "out.sol",
            "--time_limit",
            "10",
            "--random_seed",
            "0",
            *extra_args,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2
    assert not (tmp_path / "out.sol").exists()


def test_remaining_solve_budget_non_renewing_and_post_read_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """预算耗尽严格返回 None（不重发 min_guard），且 time_limit 总在 readModel 后写入。"""
    mod = _load_solver_module()

    now = 1000.0
    monkeypatch.setattr(mod.time, "monotonic", lambda: now)

    # remaining == min_guard + io_margin -> None
    assert mod.remaining_solve_budget(1001.0, min_guard=0.4, io_margin=0.6) is None
    # remaining < min_guard + io_margin -> None
    assert mod.remaining_solve_budget(1000.5, min_guard=0.4, io_margin=0.6) is None
    # remaining already negative -> None
    assert mod.remaining_solve_budget(999.0, min_guard=0.4, io_margin=0.6) is None
    # remaining > min_guard + io_margin -> remaining - io_margin
    budget = mod.remaining_solve_budget(1002.5, min_guard=0.4, io_margin=0.6)
    assert budget == pytest.approx(1.9)

    with pytest.raises(ValueError):
        mod.remaining_solve_budget(1005.0, min_guard=-0.1, io_margin=0.5)
    with pytest.raises(ValueError):
        mod.remaining_solve_budget(float("nan"), min_guard=0.1, io_margin=0.5)

    # Verify time_limit is set AFTER handle.readModel()
    mps_path = tmp_path / "problem.mps"
    mps_path.write_text(_sample_milp_mps(), encoding="utf-8")
    events: list[str] = []
    real_highs = highspy.Highs

    class _OrderSpy:
        def __init__(self) -> None:
            self._inner = real_highs()

        def readModel(self, path: str):
            events.append("readModel")
            return self._inner.readModel(path)

        def setOptionValue(self, key: str, value):
            events.append(f"setOptionValue:{key}")
            return self._inner.setOptionValue(key, value)

        def run(self):
            events.append("run")
            return self._inner.run()

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    monkeypatch.setattr(highspy, "Highs", _OrderSpy)
    handle, status = mod._solve_model(
        str(mps_path), 10.0, 0, acceleration_mode="on_demand_mutex", threads=1
    )
    assert status == highspy.HighsModelStatus.kOptimal
    assert events.index("readModel") < events.index("setOptionValue:time_limit")
    assert events.index("setOptionValue:time_limit") < events.index("run")
    assert "setOptionValue:threads" in events
    assert handle.getObjectiveValue() == pytest.approx(7.0)


def test_highs_set_solution_four_states_real_milp(tmp_path: Path) -> None:
    """真实 highspy 1.7.2 setSolution 四态闭环：
    (1) 完整合法解；(2a) 部分整数 + 有限占位值触发内置 LP 修复；
    (2b) NaN/Inf 在调用 setSolution 前拒绝；(3) 不可行离散赋值无句柄污染无缝回退；
    (4) 紧凑 time_limit 干净退出不误报可行。
    """
    mod = _load_solver_module()

    # Helper clamping checks across [0,10], [2,10], [-5,-1], [-inf, inf]
    base_h = _build_warm_start_milp()
    partial_vec = mod.build_partial_warm_start(base_h, {0: 1.0, 1: 0.0})
    assert partial_vec == [1.0, 0.0, 0.0, 2.0, -1.0, 0.0]
    assert all(math.isfinite(v) for v in partial_vec)
    with pytest.raises(ValueError, match="越界"):
        mod.build_partial_warm_start(base_h, {99: 1.0})

    # State 1: complete valid warm start (x0=0, x1=0, c0=2, c1=2, c2=-1, c3=0 -> obj = 3*2+5*2+1=17)
    h1 = _build_warm_start_milp()
    complete_start = [0.0, 0.0, 2.0, 2.0, -1.0, 0.0]
    assert mod.inject_warm_start(h1, complete_start) == highspy.HighsStatus.kOk
    assert h1.run() == highspy.HighsStatus.kOk
    assert h1.getModelStatus() == highspy.HighsModelStatus.kOptimal
    assert h1.getObjectiveValue() == pytest.approx(17.0)

    # State 2a: partial integer + clamped finite placeholder triggers built-in LP repair
    log_2a = tmp_path / "state2a.log"
    h2a = _build_warm_start_milp(log_file=str(log_2a))
    partial_2a = mod.build_partial_warm_start(h2a, {0: 0.0, 1: 1.0})
    assert partial_2a == [0.0, 1.0, 0.0, 2.0, -1.0, 0.0]
    assert mod.inject_warm_start(h2a, partial_2a) == highspy.HighsStatus.kOk
    assert h2a.run() == highspy.HighsStatus.kOk
    assert h2a.getModelStatus() == highspy.HighsModelStatus.kOptimal
    assert h2a.getObjectiveValue() == pytest.approx(17.0)
    log_text = log_2a.read_text(encoding="utf-8")
    assert "Attempting to find feasible solution for (partial) user-supplied values of discrete variables" in log_text

    # State 2b: NaN / Inf rejected BEFORE handle.setSolution is ever called
    h2b_real = _build_warm_start_milp()
    set_solution_calls: list[object] = []

    class _HandleSpy:
        def __init__(self, inner) -> None:
            self._inner = inner

        def setSolution(self, sol):
            set_solution_calls.append(sol)
            return self._inner.setSolution(sol)

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    h2b = _HandleSpy(h2b_real)
    for bad_val in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="非有限"):
            mod.build_partial_warm_start(h2b, {0: bad_val})
        with pytest.raises(ValueError, match="非有限"):
            mod.inject_warm_start(h2b, [bad_val, 0.0, 0.0, 2.0, -1.0, 0.0])
    with pytest.raises(ValueError, match="列数"):
        mod.inject_warm_start(h2b, [0.0, 0.0])
    assert set_solution_calls == []

    # State 3: infeasible discrete seed (x0=1, x1=1 violates x0+x1<=1) -> clean fallback, zero contamination
    h3 = _build_warm_start_milp()
    infeasible_seed = mod.build_partial_warm_start(h3, {0: 1.0, 1: 1.0})
    assert mod.inject_warm_start(h3, infeasible_seed) == highspy.HighsStatus.kOk
    assert h3.run() == highspy.HighsStatus.kOk
    assert h3.getModelStatus() == highspy.HighsModelStatus.kOptimal
    assert h3.getObjectiveValue() == pytest.approx(17.0)

    # State 4: tight time_limit exits cleanly with kTimeLimit and has_feasible_solution == false
    mps_4 = tmp_path / "hard.mps"
    mps_4.write_text(_hard_knapsack_mps(), encoding="utf-8")
    h4 = highspy.Highs()
    h4.setOptionValue("output_flag", False)
    assert h4.readModel(str(mps_4)) == highspy.HighsStatus.kOk
    bad_start = mod.build_partial_warm_start(h4, {0: 0.0})
    assert mod.inject_warm_start(h4, bad_start) == highspy.HighsStatus.kOk
    h4.setOptionValue("time_limit", 1e-9)
    h4.run()
    status4 = h4.getModelStatus()
    assert status4 == highspy.HighsModelStatus.kTimeLimit
    facts4 = _parse_solution(mod._solution_text(h4, status4))
    assert facts4["has_feasible_solution"] == "false"
    assert facts4["objective_value"] == "none"
    assert facts4["primals"] == []


def test_verified_incumbent_slot_atomic_replace_and_deadline_handoff(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """单槽位原子写盘（os.replace）与后续阶段预算耗尽/无更优解时完整保全已有已核查解。"""
    mod = _load_solver_module()
    sol_path = tmp_path / "solution.sol"

    # 1. Minimize vs Maximize sense, unverified rejection, and equal/worse rejection
    slot_min = mod.VerifiedIncumbentSlot(sense="minimize", sol_path=str(sol_path))
    assert not slot_min.record(
        objective_value=5.0,
        primals=[1.0, 2.0],
        original_model_verified=False,
    )
    assert slot_min.incumbent is None
    assert not sol_path.exists()

    # Spy os.replace for atomic same-dir temp file write
    replace_calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def _spy_replace(src: str, dst: str) -> None:
        replace_calls.append((src, dst))
        assert Path(src).parent == Path(dst).parent
        assert Path(src).exists()
        real_replace(src, dst)

    monkeypatch.setattr(mod.os, "replace", _spy_replace)
    assert slot_min.record(
        objective_value=10.0,
        primals=[1.0, 3.5],
        model_status="HighsModelStatus.kTimeLimit",
        best_bound=6.0,
        relative_gap=0.4,
        solve_domain="valid_relaxation",
        original_model_verified=True,
    )
    assert len(replace_calls) == 1
    assert replace_calls[0][1] == str(sol_path)
    # Temp file should have been moved away; no leftover .tmp files
    assert [p.name for p in tmp_path.iterdir()] == ["solution.sol"]

    # Equal or worse candidate does NOT overwrite
    assert not slot_min.record(objective_value=10.0, primals=[2.0, 2.0])
    assert not slot_min.record(objective_value=12.0, primals=[3.0, 1.0])
    assert slot_min.incumbent.objective_value == 10.0

    # Strictly better candidate atomically replaces sol_path
    assert slot_min.record(
        objective_value=7.0,
        primals=[1.0, 2.0],
        model_status="HighsModelStatus.kTimeLimit",
        best_bound=6.5,
        relative_gap=0.07142857142857142,
        solve_domain="valid_relaxation",
        original_model_verified=True,
    )
    assert len(replace_calls) == 2
    saved_before_stage2 = sol_path.read_bytes()

    # Simulate failed atomic replace -> prior file intact and temp file cleaned up
    def _failing_replace(src: str, dst: str) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(mod.os, "replace", _failing_replace)
    with pytest.raises(OSError, match="simulated rename failure"):
        slot_min.record(objective_value=6.8, primals=[1.0, 1.9])
    assert sol_path.read_bytes() == saved_before_stage2
    assert slot_min.incumbent.objective_value == 7.0
    assert [p.name for p in tmp_path.iterdir()] == ["solution.sol"]

    # Maximize sense check
    slot_max = mod.VerifiedIncumbentSlot(sense="maximize")
    assert slot_max.record(objective_value=5.0, primals=[1.0])
    assert not slot_max.record(objective_value=4.0, primals=[0.8])
    assert slot_max.record(objective_value=8.0, primals=[1.6])
    assert slot_max.incumbent.objective_value == 8.0

    # 2. Deadline handoff: Stage 1 persisted verified incumbent, Stage 2 exhausts budget
    monkeypatch.setattr(mod.os, "replace", real_replace)
    stage2_deadline = time.monotonic() + 0.1
    assert mod.remaining_solve_budget(stage2_deadline, min_guard=0.2, io_margin=0.2) is None
    # Even if Stage 2 ran with tight budget and hit kTimeLimit without incumbent:
    mps_hard = tmp_path / "hard.mps"
    mps_hard.write_text(_hard_knapsack_mps(), encoding="utf-8")
    h_stage2 = highspy.Highs()
    h_stage2.setOptionValue("output_flag", False)
    assert h_stage2.readModel(str(mps_hard)) == highspy.HighsStatus.kOk
    h_stage2.setOptionValue("time_limit", 1e-9)
    h_stage2.run()
    assert not slot_min.record_from_handle(
        h_stage2, h_stage2.getModelStatus(), original_model_verified=True
    )
    assert sol_path.read_bytes() == saved_before_stage2
    facts = _parse_solution(sol_path.read_text(encoding="utf-8"))
    assert facts["has_feasible_solution"] == "true"
    assert float(facts["objective_value"]) == pytest.approx(7.0)
    assert facts["primals"] == pytest.approx([1.0, 2.0])
    assert facts["solve_domain"] == "valid_relaxation"
    assert facts["original_model_verified"] == "true"


def test_optional_additive_solution_headers_only_when_non_default() -> None:
    """默认 original+verified 不输出附加头行；非默认 solve_domain/subdomain 字段正确写出。"""
    mod = _load_solver_module()

    default_text = mod._format_solution_v1(
        model_status="HighsModelStatus.kOptimal",
        feasible=True,
        objective=7.0,
        best_bound=7.0,
        relative_gap=0.0,
        primals=[1.0, 2.0],
    )
    assert "solve_domain" not in default_text
    assert "original_model_verified" not in default_text
    assert "subdomain_" not in default_text

    restricted_text = mod._format_solution_v1(
        model_status="HighsModelStatus.kOptimal",
        feasible=True,
        objective=15.0,
        best_bound=None,
        relative_gap=None,
        primals=[1.0, 5.0],
        solve_domain="restricted",
        original_model_verified=True,
        subdomain_status="HighsModelStatus.kOptimal",
        subdomain_best_bound=15.0,
        subdomain_relative_gap=0.0,
    )
    facts = _parse_solution(restricted_text)
    assert facts["solve_domain"] == "restricted"
    assert facts["original_model_verified"] == "true"
    assert facts["subdomain_status"] == "HighsModelStatus.kOptimal"
    assert float(facts["subdomain_best_bound"]) == pytest.approx(15.0)
    assert float(facts["subdomain_relative_gap"]) == pytest.approx(0.0)
    assert facts["primals"] == pytest.approx([1.0, 5.0])

    with pytest.raises(ValueError, match="solve_domain"):
        mod._format_solution_v1(
            model_status="HighsModelStatus.kOptimal",
            feasible=False,
            objective=None,
            best_bound=None,
            relative_gap=None,
            primals=[],
            solve_domain="bogus",
        )
    with pytest.raises(ValueError, match="非有限"):
        mod._format_solution_v1(
            model_status="HighsModelStatus.kOptimal",
            feasible=True,
            objective=7.0,
            best_bound=7.0,
            relative_gap=0.0,
            primals=[1.0],
            solve_domain="restricted",
            subdomain_best_bound=float("nan"),
        )
