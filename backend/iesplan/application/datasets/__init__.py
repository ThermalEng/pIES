"""数据集用例族(application/datasets)。

CSV 解析/校验纯函数经 dataset 域公开门面（权威唯一实现），事务型编排在
lifecycle；旧 services.dataset 已删除。
"""

from __future__ import annotations

from iesplan.application.datasets.lifecycle import (
    add_object_ref,
    create_builtin_sample,
    create_dataset,
    create_dataset_case,
    create_sample_case,
    default_user,
    get_dataset,
    get_dataset_case,
    get_dataset_version,
    get_dataset_version_case,
    get_object_bytes,
    list_dataset_versions,
    list_datasets_case,
    list_datasets_with_latest,
    put_object,
    require_project,
    upload_dataset_version,
    upload_dataset_version_case,
    version_files_summary,
)
from iesplan.dataset import (
    STANDARD_FIELDS,
    TIMESTAMP_COL,
    DataValidationError,
    FieldSpec,
    build_quality_report,
    get_template,
    parse_csv,
    validate_dataset,
)

__all__ = [
    "STANDARD_FIELDS",
    "TIMESTAMP_COL",
    "DataValidationError",
    "FieldSpec",
    "add_object_ref",
    "build_quality_report",
    "create_builtin_sample",
    "create_dataset",
    "create_dataset_case",
    "create_sample_case",
    "default_user",
    "get_dataset",
    "get_dataset_case",
    "get_dataset_version",
    "get_dataset_version_case",
    "get_object_bytes",
    "get_template",
    "list_dataset_versions",
    "list_datasets_case",
    "list_datasets_with_latest",
    "parse_csv",
    "put_object",
    "require_project",
    "upload_dataset_version",
    "upload_dataset_version_case",
    "validate_dataset",
    "version_files_summary",
]
