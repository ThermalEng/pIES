"""项目计算基线(0.6.5 前置阶段事项 1)契约/API/迁移测试。

覆盖:
- ``core.contracts.ProjectBaseline``: 合法构造/非法枚举/点数推导(普通年/闰年)/
  确定性摘要/canonical payload 只含三字段/严格恢复(未知字段/缺失字段/篡改
  摘要拒绝)/validate 结构化诊断/from_dict(to_dict(x)) 自洽;
- 项目创建 API: 基线三字段必填(缺失 422)、响应携带 project_baseline 与
  sha256(与 core 值对象摘要一致)、无旧 utc 字段;
- 版本固化: 版本字典与版本内容均携带 project_baseline;
- 不可变: 无任何基线更新入口(API 面)+ Postgres 触发器 DDL 常量存在;
- 迁移 0004: 旧布局 SQLite 库补列回填(1h/false/single + 默认摘要)并删除
  fixed_utc_offset_minutes 列。

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
from sqlalchemy import create_engine, select, text  # noqa: E402
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
from iesplan.models.project import Project  # noqa: E402

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
    assert ProjectBaseline(resolution=resolution, leap_year=leap_year).point_count == expected_points


def test_invalid_resolution_rejected() -> None:
    with pytest.raises(ProjectBaselineError):
        ProjectBaseline(resolution="2h", leap_year=False)


def test_invalid_scenario_mode_rejected() -> None:
    with pytest.raises(ProjectBaselineError):
        ProjectBaseline(resolution="1h", leap_year=False, scenario_mode="multi")


def test_leap_year_must_be_bool() -> None:
    with pytest.raises(ProjectBaselineError):
        ProjectBaseline(resolution="1h", leap_year="false")  # type: ignore[arg-type]


def test_digest_deterministic_and_field_sensitive() -> None:
    a1 = ProjectBaseline(resolution="1h", leap_year=False, scenario_mode="single")
    a2 = ProjectBaseline(resolution="1h", leap_year=False, scenario_mode="single")
    assert a1.digest() == a2.digest()
    assert len(a1.digest()) == 64
    assert a1.digest() != ProjectBaseline(resolution="30min", leap_year=False).digest()
    assert a1.digest() != ProjectBaseline(resolution="1h", leap_year=True).digest()


def test_canonical_payload_only_three_fields() -> None:
    payload = ProjectBaseline(resolution="1h", leap_year=False).canonical_payload()
    assert set(json.loads(payload)) == {"resolution", "leap_year", "scenario_mode"}


def test_from_dict_roundtrip_to_dict() -> None:
    baseline = ProjectBaseline(resolution="30min", leap_year=True, scenario_mode="single")
    assert ProjectBaseline.from_dict(baseline.to_dict()) == baseline


def test_from_dict_rejects_tampered_sha256() -> None:
    mapping = ProjectBaseline(resolution="1h", leap_year=False).to_dict()
    mapping["sha256"] = "0" * 64
    with pytest.raises(ProjectBaselineError):
        ProjectBaseline.from_dict(mapping)


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
        {"resolution": "2h", "leap_year": "yes", "unknown": 1, "sha256": "zz"}
    )
    codes = {d.code for d in diags}
    assert codes == {"PROJ-BASE-001"}
    details = " ".join(str(d.params.get("detail") or "") for d in diags)
    assert "2h" in details
    assert "布尔" in details
    assert "unknown" in details
    assert "64 位" in details


def test_validate_reports_digest_mismatch() -> None:
    mapping = ProjectBaseline(resolution="1h", leap_year=False).to_dict()
    mapping["sha256"] = "0" * 64
    diags = ProjectBaseline.validate(mapping)
    assert len(diags) == 1
    assert "不一致" in str(diags[0].params.get("detail") or "")


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


def test_create_project_returns_baseline_and_digest(
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
    assert project["project_baseline"]["sha256"] == expected.digest()
    assert "fixed_utc_offset_minutes" not in project


def test_create_project_ignores_legacy_utc_offset_field(
    client: TestClient, db_session: Session
) -> None:
    headers, _ = _owner_headers(client, db_session)
    resp = client.post(
        "/api/projects",
        json={
            "name": "旧字段项目",
            "utc_offset_minutes": 480,
            "baseline_resolution": "1h",
            "baseline_leap_year": False,
            "baseline_scenario_mode": "single",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    project = resp.json()["project"]
    assert project["project_baseline"]["resolution"] == "1h"
    assert "fixed_utc_offset_minutes" not in project


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
    assert version["project_baseline"]["sha256"] == expected.digest()


def test_no_baseline_update_endpoint(client: TestClient, db_session: Session) -> None:
    """基线创建后不可修改: API 面不存在任何基线更新路由。"""
    for route in projects_api.router.routes:
        assert "baseline" not in route.path


def test_baseline_immutable_trigger_ddl_exists() -> None:
    """Postgres 层不可变触发器 DDL 存在且覆盖基线四列(生产库经 db.init_db 部署)。"""
    from iesplan.models.immutable_triggers import PROJECT_BASELINE_IMMUTABLE_TRIGGER_SQL

    assert "tg_projects_baseline_immutable" in PROJECT_BASELINE_IMMUTABLE_TRIGGER_SQL
    assert "tg_project_versions_baseline_immutable" in PROJECT_BASELINE_IMMUTABLE_TRIGGER_SQL
    for column in (
        "baseline_resolution",
        "baseline_leap_year",
        "baseline_scenario_mode",
        "baseline_sha256",
    ):
        assert column in PROJECT_BASELINE_IMMUTABLE_TRIGGER_SQL


# ---------------------------------------------------------------------------
# 迁移 0004: 存量库回填默认基线 + 删除旧时区列
# ---------------------------------------------------------------------------


_OLD_PROJECTS_DDL = """
CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    owner_id INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    fixed_utc_offset_minutes INTEGER NOT NULL DEFAULT 480,
    schema_version INTEGER NOT NULL DEFAULT 1,
    current_draft_id INTEGER,
    current_version_id INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP,
    created_by INTEGER NOT NULL
);
"""

_OLD_PROJECT_VERSIONS_DDL = """
CREATE TABLE project_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    version_no INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    created_by INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    parent_version_id INTEGER,
    source_draft_id INTEGER,
    source_draft_revision INTEGER,
    reason TEXT NOT NULL,
    fixed_utc_offset_minutes INTEGER NOT NULL DEFAULT 480,
    currency TEXT,
    schema_version INTEGER NOT NULL,
    content_hash TEXT NOT NULL
);
"""


def test_migration_0004_backfills_default_baseline_and_drops_utc_column() -> None:
    """旧布局 SQLite 库: 补列 → 回填 1h/false/single + 默认摘要 → 删旧列。"""
    eng = create_engine("sqlite+pysqlite://", connect_args={"check_same_thread": False})
    with eng.begin() as conn:
        conn.execute(text(_OLD_PROJECTS_DDL))
        conn.execute(text(_OLD_PROJECT_VERSIONS_DDL))
        conn.execute(
            text("INSERT INTO projects (name, owner_id, created_by) VALUES ('存量项目', 1, 1)")
        )
        conn.execute(
            text(
                "INSERT INTO project_versions (project_id, version_no, name, created_by,"
                " reason, fixed_utc_offset_minutes, currency, schema_version, content_hash)"
                " VALUES (1, 1, 'v1', 1, 'snapshot_freeze', 480, 'CNY', 1,"
                " 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')"
            )
        )
    with eng.begin() as conn:
        _migrate_0004(conn)
    with eng.begin() as conn:
        row = conn.execute(
            text(
                "SELECT baseline_resolution, baseline_leap_year, baseline_scenario_mode,"
                " baseline_sha256 FROM projects WHERE id = 1"
            )
        ).one()
        assert row.baseline_resolution == "1h"
        assert row.baseline_leap_year == 0
        assert row.baseline_scenario_mode == "single"
        expected = ProjectBaseline(
            resolution="1h", leap_year=False, scenario_mode="single"
        ).digest()
        assert row.baseline_sha256 == expected
        vrow = conn.execute(
            text(
                "SELECT baseline_resolution, baseline_sha256 FROM project_versions WHERE id = 1"
            )
        ).one()
        assert vrow.baseline_resolution == "1h"
        assert vrow.baseline_sha256 == expected
        for table in ("projects", "project_versions"):
            cols = {
                r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).all()
            }
            assert "fixed_utc_offset_minutes" not in cols, table
            assert {"baseline_resolution", "baseline_leap_year", "baseline_scenario_mode",
                    "baseline_sha256"} <= cols
    eng.dispose()


def test_migration_0004_idempotent_on_fresh_schema() -> None:
    """全新库(create_all 已含基线列、无旧列): 迁移为 no-op 且可重复执行。"""
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
    from iesplan.models.identity import User
    from iesplan.services import project as project_service

    user = User(username="baseline-required", display_name="必填测试")
    db_session.add(user)
    db_session.flush()
    with pytest.raises(TypeError):
        project_service.create_project(db_session, user, name="缺省基线项目")
    # 显式基线: 创建成功且摘要与 core 值对象一致
    project = project_service.create_project(
        db_session, user, name="显式基线项目",
        baseline_resolution="1h",
        baseline_leap_year=False,
        baseline_scenario_mode="single",
    )
    assert project.baseline_sha256 == ProjectBaseline(
        resolution="1h", leap_year=False, scenario_mode="single"
    ).digest()
