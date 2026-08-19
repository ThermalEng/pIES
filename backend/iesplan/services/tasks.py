"""任务与资源调度服务(U08 快照装配 / 任务域服务)。

对应 docs/spec/03-task-scheduling.md 与 01-db-schema.md 第 6、7 节:
- 快照装配: 从项目版本(或草稿固化)+ 数据集版本 + 计算配置组装不可变快照,
  按 content_hash(sha256) 去重复用(规格 2.2);
- 任务生命周期: 幂等创建(幂等键 + 同快照去重)、存储门禁、状态机
  (queued→running→completed/cancelling→cancelled/timed_out/failed, 终态不可迁移)、
  取消传播(批量子任务)、手动重试(复用同一快照)、并发槽(compute/io 两池);
- 进度: PG 持久进度(UPSERT 每尝试一行)+ Redis 秒级进度(可重建)。

一致性原则: 任务的权威事实(状态/尝试/租约/进度/槽)全部写 PostgreSQL;
Redis 队列/进度/心跳为可重建视图(见 services/queue.py)。Worker 消费端在下一
波次实现, 本模块提供状态机/API 可调用的服务入口(claim_and_run 等)。

本层服务不主动 commit, 事务边界由 API 层(请求级)控制。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from iesplan import __version__
from iesplan.config import settings
from iesplan.core.diagnostics import (
    SEVERITY_BLOCKING,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SYS_STORE_CORRUPT,
    SYS_STORE_QUOTA_EXCEEDED,
    TASK_DATA_HASH_MISMATCH,
    TASK_DATA_SNAPSHOT_MISSING,
    TASK_QUEUED,
    TASK_TIMEOUT,
)
from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.core.idgen import new_id, sha256_hex
from iesplan.models.audit import StoredObject
from iesplan.models.calc import (
    CalcSnapshot,
    ComputeSlot,
    Task,
    TaskAttempt,
    TaskDiagnostic,
    TaskLease,
    TaskProgress,
)
from iesplan.models.common import IDEMPOTENCY_KEY_RE
from iesplan.models.dataset import DatasetFile
from iesplan.models.identity import User
from iesplan.models.project import Draft, Project, ProjectVersion
from iesplan.models.result import Report
from iesplan.models.uncertainty import SampleTask
from iesplan.services import identity as identity_service
from iesplan.services import project as project_service
from iesplan.services import queue

# ---------------------------------------------------------------------------
# 常量: 任务类型 / 池 / 状态机
# ---------------------------------------------------------------------------

#: 全部任务类型(01 §7.2 ck_tasks_type)
TASK_TYPES: tuple[str, ...] = (
    "calc", "optimization", "uncertainty", "import", "export", "report", "dataset_build"
)
#: 计算类任务(必须绑定 calc_snapshot_id, 规格 2.1)
COMPUTE_TYPES: tuple[str, ...] = ("calc", "optimization", "uncertainty")
#: 任务类型 → 队列池(规格 2.1)
POOL_BY_TYPE: dict[str, str] = {
    "calc": "compute",
    "optimization": "compute",
    "uncertainty": "compute",
    "report": "io",
    "dataset_build": "io",
    "export": "io",
    "import": "io",
}
#: 终态(01 §7.2 tg_tasks_terminal: 终态不可再迁移)
TERMINAL_STATUSES: tuple[str, ...] = ("completed", "cancelled", "timed_out", "failed")
#: 状态机合法迁移(规格 3.1; 数据库不建模完整状态机, 由本服务校验)
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"completed", "cancelling", "queued", "timed_out", "failed"}),
    # cancelling → cancelled 由 acknowledge_cancel 完成; → completed 为取消竞态
    # 下先落终态者为准的异常路径(规格 3.1/6.1)
    "cancelling": frozenset({"cancelled", "completed", "timed_out", "failed"}),
}
#: 租约 TTL(秒, 规格 4.2 默认 60 s)
LEASE_TTL_SECONDS = 60
#: io 池默认并发槽(规格 附录 A: io 默认 2)
IO_SLOT_CAPACITY = 2
#: 逐时结果每行估算字节(规格 8.1: ~1 KB)
_HOURLY_BYTES_PER_ROW = 1024
#: 中间文件系数(规格 8.1: k_inter 默认 0.5)
_INTERMEDIATE_FACTOR = 0.5
#: 证据包系数(规格 8.1: 默认 0.1)
_EVIDENCE_FACTOR = 0.1

#: 求解器状态 → 业务结局映射(规格 3.2 表; 求解器状态码取自 02 §11.4)
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


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class InvalidRequestError(AppError):
    """请求/参数校验失败(HTTP 400, U08 域内稳定标识)。"""

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
    """终态任务不可取消(HTTP 409, 规格 6.1)。"""

    code = "TASK-CANCEL-001"
    http_status = 409
    severity = SEVERITY_ERROR
    message_key = "ies.diag.task.cancel_denied"


class StorageQuotaError(AppError):
    """存储门禁未通过(HTTP 409, 规格 8.2; blocking 级 SYS-STORE-003)。"""

    code = SYS_STORE_QUOTA_EXCEEDED
    http_status = 409
    severity = SEVERITY_BLOCKING
    message_key = "ies.diag.store.quota_exceeded"


@dataclass(frozen=True, slots=True)
class Claim:
    """一次领取结果(尝试 + 租约 + fencing token, 规格 4.1)。"""

    task_id: int
    attempt_id: int
    attempt_no: int
    lease_token: UUID


@dataclass(frozen=True, slots=True)
class StorageEstimate:
    """存储门禁估算结果(规格 8.1/8.2)。"""

    need: int
    avail: int
    blocked: bool
    suggestions: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "need_bytes": self.need,
            "avail_bytes": self.avail,
            "blocked": self.blocked,
            "suggestions": self.suggestions,
        }


# ---------------------------------------------------------------------------
# 内容寻址工具(与 services/project.py 同一规范化约定)
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """递归转换为 JSON 安全值(datetime → ISO 字符串, Decimal → float)。"""
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _canonical_json(content: dict) -> str:
    """规范化 JSON(键排序、紧凑分隔), 保证相同内容的哈希稳定(可复现性)。"""
    return json.dumps(_jsonable(content), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_content_by_hash(db: Session, content_hash: str) -> dict:
    """按内容校验值读取内容对象并校验(缺失/哈希不符视为数据损坏)。"""
    obj = db.execute(select(StoredObject).where(StoredObject.oid == content_hash)).scalar_one_or_none()
    if obj is None or not obj.storage_path:
        raise AppError(
            "内容对象缺失(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
            location={"object_type": "object", "object_id": content_hash},
        )
    path = Path(settings.data_dir) / "objects" / obj.storage_path
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AppError(
            "内容对象读取失败(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
            location={"object_type": "object", "object_id": content_hash},
        ) from exc
    if sha256_hex(raw.encode("utf-8")) != content_hash:
        raise AppError(
            "内容校验失败(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
            location={"object_type": "object", "object_id": content_hash},
        )
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise AppError(
            "内容对象解析失败(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
        ) from exc
    if not isinstance(parsed, dict):
        raise AppError(
            "内容对象结构非法(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
        )
    return parsed


# ---------------------------------------------------------------------------
# 项目/草稿/版本解析
# ---------------------------------------------------------------------------


def _get_project(db: Session, project_id: int) -> Project:
    """按 id 取项目; 不存在或已删除(软删)一律 404。"""
    project = db.get(Project, project_id)
    if project is None or project.status == "deleted":
        raise NotFoundError(
            "项目不存在",
            params={"project_id": project_id},
            location={"object_type": "project", "object_id": project_id},
        )
    return project


def _get_current_draft(db: Session, project: Project) -> Draft:
    """取项目当前草稿(is_current=true 且修订最大者); 缺失视为数据损坏。"""
    draft = db.execute(
        select(Draft)
        .where(Draft.project_id == project.id, Draft.is_current.is_(True))
        .order_by(Draft.revision.desc())
        .limit(1)
    ).scalar_one_or_none()
    if draft is None:
        raise AppError(
            "项目缺少当前草稿(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
            location={"object_type": "project", "object_id": project.id},
        )
    return draft


def _resolve_project_inputs(
    db: Session, project: Project, actor: User, *, freeze: bool
) -> tuple[ProjectVersion | None, dict]:
    """解析任务输入: 项目版本(或当前草稿内容, 需要时固化)。

    返回 (version, content): version 为 None 表示未固化(仅读草稿内容)。
    草稿与版本内容的领域字段(模型/配置/数据集绑定)一致, 仅差命令簿记,
    因此固化的 content 可直接用于快照组装。
    """
    if project.current_version_id is not None:
        version = db.get(ProjectVersion, project.current_version_id)
        if version is None:
            raise AppError(
                "项目版本指针缺失(数据损坏)",
                code=SYS_STORE_CORRUPT,
                severity=SEVERITY_ERROR,
                message_key="ies.diag.store.corrupt",
                location={"object_type": "project", "object_id": project.id},
            )
        return version, _load_content_by_hash(db, version.content_hash)
    draft = _get_current_draft(db, project)
    content = _load_content_by_hash(db, draft.content_hash)
    if not freeze:
        return None, content
    # 草稿固化: 借 U03 版本服务创建不可变项目版本(计算输入固定, RPD 9.4)
    version = project_service.create_version(
        db, actor, project.id, name="计算任务自动固化", description=None, reason="snapshot_freeze"
    )
    return version, content


def _bound_dataset_ids(content: dict) -> list[int]:
    """从项目内容取出绑定的数据集版本 id 清单(01 §3.4 数据集绑定)。"""
    return [
        int(binding["dataset_version_id"])
        for binding in content.get("dataset_bindings", [])
        if binding.get("dataset_version_id") is not None
    ]


def _derive_random_seed(calc_config: dict) -> int:
    """随机种子强制非 NULL(规格 0.2/2.2): 配置缺省时按配置内容确定性派生。

    相同输入 → 相同种子 → 相同快照哈希, 不破坏可复现性。
    """
    digest = sha256_hex(_canonical_json(calc_config).encode("utf-8"))
    return int(digest[:12], 16)  # 取 48 bit 作为非负种子


# ---------------------------------------------------------------------------
# 快照装配(规格 2.2: 版本 + 数据集 + 配置全文 + 程序版本 + 种子 + 容差 + sha256 去重)
# ---------------------------------------------------------------------------


def assemble_snapshot(
    db: Session,
    project_id: int,
    task_type: str,
    config: dict[str, Any] | None = None,
    user: User | None = None,
) -> CalcSnapshot:
    """组装不可变计算快照(内容去重复用, 规格 2.2)。

    绑定: 项目版本(无版本时固化当前草稿)、数据集版本 id 清单、计算配置全文、
    程序版本、受控扩展版本、随机种子(强制非 NULL)、容差; content_hash 对全部
    输入规范化序列化后取 sha256, 相同输入必然同哈希(可复现), 已有同哈希
    快照直接复用(快照不可变故复用安全)。任务级 config 并入快照的
    calc_config_snapshot.task_params, 保证"相同输入 → 相同哈希"。
    """
    project = _get_project(db, project_id)
    actor = user or db.get(User, project.owner_id)
    if actor is None:
        raise InvalidRequestError("无法确定快照创建者", params={"project_id": project_id})
    version, content = _resolve_project_inputs(db, project, actor, freeze=True)
    calc_config: dict[str, Any] = dict(content.get("calc_config") or {})
    if config:
        calc_config["task_params"] = _jsonable(config)
    random_seed = calc_config.get("random_seed")
    if random_seed is None:
        random_seed = _derive_random_seed(calc_config)
    dataset_ids = _bound_dataset_ids(content)
    tolerances = calc_config.get("tolerances") or {}
    extensions = content.get("extensions") or {}

    hash_input = {
        "project_version_id": version.id,
        "dataset_version_ids": dataset_ids,
        "calc_config": calc_config,
        "program_version": __version__,
        "extension_versions": extensions,
        "random_seed": random_seed,
        "tolerances": tolerances,
    }
    content_hash = sha256_hex(_canonical_json(hash_input).encode("utf-8"))

    existing = db.execute(
        select(CalcSnapshot).where(CalcSnapshot.content_hash == content_hash)
    ).scalar_one_or_none()
    if existing is not None:
        return existing  # 相同输入复用既有快照(不可变, 去重安全)

    snapshot = CalcSnapshot(
        project_version_id=version.id,
        dataset_version_ids=dataset_ids,
        calc_config_snapshot=calc_config,
        program_version=__version__,
        extension_versions=extensions,
        random_seed=random_seed,
        tolerances=tolerances,
        content_hash=content_hash,
        created_by=actor.id,
    )
    db.add(snapshot)
    db.flush()
    return snapshot


# ---------------------------------------------------------------------------
# 存储门禁(规格 8: 提交前估算 + 安全阈值 + 清理建议)
# ---------------------------------------------------------------------------


def _resolve_storage_estimate_inputs(db: Session, project: Project, actor: User) -> tuple[dict, list[int]]:
    """只读解析估算输入(版本或草稿内容, 不固化不写库)。"""
    _version, content = _resolve_project_inputs(db, project, actor, freeze=False)
    return content, _bound_dataset_ids(content)


def list_cleanup_suggestions(db: Session) -> list[dict[str, Any]]:
    """清理建议(规格 8.3: 按"最安全 → 最激进"排序, 每项含对象清单与预计释放量)。"""
    suggestions: list[dict[str, Any]] = []

    orphaned = db.execute(
        sa.select(
            sa.func.count(StoredObject.id),
            sa.func.coalesce(sa.func.sum(StoredObject.size_bytes), 0),
        ).where(StoredObject.status == "orphaned")
    ).one()
    suggestions.append({
        "action": "cleanup_orphaned_objects",
        "message_key": "ies.fix.store.orphaned",
        "count": int(orphaned[0] or 0),
        "estimate_bytes": int(orphaned[1] or 0),
    })

    report_count = db.execute(sa.select(sa.func.count(Report.id))).scalar() or 0
    suggestions.append({
        "action": "archive_old_reports",
        "message_key": "ies.fix.store.reports",
        "count": int(report_count),
        "estimate_bytes": 0,  # 归档释放量由 U11 对象服务按实际对象计算
    })

    terminal_tasks = db.execute(
        sa.select(sa.func.count(Task.id)).where(Task.status.in_(TERMINAL_STATUSES))
    ).scalar() or 0
    suggestions.append({
        "action": "cleanup_terminal_task_files",
        "message_key": "ies.fix.store.task_files",
        "count": int(terminal_tasks),
        "estimate_bytes": 0,
    })

    suggestions.append({
        "action": "archive_project_versions",
        "message_key": "ies.fix.store.versions",
        "count": 0,
        "estimate_bytes": 0,
    })
    suggestions.append({
        "action": "reduce_samples_or_horizon",
        "message_key": "ies.fix.store.business_throttle",
        "count": 0,
        "estimate_bytes": 0,
    })
    return suggestions


def estimate_storage(
    db: Session, project_id: int, task_type: str, config: dict[str, Any] | None = None
) -> StorageEstimate:
    """提交前存储需求估算与安全阈值检查(规格 8.1/8.2)。

    S_need = S_snap + S_inter + S_hourly(+S_samples) + S_evid;
    S_avail = min(Σ objects.quota_bytes − Σ size_bytes, 卷空闲空间);
    配额未配置(Σ quota = 0)时仅以卷空闲空间为准。
    """
    project = _get_project(db, project_id)
    actor = db.get(User, project.owner_id)
    if actor is None:
        raise NotFoundError("项目所有者不存在", params={"project_id": project_id})
    content, dataset_ids = _resolve_storage_estimate_inputs(db, project, actor)
    params = config or {}

    # 快照与输入: 数据集版本对象大小之和
    snap_bytes = 0
    for dvid in dataset_ids:
        total = db.execute(
            sa.select(sa.func.coalesce(sa.func.sum(DatasetFile.size_bytes), 0)).where(
                DatasetFile.dataset_version_id == dvid
            )
        ).scalar()
        snap_bytes += int(total or 0)

    # 逐时结果: 行数 × ~1 KB(Y = 规划年数; 多目标解点 × 解点数)
    years = int(params.get("horizon_years", 1) or 1)
    n_solutions = int(params.get("n_solutions", 1) or 1)
    hourly_rows = 8760 * max(years, 1) * max(n_solutions, 1)
    hourly_bytes = hourly_rows * _HOURLY_BYTES_PER_ROW
    # 样本结果(uncertainty 批次: 样本数 × 逐时规模)
    sample_bytes = 0
    if task_type == "uncertainty":
        n_samples = int(params.get("n_samples", 0) or 0)
        sample_bytes = max(n_samples, 0) * hourly_bytes
    result_bytes = hourly_bytes + sample_bytes
    inter_bytes = int(_INTERMEDIATE_FACTOR * result_bytes)
    evid_bytes = int(_EVIDENCE_FACTOR * (snap_bytes + result_bytes))
    need = snap_bytes + inter_bytes + result_bytes + evid_bytes

    # 可用空间: min(配额余额, 卷空闲空间); 配额未配置视为无限
    used, quota = db.execute(
        sa.select(
            sa.func.coalesce(sa.func.sum(StoredObject.size_bytes), 0),
            sa.func.coalesce(sa.func.sum(StoredObject.quota_bytes), 0),
        ).where(StoredObject.status != "deleted")
    ).one()
    volume_free = shutil.disk_usage(settings.data_dir).free
    if int(quota or 0) > 0:
        avail = max(int(quota) - int(used), 0)
    else:
        avail = volume_free
    avail = min(avail, volume_free)

    blocked = need > avail - settings.storage_min_free_bytes
    suggestions = list_cleanup_suggestions(db) if blocked else []
    return StorageEstimate(need=need, avail=avail, blocked=blocked, suggestions=suggestions)


# ---------------------------------------------------------------------------
# 任务创建(幂等键 + 存储门禁 + 快照 + 入队)
# ---------------------------------------------------------------------------


def _get_task(db: Session, task_id: int) -> Task:
    """按 id 取任务; 不存在 404。"""
    task = db.get(Task, task_id)
    if task is None:
        raise NotFoundError(
            "任务不存在",
            params={"task_id": task_id},
            location={"object_type": "task", "object_id": task_id},
        )
    return task


def ensure_task_belongs(db: Session, project_id: int, task_id: int) -> Task:
    """任务必须属于该项目(否则 404, 不泄露其他项目任务存在性)。"""
    task = _get_task(db, task_id)
    if task.project_id != project_id:
        raise NotFoundError(
            "任务不存在", params={"task_id": task_id, "project_id": project_id},
            location={"object_type": "task", "object_id": task_id},
        )
    return task


def _write_diagnostic(
    db: Session,
    task_id: int,
    *,
    level: str,
    code: str,
    message: str,
    attempt_id: int | None = None,
    stack_trace: str | None = None,
    context: dict[str, Any] | None = None,
) -> TaskDiagnostic:
    """写入任务诊断(不可变, 只 INSERT; 01 §7.6)。"""
    diag = TaskDiagnostic(
        task_id=task_id,
        attempt_id=attempt_id,
        level=level,
        code=code,
        message=message,
        stack_trace=stack_trace,
        context=context,
    )
    db.add(diag)
    db.flush()
    return diag


def create_task(
    db: Session,
    user: User,
    project_id: int,
    task_type: str,
    config: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    parent_task_id: int | None = None,
) -> Task:
    """创建任务(规格 2.2 流程: 幂等检查 → 门禁 → 快照 → INSERT → 入队)。

    - 幂等键命中: 返回既有任务(replay=True), 不重复创建、不重复扣配额;
    - 存储门禁(仅计算类): 估算不足 → 409 + SYS-STORE-003 blocking 诊断 + 清理建议;
    - 快照: 相同输入复用同一 calc_snapshot_id(sha256 去重);
    - 重复提交去重: 同 (project, type, snapshot) 的非终态任务 → 复用返回
      (duplicate=True, 规格"短时间重复 → 复用并提示");
    - 入队: 按类型进入 compute/io 逻辑队列(Redis, 可重建视图)。
    """
    project_service.ensure_access(db, user, project_id, "edit")
    project = _get_project(db, project_id)
    if project.status != "active":
        raise ConflictError("项目已归档或已删除, 不能提交任务", params={"project_id": project_id})
    if task_type not in TASK_TYPES:
        raise InvalidRequestError("未知任务类型", code="TASK-REQ-002", params={"task_type": task_type})
    if idempotency_key is not None and not re.fullmatch(IDEMPOTENCY_KEY_RE, idempotency_key):
        raise InvalidRequestError(
            "幂等键格式非法(须匹配 ^[A-Za-z0-9._:-]{1,128}$)",
            code="TASK-REQ-003", params={"idempotency_key": idempotency_key},
        )
    if parent_task_id is not None:
        parent = _get_task(db, parent_task_id)
        if parent.project_id != project_id:
            raise InvalidRequestError(
                "父任务不属于该项目", code="TASK-REQ-004",
                params={"parent_task_id": parent_task_id, "project_id": project_id},
            )

    # 1) 幂等命中(唯一索引 uq_tasks_idempotency_key 兜底)
    if idempotency_key is not None:
        existing = db.execute(
            select(Task).where(Task.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing is not None:
            existing.replay = True  # 返回标记(非列属性, 供 API 呈现)
            return existing

    snapshot: CalcSnapshot | None = None
    if task_type in COMPUTE_TYPES:
        # 2) 存储门禁(门禁失败不创建快照不创建任务, 规格 8.2)
        estimate = estimate_storage(db, project_id, task_type, config)
        if estimate.blocked:
            raise StorageQuotaError(
                "存储空间不足, 任务提交被拒绝",
                params={"need_bytes": estimate.need, "avail_bytes": estimate.avail,
                        "min_pad_bytes": settings.storage_min_free_bytes,
                        "suggestions": estimate.suggestions},
                location={"object_type": "project", "object_id": project_id},
            )
        # 3) 快照装配(去重复用)
        snapshot = assemble_snapshot(db, project_id, task_type, config=config, user=user)
        # 4) 重复提交去重(仅无幂等键时生效): 同 (project, type, snapshot) 非终态
        #    任务复用(规格 2.2"短时间重复 → 复用并提示"); 携带幂等键的提交以
        #    幂等键为去重机制, 不参与快照去重(允许用户刻意提交同类任务)
        if idempotency_key is None:
            duplicate = db.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.type == task_type,
                    Task.calc_snapshot_id == snapshot.id,
                    Task.status.in_(("queued", "running", "cancelling")),
                )
                .order_by(Task.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if duplicate is not None:
                duplicate.duplicate = True
                return duplicate
    # io 类任务(report/export/import/dataset_build): 无快照, 以幂等键去重为主

    priority = 0
    deadline: datetime | None = None
    if config:
        try:
            priority = int(config.get("priority", 0))
        except (TypeError, ValueError):
            raise InvalidRequestError("priority 须为整数", code="TASK-REQ-005") from None
        raw_deadline = config.get("deadline")
        if raw_deadline is not None:
            try:
                deadline = datetime.fromisoformat(str(raw_deadline).replace("Z", "+00:00"))
            except ValueError:
                raise InvalidRequestError("deadline 须为 ISO 时间", code="TASK-REQ-005") from None

    pool = POOL_BY_TYPE[task_type]
    trace_id = new_id("trc-")
    task = Task(
        project_id=project_id,
        type=task_type,
        status="queued",
        idempotency_key=idempotency_key,
        calc_snapshot_id=snapshot.id if snapshot is not None else None,
        requested_by=user.id,
        priority=priority,
        deadline=deadline,
    )
    db.add(task)
    db.flush()
    _write_diagnostic(
        db, task.id, level=SEVERITY_INFO, code=TASK_QUEUED, message="任务已排队",
        context={"trace_id": trace_id, "queue": pool, "snapshot_id": task.calc_snapshot_id,
                 "parent_task_id": parent_task_id},
    )
    # 5) 入队(可重建视图; 权威事实 = tasks.status='queued')
    queue.enqueue(
        task.id, pool, task_type=task_type, snapshot_id=task.calc_snapshot_id,
        priority=priority, trace_id=trace_id,
    )
    return task


# ---------------------------------------------------------------------------
# 并发槽(compute_slots, 规格 5.2: 默认 compute 2 / io 2; 无空槽保持 queued)
# ---------------------------------------------------------------------------


def _ensure_slots(db: Session) -> None:
    """惰性初始化槽表(每池 capacity 行、每行容量 1, 幂等)。

    说明: 01 §7.7 ``uq_compute_slots_attempt`` 约束"一槽同时至多绑一个尝试",
    而 current_attempt_id 为单值列 —— 若每池仅一行且 capacity>1, 第二个尝试会
    覆盖第一个的绑定, 释放时无法按 current_attempt_id 定位。故按并发额度拆为
    capacity 行、每行 capacity=1, 池并发度 = 行数, 槽行与尝试一一对应。
    """
    for pool, capacity in (("compute", settings.compute_slots), ("io", IO_SLOT_CAPACITY)):
        existing = db.execute(
            select(sa.func.count(ComputeSlot.id)).where(ComputeSlot.pool_name == pool)
        ).scalar() or 0
        for _ in range(max(capacity - int(existing), 0)):
            db.add(ComputeSlot(pool_name=pool, status="free", capacity=1, in_use=0))
    db.flush()


def acquire_slot(db: Session, pool_name: str) -> ComputeSlot | None:
    """占用一个并发槽(in_use+1, FOR UPDATE 行锁防并发争抢); 无空槽返回 None。"""
    _ensure_slots(db)
    slots = db.execute(
        select(ComputeSlot)
        .where(ComputeSlot.pool_name == pool_name, ComputeSlot.status.in_(("free", "busy")))
        .order_by(ComputeSlot.id)
        .with_for_update()
    ).scalars().all()
    for slot in slots:
        if slot.in_use < slot.capacity:
            slot.in_use += 1
            return slot
    return None  # 无空槽: 任务保持 queued, 等待下一轮调度


def release_slot(db: Session, attempt_id: int) -> None:
    """释放绑定的并发槽(in_use-1, current_attempt_id 清空; 规格 5.2 释放路径)。"""
    slot = db.execute(
        select(ComputeSlot).where(ComputeSlot.current_attempt_id == attempt_id)
    ).scalar_one_or_none()
    if slot is not None:
        slot.in_use = max(slot.in_use - 1, 0)
        slot.current_attempt_id = None


# ---------------------------------------------------------------------------
# 领取与状态推进(Worker 消费端下一波次实现; 本模块提供服务入口)
# ---------------------------------------------------------------------------


def claim_and_run(db: Session, task_id: int, worker_id: str) -> Claim | None:
    """领取任务: 占槽 + 建尝试 + 建租约(发 fencing token) + 任务 running。

    规格 4.1 ①: task_attempts(running) / task_leases(active, UUID token) /
    tasks.status='running' / compute_slots.in_use+1 同事务完成。无空槽或任务
    非 queued 时返回 None(任务保持排队)。并发保护由调度器(U07)以 PG 行锁
    单事务实现, 本入口供调度/测试注入假执行器使用。
    """
    _ensure_slots(db)
    task = _get_task(db, task_id)
    if task.status != "queued":
        return None
    pool = POOL_BY_TYPE.get(task.type, "compute")
    slot = acquire_slot(db, pool)
    if slot is None:
        return None
    attempt_no = task.attempt_count + 1
    now = datetime.now(UTC)
    attempt = TaskAttempt(
        task_id=task.id, attempt_no=attempt_no, worker_id=worker_id,
        status="running", started_at=now,
    )
    db.add(attempt)
    db.flush()
    token = uuid4()
    db.add(
        TaskLease(
            attempt_id=attempt.id, lease_token=token, acquired_by=worker_id,
            acquired_at=now, renewed_at=now,
            expires_at=now + timedelta(seconds=LEASE_TTL_SECONDS), status="active",
        )
    )
    task.status = "running"
    task.attempt_count = attempt_no
    task.updated_at = now
    slot.current_attempt_id = attempt.id
    queue.remove(task.id, pool)  # 领取后出队(视图)
    return Claim(task_id=task.id, attempt_id=attempt.id, attempt_no=attempt_no, lease_token=token)


def _finish_attempt(db: Session, task: Task, status: str, stop_reason: str | None) -> TaskAttempt | None:
    """收尾当前运行尝试: 尝试终态 + 租约释放/吊销 + 槽释放(规格 4.1 ③)。"""
    attempt = db.execute(
        select(TaskAttempt)
        .where(TaskAttempt.task_id == task.id, TaskAttempt.status == "running")
        .order_by(TaskAttempt.attempt_no.desc())
        .limit(1)
    ).scalar_one_or_none()
    if attempt is None:
        return None
    attempt.status = status
    attempt.stop_reason = stop_reason
    attempt.finished_at = datetime.now(UTC)
    lease = db.execute(
        select(TaskLease).where(TaskLease.attempt_id == attempt.id, TaskLease.status == "active")
    ).scalar_one_or_none()
    if lease is not None:
        lease.status = "released" if status == "succeeded" else "revoked"
    release_slot(db, attempt.id)
    return attempt


def _check_transition(task: Task, new_status: str) -> None:
    """状态机校验: 终态不可迁移; 非法跳转抛 TaskStateError(规格 3.1)。"""
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


def complete_task(
    db: Session, task_id: int, *, outcome: str | None = None, solver_status: str | None = None
) -> Task:
    """任务正常完成(规格 3.1: running → completed; 取消竞态下 cancelling → completed)。

    business_outcome 与技术状态正交(规格 3.2): 未显式给定时按求解器状态映射,
    缺省 normal_completion。重复调用幂等(已 completed 直接返回)。
    """
    task = _get_task(db, task_id)
    if task.status == "completed":
        return task
    _check_transition(task, "completed")
    if outcome is None:
        outcome = map_business_outcome(solver_status) if solver_status else "normal_completion"
    attempt = _finish_attempt(db, task, status="succeeded", stop_reason=None)
    task.status = "completed"
    task.business_outcome = outcome
    task.updated_at = datetime.now(UTC)
    queue.clear_cancel(task.id)
    _write_diagnostic(
        db, task.id, level=SEVERITY_INFO, code=TASK_QUEUED, message="任务完成",
        attempt_id=attempt.id if attempt else None,
        context={"business_outcome": outcome},
    )
    return task


def fail_task(
    db: Session,
    task_id: int,
    *,
    code: str | None = None,
    message: str = "",
    stack_trace: str | None = None,
    level: str = SEVERITY_ERROR,
    outcome: str | None = None,
) -> Task:
    """任务失败(规格 3.1/6.3: 确定性失败不可自动重试; 写 error/blocking 诊断)。"""
    task = _get_task(db, task_id)
    if task.status == "failed":
        return task
    _check_transition(task, "failed")
    if outcome is None:
        # 快照/数据校验失败 → insufficient_evidence(规格 3.2 表)
        outcome = (
            "insufficient_evidence"
            if code in (TASK_DATA_SNAPSHOT_MISSING, TASK_DATA_HASH_MISMATCH)
            else None
        )
    attempt = _finish_attempt(db, task, status="failed", stop_reason=code or "error")
    task.status = "failed"
    task.business_outcome = outcome
    task.updated_at = datetime.now(UTC)
    _write_diagnostic(
        db, task.id, level=level, code=code or "TASK-SOLVE-001", message=message,
        attempt_id=attempt.id if attempt else None, stack_trace=stack_trace,
        context={"outcome": outcome},
    )
    return task


def timeout_task(db: Session, task_id: int, *, has_incumbent: bool = False) -> Task:
    """任务硬超时(规格 6.2: 默认 8 h; 有 incumbent → restricted_results)。"""
    task = _get_task(db, task_id)
    if task.status == "timed_out":
        return task
    _check_transition(task, "timed_out")
    attempt = _finish_attempt(db, task, status="stopped", stop_reason="timeout")
    task.status = "timed_out"
    task.business_outcome = "restricted_results" if has_incumbent else "no_recommendation"
    task.updated_at = datetime.now(UTC)
    _write_diagnostic(
        db, task.id, level=SEVERITY_ERROR, code=TASK_TIMEOUT, message="任务超过硬超时",
        attempt_id=attempt.id if attempt else None,
        context={"seconds": settings.task_timeout_hours * 3600, "incumbent_saved": has_incumbent},
    )
    return task


# ---------------------------------------------------------------------------
# 取消(规格 6.1: 权威状态变更 → 传播子任务 → 发信号)
# ---------------------------------------------------------------------------


def cancel_task(db: Session, task_id: int, reason: str = "user_cancel", actor_id: int | None = None) -> Task:
    """取消任务(规格 6.1)。

    - 终态 → 409 CancelDeniedError;
    - queued(未运行) → 直接 cancelled, 出队;
    - running → cancelling(权威) + 广播取消信号; 批量父任务传播到未完成子任务
      (queued 子任务直接取消, running 子任务进 cancelling);
    - cancelling → 幂等返回(取消已发起, 等待 Worker 收拢后 acknowledge_cancel)。
    """
    task = _get_task(db, task_id)
    if task.status in TERMINAL_STATUSES:
        raise CancelDeniedError(
            "终态任务不可取消",
            params={"task_id": task_id, "status": task.status},
            location={"object_type": "task", "object_id": task_id},
        )
    now = datetime.now(UTC)
    if task.status == "queued":
        task.status = "cancelled"
        task.updated_at = now
        queue.remove(task.id, POOL_BY_TYPE[task.type])
        return task
    if task.status == "cancelling":
        return task  # 取消已发起, 幂等

    task.status = "cancelling"
    task.updated_at = now
    queue.set_cancel(task.id, reason)
    # 批量传播: uncertainty 父任务 → 未完成子任务(规格 5.4/6.1)
    child_ids = db.execute(
        select(Task.id)
        .join(SampleTask, SampleTask.id == Task.id)
        .where(
            SampleTask.parent_task_id == task_id,
            Task.status.not_in(TERMINAL_STATUSES),
        )
    ).scalars().all()
    for child_id in child_ids:
        child = _get_task(db, child_id)
        if child.status == "queued":
            child.status = "cancelled"  # 未运行的子任务直接取消
            queue.remove(child.id, POOL_BY_TYPE[child.type])
        else:
            child.status = "cancelling"  # 运行中子任务由 Worker 收拢
            queue.set_cancel(child.id, reason)
        child.updated_at = now
    return task


def acknowledge_cancel(db: Session, task_id: int) -> Task:
    """确认取消(规格 6.1 收拢: Worker 终止子进程后调用; cancelling → cancelled)。

    已产出的样本/证据保留不回滚; 批量父任务在部分子任务已完成时记
    business_outcome=partial_batch(规格 3.2/6.1)。
    """
    task = _get_task(db, task_id)
    if task.status == "cancelled":
        return task
    if task.status != "cancelling":
        raise TaskStateError(
            "任务不在取消中", params={"task_id": task_id, "status": task.status},
            location={"object_type": "task", "object_id": task_id},
        )
    attempt = _finish_attempt(db, task, status="stopped", stop_reason="cancelled")
    outcome: str | None = None
    if task.type == "uncertainty":
        completed_children = db.execute(
            sa.select(sa.func.count(Task.id))
            .join(SampleTask, SampleTask.id == Task.id)
            .where(SampleTask.parent_task_id == task.id, Task.status == "completed")
        ).scalar() or 0
        if completed_children > 0:
            outcome = "partial_batch"
    task.status = "cancelled"
    task.business_outcome = outcome
    task.updated_at = datetime.now(UTC)
    queue.clear_cancel(task.id)
    _write_diagnostic(
        db, task.id, level=SEVERITY_INFO, code=TASK_QUEUED, message="任务已取消",
        attempt_id=attempt.id if attempt else None,
        context={"business_outcome": outcome},
    )
    return task


# ---------------------------------------------------------------------------
# 手动重试(规格 6.4: 复用同一 calc_snapshot_id, 输入含义不变)
# ---------------------------------------------------------------------------


def retry_task(db: Session, user: User, task_id: int) -> Task:
    """手动重试(仅终态任务; 计算类须存在快照, 缺失 → TASK-DATA-001 blocking)。

    重试不改变 idempotency_key 语义、不改变快照; 新尝试在下次领取时创建
    (attempt_no 递增, 新租约新 token)。
    """
    task = _get_task(db, task_id)
    project_service.ensure_access(db, user, task.project_id, "edit")
    if task.status not in TERMINAL_STATUSES:
        raise TaskStateError(
            "仅终态任务可手动重试", params={"task_id": task_id, "status": task.status},
            location={"object_type": "task", "object_id": task_id},
        )
    if task.type in COMPUTE_TYPES and task.calc_snapshot_id is None:
        raise AppError(
            "计算快照缺失, 任务不可复现, 无法重试",
            code=TASK_DATA_SNAPSHOT_MISSING,
            severity=SEVERITY_BLOCKING,
            message_key="ies.diag.task.snapshot_missing",
            params={"task_id": task_id},
            location={"object_type": "task", "object_id": task_id},
        )
    pool = POOL_BY_TYPE[task.type]
    trace_id = new_id("trc-")
    task.status = "queued"
    task.business_outcome = None
    task.updated_at = datetime.now(UTC)
    db.flush()
    _write_diagnostic(
        db, task.id, level=SEVERITY_INFO, code=TASK_QUEUED, message="手动重试已排队",
        context={"trace_id": trace_id, "queue": pool, "snapshot_id": task.calc_snapshot_id,
                 "retry": True},
    )
    queue.enqueue(
        task.id, pool, task_type=task.type, snapshot_id=task.calc_snapshot_id,
        priority=task.priority, trace_id=trace_id,
    )
    return task


# ---------------------------------------------------------------------------
# 进度(规格 7: PG 持久进度 + Redis 秒级进度; 每尝试至多一行)
# ---------------------------------------------------------------------------


def record_progress(
    db: Session, task_id: int, stage: str, percent: float, detail: dict[str, Any] | None = None,
    attempt_id: int | None = None,
) -> TaskAttempt | None:
    """记录任务进度(PG UPSERT + Redis 秒级进度, 规格 7.1)。

    attempt_id 缺省取该任务当前尝试(attempt_no 最大者); percent 收敛到 0-100。
    """
    task = _get_task(db, task_id)
    stmt = select(TaskAttempt).where(TaskAttempt.task_id == task.id).order_by(TaskAttempt.attempt_no.desc())
    if attempt_id is not None:
        stmt = stmt.where(TaskAttempt.id == attempt_id)
    attempt = db.execute(stmt.limit(1)).scalars().first()
    if attempt is None:
        return None
    percent = round(min(max(float(percent), 0.0), 100.0), 2)
    existing = db.execute(
        select(TaskProgress).where(TaskProgress.attempt_id == attempt.id)
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if existing is None:
        db.add(TaskProgress(
            attempt_id=attempt.id, progress_percent=percent, stage=stage,
            detail=detail, updated_at=now,
        ))
    else:
        existing.progress_percent = percent
        existing.stage = stage
        existing.detail = detail
        existing.updated_at = now
    db.flush()
    queue.set_progress(task.id, attempt.attempt_no, percent, stage, detail)
    return attempt


# ---------------------------------------------------------------------------
# 查询: 列表 / 详情 / 序列化
# ---------------------------------------------------------------------------


def _task_trace_id(db: Session, task: Task) -> str | None:
    """任务 trace_id(取自入队诊断 context; 规格 10.3 关联标识)。"""
    diag = db.execute(
        select(TaskDiagnostic)
        .where(TaskDiagnostic.task_id == task.id, TaskDiagnostic.code == TASK_QUEUED)
        .order_by(TaskDiagnostic.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if diag is not None and diag.context:
        return diag.context.get("trace_id")
    return None


def _progress_summary(db: Session, task: Task) -> tuple[int | None, float, str | None, dict[str, Any] | None]:
    """当前进度摘要 (attempt_no, percent, stage, detail): running 优先读 Redis 秒级进度。"""
    attempt = db.execute(
        select(TaskAttempt).where(TaskAttempt.task_id == task.id).order_by(TaskAttempt.attempt_no.desc())
    ).scalars().first()
    if attempt is None:
        return None, 0.0, "queued" if task.status == "queued" else None, None
    percent: float = 0.0
    stage: str | None = None
    detail: dict[str, Any] | None = None
    if task.status == "running":
        live = queue.get_progress(task.id, attempt.attempt_no)
        if live is not None:
            try:
                percent = float(live.get("percent", 0.0))
            except (TypeError, ValueError):
                percent = 0.0
            stage = live.get("stage")
            detail = live.get("detail")
    if stage is None:
        row = db.execute(
            select(TaskProgress).where(TaskProgress.attempt_id == attempt.id)
        ).scalar_one_or_none()
        if row is not None:
            percent = float(row.progress_percent)
            stage = row.stage
            detail = row.detail
    if task.status in TERMINAL_STATUSES and stage is None:
        # 终态无进度行: 完成定格 100, 其余 0(规格 7.3)
        percent = 100.0 if task.status == "completed" else percent
        stage = "done" if task.status == "completed" else None
    return attempt.attempt_no, percent, stage, detail


def task_summary(db: Session, task: Task) -> dict[str, Any]:
    """任务列表项摘要(规格 9.1 字段)。"""
    attempt_no, percent, stage, _detail = _progress_summary(db, task)
    queue_position: int | None = None
    if task.status == "queued":
        queue_position = queue.queue_position(task.id, POOL_BY_TYPE[task.type])
    summary: dict[str, Any] = {
        "id": task.id,
        "type": task.type,
        "status": task.status,
        "business_outcome": task.business_outcome,
        "priority": task.priority,
        "calc_snapshot_id": task.calc_snapshot_id,
        "requested_by": task.requested_by,
        "requested_at": task.requested_at,
        "attempt_count": task.attempt_count,
        "max_attempts": task.max_attempts,
        "idempotency_key": task.idempotency_key,
        "superseded_by_task_id": task.superseded_by_task_id,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "summary": {"attempt_no": attempt_no, "percent": percent, "stage": stage,
                    "queue_position": queue_position},
    }
    trace_id = _task_trace_id(db, task)
    if trace_id is not None:
        summary["trace_id"] = trace_id
    return summary


def list_tasks(
    db: Session,
    user: User,
    project_id: int,
    *,
    task_type: str | None = None,
    status: str | None = None,
    outcome: str | None = None,
    cursor: int | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """任务列表(规格 9.1: 状态/结局过滤 + 游标分页, requested_at 倒序)。"""
    project_service.ensure_access(db, user, project_id, "view")
    _get_project(db, project_id)
    stmt = select(Task).where(Task.project_id == project_id)
    if task_type is not None:
        stmt = stmt.where(Task.type == task_type)
    if status is not None:
        stmt = stmt.where(Task.status == status)
    if outcome is not None:
        stmt = stmt.where(Task.business_outcome == outcome)
    if cursor is not None:
        stmt = stmt.where(Task.id < cursor)
    rows = db.execute(stmt.order_by(Task.id.desc()).limit(limit + 1)).scalars().all()
    items = [task_summary(db, task) for task in rows[:limit]]
    next_cursor = rows[-1].id if len(rows) > limit else None
    return {"items": items, "next_cursor": next_cursor}


#: 任务诊断 context 白名单(M-03): 仅这些内部字段可向普通项目成员展示,
#: 其余(路径/对象 id/求解器参数等)仅管理员可见或置空
_DIAG_CONTEXT_ALLOWLIST: frozenset[str] = frozenset(
    {"trace_id", "queue", "snapshot_id", "business_outcome", "outcome"}
)


def _sanitize_diag_context(context: dict[str, Any] | None) -> dict[str, Any] | None:
    """诊断 context 脱敏(M-03): 白名单字段保留, 其余剔除。"""
    if not isinstance(context, dict):
        return None
    return {k: v for k, v in context.items() if k in _DIAG_CONTEXT_ALLOWLIST} or None


def task_detail(db: Session, user: User, project_id: int, task_id: int) -> dict[str, Any]:
    """任务详情(规格 9.2: 尝试/租约/进度/诊断/快照/批量关系; 不暴露 lease_token)。"""
    project_service.ensure_access(db, user, project_id, "view")
    task = _get_task(db, task_id)
    if task.project_id != project_id:
        raise NotFoundError(
            "任务不存在", params={"task_id": task_id, "project_id": project_id},
            location={"object_type": "task", "object_id": task_id},
        )
    detail = task_summary(db, task)
    if task.calc_snapshot_id is not None:
        snapshot = db.get(CalcSnapshot, task.calc_snapshot_id)
        detail["calc_snapshot"] = (
            {"id": snapshot.id, "content_hash": snapshot.content_hash,
             "random_seed": snapshot.random_seed}
            if snapshot is not None else None
        )
    else:
        detail["calc_snapshot"] = None

    attempts = db.execute(
        select(TaskAttempt).where(TaskAttempt.task_id == task.id).order_by(TaskAttempt.attempt_no)
    ).scalars().all()
    detail["attempts"] = [
        {"id": a.id, "attempt_no": a.attempt_no, "status": a.status, "worker_id": a.worker_id,
         "stop_reason": a.stop_reason, "started_at": a.started_at, "finished_at": a.finished_at}
        for a in attempts
    ]
    attempt_ids = [a.id for a in attempts] or [0]
    lease = db.execute(
        select(TaskLease)
        .where(TaskLease.attempt_id.in_(attempt_ids), TaskLease.status == "active")
        .order_by(TaskLease.id.desc())
    ).scalars().first()
    detail["current_lease"] = None
    if lease is not None:
        # 只暴露 acquired_by/renewed_at/expires_at, 不暴露 lease_token(规格 9.2)
        attempt = db.get(TaskAttempt, lease.attempt_id)
        detail["current_lease"] = {
            "attempt_no": attempt.attempt_no if attempt else None,
            "acquired_by": lease.acquired_by,
            "renewed_at": lease.renewed_at,
            "expires_at": lease.expires_at,
        }

    attempt_no, percent, stage, detail_json = _progress_summary(db, task)
    detail["progress"] = {
        "attempt_no": attempt_no, "percent": percent, "stage": stage,
        "detail": detail_json, "updated_at": None, "source": "pg",
    }
    progress_row = db.execute(
        select(TaskProgress)
        .join(TaskAttempt, TaskAttempt.id == TaskProgress.attempt_id)
        .where(TaskAttempt.task_id == task.id)
        .order_by(TaskAttempt.attempt_no.desc())
    ).scalars().first()
    if progress_row is not None:
        detail["progress"]["updated_at"] = progress_row.updated_at

    diagnostics = db.execute(
        select(TaskDiagnostic).where(TaskDiagnostic.task_id == task.id).order_by(TaskDiagnostic.id)
    ).scalars().all()
    # M-03: stack_trace 与完整 context 仅对全局管理员返回(受控审计视角);
    # viewer/owner 一律返回 stack_trace=null, context 仅保留白名单字段
    is_admin = identity_service.has_role(db, user, "admin")
    detail["diagnostics"] = [
        {
            "id": d.id, "level": d.level, "code": d.code, "message": d.message,
            "stack_trace": d.stack_trace if is_admin else None,
            "context": d.context if is_admin else _sanitize_diag_context(d.context),
            "attempt_id": d.attempt_id, "created_at": d.created_at,
        }
        for d in diagnostics
    ]

    child_ids = db.execute(
        select(Task.id).join(SampleTask, SampleTask.id == Task.id)
        .where(SampleTask.parent_task_id == task.id)
    ).scalars().all()
    children = []
    if child_ids:
        child_rows = db.execute(select(Task).where(Task.id.in_(child_ids))).scalars().all()
        children = [{"id": c.id, "status": c.status} for c in child_rows]
    parent_id = db.execute(
        select(SampleTask.parent_task_id).where(SampleTask.id == task.id).limit(1)
    ).scalar_one_or_none()
    detail["batch"] = {"parent_task_id": parent_id, "child_task_count": len(children), "children": children}
    return detail


# ---------------------------------------------------------------------------
# 业务结局映射(规格 3.2 表)
# ---------------------------------------------------------------------------


def map_business_outcome(solver_status: str) -> str:
    """求解器状态 → 业务结局(规格 3.2; 未知状态保守视为 normal_completion)。"""
    return _SOLVER_OUTCOME.get(solver_status, "normal_completion")
