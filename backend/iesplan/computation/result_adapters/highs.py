"""首个真实 ResultAdapterProvider：``ies.result-adapter.highs@1.0.0``。

与 ``ies.generator.highs_milp@1.0.0`` 配套，按有序参数记录恢复主解、
序列、度量与同次解结构化财务事实，解释其声明输出
``output/solution.sol``。该文件由求解 executable
``ies.solver.highs@1.7.2``（``ies-highs-solver`` 垫片，0.9-E 已提交
契约）写出的 ``HIGHS_SOLUTION_V1`` 文本：状态行、目标值、列数与按
索引的主解行。只使用传入的只读 Bundle、ExecutionReceipt 与声明输出；
不读项目/数据库/当前 provider，不重跑 solver，不做财务评价（0.10-D）。

语义要点（Docker 真实垫片运行证据，见本包测试夹具）：
- 成功回执只看 solution 文件的 ``model_status`` 令牌：垫片在有解、
  不可行、无界时均以退出码 0 写出文件，不可用退出码判断数学状态；
- ``kOptimal`` 须有完整主解才发布：目标与映射变量值须有限，变量按
  同次生成的实际求解列位置恢复声明单位的变量；不为找列重读MPS，
  内部辅助列按公开输出声明自然忽略；
- 目标值先加回 ``objective_offset``（MPS 无法承载的目标常数项，只加
  一次），再按 ``objective_unit`` 恢复业务单位：金额量纲守币种原值
  （财务币种，含 USD，直接采用不折算），物理量经
  ``core.units.from_si`` 反向换算；单位由生成器编译目标时确定并写入
  parameters，适配器不猜；
- ``kInfeasible``/``kUnbounded``明确映射，不发布方案；
  ``kUnboundedOrInfeasible``为``not_solved``，保留终止摘要；
- 正常``kTimeLimit``按是否有完整可行解映射``feasible``／``not_solved``，
  不把未证最优的解记为``solved``；已证明最优才为``solved``；
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
from array import array
from collections import Counter
from collections.abc import Iterator, Mapping
from decimal import Decimal
from typing import Any, NoReturn

from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.protocols import (
    ComputeResult,
    ExecutionReceipt,
    SolverBundle,
)
from iesplan.computation.result_adapter import ResultAdapterDescriptor
from iesplan.computation.result_adapters.declarations import recover_declarations
from iesplan.core.content import Content, MemoryContent, freeze_owned_json, read_content, text_lines
from iesplan.core.units import UnitError, dims_of, from_si, unit_meta

ADAPTER_ID = "ies.result-adapter.highs"
ADAPTER_VERSION = "1.0.0"
ADAPTER_REF = f"{ADAPTER_ID}@{ADAPTER_VERSION}"

DESCRIPTOR = ResultAdapterDescriptor(
    adapter_id=ADAPTER_ID,
    version=ADAPTER_VERSION,
)

#: 本 provider 消费的 Bundle 内相对路径（与 generator 声明一致）。
#: V1 主解按索引排列，变量映射需 MPS 列序。
_SOLUTION_PATH = "output/solution.sol"
_PARAMETERS_PATH = "input/parameters.json"

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


def _fail(message: str) -> NoReturn:
    raise ComputationUnavailableError(f"HiGHS 结果解释失败: {message}", reason="result-protocol-failure")


def _parse_solution(raw: Content | bytes) -> dict[str, Any]:
    """逐行解析垫片 V1 文本，不复制整份解文本与行列表。"""
    try:
        return _read_solution(text_lines(MemoryContent(raw) if isinstance(raw, bytes) else raw))
    except UnicodeDecodeError as exc:
        _fail(f"solution 非 UTF-8 文本: {exc}")


def _read_solution(lines: Iterator[str]) -> dict[str, Any]:
    """按原始列位置保存一份双精度主解；只在公开输出边界解释文法。

    只读状态与所需主解：数值写法按垫片 ``repr(float)`` 字母表由
    ``float()`` 直接解析；有限性只在发布处核对。
    """
    if next(lines, None) != "HIGHS_SOLUTION_V1":
        _fail("solution 缺 HIGHS_SOLUTION_V1 首行")
    facts: dict[str, str] = {}
    primals = array("d")
    in_primal = False
    for line in lines:
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
    for key in (
        "model_status", "has_feasible_solution", "objective_value",
        "best_bound", "relative_gap", "num_columns",
    ):
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
    if facts["has_feasible_solution"] not in ("true", "false"):
        _fail("has_feasible_solution 须为true或false")

    def number(key: str) -> float | None:
        if facts[key] == "none":
            return None
        try:
            value = float(facts[key])
        except ValueError:
            _fail(f"{key} 非法: {facts[key]!r}")
        if not math.isfinite(value):
            _fail(f"{key} 非有限")
        return value

    return {
        "model_status": facts["model_status"],
        "has_feasible_solution": facts["has_feasible_solution"] == "true",
        "objective": number("objective_value"),
        "best_bound": number("best_bound"),
        "relative_gap": number("relative_gap"),
        "primals": primals,
    }


def _parameters(bundle: SolverBundle) -> dict[str, Any]:
    """Read the small header only; declaration records are consumed once with the solution."""
    doc = json.loads(read_content(bundle.files[_PARAMETERS_PATH]))
    return {
        "offset": float(doc["objective_offset"]),
        "objective_unit": doc["objective_unit"],
        "requested": doc["outputs"],
        "records": bundle.files[doc["records"]],
        "column_count": doc["column_count"],
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

        求解器正常达到时限与监督强制超时分开。前者由状态文件解释，
        后者保持执行器记录的timed_out，不读取残留文件。
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
        if isinstance(solution_raw, bytes):
            solution_raw = MemoryContent(solution_raw)
        if not isinstance(solution_raw, Content):
            _fail(f"声明输出缺失: {_SOLUTION_PATH}")
        solution = _parse_solution(solution_raw)
        status = solution["model_status"]
        if status == _STATUS_SOLVED:
            math_status, reason = "solved", "optimal"
        elif status == _STATUS_TIME_LIMIT:
            math_status = "feasible" if solution["has_feasible_solution"] else "not_solved"
            reason = "time_limit"
        elif status in (_STATUS_INFEASIBLE, _STATUS_UNBOUNDED):
            math_status = "infeasible" if status == _STATUS_INFEASIBLE else "unbounded"
            reason = math_status
        elif status == _STATUS_UNBOUNDED_OR_INFEASIBLE:
            math_status, reason = "not_solved", "not_solved"
        else:
            _fail(f"数学状态不可发布: {status!r}")

        def objective_value(value: float | None) -> float | None:
            if value is None:
                return None
            total = value + params["offset"]
            unit = params["objective_unit"]
            try:
                if dims_of(unit) != Counter({"currency": 1}):
                    total = from_si(total, unit)
            except UnitError as exc:
                _fail(f"目标反向换算失败 →{unit!r}: {exc}")
            if not math.isfinite(total):
                _fail("目标发布值非有限")
            return total

        summary = {
            "termination_reason": reason,
            "math_status": math_status,
            "has_feasible_solution": solution["has_feasible_solution"],
            "objective_value": objective_value(solution["objective"]),
            "objective_unit": params["objective_unit"],
            "best_bound": objective_value(solution["best_bound"]),
            "relative_gap": solution["relative_gap"],
        }
        if math_status not in ("solved", "feasible"):
            return ComputeResult(
                bundle_id=receipt.bundle_id,
                receipt_id=receipt.receipt_id,
                execution_status="completed",
                math_status=math_status,
                outputs={"solver_summary": summary},
            )
        objective = solution["objective"]
        if not solution["has_feasible_solution"] or objective is None:
            _fail("数学状态有解但缺有效主解或目标值")
        if len(solution["primals"]) != params["column_count"]:
            _fail("声明列数与 solution primal 个数不一致")
        params.update(recover_declarations(
            params.pop("records"), solution["primals"], _fail,
            include_solution="solution" in params["requested"],
        ))
        variables = params["variables"]
        objective_unit = params["objective_unit"]
        business_total = summary["objective_value"]
        components_out: dict[str, object] = {}
        for ident, decl in params["components"].items():
            total = float(decl["constant"])
            if not math.isfinite(total):
                _fail(f"分量 {ident!r} 内部值非有限")
            unit = decl["unit"]
            try:
                if dims_of(unit).get("currency") == 1:
                    # 与生成器同一系数：USD/a、USD/kW/a 只还原时间/
                    # 物理分母，不经过 core.units 的无汇率跨币种转换。
                    value = total / float(unit_meta(unit)["to_si"])
                else:
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
        out: dict[str, object] = {"solver_summary": summary}
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
            out["series"] = params["series"]
            out["system_series"] = params["system_series"]
            out["financial_facts"] = _recover_financial_facts(params["financial_facts"])
            out["model_sources"] = params["model_sources"]
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
            math_status=math_status,
            outputs=freeze_owned_json(out),
        )


def _facts_value(total: float, label: str) -> str:
    """有序记录已按同次主解求值；这里只发布十进制金额，不再次遍历表达式。"""
    if not math.isfinite(total):
        _fail(f"财务事实 {label} 求值非有限")
    # 十进制字符串：沿财务既有口径经字符串转 Decimal（不引入二进制
    # 误差），序列化不作展示舍入。
    return str(Decimal(str(total)))


def _recover_financial_facts(
    decl: Mapping[str, Any],
) -> dict[str, object]:
    """结构化财务事实声明 → 同次主解业务事实（格式页映射表为唯一正文）。

    只恢复业务事实，不重算税务、不猜列名、不改写期间；字面量（身份、
    期间、比例）原样透传，维护金额与其他金额均从主解仿射恢复为十进制
    字符串。内部可信声明
    直接取值，缺件自然可见失败，不设默认掩盖。
    """
    currency = decl["currency"]

    def amount(value: float, label: str) -> str:
        return _facts_value(value, label)

    out: dict[str, object] = {
        "currency": currency,
        "construction_total": amount(decl["construction_total"], "construction_total"),
        "annual_fixed_om": amount(decl["annual_fixed_om"], "annual_fixed_om"),
        "payments_applied": [
            {
                "t": entry["t"],
                "ratio": entry["ratio"],
                "amount": amount(entry["amount"], "payments_applied"),
            }
            for entry in decl["payments_applied"]
        ],
        "net_cashflows": [
            {
                "id": entry["id"], "t": entry["t"], "direction": entry["direction"],
                "amount": amount(entry["amount"], "net_cashflows.amount"),
            }
            for entry in decl["net_cashflows"]
        ],
        "annual_operation": [
            {
                "financial_year": entry["financial_year"],
                "energy_net": amount(entry["energy_net"], "annual_operation"),
                "variable_om": amount(entry["variable_om"], "annual_operation"),
                "fixed_om": amount(entry["fixed_om"], "annual_operation"),
                "maintenance": amount(entry["maintenance"], "annual_operation.maintenance"),
                "demand_charge": amount(entry["demand_charge"], "annual_operation"),
                "net": amount(entry["net"], "annual_operation"),
            }
            for entry in decl["annual_operation"]
        ],
        "tax_cash": {
            "vat": [
                {
                    "pool": entry["pool"],
                    "month": entry["month"],
                    "paid": amount(entry["paid"], "tax_cash.vat"),
                    "refund_claimed": amount(entry["refund_claimed"], "tax_cash.vat"),
                    "refund_arrived": amount(entry["refund_arrived"], "tax_cash.vat"),
                    "carry": amount(entry["carry"], "tax_cash.vat"),
                    "receivable": amount(entry["receivable"], "tax_cash.vat.receivable"),
                }
                for entry in decl["tax_cash"]["vat"]
            ],
            "vat_rules": [
                {
                    "rule": entry["rule"], "pool": entry["pool"], "month": entry["month"],
                    "output_tax": amount(entry["output_tax"], "tax_cash.vat_rules.output_tax"),
                    "deductible_input": amount(
                        entry["deductible_input"], "tax_cash.vat_rules.deductible_input",
                    ),
                }
                for entry in decl["tax_cash"]["vat_rules"]
            ],
            "refunds": [
                {
                    "pool": entry["pool"],
                    "period": entry["period"],
                    "arrival_period": entry["arrival_period"],
                    "amount": amount(entry["amount"], "tax_cash.refunds"),
                }
                for entry in decl["tax_cash"]["refunds"]
            ],
            "income": [
                {
                    "rule": entry["rule"],
                    "year": entry["year"],
                    "taxable_base": amount(entry["taxable_base"], "tax_cash.income"),
                    "loss_used": amount(entry["loss_used"], "tax_cash.income"),
                    "loss_generated": amount(entry["loss_generated"], "tax_cash.income"),
                    "loss_expired": amount(entry["loss_expired"], "tax_cash.income"),
                    "assessed": amount(entry["assessed"], "tax_cash.income.assessed"),
                    "tax": amount(entry["tax"], "tax_cash.income"),
                }
                for entry in decl["tax_cash"]["income"]
            ],
            "depreciation": [
                {
                    "rule": entry["rule"],
                    "base_from": dict(entry["base_from"]),
                    "year": entry["year"],
                    "amount": amount(entry["amount"], "tax_cash.depreciation"),
                }
                for entry in decl["tax_cash"]["depreciation"]
            ],
            "loss_balances": [
                {
                    "rule": entry["rule"],
                    "vintage_id": entry["vintage_id"],
                    "year": entry["year"],
                    "last_usable_year": entry["last_usable_year"],
                    "amount": amount(entry["amount"], "tax_cash.loss_balances"),
                }
                for entry in decl["tax_cash"]["loss_balances"]
            ],
            "derived": [
                {
                    "rule": entry["rule"], "axis": entry["axis"], "period": entry["period"],
                    "assessed": amount(entry["assessed"], "tax_cash.derived.assessed"),
                    "payment_period": entry["payment_period"],
                    "paid": amount(entry["paid"], "tax_cash.derived.paid"),
                }
                for entry in decl["tax_cash"]["derived"]
            ],
        },
        "monthly_demand": [
            {
                "billing_id": entry["billing_id"],
                "charge_id": entry["charge_id"],
                "calendar_year": entry["calendar_year"],
                "calendar_month": entry["calendar_month"],
                "financial_year": entry["financial_year"],
                "peak_power": amount(entry["peak_power"], "monthly_demand.peak_power"),
                "peak_unit": entry["peak_unit"],
                "rate": dict(entry["rate"]),
                "charge": amount(entry["charge"], "monthly_demand.charge"),
            }
            for entry in decl["monthly_demand"]
        ],
        "annual_net_flows": [
            amount(node, "annual_net_flows")
            for node in decl["annual_net_flows"]
        ],
    }
    def construction_built(node: float, label: str) -> bool:
        value = float(amount(node, label))
        rounded = round(value)
        if not math.isfinite(value) or rounded not in (0, 1) or abs(value - rounded) > 1e-6:
            _fail(f"建设状态 {label!r} 不属于整数 0/1: {value!r}")
        return bool(rounded)

    def scale_value(node: float, unit: str, label: str) -> float:
        raw = float(amount(node, label))
        try:
            business = from_si(raw, unit)
        except UnitError as exc:
            _fail(f"建设规模 {label!r} 换算失败: {exc}")
        if not math.isfinite(business):
            _fail(f"建设规模 {label!r} 非有限数值")
        return business

    out["construction_units"] = [
        {
            "unit_id": entry["unit_id"],
            "instance_id": entry["instance_id"],
            "mode": entry["mode"],
            "built": construction_built(entry["built"], f"{entry['unit_id']}.built"),
            "scales": [
                {
                    "planning_variable_id": scale["planning_variable_id"],
                    "value": scale_value(
                        scale["value"], scale["unit"],
                        f"{entry['unit_id']}.{scale['planning_variable_id']}",
                    ),
                    "unit": scale["unit"],
                }
                for scale in entry["scales"]
            ],
        }
        for entry in decl["construction_units"]
    ]
    out["cost_items"] = [
        {**{key: entry[key] for key in (
            "instance_id", "finance_type", "component", "item_id", "axis", "period", "unit",
        )}, **{key: amount(entry[key], f"cost_items.{key}")
               for key in ("raw", "gross", "deductible_input", "net_basis")}}
        for entry in decl["cost_items"]
    ]
    out["amounts"] = [
        {"id": entry["id"], "axis": entry["axis"], "unit": entry["unit"],
         "values": [{"period": item["period"], "value": amount(item["value"], f"amounts.{entry['id']}")}
                    for item in entry["values"]]}
        for entry in decl["amounts"]
    ]
    out["cash_movements"] = [
        {
            **{key: entry[key] for key in ("id", "axis", "period", "direction", "price_basis")},
            **{key: amount(entry[key], f"cash_movements.{entry['id']}.{key}") for key in (
                "raw", "gross", "deductible_input", "net_basis", "output_tax", "net_revenue", "end_unpaid",
            )},
            "payments": [
                {"at": dict(payment["at"]), "ratio": payment["ratio"],
                 "amount": amount(payment["amount"], f"cash_movements.{entry['id']}.payments.amount")}
                for payment in entry["payments"]
            ],
        }
        for entry in decl["cash_movements"]
    ]
    out["assets"] = [
        {
            "id": entry["id"], "basis": amount(entry["basis"], f"assets.{entry['id']}.basis"),
            "capitalized_at": dict(entry["capitalized_at"]), "available_from": entry["available_from"],
            "book_values": [
                {"year": value["year"], "value": amount(value["value"], f"assets.{entry['id']}.book_values")}
                for value in entry["book_values"]
            ],
        }
        for entry in decl["assets"]
    ]
    comparison = dict(decl["comparison"])
    if comparison["mode"] == "difference":
        comparison["evaluation_net_flows"] = [
            amount(node, "comparison.evaluation_net_flows") for node in comparison["evaluation_net_flows"]
        ]
    out["comparison"] = comparison
    return out


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
