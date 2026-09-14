"""项目计算基线(0.6.5 前置阶段事项 1)契约/API/迁移测试。

覆盖:
- ``core.contracts.ProjectBaseline``: 合法构造/非法枚举/点数推导(普通年/闰年)/
  canonical payload 只含三字段/严格恢复(未知字段/缺失字段拒绝)/
  validate 结构化诊断/from_dict(to_dict(x)) 自洽;
- 项目创建 API: 基线三字段必填(缺失 422)、响应携带 project_baseline、
  基线字段仅使用当前契约;
- 版本固化: 版本字典与版本内容均携带 project_baseline;
- 不可变: 无任何基线更新入口(API 面)+ Postgres 触发器 DDL 常量存在;
- 迁移 0004: 当前项目基线迁移版本可重复执行。

测试环境: SQLite :memory:(StaticPool 共享连接) + tmp 对象存储目录。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

# 单文件运行时的安全网: 固定 SQLite, 避免 iesplan.main 启动期误连部署 Postgres
os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")

import pytest  # noqa: E402
from auth_helpers import login_headers, make_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan.api import projects as projects_api  # noqa: E402
from iesplan.config import settings  # noqa: E402
from iesplan.core.contracts import (  # noqa: E402
    ProjectBaseline,
    ProjectBaselineError,
)
from iesplan.db import Base, get_db  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.migrations import _migrate_0004  # noqa: E402

# ---------------------------------------------------------------------------
# 测试环境(与 test_project_api.py 同构)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(engine: Engine) -> Iterator[None]:
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def db_session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


@pytest.fixture()
def client(engine: Engine, db_session: Session, tmp_path: Path) -> Iterator[TestClient]:
    settings.data_dir = tmp_path
    app = create_app()
    app.include_router(projects_api.router)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as c:
        yield c


def _owner_headers(client: TestClient, db_session: Session) -> dict:
    """创建工程师用户并返回窗口会话认证头。"""
    user = make_user(db_session, "baseline_owner")
    token = login_headers(client, user)
    return token, user


# ---------------------------------------------------------------------------
# 契约: ProjectBaseline
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("resolution", "leap_year", "expected_points"),
    [
        ("1h", False, 8760),
        ("1h", True, 8784),
        ("30min", False, 17520),
        ("30min", True, 17568),
        ("15min", False, 35040),
        ("15min", True, 35136),
    ],
)
def test_point_count_table(resolution: str, leap_year: bool, expected_points: int) -> None:
    baseline = ProjectBaseline(resolution=resolution, leap_year=leap_year)
    assert baseline.point_count == expected_points
    assert baseline.initial_step == 0
    assert baseline.step_count == expected_points


@pytest.mark.parametrize(
    ("resolution", "expected_duration"),
    [("15min", 0.25), ("30min", 0.5), ("1h", 1.0)],
)
def test_step_duration_is_derived_from_resolution(
    resolution: str, expected_duration: float
) -> None:
    baseline = ProjectBaseline(resolution=resolution, leap_year=False)
    assert baseline.step_duration == expected_duration


def test_invalid_resolution_rejected() -> None:
    with pytest.raises(ProjectBaselineError):
        ProjectBaseline(resolution="2h", leap_year=False)


def test_invalid_scenario_mode_rejected() -> None:
    with pytest.raises(ProjectBaselineError):
        ProjectBaseline(resolution="1h", leap_year=False, scenario_mode="multi")


def test_leap_year_must_be_bool() -> None:
    with pytest.raises(ProjectBaselineError):
        ProjectBaseline(resolution="1h", leap_year="false")  # type: ignore[arg-type]


def test_canonical_payload_only_three_fields() -> None:
    payload = ProjectBaseline(resolution="1h", leap_year=False).canonical_payload()
    assert set(json.loads(payload)) == {"resolution", "leap_year", "scenario_mode"}


def test_from_dict_roundtrip_to_dict() -> None:
    baseline = ProjectBaseline(resolution="30min", leap_year=True, scenario_mode="single")
    assert ProjectBaseline.from_dict(baseline.to_dict()) == baseline


def test_from_dict_rejects_unknown_and_missing_fields() -> None:
    with pytest.raises(ProjectBaselineError):
        ProjectBaseline.from_dict({"resolution": "1h", "leap_year": False, "timezone": "+08:00"})
    with pytest.raises(ProjectBaselineError):
        ProjectBaseline.from_dict({"resolution": "1h"})
    with pytest.raises(ProjectBaselineError):
        ProjectBaseline.from_dict({"leap_year": False})


def test_from_dict_scenario_mode_defaults_to_single() -> None:
    baseline = ProjectBaseline.from_dict({"resolution": "1h", "leap_year": False})
    assert baseline.scenario_mode == "single"


def test_validate_reports_structured_diagnostics() -> None:
    diags = ProjectBaseline.validate(
        {"resolution": "2h", "leap_year": "yes", "unknown": 1}
    )
    codes = {d.code for d in diags}
    assert codes == {"PROJ-BASE-001"}
    details = " ".join(str(d.params.get("detail") or "") for d in diags)
    assert "2h" in details
    assert "布尔" in details
    assert "unknown" in details


def test_validate_accepts_valid_dict() -> None:
    assert ProjectBaseline.validate(
        ProjectBaseline(resolution="1h", leap_year=False).to_dict()
    ) == []


# ---------------------------------------------------------------------------
# API: 项目创建与基线必填/响应
# ---------------------------------------------------------------------------


def test_create_project_requires_baseline_fields(client: TestClient, db_session: Session) -> None:
    headers, _ = _owner_headers(client, db_session)
    resp = client.post("/api/projects", json={"name": "缺基线"}, headers=headers)
    assert resp.status_code == 422


def test_create_project_requires_explicit_scenario(client: TestClient, db_session: Session) -> None:
    headers, _ = _owner_headers(client, db_session)
    resp = client.post(
        "/api/projects",
        json={
            "name": "缺场景模式",
            "baseline_resolution": "1h",
            "baseline_leap_year": False,
        },
        headers=headers,
    )
    assert resp.status_code == 422


def test_create_project_rejects_invalid_baseline(client: TestClient, db_session: Session) -> None:
    headers, _ = _owner_headers(client, db_session)
    resp = client.post(
        "/api/projects",
        json={
            "name": "非法分辨率",
            "baseline_resolution": "2h",
            "baseline_leap_year": False,
            "baseline_scenario_mode": "single",
        },
        headers=headers,
    )
    assert resp.status_code == 422


def test_create_project_returns_baseline(
    client: TestClient, db_session: Session
) -> None:
    headers, _ = _owner_headers(client, db_session)
    resp = client.post(
        "/api/projects",
        json={
            "name": "基线项目",
            "currency": "CNY",
            "baseline_resolution": "30min",
            "baseline_leap_year": True,
            "baseline_scenario_mode": "single",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    project = resp.json()["project"]
    expected = ProjectBaseline(
        resolution="30min", leap_year=True, scenario_mode="single"
    )
    assert project["project_baseline"] == expected.to_dict()


def test_create_project_rejects_unknown_baseline_field(
    client: TestClient, db_session: Session
) -> None:
    headers, _ = _owner_headers(client, db_session)
    resp = client.post(
        "/api/projects",
        json={
            "name": "未知字段项目",
            "utc_offset_minutes": 480,
            "baseline_resolution": "1h",
            "baseline_leap_year": False,
            "baseline_scenario_mode": "single",
        },
        headers=headers,
    )
    assert resp.status_code == 422


def test_project_view_and_version_freeze_baseline(
    client: TestClient, db_session: Session
) -> None:
    headers, _ = _owner_headers(client, db_session)
    resp = client.post(
        "/api/projects",
        json={
            "name": "基线冻结项目",
            "baseline_resolution": "1h",
            "baseline_leap_year": False,
            "baseline_scenario_mode": "single",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    pid = resp.json()["project"]["id"]
    view = client.get(f"/api/projects/{pid}", headers=headers).json()
    expected = ProjectBaseline(resolution="1h", leap_year=False, scenario_mode="single")
    assert view["project"]["project_baseline"] == expected.to_dict()
    # 版本创建: 版本字典与版本内容均固化基线
    resp = client.post(
        f"/api/projects/{pid}/versions",
        json={"name": "基线版本", "reason": "milestone"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    version = resp.json()["version"]
    assert version["project_baseline"] == expected.to_dict()


def test_no_baseline_update_endpoint(client: TestClient, db_session: Session) -> None:
    """基线创建后不可修改: API 面不存在任何基线更新路由。"""
    for route in projects_api.router.routes:
        assert "baseline" not in route.path


def test_baseline_immutable_trigger_ddl_exists() -> None:
    """Postgres 层不可变触发器 DDL 存在且覆盖基线四列(生产库经 db.init_db 部署)。"""
    from iesplan.db import PROJECT_BASELINE_IMMUTABLE_TRIGGER_SQL

    assert "tg_projects_baseline_immutable" in PROJECT_BASELINE_IMMUTABLE_TRIGGER_SQL
    assert "tg_project_versions_baseline_immutable" in PROJECT_BASELINE_IMMUTABLE_TRIGGER_SQL
    for column in (
        "baseline_resolution",
        "baseline_leap_year",
        "baseline_scenario_mode",
    ):
        assert column in PROJECT_BASELINE_IMMUTABLE_TRIGGER_SQL


# ---------------------------------------------------------------------------
# 迁移 0004: 当前项目基线迁移版本
# ---------------------------------------------------------------------------
def test_migration_0004_idempotent_on_current_schema() -> None:
    """当前 schema 上迁移版本可重复执行。"""
    eng = create_engine("sqlite+pysqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        _migrate_0004(conn)
        _migrate_0004(conn)  # 重复执行幂等
    eng.dispose()


# ---------------------------------------------------------------------------
# 持久化: 服务层基线必填(不静默默认)
# ---------------------------------------------------------------------------


def test_service_requires_explicit_baseline(db_session: Session) -> None:
    """create_project 基线三字段为必填关键字参数: 缺省调用直接 TypeError。"""
    from iesplan.application.projects import lifecycle as projects_uc
    from iesplan.identity.persistence import User

    user = User(username="baseline-required", display_name="必填测试")
    db_session.add(user)
    db_session.flush()
    with pytest.raises(TypeError):
        projects_uc.create_project(db_session, user, name="缺省基线项目")
    # 显式基线: 创建成功
    project = projects_uc.create_project(
        db_session, user, name="显式基线项目",
        baseline_resolution="1h",
        baseline_leap_year=False,
        baseline_scenario_mode="single",
    )
    assert project.baseline_resolution == "1h"
    assert project.baseline_leap_year is False
