"""设备出力参数化(02 §4 设备数学模型,P1 简化线性)。

- pv_output:    光伏逐时可用出力(02 §4.3):P = C·(G_eff/G_STC)·(1−β_T·(Tc−25)),
  Tc 用 NOCT 近似:Tc = Ta + (NOCT−20)/800·G_eff;P1 简化下 G_eff = GHI
  (倾斜/朝向月度修正系数 F 由调用方预计算传入 g_eff,02 §4.3 P1)。
- heat_pump_cop:热泵 COP 随环境温度(02 §4.5 卡诺近似 + 截断):
  COP_h = clip(η·T_cnd/(T_cnd−Ta+ΔT_ev)),COP_c = clip(η·T_ev/(Ta+ΔT_cd−T_ev))。
- boiler_output / chiller_output:锅炉/制冷机线性出力(02 §4.6/§4.7)。
- gas_volume_m3:燃气输入功率 → 体积(LHV 基准,02 §4.6)。
- simulate_battery:电池 SOC 确定性递推(02 §4.4 BAT-SOC,供验证用;优化器内用线性约束)。

单位约定:功率 W、辐照 W/m²、温度 °C(接口)/K(内部)、能量 kWh(电池)。
"""

from __future__ import annotations

import numpy as np

# ---------------------------------------------------------------------------
# 光伏(02 §4.3)
# ---------------------------------------------------------------------------

#: 标准测试条件辐照(W/m²,02 §4.3 G_STC)
G_STC = 1000.0
#: 标准测试条件组件温度(°C)
T_STC = 25.0
#: 默认组件温度系数 β_T(1/K,02 §4.3)
DEFAULT_TEMP_COEFF = 0.004
#: 默认标称工作温度 NOCT(°C,02 §4.3)
DEFAULT_NOCT = 45.0
#: 默认组件标称效率 η(04 §3.3 注册表默认)
DEFAULT_PV_EFFICIENCY = 0.20


def pv_output(
    ghi_series: np.ndarray,
    capacity: float,
    temperature: np.ndarray,
    *,
    eff: float = DEFAULT_PV_EFFICIENCY,
    temp_coeff: float = DEFAULT_TEMP_COEFF,
    tilt: float = 30.0,
    azimuth: float = 180.0,
    noct: float = DEFAULT_NOCT,
    g_eff: np.ndarray | None = None,
    g_stc: float = G_STC,
    t_stc: float = T_STC,
) -> np.ndarray:
    """光伏逐时可用出力(W)(02 §4.3 PV-P/PV-T)。

    公式(P1 简化,G_eff 为有效辐照):
        P_avail(τ) = C · G_eff(τ)/G_STC · [1 − β_T·(Tc(τ) − T_STC)]
        Tc(τ) = Ta(τ) + (NOCT − 20)/800 · G_eff(τ)

    参数:
        ghi_series: 水平总辐照 (n,) W/m²。
        capacity: 装机容量 W(kWp × 1000)。
        temperature: 环境温度 (n,) °C。
        eff: 组件标称效率 η(默认 0.20)。
        temp_coeff: 温度系数 β_T /K(默认 0.004)。
        tilt / azimuth: 倾角/方位角(°);P1 简化下不参与逐时天文计算,
            仅当调用方传入 g_eff 时才体现(02 §4.3 P1:月度系数表离线标定)。
        noct: 标称工作温度 °C(默认 45)。
        g_eff: 有效辐照 (n,) W/m²;None 时取 GHI(P1 简化,倾斜修正系数默认 1)。
        g_stc / t_stc: 标准测试条件(默认 1000 W/m² / 25 °C)。
    返回:
        (n,) 可用出力 W,≥ 0;数值上 ≤ eff·capacity·G_eff/G_STC·(1+β_T·25) 等上界。
    """
    g = np.asarray(ghi_series, dtype=np.float64)
    ta = np.asarray(temperature, dtype=np.float64)
    if g.ndim != 1 or ta.ndim != 1 or g.size != ta.size:
        raise ValueError(f"ghi_series/temperature 长度不一致: {g.size} vs {ta.size}")
    if not np.all(np.isfinite(g)) or not np.all(np.isfinite(ta)):
        raise ValueError("ghi_series/temperature 包含 NaN/Inf")
    if capacity < 0:
        raise ValueError(f"capacity 必须 >= 0,实际 {capacity}")
    if eff <= 0 or g_stc <= 0:
        raise ValueError("eff/g_stc 必须为正")

    geff = np.asarray(g_eff, dtype=np.float64) if g_eff is not None else g
    if geff.ndim != 1 or geff.size != g.size:
        raise ValueError(f"g_eff 长度应为 {g.size},实际 {geff.size}")
    geff = np.maximum(geff, 0.0)  # 辐照禁止负值(02 §1.4 插值后 clamp ≥ 0)

    # 组件温度 NOCT 近似(02 §4.3 PV-T)
    tc = ta + (noct - 20.0) / 800.0 * geff
    # 可用出力(PV-P);温度项可能为负(高温降额),下限 0
    p = capacity * (geff / g_stc) * (1.0 - temp_coeff * (tc - t_stc))
    return np.maximum(p, 0.0)


# ---------------------------------------------------------------------------
# 热泵(02 §4.5)
# ---------------------------------------------------------------------------

#: 默认冷凝温度(K,02 §4.5)
DEFAULT_T_CND = 318.0
#: 默认蒸发温度(K,02 §4.5)
DEFAULT_T_EV = 280.0
#: 默认 ΔT_ev(K,02 §4.5)
DEFAULT_DELTA_T_EV = 5.0
#: 默认 ΔT_cd(K,02 §4.5)
DEFAULT_DELTA_T_CD = 10.0
#: 默认卡诺效率 η_hp(02 §4.5)
DEFAULT_HP_ETA = 0.45
#: COP 截断下限/上限默认(供热 [2.0, 5.5]、供冷 [2.5, 6.5],02 附录 B)
DEFAULT_COP_HEAT_MIN = 2.0
DEFAULT_COP_HEAT_MAX = 5.5
DEFAULT_COP_COOL_MIN = 2.5
DEFAULT_COP_COOL_MAX = 6.5


def heat_pump_cop(
    temperature_series: np.ndarray,
    mode: str,
    *,
    cop_min: float | None = None,
    cop_max: float | None = None,
    eta: float = DEFAULT_HP_ETA,
    t_cnd: float = DEFAULT_T_CND,
    t_ev: float = DEFAULT_T_EV,
    delta_t_ev: float = DEFAULT_DELTA_T_EV,
    delta_t_cd: float = DEFAULT_DELTA_T_CD,
) -> np.ndarray:
    """热泵 COP 随环境温度逐时值(02 §4.5 HP-COPh/HP-COPc,卡诺近似 + 截断)。

    供热:COP_h = clip(η·T_cnd/(T_cnd − Ta − 273.15 + ΔT_ev), [cop_min, cop_max]);
    供冷:COP_c = clip(η·T_ev/(Ta + 273.15 + ΔT_cd − T_ev), [cop_min, cop_max])。
    温度单位:输入 °C,内部 K(02 §2.2)。分母保护避免除零;截断保证 COP 位于
    注册表范围(02 §4.5)。供热 COP 随环境温度升高而增大,供冷 COP 随环境温度
    升高而减小(卡诺关系),故结果单调(截断区间内)。

    参数:
        temperature_series: 环境温度 (n,) °C。
        mode: 'heating' | 'cooling'(供热/供冷)。
        cop_min / cop_max: COP 截断区间;None 时按模式取默认
            (供热 [2.0, 5.5]、供冷 [2.5, 6.5],02 附录 B)。
        eta: 卡诺效率 η(默认 0.45)。
        t_cnd / t_ev: 冷凝/蒸发温度 K(默认 318/280)。
        delta_t_ev / delta_t_cd: 温差 K(默认 5/10)。
    返回:
        (n,) COP 逐时值。
    """
    ta_c = np.asarray(temperature_series, dtype=np.float64)
    if ta_c.ndim != 1:
        raise ValueError(f"temperature_series 应为一维,实际形状 {ta_c.shape}")
    if not np.all(np.isfinite(ta_c)):
        raise ValueError("temperature_series 包含 NaN/Inf")
    if mode not in ("heating", "cooling"):
        raise ValueError(f"mode 只能为 'heating'/'cooling',实际 {mode!r}")

    # 截断区间默认按模式取(供热 [2.0,5.5],供冷 [2.5,6.5],02 附录 B)
    if mode == "heating":
        lo = DEFAULT_COP_HEAT_MIN if cop_min is None else float(cop_min)
        hi = DEFAULT_COP_HEAT_MAX if cop_max is None else float(cop_max)
    else:
        lo = DEFAULT_COP_COOL_MIN if cop_min is None else float(cop_min)
        hi = DEFAULT_COP_COOL_MAX if cop_max is None else float(cop_max)

    ta_k = ta_c + 273.15
    if mode == "heating":
        den = t_cnd - ta_k + delta_t_ev
        cop = eta * t_cnd / np.maximum(den, 1e-6)
    else:
        den = ta_k + delta_t_cd - t_ev
        cop = eta * t_ev / np.maximum(den, 1e-6)
    return np.clip(cop, lo, hi)


# ---------------------------------------------------------------------------
# 锅炉 / 电制冷机(02 §4.6 / §4.7,P1 线性)
# ---------------------------------------------------------------------------

#: 天然气体积低位热值默认(J/m³,02 §4.6 LHV_V = 35.9 MJ/m³)
DEFAULT_LHV_J_PER_M3 = 35.9e6
#: 默认锅炉效率 η_b(02 附录 B)
DEFAULT_BOILER_EFFICIENCY = 0.90
#: 默认制冷机 COP(02 附录 B)
DEFAULT_CHILLER_COP = 4.0


def boiler_output(
    gas_power_w: np.ndarray | float,
    efficiency: float = DEFAULT_BOILER_EFFICIENCY,
) -> np.ndarray:
    """锅炉产热(W)= 燃气输入功率(W) × 效率(LHV 基准,02 §4.6 B-P)。"""
    if efficiency <= 0 or efficiency > 1:
        raise ValueError(f"efficiency 应位于 (0,1],实际 {efficiency}")
    return float(efficiency) * np.asarray(gas_power_w, dtype=np.float64)


def chiller_output(elec_power_w: np.ndarray | float, cop: float = DEFAULT_CHILLER_COP) -> np.ndarray:
    """电制冷机产冷(W)= 耗电(W) × COP(P1 常数能效,02 §4.7 C-P)。"""
    if cop <= 0:
        raise ValueError(f"cop 必须为正,实际 {cop}")
    return float(cop) * np.asarray(elec_power_w, dtype=np.float64)


def gas_volume_m3(
    gas_power_w: np.ndarray | float,
    dt_s: float,
    lhv_j_per_m3: float = DEFAULT_LHV_J_PER_M3,
) -> np.ndarray:
    """燃气输入功率 → 体积(m³):V = P_gas·Δt / LHV_V(02 §4.6 B-P)。

    参数:
        gas_power_w: 燃气输入功率 W(逐时数组或标量)。
        dt_s: 时间步长秒。
        lhv_j_per_m3: 体积低位热值 J/m³(默认 35.9e6,可配置)。
    """
    if dt_s <= 0:
        raise ValueError(f"dt_s 必须为正,实际 {dt_s}")
    if lhv_j_per_m3 <= 0:
        raise ValueError(f"lhv_j_per_m3 必须为正,实际 {lhv_j_per_m3}")
    return np.asarray(gas_power_w, dtype=np.float64) * dt_s / lhv_j_per_m3


# ---------------------------------------------------------------------------
# 电池(02 §4.4,确定性模拟供验证;优化器内用线性约束)
# ---------------------------------------------------------------------------

#: 默认充/放电效率 η(02 附录 B)
DEFAULT_BATTERY_ETA = 0.95
#: 默认 SOC 范围(02 附录 B)
DEFAULT_SOC_MIN = 0.10
DEFAULT_SOC_MAX = 0.90


def simulate_battery(
    p_bat_ch: np.ndarray,
    p_bat_dis: np.ndarray,
    capacity_kwh: float,
    soc0: float = 0.5,
    *,
    eta: float = DEFAULT_BATTERY_ETA,
    soc_min: float = DEFAULT_SOC_MIN,
    soc_max: float = DEFAULT_SOC_MAX,
    dt_h: float = 1.0,
) -> np.ndarray:
    """电池 SOC 确定性递推(02 §4.4 BAT-SOC),返回 (n+1,) SOC 数组。

    递推(能量单位 kWh,功率 W,Δt 小时):
        E(τ+1) = E(τ) + η·P_ch(τ)·Δt_h − P_dis(τ)/η·Δt_h,
        SOC(τ) = E(τ)/E_cap;SOC(0) = soc0。
    用于验证优化结果(与优化器内线性约束 BAT-SOC 比对);本函数不截断 SOC
    (越界由调用方判定为违反约束,这正是验证目的)。η_ch = η_dis = eta(默认 0.95)。

    参数:
        p_bat_ch / p_bat_dis: 逐时充/放电功率 W(长度 n 相等)。
        capacity_kwh: 额定容量 kWh(> 0)。
        soc0: 初始 SOC(0..1)。
        eta: 充放效率(默认 0.95,充放对称)。
        soc_min / soc_max: SOC 上下限(仅返回注释,不参与递推)。
        dt_h: 时间步长小时(默认 1.0;15/30/60 分钟分别 0.25/0.5/1.0)。
    返回:
        (n+1,) SOC 数组,soc[0] = soc0。
    """
    ch = np.asarray(p_bat_ch, dtype=np.float64)
    dis = np.asarray(p_bat_dis, dtype=np.float64)
    if ch.ndim != 1 or dis.ndim != 1 or ch.size != dis.size:
        raise ValueError(f"p_bat_ch/p_bat_dis 长度不一致: {ch.size} vs {dis.size}")
    if not np.all(np.isfinite(ch)) or not np.all(np.isfinite(dis)):
        raise ValueError("p_bat_ch/p_bat_dis 包含 NaN/Inf")
    if capacity_kwh <= 0:
        raise ValueError(f"capacity_kwh 必须为正,实际 {capacity_kwh}")
    if not 0 <= soc0 <= 1:
        raise ValueError(f"soc0 应位于 [0,1],实际 {soc0}")
    if not 0 < eta <= 1:
        raise ValueError(f"eta 应位于 (0,1],实际 {eta}")
    if dt_h <= 0:
        raise ValueError(f"dt_h 必须为正,实际 {dt_h}")

    e = float(soc0) * float(capacity_kwh)  # kWh
    soc = np.empty(ch.size + 1, dtype=np.float64)
    soc[0] = e / capacity_kwh
    for tau in range(ch.size):
        e += eta * ch[tau] * dt_h / 1000.0 - dis[tau] * dt_h / eta / 1000.0  # W·h → kWh
        soc[tau + 1] = e / capacity_kwh
    return soc
