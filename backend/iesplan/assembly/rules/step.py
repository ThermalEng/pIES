"""跨实例逐步端口关系的公开输入检查。

借用公共规划编译器完成语法和量纲核对。只验证声明，不生成数学行。
计算层消费已通过装配门禁的冻结数据。
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping

from iesplan.assembly.diags import ASM_PLAN_DIM, ASM_PLAN_SYNTAX, ASM_PLAN_UNDEF
from iesplan.assembly.diags import make_asm_diag
from iesplan.core.contracts import compile_step_relation_expression
from iesplan.core.expression import ExpressionCodeError, ExpressionDimensionError, ExpressionError
from iesplan.core.units import ALIAS_MAP, UNITS, dims_of

_PORT_TOKEN = re.compile(r"\b([a-z][a-z0-9_]*)\[t\]")
_RESERVED = re.compile(r"(?:pfin|qconst)\d+")


def _reject(diags, code, field, reason, **extra):
    diags.append(make_asm_diag(
        code, severity="error", blocking=True,
        params={"reason": reason, **extra},
        location={"object_type": "planning", "field": field},
    ))


def _degree(ir, aliases):
    """IR 中是否是仿射项。标量规划变量也是决策量，而不是实例容量常数。"""
    op = ir[0]
    if op == "num":
        return 0
    if op == "var":
        return 0 if aliases.get(ir[1], ir[1]) == "step_duration" else 1
    if op == "unary":
        return _degree(ir[2], aliases)
    if op == "bin":
        operation, lhs, rhs = ir[1:]
        left, right = _degree(lhs, aliases), _degree(rhs, aliases)
        if operation in ("add", "sub"):
            return max(left, right)
        if operation == "mul":
            if left + right > 1:
                raise ValueError("non_affine_product")
            return left + right
        if operation == "div":
            if right != 0:
                raise ValueError("decision_denominator")
            return left
    raise ValueError("unsupported_affine_operation")


def check_step_relations(planning: Mapping, devices: Mapping, registry: Mapping, diags: list) -> None:
    """规划或装配的用户边界调用一次。registry 为公开设备描述符映射。

    设备只需包含 definition.id；无需连接全部完成，因此保存不依赖草稿
    已达到可计算状态。内部读取、冻结与恢复绝不再次调用本函数。
    """
    constraints = planning.get("constraints") or {}
    if not any(entry.get("type") == "step_relation" for entry in constraints.values() if isinstance(entry, Mapping)):
        return
    symbols = {"step_duration": Counter({"time": 1})}
    for name, item in (planning.get("variables") or {}).items():
        # Never replace the immutable timeline primitive with a planning decision.
        # The public domain/assembly variable gate reports the name collision.
        if name == "step_duration":
            continue
        instance = devices.get(item.get("device_ref")) if isinstance(item, Mapping) else None
        desc = registry.get((instance.get("definition") or {}).get("id")) if isinstance(instance, Mapping) else None
        spec = desc.properties.get(item.get("parameter")) if desc is not None else None
        if spec is not None:
            symbols[name] = Counter(dict(dims_of(spec.unit)))
    reserved = set(symbols)
    for constraint_id, entry in sorted(constraints.items()):
        if not isinstance(entry, Mapping) or entry.get("type") != "step_relation":
            continue
        field = f"planning.constraints.{constraint_id}"
        refs = entry.get("references") or {}
        local = dict(symbols)
        broken = False
        for alias, target in sorted(refs.items()):
            ref_field = f"{field}.references.{alias}"
            if (alias in reserved or "__" in alias or alias in ALIAS_MAP or alias in UNITS
                    or _RESERVED.fullmatch(alias)):
                _reject(diags, ASM_PLAN_UNDEF, ref_field, "alias_reserved_collision", alias=alias)
                broken = True
                continue
            instance_id, iface_id = target["instance"], target["interface"]
            instance = devices.get(instance_id)
            if not isinstance(instance, Mapping):
                _reject(diags, ASM_PLAN_UNDEF, ref_field + ".instance", "step_unknown_instance", instance=instance_id)
                broken = True
                continue
            descriptor = registry.get((instance.get("definition") or {}).get("id"))
            port = descriptor.interfaces.get(iface_id) if descriptor is not None else None
            if port is None:
                _reject(diags, ASM_PLAN_UNDEF, ref_field + ".interface", "step_unknown_interface", instance=instance_id, interface=iface_id)
                broken = True
                continue
            if port.type not in ("in", "out", "bidirectional"):
                _reject(diags, ASM_PLAN_UNDEF, ref_field + ".interface", "step_port_not_connectable", port_type=port.type)
                broken = True
                continue
            local[alias] = Counter(dict(dims_of(port.unit)))
        if broken:
            continue
        expression = entry["expression"]
        used = set()

        def substitute(match):
            alias = match.group(1)
            if alias not in refs:
                return match.group(0)
            used.add(alias)
            return alias

        rewritten = _PORT_TOKEN.sub(substitute, expression)
        # All port aliases must be indexed as alias[t], all planning variables must
        # remain scalar, and t+1/t-1/fixed-step/slices are forbidden.
        if "[" in rewritten or "]" in rewritten or any(
            re.search(r"\b" + re.escape(alias) + r"\b", _PORT_TOKEN.sub(
                lambda m: "" if m.group(1) == alias else m.group(0), expression))
            for alias in refs
        ):
            _reject(diags, ASM_PLAN_SYNTAX, field + ".expression", "step_invalid_index", expression=expression)
            continue
        if not used:
            _reject(diags, ASM_PLAN_SYNTAX, field + ".expression", "step_no_indexed_port")
            continue
        try:
            compiled = compile_step_relation_expression(expression, local)
            aliases = {safe: name for name, safe in compiled.aliases}
            ir = compiled.ir
            if ir[0] != "cmp" or ir[1] not in ("eq", "le", "ge"):
                raise ValueError("step_relation_requires_equality_or_inequality")
            _degree(ir[2], aliases)
            _degree(ir[3], aliases)
        except ExpressionDimensionError as exc:
            _reject(diags, ASM_PLAN_DIM, field + ".expression", "dimension_mismatch", detail=str(exc))
        except ExpressionCodeError as exc:
            _reject(diags, ASM_PLAN_UNDEF, field + ".expression", "step_unknown_symbol", detail=str(exc))
        except (ExpressionError, ValueError) as exc:
            _reject(diags, ASM_PLAN_SYNTAX, field + ".expression", "step_expression_invalid", detail=str(exc))


__all__ = ["check_step_relations"]
