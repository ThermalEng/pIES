"""确定性 free-format MPS writer(0.8f-B)。

只序列化已完成的 ``LinearModel``，不重读装配，不做业务修补。
同一模型逐字节相同：ROWS 先目标后约束（模型已稳定排序），
COLUMNS 按变量名顺序（模型已稳定排序），RHS/BOUNDS 同序，
数字一律 ``repr`` 最短往返。

MPS 无法承载目标常数项：返回 ``(文本, 目标偏移量)``，偏移量不影响
最优解，后续 ResultAdapter 加回。目标方向经标准 OBJSENSE 节表达。
"""

from __future__ import annotations

from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.generators.linear import LinearModel
from iesplan.computation.generators.matrix import ColumnEntries, SparseModel
from iesplan.computation.protocols import StopProbe, read_stop_reason

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


def write_mps(model: LinearModel | SparseModel, *, stop: StopProbe | None = None) -> tuple[str, float]:
    """序列化项目级线性模型为 free-format MPS 文本与目标偏移量。"""
    if isinstance(model, LinearModel):
        model = SparseModel.from_public(model)

    def checkpoint() -> None:
        reason = read_stop_reason(stop)
        if reason is not None:
            raise ComputationUnavailableError("MPS 生成已停止", reason=reason)

    checkpoint()
    lines: list[str] = [f"NAME          {_MODEL_NAME}", "ROWS"]
    lines.append(f" N  {_OBJECTIVE_ROW}")
    rows = model.rows
    for index, declaration in enumerate(model.row_order):
        if index % 128 == 0:
            checkpoint()
        sense = rows.data.SENSES[rows.data.senses[rows.row_ids[declaration]]]
        lines.append(f" {_SENSE_ROWS[sense]}  {rows.names[declaration]}")
    lines.append("COLUMNS")
    columns = ColumnEntries(model, checkpoint)
    in_integer = False
    for var_index, declaration in enumerate(model.column_order):
        if var_index % 128 == 0:
            checkpoint()
        name, lower, upper, kind = model.columns.values_at(declaration)
        general_integer = kind == "binary" and not (
            lower == 0.0 and upper == 1.0
        )
        if general_integer and not in_integer:
            lines.append("    INTORG    'MARKER'                 'INTORG'")
            in_integer = True
        elif not general_integer and in_integer:
            lines.append("    INTEND    'MARKER'                 'INTEND'")
            in_integer = False
        start, end = columns.offsets[var_index], columns.offsets[var_index + 1]
        if start == end:
            # A coefficient-free variable still owns its explicit solver slot.
            lines.append(f"    {name}  {_OBJECTIVE_ROW}  {_num(0.0)}")
        for index in range(start, end, 2):
            line = f"    {name}"
            for at in range(index, min(index + 2, end)):
                row = columns.rows[at]
                row_name = _OBJECTIVE_ROW if row == -1 else rows.names[row]
                value = columns.values[at]
                line += f"  {row_name}  {_num(value)}"
            lines.append(line)
    if in_integer:
        lines.append("    INTEND    'MARKER'                 'INTEND'")
    lines.append("RHS")
    for index, declaration in enumerate(model.row_order):
        if index % 128 == 0:
            checkpoint()
        rhs = rows.rhs[declaration] - rows.constants[declaration]
        if rhs != 0:
            lines.append(f"    {_RHS_NAME}  {rows.names[declaration]}  {_num(rhs)}")
    lines.append("BOUNDS")
    for index, declaration in enumerate(model.column_order):
        if index % 128 == 0:
            checkpoint()
        name, lower, upper, kind = model.columns.values_at(declaration)
        if kind == "binary" and lower == 0.0 and upper == 1.0:
            lines.append(f" BV       {_BOUND_NAME}  {name}")
            continue
        if lower is None and upper is None:
            lines.append(f" FR       {_BOUND_NAME}  {name}")
            continue
        if (
            lower is not None
            and upper is not None
            and lower == upper
        ):
            lines.append(f" FX       {_BOUND_NAME}  {name}  {_num(lower)}")
            continue
        if lower is None:
            # 无下界即 -inf：有限上界须用 MI+UP 表达。FR+UP 会被 HiGHS
            # 判“BOUNDS 重复定义”丢弃 UP 行导致上界丢失（已实证）。
            lines.append(f" MI       {_BOUND_NAME}  {name}")
            if upper != 0:
                lines.append(
                    f" UP       {_BOUND_NAME}  {name}  {_num(upper)}"
                )
            continue
        lines.append(f" LO       {_BOUND_NAME}  {name}  {_num(lower)}")
        if upper is not None:
            lines.append(
                f" UP       {_BOUND_NAME}  {name}  {_num(upper)}"
            )
    lines.append("OBJSENSE")
    lines.append(" MAX" if model.sense == "maximize" else " MIN")
    lines.append("ENDATA")
    return "\n".join(lines) + "\n", float(model.objective.constant)
