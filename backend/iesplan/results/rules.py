"""结果域规则(证据/评估状态、状态映射、四维评估规则，归属 results)。

Wave 3-B 自 ``application.results.writes`` 回归：单领域规则的唯一权威所有者。
本模块为纯规则，不触事务、不读写跨域数据：

- 证据包状态、评估/选中类型、规则版本与评估阈值；
- 证据载荷清单、求解器/引擎状态到最优性细粒度状态的映射；
- 四维有效性检查(物理/最优性/财务/可靠性)与综合得分；
- 细粒度状态与数据库粗粒度枚举的双向映射、评估草案装配。

四维状态词汇(``PhysicalValidity`` 等)仍以 ``iesplan.metrics.validity`` 为唯一
权威，本模块直接复用(架构门禁 14 对本域复用无状态 metrics 状态模型豁免)，
不复制枚举值。跨域编排(任务/项目/对象存储/审计)仍归 application.results。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from iesplan.metrics.financial import IRRStatus
from iesplan.metrics.validity import (
    FinancialValidity,
    OptimalityValidity,
    PhysicalValidity,
    ReliabilityStatus,
    ValidityLevel,
    financial_validity_from_irr,
    from_db_value,
)
from iesplan.results.contracts import ResultAssessmentRecord, ResultInvalidRequestError

# ---------------------------------------------------------------------------
# 证据/评估状态(唯一权威，application 直接复用)
# ---------------------------------------------------------------------------

#: 证据包状态
EVIDENCE_COMPLETE = "complete"
EVIDENCE_PARTIAL = "partial"
EVIDENCE_INVALID = "invalid"

#: 评估类型(full=四维全查; 单维=只查该维, 其余记 unknown)
ASSESSMENT_TYPES: tuple[str, ...] = ("full", "physical", "optimality", "financial", "reliability")

#: 结果选中类型
SELECTION_TYPES: tuple[str, ...] = ("adopt", "reference")

#: 评估规则版本(规则变更时递增, 随每次评估记录保存)
ASSESSMENT_RULE_VERSION = "1.0.0"
#: 证据内容 schema 版本
EVIDENCE_SCHEMA_VERSION = "1.0.0"

#: 可靠性有效样本下限
DEFAULT_MIN_VALID_SAMPLES = 30

#: 最优性 gap 阈值(%)(默认 0.1%)
DEFAULT_GAP_THRESHOLD_PCT = 0.1

#: 证据载荷必需字段(清单部分)
REQUIRED_EVIDENCE_KEYS: tuple[str, ...] = (
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
OPTIMALITY_BY_SOLVER: dict[str, str] = {
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
ENGINE_STATUS_TO_OPTIMALITY: dict[str, str] = {
    "ok": "passed",
    "time_limit": "restricted",
    "infeasible": "failed",
    "unbounded": "failed",
    "numerical_failure": "failed",
}


def evidence_inner(payload: dict[str, Any]) -> dict[str, Any]:
    """证据载荷 → 内容文档: 载荷以 {"content": {...}} 打包,
    评估消费内容文档(residuals/financial/reliability/candidates)。"""
    inner = payload.get("content")
    return inner if isinstance(inner, dict) else payload


# ---------------------------------------------------------------------------
# 四维有效性检查
# ---------------------------------------------------------------------------


def check_physical(content: dict[str, Any], evidence_status: str) -> tuple[PhysicalValidity, dict]:
    """物理有效性: 能量守恒残差 + 容量约束 + 边界条件。

    缺少所需证据时不得判定通过。
    """
    if evidence_status != EVIDENCE_COMPLETE:
        return PhysicalValidity.insufficient, {"reason": "evidence_status_invalid"}
    residuals = content.get("residuals")
    if not isinstance(residuals, dict):
        return PhysicalValidity.insufficient, {"reason": "missing_residuals"}
    items = residuals.get("items")
    if not isinstance(items, list) or not items:
        return PhysicalValidity.insufficient, {"reason": "no_residual_items"}
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
        return PhysicalValidity.failed, checks
    if capacity_violations or boundary_violations:
        return PhysicalValidity.failed, checks
    return PhysicalValidity.passed, checks


def check_optimality(content: dict[str, Any]) -> tuple[OptimalityValidity, dict]:
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
    fine = OPTIMALITY_BY_SOLVER.get(solver_status) or ENGINE_STATUS_TO_OPTIMALITY.get(solver_status)
    if fine is None:
        # 无法证明最优性: 无依据不判通过
        return OptimalityValidity.insufficient, checks
    if fine == "passed" and gap is not None:
        try:
            if float(gap) > gap_threshold:
                fine = "restricted"  # 相对 gap 未达标
                checks["gap_violated"] = True
        except (TypeError, ValueError):
            pass
    return OptimalityValidity(fine), checks


def check_financial(content: dict[str, Any]) -> tuple[FinancialValidity, dict]:
    """财务有效性: 现金流与 IRR 状态细分。"""
    fin = content.get("financial")
    if not isinstance(fin, dict):
        return FinancialValidity.insufficient, {"reason": "missing_financial"}
    irr_status: IRRStatus | None = None
    raw_status = fin.get("irr_status")
    if raw_status is not None:
        try:
            irr_status = IRRStatus(str(raw_status))
        except ValueError:
            irr_status = None
    fine = financial_validity_from_irr(irr_status)
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


def check_reliability(content: dict[str, Any]) -> tuple[ReliabilityStatus, dict]:
    """可靠性状态: 样本统计(未执行/部分/不足/有效)。

    无效样本单独统计不静默计入有效分布; 有效样本低于下限视为证据不足。
    """
    rel = content.get("reliability")
    if not isinstance(rel, dict) or not rel.get("executed"):
        return ReliabilityStatus.not_executed, {"executed": False}
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
        return ReliabilityStatus.insufficient, checks
    if valid < required:
        return ReliabilityStatus.insufficient, checks
    if invalid > 0 or valid < total:
        return ReliabilityStatus.partial, checks
    return ReliabilityStatus.ok, checks


def fine_to_db(dimension: str, fine: Any) -> str:
    """细粒度状态 → 数据库粗粒度枚举(pass/fail/unknown)。

    数据库三值无法表达 restricted/na/insufficient, 归入 unknown; 权威细粒度
    状态与理由保存于 detail JSONB。
    """
    if dimension == "reliability":
        return {"ok": "pass", "insufficient": "fail"}.get(str(fine), "unknown")
    return {"passed": "pass", "failed": "fail"}.get(str(fine), "unknown")


def overall_score(states: dict[str, Any]) -> float | None:
    """综合得分(0-100): 四维各 25 分; 全部未评估返回 None。"""
    score = 0.0
    any_checked = False
    for name in ("physical", "optimality", "financial"):
        if states[name] == ValidityLevel.passed:
            score += 25.0
            any_checked = True
    if states["reliability"] == ReliabilityStatus.ok:
        score += 25.0
        any_checked = True
    return round(score, 2) if any_checked else None


def check_outcome(dimensions: dict[str, str]) -> str:
    """检查评估 → 业务结局: 任一维 fail → insufficient_evidence, 否则 normal_completion。

    输入为数据库粗粒度枚举(pass/fail/unknown); 本规则为检查类任务业务结局
    的唯一权威, Worker 与 application 只消费不复制。
    """
    for name in ("physical", "optimality", "financial", "reliability"):
        if dimensions.get(name) == "fail":
            return "insufficient_evidence"
    return "normal_completion"


def coerce_fine(dimension: str, value: object) -> Any:
    """detail 细粒度字符串 → 状态枚举(非法值保守回退, 不静默吞并到通过)。"""
    raw = str(value)
    if dimension == "reliability":
        try:
            return ReliabilityStatus(raw)
        except ValueError:
            return ReliabilityStatus.not_executed
    try:
        return ValidityLevel(raw)
    except ValueError:
        return ValidityLevel.na


def fine_states(assessment: ResultAssessmentRecord) -> dict[str, Any]:
    """评估细粒度状态: 优先 detail 内独立记录的细粒度值(权威), 否则由粗粒度列回退。"""
    detail = assessment.detail or {}
    dims = detail.get("dimensions")
    if isinstance(dims, dict) and "physical" in dims and "reliability" in dims:
        return {
            "physical": coerce_fine("physical", dims.get("physical")),
            "optimality": coerce_fine("optimality", dims.get("optimality")),
            "financial": coerce_fine("financial", dims.get("financial")),
            "financial_irr_status": dims.get("financial_irr_status"),
            "reliability": coerce_fine("reliability", dims.get("reliability")),
        }
    return {
        "physical": from_db_value(assessment.dimension_physical, "physical"),
        "optimality": from_db_value(assessment.dimension_optimality, "optimality"),
        "financial": from_db_value(assessment.dimension_financial, "financial"),
        "financial_irr_status": None,
        "reliability": from_db_value(assessment.dimension_reliability, "reliability"),
    }


@dataclass(frozen=True, slots=True)
class SystemAssessmentDraft:
    """系统评估草案(四维规则的完整输出, application 只负责落库与事务)。

    dimensions 为数据库粗粒度枚举, detail 承载权威细粒度状态与检查依据。
    """

    dimensions: dict[str, str]
    overall_score: float | None
    detail: dict[str, Any]
    comment: str


def evaluate_evidence(
    content: dict[str, Any],
    *,
    evidence_status: str,
    assessment_type: str = "full",
) -> SystemAssessmentDraft:
    """执行四维有效性检查并装配系统评估草案(追加式落库由调用方完成)。

    assessment_type: full=四维全查; physical/optimality/financial/reliability=
    只查单维(其余维度记 unknown)。校验失败的证据包不可用: 缺少可信证据,
    不得判定任一维度通过。
    """
    if assessment_type not in ASSESSMENT_TYPES:
        raise ResultInvalidRequestError(
            "未知评估类型",
            code="RES-REQ-002",
            params={"assessment_type": assessment_type, "allowed": list(ASSESSMENT_TYPES)},
        )

    if evidence_status == EVIDENCE_INVALID:
        # 校验失败不可用: 缺少可信证据, 不得判定任一维度通过
        return _draft_assessment(
            checked=["physical", "optimality", "financial", "reliability"],
            physical=PhysicalValidity.insufficient,
            optimality=OptimalityValidity.insufficient,
            financial=FinancialValidity.insufficient,
            reliability=ReliabilityStatus.not_executed,
            physical_checks={"reason": "evidence_status_invalid"},
            optimality_checks={"reason": "evidence_status_invalid"},
            financial_checks={"reason": "evidence_status_invalid", "irr_status": None},
            reliability_checks={"executed": False},
        )

    checked: list[str] = []
    if assessment_type in ("full", "physical"):
        physical, physical_checks = check_physical(content, evidence_status)
        checked.append("physical")
    else:
        physical, physical_checks = PhysicalValidity.na, {}
    if assessment_type in ("full", "optimality"):
        optimality, optimality_checks = check_optimality(content)
        checked.append("optimality")
    else:
        optimality, optimality_checks = OptimalityValidity.na, {}
    if assessment_type in ("full", "financial"):
        financial, financial_checks = check_financial(content)
        checked.append("financial")
    else:
        financial, financial_checks = FinancialValidity.na, {}
    if assessment_type in ("full", "reliability"):
        reliability, reliability_checks = check_reliability(content)
        checked.append("reliability")
    else:
        reliability, reliability_checks = ReliabilityStatus.not_executed, {}

    return _draft_assessment(
        checked=checked,
        physical=physical,
        optimality=optimality,
        financial=financial,
        reliability=reliability,
        physical_checks=physical_checks,
        optimality_checks=optimality_checks,
        financial_checks=financial_checks,
        reliability_checks=reliability_checks,
    )


def _draft_assessment(
    *,
    checked: list[str],
    physical: PhysicalValidity,
    optimality: OptimalityValidity,
    financial: FinancialValidity,
    reliability: ReliabilityStatus,
    physical_checks: dict[str, Any],
    optimality_checks: dict[str, Any],
    financial_checks: dict[str, Any],
    reliability_checks: dict[str, Any],
) -> SystemAssessmentDraft:
    """装配系统评估草案: 细粒度状态入 detail, 粗粒度枚举入列。"""
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
    states = {
        "physical": physical,
        "optimality": optimality,
        "financial": financial,
        "reliability": reliability,
    }
    return SystemAssessmentDraft(
        dimensions={
            "physical": fine_to_db("physical", physical),
            "optimality": fine_to_db("optimality", optimality),
            "financial": fine_to_db("financial", financial),
            "reliability": fine_to_db("reliability", reliability),
        },
        overall_score=overall_score(states),
        detail=detail,
        comment=f"系统自动评估(规则版本 {ASSESSMENT_RULE_VERSION}, 维度: {', '.join(checked)})",
    )
