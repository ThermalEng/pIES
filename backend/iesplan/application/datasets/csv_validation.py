"""数据集 CSV 规则兼容重导出(application/datasets)。

权威实现已收敛至 ``iesplan.dataset.tables``（dataset 域唯一实现）；
本模块仅作导入路径兼容重导出，不保留任何从 service 复制的映射/校验表，
不新增校验/哈希/回退。新代码请直接经 ``iesplan.dataset`` 领域公开门面消费。
"""

from __future__ import annotations

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
    "FieldSpec",
    "build_quality_report",
    "get_template",
    "normalized_to_csv_bytes",
    "parse_csv",
    "unit_matches",
    "validate_dataset",
]
