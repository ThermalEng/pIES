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
        return MappingProxyType(
            {key: _freeze_snapshot_value(item) for key, item in value.items()}
        )
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
SNAPSHOT_SCHEMA_VERSION: str = "3.0.0"


#: 评价确认输入合法键(任务提交边界唯一权威; 未知键拒绝, 不静默忽略)。
EVALUATION_INPUT_KEYS: frozenset[str] = frozenset({"evaluation_years", "discount_rate"})


@dataclass(frozen=True, slots=True)
class EvaluationInput:
    """计算任务评价确认输入(任务级类型化输入)。

    由任务提交边界 ``parse_evaluation_input`` 从顶层任务输入 ``evaluation``
    块唯一解析, 不从宽泛任务参数字典读取, 也不混入封闭 ``CalculationConfig``
    正文。``evaluation_years`` 为确认的评价年限(正整数, 年); ``discount_rate``
    为确认的折现率(宪法 §7.3 比例口径 ``0..1``)。
    """

    evaluation_years: int
    discount_rate: float


def parse_evaluation_input(raw: Mapping[str, Any] | None) -> EvaluationInput:
    """计算类任务评价确认输入类型化解析(任务提交边界唯一校验)。

    顶层任务输入 ``evaluation`` 块必填字典, 须恰含 ``evaluation_years``
    (正整数, 不接受布尔值)与 ``discount_rate``(``0..1`` 区间内有限数,
    宪法 §7.3 比例口径, 不接受布尔值); 缺失或形态非法 → 400
    TASK-REQ-015(无默认值, 不回退)。评价年限接受 ``>= 1`` 的连续全期
    (全期连续时域见 0.12 Roadmap; 全周期求解由后续 F 切片贯通)。
    返回不可变 ``EvaluationInput``。
    """
    if not isinstance(raw, Mapping):
        raise InvalidRequestError(
            "计算类任务须在顶层 evaluation 中明确确认评价年限与折现率",
            code="TASK-REQ-015",
            params={"evaluation": raw},
            location={"object_type": "task", "field": "evaluation"},
        )
    unknown = set(raw) - EVALUATION_INPUT_KEYS
    if unknown:
        raise InvalidRequestError(
            f"evaluation 含未知字段: {sorted(unknown)}",
            code="TASK-REQ-015",
            params={"unknown": sorted(unknown)},
            location={"object_type": "task", "field": "evaluation"},
        )
    years = raw.get("evaluation_years")
    if not isinstance(years, int) or isinstance(years, bool) or years < 1:
        raise InvalidRequestError(
            "evaluation.evaluation_years 须为正整数(无默认值)",
            code="TASK-REQ-015",
            params={"evaluation_years": years},
            location={"object_type": "task", "field": "evaluation.evaluation_years"},
        )
    rate = raw.get("discount_rate")
    if (
        isinstance(rate, bool)
        or not isinstance(rate, (int, float))
        or not math.isfinite(rate)
        or rate < 0
        or rate > 1
    ):
        raise InvalidRequestError(
            "evaluation.discount_rate 须为 0..1 区间内有限数(比例口径, 无默认值)",
            code="TASK-REQ-015",
            params={"discount_rate": rate},
            location={"object_type": "task", "field": "evaluation.discount_rate"},
        )
    return EvaluationInput(evaluation_years=years, discount_rate=float(rate))


def check_tariff_horizon_coverage(
    *,
    baseline_resolution: str,
    baseline_start_year: int,
    evaluation_years: int,
    tariff_bindings: Mapping[str, Any],
    price_series: Mapping[str, Mapping[str, Any]],
) -> None:
    """时变价格全期覆盖核对(任务提交边界唯一权威)。

    期望点数由基线起始年 + 已确认评价年限 + 分辨率逐年按公历唯一推导
    (``horizon_point_count``); 引用 ``time_series`` 价格的绑定, 其序列
    ``point_count`` 不等 → 400 TASK-REQ-017, 不重采样。``constant`` 价格
    无序列, 不参与核对。未知价格引用同样阻断(装配闸门已保证存在, 此处
    不假设)。
    """
    from iesplan.core.contracts import horizon_point_count

    expected = horizon_point_count(baseline_resolution, baseline_start_year, evaluation_years)
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
                "时变价格序列未覆盖评价全期(点数不等于全周期总点数)",
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
#: 终态(终态不可再迁移)
TERMINAL_STATUSES: tuple[str, ...] = ("completed", "cancelled", "timed_out", "failed")
#: 状态机合法迁移
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"completed", "cancelling", "queued", "timed_out", "failed"}),
    # cancelling → cancelled 由确认取消完成; → completed 为取消竞态
    # 下先落终态者为准的异常路径
    "cancelling": frozenset({"cancelled", "completed", "timed_out", "failed"}),
}
#: 租约 TTL(秒, 默认 60 s)
LEASE_TTL_SECONDS = 60


def check_transition(task: TaskRecord, new_status: str) -> None:
    """状态机校验: 终态不可迁移; 非法跳转抛 TaskStateError。"""
    if task.status in TERMINAL_STATUSES:
        raise TaskStateError(
            "终态任务不可迁移状态",
            code="TASK-STATE-002",
            params={"task_id": task.id, "status": task.status},
            location={"object_type": "task", "object_id": task.id},
        )
    if new_status not in VALID_TRANSITIONS.get(task.status, frozenset()):
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
