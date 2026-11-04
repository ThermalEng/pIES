"""System-scope input binding over modeling's public technical AST.

Only the public assembly input boundary calls the checker. Its compiled ASTs
are frozen in the receipt; generation restores them without parsing/checking
the author's expressions again. No synthetic device, allocation policy or
solver is involved.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping

from iesplan.assembly.diags import ASM_PLAN_DIM, ASM_PLAN_SYNTAX, ASM_PLAN_UNDEF, make_asm_diag
from iesplan.core.equation_grammar import EquationSyntaxError, reference_atoms
from iesplan.core.units import UnitError
from iesplan.modeling import (
    MathRelation,
    RelationAst,
    TimeIndexedRef,
    ast_from_dict,
    ast_to_dict,
    parse_equation_expression,
    parse_equation_relation,
)
from iesplan.modeling import (
    equation_dimensions as _dimension,
)
from iesplan.modeling import (
    equation_unit_dimensions as _dims,
)

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def _map(value, keys):
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise ValueError(f"字段须恰为 {', '.join(sorted(keys))}")
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("须为有限数值")
    return value


def check_system_equations(doc: Mapping, registry: Mapping, diags: list) -> dict:
    """Bind author references, dimensions and indices once at public input."""
    compiled = {}
    blocks = doc.get("system_equations", {})
    if not isinstance(blocks, Mapping):
        diags.append(
            make_asm_diag(
                ASM_PLAN_SYNTAX,
                severity="error",
                blocking=True,
                params={"detail": "system_equations 须为映射"},
                location={"object_type": "system_equations", "field": "system_equations"},
            )
        )
        return {}
    for block_id, raw in sorted(blocks.items(), key=lambda item: str(item[0])):
        base = f"system_equations.{block_id}"
        field = base
        identity = {}
        try:
            if not isinstance(block_id, str) or not _NAME.fullmatch(block_id):
                raise ValueError("系统块 ID 须为 lower_snake_case")
            _map(raw, ("constants", "references", "variables", "relations"))
            constants, references, variables = (raw[k] for k in ("constants", "references", "variables"))
            if not all(isinstance(v, Mapping) for v in (constants, references, variables)):
                raise ValueError("constants/references/variables 须为映射")
            units = {"step_duration": "s"}
            scalar = set()
            for category, entries in (
                ("constants", constants),
                ("references", references),
                ("variables", variables),
            ):
                for name, entry in sorted(entries.items(), key=lambda item: str(item[0])):
                    field = f"{base}.{category}.{name}"
                    identity = {"variable_id": name} if category == "variables" else {}
                    if not isinstance(name, str) or not _NAME.fullmatch(name) or name in units:
                        raise ValueError("局部名非法、重名或占用 step_duration")
                    if category == "constants":
                        _map(entry, ("value", "unit"))
                        _number(entry["value"])
                        unit = entry["unit"]
                        scalar.add(name)
                    elif category == "references":
                        if not isinstance(entry, Mapping) or entry.get("kind") not in (
                            "interface",
                            "property",
                            "variable",
                        ):
                            raise ValueError("reference.kind 须为 interface/property/variable")
                        kind = entry["kind"]
                        _map(entry, ("kind", "instance", kind))
                        field += ".instance"
                        instance = doc["devices"][entry["instance"]]
                        descriptor = registry[instance["definition"]["id"]]
                        field = f"{base}.{category}.{name}.{kind}"
                        table = (
                            descriptor.equations.variables
                            if kind == "variable"
                            else descriptor.properties
                            if kind == "property"
                            else descriptor.interfaces
                        )
                        unit = table[entry[kind]].unit
                        if kind == "property":
                            scalar.add(name)
                    else:
                        if (
                            not isinstance(entry, Mapping)
                            or set(entry) - {"unit", "valid_range", "initial"}
                            or not {"unit", "valid_range"} <= set(entry)
                        ):
                            raise ValueError("变量须声明 unit/valid_range，initial 可选")
                        unit = entry["unit"]
                    _dims(unit)
                    if "currency" in _dims(unit):
                        raise UnitError("系统方程只接受技术量，不接受金额")
                    units[name] = unit
            converted_vars = {}
            for name, entry in sorted(variables.items()):
                identity = {"variable_id": name}
                field = f"{base}.variables.{name}.valid_range"
                limits = _map(entry["valid_range"], ("minimum", "maximum"))
                numeric = []
                for side in ("minimum", "maximum"):
                    field = f"{base}.variables.{name}.valid_range.{side}"
                    value = limits[side]
                    if isinstance(value, Mapping):
                        _map(value, ("property_ref",))
                        ref = value["property_ref"]
                        if ref not in scalar:
                            raise ValueError("边界须引用本块常数或 property")
                        if _dims(units[ref]) != _dims(entry["unit"]):
                            raise UnitError("边界引用量纲不同")
                        numeric.append(None)
                    else:
                        numeric.append(None if value is None else _number(value))
                if all(v is not None for v in numeric) and numeric[0] > numeric[1]:
                    raise ValueError("minimum 大于 maximum")
                initial_ast = None
                if "initial" in entry:
                    field = f"{base}.variables.{name}.initial"
                    initial = entry["initial"]
                    if isinstance(initial, Mapping) and set(initial) == {"property_ref"}:
                        expr = initial["property_ref"]
                    else:
                        expr = _map(initial, ("expression",))["expression"]
                    refs, offsets = reference_atoms(expr, name)
                    if set(refs) - scalar or any(offsets) or "[" in expr:
                        raise ValueError("初值只可引用未索引的本块常数或 property")
                    parsed = parse_equation_expression(expr, relation_id=f"{name}.initial")
                    dimension = _dimension(parsed, units)
                    if dimension is not None and dimension != _dims(entry["unit"]):
                        raise UnitError("初值量纲不同")
                    initial_ast = ast_to_dict(parsed)
                converted_vars[name] = {
                    "unit": entry["unit"],
                    "valid_range": dict(limits),
                    "initial_ast": initial_ast,
                }
            if not isinstance(raw["relations"], list):
                raise ValueError("relations 须为有序列表")
            converted_relations = []
            ids = set()
            identity = {}
            for index, entry in enumerate(raw["relations"]):
                field = f"{base}.relations[{index}]"
                _map(entry, ("id", "expression"))
                rid = entry["id"]
                identity = {"relation_id": rid}
                if not isinstance(rid, str) or not _NAME.fullmatch(rid) or rid in ids:
                    raise ValueError("关系 ID 非法或重复")
                ids.add(rid)
                field += ".expression"
                expression = entry["expression"]
                relation = parse_equation_relation(expression, relation_id=rid)
                tokens = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)(\[t\s*(?:[-+]\s*\d+)?\])?", expression)
                for ref, indexing in tokens:
                    if ref not in units:
                        raise KeyError(ref)
                    if (ref in scalar or ref == "step_duration") == bool(indexing):
                        raise ValueError(f"{ref} 的标量/序列索引不匹配")
                for ref in relation.ast.lhs_refs + relation.ast.rhs_refs:
                    if ref.offset < 0 and (
                        ref.name not in variables or converted_vars[ref.name]["initial_ast"] is None
                    ):
                        raise ValueError("[t-1] 只可引用具有 initial 的本块状态")
                left = _dimension(relation.ast.lhs_root, units)
                right = _dimension(relation.ast.rhs_root, units)
                if left is not None and right is not None and left != right:
                    raise UnitError("关系两侧量纲不同")
                converted_relations.append(
                    {
                        "id": rid,
                        "lhs": ast_to_dict(relation.ast.lhs_root),
                        "rhs": ast_to_dict(relation.ast.rhs_root),
                        "lhs_refs": [
                            {"name": ref.name, "offset": ref.offset} for ref in relation.ast.lhs_refs
                        ],
                        "rhs_refs": [
                            {"name": ref.name, "offset": ref.offset} for ref in relation.ast.rhs_refs
                        ],
                    }
                )
            compiled[block_id] = {
                "constants": dict(constants),
                "references": dict(references),
                "variables": converted_vars,
                "relations": converted_relations,
            }
        except (ValueError, TypeError, KeyError, UnitError, EquationSyntaxError) as exc:
            code = (
                ASM_PLAN_DIM
                if isinstance(exc, UnitError)
                else ASM_PLAN_UNDEF
                if isinstance(exc, KeyError)
                else ASM_PLAN_SYNTAX
            )
            diags.append(
                make_asm_diag(
                    code,
                    severity="error",
                    blocking=True,
                    params={"detail": str(exc)},
                    location={
                        "object_type": "system_equations",
                        "object_id": block_id,
                        "field": field,
                        **identity,
                    },
                )
            )
    selected = set()
    for index, selection in enumerate(doc.get("outputs", {}).get("system_series", [])):
        try:
            _map(selection, ("block", "variable"))
            compiled[selection["block"]]["variables"][selection["variable"]]
            key = (selection["block"], selection["variable"])
            if key in selected:
                raise ValueError("系统序列输出重复选择")
            selected.add(key)
        except (ValueError, TypeError, KeyError) as exc:
            diags.append(
                make_asm_diag(
                    ASM_PLAN_UNDEF,
                    severity="error",
                    blocking=True,
                    params={"detail": str(exc)},
                    location={"object_type": "system_equations", "field": f"outputs.system_series[{index}]"},
                )
            )
    return compiled


def system_relations(block: Mapping) -> tuple[MathRelation, ...]:
    """Restore the frozen typed AST, not the author's expression text."""
    return tuple(
        MathRelation(
            entry["id"],
            None,
            RelationAst(
                entry["id"],
                None,
                ast_from_dict(entry["lhs"]),
                ast_from_dict(entry["rhs"]),
                tuple(TimeIndexedRef(**dict(ref)) for ref in entry["lhs_refs"]),
                tuple(TimeIndexedRef(**dict(ref)) for ref in entry["rhs_refs"]),
            ),
        )
        for entry in block["relations"]
    )


__all__ = ["check_system_equations", "system_relations"]
