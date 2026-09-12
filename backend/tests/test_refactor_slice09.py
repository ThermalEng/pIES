"""解耦重构切片 9: 审计直写收敛到 audit 域测试。

- services.project/archive 审计经 audit 域写入；
- services.validation 基准确认写入与证据回读经 audit 域；
- storage put_object 的对象创建审计经 audit 域写入；
- results 选中解读取经 audit 域（由切片 5 选中流程覆盖，此处直测读面）。
运行环境与切片 8 一致：SQLite 内存库 + 临时 data_dir（对象存储）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from iesplan import audit as audit_domain
from iesplan import project as project_domain
from iesplan.config import settings
from iesplan.db import Base
from iesplan.services import project as project_service
from iesplan.services import validation as validation_service
from iesplan.storage import put_object


@pytest.fixture()
def engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine: sa.Engine) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture(autouse=True)
def _clean_tables(engine: sa.Engine) -> Iterator[None]:
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(settings, "data_dir", d)
    return d


def _project(db: Session, name: str = "slice9-proj"):
    return project_domain.create_project(db, name=name, owner_id=7, created_by=7)


def test_project_archive_audit_through_domain(db: Session) -> None:
    user = SimpleNamespace(id=7)
    project = _project(db, "slice9-arch")
    project_service.archive_project(db, user, project.id)
    rows = audit_domain.list_entries(
        db, entity_type="project", entity_id=project.id, action="project.archived"
    )
    assert len(rows) == 1
    assert rows[0].actor_id == 7 and rows[0].after == {"status": "archived"}


def test_validation_confirm_and_evidence_through_domain(db: Session) -> None:
    user = SimpleNamespace(id=7)
    project = _project(db, "slice9-confirm")
    record = validation_service.mark_baseline_confirmed(db, project.id, user, assumptions={"k": "v"})
    assert record.after["confirmed_by"] == 7
    assert record.after["assumptions"] == {"k": "v"}
    assert "confirmed_at" in record.after

    evidence = validation_service._latest_baseline_evidence(db, project.id)
    assert evidence is not None and evidence.id == record.id
    assert evidence.after["project_version_id"] == record.after["project_version_id"]


def test_storage_put_object_audit_through_domain(db: Session, data_dir: Path) -> None:
    obj = put_object(db, b"{}", "application/json", source_category="test")
    rows = audit_domain.list_entries(db, entity_type="objects", entity_id=obj.id, action="object_created")
    assert len(rows) == 1
    assert rows[0].after["oid"] == obj.oid
