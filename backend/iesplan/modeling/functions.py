"""机理模型函数库(自 engines/devices.py 迁入,全部 SI 单位:功率 W/能量 J/温度 K)。

实现内容:
- 基础物理函数:简单传热(``heat_transfer_q``)、简单功率平衡(``power_balance``);
- 设备机理函数:pv_output / heat_pump_cop / boiler_output / chiller_output /
  gas_volume_m3 / simulate_battery(有状态,02 §4.4 BAT-SOC 的 SI 版);
- 机理映射表 ``MECHANISM_FUNCTIONS``:yaml ``function.entry`` 的函数名 → 基础函数
  + 参数键/单位绑定 + 系列输入键 + 输出端口名(02 §6.5"机理方法 mapping 到基础函数");
- ``as_device_entry``:把基础函数包装为统一调用契约 device_entry(params, series,
  state, dt_s, prices) → DeviceRunResult(参数在包装层完成业务单位 → SI 换算,
  换算唯一入口为 0 层 core/units.py,禁止硬编码系数)。

单位约定(02 §6.5):params 为注册表业务单位(kW/kWh/kWp 等),series 为内部单位
序列(W/J/K,由装配层换算),函数本体只消费 SI。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from iesplan.core.units import UnitError, to_si

from iesplan.modeling.command import DeviceRunResult
from iesplan.modeling.errors import ModelingConfigError

# ---------------------------------------------------------------------------
# 基础物理函数(简单传热 / 简单功率平衡)
# ---------------------------------------------------------------------------


def heat_transfer_q(
    ua_w_per_k: np.ndarray | float,
    t_hot_k: np.ndarray | float,
    t_cold_k: np.ndarray | float,
) -> np.ndarray:
    """简单传热:Q = UA·ΔT(02 审查意见第 3 条"简单传热"基础函数)。

    参数:
        ua_w_per_k: 传热系数与面积之积 UA(W/K,标量或逐时数组)。
        t_hot_k / t_cold_k: 热/冷侧温度(K)。
    返回:
        (n,) 传热功率 W;ΔT ≤ 0 时传热功率 ≤ 0(反向传热,不作钳制)。
    """
    ua = np.asarray(ua_w_per_k, dtype=np.float64)
    th = np.asarray(t_hot_k, dtype=np.float64)
    tc = np.asarray(t_cold_k, dtype=np.float64)
    ua = np.broadcast_to(ua, np.broadcast_shapes(ua.shape, th.shape, tc.shape))
    return ua * (th - tc)


def power_balance(
    power_in_w: np.ndarray | float,
    power_out_w: np.ndarray | float,
) -> np.ndarray:
    """简单功率平衡:盈余 = 入 − 出(节点净功率,>0 供外部消费,<0 需外部补足)。

    参数:
        power_in_w: 流入功率(W)。
        power_out_w: 流出功率(W)。
    返回:
        (n,) 净功率 W。
    """
    pin = np.asarray(power_in_w, dtype=np.float64)
    pout = np.asarray(power_out_w, dtype=np.float64)
    return np.broadcast_to(pin, np.broadcast_shapes(pin.shape, pout.shape)) - pout


# ---------------------------------------------------------------------------
# 设备机理函数(SI:W/J/K)
# ---------------------------------------------------------------------------

#: 标准测试条件辐照(W/m²,02 §4.3 G_STC)
G_STC = 1000.0
#: 标准测试条件组件温度(K)
T_STC_K = 298.15
#: 默认组件温度系数 β_T(1/K,02 §4.3)
DEFAULT_TEMP_COEFF = 0.004
#: 默认组件标称效率 η
DEFAULT_PV_EFFICIENCY = 0.20


def pv_output(
    ghi_w_m2: np.ndarray,
    temperature_k: np.ndarray,
    rated_capacity_w: float,
    efficiency: float = DEFAULT_PV_EFFICIENCY,
    temp_coeff_per_k: float = DEFAULT_TEMP_COEFF,
    noct_k: float = 318.15,  # 45 °C
) -> np.ndarray:
    """光伏逐时可用出力(W)(02 §4.3 PV-P/PV-T,SI 版)。

    P = C·(G/G_STC)·[1 − β_T·(Tc − T_STC)],Tc = Ta + (NOCT−293.15)/800·G。

    参数:
        ghi_w_m2: 水平总辐照 (n,) W/m²。
        temperature_k: 环境温度 (n,) K。
        rated_capacity_w: 装机容量 W。
        efficiency: 组件标称效率(默认 0.20)。
        temp_coeff_per_k: 温度系数 β_T(1/K,默认 0.004)。
        noct_k: 标称工作温度 K(默认 318.15 = 45 °C)。
    返回:
        (n,) 可用出力 W,≥ 0。
    """
    g = np.asarray(ghi_w_m2, dtype=np.float64)
    ta = np.asarray(temperature_k, dtype=np.float64)
    if g.ndim != 1 or ta.ndim != 1 or g.size != ta.size:
        raise ValueError(f"ghi/temperature 长度不一致: {g.size} vs {ta.size}")
    if not np.all(np.isfinite(g)) or not np.all(np.isfinite(ta)):
        raise ValueError("ghi/temperature 包含 NaN/Inf")
    if rated_capacity_w < 0:
        raise ValueError(f"rated_capacity_w 必须 >= 0,实际 {rated_capacity_w}")
    if efficiency <= 0:
        raise ValueError(f"efficiency 必须为正,实际 {efficiency}")
    geff = np.maximum(g, 0.0)
    tc = ta + (noct_k - 293.15) / 800.0 * geff
    p = rated_capacity_w * (geff / G_STC) * (1.0 - temp_coeff_per_k * (tc - T_STC_K))
    return np.maximum(p, 0.0)


#: 默认冷凝温度(K,02 §4.5)
DEFAULT_T_CND = 318.0
#: 默认蒸发温度(K,02 §4.5)
DEFAULT_T_EV = 280.0
#: 默认 ΔT_ev / ΔT_cd(K,02 §4.5)
DEFAULT_DELTA_T_EV = 5.0
DEFAULT_DELTA_T_CD = 10.0
#: 默认卡诺效率 η_hp(02 §4.5)
DEFAULT_HP_ETA = 0.45
#: COP 截断默认(供热 [2.0, 5.5]、供冷 [2.5, 6.5],02 附录 B)
DEFAULT_COP_HEAT_MIN, DEFAULT_COP_HEAT_MAX = 2.0, 5.5
DEFAULT_COP_COOL_MIN, DEFAULT_COP_COOL_MAX = 2.5, 6.5


def heat_pump_cop(
    temperature_k: np.ndarray,
    mode: str = "heating",
    *,
    eta: float = DEFAULT_HP_ETA,
    t_cnd: float = DEFAULT_T_CND,
    t_ev: float = DEFAULT_T_EV,
    delta_t_ev: float = DEFAULT_DELTA_T_EV,
    delta_t_cd: float = DEFAULT_DELTA_T_CD,
    cop_min: float | None = None,
    cop_max: float | None = None,
) -> np.ndarray:
    """热泵 COP 随环境温度(02 §4.5 卡诺近似 + 截断,SI 版)。

    COP_h = clip(η·T_cnd/(T_cnd − Ta + ΔT_ev));COP_c = clip(η·T_ev/(Ta + ΔT_cd − T_ev))。

    参数:
        temperature_k: 环境温度 (n,) K。
        mode: 'heating' | 'cooling'。
        eta: 卡诺效率(默认 0.45)。
        t_cnd / t_ev: 冷凝/蒸发温度 K。
        delta_t_ev / delta_t_cd: 蒸发/冷凝端温差 K。
        cop_min / cop_max: 截断区间;None 取 02 附录 B 默认(供热 [2.0,5.5]、供冷 [2.5,6.5])。
    返回:
        (n,) COP(无量纲)。
    """
    if mode not in ("heating", "cooling"):
        raise ValueError(f"mode 应为 'heating'|'cooling',实际 {mode!r}")
    if cop_min is None or cop_max is None:
        if mode == "heating":
            cop_min = cop_min if cop_min is not None else DEFAULT_COP_HEAT_MIN
            cop_max = cop_max if cop_max is not None else DEFAULT_COP_HEAT_MAX
        else:
            cop_min = cop_min if cop_min is not None else DEFAULT_COP_COOL_MIN
            cop_max = cop_max if cop_max is not None else DEFAULT_COP_COOL_MAX
    if cop_min > cop_max:
        raise ValueError(f"cop_min({cop_min}) 不能大于 cop_max({cop_max})")
    ta = np.asarray(temperature_k, dtype=np.float64)
    if ta.ndim != 1 or not np.all(np.isfinite(ta)):
        raise ValueError("temperature 必须为有限一维数组")
    if mode == "heating":
        cop = eta * t_cnd / (t_cnd - ta + delta_t_ev)
    else:
        cop = eta * t_ev / (ta + delta_t_cd - t_ev)
    return np.clip(cop, cop_min, cop_max)


def boiler_output(heat_demand_w: np.ndarray, efficiency: float = 0.9) -> np.ndarray:
    """锅炉输出:燃料输入功率 = 热需求/效率(02 §4.6 线性)。

    参数:
        heat_demand_w: 逐时热需求 (n,) W。
        efficiency: 热效率(默认 0.9)。
    返回:
        (n,) 燃料输入功率 W。
    """
    if not 0 < efficiency <= 1:
        raise ValueError(f"efficiency 应位于 (0,1],实际 {efficiency}")
    return np.asarray(heat_demand_w, dtype=np.float64) / efficiency


def chiller_output(elec_power_w: np.ndarray, cop: float = 4.0) -> np.ndarray:
    """电制冷机输出:制冷功率 = 电功率·COP(02 §4.7 线性)。

    参数:
        elec_power_w: 逐时电输入 (n,) W。
        cop: 性能系数(默认 4.0)。
    返回:
        (n,) 制冷功率 W。
    """
    if cop <= 0:
        raise ValueError(f"cop 必须为正,实际 {cop}")
    return np.asarray(elec_power_w, dtype=np.float64) * cop


#: 天然气低位热值默认(kJ/m³,02 §4.6 → J/m³)
DEFAULT_LHV_J_PER_M3 = 35.9e6


def gas_volume_m3(
    energy_j: np.ndarray,
    efficiency: float = 0.9,
    lhv_j_per_m3: float = DEFAULT_LHV_J_PER_M3,
) -> np.ndarray:
    """燃气输入功率/能量 → 体积(02 §4.6 LHV 基准)。

    参数:
        energy_j: 逐时需由燃气供应的能量 (n,) J。
        efficiency: 燃烧效率(默认 0.9)。
        lhv_j_per_m3: 低位热值 J/m³(默认 35.9e6)。
    返回:
        (n,) 燃气体积 m³。
    """
    if not 0 < efficiency <= 1:
        raise ValueError(f"efficiency 应位于 (0,1],实际 {efficiency}")
    if lhv_j_per_m3 <= 0:
        raise ValueError(f"lhv_j_per_m3 必须为正,实际 {lhv_j_per_m3}")
    return np.asarray(energy_j, dtype=np.float64) / (efficiency * lhv_j_per_m3)


def simulate_battery(
    charge_w: np.ndarray,
    discharge_w: np.ndarray,
    capacity_j: float,
    soc_initial: float = 0.5,
    *,
    charge_efficiency: float = 0.95,
    discharge_efficiency: float = 0.95,
    soc_min: float = 0.0,
    soc_max: float = 1.0,
    state: dict | np.ndarray | None = None,
    dt_s: float = 3600.0,
) -> tuple[np.ndarray, dict]:
    """电池 SOC 确定性递推(02 §4.4 BAT-SOC 的 SI 版,03 §5.2 有状态模型示例)。

    递推(能量 J,功率 W,Δt 秒):
        E(τ+1) = E(τ) + η_ch·P_ch(τ)·Δt − P_dis(τ)/η_dis·Δt,SOC = E/E_cap。
    SOC 按 [soc_min, soc_max] 钳制(运行期物理约束;优化器内用线性约束不截断)。

    状态约定(有状态模型统一契约,核验定案):
        - 输出序列首位为"传入状态快照"(t-1 末态或 initial 值),后续为步末 SOC
          —— 调用方按索引对齐时间轴时,首位即当前状态、第 τ+1 位为第 τ 步末态;
        - state 为 dict 时取 state['soc'] 作为初始 SOC(统一契约的当前状态快照);
        - state 为 ndarray 时取末元素(t-1 状态序列,03 §5.2 兼容形态);
        - state 为 None 时取 soc_initial(参数 initial_ref 语义)。
    返回:
        (soc: (n+1,) 首位=状态快照 + n 步末态, state_new: {'soc': 末态 SOC})。
    """
    ch = np.asarray(charge_w, dtype=np.float64)
    dis = np.asarray(discharge_w, dtype=np.float64)
    if ch.ndim != 1 or dis.ndim != 1 or ch.size != dis.size:
        raise ValueError(f"charge_w/discharge_w 长度不一致: {ch.size} vs {dis.size}")
    if not np.all(np.isfinite(ch)) or not np.all(np.isfinite(dis)):
        raise ValueError("charge_w/discharge_w 包含 NaN/Inf")
    if capacity_j <= 0:
        raise ValueError(f"capacity_j 必须为正,实际 {capacity_j}")
    if not 0 <= soc_initial <= 1:
        raise ValueError(f"soc_initial 应位于 [0,1],实际 {soc_initial}")
    if not 0 < charge_efficiency <= 1 or not 0 < discharge_efficiency <= 1:
        raise ValueError("charge/discharge_efficiency 应位于 (0,1]")
    if not 0 <= soc_min <= soc_max <= 1:
        raise ValueError(f"soc 界应满足 0 <= soc_min <= soc_max <= 1,实际 [{soc_min}, {soc_max}]")
    if dt_s <= 0:
        raise ValueError(f"dt_s 必须为正,实际 {dt_s}")

    if isinstance(state, dict):
        soc0 = float(state.get("soc", soc_initial))
    elif isinstance(state, np.ndarray):
        soc0 = float(state[-1])
    else:
        soc0 = soc_initial
    if not 0 <= soc0 <= 1:
        raise ValueError(f"状态 soc 应位于 [0,1],实际 {soc0}")

    e = soc0 * capacity_j
    n = ch.size
    soc = np.empty(n + 1, dtype=np.float64)
    soc[0] = float(np.clip(soc0, soc_min, soc_max))
    for tau in range(n):
        e += (charge_efficiency * ch[tau] - dis[tau] / discharge_efficiency) * dt_s
        soc[tau + 1] = float(np.clip(e / capacity_j, soc_min, soc_max))
    return soc, {"soc": float(soc[-1])}


# ---------------------------------------------------------------------------
# 机理映射表(yaml function.entry 函数名 → 基础函数 + 键/单位绑定)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParamBinding:
    """机理函数参数绑定:yaml 参数键 → 基础函数形参 + 注册表业务单位。"""

    fn_arg: str  # 基础函数形参名
    unit: str  # 业务单位(kW/kWh/kWp/°C/- 等,与注册表约定一致)


@dataclass(frozen=True, slots=True)
class MechanismSpec:
    """机理函数映射声明(02 §6.5:机理方法 mapping 到基础函数)。"""

    name: str  # 函数名(= yaml function.entry / model_function 末段)
    fn: Callable  # 基础函数(SI)
    series_keys: tuple[str, ...]  # 需要的标准列/运行序列键
    param_bindings: dict[str, ParamBinding]  # yaml 参数键 → (形参名, 业务单位)
    output_name: str  # 输出端口名(DeviceRunResult.outputs 键)
    output_unit: str = "W"  # 输出字段注册单位
    state_key: str | None = None  # 有状态函数:状态键(如 'soc')
    state_arg: str = "state"  # 有状态函数:状态形参名(默认 'state')
    takes_dt: bool = False  # 是否接收 dt_s 参数(能量积分类函数)


MECHANISM_FUNCTIONS: dict[str, MechanismSpec] = {
    "pv_output": MechanismSpec(
        name="pv_output",
        fn=pv_output,
        series_keys=("ghi", "t_ambient"),
        param_bindings={
            "rated_capacity_kwp": ParamBinding("rated_capacity_w", "kWp"),
            "efficiency": ParamBinding("efficiency", "-"),
        },
        output_name="pv_out",
        output_unit="W",
    ),
    "heat_pump_cop": MechanismSpec(
        name="heat_pump_cop",
        fn=heat_pump_cop,
        series_keys=("t_ambient",),
        param_bindings={},
        output_name="cop",
        output_unit="-",
    ),
    "boiler_output": MechanismSpec(
        name="boiler_output",
        fn=boiler_output,
        series_keys=("h_load",),
        param_bindings={"efficiency": ParamBinding("efficiency", "-")},
        output_name="boiler_out",
        output_unit="W",
    ),
    "chiller_output": MechanismSpec(
        name="chiller_output",
        fn=chiller_output,
        series_keys=("elec_power",),
        param_bindings={"cop": ParamBinding("cop", "-")},
        output_name="chiller_out",
        output_unit="W",
    ),
    "gas_volume_m3": MechanismSpec(
        name="gas_volume_m3",
        fn=gas_volume_m3,
        series_keys=("h_load",),
        param_bindings={
            "efficiency": ParamBinding("efficiency", "-"),
            "lhv_j_per_m3": ParamBinding("lhv_j_per_m3", "J/m³"),
        },
        output_name="gas_volume",
        output_unit="m³",
    ),
    "simulate_battery": MechanismSpec(
        name="simulate_battery",
        fn=simulate_battery,
        series_keys=("charge_w", "discharge_w"),
        param_bindings={
            "capacity_kwh": ParamBinding("capacity_j", "kWh"),
            "initial_soc": ParamBinding("soc_initial", "-"),
            "charge_efficiency": ParamBinding("charge_efficiency", "-"),
            "discharge_efficiency": ParamBinding("discharge_efficiency", "-"),
            "min_soc": ParamBinding("soc_min", "-"),
            "max_soc": ParamBinding("soc_max", "-"),
        },
        output_name="bat_out",
        output_unit="W",
        state_key="soc",
        takes_dt=True,
    ),
    "heat_transfer_q": MechanismSpec(
        name="heat_transfer_q",
        fn=heat_transfer_q,
        series_keys=("t_hot", "t_cold"),
        param_bindings={"ua_w_per_k": ParamBinding("ua_w_per_k", "W/K")},
        output_name="heat_flow",
        output_unit="W",
    ),
    "power_balance": MechanismSpec(
        name="power_balance",
        fn=power_balance,
        series_keys=("p_in", "p_out"),
        param_bindings={},
        output_name="net_power",
        output_unit="W",
    ),
}


def mechanism_spec_for(model_function: str) -> MechanismSpec | None:
    """按 model_function env path 末段查机理映射(不存在返回 None)。"""
    if not model_function:
        return None
    name = model_function.rsplit(".", 1)[-1]
    return MECHANISM_FUNCTIONS.get(name)


# ---------------------------------------------------------------------------
# 统一契约包装(02 §6.5:包装层完成业务单位 → SI 参数换算与参数映射)
# ---------------------------------------------------------------------------


def _to_si_param(value: float, unit: str) -> float:
    """业务单位参数 → SI(换算唯一入口 core/units.py to_si,01 §4.1 定案)。

    'kWp' 由注册表 ALIAS_MAP 归一为 kW(注册表已含 kWp → 1000 W);
    '-'/无量纲原样透传; 未注册单位抛 UnitError(禁止静默透传, 01 定案)。
    """
    if unit in ("-", "", None):
        return float(value)
    return float(to_si(value, unit))


def as_device_entry(
    fn: Callable,
    *,
    series_keys: tuple[str, ...],
    param_bindings: dict[str, ParamBinding],
    output_name: str,
    state_key: str | None = None,
    state_arg: str = "state",
    takes_dt: bool = False,
) -> Callable:
    """把基础机理函数包装为统一调用契约 device_entry(params, series, state, dt_s, prices)。

    包装层职责(02 §6.5):
    - 参数映射:params(业务单位)→ 基础函数形参,经 _to_si_param 换算为 SI;
    - 序列直通:series 已是内部单位(W/J/K),按键取用;
    - 状态传递:stateful 时把 state[state_key] 传入函数 state_arg 形参,结果 state_new
      回写(有状态暴露 state 输入输出);stateless 时 state_new 恒为 None;
    - 输出:包装为 DeviceRunResult(outputs={output_name: ...})。
    """
    if not series_keys:
        raise ValueError("series_keys 不能为空")
    if not output_name:
        raise ValueError("output_name 不能为空")
    # 基础函数形参 → 是否有默认值(缺省参数可跳过, 由函数默认值兜底)
    import inspect

    fn_params = inspect.signature(fn).parameters

    def entry(
        params: dict[str, float],
        series: dict[str, np.ndarray],
        state: dict[str, float] | None,
        dt_s: float,
        prices: dict[str, float],
    ) -> DeviceRunResult:
        kwargs: dict[str, object] = {}
        for key, bind in param_bindings.items():
            if key not in params:
                sig = fn_params.get(bind.fn_arg)
                if sig is None or sig.default is not inspect.Parameter.empty:
                    continue  # 可选参数: 缺省跳过, 由基础函数默认值兜底
                raise ModelingConfigError(f"设备参数缺失: {key!r}")
            kwargs[bind.fn_arg] = _to_si_param(params[key], bind.unit)
        args = [series[key] for key in series_keys]
        if state_key is not None:
            # 有状态函数接收完整状态快照 dict(函数内部按键取用, 如 state['soc'])
            kwargs[state_arg] = state if state else None
        if takes_dt:
            kwargs["dt_s"] = dt_s
        result = fn(*args, **kwargs)
        if state_key is not None:
            out_arr, state_new = result
            return DeviceRunResult(
                outputs={output_name: np.asarray(out_arr, dtype=np.float64)},
                state_new=state_new if isinstance(state_new, dict) else {"soc": float(state_new)},
            )
        return DeviceRunResult(outputs={output_name: np.asarray(result, dtype=np.float64)})

    return entry
