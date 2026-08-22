"""数据集单元(U05)测试: 模板 / 解析 / 校验 / 版本上传 / 样例数据 / API。

- 模板生成 → 构造合法 CSV(1h, 8760 行)→ 上传 → 校验通过 → 坏数据被阻断;
- 校验逻辑直接单测(行数/时间戳/缺失/范围参数化);
- 大文件规避: API 错误路径用小行数(行数不足即被阻断), 合法路径用 1h 一次;
- 全部测试使用 SQLite :memory:(StaticPool, 跨线程共享)与临时 data_dir。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
import sqlalchemy as sa
from auth_helpers import login_headers, make_user  # noqa: E402
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from iesplan.config import settings
from iesplan.core.timeaxis import build_axis
from iesplan.db import Base, get_db
from iesplan.models.dataset import DatasetFile, DatasetVersion
from iesplan.models.project import Project, ProjectMember
from iesplan.services import dataset as ds_service
from iesplan.services.dataset import (
    STANDARD_FIELDS,
    TIMESTAMP_COL,
    parse_csv,
    validate_dataset,
)

#: 标准数据列(不含 timestamp)
DATA_COLS: tuple[str, ...] = tuple(STANDARD_FIELDS.keys())


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Iterator[sa.Engine]:
    """SQLite :memory: 引擎(StaticPool: 跨线程共享同一连接)。"""
    eng = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine) -> Iterator[Session]:
    """共享会话(expire_on_commit=False: 提交后属性保持可读)。"""
    with Session(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时对象存储根目录。"""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(settings, "data_dir", d)
    return d





def _make_project(session: Session, user, name: str = "测试项目") -> Project:
    """创建测试项目(含所有者成员行, 满足项目权限判定)。"""
    proj = Project(name=name, owner_id=user.id, created_by=user.id)
    session.add(proj)
    session.flush()
    session.add(
        ProjectMember(
            project_id=proj.id, user_id=user.id, role="owner",
            auth_version=1, granted_by=user.id, granted_at=datetime.now(UTC),
        )
    )
    session.flush()
    return proj


@pytest.fixture()
def client(session: Session, data_dir: Path) -> Iterator[TestClient]:
    """FastAPI 测试客户端(挂载数据集路由, 覆盖 DB 依赖为测试会话)。"""
    from iesplan.api.datasets import router as datasets_router
    from iesplan.main import create_app

    app = create_app()
    app.include_router(datasets_router)

    def _override_get_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# CSV 构造辅助
# ---------------------------------------------------------------------------


def make_rows(
    resolution: str = "1h",
    utc_offset_minutes: int = 480,
    n: int | None = None,
    transform=None,
) -> list[dict]:
    """构造合法数据行(timestamp 为本地 naive datetime; transform(row, i) 可篡改)。

    数据年按项目本地年: 本地 2025-01-01 00:00 起点(UTC 2024-12-31 16:00)。
    """
    t0_utc = datetime(2024, 12, 31, 16, tzinfo=UTC)
    axis = build_axis(resolution, utc_offset_minutes, t0_utc=t0_utc)
    n = axis.n if n is None else n
    rows: list[dict] = []
    for i in range(n):
        ts_local = axis.timestamp(i) + timedelta(minutes=utc_offset_minutes)
        row = {
            TIMESTAMP_COL: ts_local.replace(tzinfo=None),
            "e_load": 100.0 + (i % 24) * 5.0,
            "h_load": 50.0 + (i % 12) * 3.0,
            "c_load": 30.0 + (i % 10) * 2.0,
            "t_ambient": 15.0 + 8.0 * math.sin(i / 24.0),
            "ghi": 300.0 if 6 <= (i % 24) <= 18 else 0.0,
            "electricity_price": 0.6,
            "grid_emission_factor": 0.581,
        }
        if transform is not None:
            row = transform(row, i)
        rows.append(row)
    return rows


def rows_to_csv(rows: list[dict], cols: tuple[str, ...] = DATA_COLS) -> bytes:
    """行字典 → CSV 字节(时间戳本地格式; None 输出空单元格)。"""
    lines = [",".join([TIMESTAMP_COL, *cols])]
    for row in rows:
        ts = row[TIMESTAMP_COL]
        ts_str = ts.strftime("%Y-%m-%d %H:%M") if ts is not None else ""
        values = [ts_str]
        for col in cols:
            v = row.get(col)
            if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
                values.append("")
            else:
                values.append(f"{v:g}")
        lines.append(",".join(values))
    return ("\n".join(lines) + "\n").encode("utf-8")


def make_csv(
    resolution: str = "1h",
    utc_offset_minutes: int = 480,
    n: int | None = None,
    transform=None,
) -> bytes:
    """合法 CSV 快捷构造。"""
    return rows_to_csv(make_rows(resolution, utc_offset_minutes, n, transform))


def make_df(rows: list[dict]) -> pd.DataFrame:
    """行字典 → DataFrame(timestamp 解析为 datetime)。"""
    df = pd.DataFrame(rows)
    if TIMESTAMP_COL in df.columns:
        df[TIMESTAMP_COL] = pd.to_datetime(df[TIMESTAMP_COL], errors="coerce")
    return df


# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------


def test_template_contains_bilingual_headers_and_units() -> None:
    """模板应含双语注释行、全部标准列、单位与示例, 且可被 parse_csv 解析。"""
    content = ds_service.get_template("1h")
    text = content.decode("utf-8")
    assert text[0] == "\ufeff" and text.lstrip("\ufeff").startswith("#")  # BOM + 注释行
    assert "pIES" in text
    for spec in STANDARD_FIELDS.values():
        assert spec.key in text
        assert spec.name_zh in text and spec.name_en in text
        assert spec.unit in text
        assert spec.example in text
    header_line = [line for line in text.splitlines() if not line.lstrip("\ufeff").lstrip().startswith("#")][
        0
    ]
    assert header_line == ",".join([TIMESTAMP_COL, *DATA_COLS])
    # 模板可被解析器解析(注释行被跳过)
    rows, diags = parse_csv(content, "1h")
    assert len(rows) == 3
    assert all(not d.blocking for d in diags)


def test_template_invalid_resolution_raises() -> None:
    """非法分辨率应抛 ValueError。"""
    with pytest.raises(ValueError):
        ds_service.get_template("1m")
    with pytest.raises(ValueError):
        parse_csv(b"a\n", "1m")


# ---------------------------------------------------------------------------
# parse_csv: 文件/字段/行号定位
# ---------------------------------------------------------------------------


def test_parse_csv_valid_small() -> None:
    """小文件解析: 全部标准列值正确, 无诊断。"""
    rows = make_rows(n=2)
    rows, diags = parse_csv(rows_to_csv(rows), "1h")
    assert len(rows) == 2
    assert diags == []
    assert rows[0][TIMESTAMP_COL].year == 2025
    assert rows[0]["e_load"] == 100.0


def test_parse_csv_skips_comment_and_blank_lines() -> None:
    """注释行与空行应被跳过。"""
    rows = make_rows(n=2)
    csv_bytes = ("# 注释行\n\n" + rows_to_csv(rows).decode("utf-8")).encode("utf-8")
    parsed, diags = parse_csv(csv_bytes, "1h")
    assert len(parsed) == 2
    assert diags == []


def test_parse_csv_missing_required_column() -> None:
    """缺少必需列(e_load)应报 DATA-COL-001 且阻断。"""
    rows = make_rows(n=2)
    csv_text = rows_to_csv(rows).decode("utf-8").replace(",e_load", "")
    parsed, diags = parse_csv(csv_text.encode("utf-8"), "1h")
    codes = [d.code for d in diags]
    assert "DATA-COL-001" in codes
    assert all(d.blocking for d in diags)
    assert diags[0].location.get("field") == "e_load"


def test_parse_csv_unknown_column_warns() -> None:
    """未知列应报 DATA-COL-002(warning, 不阻断), 且该列被忽略。"""
    rows = make_rows(n=1)
    csv_text = rows_to_csv(rows).decode("utf-8")
    header, body = csv_text.split("\n", 1)
    csv_text = f"{header},extra_col\n{body.rstrip()},123\n"
    parsed, diags = parse_csv(csv_text.encode("utf-8"), "1h")
    assert any(d.code == "DATA-COL-002" and not d.blocking for d in diags)
    assert parsed[0].get("extra_col") is None


def test_parse_csv_undecodable_bytes() -> None:
    """无法解码的字节应报 DATA-FILE-001。"""
    parsed, diags = parse_csv(b"\xff\xfe\x00\x80\x81\x82", "1h")
    assert parsed == []
    assert any(d.code == "DATA-FILE-001" for d in diags)
    assert diags[0].blocking


def test_parse_csv_row_width_mismatch() -> None:
    """字段数与表头不一致的行应报 DATA-FILE-002 并定位行号。"""
    rows = make_rows(n=2)
    csv_text = rows_to_csv(rows).decode("utf-8")
    first, second = csv_text.split("\n", 1)
    csv_text = first + "\n" + "2025-01-01 00:00,100\n" + second  # 少 6 列
    parsed, diags = parse_csv(csv_text.encode("utf-8"), "1h")
    assert any(d.code == "DATA-FILE-002" for d in diags)
    width_diag = next(d for d in diags if d.code == "DATA-FILE-002")
    assert width_diag.blocking
    assert width_diag.params["row_no"] == 2
    assert len(parsed) == 2  # 坏行被跳过, 原两行正常解析


def test_parse_csv_bad_timestamp() -> None:
    """非法时间戳应报 DATA-FILE-004 并定位字段/行。"""
    csv_text = rows_to_csv(make_rows(n=2)).decode("utf-8")
    csv_text = csv_text.replace("\n2025-01-01 01:00,", "\nnot-a-date,")
    parsed, diags = parse_csv(csv_text.encode("utf-8"), "1h")
    assert any(d.code == "DATA-FILE-004" for d in diags)
    ts_diag = next(d for d in diags if d.code == "DATA-FILE-004")
    assert ts_diag.location.get("field") == TIMESTAMP_COL
    assert ts_diag.location.get("row") == [3]  # 表头第 1 行, 数据行从第 2 行起
    assert parsed[1][TIMESTAMP_COL] is None


def test_parse_csv_non_numeric_value() -> None:
    """非数值单元格应报 RES-NUM-001 并定位字段/行。"""
    csv_text = rows_to_csv(make_rows(n=2)).decode("utf-8")
    csv_text = csv_text.replace(",105,", ",abc,")  # 第 2 行 e_load 值 105 → 非数值
    parsed, diags = parse_csv(csv_text.encode("utf-8"), "1h")
    assert any(d.code == "RES-NUM-001" for d in diags)
    num_diag = next(d for d in diags if d.code == "RES-NUM-001")
    assert num_diag.location.get("field") == "e_load"
    assert num_diag.location.get("row") == [3]  # 表头第 1 行, 数据行从第 2 行起
    assert parsed[1]["e_load"] is None


def test_parse_csv_empty_file() -> None:
    """无数据行应报 DATA-FILE-003。"""
    parsed, diags = parse_csv(b"timestamp,e_load,h_load\n", "1h")
    assert parsed == []
    assert any(d.code == "DATA-FILE-003" for d in diags)
    assert diags[0].blocking


# ---------------------------------------------------------------------------
# validate_dataset: 校验逻辑单测
# ---------------------------------------------------------------------------


def test_validate_row_count_mismatch() -> None:
    """行数不足(10 行)应报 DATA-TS-004 且阻断。"""
    df = make_df(make_rows(n=10))
    axis, normalized, diags = validate_dataset(df, "1h", 480)
    assert axis.n == 8760
    assert any(d.code == "DATA-TS-004" and d.blocking for d in diags)


def test_validate_duplicate_timestamps() -> None:
    """重复时间戳应报 DATA-TS-001 且阻断(并指出行号)。"""
    rows = make_rows()
    rows[1000][TIMESTAMP_COL] = rows[999][TIMESTAMP_COL]
    _, _, diags = validate_dataset(make_df(rows), "1h", 480)
    dup = [d for d in diags if d.code == "DATA-TS-001"]
    assert dup and dup[0].blocking
    assert 1000 in dup[0].location.get("row", [])


def test_validate_out_of_order() -> None:
    """乱序时间戳应报 DATA-TS-005 且阻断。"""
    rows = make_rows()
    rows[1000], rows[1001] = rows[1001], rows[1000]
    _, _, diags = validate_dataset(make_df(rows), "1h", 480)
    disorder = [d for d in diags if d.code == "DATA-TS-005"]
    assert disorder and disorder[0].blocking


def test_validate_missing_values() -> None:
    """缺失值(NaN)应报 RES-NUM-001 且阻断, 定位字段与行。"""
    rows = make_rows()
    rows[42]["e_load"] = None
    rows[43]["t_ambient"] = None
    _, _, diags = validate_dataset(make_df(rows), "1h", 480)
    missing = [d for d in diags if d.code == "RES-NUM-001"]
    assert missing
    assert all(d.blocking for d in missing)
    fields = {d.location.get("field") for d in missing}
    assert "e_load" in fields and "t_ambient" in fields


def test_validate_range_violations() -> None:
    """越界值(负荷<0 / 温度 100 / GHI 2000)应报 RES-RANGE-001 且阻断, 定位行号。"""

    def corrupt(row: dict, i: int) -> dict:
        if i == 10:
            row["e_load"] = -5.0
        if i == 20:
            row["t_ambient"] = 100.0
        if i == 30:
            row["ghi"] = 2000.0
        return row

    _, _, diags = validate_dataset(make_df(make_rows(transform=corrupt)), "1h", 480)
    ranges = [d for d in diags if d.code == "RES-RANGE-001"]
    assert len(ranges) == 3
    assert all(d.blocking for d in ranges)
    by_field = {d.location.get("field"): d.location.get("row") for d in ranges}
    assert by_field == {"e_load": [11], "t_ambient": [21], "ghi": [31]}


def test_validate_valid_full_year() -> None:
    """完整合法 8760 行: 无诊断; 时间戳归一化为 UTC(减偏移)。"""
    df = make_df(make_rows(n=8760))
    axis, normalized, diags = validate_dataset(df, "1h", 480)
    assert diags == []
    assert axis.n == 8760 and len(normalized) == 8760
    first_utc = normalized[TIMESTAMP_COL].iloc[0]
    assert first_utc.tzinfo is not None
    assert first_utc.hour == 16 and first_utc.day == 31  # 本地 2025-01-01 00:00(+8) → UTC 2024-12-31 16:00


def test_validate_invalid_utc_offset() -> None:
    """UTC 偏移越界应报 PARAM-RNG-003 且阻断。"""
    df = make_df(make_rows(n=10))
    _, _, diags = validate_dataset(df, "1h", 2000)
    assert any(d.code == "PARAM-RNG-003" and d.blocking for d in diags)


def test_validate_missing_required_column() -> None:
    """缺少 e_load 列应报 DATA-COL-001。"""
    df = make_df(make_rows(n=10)).drop(columns=["e_load"])
    _, _, diags = validate_dataset(df, "1h", 480)
    assert any(d.code == "DATA-COL-001" and d.blocking for d in diags)


def test_validate_15min_resolution() -> None:
    """15min 分辨率: 行数期望 35040。"""
    df = make_df(make_rows(n=100))
    axis, _, diags = validate_dataset(df, "15min", 480)
    assert axis.n == 35040
    assert any(d.code == "DATA-TS-004" for d in diags)


# ---------------------------------------------------------------------------
# 对象存储
# ---------------------------------------------------------------------------


def test_put_object_dedup_and_ref_count(session: Session, data_dir: Path) -> None:
    """相同内容只存一份; 引用计数递增; 文件按 sha256 落盘。"""
    payload = b"hello,dataset,1\n1,2,3\n"
    obj1 = ds_service.put_object(session, payload, "text/csv")
    obj2 = ds_service.put_object(session, payload, "text/csv")
    assert obj1.id == obj2.id
    obj_path = data_dir / "objects" / obj1.sha256
    assert obj_path.read_bytes() == payload
    ref = ds_service.add_object_ref(session, obj1, "dataset_file", "dataset_files", 1, purpose="测试")
    session.commit()
    assert ref.object_id == obj1.id
    # STO-05: 句柄为不可变快照, ref_count 经 object_info 查新视图
    from iesplan.storage import object_info

    assert object_info(session, obj1.id)["ref_count"] == 1
    # 内容不同 → 不同对象
    obj3 = ds_service.put_object(session, b"other", "text/csv")
    assert obj3.id != obj1.id
    assert obj3.sha256 == hashlib.sha256(b"other").hexdigest()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def _create_dataset_via_api(
    client: TestClient, session: Session, project_id: int, user, name: str = "负荷数据"
) -> int:
    """通过 API 创建数据集, 返回 dataset_id。"""
    resp = client.post(
        f"/api/projects/{project_id}/datasets",
        json={
            "name": name,
            "source_category": "user_upload",
            "license": "CC-BY-4.0",
            "provenance": {"origin": "测试"},
        },
        headers=login_headers(client, user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["dataset"]["id"]


def test_api_template_download(client: TestClient) -> None:
    """模板端点应返回 CSV 附件。"""
    resp = client.get("/api/datasets/template", params={"resolution": "1h"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert "e_load" in resp.text
    resp = client.get("/api/datasets/template", params={"resolution": "bad"})
    assert resp.status_code == 400


def test_api_create_dataset_and_conflict(client: TestClient, session: Session) -> None:
    """创建数据集成功; 同名(同项目)返回 409。"""
    user = make_user(session, "alice")
    proj = _make_project(session, user)
    session.commit()
    ds_id = _create_dataset_via_api(client, session, proj.id, user)
    resp = client.post(
        f"/api/projects/{proj.id}/datasets",
        json={"name": "负荷数据"},
        headers=login_headers(client, user),
    )
    assert resp.status_code == 409
    assert ds_id > 0
    # 不存在项目 → 404
    resp = client.post(
        "/api/projects/999999/datasets", json={"name": "x"},
        headers=login_headers(client, user),
    )
    assert resp.status_code == 404


def test_api_upload_valid_version(client: TestClient, session: Session, data_dir: Path) -> None:
    """合法 1h 全年级上传: 201, 版本/质量报告/对象/引用齐全。"""
    user = make_user(session, "alice")
    proj = _make_project(session, user)
    session.commit()
    ds_id = _create_dataset_via_api(client, session, proj.id, user)
    csv_bytes = make_csv("1h", n=8760)
    resp = client.post(
        f"/api/projects/{proj.id}/datasets/{ds_id}/versions",
        data={"resolution": "1h", "utc_offset_minutes": "480"},
        files={"file": ("data.csv", csv_bytes, "text/csv")},
        headers=login_headers(client, user),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    version = body["dataset_version"]
    assert version["version_no"] == 1
    assert version["resolution"] == "1h"
    assert version["timeline"] == "hourly"
    assert version["fixed_utc_offset_minutes"] == 480
    assert len(version["content_hash"]) == 64
    assert version["license"] == "CC-BY-4.0"  # 继承数据集默认许可证
    report = body["quality_report"]
    assert report["row_count"] == 8760
    assert report["checks"]["row_count"]["ok"] is True
    assert report["checks"]["missing_values"]["total"] == 0
    assert report["diagnostics"] == []
    # 对象落盘 + 数据文件引用(落盘内容哈希 = 版本 content_hash; 跳过 tmp 临时目录)
    objects = [p for p in (data_dir / "objects").iterdir() if p.is_file()]
    assert len(objects) >= 1
    assert any(hashlib.sha256(obj.read_bytes()).hexdigest() == version["content_hash"] for obj in objects)
    versions = session.query(DatasetVersion).all()
    assert len(versions) == 1
    assert versions[0].content_hash == version["content_hash"]
    assert session.query(DatasetFile).filter_by(dataset_version_id=versions[0].id).count() == 2


def test_api_upload_too_few_rows_blocked(client: TestClient, session: Session) -> None:
    """行数不足(10 行)上传应被阻断: 400 + DATA-TS-004 诊断。"""
    user = make_user(session, "alice")
    proj = _make_project(session, user)
    session.commit()
    ds_id = _create_dataset_via_api(client, session, proj.id, user)
    resp = client.post(
        f"/api/projects/{proj.id}/datasets/{ds_id}/versions",
        data={"resolution": "1h"},
        files={"file": ("small.csv", make_csv(n=10), "text/csv")},
        headers=login_headers(client, user),
    )
    assert resp.status_code == 400
    codes = [d["code"] for d in resp.json()["diagnostics"]]
    assert "DATA-TS-004" in codes
    # 未创建任何版本
    assert session.query(DatasetVersion).count() == 0


def test_api_upload_duplicate_ts_blocked(client: TestClient, session: Session) -> None:
    """重复时间戳上传应被阻断: 400 + DATA-TS-001。"""
    user = make_user(session, "alice")
    proj = _make_project(session, user)
    session.commit()
    ds_id = _create_dataset_via_api(client, session, proj.id, user)

    def dup(row: dict, i: int) -> dict:
        return row

    rows = make_rows(transform=dup)
    rows[1000][TIMESTAMP_COL] = rows[999][TIMESTAMP_COL]
    resp = client.post(
        f"/api/projects/{proj.id}/datasets/{ds_id}/versions",
        data={"resolution": "1h"},
        files={"file": ("dup.csv", rows_to_csv(rows), "text/csv")},
        headers=login_headers(client, user),
    )
    assert resp.status_code == 400
    codes = [d["code"] for d in resp.json()["diagnostics"]]
    assert "DATA-TS-001" in codes


def test_api_upload_range_blocked(client: TestClient, session: Session) -> None:
    """越界值上传应被阻断: 400 + RES-RANGE-001(带字段定位)。"""
    user = make_user(session, "alice")
    proj = _make_project(session, user)
    session.commit()
    ds_id = _create_dataset_via_api(client, session, proj.id, user)

    def corrupt(row: dict, i: int) -> dict:
        if i == 5:
            row["t_ambient"] = 99.0
        return row

    resp = client.post(
        f"/api/projects/{proj.id}/datasets/{ds_id}/versions",
        data={"resolution": "1h"},
        files={"file": ("range.csv", make_csv(transform=corrupt), "text/csv")},
        headers=login_headers(client, user),
    )
    assert resp.status_code == 400
    diagnostics = resp.json()["diagnostics"]
    assert any(d["code"] == "RES-RANGE-001" and d["location"]["field"] == "t_ambient" for d in diagnostics)


def test_api_list_and_detail(client: TestClient, session: Session) -> None:
    """列表返回最新版本; 详情返回版本列表+质量报告。"""
    user = make_user(session, "alice")
    proj = _make_project(session, user)
    session.commit()
    ds_id = _create_dataset_via_api(client, session, proj.id, user, name="配电负荷")
    csv_bytes = make_csv(n=8760)
    client.post(
        f"/api/projects/{proj.id}/datasets/{ds_id}/versions",
        data={"resolution": "1h"},
        files={"file": ("data.csv", csv_bytes, "text/csv")},
        headers=login_headers(client, user),
    )
    # 列表
    resp = client.get(f"/api/projects/{proj.id}/datasets", headers=login_headers(client, user))
    assert resp.status_code == 200
    items = resp.json()["datasets"]
    assert len(items) == 1
    assert items[0]["dataset"]["name"] == "配电负荷"
    latest = items[0]["latest_version"]
    assert latest is not None and latest["version_no"] == 1
    # 详情
    resp = client.get(f"/api/projects/{proj.id}/datasets/{ds_id}", headers=login_headers(client, user))
    assert resp.status_code == 200
    versions = resp.json()["versions"]
    assert len(versions) == 1
    assert versions[0]["quality_report"]["row_count"] == 8760
    assert {f["file_kind"] for f in versions[0]["files"]} == {"data", "metadata"}


def test_api_version_metadata_no_data_content(client: TestClient, session: Session) -> None:
    """版本详情只返回元数据+溯源+文件引用, 不含数据行。"""
    user = make_user(session, "alice")
    proj = _make_project(session, user)
    session.commit()
    ds_id = _create_dataset_via_api(client, session, proj.id, user)
    client.post(
        f"/api/projects/{proj.id}/datasets/{ds_id}/versions",
        data={"resolution": "1h"},
        files={"file": ("data.csv", make_csv(n=8760), "text/csv")},
        headers=login_headers(client, user),
    )
    resp = client.get(
        f"/api/projects/{proj.id}/datasets/{ds_id}/versions/1", headers=login_headers(client, user)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["dataset_version"]["version_no"] == 1
    assert body["provenance"]["source_category"] == "user_upload"
    assert body["license"] == "CC-BY-4.0"
    data_files = [f for f in body["files"] if f["file_kind"] == "data"]
    assert len(data_files) == 1
    assert len(data_files[0]["sha256"]) == 64
    assert data_files[0]["row_count"] == 8760
    assert "rows" not in body and "content" not in body
    # 不存在的版本 → 404
    resp = client.get(
        f"/api/projects/{proj.id}/datasets/{ds_id}/versions/99", headers=login_headers(client, user)
    )
    assert resp.status_code == 404


def test_api_unknown_dataset_404(client: TestClient, session: Session) -> None:
    """项目不存在或数据集不属于项目应返回 404。"""
    user = make_user(session, "alice")
    proj = _make_project(session, user)
    session.commit()
    resp = client.get(f"/api/projects/{proj.id}/datasets/424242", headers=login_headers(client, user))
    assert resp.status_code == 404
    resp = client.get("/api/projects/424242/datasets", headers=login_headers(client, user))
    assert resp.status_code == 404


def test_api_sample_generation(client: TestClient, session: Session, data_dir: Path) -> None:
    """内置样例: 201 + 溯源(source_category=builtin_sample) + 质量报告; 重复生成内容哈希一致。"""
    user = make_user(session, "alice")
    proj = _make_project(session, user)
    session.commit()
    ds_id = _create_dataset_via_api(client, session, proj.id, user, name="样例容器")
    resp = client.post(
        f"/api/projects/{proj.id}/datasets/{ds_id}/sample",
        params={"resolution": "1h", "region": "shanghai"},
        headers=login_headers(client, user),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    version = body["dataset_version"]
    assert version["version_no"] == 1
    assert version["provenance"]["source_category"] == "builtin_sample"
    assert version["provenance"]["region"] == "shanghai"
    assert version["provenance"]["seed"] is not None
    assert version["license"] == "CC-BY-4.0"
    report = body["quality_report"]
    assert report["row_count"] == 8760
    assert report["checks"]["missing_values"]["total"] == 0
    assert report["checks"]["ranges"]["total"] == 0
    # 再次生成: 确定性 → 相同内容哈希, 版本号递增
    resp2 = client.post(
        f"/api/projects/{proj.id}/datasets/{ds_id}/sample",
        params={"resolution": "1h", "region": "shanghai"},
        headers=login_headers(client, user),
    )
    assert resp2.status_code == 201
    assert resp2.json()["dataset_version"]["version_no"] == 2
    assert resp2.json()["dataset_version"]["content_hash"] == version["content_hash"]


def test_sample_service_deterministic_across_calls(session: Session, data_dir: Path) -> None:
    """服务层直接调用: 两次生成的行内容完全一致(含对象落盘去重)。"""
    user = make_user(session, "alice")
    proj = _make_project(session, user)
    session.commit()
    v1 = ds_service.create_builtin_sample(session, proj.id, "1h", region="beijing")
    v2 = ds_service.create_builtin_sample(session, proj.id, "1h", region="beijing")
    assert v1.content_hash == v2.content_hash
    assert v1.dataset_id == v2.dataset_id
    # 数据对象按内容去重复用(元数据对象含版本号/时间戳, 不参与去重)
    obj_ids_v1 = {f.object_id for f in session.query(DatasetFile).filter_by(dataset_version_id=v1.id)}
    obj_ids_v2 = {f.object_id for f in session.query(DatasetFile).filter_by(dataset_version_id=v2.id)}
    assert len(obj_ids_v1 & obj_ids_v2) == 1


def test_upload_with_fields_declaration(client: TestClient, session: Session) -> None:
    """上传时声明字段单位: 合法单位通过; 单位不匹配被阻断(PARAM-UNIT-002)。"""
    user = make_user(session, "alice")
    proj = _make_project(session, user)
    session.commit()
    ds_id = _create_dataset_via_api(client, session, proj.id, user)
    fields_json = '{"e_load": {"unit": "kWh"}, "t_ambient": {"unit": "C"}}'
    resp = client.post(
        f"/api/projects/{proj.id}/datasets/{ds_id}/versions",
        data={"resolution": "1h", "fields": fields_json},
        files={"file": ("data.csv", make_csv(n=8760), "text/csv")},
        headers=login_headers(client, user),
    )
    assert resp.status_code == 400, resp.text
    codes = [d["code"] for d in resp.json()["diagnostics"]]
    assert "PARAM-UNIT-002" in codes


def test_anonymous_and_xuserid_dataset_401(client: TestClient, session: Session) -> None:
    """数据集端点匿名访问 → 401; 伪造 X-User-Id 不再被接受(模板下载保持公开)。"""
    resp = client.get("/api/projects/1/datasets")
    assert resp.status_code == 401
    resp = client.post("/api/projects/1/datasets", json={"name": "匿名数据集"})
    assert resp.status_code == 401
    resp = client.get("/api/projects/1/datasets", headers={"X-User-Id": "1"})
    assert resp.status_code == 401
    # 模板下载(公开)不受影响
    resp = client.get("/api/datasets/template", params={"resolution": "1h"})
    assert resp.status_code == 200
