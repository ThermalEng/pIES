"""受控注册表:设备类型 + 算法(04 §2-§3)。

受控加载流程(04 §2.2):
- 注册项全部为**模块内静态注册**(模块导入时经 _register() 登记),不做动态导入、
  不做运行时增删(运行期只读);
- 加载时逐条校验:id 唯一、id 符合 `ies.device.*`/`ies.algo.*` 模式、版本为合法
  semver(x.y.z);任一不满足即拒绝加载(注册项不进入目录)并抛 AppError;
- 提供 snapshot() 输出注册表快照(id@version 列表,04 §2.2 规则 5),供计算快照引用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from iesplan.core.errors import AppError, NotFoundError

# ---------------------------------------------------------------------------
# 基础数据结构
# ---------------------------------------------------------------------------
_ID_PATTERN = re.compile(r"^ies\.(device|algo)\.[a-z][a-z0-9_]*$")
_SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")

#: 能量载体(与 02 §3 母线对应)
CARRIERS_ELECTRIC = "electric"
CARRIERS_HEAT = "heat"
CARRIERS_COOL = "cool"
CARRIERS_GAS = "gas"
CARRIERS_SOLAR = "solar"


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """参数规格(04 §3:name/unit/min/max/default/存量/优化变量/帮助键)。

    属性:
        name: 参数名(注册表内唯一)。
        unit: 注册单位(如 kW/kWh/CNY/kWh;- 表示无量纲)。
        min / max: 取值范围(None 表示不限制;枚举类参数用 enum 表达)。
        default: 默认值(数值/字符串/字典;引用类参数为 None)。
        is_optimizable: 是否可作为优化变量(04 §3 的 is_optimization_variable)。
        existing_default: 存量设备默认值(存量即"容量固定只优化运行"的场景,
            见 02 §4.8);新增类容量参数存量默认 0,存量类参数取 default。
        stock_or_addition: 'stock'(存量,容量固定) | 'addition'(新增,容量可优化)。
        help_key: 帮助主题键(help.param.*)。
        enum: 可选枚举取值列表(字符串/数值)。
    """

    name: str
    unit: str
    min: float | None = None
    max: float | None = None
    default: object = None
    is_optimizable: bool = False
    existing_default: float | None = None
    stock_or_addition: str = "stock"
    help_key: str = ""
    enum: tuple | None = None


def _p(
    name: str,
    unit: str,
    min: float | None,
    max: float | None,
    default: object,
    help_key: str,
    *,
    optimizable: bool = False,
    stock: str = "stock",
    existing: float | None = None,
    enum: tuple | None = None,
) -> ParameterSpec:
    """便捷构造参数规格:存量类 existing 缺省取 default;新增类 existing 缺省取 0。"""
    if existing is None:
        existing = default if stock == "stock" else 0.0
    return ParameterSpec(
        name=name,
        unit=unit,
        min=min,
        max=max,
        default=default,
        is_optimizable=optimizable,
        existing_default=existing,
        stock_or_addition=stock,
        help_key=help_key,
        enum=enum,
    )


@dataclass(frozen=True, slots=True)
class DeviceTypeSpec:
    """设备类型注册项(04 §2.1 子集 + CONTRACT 第 2 节)。"""

    type_id: str  # 'ies.device.heat_pump' 等
    version: str  # '1.3.0'
    name_zh: str
    name_en: str
    energy_carriers: list[str]  # ['electric','heat','cool','gas','solar']
    is_load: bool
    capabilities: list[str]  # 04 §2.3 能力字典
    extends: str = "ies.device.base"
    parameters: dict[str, ParameterSpec] = field(default_factory=dict)  # 参数名 → 规格
    help_topic: str = ""


@dataclass(frozen=True, slots=True)
class AlgorithmSpec:
    """算法注册项(04 §2.3 algorithm 类)。"""

    algo_id: str  # 'ies.algo.milp_hybrid'
    version: str
    name_zh: str
    name_en: str
    capabilities: list[str]  # 求解/评估能力清单
    parameters: dict[str, ParameterSpec] = field(default_factory=dict)
    help_topic: str = ""

    @property
    def default_parameters(self) -> dict[str, object]:
        """算法默认参数(供任务创建时填充)。"""
        return {name: p.default for name, p in self.parameters.items()}


# ---------------------------------------------------------------------------
# 注册表本体(模块级静态,运行期只读)
# ---------------------------------------------------------------------------
_DEVICE_TYPES: dict[str, DeviceTypeSpec] = {}
_ALGORITHMS: dict[str, AlgorithmSpec] = {}
_LOADED = False


def _check_version(version: str, what: str) -> None:
    """版本校验:必须为合法 semver(x.y.z)。"""
    if not _SEMVER_PATTERN.match(version):
        raise AppError(
            f"{what} 版本不合法: {version!r}",
            code="SYS-CFG-001",
            message_key="ies.diag.store.config_invalid",
            params={"item": what, "version": version},
        )


def _check_id(rid: str, what: str) -> None:
    """id 校验:格式 ies.(device|algo).<名字>,且目录内唯一。"""
    if not _ID_PATTERN.match(rid):
        raise AppError(
            f"{what} id 不合法: {rid!r}",
            code="SYS-CFG-001",
            message_key="ies.diag.store.config_invalid",
            params={"item": what, "id": rid},
        )
    if rid in _DEVICE_TYPES or rid in _ALGORITHMS:
        raise AppError(
            f"{what} id 重复注册: {rid!r}",
            code="SYS-CFG-001",
            message_key="ies.diag.store.config_invalid",
            params={"item": what, "id": rid},
        )


def _register_device(spec: DeviceTypeSpec) -> None:
    """登记设备类型(静态注册;校验失败抛 AppError 并拒绝加载)。"""
    _check_id(spec.type_id, "设备类型")
    _check_version(spec.version, spec.type_id)
    _DEVICE_TYPES[spec.type_id] = spec


def _register_algorithm(spec: AlgorithmSpec) -> None:
    """登记算法(静态注册;校验失败抛 AppError 并拒绝加载)。"""
    _check_id(spec.algo_id, "算法")
    _check_version(spec.version, spec.algo_id)
    _ALGORITHMS[spec.algo_id] = spec


def load_registry() -> None:
    """受控加载:执行全部静态注册(幂等)。

    模拟 04 §2.2 的"启动时校验 + 注册表构建"环节:完整性(id/版本)→ 冲突检测。
    不做动态导入、不做签名验证(内置注册项由代码即内容)。校验失败抛 AppError。
    """
    global _LOADED
    if _LOADED:
        return
    # 设备类型(9 类,04 §3)
    _register_device(
        DeviceTypeSpec(
            type_id="ies.device.grid_connection",
            version="1.2.0",
            name_zh="电网连接",
            name_en="Grid Connection",
            energy_carriers=[CARRIERS_ELECTRIC],
            is_load=False,
            capabilities=["grid_connection", "power_balance_node"],
            help_topic="help.modeling.grid_connection",
            parameters={
                "max_import_power_kw": _p(
                    "max_import_power_kw",
                    "kW",
                    0,
                    200000,
                    0,
                    "help.param.grid.max_import_power_kw",
                    optimizable=True,
                ),
                "max_export_power_kw": _p(
                    "max_export_power_kw",
                    "kW",
                    0,
                    200000,
                    0,
                    "help.param.grid.max_export_power_kw",
                    optimizable=True,
                ),
                "voltage_level_kv": _p(
                    "voltage_level_kv",
                    "kV",
                    None,
                    None,
                    10,
                    "help.param.grid.voltage_level_kv",
                    enum=(0.4, 10, 35, 110),
                ),
                "import_tariff": _p(
                    "import_tariff",
                    "CNY/kWh",
                    0,
                    None,
                    {"peak": 1.1, "flat": 0.7, "valley": 0.3},
                    "help.param.grid.import_tariff",
                ),
                "export_tariff": _p(
                    "export_tariff", "CNY/kWh", 0, None, 0.35, "help.param.grid.export_tariff"
                ),
                "demand_charge": _p(
                    "demand_charge", "CNY/kW·月", 0, None, 40, "help.param.grid.demand_charge"
                ),
            },
        )
    )
    _register_device(
        DeviceTypeSpec(
            type_id="ies.device.pv",
            version="1.3.0",
            name_zh="光伏",
            name_en="Photovoltaic (PV)",
            energy_carriers=[CARRIERS_SOLAR, CARRIERS_ELECTRIC],
            is_load=False,
            capabilities=["pv", "controllable", "optimization_variable"],
            help_topic="help.modeling.pv",
            parameters={
                "rated_capacity_kwp": _p(
                    "rated_capacity_kwp",
                    "kWp",
                    0,
                    1_000_000,
                    0,
                    "help.param.pv.rated_capacity_kwp",
                    optimizable=True,
                    stock="addition",
                ),
                "max_capacity_kwp": _p(
                    "max_capacity_kwp",
                    "kWp",
                    0,
                    1_000_000,
                    1000,
                    "help.param.pv.max_capacity_kwp",
                    stock="addition",
                ),
                "efficiency": _p("efficiency", "-", 0.05, 0.5, 0.20, "help.param.pv.efficiency"),
                "tilt_deg": _p("tilt_deg", "deg", 0, 90, 30, "help.param.pv.tilt_deg"),
                "azimuth_deg": _p("azimuth_deg", "deg", 0, 360, 180, "help.param.pv.azimuth_deg"),
                "unit_invest_cost": _p(
                    "unit_invest_cost",
                    "CNY/kWp",
                    0,
                    None,
                    3500,
                    "help.param.pv.unit_invest_cost",
                    stock="addition",
                ),
                "lifetime_years": _p("lifetime_years", "a", 1, 50, 25, "help.param.pv.lifetime_years"),
            },
        )
    )
    _register_device(
        DeviceTypeSpec(
            type_id="ies.device.battery",
            version="1.4.0",
            name_zh="电池储能",
            name_en="Battery Storage",
            energy_carriers=[CARRIERS_ELECTRIC],
            is_load=False,
            capabilities=["storage", "controllable", "optimization_variable"],
            help_topic="help.modeling.battery",
            parameters={
                "capacity_kwh": _p(
                    "capacity_kwh",
                    "kWh",
                    0,
                    10_000_000,
                    0,
                    "help.param.battery.capacity_kwh",
                    optimizable=True,
                    stock="addition",
                ),
                "max_capacity_kwh": _p(
                    "max_capacity_kwh",
                    "kWh",
                    0,
                    10_000_000,
                    5000,
                    "help.param.battery.max_capacity_kwh",
                    stock="addition",
                ),
                "rated_power_kw": _p(
                    "rated_power_kw",
                    "kW",
                    0,
                    1_000_000,
                    0,
                    "help.param.battery.rated_power_kw",
                    optimizable=True,
                    stock="addition",
                ),
                "charge_efficiency": _p(
                    "charge_efficiency", "-", 0.5, 1.0, 0.95, "help.param.battery.charge_efficiency"
                ),
                "discharge_efficiency": _p(
                    "discharge_efficiency", "-", 0.5, 1.0, 0.95, "help.param.battery.discharge_efficiency"
                ),
                "max_soc": _p("max_soc", "-", 0.5, 1.0, 0.90, "help.param.battery.max_soc"),
                "min_soc": _p("min_soc", "-", 0, 0.5, 0.1, "help.param.battery.min_soc"),
                "initial_soc": _p("initial_soc", "-", 0, 1.0, 0.5, "help.param.battery.initial_soc"),
                "cycle_life": _p("cycle_life", "次", 100, 20000, 6000, "help.param.battery.cycle_life"),
                "unit_invest_cost": _p(
                    "unit_invest_cost",
                    "CNY/kWh",
                    0,
                    None,
                    900,
                    "help.param.battery.unit_invest_cost",
                    stock="addition",
                ),
                "lifetime_years": _p("lifetime_years", "a", 1, 50, 10, "help.param.battery.lifetime_years"),
            },
        )
    )
    _register_device(
        DeviceTypeSpec(
            type_id="ies.device.electric_load",
            version="1.1.0",
            name_zh="电负荷",
            name_en="Electric Load",
            energy_carriers=[CARRIERS_ELECTRIC],
            is_load=True,
            capabilities=["load", "switchable"],
            parameters={
                "peak_power_kw": _p("peak_power_kw", "kW", 0, 10_000_000, 0, "help.param.load.peak_power_kw"),
                "load_profile": _p(
                    "load_profile", "reference", None, None, None, "help.param.load.load_profile"
                ),
                "annual_energy_kwh": _p(
                    "annual_energy_kwh", "kWh", 0, None, 0, "help.param.load.annual_energy_kwh"
                ),
                "is_switchable": _p("is_switchable", "-", None, None, False, "help.param.load.is_switchable"),
            },
        )
    )
    _register_device(
        DeviceTypeSpec(
            type_id="ies.device.heat_load",
            version="1.1.0",
            name_zh="热负荷",
            name_en="Heat Load",
            energy_carriers=[CARRIERS_HEAT],
            is_load=True,
            capabilities=["load", "heat_load"],
            parameters={
                "peak_heat_kw": _p(
                    "peak_heat_kw", "kW", 0, 10_000_000, 0, "help.param.heatload.peak_heat_kw"
                ),
                "heat_profile": _p(
                    "heat_profile", "reference", None, None, None, "help.param.heatload.heat_profile"
                ),
                "annual_heat_kwh": _p(
                    "annual_heat_kwh", "kWh", 0, None, 0, "help.param.heatload.annual_heat_kwh"
                ),
                "supply_temp_c": _p("supply_temp_c", "°C", 30, 95, 70, "help.param.heatload.supply_temp_c"),
                "return_temp_c": _p("return_temp_c", "°C", 10, 70, 50, "help.param.heatload.return_temp_c"),
            },
        )
    )
    _register_device(
        DeviceTypeSpec(
            type_id="ies.device.cooling_load",
            version="1.1.0",
            name_zh="冷负荷",
            name_en="Cooling Load",
            energy_carriers=[CARRIERS_COOL],
            is_load=True,
            capabilities=["load", "cooling_load", "dual_hvac"],
            parameters={
                "mode": _p(
                    "mode",
                    "-",
                    None,
                    None,
                    "cooling_only",
                    "help.param.coolingload.mode",
                    enum=("cooling_only", "heating_only", "cooling_heating_combo"),
                ),
                "peak_cooling_kw": _p(
                    "peak_cooling_kw", "kW", 0, 10_000_000, 0, "help.param.coolingload.peak_cooling_kw"
                ),
                "cooling_profile": _p(
                    "cooling_profile", "reference", None, None, None, "help.param.coolingload.cooling_profile"
                ),
                "annual_cooling_kwh": _p(
                    "annual_cooling_kwh", "kWh", 0, None, 0, "help.param.coolingload.annual_cooling_kwh"
                ),
                "supply_temp_c": _p("supply_temp_c", "°C", 3, 20, 7, "help.param.coolingload.supply_temp_c"),
                "return_temp_c": _p("return_temp_c", "°C", 7, 30, 12, "help.param.coolingload.return_temp_c"),
            },
        )
    )
    _register_device(
        DeviceTypeSpec(
            type_id="ies.device.heat_pump",
            version="1.3.0",
            name_zh="热泵",
            name_en="Heat Pump",
            energy_carriers=[CARRIERS_ELECTRIC, CARRIERS_HEAT, CARRIERS_COOL],
            is_load=False,
            capabilities=["heat_pump", "controllable", "dual_hvac", "optimization_variable"],
            help_topic="help.modeling.heat_pump",
            parameters={
                "rated_heat_kw": _p(
                    "rated_heat_kw",
                    "kW",
                    0,
                    1_000_000,
                    0,
                    "help.param.heatpump.rated_heat_kw",
                    optimizable=True,
                    stock="addition",
                ),
                "max_heat_kw": _p(
                    "max_heat_kw",
                    "kW",
                    0,
                    1_000_000,
                    1000,
                    "help.param.heatpump.max_heat_kw",
                    stock="addition",
                ),
                "cop": _p("cop", "-", 2.0, 6.5, 3.2, "help.param.heatpump.cop"),
                "cop_profile": _p(
                    "cop_profile", "reference", None, None, None, "help.param.heatpump.cop_profile"
                ),
                "source_type": _p(
                    "source_type",
                    "-",
                    None,
                    None,
                    "air",
                    "help.param.heatpump.source_type",
                    enum=("air", "ground", "water"),
                ),
                "mode": _p(
                    "mode",
                    "-",
                    None,
                    None,
                    "both",
                    "help.param.heatpump.mode",
                    enum=("heating", "cooling", "both"),
                ),
                "unit_invest_cost": _p(
                    "unit_invest_cost",
                    "CNY/kW",
                    0,
                    None,
                    1800,
                    "help.param.heatpump.unit_invest_cost",
                    stock="addition",
                ),
                "lifetime_years": _p("lifetime_years", "a", 1, 50, 20, "help.param.heatpump.lifetime_years"),
            },
        )
    )
    _register_device(
        DeviceTypeSpec(
            type_id="ies.device.gas_boiler",
            version="1.2.0",
            name_zh="燃气锅炉",
            name_en="Gas Boiler",
            energy_carriers=[CARRIERS_GAS, CARRIERS_HEAT],
            is_load=False,
            capabilities=["thermal_generation", "controllable", "optimization_variable"],
            parameters={
                "rated_heat_kw": _p(
                    "rated_heat_kw",
                    "kW",
                    0,
                    1_000_000,
                    0,
                    "help.param.boiler.rated_heat_kw",
                    optimizable=True,
                    stock="addition",
                ),
                "max_heat_kw": _p(
                    "max_heat_kw", "kW", 0, 1_000_000, 1000, "help.param.boiler.max_heat_kw", stock="addition"
                ),
                "thermal_efficiency": _p(
                    "thermal_efficiency", "-", 0.5, 1.0, 0.90, "help.param.boiler.thermal_efficiency"
                ),
                "gas_price": _p("gas_price", "CNY/m³", 0, None, 3.2, "help.param.boiler.gas_price"),
                "lhv_kj_per_m3": _p(
                    "lhv_kj_per_m3", "kJ/m³", 10000, 60000, 35900, "help.param.boiler.lhv_kj_per_m3"
                ),
                "unit_invest_cost": _p(
                    "unit_invest_cost",
                    "CNY/kW",
                    0,
                    None,
                    600,
                    "help.param.boiler.unit_invest_cost",
                    stock="addition",
                ),
                "lifetime_years": _p("lifetime_years", "a", 1, 50, 15, "help.param.boiler.lifetime_years"),
            },
        )
    )
    _register_device(
        DeviceTypeSpec(
            type_id="ies.device.electric_chiller",
            version="1.2.0",
            name_zh="电制冷机",
            name_en="Electric Chiller",
            energy_carriers=[CARRIERS_ELECTRIC, CARRIERS_COOL],
            is_load=False,
            capabilities=["cooling_generation", "controllable", "optimization_variable"],
            parameters={
                "rated_cooling_kw": _p(
                    "rated_cooling_kw",
                    "kW",
                    0,
                    1_000_000,
                    0,
                    "help.param.chiller.rated_cooling_kw",
                    optimizable=True,
                    stock="addition",
                ),
                "max_cooling_kw": _p(
                    "max_cooling_kw",
                    "kW",
                    0,
                    1_000_000,
                    1000,
                    "help.param.chiller.max_cooling_kw",
                    stock="addition",
                ),
                "cop": _p("cop", "-", 1.5, 8.0, 4.0, "help.param.chiller.cop"),
                "cop_profile": _p(
                    "cop_profile", "reference", None, None, None, "help.param.chiller.cop_profile"
                ),
                "cooling_temp_c": _p("cooling_temp_c", "°C", 3, 20, 7, "help.param.chiller.cooling_temp_c"),
                "unit_invest_cost": _p(
                    "unit_invest_cost",
                    "CNY/kW",
                    0,
                    None,
                    1200,
                    "help.param.chiller.unit_invest_cost",
                    stock="addition",
                ),
                "lifetime_years": _p("lifetime_years", "a", 1, 50, 18, "help.param.chiller.lifetime_years"),
            },
        )
    )
    # 算法(04 §2.3;默认 ies.algo.milp_hybrid)
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


# 模块导入即完成受控加载(静态注册 + 版本校验)
load_registry()

#: 默认算法(02 §5.8 双层分解默认策略)
DEFAULT_ALGORITHM: str = "ies.algo.milp_hybrid"


def get_device_type(type_id: str) -> DeviceTypeSpec:
    """按注册 id 取设备类型(未注册抛 NotFoundError,码 CONN-TYPE-002)。"""
    spec = _DEVICE_TYPES.get(type_id)
    if spec is None:
        raise NotFoundError(
            f"设备类型未注册: {type_id}",
            code="CONN-TYPE-002",
            message_key="ies.diag.conn.type_unregistered",
            params={"device_id": "", "type_id": type_id},
        )
    return spec


def list_device_types() -> list[DeviceTypeSpec]:
    """列出全部已注册设备类型(按注册顺序,确定性的)。"""
    return list(_DEVICE_TYPES.values())


def get_algorithm(name: str) -> AlgorithmSpec:
    """按注册 id 取算法(未注册抛 NotFoundError)。

    特殊值:"default" 或 "ies.algo.default" 返回默认算法 ies.algo.milp_hybrid。
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
    """列出全部已注册算法。"""
    return list(_ALGORITHMS.values())


def snapshot() -> list[str]:
    """注册表快照(04 §2.2 规则 5):["ies.device.pv@1.3.0", ...],供计算快照引用。"""
    return [f"{s.type_id}@{s.version}" for s in _DEVICE_TYPES.values()] + [
        f"{s.algo_id}@{s.version}" for s in _ALGORITHMS.values()
    ]
