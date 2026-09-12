"""装配共享上下文层(parser → context → rules → validator → artifact 中的 context)。

W1-Assembly 收敛:rules/checker 真正需要的共享能力(端口/模型解析、
单位量纲、电网识别、母线汇总类型、检查上下文)在此集中定义并以公开
命名导出,逻辑与搬迁前 checker.py 完全一致,不重写业务。

依赖方向:context → parser/schema/diags + core + devices 公开门面
(``iesplan.devices`` 的 ``__init__`` 门面);rules 只依赖 context,
checker/validator 作为编排层消费 context/rules,不反转。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from iesplan.assembly.parser import PORT_DECL_OVERRIDE_FIELDS
from iesplan.assembly.schema import (
    CARRIER_DEFAULT_QUANTITY_UNIT,
    NATURE_DELAYED,
    NATURE_INSTANT,
    QUANTITY_DIMS,
    QUANTITY_SIGNAL,
    AssemblyDevice,
    AssemblyPipeline,
    AssemblyPort,
    AssemblySpec,
)
from iesplan.core import units
from iesplan.core.errors import AppError, NotFoundError
from iesplan.core.expression import Dimensions
from iesplan.devices import DeviceModelDocument as DeviceTypeSpec

# ---------------------------------------------------------------------------
# 常量与业务表(与 application/models 同约定;本模块独立声明,不依赖 services)
# ---------------------------------------------------------------------------

#: 管道设备模型(RR-P2-05: 管道为合法业务设备, 在 iesplan.devices YAML 目录
#: 与其他设备一并注册, 装配模块直接消费 descriptor, 不再维护白名单/内置兜底)。
PIPELINE_MODEL_IDS: tuple[str, ...] = ("ies.device.transport_pipe",)

#: 载体 → 端口名后缀规则(in/out 为 "{载体}_{方向}",双向为 "{载体}";与 services 一致)
PORT_TYPE_TO_CARRIER: dict[str, str] = {
    "electric": "electricity",
    "thermal": "heat",
    "cooling": "cool",
    "fuel": "gas",
    "data": "data",
}

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


@dataclass(slots=True)
class CheckContext:
    """检查上下文:注册表快照/时间轴/数据集元信息(缺省时内部按需惰性加载)。"""

    registry: dict[str, DeviceTypeSpec] | None = None  # type_id → 设备模型命令规格
    time_axis: object | None = None  # core/timeaxis.TimeAxis(取 .n)或 {"n": int};None 按分辨率推导
    datasets: dict[int, dict] | None = None  # dataset_version_id → 元信息
    seed: int | None = None
    max_diags: int = 200  # 单次检查诊断上限(防风暴)
    resolved_ports: dict[str, AssemblyPort] | None = None  # 内部缓存:ref → 端口(注册表推导+显式覆盖)

    def steps_per_year(self, spec: AssemblySpec) -> int:
        """年步数(用于 PIPE-002 延迟范围判定):时间轴 → spec → 默认 8760。"""
        axis = self.time_axis
        if axis is not None:
            n = getattr(axis, "n", None)
            if isinstance(n, int) and n > 0:
                return n
            if isinstance(axis, dict):
                n = axis.get("n")
                if isinstance(n, int) and n > 0:
                    return n
        if spec.time_axis is not None:
            return spec.time_axis.steps_per_year
        return 8760


@dataclass(slots=True)
class BusSummary:
    """母线汇总(阶段 D 产物,随 CheckResult 返回供 UI/审计)。

    母线 = 载体 × 无向连通分量(边连通;双向端口视为双向连通,管道设备的两条边同属一个分量)。
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


def split_model(model: str) -> tuple[str, str | None]:
    """模型引用 → (type_id, version|None)("ies.device.pv@2.0.0" → ("ies.device.pv", "2.0.0"))。"""
    if "@" in model:
        type_id, _, version = model.rpartition("@")
        return type_id, version or None
    return model, None


def resolve_model(ctx: CheckContext, model: str) -> tuple[DeviceTypeSpec | None, bool]:
    """解析模型引用:返回 (类型规格 | None, 是否管道模型)。

    未注册返回 (None, False): 装配禁止用合成/兜底规格伪装可装配视图,
    未注册类型必须被装配显式阻断(RR-P2-05)。
    """
    type_id, _ = split_model(model)
    registry = ctx.registry
    if registry is None:
        registry = default_registry()
        ctx.registry = registry
    spec = registry.get(type_id)
    if spec is None:
        return None, False
    return spec, type_id in PIPELINE_MODEL_IDS


def grid_side_used(type_spec, device_id: str, edges) -> bool:
    """电网设备任一进出口侧是否已连接(单侧运行判定)。

    电网进出口为替代运行模式:单侧使用合法,未用侧由进出口互斥方程钳零,
    与 1.0 单端口电网同语义;INPUT-001 不得因此阻断。
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
    装配检查据此做连接合法性(REF-004/REF-005)与可解性检查。
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
                nature=NATURE_INSTANT,
                capacity=None,
            )
        )
    return ports


def derive_device_ports(spec: AssemblySpec, ctx: CheckContext, device: AssemblyDevice, *, port_source=None) -> list[AssemblyPort]:
    """设备端口推导(RR-P2-05: YAML 公开 descriptor 端口为唯一权威, 无静态回退)。

    已注册设备取 YAML 端口声明; 未注册设备(测试注入的自定义类型)按
    装配文本显式 ``ports:`` 声明转换。显式声明的 capacity 在两条路径
    之后统一合并覆盖。

    ``port_source`` 为兼容注入口:缺省使用本模块 ``yaml_device_ports``;
    checker 兼容包装经此传入其命名空间钩子(存量测试 monkeypatch 点)。
    """
    type_spec, _ = resolve_model(ctx, device.model)
    if type_spec is None:
        return []  # 模型未注册,端口无从推导(REF-002 已报)
    source = port_source if port_source is not None else yaml_device_ports
    try:
        assert type_spec.device is not None
        derived = source(device, type_spec.device.id)
    except NotFoundError:
        # 测试注入/外部自定义类型(不在 YAML 目录): 按显式声明转换
        derived = list(device.ports)
    # 显式声明覆盖(仅 capacity;载体/方向以注册表推导为准,不一致由阶段 C 报 REF-005)
    explicit_by_name = {ep.name: ep for ep in device.ports}
    merged: list[AssemblyPort] = []
    for port in derived:
        explicit = explicit_by_name.get(port.name)
        if explicit is not None and explicit.capacity is not None:
            port = AssemblyPort(
                device=port.device,
                name=port.name,
                carrier=port.carrier,
                direction=port.direction,
                quantity=port.quantity,
                unit=port.unit,
                nature=port.nature,
                delay_steps=port.delay_steps,
                capacity=explicit.capacity,
            )
        merged.append(port)
    return merged


def derive_pipeline_ports(pipe: AssemblyPipeline) -> list[AssemblyPort]:
    """管道端口推导(从 YAML descriptor 读取真实端口, 不再内置常量)。

    入端 instantaneous / 出端 delayed(延迟取 params.delay_steps, 缺省 1)。
    """
    from iesplan.devices import get_device

    spec = get_device(pipe.model.split("@", 1)[0])
    in_port = next(((name, p) for name, p in spec.interfaces.items() if p.type == "in"), None)
    out_port = next(((name, p) for name, p in spec.interfaces.items() if p.type == "out"), None)
    if in_port is None or out_port is None:
        # 未声明输入/输出端口 → 装配阻断, 不再兜底合成(RR-P2-05)。
        raise AppError(
            f"管道模型 {pipe.model} 必须声明 in/out 端口, 装配不可用",
            code="SYS-CFG-001",
            message_key="ies.diag.store.config_invalid",
            params={"model": pipe.model},
        )
    delay = int(pipe.params.get("delay_steps", 1) or 1)
    in_name, in_spec = in_port
    out_name, out_spec = out_port
    in_carrier = in_spec.carrier
    in_qty, in_unit = CARRIER_DEFAULT_QUANTITY_UNIT[in_carrier]
    out_carrier = out_spec.carrier
    out_qty, out_unit = CARRIER_DEFAULT_QUANTITY_UNIT[out_carrier]
    return [
        AssemblyPort(
            device=pipe.id,
            name=in_name,
            carrier=in_carrier,
            direction="in",
            quantity=in_qty,
            unit=in_unit,
            nature=NATURE_INSTANT,
        ),
        AssemblyPort(
            device=pipe.id,
            name=out_name,
            carrier=out_carrier,
            direction="out",
            quantity=out_qty,
            unit=out_unit,
            nature=NATURE_DELAYED,
            delay_steps=delay,
        ),
    ]


def resolve_ports(spec: AssemblySpec, ctx: CheckContext) -> dict[str, AssemblyPort]:
    """全量端口解析(ref → AssemblyPort),结果缓存到 ctx.resolved_ports。

    显式声明与注册表推导不一致的字段交由阶段 C 的 ASM-REF-005 处理,此处按"注册表为准"合并。
    """
    resolved: dict[str, AssemblyPort] = {}
    for device in spec.devices:
        for port in derive_device_ports(spec, ctx, device):
            resolved[port.ref] = port
    for pipe in spec.pipelines:
        for port in derive_pipeline_ports(pipe):
            resolved[port.ref] = port
    # 管道端口的显式覆盖声明(容量等)
    explicit_by_ref: dict[str, AssemblyPort] = {}
    for ep in spec.explicit_pipeline_ports:
        existing = explicit_by_ref.get(ep.ref)
        if existing is None or ep.capacity is not None:
            explicit_by_ref[ep.ref] = ep
    for ref, ep in explicit_by_ref.items():
        base = resolved.get(ref)
        if base is None:
            resolved[ref] = ep
            continue
        merged = AssemblyPort(
            device=base.device,
            name=base.name,
            carrier=base.carrier,
            direction=base.direction,
            quantity=base.quantity,
            unit=base.unit,
            nature=base.nature,
            delay_steps=base.delay_steps,
            capacity=ep.capacity if ep.capacity is not None else base.capacity,
        )
        resolved[ref] = merged
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
    # 能量↔功率: 数据列按步能量声明, 端口按功率; 引擎按步长换算(见 _merge_rows)
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
    "PORT_DECL_OVERRIDE_FIELDS",
    "PORT_TYPE_TO_CARRIER",
    "PIPELINE_MODEL_IDS",
    "PEAK_PARAM_BY_LOAD",
    "GRID_SIDE_PORTS",
    "EXOGENOUS_SUPPLY_CARRIERS",
    "UNIT_CATEGORY_DIMS",
    "CheckContext",
    "BusSummary",
    "AssemblySpec",
    "AssemblyDevice",
    "AssemblyPipeline",
    "AssemblyPort",
    "DeviceTypeSpec",
    "split_model",
    "resolve_model",
    "grid_side_used",
    "default_registry",
    "yaml_device_ports",
    "derive_device_ports",
    "derive_pipeline_ports",
    "resolve_ports",
    "ensure_ports",
    "unit_category",
    "units_compatible",
    "unit_dims",
    "to_watts",
]
