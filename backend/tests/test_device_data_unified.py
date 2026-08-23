"""0.6.0 事项 2: 包内设备 CSV 与 GUI 上传共用同一规范化流程。

退出标准: 手写 CSV 与 GUI 上传对同一内容产生同一规范摘要;
时间、单位或长度不一致时明确失败。
"""

from __future__ import annotations

import hashlib
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



class TestGuiUploadParity:
    """GUI 上传路径(upload_dataset_version)与手写 CSV 同一规范化流程(0.6.0 退出标准)。"""

    @pytest.fixture()
    def upload_env(self, monkeypatch, tmp_path: Path):
        """SQLite 会话 + 临时对象存储 + 项目/数据集。"""
        from datetime import UTC, datetime

        import sqlalchemy as sa
        from sqlalchemy.orm import Session

        from iesplan.config import settings
        from iesplan.db import Base
        from iesplan.models.dataset import Dataset
        from iesplan.models.identity import User
        from iesplan.models.project import Project, ProjectMember

        monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
        data_root = Path(settings.data_dir)
        data_root.mkdir()  # 存储门禁 STO-06 需可测磁盘容量
        eng = sa.create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=sa.pool.StaticPool,
        )
        Base.metadata.create_all(eng)
        with Session(eng, expire_on_commit=False) as session:
            user = User(username="uploader", display_name="上传者")
            session.add(user)
            session.flush()
            proj = Project(name="上传项目", owner_id=user.id, created_by=user.id)
            session.add(proj)
            session.flush()
            session.add(
                ProjectMember(
                    project_id=proj.id, user_id=user.id, role="owner",
                    auth_version=1, granted_by=user.id, granted_at=datetime.now(UTC),
                )
            )
            ds = Dataset(project_id=proj.id, name="设备数据", status="draft", created_by=user.id)
            session.add(ds)
            session.commit()
            yield session, proj, ds
        eng.dispose()

    def test_gui_upload_same_content_same_summary_as_handwritten(self, upload_env) -> None:
        """同一内容(全年): GUI 上传落盘字节与手写 CSV canonicalize 摘要一致。"""
        from sqlalchemy import select

        from iesplan.models.dataset import DatasetFile
        from iesplan.services.dataset import upload_dataset_version

        session, _proj, ds = upload_env
        desc = _sample_desc()
        handwritten = handwritten_template("ies.device.electric_load@1.2.0", n_rows=8760)
        r_hand = canonicalize_device_data(handwritten.encode("utf-8"), desc)
        assert not any(d.blocking for d in r_hand.diagnostics), [d.to_dict() for d in r_hand.diagnostics]

        # fields 留空: 文件已声明元数据 → 文件为权威来源
        version = upload_dataset_version(
            session, ds.id, "1h", 480,
            {},
            handwritten.encode("utf-8"),
            {"source_category": "user_upload"},
        )
        # 版本 content_hash == 手写 CSV 规范摘要(同一规范化器产物)
        assert version.content_hash == r_hand.canonical_sha256
        report = version.quality_report
        assert report["has_blocking_errors"] is False
        assert report["canonical_sha256"] == r_hand.canonical_sha256
        # 落盘对象字节即规范字节(经公开存储门面校验 sha256)
        data_file = session.execute(
            select(DatasetFile).where(
                DatasetFile.dataset_version_id == version.id, DatasetFile.file_kind == "data"
            )
        ).scalars().one()
        from iesplan.storage import get_object

        stored = get_object(session, data_file.object_id)
        assert stored == r_hand.canonical_csv_bytes()
        assert hashlib.sha256(stored).hexdigest() == version.content_hash

    def test_gui_bare_csv_upload_normalized(self, upload_env) -> None:
        """裸 CSV 上传经同一规范化器: 合法通过且落盘 UTC 形态; 行数不符阻断。"""
        from iesplan.services.dataset import DataValidationError, upload_dataset_version

        session, _proj, ds = upload_env
        from datetime import UTC, datetime, timedelta

        lines = [
            "timestamp,e_load,h_load,c_load,t_ambient,ghi,electricity_price,"
            "grid_emission_factor"
        ]
        t0 = datetime(2025, 1, 1, tzinfo=UTC)
        for i in range(8760):
            ts = (t0 + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%S")
            lines.append(f"{ts},{100.0 + (i % 24)},50,30,15.5,300.0,0.6,0.581")
        good = ("\n".join(lines) + "\n").encode("utf-8")
        version = upload_dataset_version(
            session, ds.id, "1h", 480,
            {},
            good,
            {"source_category": "user_upload", "created_reason": "upload"},
        )
        report = version.quality_report
        assert report["row_count"] == 8760
        assert len(version.content_hash) == 64
        assert report["has_blocking_errors"] is False

        with pytest.raises(DataValidationError) as exc_info:
            short = "\n".join(lines[:3]) + "\n"
            upload_dataset_version(session, ds.id, "1h", 480, {}, short.encode("utf-8"), {})
        codes = [d.code for d in exc_info.value.diagnostics]
        assert "DATA-TS-004" in codes  # 行数期望校验保留

    def test_gui_upload_bad_unit_blocked(self, upload_env) -> None:
        """裸 CSV 声明不兼容单位 → DATA-COL-006 阻断(不再静默透传)。"""
        from iesplan.services.dataset import DataValidationError, upload_dataset_version

        session, _proj, ds = upload_env
        csv_text = "timestamp,e_load\n" + "".join(
            f"2025-01-01 {i:02d}:00,{100.0 + i}\n" for i in range(24)
        )
        with pytest.raises(DataValidationError) as exc_info:
            upload_dataset_version(
                session, ds.id, "1h", 480,
                {"e_load": {"unit": "MW"}},  # 与权威单位 kWh 量纲不兼容
                csv_text.encode("utf-8"), {},
            )
        codes = [d.code for d in exc_info.value.diagnostics]
        assert "DATA-COL-006" in codes

    def test_gui_upload_declared_file_unknown_device_rejected(self, upload_env) -> None:
        """声明未注册 device_model 的文件 → 明确失败, 不猜测。"""
        from iesplan.services.dataset import upload_dataset_version

        session, _proj, ds = upload_env
        bad = handwritten_template("ies.device.nonexistent@1.0.0")
        with pytest.raises(LookupError):
            upload_dataset_version(session, ds.id, "1h", 480, {}, bad.encode("utf-8"), {})

    def test_gui_upload_inline_offset_in_fixed_offset_file_blocked(self, upload_env) -> None:
        """上传文件声明 fixed_offset 但行内带偏移 → DATA-TIME-006 阻断(经 GUI 路径)。"""
        from iesplan.services.dataset import DataValidationError, upload_dataset_version

        session, _proj, ds = upload_env
        text = handwritten_template("ies.device.electric_load@1.2.0").replace(
            "2025-01-01T00:00:00,", "2025-01-01T09:00:00+09:00,"
        )
        with pytest.raises(DataValidationError) as exc_info:
            upload_dataset_version(session, ds.id, "1h", 480, {}, text.encode("utf-8"), {})
        codes = [d.code for d in exc_info.value.diagnostics]
        assert "DATA-TIME-006" in codes

    def test_gui_upload_metadata_file_authoritative_for_axis(self, upload_env) -> None:
        """P1: 文件已声明元数据时, 行数期望与存储时间轴以文件为权威。

        30min 文件(全年 17520 行, fixed_offset +540)配 resolution=1h/480 请求:
        - 不再按请求分辨率(1h→8760)误判行数不匹配, 上传成功;
        - 版本 resolution/fixed_utc_offset_minutes 按文件声明持久化
          (30min/540), 而非请求参数(1h/480)。
        """
        from iesplan.services.dataset import upload_dataset_version

        session, _proj, ds = upload_env
        csv_text = handwritten_template(
            "ies.device.electric_load@1.2.0",
            n_rows=17520,
            resolution="30min",
            fixed_utc_offset_minutes=540,
            step_minutes=30,
        )
        version = upload_dataset_version(
            session, ds.id, "1h", 480,
            {},
            csv_text.encode("utf-8"),
            {"source_category": "user_upload"},
        )
        assert version.resolution == "30min"
        assert version.fixed_utc_offset_minutes == 540
        assert version.quality_report["row_count"] == 17520
        assert version.quality_report["has_blocking_errors"] is False

    def test_gui_upload_metadata_file_row_count_uses_file_resolution(self, upload_env) -> None:
        """P1: 元数据文件行数期望按文件声明分辨率推导(30min → 17520), 不符阻断。

        声明 30min 却只给 8760 行(1h 全年行数): 期望 17520 → DATA-TS-004,
        不能被当作 1h 文件静默接受。
        """
        from iesplan.services.dataset import DataValidationError, upload_dataset_version

        session, _proj, ds = upload_env
        csv_text = handwritten_template(
            "ies.device.electric_load@1.2.0",
            n_rows=8760,
            resolution="30min",
            fixed_utc_offset_minutes=540,
            step_minutes=30,
        )
        with pytest.raises(DataValidationError) as exc_info:
            upload_dataset_version(session, ds.id, "1h", 480, {}, csv_text.encode("utf-8"), {})
        codes = [d.code for d in exc_info.value.diagnostics]
        assert "DATA-TS-004" in codes


def handwritten_template(
    device_model: str,
    n_rows: int = 3,
    *,
    resolution: str = "1h",
    fixed_utc_offset_minutes: int = 480,
    step_minutes: int = 60,
) -> str:
    """构造声明元数据的最小设备数据文件(与目录 electric_load.csv 同构)。

    n_rows: 数据行数(数据集版本上传要求全年行数, 对等测试传 8760)。
    时间戳为固定偏移的本地时间, 按 step_minutes 递增。
    """
    from datetime import datetime, timedelta

    lines = [
        "# schema: ies.device-data",
        "# schema_version: 1.0.0",
        "# dataset_id: gui_parity_check",
        f"# device_model: {device_model}",
        "# series_mode: timeline",
        f"# resolution: {resolution}",
        "# timestamp_mode: fixed_offset",
        f"# fixed_utc_offset_minutes: {fixed_utc_offset_minutes}",
        "# unit.e_load: kWh",
        "timestamp,e_load",
    ]
    t0 = datetime(2025, 1, 1)
    for i in range(n_rows):
        ts = (t0 + timedelta(minutes=i * step_minutes)).strftime("%Y-%m-%dT%H:%M:%S")
        lines.append(f"{ts},{100.0 + (i % 24) * 0.5}")
    return "\n".join(lines) + "\n"


class TestOptionalColumnMissingValues:
    """finding P2: 模型允许缺失的列保留缺失, 不零填充。"""

    @staticmethod
    def _optional_desc():
        """e_load 必需 + h_load 可选的最小公开描述(与 DeviceModelDescriptor 同构)。"""
        from dataclasses import dataclass, field

        from iesplan.devices.datacontract import DataInputDecl

        @dataclass(frozen=True)
        class _Desc:
            type_id: str = "ies.device.test"
            version: str = "1.0.0"
            data_inputs: dict = field(default_factory=dict)

        return _Desc(
            data_inputs={
                "e_load": DataInputDecl(column_id="e_load", unit="kWh", required=True),
                "h_load": DataInputDecl(column_id="h_load", unit="kWh", required=False),
            }
        )

    def test_allowed_missing_values_not_zero_filled(self, tmp_path: Path) -> None:
        """可选列空白 → 数组保留 NaN; 不得静默补零。"""
        import math

        desc = self._optional_desc()
        text = (
            "# schema: ies.device-data\n"
            "# schema_version: 1.0.0\n"
            "# dataset_id: optional_missing\n"
            "# device_model: ies.device.test@1.0.0\n"
            "# series_mode: timeline\n"
            "# resolution: 1h\n"
            "# timestamp_mode: fixed_offset\n"
            "# fixed_utc_offset_minutes: 480\n"
            "# unit.e_load: kWh\n"
            "# unit.h_load: kWh\n"
            "timestamp,e_load,h_load\n"
            "2025-01-01T00:00:00,48.3,\n"
            "2025-01-01T01:00:00,46.7,\n"
        )
        csv_path = tmp_path / "optional.csv"
        csv_path.write_text(text, encoding="utf-8")
        cols = load_profile_columns(csv_path, desc)
        assert list(cols["e_load"]) == [48.3, 46.7]
        # 可选列缺失保留 NaN, 不被填成 0
        assert all(math.isnan(v) for v in cols["h_load"])

    def test_required_column_missing_value_blocked_not_zero_filled(self, tmp_path: Path) -> None:
        """必需列缺值 → 阻断诊断(DATA-VAL-002), 不产生补零数组。"""
        from iesplan.core.errors import AppError

        desc = self._optional_desc()
        text = (
            "# schema: ies.device-data\n"
            "# schema_version: 1.0.0\n"
            "# dataset_id: required_missing\n"
            "# device_model: ies.device.test@1.0.0\n"
            "# series_mode: timeline\n"
            "# resolution: 1h\n"
            "# timestamp_mode: fixed_offset\n"
            "# fixed_utc_offset_minutes: 480\n"
            "# unit.e_load: kWh\n"
            "# unit.h_load: kWh\n"
            "timestamp,e_load,h_load\n"
            "2025-01-01T00:00:00,,30.0\n"
        )
        csv_path = tmp_path / "required_missing.csv"
        csv_path.write_text(text, encoding="utf-8")
        with pytest.raises(AppError) as exc_info:
            load_profile_columns(csv_path, desc)
        assert any(d["code"] == "DATA-VAL-002" for d in exc_info.value.params["diagnostics"])


def _write_tmp(text: str) -> Path:
    import tempfile

    fd, name = tempfile.mkstemp(suffix=".csv")
    with open(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return Path(name)
