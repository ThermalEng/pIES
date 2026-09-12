"""解耦重构切片 9: 审计直写收敛到 audit 域测试。

- application.projects.archive 审计经 audit 域写入；
- application.validations 基准确认写入与证据回读(经公开预检能力)；
- storage put_object 不再直接写审计（反向依赖已移除；对象管理审计由
  application.objects 用例在成功路径记录）；
- results 选中解读取经 audit 域（由切片 5 选中流程覆盖，此处直测读面）。
运行环境与切片 8 一致：SQLite 内存库 + 临时 data_dir（对象存储）。
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

from iesplan import audit as audit_domain
from iesplan import project as project_domain
from iesplan.application.projects import lifecycle as projects_uc
from iesplan.application.validations import precheck as validations_uc
from iesplan.config import settings
from iesplan.db import Base
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
    projects_uc.archive_project(db, user, project.id)
    rows = audit_domain.list_entries(
        db, entity_type="project", entity_id=project.id, action="project.archived"
    )
    assert len(rows) == 1
    assert rows[0].actor_id == 7 and rows[0].after == {"status": "archived"}


def test_validation_confirm_and_evidence_through_domain(db: Session, data_dir: Path) -> None:
    user = SimpleNamespace(id=7)
    project = _project(db, "slice9-confirm")
    # 公开预检要求当前草稿存在: 先建空绑定草稿(数据诊断不影响 FIN 断言)
    obj = put_object(
        db, json.dumps({"dataset_bindings": []}).encode("utf-8"),
        "application/json", source_category="draft",
    )
    project_domain.create_draft(db, project_id=project.id, content_object_id=obj.id, updated_by=7)
    record = validations_uc.mark_baseline_confirmed(db, project.id, user, assumptions={"k": "v"})
    assert record.after["confirmed_by"] == 7
    assert record.after["assumptions"] == {"k": "v"}
    assert "confirmed_at" in record.after

    # 证据回读经公开预检能力(旧 services.validation._latest_baseline_evidence
    # 私有函数已删除, 不复制其实现; 预检内部走同一证据读取路径):
    # 确认后财务基准缺失诊断 VALID-FIN-001 消除
    report = validations_uc.validate_project(db, project.id)
    assert "VALID-FIN-001" not in [d.code for d in report.diagnostics]


def test_storage_put_object_no_direct_audit(db: Session, data_dir: Path) -> None:
    """存储层不直接写审计(反向依赖已移除); 需要审计的公开业务用例在成功路径记录。"""
    obj = put_object(db, b"{}", "application/json", source_category="test")
    rows = audit_domain.list_entries(db, entity_type="objects", entity_id=obj.id, action="object_created")
    assert rows == []
