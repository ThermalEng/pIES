"""结果域公开门面（证据/评估/索引/选中/报告表，归属 results）。

外部只允许经本门面消费 contract 与 repository 协议；不得导入本域
repository 实现（切片 5 落实）、`iesplan.models` 或 services。
"""

from __future__ import annotations

from iesplan.results.contracts import (
    EvidencePackageRecord,
    ReportRecord,
    ResultAssessmentRecord,
    ResultConflictError,
    ResultIndexRecord,
    ResultNotFoundError,
    ResultSelectionRecord,
)
from iesplan.results.repository import ResultsRepository

__all__ = [
    "EvidencePackageRecord",
    "ReportRecord",
    "ResultAssessmentRecord",
    "ResultConflictError",
    "ResultIndexRecord",
    "ResultNotFoundError",
    "ResultSelectionRecord",
    "ResultsRepository",
]
