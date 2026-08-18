"""方案评价引擎单元测试(02 §7/§8):4 步迷你算例手算校验电平衡与费用、电池充放互斥。

纯计算测试,不依赖 DB。
"""

from datetime import UTC, datetime

import numpy as np
import pytest

from iesplan.core.timeaxis import TimeAxis
from iesplan.engines.eval_run import evaluate_plan


def make_axis(n: int, step_minutes: int = 60) -> TimeAxis:
    """构造迷你时间轴(n 步,1h 或 30min 步长)。"""
    return TimeAxis(
        resolution="1h" if step_minutes == 60 else "30min",
        n=n,
        step_minutes=step_minutes,
        utc_offset_minutes=480,
        t0_utc=datetime(2025, 1, 1, tzinfo=UTC),
        hour_of_year=np.arange(n, dtype=np.int64),
        day_of_year=np.zeros(n, dtype=np.int64),
        season=np.zeros(n, dtype=np.int64),
    )


def base_plan(**overrides) -> dict:
    """迷你方案:电网(购电上限 5000 kW)+ 电池(2 kWh,1C)。"""
    plan = {
        "devices": [
            {
                "type": "ies.device.grid_connection",
                "params": {"max_import_power_kw": 5000, "max_export_power_kw": 0,
                           "export_tariff": 0.35, "demand_charge": 0},
                "is_new": False,
            },
            {
                "type": "ies.device.battery",
                "params": {"capacity_kwh": 2, "rated_power_kw": 2, "initial_soc": 0.5,
                           "min_soc": 0.1, "max_soc": 0.9,
                           "charge_efficiency": 0.95, "discharge_efficiency": 0.95},
                "is_new": False,
            },
        ],
        "reverse_feed_allowed": False,
        "lambda_h": 0.05,
        "lambda_c": 0.08,
    }
    plan.update(overrides)
    return plan


def base_data(**overrides) -> dict:
    """迷你数据:4 步 1 kW 电负荷,峰谷电价 [0.3, 0.3, 1.1, 1.1]。"""
    data = {
        "e_load": np.array([1000.0, 1000.0, 1000.0, 1000.0]),
        "h_load": np.array([0.0, 0.0, 0.0, 0.0]),
        "c_load": np.array([0.0, 0.0, 0.0, 0.0]),
        "tariff_buy": np.array([0.3, 0.3, 1.1, 1.1]),
        "tariff_sell": 0.35,
        "gas_price": 3.2,
        "emission_factor_grid": 0.581,
        "emission_factor_gas": 2.0,
    }
    data.update(overrides)
    return data


class TestBatteryArbitrage:
    """电池峰谷套利手算校验(02 §4.4/§7)。"""

    def test_energy_balance_and_mutual_exclusion(self):
        res = evaluate_plan(base_plan(), base_data(), make_axis(4))
        assert res.status == "ok"
        f = res.flows
        eps = 1e-6
        # 电平衡逐时手算:购电 + 光伏 + 放电 = 负荷 + 售电 + 充电(+ 热泵/制冷机/泵为 0)
        lhs = f["p_grid_buy"] + f["p_pv"] + f["p_bat_dis"]
        rhs = f["p_grid_sell"] + f["p_bat_ch"] + f["p_hp_elec"] + f["p_chiller_elec"] + f["p_pump"]
        assert np.allclose(lhs - rhs, f["e_load"], atol=1e-5)
        # 充放互斥生效(02 §4.4 BAT-MU):任一步不可同时充放
        assert np.all(f["u_ch"] + f["u_dis"] <= 1.0 + eps)
        assert not np.any((f["p_bat_ch"] > eps) & (f["p_bat_dis"] > eps))
        # 谷时充电、峰时放电(充放在各时段内的分配不唯一,只断言时段归属)
        assert np.all(f["p_bat_ch"][2:4] == 0)
        assert np.all(f["p_bat_dis"][0:2] == 0)
        assert np.sum(f["p_bat_ch"][0:2]) > 0
        assert np.sum(f["p_bat_dis"][2:4]) > 0
        # SOC 范围与终态约束(02 §4.4 BAT-BND/§5.4)
        assert np.all(f["soc"] >= 0.1 - eps) and np.all(f["soc"] <= 0.9 + eps)
        assert f["soc"][0] == pytest.approx(0.5)
        assert f["soc"][-1] >= 0.5 - eps
        # 手算(02 §4.4 BAT-SOC):谷时第 0 步充电 C = 0.8/η = 0.8421 kWh
        # (SOC 增加 η·C = 0.8 → 0.5→0.9 触顶),峰时放电 D = η²·C = 0.76 kWh,
        # 期末 SOC 回到 0.5(满足 E(n) ≥ soc0·E_cap 等号)。
        charge_kwh = np.sum(f["p_bat_ch"]) * 1.0 / 1000.0
        disch_kwh = np.sum(f["p_bat_dis"]) * 1.0 / 1000.0
        assert charge_kwh == pytest.approx(0.8 / 0.95, abs=0.01)
        assert disch_kwh == pytest.approx(0.76, abs=0.01)
        assert f["soc"][-1] == pytest.approx(0.5, abs=0.02)

    def test_cost_hand_calc(self):
        res = evaluate_plan(base_plan(), base_data(), make_axis(4))
        # 手算费用:谷时购电 (2 + 0.8421) kWh×0.3 = 0.8526;
        #   峰时购电 (2 − 0.76) kWh×1.1 = 1.364;合计 2.2166 元
        assert float(res.kpi["buy_cost"]) == pytest.approx(2.2166, abs=0.02)
        assert float(res.kpi["sell_revenue"]) == pytest.approx(0.0, abs=1e-6)
        assert float(res.kpi["total_op_cost"]) == pytest.approx(2.2166, abs=0.02)
        assert float(res.kpi["gas_cost"]) == pytest.approx(0.0, abs=1e-6)
        # 目标函数(元)与总费用一致
        assert res.objective == pytest.approx(2.2166, abs=0.02)
        # 电量:年购电 = 4 + 0.8421 − 0.76 = 4.0821 kWh
        assert res.kpi["annual_buy_kwh"] == pytest.approx(4.0821, abs=0.02)
        assert res.kpi["annual_sell_kwh"] == pytest.approx(0.0, abs=1e-9)
        # 碳排放 = 购电 × 0.581 kg/kWh
        assert res.kpi["co2_total_kg"] == pytest.approx(4.0821 * 0.581, abs=0.02)
        # 峰值购电:谷时步 ≤ 1 + 0.8421 = 1.8421 kW,峰时步 ≤ 1 kW
        assert 1.4 <= res.kpi["peak_grid_buy_kw"] <= 1.85

    def test_soc_validation_matches_simulation(self):
        from iesplan.engines.devices import simulate_battery

        res = evaluate_plan(base_plan(), base_data(), make_axis(4))
        f = res.flows
        soc_sim = simulate_battery(f["p_bat_ch"], f["p_bat_dis"], capacity_kwh=2.0,
                                   soc0=0.5, eta=0.95)
        assert np.allclose(f["soc"], soc_sim, atol=1e-6)

    def test_no_battery_plan_feasible(self):
        plan = {
            "devices": [
                {"type": "ies.device.grid_connection",
                 "params": {"max_import_power_kw": 5000, "max_export_power_kw": 0},
                 "is_new": False},
            ],
            "reverse_feed_allowed": False,
        }
        res = evaluate_plan(plan, base_data(), make_axis(4))
        assert res.status == "ok"
        assert np.allclose(res.flows["p_grid_buy"], res.flows["e_load"])
        assert res.kpi["annual_buy_kwh"] == pytest.approx(4.0, abs=1e-9)
        assert float(res.kpi["total_op_cost"]) == pytest.approx(2.8, abs=1e-6)


class TestPvAndReverseFeed:
    """光伏 + 允许反送电(02 §4.3/§3.6)。"""

    def _plan(self) -> dict:
        return {
            "devices": [
                {"type": "ies.device.grid_connection",
                 "params": {"max_import_power_kw": 5000, "max_export_power_kw": 10,
                            "export_tariff": 0.35},
                 "is_new": False},
                {"type": "ies.device.pv",
                 "params": {"rated_capacity_kwp": 2, "efficiency": 0.20,
                            "tilt_deg": 30, "azimuth_deg": 180},
                 "is_new": False},
            ],
            "reverse_feed_allowed": True,
        }

    def test_pv_output_and_sell_revenue(self):
        data = base_data(
            tariff_buy=np.array([0.4, 0.4, 1.1, 1.1]),  # 最低购电价 > 售电价,避免套利
            ghi=np.array([1000.0, 1000.0, 0.0, 0.0]),
            temperature=np.array([25.0, 25.0, 25.0, 25.0]),
        )
        res = evaluate_plan(self._plan(), data, make_axis(4))
        assert res.status == "ok"
        f = res.flows
        # 手算:PV 2 kWp,GHI=1000,Ta=25 → Tc = 25 + (45-20)/800*1000 = 56.25°C
        # 降额因子 1-0.004*(56.25-25) = 0.875 → 出力 = 2000×1.0×0.875 = 1750 W
        assert np.allclose(f["p_pv"][0:2], 1750.0, atol=1e-6)
        assert np.allclose(f["p_pv"][2:4], 0.0, atol=1e-6)
        # 电平衡:购电 + 光伏 = 负荷 + 售电
        assert np.allclose(f["p_grid_buy"] + f["p_pv"],
                           f["e_load"] + f["p_grid_sell"], atol=1e-5)
        # 光伏盈余 750 W × 2h = 1.5 kWh 反送电网
        assert f["p_grid_sell"][0] == pytest.approx(750.0, abs=1e-6)
        assert f["p_grid_buy"][2] == pytest.approx(1000.0, abs=1e-6)
        # 费用:购电 2 kWh(峰时 1.1 元)× = 2.2,售电收入 1.5×0.35 = 0.525
        assert res.kpi["annual_buy_kwh"] == pytest.approx(2.0, abs=1e-9)
        assert res.kpi["annual_sell_kwh"] == pytest.approx(1.5, abs=1e-9)
        assert float(res.kpi["buy_cost"]) == pytest.approx(2.2, abs=1e-6)
        assert float(res.kpi["sell_revenue"]) == pytest.approx(0.525, abs=0.01)  # Decimal 保留 2 位小数
        # 自用率 = (光伏发电 3.5 − 售电 1.5)/3.5
        assert res.kpi["pv_self_use_rate"] == pytest.approx((3.5 - 1.5) / 3.5, abs=1e-6)
        assert res.kpi["annual_pv_kwh"] == pytest.approx(3.5, abs=1e-9)

    def test_forbid_reverse_feed_by_default(self):
        # 未开启 reverse_feed_allowed → 售电恒为 0(02 §3.6 禁止反送电)
        data = base_data(
            ghi=np.array([1000.0, 1000.0, 0.0, 0.0]),
            temperature=np.array([25.0, 25.0, 25.0, 25.0]),
        )
        plan = self._plan()
        plan["reverse_feed_allowed"] = False
        res = evaluate_plan(plan, data, make_axis(4))
        assert res.status == "ok"
        assert np.all(res.flows["p_grid_sell"] == 0.0)
        assert res.kpi["annual_sell_kwh"] == 0.0
        # 存在禁止反送电提示诊断(显著展示,02 §3.6)
        codes = [d["code"] for d in res.diagnostics]
        assert "ENG-NOTE-001" in codes


class TestHeatSupply:
    """热泵 + 燃气锅炉供热(02 §4.5/§4.6),零损耗手算。"""

    def _plan(self) -> dict:
        return {
            "devices": [
                {"type": "ies.device.grid_connection",
                 "params": {"max_import_power_kw": 5000, "max_export_power_kw": 0},
                 "is_new": False},
                {"type": "ies.device.heat_pump",
                 "params": {"rated_heat_kw": 1.5, "cop": 3.0, "mode": "heating"},
                 "is_new": False},
                {"type": "ies.device.gas_boiler",
                 "params": {"rated_heat_kw": 10, "thermal_efficiency": 0.9,
                            "gas_price": 3.2, "lhv_kj_per_m3": 35900},
                 "is_new": False},
            ],
            "reverse_feed_allowed": False,
            "lambda_h": 0.0,   # 零损耗便于手算
            "c_ph": 0.0, "c_pc": 0.0,
        }

    def test_heat_balance_and_costs(self):
        data = base_data(
            e_load=np.array([0.0, 0.0, 0.0, 0.0]),
            h_load=np.array([2000.0, 2000.0, 0.0, 0.0]),
            tariff_buy=np.full(4, 0.5),
        )
        res = evaluate_plan(self._plan(), data, make_axis(4))
        assert res.status == "ok"
        f = res.flows
        # 热平衡:锅炉产热 + 热泵供热 = 热负荷(λ_h=0)
        assert np.allclose(f["p_boiler"] + f["p_hp_heat"], f["h_load"], atol=1e-5)
        # 手算:热泵满发 1.5 kW 热(耗电 1.5/3 = 0.5 kW),锅炉补 0.5 kW 热
        assert np.allclose(f["p_hp_heat"][0:2], 1500.0, atol=1e-5)
        assert np.allclose(f["p_boiler"][0:2], 500.0, atol=1e-5)
        assert np.allclose(f["p_hp_elec"][0:2], 500.0, atol=1e-5)
        # 燃气功率 = 500/0.9 ≈ 555.6 W;气量 = 555.6×3600/35.9e6 ≈ 0.0557 m³/步
        gas_w = 500.0 / 0.9
        assert np.allclose(f["p_boiler_gas"][0:2], gas_w, atol=1e-3)
        assert f["v_gas"][0] == pytest.approx(gas_w * 3600.0 / 35.9e6, rel=1e-9)
        # 费用:电费 1 kWh × 0.5 = 0.5;燃气 2×0.0557×3.2 ≈ 0.3566
        assert float(res.kpi["buy_cost"]) == pytest.approx(0.5, abs=1e-6)
        assert float(res.kpi["gas_cost"]) == pytest.approx(2 * 0.05571 * 3.2, abs=0.01)  # Decimal 保留 2 位
        # 碳排放 = 购电 1 kWh×0.581 + 燃气 0.1114 m³×2.0
        assert res.kpi["co2_total_kg"] == pytest.approx(0.581 + 0.1114 * 2.0, abs=1e-3)


class TestShedding:
    """负荷削减(02 §3.7):显著报告 + 惩罚计价。"""

    def test_shedding_when_capacity_insufficient(self):
        plan = {
            "devices": [
                {"type": "ies.device.grid_connection",
                 "params": {"max_import_power_kw": 5, "max_export_power_kw": 0},
                 "is_new": False},
            ],
            "reverse_feed_allowed": False,
        }
        data = base_data(e_load=np.full(4, 10000.0))  # 10 kW 负荷,购电上限 5 kW
        # 不允许削减 → 不可行
        res_infeasible = evaluate_plan(plan, data, make_axis(4))
        assert res_infeasible.status == "infeasible"
        # 允许削减 → 每步削减 5 kW,惩罚 5×1.1 = 5.5 元/kWh
        res = evaluate_plan(plan, data, make_axis(4), {"shedding": True})
        assert res.status == "ok"
        f = res.flows
        assert np.allclose(f["p_grid_buy"], 5000.0, atol=1e-5)
        assert np.allclose(f["p_shed_e"], 5000.0, atol=1e-5)
        # 削减能量 5 kW × 4h = 20 kWh,削减率 0.5
        assert res.kpi["shed_energy_kwh"] == pytest.approx(20.0, abs=1e-9)
        assert res.kpi["shed_ratio"] == pytest.approx(0.5, abs=1e-9)
        assert res.kpi["shed_events"] == [[0, 3]]
        # 费用:购电 20 kWh(谷 0.3×10 + 峰 1.1×10 = 14 元)+ 削减惩罚 20×5.5 = 110 元
        assert float(res.kpi["total_op_cost"]) == pytest.approx(14.0 + 110.0, abs=0.1)
        # 显著报告:ENG-SHED-001 警告诊断
        codes = [d["code"] for d in res.diagnostics]
        assert "ENG-SHED-001" in codes


class TestStepSizes:
    """15/30/60 分钟步长能量换算(02 §1.1/§7)。"""

    @pytest.mark.parametrize("step_minutes,expected_kwh", [
        (60, 4.0),
        (30, 2.0),
        (15, 1.0),
    ])
    def test_energy_scaling(self, step_minutes, expected_kwh):
        plan = {
            "devices": [
                {"type": "ies.device.grid_connection",
                 "params": {"max_import_power_kw": 5000, "max_export_power_kw": 0},
                 "is_new": False},
            ],
            "reverse_feed_allowed": False,
        }
        data = base_data(tariff_buy=np.full(4, 0.7))
        res = evaluate_plan(plan, data, make_axis(4, step_minutes=step_minutes))
        assert res.status == "ok"
        assert res.kpi["annual_buy_kwh"] == pytest.approx(expected_kwh, abs=1e-9)
        assert float(res.kpi["total_op_cost"]) == pytest.approx(expected_kwh * 0.7, abs=1e-6)


class TestInfeasibilityDiagnostics:
    """不可行时的诊断与空流输出(02 §7.4)。"""

    def test_insufficient_generation(self):
        # 只有 5 kW 购电、无其他设备,而负荷 10 kW → 不可行
        plan = {
            "devices": [
                {"type": "ies.device.grid_connection",
                 "params": {"max_import_power_kw": 5, "max_export_power_kw": 0},
                 "is_new": False},
            ],
            "reverse_feed_allowed": False,
        }
        res = evaluate_plan(plan, base_data(e_load=np.full(4, 10000.0)), make_axis(4))
        assert res.status == "infeasible"
        assert res.objective is None
        assert res.flows == {}
        codes = [d["code"] for d in res.diagnostics]
        assert "ENG-SOLVE-001" in codes
