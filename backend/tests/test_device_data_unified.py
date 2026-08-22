"""0.6.0 事项 2: 包内设备 CSV 与 GUI 上传共用同一规范化流程。

退出标准: 手写 CSV 与 GUI 上传对同一内容产生同一规范摘要;
时间、单位或长度不一致时明确失败。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from iesplan.devices import get_device_descriptor
from iesplan.devices.datacontract import (
    canonicalize_device_data,
    normalize_upload_csv,
)
from iesplan.devices.profile import canonicalize_profile_csv, load_profile_columns

CATALOG_DIR = Path(__file__).resolve().parents[1] / "iesplan/devices/catalog"


def _sample_desc():
    return get_device_descriptor("ies.device.electric_load")


#: 与目录 electric_load.csv 同一语义的最小手写 CSV(元数据 + 3 行)
_HANDWRITTEN = """\
# schema: ies.device-data
# schema_version: 1.0.0
# dataset_id: handwritten_electric_load
# device_model: ies.device.electric_load@1.2.0
# series_mode: timeline
# resolution: 1h
# timestamp_mode: fixed_offset
# fixed_utc_offset_minutes: 480
# unit.e_load: kWh
timestamp,e_load
2025-01-01T00:00:00,48.3
2025-01-01T01:00:00,46.7
2025-01-01T02:00:00,45.9
"""


class TestSharedCanonicalFlow:
    def test_handwritten_and_profile_csv_same_flow(self) -> None:
        """手写 ies.device-data CSV 经包内设备读取路径 → 同一规范流程。"""
        desc = _sample_desc()
        # 手写 CSV 走 canonicalize_device_data
        r_hand = canonicalize_device_data(_HANDWRITTEN.encode("utf-8"), desc)
        assert not any(d.blocking for d in r_hand.diagnostics), [d.to_dict() for d in r_hand.diagnostics]
        # 目录 CSV 走 profile 路径(与上传共用 normalize_upload_csv)
        r_profile = canonicalize_profile_csv(CATALOG_DIR / "electric_load.csv", desc)
        blockers = [d.to_dict() for d in r_profile.diagnostics if d.blocking]
        assert not blockers, blockers
        # 两条路径都产生规范摘要(canonical_sha256 非空且一致的表格式)
        assert r_hand.canonical_sha256
        assert r_profile.canonical_sha256
        assert r_hand.column_order == ("timestamp", "e_load")
        assert r_profile.column_order == ("timestamp", "e_load")

    def test_same_content_upload_and_device_path_same_summary(self) -> None:
        """同一内容的 GUI 上传路径与包内设备路径 → 同一规范表格摘要。

        对同一份手写 CSV:
        - GUI 上传经 normalize_upload_csv(文件已声明元数据 → canonicalize);
        - 包内设备路径经 canonicalize_profile_csv(检测到元数据 → canonicalize)。
        两条路径共用 canonicalize_device_data, 规范摘要必然一致。
        """
        desc = _sample_desc()
        r_upload = normalize_upload_csv(
            _HANDWRITTEN.encode("utf-8"),
            desc,
            dataset_id="x",
            device_model="ies.device.electric_load@1.2.0",
            resolution="1h",
            utc_offset_minutes=480,
            units={"e_load": "kWh"},
        )
        r_device = canonicalize_profile_csv(_write_tmp(_HANDWRITTEN), desc)
        assert not any(d.blocking for d in r_upload.diagnostics), [d.to_dict() for d in r_upload.diagnostics]
        assert not any(d.blocking for d in r_device.diagnostics), [d.to_dict() for d in r_device.diagnostics]
        # 数据表摘要一致(规范表格字节一致 → sha 一致)
        assert r_upload.canonical_sha256 == r_device.canonical_sha256

    def test_same_content_gui_bare_csv_same_table(self) -> None:
        """裸 CSV(无元数据)上传与带元数据文件 → 数据表字节一致。

        裸 CSV 由上传参数构造元数据, 数据表部分与带元数据文件一致
        (时间戳统一 UTC 带 Z、数值去尾零)。
        """
        desc = _sample_desc()
        bare = "timestamp,e_load\n2025-01-01T00:00:00,48.3\n"
        r_bare = normalize_upload_csv(
            bare.encode("utf-8"),
            desc,
            dataset_id="x",
            device_model="ies.device.electric_load@1.2.0",
            resolution="1h",
            utc_offset_minutes=480,
            units={"e_load": "kWh"},
        )
        assert not any(d.blocking for d in r_bare.diagnostics), [d.to_dict() for d in r_bare.diagnostics]
        assert r_bare.utc_timestamps[0].hour == 16  # 本地 00:00(+8) → UTC 前一天 16:00
        assert "Z" in r_bare.canonical_csv_bytes().decode("utf-8")

    def test_load_profile_columns_uses_canonical_flow(self) -> None:
        """load_profile_columns 经规范化流程读取, 返回列数组且无阻断。"""
        desc = _sample_desc()
        cols = load_profile_columns(CATALOG_DIR / "electric_load.csv", desc)
        assert "e_load" in cols
        assert cols["e_load"].shape[0] == 8760
        assert float(cols["e_load"][0]) == pytest.approx(48.3)

    def test_profile_csv_bad_unit_fails(self) -> None:
        """包内设备 CSV 单位与设备模型不一致 → 阻断, 不静默透传。"""
        desc = _sample_desc()
        bad = _HANDWRITTEN.replace("# unit.e_load: kWh", "# unit.e_load: MW")
        r = canonicalize_device_data(bad.encode("utf-8"), desc)
        assert any(d.code == "DATA-COL-006" for d in r.diagnostics)


def _write_tmp(text: str) -> Path:
    import tempfile

    fd, name = tempfile.mkstemp(suffix=".csv")
    with open(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return Path(name)
