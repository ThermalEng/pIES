"""设备出力参数化单元测试(02 §4):PV 量级、COP 单调性、线性锅炉/制冷机、电池 SOC 递推。

纯计算测试,不依赖 DB;全部用手算解析解断言。
"""

import numpy as np
import pytest

from iesplan.engines.devices import (
    boiler_output,
    chiller_output,
    gas_volume_m3,
    heat_pump_cop,
    pv_output,
    simulate_battery,
)


class TestPvOutput:
    """光伏出力(02 §4.3):P = C·(G_eff/G_STC)·(1−β_T·(Tc−25)),Tc 用 NOCT 近似。"""

    def test_output_magnitude_hand_calc(self):
        # GHI=800 W/m²,Ta=25°C,C=100 kWp=1e5 W,η=0.2,β_T=0.004,NOCT=45
        # Tc = 25 + (45-20)/800*800 = 50°C → 降额因子 1-0.004*25 = 0.9
        # P = 1e5 * (800/1000) * 0.9 = 72000 W
        ghi = np.full(4, 800.0)
        temp = np.full(4, 25.0)
        p = pv_output(ghi, 100_000.0, temp, eff=0.20, temp_coeff=0.004)
        assert p.shape == (4,)
        assert np.allclose(p, 72000.0)

    def test_output_scales_with_ghi(self):
        # GHI 加倍 → 出力加倍(温度系数取 0,温度项不干扰比例)
        ghi = np.array([500.0, 1000.0])
        temp = np.full(2, 15.0)
        p = pv_output(ghi, 50_000.0, temp, eff=0.20, temp_coeff=0.0)
        assert p[1] == pytest.approx(2.0 * p[0], rel=1e-9)

    def test_output_decreases_with_temperature(self):
        ghi = np.full(4, 1000.0)
        p_cool = pv_output(ghi, 100_000.0, np.full(4, 10.0))
        p_hot = pv_output(ghi, 100_000.0, np.full(4, 40.0))
        assert np.all(p_cool > p_hot)

    def test_output_nonnegative_and_clamped(self):
        # 极高温 → 出力接近 0 且不为负;夜间 GHI=0 → 出力 0
        p = pv_output(np.full(4, 1000.0), 100_000.0, np.full(4, 80.0))
        assert np.all(p >= 0.0)
        p0 = pv_output(np.zeros(4), 100_000.0, np.full(4, 25.0))
        assert np.allclose(p0, 0.0)

    def test_ghi_length_mismatch(self):
        with pytest.raises(ValueError):
            pv_output(np.array([1.0, 2.0]), 1000.0, np.array([1.0]))


class TestHeatPumpCop:
    """热泵 COP(02 §4.5 卡诺近似 + 截断):供热随环境温度升高而增大,供冷相反。"""

    def test_heating_cop_monotonic_increasing(self):
        temps = np.array([-5.0, 5.0, 15.0, 25.0])  # 温和区间,不触发截断
        cop = heat_pump_cop(temps, "heating")
        assert np.all(np.diff(cop) > 0)

    def test_cooling_cop_monotonic_decreasing(self):
        temps = np.array([15.0, 25.0, 35.0, 40.0])
        cop = heat_pump_cop(temps, "cooling")
        assert np.all(np.diff(cop) < 0)

    def test_heating_cop_hand_value(self):
        # 0°C:COP_h = 0.45*318/(318-273.15+5) = 143.1/49.85 ≈ 2.8706
        cop = heat_pump_cop(np.array([0.0]), "heating")
        assert cop[0] == pytest.approx(0.45 * 318.0 / (318.0 - 273.15 + 5.0), rel=1e-6)

    def test_cop_clipped_to_range(self):
        # 极高温供热 → 卡诺值超高 → 截断到上限 5.5;极低温供热 → 截断到下限 2.0
        hot = heat_pump_cop(np.array([45.0]), "heating")
        assert hot[0] == pytest.approx(5.5)
        # 极低温供热 → 卡诺值 1.79 → 截断到下限 2.0
        cold = heat_pump_cop(np.array([-30.0]), "heating")
        assert cold[0] == pytest.approx(2.0)
        # 供冷:低温环境卡诺值超高 → 截断到上限 6.5;高温环境 → 截断到下限 2.5
        cool_lo = heat_pump_cop(np.array([5.0]), "cooling")
        assert cool_lo[0] == pytest.approx(6.5)
        cool_hi = heat_pump_cop(np.array([50.0]), "cooling")
        assert cool_hi[0] == pytest.approx(2.5)

    def test_custom_clip_range(self):
        cop = heat_pump_cop(np.array([0.0]), "heating", cop_min=3.0, cop_max=4.0)
        assert cop[0] == pytest.approx(3.0)  # 2.87 被抬到下限 3.0

    def test_invalid_mode(self):
        with pytest.raises(ValueError):
            heat_pump_cop(np.array([10.0]), "ventilation")


class TestLinearDevices:
    """锅炉/制冷机/气量换算(02 §4.6/§4.7 线性)。"""

    def test_boiler_output(self):
        gas = np.array([1000.0, 2000.0])
        heat = boiler_output(gas, efficiency=0.90)
        assert np.allclose(heat, gas * 0.90)

    def test_chiller_output(self):
        elec = np.array([500.0, 1000.0])
        cool = chiller_output(elec, cop=4.0)
        assert np.allclose(cool, elec * 4.0)

    def test_gas_volume(self):
        # 1000 W × 3600 s = 3.6e6 J ÷ 35.9e6 J/m³ ≈ 0.10028 m³
        v = gas_volume_m3(np.array([1000.0]), 3600.0, lhv_j_per_m3=35.9e6)
        assert v[0] == pytest.approx(3.6e6 / 35.9e6, rel=1e-9)


class TestSimulateBattery:
    """电池 SOC 确定性递推(02 §4.4 BAT-SOC),用于优化结果验证。"""

    def test_soc_recursion_hand_calc(self):
        # 容量 10 kWh,SOC0=0.5 → E0=5 kWh;1h 步长
        # ch=[1,1,0],dis=[0,0,0.5] kW → 递推:
        # E1 = 5 + 0.95*1 = 5.95;E2 = 5.95+0.95 = 6.90;E3 = 6.90 - 0.5/0.95 ≈ 6.3737
        ch = np.array([1000.0, 1000.0, 0.0])
        dis = np.array([0.0, 0.0, 500.0])
        soc = simulate_battery(ch, dis, capacity_kwh=10.0, soc0=0.5, eta=0.95)
        assert soc.shape == (4,)
        assert soc[0] == pytest.approx(0.5)
        assert soc[1] == pytest.approx(5.95 / 10.0)
        assert soc[2] == pytest.approx(6.90 / 10.0)
        assert soc[3] == pytest.approx((6.90 - 0.5 / 0.95) / 10.0, rel=1e-9)

    def test_discharge_losses_symmetry(self):
        # 充 1 kWh(η=0.95)后放电至 SOC 初值需要放 η² = 0.9025 kWh(BAT-SOC 非对称)
        ch = np.array([1000.0, 0.0])
        dis = np.array([0.0, 902.5])
        soc = simulate_battery(ch, dis, capacity_kwh=10.0, soc0=0.5, eta=0.95)
        assert soc[2] == pytest.approx(0.5, abs=1e-9)

    def test_step_size_scaling(self):
        # 30 分钟步长:充电 1000 W × 0.5 h = 0.5 kWh,SOC 增量 0.475/10
        soc = simulate_battery(np.array([1000.0]), np.array([0.0]),
                               capacity_kwh=10.0, soc0=0.5, eta=0.95, dt_h=0.5)
        assert soc[1] == pytest.approx(0.5 + 0.475 / 10.0, abs=1e-9)

    def test_invalid_inputs(self):
        with pytest.raises(ValueError):
            simulate_battery(np.array([1.0]), np.array([1.0, 2.0]), 10.0)
        with pytest.raises(ValueError):
            simulate_battery(np.array([1.0]), np.array([1.0]), 0.0)
