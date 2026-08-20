"""单因素敏感性分析与任务编排(03 §8.2/§8.3,审查意见第 7 条)。

纯计算(无 DB):
  - `rank_indicators`: 单参数扫描内,各指标对参数的影响度排序(按最大 |变化率|);
  - `rank_parameters`: 多参数单因素扫描之间,参数对结果的影响度排序
    (指标对参数的变化率/影响排序,任务范围)。

编排(DB 层,懒导入):
  - `run_sensitivity_analysis`: 创建 'analysis' 类型任务(任务参数含 sweeps),
    返回 task_id(03 §8.2);'analysis' 任务类型注册与 ck_tasks_type CHECK 迁移
    属里程碑 M5(03 §9.7),未注册时抛 AnalysisError 给出明确前置条件(不静默降级);
  - `build_analysis_payload`: SweepResult[] → evidence 载荷
    (result_kind='analysis_result' + sweeps 表 + summary + financial 块,03 §8.3)。

analysis 包导入本身无 DB 依赖(类型注解经 TYPE_CHECKING)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from iesplan.analysis.wrapper import (
    AnalysisError,
    SweepResult,
    _financial_to_dict,
    _jsonable_kpi,
    summarize_sweep,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from iesplan.analysis.wrapper import SweepSpec

__all__ = [
    "build_analysis_payload",
    "build_sensitivity_task_config",
    "rank_indicators",
    "rank_parameters",
    "run_sensitivity_analysis",
]


# ---------------------------------------------------------------------------
# 影响排序(指标对参数的变化率 / 影响排序)
# ---------------------------------------------------------------------------


def rank_indicators(
    sweep_results: Sequence[SweepResult], indicator_keys: Sequence[str] | None = None
) -> list[dict]:
    """单参数扫描:指标对参数的影响度排序(按最大 |变化率| 降序,03 §8.2)。

    返回: [{indicator, unit, impact, max_change_rate, min_change_rate,
    direction('positive'/'negative'/'mixed'/'flat'), monotonicity}, ...]。
    impact = 各扫描点 |change_rate| 的最大值(相对基准点)。
    """
    table = summarize_sweep(list(sweep_results))
    keys = set(indicator_keys) if indicator_keys is not None else None
    ranked: list[dict] = []
    for key, ind in table["indicators"].items():
        if keys is not None and key not in keys:
            continue
        crs = [p["change_rate"] for p in ind["points"] if p.get("change_rate") is not None]
        impact = max(abs(c) for c in crs) if crs else 0.0
        max_cr = max(crs) if crs else 0.0
        min_cr = min(crs) if crs else 0.0
        if not crs or max_cr == min_cr == 0:
            direction = "flat"  # 无变化率或全部为 0(恒定指标)
        elif min_cr >= 0 and max_cr > 0:
            direction = "positive"
        elif max_cr <= 0 and min_cr < 0:
            direction = "negative"
        else:
            direction = "mixed"
        ranked.append(
            {
                "indicator": key,
                "unit": ind["unit"],
                "impact": impact,
                "max_change_rate": max_cr,
                "min_change_rate": min_cr,
                "direction": direction,
                "monotonicity": ind["monotonicity"],
            }
        )
    ranked.sort(key=lambda d: d["impact"], reverse=True)
    return ranked


def rank_parameters(
    sweep_sets: Mapping[str, Sequence[SweepResult]],
    indicator_keys: Sequence[str] | None = None,
) -> list[dict]:
    """多参数单因素扫描:参数对结果的影响度排序(按最大 |变化率| 降序)。

    sweep_sets: param_path → 该参数扫描的 SweepResult 列表;每个参数的影响度
    = 该参数各指标影响度(indicator 级 max|change_rate|)的最大值,并给出主导
    指标(top_indicator)与方向。
    返回: [{param_path, unit, impact, top_indicator, top_change_rate,
    direction('positive'/'negative'), ...}, ...]。
    """
    keys = set(indicator_keys) if indicator_keys is not None else None
    ranked: list[dict] = []
    for param_path, results in sweep_sets.items():
        table = summarize_sweep(list(results))
        best: dict | None = None
        for key, ind in table["indicators"].items():
            if keys is not None and key not in keys:
                continue
            crs = [p["change_rate"] for p in ind["points"] if p.get("change_rate") is not None]
            if not crs:
                continue
            impact = max(abs(c) for c in crs)
            if best is None or impact > best["impact"]:
                best = {
                    "impact": impact,
                    "indicator": key,
                    "change_rate": max(crs, key=abs),
                }
        entry: dict = {
            "param_path": param_path,
            "unit": table["unit"],
            "impact": 0.0,
            "top_indicator": None,
            "top_change_rate": None,
            "direction": "flat",
        }
        if best is not None:
            entry.update(
                {
                    "impact": best["impact"],
                    "top_indicator": best["indicator"],
                    "top_change_rate": best["change_rate"],
                    "direction": "positive" if best["change_rate"] > 0 else "negative",
                }
            )
        ranked.append(entry)
    ranked.sort(key=lambda d: d["impact"], reverse=True)
    return ranked


# ---------------------------------------------------------------------------
# 证据载荷(03 §8.3)
# ---------------------------------------------------------------------------


def build_analysis_payload(sweep_results: Sequence[SweepResult]) -> dict:
    """SweepResult[] → evidence payload(03 §8.3)。

    输出: {"result_kind": "analysis_result", "sweeps": [逐点表(含 financial 列)],
    "summary": summarize_sweep, "sensitivity": {rank_indicators},
    "financial": 基准点(首个 ok 结果)财务块}。
    """
    results = list(sweep_results)
    ok = [r for r in results if r.status == "ok"]
    base_fin = ok[0].financial if ok else None
    return {
        "result_kind": "analysis_result",
        "sweeps": [
            {
                "param_path": r.param_path,
                "param_value": r.param_value,
                "unit": r.unit,
                "status": r.status,
                "kpi": _jsonable_kpi(r.kpi),
                "financial": _financial_to_dict(r.financial),
                "solver_status": r.solver_status,
            }
            for r in results
        ],
        "summary": summarize_sweep(results),
        "sensitivity": {"rank_indicators": rank_indicators(results)},
        "financial": _financial_to_dict(base_fin),
    }


# ---------------------------------------------------------------------------
# 任务编排(DB 层,03 §8.2 run_sensitivity_analysis)
# ---------------------------------------------------------------------------


def build_sensitivity_task_config(
    sweeps: Sequence[SweepSpec], base_config: dict | None = None
) -> dict:
    """SweepSpec[] → 任务配置 dict(可 JSON 落库;校验 sweeps 非空、values 非空)。"""
    if not sweeps:
        raise AnalysisError(
            "sweeps 不能为空", code="ANA-SWEEP-001", message_key="ies.diag.analysis.empty_sweeps"
        )
    config: dict = {
        "sweeps": [
            {"param_path": s.param_path, "values": list(s.values), "unit": s.unit} for s in sweeps
        ]
    }
    if base_config:
        config["base_config"] = dict(base_config)
    return config


def run_sensitivity_analysis(
    db: Session,
    project_id: int,
    base_config: dict,
    sweeps: list[SweepSpec],
) -> int:
    """创建 'analysis' 类型任务(任务参数含 sweeps)并返回 task_id(03 §8.2)。

    编排层: 懒导入 services.tasks/identity(analysis 包导入无 DB 依赖)。
    前置条件: 'analysis' 任务类型已注册(里程碑 M5: TASK_TYPES/POOL_BY_TYPE 增加
    'analysis' + ck_tasks_type CHECK 迁移,03 §9.7),未注册时抛 AnalysisError。
    请求用户: base_config['requested_by'](API 路由注入的认证用户 id,03 §10.3)。
    """
    from iesplan.services import tasks as tasks_service  # noqa: PLC0415
    from iesplan.services.identity import get_user_by_id  # noqa: PLC0415

    if "analysis" not in getattr(tasks_service, "TASK_TYPES", ()):
        raise AnalysisError(
            "analysis 任务类型未注册: 需里程碑 M5 将 'analysis' 加入 TASK_TYPES/POOL_BY_TYPE"
            " 并迁移 ck_tasks_type CHECK(03 §9.7)",
            code="ANA-TASK-001",
            message_key="ies.diag.analysis.task_type_unregistered",
        )
    user_id = (base_config or {}).get("requested_by")
    user = get_user_by_id(db, int(user_id)) if user_id is not None else None
    if user is None:
        raise AnalysisError(
            "缺少请求用户(base_config.requested_by 须为认证用户 id)",
            code="ANA-TASK-002",
            message_key="ies.diag.analysis.missing_user",
        )
    task = tasks_service.create_task(
        db,
        user,
        project_id,
        "analysis",
        config=build_sensitivity_task_config(sweeps, base_config),
    )
    return int(task.id)
