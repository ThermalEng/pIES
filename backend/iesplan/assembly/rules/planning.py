"""规划目标与约束校验(ASM-PLAN-001/002/003)。

``planning`` 的目标与约束表达式只接受普通标识符 ``Name``(财务分量
标识符与已声明规划变量名), 别名改写与编译收敛到
``core.contracts.planning_expression`` 公共入口，不新造第二套解析器。
财务标识符含 ``__``(引擎安全边界禁止)：未知 ``__`` 标记先按规范
分类阻断，不送入引擎。

标识符映射(assembly-yaml.md §规划表达式)：
- ``<instance>__<upfront_capex|annual_fixed_om|period_variable_om>``：
  实例须 costed、其 finance_type 声明该分量且该分量 driver 按增量语义
  齐全；existing 不得引用沉没分量(upfront/annual)。
- ``<binding>__<period_energy_purchase|period_energy_sale>``：
  后缀须与价格条目 ``direction`` 一致。
- 其余(模型技术量等)不在装配阶段定义域内，未定义即阻断。

量纲：设备成本分量 currency、annual_fixed_om 为 currency/年(annual
与时间跨度字面量相乘后方可与 currency 相加，由引擎量纲核对强制)；
能源分量 currency；变量量纲取其声明单位(与设备参数单位一致)。
金额字面量币种必须与快照 currency 一致(引擎只核币种类别)。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Mapping

from iesplan.assembly.diags import ASM_PLAN_DIM, ASM_PLAN_SYNTAX, ASM_PLAN_UNDEF
from iesplan.assembly.diags import make_asm_diag as make_diag
from iesplan.assembly.rules.finance import required_components
from iesplan.assembly.rules.tariff import binding_prices
from iesplan.core.contracts import (
    Constraint,
    Objective,
    PlanningConfigError,
    PlanningVariable,
    compile_planning_expression,
)
from iesplan.core.diagnostics import Diagnostic
from iesplan.core.expression import (
    ExpressionCodeError,
    ExpressionDimensionError,
    ExpressionError,
    ExpressionSyntaxError,
)
from iesplan.core.units import ALIAS_MAP, UNITS, dims_of
from iesplan.finance import CURRENCIES, EffectiveFinanceConfig, FinanceTypeEntry

#: 分量后缀族(实例前缀合法)与能源后缀族(绑定前缀合法)
_COST_SUFFIXES: tuple[str, ...] = ("upfront_capex", "annual_fixed_om", "period_variable_om")
_ENERGY_SUFFIXES: tuple[str, ...] = ("period_energy_purchase", "period_energy_sale")

#: 标识符词元
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: 金额字面量(数值 + 币种词)：只核已知币种词汇，币种须与快照一致
_CURRENCY_LITERAL_RE = re.compile(
    r"(?<![A-Za-z0-9_.])\d[\d_]*(?:\.\d+)?\s*(CNY|USD)(?![A-Za-z0-9_])"
)


def _plan_fail(diags, code, field, reason, extra=None):
    params: dict = {"reason": reason}
    if extra:
        params.update(extra)
    diags.append(
        make_diag(
            code,
            severity="error",
            blocking=True,
            params=params,
            location={"object_type": "planning", "field": field},
        )
    )


def _produced_components(
    origin: str, ft_entry: FinanceTypeEntry, mapped: set[str]
) -> set[str]:
    """该绑定下实际产生的分量(声明 + 该分量 driver 齐全；fixed 无需 driver)。"""
    produced: set[str] = set()
    for component in required_components(origin):
        comp = getattr(ft_entry, component)
        if comp is None:
            continue
        if comp.linear is None:
            produced.add(component)
        elif set(comp.linear) <= set(mapped):
            produced.add(component)
    return produced


def finance_identifiers(
    doc: Mapping[str, Any],
    snapshot: EffectiveFinanceConfig,
) -> tuple[dict[str, Counter], dict[str, dict]]:
    """({标识符: 量纲}, {instance: 生产信息})；调用方保证绑定已过校验。"""
    dims: dict[str, Counter] = {}
    prod: dict[str, dict] = {}
    devices = doc.get("devices") or {}
    instances = ((doc.get("finance_binding") or {}).get("instances")) or {}
    for dev_id, dev in devices.items():
        entry = instances.get(dev_id)
        info: dict = {
            "origin": (dev.get("asset_origin") if isinstance(dev, Mapping) else None),
            "costed": False,
            "produced": set(),
        }
        prod[str(dev_id)] = info
        if not isinstance(entry, Mapping) or entry.get("type") != "costed":
            continue
        ft_entry = snapshot.finance_types.get(entry.get("finance_type"))
        if ft_entry is None:
            continue
        info["costed"] = True
        mapped = set((entry.get("drivers") or {}))
        produced = _produced_components(info["origin"], ft_entry, mapped)
        info["produced"] = produced
        for component in produced:
            ident = f"{dev_id}__{component}"
            if component == "annual_fixed_om":
                dims[ident] = Counter({"currency": 1, "time": -1})
            else:
                dims[ident] = Counter({"currency": 1})
    tariff = binding_prices(doc, snapshot)
    for binding_id, price in tariff.items():
        suffix = "period_energy_purchase" if price.direction == "purchase" else "period_energy_sale"
        dims[f"{binding_id}__{suffix}"] = Counter({"currency": 1})
    return dims, prod


def _check_expression(
    expr: str,
    *,
    field: str,
    allowed: Mapping[str, Counter],
    currency: str,
    instance_ids: set[str],
    binding_ids: set[str],
    prod: Mapping[str, dict],
    diags: list[Diagnostic],
) -> None:
    """单个表达式：币种字面量 → __ 预分类 → 别名改写 → 公共引擎。"""
    for match in _CURRENCY_LITERAL_RE.finditer(expr):
        if match.group(1) != currency:
            _plan_fail(
                diags, ASM_PLAN_DIM, field, "currency_mismatch",
                {"expression": expr, "literal": match.group(0).strip(), "currency": currency},
            )
            return
    for token in _IDENT_RE.findall(expr):
        if "__" not in token or token in allowed:
            continue
        prefix, _, suffix = token.partition("__")
        if prefix in instance_ids:
            if suffix in _COST_SUFFIXES:
                info = prod.get(prefix, {})
                if (
                    info.get("costed")
                    and info.get("origin") == "existing"
                    and suffix != "period_variable_om"
                ):
                    reason = "sunk_component_reference"
                else:
                    reason = "unproduced_component"
            elif suffix in _ENERGY_SUFFIXES:
                reason = "component_prefix_mismatch"
            else:
                reason = "undefined_identifier"
        elif prefix in binding_ids:
            if suffix in _ENERGY_SUFFIXES:
                reason = "direction_suffix_mismatch"
            elif suffix in _COST_SUFFIXES:
                reason = "component_prefix_mismatch"
            else:
                reason = "undefined_identifier"
        else:
            reason = "undefined_identifier"
        _plan_fail(
            diags, ASM_PLAN_UNDEF, field, reason,
            {"expression": expr, "identifier": token},
        )
        return
    # 别名改写与编译收敛到公共规划 IR 入口（币种已在上方先行核对，
    # 此处传 currency=None 不重复核对；单位字面量由引擎内部一次改写）。
    try:
        compile_planning_expression(expr, allowed, currency=None)
    except ExpressionSyntaxError as exc:
        _plan_fail(
            diags, ASM_PLAN_SYNTAX, field, "expression_syntax",
            {"expression": expr, "detail": str(exc)},
        )
    except ExpressionDimensionError as exc:
        _plan_fail(
            diags, ASM_PLAN_DIM, field, "dimension_mismatch",
            {"expression": expr, "detail": str(exc)},
        )
    except ExpressionCodeError as exc:
        symbol = str(exc.params.get("variable", ""))
        _plan_fail(
            diags, ASM_PLAN_UNDEF, field, "undefined_identifier",
            {"expression": expr, "identifier": symbol or str(exc)[:80]},
        )
    except ExpressionError as exc:
        _plan_fail(
            diags, ASM_PLAN_SYNTAX, field, "expression_rejected",
            {"expression": expr, "detail": str(exc)},
        )


def _check_build_scope(
    doc: Mapping[str, Any],
    snapshot: EffectiveFinanceConfig,
    var_name: str,
    variable: PlanningVariable,
    diags: list[Diagnostic],
) -> None:
    """输入边界建设变量核对(首年建设；无年度索引)。

    - 固定建设成本为正的 costed 实例：必建变量(下界 > 0，建设必然
      发生)在离散语义实现前按常数计一次放行；可选建设变量(下界为
      0/缺席)的触发在纯 LP 内无精确表达，明确拒收
      (``positive_fixed_cost_unsupported``)，不猜 ε/最小建设量，
      不重定义下界，不放行旧按常数计费路径。
      ``upfront_capex.fixed`` 缺席或为零 → 无固定成本，不在拒收范畴。
      无建设变量的固定成本实例走既有静态路径，不在本次核对。
    """
    field = f"planning.variables.{var_name}"
    instances = (doc.get("finance_binding") or {}).get("instances") or {}
    entry = instances.get(variable.device_ref)
    if not isinstance(entry, Mapping) or entry.get("type") != "costed":
        return
    ft_entry = snapshot.finance_types.get(entry.get("finance_type"))
    if ft_entry is None:
        return
    upfront = ft_entry.upfront_capex
    fixed = upfront.fixed if upfront is not None else None
    if fixed is None or fixed.value <= 0:
        return
    lower = variable.lower_bound
    if lower is not None and lower > 0:
        return
    _plan_fail(
        diags, ASM_PLAN_UNDEF, field, "positive_fixed_cost_unsupported",
        {
            "variable": var_name,
            "device_ref": variable.device_ref,
            "finance_type": entry.get("finance_type"),
        },
    )


def check_planning(
    doc: Mapping[str, Any],
    snapshot: EffectiveFinanceConfig,
    registry,
    diags: list[Diagnostic],
) -> None:
    """planning: 目标/变量/约束形状(核心契约) + 标识符映射 + 量纲。

    建设变量核对无年度索引依赖，在输入边界统一执行。
    """
    devices = doc.get("devices") or {}
    planning = doc.get("planning") or {}
    objective_raw = planning.get("objective") or {}
    try:
        Objective.from_dict(
            {"sense": objective_raw.get("sense"), "expression": objective_raw.get("expression")}
        )
    except PlanningConfigError as exc:
        _plan_fail(
            diags, ASM_PLAN_UNDEF, "planning.objective", "objective_invalid",
            {"detail": str(exc)},
        )
        return
    finance_dims, prod = finance_identifiers(doc, snapshot)
    allowed: dict[str, Counter] = dict(finance_dims)
    variables = planning.get("variables") or {}
    # 同一实例同一参数只允许一个建设变量：与保存边界领域层
    # validate_planning_domain 同一规则键，此处按装配诊断口径
    # 报告（含变量名与字段定位）；只读已解析变量，不重验可信内容。
    seen_targets: dict[tuple[str, str], str] = {}
    for var_name in sorted(variables):
        entry = variables[var_name]
        field = f"planning.variables.{var_name}"
        if not isinstance(entry, Mapping):
            continue
        try:
            variable = PlanningVariable.from_dict(entry)
        except PlanningConfigError as exc:
            _plan_fail(
                diags, ASM_PLAN_UNDEF, field, "variable_invalid",
                {"detail": str(exc)},
            )
            continue
        target = (variable.device_ref, variable.parameter)
        first = seen_targets.get(target)
        if first is None:
            seen_targets[target] = var_name
        else:
            _plan_fail(
                diags, ASM_PLAN_UNDEF, field, "variable_duplicate_target",
                {
                    "variable": var_name,
                    "conflict": first,
                    "device_ref": variable.device_ref,
                    "parameter": variable.parameter,
                },
            )
            continue
        _check_build_scope(doc, snapshot, var_name, variable, diags)
        if var_name in finance_dims:
            _plan_fail(
                diags, ASM_PLAN_UNDEF, field, "variable_reserved_collision",
                {"variable": var_name},
            )
            continue
        if re.fullmatch(r"qconst\d+", var_name):
            _plan_fail(
                diags, ASM_PLAN_UNDEF, field, "variable_reserved_collision",
                {"variable": var_name},
            )
            continue
        if var_name.lower() in ALIAS_MAP or var_name in UNITS:
            # 变量名与单位词相同会劫持数值单位字面量(别名改写破坏字面量)，
            # 必须拒绝(引擎单位词汇见 core.units)。
            _plan_fail(
                diags, ASM_PLAN_UNDEF, field, "variable_unit_word_collision",
                {"variable": var_name},
            )
            continue
        dev = devices.get(variable.device_ref)
        if dev is None or not isinstance(dev, Mapping):
            _plan_fail(
                diags, ASM_PLAN_UNDEF, field, "variable_unknown_device",
                {"variable": var_name, "device_ref": variable.device_ref},
            )
            continue
        descriptor = None
        definition = dev.get("definition") or {}
        if isinstance(definition, Mapping):
            descriptor = registry.get(definition.get("id"))
        if descriptor is None:
            continue
        pspec = descriptor.properties.get(variable.parameter)
        if pspec is None:
            _plan_fail(
                diags, ASM_PLAN_UNDEF, field, "variable_unknown_parameter",
                {"variable": var_name, "parameter": variable.parameter},
            )
            continue
        if variable.unit is not None and variable.unit != pspec.unit:
            _plan_fail(
                diags, ASM_PLAN_UNDEF, field, "variable_unit_mismatch",
                {
                    "variable": var_name,
                    "unit": variable.unit,
                    "parameter_unit": pspec.unit,
                },
            )
            continue
        var_range = pspec.valid_range
        if var_range is not None and (
            variable.lower_bound is not None or variable.upper_bound is not None
        ):
            low, high = var_range
            bad = False
            if variable.lower_bound is not None and low is not None:
                bad = bad or float(variable.lower_bound) < low
            if variable.upper_bound is not None and high is not None:
                bad = bad or float(variable.upper_bound) > high
            if bad:
                _plan_fail(
                    diags, ASM_PLAN_UNDEF, field, "variable_bound_out_of_range",
                    {"variable": var_name},
                )
                continue
        unit = variable.unit or pspec.unit
        try:
            allowed[var_name] = Counter(dict(dims_of(unit)))
        except Exception as exc:
            _plan_fail(
                diags, ASM_PLAN_UNDEF, field, "variable_unit_unrecognized",
                {"variable": var_name, "unit": unit, "detail": str(exc)},
            )
            allowed[var_name] = Counter()
    instance_ids = {str(k) for k in devices}
    binding_ids = {str(k) for k in binding_prices(doc, snapshot)}
    _check_expression(
        objective_raw.get("expression"),
        field="planning.objective.expression",
        allowed=allowed,
        currency=snapshot.currency,
        instance_ids=instance_ids,
        binding_ids=binding_ids,
        prod=prod,
        diags=diags,
    )
    constraints = planning.get("constraints") or {}
    for const_name in sorted(constraints):
        entry = constraints[const_name]
        field = f"planning.constraints.{const_name}"
        if not isinstance(entry, Mapping):
            continue
        try:
            # 约束形状唯一权威归公共 Constraint 契约(type 白名单/expression/
            # enabled)，此处不另设字段定义，只做装配标识符映射与量纲。
            constraint = Constraint.from_dict(entry)
        except PlanningConfigError as exc:
            _plan_fail(
                diags, ASM_PLAN_UNDEF, field, "constraint_invalid",
                {"detail": str(exc)},
            )
            continue
        if not constraint.enabled:
            continue
        _check_expression(
            constraint.expression,
            field=f"{field}.expression",
            allowed=allowed,
            currency=snapshot.currency,
            instance_ids=instance_ids,
            binding_ids=binding_ids,
            prod=prod,
            diags=diags,
        )


__all__ = ["check_planning", "finance_identifiers"]
