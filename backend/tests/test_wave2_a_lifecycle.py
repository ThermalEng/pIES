"""Wave 2 W2-A: application/projects/lifecycle 用例测试。

覆盖：
- 用例可独立执行并提交事务（新会话可读回写入）；
- 行为与旧服务一致（关键路径对照：创建/视图/草稿修订/归档/删除）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")

import pytest  # noqa: E402
from auth_helpers import make_user  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan.application.projects import lifecycle as lifecycle_uc  # noqa: E402
from iesplan.config import settings  # noqa: E402
from iesplan.core.errors import ConflictError, ForbiddenError, NotFoundError  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.services import project as legacy_project  # noqa: E402


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
def db_session(engine: Engine, tmp_path: Path) -> Iterator[Session]:
    settings.data_dir = tmp_path
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


def _new_session(engine: Engine) -> Session:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return factory()


BASELINE = {
    "baseline_resolution": "1h",
    "baseline_leap_year": False,
    "baseline_scenario_mode": "single",
}


def _norm_view(view: dict) -> dict:
    """视图对照归一化：剥离自增 id 与时间戳，保留业务语义。"""
    project = {k: v for k, v in view["project"].items() if k not in ("id", "created_at", "updated_at")}
    project.pop("current_draft_id", None)
    project.pop("current_version_id", None)
    draft = {k: v for k, v in view["draft"].items() if k not in ("id", "content_object_id")}
    draft.pop("created_at", None)
    draft.pop("updated_at", None)
    return {"project": project, "draft": draft, "versions": view["versions"], "my_role": view["my_role"]}


def test_create_commits_and_view(engine: Engine, db_session: Session) -> None:
    owner = make_user(db_session, "w2a_owner1")
    project = lifecycle_uc.create_project(db_session, owner, "W2A 项目一", **BASELINE)
    assert project.name == "W2A 项目一"
    assert project.owner_id == owner.id
    pid = project.id

    # 独立执行并提交事务：新会话可读回
    with _new_session(engine) as fresh:
        view = lifecycle_uc.get_project_view(fresh, owner, pid)
    assert view["project"]["name"] == "W2A 项目一"
    assert view["draft"]["revision"] == 1
    assert view["draft"]["content"]["language"] == "zh-CN"
    assert view["versions"] == []
    assert view["my_role"] == "owner"


def test_behavior_matches_legacy_service(engine: Engine, db_session: Session) -> None:
    owner = make_user(db_session, "w2a_owner2")
    new_proj = lifecycle_uc.create_project(db_session, owner, "W2A 新用例", **BASELINE)
    legacy_proj = legacy_project.create_project(db_session, owner, "W2A 旧服务", **BASELINE)
    db_session.commit()

    with _new_session(engine) as fresh:
        new_view = _norm_view(lifecycle_uc.get_project_view(fresh, owner, new_proj.id))
        legacy_view = lifecycle_uc.get_project_view(fresh, owner, legacy_proj.id)
    legacy_view = _norm_view(
        {"project": {**legacy_view["project"], "name": "W2A 新用例"},
         "draft": legacy_view["draft"], "versions": [], "my_role": legacy_view["my_role"]}
    )
    assert new_view == legacy_view

    # 草稿修订对照：同一命令在两侧产生同一结果
    cmd = lambda cid: {
        "id": cid, "project_id": None, "unit": "model",
        "type": "model.upsert_device", "payload": {"name": "pv1", "kind": "new"},
    }
    new_res = lifecycle_uc.update_draft(db_session, owner, new_proj.id, [cmd("c1")], 1)
    legacy_res = legacy_project.update_draft(db_session, owner, legacy_proj.id, [cmd("c1")], 1)
    db_session.commit()
    assert new_res["revision"] == legacy_res["revision"] == 2
    assert new_res["results"] == legacy_res["results"]
    with _new_session(engine) as fresh:
        new_content = lifecycle_uc.get_current_draft_content(fresh, new_proj.id)
        legacy_content = legacy_project.get_current_draft_content(fresh, legacy_proj.id)
    assert new_content == legacy_content


def test_archive_unarchive_delete_flow(engine: Engine, db_session: Session) -> None:
    owner = make_user(db_session, "w2a_owner3")
    stranger = make_user(db_session, "w2a_stranger3")
    project = lifecycle_uc.create_project(db_session, owner, "W2A 生命周期", **BASELINE)
    pid = project.id

    with pytest.raises(ForbiddenError):
        lifecycle_uc.archive_project(db_session, stranger, pid)

    archived = lifecycle_uc.archive_project(db_session, owner, pid)
    assert archived.status == "archived"
    with _new_session(engine) as fresh:
        assert lifecycle_uc.get_project_view(fresh, owner, pid)["project"]["status"] == "archived"

    active = lifecycle_uc.unarchive_project(db_session, owner, pid)
    assert active.status == "active"

    visible = lifecycle_uc.list_visible_projects(db_session, owner)
    assert {p["name"] for p in visible} == {"W2A 生命周期"}
    assert lifecycle_uc.list_visible_projects(db_session, owner, status="archived") == []

    lifecycle_uc.delete_project(db_session, owner, pid, confirm=True, name="W2A 生命周期")
    with _new_session(engine) as fresh:
        assert lifecycle_uc.list_visible_projects(fresh, owner) == []
        with pytest.raises(NotFoundError):
            lifecycle_uc.get_project_view(fresh, owner, pid)


def test_update_draft_idempotent_and_conflict(db_session: Session) -> None:
    owner = make_user(db_session, "w2a_owner4")
    project = lifecycle_uc.create_project(db_session, owner, "W2A 草稿", **BASELINE)
    pid = project.id
    cmd = {
        "id": "c1", "project_id": pid, "unit": "model",
        "type": "model.upsert_device", "payload": {"name": "pv1", "kind": "new"},
    }
    first = lifecycle_uc.update_draft(db_session, owner, pid, [cmd], 1)
    assert first["revision"] == 2
    # 整批重试 → 幂等返回原结果，不再递增
    retry = lifecycle_uc.update_draft(db_session, owner, pid, [cmd], 1)
    assert retry["revision"] == 2
    assert retry["results"][0]["status"] == "idempotent"
    # 新命令携带过期修订 → 冲突
    cmd2 = dict(cmd, id="c2")
    with pytest.raises(ConflictError):
        lifecycle_uc.update_draft(db_session, owner, pid, [cmd2], 1)
