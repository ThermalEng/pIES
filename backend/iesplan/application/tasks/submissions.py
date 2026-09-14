"""任务用例族(application/tasks): 提交/取消/重试/租约。

任务提交与状态机编排的唯一实现(已收敛原 ``services.tasks`` +
``services.queue`` 语义，旧服务已删除):

- 提交: 权限 → 项目状态 → 类型/幂等键/父任务校验 → 幂等命中 → 存储门禁 →
  快照装配(含草稿固化) → 重复提交去重 → 任务行 → 诊断 → 入队；
- 取消: 终态拒绝 → queued 直接取消 → running 转 cancelling + 取消信号 +
  批量子任务传播；确认取消收拢尝试/租约/槽；
- 重试: 仅终态可重试(计算类须有快照) → 回 queued + 入队；
- 租约: 占槽 + 建尝试 + 建租约(fencing token) + running + 出队。

事务: 顶层用例拥有提交/回滚(``db.commit`` 收尾, 失败 ``db.rollback``);
内部步骤只经领域公开门面写入 + flush, 不提交。队列(可重建视图)的入队/
出队/信号与旧服务同一位置触发, 行为一致。

数据访问只经领域公开门面(tasks/project/dataset/audit/storage/identity/
configuration, 含 tasks 域队列可重建视图); 不导入 ``models.*``。
幂等键格式改接任务域常量唯一权威(``iesplan.tasks.contracts.IDEMPOTENCY_KEY_RE``)。
任务类型/状态机/业务结局映射与任务错误唯一权威归 tasks 域
(``iesplan.tasks`` 门面), 本模块只做提交/幂等/快照/事务编排, 直接复用。
快照固化时的版本内容规则(含财务/规划引用闭合)经
``application.projects.versions.freeze_snapshot_version`` 项目用例拥有
(语义与旧 services.project 一致), 本模块只编排提交/幂等/快照/事务。
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from iesplan import __version__
from iesplan import dataset as dataset_domain
from iesplan import identity as identity_domain
from iesplan import project as project_domain
from iesplan import results as results_domain
from iesplan import tasks as tasks_domain
from iesplan.application.projects import (
    current_version_matches_draft as _current_version_matches_draft,
    ensure_access,
    freeze_snapshot_version as _freeze_snapshot_version,
    load_content_object,
)
from iesplan.assembly import (
    AssemblyValidationError,
    ValidatedAssemblyArtifact,
    validate_project_export,
)
from iesplan.config import settings
from iesplan.core.diagnostics import (
    SEVERITY_BLOCKING,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SYS_STORE_CORRUPT,
    TASK_DATA_SNAPSHOT_MISSING,
    TASK_QUEUED,
)
from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.core.idgen import new_id
from iesplan.core.jsonutil import jsonable
from iesplan.identity.contracts import UserRecord
from iesplan.project.contracts import ProjectRecord, ProjectVersionRecord
from iesplan.storage import (
    object_info,
    orphaned_stats,
    usage_summary,
)
from iesplan.tasks.contracts import (
    COMPUTE_TYPES,
    IDEMPOTENCY_KEY_RE as _IDEMPOTENCY_KEY_RE,
    IO_SLOT_CAPACITY,
    LEASE_TTL_SECONDS,
    POOL_BY_TYPE,
    TASK_TYPES,
    TERMINAL_STATUSES,
    CalcSnapshotRecord,
    CancelDeniedError,
    ComputeSlotRecord,
    InvalidRequestError,
    StorageQuotaError,
    TaskAttemptRecord,
    TaskRecord,
    TaskStateError,
)

#: 任务类型/池/状态机/结局映射与任务错误唯一权威见 tasks 域
#: (``iesplan.tasks`` 门面, 本模块顶层导入复用); 此处不复制。
#: 逐时结果每行估算字节(~1 KB)
_HOURLY_BYTES_PER_ROW = 1024
#: 中间文件系数(默认 0.5)
_INTERMEDIATE_FACTOR = 0.5
#: 证据包系数(默认 0.1)
_EVIDENCE_FACTOR = 0.1
#: 幂等键格式唯一权威见 tasks.contracts(顶层导入 _IDEMPOTENCY_KEY_RE)。

#: 项目所有者能力集唯一权威: iesplan.project.OWNER_CAPABILITIES(经 project_domain 取用, 此处不复制)。


@dataclass(frozen=True, slots=True)
class Claim:
    """一次领取结果(尝试 + 租约 + fencing token)。"""

    task_id: int
    attempt_id: int
    attempt_no: int
    lease_token: UUID


@dataclass(frozen=True, slots=True)
class StorageEstimate:
    """存储门禁估算结果。"""

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
# 项目/权限/内容读取胶水(复制 services.project 对应语义, 经领域公开门面)
# ---------------------------------------------------------------------------


def require_project(db: Session, project_id: int) -> ProjectRecord:
    """按 id 取项目; 不存在一律 404。"""
    project = project_domain.get_project(db, project_id)
    if project is None:
        raise NotFoundError(
            "项目不存在",
            params={"project_id": project_id},
            location={"object_type": "project", "object_id": project_id},
        )
    return project


def _get_current_draft(db: Session, project: ProjectRecord):
    """取项目当前草稿; 缺失视为数据损坏。"""
    draft = project_domain.get_current_draft(db, project.id)
    if draft is None:
        raise AppError(
            "项目缺少当前草稿(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
            location={"object_type": "project", "object_id": project.id},
        )
    return draft






# ---------------------------------------------------------------------------
# 项目/草稿/版本解析(复制 services.tasks._resolve_project_inputs 语义)
# ---------------------------------------------------------------------------


def _resolve_project_inputs(
    db: Session, project: ProjectRecord, actor: UserRecord, *, freeze: bool
) -> tuple[ProjectVersionRecord | None, dict]:
    """解析任务输入: 项目版本(或当前草稿内容, 需要时固化)。

    返回 (version, content): version 为 None 表示未固化(仅读草稿内容)。
    已有版本但草稿领域内容已变更时版本不再新鲜, 必须按当前草稿重新固化,
    否则后续任务会静默运行旧版本输入。
    """
    version = None
    if project.current_version_id is not None:
        version = project_domain.get_version(db, project.id, project.current_version_id)
        if version is None:
            raise AppError(
                "项目版本指针缺失(数据损坏)",
                code=SYS_STORE_CORRUPT,
                severity=SEVERITY_ERROR,
                message_key="ies.diag.store.corrupt",
                location={"object_type": "project", "object_id": project.id},
            )
        if not _current_version_matches_draft(db, project):
            version = None  # 草稿已变更: 需重新固化
    if version is not None:
        return version, load_content_object(db, version.content_object_id)
    draft = _get_current_draft(db, project)
    content = load_content_object(db, draft.content_object_id)
    if not freeze:
        return None, content
    # 草稿固化: 创建不可变项目版本(计算输入固定)
    version = _freeze_snapshot_version(db, actor, project, draft)
    return version, content






def _bound_dataset_ids(content: dict) -> list[int]:
    """从项目内容取出绑定的数据集版本 id 清单。"""
    return [
        int(binding["dataset_version_id"])
        for binding in content.get("dataset_bindings", [])
        if binding.get("dataset_version_id") is not None
    ]


def _derive_random_seed(version_id: int) -> int:
    """随机种子强制非 NULL: 配置缺省时取项目版本 id(48 bit 非负种子)。

    版本 id 为明确标识(非内容摘要): 同一版本重提 → 同一种子(可复现);
    不同版本 → 不同种子。不做摘要派生。
    """
    return int(version_id) % (1 << 48)


# ---------------------------------------------------------------------------
# 快照装配(复制 services.tasks.assemble_snapshot 语义)
# ---------------------------------------------------------------------------


def _snapshot_inputs_equal(
    snapshot: CalcSnapshotRecord,
    *,
    dataset_ids: list[int],
    calc_config: dict,
    program_version: str,
    extensions: dict,
    random_seed: int,
    tolerances: dict,
    canonical_text: str | None,
    receipt: dict,
) -> bool:
    """快照输入一致判定: 全部快照内容字段逐项相等(不使用内容摘要)。"""
    return (
        list(snapshot.dataset_version_ids or []) == list(dataset_ids)
        and jsonable(snapshot.calc_config_snapshot or {}) == jsonable(calc_config)
        and (snapshot.program_version or "") == program_version
        and jsonable(snapshot.extension_versions or {}) == jsonable(extensions)
        and snapshot.random_seed == random_seed
        and jsonable(snapshot.tolerances or {}) == jsonable(tolerances)
        and (snapshot.canonical_assembly_text or "") == (canonical_text or "")
        and jsonable(snapshot.assembly_receipt or {}) == jsonable(receipt)
    )


def _assemble_snapshot(
    db: Session,
    project_id: int,
    task_type: str,
    config: dict[str, Any] | None = None,
    user: UserRecord | None = None,
) -> CalcSnapshotRecord:
    """组装不可变计算快照(相同输入复用)。

    绑定: 项目版本(无版本时固化当前草稿)、数据集版本 id 清单、计算配置全文、
    程序版本、受控扩展版本、随机种子(强制非 NULL)、容差与规范装配文本/回执;
    相同输入复用既有快照(快照内容字段逐项相等判定)。任务级 config 并入快照
    的 calc_config_snapshot.task_params。
    """
    project = require_project(db, project_id)
    actor = user or identity_domain.get_user(db, project.owner_id)
    if actor is None:
        raise InvalidRequestError("无法确定快照创建者", params={"project_id": project_id})
    version, content = _resolve_project_inputs(db, project, actor, freeze=True)
    assert version is not None
    calc_config: dict[str, Any] = dict(content.get("calc_config") or {})
    if config:
        calc_config["task_params"] = jsonable(config)
    random_seed = calc_config.get("random_seed")
    if random_seed is None:
        random_seed = _derive_random_seed(version.id)
    dataset_ids = _bound_dataset_ids(content)
    tolerances = calc_config.get("tolerances") or {}
    extensions = content.get("extensions") or {}

    # 统一生产闸门: 先签发规范文本/回执二件套, 失败不创建快照或任务。
    # 回执进入快照身份; 快照去重使用内容字段逐项相等判定, 文本仅校验字头。
    artifact = _assembly_gate(db, project_id, content, task_type)
    receipt = artifact.receipt.to_dict()
    for candidate in tasks_domain.list_snapshots_for_version(db, version.id):
        if _snapshot_inputs_equal(
            candidate,
            dataset_ids=dataset_ids,
            calc_config=calc_config,
            program_version=__version__,
            extensions=extensions,
            random_seed=random_seed,
            tolerances=tolerances,
            canonical_text=artifact.canonical_text,
            receipt=receipt,
        ):
            return candidate  # 相同输入复用既有快照(不可变, 复用安全)

    return tasks_domain.create_snapshot(
        db,
        project_version_id=version.id,
        dataset_version_ids=dataset_ids,
        calc_config_snapshot=calc_config,
        random_seed=random_seed,
        created_by=actor.id,
        program_version=__version__,
        extension_versions=extensions,
        tolerances=tolerances,
        canonical_assembly_text=artifact.canonical_text,
        assembly_receipt=receipt,
    )


def _assembly_gate(db: Session, project_id: int, content: dict, task_type: str) -> ValidatedAssemblyArtifact:
    """计算任务统一装配闸门: 完整四阶段校验后签发规范二件套。

    阻断诊断抛 AssemblyValidationError(HTTP 422)。签发即受信，不做重复复核。
    """
    if task_type not in COMPUTE_TYPES:
        raise InvalidRequestError("仅计算类任务可装配计算快照", params={"task_type": task_type})
    export_content = dict(content)
    export_content.setdefault("graph_id", project_id)
    export_content.setdefault("name", f"project_{project_id}")
    result = validate_project_export(export_content, datasets=_dataset_meta_for(db, content))
    if result.artifact is None:
        raise AssemblyValidationError(result.diagnostics)
    return result.artifact


def _dataset_meta_for(db: Session, content: dict) -> dict[int, dict]:
    """项目绑定数据集版本 → 装配公开元信息。

    只通过 dataset 域与 storage 公开门面读取版本/对象元信息。每个版本选择
    ``file_kind=data`` 的权威数据本体; 缺文件/对象时不伪造元信息, 由统一
    装配入口给出阻断诊断。
    """
    meta: dict[int, dict] = {}
    for binding in content.get("dataset_bindings", []) or []:
        vid = binding.get("dataset_version_id")
        if vid is None:
            continue
        try:
            vid = int(vid)
        except (TypeError, ValueError):
            continue
        version = dataset_domain.get_version(db, vid)
        if version is None:
            continue
        fields = version.fields if isinstance(version.fields, dict) else {}
        units = version.units if isinstance(version.units, dict) else {}
        columns: dict[str, str] = {}
        for col, field in fields.items():
            if not isinstance(field, dict):
                continue
            unit = field.get("unit") or units.get(col) or ""
            if isinstance(unit, str) and unit:
                columns[col] = unit

        data_file = next(
            (f for f in dataset_domain.list_files(db, vid) if f.file_kind == "data"),
            None,
        )
        object_meta: dict = {}
        if data_file is not None:
            object_meta = object_info(db, data_file.object_id)
        media_type = object_meta.get("media_type")
        if not media_type and data_file is not None:
            media_type = {
                "csv": "text/csv; charset=utf-8",
                "parquet": "application/vnd.apache.parquet",
                "json": "application/json",
            }.get(data_file.format)
        meta[vid] = {
            "id": vid,
            "name": f"ds{vid}",
            "columns": list(columns.keys()),
            "column_units": columns,
            "resolution": version.resolution or "",
            "media_type": media_type or "",
        }
    return meta


# ---------------------------------------------------------------------------
# 存储门禁(复制 services.tasks.estimate_storage 语义)
# ---------------------------------------------------------------------------


def _resolve_storage_estimate_inputs(
    db: Session, project: ProjectRecord, actor: UserRecord
) -> tuple[dict, list[int]]:
    """只读解析估算输入(版本或草稿内容, 不固化不写库)。"""
    _version, content = _resolve_project_inputs(db, project, actor, freeze=False)
    return content, _bound_dataset_ids(content)


def list_cleanup_suggestions(db: Session) -> list[dict[str, Any]]:
    """清理建议(按"最安全 → 最激进"排序, 每项含对象清单与预计释放量)。"""
    suggestions: list[dict[str, Any]] = []

    orphaned = orphaned_stats(db)
    suggestions.append(
        {
            "action": "cleanup_orphaned_objects",
            "message_key": "ies.fix.store.orphaned",
            "count": orphaned["count"],
            "estimate_bytes": orphaned["total_bytes"],
        }
    )

    report_count = results_domain.count_reports(db)
    suggestions.append(
        {
            "action": "archive_old_reports",
            "message_key": "ies.fix.store.reports",
            "count": int(report_count),
            "estimate_bytes": 0,
        }
    )

    terminal_tasks = tasks_domain.count_tasks_by_statuses(db, TERMINAL_STATUSES)
    suggestions.append(
        {
            "action": "cleanup_terminal_task_files",
            "message_key": "ies.fix.store.task_files",
            "count": int(terminal_tasks),
            "estimate_bytes": 0,
        }
    )

    suggestions.append(
        {
            "action": "archive_project_versions",
            "message_key": "ies.fix.store.versions",
            "count": 0,
            "estimate_bytes": 0,
        }
    )
    suggestions.append(
        {
            "action": "reduce_samples_or_horizon",
            "message_key": "ies.fix.store.business_throttle",
            "count": 0,
            "estimate_bytes": 0,
        }
    )
    return suggestions


def estimate_storage(
    db: Session, project_id: int, task_type: str, config: dict[str, Any] | None = None
) -> StorageEstimate:
    """提交前存储需求估算与安全阈值检查。

    S_need = S_snap + S_inter + S_hourly(+S_samples) + S_evid;
    S_avail = min(配额余额, 卷空闲空间); 配额未配置时仅以卷空闲空间为准。
    只读, 不拥有事务。
    """
    project = require_project(db, project_id)
    actor = identity_domain.get_user(db, project.owner_id)
    if actor is None:
        raise NotFoundError("项目所有者不存在", params={"project_id": project_id})
    content, dataset_ids = _resolve_storage_estimate_inputs(db, project, actor)
    params = config or {}

    # 快照与输入: 数据集版本对象大小之和
    snap_bytes = 0
    for dvid in dataset_ids:
        snap_bytes += sum(f.size_bytes or 0 for f in dataset_domain.list_files(db, dvid))

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
    usage = usage_summary(db)
    volume_free = shutil.disk_usage(settings.data_dir).free
    if int(usage["quota_bytes"] or 0) > 0:
        avail = max(int(usage["quota_bytes"]) - int(usage["used_bytes"]), 0)
    else:
        avail = volume_free
    avail = min(avail, volume_free)

    blocked = need > avail - settings.storage_min_free_bytes
    suggestions = list_cleanup_suggestions(db) if blocked else []
    return StorageEstimate(need=need, avail=avail, blocked=blocked, suggestions=suggestions)


# ---------------------------------------------------------------------------
# 任务提交(复制 services.tasks.create_task 语义; 顶层拥有事务)
# ---------------------------------------------------------------------------


def _get_task(db: Session, task_id: int) -> TaskRecord:
    """按 id 取任务; 不存在 404。"""
    task = tasks_domain.get_task(db, task_id)
    if task is None:
        raise NotFoundError(
            "任务不存在",
            params={"task_id": task_id},
            location={"object_type": "task", "object_id": task_id},
        )
    return task


def ensure_task_belongs(db: Session, project_id: int, task_id: int) -> TaskRecord:
    """任务必须属于该项目(否则 404, 不泄露其他项目任务存在性)。只读, 不拥有事务。"""
    task = _get_task(db, task_id)
    if task.project_id != project_id:
        raise NotFoundError(
            "任务不存在",
            params={"task_id": task_id, "project_id": project_id},
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
) -> None:
    """写入任务诊断(不可变, 只 INSERT; 经 tasks 域)。"""
    tasks_domain.append_diagnostic(
        db,
        task_id=task_id,
        level=level,
        message=message,
        attempt_id=attempt_id,
        code=code,
        stack_trace=stack_trace,
        context=context,
    )


def _submit_task(
    db: Session,
    user: UserRecord,
    project_id: int,
    task_type: str,
    config: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    parent_task_id: int | None = None,
) -> tuple[TaskRecord, dict[str, bool]]:
    """创建任务(幂等检查 → 门禁 → 快照 → INSERT → 入队)。

    返回 (任务记录, 标记): 标记含 replay/duplicate。内部步骤只 flush,
    由顶层 submit_task 提交。
    """
    ensure_access(db, user, project_id, "edit")
    project = require_project(db, project_id)
    if project.status != "active":
        raise ConflictError("项目已归档或已删除, 不能提交任务", params={"project_id": project_id})
    if task_type not in TASK_TYPES:
        raise InvalidRequestError("未知任务类型", code="TASK-REQ-002", params={"task_type": task_type})
    if task_type == "analysis":
        # 批量分析必须有扫描规格(task_params.sweeps)
        sweeps = (config or {}).get("sweeps")
        if not isinstance(sweeps, list) or not sweeps:
            raise InvalidRequestError(
                "analysis 任务缺少扫描规格(sweeps)",
                params={"task_type": task_type, "expected": "task_config.sweeps 非空数组"},
            )
    if idempotency_key is not None and not re.fullmatch(_IDEMPOTENCY_KEY_RE, idempotency_key):
        raise InvalidRequestError(
            "幂等键格式非法(须匹配 ^[A-Za-z0-9._:-]{1,128}$)",
            code="TASK-REQ-003",
            params={"idempotency_key": idempotency_key},
        )
    if parent_task_id is not None:
        parent = _get_task(db, parent_task_id)
        if parent.project_id != project_id:
            raise InvalidRequestError(
                "父任务不属于该项目",
                code="TASK-REQ-004",
                params={"parent_task_id": parent_task_id, "project_id": project_id},
            )

    # 1) 幂等命中(限定项目范围)
    if idempotency_key is not None:
        existing = tasks_domain.get_task_by_idempotency(db, project_id, idempotency_key)
        if existing is not None:
            return existing, {"replay": True, "duplicate": False}

    snapshot: CalcSnapshotRecord | None = None
    if task_type in COMPUTE_TYPES:
        # 2) 存储门禁(门禁失败不创建快照不创建任务)
        estimate = estimate_storage(db, project_id, task_type, config)
        if estimate.blocked:
            raise StorageQuotaError(
                "存储空间不足, 任务提交被拒绝",
                params={
                    "need_bytes": estimate.need,
                    "avail_bytes": estimate.avail,
                    "min_pad_bytes": settings.storage_min_free_bytes,
                    "suggestions": estimate.suggestions,
                },
                location={"object_type": "project", "object_id": project_id},
            )
        # 3) 快照装配(去重复用)
        snapshot = _assemble_snapshot(db, project_id, task_type, config=config, user=user)
        # 4) 重复提交去重(仅无幂等键时生效): 同 (project, type, snapshot) 非终态
        #    任务复用; 携带幂等键的提交以幂等键为去重机制, 不参与快照去重
        if idempotency_key is None:
            duplicate = tasks_domain.find_active_duplicate(db, project_id, task_type, snapshot.id)
            if duplicate is not None:
                return duplicate, {"replay": False, "duplicate": True}
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
    task = tasks_domain.create_task(
        db,
        project_id=project_id,
        type=task_type,
        requested_by=user.id,
        idempotency_key=idempotency_key,
        calc_snapshot_id=snapshot.id if snapshot is not None else None,
        priority=priority,
        deadline=deadline,
    )
    _write_diagnostic(
        db,
        task.id,
        level=SEVERITY_INFO,
        code=TASK_QUEUED,
        message="任务已排队",
        context={
            "trace_id": trace_id,
            "queue": pool,
            "snapshot_id": task.calc_snapshot_id,
            "parent_task_id": parent_task_id,
        },
    )
    # 5) 入队(可重建视图; 权威事实 = tasks.status='queued')
    tasks_domain.enqueue(
        task.id,
        pool,
        task_type=task_type,
        snapshot_id=task.calc_snapshot_id,
        priority=priority,
        trace_id=trace_id,
    )
    return task, {"replay": False, "duplicate": False}


def submit_task(
    db: Session,
    user: UserRecord,
    project_id: int,
    task_type: str,
    config: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    parent_task_id: int | None = None,
) -> tuple[TaskRecord, dict[str, bool]]:
    """提交任务用例; 本层拥有事务提交/回滚。"""
    try:
        result = _submit_task(
            db, user, project_id, task_type,
            config=config, idempotency_key=idempotency_key, parent_task_id=parent_task_id,
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------------------
# 并发槽与领取/租约(复制 services.tasks 语义; 顶层拥有事务)
# ---------------------------------------------------------------------------


def _ensure_slots(db: Session) -> None:
    """惰性初始化槽表(每池 capacity 行、每行容量 1, 幂等)。"""
    tasks_domain.ensure_slots(db, {"compute": settings.compute_slots, "io": IO_SLOT_CAPACITY})


def acquire_slot(db: Session, pool_name: str) -> ComputeSlotRecord | None:
    """占用一个并发槽; 无空槽返回 None。只 flush, 不拥有事务。"""
    _ensure_slots(db)
    return tasks_domain.acquire_slot(db, pool_name)


def release_slot(db: Session, attempt_id: int) -> None:
    """释放绑定的并发槽。只 flush, 不拥有事务。"""
    tasks_domain.release_slot(db, attempt_id)


def _claim_task(db: Session, task_id: int, worker_id: str) -> Claim | None:
    """领取任务: 占槽 + 建尝试 + 建租约(发 fencing token) + 任务 running。

    task_attempts(running) / task_leases(active, UUID token) /
    tasks.status='running' / compute_slots.in_use+1 同事务完成。无空槽或任务
    非 queued 时返回 None(任务保持排队)。
    """
    _ensure_slots(db)
    task = _get_task(db, task_id)
    if task.status != "queued":
        return None
    pool = POOL_BY_TYPE.get(task.type, "compute")
    slot = acquire_slot(db, pool)
    if slot is None:
        return None
    attempt = tasks_domain.create_attempt(db, task_id=task.id, worker_id=worker_id)
    token = uuid4()
    tasks_domain.acquire_lease(
        db,
        attempt_id=attempt.id,
        lease_token=str(token),
        acquired_by=worker_id,
        ttl_seconds=LEASE_TTL_SECONDS,
    )
    tasks_domain.set_task_status(db, task.id, "running")
    tasks_domain.bind_slot_attempt(db, slot.id, attempt.id)
    tasks_domain.remove(task.id, pool)  # 领取后出队(视图)
    return Claim(
        task_id=task.id,
        attempt_id=attempt.id,
        attempt_no=attempt.attempt_no,
        lease_token=token,
    )


def claim_task(db: Session, task_id: int, worker_id: str) -> Claim | None:
    """领取任务用例(租约编排); 本层拥有事务提交/回滚。"""
    try:
        result = _claim_task(db, task_id, worker_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def _finish_attempt(
    db: Session, task: TaskRecord, status: str, stop_reason: str | None
) -> TaskAttemptRecord | None:
    """收尾当前运行尝试: 尝试终态 + 租约释放/吊销 + 槽释放。"""
    attempt = tasks_domain.get_running_attempt(db, task.id)
    if attempt is None:
        return None
    finished = tasks_domain.finish_attempt(db, attempt.id, status, stop_reason=stop_reason)
    lease = tasks_domain.get_active_lease_for_attempt(db, attempt.id)
    if lease is not None:
        tasks_domain.release_lease(
            db,
            attempt.id,
            lease.lease_token,
            status="released" if status == "succeeded" else "revoked",
        )
    release_slot(db, attempt.id)
    return finished


# ---------------------------------------------------------------------------
# 取消(复制 services.tasks.cancel_task/acknowledge_cancel 语义; 顶层拥有事务)
# ---------------------------------------------------------------------------


def _cancel_task(
    db: Session, task_id: int, reason: str = "user_cancel", actor_id: int | None = None
) -> TaskRecord:
    """取消任务。

    - 终态 → 409 CancelDeniedError;
    - queued(未运行) → 直接 cancelled, 出队;
    - running → cancelling(权威) + 广播取消信号; 批量父任务传播到未完成子任务
      (queued 子任务直接取消, running 子任务进 cancelling);
    - cancelling → 幂等返回。
    """
    task = _get_task(db, task_id)
    if task.status in TERMINAL_STATUSES:
        raise CancelDeniedError(
            "终态任务不可取消",
            params={"task_id": task_id, "status": task.status},
            location={"object_type": "task", "object_id": task_id},
        )
    if task.status == "queued":
        task = tasks_domain.set_task_status(db, task.id, "cancelled")
        tasks_domain.remove(task.id, POOL_BY_TYPE[task.type])
        return task
    if task.status == "cancelling":
        return task  # 取消已发起, 幂等

    task = tasks_domain.set_task_status(db, task.id, "cancelling")
    tasks_domain.set_cancel(task.id, reason)
    # 批量传播: uncertainty 父任务 → 未完成子任务
    children = tasks_domain.list_child_tasks(db, task_id)
    for child in children:
        if child.status in TERMINAL_STATUSES:
            continue
        if child.status == "queued":
            tasks_domain.set_task_status(db, child.id, "cancelled")  # 未运行的子任务直接取消
            tasks_domain.remove(child.id, POOL_BY_TYPE[child.type])
        else:
            tasks_domain.set_task_status(db, child.id, "cancelling")  # 运行中子任务由 Worker 收拢
            tasks_domain.set_cancel(child.id, reason)
    return task


def cancel_task(
    db: Session, task_id: int, reason: str = "user_cancel", actor_id: int | None = None
) -> TaskRecord:
    """取消任务用例; 本层拥有事务提交/回滚。"""
    try:
        result = _cancel_task(db, task_id, reason=reason, actor_id=actor_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def _acknowledge_cancel(db: Session, task_id: int) -> TaskRecord:
    """确认取消(Worker 终止子进程后调用; cancelling → cancelled)。

    已产出的样本/证据保留不回滚; 批量父任务在部分子任务已完成时记
    business_outcome=partial_batch。
    """
    task = _get_task(db, task_id)
    if task.status == "cancelled":
        return task
    if task.status != "cancelling":
        raise TaskStateError(
            "任务不在取消中",
            params={"task_id": task_id, "status": task.status},
            location={"object_type": "task", "object_id": task_id},
        )
    attempt = _finish_attempt(db, task, status="stopped", stop_reason="cancelled")
    outcome: str | None = None
    if task.type == "uncertainty":
        completed_children = sum(
            1 for child in tasks_domain.list_child_tasks(db, task.id) if child.status == "completed"
        )
        if completed_children > 0:
            outcome = "partial_batch"
    task = tasks_domain.set_task_status(db, task.id, "cancelled", business_outcome=outcome)
    tasks_domain.clear_cancel(task.id)
    _write_diagnostic(
        db,
        task.id,
        level=SEVERITY_INFO,
        code=TASK_QUEUED,
        message="任务已取消",
        attempt_id=attempt.id if attempt else None,
        context={"business_outcome": outcome},
    )
    return task


def acknowledge_cancel(db: Session, task_id: int) -> TaskRecord:
    """确认取消用例; 本层拥有事务提交/回滚。"""
    try:
        result = _acknowledge_cancel(db, task_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------------------
# 手动重试(复制 services.tasks.retry_task 语义; 顶层拥有事务)
# ---------------------------------------------------------------------------


def _retry_task(db: Session, user: UserRecord, task_id: int) -> TaskRecord:
    """手动重试(仅终态任务; 计算类须存在快照, 缺失 → TASK-DATA-001 blocking)。

    重试不改变 idempotency_key 语义、不改变快照; 新尝试在下次领取时创建
    (attempt_no 递增, 新租约新 token)。
    """
    task = _get_task(db, task_id)
    ensure_access(db, user, task.project_id, "edit")
    if task.status not in TERMINAL_STATUSES:
        raise TaskStateError(
            "仅终态任务可手动重试",
            params={"task_id": task_id, "status": task.status},
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
    task = tasks_domain.set_task_status(db, task.id, "queued", business_outcome=None)
    _write_diagnostic(
        db,
        task.id,
        level=SEVERITY_INFO,
        code=TASK_QUEUED,
        message="手动重试已排队",
        context={"trace_id": trace_id, "queue": pool, "snapshot_id": task.calc_snapshot_id, "retry": True},
    )
    tasks_domain.enqueue(
        task.id,
        pool,
        task_type=task.type,
        snapshot_id=task.calc_snapshot_id,
        priority=task.priority,
        trace_id=trace_id,
    )
    return task


def retry_task(db: Session, user: UserRecord, task_id: int) -> TaskRecord:
    """手动重试用例; 本层拥有事务提交/回滚。"""
    try:
        result = _retry_task(db, user, task_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
