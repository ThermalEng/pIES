"""解耦重构切片 6: services.project / services.validation 迁移测试。

- tasks 域新增: cancel_pending_tasks(删除协调)/has_running_tasks(删除 guard);
- dataset 域新增: list_versions_by_ids/list_dataset_ids(绑定校验批量读);
- services.project.delete_project 改经 tasks 域: 运行中阻断 / 排队取消;
- services.validation._check_data 改经 dataset 域: 绑定版本归属与质量门。
运行环境与切片 5 一致: SQLite 内存库 + 临时 data_dir(对象存储)。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from iesplan import dataset as dataset_domain
from iesplan import project as project_domain
from iesplan import tasks as tasks_domain
from iesplan.config import settings
from iesplan.core.errors import ConflictError
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


def _project(db: Session, name: str = "slice6-proj"):
    return project_domain.create_project(db, name=name, owner_id=7, created_by=7)


def _task(db: Session, project_id: int, status: str = "queued") -> int:
    task = tasks_domain.create_task(db, project_id=project_id, type="calc", requested_by=7)
    if status != "queued":
        tasks_domain.set_task_status(db, task.id, status)
    return task.id


def test_tasks_domain_cancel_pending_and_running_guard(db: Session) -> None:
    project = _project(db)
    q1 = _task(db, project.id, "queued")
    q2 = _task(db, project.id, "cancelling")
    r1 = _task(db, project.id, "running")

    assert tasks_domain.has_running_tasks(db, project.id) is True
    assert tasks_domain.has_running_tasks(db, 999999) is False

    cancelled = tasks_domain.cancel_pending_tasks(db, project.id)
    assert cancelled == 2
    assert tasks_domain.get_task(db, q1).status == "cancelled"
    assert tasks_domain.get_task(db, q2).status == "cancelled"
    assert tasks_domain.get_task(db, r1).status == "running"
    # 运行中不受影响, 仍阻断
    assert tasks_domain.has_running_tasks(db, project.id) is True
    assert tasks_domain.cancel_pending_tasks(db, 999999) == 0


def test_project_delete_flow_through_tasks_domain(db: Session) -> None:
    user = SimpleNamespace(id=7)
    project = _project(db, "slice6-del")
    running = _task(db, project.id, "running")
    queued = _task(db, project.id, "queued")

    with pytest.raises(ConflictError):
        project_service.delete_project(db, user, project.id, confirm=True, name="slice6-del")
    # 阻断前排队任务已被取消(先取消后检查, 与迁移前语义一致)
    assert tasks_domain.get_task(db, queued).status == "cancelled"
    assert tasks_domain.get_task(db, running).status == "running"

    tasks_domain.set_task_status(db, running, "failed")
    project_service.delete_project(db, user, project.id, confirm=True, name="slice6-del")
    assert project_domain.get_project(db, project.id) is None  # 软删后不可见


def test_dataset_domain_batch_reads(db: Session) -> None:
    project = _project(db)
    ds = dataset_domain.create_dataset(db, name="d1", created_by=7, project_id=project.id)
    shared = dataset_domain.create_dataset(db, name="shared", created_by=7)
    v1 = dataset_domain.create_version(
        db,
        dataset_id=ds.id,
        timeline="hourly",
        fixed_utc_offset_minutes=0,
        fields={},
        units={},
        created_by=7,
        quality_report={"diagnostics": []},
    )
    assert [v.id for v in dataset_domain.list_versions_by_ids(db, [v1.id])] == [v1.id]
    assert dataset_domain.list_versions_by_ids(db, []) == []
    assert dataset_domain.list_versions_by_ids(db, [999999]) == []
    # 严格归属: 不含共享数据集
    assert dataset_domain.list_dataset_ids(db, project.id) == [ds.id]
    assert shared.id not in dataset_domain.list_dataset_ids(db, project.id)


def test_validation_check_data_through_dataset_domain(db: Session, data_dir: Path) -> None:
    project = _project(db, "slice6-valid")
    ds = dataset_domain.create_dataset(db, name="d2", created_by=7, project_id=project.id)
    v1 = dataset_domain.create_version(
        db,
        dataset_id=ds.id,
        timeline="hourly",
        fixed_utc_offset_minutes=0,
        fields={},
        units={},
        created_by=7,
        quality_report={"diagnostics": []},
    )
    content = {"dataset_bindings": [{"dataset_version_id": v1.id}]}
    obj = put_object(db, json.dumps(content).encode("utf-8"), "application/json", source_category="draft")
    project_domain.create_draft(db, project_id=project.id, content_object_id=obj.id, updated_by=7)

    diags: list = []
    validation_service._check_data(db, project_domain.get_project(db, project.id), diags)
    assert diags == []

    # 绑定不存在的版本 → 阻断诊断(经域门面读到缺失)
    bad = {"dataset_bindings": [{"dataset_version_id": 999999}]}
    obj2 = put_object(db, json.dumps(bad).encode("utf-8"), "application/json", source_category="draft")
    project_domain.create_draft(db, project_id=project.id, content_object_id=obj2.id, updated_by=7)
    diags2: list = []
    validation_service._check_data(db, project_domain.get_project(db, project.id), diags2)
    assert any(d.code == "VALID-DATA-004" for d in diags2)
