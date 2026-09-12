"""校验用例族(application/validations)。

从 services.validation 复制的 U07 项目校验流程（完整预检与财务基准确认）。
"""

from __future__ import annotations

from iesplan.application.validations.precheck import (
    BASELINE_ACTION,
    LEGACY_SERVICE_CALLS,
    ValidationReport,
    get_latest_validation_report,
    mark_baseline_confirmed,
    store_validation_report,
    validate_project,
)

__all__ = [
    "BASELINE_ACTION",
    "LEGACY_SERVICE_CALLS",
    "ValidationReport",
    "get_latest_validation_report",
    "mark_baseline_confirmed",
    "store_validation_report",
    "validate_project",
]
