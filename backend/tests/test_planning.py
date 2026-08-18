"""规划引擎单元测试(02 §5.3-§5.6/§6.2):迷你算例跑通、候选排序、IRR 硬约束过滤。

纯计算测试,不依赖 DB。
经济学设计(便于手算):电价 [0.2, 0.2, 2.0, 2.0] 元/kWh,负荷 1 kW 恒定;
- 基准年费用 = 0.2×2 + 2.0×2 = 4.4 元;
- 电池 2 kWh(单位投资 5 元/kWh,CAPEX 10 元):谷时充电 C = 0.8/η = 0.8421 kWh
  (SOC 0.5→0.9 触顶),峰时放电 D = η²·C = 0.76 kWh;年费用 = 2.8421×0.2 +
  1.24×2.0 = 3.0484 元,年节省 1.3516 元,IRR ≈ 10.4%;
- 光伏 1 kWp(单位投资 15 元/kWp,CAPEX 15 元):年节省 0.35 元,20 年内
  税后现金流 NPV(r=0) < CAPEX → IRR 为负,被 IRR 下限过滤;
- 组合(光伏+电池):CAPEX 25 元,年节省 1.7016 元,IRR ≈ 2.1% < 电池单装 10.4%。
"""

from datetime import UTC, datetime

import numpy as np
import pytest

from iesplan.core.timeaxis import TimeAxis
from iesplan.engines.planning import PlanCandidate, PlanningResult, run_planning


def make_axis(n: int = 4) -> TimeAxis:
    return TimeAxis(
        resolution="1h",
        n=n,
        step_minutes=60,
        utc_offset_minutes=480,
        t0_utc=datetime(2025, 1, 1, tzinfo=UTC),
        hour_of_year=np.arange(n, dtype=np.int64),
        day_of_year=np.zeros(n, dtype=np.int64),
        season=np.zeros(n, dtype=np.int64),
    )


def data() -> dict:
    return {
        "e_load": np.array([1000.0, 1000.0, 1000.0, 1000.0]),
        "h_load": np.array([0.0, 0.0, 0.0, 0.0]),
        "c_load": np.array([0.0, 0.0, 0.0, 0.0]),
        "tariff_buy": np.array([0.2, 0.2, 2.0, 2.0]),
        "tariff_sell": 0.35,
        "gas_price": 3.2,
        "emission_factor_grid": 0.581,
        "emission_factor_gas": 2.0,
    }


def plan_template() -> dict:
    """方案模板:存量电网 + 两个新增设备(光伏 1 kWp 网格、电池 2 kWh 网格)。"""
    return {
        "devices": [
            {"type": "ies.device.grid_connection",
             "params": {"max_import_power_kw": 5000, "max_export_power_kw": 0},
             "is_new": False},
            {"type": "ies.device.pv",
             "params": {"rated_capacity_kwp": 0, "max_capacity_kwp": 1,
                        "unit_invest_cost": 15, "efficiency": 0.20},
             "is_new": True},
            {"type": "ies.device.battery",
             "params": {"capacity_kwh": 0, "max_capacity_kwh": 2,
                        "rated_power_kw": 2, "unit_invest_cost": 5,
                        "initial_soc": 0.5, "min_soc": 0.1, "max_soc": 0.9},
             "is_new": True},
        ],
        "reverse_feed_allowed": False,
        "lambda_h": 0.05,
        "lambda_c": 0.08,
    }


class TestPlanningBasic:
    """迷你算例跑通与候选排序(02 §5.6 阶段 1 代理 + IRR 硬约束)。"""

    def test_candidates_sorted_and_best(self):
        opts = {
            "grid_step": {"ies.device.pv": 1.0, "ies.device.battery": 2.0},
            "irr_floor": 0.0,           # 放开下限,验证排序与过滤
            "project_years": 20,
            "depreciation_years": 10,
            "tax_rate": 0.25,
            "discount_rate": 0.08,
        }
        res = run_planning(plan_template(), data(), make_axis(), opts)
        assert res.status == "ok"
        assert isinstance(res, PlanningResult)
        # 基准年费用 = 直接购电:0.2×2 + 2.0×2 = 4.4 元(02 §5.3)
        assert res.baseline_cost == pytest.approx(4.4, abs=0.05)
        assert res.candidates, "应至少有一个候选"
        # 候选按 IRR 降序
        irrs = [c.irr for c in res.candidates]
        assert irrs == sorted(irrs, reverse=True)
        # 光伏单装年节省 0.35 元无法回收 CAPEX 15 元 → IRR 为负 → 被过滤
        pv_only = [c for c in res.candidates if set(c.capacities) == {"ies.device.pv"}]
        assert pv_only == []
        # 电池单装 IRR(≈10.4%)高于组合(≈2.1%)→ best = 电池 2 kWh
        bat = next(c for c in res.candidates if set(c.capacities) == {"ies.device.battery"})
        combo = next(c for c in res.candidates
                     if set(c.capacities) == {"ies.device.pv", "ies.device.battery"})
        assert bat.irr is not None and combo.irr is not None
        assert bat.irr > combo.irr
        assert res.best is not None
        assert res.best.capacities == {"ies.device.battery": 2.0}
        # 候选结构字段完整
        c = res.best
        assert isinstance(c, PlanCandidate)
        assert c.capex == pytest.approx(10.0)
        assert c.annual_op_cost == pytest.approx(3.0484, abs=0.05)
        assert c.annual_saving == pytest.approx(1.3516, abs=0.05)
        assert c.irr > 0.0
        assert npv_sign_consistent(c)
        # 组合容量正确
        assert combo.capacities["ies.device.pv"] == 1.0
        assert combo.capacities["ies.device.battery"] == 2.0
        assert combo.capex == pytest.approx(15.0 + 10.0)
        # 汇总诊断含过滤计数(光伏单装被过滤)
        summary = next(d for d in res.diagnostics if d["code"] == "ENG-PLAN-005")
        assert summary["params"]["filtered_by_irr"] >= 1

    def test_irr_floor_filters_all(self):
        # IRR 下限 0.15(15%)高于全部候选(最高 ≈10.4%)→ 全部过滤 → no_feasible
        opts = {
            "grid_step": {"ies.device.pv": 1.0, "ies.device.battery": 2.0},
            "irr_floor": 0.15,
            "project_years": 20,
            "depreciation_years": 10,
        }
        res = run_planning(plan_template(), data(), make_axis(), opts)
        assert res.status == "no_feasible"
        assert res.best is None
        assert res.candidates == []
        codes = [d["code"] for d in res.diagnostics]
        assert "ENG-PLAN-004" in codes  # 无候选诊断(含过滤计数)

    def test_baseline_infeasible(self):
        # 购电上限 1 kW < 负荷 10 kW,且新增光伏无法满足 → 基准不可行(02 §5.3)
        plan = {
            "devices": [
                {"type": "ies.device.grid_connection",
                 "params": {"max_import_power_kw": 1, "max_export_power_kw": 0},
                 "is_new": False},
                {"type": "ies.device.pv",
                 "params": {"rated_capacity_kwp": 0, "max_capacity_kwp": 1,
                            "unit_invest_cost": 15},
                 "is_new": True},
            ],
            "reverse_feed_allowed": False,
        }
        d = data()
        d["e_load"] = np.full(4, 10000.0)
        res = run_planning(plan, d, make_axis(), {})
        assert res.status == "no_feasible"
        assert res.baseline_cost is None
        codes = [d["code"] for d in res.diagnostics]
        assert "ENG-PLAN-001" in codes  # 基准方案无可行解

    def test_sampling_when_combo_explosion(self):
        # 小网格步长 → 组合数超上限 → 按优先级抽样(02 §5.8 规模控制)
        opts = {
            "grid_step": {"ies.device.pv": 0.1, "ies.device.battery": 0.2},
            "max_combinations": 10,
            "priority": {"ies.device.battery": 10.0, "ies.device.pv": 1.0},
            "irr_floor": 0.0,
            "project_years": 20,
            "depreciation_years": 10,
        }
        res = run_planning(plan_template(), data(), make_axis(), opts)
        assert res.status in ("ok", "no_feasible")
        codes = [d["code"] for d in res.diagnostics]
        assert "ENG-PLAN-003" in codes  # 抽样诊断
        assert len(res.candidates) <= 10


def npv_sign_consistent(c: PlanCandidate) -> bool:
    """NPV 为有限数值(现金流构建成功)。"""
    return c.npv is not None and np.isfinite(c.npv)
