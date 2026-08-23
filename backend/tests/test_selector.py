"""计算引擎命令化与算法选择(03 §9.3/§9.4: 意见第 5 条「算法选择被忽略」修复)。

- engines/selector.py: algorithm{mode,name} → (command_id, solver_opts),
  tolerances/seed 来源收敛(快照权威);
- modeling/command.py: ies.command.compute.* 计算引擎命令注册 + 分发;
- executors._engine_entry: 经命令注册表取函数(不再直接 import 引擎)。
"""

from __future__ import annotations

import pytest

from iesplan.core.errors import NotFoundError
from iesplan.engines.selector import (
    ALGO_TO_ENGINE,
    DEFAULT_MIP_REL_GAP,
    DEFAULT_TIME_LIMIT,
    select_engine,
)
from iesplan.modeling.command import (
    clear_commands,
    get_compute_entry,
    init_compute_commands,
)


@pytest.fixture(autouse=True)
def _clean_commands():
    # 快照注册表并恢复(不净空后丢弃: 计算命令注册表是进程级共享状态,
    # 清空会泄漏到后续测试文件, 导致 runner 测试找不到引擎命令)
    from iesplan.modeling.command import snapshot as cmd_snapshot

    before = cmd_snapshot()
    clear_commands()
    init_compute_commands()
    yield
    clear_commands()
    from iesplan.modeling.command import register_command

    for command_id in before:
        from iesplan.modeling.command import get_command

        cmd = get_command(command_id)
        if cmd is not None:
            register_command(cmd)


class TestAtomicReplace:
    """BE-REG-02: 注册表原子替换语义。"""

    def test_replace_all_is_atomic(self):
        from iesplan.modeling.command import (
            ModuleCommand,
            get_command,
            replace_all_commands,
            snapshot,
        )

        staged = {
            "ies.command.compute.evaluate_plan.v1": ModuleCommand(
                command_id="ies.command.compute.evaluate_plan.v1",
                function_ref="iesplan.engines.eval_run.evaluate_plan",
                version="1.0.0",
            ),
            "ies.command.model.ies.device.pv.mechanism.1.4.0": ModuleCommand(
                command_id="ies.command.model.ies.device.pv.mechanism.1.4.0",
                function_ref="iesplan.modeling.functions.pv_output",
                version="1.4.0",
            ),
        }
        replace_all_commands(staged)
        assert get_command("ies.command.model.ies.device.pv.mechanism.1.4.0") is not None
        assert len(snapshot()) == 2

    def test_failed_build_keeps_old_snapshot(self, monkeypatch):
        """任一设备构建失败时旧快照完整保留(BE-REG-02 核心)。"""
        from iesplan.core.errors import AppError
        from iesplan.devices import DeviceModelDescriptor
        from iesplan.modeling import registry_loader
        from iesplan.modeling.command import ModuleCommand, snapshot

        # 先放入一个"旧"命令
        old = ModuleCommand(
            command_id="ies.command.compute.evaluate_plan.v1",
            function_ref="iesplan.engines.eval_run.evaluate_plan",
            version="1.0.0",
        )
        from iesplan.modeling.command import register_command

        register_command(old)
        before = snapshot()

        # 描述列表: 一个正常 + 一个构建必败(未知命令 ID → 组合根解析拒绝)
        good = DeviceModelDescriptor(
            type_id="ies.device.pv", version="1.4.0", name_zh="光伏", name_en="PV",
            model_method="mechanism", stateful=False, fidelity="medium",
            energy_carriers=("solar", "electric"), is_load=False,
            capabilities=("pv",), extends="ies.device.base", help_topic="",
            parameters={}, ports=(), time_series={}, states=(),
            model_commands={"pv": "ies.model-command.pv.generation@1.0.0"},
        )
        bad = DeviceModelDescriptor(
            type_id="ies.device.bogus", version="1.0.0", name_zh="坏设备", name_en="Bad",
            model_method="mechanism", stateful=False, fidelity="medium",
            energy_carriers=("electric",), is_load=False,
            capabilities=("pv",), extends="ies.device.base", help_topic="",
            parameters={}, ports=(), time_series={}, states=(),
            model_commands={"pv": "ies.model-command.unknown.fn@1.0.0"},
        )
        monkeypatch.setattr(
            registry_loader, "list_device_descriptors",
            lambda: [good, bad],
        )
        with pytest.raises(AppError):
            registry_loader.register_catalog_commands()
        # 旧快照完整保留(不出现部分替换, 也不含失败设备的半成品命令)
        assert snapshot() == before
        assert "ies.command.compute.evaluate_plan.v1" in before
        assert not any("bogus" in c for c in snapshot())


class TestSelector:
    def test_default_auto_selects_milp_hybrid(self):
        command_id, opts = select_engine({"algorithm": {"mode": "auto"}}, "calc")
        assert command_id == "ies.command.compute.evaluate_plan.v1"
        assert opts["mip_rel_gap"] == DEFAULT_MIP_REL_GAP
        assert opts["timeout"] == DEFAULT_TIME_LIMIT

    def test_manual_algorithm_maps_to_command(self):
        command_id, opts = select_engine(
            {"algorithm": {"mode": "manual", "name": "ies.algo.milp_hybrid"}}, "calc"
        )
        assert command_id == ALGO_TO_ENGINE["ies.algo.milp_hybrid"]

    def test_unknown_algorithm_rejected(self):
        # 未注册算法: 抛 NotFoundError 拒绝(不静默回退默认,
        # codex 二次审核 Medium-2: 防止输入歧义)
        with pytest.raises(NotFoundError):
            select_engine(
                {"algorithm": {"mode": "manual", "name": "ies.algo.bogus"}}, "calc"
            )

    def test_registered_but_no_engine_mapping_rejected(self):
        # 已注册(registry)但无引擎命令映射的算法(如 lp_relax 阶段 B 未实现):
        # 同样拒绝, 不静默回退默认
        with pytest.raises(NotFoundError):
            select_engine(
                {"algorithm": {"mode": "manual", "name": "ies.algo.lp_relax"}}, "calc"
            )

    def test_algorithm_str_form_rejected_unknown(self):
        with pytest.raises(NotFoundError):
            select_engine({"algorithm": "ies.algo.bogus"}, "calc")

    def test_solver_opts_merge_snapshot_tolerances_then_task_overrides(self):
        class FakeSnapshot:
            tolerances = {"gap_rel": 0.01, "time_limit_s": 60}
            random_seed = 12345

        command_id, opts = select_engine(
            {
                "algorithm": {"mode": "auto"},
                "task_params": {"solver_options": {"timeout": 30}},
            },
            "calc",
            snapshot=FakeSnapshot(),
        )
        # 快照 tolerances 映射: gap_rel → mip_rel_gap; time_limit_s → timeout
        assert opts["mip_rel_gap"] == 0.01
        # task_params.solver_options 覆盖快照 tolerances(03 §9.4)
        assert opts["timeout"] == 30
        # 快照 random_seed 注入(权威来源)
        assert opts["seed"] == 12345

    def test_planning_command_mapped(self):
        assert ALGO_TO_ENGINE["ies.algo.milp_hybrid"] == "ies.command.compute.evaluate_plan.v1"
        # 未实现引擎的算法不得出现在映射中(codex 二次审核 High-2)
        assert "ies.algo.lp_relax" not in ALGO_TO_ENGINE
        assert "ies.command.compute.evaluate_plan_lp.v1" not in (
            "ies.command.compute.evaluate_plan.v1",
            "ies.command.compute.run_planning.v1",
            "ies.command.compute.uncertainty.v1",
        )


class TestComputeCommands:
    def test_init_registers_compute_commands(self):
        # init_compute_commands 已注册全部已实现的计算引擎命令
        for command_id in (
            "ies.command.compute.evaluate_plan.v1",
            "ies.command.compute.run_planning.v1",
            "ies.command.compute.uncertainty.v1",
        ):
            assert get_compute_entry(command_id) is not None
        # 未实现函数不得注册为命令(阶段 B 的 lp_relax 实现前无死命令,
        # codex 二次审核 High-2)
        with pytest.raises(NotFoundError):
            get_compute_entry("ies.command.compute.evaluate_plan_lp.v1")

    def test_get_compute_entry_unknown_raises(self):
        with pytest.raises(NotFoundError):
            get_compute_entry("ies.command.compute.bogus.v1")

    def test_entry_resolves_to_engine_function(self):
        from iesplan.engines.eval_run import evaluate_plan

        fn = get_compute_entry("ies.command.compute.evaluate_plan.v1")
        assert fn is evaluate_plan

    def test_planning_entry_resolves(self):
        from iesplan.engines.planning import run_planning

        fn = get_compute_entry("ies.command.compute.run_planning.v1")
        assert fn is run_planning


class TestSeedPropagation:
    """High-5 修复: 快照 seed 经 selector 到达求解器(不再硬编码 42)。"""

    def test_evaluate_plan_uses_seed_from_opts(self, monkeypatch):
        import numpy as np

        from iesplan.engines import eval_run

        captured: dict = {}

        def fake_solve_milp(c_obj, integrality, bounds, cons, **kwargs):
            captured.update(kwargs)
            from iesplan.engines.solver import SolveResult

            return SolveResult(status="ok", objective=0.0, x=None, gap=0.0,
                               stop_reason="optimal")

        monkeypatch.setattr(eval_run, "solve_milp", fake_solve_milp)
        plan = {"devices": [], "reverse_feed_allowed": False}
        data = {k: np.zeros(1, dtype=np.float64) for k in ("e_load", "h_load", "c_load")}

        class FakeAxis:
            n = 1
            step_seconds = 3600.0

        opts = {"seed": 12345}
        eval_run.evaluate_plan(plan, data, FakeAxis(), opts)
        assert captured.get("seed") == 12345, f"solve_milp 未收到 seed: {captured}"

        # 缺省 seed 兜底 42
        captured.clear()
        eval_run.evaluate_plan(plan, data, FakeAxis(), {})
        assert captured.get("seed") == 42
