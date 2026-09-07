"""0.6.5 退役清理(旧 sequence_prep 路径残留)集成测试。

覆盖 ``services.project.purge_legacy_sequence_prep``:
- dry-run 只报告不写入(回执字段与执行路径一致);
- 当前草稿含 ``prepared_sequences`` 键 → 移除并推进新草稿行(内容寻址),
  历史草稿/版本内容属不可变证据只计入回执不改写;
- ``sequence_prep:*`` 目的 owner 引用全部解绑(对象进入 orphaned);
- 幂等: 再次执行无新清理动作(不推进修订、不重复解绑、不重复审计)。

测试环境与 test_project_model_save 同构(SQLite 内存 + tmp 对象存储目录)。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# 单文件运行安全网: 固定 SQLite(全量运行时已被其他测试模块先行导入, 无副作用)
os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")

import pytest  # noqa: E402
from auth_helpers import make_user  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan.config import settings  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.models.audit import AuditLog  # noqa: E402
from iesplan.models.project import Draft, Project, ProjectVersion  # noqa: E402
from iesplan.services import project as project_service  # noqa: E402
from iesplan.storage import (  # noqa: E402
    attach,
    find_refs_by_entity_type,
    object_info,
    put_object,
)

KEY = "prepared_sequences"

LEGACY_SAMPLE = {
    "1": {"load_data": {"object_id": 11, "content_sha256": "a" * 64,
                        "receipt_sha256": "b" * 64, "source_mode": "data_repeat"}}
}


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    from iesplan.migrations import apply_migrations

    apply_migrations(eng)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(engine: Engine) -> Iterator[None]:
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def db(engine: Engine, tmp_path: Path) -> Iterator[Session]:
    settings.data_dir = tmp_path
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


def _create_project_with_residue(db: Session) -> tuple[Project, dict[str, Any]]:
    """建项目并注入旧序列预备残留: 当前草稿键 + 历史草稿键 + 版本键 + 预备引用。"""
    user = make_user(db, "purge_owner")
    project = project_service.create_project(
        db, user, "残留项目",
        baseline_resolution="1h", baseline_leap_year=False,
        baseline_scenario_mode="single",
    )
    db.flush()
    content = project_service.get_current_draft_content(db, project.id)
    content[KEY] = LEGACY_SAMPLE
    key_hash = project_service.store_content_object(db, content)
    current = db.execute(
        select(Draft).where(
            Draft.project_id == project.id, Draft.is_current.is_(True)
        )
    ).scalar_one()
    current.content_hash = key_hash
    db.flush()
    # 历史草稿(非当前)也引用含键内容(不可变证据, 只计入回执)
    historical = Draft(
        project_id=project.id,
        revision=current.revision + 1,
        content_hash=key_hash,
        parent_draft_id=current.id,
        is_current=False,
        updated_by=user.id,
    )
    db.add(historical)
    # 版本内容同样含键(旧路径经版本固化后的形态)
    version = ProjectVersion(
        project_id=project.id,
        version_no=1,
        name="遗留版本",
        reason="legacy",
        created_by=user.id,
        baseline_resolution=project.baseline_resolution,
        baseline_leap_year=project.baseline_leap_year,
        baseline_scenario_mode=project.baseline_scenario_mode,
        baseline_sha256=project.baseline_sha256,
        schema_version=1,
        content_hash=key_hash,
        source_draft_id=current.id,
        source_draft_revision=current.revision,
    )
    db.add(version)
    # 旧预备产物引用(挂到项目模型最终 owner 实体类型)
    prep_ids: list[int] = []
    for i, suffix in ((1, "load_data"), (2, "weather")):
        handle = put_object(
            db, f"legacy-prep-{i}".encode(), "application/json",
            source_category="sequence_prep_legacy",
        )
        attach(db, handle.id, "project_model", 9000 + i,
               ref_entity_type="project_model",
               purpose=f"sequence_prep:canonical:{suffix}")
        prep_ids.append(handle.id)
    db.flush()
    return project, {
        "current_draft_id": current.id,
        "historical_draft_id": historical.id,
        "version_id": version.id,
        "prep_object_ids": prep_ids,
    }


def test_dry_run_reports_without_writing(db: Session) -> None:
    project, _ids = _create_project_with_residue(db)
    report = project_service.purge_legacy_sequence_prep(db, dry_run=True)
    assert report["dry_run"] is True
    assert report["drafts_scanned"] == 2
    assert report["drafts_with_key"] == 2
    assert report["current_drafts_cleaned"] == 1
    assert report["historical_drafts_with_key"] == 1
    assert report["versions_scanned"] == 1
    assert report["versions_with_key"] == 1
    assert report["prep_refs_found"] == 2
    assert report["prep_refs_detached"] == 0
    assert report["prep_object_ids"] == []
    assert report["cleaned_projects"] == []
    # dry-run 不做任何写入
    current = project_service.get_current_draft_content(db, project.id)
    assert KEY in current
    assert len(find_refs_by_entity_type(db, "project_model")) == 2


def test_apply_cleans_current_draft_and_detaches_prep_refs(db: Session) -> None:
    project, ids = _create_project_with_residue(db)
    before_revision = project_service.get_current_draft(db, project).revision
    report = project_service.purge_legacy_sequence_prep(db, dry_run=False)
    db.flush()  # 模拟离线 CLI 提交点: 解绑/新草稿行在该边界真正落库
    assert report["dry_run"] is False
    assert report["current_drafts_cleaned"] == 1
    assert report["prep_refs_detached"] == 2
    assert sorted(report["prep_object_ids"]) == sorted(map(str, ids["prep_object_ids"]))
    assert report["cleaned_projects"] == [project.id]

    # 当前草稿: 键移除 + 修订推进(内容寻址新对象), 历史草稿/版本不改写
    draft = project_service.get_current_draft(db, project)
    assert draft.revision == before_revision + 2  # 注入的历史草稿占用 +1, 清理再推进 +1
    content = project_service.get_current_draft_content(db, project.id)
    assert KEY not in content
    historical = db.get(Draft, ids["historical_draft_id"])
    assert historical.content_hash != draft.content_hash
    historical_content = project_service.load_content_object(db, historical.content_hash)
    assert KEY in historical_content  # 不可变证据保留
    version = db.get(ProjectVersion, ids["version_id"])
    assert project_service.load_content_object(db, version.content_hash).get(KEY) is not None

    # 预备引用全部解绑 → 对象进入 orphaned
    assert find_refs_by_entity_type(db, "project_model") == []
    for obj_id in ids["prep_object_ids"]:
        assert object_info(db, obj_id)["status"] == "orphaned"

    # 审计证据
    audit = db.execute(
        select(AuditLog).where(
            AuditLog.entity_type == "project", AuditLog.action == "project.sequence_prep_legacy_purged"
        )
    ).scalars().all()
    assert len(audit) == 1
    assert audit[0].entity_id == project.id


def test_deleted_project_drafts_not_resurrected(db: Session) -> None:
    project, ids = _create_project_with_residue(db)
    # 软删项目: 草稿链停止推进, 清理跳过当前草稿修订但解绑预备产物引用
    project.status = "deleted"
    db.flush()
    report = project_service.purge_legacy_sequence_prep(db, dry_run=False)
    db.flush()
    assert report["current_drafts_cleaned"] == 0
    assert report["cleaned_projects"] == []
    assert sorted(report["deleted_project_drafts"]) == sorted([ids["current_draft_id"]])
    assert report["prep_refs_detached"] == 2
    assert find_refs_by_entity_type(db, "project_model") == []
    # 草稿链不推进: 当前草稿仍是含键对象、is_current 不变(软删项目
    # 修订链已冻结, 不通过 project-service 读当前草稿以免 deleted-check)
    draft = db.execute(
        select(Draft).where(
            Draft.project_id == project.id, Draft.is_current.is_(True)
        )
    ).scalar_one()
    assert KEY in project_service.load_content_object(db, draft.content_hash)


def test_apply_is_idempotent(db: Session) -> None:
    project, _ids = _create_project_with_residue(db)
    first = project_service.purge_legacy_sequence_prep(db, dry_run=False)
    db.flush()  # 离线 CLI 提交点(见 test_apply 注释)
    assert first["current_drafts_cleaned"] == 1
    assert first["prep_refs_found"] == 2
    assert first["prep_refs_detached"] == 2, first
    assert find_refs_by_entity_type(db, "project_model") == []
    revision_after_first = project_service.get_current_draft(db, project).revision
    audit_count = len(
        db.execute(
            select(AuditLog).where(AuditLog.action == "project.sequence_prep_legacy_purged")
        ).scalars().all()
    )
    # 二次执行: 当前草稿已无键、引用已解绑 → 无新清理动作、不推进修订
    second = project_service.purge_legacy_sequence_prep(db, dry_run=False)
    assert second["current_drafts_cleaned"] == 0
    assert second["prep_refs_found"] == 0
    assert second["prep_refs_detached"] == 0
    assert second["cleaned_projects"] == []
    assert project_service.get_current_draft(db, project).revision == revision_after_first
    assert (
        len(db.execute(
            select(AuditLog).where(AuditLog.action == "project.sequence_prep_legacy_purged")
        ).scalars().all())
        == audit_count
    )
