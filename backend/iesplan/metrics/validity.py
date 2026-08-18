"""结果有效性四维状态模型。

依据 01-db-schema.md §8.2(result_assessments 四维评估)与 RPD 第 10.4 节。
数据库枚举为 pass/fail/unknown;本模块提供内存内更细分的状态模型:

    物理有效性 PhysicalValidity  : 潮流/热网/供需平衡
    最优性有效性 OptimalityValidity: 间隙达标/多方案排序稳定
    财务有效性 FinancialValidity : IRR/NPV 达标,附 IRR 状态细分
    可靠性状态 ReliabilityStatus  : 失负荷概率/备用裕度评估是否完成

核心不变量 4:汇总只派生"可用/受限使用/不可用"摘要,绝不隐藏任一维度
——输出中四个维度(含未执行/不适用)必须原样可见。
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from iesplan.metrics.financial import IRRStatus

_DEFINITION_VERSION = "1.0.0"
_REF_BASE = ["01-db-schema.md#8.2", "ies.validity.four_dimensions"]


class ValidityLevel(StrEnum):
    """有效性等级(物理/最优性/财务共用,通用别名;三个维度枚举见下)。"""

    passed = "passed"  # 通过
    restricted = "restricted"  # 受限:有保留地使用
    failed = "failed"  # 失败:不可用
    na = "na"  # 不适用:该维度不适用于本结果类型
    insufficient = "insufficient"  # 证据不足:无法完整评估


class PhysicalValidity(StrEnum):
    """物理可行性(供需平衡、潮流/热网)。"""

    passed = "passed"
    restricted = "restricted"
    failed = "failed"
    na = "na"
    insufficient = "insufficient"


class OptimalityValidity(StrEnum):
    """最优性(求解间隙达标、多方案排序稳定)。"""

    passed = "passed"
    restricted = "restricted"
    failed = "failed"
    na = "na"
    insufficient = "insufficient"


class FinancialValidity(StrEnum):
    """财务有效性(IRR/NPV 达标);irr_status 细分见 financial_validity_from_irr。"""

    passed = "passed"
    restricted = "restricted"
    failed = "failed"
    na = "na"
    insufficient = "insufficient"


class ReliabilityStatus(StrEnum):
    """可靠性评估执行状态(区别于前三维的等级语义)。"""

    not_executed = "not_executed"  # 未执行(该结果不要求可靠性评估)
    partial = "partial"  # 部分执行(部分场景/时段完成)
    insufficient = "insufficient"  # 证据不足
    ok = "ok"  # 完成且达标


# ---------------------------------------------------------------------------
# 财务有效性与 IRR 状态映射
# ---------------------------------------------------------------------------

_IRR_TO_FINANCIAL: dict[IRRStatus, FinancialValidity] = {
    IRRStatus.unique: FinancialValidity.passed,
    IRRStatus.multiple: FinancialValidity.restricted,  # 多根,需保守处理
    IRRStatus.none: FinancialValidity.failed,  # 无任何回报
    IRRStatus.degenerate: FinancialValidity.na,  # 现金流退化,无法评估
    IRRStatus.out_of_domain: FinancialValidity.failed,  # 无正实根,投资不可回收
    IRRStatus.numerical_failure: FinancialValidity.restricted,  # 计算受限
}


def financial_validity_from_irr(irr_status: IRRStatus | None) -> FinancialValidity:
    """由 IRR 状态派生财务有效性等级(REQ-FIN-005 细分)。"""
    if irr_status is None:
        return FinancialValidity.insufficient
    return _IRR_TO_FINANCIAL[irr_status]


# ---------------------------------------------------------------------------
# 与数据库枚举(01 §8.2)互转
# ---------------------------------------------------------------------------


def from_db_value(value: str | None, dimension: str) -> ValidityLevel | ReliabilityStatus:
    """把 DB CHECK 枚举(pass/fail/unknown)转成内存细分状态。"""
    normalized = (value or "").strip().lower()
    if dimension == "reliability":
        if normalized == "pass":
            return ReliabilityStatus.ok
        if normalized == "fail":
            return ReliabilityStatus.insufficient
        return ReliabilityStatus.not_executed  # unknown/缺失 -> 未执行
    if normalized == "pass":
        return ValidityLevel.passed
    if normalized == "fail":
        return ValidityLevel.failed
    return ValidityLevel.na  # unknown/缺失 -> 不适用


# ---------------------------------------------------------------------------
# 四维汇总
# ---------------------------------------------------------------------------


def summarize_four_dimensions(
    physical: PhysicalValidity | ValidityLevel | str,
    optimality: OptimalityValidity | ValidityLevel | str,
    financial: FinancialValidity | ValidityLevel | str,
    reliability: ReliabilityStatus | str,
    financial_irr_status: IRRStatus | str | None = None,
    refs: Sequence[str] | None = None,
) -> dict:
    """汇总四维评估结果,派生 可用(usable)/受限使用(restricted)/不可用(unusable)。

    组合规则:
      1. 任一维度 failed           -> 不可用(unusable)
      2. 至少一维 passed/ok 且其余为 passed/ok/na/not_executed -> 可用(usable)
      3. 任一 restricted/insufficient/partial -> 受限使用(restricted)
      4. 其余(全 na / 未评估)      -> 不可用(证据不足)

    输出完整保留四个维度(含 irr 细分),不隐藏任何维度(核心不变量 4)。
    """
    phy = _coerce(physical, ValidityLevel)
    opt = _coerce(optimality, ValidityLevel)
    fin = _coerce(financial, ValidityLevel)
    rel = _coerce(reliability, ReliabilityStatus)
    irr_status: IRRStatus | None = None
    if financial_irr_status is not None:
        irr_status = (
            financial_irr_status
            if isinstance(financial_irr_status, IRRStatus)
            else IRRStatus(str(financial_irr_status))
        )

    levels = (phy, opt, fin)
    # 规则 1:显式失败
    if ValidityLevel.failed in levels:
        summary = "unusable"
    else:
        has_passed = any(v == ValidityLevel.passed for v in levels) or rel == ReliabilityStatus.ok
        has_restrict = any(v in {ValidityLevel.restricted, ValidityLevel.insufficient} for v in levels)
        has_restrict = has_restrict or rel in {ReliabilityStatus.partial, ReliabilityStatus.insufficient}
        if has_passed and not has_restrict:
            summary = "usable"
        elif has_restrict:
            summary = "restricted"
        else:
            summary = "unusable"

    reasons: list[str] = []
    for name, v in (("physical", phy), ("optimality", opt), ("financial", fin)):
        if v == ValidityLevel.failed:
            reasons.append(f"{name}:failed")
        elif v in (ValidityLevel.restricted, ValidityLevel.insufficient):
            reasons.append(f"{name}:{v.value}")
    if rel != ReliabilityStatus.ok:
        reasons.append(f"reliability:{rel.value}")
    if irr_status is not None and irr_status != IRRStatus.unique:
        reasons.append(f"irr_status:{irr_status.value}")

    return {
        "summary": summary,
        "dimensions": {
            "physical": phy.value,
            "optimality": opt.value,
            "financial": fin.value,
            "financial_irr_status": irr_status.value if irr_status is not None else None,
            "reliability": rel.value,
        },
        "reasons": reasons,
        "definition_version": _DEFINITION_VERSION,
        "refs": list(refs) if refs else _REF_BASE,
    }


_VALIDITY_ENUMS = (ValidityLevel, PhysicalValidity, OptimalityValidity, FinancialValidity)


def _coerce(value: object, cls: type) -> ValidityLevel | ReliabilityStatus:
    """把字符串/枚举统一转成目标枚举(非法值抛 ValueError 以免静默吞并)。

    维度枚举(Physical/Optimality/Financial)与 ValidityLevel 按值等价
    (str 混合枚举),统一归一化到 ValidityLevel 参与组合判定。
    """
    if isinstance(value, _VALIDITY_ENUMS) or isinstance(value, ReliabilityStatus):
        if cls is ReliabilityStatus:
            if isinstance(value, ReliabilityStatus):
                return value
            return cls(str(value.value))
        return cls(value if isinstance(value, str) else value.value)
    if isinstance(value, str):
        return cls(value)
    raise TypeError(f"无法把 {value!r} 转换为 {cls.__name__}")
