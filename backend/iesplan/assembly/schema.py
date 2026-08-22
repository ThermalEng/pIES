"""装配数据模型(与装配文本一一对应,文本为唯一事实源)。

设计约束见开发者指南 architecture.md 与 contracts.md；建模方法取值为
mechanism/data_repeat/data_predict。

所有 dataclass 均为 slots + frozen 语义;列表字段为普通 list,解析后不再可变。
id 引用一律用点路径字符串 "<device>.<port>",在文件内解析为对象时用
AssemblySpec.resolve_port(ref) 返回 (AssemblyDevice, AssemblyPort) 或 None。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 装配文本格式版本(与文本 format_version 一致,05 §7.2 裁决:YAML 1.2、"1.0")
FORMAT_VERSION = "1.0"

#: 建模方法标志(05 §7.1 裁决统一命名:model_method)
MODEL_METHOD_MECHANISM = "mechanism"  # 机理方法
MODEL_METHOD_DATA_REPEAT = "data_repeat"  # 数据方法-简单周期重复
MODEL_METHOD_DATA_PREDICT = "data_predict"  # 数据方法-历史数据预测模型
MODEL_METHODS: tuple[str, ...] = (
    MODEL_METHOD_MECHANISM,
    MODEL_METHOD_DATA_REPEAT,
    MODEL_METHOD_DATA_PREDICT,
)

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
QUANTITIES: tuple[str, ...] = (
    QUANTITY_POWER,
    QUANTITY_ENERGY,
    QUANTITY_FLOW,
    QUANTITY_TEMPERATURE,
    QUANTITY_SOC,
    QUANTITY_RATIO,
    QUANTITY_PRICE,
    QUANTITY_SIGNAL,
)

#: 端口时间性质
NATURE_INSTANT = "instantaneous"  # 同时间严格相等
NATURE_DELAYED = "delayed"  # 输出滞后 delay_steps(管道设备输出端)
NATURES: tuple[str, ...] = (NATURE_INSTANT, NATURE_DELAYED)

#: 载体枚举(与 models/model.py ports.port_type CHECK 一致 + solar/water/data)
CARRIERS: tuple[str, ...] = ("electric", "heat", "cool", "gas", "solar", "water", "data")

#: 端口方向
DIRECTIONS: tuple[str, ...] = ("in", "out", "bidirectional")

#: 时间轴分辨率(与 core/timeaxis.py RESOLUTIONS 对齐)
RESOLUTIONS: tuple[str, ...] = ("15min", "30min", "1h")

#: 载体 → (默认物理量, 标准单位)(solar 为环境侧, 无连接端口)
CARRIER_DEFAULT_QUANTITY_UNIT: dict[str, tuple[str, str]] = {
    "electric": (QUANTITY_POWER, "W"),
    "heat": (QUANTITY_POWER, "W"),
    "cool": (QUANTITY_POWER, "W"),
    "gas": (QUANTITY_FLOW, "m3/s"),
    "water": (QUANTITY_FLOW, "m3/s"),
    "data": (QUANTITY_SIGNAL, "-"),
}

#: 标准单位 → 表达式引擎量纲标签(core/expression.py DIM_* 同构)
_QUANTITY_DIMS: dict[str, dict[str, int]] = {
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
class TimeAxisRef:
    """时间轴引用(与 core/timeaxis.py 对齐)。"""

    resolution: str  # 15min | 30min | 1h
    start: str = "2025-01-01T00:00:00Z"  # ISO8601 UTC
    timezone_offset_min: int = 0

    @property
    def steps_per_year(self) -> int:
        """年步数:35040 / 17520 / 8760(与 core/timeaxis.py RESOLUTIONS 一致)。"""
        return {"15min": 35040, "30min": 17520, "1h": 8760}.get(self.resolution, 8760)


@dataclass(slots=True, frozen=True)
class AssemblyPort:
    """端口(端)。"""

    device: str  # 所属设备 id
    name: str  # 端口名(设备内唯一)
    carrier: str  # electric|heat|cool|gas|solar|water|data
    direction: str  # in|out|bidirectional
    quantity: str  # QUANTITY_*
    unit: str  # 标准单位(W|J|K|m3/s|...)
    nature: str = NATURE_INSTANT  # instantaneous|delayed
    delay_steps: int = 0  # nature=delayed 时有效(由管道设备 params.delay_steps 推导)
    capacity: float | None = None  # 端口容量(标准单位)

    @property
    def ref(self) -> str:
        """点路径引用 "<device>.<name>"。"""
        return f"{self.device}.{self.name}"


@dataclass(slots=True, frozen=True)
class DataRef:
    """数据集引用(设备承载时间序列数据的标准文件引用)。"""

    key: str  # 参数名(load_profile/heat_profile/...)
    dataset_version_id: int
    dataset_name: str = ""
    columns: list[str] = field(default_factory=list)
    unit: str = ""  # 文件头声明单位(非标准单位,检查器只做量纲一致性)
    resolution: str = ""  # 15min|30min|1h


@dataclass(slots=True, frozen=True)
class AssemblyDevice:
    """设备实例(节点)。"""

    id: str
    model: str  # "ies.device.heat_pump@1.2.0"
    kind: str = DEVICE_KIND_EXISTING  # existing | new
    model_method: str = MODEL_METHOD_MECHANISM  # 建模方法标志(05 §7.1)
    stateful: bool = False  # 有/无状态模型标志
    params: dict[str, object] = field(default_factory=dict)
    data_refs: list[DataRef] = field(default_factory=list)
    ports: list[AssemblyPort] = field(default_factory=list)  # 从模型注册表推导,可覆盖 capacity
    meta: dict[str, object] = field(default_factory=dict)  # 布局等,不参与语义/哈希


@dataclass(slots=True, frozen=True)
class AssemblyEdge:
    """边:from 端输出 → to 端输入,两端参数同一时间步数值严格相等。

    输出对输出(母线汇合写法,04 §2.3.4)亦合法:同一母线上各源输出端口即母线汇合点。
    """

    id: str
    from_port: str  # "<device>.<port>"
    to_port: str  # "<device>.<port>"
    capacity: float | None = None  # 边容量(标准单位),None=不限制
    meta: dict[str, object] = field(default_factory=dict)

    @property
    def ends(self) -> tuple[str, str]:
        """两端点路径。"""
        return (self.from_port, self.to_port)

    def resolve(self, spec: "AssemblySpec") -> tuple[AssemblyPort | None, AssemblyPort | None]:
        """解析两端端口(未定义返回 None)。"""
        return spec.port_by_ref(self.from_port), spec.port_by_ref(self.to_port)


@dataclass(slots=True, frozen=True)
class AssemblyPipeline:
    """管道设备(有状态传输,体现非同时性;在 devices 之外单独列出,便于检查器特判)。

    输出端口 nature=delayed + delay_steps:t 时刻输出 = 输入端口 t−delay_steps 时刻取值。
    """

    id: str
    model: str = "ies.device.transport_pipe@1.0.0"
    params: dict[str, object] = field(default_factory=dict)  # delay_steps/loss_per_step


@dataclass(slots=True, frozen=True)
class AssemblyConstraint:
    """组合级约束(表达式引擎语法,core/expression.py)。"""

    id: str
    type: str  # ratio|capacity|schedule|generic
    expr: str  # "hp1.electric_in <= 0.8 * grid.electric_out"
    enabled: bool = True


@dataclass(slots=True, frozen=True)
class CalcRequirements:
    """计算要求(第 5 步计算模块输入;builder 从 calc_config/快照填充)。"""

    algorithm: str = "ies.algo.milp_hybrid@1.0.0"
    tolerances: dict[str, float] = field(
        default_factory=lambda: {"mip_rel_gap": 0.001, "time_limit_s": 600.0}
    )
    seed: int | None = None


@dataclass(slots=True)
class AssemblySpec:
    """装配文件解析结果(内存模型)。"""

    name: str = ""
    format_version: str = FORMAT_VERSION
    source_graph_id: int | None = None
    time_axis: TimeAxisRef | None = None
    devices: list[AssemblyDevice] = field(default_factory=list)
    edges: list[AssemblyEdge] = field(default_factory=list)
    pipelines: list[AssemblyPipeline] = field(default_factory=list)
    constraints: list[AssemblyConstraint] = field(default_factory=list)
    requirements: CalcRequirements | None = None
    explicit_pipeline_ports: list[AssemblyPort] = field(default_factory=list)  # 管道端口的显式覆盖声明

    def device_by_id(self, device_id: str) -> AssemblyDevice | None:
        """按设备 id 查设备实例。"""
        for d in self.devices:
            if d.id == device_id:
                return d
        return None

    def pipeline_by_id(self, pipeline_id: str) -> AssemblyPipeline | None:
        """按管道 id 查管道设备。"""
        for p in self.pipelines:
            if p.id == pipeline_id:
                return p
        return None

    def port_by_ref(self, ref: str) -> AssemblyPort | None:
        """按 "<dev>.<port>" 查端口(含管道设备推导端口)。"""
        device_id, _, name = ref.partition(".")
        if not name:
            return None
        device = self.device_by_id(device_id)
        if device is not None:
            for p in device.ports:
                if p.name == name:
                    return p
        pipe = self.pipeline_by_id(device_id)
        if pipe is not None:
            for p in self.all_ports():
                if p.device == device_id and p.name == name:
                    return p
        return None

    def device_ids(self) -> set[str]:
        """全部设备实例 id(含管道设备,保证文件内唯一性检查覆盖两节)。"""
        return {d.id for d in self.devices} | {p.id for p in self.pipelines}

    def all_ports(self) -> list[AssemblyPort]:
        """全部端口(含管道设备推导端口)。"""
        ports = [p for d in self.devices for p in d.ports]
        for pipe in self.pipelines:
            ports.extend(_pipeline_derived_ports(pipe))
        return ports

    def all_devices(self) -> list[AssemblyDevice | AssemblyPipeline]:
        """设备实例 + 管道设备(统一遍历用;id 在文件内唯一)。"""
        return [*self.devices, *self.pipelines]


def _pipeline_derived_ports(pipe: AssemblyPipeline) -> list[AssemblyPort]:
    """管道设备推导端口(入端 instantaneous / 出端 delayed;载体取 heat,可被显式声明覆盖)。"""
    delay = int(pipe.params.get("delay_steps", 1) or 1)
    return [
        AssemblyPort(
            device=pipe.id,
            name="heat_in",
            carrier="heat",
            direction="in",
            quantity=QUANTITY_POWER,
            unit="W",
            nature=NATURE_INSTANT,
        ),
        AssemblyPort(
            device=pipe.id,
            name="heat_out",
            carrier="heat",
            direction="out",
            quantity=QUANTITY_POWER,
            unit="W",
            nature=NATURE_DELAYED,
            delay_steps=delay,
        ),
    ]
