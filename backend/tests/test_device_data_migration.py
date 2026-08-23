"""0.6.0 事项 3: 现有设备 CSV 迁移到 ies.device-data 1.0.0 + 迁移回执。

- 迁移后的 catalog CSV 通过 ies.device-data 1.0.0 校验;
- 迁移回执记录迁移文件、旧/新摘要、校验结果、行数与列声明;
- 后续装配只持有已校验的内容引用(对象存储 ObjectId), 不依赖上传文件名。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from iesplan.devices import get_device_descriptor
from iesplan.devices.csv_migration import MIGRATION_TARGETS, RECEIPT_FILENAME, migrate_catalog_csvs
from iesplan.devices.datacontract import SCHEMA_ID, SCHEMA_VERSION, canonicalize_device_data
from iesplan.devices.profile import load_profile_columns

CATALOG_DIR = Path(__file__).resolve().parents[1] / "iesplan/devices/catalog"


class TestCatalogMigration:
    def test_migrated_catalog_csvs_validate(self) -> None:
        """迁移后的 catalog CSV 全部通过 ies.device-data 1.0.0 校验。"""
        for device_model, fname in MIGRATION_TARGETS:
            path = CATALOG_DIR / fname
            assert path.exists(), f"{fname} 缺失"
            desc = get_device_descriptor(device_model.split("@")[0])
            result = canonicalize_device_data(path.read_bytes(), desc)
            blockers = [d for d in result.diagnostics if d.blocking]
            assert not blockers, f"{fname}: {[d.code for d in blockers]}"
            assert len(result.rows) == 8760, f"{fname} 行数应为 8760"
            # 元数据头已声明
            text = path.read_text(encoding="utf-8")
            assert f"# schema: {SCHEMA_ID}" in text
            assert f"# schema_version: {SCHEMA_VERSION}" in text

    def test_migration_receipt_present_and_traceable(self) -> None:
        """迁移回执存在, 记录旧/新摘要与校验结果。"""
        receipt_path = CATALOG_DIR / RECEIPT_FILENAME
        assert receipt_path.exists()
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["migration"] == "ies.device-data"
        assert receipt["to_schema"] == SCHEMA_VERSION
        entries = {e["file"]: e for e in receipt["entries"]}
        assert set(entries) == {"electric_load.csv", "heat_load.csv", "cooling_load.csv"}
        for entry in entries.values():
            assert entry["ok"] is True
            assert len(entry["old_sha256"]) == 64
            assert len(entry["new_sha256"]) == 64
            assert entry["row_count"] == 8760
            assert entry["blocking_diagnostics"] == []
            # 当前文件摘要 = 回执 new_sha256(内容未被改动)
            cur = hashlib.sha256((CATALOG_DIR / entry["file"]).read_bytes()).hexdigest()
            assert cur == entry["new_sha256"]

    def test_load_profile_columns_reads_migrated(self) -> None:
        """load_profile_columns 读取迁移后 CSV 正常(列数组完整)。"""
        desc = get_device_descriptor("ies.device.electric_load")
        cols = load_profile_columns(CATALOG_DIR / "electric_load.csv", desc)
        assert "e_load" in cols and cols["e_load"].shape[0] == 8760


class TestMigrationFunction:
    def test_migrate_legacy_csv_produces_receipt(self, tmp_path: Path) -> None:
        """对旧版 CSV(旧注释头)执行迁移 → 生成回执且新文件通过校验。"""
        src = CATALOG_DIR / "electric_load.csv"
        # 去掉元数据头, 恢复旧版双语注释头
        lines = src.read_text(encoding="utf-8").split("\n")
        data_start = next(
            i for i, ln in enumerate(lines) if ln.startswith("timestamp,")
        )
        legacy = [
            "# pIES 设备标准时间序列数据 / pIES device standard time series data",
            "# 设备 device: ies.device.electric_load",
            "# 分辨率 resolution: 1h",
        ]
        legacy.extend(lines[data_start:])
        tmp_csv = tmp_path / "electric_load.csv"
        tmp_csv.write_text("\n".join(legacy) + "\n", encoding="utf-8")
        # 其余两个目标用迁移后文件补齐(迁移只处理存在的目标, 全量校验)
        shutil.copy(CATALOG_DIR / "heat_load.csv", tmp_path / "heat_load.csv")
        shutil.copy(CATALOG_DIR / "cooling_load.csv", tmp_path / "cooling_load.csv")

        receipt = migrate_catalog_csvs(tmp_path)
        entry = receipt["entries"][0]
        assert entry["ok"] is True
        assert entry["file"] == "electric_load.csv"
        assert (tmp_path / RECEIPT_FILENAME).exists()
        # 迁移后新文件通过校验
        desc = get_device_descriptor("ies.device.electric_load")
        result = canonicalize_device_data(tmp_csv.read_bytes(), desc)
        assert not any(d.blocking for d in result.diagnostics)
        assert len(result.rows) == 8760

    def test_migrate_failure_no_partial_write(self, tmp_path: Path) -> None:
        """迁移校验失败 → 不写回任何文件(失败可见, 不半迁移)。

        迁移把旧版文件规范化为设备模型声明的标准形态; 失败发生在数据本身
        违反契约(如缺必需列或时间戳非法)时, 此时不得写回任何文件。
        """
        # 制造缺失必需列(表头只剩 timestamp)
        bad = [
            "# schema: ies.device-data",
            "# schema_version: 1.0.0",
            "# dataset_id: bad",
            "# device_model: ies.device.electric_load@1.2.0",
            "# series_mode: timeline",
            "# resolution: 1h",
            "# timestamp_mode: fixed_offset",
            "# fixed_utc_offset_minutes: 480",
            "timestamp",
            "2025-01-01T00:00:00",
        ]
        tmp_csv = tmp_path / "electric_load.csv"
        tmp_csv.write_text("\n".join(bad) + "\n", encoding="utf-8")
        # 其余两个目标合法, 验证任一失败整体拒绝
        shutil.copy(CATALOG_DIR / "heat_load.csv", tmp_path / "heat_load.csv")
        shutil.copy(CATALOG_DIR / "cooling_load.csv", tmp_path / "cooling_load.csv")

        with pytest.raises(ValueError):
            migrate_catalog_csvs(tmp_path)
        # 无文件被写回(三个目标都未变)
        cur = hashlib.sha256(tmp_csv.read_bytes()).hexdigest()
        assert cur == hashlib.sha256(("\n".join(bad) + "\n").encode()).hexdigest()
        heat_cur = hashlib.sha256((tmp_path / "heat_load.csv").read_bytes()).hexdigest()
        heat_src = hashlib.sha256((CATALOG_DIR / "heat_load.csv").read_bytes()).hexdigest()
        assert heat_cur == heat_src
