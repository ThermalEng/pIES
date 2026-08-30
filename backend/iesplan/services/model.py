"""系统模型服务(U04 模型写入单元): 设备/端口/连接写入、拓扑校验与图内容寻址。

- 写入目标为关系模型表（见 modules/persistence.md；表见 models/model.py）: system_graphs / devices / ports / connections;
- 设备类型与参数 schema 以受控注册表(04 §3)为唯一事实源, 类型未注册/参数越界一律拒绝;
- 端口按设备类型的能源载体自动生成(如 heat_pump → electric_in/heat_out/cool_out);
- 连接校验: 能源类型一致 + 方向兼容(源→汇) + 同项目同图 + 无重复, 失败返回可定位诊断;
- 图内容哈希: sha256(规范化 JSON), 覆盖节点/边/参数(含行 id), 排除布局与易变元数据。

约定:
- 布局坐标存于设备 params["__layout"]["position"], 不参与注册表校验与内容哈希;
- 完整注册表类型 id 存于 params["type_detail"](01 §4.2 "细分类别");
- 诊断码: 优先使用 04 §5.3 已登记码; 连接校验码(CONN-PORT-*)为新码, 经 AppError 输出,
  待诊断目录后续登记(本单元不修改 core/diagnostics 目录)。
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from iesplan.core.diagnostics import (
    CONN_NODE_ORPHAN,
    CONN_TYPE_UNREGISTERED,
    PARAM_CONFLICT,
    PARAM_RNG_OUT,
    PARAM_UNIT_INCONSISTENT,
    PARAM_UNIT_MISMATCH,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    Diagnostic,
    make_diag,
)
from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.core.idgen import sha256_hex
from iesplan.devices import DeviceModelDescriptor, ParameterSpec, get_device_descriptor
from iesplan.models.model import Connection, Device, Port, SystemGraph
from iesplan.models.project import Draft, Project
from iesplan.services import project as project_service

# ---------------------------------------------------------------------------
# 常量: 载体/端口/连接类型映射(01 §4.3/§4.4 枚举约束)
# ---------------------------------------------------------------------------

#: 载体 → 端口类型(port_type CHECK)。solar 为环境侧载体, 不生成可连接端口。
CARRIER_PORT_TYPE: dict[str, str] = {
    "electric": "electric",
    "heat": "thermal",
    "cool": "cooling",
    "gas": "fuel",
}

#: 端口类型 → 连接类型(conn_type CHECK)
CONN_TYPE_BY_PORT: dict[str, str] = {
    "electric": "electric_line",
    "thermal": "thermal_pipe",
    "cooling": "cooling_pipe",
    "fuel": "fuel_pipe",
    "data": "data_link",
}

#: 能力字典 → 设备粗分类别(yaml capabilities 派生, RR-P1-04: 不再维护设备类型静态表)
_CAPABILITY_COARSE: dict[str, str] = {
    "grid_connection": "source",
    "pv": "pv",
    "storage": "storage",
    "load": "load",
    "heat_pump": "converter",
    "thermal_generation": "boiler",
    "cooling_generation": "chiller",
}

#: 内部保留参数键: '_' 前缀(布局等)与 type_detail(细分类别), 不参与注册表校验与内容哈希
_TYPE_DETAIL_KEY = "type_detail"
_LAYOUT_KEY = "__layout"
_MODEL_FIDELITIES = ("low", "medium", "high")

#: 必填参数(04 §3 各设备 required 清单中无默认值的 reference 类参数;
#: 注册表未直接编码必填标志, 在此显式声明; 其余参数均有默认值, 缺省按默认填充)
#: 缺省时按显式 null 归一(前端跳过 default=null 的参数键, 显式 null 历来可创建,
#: 此处统一"缺失"与"显式 null"两种写法, 避免前端拖拽负荷设备被 400 阻断)
_REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "ies.device.electric_load": ("load_profile",),
    "ies.device.heat_load": ("heat_profile",),
    "ies.device.cooling_load": ("cooling_profile",),
}

# ---------------------------------------------------------------------------
# 连接校验错误码(新码, 经 AppError 输出; 诊断目录后续登记)
# ---------------------------------------------------------------------------
CONN_ENERGY_MISMATCH = "CONN-PORT-001"  # 能源类型不一致
CONN_DIRECTION_INVALID = "CONN-PORT-002"  # 方向不兼容
CONN_CROSS_PROJECT = "CONN-PORT-003"  # 端口不属于同一项目图
CONN_DUPLICATE = "CONN-DUP-001"  # 重复连接
CONN_SELF_LOOP = "CONN-DUP-002"  # 自环


class ModelValidationError(AppError):
    """模型写入校验失败(HTTP 400, 携带诊断码与对象定位)。"""

    http_status = 400
    severity = SEVERITY_ERROR


def _is_internal_key(name: str) -> bool:
    """是否内部保留参数键(布局/细分类别), 不参与注册表校验与内容哈希。"""
    return name.startswith("_") or name == _TYPE_DETAIL_KEY


def _json_clean(value: Any) -> Any:
    """JSON 序列化清洗: Decimal → float, datetime → ISO 文本, 其余原样。"""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_clean(v) for v in value]
    return value


def _normalize_position(position: dict | None) -> dict | None:
    """校验并规范化布局坐标 {"x": float, "y": float}; 非法输入抛校验错误。"""
    if position is None:
        return None
    try:
        return {"position": {"x": float(position["x"]), "y": float(position["y"])}}
    except (KeyError, TypeError, ValueError):
        raise ModelValidationError(
            "布局坐标非法",
            code=PARAM_RNG_OUT,
            message_key="ies.diag.param.rng_out",
            params={"param": "position", "value": _json_clean(position), "min": None, "max": None},
            location={"object_type": "device", "field": "position"},
        ) from None


def _port_name(carrier: str, direction: str) -> str:
    """端口命名: in/out 为 {载体}_{方向}, 双向为 {载体}(每设备每载体至多一个端口)。"""
    return f"{carrier}_{direction}" if direction in ("in", "out") else carrier


def _descriptor_ports(spec: DeviceModelDescriptor, params: dict | None = None) -> list[dict]:
    """设备类型的真实端口列表(RR-P1-04: YAML 端口声明为唯一权威来源)。

    返回 [{carrier, direction, name, capacity_ref}]。热泵按 mode 参数裁剪端口:
    heating 只保留 heat, cooling 只保留 cool —— 避免未启用的冷/热载体成为
    拓扑校验的孤立载体(PARAM-UNIT-003 能源不平衡误报)。双向端口名不带方向后缀。
    """
    params = params or {}
    ports = []
    for p in spec.ports:
        carrier = p.energy_carrier
        if spec.type_id == "ies.device.heat_pump" and carrier in ("heat", "cool"):
            mode = params.get("mode", "both")
            if mode == "heating" and carrier == "cool":
                continue
            if mode == "cooling" and carrier == "heat":
                continue
        ports.append(
            {
                "carrier": carrier,
                "direction": p.direction,
                "name": p.name,
                "capacity_ref": p.capacity_ref,
            }
        )
    return ports


def _sync_ports_for_params(
    db: Session, device: Device, spec: DeviceModelDescriptor, params: dict
) -> None:
    """按设备参数(热泵 mode)重同步端口: 补齐应存在但缺失的端口, 删除被裁剪的端口。

    以 YAML 端口声明为唯一权威(与创建设备路径一致): 期望端口集合按端口名
    (device_id, name) 精确匹配, 支持同一载体多个不同名端口; 不再按
    carrier+direction 合并(同载能多端口时后一个会覆盖前一个, 服务器端口缺失,
    前端真实句柄找不到对应服务器端口 → 合法连线被判定端口缺失, 见 codex 复审 N1)。
    被删除端口的既有连接一并删除(端口语义随模式变化, 原连接不再有效)。
    """
    # 期望端口: YAML 声明中可生成连接端口的那部分(按 name 去重)
    wanted: dict[str, dict] = {}
    for port in _descriptor_ports(spec, params):
        if port["carrier"] not in CARRIER_PORT_TYPE:
            continue  # solar 等环境侧载体不生成可连接端口
        ptype = CARRIER_PORT_TYPE[port["carrier"]]
        # 同载能同方向多端口各有真实名; 不同名端口都保留
        wanted.setdefault(
            port["name"],
            {"carrier": port["carrier"], "direction": port["direction"], "ptype": ptype},
        )
    existing = {p.name: p for p in db.scalars(select(Port).where(Port.device_id == device.id))}
    # 删除不再需要的端口(及其连接): 现有端口名不在期望集合内
    for name, port in existing.items():
        if name not in wanted:
            db.execute(sa.delete(Connection).where(Connection.from_port_id == port.id))
            db.execute(sa.delete(Connection).where(Connection.to_port_id == port.id))
            db.delete(port)
    # 补回应有但缺失的端口(名称取自 YAML 端口声明)
    for name, want in wanted.items():
        if name not in existing:
            db.add(
                Port(
                    device_id=device.id,
                    port_type=want["ptype"],
                    direction=want["direction"],
                    name=name,
                    params={},
                )
            )


def _coarse_category(type_id: str) -> str:
    """注册表类型 id → devices.device_type 粗分类别(01 §4.2 CHECK; 未知类型落 'other')。"""
    from iesplan.devices import get_device_descriptor

    try:
        desc = get_device_descriptor(type_id)
    except NotFoundError:
        return "other"
    for cap in desc.capabilities:
        category = _CAPABILITY_COARSE.get(cap)
        if category:
            return category
    if desc.is_load:
        return "load"
    carriers = set(desc.energy_carriers)
    if "gas" in carriers:
        return "boiler"
    if "cool" in carriers:
        return "chiller"
    if "heat" in carriers:
        return "converter"
    return "other"


def _resolve_type_id(device: Device) -> str:
    """从设备行解析完整注册表类型 id(params.type_detail 优先, 回退粗分类别)。"""
    detail = device.params.get(_TYPE_DETAIL_KEY)
    return detail if isinstance(detail, str) and detail else device.device_type


def _try_get_device_type(type_id: str) -> DeviceModelDescriptor | None:
    """按注册表取设备类型; 未注册返回 None(不抛错, 供校验诊断用)。"""
    try:
        return get_device_descriptor(type_id)
    except NotFoundError:
        return None


def _ensure_mutable(graph: SystemGraph) -> None:
    """版本图不可修改(01 §4.1 冻结规则, 应用层判定)。"""
    if graph.project_version_id is not None:
        raise ConflictError(
            "版本图不可修改",
            location={"object_type": "system_graph", "object_id": str(graph.id)},
        )


def _raise_diagnostics(diags: list[Diagnostic]) -> None:
    """校验诊断中任一 error 级即抛校验错误(携带诊断码/参数/定位), 供写入操作拒绝。"""
    for d in diags:
        if d.severity == SEVERITY_ERROR:
            raise ModelValidationError(
                d.message_key,
                code=d.code,
                message_key=d.message_key,
                params=d.params,
                location=d.location,
            )


# ---------------------------------------------------------------------------
# 系统图: 工作图查询/创建与内容哈希
# ---------------------------------------------------------------------------


def _find_working_graph(db: Session, project_id: int) -> SystemGraph | None:
    """项目的工作图(挂草稿即工作图, 01 §4.1; 按 id 升序取最早一张, 保证确定性)。"""
    return db.scalar(
        select(SystemGraph)
        .where(SystemGraph.project_id == project_id, SystemGraph.draft_id.is_not(None))
        .order_by(SystemGraph.id)
    )


def get_or_create_working_graph(db: Session, project_id: int, created_by: int = 1) -> SystemGraph:
    """取项目工作图; 不存在则连同工作草稿一起创建(幂等)。

    草稿为工作图的内容载体(01 §3.2/§4.1): 草稿 content_hash 与图内容哈希保持一致。
    并发安全: 首批设备快速连发时多个请求可能同时判定"无工作图"并发建图(实测同一项目
    8ms 内出现两张图, 设备被随机分裂); 由 uq_system_graphs_working 部分唯一索引
    (配合草稿 uq_drafts_revision/uq_drafts_current)兜底 —— 唯一键冲突方回滚本事务
    半成品后重查胜方已提交的图, 重试上限内必收敛。
    """
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(
            f"项目不存在: {project_id}",
            location={"object_type": "project", "object_id": str(project_id)},
        )
    for _attempt in range(3):
        graph = _find_working_graph(db, project_id)
        if graph is not None:
            return graph
        try:
            # 复用既有当前草稿, 否则新建(修订号顺延)
            draft = db.scalar(
                select(Draft)
                .where(Draft.project_id == project_id, Draft.is_current.is_(True))
                .order_by(Draft.revision.desc())
            )
            if draft is None:
                max_rev = db.scalar(select(sa.func.max(Draft.revision)).where(Draft.project_id == project_id))
                draft = Draft(
                    project_id=project_id,
                    revision=int(max_rev or 0) + 1,
                    content_hash="0" * 64,
                    updated_by=created_by,
                    is_current=True,
                )
                db.add(draft)
                db.flush()
            graph = SystemGraph(
                project_id=project_id,
                draft_id=draft.id,
                name="工作图",
                graph_hash=draft.content_hash,
                created_by=created_by,
            )
            db.add(graph)
            db.flush()
            refresh_graph_hash(db, graph)
            db.commit()
            return graph
        except IntegrityError:
            # 并发竞争者已提交建图/建草稿: 放弃本事务半成品, 下一轮重查胜方结果
            db.rollback()
    raise ConflictError(
        "工作图创建失败: 并发冲突, 请重试",
        location={"object_type": "project", "object_id": str(project_id)},
    )


def _load_devices(db: Session, graph_id: int) -> list[Device]:
    """图内设备(按 id 升序, 保证内容哈希确定性)。"""
    return list(db.scalars(select(Device).where(Device.graph_id == graph_id).order_by(Device.id)))


def _load_ports(db: Session, graph_id: int) -> list[Port]:
    """图内端口(按 id 升序)。"""
    stmt = (
        select(Port)
        .join(Device, Port.device_id == Device.id)
        .where(Device.graph_id == graph_id)
        .order_by(Port.id)
    )
    return list(db.scalars(stmt))


def _load_connections(db: Session, graph_id: int) -> list[Connection]:
    """图内连接(按 id 升序)。"""
    return list(
        db.scalars(select(Connection).where(Connection.graph_id == graph_id).order_by(Connection.id))
    )


def _device_hash_payload(device: Device) -> dict:
    """设备的内容哈希载荷(排除布局等内部保留键; 服务端默认值兜底, 保证行内/库内一致)。"""
    params = {k: v for k, v in device.params.items() if not _is_internal_key(k)}
    return {
        "id": device.id,
        "device_type": _resolve_type_id(device),
        "kind": device.kind,
        "name": device.name,
        "params": params,
        "model_fidelity": device.model_fidelity,
        "status": device.status or "active",
    }


def refresh_graph_hash(db: Session, graph: SystemGraph) -> str:
    """重算图内容哈希(规范化 JSON → sha256)并写回, 同步草稿内容哈希。

    内容 = 设备/端口/连接(含行 id 与参数, 排除布局与项目/图/名称等易变元数据),
    规范化 = 列表按 id 排序 + json sort_keys, 同一内容哈希稳定。
    """
    db.flush()  # 先落盘挂起的新增/删除, 保证哈希覆盖当前事务内的完整图内容
    devices = _load_devices(db, graph.id)
    ports = _load_ports(db, graph.id)
    conns = _load_connections(db, graph.id)
    payload = {
        "devices": [_device_hash_payload(d) for d in devices],
        "ports": [
            {
                "id": p.id,
                "device_id": p.device_id,
                "port_type": p.port_type,
                "direction": p.direction,
                "name": p.name,
                "capacity": _json_clean(p.capacity),
                "params": p.params or {},
            }
            for p in ports
        ],
        "connections": [
            {
                "id": c.id,
                "from_port_id": c.from_port_id,
                "to_port_id": c.to_port_id,
                "conn_type": c.conn_type,
                "capacity": _json_clean(c.capacity),
                "loss_rate": _json_clean(c.loss_rate),
                "params": c.params or {},
            }
            for c in conns
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    graph_hash = sha256_hex(canonical.encode("utf-8"))
    graph.graph_hash = graph_hash
    if graph.draft_id is not None:
        draft = db.get(Draft, graph.draft_id)
        if draft is not None:
            # 草稿为工作图的内容载体(01 §3.2/§4.1): 图内容并入草稿内容文档并落为
            # 内容寻址对象, 使草稿 content_hash 可解析(校验/草稿命令依赖);
            # 既有内容节(dataset_bindings/calc_config 等)原样保留。
            content = _draft_content_with_model(db, draft, payload)
            draft.content_hash = project_service.store_content_object(db, content)
    db.flush()
    return graph_hash


def _draft_content_with_model(db: Session, draft: Draft, payload: dict) -> dict:
    """草稿内容文档: 图内容(设备/端口/连接)并入 model 节, 其余内容节原样保留。

    草稿内容对象缺失(如新建图前的占位哈希)或损坏时回退到初始内容骨架,
    避免模型写入依赖其他单元的内容落盘时序。
    """
    try:
        content = project_service.load_content_object(db, draft.content_hash)
    except AppError:
        content = project_service.initial_content()
    if not isinstance(content, dict):
        content = project_service.initial_content()
    content.setdefault("model", {"devices": [], "ports": [], "connections": []})
    content["model"] = {
        "devices": payload.get("devices", []),
        "ports": payload.get("ports", []),
        "connections": payload.get("connections", []),
    }
    # 补齐骨架节(既有内容对象可能缺少非模型节), 保证草稿命令/只读聚合可安全访问
    skeleton = project_service.initial_content()
    for key, default in skeleton.items():
        if key not in content:
            content[key] = default
    return content


# ---------------------------------------------------------------------------
# 参数校验(注册表 schema: 类型/单位/范围/枚举/必填)
# ---------------------------------------------------------------------------


def validate_device_params(
    device_type: str, params: dict | None, device_id: int | str = ""
) -> list[Diagnostic]:
    """按注册表 schema 校验设备参数(04 §3)。

    规则:
    - 类型未注册抛 NotFoundError(CONN-TYPE-002);
    - 注册表 default 为 None 的参数视为必填(reference 类, 如负荷曲线), 缺失时
      归一为显式 null(与前端跳过 null 默认值的行为对齐, 不再因缺键拒绝创建);
    - 数值参数校验 min/max(越界 → PARAM-RNG-003), 枚举参数校验取值, 类型不匹配 → PARAM-UNIT-002;
    - 内部保留键('_' 前缀与 type_detail)不参与校验。
    """
    spec = get_device_descriptor(device_type)
    params = dict(params or {})  # 拷贝: 归一缺省键不得改写调用方/ORM 上的原字典
    # 必填参数缺失按显式 null 归一(前端 buildDefaultParams 跳过 default=null 的键,
    # 拖拽新建负荷设备时 load_profile/heat_profile 等不会出现; 显式 null 历来通过校验)
    for required_name in _REQUIRED_PARAMS.get(device_type, ()):
        params.setdefault(required_name, None)
    provided = {k: v for k, v in params.items() if not _is_internal_key(k)}
    diags: list[Diagnostic] = []
    loc = {"object_type": "device", "object_id": str(device_id), "field": ""}

    for name, value in provided.items():
        pspec = spec.parameters.get(name)
        if pspec is None:
            diags.append(
                make_diag(
                    PARAM_UNIT_MISMATCH,
                    severity=SEVERITY_ERROR,
                    params={"param": name, "expected": "registered", "actual": "unknown"},
                    location={**loc, "field": name},
                )
            )
            continue
        diags.extend(_check_param_value(name, value, pspec, loc))
    return diags


def _check_param_value(
    name: str, value: Any, pspec: ParameterSpec, loc: dict
) -> list[Diagnostic]:
    """单参数取值校验(枚举/类型/范围), 返回诊断列表。"""
    # 引用类参数(unit == "reference", 如负荷曲线/COP 曲线): 值为数据集引用
    # (字符串/对象)或 None(未绑定); 引用语义由数据绑定层校验, 不在此做类型检查
    if pspec.unit == "reference":
        return []
    # 枚举类参数
    if pspec.enum is not None:
        if value not in pspec.enum:
            return [
                make_diag(
                    PARAM_RNG_OUT,
                    severity=SEVERITY_ERROR,
                    params={
                        "param": name,
                        "value": _json_clean(value),
                        "min": None,
                        "max": None,
                        "allowed": list(pspec.enum),
                    },
                    location={**loc, "field": name},
                )
            ]
        return []
    # 布尔类参数
    if isinstance(pspec.default, bool):
        if not isinstance(value, bool):
            return [_type_mismatch_diag(name, value, pspec, loc)]
        return []
    # 字典类参数(如分时电价 import_tariff)
    if isinstance(pspec.default, dict):
        if not isinstance(value, dict):
            return [_type_mismatch_diag(name, value, pspec, loc)]
        return []
    # 数值类参数(带范围或数值默认值)
    if pspec.min is not None or pspec.max is not None or isinstance(pspec.default, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [_type_mismatch_diag(name, value, pspec, loc)]
        # M-08: 拒绝 NaN/Infinity(非有限值可绕过 min/max 比较, 污染后续求解与哈希)
        if not math.isfinite(float(value)):
            return [_type_mismatch_diag(name, value, pspec, loc)]
        if pspec.min is not None and value < pspec.min:
            return [
                make_diag(
                    PARAM_RNG_OUT,
                    severity=SEVERITY_ERROR,
                    params={"param": name, "value": value, "min": pspec.min, "max": pspec.max},
                    location={**loc, "field": name},
                )
            ]
        if pspec.max is not None and value > pspec.max:
            return [
                make_diag(
                    PARAM_RNG_OUT,
                    severity=SEVERITY_ERROR,
                    params={"param": name, "value": value, "min": pspec.min, "max": pspec.max},
                    location={**loc, "field": name},
                )
            ]
    return []


def _type_mismatch_diag(name: str, value: Any, pspec: ParameterSpec, loc: dict) -> Diagnostic:
    """参数类型/单位不匹配诊断(期望注册单位, 实际为 Python 类型名)。"""
    return make_diag(
        PARAM_UNIT_MISMATCH,
        severity=SEVERITY_ERROR,
        params={"param": name, "expected": pspec.unit, "actual": type(value).__name__},
        location={**loc, "field": name},
    )


# ---------------------------------------------------------------------------
# 设备写入
# ---------------------------------------------------------------------------


def create_device(
    db: Session,
    project_id: int,
    device_type: str,
    name: str,
    params: dict | None = None,
    is_existing: bool = False,
    model_precision: str = "medium",
    position: dict | None = None,
    created_by: int = 1,
) -> Device:
    """创建设备: 校验类型/参数, 按载体生成端口, 刷新图内容哈希。

    参数:
        device_type: 注册表类型 id(如 'ies.device.heat_pump'), 未注册抛 NotFoundError。
        name: 设备名(图内唯一, 重复抛 ConflictError)。
        params: 参数(按注册表 schema 校验, 越界/类型错误/缺必填 → 校验错误并定位)。
        is_existing: True=存量设备(kind='existing'), False=新增设备(kind='new')。
        model_precision: 模型精度 low/medium/high(01 §4.2 model_fidelity)。
        position: 画布坐标 {"x": float, "y": float}(布局信息, 不入内容哈希)。
        created_by: 创建者用户 id(工作图不存在时用于建图/建草稿)。
    """
    spec = get_device_descriptor(device_type)
    if model_precision not in _MODEL_FIDELITIES:
        raise ModelValidationError(
            "模型精度非法",
            code=PARAM_RNG_OUT,
            message_key="ies.diag.param.rng_out",
            params={
                "param": "model_precision",
                "value": model_precision,
                "min": None,
                "max": None,
                "allowed": list(_MODEL_FIDELITIES),
            },
            location={"object_type": "device", "field": "model_precision"},
        )
    if not name or not name.strip():
        raise ModelValidationError(
            "设备名称不能为空",
            code=PARAM_RNG_OUT,
            message_key="ies.diag.param.rng_out",
            params={"param": "name", "value": name, "min": None, "max": None},
            location={"object_type": "device", "field": "name"},
        )
    params = dict(params or {})
    params[_TYPE_DETAIL_KEY] = device_type  # 完整注册表类型 id(01 §4.2 细分类别)
    if position is not None:
        params[_LAYOUT_KEY] = _normalize_position(position)
    # 缺省必填参数归一为显式 null(与 validate_device_params 语义一致, 存储与显式 null 相同)
    for required_name in _REQUIRED_PARAMS.get(device_type, ()):
        params.setdefault(required_name, None)
    diags = validate_device_params(device_type, params)
    _raise_diagnostics(diags)
    graph = get_or_create_working_graph(db, project_id, created_by)
    _ensure_mutable(graph)
    if db.scalar(select(Device.id).where(Device.graph_id == graph.id, Device.name == name)) is not None:
        raise ConflictError(
            f"设备名称重复: {name}",
            params={"device_id": "", "name": name},
            location={"object_type": "device", "field": "name"},
        )
    device = Device(
        graph_id=graph.id,
        device_type=_coarse_category(device_type),
        kind="existing" if is_existing else "new",
        name=name,
        params=params,
        model_fidelity=model_precision,
        status="active",
    )
    db.add(device)
    db.flush()
    # 按设备类型 YAML 端口声明生成端口(RR-P1-04: 端口名/方向/载能来自公开
    # descriptor, 不再使用静态方向表; 热泵按 mode 参数裁剪未启用的冷/热端口)
    for port in _descriptor_ports(spec, params):
        carrier = port["carrier"]
        if carrier not in CARRIER_PORT_TYPE:
            continue  # solar 等环境侧载体不生成可连接端口
        db.add(
            Port(
                device_id=device.id,
                port_type=CARRIER_PORT_TYPE[carrier],
                direction=port["direction"],
                name=port["name"],
                params={},
            )
        )
    refresh_graph_hash(db, graph)
    db.commit()
    return device


def _get_project_device(db: Session, project_id: int, device_id: int) -> tuple[Device, SystemGraph]:
    """取项目工作图内的设备(不存在/跨项目抛 NotFoundError)。"""
    device = db.get(Device, device_id)
    if device is None:
        raise NotFoundError(
            f"设备不存在: {device_id}",
            location={"object_type": "device", "object_id": str(device_id)},
        )
    graph = db.get(SystemGraph, device.graph_id)
    if graph is None or graph.project_id != project_id:
        raise NotFoundError(
            f"设备不属于该项目: {device_id}",
            location={"object_type": "device", "object_id": str(device_id)},
        )
    return device, graph


def update_device(
    db: Session,
    project_id: int,
    device_id: int,
    *,
    name: str | None = None,
    params: dict | None = None,
    position: dict | None = None,
) -> Device:
    """更新设备名称/参数/位置(仅更新提供的字段; 参数重新按注册表校验)。"""
    device, graph = _get_project_device(db, project_id, device_id)
    _ensure_mutable(graph)
    if name is not None:
        if not name.strip():
            raise ModelValidationError(
                "设备名称不能为空",
                code=PARAM_RNG_OUT,
                message_key="ies.diag.param.rng_out",
                params={"param": "name", "value": name, "min": None, "max": None},
                location={"object_type": "device", "object_id": str(device_id), "field": "name"},
            )
        dup = db.scalar(
            select(Device.id).where(
                Device.graph_id == graph.id, Device.name == name, Device.id != device_id
            )
        )
        if dup is not None:
            raise ConflictError(
                f"设备名称重复: {name}",
                params={"device_id": str(device_id), "name": name},
                location={"object_type": "device", "object_id": str(device_id), "field": "name"},
            )
        device.name = name
    if params is not None:
        new_params = dict(params)
        old_layout = device.params.get(_LAYOUT_KEY)
        if position is None and old_layout is not None:
            new_params[_LAYOUT_KEY] = old_layout  # 未显式更新位置时保留既有布局
        if position is not None:
            new_params[_LAYOUT_KEY] = _normalize_position(position)
        new_params[_TYPE_DETAIL_KEY] = _resolve_type_id(device)
        # 缺省必填参数归一为显式 null(与创建路径一致, 避免前端缺省键被校验拒绝)
        for required_name in _REQUIRED_PARAMS.get(_resolve_type_id(device), ()):
            new_params.setdefault(required_name, None)
        diags = validate_device_params(_resolve_type_id(device), new_params, device_id=device.id)
        _raise_diagnostics(diags)
        # 模式类参数变更(热泵 mode)需重同步端口: 按新参数裁剪/补回载体端口,
        # 被裁剪端口的既有连接一并删除(端口语义随模式变化, 原连接不再有效)
        spec = _try_get_device_type(_resolve_type_id(device))
        if spec is not None:
            _sync_ports_for_params(db, device, spec, new_params)
        device.params = new_params
    elif position is not None:
        new_params = dict(device.params)
        new_params[_LAYOUT_KEY] = _normalize_position(position)
        device.params = new_params
    device.updated_at = datetime.now(UTC)
    refresh_graph_hash(db, graph)
    db.commit()
    return device


def delete_device(db: Session, project_id: int, device_id: int) -> None:
    """删除设备(级联删除其端口与连接, 01 §4.2-4.4 归属语义)。"""
    device, graph = _get_project_device(db, project_id, device_id)
    _ensure_mutable(graph)
    port_ids = list(db.scalars(select(Port.id).where(Port.device_id == device_id)))
    if port_ids:
        db.execute(sa.delete(Connection).where(Connection.from_port_id.in_(port_ids)))
        db.execute(sa.delete(Connection).where(Connection.to_port_id.in_(port_ids)))
        db.execute(sa.delete(Port).where(Port.id.in_(port_ids)))
    db.delete(device)
    refresh_graph_hash(db, graph)
    db.commit()


def get_device_ports(db: Session, device_id: int) -> list[Port]:
    """按设备取端口(按 id 排序, 供创建/更新响应返回)。"""
    return list(db.scalars(select(Port).where(Port.device_id == device_id).order_by(Port.id)))


# ---------------------------------------------------------------------------
# 连接写入
# ---------------------------------------------------------------------------


def _check_connection_attrs(attrs: dict) -> tuple[float | None, float, dict]:
    """校验并拆解连接属性: (capacity, loss_rate, params)。

    capacity 非负数值或 None; loss_rate 0..1(01 §4.4 CHECK); 违规抛校验错误。
    """
    capacity = attrs.get("capacity")
    if capacity is not None:
        if (
            isinstance(capacity, bool) or not isinstance(capacity, (int, float))
            or not math.isfinite(float(capacity)) or capacity < 0
        ):
            raise ModelValidationError(
                "连接容量非法",
                code=PARAM_RNG_OUT,
                message_key="ies.diag.param.rng_out",
                params={"param": "capacity", "value": _json_clean(capacity), "min": 0, "max": None},
                location={"object_type": "connection", "field": "capacity"},
            )
    loss_rate = attrs.get("loss_rate", 0)
    if (
        isinstance(loss_rate, bool) or not isinstance(loss_rate, (int, float))
        or not math.isfinite(float(loss_rate)) or not 0 <= loss_rate <= 1
    ):
        raise ModelValidationError(
            "连接损耗率非法",
            code=PARAM_RNG_OUT,
            message_key="ies.diag.param.rng_out",
            params={"param": "loss_rate", "value": _json_clean(loss_rate), "min": 0, "max": 1},
            location={"object_type": "connection", "field": "loss_rate"},
        )
    extra = attrs.get("params", {})
    if not isinstance(extra, dict):
        raise ModelValidationError(
            "连接扩展参数必须是对象",
            code=PARAM_UNIT_MISMATCH,
            message_key="ies.diag.param.unit_mismatch",
            params={"param": "params", "expected": "object", "actual": type(extra).__name__},
            location={"object_type": "connection", "field": "params"},
        )
    return capacity, float(loss_rate), extra


def connect(
    db: Session,
    project_id: int,
    from_port_id: int,
    to_port_id: int,
    attrs: dict | None = None,
) -> Connection:
    """创建连接(源端口 → 汇端口)。

    校验(失败抛校验错误并定位到端口):
    - 两端端口存在且属于同一项目工作图(CONN-PORT-003);
    - 能源类型(port_type)一致(CONN-PORT-001);
    - 方向兼容: 源 direction ∈ {out, bidirectional}, 汇 ∈ {in, bidirectional}(CONN-PORT-002);
    - 禁止自环(CONN-DUP-002)与重复连接(同图同两端同类型, CONN-DUP-001)。
    """
    from_port = db.get(Port, from_port_id)
    if from_port is None:
        raise NotFoundError(
            f"起点端口不存在: {from_port_id}",
            location={"object_type": "port", "object_id": str(from_port_id)},
        )
    to_port = db.get(Port, to_port_id)
    if to_port is None:
        raise NotFoundError(
            f"终点端口不存在: {to_port_id}",
            location={"object_type": "port", "object_id": str(to_port_id)},
        )
    from_dev = db.get(Device, from_port.device_id)
    to_dev = db.get(Device, to_port.device_id)
    from_graph = db.get(SystemGraph, from_dev.graph_id) if from_dev else None
    to_graph = db.get(SystemGraph, to_dev.graph_id) if to_dev else None
    loc = {
        "object_type": "connection",
        "from_port_id": from_port_id,
        "to_port_id": to_port_id,
    }
    if (
        from_graph is None
        or to_graph is None
        or from_graph.id != to_graph.id
        or from_graph.project_id != project_id
    ):
        raise ModelValidationError(
            "端口不属于同一项目图",
            code=CONN_CROSS_PROJECT,
            message_key="ies.diag.conn.cross_project",
            params={
                "from_port_id": from_port_id,
                "to_port_id": to_port_id,
                "project_id": project_id,
            },
            location=loc,
        )
    _ensure_mutable(from_graph)
    if from_port.port_type != to_port.port_type:
        raise ModelValidationError(
            "端口能源类型不一致",
            code=CONN_ENERGY_MISMATCH,
            message_key="ies.diag.conn.energy_mismatch",
            params={
                "from_port_id": from_port_id,
                "from_port_type": from_port.port_type,
                "to_port_id": to_port_id,
                "to_port_type": to_port.port_type,
            },
            location={**loc, "field": "port_type"},
        )
    if (
        from_port.direction not in ("out", "bidirectional")
        or to_port.direction not in ("in", "bidirectional")
    ):
        raise ModelValidationError(
            "端口方向不兼容(连接须为源→汇)",
            code=CONN_DIRECTION_INVALID,
            message_key="ies.diag.conn.direction_invalid",
            params={
                "from_port_id": from_port_id,
                "from_direction": from_port.direction,
                "to_port_id": to_port_id,
                "to_direction": to_port.direction,
            },
            location={**loc, "field": "direction"},
        )
    if from_port_id == to_port_id:
        raise ModelValidationError(
            "禁止自环连接",
            code=CONN_SELF_LOOP,
            message_key="ies.diag.conn.self_loop",
            params={"from_port_id": from_port_id, "to_port_id": to_port_id},
            location=loc,
        )
    conn_type = CONN_TYPE_BY_PORT[from_port.port_type]
    dup = db.scalar(
        select(Connection.id).where(
            Connection.graph_id == from_graph.id,
            Connection.from_port_id == from_port_id,
            Connection.to_port_id == to_port_id,
            Connection.conn_type == conn_type,
        )
    )
    if dup is not None:
        raise ModelValidationError(
            "连接已存在(同图同两端同类型)",
            code=CONN_DUPLICATE,
            message_key="ies.diag.conn.duplicate",
            params={
                "connection_id": dup,
                "from_port_id": from_port_id,
                "to_port_id": to_port_id,
            },
            location=loc,
        )
    capacity, loss_rate, extra = _check_connection_attrs(attrs or {})
    conn = Connection(
        graph_id=from_graph.id,
        from_port_id=from_port_id,
        to_port_id=to_port_id,
        conn_type=conn_type,
        capacity=capacity,
        loss_rate=loss_rate,
        params=extra,
    )
    db.add(conn)
    db.flush()
    refresh_graph_hash(db, from_graph)
    db.commit()
    return conn


def _get_project_connection(db: Session, project_id: int, conn_id: int) -> tuple[Connection, SystemGraph]:
    """取项目工作图内的连接(不存在/跨项目抛 NotFoundError)。"""
    conn = db.get(Connection, conn_id)
    if conn is None:
        raise NotFoundError(
            f"连接不存在: {conn_id}",
            location={"object_type": "connection", "object_id": str(conn_id)},
        )
    graph = db.get(SystemGraph, conn.graph_id)
    if graph is None or graph.project_id != project_id:
        raise NotFoundError(
            f"连接不属于该项目: {conn_id}",
            location={"object_type": "connection", "object_id": str(conn_id)},
        )
    return conn, graph


def disconnect(db: Session, project_id: int, conn_id: int) -> None:
    """断开连接(删除连接行并刷新图内容哈希)。"""
    conn, graph = _get_project_connection(db, project_id, conn_id)
    _ensure_mutable(graph)
    db.delete(conn)
    refresh_graph_hash(db, graph)
    db.commit()


def update_connection(db: Session, project_id: int, conn_id: int, attrs: dict) -> Connection:
    """更新连接属性(capacity/loss_rate/params, 仅更新提供的字段)。"""
    conn, graph = _get_project_connection(db, project_id, conn_id)
    _ensure_mutable(graph)
    if "capacity" in attrs or "loss_rate" in attrs or "params" in attrs:
        # 仅更新提供的字段: 未提供的以现值兜底(注意 DB 数值列为 Decimal, 需清洗)
        merged = dict(attrs)
        merged.setdefault("capacity", _json_clean(conn.capacity))
        merged.setdefault("loss_rate", _json_clean(conn.loss_rate))
        merged.setdefault("params", conn.params)
        capacity, loss_rate, extra = _check_connection_attrs(merged)
        conn.capacity = capacity
        conn.loss_rate = loss_rate
        conn.params = extra
    refresh_graph_hash(db, graph)
    db.commit()
    return conn


# ---------------------------------------------------------------------------
# 图序列化与读取
# ---------------------------------------------------------------------------


def serialize_device(device: Device) -> dict:
    """设备响应结构(device_type 返回完整注册表类型 id, category 为粗分类别)。"""
    return {
        "id": device.id,
        "device_type": _resolve_type_id(device),
        "category": device.device_type,
        "kind": device.kind,
        "name": device.name,
        "description": device.description,
        "params": device.params,
        "model_fidelity": device.model_fidelity,
        "status": device.status,
    }


def serialize_port(port: Port) -> dict:
    """端口响应结构。"""
    return {
        "id": port.id,
        "device_id": port.device_id,
        "port_type": port.port_type,
        "direction": port.direction,
        "name": port.name,
        "capacity": _json_clean(port.capacity),
        "params": port.params,
    }


def serialize_connection(conn: Connection) -> dict:
    """连接响应结构。"""
    return {
        "id": conn.id,
        "from_port_id": conn.from_port_id,
        "to_port_id": conn.to_port_id,
        "conn_type": conn.conn_type,
        "capacity": _json_clean(conn.capacity),
        "loss_rate": _json_clean(conn.loss_rate),
        "params": conn.params,
    }


def get_graph(db: Session, project_id: int) -> dict:
    """读取项目工作图: 拓扑(设备/端口/连接) + 布局对象。

    项目不存在抛 NotFoundError。尚无工作图时是正常流程(新建项目未建模),
    返回显式空态: has_graph=False + 空拓扑结构(graph_id=None), 调用方不得再从
    graph_id 是否为 None 猜测图是否存在。
    """
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(
            f"项目不存在: {project_id}",
            location={"object_type": "project", "object_id": str(project_id)},
        )
    graph = _find_working_graph(db, project_id)
    if graph is None:
        return {
            "has_graph": False,
            "graph_id": None,
            "name": "",
            "graph_hash": "",
            "devices": [],
            "ports": [],
            "connections": [],
            "layout": {"devices": {}},
        }
    devices = _load_devices(db, graph.id)
    ports = _load_ports(db, graph.id)
    conns = _load_connections(db, graph.id)
    layout_devices: dict[str, dict] = {}
    for d in devices:
        pos = (d.params.get(_LAYOUT_KEY) or {}).get("position")
        if isinstance(pos, dict) and "x" in pos and "y" in pos:
            layout_devices[str(d.id)] = {
                "position": {"x": float(pos["x"]), "y": float(pos["y"])}
            }
    return {
        "has_graph": True,
        "graph_id": graph.id,
        "name": graph.name,
        "graph_hash": graph.graph_hash,
        "devices": [serialize_device(d) for d in devices],
        "ports": [serialize_port(p) for p in ports],
        "connections": [serialize_connection(c) for c in conns],
        "layout": {"devices": layout_devices},
    }


# ---------------------------------------------------------------------------
# 拓扑校验
# ---------------------------------------------------------------------------


def validate_topology(graph: dict) -> list[Diagnostic]:
    """拓扑校验(对图序列化结构): 孤立设备/未连接负荷警告, 能源不平衡/重复连接错误。

    - 孤立设备: 无任何连接 → CONN-NODE-001 警告;
    - 未连接负荷: 有连接但无汇入的负荷 → CONN-NODE-001 警告(完全无连接者已按孤立告警, 不重复);
    - 能源不平衡: 某载体端口只有源无汇(或反之) → PARAM-UNIT-003 错误(注册目录近似码);
    - 重复连接: 同图同两端同类型多条 → PARAM-CONF-001 错误(注册目录近似码)。
    """
    diags: list[Diagnostic] = []
    devices = graph.get("devices", [])
    ports = graph.get("ports", [])
    conns = graph.get("connections", [])
    if not devices:
        return diags

    conn_ports: set[int] = set()
    for c in conns:
        conn_ports.add(c["from_port_id"])
        conn_ports.add(c["to_port_id"])

    # 1) 孤立设备(无任何连接)警告
    for d in devices:
        dport_ids = [p["id"] for p in ports if p["device_id"] == d["id"]]
        if not any(pid in conn_ports for pid in dport_ids):
            diags.append(
                make_diag(
                    CONN_NODE_ORPHAN,
                    severity=SEVERITY_WARNING,
                    params={"device_id": d["id"], "device_name": d["name"]},
                    location={"object_type": "device", "object_id": str(d["id"])},
                )
            )

    # 2) 未连接负荷警告(负荷必须有汇入连接)
    for d in devices:
        spec = _try_get_device_type(d["device_type"])
        if spec is None or not spec.is_load:
            continue
        dport_ids = {p["id"] for p in ports if p["device_id"] == d["id"]}
        if not dport_ids or not any(pid in conn_ports for pid in dport_ids):
            continue  # 完全无连接 → 已按孤立设备告警
        if not any(c["to_port_id"] in dport_ids for c in conns):
            diags.append(
                make_diag(
                    CONN_NODE_ORPHAN,
                    severity=SEVERITY_WARNING,
                    params={"device_id": d["id"], "device_name": d["name"]},
                    location={"object_type": "device", "object_id": str(d["id"]), "field": "incoming"},
                )
            )

    # 3) 能源不平衡: 某载体端口只有源或只有汇(双向视为源汇兼具)
    type_dirs: dict[str, set[str]] = {}
    for p in ports:
        type_dirs.setdefault(p["port_type"], set()).add(p["direction"])
    for ptype, dirs in sorted(type_dirs.items()):
        has_source = any(d in ("out", "bidirectional") for d in dirs)
        has_sink = any(d in ("in", "bidirectional") for d in dirs)
        if has_source and has_sink:
            continue
        diags.append(
            make_diag(
                PARAM_UNIT_INCONSISTENT,
                severity=SEVERITY_ERROR,
                params={
                    "param": ptype,
                    "expected": "source_and_sink",
                    "actual": "source_only" if has_source else "sink_only",
                },
                location={"object_type": "system_graph", "field": f"carrier:{ptype}"},
            )
        )

    # 4) 重复连接(同图同两端同连接类型)
    seen: dict[tuple[int, int, str], int] = {}
    for c in sorted(conns, key=lambda c: c["id"]):
        key = (c["from_port_id"], c["to_port_id"], c["conn_type"])
        if key in seen:
            diags.append(
                make_diag(
                    PARAM_CONFLICT,
                    severity=SEVERITY_ERROR,
                    params={
                        "p1": f"connection:{seen[key]}",
                        "p2": f"connection:{c['id']}",
                    },
                    location={
                        "object_type": "connection",
                        "field": "ends",
                        "from_port_id": c["from_port_id"],
                        "to_port_id": c["to_port_id"],
                    },
                )
            )
        else:
            seen[key] = c["id"]
    return diags


def validate_project_model(db: Session, project_id: int) -> list[Diagnostic]:
    """项目模型校验 = 拓扑诊断 + 每台设备参数诊断(类型未注册 → CONN-TYPE-002)。"""
    graph = get_graph(db, project_id)
    diags = validate_topology(graph)
    for dev in graph["devices"]:
        try:
            diags.extend(
                validate_device_params(dev["device_type"], dev["params"], device_id=dev["id"])
            )
        except NotFoundError:
            diags.append(
                make_diag(
                    CONN_TYPE_UNREGISTERED,
                    severity=SEVERITY_ERROR,
                    params={"device_id": dev["id"], "type_id": dev["device_type"]},
                    location={
                        "object_type": "device",
                        "object_id": str(dev["id"]),
                        "field": "device_type",
                    },
                )
            )
    # Diagnostic 深度不可变: 项目上下文经派生方法生成新对象
    return [d.with_context(project_id=str(project_id)) for d in diags]
