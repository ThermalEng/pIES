"""计算配置服务: 默认配置生成、保存、校验与读取。

依据 架构宪法 §4 后端模块与职责(modeling/assembly) + 领域模型 §规划、财务与计算配置。

本模块是计算配置域的唯一写入单元:

- 默认配置基于系统模型(当前工作图)设备清单与受控注册表生成:
  设备参数取注册表默认值(叠加设备行参数), 新建设备的容量类参数
  (is_optimizable)生成 continuous 优化变量, 存量设备容量固定不生成变量;
- 默认目标 = 税后项目投资 IRR 最大化; 默认最低 IRR 硬约束 0.08
  (最低税后项目投资 IRR 是不可被目标权重抵消的硬约束,
  与折现率是两个独立字段);
- 保存与草稿修订绑定(乐观锁): expected_revision 与当前草稿修订不一致抛
  ConflictError;
- 校验: 变量类型/初始值在界内、目标合法、约束表达式用 expression.parse_expr
  做解析+量纲+范围校验、IRR 硬约束与折现率独立、算法能力兼容
  (mode=auto 不做能力检查)。

计算配置结构(JSON):
{
  "parameters": {
    "devices": {<device_id>: {<param>: <value>}},     # 设备参数当前值
    "economic": {discount_rate, tax_rate, ...},       # 经济参数
    "environmental": {emission_factor_grid, ...},     # 环境参数
  },
  "variables": [{name, type, initial, min, max, device_ref, param, unit}],
  "objectives": [{metric, direction, weight}],
  "constraints": [{type: predefined|expression, payload}],
  "algorithm": {"mode": "auto"|"manual", "name": <注册表算法 id>},
  "irr_floor": Decimal,          # 最低税后 IRR 硬约束(0..1, 独立顶层字段)
  "tolerances": {...},           # 容差(兼容输入键 "tolerance")
  "random_seed": int | None,
}
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from iesplan.core.diagnostics import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    Diagnostic,
    make_diag,
)
from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.core.expression import (
    Dimensions,
    ExpressionError,
    parse_expr,
)
from iesplan.core.contracts import ParameterSpec
from iesplan.engines.registry import (
    DEFAULT_ALGORITHM,
    AlgorithmSpec,
    get_algorithm,
    list_algorithms,
)
from iesplan.devices import (
    DeviceModelDescriptor as DeviceTypeSpec,
    get_device_descriptor as get_device_type,
    list_device_descriptors as list_device_types,
)
from iesplan.core.units import UnitError, dims_of
from iesplan.db import SessionLocal
from iesplan.models.audit import AuditLog
from iesplan.models.calc import CalcConfig
from iesplan.models.model import Device, SystemGraph
from iesplan.models.project import Draft, Project

# ---------------------------------------------------------------------------
# 常量: 配置结构 / 目标 / 预定义约束
# ---------------------------------------------------------------------------

#: 默认配置名(每项目一个当前配置行, 版本化)
DEFAULT_CONFIG_NAME: Final[str] = "default"

#: 变量类型白名单(连续/整数/枚举/布尔; 宪法 §4 + 领域模型 §规划、财务与计算配置)
VARIABLE_TYPES: Final[tuple[str, ...]] = ("continuous", "integer", "enum", "boolean")

#: 目标指标字典(领域模型 §规划、财务与计算配置 可选目标; id -> 中文说明)
OBJECTIVE_METRICS: Final[dict[str, str]] = {
    "irr_after_tax": "税后项目投资 IRR(主目标)",
    "npv_after_tax": "税后 NPV",
    "co2_emissions": "年 CO2 排放",
    "annual_energy_cost": "年购能费用",
    "pv_self_consumption": "光伏自用率",
}

#: 预定义约束种类(简单模式使用预定义约束; 宪法 §4 + 领域模型 §规划、财务与计算配置)
PREDEFINED_CONSTRAINT_KINDS: Final[dict[str, str]] = {
    "load_satisfaction": "负荷必须完全满足(默认不允许削减)",
    "capacity_limits": "设备容量上限",
    "co2_cap": "年碳排放上限",
    "energy_cost_cap": "年购能费用上限",
}

#: 算法注册表 id -> calc_configs.algorithm 列短名
#: mc_sampling 属采样/不确定性类而非求解类, 归入 'custom'。
ALGO_DB_CLASS: Final[dict[str, str]] = {
    "ies.algo.milp_hybrid": "milp",
    "ies.algo.lp_relax": "lp",
    "ies.algo.mc_sampling": "custom",
}
_DB_CLASS_TO_ALGO: Final[dict[str, str]] = {v: k for k, v in ALGO_DB_CLASS.items()}

#: 求解器标识(契约第3节: scipy>=1.13 的 HiGHS)
SOLVER_ID: Final[str] = "highs"

#: 随机种子允许范围(与注册表算法参数 seed 一致)
_SEED_MAX: Final[int] = 2**31 - 1

#: 变量名必须是合法 Python 标识符(约束表达式经 ast 解析, 变量名被直接引用)
_IDENT_RE: Final[re.Pattern] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 经济参数规格(单位/范围/默认值/帮助键; 元数据供前端渲染)
ECONOMIC_PARAM_SPECS: Final[dict[str, dict]] = {
    "discount_rate": {
        "unit": "-", "min": 0.0, "max": 1.0, "default": 0.08,
        "help_key": "help.param.economic.discount_rate", "is_optimizable": False,
    },
    "tax_rate": {
        "unit": "-", "min": 0.0, "max": 1.0, "default": 0.25,
        "help_key": "help.param.economic.tax_rate", "is_optimizable": False,
    },
    "project_years": {
        "unit": "a", "min": 1, "max": 50, "default": 20,
        "help_key": "help.param.economic.project_years", "is_optimizable": False,
    },
    "depreciation_years": {
        "unit": "a", "min": 1, "max": 50, "default": 10,
        "help_key": "help.param.economic.depreciation_years", "is_optimizable": False,
    },
    "currency": {
        "unit": "-", "default": "CNY", "enum": ("CNY", "USD"),
        "help_key": "help.param.economic.currency", "is_optimizable": False,
    },
}

#: 环境参数规格(排放因子, 排放边界; 领域模型 §规划、财务与计算配置)
ENVIRONMENTAL_PARAM_SPECS: Final[dict[str, dict]] = {
    "emission_factor_grid": {
        "unit": "tCO2/MWh", "min": 0.0, "max": 10.0, "default": 0.581,
        "help_key": "help.param.environmental.emission_factor_grid", "is_optimizable": False,
    },
    "emission_factor_gas": {
        "unit": "tCO2/万m³", "min": 0.0, "max": 50.0, "default": 2.0,
        "help_key": "help.param.environmental.emission_factor_gas", "is_optimizable": False,
    },
}

# 设备类型短名 -> 注册表 id(兼容 models.devices.device_type 的 CHECK 短名,
# 注册表 id 可直接使用; 未映射的短名视为无注册表规格)
_DEVICE_SHORT_ALIASES: Final[dict[str, str]] = {
    "pv": "ies.device.pv",
    "storage": "ies.device.battery",
    "boiler": "ies.device.gas_boiler",
    "chiller": "ies.device.electric_chiller",
    "load": "ies.device.electric_load",
}


# ---------------------------------------------------------------------------
# 图/设备类型解析
# ---------------------------------------------------------------------------


def resolve_device_type(device_type: str) -> DeviceTypeSpec | None:
    """解析设备类型规格: 注册表 id 优先, 兼容短名; 无法识别返回 None。"""
    try:
        return get_device_type(device_type)
    except NotFoundError:
        pass
    full = _DEVICE_SHORT_ALIASES.get(device_type)
    if full is not None:
        try:
            return get_device_type(full)
        except NotFoundError:
            pass
    # 后缀匹配(如 "grid_connection" -> "ies.device.grid_connection")
    for spec in list_device_types():
        if spec.type_id.rsplit(".", 1)[-1] == device_type:
            return spec
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


def load_work_graph(db: Session, project_id: int) -> dict:
    """加载项目当前工作图(设备清单)。

    优先取 current_draft_id 关联的工作图; 未关联时取该项目最近一张工作图;
    无任何图返回空设备清单(默认配置仍可生成)。
    """
    graph = None
    if project_id is not None:
        if (proj := db.get(Project, project_id)) is not None and proj.current_draft_id is not None:
            graph = db.scalar(
                select(SystemGraph).where(
                    SystemGraph.project_id == project_id,
                    SystemGraph.draft_id == proj.current_draft_id,
                )
            )
        if graph is None:
            graph = db.scalar(
                select(SystemGraph)
                .where(SystemGraph.project_id == project_id, SystemGraph.draft_id.is_not(None))
                .order_by(SystemGraph.id.desc())
                .limit(1)
            )
    if graph is None:
        return {"devices": []}
    rows = db.scalars(select(Device).where(Device.graph_id == graph.id)).all()
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


def _device_max_capacity(dev: dict, spec: DeviceTypeSpec, param: ParameterSpec) -> float | None:
    """变量上界: 优先取设备 max_* 容量参数当前值(如 max_capacity_kwp), 否则取注册表 max。"""
    for name, p in spec.parameters.items():
        if p.stock_or_addition != "addition":
            continue
        # 形如 max_*_kw / max_capacity_* 的上限参数(不必是优化变量)
        if re.match(r"^max_", name):
            val = dev["params"].get(name, p.default)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return float(val)
    return param.max


# ---------------------------------------------------------------------------
# 默认配置生成
# ---------------------------------------------------------------------------


def _safe_var_name(device: dict, param_name: str) -> str:
    """生成合法标识符变量名: 设备名净化后加参数名, 名称不可用时退回 dev<id>_<param>。"""
    base = re.sub(r"[^A-Za-z0-9_]", "_", device.get("name") or "").strip("_")
    if not base:
        base = f"dev{device.get('id') or 0}"
    return f"{base}_{param_name}"


def _default_parameters(graph: dict) -> dict:
    """设备参数当前值 = 注册表默认值叠加设备行参数(设备行参数优先)。

    存量与新增设备的容量参数均以注册表 existing_default/default 打底:
    - 存量: 设备行参数即"容量固定"的当前值;
    - 新增: 当前值=注册表默认(容量参数为 0), 由变量参与优化。
    """
    devices: dict = {}
    for dev in normalize_devices(graph):
        spec = resolve_device_type(dev["device_type"])
        if spec is None:
            continue
        merged = {name: p.default for name, p in spec.parameters.items()}
        merged.update(dev["params"])  # 设备行参数覆盖注册表默认
        devices[str(dev["id"]) if dev["id"] is not None else dev["name"]] = merged
    return {
        "devices": devices,
        "economic": {k: v["default"] for k, v in ECONOMIC_PARAM_SPECS.items()},
        "environmental": {k: v["default"] for k, v in ENVIRONMENTAL_PARAM_SPECS.items()},
    }


def _default_variables(graph: dict) -> list[dict]:
    """默认变量集(领域模型 §规划、财务与计算配置): 新建设备容量参数为 continuous 变量, 存量固定不生成。"""
    variables: list[dict] = []
    for dev in normalize_devices(graph):
        spec = resolve_device_type(dev["device_type"])
        if spec is None:
            continue
        if dev["kind"] not in ("new", "addition"):
            continue  # 存量设备: 容量固定, 只优化运行
        for name, p in spec.parameters.items():
            if not p.is_optimizable:
                continue
            current = dev["params"].get(name, p.default)
            if not isinstance(current, (int, float)) or isinstance(current, bool):
                current = 0.0
            variables.append(
                {
                    "name": _safe_var_name(dev, name),
                    "type": "continuous",
                    "initial": float(current),
                    "min": float(p.min) if p.min is not None else None,
                    "max": _device_max_capacity(dev, spec, p),
                    "device_ref": dev["id"],
                    "param": name,
                    "unit": p.unit,
                }
            )
    return variables


def _build_default_config(db: Session, project_id: int) -> dict:
    """生成默认计算配置(见模块 docstring 的结构说明)。"""
    graph = load_work_graph(db, project_id)
    params = _default_parameters(graph)
    params["economic"]["currency"] = "CNY"
    if (proj := db.get(Project, project_id)) is not None:
        params["economic"]["currency"] = proj.currency or "CNY"
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
            name: p.default
            for name, p in algo.parameters.items()
            if name in ("gap_rel", "time_limit_s")
        },
        "random_seed": 42,
    }


def get_default_config(project_id: int, db: Session | None = None) -> dict:
    """生成默认计算配置(基于系统模型设备清单)。

    参数:
        project_id: 项目 id。
        db: 数据库会话; 为 None 时自行打开会话(兼容单参数调用)。
    """
    if db is None:
        with SessionLocal() as session:
            return _build_default_config(session, project_id)
    return _build_default_config(db, project_id)


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def _dims_for_unit(unit: str) -> Dimensions:
    """变量单位 -> 表达式量纲(统一走 core/units.dims_of, 宪法 §4)。

    已注册单位(含复合,如 kW/kWh/CNY/kWh)精确量纲;未注册单位视为无量纲
    (不参与量纲检查,兼容旧配置)。
    """
    if not unit:
        return Counter()
    try:
        return dims_of(unit)
    except UnitError:
        return Counter()


def _is_number(value: object) -> bool:
    """数值检查(int/float, 布尔除外)。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_structure(config: dict, diags: list[Diagnostic]) -> None:
    """顶层结构校验(必需段、算法模式、随机种子、容差)。"""
    for key in ("parameters", "variables", "objectives", "constraints", "algorithm"):
        if key not in config:
            diags.append(
                make_diag(
                    "SYS-CFG-001", SEVERITY_ERROR,
                    params={"field": key, "reason": "缺失必需配置段"},
                    location={"object_type": "config", "object_id": "", "field": key},
                )
            )
            return  # 缺段后其余检查无意义, 避免级联噪声
    for key in ("parameters", "variables", "objectives", "constraints", "algorithm"):
        expected = dict
        if key in ("variables", "objectives", "constraints"):
            expected = list
        if not isinstance(config[key], expected):
            diags.append(
                make_diag(
                    "SYS-CFG-001", SEVERITY_ERROR,
                    params={"field": key, "reason": f"配置段必须是 {expected.__name__}"},
                    location={"object_type": "config", "object_id": "", "field": key},
                )
            )
            return
    algo = config["algorithm"]
    if algo.get("mode") not in ("auto", "manual"):
        diags.append(
            make_diag(
                "SYS-CFG-001", SEVERITY_ERROR,
                params={"field": "algorithm.mode", "value": algo.get("mode")},
                location={"object_type": "config", "object_id": "", "field": "algorithm.mode"},
            )
        )
    seed = config.get("random_seed")
    if seed is not None and (not _is_number(seed) or not 0 <= int(seed) <= _SEED_MAX):
        diags.append(
            make_diag(
                "PARAM-RNG-003", SEVERITY_ERROR,
                params={"param": "random_seed", "value": seed, "min": 0, "max": _SEED_MAX},
                location={"object_type": "config", "object_id": "", "field": "random_seed"},
            )
        )


def _validate_parameters(
    config: dict, graph: dict, devices_by_key: dict, diags: list[Diagnostic]
) -> None:
    """参数校验: 设备参数按注册表规格(类型/范围/枚举); 经济/环境参数按固定规格。"""
    params = config["parameters"]
    if not isinstance(params, dict):
        diags.append(
            make_diag(
                "SYS-CFG-001", SEVERITY_ERROR,
                params={"field": "parameters", "reason": "参数段必须是对象"},
                location={"object_type": "config", "object_id": "", "field": "parameters"},
            )
        )
        return
    device_params = params.get("devices", {})
    for dev in normalize_devices(graph):
        spec = resolve_device_type(dev["device_type"])
        key = str(dev["id"]) if dev["id"] is not None else dev["name"]
        devices_by_key[key] = dev
        if spec is None:
            continue
        cur = device_params.get(key, {})
        if not isinstance(cur, dict):
            diags.append(
                make_diag(
                    "SYS-CFG-001", SEVERITY_ERROR,
                    params={"device": key, "reason": "设备参数必须是对象"},
                    location={"object_type": "device", "object_id": key, "field": "params"},
                )
            )
            continue
        for pname, pspec in spec.parameters.items():
            value = cur.get(pname, pspec.default)
            if pspec.enum is not None:
                if value not in pspec.enum:
                    diags.append(
                        make_diag(
                            "PARAM-RNG-003", SEVERITY_ERROR,
                            params={"param": pname, "value": value, "enum": list(pspec.enum)},
                            location={"object_type": "device", "object_id": key, "field": pname},
                        )
                    )
            elif pspec.unit != "reference" and isinstance(pspec.default, (int, float)) and not isinstance(pspec.default, bool):
                if not _is_number(value):
                    diags.append(
                        make_diag(
                            "PARAM-UNIT-002", SEVERITY_ERROR,
                            params={"param": pname, "value": repr(value), "expected": "数值"},
                            location={"object_type": "device", "object_id": key, "field": pname},
                        )
                    )
                else:
                    lo, hi = pspec.min, pspec.max
                    if (lo is not None and value < lo) or (hi is not None and value > hi):
                        diags.append(
                            make_diag(
                                "PARAM-RNG-003", SEVERITY_ERROR,
                                params={"param": pname, "value": value, "min": lo, "max": hi},
                                location={"object_type": "device", "object_id": key, "field": pname},
                            )
                        )
    # 经济/环境参数(固定规格表)
    for section, specs in (
        ("economic", ECONOMIC_PARAM_SPECS),
        ("environmental", ENVIRONMENTAL_PARAM_SPECS),
    ):
        section_params = params.get(section, {})
        if not isinstance(section_params, dict):
            diags.append(
                make_diag(
                    "SYS-CFG-001", SEVERITY_ERROR,
                    params={"field": f"parameters.{section}", "reason": "必须是对象"},
                    location={"object_type": "config", "object_id": "", "field": section},
                )
            )
            continue
        for pname, pspec in specs.items():
            value = section_params.get(pname, pspec["default"])
            if "enum" in pspec:
                if value not in pspec["enum"]:
                    diags.append(
                        make_diag(
                            "PARAM-RNG-003", SEVERITY_ERROR,
                            params={"param": pname, "value": value, "enum": list(pspec["enum"])},
                            location={"object_type": "config", "object_id": "", "field": pname},
                        )
                    )
                continue
            if not _is_number(value):
                diags.append(
                    make_diag(
                        "PARAM-UNIT-002", SEVERITY_ERROR,
                        params={"param": pname, "value": repr(value), "expected": "数值"},
                        location={"object_type": "config", "object_id": "", "field": pname},
                    )
                )
            else:
                lo, hi = pspec.get("min"), pspec.get("max")
                if (lo is not None and value < lo) or (hi is not None and value > hi):
                    diags.append(
                        make_diag(
                            "PARAM-RNG-003", SEVERITY_ERROR,
                            params={"param": pname, "value": value, "min": lo, "max": hi},
                            location={"object_type": "config", "object_id": "", "field": pname},
                        )
                    )


def _validate_variables(
    config: dict, devices_by_key: dict, diags: list[Diagnostic]
) -> None:
    """变量校验: 类型/初始值在界内/枚举取值/设备引用(宪法 §4 + 领域模型 §规划、财务与计算配置)。"""
    variables = config["variables"]
    if not isinstance(variables, list):
        return
    seen: set[str] = set()
    for idx, v in enumerate(variables):
        name = v.get("name") if isinstance(v, dict) else None
        loc = {
            "object_type": "variable",
            "object_id": str(name or ""),
            "field": f"variables[{idx}]",
        }
        if not isinstance(v, dict):
            diags.append(
                make_diag(
                    "SYS-CFG-001", SEVERITY_ERROR,
                    params={"field": f"variables[{idx}]", "reason": "变量声明必须是对象"},
                    location=loc,
                )
            )
            continue
        if not isinstance(name, str) or not _IDENT_RE.fullmatch(name):
            diags.append(
                make_diag(
                    "SYS-CFG-001", SEVERITY_ERROR,
                    params={
                        "field": "name",
                        "value": name,
                        "reason": "变量名必须是标识符 [A-Za-z_][A-Za-z0-9_]*",
                    },
                    location=loc,
                )
            )
        elif name in seen:
            diags.append(
                make_diag(
                    "PARAM-CONF-001", SEVERITY_ERROR,
                    params={"variable": name, "reason": "变量名重复"},
                    location=loc,
                )
            )
        else:
            seen.add(name)
        vtype = v.get("type")
        if vtype not in VARIABLE_TYPES:
            diags.append(
                make_diag(
                    "SYS-CFG-001", SEVERITY_ERROR,
                    params={"variable": name, "type": vtype, "allowed": list(VARIABLE_TYPES)},
                    location=loc,
                )
            )
            continue
        initial = v.get("initial")
        if vtype in ("continuous", "integer"):
            lo, hi = v.get("min"), v.get("max")
            for key, val in (("min", lo), ("max", hi), ("initial", initial)):
                if val is not None and not _is_number(val):
                    diags.append(
                        make_diag(
                            "PARAM-UNIT-002", SEVERITY_ERROR,
                            params={"param": name, "field": key, "value": repr(val), "expected": "数值"},
                            location=loc,
                        )
                    )
            if _is_number(lo) and _is_number(hi) and lo > hi:
                diags.append(
                    make_diag(
                        "PARAM-CONF-001", SEVERITY_ERROR,
                        params={"variable": name, "reason": "min 大于 max"},
                        location=loc,
                    )
                )
            if initial is None:
                diags.append(
                    make_diag(
                        "SYS-CFG-001", SEVERITY_ERROR,
                        params={"variable": name, "reason": "变量必须有初始值"},
                        location=loc,
                    )
                )
            elif _is_number(initial):
                if _is_number(lo) and initial < lo:
                    diags.append(
                        make_diag(
                            "PARAM-RNG-003", SEVERITY_ERROR,
                            params={"param": name, "value": initial, "min": lo, "max": hi},
                            location=loc,
                        )
                    )
                if _is_number(hi) and initial > hi:
                    diags.append(
                        make_diag(
                            "PARAM-RNG-003", SEVERITY_ERROR,
                            params={"param": name, "value": initial, "min": lo, "max": hi},
                            location=loc,
                        )
                    )
                if vtype == "integer" and float(initial) != int(initial):
                    diags.append(
                        make_diag(
                            "SYS-CFG-001", SEVERITY_ERROR,
                            params={"variable": name, "reason": "integer 变量初始值必须为整数"},
                            location=loc,
                        )
                    )
        elif vtype == "boolean":
            if initial not in (0, 1, True, False):
                diags.append(
                    make_diag(
                        "PARAM-RNG-003", SEVERITY_ERROR,
                        params={"param": name, "value": initial, "min": 0, "max": 1},
                        location=loc,
                    )
                )
        elif vtype == "enum":
            values = v.get("values")
            if not isinstance(values, list) or not values:
                diags.append(
                    make_diag(
                        "SYS-CFG-001", SEVERITY_ERROR,
                        params={"variable": name, "reason": "enum 变量必须提供 values 列表"},
                        location=loc,
                    )
                )
            elif initial not in values:
                diags.append(
                    make_diag(
                        "PARAM-RNG-003", SEVERITY_ERROR,
                        params={"param": name, "value": initial, "enum": values},
                        location=loc,
                    )
                )
        # 设备引用: 必须指向图中存在的设备
        dev_ref = v.get("device_ref")
        if dev_ref is not None and str(dev_ref) not in devices_by_key:
            diags.append(
                make_diag(
                    "CONN-TYPE-002", SEVERITY_ERROR,
                    params={"device_id": str(dev_ref), "type_id": ""},
                    location=loc,
                )
            )


def _validate_objectives(config: dict, diags: list[Diagnostic]) -> None:
    """目标校验: 至少一个目标, 指标/方向/权重合法(宪法 §4 + 领域模型 §规划、财务与计算配置)。"""
    objectives = config["objectives"]
    if not isinstance(objectives, list) or not objectives:
        diags.append(
            make_diag(
                "SYS-CFG-001", SEVERITY_ERROR,
                params={"field": "objectives", "reason": "至少需要一个目标"},
                location={"object_type": "config", "object_id": "", "field": "objectives"},
            )
        )
        return
    for idx, obj in enumerate(objectives):
        loc = {
            "object_type": "objective",
            "object_id": str(obj.get("metric") if isinstance(obj, dict) else ""),
            "field": f"objectives[{idx}]",
        }
        if not isinstance(obj, dict):
            diags.append(
                make_diag(
                    "SYS-CFG-001", SEVERITY_ERROR,
                    params={"field": f"objectives[{idx}]", "reason": "目标声明必须是对象"},
                    location=loc,
                )
            )
            continue
        metric = obj.get("metric")
        if metric not in OBJECTIVE_METRICS:
            diags.append(
                make_diag(
                    "SYS-CFG-001", SEVERITY_ERROR,
                    params={"metric": metric, "allowed": sorted(OBJECTIVE_METRICS)},
                    location=loc,
                )
            )
        direction = obj.get("direction")
        if direction not in ("max", "min"):
            diags.append(
                make_diag(
                    "SYS-CFG-001", SEVERITY_ERROR,
                    params={"metric": metric, "direction": direction},
                    location=loc,
                )
            )
        elif metric == "irr_after_tax" and direction != "max":
            diags.append(
                make_diag(
                    "SYS-CFG-001", SEVERITY_ERROR,
                    params={"metric": metric, "reason": "IRR 目标只能取 max 方向"},
                    location=loc,
                )
            )
        weight = obj.get("weight", 1.0)
        if not _is_number(weight) or weight < 0:
            diags.append(
                make_diag(
                    "PARAM-RNG-003", SEVERITY_ERROR,
                    params={"param": f"objectives[{idx}].weight", "value": weight, "min": 0},
                    location=loc,
                )
            )


def _validate_expression_constraint(
    payload: dict, variables: list[dict], idx: int, diags: list[Diagnostic]
) -> None:
    """表达式约束校验: parse_expr 解析+量纲+范围, 初始值试算(宪法 §4 + 领域模型 §规划、财务与计算配置)。"""
    loc = {"object_type": "constraint", "object_id": f"expr[{idx}]", "field": "payload.expression"}
    expr = payload.get("expression") if isinstance(payload, dict) else None
    if not isinstance(expr, str) or not expr.strip():
        diags.append(
            make_diag(
                "EXPR-SYN-001", SEVERITY_ERROR,
                params={"expr": expr},
                location=loc,
            )
        )
        return
    allowed = {v["name"] for v in variables if isinstance(v, dict) and "name" in v}
    dims = {
        v["name"]: _dims_for_unit(v.get("unit"))
        for v in variables
        if isinstance(v, dict) and "name" in v
    }
    try:
        compiled = parse_expr(expr, allowed, dims)
    except ExpressionError as exc:
        diags.append(
            make_diag(
                exc.code, SEVERITY_ERROR,
                params={**exc.params, "expression": expr},
                location=loc,
            )
        )
        return
    # 运行期检查: 在变量初始值处试算, 捕获除零/定义域等 EXPR-RUN-001
    try:
        compiled.eval(
            {
                v["name"]: v["initial"]
                for v in variables
                if isinstance(v, dict) and "name" in v and _is_number(v.get("initial"))
            }
        )
    except ExpressionError as exc:
        diags.append(
            make_diag(
                exc.code, SEVERITY_ERROR,
                params={**exc.params, "expression": expr},
                location=loc,
            )
        )


def _validate_constraints(
    config: dict, variables: list[dict], diags: list[Diagnostic]
) -> None:
    """约束校验: predefined 种类合法; expression 走受限表达式引擎。"""
    constraints = config["constraints"]
    if not isinstance(constraints, list):
        diags.append(
            make_diag(
                "SYS-CFG-001", SEVERITY_ERROR,
                params={"field": "constraints", "reason": "约束段必须是数组"},
                location={"object_type": "config", "object_id": "", "field": "constraints"},
            )
        )
        return
    for idx, c in enumerate(constraints):
        loc = {"object_type": "constraint", "object_id": str(idx), "field": "type"}
        if not isinstance(c, dict):
            diags.append(
                make_diag(
                    "SYS-CFG-001", SEVERITY_ERROR,
                    params={"field": f"constraints[{idx}]", "reason": "约束声明必须是对象"},
                    location=loc,
                )
            )
            continue
        ctype = c.get("type")
        payload = c.get("payload", {})
        if ctype == "predefined":
            kind = payload.get("kind") if isinstance(payload, dict) else None
            if kind not in PREDEFINED_CONSTRAINT_KINDS:
                diags.append(
                    make_diag(
                        "SYS-CFG-001", SEVERITY_ERROR,
                        params={"kind": kind, "allowed": sorted(PREDEFINED_CONSTRAINT_KINDS)},
                        location={
                            "object_type": "constraint",
                            "object_id": str(idx),
                            "field": "payload.kind",
                        },
                    )
                )
            elif kind == "co2_cap" and not _is_number(payload.get("max_tons")):
                diags.append(
                    make_diag(
                        "SYS-CFG-001", SEVERITY_ERROR,
                        params={"kind": kind, "reason": "co2_cap 需要数值 payload.max_tons"},
                        location={
                            "object_type": "constraint",
                            "object_id": str(idx),
                            "field": "payload.max_tons",
                        },
                    )
                )
            elif kind == "energy_cost_cap" and not _is_number(payload.get("max_amount")):
                diags.append(
                    make_diag(
                        "SYS-CFG-001", SEVERITY_ERROR,
                        params={"kind": kind, "reason": "energy_cost_cap 需要数值 payload.max_amount"},
                        location={
                            "object_type": "constraint",
                            "object_id": str(idx),
                            "field": "payload.max_amount",
                        },
                    )
                )
        elif ctype == "expression":
            _validate_expression_constraint(payload, variables, idx, diags)
        else:
            diags.append(
                make_diag(
                    "SYS-CFG-001", SEVERITY_ERROR,
                    params={"type": ctype, "allowed": ["predefined", "expression"]},
                    location=loc,
                )
            )


def _validate_irr_and_discount(config: dict, diags: list[Diagnostic]) -> None:
    """IRR 硬约束与折现率独立字段检查(宪法 §4 + 领域模型 §规划、财务与计算配置)。

    - irr_floor 必须是顶层字段(0..1), 不得混入经济参数段;
    - discount_rate 必须位于 parameters.economic, 不得出现在顶层;
    - 两者语义独立, 不要求大小关系。
    """
    irr_floor = config.get("irr_floor")
    if irr_floor is None:
        diags.append(
            make_diag(
                "SYS-CFG-001", SEVERITY_ERROR,
                params={"field": "irr_floor", "reason": "缺少最低 IRR 硬约束字段"},
                location={"object_type": "config", "object_id": "", "field": "irr_floor"},
            )
        )
    elif not _is_number(irr_floor) or not 0 <= float(irr_floor) <= 1:
        diags.append(
            make_diag(
                "PARAM-RNG-003", SEVERITY_ERROR,
                params={"param": "irr_floor", "value": irr_floor, "min": 0, "max": 1},
                location={"object_type": "config", "object_id": "", "field": "irr_floor"},
            )
        )
    params = config.get("parameters", {})
    if isinstance(params, dict):
        econ = params.get("economic", {})
        if isinstance(econ, dict) and "irr_floor" in econ:
            diags.append(
                make_diag(
                    "PARAM-CONF-001", SEVERITY_ERROR,
                    params={
                        "reason": "最低 IRR 硬约束是独立顶层字段, 不应位于 parameters.economic"
                    },
                    location={
                        "object_type": "config",
                        "object_id": "",
                        "field": "parameters.economic.irr_floor",
                    },
                )
            )
        if "discount_rate" in config:
            diags.append(
                make_diag(
                    "PARAM-CONF-001", SEVERITY_ERROR,
                    params={
                        "reason": "折现率必须位于 parameters.economic, 与最低 IRR 硬约束是不同字段"
                    },
                    location={"object_type": "config", "object_id": "", "field": "discount_rate"},
                )
            )
        elif not isinstance(econ, dict) or "discount_rate" not in econ:
            diags.append(
                make_diag(
                    "SYS-CFG-001", SEVERITY_ERROR,
                    params={
                        "field": "parameters.economic.discount_rate",
                        "reason": "缺少折现率字段",
                    },
                    location={
                        "object_type": "config",
                        "object_id": "",
                        "field": "parameters.economic.discount_rate",
                    },
                )
            )


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
                "CONN-TYPE-002", SEVERITY_ERROR,
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
    if any(
        isinstance(v, dict) and v.get("type") in ("integer", "boolean", "enum")
        for v in variables
    ):
        needs.add("milp")  # 离散变量需要 MILP 求解能力
    if any(
        isinstance(v, dict) and v.get("type") == "continuous"
        for v in variables
    ):
        needs.add("capacity_design")  # 容量设计
    missing = sorted(needs - set(spec.capabilities))
    if missing:
        diags.append(
            make_diag(
                "SYS-CFG-001", SEVERITY_ERROR,
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
                "SYS-CFG-001", SEVERITY_ERROR,
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
                    "SYS-CFG-001", SEVERITY_WARNING,
                    params={"param": key, "reason": "非当前算法注册参数, 将被忽略"},
                    location=loc,
                )
            )
            continue
        if not _is_number(value):
            diags.append(
                make_diag(
                    "PARAM-UNIT-002", SEVERITY_ERROR,
                    params={"param": key, "value": repr(value), "expected": "数值"},
                    location=loc,
                )
            )
        elif (p.min is not None and value < p.min) or (p.max is not None and value > p.max):
            diags.append(
                make_diag(
                    "PARAM-RNG-003", SEVERITY_ERROR,
                    params={"param": key, "value": value, "min": p.min, "max": p.max},
                    location=loc,
                )
            )


def normalize_config(config: dict) -> dict:
    """输入归一化: 补齐缺失段默认值, "tolerance" -> "tolerances"。"""
    normalized = dict(config)
    if "tolerance" in normalized and "tolerances" not in normalized:
        normalized["tolerances"] = normalized.pop("tolerance")
    defaults: dict = {
        "variables": [],
        "objectives": [],
        "constraints": [],
        "algorithm": {"mode": "auto", "name": DEFAULT_ALGORITHM},
        "random_seed": None,
    }
    for key, default in defaults.items():
        if key not in normalized or normalized[key] is None:
            normalized[key] = default
    return normalized


def validate_config(
    config: dict,
    graph: dict,
    data_version_ref: list[int] | None = None,
) -> list[Diagnostic]:
    """校验计算配置(配置校验门禁; 宪法 §4 + 领域模型 §规划、财务与计算配置)。

    参数:
        config: 计算配置 dict(结构见模块 docstring)。
        graph: 系统模型图 dict: {"devices": [{id, device_type, kind, name, params}]}。
        data_version_ref: 数据版本引用(list[int] | None, 仅做形状检查;
            内容校验属于 U05 数据单元)。

    返回:
        诊断列表(空列表表示通过; error/blocking 条目阻断保存)。
    """
    diags: list[Diagnostic] = []
    if not isinstance(config, dict):
        diags.append(
            make_diag(
                "SYS-CFG-001", SEVERITY_ERROR,
                params={"reason": "配置必须是对象"},
                location={"object_type": "config", "object_id": "", "field": ""},
            )
        )
        return diags
    config = normalize_config(config)
    if data_version_ref is not None and (
        not isinstance(data_version_ref, list)
        or any(not isinstance(v, int) or v <= 0 for v in data_version_ref)
    ):
        diags.append(
            make_diag(
                "SYS-CFG-001", SEVERITY_ERROR,
                params={"field": "data_version_ref", "reason": "必须是正整数 id 列表"},
                location={"object_type": "config", "object_id": "", "field": "data_version_ref"},
            )
        )
    _validate_structure(config, diags)
    if any(d.severity == SEVERITY_ERROR for d in diags):
        return diags  # 结构损坏, 不继续避免级联噪声
    devices_by_key: dict[str, dict] = {}
    _validate_parameters(config, graph, devices_by_key, diags)
    _validate_variables(config, devices_by_key, diags)
    _validate_objectives(config, diags)
    _validate_constraints(config, config["variables"], diags)
    _validate_irr_and_discount(config, diags)
    _validate_algorithm(config, diags)
    _validate_tolerances(config, diags)
    return diags


# ---------------------------------------------------------------------------
# 保存与读取
# ---------------------------------------------------------------------------


def _current_draft_revision(db: Session, project_id: int) -> int:
    """当前草稿修订号; 项目尚无草稿时按 1 处理(领域模型 §项目聚合 初始草稿 revision=1)。"""
    proj = db.get(Project, project_id)
    if proj is None:
        raise NotFoundError(
            f"项目不存在: {project_id}",
            code="RES-MISS-003",
            message_key="ies.diag.res.not_found",
            params={"project_id": project_id},
        )
    if proj.current_draft_id is not None:
        draft = db.get(Draft, proj.current_draft_id)
        if draft is not None:
            return draft.revision
    return 1


def _sync_draft_config(
    db: Session, proj: Project, config: dict, row: CalcConfig,
) -> None:
    """把已保存配置同步进当前草稿内容的 calc_config 节(不递增草稿修订)。

    计算快照装配(services/tasks.assemble_snapshot)与项目包导出以草稿内容为
    权威输入; 若不同步, 保存的配置不会进入快照/导出包(配置语义丢失)。
    任务级参数(task_params)属于任务提交时的覆盖项, 原样保留不覆盖。
    """
    if proj.current_draft_id is None:
        return
    draft = db.get(Draft, proj.current_draft_id)
    if draft is None:
        return
    from iesplan.services import project as project_service  # 延迟导入避免环

    try:
        content = project_service.load_content_object(db, draft.content_hash)
    except AppError:
        # 占位/缺失内容对象(测试种子或旧数据): 回退初始骨架, 不阻断保存
        content = project_service.initial_content()
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
    draft.content_hash = project_service.store_content_object(db, content)


def save_config(
    db: Session,
    project_id: int,
    config: dict,
    expected_revision: int,
    *,
    user_id: int | None = None,
) -> CalcConfig:
    """保存计算配置(与草稿修订绑定, 乐观锁; 冻结行则新建版本行)。

    参数:
        db: 数据库会话。
        project_id: 项目 id。
        config: 计算配置 dict。
        expected_revision: 期望的草稿修订号; 与实际修订不符抛 ConflictError。
        user_id: 修改者; None 时回退到项目 owner(认证接线前的兼容路径)。

    返回:
        保存后的 CalcConfig 行。
    """
    proj = db.get(Project, project_id)
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
    config = normalize_config(config)
    row = db.scalar(
        select(CalcConfig)
        .where(
            CalcConfig.project_id == project_id,
            CalcConfig.name == DEFAULT_CONFIG_NAME,
        )
        .order_by(CalcConfig.version.desc())
        .limit(1)
    )
    if row is not None and row.status == "frozen":
        row = None  # 冻结行不可修改(01 §6.1 触发器语义), 新建版本行
    if row is None:
        max_version = (
            db.scalar(
                select(CalcConfig.version)
                .where(
                    CalcConfig.project_id == project_id,
                    CalcConfig.name == DEFAULT_CONFIG_NAME,
                )
                .order_by(CalcConfig.version.desc())
                .limit(1)
            )
            or 0
        )
        row = CalcConfig(
            project_id=project_id,
            name=DEFAULT_CONFIG_NAME,
            version=max_version + 1,
            status="draft",
            updated_by=user_id or proj.owner_id,
        )
        db.add(row)
    params = config["parameters"]
    row.params = params
    row.variables = config["variables"]
    row.objectives = config["objectives"]
    row.constraints = config["constraints"]
    row.min_irr = config.get("irr_floor")
    algo_mode = config.get("algorithm", {}).get("mode", "auto")
    algo_name = config.get("algorithm", {}).get("name")
    row.algorithm = None if algo_mode == "auto" else ALGO_DB_CLASS.get(algo_name or "", algo_name)
    row.solver = SOLVER_ID
    row.tolerances = config.get("tolerances", {})
    row.random_seed = config.get("random_seed")
    row.updated_at = datetime.now(UTC)
    # 确保新行的 id 已分配(audit_log.entity_id 非空约束)
    db.flush()
    # 0.2.0 B4: 配置保存属"项目/数据/计算配置"关键变更(宪法 §16), 保留不可变
    # 最小化脱敏审计(只记版本/变量数/目标/算法, 不复制完整配置)
    db.add(
        AuditLog(
            entity_type="calc_config",
            entity_id=row.id,
            action="config.saved",
            actor_id=user_id or proj.owner_id,
            actor_type="user",
            before=None,
            after={
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
    )
    # 同步当前草稿内容的 calc_config 节(快照装配/项目包导出以草稿内容为
    # 权威输入, 不更新则保存的配置不进入计算快照与导出包)
    _sync_draft_config(db, proj, config, row)
    db.commit()
    db.refresh(row)
    return row


def _row_to_algorithm(row: CalcConfig) -> dict:
    """DB 算法列 -> 配置算法段(auto 模式存储为 NULL)。"""
    if row.algorithm is None:
        return {"mode": "auto", "name": DEFAULT_ALGORITHM}
    algo_id = _DB_CLASS_TO_ALGO.get(row.algorithm, row.algorithm)
    try:
        get_algorithm(algo_id)
    except NotFoundError:
        return {"mode": "manual", "name": row.algorithm}
    return {"mode": "manual", "name": algo_id}


def _row_to_config(row: CalcConfig) -> dict:
    """CalcConfig 行 -> 计算配置 dict。"""
    return {
        "parameters": row.params or {},
        "variables": row.variables or [],
        "objectives": row.objectives or [],
        "constraints": row.constraints or [],
        "algorithm": _row_to_algorithm(row),
        "irr_floor": float(row.min_irr) if row.min_irr is not None else None,
        "tolerances": row.tolerances or {},
        "random_seed": row.random_seed,
    }


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


def parameter_metadata(graph: dict) -> dict:
    """生成参数元数据(每个参数的单位/范围/默认值/帮助键, 供前端渲染)。

    graph: 与 validate_config 同构的设备清单 dict。
    """
    device_meta: dict[str, dict] = {}
    for dev in normalize_devices(graph):
        spec = resolve_device_type(dev["device_type"])
        if spec is None:
            continue
        key = str(dev["id"]) if dev["id"] is not None else dev["name"]
        device_meta[key] = {name: _param_meta(p) for name, p in spec.parameters.items()}
    return {
        "parameters": {
            "devices": device_meta,
            "economic": {
                name: {k: v for k, v in spec.items()} for name, spec in ECONOMIC_PARAM_SPECS.items()
            },
            "environmental": {
                name: {k: v for k, v in spec.items()} for name, spec in ENVIRONMENTAL_PARAM_SPECS.items()
            },
        },
    }


def get_config(project_id: int, db: Session | None = None) -> dict:
    """读取当前计算配置(未保存时返回生成的默认配置, 不带版本)。

    返回:
        {"config": dict, "meta": dict, "version": int|None, "status": str, "updated_at": str|None}
    """
    if db is None:
        with SessionLocal() as session:
            return _read_config(session, project_id)
    return _read_config(db, project_id)


def _read_config(db: Session, project_id: int) -> dict:
    if db.get(Project, project_id) is None:
        raise NotFoundError(
            f"项目不存在: {project_id}",
            code="RES-MISS-003",
            message_key="ies.diag.res.not_found",
            params={"project_id": project_id},
        )
    graph = load_work_graph(db, project_id)
    meta = parameter_metadata(graph)
    row = db.scalar(
        select(CalcConfig)
        .where(
            CalcConfig.project_id == project_id,
            CalcConfig.name == DEFAULT_CONFIG_NAME,
        )
        .order_by(CalcConfig.version.desc())
        .limit(1)
    )
    if row is None:
        return {
            "config": _build_default_config(db, project_id),
            "meta": meta,
            "version": None,
            "status": "draft",
            "updated_at": None,
        }
    return {
        "config": _row_to_config(row),
        "meta": meta,
        "version": row.version,
        "status": row.status,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
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
