"""确定性 free-format MPS writer(0.8f-B)。

只序列化已完成的数值矩阵或小型 ``LinearModel``，不重读装配，不做业务修补。
同一模型逐字节相同：ROWS 先目标后约束（模型已稳定排序），
COLUMNS 按变量名顺序（模型已稳定排序），RHS/BOUNDS 同序，
数字一律 ``repr`` 最短往返。

按记录产生有界字节块；显式请求全文时复用同一writer。MPS无法承载
目标常数项，全文入口返回 ``(文本, 目标偏移量)``，偏移量不影响最优解，
后续ResultAdapter加回。目标方向经标准OBJSENSE节表达。
"""

from __future__ import annotations

from collections.abc import Iterator

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

_CHUNK_BYTES = 65_536

__all__ = ["iter_mps_chunks", "write_mps"]


def _num(value: float) -> str:
    """确定性数字格式（最短往返；-0.0 已在降级期归一）。"""
    return repr(float(value))


def _mps_records(model: SparseModel, stop: StopProbe | None) -> Iterator[str]:
    """依次产生当前模型的记录，不保留全量文本行。"""
    def checkpoint() -> None:
        reason = read_stop_reason(stop)
        if reason is not None:
            raise ComputationUnavailableError("MPS 生成已停止", reason=reason)

    checkpoint()
    yield f"NAME          {_MODEL_NAME}"
    yield "ROWS"
    yield f" N  {_OBJECTIVE_ROW}"
    rows = model.rows
    for index, declaration in enumerate(model.row_order):
        if index % 128 == 0:
            checkpoint()
        sense = rows.data.SENSES[rows.data.senses[rows.row_ids[declaration]]]
        yield f" {_SENSE_ROWS[sense]}  {rows.names[declaration]}"
    yield "COLUMNS"
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
            yield "    INTORG    'MARKER'                 'INTORG'"
            in_integer = True
        elif not general_integer and in_integer:
            yield "    INTEND    'MARKER'                 'INTEND'"
            in_integer = False
        start, end = columns.offsets[var_index], columns.offsets[var_index + 1]
        if start == end:
            # A coefficient-free variable still owns its explicit solver slot.
            yield f"    {name}  {_OBJECTIVE_ROW}  {_num(0.0)}"
        for index in range(start, end, 2):
            line = f"    {name}"
            for at in range(index, min(index + 2, end)):
                row = columns.rows[at]
                row_name = _OBJECTIVE_ROW if row == -1 else rows.names[row]
                value = columns.values[at]
                line += f"  {row_name}  {_num(value)}"
            yield line
    if in_integer:
        yield "    INTEND    'MARKER'                 'INTEND'"
    yield "RHS"
    for index, declaration in enumerate(model.row_order):
        if index % 128 == 0:
            checkpoint()
        rhs = rows.rhs[declaration] - rows.constants[declaration]
        if rhs != 0:
            yield f"    {_RHS_NAME}  {rows.names[declaration]}  {_num(rhs)}"
    yield "BOUNDS"
    for index, declaration in enumerate(model.column_order):
        if index % 128 == 0:
            checkpoint()
        name, lower, upper, kind = model.columns.values_at(declaration)
        if kind == "binary" and lower == 0.0 and upper == 1.0:
            yield f" BV       {_BOUND_NAME}  {name}"
            continue
        if lower is None and upper is None:
            yield f" FR       {_BOUND_NAME}  {name}"
            continue
        if (
            lower is not None
            and upper is not None
            and lower == upper
        ):
            yield f" FX       {_BOUND_NAME}  {name}  {_num(lower)}"
            continue
        if lower is None:
            # 无下界即 -inf：有限上界须用 MI+UP 表达。FR+UP 会被 HiGHS
            # 判“BOUNDS 重复定义”丢弃 UP 行导致上界丢失（已实证）。
            yield f" MI       {_BOUND_NAME}  {name}"
            if upper != 0:
                yield f" UP       {_BOUND_NAME}  {name}  {_num(upper)}"
            continue
        yield f" LO       {_BOUND_NAME}  {name}  {_num(lower)}"
        if upper is not None:
            yield f" UP       {_BOUND_NAME}  {name}  {_num(upper)}"
    yield "OBJSENSE"
    yield " MAX" if model.sense == "maximize" else " MIN"
    yield "ENDATA"


def iter_mps_chunks(
    model: LinearModel | SparseModel, *, stop: StopProbe | None = None,
) -> Iterator[bytes]:
    """按既定行列顺序产生至多64KiB的UTF-8块，不建立全量文本或字节。

    每次迭代独立建立同一数值矩阵的紧凑列方向索引；不修改模型，也不
    持有文件句柄、路径或存储依赖。UTF-8字符可跨块，拼接字节与原格式一致。
    """
    if isinstance(model, LinearModel):
        model = SparseModel.from_public(model)
    buffer = bytearray()
    for record in _mps_records(model, stop):
        encoded = memoryview((record + "\n").encode("utf-8"))
        offset = 0
        while offset < len(encoded):
            take = min(_CHUNK_BYTES - len(buffer), len(encoded) - offset)
            buffer.extend(encoded[offset:offset + take])
            offset += take
            if len(buffer) == _CHUNK_BYTES:
                yield bytes(buffer)
                buffer.clear()
    if buffer:
        yield bytes(buffer)


def write_mps(
    model: LinearModel | SparseModel, *, stop: StopProbe | None = None,
) -> tuple[str, float]:
    """显式请求完整文本时汇集同一分块writer，并返回目标偏移量。

    分块消费者直接使用iter_mps_chunks；本函数不是另一套MPS实现。
    """
    if isinstance(model, LinearModel):
        model = SparseModel.from_public(model)
    text = b"".join(iter_mps_chunks(model, stop=stop)).decode("utf-8")
    return text, float(model.objective.constant)
