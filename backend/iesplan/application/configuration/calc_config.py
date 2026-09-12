"""计算配置用例(application/configuration)。

``api/config.py`` 经本模块访问计算配置，依赖方向
``api → application.configuration.calc_config → {configuration, project,
model, audit} 域公开门面 + application.projects.content_objects``；
不导入 ORM、不导入其他域内部模块、不调用 ``services.*``。

职责（与旧 ``services.config`` 一致）：
- 工作图加载、默认配置生成、配置读写（读用例不提交）；
- ``save_config`` 为顶层事务拥有者（成功 ``db.commit``，失败
  ``db.rollback``；保存同时同步当前草稿内容的 ``calc_config`` 节，
  任务级参数 ``task_params`` 原样保留）；
- 设备/算法注册表相关规则（设备类型解析、默认配置生成、设备参数元数据、
  算法校验/容差/算法元数据）归属本层（域源码纯度门禁；configuration 域仅
  保留纯规则），行为与原领域实现一致；
- ``row_to_config`` 直用 configuration 域唯一实现（本模块不保留从
  service 复制的映射表）。

本模块不新增校验/哈希/回退。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from iesplan import audit as audit_domain
from iesplan import configuration as configuration_domain
from iesplan import model as model_domain
from iesplan import project as project_domain
from iesplan.application.projects.content_objects import (
    load_content_object,
    store_content_object,
)
from iesplan.configuration import row_to_config
from iesplan.configuration.contracts import CalcConfigRecord
from iesplan.core.contracts import ParameterSpec
from iesplan.core.diagnostics import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    Diagnostic,
    make_diag,
)
from iesplan.core.errors import ConflictError, NotFoundError
from iesplan.devices import (
    DeviceModelDocument as DeviceTypeSpec,
)
from iesplan.devices import (
    get_device as get_device_type,
)
from iesplan.devices.contracts2 import PropertySpec
from iesplan.engines import (
    DEFAULT_ALGORITHM,
    AlgorithmSpec,
    get_algorithm,
    list_algorithms,
)
from iesplan.project.contracts import ProjectRecord

__all__ = [
    "get_config",
    "get_default_config",
    "list_algorithms_meta",
    "load_work_graph",
    "parameter_metadata",
    "row_to_config",
    "save_config",
    "validate_config",
]


# ---------------------------------------------------------------------------
# 设备/算法注册表规则（归属 application.configuration.calc_config）
# ---------------------------------------------------------------------------
#
# 以下规则依赖 iesplan.devices / iesplan.engines 注册表，不得下沉
# configuration 域（域源码纯度门禁）；行为与原领域实现一致。


# ---------------------------------------------------------------------------
# 图/设备类型解析
# ---------------------------------------------------------------------------


def resolve_device_type(device_type: str) -> DeviceTypeSpec | None:
    """按 2.0 稳定设备 ID 解析设备规格；未注册返回 None。"""
    try:
        return get_device_type(device_type)
    except NotFoundError:
        return None


def normalize_devices(graph: dict) -> list[dict]:
    """把系统图 dict 归一化为设备清单(兼容 DB 行与规划模板两种形态)。

    graph: {"devices": [{id, device_type|type, kind|is_new, name, params}, ...]}
    """
    devices: list[dict] = []
    for dev in graph.get("devices", []) or []:
        if not isinstance(dev, dict):
            continue
        kind = dev.get("kind")
        if kind is None:
            kind = "new" if dev.get("is_new") else "existing"
        devices.append(
            {
                "id": dev.get("id") or dev.get("device_id"),
                "device_type": dev.get("device_type") or dev.get("type") or "",
                "kind": kind,
                "name": dev.get("name") or "",
                "params": dict(dev.get("params") or {}),
            }
        )
    return devices


# ---------------------------------------------------------------------------
# 默认配置生成(纯构造: 调用方先备好工作图与币种)
# ---------------------------------------------------------------------------


def _default_parameters(graph: dict) -> dict:
    """设备参数当前值 = 注册表默认值叠加设备行参数(设备行参数优先)。

    解析优先使用设备行 params['type_detail'](模型服务写入的完整 2.0 注册表
    ID), 回退到 device_type 短名; 未注册返回 None 的设备跳过。存量与新增
    设备均以注册表 property value 打底, 设备行参数覆盖。
    """
    devices: dict = {}
    for dev in normalize_devices(graph):
        params = dev["params"] or {}
        type_id = params.get("type_detail") or dev["device_type"]
        spec = resolve_device_type(type_id)
        if spec is None:
            continue
        merged = {name: p.value for name, p in spec.properties.items()}
        merged.update(dev["params"])  # 设备行参数覆盖注册表默认
        devices[str(dev["id"]) if dev["id"] is not None else dev["name"]] = merged
    return {
        "devices": devices,
        "economic": {k: v["default"] for k, v in configuration_domain.ECONOMIC_PARAM_SPECS.items()},
        "environmental": {k: v["default"] for k, v in configuration_domain.ENVIRONMENTAL_PARAM_SPECS.items()},
    }


def _default_variables(graph: dict) -> list[dict]:
    """默认不从设备技术常量猜测规划变量；规划配置必须显式声明。"""
    return []


def build_default_config(graph: dict, currency: str = "CNY") -> dict:
    """生成默认计算配置(见 services 收敛前的模块 docstring 结构说明)。

    参数:
        graph: 系统模型图 dict(与 validate_config 同构的设备清单)。
        currency: 经济参数币种(调用方按项目币种传入, 缺省 CNY)。
    """
    params = _default_parameters(graph)
    params["economic"]["currency"] = currency or "CNY"
    algo = get_algorithm(DEFAULT_ALGORITHM)
    return {
        "parameters": params,
        "variables": _default_variables(graph),
        "objectives": [{"metric": "irr_after_tax", "direction": "max", "weight": 1.0}],
        # 默认不允许未满足负荷(领域模型 §规划、财务与计算配置)
        "constraints": [
            {"type": "predefined", "payload": {"kind": "load_satisfaction", "allow_shed": False}}
        ],
        "algorithm": {"mode": "auto", "name": DEFAULT_ALGORITHM},
        "irr_floor": 0.08,  # 最低税后项目投资 IRR 硬约束(默认 8%)
        "tolerances": {
            name: p.default for name, p in algo.parameters.items() if name in ("gap_rel", "time_limit_s")
        },
        "random_seed": 42,
    }



def load_work_graph(db: Session, project_id: int) -> dict:
    """加载项目当前工作图(设备清单)。

    优先取 current_draft_id 关联的工作图; 未关联时取该项目最近一张工作图;
    无任何图返回空设备清单(默认配置仍可生成)。
    """
    graph = None
    if project_id is not None:
        proj = project_domain.get_project(db, project_id)
        if proj is not None and proj.current_draft_id is not None:
            graph = model_domain.find_graph_by_draft(db, project_id, proj.current_draft_id)
        if graph is None:
            graph = model_domain.find_latest_working_graph(db, project_id)
    if graph is None:
        return {"devices": []}
    rows = model_domain.list_devices(db, graph.id)
    return {
        "devices": [
            {
                "id": d.id,
                "device_type": d.device_type,
                "kind": d.kind,
                "name": d.name,
                "params": dict(d.params or {}),
            }
            for d in rows
        ]
    }


def get_default_config(db: Session, project_id: int) -> dict:
    """重新生成默认计算配置（不保存；币种按项目币种，缺省 CNY）。"""
    proj = project_domain.get_project(db, project_id)
    currency = proj.currency if proj is not None and proj.currency else "CNY"
    graph = load_work_graph(db, project_id)
    return build_default_config(graph, currency)


#: 完整校验形状（与领域 _validate_structure 的提前返回条件一致）：
#: 设备/算法分支依赖完整结构，形状损坏时仅返回领域诊断，不再追加分支诊断。
_CONFIG_SHAPES: dict[str, type] = {
    "parameters": dict,
    "variables": list,
    "objectives": list,
    "constraints": list,
    "algorithm": dict,
}


def _is_number(value: object) -> bool:
    """数值检查（int/float，布尔除外；与领域同名辅助语义一致）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_device_parameters(config: dict, devices: list[dict], diags: list[Diagnostic]) -> None:
    """设备参数校验（设备分支）：设备参数按注册表规格（类型/范围）检查。

    由 ``validate_config`` 在领域纯规则之后调用；行为与原领域实现一致。
    """
    params = config["parameters"]
    if not isinstance(params, dict):
        diags.append(
            make_diag(
                "SYS-CFG-001",
                SEVERITY_ERROR,
                params={"field": "parameters", "reason": "参数段必须是对象"},
                location={"object_type": "config", "object_id": "", "field": "parameters"},
            )
        )
        return
    device_params = params.get("devices", {})
    for dev in devices:
        dev_params = dev["params"] or {}
        type_id = dev_params.get("type_detail") or dev["device_type"]
        spec = resolve_device_type(type_id)
        key = str(dev["id"]) if dev["id"] is not None else dev["name"]
        if spec is None:
            continue
        cur = device_params.get(key, {})
        if not isinstance(cur, dict):
            diags.append(
                make_diag(
                    "SYS-CFG-001",
                    SEVERITY_ERROR,
                    params={"device": key, "reason": "设备参数必须是对象"},
                    location={"object_type": "device", "object_id": key, "field": "params"},
                )
            )
            continue
        for pname, pspec in spec.properties.items():
            value = cur.get(pname, pspec.value)
            if isinstance(pspec.value, (int, float)) and not isinstance(pspec.value, bool):
                if not _is_number(value):
                    diags.append(
                        make_diag(
                            "PARAM-UNIT-002",
                            SEVERITY_ERROR,
                            params={"param": pname, "value": repr(value), "expected": "数值"},
                            location={"object_type": "device", "object_id": key, "field": pname},
                        )
                    )
                else:
                    lo, hi = pspec.minimum, pspec.maximum
                    if (lo is not None and value < lo) or (hi is not None and value > hi):
                        diags.append(
                            make_diag(
                                "PARAM-RNG-003",
                                SEVERITY_ERROR,
                                params={"param": pname, "value": value, "min": lo, "max": hi},
                                location={"object_type": "device", "object_id": key, "field": pname},
                            )
                        )


def _validate_device_refs(config: dict, devices: list[dict], diags: list[Diagnostic]) -> None:
    """变量设备引用校验（设备分支）：device_ref 必须指向图中存在的设备。

    仅检查经纯规则类型校验通过的变量（类型非法已报错，不级联引用诊断，
    与原领域实现一致）；由 ``validate_config`` 在纯规则之后调用。
    """
    devices_by_key = {str(dev["id"]) if dev["id"] is not None else dev["name"]: dev for dev in devices}
    variables = config["variables"]
    if not isinstance(variables, list):
        return
    for idx, v in enumerate(variables):
        if not isinstance(v, dict):
            continue
        if v.get("type") not in configuration_domain.VARIABLE_TYPES:
            continue
        name = v.get("name")
        loc = {
            "object_type": "variable",
            "object_id": str(name or ""),
            "field": f"variables[{idx}]",
        }
        dev_ref = v.get("device_ref")
        if dev_ref is not None and str(dev_ref) not in devices_by_key:
            diags.append(
                make_diag(
                    "CONN-TYPE-002",
                    SEVERITY_ERROR,
                    params={"device_id": str(dev_ref), "type_id": ""},
                    location=loc,
                )
            )


def validate_config(config: dict, graph: dict) -> list:
    """校验计算配置（不保存）：领域纯规则 + 设备/算法分支，全量实现。

    诊断集合与原领域全量实现一致（设备/经济/环境参数、变量、目标、约束、
    IRR、算法、容差）；分支诊断追加在纯规则诊断之后。
    """
    diags = configuration_domain.validate_config(config, graph)
    if not isinstance(config, dict) or any(
        not isinstance(config.get(key), expected) for key, expected in _CONFIG_SHAPES.items()
    ):
        return diags  # 结构损坏：领域已提前返回，分支依赖完整结构不再追加
    normalized = configuration_domain.normalize_config(config)
    devices = normalize_devices(graph)
    _validate_device_parameters(normalized, devices, diags)
    _validate_device_refs(normalized, devices, diags)
    _validate_algorithm(normalized, diags)
    _validate_tolerances(normalized, diags)
    return diags


def _current_draft_revision(db: Session, project_id: int) -> int:
    """当前草稿修订号; 项目尚无草稿时按 1 处理(领域模型 §项目聚合 初始草稿 revision=1)。"""
    proj = project_domain.get_project(db, project_id)
    if proj is None:
        raise NotFoundError(
            f"项目不存在: {project_id}",
            code="RES-MISS-003",
            message_key="ies.diag.res.not_found",
            params={"project_id": project_id},
        )
    if proj.current_draft_id is not None:
        draft = project_domain.get_draft(db, proj.current_draft_id)
        if draft is not None:
            return draft.revision
    return 1


def _sync_draft_config(
    db: Session,
    proj: ProjectRecord,
    config: dict,
    row: CalcConfigRecord,
) -> None:
    """把已保存配置同步进当前草稿内容的 calc_config 节(不递增草稿修订)。

    计算快照装配与项目包导出以草稿内容为权威输入; 若不同步, 保存的配置
    不会进入快照/导出包(配置语义丢失)。任务级参数(task_params)属于任务
    提交时的覆盖项, 原样保留不覆盖。
    """
    if proj.current_draft_id is None:
        return
    draft = project_domain.get_draft(db, proj.current_draft_id)
    if draft is None:
        return
    # 内容对象缺失或损坏时直接抛出加载原错误, 不回退初始骨架
    # (宪法 §13: 对象缺失或不可读返回实际错误, 禁止旧副本回退)。
    content = load_content_object(db, draft.content_object_id)
    old_calc = content.get("calc_config") or {}
    content["calc_config"] = {
        "params": dict(config.get("parameters") or {}),
        "variables": list(config.get("variables") or []),
        "objectives": list(config.get("objectives") or []),
        "constraints": list(config.get("constraints") or []),
        "algorithm": dict(config.get("algorithm") or {}),
        "solver": row.solver,
        "tolerances": dict(config.get("tolerances") or {}),
        "random_seed": config.get("random_seed"),
        "irr_floor": config.get("irr_floor"),
    }
    if isinstance(old_calc.get("task_params"), dict):
        content["calc_config"]["task_params"] = old_calc["task_params"]
    content_object_id = store_content_object(db, content)
    project_domain.update_draft_content_ref(db, draft.id, content_object_id)


def save_config(
    db: Session,
    project_id: int,
    config: dict,
    expected_revision: int,
    *,
    user_id: int | None = None,
) -> CalcConfigRecord:
    """保存计算配置(与草稿修订绑定, 乐观锁; 冻结行则新建版本行, 经 configuration 域)。

    参数:
        db: 数据库会话。
        project_id: 项目 id。
        config: 计算配置 dict。
        expected_revision: 期望的草稿修订号; 与实际修订不符抛 ConflictError。
        user_id: 修改者; None 时回退到项目 owner(认证接线前的兼容路径)。

    返回:
        保存后的 CalcConfig 行。
    """
    proj = project_domain.get_project(db, project_id)
    if proj is None:
        raise NotFoundError(
            f"项目不存在: {project_id}",
            code="RES-MISS-003",
            message_key="ies.diag.res.not_found",
            params={"project_id": project_id},
        )
    current_revision = _current_draft_revision(db, project_id)
    if expected_revision != current_revision:
        raise ConflictError(
            f"草稿修订冲突: 期望 {expected_revision}, 当前 {current_revision}",
            params={
                "expected_revision": expected_revision,
                "current_revision": current_revision,
            },
        )
    config = configuration_domain.normalize_config(config)
    existing = [
        c
        for c in configuration_domain.list_calc_configs(db, project_id)
        if c.name == configuration_domain.DEFAULT_CONFIG_NAME
    ]
    latest = max(existing, key=lambda c: c.version, default=None)
    if latest is not None and latest.status == "frozen":
        latest = None  # 冻结行不可修改(01 §6.1 触发器语义), 新建版本行
    algo_mode = config.get("algorithm", {}).get("mode", "auto")
    algo_name = config.get("algorithm", {}).get("name")
    values = {
        "params": config["parameters"],
        "variables": config["variables"],
        "objectives": config["objectives"],
        "constraints": config["constraints"],
        "min_irr": config.get("irr_floor"),
        "algorithm": None
        if algo_mode == "auto"
        else configuration_domain.ALGO_DB_CLASS.get(algo_name or "", algo_name),
        "solver": configuration_domain.SOLVER_ID,
        "tolerances": config.get("tolerances", {}),
        "random_seed": config.get("random_seed"),
    }
    actor = user_id or proj.owner_id
    try:
        if latest is None:
            row = configuration_domain.create_calc_config(
                db,
                project_id=project_id,
                name=configuration_domain.DEFAULT_CONFIG_NAME,
                params=values["params"],
                variables=values["variables"],
                objectives=values["objectives"],
                constraints=values["constraints"],
                tolerances=values["tolerances"],
                updated_by=actor,
                min_irr=values["min_irr"],
                algorithm=values["algorithm"],
                solver=values["solver"],
                random_seed=values["random_seed"],
            )
        else:
            row = configuration_domain.update_calc_config(
                db, latest.id, values=values, updated_by=actor
            )
        # 0.2.0 B4: 配置保存属"项目/数据/计算配置"关键变更(宪法 §16), 保留不可变
        # 最小化脱敏审计(只记版本/变量数/目标/算法, 不复制完整配置)
        audit_domain.append_entry(
            db,
            actor_id=user_id or proj.owner_id,
            action="config.saved",
            entity_type="calc_config",
            entity_id=row.id,
            actor_type="user",
            extra={
                "project_id": project_id,
                "version": row.version,
                "status": row.status,
                "variables": len(config.get("variables") or []),
                "objectives": len(config.get("objectives") or []),
                "constraints": len(config.get("constraints") or []),
                "algorithm": row.algorithm,
                "random_seed": config.get("random_seed"),
            },
        )
        # 同步当前草稿内容的 calc_config 节(快照装配/项目包导出以草稿内容为
        # 权威输入, 不更新则保存的配置不进入计算快照与导出包)
        _sync_draft_config(db, proj, config, row)
        db.commit()
    except Exception:
        db.rollback()
        raise
    # row 为 configuration 域记录(已物化值对象), 无需 ORM refresh。
    return row


def get_config(db: Session, project_id: int) -> dict:
    """读取当前计算配置(未保存时返回生成的默认配置, 不带版本)。

    返回:
        {"config": dict, "meta": dict, "version": int|None, "status": str, "updated_at": str|None}
    """
    if project_domain.get_project(db, project_id) is None:
        raise NotFoundError(
            f"项目不存在: {project_id}",
            code="RES-MISS-003",
            message_key="ies.diag.res.not_found",
            params={"project_id": project_id},
        )
    graph = load_work_graph(db, project_id)
    meta = parameter_metadata(graph)
    configs = [
        c
        for c in configuration_domain.list_calc_configs(db, project_id)
        if c.name == configuration_domain.DEFAULT_CONFIG_NAME
    ]
    row = max(configs, key=lambda c: c.version, default=None)
    if row is None:
        return {
            "config": get_default_config(db, project_id),
            "meta": meta,
            "version": None,
            "status": "draft",
            "updated_at": None,
        }
    return {
        "config": configuration_domain.row_to_config(row),
        "meta": meta,
        "version": row.version,
        "status": row.status,
        "updated_at": row.updated_at,
    }


# ---------------------------------------------------------------------------
# 算法分支校验与元数据（注册表相关，行为与原领域实现一致）
# ---------------------------------------------------------------------------


def _validate_algorithm(config: dict, diags: list[Diagnostic]) -> None:
    """算法校验: 手动模式检查注册与能力兼容; auto 不查能力(宪法 §4 + 领域模型 §规划、财务与计算配置)。"""
    algo = config["algorithm"]
    mode = algo.get("mode", "auto")
    if mode == "auto":
        return
    name = algo.get("name") or DEFAULT_ALGORITHM
    loc = {"object_type": "algorithm", "object_id": name, "field": "algorithm.name"}
    try:
        spec = get_algorithm(name)
    except NotFoundError:
        diags.append(
            make_diag(
                "CONN-TYPE-002",
                SEVERITY_ERROR,
                params={"device_id": "", "type_id": name},
                location=loc,
            )
        )
        return
    # 能力需求推导
    needs: set[str] = set()
    if config.get("irr_floor") is not None:
        needs.add("irr_hard_constraint")  # 最低 IRR 硬约束
    objectives = config.get("objectives") or []
    if len(objectives) > 1:
        needs.add("multi_objective")
    variables = config.get("variables") or []
    if any(isinstance(v, dict) and v.get("type") in ("integer", "boolean", "enum") for v in variables):
        needs.add("milp")  # 离散变量需要 MILP 求解能力
    if any(isinstance(v, dict) and v.get("type") == "continuous" for v in variables):
        needs.add("capacity_design")  # 容量设计
    missing = sorted(needs - set(spec.capabilities))
    if missing:
        diags.append(
            make_diag(
                "SYS-CFG-001",
                SEVERITY_ERROR,
                params={
                    "algorithm": name,
                    "missing_capabilities": missing,
                    "reason": "算法不支持当前配置所需能力",
                },
                location=loc,
            )
        )


def _validate_tolerances(config: dict, diags: list[Diagnostic]) -> None:
    """容差校验: 键必须是算法注册参数, 数值在其界内; 未知键给警告。"""
    tolerances = config.get("tolerances", {})
    if not isinstance(tolerances, dict):
        diags.append(
            make_diag(
                "SYS-CFG-001",
                SEVERITY_ERROR,
                params={"field": "tolerances", "reason": "容差必须是对象"},
                location={"object_type": "config", "object_id": "", "field": "tolerances"},
            )
        )
        return
    name = config.get("algorithm", {}).get("name") or DEFAULT_ALGORITHM
    try:
        spec: AlgorithmSpec = get_algorithm(name)
    except NotFoundError:
        spec = get_algorithm(DEFAULT_ALGORITHM)  # 算法非法时按默认算法规格兜底
    for key, value in tolerances.items():
        loc = {"object_type": "config", "object_id": "", "field": f"tolerances.{key}"}
        p = spec.parameters.get(key)
        if p is None:
            diags.append(
                make_diag(
                    "SYS-CFG-001",
                    SEVERITY_WARNING,
                    params={"param": key, "reason": "非当前算法注册参数, 将被忽略"},
                    location=loc,
                )
            )
            continue
        if not _is_number(value):
            diags.append(
                make_diag(
                    "PARAM-UNIT-002",
                    SEVERITY_ERROR,
                    params={"param": key, "value": repr(value), "expected": "数值"},
                    location=loc,
                )
            )
        elif (p.min is not None and value < p.min) or (p.max is not None and value > p.max):
            diags.append(
                make_diag(
                    "PARAM-RNG-003",
                    SEVERITY_ERROR,
                    params={"param": key, "value": value, "min": p.min, "max": p.max},
                    location=loc,
                )
            )



def _param_meta(p: ParameterSpec) -> dict:
    """参数规格 -> 元数据(单位/范围/默认/帮助键/枚举)。"""
    return {
        "unit": p.unit,
        "min": p.min,
        "max": p.max,
        "default": p.default,
        "enum": list(p.enum) if p.enum else None,
        "is_optimizable": p.is_optimizable,
        "stock_or_addition": p.stock_or_addition,
        "help_key": p.help_key,
    }


def _property_meta(p: PropertySpec) -> dict:
    """设备 2.0 技术常量元数据。"""
    return {
        "unit": p.unit,
        "min": p.minimum,
        "max": p.maximum,
        "default": p.value,
    }



def parameter_metadata(graph: dict) -> dict:
    """生成参数元数据(每个参数的单位/范围/默认值/帮助键, 供前端渲染)。

    graph: 与 validate_config 同构的设备清单 dict。
    """
    device_meta: dict[str, dict] = {}
    for dev in normalize_devices(graph):
        params = dev["params"] or {}
        type_id = params.get("type_detail") or dev["device_type"]
        spec = resolve_device_type(type_id)
        if spec is None:
            continue
        key = str(dev["id"]) if dev["id"] is not None else dev["name"]
        device_meta[key] = {name: _property_meta(p) for name, p in spec.properties.items()}
    return {
        "parameters": {
            "devices": device_meta,
            "economic": {
                name: {k: v for k, v in spec.items()} for name, spec in configuration_domain.ECONOMIC_PARAM_SPECS.items()
            },
            "environmental": {
                name: {k: v for k, v in spec.items()} for name, spec in configuration_domain.ENVIRONMENTAL_PARAM_SPECS.items()
            },
        },
    }



def list_algorithms_meta() -> list[dict]:
    """算法注册表列表(含参数规格元数据), 供 /api/registry/algorithms。"""
    return [
        {
            "algo_id": spec.algo_id,
            "version": spec.version,
            "name_zh": spec.name_zh,
            "name_en": spec.name_en,
            "capabilities": list(spec.capabilities),
            "help_topic": spec.help_topic,
            "parameters": [_param_meta(p) | {"name": n} for n, p in spec.parameters.items()],
        }
        for spec in list_algorithms()
    ]
