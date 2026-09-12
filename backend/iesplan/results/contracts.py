"""结果域公开契约(证据/评估/索引/选中/报告表，归属 results)。

- 证据、评估一经创建不可修改；结果索引只保留最新评估引用；
- 系统评估与人工评估是不同写入入口（assessor 区分），禁止互相转换；
- 只含不可变值对象与领域错误；不导入 ORM、Session、services 或 application。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from iesplan.core.errors import ConflictError, NotFoundError


class ResultNotFoundError(NotFoundError):
    """证据/评估/索引/选中/报告不存在（沿用基类诊断码，不新增码）。"""


class ResultConflictError(ConflictError):
    """结果索引/选中并发冲突（沿用基类诊断码，不新增码）。"""


@dataclass(frozen=True, slots=True)
class EvidencePackageRecord:
    """证据包（evidence_packages 表公开视图，不可变）。"""

    id: int
    task_id: int
    calc_snapshot_id: int
    object_id: int
    status: str
    attempt_id: int | None = None
    created_by: int = 0
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class ResultAssessmentRecord:
    """结果评估（result_assessments 表公开视图，不可变）。"""

    id: int
    evidence_package_id: int
    assessor: str
    dimension_physical: str
    dimension_optimality: str
    dimension_financial: str
    dimension_reliability: str
    assessed_by: int | None = None
    overall_score: float | None = None
    comment: str | None = None
    detail: dict[str, Any] | None = None
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class ResultIndexRecord:
    """结果索引（result_index 表公开视图，只引用最新评估）。"""

    id: int
    project_id: int
    project_version_id: int
    evidence_package_id: int
    assessment_id: int | None = None
    is_latest: bool = True
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class ResultSelectionRecord:
    """结果选中（result_selections 表公开视图，追加式）。"""

    id: int
    project_id: int
    result_index_id: int
    selected_by: int
    reason: str | None = None
    is_current: bool = True
    selected_at: str | None = None


@dataclass(frozen=True, slots=True)
class ReportRecord:
    """报告（reports 表公开视图，正文在对象存储）。"""

    id: int
    project_id: int
    report_type: str
    object_id: int
    status: str
    generated_by_task_id: int | None = None
    generated_by: int = 0
