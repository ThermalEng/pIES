"""算法注册表(RR-P2-02: 从 core/registry 迁出, 单独模块)。

- 启动期受控加载: 静态注册 + id 唯一 + semver 校验;
- 不依赖任何业务模块(无 from iesplan.devices / iesplan.modeling 等导入);
- 供 engines/selector.py / engines/planning.py / services/config.py 调用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from iesplan.core.contracts.parameters import ParameterSpec
from iesplan.core.errors import AppError, NotFoundError

# ---------------------------------------------------------------------------
# 基础数据结构
# ---------------------------------------------------------------------------
_ID_PATTERN = re.compile(r"^ies\.algo\.[a-z][a-z0-9_]*$")
_SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


def _p(
    name: str,
    unit: str,
    min: float | None,
    max: float | None,
    default: object,
    help_key: str,
) -> ParameterSpec:
    return ParameterSpec(
        name=name,
        unit=unit,
        min=min,
        max=max,
        default=default,
        is_optimizable=False,
        existing_default=default if isinstance(default, (int, float)) and not isinstance(default, bool) else None,
        stock_or_addition="stock",
        help_key=help_key,
        enum=None,
    )


@dataclass(frozen=True, slots=True)
class AlgorithmSpec:
    """算法注册项(04 §2.3 algorithm 类)。"""

    algo_id: str
    version: str
    name_zh: str
    name_en: str
    capabilities: list[str]
    parameters: dict[str, ParameterSpec] = field(default_factory=dict)
    help_topic: str = ""

    @property
    def default_parameters(self) -> dict[str, object]:
        return {name: p.default for name, p in self.parameters.items()}


# ---------------------------------------------------------------------------
# 注册表本体
# ---------------------------------------------------------------------------
_ALGORITHMS: dict[str, AlgorithmSpec] = {}
_LOADED = False


def _check_version(version: str, what: str) -> None:
    if not _SEMVER_PATTERN.match(version):
        raise AppError(
            f"{what} 版本不合法: {version!r}",
            code="SYS-CFG-001",
            message_key="ies.diag.store.config_invalid",
            params={"item": what, "version": version},
        )


def _check_algo_id(rid: str, what: str) -> None:
    if not _ID_PATTERN.match(rid):
        raise AppError(
            f"{what} id 不合法: {rid!r}",
            code="SYS-CFG-001",
            message_key="ies.diag.store.config_invalid",
            params={"item": what, "id": rid},
        )
    if rid in _ALGORITHMS:
        raise AppError(
            f"{what} id 重复注册: {rid!r}",
            code="SYS-CFG-001",
            message_key="ies.diag.store.config_invalid",
            params={"item": what, "id": rid},
        )


def _register_algorithm(spec: AlgorithmSpec) -> None:
    _check_algo_id(spec.algo_id, "算法")
    _check_version(spec.version, spec.algo_id)
    _ALGORITHMS[spec.algo_id] = spec


def load_registry() -> None:
    """受控加载: 算法静态注册(幂等)。"""
    global _LOADED
    if _LOADED:
        return
    _register_algorithm(
        AlgorithmSpec(
            algo_id="ies.algo.milp_hybrid",
            version="1.0.0",
            name_zh="MILP 双层分解(容量层+运行层)",
            name_en="MILP Hybrid (capacity + operation decomposition)",
            capabilities=["milp", "capacity_design", "evaluation", "multi_objective", "irr_hard_constraint"],
            parameters={
                "gap_rel": _p("gap_rel", "-", 0.0, 0.1, 0.001, "help.param.algo.gap_rel"),
                "time_limit_s": _p("time_limit_s", "s", 1, 86400, 600, "help.param.algo.time_limit_s"),
                "seed": _p("seed", "-", 0, 2**31 - 1, 42, "help.param.algo.seed"),
                "n_typical_days": _p("n_typical_days", "d", 1, 365, 12, "help.param.algo.n_typical_days"),
                "irr_min": _p("irr_min", "-", 0.0, 1.0, 0.08, "help.param.algo.irr_min"),
                "discount_rate": _p("discount_rate", "-", 0.0, 1.0, 0.08, "help.param.algo.discount_rate"),
                "max_parallel": _p("max_parallel", "-", 1, 64, 4, "help.param.algo.max_parallel"),
            },
            help_topic="help.config.algorithm",
        )
    )
    _register_algorithm(
        AlgorithmSpec(
            algo_id="ies.algo.lp_relax",
            version="1.0.0",
            name_zh="LP 松弛快速评估(P1)",
            name_en="LP relaxation quick evaluation (P1)",
            capabilities=["lp", "evaluation", "fast_mode"],
            parameters={
                "time_limit_s": _p("time_limit_s", "s", 1, 86400, 300, "help.param.algo.time_limit_s"),
                "seed": _p("seed", "-", 0, 2**31 - 1, 42, "help.param.algo.seed"),
            },
            help_topic="help.config.algorithm",
        )
    )
    _register_algorithm(
        AlgorithmSpec(
            algo_id="ies.algo.mc_sampling",
            version="1.0.0",
            name_zh="蒙特卡洛场景采样(不确定性)",
            name_en="Monte Carlo scenario sampling (uncertainty)",
            capabilities=["mc", "uncertainty", "sampling", "evaluation"],
            parameters={
                "n_samples": _p("n_samples", "-", 1, 10000, 100, "help.param.algo.n_samples"),
                "seed_base": _p("seed_base", "-", 0, 2**31 - 1, 42, "help.param.algo.seed_base"),
                "max_parallel": _p("max_parallel", "-", 1, 64, 4, "help.param.algo.max_parallel"),
            },
            help_topic="help.config.algorithm",
        )
    )
    _LOADED = True


load_registry()

#: 默认算法(02 §5.8 双层分解默认策略)
DEFAULT_ALGORITHM: str = "ies.algo.milp_hybrid"


def get_algorithm(name: str) -> AlgorithmSpec:
    """按注册 id 取算法(未注册抛 NotFoundError)。

    特殊值 "default" / "ies.algo.default" 返回默认算法。
    """
    if name in ("default", "ies.algo.default"):
        name = DEFAULT_ALGORITHM
    spec = _ALGORITHMS.get(name)
    if spec is None:
        raise NotFoundError(
            f"算法未注册: {name}",
            code="CONN-TYPE-002",
            message_key="ies.diag.conn.type_unregistered",
            params={"device_id": "", "type_id": name},
        )
    return spec


def list_algorithms() -> list[AlgorithmSpec]:
    return list(_ALGORITHMS.values())


def _check_version_external(version: str, what: str) -> None:
    """对外公开 semver 校验(原 core.registry 公共符号, 测试侧继续消费)。"""
    _check_version(version, what)


__all__ = [
    "AlgorithmSpec",
    "DEFAULT_ALGORITHM",
    "get_algorithm",
    "list_algorithms",
    "load_registry",
    "_check_version_external",
]