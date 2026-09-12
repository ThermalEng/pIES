"""结果用例族(application/results): 证据/评估/index/选中/视图/逐时/检查。

结果编排的唯一实现(已收敛原 ``services.results`` 语义, 旧服务已删除):

- 证据写入: 写入资格校验(尝试 running + 租约 active + fencing token 未过期)
  → 载荷清单校验 → 打包落盘 → 证据包行 → 对象引用 → 审计(证据包只 INSERT);
- 评估写入: 四维有效性检查(物理/最优性/财务/可靠性) → 新评估记录追加,
  不覆盖原记录; 汇总只在读取时派生;
- index 重建: 同证据挂接新评估只更新 assessment_id 指针; 新证据发布转交
  最新标记并插入新行。

事务: 顶层用例拥有提交/回滚(``db.commit`` 收尾, 失败 ``db.rollback``);
内部步骤只经领域公开门面写入 + flush, 不提交。

数据访问只经领域公开门面(tasks/results/project/audit/storage)与公开
metrics 状态模型; 不导入 ``models.*``。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from iesplan import audit as audit_domain
from iesplan import project as project_domain
from iesplan import results as results_domain
from iesplan import tasks as tasks_domain
from iesplan.application.tasks.submissions import (
    ensure_project_access,
    ensure_task_belongs,
    submit_task,
)
from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.core.jsonutil import canonical_json
from iesplan.engines.planning import CAPACITY_PARAM
from iesplan.identity.contracts import UserRecord
from iesplan.metrics import validity
from iesplan.metrics.financial import IRRStatus
from iesplan.results.contracts import (
    EvidencePackageRecord,
    ResultAssessmentRecord,
    ResultIndexRecord,
    ResultSelectionRecord,
)
from iesplan.storage import add_ref, get_object, object_info, put_object
from iesplan.tasks.contracts import TaskAttemptRecord, TaskRecord

# ---------------------------------------------------------------------------
# 常量(与 services.results 同值, 复制不改语义)
# ---------------------------------------------------------------------------

#: 证据包状态
EVIDENCE_COMPLETE = "complete"
EVIDENCE_PARTIAL = "partial"
EVIDENCE_INVALID = "invalid"

#: 评估类型(full=四维全查; 单维=只查该维, 其余记 unknown)
ASSESSMENT_TYPES: tuple[str, ...] = ("full", "physical", "optimality", "financial", "reliability")

#: 结果选中类型
SELECTION_TYPES: tuple[str, ...] = ("adopt", "reference")

#: 逐时查询默认/上限分页大小
DEFAULT_HOURLY_LIMIT = 5000
MAX_HOURLY_LIMIT = 50000

#: 评估规则版本(规则变更时递增, 随每次评估记录保存)
ASSESSMENT_RULE_VERSION = "1.0.0"
#: 证据内容 schema 版本
EVIDENCE_SCHEMA_VERSION = "1.0.0"

#: 可靠性有效样本下限
DEFAULT_MIN_VALID_SAMPLES = 30

#: 最优性 gap 阈值(%)(默认 0.1%)
DEFAULT_GAP_THRESHOLD_PCT = 0.1

#: 证据载荷必需字段(清单部分)
_REQUIRED_EVIDENCE_KEYS: tuple[str, ...] = (
    "snapshot_id",
    "algorithm",
    "seed",
    "stop_condition",
    "solve",
    "candidate_indices",
    "metrics",
    "hourly_refs",
    "content",
)

#: 求解器状态 → 最优性细粒度状态
_OPTIMALITY_BY_SOLVER: dict[str, str] = {
    "OPTIMAL": "passed",
    "TIME_LIMIT_WITH_INCUMBENT": "restricted",
    "PARTIAL_BATCH": "restricted",
    "NO_FEASIBLE_FOUND": "failed",
    "INFEASIBLE": "failed",
    "INFEASIBLE_BY_IRR_FLOOR": "failed",
    "BASE_INFEASIBLE": "failed",
    "MODEL_AUDIT_FAIL": "failed",
    "NO_PARETO_FEASIBLE": "failed",
}

#: 引擎内部状态码 → 最优性细粒度状态
_ENGINE_STATUS_TO_OPTIMALITY: dict[str, str] = {
    "ok": "passed",
    "time_limit": "restricted",
    "infeasible": "failed",
    "unbounded": "failed",
    "numerical_failure": "failed",
}


# ---------------------------------------------------------------------------
# 错误类型(与 services.results 同 code/HTTP 状态, 复制不改语义)
# ---------------------------------------------------------------------------


class EvidenceWriteDeniedError(ConflictError):
    """证据写入资格校验失败(尝试状态/租约/fencing), HTTP 409。"""

    code = "EVID-FENCE-001"
    message_key = "ies.diag.evidence.write_denied"


class EvidenceInvalidError(AppError):
    """证据载荷结构非法(无法打包), HTTP 400。"""

    code = "EVID-DATA-001"
    http_status = 400
    severity = "error"
    message_key = "ies.diag.evidence.invalid"


class ResultInvalidRequestError(AppError):
    """结果域请求参数非法, HTTP 400。"""

    code = "RES-REQ-001"
    http_status = 400
    severity = "error"
    message_key = "ies.diag.param.invalid"


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


# ---------------------------------------------------------------------------
# 证据写入(复制 services.results.submit_evidence 语义; 顶层拥有事务)
# ---------------------------------------------------------------------------


def _verify_write_eligibility(
    db: Session, task: TaskRecord, attempt_id: int, token: str | UUID
) -> TaskAttemptRecord:
    """校验当前尝试的证据写入资格(租约 + fencing)。

    要求:
      1. 尝试存在且属于该任务, 状态为 running;
      2. 租约 active 且 lease_token 与调用方持有 token 一致;
      3. 租约未过期(expires_at > now)。
    任一不满足抛 EvidenceWriteDeniedError(409)。
    """
    attempt = tasks_domain.get_attempt(db, attempt_id)
    if attempt is None:
        raise EvidenceWriteDeniedError(
            "尝试不存在",
            params={"task_id": task.id, "attempt_id": attempt_id},
            location={"object_type": "task_attempt", "object_id": attempt_id},
        )
    if attempt.task_id != task.id:
        raise EvidenceWriteDeniedError(
            "尝试不属于该任务",
            params={"task_id": task.id, "attempt_id": attempt_id},
            location={"object_type": "task_attempt", "object_id": attempt_id},
        )
    if attempt.status != "running":
        raise EvidenceWriteDeniedError(
            "尝试已结束, 不再具备证据写入资格",
            params={"task_id": task.id, "attempt_id": attempt_id, "attempt_status": attempt.status},
            location={"object_type": "task_attempt", "object_id": attempt_id},
        )
    try:
        token_uuid = token if isinstance(token, UUID) else UUID(str(token))
    except (ValueError, TypeError) as exc:
        raise EvidenceWriteDeniedError(
            "fencing token 格式非法",
            params={"task_id": task.id, "attempt_id": attempt_id},
        ) from exc
    lease = tasks_domain.get_lease_by_token(db, str(token_uuid))
    if lease is None or lease.status != "active" or lease.attempt_id != attempt_id:
        raise EvidenceWriteDeniedError(
            "租约不匹配或已失效(fencing 拒绝)",
            params={"task_id": task.id, "attempt_id": attempt_id},
            location={"object_type": "task_lease", "object_id": attempt_id},
        )
    expires_dt = datetime.fromisoformat(lease.expires_at)
    if expires_dt.tzinfo is None:
        expires_dt = expires_dt.replace(tzinfo=UTC)
    if datetime.now(UTC) > expires_dt:
        raise EvidenceWriteDeniedError(
            "租约已过期, 证据写入被拒绝(fencing)",
            params={"task_id": task.id, "attempt_id": attempt_id, "expires_at": expires_dt.isoformat()},
            location={"object_type": "task_lease", "object_id": attempt_id},
        )
    return attempt


def _validate_evidence_payload(
    db: Session, task: TaskRecord, payload: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """校验证据载荷(清单校验), 返回 (content, 问题清单)。

    校验不通过不抛错, 以问题清单返回 —— 由调用方落库为 status='invalid'。
    """
    problems: list[str] = []
    missing = [key for key in _REQUIRED_EVIDENCE_KEYS if key not in payload]
    if missing:
        problems.append(f"缺少必需字段: {','.join(missing)}")
    try:
        snapshot_id = int(payload["snapshot_id"])
    except (TypeError, ValueError, KeyError):
        snapshot_id = None
        problems.append("snapshot_id 须为整数")
    if snapshot_id is not None and snapshot_id != task.calc_snapshot_id:
        problems.append(f"快照不一致: 证据 {snapshot_id} != 任务输入 {task.calc_snapshot_id}")
    content = payload.get("content")
    if not isinstance(content, dict):
        problems.append("content 必须是对象")
    if not isinstance(payload.get("seed"), int):
        problems.append("seed 必须是整数")
    for key in ("stop_condition", "solve", "metrics"):
        if not isinstance(payload.get(key), dict):
            problems.append(f"{key} 必须是对象")
    indices = payload.get("candidate_indices")
    if not isinstance(indices, list) or not all(isinstance(i, int) for i in indices):
        problems.append("candidate_indices 必须是整数数组")
    hourly_refs = payload.get("hourly_refs")
    if not isinstance(hourly_refs, list) or not hourly_refs:
        problems.append("hourly_refs 必须是非空数组(逐时结果对象引用)")
    else:
        for ref in hourly_refs:
            if not isinstance(ref, dict):
                problems.append("hourly_refs 元素必须是对象")
                continue
            obj_id = ref.get("object_id")
            if not isinstance(obj_id, int):
                problems.append("hourly_refs 元素缺少 object_id")
                continue
            try:
                object_info(db, obj_id)
            except NotFoundError:
                problems.append(f"hourly_refs 引用的对象不存在: object_id={obj_id}")
            if not isinstance(ref.get("fields"), list) or not ref["fields"]:
                problems.append("hourly_refs 元素缺少 fields 清单")
            if not isinstance(ref.get("rows"), int) or ref["rows"] <= 0:
                problems.append("hourly_refs 元素缺少 rows 数")
    return content if isinstance(content, dict) else {}, problems


def _submit_evidence(
    db: Session,
    task_id: int,
    attempt_id: int,
    token: str | UUID,
    payload: dict[str, Any],
) -> EvidencePackageRecord:
    """提交证据包(不可变)。

    流程: 写入资格校验 → 载荷清单校验 → 打包为对象存储对象 → 建立证据包行 →
    建立对象引用。证据包只 INSERT, 同一任务每次提交追加新行。内部步骤只
    flush, 由顶层 submit_evidence 提交。
    """
    task = _get_task(db, task_id)
    if not isinstance(payload, dict):
        raise EvidenceInvalidError("证据载荷必须是 JSON 对象", code="EVID-DATA-001")
    _verify_write_eligibility(db, task, attempt_id, token)

    # 载荷校验: 校验失败仍落库但标记 invalid, 保留审计
    problems = _validate_evidence_payload(db, task, payload)[1]
    status = EVIDENCE_INVALID if problems else EVIDENCE_COMPLETE
    invalid_reason = ";".join(problems) if problems else None

    # 打包: 规范化序列化整个载荷并落盘为对象存储对象
    blob = canonical_json(payload).encode("utf-8")
    obj = put_object(
        db,
        blob,
        content_type="application/json",
        source_category="evidence",
        actor_id=payload.get("created_by") or task.requested_by,
        actor_type="system",
    )
    package = results_domain.create_evidence(
        db,
        task_id=task.id,
        calc_snapshot_id=task.calc_snapshot_id,
        object_id=obj.id,
        status=status,
        attempt_id=attempt_id,
        created_by=int(payload.get("created_by") or task.requested_by),
    )
    # 对象引用: 证据包引用对象 → 禁止进入清理候选
    add_ref(
        db,
        obj.id,
        "evidence_package",
        package.id,
        purpose="evidence_content",
        actor_id=payload.get("created_by") or task.requested_by,
    )
    _audit(
        db,
        "evidence_packages",
        package.id,
        "evidence_package_created",
        actor_id=payload.get("created_by") or task.requested_by,
        after={
            "task_id": task.id,
            "attempt_id": attempt_id,
            "object_id": obj.id,
            "status": status,
            "invalid_reason": invalid_reason,
            "size_bytes": len(blob),
        },
    )
    return package


def submit_evidence(
    db: Session,
    task_id: int,
    attempt_id: int,
    token: str | UUID,
    payload: dict[str, Any],
) -> EvidencePackageRecord:
    """提交证据包用例; 本层拥有事务提交/回滚。"""
    try:
        result = _submit_evidence(db, task_id, attempt_id, token, payload)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


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
    """读取证据包内容(对象引用定位; 对象缺失/损坏抛数据损坏错误)。只读。"""
    raw = get_object(db, package.object_id)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
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


def _evidence_inner(payload: dict[str, Any]) -> dict[str, Any]:
    """证据载荷 → 内容文档: 载荷以 {"content": {...}} 打包,
    评估消费内容文档(residuals/financial/reliability/candidates)。"""
    inner = payload.get("content")
    return inner if isinstance(inner, dict) else payload


# ---------------------------------------------------------------------------
# 四维有效性检查(复制 services.results 语义)
# ---------------------------------------------------------------------------


def _check_physical(content: dict[str, Any], evidence_status: str) -> tuple[validity.PhysicalValidity, dict]:
    """物理有效性: 能量守恒残差 + 容量约束 + 边界条件。

    缺少所需证据时不得判定通过。
    """
    if evidence_status != EVIDENCE_COMPLETE:
        return validity.PhysicalValidity.insufficient, {"reason": "evidence_status_invalid"}
    residuals = content.get("residuals")
    if not isinstance(residuals, dict):
        return validity.PhysicalValidity.insufficient, {"reason": "missing_residuals"}
    items = residuals.get("items")
    if not isinstance(items, list) or not items:
        return validity.PhysicalValidity.insufficient, {"reason": "no_residual_items"}
    failed_items = [
        {
            "name": item.get("name"),
            "normalized": item.get("normalized"),
            "tol": item.get("tol"),
            "residual": item.get("residual"),
            "scale": item.get("scale"),
            "tau": item.get("tau"),
        }
        for item in items
        if isinstance(item, dict) and not item.get("passed", False)
    ]
    constraints = content.get("constraints") or {}
    capacity_violations = constraints.get("capacity_violations") or []
    boundary_violations = constraints.get("boundary_violations") or []
    checks: dict[str, Any] = {
        "residuals_all_passed": bool(residuals.get("all_passed")) and not failed_items,
        "max_normalized": residuals.get("max_normalized"),
        "capacity_violations": len(capacity_violations),
        "boundary_violations": len(boundary_violations),
        "failed_items": failed_items[:10],
    }
    if failed_items:
        return validity.PhysicalValidity.failed, checks
    if capacity_violations or boundary_violations:
        return validity.PhysicalValidity.failed, checks
    return validity.PhysicalValidity.passed, checks


def _check_optimality(content: dict[str, Any]) -> tuple[validity.OptimalityValidity, dict]:
    """最优性有效性: 求解状态/Gap/停止原因。

    记录原始求解状态、目标值、界、相对 Gap 与停止原因; Gap 只在求解器
    给出数学上有效的 Gap 时参与判定。
    """
    solve = content.get("solve") or {}
    stop = content.get("stop_condition") or {}
    solver_status = str(solve.get("solver_status") or stop.get("status") or "")
    gap = solve.get("gap")
    if gap is None:
        gap = stop.get("gap")
    gap_threshold = float(stop.get("gap_threshold_pct", DEFAULT_GAP_THRESHOLD_PCT))
    checks: dict[str, Any] = {
        "solver_status": solver_status,
        "objective": solve.get("objective"),
        "gap": gap,
        "stop_reason": stop.get("stop_reason") or solve.get("stop_reason"),
        "gap_threshold_pct": gap_threshold,
        "feasible": solve.get("feasible", solve.get("x_available")),
    }
    fine = _OPTIMALITY_BY_SOLVER.get(solver_status) or _ENGINE_STATUS_TO_OPTIMALITY.get(solver_status)
    if fine is None:
        # 无法证明最优性: 无依据不判通过
        return validity.OptimalityValidity.insufficient, checks
    if fine == "passed" and gap is not None:
        try:
            if float(gap) > gap_threshold:
                fine = "restricted"  # 相对 gap 未达标
                checks["gap_violated"] = True
        except (TypeError, ValueError):
            pass
    return validity.OptimalityValidity(fine), checks


def _check_financial(content: dict[str, Any]) -> tuple[validity.FinancialValidity, dict]:
    """财务有效性: 现金流与 IRR 状态细分。"""
    fin = content.get("financial")
    if not isinstance(fin, dict):
        return validity.FinancialValidity.insufficient, {"reason": "missing_financial"}
    irr_status: IRRStatus | None = None
    raw_status = fin.get("irr_status")
    if raw_status is not None:
        try:
            irr_status = IRRStatus(str(raw_status))
        except ValueError:
            irr_status = None
    fine = validity.financial_validity_from_irr(irr_status)
    checks: dict[str, Any] = {
        "irr": fin.get("irr"),
        "irr_status": irr_status.value if irr_status is not None else None,
        "irr_message": fin.get("irr_message"),
        "npv": fin.get("npv"),
        "investment": fin.get("investment"),
        "baseline_cost": fin.get("baseline_cost"),
        "cashflows_len": len(fin.get("cashflows") or []),
    }
    return fine, checks


def _check_reliability(content: dict[str, Any]) -> tuple[validity.ReliabilityStatus, dict]:
    """可靠性状态: 样本统计(未执行/部分/不足/有效)。

    无效样本单独统计不静默计入有效分布; 有效样本低于下限视为证据不足。
    """
    rel = content.get("reliability")
    if not isinstance(rel, dict) or not rel.get("executed"):
        return validity.ReliabilityStatus.not_executed, {"executed": False}
    total = int(rel.get("total_samples") or 0)
    valid = int(rel.get("valid_samples") or 0)
    invalid = int(rel.get("invalid_samples") or max(total - valid, 0))
    required = int(rel.get("required_valid_samples") or DEFAULT_MIN_VALID_SAMPLES)
    checks: dict[str, Any] = {
        "executed": True,
        "mode": rel.get("mode"),
        "total_samples": total,
        "valid_samples": valid,
        "invalid_samples": invalid,
        "required_valid_samples": required,
        "failure_reasons": rel.get("failure_reasons") or [],
        "scope": rel.get("scope"),
        "metrics": rel.get("metrics"),
    }
    if total <= 0 or valid <= 0:
        return validity.ReliabilityStatus.insufficient, checks
    if valid < required:
        return validity.ReliabilityStatus.insufficient, checks
    if invalid > 0 or valid < total:
        return validity.ReliabilityStatus.partial, checks
    return validity.ReliabilityStatus.ok, checks


def _fine_to_db(dimension: str, fine: Any) -> str:
    """细粒度状态 → 数据库粗粒度枚举(pass/fail/unknown)。

    数据库三值无法表达 restricted/na/insufficient, 归入 unknown; 权威细粒度
    状态与理由保存于 detail JSONB。
    """
    if dimension == "reliability":
        return {"ok": "pass", "insufficient": "fail"}.get(str(fine), "unknown")
    return {"passed": "pass", "failed": "fail"}.get(str(fine), "unknown")


def _overall_score(states: dict[str, Any]) -> float | None:
    """综合得分(0-100): 四维各 25 分; 全部未评估返回 None。"""
    score = 0.0
    any_checked = False
    for name in ("physical", "optimality", "financial"):
        if states[name] == validity.ValidityLevel.passed:
            score += 25.0
            any_checked = True
    if states["reliability"] == validity.ReliabilityStatus.ok:
        score += 25.0
        any_checked = True
    return round(score, 2) if any_checked else None


def assess_evidence(
    db: Session,
    evidence_package_id: int,
    assessment_type: str = "full",
    user: UserRecord | None = None,
) -> ResultAssessmentRecord:
    """执行四维有效性检查并创建新评估记录(追加式, 不覆盖原记录)。

    assessment_type: full=四维全查; physical/optimality/financial/reliability=
    只查单维(其余维度记 unknown)。内部步骤只 flush, 由顶层 run_assessment 提交。
    """
    if assessment_type not in ASSESSMENT_TYPES:
        raise ResultInvalidRequestError(
            "未知评估类型",
            code="RES-REQ-002",
            params={"assessment_type": assessment_type, "allowed": list(ASSESSMENT_TYPES)},
        )
    package = get_evidence(db, evidence_package_id)
    content = _evidence_inner(evidence_content(db, package))

    if package.status == EVIDENCE_INVALID:
        # 校验失败不可用: 缺少可信证据, 不得判定任一维度通过
        return _build_assessment(
            db,
            package,
            checked=["physical", "optimality", "financial", "reliability"],
            physical=validity.PhysicalValidity.insufficient,
            optimality=validity.OptimalityValidity.insufficient,
            financial=validity.FinancialValidity.insufficient,
            reliability=validity.ReliabilityStatus.not_executed,
            physical_checks={"reason": "evidence_status_invalid"},
            optimality_checks={"reason": "evidence_status_invalid"},
            financial_checks={"reason": "evidence_status_invalid", "irr_status": None},
            reliability_checks={"executed": False},
            user=user,
        )

    checked: list[str] = []
    if assessment_type in ("full", "physical"):
        physical, physical_checks = _check_physical(content, package.status)
        checked.append("physical")
    else:
        physical, physical_checks = validity.PhysicalValidity.na, {}
    if assessment_type in ("full", "optimality"):
        optimality, optimality_checks = _check_optimality(content)
        checked.append("optimality")
    else:
        optimality, optimality_checks = validity.OptimalityValidity.na, {}
    if assessment_type in ("full", "financial"):
        financial, financial_checks = _check_financial(content)
        checked.append("financial")
    else:
        financial, financial_checks = validity.FinancialValidity.na, {}
    if assessment_type in ("full", "reliability"):
        reliability, reliability_checks = _check_reliability(content)
        checked.append("reliability")
    else:
        reliability, reliability_checks = validity.ReliabilityStatus.not_executed, {}

    return _build_assessment(
        db,
        package,
        checked=checked,
        physical=physical,
        optimality=optimality,
        financial=financial,
        reliability=reliability,
        physical_checks=physical_checks,
        optimality_checks=optimality_checks,
        financial_checks=financial_checks,
        reliability_checks=reliability_checks,
        user=user,
    )


def _build_assessment(
    db: Session,
    package: EvidencePackageRecord,
    *,
    checked: list[str],
    physical: validity.PhysicalValidity,
    optimality: validity.OptimalityValidity,
    financial: validity.FinancialValidity,
    reliability: validity.ReliabilityStatus,
    physical_checks: dict[str, Any],
    optimality_checks: dict[str, Any],
    financial_checks: dict[str, Any],
    reliability_checks: dict[str, Any],
    user: UserRecord | None,
) -> ResultAssessmentRecord:
    """构造评估记录: 细粒度状态入 detail, 粗粒度枚举入列, 追加 INSERT。"""
    detail: dict[str, Any] = {
        "definition_version": ASSESSMENT_RULE_VERSION,
        "rule_versions": {
            "physical": ASSESSMENT_RULE_VERSION,
            "optimality": ASSESSMENT_RULE_VERSION,
            "financial": ASSESSMENT_RULE_VERSION,
            "reliability": ASSESSMENT_RULE_VERSION,
        },
        "checked": checked,
        "dimensions": {
            "physical": physical.value,
            "optimality": optimality.value,
            "financial": financial.value,
            "financial_irr_status": financial_checks.get("irr_status"),
            "reliability": reliability.value,
        },
        "checks": {
            "physical": physical_checks,
            "optimality": optimality_checks,
            "financial": financial_checks,
            "reliability": reliability_checks,
        },
    }
    return results_domain.create_system_assessment(
        db,
        evidence_package_id=package.id,
        dimensions={
            "physical": _fine_to_db("physical", physical),
            "optimality": _fine_to_db("optimality", optimality),
            "financial": _fine_to_db("financial", financial),
            "reliability": _fine_to_db("reliability", reliability),
        },
        overall_score=_overall_score(
            {
                "physical": physical,
                "optimality": optimality,
                "financial": financial,
                "reliability": reliability,
            }
        ),
        detail=detail,
        assessed_by=user.id if user is not None else None,
        comment=f"系统自动评估(规则版本 {ASSESSMENT_RULE_VERSION}, 维度: {', '.join(checked)})",
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


def _coerce_fine(dimension: str, value: object) -> Any:
    """detail 细粒度字符串 → 状态枚举(非法值保守回退, 不静默吞并到通过)。"""
    raw = str(value)
    if dimension == "reliability":
        try:
            return validity.ReliabilityStatus(raw)
        except ValueError:
            return validity.ReliabilityStatus.not_executed
    try:
        return validity.ValidityLevel(raw)
    except ValueError:
        return validity.ValidityLevel.na


def _fine_states(assessment: ResultAssessmentRecord) -> dict[str, Any]:
    """评估细粒度状态: 优先 detail 内独立记录的细粒度值(权威), 否则由粗粒度列回退。"""
    detail = assessment.detail or {}
    dims = detail.get("dimensions")
    if isinstance(dims, dict) and "physical" in dims and "reliability" in dims:
        return {
            "physical": _coerce_fine("physical", dims.get("physical")),
            "optimality": _coerce_fine("optimality", dims.get("optimality")),
            "financial": _coerce_fine("financial", dims.get("financial")),
            "financial_irr_status": dims.get("financial_irr_status"),
            "reliability": _coerce_fine("reliability", dims.get("reliability")),
        }
    return {
        "physical": validity.from_db_value(assessment.dimension_physical, "physical"),
        "optimality": validity.from_db_value(assessment.dimension_optimality, "optimality"),
        "financial": validity.from_db_value(assessment.dimension_financial, "financial"),
        "financial_irr_status": None,
        "reliability": validity.from_db_value(assessment.dimension_reliability, "reliability"),
    }


def assessment_to_dict(db: Session, assessment: ResultAssessmentRecord) -> dict[str, Any]:
    """评估序列化(含只读派生摘要, 绝不覆盖原始维度)。"""
    states = _fine_states(assessment)
    summary = validity.summarize_four_dimensions(
        states["physical"],
        states["optimality"],
        states["financial"],
        states["reliability"],
        states["financial_irr_status"],
    )
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
    """生成参数差异补丁(供项目单元 apply_result 应用)。

    补丁形状: {"params": {"result_adoption": {...}}}, apply_result 深合并进
    calc_config.params。容量同时给出设备类型粒度(type_id → 容量)与
    注册表参数名粒度(capacity_params)。
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
    capacity_params = {
        CAPACITY_PARAM.get(str(type_id), str(type_id)): value for type_id, value in capacities.items()
    }
    return {
        "params": {
            "result_adoption": {
                "solution_index": solution_id,
                "capacities": capacities,
                "capacity_params": capacity_params,
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
    ensure_project_access(db, user, task.project_id, "edit")
    index = latest_index(db, task)
    if index is None:
        raise NotFoundError("该任务尚无结果索引, 无法选择结果", params={"task_id": task_id})
    package = get_evidence(db, index.evidence_package_id)
    content = _evidence_inner(evidence_content(db, package))

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
    content = _evidence_inner(evidence_content(db, package))
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


def read_hourly(
    db: Session,
    content: dict[str, Any],
    field: str,
    start: int = 0,
    end: int | None = None,
    limit: int = DEFAULT_HOURLY_LIMIT,
    solution_id: int | None = None,
) -> dict[str, Any]:
    """逐时结果查询(从对象存储按对象 id 读取; 行号分页)。

    返回: {field, unit, start, end, values, next_start, total_rows}。
    """
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
    raw = get_object(db, int(ref["object_id"]))
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AppError(
            "逐时结果对象解析失败(数据损坏)",
            code="SYS-STORE-004",
            severity="error",
            message_key="ies.diag.store.corrupt",
            params={"object_id": ref["object_id"]},
        ) from exc
    if not isinstance(doc, dict):
        raise AppError(
            "逐时结果对象结构非法(数据损坏)",
            code="SYS-STORE-004",
            severity="error",
            message_key="ies.diag.store.corrupt",
            params={"object_id": ref["object_id"]},
        )
    data = doc.get("data")
    values = data.get(field) if isinstance(data, dict) else None
    if not isinstance(values, list):
        raise AppError(
            f"逐时结果对象缺少字段 {field}(数据损坏)",
            code="SYS-STORE-004",
            severity="error",
            message_key="ies.diag.store.corrupt",
            params={"object_id": ref["object_id"], "field": field},
        )
    total = len(values)
    start_i = max(int(start), 0)
    end_i = total if end is None else min(max(int(end), start_i), total)
    if start_i >= total:
        return {
            "field": field,
            "unit": None,
            "start": start_i,
            "end": start_i,
            "values": [],
            "next_start": None,
            "total_rows": total,
        }
    limit = max(1, min(int(limit), MAX_HOURLY_LIMIT))
    chunk_end = min(end_i, start_i + limit)
    meta = doc.get("meta") or {}
    units = meta.get("units") if isinstance(meta, dict) else None
    unit = units.get(field) if isinstance(units, dict) else None
    return {
        "field": field,
        "unit": unit,
        "start": start_i,
        "end": chunk_end,
        "values": values[start_i:chunk_end],
        "next_start": chunk_end if chunk_end < end_i else None,
        "total_rows": total,
    }


def result_view(db: Session, user: UserRecord, project_id: int, task_id: int) -> dict[str, Any]:
    """结果视图: 四维结论/业务结局/指标摘要/逐时结果引用/当前选中。

    四维结论以评估记录为准(细粒度 + 派生摘要), 不做任何重新计算。
    任务存在但尚无证据包是可查询的正常状态(任务未完成), evidence_status="no_evidence"
    显式声明; 此时各内容字段一律为 None。只读, 不拥有事务。
    """
    ensure_project_access(db, user, project_id, "view")
    task = ensure_task_belongs(db, project_id, task_id)
    package = latest_evidence(db, task_id)
    assessment = latest_assessment(db, package.id) if package is not None else None
    selection = current_selection(db, project_id)
    content: dict[str, Any] = {}
    if package is not None:
        content = evidence_content(db, package)
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
        # 证据内容中的候选解列表(方案评价单解 / 规划候选列表, 含 IRR/NPV)
        "candidates": (
            content.get("candidates") if content and isinstance(content.get("candidates"), list) else None
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
) -> TaskRecord:
    """对已有证据包创建检查任务(report 类型, io 池)。

    evidence_package_id 缺省取该任务最新证据包; 检查任务配置携带证据包引用,
    io Worker 消费后执行四维复查(本阶段仅创建任务)。提交由任务提交用例拥有。
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
    task, _flags = submit_task(
        db,
        user,
        project_id,
        "report",
        config={"action": "check", "evidence_package_id": package.id, "source_task_id": task_id},
    )
    return task
