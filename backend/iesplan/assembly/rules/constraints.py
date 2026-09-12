"""约束表达式检查(ASM-CONST-001..003;复用 core/expression.py 引擎)。

W1-Assembly 收敛:本模块原以内联形式位于 checker.py,逻辑逐字搬迁至此,
仅将共享能力(``ensure_ports``/``resolve_model``/``unit_dims``)改为经
context 公开接口消费,checker/validator 改为经 rules 包调用本模块。
"""

from __future__ import annotations

import re

from iesplan.assembly.context import AssemblySpec, ensure_ports, resolve_model, unit_dims
from iesplan.assembly.diags import ASM_CONST_DIM, ASM_CONST_SYNTAX, ASM_CONST_UNDEF
from iesplan.assembly.diags import make_asm_diag as make_diag
from iesplan.core.diagnostics import Diagnostic
from iesplan.core.expression import (
    Dimensions,
    ExpressionCodeError,
    ExpressionDimensionError,
    ExpressionError,
    ExpressionSyntaxError,
    parse_expr,
)

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


def _defined_symbols(spec: AssemblySpec, ctx) -> dict[str, Dimensions]:
    """全部可引用符号 → 量纲:端口引用 <dev>.<port> + 参数引用 <dev>.<param>。"""
    resolved = ensure_ports(spec, ctx)
    symbols: dict[str, Dimensions] = {}
    for ref, port in resolved.items():
        symbols[ref] = unit_dims(port.unit, port.quantity)
    for device in spec.devices:
        type_spec, _ = resolve_model(ctx, device.model)
        if type_spec is None:
            continue
        for name, value in device.params.items():
            if isinstance(value, (dict, list)):
                continue
            pspec = type_spec.properties.get(name)
            p_unit = pspec.unit if pspec is not None else None
            symbols[f"{device.id}.{name}"] = unit_dims(p_unit, None)
    return symbols


def run_constraint_checks(spec: AssemblySpec, ctx) -> list[Diagnostic]:
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


__all__ = ["run_constraint_checks"]
