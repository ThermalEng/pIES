"""数据集域公开门面（datasets/dataset_versions/dataset_files 表，归属 dataset）。

外部只允许经本门面消费 contract、repository 协议与 repository 实现函数；
不得导入 `iesplan.models`、services 或其他域的内部模块。
"""

from __future__ import annotations

from iesplan.dataset import persistence, tables
from iesplan.dataset.contracts import (
    DatasetConflictError,
    DatasetFileRecord,
    DatasetNotFoundError,
    DatasetRecord,
    DatasetVersionRecord,
)
from iesplan.dataset.tables import (
    DATA_FILE_DECODE,
    DATA_FILE_EMPTY,
    DATA_FILE_ROW_WIDTH,
    DATA_FILE_TS_PARSE,
    DATA_TS_OUT_CALENDAR,
    DATA_TS_OUT_OF_ORDER,
    DATA_TS_ROW_COUNT,
    DATA_TS_STEP_MISALIGNED,
    DEFAULT_SOURCE_CATEGORY,
    REQUIRED_COLUMNS,
    REQUIRED_FIELDS,
    SAMPLE_LICENSE,
    STANDARD_FIELDS,
    TIMELINE_MAP,
    TIMESTAMP_COL,
    DataValidationError,
    FieldSpec,
    build_quality_report,
    get_template,
    normalized_to_csv_bytes,
    parse_csv,
    unit_matches,
    validate_dataset,
)

add_file = persistence.add_file
create_dataset = persistence.create_dataset
create_version = persistence.create_version
get_dataset = persistence.get_dataset
get_latest_version = persistence.get_latest_version
get_version = persistence.get_version
get_version_by_no = persistence.get_version_by_no
list_dataset_ids = persistence.list_dataset_ids
list_datasets = persistence.list_datasets
list_files = persistence.list_files
list_versions = persistence.list_versions
list_versions_by_ids = persistence.list_versions_by_ids
set_dataset_status = persistence.set_dataset_status

#: 领域不可变表清单(唯一真相归 persistence 所有, 本门面只做引用重导出)。
IMMUTABLE_TABLES = persistence.IMMUTABLE_TABLES


def install_tables() -> None:
    """公开生命周期钩子: 导入本域 persistence 即完成 Base.metadata 表注册(幂等, 无其他副作用)。"""
    persistence.install_tables()


def install_triggers() -> tuple[str, ...]:
    """公开生命周期钩子: 返回本域触发器部署语句(按执行序, 供组合根编排收集)。"""
    return persistence.install_triggers()


__all__ = [
    "DATA_FILE_DECODE",
    "DATA_FILE_EMPTY",
    "DATA_FILE_ROW_WIDTH",
    "DATA_FILE_TS_PARSE",
    "DATA_TS_OUT_CALENDAR",
    "DATA_TS_OUT_OF_ORDER",
    "DATA_TS_ROW_COUNT",
    "DATA_TS_STEP_MISALIGNED",
    "DEFAULT_SOURCE_CATEGORY",
    "REQUIRED_COLUMNS",
    "REQUIRED_FIELDS",
    "SAMPLE_LICENSE",
    "STANDARD_FIELDS",
    "TIMELINE_MAP",
    "TIMESTAMP_COL",
    "DataValidationError",
    "DatasetConflictError",
    "DatasetFileRecord",
    "DatasetNotFoundError",
    "DatasetRecord",
    "DatasetVersionRecord",
    "FieldSpec",
    "add_file",
    "build_quality_report",
    "create_dataset",
    "create_version",
    "get_dataset",
    "get_latest_version",
    "get_template",
    "get_version",
    "get_version_by_no",
    "list_dataset_ids",
    "list_datasets",
    "list_files",
    "list_versions",
    "list_versions_by_ids",
    "normalized_to_csv_bytes",
    "parse_csv",
    "set_dataset_status",
    "tables",
    "unit_matches",
    "validate_dataset",
    "IMMUTABLE_TABLES",
    "install_tables",
    "install_triggers",
]
