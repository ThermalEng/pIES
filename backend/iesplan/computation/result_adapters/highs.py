"""首个真实 ResultAdapterProvider：``ies.result-adapter.highs@1.0.0``。

与 ``ies.generator.highs_milp@1.0.0`` 配套，解释其声明输出
``output/solution.sol``。该文件由 HiGHS 1.7.2 CLI 经 ``--solution_file``
写出，风格为 CLI 默认的 raw（``write_solution_style`` 默认 ``0``，
见 HiGHS 1.7.2 ``HighsOptions`` 与 ``RunHighs.cpp`` 写出路径；
Docker 内 highspy 1.7.2 同版本库逐字节复现）。只使用传入的只读
Bundle、ExecutionReceipt 与声明输出；不读项目/数据库/当前 provider，
不重跑 solver，不做财务评价（0.10-D）。

语义要点（Docker 真实运行证据，见本包测试夹具）：
- 成功回执只看 solution 文件的 ``Model status`` 行：求解器退出码
  在有解/不可行/无界时恒为 0，不可用退出码判断数学状态；
- ``Optimal`` 须附 ``Feasible`` 主解才发布：目标与业务变量值须有
  限，变量按 solution 列名对 ``unit_map`` 键映射（内部二进制列不在
  ``unit_map`` 中，自然忽略；``unit_map`` 缺键、列名重复一律失败）；
  未知状态、格式损坏、缺失必需段、稀疏风格（``# Columns`` 为负）、
  非有限数值一律结果协议失败，不发布伪成功；
- ``Infeasible``/``Unbounded`` 明确映射，不发布业务输出；
  ``Primal infeasible or unbounded`` 记 ``not_solved``，不发布；
- 文件自报 ``Time limit reached`` 时明确报错（部分解不当成功）；
  该错误与监督超时路径的 ``timed_out`` 技术状态无冲突：后者只属于
  回执 ``timed_out``（进程被监督杀死，根本不读 solution 文件）；
  其余非最优数学状态一律明确报错，不猜数值失败；
- 物理变量经 ``core.units.from_si`` 反向换算到业务单位；金额保持
  财务币种（SI 金额数值即 CNY，USD 在 build 侧已被拒绝）；目标值
  加回 ``objective_offset``（MPS 无法承载的目标常数项）；
- 非成功回执按 0.10-A 映射表返回执行状态，数学状态一律
  ``not_solved``，不发布业务输出。
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any, NoReturn

from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.protocols import (
    ComputeResult,
    ExecutionReceipt,
    SolverBundle,
)
from iesplan.computation.result_adapter import ResultAdapterDescriptor
from iesplan.core.units import UnitError, from_si

ADAPTER_ID = "ies.result-adapter.highs"
ADAPTER_VERSION = "1.0.0"
ADAPTER_REF = f"{ADAPTER_ID}@{ADAPTER_VERSION}"

DESCRIPTOR = ResultAdapterDescriptor(
    adapter_id=ADAPTER_ID,
    version=ADAPTER_VERSION,
)

#: 本 provider 消费的 Bundle 内相对路径（与 generator 声明一致）。
#: 注意：不再需要读 problem.mps——变量映射只凭 solution 列名对
#: parameters.unit_map 键，顺序无关（unit_map 按名排序，MPS 列按名
#: 排序，两者顺序今日恰好一致，但适配器不依赖该巧合）。
_SOLUTION_PATH = "output/solution.sol"
_PARAMETERS_PATH = "input/parameters.json"

#: 金额业务单位（SI 金额数值即 CNY；USD 在 build 侧已被拒绝）。
_OBJECTIVE_UNIT = "CNY"

#: 本 provider 正确表达的请求输出（metrics 为后处理摘要，不对应
#: 求解器第二个原始文件；未知请求明确拒绝）。
_SUPPORTED_OUTPUTS = ("solution", "metrics")

#: HiGHS 1.7.2 raw solution 的数学状态全集（utilModelStatusToString
#: 输出，大小写敏感，逐字匹配；未知字符串即协议失败）。
_STATUS_OPTIMAL = "Optimal"
_STATUS_INFEASIBLE = "Infeasible"
_STATUS_UNBOUNDED = "Unbounded"
_STATUS_UNBOUNDED_OR_INFEASIBLE = "Primal infeasible or unbounded"
_STATUS_TIME_LIMIT = "Time limit reached"
_KNOWN_STATUSES = frozenset(
    {
        "Not Set",
        "Load error",
        "Model error",
        "Presolve error",
        "Solve error",
        "Postsolve error",
        "Empty",
        "Memory limit reached",
        _STATUS_OPTIMAL,
        _STATUS_INFEASIBLE,
        _STATUS_UNBOUNDED_OR_INFEASIBLE,
        _STATUS_UNBOUNDED,
        "Bound on objective reached",
        "Target for objective reached",
        _STATUS_TIME_LIMIT,
        "Iteration limit reached",
        "Solution limit reached",
        "Interrupted by user",
        "Unknown",
    }
)

#: HiGHS ``%.Ng`` 数值渲染字母表之外的写法一律视为损坏（float()
#: 会接受十六进制与下划线，writer 从不产出，先收紧再解析）。
_NUMBER_RE = re.compile(
    r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?\Z",
)
_NONFINITE_TOKENS = frozenset({"inf", "+inf", "-inf", "infinity", "+infinity", "-infinity", "nan"})


def _fail(message: str) -> NoReturn:
    raise ComputationUnavailableError(f"HiGHS 结果解释失败: {message}", reason="result-protocol-failure")


def _parse_number(token: str, where: str) -> float:
    """解析 writer 字母表内的数值；写法越界即协议失败。"""
    lowered = token.lower()
    if lowered in _NONFINITE_TOKENS:
        return float(lowered)
    if not _NUMBER_RE.match(token):
        _fail(f"{where} 数值写法非法: {token!r}")
    return float(token)


def _parse_count(token: str, where: str) -> int:
    """解析 ``# Columns/Rows`` 计数；负数（稀疏风格）明确拒绝。"""
    try:
        count = int(token)
    except ValueError:
        _fail(f"{where} 计数非法: {token!r}")
    if count < 0:
        _fail(f"{where} 为稀疏风格，本适配器只接受 CLI 默认 raw: {token!r}")
    return count


class _Cursor:
    """行游标：节之间容忍空行，节内逐行严格消费。"""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self._pos = 0

    def next_content(self, where: str) -> str:
        """取下一非空行；到文件尾即协议失败。"""
        while self._pos < len(self._lines):
            line = self._lines[self._pos].strip()
            self._pos += 1
            if line:
                return line
        _fail(f"{where} 缺失（文件意外结束）")

    def next_physical(self, where: str) -> str:
        """取下一物理行（含空行，计数为 0 的行组即空行）。"""
        if self._pos >= len(self._lines):
            _fail(f"{where} 缺失（文件意外结束）")
        line = self._lines[self._pos].strip()
        self._pos += 1
        return line

    def expect(self, text: str) -> None:
        """下一非空行必须逐字等于 ``text``。"""
        got = self.next_content(f"期待 {text!r}")
        if got != text:
            _fail(f"期待 {text!r}，得到 {got!r}")

    def end(self) -> None:
        """基址段之后只允许空行到文件尾。"""
        while self._pos < len(self._lines):
            if self._lines[self._pos].strip():
                _fail(f"文件尾多余内容: {self._lines[self._pos]!r}")
            self._pos += 1


def _parse_named_values(cursor: _Cursor, count: int, where: str) -> list[tuple[str, float]]:
    """解析 ``<名> <数值>`` 行组；重名、缺行、坏值即协议失败。"""
    pairs: list[tuple[str, float]] = []
    seen: set[str] = set()
    for _ in range(count):
        line = cursor.next_physical(f"{where} 第 {len(pairs)} 行")
        name, sep, token = line.rpartition(" ")
        if not sep or not name:
            _fail(f"{where} 行非法: {line!r}")
        if name in seen:
            _fail(f"{where} 列名重复: {name!r}")
        seen.add(name)
        pairs.append((name, _parse_number(token, f"{where} {name!r}")))
    return pairs


def _parse_section(cursor: _Cursor, kind: str) -> tuple[str, float, list, list] | None:
    """解析 Primal/Dual 节；``None`` 表示无解块。

    返回 ``(可行性, 目标, 列, 行)``；Dual 节无目标行，以 ``nan`` 占位。
    """
    cursor.expect(f"# {kind} solution values")
    feasibility = cursor.next_content(f"{kind} 可行性")
    if feasibility == "None":
        return None
    if feasibility not in ("Feasible", "Infeasible"):
        _fail(f"{kind} 可行性未知: {feasibility!r}")
    objective = math.nan
    if kind == "Primal":
        line = cursor.next_content("Primal 目标行")
        head, sep, token = line.partition(" ")
        if not sep or head != "Objective":
            _fail(f"Primal 目标行非法: {line!r}")
        objective = _parse_number(token.strip(), "Objective")
    line = cursor.next_content(f"{kind} 列数行")
    head, sep, token = line.partition(" ")
    if not sep or head != "#" or not token.startswith("Columns "):
        _fail(f"{kind} 列数行非法: {line!r}")
    columns = _parse_named_values(
        cursor, _parse_count(token[len("Columns ") :].strip(), f"{kind} 列数"), kind
    )
    line = cursor.next_content(f"{kind} 行数行")
    head, sep, token = line.partition(" ")
    if not sep or head != "#" or not token.startswith("Rows "):
        _fail(f"{kind} 行数行非法: {line!r}")
    rows = _parse_named_values(cursor, _parse_count(token[len("Rows ") :].strip(), f"{kind} 行数"), kind)
    return (feasibility, objective, columns, rows)


def _parse_basis(cursor: _Cursor) -> None:
    """消费基址段（形状校验，不发布；计数不对模型断言）。"""
    cursor.expect("# Basis")
    cursor.expect("HiGHS v1")
    validity = cursor.next_content("基址有效性")
    if validity == "None":
        return
    if validity != "Valid":
        _fail(f"基址有效性未知: {validity!r}")
    for kind in ("Columns", "Rows"):
        line = cursor.next_content(f"基址{kind}行")
        head, sep, token = line.partition(" ")
        if not sep or head != "#" or not token.startswith(f"{kind} "):
            _fail(f"基址{kind}行非法: {line!r}")
        count = _parse_count(token[len(kind) + 1 :].strip(), f"基址{kind}")
        ints = cursor.next_physical(f"基址{kind}取值").split()
        if len(ints) != count:
            _fail(f"基址{kind}个数 {len(ints)} 与声明 {count} 不一致")
        for token in ints:
            try:
                int(token)
            except ValueError:
                _fail(f"基址{kind}取值非法: {token!r}")


def _parse_solution(raw: bytes) -> dict[str, Any]:
    """严格解析 HiGHS 1.7.2 CLI 默认 raw solution；任一偏离即协议失败。"""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail(f"solution 非 UTF-8 文本: {exc}")
    cursor = _Cursor(text.splitlines())
    cursor.expect("Model status")
    status = cursor.next_content("数学状态")
    if status not in _KNOWN_STATUSES:
        _fail(f"未知数学状态: {status!r}")
    primal = _parse_section(cursor, "Primal")
    dual = _parse_section(cursor, "Dual")
    _parse_basis(cursor)
    cursor.end()
    return {"model_status": status, "primal": primal, "dual": dual}


def _parameters(bundle: SolverBundle) -> dict[str, Any]:
    """读 Bundle 内 generator 规范 parameters；形状非法即协议失败。"""
    raw = bundle.files.get(_PARAMETERS_PATH)
    if not isinstance(raw, bytes):
        _fail(f"Bundle 缺输入文件: {_PARAMETERS_PATH}")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        _fail(f"parameters 非法 JSON: {exc}")
    if not isinstance(doc, dict):
        _fail("parameters 须为 JSON 对象")
    unit_map = doc.get("unit_map")
    if not isinstance(unit_map, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in unit_map.items()
    ):
        _fail("parameters.unit_map 须为字符串映射")
    offset = doc.get("objective_offset")
    if not isinstance(offset, (int, float)) or isinstance(offset, bool) or not math.isfinite(offset):
        _fail("parameters.objective_offset 须为有限数")
    requested = doc.get("outputs")
    if not isinstance(requested, list) or not all(isinstance(item, str) for item in requested):
        _fail("parameters.outputs 须为字符串数组")
    unknown = set(requested) - set(_SUPPORTED_OUTPUTS)
    if unknown:
        _fail(f"未知请求输出: {sorted(unknown)!r}")
    return {
        "unit_map": dict(unit_map),
        "offset": float(offset),
        "requested": list(requested),
    }


class HighsResultAdapter:
    """HiGHS 求解输出适配实现（无状态，可直接实例化）。

    注册选择由 ``build_result_adapter_registry`` 完成，此处信任；
    只凭传入三元组解释，不复核注册身份。
    """

    __slots__ = ()

    @property
    def descriptor(self) -> ResultAdapterDescriptor:
        """公开能力描述符（与 ``ref`` 单一来源）。"""
        return DESCRIPTOR

    @property
    def ref(self) -> str:
        """适配器精确引用（即描述符引用，不另存第二份）。"""
        return DESCRIPTOR.ref

    def available(self) -> bool:
        """纯解释实现恒可用。"""
        return True

    def adapt(
        self,
        bundle: SolverBundle,
        receipt: ExecutionReceipt,
        outputs: Mapping[str, object],
    ) -> ComputeResult:
        """把 Bundle、回执和声明输出映射为统一计算结果。"""
        execution = self._execution_status(receipt)
        if receipt.status != "succeeded":
            return ComputeResult(
                bundle_id=receipt.bundle_id,
                receipt_id=receipt.receipt_id,
                execution_status=execution,
                math_status="not_solved",
                outputs={},
            )
        return self._adapt_succeeded(bundle, receipt, outputs)

    @staticmethod
    def _execution_status(receipt: ExecutionReceipt) -> str:
        """回执七种状态到技术状态的真实映射（0.10-A 契约规则）。

        监督超时（``timed_out``）由执行器在进程被杀时直接记录，
        此时根本不读 solution 文件；文件自报时间限制另见
        ``_adapt_succeeded`` 的明确报错。
        """
        status = receipt.status
        if status == "succeeded":
            return "completed"
        if status == "process_failed":
            if receipt.termination_reason == "oom":
                return "resource_exhausted"
            return "failed"
        mapping = {
            "policy_rejected": "rejected",
            "output_protocol_failed": "protocol_failed",
            "timed_out": "timed_out",
            "cancelled": "cancelled",
            "fenced": "fenced",
        }
        try:
            return mapping[status]
        except KeyError:
            _fail(f"未知回执状态: {status!r}")

    def _adapt_succeeded(
        self,
        bundle: SolverBundle,
        receipt: ExecutionReceipt,
        outputs: Mapping[str, object],
    ) -> ComputeResult:
        params = _parameters(bundle)
        solution_raw = outputs.get(_SOLUTION_PATH)
        if not isinstance(solution_raw, bytes):
            _fail(f"声明输出缺失: {_SOLUTION_PATH}")
        solution = _parse_solution(solution_raw)
        status = solution["model_status"]
        if status == _STATUS_TIME_LIMIT:
            _fail("求解器自报达到时间限制：部分解不当成功解释")
        if status != _STATUS_OPTIMAL:
            if status in (_STATUS_INFEASIBLE, _STATUS_UNBOUNDED):
                math_status = "infeasible" if status == _STATUS_INFEASIBLE else "unbounded"
            elif status == _STATUS_UNBOUNDED_OR_INFEASIBLE:
                math_status = "not_solved"
            else:
                _fail(f"数学状态不可发布: {status!r}")
            return ComputeResult(
                bundle_id=receipt.bundle_id,
                receipt_id=receipt.receipt_id,
                execution_status="completed",
                math_status=math_status,
                outputs={},
            )
        primal = solution["primal"]
        if primal is None or primal[0] != "Feasible":
            _fail("Optimal 却无可行主解，不发布")
        objective = primal[1]
        if not math.isfinite(objective):
            _fail("有解但 Objective 非有限")
        by_name = {name: value for name, value in primal[2]}
        variables: list[dict[str, object]] = []
        for name, unit in params["unit_map"].items():
            if name not in by_name:
                _fail(f"unit_map 变量不在 solution 列中: {name!r}")
            try:
                value = from_si(by_name[name], unit)
            except UnitError as exc:
                _fail(f"反向换算失败 {name!r}→{unit!r}: {exc}")
            if not math.isfinite(value):
                _fail(f"换算后非有限数值: {name!r}")
            variables.append({"name": name, "value": value, "unit": unit})
        total = objective + params["offset"]
        if not math.isfinite(total):
            _fail("加回 objective_offset 后非有限")
        out: dict[str, object] = {}
        if "solution" in params["requested"]:
            out["solution"] = {
                "objective_value": total,
                "objective_unit": _OBJECTIVE_UNIT,
                "variables": variables,
            }
        if "metrics" in params["requested"]:
            out["metrics"] = {
                "model_status": status,
                "objective_value": total,
                "objective_unit": _OBJECTIVE_UNIT,
                "num_variables": len(variables),
            }
        return ComputeResult(
            bundle_id=receipt.bundle_id,
            receipt_id=receipt.receipt_id,
            execution_status="completed",
            math_status="solved",
            outputs=out,
        )


#: 组合根显式注册的内置 provider 单例（无状态，可共享）。
HIGHS_ADAPTER = HighsResultAdapter()


__all__ = [
    "ADAPTER_ID",
    "ADAPTER_REF",
    "ADAPTER_VERSION",
    "DESCRIPTOR",
    "HIGHS_ADAPTER",
    "HighsResultAdapter",
]
