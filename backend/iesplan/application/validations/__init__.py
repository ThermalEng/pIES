"""校验用例族(application/validations)。

U07 项目校验流程（完整预检与财务基准确认；旧 services.validation 已删除，
载体/端口映射取值与 application.models 唯一实现一致）。
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
