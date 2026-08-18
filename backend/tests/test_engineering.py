"""工程指标单元测试:能量平衡、峰值需量、容量利用率、负荷满足率。

平衡表用守恒数据验证残差约等于 0(02 §3.8);纯计算,不依赖 DB。
"""

import numpy as np
import pytest

from iesplan.metrics.engineering import (
    capacity_utilization,
    energy_balance_summary,
    load_met_ratio,
    peak_demand,
)


class TestEnergyBalanceSummary:
    """电/热/冷年度平衡表与残差。"""

    def test_electric_conservation(self):
        # 供给 = 需求,残差应为 0
        result = energy_balance_summary(
            {"p_pv": 1000, "p_grid_buy": 1500, "e_load": 2500},
            resolution="1h",
        )
        elec = result["electric"]
        assert elec["production_total_kwh"] == pytest.approx(2500)
        assert elec["consumption_total_kwh"] == pytest.approx(2500)
        assert elec["residual_kwh"] == pytest.approx(0.0, abs=1e-9)
        assert elec["loss_kwh"] == 0.0
        assert elec["unit"] == "kWh"
        assert elec["definition_version"] == "1.0.0"
        assert elec["refs"]

    def test_electric_with_battery_and_hp(self):
        # 含电池充放与热泵耗电的完整电平衡
        flows = {
            "p_pv": 2000,
            "p_grid_buy": 800,
            "p_bat_dis": 300,
            "e_load": 2500,
            "p_bat_ch": 200,
            "p_hp": 300,
            "p_chl": 100,
            "p_pump": 0,
            "p_grid_sell": 0,
        }
        result = energy_balance_summary(flows, resolution="1h")["electric"]
        # 供给 2000+800+300 = 3100;需求 2500+200+300+100 = 3100
        assert result["residual_kwh"] == pytest.approx(0.0, abs=1e-9)

    def test_heat_conservation_with_loss(self):
        # 供给 = (1 + λ_h) × 需求,残差为 0;λ_h 默认 0.05
        result = energy_balance_summary(
            {"q_b": 1000, "q_hp_h": 50, "q_del_h": 1000},
            resolution="1h",
        )
        heat = result["heat"]
        assert heat["production_total_kwh"] == pytest.approx(1050)
        assert heat["loss_kwh"] == pytest.approx(50)
        assert heat["residual_kwh"] == pytest.approx(0.0, abs=1e-9)

    def test_cool_conservation_with_custom_lambda(self):
        # 自定义 λ_c = 0.1:供给 = 1.1 × 1000 = 1100
        result = energy_balance_summary(
            {"q_chl": 1100, "q_del_c": 1000, "lambda_c": 0.1},
            resolution="1h",
        )
        cool = result["cool"]
        assert cool["loss_kwh"] == pytest.approx(100)
        assert cool["residual_kwh"] == pytest.approx(0.0, abs=1e-9)

    def test_hourly_series_integrated(self):
        # 逐时 1 kW × 8760 h = 8760 kWh(自动推断 1h 分辨率)
        result = energy_balance_summary({"p_pv": np.ones(8760), "e_load": np.ones(8760)})
        elec = result["electric"]
        assert elec["production_total_kwh"] == pytest.approx(8760, abs=0.5)
        assert elec["consumption_total_kwh"] == pytest.approx(8760, abs=0.5)
        assert elec["residual_kwh"] == pytest.approx(0.0, abs=1.0)

    def test_three_carriers_present(self):
        result = energy_balance_summary({"e_load": 100, "q_del_h": 100, "q_del_c": 100})
        assert set(result.keys()) == {"electric", "heat", "cool"}

    def test_unknown_resolution_rejected(self):
        with pytest.raises(ValueError):
            energy_balance_summary({"e_load": np.ones(10)}, resolution=None)


class TestPeakDemand:
    def test_peak_value_and_index(self):
        r = peak_demand([1, 2, 5, 3], resolution="1h")
        assert r["peak_kw"] == 5.0
        assert r["peak_index"] == 2
        assert r["n_steps"] == 4
        assert r["unit"] == "kW"
        assert r["definition_version"] == "1.0.0"
        assert r["refs"]

    def test_scalar_input(self):
        r = peak_demand(100)
        assert r["peak_kw"] == 100.0
        assert r["peak_index"] == 0

    def test_numpy_series(self):
        r = peak_demand(np.array([0.0, 3.5, 1.0]), resolution="1h")
        assert r["peak_kw"] == pytest.approx(3.5)

    def test_invalid_series(self):
        with pytest.raises(ValueError):
            peak_demand([1.0, float("nan")])


class TestCapacityUtilization:
    def test_half_utilization(self):
        # 100 kW × 8760 h = 876000 kWh 满负荷;438000 对应 50%
        r = capacity_utilization(100, 438_000, resolution="1h")
        assert r["ratio"] == pytest.approx(0.5)
        assert r["unit"] == "-"
        assert r["definition_version"] == "1.0.0"

    def test_zero_capacity(self):
        r = capacity_utilization(0, 1000, resolution="1h")
        assert r["ratio"] is None
        assert "未定义" in r["note"]

    def test_full_load(self):
        r = capacity_utilization(50, 50 * 8760, resolution="1h")
        assert r["ratio"] == pytest.approx(1.0, abs=1e-9)


class TestLoadMetRatio:
    def test_partial_met(self):
        r = load_met_ratio(800, 1000)
        assert r["ratio"] == pytest.approx(0.8)
        assert r["unmet_kwh"] == pytest.approx(200)
        assert r["unit"] == "kWh"
        assert r["definition_version"] == "1.0.0"
        assert r["refs"]

    def test_full_met(self):
        r = load_met_ratio(1000, 1000)
        assert r["ratio"] == pytest.approx(1.0)
        assert r["unmet_kwh"] == 0.0

    def test_over_delivery(self):
        r = load_met_ratio(1200, 1000)
        assert r["ratio"] == pytest.approx(1.2)
        assert r["unmet_kwh"] == 0.0  # 未满足量不为负

    def test_zero_required(self):
        r = load_met_ratio(0, 0)
        assert r["ratio"] is None
        assert "未定义" in r["note"]

    def test_series_inputs(self):
        r = load_met_ratio(np.full(8760, 1.0), np.full(8760, 0.9))
        # delivered 8760 kWh,required 7884 kWh
        assert r["ratio"] == pytest.approx(8760 / 7884, rel=1e-3)
        assert r["unmet_kwh"] == pytest.approx(0.0, abs=10)
