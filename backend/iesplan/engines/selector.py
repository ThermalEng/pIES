"""算法选择(03 §9.3:审查意见第 5 条「算法选择被忽略」修复)。

calc_config.algorithm{mode, name} → (计算引擎命令 id, solver_opts):
- mode='auto' 或缺省 → 默认 ies.algo.milp_hybrid;
- name 未注册或已注册但无引擎命令映射 → 抛 NotFoundError 拒绝
  (不静默回退默认, codex 二次审核 Medium-2: 防止输入歧义);
- solver_opts 来源收敛(03 §9.4):task_params.solver_options > 快照 tolerances >
  solver.py 默认值(DEFAULT_MIP_REL_GAP=0.001 / DEFAULT_TIME_LIMIT=600);
- 随机 seed 统一:快照 random_seed 注入 options["seed"],引擎内 seed=42 硬编码移除。

计算引擎以命令 id 注册于 modeling/command.py(ies.command.compute.*),
executors 经 modeling.resolve_function_ref 取函数(不再直接 import 引擎函数)。
"""

from __future__ import annotations

from typing import Final

from iesplan.core.errors import NotFoundError
from iesplan.core.registry import DEFAULT_ALGORITHM, get_algorithm

#: 算法 id → 计算引擎命令(03 §9.3;仅注册已实现函数,阶段 B 的 lp_relax
#: 变体实现前不暴露为可选算法,避免选到不存在的引擎命令)
ALGO_TO_ENGINE: Final[dict[str, str]] = {
    "ies.algo.milp_hybrid": "ies.command.compute.evaluate_plan.v1",
    "ies.algo.mc_sampling": "ies.command.compute.uncertainty.v1",  # 采样/不确定性(不参与 calc)
}

#: 快照 tolerances 键 → solver 选项键(03 §9.4 收敛精度来源统一)
_TOLERANCE_KEYS: Final[dict[str, str]] = {
    "gap_rel": "mip_rel_gap",
    "time_limit_s": "timeout",
}

#: solver.py 默认值(03 §9.4 兜底层)
DEFAULT_MIP_REL_GAP: Final[float] = 0.001
DEFAULT_TIME_LIMIT: Final[float] = 600.0


def select_engine(calc_config: dict, task_type: str, snapshot: object | None = None) -> tuple[str, dict]:
    """按计算配置选择引擎命令与求解选项(03 §9.3/§9.4)。

    参数:
        calc_config: 计算配置 dict(含 algorithm{tmode, name} 与 tolerances)。
        task_type: 任务类型(calc/optimization/uncertainty;确定默认算法归属)。
        snapshot: CalcSnapshot(提供 random_seed 与 tolerances 权威来源)。

    返回:
        (command_id, solver_opts)。solver_opts 合并顺序:
        snapshot.tolerances → task_params.solver_options(后者覆盖, 03 §9.4);
        随机 seed 来自 snapshot.random_seed(快照权威)。

    异常:
        NotFoundError: 手动指定的算法未注册, 或已注册但无引擎命令映射
        (lp_relax 阶段 B 未实现前选择它即拒绝, 不做静默回退;
        codex 二次审核 Medium-2)。
    """
    algorithm = calc_config.get("algorithm") or {}
    algo_id = DEFAULT_ALGORITHM
    if isinstance(algorithm, dict):
        mode = algorithm.get("mode") or "auto"
        name = algorithm.get("name")
        if mode != "auto" and isinstance(name, str) and name:
            algo_id = name
    elif isinstance(algorithm, str) and algorithm:
        algo_id = algorithm
    # 手动指定: 未注册或已注册但无引擎实现 → 抛错拒绝(不静默回退默认,
    # 防止"用户以为跑的算法 A 实际跑了默认算法 B"的输入歧义)
    if algo_id not in ALGO_TO_ENGINE and algo_id not in ("default", "ies.algo.default"):
        get_algorithm(algo_id)  # 未注册 → NotFoundError
        raise NotFoundError(
            f"算法已注册但无引擎命令映射: {algo_id}(实现前不可选择)",
            code="CONN-TYPE-002",
            params={"device_id": "", "type_id": algo_id},
        )
    command_id = ALGO_TO_ENGINE.get(
        DEFAULT_ALGORITHM if algo_id in ("default", "ies.algo.default") else algo_id,
        ALGO_TO_ENGINE[DEFAULT_ALGORITHM],
    )

    # 求解选项收敛(03 §9.4): 快照 tolerances → task_params.solver_options(后者覆盖)
    opts: dict = {}
    tolerances = {}
    if snapshot is not None:
        tolerances = getattr(snapshot, "tolerances", None) or {}
        seed = getattr(snapshot, "random_seed", None)
        if seed is not None:
            opts["seed"] = int(seed)
    if isinstance(tolerances, dict):
        for snap_key, opt_key in _TOLERANCE_KEYS.items():
            if snap_key in tolerances:
                opts[opt_key] = float(tolerances[snap_key])
    task_params = (calc_config.get("task_params") or {}) if isinstance(calc_config, dict) else {}
    solver_options = task_params.get("solver_options") or {}
    if isinstance(solver_options, dict):
        opts.update({k: float(v) for k, v in solver_options.items() if isinstance(v, (int, float))})
    opts.setdefault("mip_rel_gap", DEFAULT_MIP_REL_GAP)
    opts.setdefault("timeout", DEFAULT_TIME_LIMIT)
    return command_id, opts


__all__ = ["ALGO_TO_ENGINE", "DEFAULT_MIP_REL_GAP", "DEFAULT_TIME_LIMIT", "select_engine"]
