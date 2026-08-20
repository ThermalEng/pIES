"""任意方案评价引擎(02 §5.3、§7、§8、§9)。

evaluate_plan(plan, data, axis, options) 构建固定容量运行 MILP 并求解:
- 变量:逐时功率流(电网购/售、光伏、电池充/放、热泵电耗与热/冷出力、锅炉产热
  与燃气输入、制冷机产冷与电耗、泵耗电、电池能量 E、削减量),单位 W(E 为 J);
- 二进制:电池充放互斥 u_ch/u_dis(每步);热泵双模式互斥 u_hp_h/u_hp_c(mode=both);
- 目标:最小化年运行成本 = 购电费 − 售电收入 + 燃气费(+ 削减惩罚,启用削减时);
- 输出 EvalResult:逐时流字段(02 §8.1 命名,功率 W、SOC 0..1)+ kpi(02 §8.2)
  + diagnostics(04 §5 结构);金额用 Decimal 重算(CONTRACT §3),矩阵用 float64。

步长:15/30/60 分钟均可用(时间步长因子 = step_minutes/60 小时用于能量换算)。

引擎级诊断码(ENG-*,本模块稳定产出,消息键 ies.diag.eng.*):
- ENG-SOLVE-001  求解失败/无可行解(error)
- ENG-SOLVE-002  时间上限,返回 incumbent(warning)
- ENG-AUDIT-001  后验残差审计未通过(error,02 §9.1)
- ENG-SHED-001   发生负荷削减,必须显著报告(warning,02 §3.7)
- ENG-NOTE-001   禁止反送电已生效(info,02 §3.6)
- ENG-NOTE-002   需量费未建模(方案含 demand_charge,本引擎 P1 不优化需量费,info)
- ENG-NOTE-003   方案缺少电网连接设备,购电按无容量上限处理(info)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal

import numpy as np
from scipy import sparse
from scipy.optimize import Bounds, LinearConstraint

from iesplan.core.timeaxis import TimeAxis
from iesplan.engines.balance import (
    DEFAULT_C_PC,
    DEFAULT_C_PH,
    DEFAULT_LAMBDA_C,
    DEFAULT_LAMBDA_H,
    build_cold_balance,
    build_electric_balance,
    build_grid_capacity,
    build_heat_balance,
    build_pump_equation,
)
from iesplan.engines.devices import (
    DEFAULT_BOILER_EFFICIENCY,
    DEFAULT_CHILLER_COP,
    DEFAULT_LHV_J_PER_M3,
    DEFAULT_NOCT,
    DEFAULT_PV_EFFICIENCY,
    DEFAULT_SOC_MAX,
    DEFAULT_SOC_MIN,
    DEFAULT_TEMP_COEFF,
    heat_pump_cop,
    pv_output,
)
from iesplan.engines.solver import DEFAULT_MIP_REL_GAP, DEFAULT_TIME_LIMIT, solve_milp

#: kWh → J(02 §2.2:1 kWh = 3.6e6 J)
KWH_TO_J = 3.6e6
#: W → kW
W_TO_KW = 1e-3
#: 削减惩罚默认系数(5 × 最高购电单价,02 §3.7)
SHED_PENALTY_MULTIPLIER = 5.0
#: 数值零容差(判定充放互斥/边界)
_EPS = 1e-6

#: 设备类型 id 常量
T_GRID = "ies.device.grid_connection"
T_PV = "ies.device.pv"
T_BATTERY = "ies.device.battery"
T_HP = "ies.device.heat_pump"
T_BOILER = "ies.device.gas_boiler"
T_CHILLER = "ies.device.electric_chiller"

#: 新增设备的容量参数名(用于规划引擎网格枚举,04 §3 参数名)
CAPACITY_PARAM: dict[str, str] = {
    T_PV: "rated_capacity_kwp",
    T_BATTERY: "capacity_kwh",
    T_HP: "rated_heat_kw",
    T_BOILER: "rated_heat_kw",
    T_CHILLER: "rated_cooling_kw",
}

#: 新增设备容量上限参数名(04 §3)
MAX_CAPACITY_PARAM: dict[str, str] = {
    T_PV: "max_capacity_kwp",
    T_BATTERY: "max_capacity_kwh",
    T_HP: "max_heat_kw",
    T_BOILER: "max_heat_kw",
    T_CHILLER: "max_cooling_kw",
}


def _diag(
    code: str, severity: str, message_key: str, params: dict | None = None, location: dict | None = None,
) -> dict:
    """构造引擎级诊断 dict(04 §5.4 JSON 结构,后端只出数据与消息键)。"""
    return {
        "code": code,
        "severity": severity,
        "blocking": severity == "blocking",
        "message_key": message_key,
        "params": params or {},
        "location": location,
        "source": "solve.eval_run",
        "ref_ids": [],
    }


@dataclass(slots=True)
class EvalResult:
    """方案评价结果(02 §7.4、§8)。

    属性:
        status: ok / infeasible / unbounded / time_limit / numerical_failure。
        flows: 逐时流字段 dict(02 §8.1 命名;功率 W、SOC 0..1、E J、u 为 0/1)。
        kpi: 汇总指标 dict(02 §8.2;金额为 Decimal,其余 float)。
        diagnostics: 诊断 dict 列表(04 §5 结构)。
        objective: 目标函数值(最小化运行成本,元)。
        gap: MIP gap(%)(02 §9.2)。
        stop_reason: 求解器停止原因。
        solve_info: 求解器/模型信息(时间步长、变量数、二进制数等)。
    """

    status: str
    flows: dict[str, np.ndarray]
    kpi: dict
    diagnostics: list[dict]
    objective: float | None = None
    gap: float | None = None
    stop_reason: str = ""
    solve_info: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 方案/数据解析
# ---------------------------------------------------------------------------


def _find_device(plan: dict, type_id: str) -> dict | None:
    """在方案的设备列表中查找指定类型的第一台设备(未找到返回 None)。"""
    for dev in plan.get("devices", []):
        if dev.get("type") == type_id:
            return dev
    return None


def _param(dev: dict | None, name: str, default: float) -> float:
    """设备参数取值(缺失时用默认值)。"""
    if dev is None:
        return default
    return float(dev.get("params", {}).get(name, default))


def _str_param(dev: dict | None, name: str, default: str) -> str:
    """设备字符串参数取值(缺失时用默认值)。"""
    if dev is None:
        return default
    v = dev.get("params", {}).get(name, default)
    return str(v) if v is not None else default


def _check_series_len(arr: np.ndarray | None, n: int, name: str) -> np.ndarray:
    """校验逐时数组长度并转 float64;None 时返回零数组。"""
    if arr is None:
        return np.zeros(n, dtype=np.float64)
    a = np.asarray(arr, dtype=np.float64)
    if a.ndim == 0:
        return np.full(n, float(a))
    if a.ndim != 1 or a.size != n:
        raise ValueError(f"data['{name}'] 长度应为 {n},实际 {a.size}")
    if not np.all(np.isfinite(a)):
        raise ValueError(f"data['{name}'] 包含 NaN/Inf")
    return a


def _resolve_tariff(data: dict, n: int, key: str, default: float) -> np.ndarray:
    """电价序列解析:数组直接使用;字典 + data['tariff_period'](int 索引)映射。

    返回 (n,) 元/kWh 逐时数组。
    """
    v = data.get(key)
    if v is None:
        return np.full(n, float(default))
    if isinstance(v, dict):
        # 时段表:dict 值列表按插入顺序,data['tariff_period'] 为 0..k-1 索引数组
        periods = data.get("tariff_period")
        if periods is None:
            raise ValueError("电价按时段表给出时,data 必须含 'tariff_period'(逐时时段索引数组)")
        values = np.array([float(x) for x in v.values()], dtype=np.float64)
        p = np.asarray(periods, dtype=np.int64)
        if p.ndim != 1 or p.size != n:
            raise ValueError(f"data['tariff_period'] 长度应为 {n},实际 {p.size}")
        if p.size and (p.min() < 0 or p.max() >= values.size):
            raise ValueError("data['tariff_period'] 索引超出时段表范围")
        return values[p]
    arr = np.asarray(v, dtype=np.float64)
    if arr.ndim == 0:
        return np.full(n, float(arr))
    if arr.ndim != 1 or arr.size != n:
        raise ValueError(f"data['{key}'] 长度应为 {n},实际 {arr.size}")
    return arr


# ---------------------------------------------------------------------------
# 变量布局
# ---------------------------------------------------------------------------


def _band(n: int, n_vars: int, bands: list[tuple[int, float | np.ndarray]]) -> sparse.csr_matrix:
    """构造逐 τ 稀疏对角带矩阵(形状 (n, n_vars))。

    bands 为 [(列块起始, 逐时系数)] 列表;第 τ 行在列 start+τ 处的系数为
    coef[τ](标量系数广播为 n 维)。稀疏表示保证内存 O(n·|bands|),全年
    8760 步模型可构建(稠密矩阵为 O(n·n_vars) 内存,不可行)。
    """
    rows: list[np.ndarray] = []
    cols: list[np.ndarray] = []
    vals: list[np.ndarray] = []
    tau = np.arange(n, dtype=np.int64)
    for start, coef in bands:
        rows.append(tau)
        cols.append(int(start) + tau)
        vals.append(np.broadcast_to(np.asarray(coef, dtype=np.float64), (n,)))
    if not rows:
        return sparse.csr_matrix((n, n_vars), dtype=np.float64)
    return sparse.csr_matrix(
        (np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
        shape=(n, n_vars),
    )


def _make_layout(
    n: int,
    *,
    has_battery: bool,
    shedding: bool,
    hp_both: bool,
) -> tuple[dict[str, int], int, list[int]]:
    """构造变量块布局:返回 (块名→起始列, 变量总数, 二进制块名列表)。

    连续流块每块 n 步(列号 = 起始 + τ);e_bat 为 n+1 步(含初始状态)。
    二进制块(整数变量)每块 n 步。
    """
    block_names: list[tuple[str, int]] = [
        ("p_grid_buy", n), ("p_grid_sell", n), ("p_pv", n),
        ("p_bat_ch", n), ("p_bat_dis", n),
        ("p_hp_elec", n), ("p_hp_heat", n), ("p_hp_cool", n),
        ("p_boiler", n), ("p_boiler_gas", n),
        ("p_chiller", n), ("p_chiller_elec", n), ("p_pump", n),
    ]
    if has_battery:
        block_names.append(("e_bat", n + 1))
    if shedding:
        block_names += [("p_shed_e", n), ("p_shed_h", n), ("p_shed_c", n)]
    binary_blocks: list[str] = []
    if has_battery:
        binary_blocks += ["u_ch", "u_dis"]
    if hp_both:
        binary_blocks += ["u_hp_h", "u_hp_c"]
    block_names += [(name, n) for name in binary_blocks]

    layout: dict[str, int] = {}
    start = 0
    for name, size in block_names:
        layout[name] = start
        start += size
    return layout, start, binary_blocks


# ---------------------------------------------------------------------------
# 残差审计(02 §9.1)
# ---------------------------------------------------------------------------


def _audit_flows(
    flows: dict[str, np.ndarray],
    *,
    e_load: np.ndarray,
    h_load: np.ndarray,
    c_load: np.ndarray,
    lambda_h: float,
    lambda_c: float,
    c_ph: float,
    c_pc: float,
    eta_ch: float,
    eta_dis: float,
    dt_s: float,
    e_cap_j: float,
) -> list[dict]:
    """逐物理量残差审计(02 §9.1):归一化残差 = r/S,任一 > 容差即报错。

    返回诊断列表(全部通过时为空)。
    """
    diags: list[dict] = []
    n = int(e_load.size)

    def norm_resid(resid: np.ndarray, scale: float) -> float:
        return float(np.max(np.abs(resid))) / max(1.0, scale)

    # 电平衡(02 §3.2 E-BAL)
    r_e = (
        flows["p_grid_buy"] + flows["p_pv"] + flows["p_bat_dis"]
        - (flows["p_grid_sell"] + flows["p_bat_ch"] + flows["p_hp_elec"]
           + flows["p_chiller_elec"] + flows["p_pump"])
        - e_load
        + flows.get("p_shed_e", np.zeros(n))
    )
    # 热平衡(H-BAL)
    r_h = (
        flows["p_boiler"] + flows["p_hp_heat"]
        - (1.0 + lambda_h) * (h_load - flows.get("p_shed_h", np.zeros(n)))
    )
    # 冷平衡(C-BAL)
    r_c = (
        flows["p_chiller"] + flows["p_hp_cool"]
        - (1.0 + lambda_c) * (c_load - flows.get("p_shed_c", np.zeros(n)))
    )
    # 泵耗电(PUMP)
    r_p = flows["p_pump"] - c_ph * (flows["p_boiler"] + flows["p_hp_heat"]) - c_pc * (
        flows["p_chiller"] + flows["p_hp_cool"]
    )
    items = [
        ("电平衡", r_e, float(np.max(e_load)) if e_load.size else 0.0, 1e-6),
        ("热平衡", r_h, float(np.max(h_load)) if h_load.size else 0.0, 1e-6),
        ("冷平衡", r_c, float(np.max(c_load)) if c_load.size else 0.0, 1e-6),
        ("泵耗电", r_p, max(float(np.max(flows["p_pump"])), 1.0), 1e-6),
    ]
    # SOC 递推(BAT-SOC)
    if "e_bat" in flows:
        e = flows["e_bat"]
        r_s = e[1:] - e[:-1] - eta_ch * flows["p_bat_ch"] * dt_s + flows["p_bat_dis"] / eta_dis * dt_s
        items.append(("SOC 递推", r_s, e_cap_j, 1e-6))
    # 充放互斥(整数,容差 0)
    if "u_ch" in flows:
        items.append(("充放互斥", flows["u_ch"] + flows["u_dis"] - 1.0, 1.0, 1e-6))

    for name, resid, scale, tol in items:
        nr = norm_resid(resid, scale)
        if nr > tol:
            tau = int(np.argmax(np.abs(resid)))
            diags.append(
                _diag(
                    "ENG-AUDIT-001",
                    "error",
                    "ies.diag.eng.audit_fail",
                    {"item": name, "residual": float(np.max(np.abs(resid))), "scale": float(scale),
                     "normalized": nr, "tau": tau},
                )
            )
    return diags


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def evaluate_plan(
    plan: dict,
    data: dict,
    axis: TimeAxis,
    options: dict | None = None,
) -> EvalResult:
    """任意方案评价(02 §7.4):固定容量运行 MILP 求解。

    参数:
        plan: 方案 dict:
            - "devices": 设备实例列表,每项 {"type": 注册设备 id, "params": {...},
              "is_new": bool}(存量容量固定;新增在本引擎同样固定为给定容量,
              只做运行优化,02 §7.1);参数名见 04 §3 注册表。
            - "reverse_feed_allowed": bool(默认 False = 禁止反送电,02 §3.6)。
            - "lambda_h" / "lambda_c": 热/冷输配损耗率(默认 0.05 / 0.08)。
            - "c_ph" / "c_pc": 泵耗电系数 W/W(默认 0.02)。
            - "c_tr_h" / "c_tr_c": 可选热/冷输配容量 W(02 §3.3/§3.4 H-TR/C-TR)。
        data: 逐时数据 dict:
            - "e_load" / "h_load" / "c_load": (n,) W(热/冷缺省为 0)。
            - "temperature": (n,) °C;仅 PV/热泵存在时必需。
            - "ghi": (n,) W/m²;仅 PV 存在时必需。
            - "tariff_buy": (n,) 元/kWh 数组,或 {峰:…,平:…,谷:…} 时段表 +
              "tariff_period" 索引数组(元/kWh)。
            - "tariff_sell": 标量或 (n,) 元/kWh(缺省 0.35;电网设备 export_tariff 优先)。
            - "gas_price": 元/m³(缺省 3.2;锅炉设备 gas_price 优先)。
            - "emission_factor_grid": kg/kWh(缺省 0.581);"emission_factor_gas": kg/m³(缺省 2.0)。
        axis: 时间轴(TimeAxis,15/30/60 分钟均可;n = 步数)。
        options: dict:
            - "shedding": bool(默认 False = 不允许削减,02 §3.7)。
            - "shed_penalty": 元/kWh(缺省 5 × 最高购电价)。
            - "timeout": 秒(缺省 600,02 §9.3)。
            - "mip_rel_gap": 相对 gap 停止条件(缺省 0.001)。
    返回:
        EvalResult(02 §8)。
    """
    opts = options or {}
    n = int(axis.n)
    dt_s = float(axis.step_seconds)
    dt_h = dt_s / 3600.0
    shedding = bool(opts.get("shedding", False))

    grid_dev = _find_device(plan, T_GRID)
    pv_dev = _find_device(plan, T_PV)
    bat_dev = _find_device(plan, T_BATTERY)
    hp_dev = _find_device(plan, T_HP)
    boiler_dev = _find_device(plan, T_BOILER)
    chiller_dev = _find_device(plan, T_CHILLER)

    has_battery = bat_dev is not None and _param(bat_dev, "capacity_kwh", 0.0) > 0
    has_hp = hp_dev is not None and _param(hp_dev, "rated_heat_kw", 0.0) > 0
    has_boiler = boiler_dev is not None and _param(boiler_dev, "rated_heat_kw", 0.0) > 0
    has_chiller = chiller_dev is not None and _param(chiller_dev, "rated_cooling_kw", 0.0) > 0
    has_pv = pv_dev is not None and _param(pv_dev, "rated_capacity_kwp", 0.0) > 0

    hp_mode = _str_param(hp_dev, "mode", "both") if has_hp else "none"
    if hp_mode == "cooling_heating_combo":
        hp_mode = "both"
    hp_both = hp_mode == "both"

    # 逐时数据
    e_load = _check_series_len(data.get("e_load"), n, "e_load")
    h_load = _check_series_len(data.get("h_load"), n, "h_load")
    c_load = _check_series_len(data.get("c_load"), n, "c_load")
    temperature = _check_series_len(data.get("temperature"), n, "temperature")
    ghi = _check_series_len(data.get("ghi"), n, "ghi")

    # 参数:输配损耗与泵系数(02 §3.3-§3.5)
    lambda_h = float(plan.get("lambda_h", DEFAULT_LAMBDA_H))
    lambda_c = float(plan.get("lambda_c", DEFAULT_LAMBDA_C))
    c_ph = float(plan.get("c_ph", DEFAULT_C_PH))
    c_pc = float(plan.get("c_pc", DEFAULT_C_PC))

    # 电网(02 §3.6 GRID-CAP)
    forbid_reverse = not bool(plan.get("reverse_feed_allowed", False))
    diagnostics: list[dict] = []
    if grid_dev is not None:
        c_import_w = _param(grid_dev, "max_import_power_kw", 0.0) * 1000.0
        c_export_w = _param(grid_dev, "max_export_power_kw", 0.0) * 1000.0
        export_tariff = _param(grid_dev, "export_tariff", -1.0)
    else:
        c_import_w = float("inf")
        c_export_w = 0.0
        export_tariff = -1.0
        diagnostics.append(
            _diag("ENG-NOTE-003", "info", "ies.diag.eng.no_grid_device",
                  {"import_cap_w": "inf"})
        )
    if forbid_reverse:
        c_export_w = 0.0
        diagnostics.append(
            _diag("ENG-NOTE-001", "info", "ies.diag.eng.reverse_feed_forbidden")
        )
    demand_charge = _param(grid_dev, "demand_charge", 0.0)
    if grid_dev is not None and demand_charge > 0:
        diagnostics.append(
            _diag("ENG-NOTE-002", "info", "ies.diag.eng.demand_charge_not_modeled",
                  {"demand_charge": demand_charge})
        )

    # 电价/气价(元/kWh、元/m³)
    tariff_buy = _resolve_tariff(data, n, "tariff_buy", 0.7)
    if export_tariff >= 0.0:
        tariff_sell = np.full(n, float(export_tariff))
    else:
        tariff_sell = _resolve_tariff(data, n, "tariff_sell", 0.35)
    gas_price = float(_param(boiler_dev, "gas_price", -1.0))
    if gas_price < 0:
        gas_price = float(data.get("gas_price", 3.2))
    eff_grid = float(data.get("emission_factor_grid", 0.581))
    eff_gas = float(data.get("emission_factor_gas", 2.0))

    # 削减惩罚(02 §3.7:默认 5 × 最高购电价)
    shed_penalty = float(opts.get("shed_penalty", SHED_PENALTY_MULTIPLIER * float(np.max(tariff_buy))))

    # 设备参数(02 §4)
    pv_avail = np.zeros(n)
    if has_pv:
        cap_w = _param(pv_dev, "rated_capacity_kwp", 0.0) * 1000.0
        eff_pv = _param(pv_dev, "efficiency", DEFAULT_PV_EFFICIENCY)
        pv_avail = pv_output(
            ghi, cap_w, temperature,
            eff=eff_pv,
            temp_coeff=_param(pv_dev, "temp_coeff", DEFAULT_TEMP_COEFF),
            tilt=_param(pv_dev, "tilt_deg", 30.0),
            azimuth=_param(pv_dev, "azimuth_deg", 180.0),
            noct=_param(pv_dev, "noct", DEFAULT_NOCT),
        )
        p_inv_w = _param(pv_dev, "inverter_capacity_kw", 0.0) * 1000.0
        if p_inv_w <= 0:
            p_inv_w = cap_w
        pv_avail = np.minimum(pv_avail, p_inv_w)

    eta_ch = eta_dis = 0.95
    soc_min = DEFAULT_SOC_MIN
    soc_max = DEFAULT_SOC_MAX
    soc0 = 0.5
    e_cap_j = 0.0
    p_ch_max = 0.0
    p_dis_max = 0.0
    if has_battery:
        e_cap_kwh = _param(bat_dev, "capacity_kwh", 0.0)
        e_cap_j = e_cap_kwh * KWH_TO_J
        eta_ch = _param(bat_dev, "charge_efficiency", 0.95)
        eta_dis = _param(bat_dev, "discharge_efficiency", 0.95)
        soc_min = _param(bat_dev, "min_soc", DEFAULT_SOC_MIN)
        soc_max = _param(bat_dev, "max_soc", DEFAULT_SOC_MAX)
        soc0 = _param(bat_dev, "initial_soc", 0.5)
        p_rated_kw = _param(bat_dev, "rated_power_kw", 0.0)
        if p_rated_kw <= 0:
            p_rated_kw = e_cap_kwh  # 默认 1C 充放倍率
        p_ch_max = p_rated_kw * 1000.0
        p_dis_max = p_rated_kw * 1000.0

    hp_cap_w = 0.0
    cop_h = np.full(n, 0.0)
    cop_c = np.full(n, 0.0)
    if has_hp:
        hp_cap_w = _param(hp_dev, "rated_heat_kw", 0.0) * 1000.0
        cop_const = _param(hp_dev, "cop", 3.2)
        hp_params = hp_dev.get("params", {})
        profile = hp_params.get("cop_profile")
        # COP 序列优先级:显式 cop_profile > 温度卡诺近似(有温度数据时) > 常数 cop
        if profile is not None:
            prof = np.asarray(profile, dtype=np.float64)
            if prof.ndim == 0:
                cop_h = np.full(n, float(prof)) if hp_mode in ("heating", "both") else cop_h
                cop_c = np.full(n, float(prof)) if hp_mode in ("cooling", "both") else cop_c
            else:
                if prof.ndim != 1 or prof.size != n:
                    raise ValueError(f"cop_profile 长度应为 {n},实际 {prof.size}")
                cop_h = np.clip(prof, 1e-6, None) if hp_mode in ("heating", "both") else cop_h
                cop_c = np.clip(prof, 1e-6, None) if hp_mode in ("cooling", "both") else cop_c
        elif "temperature" in data:
            # 温度卡诺近似(02 §4.5)
            if hp_mode in ("heating", "both"):
                cop_h = heat_pump_cop(
                    temperature, "heating",
                    cop_min=_param(hp_dev, "cop_min", 2.0), cop_max=_param(hp_dev, "cop_max", 5.5),
                )
            if hp_mode in ("cooling", "both"):
                cop_c = heat_pump_cop(
                    temperature, "cooling",
                    cop_min=_param(hp_dev, "cop_cool_min", 2.5), cop_max=_param(hp_dev, "cop_cool_max", 6.5),
                )
        else:
            # 无温度数据:常数 COP(P1,02 §4.5 回归式退化为常数)
            cop_h = np.full(n, cop_const) if hp_mode in ("heating", "both") else cop_h
            cop_c = np.full(n, cop_const) if hp_mode in ("cooling", "both") else cop_c
        if hp_mode in ("heating", "cooling"):
            # 单模式:另一模式 COP 置 0(该模式无输出)
            if hp_mode == "heating":
                cop_c = np.zeros(n)
            else:
                cop_h = np.zeros(n)
    elif hp_dev is not None:
        # 存在热泵但容量为 0:等效于无热泵
        has_hp = False

    boiler_eta = DEFAULT_BOILER_EFFICIENCY
    lhv_j = DEFAULT_LHV_J_PER_M3
    boiler_gas_max_w = 0.0
    if has_boiler:
        boiler_eta = _param(boiler_dev, "thermal_efficiency", DEFAULT_BOILER_EFFICIENCY)
        lhv_j = _param(boiler_dev, "lhv_kj_per_m3", DEFAULT_LHV_J_PER_M3 / 1000.0) * 1000.0
        boiler_cap_w = _param(boiler_dev, "rated_heat_kw", 0.0) * 1000.0
        boiler_gas_max_w = boiler_cap_w / boiler_eta

    chiller_cop = DEFAULT_CHILLER_COP
    chiller_elec_max_w = 0.0
    if has_chiller:
        chiller_cop = _param(chiller_dev, "cop", DEFAULT_CHILLER_COP)
        chiller_cap_w = _param(chiller_dev, "rated_cooling_kw", 0.0) * 1000.0
        chiller_elec_max_w = chiller_cap_w / chiller_cop

    # ------------------------------------------------------------------
    # 变量布局与边界
    # ------------------------------------------------------------------
    layout, n_vars, binary_blocks = _make_layout(
        n, has_battery=has_battery, shedding=shedding, hp_both=hp_both,
    )

    def blk(name: str) -> int:
        return layout[name]

    lb = np.zeros(n_vars, dtype=np.float64)
    ub = np.full(n_vars, np.inf, dtype=np.float64)
    ub[blk("p_grid_buy"): blk("p_grid_buy") + n] = c_import_w if np.isfinite(c_import_w) else np.inf
    ub[blk("p_grid_sell"): blk("p_grid_sell") + n] = c_export_w if np.isfinite(c_export_w) else np.inf
    ub[blk("p_pv"): blk("p_pv") + n] = pv_avail
    ub[blk("p_hp_elec"): blk("p_hp_elec") + n] = hp_cap_w
    ub[blk("p_boiler_gas"): blk("p_boiler_gas") + n] = boiler_gas_max_w
    ub[blk("p_chiller_elec"): blk("p_chiller_elec") + n] = chiller_elec_max_w
    ub[blk("p_boiler"): blk("p_boiler") + n] = boiler_gas_max_w * boiler_eta  # 产热上限
    ub[blk("p_chiller"): blk("p_chiller") + n] = chiller_elec_max_w * chiller_cop
    if has_battery:
        e0 = soc0 * e_cap_j
        lb[blk("e_bat"): blk("e_bat") + n + 1] = soc_min * e_cap_j
        ub[blk("e_bat"): blk("e_bat") + n + 1] = soc_max * e_cap_j
        lb[blk("e_bat")] = e0
        ub[blk("e_bat")] = e0  # E(0) = soc0·E_cap(02 §5.4 年初复位)
        lb[blk("p_bat_ch"): blk("p_bat_ch") + n] = 0.0
        ub[blk("p_bat_ch"): blk("p_bat_ch") + n] = p_ch_max
        lb[blk("p_bat_dis"): blk("p_bat_dis") + n] = 0.0
        ub[blk("p_bat_dis"): blk("p_bat_dis") + n] = p_dis_max
    else:
        ub[blk("p_bat_ch"): blk("p_bat_ch") + n] = 0.0
        ub[blk("p_bat_dis"): blk("p_bat_dis") + n] = 0.0
    if not has_hp:
        # 无热泵时供热/供冷输出必须钳制为 0: 否则热/冷平衡方程中出现
        # 无成本无限出力(免费能量), 系统过小也不会判不可行(02 §4.5 HP 语义)
        ub[blk("p_hp_heat"): blk("p_hp_heat") + n] = 0.0
        ub[blk("p_hp_cool"): blk("p_hp_cool") + n] = 0.0
    for b in binary_blocks:
        lb[blk(b): blk(b) + n] = 0.0
        ub[blk(b): blk(b) + n] = 1.0
    bounds = Bounds(lb, ub)

    integrality = np.zeros(n_vars, dtype=np.int8)
    for b in binary_blocks:
        integrality[blk(b): blk(b) + n] = 1

    # ------------------------------------------------------------------
    # 约束
    # ------------------------------------------------------------------
    cons: list[LinearConstraint] = [
        build_electric_balance(
            n, n_vars, e_load=e_load,
            p_grid_buy=blk("p_grid_buy"), p_grid_sell=blk("p_grid_sell"),
            p_pv=blk("p_pv"), p_bat_ch=blk("p_bat_ch"), p_bat_dis=blk("p_bat_dis"),
            p_hp_elec=blk("p_hp_elec"), p_chiller_elec=blk("p_chiller_elec"),
            p_pump=blk("p_pump"),
            p_shed_e=blk("p_shed_e") if shedding else None,
        ),
        build_heat_balance(
            n, n_vars, h_load=h_load,
            p_boiler=blk("p_boiler"), p_hp_heat=blk("p_hp_heat"),
            p_shed_h=blk("p_shed_h") if shedding else None,
            lambda_h=lambda_h,
        ),
        build_cold_balance(
            n, n_vars, c_load=c_load,
            p_chiller=blk("p_chiller"), p_hp_cool=blk("p_hp_cool"),
            p_shed_c=blk("p_shed_c") if shedding else None,
            lambda_c=lambda_c,
        ),
        build_pump_equation(
            n, n_vars,
            p_pump=blk("p_pump"), p_boiler=blk("p_boiler"), p_hp_heat=blk("p_hp_heat"),
            p_chiller=blk("p_chiller"), p_hp_cool=blk("p_hp_cool"),
            c_ph=c_ph, c_pc=c_pc,
        ),
    ]
    cons.extend(build_grid_capacity(
        n, n_vars,
        p_grid_buy=blk("p_grid_buy"), p_grid_sell=blk("p_grid_sell"),
        c_import=c_import_w, c_export=c_export_w, forbid_reverse_feed=forbid_reverse,
    ))

    def row_constraint(
        cols: np.ndarray, coefs: np.ndarray, ub: float, lb: float = -np.inf,
    ) -> LinearConstraint:
        """单行不等式/等式约束:lb ≤ coefs·x[cols] ≤ ub(逐 τ 同系数)。

        lb == ub 即等式约束(如 p_boiler = η_b·p_boiler_gas)。
        """
        A = _band(n, n_vars, [(int(col), float(coef)) for col, coef in zip(cols, coefs, strict=True)])
        return LinearConstraint(A, lb=float(lb), ub=float(ub))

    # 锅炉(02 §4.6 B-P):p_boiler = η_b·p_boiler_gas
    if has_boiler:
        cons.append(row_constraint(
            np.array([blk("p_boiler"), blk("p_boiler_gas")]),
            np.array([1.0, -boiler_eta]), 0.0, lb=0.0,
        ))
    # 制冷机(02 §4.7 C-P):p_chiller = COP·p_chiller_elec
    if has_chiller:
        cons.append(row_constraint(
            np.array([blk("p_chiller"), blk("p_chiller_elec")]),
            np.array([1.0, -chiller_cop]), 0.0, lb=0.0,
        ))
    # 热泵(02 §4.5 HP-P / HP-CAP;rated_heat_kw 为产热/产冷额定容量)
    tau = np.arange(n)
    if has_hp:
        if hp_mode == "heating":
            # p_hp_heat = COP_h(τ)·p_hp_elec;产热 ≤ 额定热容量
            A = _band(n, n_vars, [(blk("p_hp_heat"), 1.0), (blk("p_hp_elec"), -cop_h)])
            cons.append(LinearConstraint(A, lb=np.zeros(n), ub=np.zeros(n)))
            cons.append(row_constraint(np.array([blk("p_hp_heat")]), np.array([1.0]), hp_cap_w))
        elif hp_mode == "cooling":
            A = _band(n, n_vars, [(blk("p_hp_cool"), 1.0), (blk("p_hp_elec"), -cop_c)])
            cons.append(LinearConstraint(A, lb=np.zeros(n), ub=np.zeros(n)))
            cons.append(row_constraint(np.array([blk("p_hp_cool")]), np.array([1.0]), hp_cap_w))
        else:  # both:p_hp_elec = p_hp_heat/COP_h + p_hp_cool/COP_c;模式互斥
            A = _band(n, n_vars, [
                (blk("p_hp_elec"), 1.0),
                (blk("p_hp_heat"), -1.0 / np.maximum(cop_h, 1e-6)),
                (blk("p_hp_cool"), -1.0 / np.maximum(cop_c, 1e-6)),
            ])
            cons.append(LinearConstraint(A, lb=np.zeros(n), ub=np.zeros(n)))
            # 模式互斥:Q ≤ 额定容量·u(HP-CAP,02 §4.5)
            A2 = _band(n, n_vars, [(blk("p_hp_heat"), 1.0), (blk("u_hp_h"), -hp_cap_w)])
            cons.append(LinearConstraint(A2, lb=-np.inf, ub=np.zeros(n)))
            A3 = _band(n, n_vars, [(blk("p_hp_cool"), 1.0), (blk("u_hp_c"), -hp_cap_w)])
            cons.append(LinearConstraint(A3, lb=-np.inf, ub=np.zeros(n)))
            # u_hp_h + u_hp_c ≤ 1
            A4 = _band(n, n_vars, [(blk("u_hp_h"), 1.0), (blk("u_hp_c"), 1.0)])
            cons.append(LinearConstraint(A4, lb=-np.inf, ub=np.ones(n)))
    # 电池(02 §4.4 BAT-SOC / BAT-MU)
    if has_battery:
        # E(τ+1) − E(τ) − η_ch·P_ch·Δt + P_dis/η_dis·Δt = 0(BAT-SOC)
        A = _band(n, n_vars, [
            (blk("e_bat") + 1, 1.0),
            (blk("e_bat"), -1.0),
            (blk("p_bat_ch"), -eta_ch * dt_s),
            (blk("p_bat_dis"), dt_s / eta_dis),
        ])
        cons.append(LinearConstraint(A, lb=np.zeros(n), ub=np.zeros(n)))
        # 期末约束:E(n) ≥ soc0·E_cap(02 §5.4 规划期末)
        A_f = np.zeros((1, n_vars), dtype=np.float64)
        A_f[0, blk("e_bat") + n] = 1.0
        cons.append(LinearConstraint(A_f, lb=np.array([e0]), ub=np.inf))
        # 充放互斥(BAT-MU):P_ch ≤ u_ch·P_max, P_dis ≤ u_dis·P_max, u_ch+u_dis ≤ 1
        A5 = _band(n, n_vars, [(blk("p_bat_ch"), 1.0), (blk("u_ch"), -p_ch_max)])
        cons.append(LinearConstraint(A5, lb=-np.inf, ub=np.zeros(n)))
        A6 = _band(n, n_vars, [(blk("p_bat_dis"), 1.0), (blk("u_dis"), -p_dis_max)])
        cons.append(LinearConstraint(A6, lb=-np.inf, ub=np.zeros(n)))
        A7 = _band(n, n_vars, [(blk("u_ch"), 1.0), (blk("u_dis"), 1.0)])
        cons.append(LinearConstraint(A7, lb=-np.inf, ub=np.ones(n)))
    # 输配容量(02 §3.3 H-TR / §3.4 C-TR,可选)
    c_tr_h = plan.get("c_tr_h")
    if c_tr_h is not None:
        cons.append(row_constraint(
            np.array([blk("p_boiler"), blk("p_hp_heat")]), np.array([1.0, 1.0]), float(c_tr_h),
        ))
    c_tr_c = plan.get("c_tr_c")
    if c_tr_c is not None:
        cons.append(row_constraint(
            np.array([blk("p_chiller"), blk("p_hp_cool")]), np.array([1.0, 1.0]), float(c_tr_c),
        ))

    # ------------------------------------------------------------------
    # 目标:最小化 购电费 − 售电收入 + 燃气费(+ 削减惩罚)
    # 系数:元 per W = π(元/kWh)·Δt(s)/3.6e6
    # ------------------------------------------------------------------
    c_obj = np.zeros(n_vars, dtype=np.float64)
    c_obj[blk("p_grid_buy"): blk("p_grid_buy") + n] = tariff_buy * dt_s / KWH_TO_J
    c_obj[blk("p_grid_sell"): blk("p_grid_sell") + n] = -tariff_sell * dt_s / KWH_TO_J
    if has_boiler:
        c_obj[blk("p_boiler_gas"): blk("p_boiler_gas") + n] = gas_price * dt_s / lhv_j
    if shedding:
        for name in ("p_shed_e", "p_shed_h", "p_shed_c"):
            c_obj[blk(name): blk(name) + n] = shed_penalty * dt_s / KWH_TO_J

    # ------------------------------------------------------------------
    # 求解(02 §9.2/§9.3)
    # ------------------------------------------------------------------
    timeout = float(opts.get("timeout", DEFAULT_TIME_LIMIT))
    mip_gap_setting = float(opts.get("mip_rel_gap", DEFAULT_MIP_REL_GAP))
    seed = int(opts.get("seed", 42))  # 快照随机 seed 经 selector 注入(03 §9.4),缺省 42
    result = solve_milp(
        c_obj, integrality, bounds, cons,
        timeout=timeout, mip_rel_gap=mip_gap_setting, seed=seed,
    )
    solve_info = {
        "n_steps": n, "dt_s": dt_s, "n_vars": n_vars,
        "n_binary": n * len(binary_blocks),
        "has_battery": has_battery, "shedding": shedding,
        "solver": result.raw.get("solver", ""),
        "mip_node_count": result.raw.get("mip_node_count"),
    }

    if result.status in ("infeasible", "unbounded") or result.x is None:
        diagnostics.append(
            _diag("ENG-SOLVE-001", "error", "ies.diag.eng.solve_infeasible",
                  {"status": result.status, "reason": result.stop_reason})
        )
        return EvalResult(
            status=result.status, flows={}, kpi={}, diagnostics=diagnostics,
            objective=result.objective, gap=result.gap, stop_reason=result.stop_reason,
            solve_info=solve_info,
        )
    if result.status == "time_limit":
        diagnostics.append(
            _diag("ENG-SOLVE-002", "warning", "ies.diag.eng.time_limit_incumbent",
                  {"timeout": timeout, "gap": result.gap, "reason": result.stop_reason})
        )

    x = result.x

    # ------------------------------------------------------------------
    # 逐时流提取(02 §8.1 命名;功率 W,SOC 0..1)
    # ------------------------------------------------------------------
    flows: dict[str, np.ndarray] = {}
    flow_names = [
        "p_grid_buy", "p_grid_sell", "p_pv", "p_bat_ch", "p_bat_dis",
        "p_hp_elec", "p_hp_heat", "p_hp_cool",
        "p_boiler", "p_boiler_gas", "p_chiller", "p_chiller_elec", "p_pump",
    ]
    for name in flow_names:
        flows[name] = x[blk(name): blk(name) + n].copy()
    flows["p_pv_avail"] = pv_avail.copy()
    flows["q_curt_pv"] = np.maximum(pv_avail - flows["p_pv"], 0.0)
    flows["e_load"] = e_load.copy()
    flows["h_load"] = h_load.copy()
    flows["c_load"] = c_load.copy()
    for b in binary_blocks:
        flows[b] = np.round(x[blk(b): blk(b) + n]).astype(np.float64)
    if has_battery:
        e_bat = x[blk("e_bat"): blk("e_bat") + n + 1].copy()
        flows["e_bat"] = e_bat
        flows["soc"] = e_bat / e_cap_j
    if shedding:
        for name in ("p_shed_e", "p_shed_h", "p_shed_c"):
            flows[name] = x[blk(name): blk(name) + n].copy()

    # ------------------------------------------------------------------
    # 残差审计(02 §9.1)
    # ------------------------------------------------------------------
    audit_diags = _audit_flows(
        flows, e_load=e_load, h_load=h_load, c_load=c_load,
        lambda_h=lambda_h, lambda_c=lambda_c, c_ph=c_ph, c_pc=c_pc,
        eta_ch=eta_ch, eta_dis=eta_dis, dt_s=dt_s, e_cap_j=e_cap_j,
    )
    status = result.status
    if audit_diags:
        diagnostics.extend(audit_diags)
        status = "numerical_failure"

    # ------------------------------------------------------------------
    # 逐时费用与排放(02 §8.1 费用/排放列;Decimal 重算金额,CONTRACT §3)
    # ------------------------------------------------------------------
    e_buy_kwh = flows["p_grid_buy"] * dt_h / 1000.0
    e_sell_kwh = flows["p_grid_sell"] * dt_h / 1000.0
    gas_m3 = flows["p_boiler_gas"] * dt_s / lhv_j if has_boiler else np.zeros(n)
    flows["v_gas"] = gas_m3
    flows["cost_buy"] = tariff_buy * e_buy_kwh
    flows["cost_gas"] = gas_price * gas_m3
    flows["revenue_sell"] = tariff_sell * e_sell_kwh
    flows["cost_total_step"] = flows["cost_buy"] + flows["cost_gas"] - flows["revenue_sell"]
    flows["co2_total_step"] = e_buy_kwh * eff_grid + gas_m3 * eff_gas

    buy_cost = _decimal_sum(tariff_buy, e_buy_kwh)
    gas_cost = _decimal_sum(np.full(n, gas_price), gas_m3)
    sell_revenue = _decimal_sum(tariff_sell, e_sell_kwh)

    # ------------------------------------------------------------------
    # KPI(02 §8.2)
    # ------------------------------------------------------------------
    e_pv_kwh = flows["p_pv"] * dt_h / 1000.0
    e_pv_avail_kwh = pv_avail * dt_h / 1000.0
    e_curt_kwh = flows["q_curt_pv"] * dt_h / 1000.0
    e_load_kwh = e_load * dt_h / 1000.0
    e_load_total = float(np.sum(e_load_kwh))

    kpi: dict = {
        "annual_buy_kwh": float(np.sum(e_buy_kwh)),
        "annual_sell_kwh": float(np.sum(e_sell_kwh)),
        "annual_pv_kwh": float(np.sum(e_pv_kwh)),
        "pv_curtailment_kwh": float(np.sum(e_curt_kwh)),
        "pv_curtailment_ratio": float(np.sum(e_curt_kwh) / np.sum(e_pv_avail_kwh))
        if np.sum(e_pv_avail_kwh) > 0 else 0.0,
        "gas_volume_m3": float(np.sum(gas_m3)),
        "annual_heat_supply_kwh": float(np.sum((flows["p_boiler"] + flows["p_hp_heat"]) * dt_h / 1000.0)),
        "annual_cool_supply_kwh": float(np.sum((flows["p_chiller"] + flows["p_hp_cool"]) * dt_h / 1000.0)),
        "self_sufficiency_rate": (e_load_total - float(np.sum(e_buy_kwh))) / e_load_total
        if e_load_total > 0 else 1.0,
        "pv_self_use_rate": float(np.sum(e_pv_kwh) - float(np.sum(e_sell_kwh))) / float(np.sum(e_pv_kwh))
        if float(np.sum(e_pv_kwh)) > 0 else 0.0,
        "total_op_cost": _money(buy_cost + gas_cost - sell_revenue),
        "buy_cost": _money(buy_cost),
        "gas_cost": _money(gas_cost),
        "sell_revenue": _money(sell_revenue),
        "co2_total_kg": float(np.sum(flows["co2_total_step"])),
        "co2_grid_kg": float(np.sum(e_buy_kwh * eff_grid)),
        "co2_gas_kg": float(np.sum(gas_m3 * eff_gas)),
        "peak_grid_buy_kw": float(np.max(flows["p_grid_buy"])) * W_TO_KW,
        "peak_grid_sell_kw": float(np.max(flows["p_grid_sell"])) * W_TO_KW,
        "max_demand_kw": float(np.max(flows["p_grid_buy"])) * W_TO_KW,  # 需量 = 峰值购电(P1 不优化需量费)
    }
    if has_battery:
        cycles = float(np.sum(flows["p_bat_ch"] + flows["p_bat_dis"]) * dt_s / (2.0 * e_cap_j))
        kpi.update({
            "max_soc": float(np.max(flows["soc"])),
            "min_soc": float(np.min(flows["soc"])),
            "initial_soc": soc0,
            "final_soc": float(flows["soc"][-1]),
            "battery_cycles_equivalent": cycles,
        })
    if shedding:
        shed_power = flows["p_shed_e"] + flows["p_shed_h"] + flows["p_shed_c"]
        shed_total_kwh = float(np.sum(shed_power * dt_h / 1000.0))
        demand_power = e_load + (1.0 + lambda_h) * h_load + (1.0 + lambda_c) * c_load
        demand_kwh = float(np.sum(demand_power * dt_h / 1000.0))
        shed_penalty_cost = _money(Decimal(str(shed_penalty)) * Decimal(str(shed_total_kwh)))
        kpi["total_op_cost"] = _money(buy_cost + gas_cost - sell_revenue + Decimal(str(shed_penalty_cost)))
        kpi["shed_penalty_cost"] = shed_penalty_cost
        events: list[list[int]] = []
        active = False
        start = 0
        shed_flag = (flows["p_shed_e"] + flows["p_shed_h"] + flows["p_shed_c"]) > _EPS
        for tau in range(n):
            if shed_flag[tau] and not active:
                active, start = True, tau
            elif not shed_flag[tau] and active:
                active = False
                events.append([start, tau - 1])
        if active:
            events.append([start, n - 1])
        kpi.update({
            "shed_energy_kwh": shed_total_kwh,
            "shed_ratio": shed_total_kwh / demand_kwh if demand_kwh > 0 else 0.0,
            "shed_events": events,
        })
        if shed_total_kwh > _EPS:
            diagnostics.append(
                _diag("ENG-SHED-001", "warning", "ies.diag.eng.shedding",
                      {"shed_energy_kwh": shed_total_kwh,
                       "shed_ratio": kpi["shed_ratio"], "events": events[:10]})
            )

    return EvalResult(
        status=status, flows=flows, kpi=kpi, diagnostics=diagnostics,
        objective=result.objective, gap=result.gap, stop_reason=result.stop_reason,
        solve_info=solve_info,
    )


def _decimal_sum(price: np.ndarray, energy_kwh: np.ndarray) -> Decimal:
    """金额精确重算:逐项 Decimal 单价 × float 能量求和(CONTRACT §3 规则 2)。"""
    total = Decimal("0")
    for p, e in zip(price, energy_kwh, strict=True):
        total += Decimal(str(p)) * Decimal(str(e))
    return total


def _money(d: Decimal) -> Decimal:
    """金额保留 2 位小数,四舍五入 half-even(CONTRACT §3)。"""
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
