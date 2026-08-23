"""0.6.0 事项 3: 现有设备 CSV 一次性迁移到 ies.device-data 1.0.0。

- 迁移对象: catalog/<id>.csv(electric_load/heat_load/cooling_load), 给
  8760 行数据表加 ies.device-data 元数据头(原为旧版双语注释行);
- 迁移方式: 数据行原样保留(时间戳为本地无时区, fixed_offset 480),
  仅在表头之前插入标准元数据; 数值不重算、不删行、不补零;
- 迁移后必须通过 ies.device-data 1.0.0 规范化校验(任一阻断即拒迁);
- 生成迁移回执: 记录迁移文件、旧/新摘要、校验结果、行数与列声明。

回执结构(写入 catalog/migration-receipt-0.6.0.json):
{
  "migration": "ies.device-data",
  "from_schema": "legacy",
  "to_schema": "1.0.0",
  "generated_at": ISO8601 UTC,
  "entries": [
    {
      "file": "electric_load.csv",
      "device_model": "ies.device.electric_load@1.2.0",
      "old_sha256": ...,
      "new_sha256": ...,
      "row_count": 8760,
      "columns": ["timestamp", "e_load"],
      "blocking_diagnostics": [],
      "ok": true
    }
  ]
}

该回执为一次性迁移证据(只读历史), 不进入运行期读取路径; 后续装配只持有
已校验的内容引用(对象存储 ObjectId), 不依赖上传文件名。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from iesplan.devices import get_device_descriptor
from iesplan.devices.datacontract import SCHEMA_ID, SCHEMA_VERSION, canonicalize_device_data

#: 迁移的目标设备 CSV(与 YAML 同名, 平铺 catalog 布局)
MIGRATION_TARGETS: tuple[tuple[str, str], ...] = (
    ("ies.device.electric_load@1.2.0", "electric_load.csv"),
    ("ies.device.heat_load@1.1.0", "heat_load.csv"),
    ("ies.device.cooling_load@1.1.0", "cooling_load.csv"),
)

#: 迁移回执文件名(一次性证据)
RECEIPT_FILENAME = "migration-receipt-0.6.0.json"


def _meta_header(device_model: str, dataset_id: str, column: str) -> str:
    """构造 ies.device-data 元数据头(与手写样例一致)。"""
    return "\n".join(
        [
            f"# schema: {SCHEMA_ID}",
            f"# schema_version: {SCHEMA_VERSION}",
            f"# dataset_id: {dataset_id}",
            f"# device_model: {device_model}",
            "# series_mode: timeline",
            "# resolution: 1h",
            "# timestamp_mode: fixed_offset",
            "# fixed_utc_offset_minutes: 480",
            f"# unit.{column}: kWh",
            "# note.migrated_from: legacy_catalog_csv",
        ]
    )


def _migrate_csv(path: Path, device_model: str) -> dict:
    """迁移单个 catalog CSV → ies.device-data 1.0.0。

    原文件数据行原样保留(仅去掉旧版注释头), 前插标准元数据头;
    迁移后经 canonicalize_device_data 全量校验(含行数/时间戳/单位/范围)。
    """
    old_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    lines = path.read_text(encoding="utf-8").split("\n")
    # 去旧注释头(表头之前 # 行)
    data_lines: list[str] = []
    in_header = False
    for line in lines:
        stripped = line.strip()
        if not in_header:
            if not stripped:
                continue
            if stripped.startswith("#"):
                continue
            in_header = True
        data_lines.append(line)
    if not data_lines:
        raise ValueError(f"{path} 无数据行(迁移前为空)")
    header = data_lines[0]
    if "," not in header:
        raise ValueError(f"{path} 表头非法: {header!r}")
    first_col = header.split(",")[0].strip()
    if first_col != "timestamp":
        raise ValueError(f"{path} 第一列必须为 timestamp, 实际 {first_col!r}")

    dataset_id = path.stem
    column = header.split(",")[1].strip()
    meta = _meta_header(device_model, dataset_id, column)
    migrated = meta + "\n" + "\n".join(data_lines) + "\n"
    migrated_bytes = migrated.encode("utf-8")

    # 全量校验(0.6.0: 迁移后必须通过, 任一阻断即拒迁)
    desc = get_device_descriptor(device_model.split("@")[0])
    result = canonicalize_device_data(migrated_bytes, desc)
    blockers = [d.to_dict() for d in result.diagnostics if d.blocking]

    new_sha = hashlib.sha256(migrated_bytes).hexdigest()
    return {
        "file": path.name,
        "device_model": device_model,
        "old_sha256": old_sha,
        "new_sha256": new_sha,
        "row_count": len(result.rows),
        "columns": list(result.column_order),
        "blocking_diagnostics": blockers,
        "ok": not blockers,
        "migrated_bytes": migrated_bytes if not blockers else None,
    }


def migrate_catalog_csvs(catalog_dir: Path | None = None) -> dict:
    """迁移 catalog 目录下目标 CSV 并生成迁移回执(一次性)。

    全部目标迁移并校验通过后原子写回文件 + 生成回执; 任一迁移校验失败
    不写回任何文件(失败可见, 不半迁移)。
    """
    from iesplan.devices.loader import DEFAULT_CATALOG_DIR

    catalog = Path(catalog_dir) if catalog_dir is not None else DEFAULT_CATALOG_DIR
    entries: list[dict] = []
    staged: list[tuple[Path, bytes]] = []
    for device_model, fname in MIGRATION_TARGETS:
        path = catalog / fname
        if not path.exists():
            raise FileNotFoundError(f"迁移目标不存在: {path}")
        entry = _migrate_csv(path, device_model)
        entries.append(entry)
        if not entry["ok"]:
            # 任一阻断: 不写回任何文件
            raise ValueError(
                f"迁移校验失败: {entry['file']} — {[d['code'] for d in entry['blocking_diagnostics']]}"
            )
        staged.append((path, entry["migrated_bytes"]))

    # 全部通过 → 原子写回
    for path, data in staged:
        path.write_bytes(data)

    receipt = {
        "migration": "ies.device-data",
        "from_schema": "legacy_catalog_csv",
        "to_schema": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "entries": [
            {k: v for k, v in e.items() if k != "migrated_bytes"} for e in entries
        ],
    }
    receipt_path = catalog / RECEIPT_FILENAME
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return receipt


__all__ = [
    "RECEIPT_FILENAME",
    "MIGRATION_TARGETS",
    "migrate_catalog_csvs",
]
