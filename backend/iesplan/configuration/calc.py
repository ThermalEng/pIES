"""计算配置领域规则（校验/默认生成/元数据/序列化，归属 configuration）。

本模块是计算配置权威规则的唯一实现（由 ``services.config`` 收敛而来，
旧服务已删除）：
- 默认配置生成（纯构造部分）、输入归一化与全量校验；
- 参数元数据（单位/范围/默认值/帮助键，供前端渲染）；
- 算法注册表元数据；
- 计算配置行 ↔ 配置字典的序列化（``row_to_config``）。

本模块为纯规则：不持有会话、不做提交、不读写对象存储；需要数据库的
编排（工作图加载、配置读写、保存事务）由
``application.configuration.calc_config`` 经领域公开门面完成。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Final

from iesplan.configuration.contracts import CalcConfigRecord
from iesplan.core.contracts import ParameterSpec
from iesplan.core.diagnostics import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    Diagnostic,
    make_diag,
)
from iesplan.core.errors import NotFoundError
from iesplan.core.expression import (
    Dimensions,
    ExpressionError,
    parse_expr,
)
from iesplan.core.units import UnitError, dims_of
from iesplan.devices import (
    DeviceModelDocument as DeviceTypeSpec,
)
from iesplan.devices import (
    get_device as get_device_type,
)
from iesplan.devices.contracts2 import PropertySpec
from iesplan.engines.registry import (
    DEFAULT_ALGORITHM,
    AlgorithmSpec,
    get_algorithm,
    list_algorithms,
)

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
        "unit": "-",
        "min": 0.0,
        "max": 1.0,
        "default": 0.08,
        "help_key": "help.param.economic.discount_rate",
        "is_optimizable": False,
    },
    "tax_rate": {
        "unit": "-",
        "min": 0.0,
        "max": 1.0,
        "default": 0.25,
        "help_key": "help.param.economic.tax_rate",
        "is_optimizable": False,
    },
    "project_years": {
        "unit": "a",
        "min": 1,
        "max": 50,
        "default": 20,
        "help_key": "help.param.economic.project_years",
        "is_optimizable": False,
    },
    "depreciation_years": {
        "unit": "a",
        "min": 1,
        "max": 50,
        "default": 10,
        "help_key": "help.param.economic.depreciation_years",
        "is_optimizable": False,
    },
    "currency": {
        "unit": "-",
        "default": "CNY",
        "enum": ("CNY", "USD"),
        "help_key": "help.param.economic.currency",
        "is_optimizable": False,
    },
}

#: 环境参数规格(排放因子, 排放边界; 领域模型 §规划、财务与计算配置)
ENVIRONMENTAL_PARAM_SPECS: Final[dict[str, dict]] = {
    "emission_factor_grid": {
        "unit": "tCO2/MWh",
        "min": 0.0,
        "max": 10.0,
        "default": 0.581,
        "help_key": "help.param.environmental.emission_factor_grid",
        "is_optimizable": False,
    },
    "emission_factor_gas": {
        "unit": "tCO2/万m³",
        "min": 0.0,
        "max": 50.0,
        "default": 2.0,
        "help_key": "help.param.environmental.emission_factor_gas",
        "is_optimizable": False,
    },
}

# 设备类型解析: 优先设备行 params['type_detail'](完整 2.0 注册表 ID),
# 回退 device_type 列(粗分类短名, 需 CHECK 约束兼容); 注册表 id 可直接使用,
# 未注册的短名视为无注册表规格(调用方跳过)
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
        "economic": {k: v["default"] for k, v in ECONOMIC_PARAM_SPECS.items()},
        "environmental": {k: v["default"] for k, v in ENVIRONMENTAL_PARAM_SPECS.items()},
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
                    "SYS-CFG-001",
                    SEVERITY_ERROR,
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
                    "SYS-CFG-001",
                    SEVERITY_ERROR,
                    params={"field": key, "reason": f"配置段必须是 {expected.__name__}"},
                    location={"object_type": "config", "object_id": "", "field": key},
                )
            )
            return
    algo = config["algorithm"]
    if algo.get("mode") not in ("auto", "manual"):
        diags.append(
            make_diag(
                "SYS-CFG-001",
                SEVERITY_ERROR,
                params={"field": "algorithm.mode", "value": algo.get("mode")},
                location={"object_type": "config", "object_id": "", "field": "algorithm.mode"},
            )
        )
    seed = config.get("random_seed")
    if seed is not None and (not _is_number(seed) or not 0 <= int(seed) <= _SEED_MAX):
        diags.append(
            make_diag(
                "PARAM-RNG-003",
                SEVERITY_ERROR,
                params={"param": "random_seed", "value": seed, "min": 0, "max": _SEED_MAX},
                location={"object_type": "config", "object_id": "", "field": "random_seed"},
            )
        )


def _validate_parameters(config: dict, graph: dict, devices_by_key: dict, diags: list[Diagnostic]) -> None:
    """参数校验: 设备参数按注册表规格(类型/范围/枚举); 经济/环境参数按固定规格。"""
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
    for dev in normalize_devices(graph):
        dev_params = dev["params"] or {}
        type_id = dev_params.get("type_detail") or dev["device_type"]
        spec = resolve_device_type(type_id)
        key = str(dev["id"]) if dev["id"] is not None else dev["name"]
        devices_by_key[key] = dev
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
    # 经济/环境参数(固定规格表)
    for section, specs in (
        ("economic", ECONOMIC_PARAM_SPECS),
        ("environmental", ENVIRONMENTAL_PARAM_SPECS),
    ):
        section_params = params.get(section, {})
        if not isinstance(section_params, dict):
            diags.append(
                make_diag(
                    "SYS-CFG-001",
                    SEVERITY_ERROR,
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
                            "PARAM-RNG-003",
                            SEVERITY_ERROR,
                            params={"param": pname, "value": value, "enum": list(pspec["enum"])},
                            location={"object_type": "config", "object_id": "", "field": pname},
                        )
                    )
                continue
            if not _is_number(value):
                diags.append(
                    make_diag(
                        "PARAM-UNIT-002",
                        SEVERITY_ERROR,
                        params={"param": pname, "value": repr(value), "expected": "数值"},
                        location={"object_type": "config", "object_id": "", "field": pname},
                    )
                )
            else:
                lo, hi = pspec.get("min"), pspec.get("max")
                if (lo is not None and value < lo) or (hi is not None and value > hi):
                    diags.append(
                        make_diag(
                            "PARAM-RNG-003",
                            SEVERITY_ERROR,
                            params={"param": pname, "value": value, "min": lo, "max": hi},
                            location={"object_type": "config", "object_id": "", "field": pname},
                        )
                    )


def _validate_variables(config: dict, devices_by_key: dict, diags: list[Diagnostic]) -> None:
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
                    "SYS-CFG-001",
                    SEVERITY_ERROR,
                    params={"field": f"variables[{idx}]", "reason": "变量声明必须是对象"},
                    location=loc,
                )
            )
            continue
        if not isinstance(name, str) or not _IDENT_RE.fullmatch(name):
            diags.append(
                make_diag(
                    "SYS-CFG-001",
                    SEVERITY_ERROR,
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
                    "PARAM-CONF-001",
                    SEVERITY_ERROR,
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
                    "SYS-CFG-001",
                    SEVERITY_ERROR,
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
                            "PARAM-UNIT-002",
                            SEVERITY_ERROR,
                            params={"param": name, "field": key, "value": repr(val), "expected": "数值"},
                            location=loc,
                        )
                    )
            if _is_number(lo) and _is_number(hi) and lo > hi:
                diags.append(
                    make_diag(
                        "PARAM-CONF-001",
                        SEVERITY_ERROR,
                        params={"variable": name, "reason": "min 大于 max"},
                        location=loc,
                    )
                )
            if initial is None:
                diags.append(
                    make_diag(
                        "SYS-CFG-001",
                        SEVERITY_ERROR,
                        params={"variable": name, "reason": "变量必须有初始值"},
                        location=loc,
                    )
                )
            elif _is_number(initial):
                if _is_number(lo) and initial < lo:
                    diags.append(
                        make_diag(
                            "PARAM-RNG-003",
                            SEVERITY_ERROR,
                            params={"param": name, "value": initial, "min": lo, "max": hi},
                            location=loc,
                        )
                    )
                if _is_number(hi) and initial > hi:
                    diags.append(
                        make_diag(
                            "PARAM-RNG-003",
                            SEVERITY_ERROR,
                            params={"param": name, "value": initial, "min": lo, "max": hi},
                            location=loc,
                        )
                    )
                if vtype == "integer" and float(initial) != int(initial):
                    diags.append(
                        make_diag(
                            "SYS-CFG-001",
                            SEVERITY_ERROR,
                            params={"variable": name, "reason": "integer 变量初始值必须为整数"},
                            location=loc,
                        )
                    )
        elif vtype == "boolean":
            if initial not in (0, 1, True, False):
                diags.append(
                    make_diag(
                        "PARAM-RNG-003",
                        SEVERITY_ERROR,
                        params={"param": name, "value": initial, "min": 0, "max": 1},
                        location=loc,
                    )
                )
        elif vtype == "enum":
            values = v.get("values")
            if not isinstance(values, list) or not values:
                diags.append(
                    make_diag(
                        "SYS-CFG-001",
                        SEVERITY_ERROR,
                        params={"variable": name, "reason": "enum 变量必须提供 values 列表"},
                        location=loc,
                    )
                )
            elif initial not in values:
                diags.append(
                    make_diag(
                        "PARAM-RNG-003",
                        SEVERITY_ERROR,
                        params={"param": name, "value": initial, "enum": values},
                        location=loc,
                    )
                )
        # 设备引用: 必须指向图中存在的设备
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


def _validate_objectives(config: dict, diags: list[Diagnostic]) -> None:
    """目标校验: 至少一个目标, 指标/方向/权重合法(宪法 §4 + 领域模型 §规划、财务与计算配置)。"""
    objectives = config["objectives"]
    if not isinstance(objectives, list) or not objectives:
        diags.append(
            make_diag(
                "SYS-CFG-001",
                SEVERITY_ERROR,
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
                    "SYS-CFG-001",
                    SEVERITY_ERROR,
                    params={"field": f"objectives[{idx}]", "reason": "目标声明必须是对象"},
                    location=loc,
                )
            )
            continue
        metric = obj.get("metric")
        if metric not in OBJECTIVE_METRICS:
            diags.append(
                make_diag(
                    "SYS-CFG-001",
                    SEVERITY_ERROR,
                    params={"metric": metric, "allowed": sorted(OBJECTIVE_METRICS)},
                    location=loc,
                )
            )
        direction = obj.get("direction")
        if direction not in ("max", "min"):
            diags.append(
                make_diag(
                    "SYS-CFG-001",
                    SEVERITY_ERROR,
                    params={"metric": metric, "direction": direction},
                    location=loc,
                )
            )
        elif metric == "irr_after_tax" and direction != "max":
            diags.append(
                make_diag(
                    "SYS-CFG-001",
                    SEVERITY_ERROR,
                    params={"metric": metric, "reason": "IRR 目标只能取 max 方向"},
                    location=loc,
                )
            )
        weight = obj.get("weight", 1.0)
        if not _is_number(weight) or weight < 0:
            diags.append(
                make_diag(
                    "PARAM-RNG-003",
                    SEVERITY_ERROR,
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
                "EXPR-SYN-001",
                SEVERITY_ERROR,
                params={"expr": expr},
                location=loc,
            )
        )
        return
    allowed = {v["name"] for v in variables if isinstance(v, dict) and "name" in v}
    dims = {
        v["name"]: _dims_for_unit(v.get("unit")) for v in variables if isinstance(v, dict) and "name" in v
    }
    try:
        compiled = parse_expr(expr, allowed, dims)
    except ExpressionError as exc:
        diags.append(
            make_diag(
                exc.code,
                SEVERITY_ERROR,
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
                exc.code,
                SEVERITY_ERROR,
                params={**exc.params, "expression": expr},
                location=loc,
            )
        )


def _validate_constraints(config: dict, variables: list[dict], diags: list[Diagnostic]) -> None:
    """约束校验: predefined 种类合法; expression 走受限表达式引擎。"""
    constraints = config["constraints"]
    if not isinstance(constraints, list):
        diags.append(
            make_diag(
                "SYS-CFG-001",
                SEVERITY_ERROR,
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
                    "SYS-CFG-001",
                    SEVERITY_ERROR,
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
                        "SYS-CFG-001",
                        SEVERITY_ERROR,
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
                        "SYS-CFG-001",
                        SEVERITY_ERROR,
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
                        "SYS-CFG-001",
                        SEVERITY_ERROR,
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
                    "SYS-CFG-001",
                    SEVERITY_ERROR,
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
                "SYS-CFG-001",
                SEVERITY_ERROR,
                params={"field": "irr_floor", "reason": "缺少最低 IRR 硬约束字段"},
                location={"object_type": "config", "object_id": "", "field": "irr_floor"},
            )
        )
    elif not _is_number(irr_floor) or not 0 <= float(irr_floor) <= 1:
        diags.append(
            make_diag(
                "PARAM-RNG-003",
                SEVERITY_ERROR,
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
                    "PARAM-CONF-001",
                    SEVERITY_ERROR,
                    params={"reason": "最低 IRR 硬约束是独立顶层字段, 不应位于 parameters.economic"},
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
                    "PARAM-CONF-001",
                    SEVERITY_ERROR,
                    params={"reason": "折现率必须位于 parameters.economic, 与最低 IRR 硬约束是不同字段"},
                    location={"object_type": "config", "object_id": "", "field": "discount_rate"},
                )
            )
        elif not isinstance(econ, dict) or "discount_rate" not in econ:
            diags.append(
                make_diag(
                    "SYS-CFG-001",
                    SEVERITY_ERROR,
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
                "SYS-CFG-001",
                SEVERITY_ERROR,
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
                "SYS-CFG-001",
                SEVERITY_ERROR,
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
# 序列化: 计算配置行 ↔ 配置字典
# ---------------------------------------------------------------------------


def _row_to_algorithm(row: CalcConfigRecord) -> dict:
    """DB 算法列 -> 配置算法段(auto 模式存储为 NULL)。"""
    if row.algorithm is None:
        return {"mode": "auto", "name": DEFAULT_ALGORITHM}
    algo_id = _DB_CLASS_TO_ALGO.get(row.algorithm, row.algorithm)
    try:
        get_algorithm(algo_id)
    except NotFoundError:
        return {"mode": "manual", "name": row.algorithm}
    return {"mode": "manual", "name": algo_id}


def row_to_config(row: CalcConfigRecord) -> dict:
    """CalcConfig 行 -> 计算配置 dict（公开序列化，与旧私有函数同形）。

    DB 算法列 → 配置算法段：NULL 为 auto 模式；其余映射回注册表算法 id，
    未注册短名原样返回 manual。
    """
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


# ---------------------------------------------------------------------------
# 元数据: 参数规格与算法注册表
# ---------------------------------------------------------------------------


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
                name: {k: v for k, v in spec.items()} for name, spec in ECONOMIC_PARAM_SPECS.items()
            },
            "environmental": {
                name: {k: v for k, v in spec.items()} for name, spec in ENVIRONMENTAL_PARAM_SPECS.items()
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
