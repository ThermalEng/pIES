"""确定性 free-format MPS writer(0.8f-B)。

只序列化已完成的 ``LinearModel``，不重读装配，不做业务修补。
同一模型逐字节相同：ROWS 先目标后约束（模型已稳定排序），
COLUMNS 按变量名顺序（模型已稳定排序），RHS/BOUNDS 同序，
数字一律 ``repr`` 最短往返。

MPS 无法承载目标常数项：返回 ``(文本, 目标偏移量)``，偏移量不影响
最优解，后续 ResultAdapter 加回。目标方向经标准 OBJSENSE 节表达。
"""

from __future__ import annotations

from iesplan.computation.generators.linear import LinearModel

#: 目标行名 / RHS 集名 / BOUNDS 集名 / 模型名（固定，保确定性）。
_OBJECTIVE_ROW = "OBJ"
_RHS_NAME = "RHS1"
_BOUND_NAME = "BND1"
_MODEL_NAME = "IES_MILP"

_SENSE_ROWS = {"eq": "E", "le": "L", "ge": "G"}

__all__ = ["write_mps"]


def _num(value: float) -> str:
    """确定性数字格式（最短往返；-0.0 已在降级期归一）。"""
    return repr(float(value))


def write_mps(model: LinearModel) -> tuple[str, float]:
    """序列化项目级线性模型为 free-format MPS 文本与目标偏移量。"""
    if not isinstance(model, LinearModel):
        raise TypeError("model 须为 LinearModel")
    lines: list[str] = [f"NAME          {_MODEL_NAME}", "ROWS"]
    lines.append(f" N  {_OBJECTIVE_ROW}")
    for row in model.constraints:
        lines.append(f" {_SENSE_ROWS[row.sense]}  {row.name}")
    lines.append("COLUMNS")
    columns: dict[str, list[tuple[str, float]]] = {
        var.name: [] for var in model.variables
    }
    for key, coeff in model.objective.terms:
        if coeff != 0:
            columns[key[0]].append((_OBJECTIVE_ROW, coeff))
    for row in model.constraints:
        for key, value in row.expression.terms:
            if value != 0:
                columns[key[0]].append((row.name, value))
    in_integer = False
    for var in model.variables:
        general_integer = var.kind == "binary" and not (
            var.lower == 0.0 and var.upper == 1.0
        )
        if general_integer and not in_integer:
            lines.append("    INTORG    'MARKER'                 'INTORG'")
            in_integer = True
        elif not general_integer and in_integer:
            lines.append("    INTEND    'MARKER'                 'INTEND'")
            in_integer = False
        entries = columns[var.name]
        for index in range(0, len(entries), 2):
            chunk = entries[index:index + 2]
            line = f"    {var.name}"
            for row_name, value in chunk:
                line += f"  {row_name}  {_num(value)}"
            lines.append(line)
    if in_integer:
        lines.append("    INTEND    'MARKER'                 'INTEND'")
    lines.append("RHS")
    for row in model.constraints:
        rhs = row.rhs - row.expression.constant
        if rhs != 0:
            lines.append(f"    {_RHS_NAME}  {row.name}  {_num(rhs)}")
    lines.append("BOUNDS")
    for var in model.variables:
        if var.kind == "binary" and var.lower == 0.0 and var.upper == 1.0:
            lines.append(f" BV       {_BOUND_NAME}  {var.name}")
            continue
        if var.lower is None and var.upper is None:
            lines.append(f" FR       {_BOUND_NAME}  {var.name}")
            continue
        if (
            var.lower is not None
            and var.upper is not None
            and var.lower == var.upper
        ):
            lines.append(f" FX       {_BOUND_NAME}  {var.name}  {_num(var.lower)}")
            continue
        if var.lower is None:
            if var.upper == 0:
                lines.append(f" MI       {_BOUND_NAME}  {var.name}")
            else:
                lines.append(f" FR       {_BOUND_NAME}  {var.name}")
                lines.append(
                    f" UP       {_BOUND_NAME}  {var.name}  {_num(var.upper)}"  # type: ignore[arg-type]
                )
            continue
        lines.append(f" LO       {_BOUND_NAME}  {var.name}  {_num(var.lower)}")
        if var.upper is not None:
            lines.append(
                f" UP       {_BOUND_NAME}  {var.name}  {_num(var.upper)}"
            )
    lines.append("OBJSENSE")
    lines.append(" MAX" if model.sense == "maximize" else " MIN")
    lines.append("ENDATA")
    return "\n".join(lines) + "\n", float(model.objective.constant)
