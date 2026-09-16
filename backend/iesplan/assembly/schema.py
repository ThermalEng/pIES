"""装配数据模型(ies.assembly 2.0.0 内存形态)。

所有 dataclass 均为 slots + frozen 语义;列表字段为普通 list,解析后不再可变。
id 引用一律用点路径字符串 "<device>.<port>";端口一律经 CheckContext
解析(注册表推导)，内存模型不再保存显式端口覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 设备存量/新增
DEVICE_KIND_EXISTING = "existing"
DEVICE_KIND_NEW = "new"

#: 端口物理量枚举
QUANTITY_POWER = "power"  # W
QUANTITY_ENERGY = "energy"  # J
QUANTITY_FLOW = "flow"  # m3/s(燃气/水)
QUANTITY_TEMPERATURE = "temperature"  # K
QUANTITY_SOC = "soc"  # 0..1
QUANTITY_RATIO = "ratio"  # 无量纲
QUANTITY_PRICE = "price"  # CNY/J
QUANTITY_SIGNAL = "signal"  # 控制信号(无量纲)

#: 载体 → (默认物理量, 标准单位)(solar 为环境侧, 无连接端口)
CARRIER_DEFAULT_QUANTITY_UNIT: dict[str, tuple[str, str]] = {
    "electricity": (QUANTITY_POWER, "kW"),
    "heat": (QUANTITY_POWER, "kW"),
    "cool": (QUANTITY_POWER, "kW"),
    "gas": (QUANTITY_FLOW, "m3/s"),
    "water": (QUANTITY_FLOW, "m3/s"),
    "data": (QUANTITY_SIGNAL, "-"),
}

#: 标准单位 → 表达式引擎量纲标签(core/expression.py DIM_* 同构)
#: 公开名称(供 assembly 上下文/规则层消费)。
QUANTITY_DIMS: dict[str, dict[str, int]] = {
    QUANTITY_POWER: {"power": 1},
    QUANTITY_ENERGY: {"energy": 1},
    QUANTITY_FLOW: {"flow": 1},
    QUANTITY_TEMPERATURE: {"temperature": 1},
    QUANTITY_SOC: {},
    QUANTITY_RATIO: {},
    QUANTITY_PRICE: {"currency": 1, "energy": -1},
    QUANTITY_SIGNAL: {},
}


@dataclass(slots=True, frozen=True)
class AssemblyPort:
    """端口(端；载体/方向/量纲一律由注册表推导，2.0 无显式覆盖入口)。"""

    device: str  # 所属设备 id
    name: str  # 端口名(设备内唯一)
    carrier: str  # electric|heat|cool|gas|solar|water|data
    direction: str  # in|out|bidirectional
    quantity: str  # QUANTITY_*
    unit: str  # 标准单位(W|J|K|m3/s|...)

    @property
    def ref(self) -> str:
        """点路径引用 "<device>.<name>"。"""
        return f"{self.device}.{self.name}"


@dataclass(slots=True, frozen=True)
class AssemblyDevice:
    """设备实例(节点，ies.assembly 2.0.0 纯技术契约形态)。

    definition_id 为裸稳定设备 ID(无 @版本钉选)；kind 即 asset_origin；
    properties 为 {prop: {value, unit}} 非时变技术常量覆盖；
    predefined 为 {iface: {mode, ...}} 预定义来源声明(路径/值/目标类型)，
    data_repeat/data_predict 的文件内容由校验器另行解析确认。
    """

    id: str
    definition_id: str  # 稳定设备模型 ID(裸 ID，如 ies.device.heat_pump)
    kind: str = DEVICE_KIND_EXISTING  # existing | new(装配 asset_origin)
    properties: dict[str, dict[str, object]] = field(default_factory=dict)
    predefined: dict[str, dict[str, object]] = field(default_factory=dict)


def prop_map(device: AssemblyDevice) -> dict[str, object]:
    """属性标量映射({name: value}，供规则层数值读取)。"""
    out: dict[str, object] = {}
    for name, entry in device.properties.items():
        if isinstance(entry, dict) and "value" in entry:
            out[name] = entry["value"]
    return out


@dataclass(slots=True, frozen=True)
class AssemblyEdge:
    """边:from 端输出 → to 端输入,两端参数同一时间步数值严格相等。

    输出对输出(母线汇合写法,04 §2.3.4)亦合法:同一母线上各源输出端口即母线汇合点。
    """

    id: str
    from_port: str  # "<device>.<port>"
    to_port: str  # "<device>.<port>"




@dataclass(slots=True)
class AssemblySpec:
    """装配文件解析结果(内存模型; 2.0.0 无 calculation/time_axis)。

    步数由项目计算基线(resolution/leap_year)唯一推导, 经 CheckContext
    显式传入, 不在本模型内保存时间轴。transport_pipe 为普通 2.0 设备,
    与其他设备同列 devices，不再单独建节。
    """

    name: str = ""
    devices: list[AssemblyDevice] = field(default_factory=list)
    edges: list[AssemblyEdge] = field(default_factory=list)
