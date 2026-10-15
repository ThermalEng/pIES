"""任务域公开契约(快照/任务/尝试/租约/进度/诊断/槽位/不确定性表，归属 tasks)。

- 任务执行只消费不可变快照；快照一经创建不得修改；
- 快照装配产物只含规范二件套(canonical_assembly_text + assembly_receipt)，
  旧 assembly_text 列已删除(W2-B, 不做升级回填)；
- 只含不可变值对象、领域错误与无状态纯规则（类型/状态机/结局映射）；
  不导入 ORM、Session、services 或 application。
- 任务状态、业务结局映射与任务错误唯一权威归 tasks 域；
  application.tasks 只做提交/幂等/快照/事务编排，直接复用本模块。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

from iesplan.core.diagnostics import (
    SEVERITY_BLOCKING,
    SEVERITY_ERROR,
    SYS_STORE_QUOTA_EXCEEDED,
    TASK_EXEC_UNAVAILABLE,
)
from iesplan.core.errors import AppError, ConflictError, NotFoundError

#: 幂等键格式(字母/数字/._:-, 1-128 位)。任务域唯一权威:
#: 任务提交 API/用例与 DDL 均引用此处, 不得各自复述字面量,
#: 不得上提 core。
IDEMPOTENCY_KEY_RE: str = "^[A-Za-z0-9._:-]{1,128}$"


class TaskNotFoundError(NotFoundError):
    """任务/快照/尝试/租约不存在（沿用基类诊断码，不新增码）。"""


class TaskConflictError(ConflictError):
    """任务状态机冲突或幂等/并发冲突（沿用基类诊断码，不新增码）。"""


class InvalidRequestError(AppError):
    """任务请求/参数校验失败(HTTP 400)。"""

    code = "TASK-REQ-001"
    http_status = 400
    severity = SEVERITY_ERROR
    message_key = "ies.diag.param.invalid"


class TaskStateError(AppError):
    """任务状态机非法迁移(HTTP 409)。"""

    code = "TASK-STATE-001"
    http_status = 409
    severity = SEVERITY_ERROR
    message_key = "ies.diag.task.state_conflict"


class CancelDeniedError(AppError):
    """终态任务不可取消(HTTP 409)。"""

    code = "TASK-CANCEL-001"
    http_status = 409
    severity = SEVERITY_ERROR
    message_key = "ies.diag.task.cancel_denied"


class StorageQuotaError(AppError):
    """任务提交存储门禁未通过(HTTP 409; blocking 级 SYS-STORE-003)。"""

    code = SYS_STORE_QUOTA_EXCEEDED
    http_status = 409
    severity = SEVERITY_BLOCKING
    message_key = "ies.diag.store.quota_exceeded"


class ExecutionUnavailableError(AppError):
    """任务执行入口未实现/不可用(未实现的 I/O 执行器等, 非求解失败)。

    确定性失败: 经失败收拢落 failed + TASK-EXEC-001, 不得借用
    TASK-SOLVE-001 充数, 更不得伪造成功。
    """

    code = TASK_EXEC_UNAVAILABLE
    severity = SEVERITY_ERROR
    message_key = "ies.diag.task.exec_unavailable"


def _freeze_snapshot_value(value: object) -> object:
    """递归冻结快照封存值(dict → MappingProxy, list/tuple → tuple)。

    只做表示冻结, 不校验内容: 快照创建边界已签发, 运行期不再复核。
    """
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_snapshot_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_snapshot_value(item) for item in value)
    return value


#: 计算快照 schema 版本(宪法 §7.1/§7.7: 长期快照必须带 schema_version)。
#: 未正式发布, 无旧库兼容: 现行值唯一, 创建时显式写入, 复用身份包含版本。
#: 2.0.0 新增必填 device_models(逐实例设备数学贡献): 破坏性 schema 变化
#: 按宪法 §5.3 升主版本(与未来产品 1.1.0 无关)。
#: 3.0.0 新增必填 evaluation 确认块(评价年限/折现率/确认人/确认时间,
#: 0.12-F1): 快照复用身份包含年限/折现率, 不同即新快照; 确认人/确认时间
#: 只冻结存档, 不参与复用判定。
#: 4.0.0 评价确认块改为建设期/运营期/付款/维护冻结(0.12-T1): 评价年限
#: 含建设期(``evaluation_years == construction_period + operation_years``);
#: 运行点数/物化/预测/生成一律用 ``operation_years``; 复用身份包含新冻结
#: 字段(确认时间除外); 不保留旧字段语义或双轨读取。
SNAPSHOT_SCHEMA_VERSION: str = "4.0.0"


#: 评价确认输入合法键(任务提交边界唯一权威; 未知键拒绝, 不静默忽略)。
EVALUATION_INPUT_KEYS: frozenset[str] = frozenset(
    {
        "evaluation_years",
        "construction_period",
        "operation_years",
        "discount_rate",
        "construction_payments",
        "maintenance_expenses",
    }
)


@dataclass(frozen=True, slots=True)
class ConstructionPayment:
    """单笔建设付款比例(开工起整数年时点 + 十进制比例)。"""

    t: int
    ratio: str  # Decimal 定点字符串; 同一输入内 Σratio 精确为 1


@dataclass(frozen=True, slots=True)
class MaintenanceExpense:
    """单项维护/换新支出(财务发生年 + 十进制金额 + 币种)。"""

    year: int
    amount: str  # Decimal 定点字符串, 非负
    currency: str


@dataclass(frozen=True, slots=True)
class EvaluationInput:
    """计算任务评价确认输入(任务级类型化输入)。

    由任务提交边界 ``parse_evaluation_input`` 从顶层任务输入 ``evaluation``
    块唯一解析, 不从宽泛任务参数字典读取, 也不混入封闭 ``CalculationConfig``
    正文。``evaluation_years`` 为确认的财务评价总年限(含建设期, 年);
    ``construction_period`` 为非负整数建设期(年, 显式 0 合法, 不缺省);
    ``operation_years`` 为正整数投产后运营年限(运行点数/物化/预测/生成
    唯一使用的年限); ``discount_rate`` 为确认的折现率(宪法 §7.3 比例口径
    ``0..1``); ``construction_payments``/``maintenance_expenses`` 为冻结
    的付款比例与维护清单(实际现金流计算由后续切片消费)。
    """

    evaluation_years: int
    construction_period: int
    operation_years: int
    discount_rate: float
    construction_payments: tuple[ConstructionPayment, ...] = ()
    maintenance_expenses: tuple[MaintenanceExpense, ...] = ()


def _evaluation_fail(message: str, params: dict[str, Any], field: str) -> InvalidRequestError:
    """评价确认输入非法 → 400 TASK-REQ-015(无默认值, 不回退)。"""
    return InvalidRequestError(
        message,
        code="TASK-REQ-015",
        params=params,
        location={"object_type": "task", "field": field},
    )


def _as_decimal(value: object, field: str, *, allow_str: bool) -> Decimal:
    """JSON 数值/定点字符串 → Decimal(十进制语义; 拒 bool/非有限)。

    ``allow_str`` 为假时只接受 int/float(付款比例 ``ratio: number``);
    为真时只接受定点字符串(维护金额 ``amount`` 的权威形态是十进制
    字符串, 宪法 §7.3 禁止金额 JSON number; 不接受 int/float, 不能靠
    ``float → str`` 事后补回已丢精度)。
    """
    if isinstance(value, bool):
        raise _evaluation_fail(
            f"{field} 须为十进制数值",
            {field: value},
            f"evaluation.{field}",
        )
    if isinstance(value, str):
        if not allow_str:
            raise _evaluation_fail(
                f"{field} 须为数值",
                {field: value},
                f"evaluation.{field}",
            )
        text = value
    elif isinstance(value, (int, float)):
        if allow_str:
            raise _evaluation_fail(
                f"{field} 须为十进制定点字符串(禁止 JSON number)",
                {field: value},
                f"evaluation.{field}",
            )
        text = str(value)
    else:
        raise _evaluation_fail(
            f"{field} 须为十进制数值",
            {field: value},
            f"evaluation.{field}",
        )
    try:
        d = Decimal(text)
    except (InvalidOperation, ValueError):
        raise _evaluation_fail(
            f"{field} 十进制解析失败",
            {field: value},
            f"evaluation.{field}",
        ) from None
    if not d.is_finite():
        raise _evaluation_fail(
            f"{field} 须为有限数",
            {field: value},
            f"evaluation.{field}",
        )
    return d


def _as_fixed_str(d: Decimal) -> str:
    """Decimal → 规范定点字符串(冻结/复用身份用, 去指数形态)。

    纯表示规范化, 不做数值舍入: 不得经 ``normalize()``(其行为受
    Decimal 上下文精度影响, 会无提示改写已确认金额)。``format(d, "f")``
    不改变数值(构造器本就不按上下文截断位数), 此处仅去指数形态与尾零。
    """
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    return text


def parse_evaluation_input(raw: Mapping[str, Any] | None) -> EvaluationInput:
    """计算类任务评价确认输入类型化解析(任务提交边界唯一校验)。

    顶层任务输入 ``evaluation`` 块必填字典, 须恰含 ``evaluation_years``
    (正整数, 含建设期的财务评价总年限)、``construction_period``(非负整数
    建设期年数, 显式 0 合法, 不缺省为零)、``operation_years``(正整数投产后
    运营年限)、``discount_rate``(``0..1`` 区间内有限数, 宪法 §7.3 比例口径)、
    ``construction_payments``(付款比例清单)、``maintenance_expenses``
    (维护/换新清单); 缺失或形态非法 → 400 TASK-REQ-015(无默认值, 不回退)。
    必须满足 ``evaluation_years == construction_period + operation_years``;
    付款比例按十进制语义求和须精确等于 1(时点为开工起整数年
    ``0..construction_period``); 维护发生年为财务时轴索引
    ``construction_period+1..evaluation_years``(对应 ``CF[year]``),
    金额为非负十进制定点字符串(拒 JSON number, 无精度上限), 币种为非空
    字符串(与有效财务配置一致性由提交边界持快照核对, 不在此重复读取)。
    布尔值一律不接受为数值。
    返回不可变 ``EvaluationInput``。
    """
    if not isinstance(raw, Mapping):
        raise _evaluation_fail(
            "计算类任务须在顶层 evaluation 中明确确认评价时轴与折现率",
            {"evaluation": raw},
            "evaluation",
        )
    unknown = set(raw) - EVALUATION_INPUT_KEYS
    if unknown:
        raise _evaluation_fail(
            f"evaluation 含未知字段: {sorted(unknown)}",
            {"unknown": sorted(unknown)},
            "evaluation",
        )
    missing = EVALUATION_INPUT_KEYS - set(raw)
    if missing:
        raise _evaluation_fail(
            f"evaluation 缺少必填字段: {sorted(missing)}",
            {"missing": sorted(missing)},
            "evaluation",
        )
    years = raw.get("evaluation_years")
    if not isinstance(years, int) or isinstance(years, bool) or years < 1:
        raise _evaluation_fail(
            "evaluation.evaluation_years 须为正整数(无默认值)",
            {"evaluation_years": years},
            "evaluation.evaluation_years",
        )
    period = raw.get("construction_period")
    if not isinstance(period, int) or isinstance(period, bool) or period < 0:
        raise _evaluation_fail(
            "evaluation.construction_period 须为非负整数年(显式 0 合法, 不缺省)",
            {"construction_period": period},
            "evaluation.construction_period",
        )
    operation = raw.get("operation_years")
    if not isinstance(operation, int) or isinstance(operation, bool) or operation < 1:
        raise _evaluation_fail(
            "evaluation.operation_years 须为正整数运营年限(无默认值)",
            {"operation_years": operation},
            "evaluation.operation_years",
        )
    if years != period + operation:
        raise _evaluation_fail(
            "evaluation 须满足 evaluation_years == construction_period + operation_years",
            {
                "evaluation_years": years,
                "construction_period": period,
                "operation_years": operation,
            },
            "evaluation",
        )
    rate = raw.get("discount_rate")
    if (
        isinstance(rate, bool)
        or not isinstance(rate, (int, float))
        or not math.isfinite(rate)
        or rate < 0
        or rate > 1
    ):
        raise _evaluation_fail(
            "evaluation.discount_rate 须为 0..1 区间内有限数(比例口径, 无默认值)",
            {"discount_rate": rate},
            "evaluation.discount_rate",
        )
    payments_raw = raw.get("construction_payments")
    if not isinstance(payments_raw, (list, tuple)):
        raise _evaluation_fail(
            "evaluation.construction_payments 须为付款比例清单",
            {"construction_payments": payments_raw},
            "evaluation.construction_payments",
        )
    payments: list[ConstructionPayment] = []
    total = Decimal(0)
    for index, entry in enumerate(payments_raw):
        field = f"evaluation.construction_payments[{index}]"
        if not isinstance(entry, Mapping):
            raise _evaluation_fail(
                f"{field} 须为 {{t, ratio}} 字典",
                {"entry": entry},
                field,
            )
        unknown_entry = set(entry) - {"t", "ratio"}
        if unknown_entry or {"t", "ratio"} - set(entry):
            raise _evaluation_fail(
                f"{field} 须恰含 t 与 ratio",
                {"entry": dict(entry)},
                field,
            )
        t = entry.get("t")
        if not isinstance(t, int) or isinstance(t, bool) or t < 0 or t > period:
            raise _evaluation_fail(
                f"{field}.t 须为开工起整数年时点 0..{period}",
                {"t": t},
                f"{field}.t",
            )
        ratio = _as_decimal(
            entry.get("ratio"),
            f"construction_payments[{index}].ratio",
            allow_str=False,
        )
        if ratio < 0 or ratio > 1:
            raise _evaluation_fail(
                f"{field}.ratio 须在 0..1 内",
                {"ratio": entry.get("ratio")},
                f"{field}.ratio",
            )
        total += ratio
        payments.append(ConstructionPayment(t=t, ratio=_as_fixed_str(ratio)))
    if total != Decimal(1):
        raise _evaluation_fail(
            "evaluation.construction_payments 比例按十进制语义求和须精确等于 1",
            {"total": str(total)},
            "evaluation.construction_payments",
        )
    maintenance_raw = raw.get("maintenance_expenses")
    if not isinstance(maintenance_raw, (list, tuple)):
        raise _evaluation_fail(
            "evaluation.maintenance_expenses 须为维护/换新清单(无支出给空清单)",
            {"maintenance_expenses": maintenance_raw},
            "evaluation.maintenance_expenses",
        )
    maintenance: list[MaintenanceExpense] = []
    for index, entry in enumerate(maintenance_raw):
        field = f"evaluation.maintenance_expenses[{index}]"
        if not isinstance(entry, Mapping):
            raise _evaluation_fail(
                f"{field} 须为 {{year, amount, currency}} 字典",
                {"entry": entry},
                field,
            )
        if set(entry) != {"year", "amount", "currency"}:
            raise _evaluation_fail(
                f"{field} 须恰含 year、amount 与 currency",
                {"entry": dict(entry)},
                field,
            )
        year = entry.get("year")
        if not isinstance(year, int) or isinstance(year, bool) or year < period + 1 or year > years:
            raise _evaluation_fail(
                f"{field}.year 须为财务发生年 {period + 1}..{years}(对应 CF[year])",
                {"year": year},
                f"{field}.year",
            )
        amount = _as_decimal(
            entry.get("amount"),
            f"maintenance_expenses[{index}].amount",
            allow_str=True,
        )
        if amount < 0:
            raise _evaluation_fail(
                f"{field}.amount 须为非负十进制金额",
                {"amount": entry.get("amount")},
                f"{field}.amount",
            )
        currency = entry.get("currency")
        if not isinstance(currency, str) or not currency:
            raise _evaluation_fail(
                f"{field}.currency 须为非空币种字符串",
                {"currency": currency},
                f"{field}.currency",
            )
        maintenance.append(MaintenanceExpense(year=year, amount=_as_fixed_str(amount), currency=currency))
    return EvaluationInput(
        evaluation_years=years,
        construction_period=period,
        operation_years=operation,
        discount_rate=float(rate),
        construction_payments=tuple(payments),
        maintenance_expenses=tuple(maintenance),
    )


def check_tariff_horizon_coverage(
    *,
    baseline_resolution: str,
    baseline_start_year: int,
    operation_years: int,
    tariff_bindings: Mapping[str, Any],
    price_series: Mapping[str, Mapping[str, Any]],
) -> None:
    """时变价格运营期覆盖核对(任务提交边界唯一权威)。

    期望点数由运行起始年 + 已确认运营年限 + 分辨率逐年按公历唯一推导
    (``horizon_point_count``); 引用 ``time_series`` 价格的绑定, 其序列
    ``point_count`` 不等 → 400 TASK-REQ-017, 不重采样。``constant`` 价格
    无序列, 不参与核对。未知价格引用同样阻断(装配闸门已保证存在, 此处
    不假设)。
    """
    from iesplan.core.contracts import horizon_point_count

    expected = horizon_point_count(baseline_resolution, baseline_start_year, operation_years)
    for binding_id in sorted(tariff_bindings):
        entry = tariff_bindings[binding_id]
        if not isinstance(entry, Mapping):
            continue
        price_id = entry.get("price")
        meta = price_series.get(price_id) if isinstance(price_id, str) else None
        if meta is None:
            raise InvalidRequestError(
                "计费绑定引用的价格不存在, 无法核对全期覆盖",
                code="TASK-REQ-017",
                params={"binding_id": binding_id, "price": price_id},
                location={"object_type": "tariff_binding", "object_id": str(binding_id)},
            )
        if meta.get("kind") != "time_series":
            continue
        if meta.get("point_count") != expected:
            raise InvalidRequestError(
                "时变价格序列未覆盖运营期(点数不等于运营期总点数)",
                code="TASK-REQ-017",
                params={
                    "binding_id": binding_id,
                    "price": price_id,
                    "series_point_count": meta.get("point_count"),
                    "expected_point_count": expected,
                },
                location={"object_type": "tariff_binding", "object_id": str(binding_id)},
            )


@dataclass(frozen=True, slots=True)
class CalcSnapshotRecord:
    """计算快照（calc_snapshots 表公开视图，不可变，任务唯一输入）。

    长期快照字段深度不可变: 构造时把封存 ``dict``/``list`` 递归冻结为
    只读 ``MappingProxy``/``tuple``（先复制再冻结），不与任何模块共享
    可变 ``dict``/``list``。需要与 JSON 输入比较或序列化时，由调用边界
    统一转回普通 JSON 值。容差唯一权威是计算配置正文的 ``convergence``,
    本记录不设第二份 tolerances 字段。
    """

    id: int
    project_version_id: int
    dataset_version_ids: tuple[int, ...]
    calc_config_snapshot: Mapping[str, Any]
    random_seed: int
    #: 快照 schema 版本(宪法 §7.1/§7.7: 非空必填, 创建时显式写入)。
    schema_version: str
    #: 提交边界经组合根已发布生成器目录解析冻结的配套结果适配器精确引用(必填)。
    result_adapter: str
    #: 提交边界冻结的执行器精确引用(单声明; 多声明无公开选择规则即阻断)(必填)。
    executor: str
    #: 逐装配实例数学贡献(``{instance_id: contribution dict}``，必填):
    #: 提交边界与装配闸门同一次设备解析冻结；Worker 只恢复，不重跑校验。
    device_models: Mapping[str, Any]
    #: 评价确认冻结块(顶层必填, 快照主版本 3.0.0): 年限/折现率/确认人/
    #: 确认时间, 由提交边界类型化解析后服务端写入, 不混入封闭
    #: ``CalculationConfig`` 正文, 也不从宽泛任务参数字典读取。
    evaluation: Mapping[str, Any]
    program_version: str | None = None
    extension_versions: Mapping[str, Any] | None = None
    canonical_assembly_text: str | None = None
    assembly_receipt: Mapping[str, Any] | None = None
    created_by: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_version_ids", tuple(self.dataset_version_ids))
        for name in (
            "calc_config_snapshot",
            "extension_versions",
            "assembly_receipt",
            "device_models",
            "evaluation",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _freeze_snapshot_value(value))


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """任务行（tasks 表公开视图）。"""

    id: int
    project_id: int
    type: str
    status: str
    requested_by: int
    business_outcome: str | None = None
    idempotency_key: str | None = None
    calc_snapshot_id: int | None = None
    priority: int = 0
    deadline: str | None = None
    attempt_count: int = 0
    max_attempts: int = 3
    superseded_by_task_id: int | None = None
    requested_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class TaskAttemptRecord:
    """任务尝试行（task_attempts 表公开视图）。"""

    id: int
    task_id: int
    attempt_no: int
    status: str
    worker_id: str | None = None
    stop_reason: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True, slots=True)
class TaskLeaseRecord:
    """任务租约（task_leases 表公开视图；token 为不透明字符串）。"""

    id: int
    attempt_id: int
    lease_token: str
    status: str
    acquired_by: str | None = None
    expires_at: str | None = None
    renewed_at: str | None = None


@dataclass(frozen=True, slots=True)
class TaskProgressRecord:
    """任务进度（task_progress 表公开视图，每尝试至多一行）。"""

    id: int
    attempt_id: int
    progress_percent: float
    stage: str | None = None
    detail: dict[str, Any] | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class TaskDiagnosticRecord:
    """任务诊断（task_diagnostics 表公开视图，不可变）。"""

    id: int
    task_id: int
    level: str
    message: str
    attempt_id: int | None = None
    code: str | None = None
    stack_trace: str | None = None
    context: dict[str, Any] | None = None
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class ComputeSlotRecord:
    """计算并发槽（compute_slots 表公开视图）。"""

    id: int
    pool_name: str
    status: str
    capacity: int
    in_use: int
    current_attempt_id: int | None = None


@dataclass(frozen=True, slots=True)
class UncertaintySnapshotRecord:
    """不确定性快照（uncertainty_snapshots 表公开视图，不可变）。"""

    id: int
    calc_snapshot_id: int
    method: str
    n_samples: int
    random_seed: int
    distributions: dict[str, Any]
    created_by: int = 0


@dataclass(frozen=True, slots=True)
class SampleTaskRecord:
    """采样任务（sample_tasks 表公开视图）。"""

    id: int
    uncertainty_snapshot_id: int
    sample_index: int
    status: str
    parent_task_id: int | None = None
    parent_sample_id: int | None = None
    depth: int = 0
    params: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SampleRecordRecord:
    """样本记录（sample_records 表公开视图）。"""

    id: int
    sample_task_id: int
    variable_name: str
    value: float
    unit: str | None = None


# ---------------------------------------------------------------------------
# 任务类型 / 队列池 / 状态机（单领域规则唯一权威；application 只复用）
# ---------------------------------------------------------------------------

#: 全部任务类型
TASK_TYPES: tuple[str, ...] = (
    "calc",
    "optimization",
    "uncertainty",
    "analysis",
    "import",
    "export",
    "report",
    "dataset_build",
)
#: 计算类任务(必须绑定 calc_snapshot_id)
COMPUTE_TYPES: tuple[str, ...] = ("calc", "optimization", "uncertainty", "analysis")
#: 任务类型 → 队列池
POOL_BY_TYPE: dict[str, str] = {
    "calc": "compute",
    "optimization": "compute",
    "uncertainty": "compute",
    "analysis": "compute",
    "report": "io",
    "dataset_build": "io",
    "export": "io",
    "import": "io",
}
#: 终态(除授权的手动重试迁出外不可再迁移)
TERMINAL_STATUSES: tuple[str, ...] = ("completed", "cancelled", "timed_out", "failed")
#: 可手动重试的终态(失败/超时/已取消 → queued; completed 永不重开)
RETRYABLE_STATUSES: tuple[str, ...] = ("failed", "timed_out", "cancelled")
#: 状态机合法迁移(终态中仅授权的手动重试可迁出, 且只能回到 queued)
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"completed", "cancelling", "queued", "timed_out", "failed"}),
    # cancelling → cancelled 由确认取消完成; → completed 为取消竞态
    # 下先落终态者为准的异常路径
    "cancelling": frozenset({"cancelled", "completed", "timed_out", "failed"}),
    "failed": frozenset({"queued"}),
    "timed_out": frozenset({"queued"}),
    "cancelled": frozenset({"queued"}),
}
#: 租约 TTL(秒, 默认 60 s)
LEASE_TTL_SECONDS = 60


def check_transition(task: TaskRecord, new_status: str) -> None:
    """状态机校验: 以合法迁移表为唯一权威; 终态迁出仅授权的手动重试。

    终态阻断沿用 TASK-STATE-002, 其他非法跳转沿用 TASK-STATE-003。
    """
    if new_status not in VALID_TRANSITIONS.get(task.status, frozenset()):
        if task.status in TERMINAL_STATUSES:
            raise TaskStateError(
                "终态任务不可迁移状态",
                code="TASK-STATE-002",
                params={"task_id": task.id, "status": task.status},
                location={"object_type": "task", "object_id": task.id},
            )
        raise TaskStateError(
            "非法状态迁移",
            code="TASK-STATE-003",
            params={"task_id": task.id, "from": task.status, "to": new_status},
            location={"object_type": "task", "object_id": task.id},
        )


#: 合法业务结局(与 tasks 表 ck_tasks_outcome 约束一致, 本域为唯一权威)
BUSINESS_OUTCOMES: tuple[str, ...] = (
    "normal_completion",
    "no_recommendation",
    "no_feasible_multi_objective",
    "partial_batch",
    "restricted_results",
    "insufficient_evidence",
)

#: 求解器状态 → 业务结局映射
_SOLVER_OUTCOME: dict[str, str] = {
    "OPTIMAL": "normal_completion",
    "TIME_LIMIT_WITH_INCUMBENT": "restricted_results",
    "NO_FEASIBLE_FOUND": "no_recommendation",
    "INFEASIBLE_BY_IRR_FLOOR": "no_recommendation",
    "BASE_INFEASIBLE": "no_recommendation",
    "MODEL_AUDIT_FAIL": "insufficient_evidence",
    "NO_PARETO_FEASIBLE": "no_feasible_multi_objective",
    "PARTIAL_BATCH": "partial_batch",
}


def map_business_outcome(solver_status: str) -> str:
    """求解器状态 → 业务结局(未知状态保守视为 normal_completion)。"""
    return _SOLVER_OUTCOME.get(solver_status, "normal_completion")
