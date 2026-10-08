"""装配共享上下文层:端口/模型解析、单位量纲、电网识别、母线汇总类型、检查上下文。

校验器图与系统阶段经 ``iesplan.assembly.rules`` 消费本模块公开接口;
端口推导结果缓存到 ``CheckContext.resolved_ports``。

依赖方向:context → schema/diags + core + devices 公开门面
(``iesplan.devices`` 的 ``__init__`` 门面);不依赖 services/engines/worker。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from iesplan.assembly.schema import (
    CARRIER_DEFAULT_QUANTITY_UNIT,
    QUANTITY_DIMS,
    QUANTITY_SIGNAL,
    AssemblyDevice,
    AssemblyPort,
    AssemblySpec,
)
from iesplan.core import units
from iesplan.core.contracts import is_leap_year
from iesplan.core.errors import AppError
from iesplan.core.expression import Dimensions
from iesplan.devices import DeviceModelDocument as DeviceTypeSpec

# ---------------------------------------------------------------------------
# 常量与业务表(与 application/models 同约定;本模块独立声明,不依赖 services)
# ---------------------------------------------------------------------------

#: 设备参数单位 → W 的换算键(母线固定供给/需求上限估算用)
PEAK_PARAM_BY_LOAD: dict[str, str] = {
    "ies.device.electric_load": "peak_power_kw",
    "ies.device.heat_load": "peak_heat_kw",
    "ies.device.cooling_load": "peak_cooling_kw",
}

#: 电网进出口端口名(与 rules.solvability 的电网识别集合一致)
GRID_SIDE_PORTS = ("electricity_import", "electricity_export")

#: 外生供给载体: 目录内无源设备, in 端口无来边不报输入不完备(与 solar
#: 环境侧同理; 燃气由外部管网购入, 引擎按 gas_price 计价, 不依赖来边能量流)
EXOGENOUS_SUPPLY_CARRIERS = frozenset({"solar", "gas"})


# ---------------------------------------------------------------------------
# 上下文与母线汇总类型
# ---------------------------------------------------------------------------


#: 分辨率 → 每日步数(全项目统一计算分辨率, 均能确定性切分一天)
_STEPS_PER_DAY: dict[str, int] = {"15min": 96, "30min": 48, "1h": 24}


def steps_for_year(resolution: str, year: int) -> int:
    """分辨率 + 公历年 → 该年步数(分辨率 × 365/366 天; 未知分辨率抛错, 不猜测)。"""
    per_day = _STEPS_PER_DAY.get(resolution)
    if per_day is None:
        raise AppError(
            f"未知计算分辨率: {resolution!r}",
            code="SYS-CFG-001",
            message_key="ies.diag.store.config_invalid",
            params={"resolution": resolution},
        )
    return per_day * (366 if is_leap_year(year) else 365)


@dataclass(slots=True)
class CheckContext:
    """检查上下文:注册表快照/基线步数/数据集元信息。

    年步数与基线分辨率由调用方(校验器)从项目计算基线显式推导后传入;
    上下文不保存时间轴、不做默认推导。
    """

    registry: dict[str, DeviceTypeSpec] | None = None  # type_id → 设备模型命令规格
    baseline_resolution: str | None = None  # 项目基线 resolution(15min/30min/1h)
    period_steps: int | None = None  # 基线年步数(分辨率 × 365/366)
    seed: int | None = None
    max_diags: int = 200  # 单次检查诊断上限(防风暴)
    resolved_ports: dict[str, AssemblyPort] | None = None  # 内部缓存:ref → 端口(注册表推导)

    def steps_per_year(self, spec: AssemblySpec) -> int:
        """年步数: 必须由调用方显式传入。"""
        if self.period_steps is None or self.period_steps <= 0:
            raise AppError(
                "检查上下文缺少年步数(调用方必须从项目基线显式推导传入)",
                code="SYS-CFG-001",
                message_key="ies.diag.store.config_invalid",
                params={},
            )
        return self.period_steps


@dataclass(slots=True)
class BusSummary:
    """母线汇总(阶段 D 产物,随 CheckResult 返回供 UI/审计)。

    母线 = 载体 × 无向连通分量(边连通;双向端口视为双向连通)。
    """

    carrier: str
    port_refs: list[str] = field(default_factory=list)
    device_ids: list[str] = field(default_factory=list)
    source_port_refs: list[str] = field(default_factory=list)
    sink_port_refs: list[str] = field(default_factory=list)
    has_storage: bool = False
    has_grid: bool = False
    fixed_supply_max_w: float | None = None  # Σ固定源上限(W)
    demand_max_w: float | None = None  # Σ需求上限(W)
    n_controllable: int = 0  # 可控变量数(自由度提示)
    n_balance_eq: int = 0  # 平衡方程数(步数×载体数)


# ---------------------------------------------------------------------------
# 模型解析
# ---------------------------------------------------------------------------


def resolve_model(ctx: CheckContext, model: str) -> DeviceTypeSpec | None:
    """解析模型引用:按裸 device id 返回类型规格(2.0 已拒绝 @ 钉选，无版本拆分)。

    未注册返回 None: 装配禁止用合成/兜底规格伪装可装配视图,
    未注册类型必须被装配显式阻断。transport_pipe 为普通 2.0 设备，
    不再单独判别。
    """
    registry = ctx.registry
    if registry is None:
        registry = default_registry()
        ctx.registry = registry
    return registry.get(model)


def grid_side_used(type_spec, device_id: str, edges) -> bool:
    """电网设备任一进出口侧是否已连接(单侧运行判定)。

    电网进出口为替代运行模式:单侧使用合法,未用侧由进出口互斥方程钳零;
    INPUT-001 不得因此阻断。
    """
    if (
        type_spec is None
        or type_spec.device is None
        or type_spec.device.id != "ies.device.grid_connection"
    ):
        return False
    side_refs = {f"{device_id}.{name}" for name in GRID_SIDE_PORTS}
    return any(e.from_port in side_refs or e.to_port in side_refs for e in edges)


def default_registry() -> dict[str, DeviceTypeSpec]:
    """装配检查的模块内注册表快照(RR-P2-02/05: 消费 devices 公开 descriptor)。

    从 ``iesplan.devices.list_devices()`` 公开门面构建本模块自己的
    只读候选字典; 注册表未初始化(未调用 init_registry)或为空都必须使装配
    不可用并暴露诊断(宪法 5.3/9.5: 禁止静态回退和宽泛异常兜底)。
    """
    from iesplan.devices import list_devices

    descriptors = list_devices()
    merged: dict[str, DeviceTypeSpec] = {}
    for desc in descriptors:
        # 直接采用公开 descriptor(不可变映射/元组已冻结), 不复制重建:
        # 装配只读消费, 共享对象不再可变, 也不会跨模块别名引用下划线符号。
        if desc.device is not None:
            merged[desc.device.id] = desc
    if not merged:
        raise AppError(
            "装配检查: YAML 设备注册表为空(未初始化或目录无设备), 装配不可用",
            code="SYS-CFG-001",
            message_key="ies.diag.store.config_invalid",
            params={"service": "assembly"},
        )
    return merged


# ---------------------------------------------------------------------------
# 端口解析(注册表推导 + 显式覆盖)
# ---------------------------------------------------------------------------


def yaml_device_ports(device: AssemblyDevice, type_id: str) -> list[AssemblyPort]:
    """从 YAML 设备注册表取端口定义 → AssemblyPort 列表(权威来源)。

    yaml 端口(DeviceYamlSpec.ports: 端口名/载体/方向/容量引用)为唯一权威,
    装配检查据此做连接合法性与可解性检查。
    注册表未初始化或端口数据错误一律向上阻断(RR-P2-05: 无静态回退)。
    """
    from iesplan.devices import get_device

    spec = get_device(type_id)
    ports: list[AssemblyPort] = []
    for name, interface in spec.interfaces.items():
        if interface.carrier not in CARRIER_DEFAULT_QUANTITY_UNIT:
            continue  # solar 等环境侧载体(不可连接)不参与装配端口/母线平衡
        unit = interface.unit
        qty = CARRIER_DEFAULT_QUANTITY_UNIT.get(interface.carrier, (QUANTITY_SIGNAL, "-"))[0]
        ports.append(
            AssemblyPort(
                device=device.id,
                name=name,
                carrier=interface.carrier,
                direction=interface.type,
                quantity=qty,
                unit=unit,
            )
        )
    return ports


def derive_device_ports(spec: AssemblySpec, ctx: CheckContext, device: AssemblyDevice) -> list[AssemblyPort]:
    """设备端口推导(RR-P2-05: devices 2.0 正式注册端口为唯一权威,无回退)。

    已注册设备取注册表端口声明;未注册设备(含未走 2.0 正式注册的
    自定义类型)端口无从推导,返回空列表(未注册由校验器阻断),
    不再按显式端口声明回退。端口载体/方向/量纲一律以注册表为准，
    2.0 无显式覆盖入口。
    """
    type_spec = resolve_model(ctx, device.definition_id)
    if type_spec is None:
        return []  # 模型未注册,端口无从推导(REF-002 已报)
    assert type_spec.device is not None
    return yaml_device_ports(device, type_spec.device.id)


def resolve_ports(spec: AssemblySpec, ctx: CheckContext) -> dict[str, AssemblyPort]:
    """全量端口解析(ref → AssemblyPort),结果缓存到 ctx.resolved_ports。

    端口唯一来自注册表推导(transport_pipe 为普通 2.0 设备,同列 devices)。
    """
    resolved: dict[str, AssemblyPort] = {}
    for device in spec.devices:
        for port in derive_device_ports(spec, ctx, device):
            resolved[port.ref] = port
    ctx.resolved_ports = resolved
    return resolved


def ensure_ports(spec: AssemblySpec, ctx: CheckContext) -> dict[str, AssemblyPort]:
    """惰性端口解析(各阶段规则入口保证 resolved_ports 已就绪)。"""
    if ctx.resolved_ports is None:
        return resolve_ports(spec, ctx)
    return ctx.resolved_ports


# ---------------------------------------------------------------------------
# 单位量纲辅助
# ---------------------------------------------------------------------------


def unit_category(unit: str) -> str | None:
    """单位类别(core/units 注册表类别;未注册返回 None)。"""
    from iesplan.core.units import ALIAS_MAP, UNITS

    uid = ALIAS_MAP.get(unit.lower())
    if uid is not None and uid in UNITS:
        return UNITS[uid].category
    return None


def units_compatible(u1: str | None, u2: str | None) -> bool:
    """两端单位量纲是否可换算(core/units.py convert 判定;无量纲 "-"/"" 与自身相容)。

    能量↔功率视为相容(数据列按步能量 kWh 声明、端口按功率 W 的领域约定,
    引擎按步长换算 kWh×1000/步长小时 → W)。
    """
    a, b = (u1 or "").strip(), (u2 or "").strip()
    if a == b:
        return True
    dimensionless = ("", "-", "1")
    if a in dimensionless and b in dimensionless:
        return True
    if a in dimensionless or b in dimensionless:
        return False
    try:
        units.convert(1.0, a, b)
        return True
    except Exception:
        pass
    # 能量↔功率: 数据列按步能量声明, 端口按功率; 业务单位到求解器内部单位的换算
    # 只发生在 GeneratorProvider 边界(宪法 §4.5/§7.4)。
    cats = {unit_category(a), unit_category(b)}
    if cats == {"energy", "power"}:
        return True
    return False


UNIT_CATEGORY_DIMS: dict[str, dict[str, int]] = {
    "power": {"power": 1},
    "energy": {"energy": 1},
    "temperature": {"temperature": 1},
    "currency": {"currency": 1},
    "duration": {"time": 1},
    "angle": {"angle": 1},
}


def unit_dims(unit: str | None, quantity: str | None = None) -> Dimensions:
    """单位 → 表达式量纲(先查 core/units 注册表类别,未注册按物理量回退)。"""
    from collections import Counter

    u = (unit or "").strip()
    if u in ("", "-", "1"):
        return Counter()
    from iesplan.core.units import ALIAS_MAP, UNITS

    uid = ALIAS_MAP.get(u.lower())
    if uid is not None and uid in UNITS:
        cat = UNITS[uid].category
        return Counter(UNIT_CATEGORY_DIMS.get(cat, {}))
    # 未注册单位(如 m3/s、CNY/J):按端口物理量回退
    return Counter(QUANTITY_DIMS.get(quantity or "", {}))


def to_watts(value: float | None, unit: str | None) -> float | None:
    """业务单位数值 → W(仅当单位可换算到 W;否则 None)。"""
    if value is None:
        return None
    try:
        return units.convert(float(value), unit or "W", "W")
    except Exception:
        return None


__all__ = [
    "steps_for_year",
    "PEAK_PARAM_BY_LOAD",
    "GRID_SIDE_PORTS",
    "EXOGENOUS_SUPPLY_CARRIERS",
    "UNIT_CATEGORY_DIMS",
    "CheckContext",
    "BusSummary",
    "AssemblySpec",
    "AssemblyDevice",
    "AssemblyPort",
    "DeviceTypeSpec",
    "resolve_model",
    "grid_side_used",
    "default_registry",
    "yaml_device_ports",
    "derive_device_ports",
    "resolve_ports",
    "ensure_ports",
    "unit_category",
    "units_compatible",
    "unit_dims",
    "to_watts",
]
