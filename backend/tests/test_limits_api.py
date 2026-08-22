"""资源使用边界(0.2.0 A4)测试: 全局限流 + 上传配额 + dataset meta 白名单。

覆盖(严格对应 A4 验收):
- 限流触发: 阈值内放行, 超阈值 429(标准错误信封 API-RL-001);
- 豁免路径(健康/就绪/登录)不参与限流;
- 配额超限: 用户/项目上传配额超限 413(API-QUOTA-001);
- meta 白名单: 未知键/畸形 provenance 拒绝 400(API-META-001);
- 合法上传不受影响(白名单放行合法 meta/fields)。
"""
from __future__ import annotations

import math
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from iesplan.config import settings
from iesplan.db import Base, get_db
from iesplan.main import create_app

# ---------------------------------------------------------------------------
# 测试环境(SQLite :memory: + 临时 data_dir)
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Iterator[object]:
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture(autouse=True)
def _clean_tables(engine) -> Iterator[None]:
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def client(db: Session, tmp_path, monkeypatch) -> Iterator[TestClient]:
    """完整应用客户端(含限流中间件), 替换 get_db 依赖, data_dir 指向临时目录。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    app = create_app()

    def _override_get_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _make_user(db: Session, username: str) -> object:
    """创建带密码凭证、首登不强制改密的测试用户(可真实登录)。"""
    from auth_helpers import make_user

    return make_user(db, username)


def _login(client: TestClient, user) -> dict[str, str]:
    from auth_helpers import login_headers

    return login_headers(client, user)


def _create_project(client: TestClient, user, name: str = "限流项目") -> int:
    """通过 API 创建项目, 返回 project_id。"""
    resp = client.post(
        "/api/projects",
        json={"name": name, "currency": "CNY", "utc_offset_minutes": 480},
        headers=_login(client, user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["project"]["id"]


def _make_csv(n: int = 8760) -> bytes:
    """合法 1h 数据集 CSV(时间戳本地格式, 标准列)。"""
    from datetime import UTC, datetime, timedelta

    from iesplan.core.timeaxis import build_axis

    t0_utc = datetime(2024, 12, 31, 16, tzinfo=UTC)
    axis = build_axis("1h", 480, t0_utc=t0_utc)
    n = axis.n if n is None else n
    lines = [
        "timestamp,e_load,h_load,c_load,t_ambient,ghi,electricity_price,grid_emission_factor"
    ]
    for i in range(n):
        ts_local = axis.timestamp(i) + timedelta(minutes=480)
        ts_str = ts_local.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M")
        lines.append(
            f"{ts_str},{100.0 + (i % 24) * 5.0},{50.0 + (i % 12) * 3.0},"
            f"{30.0 + (i % 10) * 2.0},{15.0 + 8.0 * math.sin(i / 24.0)},"
            f"{300.0 if 6 <= (i % 24) <= 18 else 0.0},{0.6},{0.581}"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _create_dataset(client: TestClient, user, project_id: int, name: str = "负荷") -> int:
    resp = client.post(
        f"/api/projects/{project_id}/datasets",
        json={"name": name, "source_category": "user_upload", "license": "CC-BY-4.0"},
        headers=_login(client, user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["dataset"]["id"]


# ---------------------------------------------------------------------------
# 全局限流
# ---------------------------------------------------------------------------


def test_rate_limit_allows_below_threshold(client: TestClient, db: Session, monkeypatch) -> None:
    """阈值内请求正常放行(200/401 等业务状态, 不被限流误伤)。"""
    monkeypatch.setattr(settings, "rate_limit_max_requests", 100)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 60)
    from iesplan.api.limits import reset_rate_limit

    reset_rate_limit()
    # 多次请求未超阈值: 匿名访问业务端点返回 401(认证错误), 不是 429
    for _ in range(5):
        resp = client.get("/api/projects/1/datasets")
        assert resp.status_code == 401, resp.text


def test_rate_limit_returns_429_after_threshold(client: TestClient, db: Session, monkeypatch) -> None:
    """超过阈值返回 429 + 标准错误信封(API-RL-001)。"""
    monkeypatch.setattr(settings, "rate_limit_max_requests", 3)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 60)
    from iesplan.api.limits import reset_rate_limit

    reset_rate_limit()
    for _ in range(3):
        resp = client.get("/api/projects/1/datasets")  # 非豁免路径(401 认证错误也计入)
        assert resp.status_code == 401, resp.text
    # 第 4 次触发 429
    resp = client.get("/api/projects/1/datasets")
    assert resp.status_code == 429
    body = resp.json()
    assert body["error"]["code"] == "API-RL-001"
    assert body["error"]["message_key"] == "ies.error.rate_limited"
    assert body["error"]["params"]["retry_after"] == 60


def test_rate_limit_exempts_health_and_login(client: TestClient, db: Session, monkeypatch) -> None:
    """健康/就绪/登录豁免路径不参与限流(探针与登录高频不误伤)。"""
    monkeypatch.setattr(settings, "rate_limit_max_requests", 2)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 60)
    from iesplan.api.limits import reset_rate_limit

    reset_rate_limit()
    # 业务路径先消耗配额
    client.get("/api/projects/1/datasets")
    client.get("/api/projects/1/datasets")
    # 第 3 次业务请求应 429
    assert client.get("/api/projects/1/datasets").status_code == 429
    # 但豁免路径不受影响
    assert client.get("/api/healthz").status_code == 200
    assert client.get("/api/readyz").status_code in (200, 503)
    resp = client.post(
        "/api/auth/login",
        json={"username": "nobody", "password": "x"},
    )
    assert resp.status_code == 401  # 认证失败而非 429


def test_rate_limit_disabled_passes_through(client: TestClient, db: Session, monkeypatch) -> None:
    """关闭开关(rate_limit_enabled=false)时透明放行。"""
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(settings, "rate_limit_max_requests", 1)
    from iesplan.api.limits import reset_rate_limit

    reset_rate_limit()
    for _ in range(3):
        resp = client.get("/api/projects/1/datasets")
        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# 上传配额
# ---------------------------------------------------------------------------


def test_upload_quota_blocks_over_quota_dataset(client: TestClient, db: Session, monkeypatch) -> None:
    """用户上传配额超限: 数据集版本上传拒绝 413(API-QUOTA-001)。"""
    monkeypatch.setattr(settings, "upload_quota_bytes", 1024)
    monkeypatch.setattr(settings, "project_quota_bytes", 0)
    user = _make_user(db, "quota_user")
    pid = _create_project(client, user)
    ds_id = _create_dataset(client, user, pid)
    csv_bytes = _make_csv(n=8760)
    assert len(csv_bytes) > 1024  # 本次上传即超配额

    resp = client.post(
        f"/api/projects/{pid}/datasets/{ds_id}/versions",
        data={"resolution": "1h", "utc_offset_minutes": "480"},
        files={"file": ("data.csv", csv_bytes, "text/csv")},
        headers=_login(client, user),
    )
    assert resp.status_code == 413, resp.text
    body = resp.json()
    assert body["error"]["code"] == "API-QUOTA-001"
    assert body["error"]["message_key"] == "ies.error.upload_quota_exceeded"
    assert body["error"]["params"]["scope"] == "user"


def test_upload_quota_allows_within_quota(client: TestClient, db: Session, monkeypatch) -> None:
    """配额内合法上传不受影响(200 创建数据集 + 201 上传版本)。"""
    monkeypatch.setattr(settings, "upload_quota_bytes", 10 * 1024 * 1024)  # 10MB
    monkeypatch.setattr(settings, "project_quota_bytes", 0)
    user = _make_user(db, "quota_ok")
    pid = _create_project(client, user)
    ds_id = _create_dataset(client, user, pid)
    csv_bytes = _make_csv(n=8760)

    resp = client.post(
        f"/api/projects/{pid}/datasets/{ds_id}/versions",
        data={"resolution": "1h", "utc_offset_minutes": "480"},
        files={"file": ("data.csv", csv_bytes, "text/csv")},
        headers=_login(client, user),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["dataset_version"]["version_no"] == 1


def test_import_quota_blocks_over_quota(client: TestClient, db: Session, monkeypatch) -> None:
    """项目包导入计入用户配额: 超限拒绝 413。"""
    monkeypatch.setattr(settings, "upload_quota_bytes", 64)
    monkeypatch.setattr(settings, "project_quota_bytes", 0)
    user = _make_user(db, "import_quota")
    # 构造一个非 zip(但够大) —— 配额门禁先于格式校验? 实现顺序: 配额在
    # _parse_package 之前, 故超配额直接 413 而非 400 格式错误。
    resp = client.post(
        "/api/projects/import",
        files={"file": ("pkg.bin", b"x" * 256, "application/zip")},
        headers=_login(client, user),
    )
    assert resp.status_code == 413, resp.text
    body = resp.json()
    assert body["error"]["code"] == "API-QUOTA-001"
    assert body["error"]["params"]["scope"] == "user"


# ---------------------------------------------------------------------------
# dataset meta/fields 白名单
# ---------------------------------------------------------------------------


def test_meta_whitelist_rejects_unknown_keys(client: TestClient, db: Session) -> None:
    """meta 顶层未知键拒绝 400(API-META-001), 不污染质量报告。"""
    user = _make_user(db, "meta_bad")
    pid = _create_project(client, user)
    ds_id = _create_dataset(client, user, pid)
    resp = client.post(
        f"/api/projects/{pid}/datasets/{ds_id}/versions",
        data={
            "resolution": "1h",
            "utc_offset_minutes": "480",
            "meta": '{"evil_payload": {"x": 1}, "created_reason": "upload"}',
        },
        files={"file": ("data.csv", _make_csv(n=8760), "text/csv")},
        headers=_login(client, user),
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["code"] == "API-META-001"
    assert "evil_payload" in body["error"]["params"]["errors"][0]


def test_meta_whitelist_rejects_malformed_provenance(client: TestClient, db: Session) -> None:
    """畸形 provenance(嵌套任意对象/未知溯源键)拒绝 400。"""
    user = _make_user(db, "meta_prov")
    pid = _create_project(client, user)
    ds_id = _create_dataset(client, user, pid)
    resp = client.post(
        f"/api/projects/{pid}/datasets/{ds_id}/versions",
        data={
            "resolution": "1h",
            "utc_offset_minutes": "480",
            "meta": '{"provenance": {"__proto__": {"polluted": true}}}',
        },
        files={"file": ("data.csv", _make_csv(n=8760), "text/csv")},
        headers=_login(client, user),
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "API-META-001"


def test_fields_whitelist_rejects_unknown_attributes(client: TestClient, db: Session) -> None:
    """fields 未知属性(仅允许 unit)拒绝 400。"""
    user = _make_user(db, "fields_bad")
    pid = _create_project(client, user)
    ds_id = _create_dataset(client, user, pid)
    resp = client.post(
        f"/api/projects/{pid}/datasets/{ds_id}/versions",
        data={
            "resolution": "1h",
            "utc_offset_minutes": "480",
            "fields": '{"e_load": {"unit": "kWh", "injected": {"a": 1}}}',
        },
        files={"file": ("data.csv", _make_csv(n=8760), "text/csv")},
        headers=_login(client, user),
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "API-META-001"


def test_legal_upload_with_meta_whitelist_pass(client: TestClient, db: Session) -> None:
    """合法 meta(provenance 标准键)与合法 fields 正常上传 201。"""
    user = _make_user(db, "meta_ok")
    pid = _create_project(client, user)
    ds_id = _create_dataset(client, user, pid)
    resp = client.post(
        f"/api/projects/{pid}/datasets/{ds_id}/versions",
        data={
            "resolution": "1h",
            "utc_offset_minutes": "480",
            "fields": '{"e_load": {"unit": "kWh"}}',
            "meta": (
                '{"source_category": "user_upload", "license": "CC-BY-4.0", '
                '"provenance": {"origin": "测试", "region": "shanghai", "seed": 42, '
                '"time_range": {"start": "2025-01-01T00:00:00"}}, '
                '"created_reason": "upload"}'
            ),
        },
        files={"file": ("data.csv", _make_csv(n=8760), "text/csv")},
        headers=_login(client, user),
    )
    assert resp.status_code == 201, resp.text
    version = resp.json()["dataset_version"]
    assert version["version_no"] == 1
    assert version["provenance"]["origin"] == "测试"
    assert version["provenance"]["seed"] == 42
    assert version["provenance"]["time_range"]["start"] == "2025-01-01T00:00:00"
    assert version["license"] == "CC-BY-4.0"
