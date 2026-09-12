"""计算配置领域纯规则（结构校验/归一化/行序列化，归属 configuration）。

本模块是计算配置纯规则的唯一实现（由 ``services.config`` 收敛而来，
旧服务已删除），仅依赖标准库与 ``iesplan.core``：
- 输入归一化与结构/参数（经济/环境）/变量/目标/约束/IRR 校验；
- 计算配置行 ↔ 配置字典的序列化（``row_to_config``）。

设备注册表相关（设备类型解析、默认配置生成、设备参数元数据）与算法
注册表相关（算法校验/容差/算法元数据）归属
``application.configuration.calc_config``（域源码纯度门禁）；需要数据库的
编排（工作图加载、配置读写、保存事务）同样由该用例经领域公开门面完成。

本模块为纯规则：不持有会话、不做提交、不读写对象存储。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Final

from iesplan.configuration.contracts import CalcConfigRecord
from iesplan.core.diagnostics import (
    SEVERITY_ERROR,
    Diagnostic,
    make_diag,
)
from iesplan.core.expression import (
    Dimensions,
    ExpressionError,
    parse_expr,
)
from iesplan.core.units import UnitError, dims_of

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

#: 默认算法（与 engines 注册表 DEFAULT_ALGORITHM 同值；领域内仅作回退
#: 默认名，不导入注册表；注册表能力查询归 application 层）。
DEFAULT_ALGORITHM: Final[str] = "ies.algo.milp_hybrid"

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

def _validate_parameters(config: dict, diags: list[Diagnostic]) -> None:
    """参数校验: 经济/环境参数按固定规格（纯规则，不依赖设备注册表）。

    设备参数按注册表规格的类型/范围校验与设备引用校验归属
    ``application.configuration.calc_config``（域源码纯度门禁），由应用层
    在本函数之后追加诊断。
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

def _validate_variables(config: dict, diags: list[Diagnostic]) -> None:
    """变量校验: 类型/初始值在界内/枚举取值(宪法 §4 + 领域模型 §规划、财务与计算配置)。

    变量 device_ref 指向图中设备的存在性校验归属
    ``application.configuration.calc_config``（设备分支，随设备参数校验追加）。
    """
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
    """校验计算配置的纯规则部分(配置校验门禁; 宪法 §4 + 领域模型 §规划、财务与计算配置)。

    只含不依赖设备/算法注册表的检查：归一化、数据版本引用形状、顶层结构、
    经济/环境参数、变量（类型/初值/枚举）、目标、约束、IRR/折现率。
    设备参数注册表校验、变量设备引用校验、算法能力与容差校验归属
    ``application.configuration.calc_config.validate_config``，由应用层在
    本函数返回的诊断之后追加（完整校验请经应用层入口）。

    参数:
        config: 计算配置 dict(结构见模块 docstring)。
        graph: 系统模型图 dict（纯规则部分不消费，仅为接口稳定保留）。
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
    _validate_parameters(config, diags)
    _validate_variables(config, diags)
    _validate_objectives(config, diags)
    _validate_constraints(config, config["variables"], diags)
    _validate_irr_and_discount(config, diags)
    return diags


# ---------------------------------------------------------------------------
# 序列化: 计算配置行 ↔ 配置字典
# ---------------------------------------------------------------------------

def _row_to_algorithm(row: CalcConfigRecord) -> dict:
    """DB 算法列 -> 配置算法段(auto 模式存储为 NULL)。

    纯映射（不查注册表）：NULL 为 auto；短名按 ALGO_DB_CLASS 映射回注册表
    算法 id，未映射值原样返回 manual。映射目标均为静态注册算法 id，
    与原注册表存在性检查结论一致。
    """
    if row.algorithm is None:
        return {"mode": "auto", "name": DEFAULT_ALGORITHM}
    return {"mode": "manual", "name": _DB_CLASS_TO_ALGO.get(row.algorithm, row.algorithm)}

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

