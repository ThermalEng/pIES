"""结果用例族(application/results): 证据/评估/index/选中/视图/逐时/检查。

跨域编排的唯一实现(已收敛原 ``services.results`` 语义, 旧服务已删除):

- 证据写入: 写入资格校验(尝试 running + 租约 active + fencing token 未过期)
  → 载荷清单校验 → 打包落盘 → 证据包行 → 对象引用 → 审计(证据包只 INSERT);
- 评估写入: 经 results 域四维规则评估 → 新评估记录追加, 不覆盖原记录;
  汇总只在读取时派生;
- index 重建: 同证据挂接新评估只更新 assessment_id 指针; 新证据发布转交
  最新标记并插入新行。

单领域规则(证据/评估状态、结果错误、状态映射、四维评估规则)归 results 域
所有(``iesplan.results.rules``/``contracts``), 本层直接复用, 不复制。
本层只拥有事务、步骤顺序与 tasks/project/storage/audit/results 跨域协调。

事务: 顶层用例拥有提交/回滚(``db.commit`` 收尾, 失败 ``db.rollback``);
内部步骤只经领域公开门面写入 + flush, 不提交。

数据访问只经领域公开门面(tasks/results/project/audit/storage)与公开
metrics 状态模型; 不导入 ``models.*``。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from iesplan import audit as audit_domain
from iesplan import project as project_domain
from iesplan import results as results_domain
from iesplan import tasks as tasks_domain
from iesplan.application.projects import ensure_access
from iesplan.application.results.content import read_evidence_projection
from iesplan.application.tasks import (
    ensure_task_belongs,
    submit_task,
)
from iesplan.core.content import Content
from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.core.jsonstream import ArrayReader, JsonReadError, ObjectFields, project_json
from iesplan.identity.contracts import UserRecord
from iesplan.results.contracts import (
    SINGLE_SOLUTION_ID,
    EvidencePackageRecord,
    ResultAssessmentRecord,
    ResultIndexRecord,
    ResultInvalidRequestError,
    ResultSelectionRecord,
)
from iesplan.storage import get_object_content
from iesplan.tasks.contracts import CalcSnapshotRecord, TaskRecord

# 领域规则复导出(application 公开面保持稳定, 权威归 results 域):
# 证据/评估状态、评估/选中类型、规则版本、评估阈值、结果错误。
EVIDENCE_COMPLETE = results_domain.EVIDENCE_COMPLETE
EVIDENCE_PARTIAL = results_domain.EVIDENCE_PARTIAL
EVIDENCE_INVALID = results_domain.EVIDENCE_INVALID
ASSESSMENT_TYPES = results_domain.ASSESSMENT_TYPES
SELECTION_TYPES = results_domain.SELECTION_TYPES
ASSESSMENT_RULE_VERSION = results_domain.ASSESSMENT_RULE_VERSION
EVIDENCE_SCHEMA_VERSION = results_domain.EVIDENCE_SCHEMA_VERSION
DEFAULT_MIN_VALID_SAMPLES = results_domain.DEFAULT_MIN_VALID_SAMPLES
DEFAULT_GAP_THRESHOLD_PCT = results_domain.DEFAULT_GAP_THRESHOLD_PCT

#: 逐时查询默认/上限分页大小(存储读取编排的分页护栏, 归本层)
DEFAULT_HOURLY_LIMIT = 5000
MAX_HOURLY_LIMIT = 50000


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _get_task(db: Session, task_id: int) -> TaskRecord:
    """按 id 取任务(经 tasks 域); 不存在 404。"""
    task = tasks_domain.get_task(db, task_id)
    if task is None:
        raise NotFoundError(
            "任务不存在",
            params={"task_id": task_id},
            location={"object_type": "task", "object_id": task_id},
        )
    return task


def _audit(
    db: Session,
    entity_type: str,
    entity_id: int,
    action: str,
    *,
    actor_id: int | None = None,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    """写入不可变审计日志(只 INSERT)。"""
    audit_domain.append_entry(
        db,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_type="user" if actor_id is not None else "system",
        before=before,
        extra=after,
    )


def _evidence_project_version(db: Session, task: TaskRecord) -> int | None:
    """证据/结果索引对应的项目版本 id: 优先任务快照版本, 回退项目当前版本。"""
    if task.calc_snapshot_id is not None:
        snapshot = tasks_domain.get_snapshot(db, task.calc_snapshot_id)
        if snapshot is not None:
            return snapshot.project_version_id
    project = project_domain.get_project(db, task.project_id)
    return project.current_version_id if project is not None else None


def get_evidence(db: Session, package_id: int) -> EvidencePackageRecord:
    """按 id 读取证据包; 不存在 404。只读, 不拥有事务。"""
    package = results_domain.get_evidence(db, package_id)
    if package is None:
        raise NotFoundError(
            "证据包不存在",
            params={"evidence_package_id": package_id},
            location={"object_type": "evidence_package", "object_id": package_id},
        )
    return package


def evidence_content(db: Session, package: EvidencePackageRecord) -> dict[str, Any]:
    """读取封存证据的查询投影，不物化完整求解列/逐步值。只读。"""
    try:
        parsed = read_evidence_projection(db, package)
    except (ValueError, UnicodeDecodeError, JsonReadError) as exc:
        raise AppError(
            "证据包内容解析失败(数据损坏)",
            code="SYS-STORE-004",
            severity="error",
            message_key="ies.diag.store.corrupt",
            params={"evidence_package_id": package.id},
        ) from exc
    if not isinstance(parsed, dict):
        raise AppError(
            "证据包内容结构非法(数据损坏)",
            code="SYS-STORE-004",
            severity="error",
            message_key="ies.diag.store.corrupt",
            params={"evidence_package_id": package.id},
        )
    return parsed


def latest_evidence(db: Session, task_id: int) -> EvidencePackageRecord | None:
    """任务最新证据包。只读。"""
    return results_domain.latest_evidence_for_task(db, task_id)


def latest_assessment(db: Session, evidence_package_id: int) -> ResultAssessmentRecord | None:
    """证据包最新评估记录。只读。"""
    return results_domain.latest_assessment(db, evidence_package_id)


def list_assessments(db: Session, task_id: int) -> list[ResultAssessmentRecord]:
    """任务全部证据包上的评估历史(时间倒序)。只读。"""
    return results_domain.list_assessments_for_task(db, task_id)


def assess_evidence(
    db: Session,
    evidence_package_id: int,
    assessment_type: str = "full",
    user: UserRecord | None = None,
) -> ResultAssessmentRecord:
    """执行评估并创建新评估记录(追加式, 不覆盖原记录)。

    跨域编排: 取证据包(tasks/results)→读内容对象(storage)→经 results 域
    四维规则评估→追加落库(results)。内部步骤只 flush, 由顶层 run_assessment 提交。
    """
    package = get_evidence(db, evidence_package_id)
    content = results_domain.evidence_inner(evidence_content(db, package))
    draft = results_domain.evaluate_evidence(
        content,
        evidence_status=package.status,
        assessment_type=assessment_type,
    )
    return results_domain.create_system_assessment(
        db,
        evidence_package_id=package.id,
        dimensions=draft.dimensions,
        overall_score=draft.overall_score,
        detail=draft.detail,
        assessed_by=user.id if user is not None else None,
        comment=draft.comment,
    )


def run_assessment(
    db: Session,
    evidence_package_id: int,
    assessment_type: str = "full",
    user: UserRecord | None = None,
) -> ResultAssessmentRecord:
    """执行评估用例; 本层拥有事务提交/回滚。"""
    try:
        result = assess_evidence(db, evidence_package_id, assessment_type, user=user)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------------------
# 结果索引(复制 services.results.update_result_index 语义; 顶层拥有事务)
# ---------------------------------------------------------------------------


def refresh_result_index(
    db: Session,
    task_id: int,
    assessment_id: int,
    business_outcome: str | None = None,
) -> ResultIndexRecord:
    """更新结果索引(仅最新引用)。

    - 同证据包挂接新评估: 只更新最新索引行的 assessment_id 指针;
    - 新证据包发布: 旧行 is_latest=false, 插入新行;
    - 索引行经外键可追溯至快照/证据包/评估。内部步骤只 flush,
      由顶层 update_result_index 提交。
    """
    assessment = results_domain.get_assessment(db, assessment_id)
    if assessment is None:
        raise NotFoundError(
            "评估记录不存在",
            params={"assessment_id": assessment_id},
            location={"object_type": "result_assessment", "object_id": assessment_id},
        )
    package = results_domain.get_evidence(db, assessment.evidence_package_id)
    if package is None:
        raise AppError(
            "评估引用的证据包缺失(数据损坏)",
            code="SYS-STORE-004",
            severity="error",
            message_key="ies.diag.store.corrupt",
            params={"assessment_id": assessment_id, "evidence_package_id": assessment.evidence_package_id},
        )
    task = _get_task(db, task_id)
    project_version_id = _evidence_project_version(db, task)
    if project_version_id is None:
        raise ConflictError(
            "项目尚无版本, 结果无法建立索引",
            params={"task_id": task_id, "project_id": task.project_id},
        )
    # 同证据只更新评估指针, 新证据则转交最新标记并插入新行
    index = results_domain.point_index_latest(
        db,
        project_id=task.project_id,
        project_version_id=project_version_id,
        evidence_package_id=package.id,
        assessment_id=assessment.id,
    )
    _audit(
        db,
        "result_index",
        index.id,
        "result_index_updated",
        after={
            "task_id": task_id,
            "assessment_id": assessment_id,
            "evidence_package_id": package.id,
            "business_outcome": business_outcome,
        },
    )
    return index


def update_result_index(
    db: Session,
    task_id: int,
    assessment_id: int,
    business_outcome: str | None = None,
) -> ResultIndexRecord:
    """更新结果索引用例; 本层拥有事务提交/回滚。"""
    try:
        result = refresh_result_index(db, task_id, assessment_id, business_outcome=business_outcome)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------------------
# 评估序列化/结果索引读/选中/差异/逐时/视图/检查任务(原 services.results 语义)
# ---------------------------------------------------------------------------


def assessment_to_dict(db: Session, assessment: ResultAssessmentRecord) -> dict[str, Any]:
    """评估序列化(含只读派生摘要, 绝不覆盖原始维度)。"""
    summary = results_domain.summarize_assessment(assessment)
    states = results_domain.fine_states(assessment)
    score = assessment.overall_score
    return {
        "id": assessment.id,
        "evidence_package_id": assessment.evidence_package_id,
        "assessor": assessment.assessor,
        "assessed_by": assessment.assessed_by,
        "created_at": assessment.created_at,
        "dimensions": {
            "physical": assessment.dimension_physical,
            "optimality": assessment.dimension_optimality,
            "financial": assessment.dimension_financial,
            "reliability": assessment.dimension_reliability,
        },
        "fine_states": {k: v.value if hasattr(v, "value") else v for k, v in states.items()},
        "summary": summary,
        "overall_score": float(score) if score is not None else None,
        "comment": assessment.comment,
        "detail": assessment.detail,
    }


def latest_index(db: Session, task: TaskRecord) -> ResultIndexRecord | None:
    """任务当前版本的“最新”结果索引行(is_latest)。"""
    project_version_id = _evidence_project_version(db, task)
    if project_version_id is None:
        return None
    return results_domain.latest_index_for_version(db, project_version_id)


def current_selection(db: Session, project_id: int) -> ResultSelectionRecord | None:
    """项目当前采用结果(is_current)。"""
    return results_domain.current_selection(db, project_id)


def build_diff_patch(content: dict[str, Any], solution_id: int) -> dict[str, Any]:
    """生成参数差异补丁(选择记录追溯与差异预览用)。

    补丁形状: {"params": {"result_adoption": {...}}}。方案应用不再消费
    浏览器侧补丁, 由服务端从权威任务/快照/证据解析(apply_scheme)。
    容量只给证据原生契约粒度(type_id → 容量);
    注册表参数名解释不得经旧 engines 静态映射, 有消费方时经 devices
    公开能力解析。
    """
    candidates = content.get("candidates")
    selected: dict[str, Any] | None = None
    if isinstance(candidates, list):
        for cand in candidates:
            if isinstance(cand, dict) and int(cand.get("index", -1)) == solution_id:
                selected = cand
                break
    capacities = selected.get("capacities") if isinstance(selected, dict) else {}
    if not isinstance(capacities, dict):
        capacities = {}
    return {
        "params": {
            "result_adoption": {
                "solution_index": solution_id,
                "capacities": capacities,
                "irr": selected.get("irr") if isinstance(selected, dict) else None,
                "npv": selected.get("npv") if isinstance(selected, dict) else None,
            }
        }
    }


def _selection_solution(db: Session, selection: ResultSelectionRecord) -> int | None:
    """当前选中的解标识: 从该选中的不可变审计记录读取。"""
    rows = audit_domain.list_entries(
        db,
        entity_type="result_selections",
        entity_id=selection.id,
        action="result_selected",
        limit=1,
    )
    if rows and isinstance(rows[0].after, dict):
        return rows[0].after.get("solution_id")
    return None


def select_result(
    db: Session,
    user: UserRecord,
    task_id: int,
    solution_id: int,
    selection_type: str,
    reference_rule: str | None = None,
    reason: str | None = None,
) -> ResultSelectionRecord:
    """选择结果(追加式, 换选=新行 + 旧行 is_current=false)。

    保存: 所选解标识/选择人/类型/参考规则/理由 + 参数差异补丁(所选解标识与
    补丁承载于不可变审计日志, 供 diff 预览与结果应用追溯)。内部步骤只 flush,
    由调用方提交。
    """
    if selection_type not in SELECTION_TYPES:
        raise ResultInvalidRequestError(
            "未知选择类型",
            code="RES-REQ-003",
            params={"selection_type": selection_type, "allowed": list(SELECTION_TYPES)},
        )
    task = _get_task(db, task_id)
    ensure_access(db, user, task.project_id, "edit")
    index = latest_index(db, task)
    if index is None:
        raise NotFoundError("该任务尚无结果索引, 无法选择结果", params={"task_id": task_id})
    package = get_evidence(db, index.evidence_package_id)
    content = results_domain.evidence_inner(evidence_content(db, package))

    # 校验所选解标识在证据候选范围内
    candidate_indices = content.get("candidate_indices") or []
    candidates = content.get("candidates") or []
    valid_ids: list[int] = []
    if isinstance(candidates, list) and candidates:
        valid_ids = [int(cand.get("index", i)) for i, cand in enumerate(candidates) if isinstance(cand, dict)]
    else:
        valid_ids = [int(i) for i in candidate_indices if isinstance(i, int)]
    if solution_id not in valid_ids:
        raise ResultInvalidRequestError(
            "solution_id 不在证据候选解范围内",
            code="RES-REQ-004",
            params={"solution_id": solution_id, "candidate_indices": valid_ids},
        )

    diff_patch = build_diff_patch(content, solution_id)

    # 换选: 旧当前选中置 false, 插入新选中行(同一事务)
    selection = results_domain.select_result(
        db,
        project_id=task.project_id,
        result_index_id=index.id,
        selected_by=user.id,
        reason=reason,
    )
    _audit(
        db,
        "result_selections",
        selection.id,
        "result_selected",
        actor_id=user.id,
        after={
            "task_id": task_id,
            "solution_id": solution_id,
            "selection_type": selection_type,
            "reference_rule": reference_rule,
            "result_index_id": index.id,
            "evidence_package_id": package.id,
            "diff_patch": diff_patch,
        },
    )
    return selection


def _planning_solution(
    task: TaskRecord,
    package: EvidencePackageRecord | None,
    content: dict[str, Any],
    snapshot: CalcSnapshotRecord | None,
) -> dict[str, Any] | None:
    """规划解投影: 用调用方已有任务/证据/内容/快照材料直接投影(无新查询)。

    无证据包或封存解无 solution 块(metrics-only)是业务缺席, 返回 None,
    绝不伪造单解身份。快照冻结字段(回执名单、正文绑定)直接消费, 缺失
    即正常错误路径可见, 不伪装成无解。变量名即规划变量原名(生成器保
    留), 不按设备类型/显示名称/指标键猜测。
    """
    if package is None:
        return None
    inner = results_domain.evidence_inner(content)
    # compute_result 块缺席是合法业务形态(非计算类证据), 与块内无解区分:
    # 前者即缺席, 后者(metrics-only)亦缺席; 块形态残缺即自然错误可见。
    compute_result = inner.get("compute_result")
    if compute_result is None:
        return None
    solution = compute_result["outputs"].get("solution")
    if solution is None:
        return None
    params = results_domain.planning_solution_params(
        solution["variables"],
        snapshot.assembly_receipt["planning"]["variables"],
        json.loads(snapshot.canonical_assembly_text)["planning"]["variables"],
    )
    return {
        "solution_id": SINGLE_SOLUTION_ID,
        "single_solution": True,
        "params": params,
        "refs": {
            "task_id": task.id,
            "calc_snapshot_id": task.calc_snapshot_id,
            "project_version_id": snapshot.project_version_id,
            "evidence_package_id": package.id,
            "dataset_version_ids": list(snapshot.dataset_version_ids or []),
        },
    }


def selection_diff(db: Session, project_id: int) -> dict[str, Any] | None:
    """当前选中结果的参数差异预览(补丁 + 校验值 + 来源信息)。

    无选中时返回 None; 选中但无法解析所选解(审计缺失)抛 409 数据不一致。
    """
    selection = current_selection(db, project_id)
    if selection is None:
        return None
    solution_id = _selection_solution(db, selection)
    if solution_id is None:
        raise AppError(
            "选中记录缺少所选解标识(审计缺失, 数据不一致)",
            code="SYS-STORE-004",
            severity="error",
            message_key="ies.diag.store.corrupt",
            params={"selection_id": selection.id},
        )
    index = results_domain.get_index(db, selection.result_index_id)
    package = get_evidence(db, index.evidence_package_id) if index is not None else None
    if package is None:
        raise AppError(
            "选中结果引用的结果索引缺失(数据损坏)",
            code="SYS-STORE-004",
            severity="error",
            message_key="ies.diag.store.corrupt",
            params={"selection_id": selection.id},
        )
    content = results_domain.evidence_inner(evidence_content(db, package))
    diff_patch = build_diff_patch(content, solution_id)
    return {
        "solution_id": solution_id,
        "diff_patch": diff_patch,
        "result_index_id": index.id,
        "evidence_package_id": package.id,
        "project_version_id": index.project_version_id,
        "selected_at": selection.selected_at,
        "reason": selection.reason,
    }


def _pick_hourly_ref(refs: list[dict[str, Any]], solution_id: int | None) -> dict[str, Any]:
    """选取逐时结果引用: 显式 solution_id 优先, 缺省第一份。"""
    if solution_id is not None:
        for ref in refs:
            if int(ref.get("solution_id", -1)) == solution_id:
                return ref
        raise ResultInvalidRequestError(
            "solution_id 无对应逐时结果引用",
            code="RES-REQ-006",
            params={"solution_id": solution_id},
        )
    return refs[0]


@dataclass(frozen=True)
class HourlyReadSource:
    source: Content
    object_id: int
    field: str


def prepare_hourly_read(
    db: Session,
    content: dict[str, Any],
    field: str,
    solution_id: int | None = None,
) -> HourlyReadSource:
    """检查公开查询字段并解析会话独立的内容源，尚不扫描文件。"""
    refs = content.get("hourly_refs")
    if not isinstance(refs, list) or not refs:
        raise NotFoundError("证据包无逐时结果引用", params={"reason": "no_hourly_refs"})
    ref = _pick_hourly_ref(refs, solution_id)
    fields = ref.get("fields") or []
    if field not in fields:
        raise ResultInvalidRequestError(
            "未知逐时字段",
            code="RES-REQ-007",
            params={"field": field, "available": fields},
        )
    object_id = int(ref["object_id"])
    return HourlyReadSource(get_object_content(db, object_id), object_id, field)


def read_hourly(
    prepared: HourlyReadSource,
    start: int = 0,
    end: int | None = None,
    limit: int = DEFAULT_HOURLY_LIMIT,
) -> dict[str, Any]:
    """不持有数据库连接的逐时分页扫描；身份与单位取封存对象。"""
    field = prepared.field
    start_i = max(int(start), 0)
    limit = max(1, min(int(limit), MAX_HOURLY_LIMIT))
    requested_end = start_i + limit if end is None else min(max(int(end), start_i), start_i + limit)

    def page(items):
        values = []
        total = 0
        for total, item in enumerate(items, 1):
            if start_i <= total - 1 < requested_end:
                values.append(item)
        return total, values

    try:
        doc = project_json(prepared.source, ObjectFields({
            "data": ObjectFields({field: ArrayReader(page)}),
            "meta": ObjectFields({"units": ObjectFields({field: True}),
                                  "series": ObjectFields({field: True})}),
        }))
    except (ValueError, UnicodeDecodeError, JsonReadError) as exc:
        raise AppError(
            "逐时结果对象解析失败(数据损坏)",
            code="SYS-STORE-004",
            severity="error",
            message_key="ies.diag.store.corrupt",
            params={"object_id": prepared.object_id},
        ) from exc
    if not isinstance(doc, dict):
        raise AppError(
            "逐时结果对象结构非法(数据损坏)",
            code="SYS-STORE-004",
            severity="error",
            message_key="ies.diag.store.corrupt",
            params={"object_id": prepared.object_id},
        )
    data = doc.get("data")
    values = data.get(field) if isinstance(data, dict) else None
    if values is None:
        raise AppError(
            f"逐时结果对象缺少字段 {field}(数据损坏)",
            code="SYS-STORE-004",
            severity="error",
            message_key="ies.diag.store.corrupt",
            params={"object_id": prepared.object_id, "field": field},
        )
    total, values = values
    end_i = total if end is None else min(max(int(end), start_i), total)
    # 身份与单位取对象内已封存元数据（与值同源），分支前统一读取，
    # 空窗口（start>=total）同样返回同一身份和单位。
    meta = doc["meta"]
    unit = meta["units"][field]
    identity = meta["series"][field]
    if start_i >= total:
        return {
            "field": field,
            "unit": unit,
            **{key: value for key, value in identity.items() if key not in ("series_id", "unit")},
            "start": start_i,
            "end": start_i,
            "values": [],
            "next_start": None,
            "total_rows": total,
        }
    chunk_end = min(end_i, start_i + limit)
    return {
        "field": field,
        "unit": unit,
        **{key: value for key, value in identity.items() if key not in ("series_id", "unit")},
        "start": start_i,
        "end": chunk_end,
        "values": values,
        "next_start": chunk_end if chunk_end < end_i else None,
        "total_rows": total,
    }


def _financial_absence(
    business_outcome: str | None,
    financial: Any,
    snapshot_id: int | None,
    db: Session,
    *,
    evaluator_results: list[Any] | None = None,
) -> str | None:
    """财务评价缺块原因(只读派生, 不校验、不重算)。"""
    if financial is not None:
        return None
    if business_outcome not in ("normal_completion", "restricted_results"):
        return "outcome_not_normal"
    snapshot = tasks_domain.get_snapshot(db, snapshot_id) if snapshot_id else None
    config = snapshot.calc_config_snapshot if snapshot is not None else None
    outputs = config.get("outputs") if isinstance(config, Mapping) else None
    if isinstance(outputs, (list, tuple)) and "solution" not in outputs:
        return "solution_not_requested"
    if isinstance(evaluator_results, list):
        finance_entry = next(
            (e for e in evaluator_results if isinstance(e, Mapping) and e.get("evaluator") == "ies.evaluator.finance@1.0.0"),
            None,
        )
        if finance_entry is None:
            return "evaluators_empty"
        status = finance_entry.get("status")
        if status in ("failed", "timed_out", "cancelled"):
            return "evaluator_failed"
        if status == "skipped":
            return "outcome_not_normal"
    return "missing_unknown"


def result_view(db: Session, user: UserRecord, project_id: int, task_id: int) -> dict[str, Any]:
    """结果视图: 四维结论/业务结局/指标摘要/逐时结果引用/当前选中。

    四维结论以评估记录为准(细粒度 + 派生摘要), 不做任何重新计算。
    任务存在但尚无证据包是可查询的正常状态(任务未完成), evidence_status="no_evidence"
    显式声明; 此时各内容字段一律为 None。只读, 不拥有事务。
    """
    ensure_access(db, user, project_id, "view")
    task = ensure_task_belongs(db, project_id, task_id)
    package = latest_evidence(db, task_id)
    assessment = latest_assessment(db, package.id) if package is not None else None
    selection = current_selection(db, project_id)
    snapshot_row = (
        tasks_domain.get_snapshot(db, task.calc_snapshot_id) if task.calc_snapshot_id else None
    )
    content: dict[str, Any] = {}
    if package is not None:
        content = evidence_content(db, package)
    compute = results_domain.evidence_inner(content).get("compute_result")
    return {
        "task": {
            "id": task.id,
            "type": task.type,
            "status": task.status,
            "business_outcome": task.business_outcome,
            "calc_snapshot_id": task.calc_snapshot_id,
        },
        # no_evidence=任务尚无证据包(未完成); available=已提交证据包
        "evidence_status": "no_evidence" if package is None else "available",
        "evidence": (
            {
                "id": package.id,
                "status": package.status,
                "object_id": package.object_id,
                "attempt_id": package.attempt_id,
                "created_at": package.created_at,
            }
            if package is not None
            else None
        ),
        "assessment": assessment_to_dict(db, assessment) if assessment is not None else None,
        "metrics_summary": content.get("metrics") if content else None,
        "solver_summary": compute["outputs"].get("solver_summary") if compute is not None else None,
        # 已封存财务评价(同一证据内容原样投影, 不重算、不读最新配置)。
        "financial_evaluation": content.get("financial_evaluation") if content else None,
        # 缺块原因(可观察事实, 不猜测): outcome_not_normal=未求解/无可行方案;
        # solution_not_requested=快照 outputs 未请求 solution; missing_unknown=
        # 请求了 solution 仍无块(如历史证据); 有块时为 None。
        "financial_absence": _financial_absence(
            task.business_outcome,
            content.get("financial_evaluation") if content else None,
            task.calc_snapshot_id,
            db,
            evaluator_results=(
                content.get("evaluator_results")
                if content and isinstance(content.get("evaluator_results"), list)
                else None
            ),
        ),
        "evaluator_results": (
            list(content.get("evaluator_results") or []) if content else []
        ),
        "evaluation_reports": (
            list(content.get("evaluation_reports") or []) if content else []
        ),
        # 证据内容中的候选解列表(方案评价单解 / 规划候选列表, 含 IRR/NPV)
        "candidates": (
            content.get("candidates") if content and isinstance(content.get("candidates"), list) else None
        ),
        # 规划解投影(已有材料直接投影; 无解可读即 None, 不伪造)。
        "planning_solution": _planning_solution(task, package, content, snapshot_row),
        # 原快照资源引用(历史来源展示唯一依据; 无快照即 None, 不读当前数据)。
        "snapshot_refs": (
            {
                "snapshot_id": snapshot_row.id,
                "project_version_id": snapshot_row.project_version_id,
                "dataset_version_ids": list(snapshot_row.dataset_version_ids or []),
            }
            if snapshot_row is not None
            else None
        ),
        "best": content.get("best") if content else None,
        "plan_summary": content.get("summary") if content else None,
        "hourly_refs": content.get("hourly_refs") if content else None,
        "selection": (
            {
                "id": selection.id,
                "result_index_id": selection.result_index_id,
                "selected_by": selection.selected_by,
                "selected_at": selection.selected_at,
                "reason": selection.reason,
            }
            if selection is not None
            else None
        ),
    }


def run_check_task(
    db: Session,
    user: UserRecord,
    project_id: int,
    task_id: int,
    evidence_package_id: int | None = None,
    *,
    idempotency_key: str,
) -> tuple[TaskRecord, dict[str, bool]]:
    """对已有证据包创建检查任务(report 类型, io 池)。

    evidence_package_id 缺省取该任务最新证据包; 检查任务配置携带证据包引用,
    io Worker 消费后执行四维复查(本阶段仅创建任务)。幂等键必填(调用方提供),
    同键重试返回同一逻辑任务。提交由任务提交用例拥有。
    """
    ensure_task_belongs(db, project_id, task_id)
    if evidence_package_id is None:
        package = latest_evidence(db, task_id)
        if package is None:
            raise NotFoundError("任务尚无证据包, 无法创建检查任务", params={"task_id": task_id})
    else:
        package = get_evidence(db, evidence_package_id)
        if package.task_id != task_id:
            raise NotFoundError(
                "证据包不属于该任务",
                params={"evidence_package_id": evidence_package_id, "task_id": task_id},
            )
    return submit_task(
        db,
        user,
        project_id,
        "report",
        config={"action": "check", "evidence_package_id": package.id, "source_task_id": task_id},
        idempotency_key=idempotency_key,
    )
