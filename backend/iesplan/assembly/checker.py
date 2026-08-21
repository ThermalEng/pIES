"""装配检查器编排:阶段 B(连接合法性)→ C(模型可解性)→ D(整体可解性)→ 约束表达式。

入口统一返回 CheckResult(diagnostics 全量收集,不做短路,一次检查给出完整清单);
check_graph_inputs 是任务装配集成点(tasks.assemble_snapshot 调用):以项目版本
content(含 model 图)为输入,先 build_assembly 再 check_assembly。

端口解析(注册表推导 + 显式声明覆盖)在本模块完成并缓存到 CheckContext.resolved_ports:
- 设备端口:按设备类型业务方向表(services/model.py 同约定)推导,载体→(物理量, 标准单位);
- 管道端口:入端 instantaneous / 出端 delayed(延迟步数取 params.delay_steps);
- 显式 `ports:` 声明仅覆盖 capacity(与推导不一致按 ASM-REF-005 告警,注册表为准)。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from iesplan.assembly.diags import ASM_CONST_DIM, ASM_CONST_SYNTAX, ASM_CONST_UNDEF
from iesplan.assembly.schema import (
    CARRIER_DEFAULT_QUANTITY_UNIT,
    NATURE_DELAYED,
    NATURE_INSTANT,
    AssemblyDevice,
    AssemblyPipeline,
    AssemblyPort,
    AssemblySpec,
    QUANTITY_SIGNAL,
)
from iesplan.core import units
from iesplan.core.diagnostics import Diagnostic, SEVERITY_BLOCKING, SEVERITY_ERROR, make_diag
from iesplan.core.errors import AppError, NotFoundError
from iesplan.core.expression import (
    Dimensions,
    ExpressionCodeError,
    ExpressionDimensionError,
    ExpressionError,
    ExpressionSyntaxError,
    parse_expr,
)
from iesplan.devices import DeviceModelDescriptor as DeviceTypeSpec

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量与业务表(与 services/model.py 同约定;本模块独立声明,不依赖 services)
# ---------------------------------------------------------------------------

#: 管道设备模型(RR-P2-05: 管道为合法业务设备, 在 iesplan.devices YAML 目录
#; 与其他设备一并注册, 装配模块直接消费 descriptor, 不再维护白名单/内置兜底)。
PIPELINE_MODEL_IDS: tuple[str, ...] = ("ies.device.transport_pipe",)

#: 载体 → 端口名后缀规则(in/out 为 "{载体}_{方向}",双向为 "{载体}";与 services 一致)
PORT_TYPE_TO_CARRIER: dict[str, str] = {
    "electric": "electric",
    "thermal": "heat",
    "cooling": "cool",
    "fuel": "gas",
    "data": "data",
}

#: 设备参数单位 → W 的换算键(母线固定供给/需求上限估算用)
_PEAK_PARAM_BY_LOAD: dict[str, str] = {
    "ies.device.electric_load": "peak_power_kw",
    "ies.device.heat_load": "peak_heat_kw",
    "ies.device.cooling_load": "peak_cooling_kw",
}


# ---------------------------------------------------------------------------
# 上下文与结果
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


@dataclass(slots=True)
class CheckResult:
    """检查结果:诊断全量 + 母线汇总。"""

    diagnostics: list[Diagnostic]
    buses: list[BusSummary] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """无 error/blocking 级诊断。"""
        return all(d.severity not in (SEVERITY_ERROR, SEVERITY_BLOCKING) for d in self.diagnostics)

    @property
    def blocking_diags(self) -> list[Diagnostic]:
        """blocking/error 级诊断列表。"""
        return [d for d in self.diagnostics if d.severity in (SEVERITY_ERROR, SEVERITY_BLOCKING)]

    def by_code(self, code: str) -> list[Diagnostic]:
        """按诊断码筛选。"""
        return [d for d in self.diagnostics if d.code == code]


class AssemblyCheckError(AppError):
    """装配检查未通过(存在 error/blocking 级诊断),供任务装配闸门抛 HTTP 422。

    携带完整诊断列表;tasks.assemble_snapshot 调用 check_graph_inputs 后,
    结果不为 ok 时抛本异常并写入任务 diagnostics。
    """

    code = "ASM-CHECK-FAILED"
    message_key = "ies.diag.asm.check_failed"
    http_status = 422

    def __init__(self, diagnostics: list[Diagnostic], message: str = "") -> None:
        self.diagnostics = list(diagnostics)
        super().__init__(
            message or f"装配检查未通过:{len(self.diagnostics)} 条诊断",
            code=self.code,
            message_key=self.message_key,
            params={
                "diag_count": len(self.diagnostics),
                "diagnostics": [d.to_dict() for d in self.diagnostics],
            },
        )


# ---------------------------------------------------------------------------
# 端口解析(注册表推导 + 显式覆盖)
# ---------------------------------------------------------------------------


def _split_model(model: str) -> tuple[str, str | None]:
    """模型引用 → (type_id, version|None)("ies.device.pv@1.3.0" → ("ies.device.pv", "1.3.0"))。"""
    if "@" in model:
        type_id, _, version = model.rpartition("@")
        return type_id, version or None
    return model, None


def resolve_model(ctx: CheckContext, model: str) -> tuple[DeviceTypeSpec | None, bool]:
    """解析模型引用:返回 (类型规格 | None, 是否管道模型)。

    未注册返回 (None, False): 装配禁止用合成/兜底规格伪装可装配视图,
    未注册类型必须被装配显式阻断(RR-P2-05)。
    """
    type_id, _ = _split_model(model)
    registry = ctx.registry
    if registry is None:
        registry = _default_registry()
        ctx.registry = registry
    spec = registry.get(type_id)
    if spec is None:
        return None, False
    return spec, type_id in PIPELINE_MODEL_IDS


def _default_registry() -> dict[str, DeviceTypeSpec]:
    """装配检查的模块内注册表快照(RR-P2-02/05: 消费 devices 公开 descriptor)。

    从 ``iesplan.devices.list_device_descriptors()`` 公开门面构建本模块自己的
    只读候选字典; 注册表未初始化(未调用 init_registry)或为空都必须使装配
    不可用并暴露诊断(宪法 5.3/9.5: 禁止静态回退和宽泛异常兜底)。
    """
    from iesplan.devices import list_device_descriptors

    descriptors = list_device_descriptors()
    merged: dict[str, DeviceTypeSpec] = {}
    for desc in descriptors:
        # 直接采用公开 descriptor(不可变映射/元组已冻结), 不复制重建:
        # 装配只读消费, 共享对象不再可变, 也不会跨模块别名引用下划线符号。
        merged[desc.type_id] = desc
    if not merged:
        raise AppError(
            "装配检查: YAML 设备注册表为空(未初始化或目录无设备), 装配不可用",
            code="SYS-CFG-001",
            message_key="ies.diag.store.config_invalid",
            params={"service": "assembly"},
        )
    return merged


def _yaml_device_ports(device: AssemblyDevice, type_id: str) -> list[AssemblyPort]:
    """从 YAML 设备注册表取端口定义 → AssemblyPort 列表(权威来源)。

    yaml 端口(DeviceYamlSpec.ports: 端口名/载体/方向/容量引用)为唯一权威,
    装配检查据此做连接合法性(REF-004/REF-005)与可解性检查。
    注册表未初始化或端口数据错误一律向上阻断(RR-P2-05: 无静态回退)。
    """
    from iesplan.devices import get_device_descriptor

    spec = get_device_descriptor(type_id)
    ports: list[AssemblyPort] = []
    for p in spec.ports:
        if p.energy_carrier not in CARRIER_DEFAULT_QUANTITY_UNIT:
            continue  # solar 等环境侧载体(不可连接)不参与装配端口/母线平衡
        unit = CARRIER_DEFAULT_QUANTITY_UNIT.get(p.energy_carrier, (QUANTITY_SIGNAL, "-"))[1]
        qty = CARRIER_DEFAULT_QUANTITY_UNIT.get(p.energy_carrier, (QUANTITY_SIGNAL, "-"))[0]
        capacity = None
        if p.capacity_ref and p.capacity_ref in spec.parameters:
            cap = spec.parameters[p.capacity_ref].default
            if isinstance(cap, (int, float)):
                capacity = float(cap)
        ports.append(
            AssemblyPort(
                device=device.id,
                name=p.name,
                carrier=p.energy_carrier,
                direction=p.direction,
                quantity=qty,
                unit=unit,
                nature=NATURE_INSTANT,
                capacity=capacity,
            )
        )
    return ports


def _port_name(carrier: str, direction: str) -> str:
    """端口命名:in/out 为 "{载体}_{方向}",双向为 "{载体}"。"""
    return f"{carrier}_{direction}" if direction in ("in", "out") else carrier


def _derive_device_ports(spec: AssemblySpec, ctx: CheckContext, device: AssemblyDevice) -> list[AssemblyPort]:
    """设备端口推导(RR-P2-05: YAML 公开 descriptor 端口为唯一权威, 无静态回退)。

    已注册设备取 YAML 端口声明; 未注册设备(测试注入的自定义类型)按
    装配文本显式 ``ports:`` 声明转换。显式声明的 capacity 在两条路径
    之后统一合并覆盖。
    """
    type_spec, _ = resolve_model(ctx, device.model)
    if type_spec is None:
        return []  # 模型未注册,端口无从推导(REF-002 已报)
    try:
        derived = _yaml_device_ports(device, type_spec.type_id)
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


def _derive_pipeline_ports(pipe: AssemblyPipeline) -> list[AssemblyPort]:
    """管道端口推导(从 YAML descriptor 读取真实端口, 不再内置常量)。

    入端 instantaneous / 出端 delayed(延迟取 params.delay_steps, 缺省 1)。
    """
    from iesplan.devices import get_device_descriptor

    spec = get_device_descriptor(pipe.model.split("@", 1)[0])
    in_port = next((p for p in spec.ports if p.direction == "in"), None)
    out_port = next((p for p in spec.ports if p.direction == "out"), None)
    if in_port is None or out_port is None:
        # 未声明输入/输出端口 → 装配阻断, 不再兜底合成(RR-P2-05)。
        raise AppError(
            f"管道模型 {pipe.model} 必须声明 in/out 端口, 装配不可用",
            code="SYS-CFG-001",
            message_key="ies.diag.store.config_invalid",
            params={"model": pipe.model},
        )
    delay = int(pipe.params.get("delay_steps", 1) or 1)
    in_carrier = in_port.energy_carrier
    in_qty, in_unit = CARRIER_DEFAULT_QUANTITY_UNIT[in_carrier]
    out_carrier = out_port.energy_carrier
    out_qty, out_unit = CARRIER_DEFAULT_QUANTITY_UNIT[out_carrier]
    return [
        AssemblyPort(
            device=pipe.id,
            name=in_port.name,
            carrier=in_carrier,
            direction="in",
            quantity=in_qty,
            unit=in_unit,
            nature=NATURE_INSTANT,
        ),
        AssemblyPort(
            device=pipe.id,
            name=out_port.name,
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
        for port in _derive_device_ports(spec, ctx, device):
            resolved[port.ref] = port
    for pipe in spec.pipelines:
        for port in _derive_pipeline_ports(pipe):
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


def _unit_category(unit: str) -> str | None:
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
    cats = {_unit_category(a), _unit_category(b)}
    if cats == {"energy", "power"}:
        return True
    return False


_UNIT_CATEGORY_DIMS: dict[str, dict[str, int]] = {
    "power": {"power": 1},
    "energy": {"energy": 1},
    "temperature": {"temperature": 1},
    "currency": {"currency": 1},
    "duration": {"time": 1},
    "angle": {"angle": 1},
}


def _unit_dims(unit: str | None, quantity: str | None = None) -> Dimensions:
    """单位 → 表达式量纲(先查 core/units 注册表类别,未注册按物理量回退)。"""
    from collections import Counter

    u = (unit or "").strip()
    if u in ("", "-", "1"):
        return Counter()
    from iesplan.core.units import ALIAS_MAP, UNITS

    uid = ALIAS_MAP.get(u.lower())
    if uid is not None and uid in UNITS:
        cat = UNITS[uid].category
        return Counter(_UNIT_CATEGORY_DIMS.get(cat, {}))
    # 未注册单位(如 m3/s、CNY/J):按端口物理量回退
    from iesplan.assembly.schema import _QUANTITY_DIMS

    return Counter(_QUANTITY_DIMS.get(quantity or "", {}))


def _to_watts(value: float | None, unit: str | None) -> float | None:
    """业务单位数值 → W(仅当单位可换算到 W;否则 None)。"""
    if value is None:
        return None
    try:
        return units.convert(float(value), unit or "W", "W")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 约束表达式检查
# ---------------------------------------------------------------------------

#: 点路径符号 token(<dev>.<port> / <dev>.<param>;表达式引擎 AST 白名单不支持属性访问,
#: 检查器先做符号重写,未重写成功的点路径即未定义符号 → ASM-CONST-003)
_SYMBOL_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_.])([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)(?![A-Za-z0-9_.])"
)


def _rewrite_symbols(expr: str, symbols: dict[str, Dimensions]) -> tuple[str, dict[str, Dimensions]]:
    """已知符号(点路径)→ vN 标识符(引擎不支持属性访问,先做符号重写)。"""
    var_dims: dict[str, Dimensions] = {}
    new = expr
    for i, sym in enumerate(sorted(symbols, key=len, reverse=True), start=1):
        name = f"v{i}"
        var_dims[name] = symbols[sym]
        new = re.sub(
            rf"(?<![A-Za-z0-9_.]){re.escape(sym)}(?![A-Za-z0-9_.])",
            name,
            new,
        )
    return new, var_dims


def _undefined_symbols(expr: str) -> list[str]:
    """重写后剩余的点路径 token(未定义设备/端口/参数符号)。"""
    return [m.group(1) for m in _SYMBOL_TOKEN_RE.finditer(expr)]


def _defined_symbols(spec: AssemblySpec, ctx: CheckContext) -> dict[str, Dimensions]:
    """全部可引用符号 → 量纲:端口引用 <dev>.<port> + 参数引用 <dev>.<param>。"""
    resolved = ensure_ports(spec, ctx)
    symbols: dict[str, Dimensions] = {}
    for ref, port in resolved.items():
        symbols[ref] = _unit_dims(port.unit, port.quantity)
    registry = ctx.registry or _default_registry()
    for device in spec.devices:
        type_spec, _ = resolve_model(ctx, device.model)
        if type_spec is None:
            continue
        for name, value in device.params.items():
            if isinstance(value, (dict, list)):
                continue
            pspec = type_spec.parameters.get(name)
            p_unit = pspec.unit if pspec is not None else None
            symbols[f"{device.id}.{name}"] = _unit_dims(p_unit, None)
    return symbols


def run_constraint_checks(spec: AssemblySpec, ctx: CheckContext) -> list[Diagnostic]:
    """约束表达式检查(ASM-CONST-001..003;复用 core/expression.py 引擎)。"""
    diags: list[Diagnostic] = []
    if not spec.constraints:
        return diags
    symbols = _defined_symbols(spec, ctx)
    for constraint in spec.constraints:
        if not constraint.enabled:
            continue
        # 1) 点路径符号重写(已知符号 → vN);2) 显式单位后缀由 parse_expr
        #    内部改写为带量纲常量(01 §5.5, 引擎层共享, 检查器不再预改写)
        expr, symbol_dims = _rewrite_symbols(constraint.expr, symbols)
        loc = {"object_type": "constraint", "object_id": constraint.id, "field": "expr"}
        # 未重写成功的点路径 = 引用未定义符号
        undefined = _undefined_symbols(expr)
        if undefined:
            diags.append(
                make_diag(
                    ASM_CONST_UNDEF,
                    severity="error",
                    blocking=True,
                    params={
                        "constraint": constraint.id,
                        "symbol": undefined[0],
                        "expr": constraint.expr,
                    },
                    location=loc,
                )
            )
            continue
        var_dims = symbol_dims
        try:
            parse_expr(expr, set(var_dims), var_dims)
        except ExpressionCodeError as exc:  # 引用未定义符号
            diags.append(
                make_diag(
                    ASM_CONST_UNDEF,
                    severity="error",
                    blocking=True,
                    params={
                        "constraint": constraint.id,
                        "symbol": exc.params.get("variable", ""),
                        "expr": constraint.expr,
                    },
                    location=loc,
                )
            )
        except ExpressionDimensionError as exc:  # 量纲不一致
            diags.append(
                make_diag(
                    ASM_CONST_DIM,
                    severity="error",
                    blocking=True,
                    params={"constraint": constraint.id, "expr": constraint.expr, "detail": str(exc)},
                    location=loc,
                )
            )
        except ExpressionSyntaxError as exc:  # 语法错误
            diags.append(
                make_diag(
                    ASM_CONST_SYNTAX,
                    severity="error",
                    blocking=True,
                    params={"constraint": constraint.id, "expr": constraint.expr, "detail": str(exc)},
                    location=loc,
                )
            )
        except ExpressionError as exc:  # 白名单/范围/类型等其余错误
            diags.append(
                make_diag(
                    ASM_CONST_SYNTAX,
                    severity="error",
                    blocking=True,
                    params={"constraint": constraint.id, "expr": constraint.expr, "detail": str(exc)},
                    location=loc,
                )
            )
    return diags


# ---------------------------------------------------------------------------
# 编排入口
# ---------------------------------------------------------------------------


def _default_context(spec: AssemblySpec | None = None) -> CheckContext:
    """默认检查上下文:注册表快照 + 按 spec 时间轴惰性加载。"""
    ctx = CheckContext(registry=_default_registry())
    if spec is not None and spec.time_axis is not None:
        ctx.time_axis = {"n": spec.time_axis.steps_per_year, "resolution": spec.time_axis.resolution}
    return ctx


def check_assembly(spec: AssemblySpec, *, ctx: CheckContext | None = None) -> CheckResult:
    """装配对象检查:阶段 B/C/D 全量执行;阶段 A 已由 parse 完成(文本入口再次校验)。"""
    from iesplan.assembly.rules import run_phase_b, run_phase_c, run_phase_d

    ctx = ctx or _default_context(spec)
    ensure_ports(spec, ctx)
    diags = run_phase_b(spec, ctx)
    diags += run_phase_c(spec, ctx)
    d_diags, buses = run_phase_d(spec, ctx)
    diags += d_diags
    diags += run_constraint_checks(spec, ctx)
    diags = diags[: ctx.max_diags]
    return CheckResult(diagnostics=diags, buses=buses)


def check_assembly_text(text: str, *, ctx: CheckContext | None = None) -> CheckResult:
    """文本 → parse(A) → check(B/C/D),一次调用返回完整结果。"""
    from iesplan.assembly.parser import parse_assembly

    result = parse_assembly(text)
    if result.spec is None:
        return CheckResult(diagnostics=list(result.diagnostics))
    return check_assembly(result.spec, ctx=ctx)


def check_graph_inputs(
    project_version_content: dict,
    *,
    datasets: dict[int, dict] | None = None,
    ctx: CheckContext | None = None,
) -> CheckResult:
    """任务装配集成点:内容 → build_assembly → check_assembly。

    供 tasks.assemble_snapshot 在 content_hash 去重之后调用;
    返回结果中 error 级诊断即阻断任务下发(写入任务 diagnostics)。
    content 结构:{"model": {devices, ports, connections}, "calc_config": {...}} 或扁平图结构。
    """
    from iesplan.assembly.builder import build_assembly

    model_part = project_version_content.get("model", project_version_content)
    if not isinstance(model_part, dict):
        model_part = {}
    graph = {
        "devices": model_part.get("devices", []),
        "ports": model_part.get("ports", []),
        "connections": model_part.get("connections", []),
    }
    calc_cfg = project_version_content.get("calc_config")
    calc_config = calc_cfg if isinstance(calc_cfg, dict) else None
    spec = build_assembly(graph, datasets=datasets, calc_config=calc_config)
    # 数据集元信息必须进入检查上下文:缺失版本/列/分辨率检查仅在 ctx.datasets
    # 非 None 时执行(codex 二次审核 High-1: 之前只喂给 builder, 闸门检查被绕过)
    ctx = ctx or _default_context(spec)
    if datasets is not None and ctx.datasets is None:
        ctx.datasets = datasets
    return check_assembly(spec, ctx=ctx)


__all__ = [
    "CheckContext",
    "BusSummary",
    "CheckResult",
    "AssemblyCheckError",
    "check_assembly",
    "check_assembly_text",
    "check_graph_inputs",
    "resolve_ports",
    "resolve_model",
    "ensure_ports",
    "units_compatible",
    "run_constraint_checks",
    "PIPELINE_MODEL_IDS",
    "PORT_TYPE_TO_CARRIER",
    "_split_model",
]
