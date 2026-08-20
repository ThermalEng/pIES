"""结果与证据服务(U09 结果写入单元 / U12 有效性检查单元 / U14 结果选择)。

对应 RPD 第 10/11 节、17.7/17.8 与 01-db-schema.md 第 8 节:
- submit_evidence: 证据包提交 —— 校验当前尝试写入资格(租约 + fencing token),
  保存不可变证据包(快照引用/算法/种子/停止条件/原始求解状态/候选索引/指标对象/
  逐时结果对象引用/清单 + 内容校验), 证据包只 INSERT 不 UPDATE;
- get_evidence / evidence_content: 证据读取(对象存储, 读取时校验);
- run_assessment: 四维有效性检查(物理/最优性/财务/可靠性, 调用 metrics.validity
  状态模型), 每次检查创建新评估记录不覆盖原记录; 四维结论独立记录, 汇总
  (可用/受限使用/不可用)只在读取时从细粒度状态派生, 绝不覆盖原始维度
  (核心不变量 4, RPD 10.4);
- update_result_index: 结果索引 —— 只保留最新引用, 新结果发布插入新行并转交
  旧行 is_latest 标记; 同证据包挂接新评估只更新 assessment_id 指针;
- select_result: 结果选中(追加式, 01 §8.4) —— 保存所选解标识/用户/类型/参数
  差异补丁与确认预览内容校验(所选解标识与补丁承载于不可变审计日志);
- build_diff_patch: 参数差异补丁生成(结果应用由项目单元 apply_result 处理);
- read_hourly: 逐时结果查询(对象存储, 分页);
- run_check_task: 对已有证据包创建检查任务(report 类型, io 池)。

一致性原则: 证据/评估/索引/选中全部追加式写入; 本层服务不主动 commit,
事务边界由 API 层(请求级)控制。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from iesplan.core.jsonutil import canonical_json, jsonable

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.core.idgen import sha256_hex
from iesplan.engines.planning import CAPACITY_PARAM
from iesplan.metrics import validity
from iesplan.metrics.financial import IRRStatus
from iesplan.models.audit import AuditLog, StoredObject
from iesplan.models.calc import CalcSnapshot, Task, TaskAttempt, TaskLease
from iesplan.models.common import HASH64_RE
from iesplan.models.identity import User
from iesplan.models.project import Project
from iesplan.models.result import EvidencePackage, ResultAssessment, ResultIndex, ResultSelection
from iesplan.services import objects as objects_service
from iesplan.services import project as project_service
from iesplan.services import tasks as tasks_service

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 证据包状态(01 §8.1)
EVIDENCE_COMPLETE = "complete"
EVIDENCE_PARTIAL = "partial"
EVIDENCE_INVALID = "invalid"

#: 评估类型(full=四维全查; 单维=只查该维, 其余维度记 unknown, 01 §8.2 追加语义)
ASSESSMENT_TYPES: tuple[str, ...] = ("full", "physical", "optimality", "financial", "reliability")

#: 结果选中类型(01 §8.4 业务索引)
SELECTION_TYPES: tuple[str, ...] = ("adopt", "reference")

#: 评估规则版本(规则变更时递增, 随每次评估记录保存)
ASSESSMENT_RULE_VERSION = "1.0.0"
#: 证据内容 schema 版本
EVIDENCE_SCHEMA_VERSION = "1.0.0"

#: 逐时查询默认/上限分页大小
DEFAULT_HOURLY_LIMIT = 5000
MAX_HOURLY_LIMIT = 50000

#: 可靠性有效样本下限(低于即证据不足, RPD 17.7 REQ-REL-003; 内容可覆盖)
DEFAULT_MIN_VALID_SAMPLES = 30

#: 最优性 gap 阈值(%)(02 §9.2 默认停止条件 0.1%; 内容可覆盖)
DEFAULT_GAP_THRESHOLD_PCT = 0.1

#: 证据载荷必需字段(清单部分, 与 content 内容校验值共同构成"清单+内容校验")
_REQUIRED_EVIDENCE_KEYS: tuple[str, ...] = (
    "snapshot_id", "algorithm", "seed", "stop_condition", "solve",
    "candidate_indices", "metrics", "hourly_refs", "content", "checksum",
)

#: 求解器状态 → 最优性细粒度状态(RPD 17.7 REQ-VALID-002; 与 03 规格 3.2 表同源)
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

#: 引擎内部状态码 → 最优性细粒度状态(02 §11.4)
_ENGINE_STATUS_TO_OPTIMALITY: dict[str, str] = {
    "ok": "passed",
    "time_limit": "restricted",
    "infeasible": "failed",
    "unbounded": "failed",
    "numerical_failure": "failed",
}


# ---------------------------------------------------------------------------
# 错误类型
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
    """结果域请求参数非法(如未知逐时字段/越界解索引), HTTP 400。"""

    code = "RES-REQ-001"
    http_status = 400
    severity = "error"
    message_key = "ies.diag.param.invalid"


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _get_task(db: Session, task_id: int) -> Task:
    """按 id 取任务; 不存在 404。"""
    task = db.get(Task, task_id)
    if task is None:
        raise NotFoundError(
            "任务不存在", params={"task_id": task_id},
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
    """写入不可变审计日志(01 §10.3; 本模块只 INSERT)。"""
    db.add(
        AuditLog(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            actor_id=actor_id,
            actor_type="user" if actor_id is not None else "system",
            before=before,
            after=after,
        )
    )


def _evidence_project_version(db: Session, task: Task) -> int | None:
    """证据/结果索引对应的项目版本 id: 优先任务快照版本, 回退项目当前版本。"""
    if task.calc_snapshot_id is not None:
        snapshot = db.get(CalcSnapshot, task.calc_snapshot_id)
        if snapshot is not None:
            return snapshot.project_version_id
    project = db.get(Project, task.project_id)
    return project.current_version_id if project is not None else None


# ---------------------------------------------------------------------------
# 证据服务(01 §8.1: 不可变证据包)
# ---------------------------------------------------------------------------


def _verify_write_eligibility(db: Session, task: Task, attempt_id: int, token: str | UUID) -> TaskAttempt:
    """校验当前尝试的证据写入资格(规格 4.1/4.2 租约 + fencing)。

    要求:
      1. 尝试存在且属于该任务, 状态为 running(尝试结束后不可再写证据);
      2. 租约 active 且 lease_token 与调用方持有 token 一致(fencing 防僵尸写);
      3. 租约未过期(expires_at > now)。
    任一不满足抛 EvidenceWriteDeniedError(409)。
    """
    attempt = db.get(TaskAttempt, attempt_id)
    if attempt is None:
        raise EvidenceWriteDeniedError(
            "尝试不存在", params={"task_id": task.id, "attempt_id": attempt_id},
            location={"object_type": "task_attempt", "object_id": attempt_id},
        )
    if attempt.task_id != task.id:
        raise EvidenceWriteDeniedError(
            "尝试不属于该任务", params={"task_id": task.id, "attempt_id": attempt_id},
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
            "fencing token 格式非法", params={"task_id": task.id, "attempt_id": attempt_id},
        ) from exc
    lease = db.execute(
        select(TaskLease).where(TaskLease.lease_token == token_uuid)
    ).scalar_one_or_none()
    if lease is None or lease.status != "active" or lease.attempt_id != attempt_id:
        raise EvidenceWriteDeniedError(
            "租约不匹配或已失效(fencing 拒绝)",
            params={"task_id": task.id, "attempt_id": attempt_id},
            location={"object_type": "task_lease", "object_id": attempt_id},
        )
    expires = lease.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if datetime.now(UTC) > expires:
        raise EvidenceWriteDeniedError(
            "租约已过期, 证据写入被拒绝(fencing)",
            params={"task_id": task.id, "attempt_id": attempt_id, "expires_at": expires.isoformat()},
            location={"object_type": "task_lease", "object_id": attempt_id},
        )
    return attempt


def _validate_evidence_payload(
    db: Session, task: Task, payload: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """校验证据载荷(清单 + 内容校验), 返回 (content, 问题清单)。

    校验项: 必需字段齐全(清单)、快照与任务输入一致、seed/候选索引/逐时引用
    类型合法、content 校验值(sha256)与清单一致、逐时对象引用存在且可读。
    校验不通过不抛错, 以问题清单返回 —— 由调用方落库为 status='invalid'
    (校验失败不可用, 01 §8.1), 保留审计痕迹。
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
        problems.append(
            f"快照不一致: 证据 {snapshot_id} != 任务输入 {task.calc_snapshot_id}"
        )
    checksum = payload.get("checksum")
    if not isinstance(checksum, str) or not re.fullmatch(HASH64_RE, checksum):
        problems.append("checksum 格式非法(须为 64 位十六进制)")
    content = payload.get("content")
    if not isinstance(content, dict):
        problems.append("content 必须是对象")
    elif isinstance(checksum, str) and re.fullmatch(HASH64_RE, checksum):
        if sha256_hex(canonical_json(content).encode("utf-8")) != checksum:
            problems.append("内容校验失败: content 与 checksum 不一致")
    if not isinstance(payload.get("seed"), int):
        problems.append("seed 必须是整数(可复现性, REQ-NF-001)")
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
            exists = db.execute(
                sa.select(StoredObject.id).where(StoredObject.id == obj_id)
            ).scalar_one_or_none()
            if exists is None:
                problems.append(f"hourly_refs 引用的对象不存在: object_id={obj_id}")
            if not isinstance(ref.get("fields"), list) or not ref["fields"]:
                problems.append("hourly_refs 元素缺少 fields 清单")
            if not isinstance(ref.get("rows"), int) or ref["rows"] <= 0:
                problems.append("hourly_refs 元素缺少 rows 数")
    return content if isinstance(content, dict) else {}, problems


def submit_evidence(
    db: Session,
    task_id: int,
    attempt_id: int,
    token: str | UUID,
    payload: dict[str, Any],
) -> EvidencePackage:
    """提交证据包(01 §8.1 不可变; 规格 4.1 ③ 由 Worker 在尝试内调用)。

    流程: 写入资格校验(尝试 running + 租约 active + fencing token 未过期) →
    载荷校验(清单 + content 校验值) → 打包为内容寻址对象 → 建立证据包行 →
    建立对象引用(证据包引用的对象不可清理, RPD 23.2)。证据包只 INSERT,
    同一任务每次提交追加新行(不覆盖)。
    """
    task = _get_task(db, task_id)
    if not isinstance(payload, dict):
        raise EvidenceInvalidError("证据载荷必须是 JSON 对象", code="EVID-DATA-001")
    _verify_write_eligibility(db, task, attempt_id, token)

    # 载荷校验: 校验失败仍落库但标记 invalid(校验失败不可用, 01 §8.1), 保留审计痕迹
    problems = _validate_evidence_payload(db, task, payload)[1]
    status = EVIDENCE_INVALID if problems else EVIDENCE_COMPLETE
    invalid_reason = ";".join(problems) if problems else None

    # 打包: 规范化序列化整个载荷(含 content 与 checksum, 即"清单+内容校验")
    blob = canonical_json(payload).encode("utf-8")
    content_hash = sha256_hex(blob)
    obj = objects_service.put_object(
        db, blob, content_type="application/json", source_category="evidence",
        actor_id=payload.get("created_by") or task.requested_by,
        actor_type="system",
    )
    package = EvidencePackage(
        task_id=task.id,
        attempt_id=attempt_id,
        calc_snapshot_id=task.calc_snapshot_id,
        object_id=obj.id,
        content_hash=content_hash,
        status=status,
        created_by=int(payload.get("created_by") or task.requested_by),
    )
    db.add(package)
    db.flush()
    # 对象引用: 证据包引用对象 → 禁止进入清理候选(23.2 双保险)
    objects_service.add_ref(
        db, obj.id, "evidence_package", package.id,
        purpose="evidence_content", actor_id=payload.get("created_by") or task.requested_by,
    )
    _audit(
        db, "evidence_packages", package.id, "evidence_package_created",
        actor_id=payload.get("created_by") or task.requested_by,
        after={"task_id": task.id, "attempt_id": attempt_id, "content_hash": content_hash,
               "status": status, "invalid_reason": invalid_reason, "size_bytes": len(blob)},
    )
    return package


def get_evidence(db: Session, package_id: int) -> EvidencePackage:
    """按 id 读取证据包; 不存在 404。"""
    package = db.get(EvidencePackage, package_id)
    if package is None:
        raise NotFoundError(
            "证据包不存在", params={"evidence_package_id": package_id},
            location={"object_type": "evidence_package", "object_id": package_id},
        )
    return package


def evidence_content(db: Session, package: EvidencePackage) -> dict[str, Any]:
    """读取证据包内容(对象存储, 读取时校验 sha256; 损坏抛数据损坏错误)。

    返回证据载荷完整 dict(含 content 与 checksum)。
    """
    raw = objects_service.get_object(db, package.object_id)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AppError(
            "证据包内容解析失败(数据损坏)",
            code="SYS-STORE-004", severity="error", message_key="ies.diag.store.corrupt",
            params={"evidence_package_id": package.id},
        ) from exc
    if not isinstance(parsed, dict):
        raise AppError(
            "证据包内容结构非法(数据损坏)",
            code="SYS-STORE-004", severity="error", message_key="ies.diag.store.corrupt",
            params={"evidence_package_id": package.id},
        )
    return parsed


def _evidence_inner(payload: dict[str, Any]) -> dict[str, Any]:
    """证据载荷 → 内容文档: 载荷以 {"content": {...}, "checksum": ...} 打包,
    评估/选择/差异等消费内容文档(residuals/financial/reliability/candidates)。"""
    inner = payload.get("content")
    return inner if isinstance(inner, dict) else payload


def latest_evidence(db: Session, task_id: int) -> EvidencePackage | None:
    """任务最新证据包(按创建时间倒序取最新一条)。"""
    return db.execute(
        select(EvidencePackage)
        .where(EvidencePackage.task_id == task_id)
        .order_by(EvidencePackage.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def latest_assessment(db: Session, evidence_package_id: int) -> ResultAssessment | None:
    """证据包最新评估记录(追加式, 历史通过查询 8.2 获取)。"""
    return db.execute(
        select(ResultAssessment)
        .where(ResultAssessment.evidence_package_id == evidence_package_id)
        .order_by(ResultAssessment.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def list_assessments(db: Session, task_id: int) -> list[ResultAssessment]:
    """任务全部证据包上的评估历史(不可变, 时间倒序)。"""
    return list(
        db.execute(
            select(ResultAssessment)
            .join(EvidencePackage, EvidencePackage.id == ResultAssessment.evidence_package_id)
            .where(EvidencePackage.task_id == task_id)
            .order_by(ResultAssessment.id.desc())
        ).scalars()
    )


# ---------------------------------------------------------------------------
# 四维有效性检查(U12, RPD 10.4 / 17.7)
# ---------------------------------------------------------------------------


def _check_physical(content: dict[str, Any], evidence_status: str) -> tuple[validity.PhysicalValidity, dict]:
    """物理有效性: 能量守恒残差 + 容量约束 + 边界条件(02 §9.1 后验审计)。

    证据包携带求解器残差审计结果(归一化残差 vs 容差)与约束/边界违例清单;
    缺少所需证据时不得判定通过(REQ-VALID-001)。
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
        {"name": item.get("name"), "normalized": item.get("normalized"), "tol": item.get("tol"),
         "residual": item.get("residual"), "scale": item.get("scale"), "tau": item.get("tau")}
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
    """最优性有效性: 求解状态/Gap/停止原因(REQ-VALID-002)。

    记录原始求解状态、目标值、界、相对 Gap 与停止原因; Gap 只在求解器
    给出数学上有效的 Gap 时参与判定(证据包内 stop_condition 携带)。
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
        # 无法证明最优性: 无依据不判通过(REQ-VALID-001)
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
    """财务有效性: 现金流与 IRR 状态细分(REQ-FIN-005 / 02 §5.2)。

    IRR 状态(unique/none/multiple/degenerate/out_of_domain/numerical_failure)
    映射财务细粒度状态, 使用 metrics.validity.financial_validity_from_irr。
    """
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
    """可靠性状态: 样本统计(未执行/部分/不足/有效, REQ-REL-003)。

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
    """细粒度状态 → 数据库粗粒度枚举(01 §8.2 CHECK: pass/fail/unknown)。

    数据库三值无法表达 restricted/na/insufficient, 归入 unknown; 权威细粒度
    状态与理由保存于 detail JSONB, 汇总只从细粒度派生(核心不变量 4)。
    """
    if dimension == "reliability":
        return {"ok": "pass", "insufficient": "fail"}.get(str(fine), "unknown")
    return {"passed": "pass", "failed": "fail"}.get(str(fine), "unknown")


def _overall_score(states: dict[str, Any]) -> float | None:
    """综合得分(0-100): 四维各 25 分; 全部未评估(na/未执行)返回 None。"""
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


def run_assessment(
    db: Session,
    evidence_package_id: int,
    assessment_type: str = "full",
    user: User | None = None,
) -> ResultAssessment:
    """执行四维有效性检查并创建新评估记录(追加式, 不覆盖原记录, RPD 11.2)。

    assessment_type: full=四维全查; physical/optimality/financial/reliability=
    只查单维(其余维度记 unknown)。四维结论独立记录; 汇总(可用/受限使用/不可用)
    不在本函数落库, 只在读取时派生(核心不变量 4)。
    """
    if assessment_type not in ASSESSMENT_TYPES:
        raise ResultInvalidRequestError(
            "未知评估类型", code="RES-REQ-002",
            params={"assessment_type": assessment_type, "allowed": list(ASSESSMENT_TYPES)},
        )
    package = get_evidence(db, evidence_package_id)
    content = _evidence_inner(evidence_content(db, package))

    if package.status == EVIDENCE_INVALID:
        # 校验失败不可用: 缺少可信证据, 不得判定任一维度通过(REQ-VALID-001)
        return _build_assessment(
            db, package,
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
        db, package, checked=checked,
        physical=physical, optimality=optimality, financial=financial, reliability=reliability,
        physical_checks=physical_checks, optimality_checks=optimality_checks,
        financial_checks=financial_checks, reliability_checks=reliability_checks,
        user=user,
    )


def _build_assessment(
    db: Session,
    package: EvidencePackage,
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
    user: User | None,
) -> ResultAssessment:
    """构造评估记录: 细粒度状态入 detail, 粗粒度枚举入列, 追加 INSERT。"""
    detail: dict[str, Any] = {
        "definition_version": ASSESSMENT_RULE_VERSION,
        "rule_versions": {
            "physical": ASSESSMENT_RULE_VERSION, "optimality": ASSESSMENT_RULE_VERSION,
            "financial": ASSESSMENT_RULE_VERSION, "reliability": ASSESSMENT_RULE_VERSION,
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
    assessment = ResultAssessment(
        evidence_package_id=package.id,
        assessor="system",
        assessed_by=user.id if user is not None else None,
        dimension_physical=_fine_to_db("physical", physical),
        dimension_optimality=_fine_to_db("optimality", optimality),
        dimension_financial=_fine_to_db("financial", financial),
        dimension_reliability=_fine_to_db("reliability", reliability),
        overall_score=_overall_score({"physical": physical, "optimality": optimality,
                                     "financial": financial, "reliability": reliability}),
        comment=f"系统自动评估(规则版本 {ASSESSMENT_RULE_VERSION}, 维度: {', '.join(checked)})",
        detail=detail,
    )
    db.add(assessment)
    db.flush()
    return assessment


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


def _fine_states(assessment: ResultAssessment) -> dict[str, Any]:
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


def assessment_to_dict(db: Session, assessment: ResultAssessment) -> dict[str, Any]:
    """评估序列化(含只读派生摘要, 绝不覆盖原始维度)。"""
    states = _fine_states(assessment)
    summary = validity.summarize_four_dimensions(
        states["physical"], states["optimality"], states["financial"],
        states["reliability"], states["financial_irr_status"],
    )
    score = assessment.overall_score
    return {
        "id": assessment.id,
        "evidence_package_id": assessment.evidence_package_id,
        "assessor": assessment.assessor,
        "assessed_by": assessment.assessed_by,
        "created_at": assessment.created_at.isoformat() if assessment.created_at else None,
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


# ---------------------------------------------------------------------------
# 结果索引(01 §8.3: 仅最新引用, 不覆盖历史评估)
# ---------------------------------------------------------------------------


def update_result_index(
    db: Session,
    task_id: int,
    assessment_id: int,
    business_outcome: str | None = None,
) -> ResultIndex:
    """更新结果索引(仅最新引用)。

    - 同证据包挂接新评估: 只更新最新索引行的 assessment_id 指针(历史评估仍可
      通过 8.2 查询, 不覆盖);
    - 新证据包发布: 旧行 is_latest=false, 插入新行(01 §8.3 同一事务);
    - result_hash = 输入快照哈希 + 证据内容哈希 + 业务结局(01 §8.3 业务哈希)。
    """
    assessment = db.get(ResultAssessment, assessment_id)
    if assessment is None:
        raise NotFoundError(
            "评估记录不存在", params={"assessment_id": assessment_id},
            location={"object_type": "result_assessment", "object_id": assessment_id},
        )
    package = db.get(EvidencePackage, assessment.evidence_package_id)
    if package is None:
        raise AppError(
            "评估引用的证据包缺失(数据损坏)",
            code="SYS-STORE-004", severity="error", message_key="ies.diag.store.corrupt",
            params={"assessment_id": assessment_id, "evidence_package_id": assessment.evidence_package_id},
        )
    task = _get_task(db, task_id)
    project_version_id = _evidence_project_version(db, task)
    if project_version_id is None:
        raise ConflictError(
            "项目尚无版本, 结果无法建立索引",
            params={"task_id": task_id, "project_id": task.project_id},
        )
    snapshot = db.get(CalcSnapshot, task.calc_snapshot_id) if task.calc_snapshot_id else None
    result_hash = sha256_hex(canonical_json({
        "snapshot_hash": snapshot.content_hash if snapshot is not None else None,
        "evidence_hash": package.content_hash,
        "business_outcome": business_outcome,
    }).encode("utf-8"))

    existing = db.execute(
        select(ResultIndex)
        .where(ResultIndex.project_version_id == project_version_id, ResultIndex.is_latest.is_(True))
    ).scalar_one_or_none()
    if existing is not None and existing.evidence_package_id == package.id:
        existing.assessment_id = assessment.id  # 挂接新评估, 不新增索引行
        index = existing
    else:
        if existing is not None:
            existing.is_latest = False  # 新结果发布: 转交最新标记
        index = ResultIndex(
            project_id=task.project_id,
            project_version_id=project_version_id,
            evidence_package_id=package.id,
            assessment_id=assessment.id,
            result_hash=result_hash,
            is_latest=True,
        )
        db.add(index)
    db.flush()
    _audit(
        db, "result_index", index.id, "result_index_updated",
        after={"task_id": task_id, "assessment_id": assessment_id,
               "evidence_package_id": package.id, "business_outcome": business_outcome,
               "result_hash": result_hash},
    )
    return index


def latest_index(db: Session, task: Task) -> ResultIndex | None:
    """任务当前版本的"最新"结果索引行(01 §8.3 is_latest)。"""
    project_version_id = _evidence_project_version(db, task)
    if project_version_id is None:
        return None
    return db.execute(
        select(ResultIndex)
        .where(ResultIndex.project_version_id == project_version_id, ResultIndex.is_latest.is_(True))
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# 结果选中(01 §8.4 追加式)与参数差异补丁
# ---------------------------------------------------------------------------


def build_diff_patch(content: dict[str, Any], solution_id: int) -> dict[str, Any]:
    """生成参数差异补丁(供项目单元 apply_result 应用; RPD 10.1 应用=新版本)。

    补丁形状: {"params": {"result_adoption": {...}}}, apply_result 深合并进
    calc_config.params。容量同时给出设备类型粒度(type_id → 容量)与
    注册表参数名粒度(capacity_params, 04 §3 容量参数名)。
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
        CAPACITY_PARAM.get(str(type_id), str(type_id)): value
        for type_id, value in capacities.items()
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


def _selection_solution(db: Session, selection: ResultSelection) -> int | None:
    """当前选中的解标识: 从该选中的不可变审计记录读取(01 §10.3)。"""
    row = db.execute(
        select(AuditLog)
        .where(AuditLog.entity_type == "result_selections", AuditLog.entity_id == selection.id,
               AuditLog.action == "result_selected")
        .order_by(AuditLog.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is not None and isinstance(row.after, dict):
        return row.after.get("solution_id")
    return None


def select_result(
    db: Session,
    user: User,
    task_id: int,
    solution_id: int,
    selection_type: str,
    reference_rule: str | None = None,
    reason: str | None = None,
    preview_checksum: str | None = None,
) -> ResultSelection:
    """选择结果(01 §8.4: 追加式, 换选=新行 + 旧行 is_current=false)。

    保存: 所选解标识/选择人/类型/参考规则/理由 + 参数差异补丁与确认预览内容
    校验(所选解标识与补丁承载于不可变审计日志, 供 diff 预览与结果应用追溯)。
    """
    if selection_type not in SELECTION_TYPES:
        raise ResultInvalidRequestError(
            "未知选择类型", code="RES-REQ-003",
            params={"selection_type": selection_type, "allowed": list(SELECTION_TYPES)},
        )
    task = _get_task(db, task_id)
    project_service.ensure_access(db, user, task.project_id, "edit")
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
        valid_ids = [
            int(cand.get("index", i)) for i, cand in enumerate(candidates)
            if isinstance(cand, dict)
        ]
    else:
        valid_ids = [int(i) for i in candidate_indices if isinstance(i, int)]
    if solution_id not in valid_ids:
        raise ResultInvalidRequestError(
            "solution_id 不在证据候选解范围内", code="RES-REQ-004",
            params={"solution_id": solution_id, "candidate_indices": valid_ids},
        )

    diff_patch = build_diff_patch(content, solution_id)
    # 确认预览内容校验: 客户端确认过的预览摘要必须与当前差异补丁一致
    if preview_checksum is not None:
        if not isinstance(preview_checksum, str) or not re.fullmatch(HASH64_RE, preview_checksum):
            raise ResultInvalidRequestError(
                "preview_checksum 格式非法(须为 64 位十六进制)", code="RES-REQ-005",
            )
        actual = sha256_hex(canonical_json(diff_patch).encode("utf-8"))
        if actual != preview_checksum:
            raise ConflictError(
                "确认预览内容校验失败: 差异补丁已变化, 请重新确认(RPD 20.3)",
                params={"preview_checksum": preview_checksum, "actual": actual},
            )

    # 换选: 旧当前选中置 false, 插入新选中行(01 §8.4 同一事务)
    old = db.execute(
        select(ResultSelection)
        .where(ResultSelection.project_id == task.project_id, ResultSelection.is_current.is_(True))
    ).scalar_one_or_none()
    if old is not None:
        old.is_current = False
    selection = ResultSelection(
        project_id=task.project_id,
        result_index_id=index.id,
        selected_by=user.id,
        reason=reason,
        is_current=True,
    )
    db.add(selection)
    db.flush()
    _audit(
        db, "result_selections", selection.id, "result_selected", actor_id=user.id,
        after={"task_id": task_id, "solution_id": solution_id, "selection_type": selection_type,
               "reference_rule": reference_rule, "result_index_id": index.id,
               "evidence_package_id": package.id, "diff_patch": diff_patch,
               "preview_checksum": preview_checksum},
    )
    return selection


def current_selection(db: Session, project_id: int) -> ResultSelection | None:
    """项目当前采用结果(01 §8.4 is_current)。"""
    return db.execute(
        select(ResultSelection)
        .where(ResultSelection.project_id == project_id, ResultSelection.is_current.is_(True))
    ).scalar_one_or_none()


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
            code="SYS-STORE-004", severity="error", message_key="ies.diag.store.corrupt",
            params={"selection_id": selection.id},
        )
    index = db.get(ResultIndex, selection.result_index_id)
    package = get_evidence(db, index.evidence_package_id) if index is not None else None
    if package is None:
        raise AppError(
            "选中结果引用的结果索引缺失(数据损坏)",
            code="SYS-STORE-004", severity="error", message_key="ies.diag.store.corrupt",
            params={"selection_id": selection.id},
        )
    content = _evidence_inner(evidence_content(db, package))
    diff_patch = build_diff_patch(content, solution_id)
    return {
        "solution_id": solution_id,
        "diff_patch": diff_patch,
        "preview_checksum": sha256_hex(canonical_json(diff_patch).encode("utf-8")),
        "result_index_id": index.id,
        "evidence_package_id": package.id,
        "project_version_id": index.project_version_id,
        "selected_at": selection.selected_at.isoformat() if selection.selected_at else None,
        "reason": selection.reason,
    }


# ---------------------------------------------------------------------------
# 逐时结果查询(对象存储, 分页)
# ---------------------------------------------------------------------------


def _pick_hourly_ref(refs: list[dict[str, Any]], solution_id: int | None) -> dict[str, Any]:
    """选取逐时结果引用: 显式 solution_id 优先, 缺省第一份。"""
    if solution_id is not None:
        for ref in refs:
            if int(ref.get("solution_id", -1)) == solution_id:
                return ref
        raise ResultInvalidRequestError(
            "solution_id 无对应逐时结果引用", code="RES-REQ-006",
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
    """逐时结果查询(从对象存储读取, 校验 sha256; 行号分页)。

    返回: {field, unit, start, end, values, next_start, total_rows}。
    """
    refs = content.get("hourly_refs")
    if not isinstance(refs, list) or not refs:
        raise NotFoundError("证据包无逐时结果引用", params={"reason": "no_hourly_refs"})
    ref = _pick_hourly_ref(refs, solution_id)
    fields = ref.get("fields") or []
    if field not in fields:
        raise ResultInvalidRequestError(
            "未知逐时字段", code="RES-REQ-007",
            params={"field": field, "available": fields},
        )
    raw = objects_service.get_object(db, int(ref["object_id"]))
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AppError(
            "逐时结果对象解析失败(数据损坏)",
            code="SYS-STORE-004", severity="error", message_key="ies.diag.store.corrupt",
            params={"object_id": ref["object_id"]},
        ) from exc
    if not isinstance(doc, dict):
        raise AppError(
            "逐时结果对象结构非法(数据损坏)",
            code="SYS-STORE-004", severity="error", message_key="ies.diag.store.corrupt",
            params={"object_id": ref["object_id"]},
        )
    data = doc.get("data")
    values = data.get(field) if isinstance(data, dict) else None
    if not isinstance(values, list):
        raise AppError(
            f"逐时结果对象缺少字段 {field}(数据损坏)",
            code="SYS-STORE-004", severity="error", message_key="ies.diag.store.corrupt",
            params={"object_id": ref["object_id"], "field": field},
        )
    total = len(values)
    start_i = max(int(start), 0)
    end_i = total if end is None else min(max(int(end), start_i), total)
    if start_i >= total:
        return {"field": field, "unit": None, "start": start_i, "end": start_i,
                "values": [], "next_start": None, "total_rows": total}
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


# ---------------------------------------------------------------------------
# 结果视图与检查任务
# ---------------------------------------------------------------------------


def result_view(db: Session, user: User, project_id: int, task_id: int) -> dict[str, Any]:
    """结果视图: 四维结论/业务结局/指标摘要/逐时结果引用/当前选中。

    四维结论以评估记录为准(细粒度 + 派生摘要), 不做任何重新计算(RPD 11.3)。
    """
    project_service.ensure_access(db, user, project_id, "view")
    task = tasks_service.ensure_task_belongs(db, project_id, task_id)
    package = latest_evidence(db, task_id)
    assessment = (
        latest_assessment(db, package.id) if package is not None else None
    )
    selection = current_selection(db, project_id)
    content: dict[str, Any] = {}
    if package is not None:
        content = evidence_content(db, package)
    return {
        "task": {
            "id": task.id, "type": task.type, "status": task.status,
            "business_outcome": task.business_outcome,
            "calc_snapshot_id": task.calc_snapshot_id,
        },
        "evidence": (
            {"id": package.id, "status": package.status, "content_hash": package.content_hash,
             "attempt_id": package.attempt_id, "created_at": package.created_at.isoformat()
             if package.created_at else None}
            if package is not None else None
        ),
        "assessment": assessment_to_dict(db, assessment) if assessment is not None else None,
        "metrics_summary": content.get("metrics") if content else None,
        # 证据内容中的候选解列表(方案评价单解 / 规划候选列表, 含 IRR/NPV)
        "candidates": (
            content.get("candidates")
            if content and isinstance(content.get("candidates"), list)
            else None
        ),
        "best": content.get("best") if content else None,
        "plan_summary": content.get("summary") if content else None,
        "hourly_refs": content.get("hourly_refs") if content else None,
        "selection": (
            {"id": selection.id, "result_index_id": selection.result_index_id,
             "selected_by": selection.selected_by,
             "selected_at": selection.selected_at.isoformat() if selection.selected_at else None,
             "reason": selection.reason}
            if selection is not None else None
        ),
    }


def run_check_task(
    db: Session,
    user: User,
    project_id: int,
    task_id: int,
    evidence_package_id: int | None = None,
) -> Task:
    """对已有证据包创建检查任务(report 类型, io 池, 03 规格 2.1)。

    evidence_package_id 缺省取该任务最新证据包; 检查任务配置携带证据包引用,
    io Worker 消费后执行四维复查(本阶段仅创建任务)。
    """
    tasks_service.ensure_task_belongs(db, project_id, task_id)
    if evidence_package_id is None:
        package = latest_evidence(db, task_id)
        if package is None:
            raise NotFoundError("任务尚无证据包, 无法创建检查任务", params={"task_id": task_id})
    else:
        package = get_evidence(db, evidence_package_id)
        if package.task_id != task_id:
            raise NotFoundError(
                "证据包不属于该任务", params={"evidence_package_id": evidence_package_id, "task_id": task_id},
            )
    return tasks_service.create_task(
        db, user, project_id, "report",
        config={"action": "check", "evidence_package_id": package.id, "source_task_id": task_id},
    )
