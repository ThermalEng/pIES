"""首个真实 ResultAdapterProvider：``ies.result-adapter.highs@2.0.0``。

与 ``ies.generator.highs_milp@2.0.0`` 配套，解释其声明输出
``output/solution.sol``。该文件由求解 executable
``ies.solver.highs@1.7.2``（``ies-highs-solver`` 垫片，0.9-E 已提交
契约）写出的 ``HIGHS_SOLUTION_V1`` 文本：状态行、目标值、列数与按
索引的主解行。只使用传入的只读 Bundle、ExecutionReceipt 与声明输出；
不读项目/数据库/当前 provider，不重跑 solver，不做财务评价（0.10-D）。

语义要点（Docker 真实垫片运行证据，见本包测试夹具）：
- 成功回执只看 solution 文件的 ``model_status`` 令牌：垫片在有解、
  不可行、无界时均以退出码 0 写出文件，不可用退出码判断数学状态；
- ``kOptimal`` 须有完整主解才发布：目标与映射变量值须有限，变量按
  MPS 列序对 ``unit_map`` 键映射（内部二进制列不在 ``unit_map`` 中，
  自然忽略；缺键、列数不一致一律失败）；
- 目标值先加回 ``objective_offset``（MPS 无法承载的目标常数项，只加
  一次），再按 ``objective_unit`` 恢复业务单位：金额量纲守币种原值
  （财务币种，含 USD，直接采用不折算），物理量经
  ``core.units.from_si`` 反向换算；单位由生成器编译目标时确定并写入
  parameters，适配器不猜；
- ``kInfeasible``/``kUnbounded`` 明确映射，不发布业务输出；
  ``kUnboundedOrInfeasible`` 记 ``not_solved``，不发布；
- 文件自报 ``kTimeLimit`` 时明确报错（部分解不当成功解释）；垫片超时
  恒以非零退出且不写文件，走回执 ``process_failed`` 路径；
  其余非最优数学状态一律明确报错，不猜数值失败；
- 未知状态、格式损坏、缺失输出、发布值非有限一律结果协议失败；
- 非成功回执按 0.10-A 映射表返回执行状态，数学状态一律
  ``not_solved``，不发布业务输出。

解析只读状态与所需主解：V1 没有对偶/基址段，不解析不存在的段；
parameters 由可信生成器产生，直接读取必需事实（缺键、形态错误即
协议失败），不做第二次输入 schema 校验。
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Mapping
from typing import Any, NoReturn

from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.protocols import (
    ComputeResult,
    ExecutionReceipt,
    SolverBundle,
)
from iesplan.computation.result_adapter import ResultAdapterDescriptor
from iesplan.core.units import UnitError, dims_of, from_si

ADAPTER_ID = "ies.result-adapter.highs"
ADAPTER_VERSION = "2.0.0"
ADAPTER_REF = f"{ADAPTER_ID}@{ADAPTER_VERSION}"

DESCRIPTOR = ResultAdapterDescriptor(
    adapter_id=ADAPTER_ID,
    version=ADAPTER_VERSION,
)

#: 本 provider 消费的 Bundle 内相对路径（与 generator 声明一致）。
#: V1 主解按索引排列，变量映射需 MPS 列序。
_SOLUTION_PATH = "output/solution.sol"
_PARAMETERS_PATH = "input/parameters.json"
_PROBLEM_PATH = "input/problem.mps"

#: 本 provider 发布时只认识的请求输出（未知请求由生成器在 build 侧
#: 拒绝；此处只按已请求键发布，不重复校验）。
_SUPPORTED_OUTPUTS = ("solution", "metrics")

#: 垫片写出的 model_status 令牌（``HighsModelStatus`` 全集，逐字匹配；
#: 未知令牌即协议失败）。
_STATUS_PREFIX = "HighsModelStatus."
_STATUS_SOLVED = "HighsModelStatus.kOptimal"
_STATUS_INFEASIBLE = "HighsModelStatus.kInfeasible"
_STATUS_UNBOUNDED = "HighsModelStatus.kUnbounded"
_STATUS_UNBOUNDED_OR_INFEASIBLE = "HighsModelStatus.kUnboundedOrInfeasible"
_STATUS_TIME_LIMIT = "HighsModelStatus.kTimeLimit"
_KNOWN_STATUSES = frozenset(
    _STATUS_PREFIX + name
    for name in (
        "kNotset",
        "kLoadError",
        "kModelError",
        "kPresolveError",
        "kSolveError",
        "kPostsolveError",
        "kModelEmpty",
        "kOptimal",
        "kInfeasible",
        "kUnboundedOrInfeasible",
        "kUnbounded",
        "kObjectiveBound",
        "kObjectiveTarget",
        "kTimeLimit",
        "kIterationLimit",
        "kSolutionLimit",
        "kInterrupt",
        "kUnknown",
    )
)

#: MPS COLUMNS 段内的标记行首字段。
_MARKER_FIELDS = ("INTORG", "INTEND")


def _fail(message: str) -> NoReturn:
    raise ComputationUnavailableError(f"HiGHS 结果解释失败: {message}", reason="result-protocol-failure")


def _parse_solution(raw: bytes) -> dict[str, Any]:
    """严格解析垫片 V1 文本；任一偏离即协议失败。

    只读状态与所需主解：数值写法按垫片 ``repr(float)`` 字母表由
    ``float()`` 直接解析；有限性只在发布处核对。
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail(f"solution 非 UTF-8 文本: {exc}")
    lines = text.splitlines()
    if not lines or lines[0] != "HIGHS_SOLUTION_V1":
        _fail("solution 缺 HIGHS_SOLUTION_V1 首行")
    facts: dict[str, str] = {}
    primals: list[float] = []
    in_primal = False
    for line in lines[1:]:
        if line == "primal:":
            in_primal = True
            continue
        key, sep, value = line.partition(":")
        if not sep or not key.strip():
            _fail(f"solution 行非法: {line!r}")
        if not in_primal:
            facts[key.strip()] = value.strip()
            continue
        try:
            position = int(key.strip())
        except ValueError:
            _fail(f"primal 序号非法: {line!r}")
        if position != len(primals):
            _fail(f"primal 序号不连续: {line!r}")
        try:
            primals.append(float(value.strip()))
        except ValueError:
            _fail(f"primal 数值非法: {line!r}")
    for key in ("model_status", "objective_value", "num_columns"):
        if key not in facts:
            _fail(f"solution 缺必需段: {key}")
    if facts["model_status"] not in _KNOWN_STATUSES:
        _fail(f"未知 model_status: {facts['model_status']!r}")
    try:
        num_columns = int(facts["num_columns"])
    except ValueError:
        _fail(f"num_columns 非法: {facts['num_columns']!r}")
    if num_columns != len(primals):
        _fail(f"primal 个数 {len(primals)} 与 num_columns {num_columns} 不一致")
    try:
        objective = float(facts["objective_value"])
    except ValueError:
        _fail(f"objective_value 非法: {facts['objective_value']!r}")
    return {
        "model_status": facts["model_status"],
        "objective": objective,
        "primals": primals,
    }


def _mps_column_names(raw: bytes) -> list[str]:
    """按 MPS COLUMNS 段恢复 LP 变量顺序（V1 主解按索引排列所需）。

    配对生成器 writer：每变量块连续，块内每行重复列名，整数标记行
    只出现在变量之间；同名连续行属同一列，列名切换即新列，至 RHS
    段结束。列名复现（非连续重复）即文件损坏，明确失败。
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail(f"problem.mps 非 UTF-8 文本: {exc}")
    lines = text.splitlines()
    try:
        start = lines.index("COLUMNS") + 1
    except ValueError:
        _fail("problem.mps 缺 COLUMNS 段")
    names: list[str] = []
    seen: set[str] = set()
    previous: str | None = None
    for line in lines[start:]:
        parts = line.split()
        if not parts:
            _fail("problem.mps 含空行")
        head = parts[0]
        if head == "RHS":
            break
        if head in _MARKER_FIELDS:
            previous = None
            continue
        if len(parts) < 3 or len(parts) % 2 == 0:
            _fail(f"problem.mps 列行非法: {line!r}")
        if head != previous:
            if head in seen:
                _fail(f"problem.mps 列名复现: {head!r}")
            names.append(head)
            seen.add(head)
            previous = head
    return names


def _parameters(bundle: SolverBundle) -> dict[str, Any]:
    """读 Bundle 内 generator 规范 parameters（可信生成器产物）。

    只取计算必需事实：unit_map、objective_offset、objective_unit、
    components（财务分量求值声明）、step_series（可发布步序列声明）、
    outputs；缺键或形态错误即协议失败，不做 schema 复核。
    无声明的老 Bundle 不静默降级、不回退旧解析，直接失败。
    """
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
    if not isinstance(unit_map, dict):
        _fail("parameters.unit_map 缺失或非法")
    offset = doc.get("objective_offset")
    if not isinstance(offset, (int, float)) or isinstance(offset, bool):
        _fail("parameters.objective_offset 缺失或非法")
    objective_unit = doc.get("objective_unit")
    if not isinstance(objective_unit, str):
        _fail("parameters.objective_unit 缺失或非法")
    requested = doc.get("outputs")
    if not isinstance(requested, list):
        _fail("parameters.outputs 缺失或非法")
    series = doc.get("step_series")
    if not isinstance(series, list):
        _fail("parameters.step_series 缺失或非法")
    for entry in series:
        if not isinstance(entry, dict):
            _fail("parameters.step_series 条目非法")
        for key in ("series_id", "instance_id", "variable_id", "unit"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                _fail(f"parameters.step_series 条目缺 {key}")
        columns = entry.get("columns")
        if (
            not isinstance(columns, list)
            or not columns
            or not all(isinstance(name, str) and name for name in columns)
        ):
            _fail("parameters.step_series 条目 columns 非法")
    components = doc.get("components")
    if not isinstance(components, dict):
        _fail("parameters.components 缺失或非法")
    for ident, decl in components.items():
        if not isinstance(ident, str) or not isinstance(decl, dict):
            _fail(f"parameters.components 条目非法: {ident!r}")
        if not isinstance(decl.get("unit"), str) or not isinstance(
            decl.get("caliber"), str
        ):
            _fail(f"分量 {ident!r} 缺 unit/caliber")
        if not isinstance(decl.get("constant"), (int, float)) or isinstance(
            decl.get("constant"), bool
        ):
            _fail(f"分量 {ident!r} 的 constant 非法")
        terms = decl.get("terms")
        if not isinstance(terms, dict) or not all(
            isinstance(name, str)
            and isinstance(coeff, (int, float))
            and not isinstance(coeff, bool)
            for name, coeff in terms.items()
        ):
            _fail(f"分量 {ident!r} 的 terms 非法")
    return {
        "unit_map": dict(unit_map),
        "offset": float(offset),
        "objective_unit": objective_unit,
        "requested": list(requested),
        "step_series": [dict(entry) for entry in series],
        "components": {ident: dict(decl) for ident, decl in components.items()},
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

        垫片超时恒以非零退出且不写 solution 文件，走普通
        ``process_failed``（``failed``）；监督超时（``timed_out``）由
        执行器在进程被杀时直接记录，根本不读 solution 文件；文件自报
        时间限制另见 ``_adapt_succeeded`` 的明确报错。
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
        if status != _STATUS_SOLVED:
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
        objective = solution["objective"]
        if not math.isfinite(objective):
            _fail("有解但 objective_value 非有限")
        problem_raw = bundle.files.get(_PROBLEM_PATH)
        if not isinstance(problem_raw, bytes):
            _fail(f"Bundle 缺输入文件: {_PROBLEM_PATH}")
        names = _mps_column_names(problem_raw)
        if len(names) != len(solution["primals"]):
            _fail("MPS 变量数与 solution primal 个数不一致")
        variables: list[dict[str, object]] = []
        for name, unit in params["unit_map"].items():
            if not isinstance(name, str) or not isinstance(unit, str):
                _fail("parameters.unit_map 键值须为字符串")
            try:
                index = names.index(name)
            except ValueError:
                _fail(f"unit_map 变量不在 MPS 列中: {name!r}")
            try:
                value = from_si(solution["primals"][index], unit)
            except UnitError as exc:
                _fail(f"反向换算失败 {name!r}→{unit!r}: {exc}")
            if not math.isfinite(value):
                _fail(f"换算后非有限数值: {name!r}")
            variables.append({"name": name, "value": value, "unit": unit})
        total = objective + params["offset"]
        objective_unit = params["objective_unit"]
        try:
            currency = dims_of(objective_unit) == Counter({"currency": 1})
        except UnitError as exc:
            _fail(f"目标单位无法解析 {objective_unit!r}: {exc}")
        if currency:
            business_total = total
        else:
            try:
                business_total = from_si(total, objective_unit)
            except UnitError as exc:
                _fail(f"目标反向换算失败 →{objective_unit!r}: {exc}")
        if not math.isfinite(business_total):
            _fail("目标发布值非有限")
        components_out: dict[str, object] = {}
        for ident, decl in params["components"].items():
            total = float(decl["constant"])
            for name, coeff in decl["terms"].items():
                try:
                    index = names.index(name)
                except ValueError:
                    _fail(f"分量 {ident!r} 的 LP 变量不在 MPS 列中: {name!r}")
                total += float(coeff) * solution["primals"][index]
            if not math.isfinite(total):
                _fail(f"分量 {ident!r} 内部值非有限")
            unit = decl["unit"]
            try:
                currency = dims_of(unit) == Counter({"currency": 1})
            except UnitError as exc:
                _fail(f"分量 {ident!r} 单位无法解析 {unit!r}: {exc}")
            if currency:
                value = total
            else:
                try:
                    value = from_si(total, unit)
                except UnitError as exc:
                    _fail(f"分量 {ident!r} 反向换算失败 →{unit!r}: {exc}")
            if not math.isfinite(value):
                _fail(f"分量 {ident!r} 发布值非有限")
            components_out[ident] = {
                "value": value,
                "unit": unit,
                "caliber": decl["caliber"],
            }
        out: dict[str, object] = {}
        if "solution" in params["requested"]:
            out["solution"] = {
                "objective_value": business_total,
                "objective_unit": objective_unit,
                "variables": variables,
                "components": components_out,
            }
            # 统一求解步序列：按生成器声明的显式列引用，用同一次主解
            # 恢复业务单位序列发布（与 variables 同一 primal 路径）。
            # 随 solution 请求发布；声明列缺失/换算失败即协议失败，
            # 不补零、不回退。metrics-only 无 solution 即无 series。
            series_out: dict[str, object] = {}
            for entry in params["step_series"]:
                unit = entry["unit"]
                values: list[float] = []
                for name in entry["columns"]:
                    try:
                        index = names.index(name)
                    except ValueError:
                        _fail(f"步序列 {entry['series_id']!r} 的列不在 MPS 列中: {name!r}")
                    try:
                        value = from_si(solution["primals"][index], unit)
                    except UnitError as exc:
                        _fail(f"步序列 {entry['series_id']!r} 反向换算失败 →{unit!r}: {exc}")
                    if not math.isfinite(value):
                        _fail(f"步序列 {entry['series_id']!r} 换算后非有限数值")
                    values.append(value)
                series_out[entry["series_id"]] = {
                    "instance_id": entry["instance_id"],
                    "variable_id": entry["variable_id"],
                    "unit": unit,
                    "values": values,
                }
            out["series"] = series_out
        if "metrics" in params["requested"]:
            out["metrics"] = {
                "model_status": status,
                "objective_value": business_total,
                "objective_unit": objective_unit,
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
