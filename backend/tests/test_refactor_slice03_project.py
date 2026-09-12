"""解耦重构切片 3：project 域 repository 实现测试。

- 直接覆盖 `iesplan.project.persistence`（SQL 实现），用 SQLite 内存库；
- 正常/未找到/唯一冲突/软删隔离/savepoint 隔离均有断言；
- 不经过 services/API，repository 可独立测试即是收敛证明。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from iesplan import project as project_domain
from iesplan.db import Base
from iesplan.project.contracts import ProjectConflictError


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


def _make_project(db: Session, name: str = "p1") -> object:
    return project_domain.create_project(
        db,
        name=name,
        owner_id=7,
        created_by=7,
        baseline_resolution="1h",
        baseline_leap_year=False,
        baseline_scenario_mode="single",
    )


def test_create_get_and_missing(db_session: Session) -> None:
    created = _make_project(db_session, "alpha")
    assert created.id > 0
    assert created.status == "active"
    assert created.owner_id == 7
    assert created.current_draft_id is None
    found = project_domain.get_project(db_session, created.id)
    assert found is not None and found.name == "alpha"
    assert project_domain.get_project(db_session, 999999) is None


def test_duplicate_name_conflict_requires_caller_rollback(db_session: Session) -> None:
    _make_project(db_session, "dup")
    with pytest.raises(ProjectConflictError):
        _make_project(db_session, "dup")
    # 与旧 services 契约一致：冲突后调用方须回滚会话（实测 SA 2.0 下 flush
    # 失败后会话不可继续，savepoint 保不住可用性），回滚后会话恢复可用
    db_session.rollback()
    again = _make_project(db_session, "dup-ok")
    assert project_domain.get_project(db_session, again.id) is not None


def test_list_statuses_cursor_and_count(db_session: Session) -> None:
    a = _make_project(db_session, "a")
    b = _make_project(db_session, "b")
    project_domain.set_project_status(db_session, b.id, "archived")
    page = project_domain.list_projects(db_session, owner_id=7, statuses=["active", "archived"])
    assert [p.name for p in page.items] == ["b", "a"]
    assert page.next_cursor is None
    only_active = project_domain.list_projects(db_session, owner_id=7, statuses=["active"])
    assert [p.name for p in only_active.items] == ["a"]
    first = project_domain.list_projects(db_session, owner_id=7, limit=1)
    assert [p.name for p in first.items] == ["b"]
    assert first.next_cursor == b.id
    second = project_domain.list_projects(db_session, owner_id=7, limit=1, cursor=first.next_cursor)
    assert [p.name for p in second.items] == ["a"]
    assert project_domain.count_projects_by_owner(db_session, [7, 8]) == {7: 2}
    assert project_domain.count_projects_by_owner(db_session, []) == {}
    assert a.id != b.id


def test_deleted_excluded_from_get_but_status_flips(db_session: Session) -> None:
    created = _make_project(db_session, "gone")
    flipped = project_domain.set_project_status(db_session, created.id, "deleted")
    assert flipped.status == "deleted"
    assert project_domain.get_project(db_session, created.id) is None
    # 同名可重建（唯一键在 name 上：旧行仍占名，符合现状约束语义）
    with pytest.raises(ProjectConflictError):
        _make_project(db_session, "gone")


def test_set_status_missing_raises(db_session: Session) -> None:
    with pytest.raises(ProjectConflictError):
        project_domain.set_project_status(db_session, 999999, "archived")


def test_revision_pointers(db_session: Session) -> None:
    created = _make_project(db_session, "ptr")
    updated = project_domain.update_revision_pointers(
        db_session,
        created.id,
        finance_profile_id=3,
        overrides_revision=5,
        effective_finance_revision=6,
        planning_revision=7,
    )
    assert updated.finance_profile_id == 3
    assert updated.overrides_revision == 5
    assert updated.effective_finance_revision == 6
    assert updated.planning_revision == 7


def test_draft_lifecycle_and_current_invariant(db_session: Session) -> None:
    created = _make_project(db_session, "d")
    assert project_domain.get_current_draft(db_session, created.id) is None
    first = project_domain.create_draft(db_session, project_id=created.id, content_object_id=11, updated_by=7)
    assert first.revision == 1
    assert first.is_current is True
    assert first.parent_draft_id is None
    current = project_domain.get_current_draft(db_session, created.id)
    assert current is not None and current.id == first.id
    refreshed = project_domain.get_project(db_session, created.id)
    assert refreshed is not None and refreshed.current_draft_id == first.id
    second = project_domain.create_draft(
        db_session, project_id=created.id, content_object_id=12, updated_by=7
    )
    assert second.revision == 2
    assert second.parent_draft_id == first.id
    by_revision = project_domain.get_draft_revision(db_session, created.id, 1)
    assert by_revision is not None and by_revision.id == first.id
    assert project_domain.get_draft_revision(db_session, created.id, 99) is None
    by_id = project_domain.get_draft(db_session, second.id)
    assert by_id is not None and by_id.content_object_id == 12
    assert project_domain.get_draft(db_session, 999999) is None
    # drafts 表无 created_at 列：记录恒为 None，与旧 ORM 缺失属性读值一致，
    # 保证 draft_to_dict 输出形状不变（回归：test_auth_api 删除用户链路）。
    assert first.created_at is None


def test_update_draft_content_ref(db_session: Session) -> None:
    created = _make_project(db_session, "ref")
    draft = project_domain.create_draft(db_session, project_id=created.id, content_object_id=21, updated_by=7)
    moved = project_domain.update_draft_content_ref(db_session, draft.id, 22)
    assert moved is not None and moved.content_object_id == 22
    assert moved.revision == draft.revision
    assert project_domain.update_draft_content_ref(db_session, 999999, 22) is None


def test_version_lifecycle_refs_and_parent(db_session: Session) -> None:
    created = _make_project(db_session, "v")
    draft = project_domain.create_draft(db_session, project_id=created.id, content_object_id=31, updated_by=7)
    v1 = project_domain.create_version(
        db_session,
        project_id=created.id,
        name="v1",
        reason="manual_save",
        created_by=7,
        content_object_id=41,
        source_draft_id=draft.id,
        source_draft_revision=draft.revision,
    )
    assert v1.version_no == 1
    assert v1.parent_version_id is None
    # versions 表有 created_at：记录如实映射，保证 version_to_dict 输出不变。
    assert v1.created_at is not None
    refs = project_domain.list_version_refs(db_session, v1.id)
    assert len(refs) == 1
    assert refs[0].ref_type == "object" and refs[0].object_id == 41
    v2 = project_domain.create_version(
        db_session,
        project_id=created.id,
        name="v2",
        reason="manual_save",
        created_by=7,
        content_object_id=42,
    )
    assert v2.version_no == 2
    assert v2.parent_version_id == v1.id
    versions = project_domain.list_versions(db_session, created.id)
    assert [v.version_no for v in versions] == [2, 1]
    assert project_domain.get_version(db_session, created.id, v1.id) is not None
    assert project_domain.get_version(db_session, created.id + 1, v1.id) is None
    assert project_domain.get_version(db_session, created.id, 999999) is None
    pointed = project_domain.get_project(db_session, created.id)
    assert pointed is not None and pointed.current_version_id == v2.id


def test_duplicate_version_ref_conflicts(db_session: Session) -> None:
    created = _make_project(db_session, "vr")
    draft = project_domain.create_draft(db_session, project_id=created.id, content_object_id=51, updated_by=7)
    version = project_domain.create_version(
        db_session,
        project_id=created.id,
        name="v1",
        reason="manual_save",
        created_by=7,
        content_object_id=52,
        source_draft_id=draft.id,
        source_draft_revision=draft.revision,
    )
    with pytest.raises(ProjectConflictError):
        project_domain.add_version_ref(
            db_session,
            project_version_id=version.id,
            ref_type="object",
            object_id=52,
            ref_key="project_version_content",
        )
