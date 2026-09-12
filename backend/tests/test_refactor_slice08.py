"""解耦重构切片 8: audit 域新建 + services.audit/config 迁移测试。

- 直接覆盖 `iesplan.audit` 门面：追加写入（after 由 revision/result/extra
  组装）与过滤 + 游标分页查询；
- 覆盖迁移后的 `iesplan.services.audit`：统一入口返回记录、查询信封、
  序列化形状（时间为 ISO 字符串）；
- 覆盖迁移后的 `services.config.load_work_graph`（经 model 域取草稿图/
  最近工作图/设备清单）。
运行环境与切片 7 一致：SQLite 内存库 + 临时 data_dir（对象存储）。
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
from iesplan import model as model_domain
from iesplan import project as project_domain
from iesplan.config import settings
from iesplan.db import Base
from iesplan.services import audit as audit_service
from iesplan.services import config as config_service


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


def test_audit_domain_append_and_list(db: Session) -> None:
    row = audit_domain.append_entry(
        db,
        actor_id=7,
        action="project.created",
        entity_type="project",
        entity_id=1,
        revision=3,
        result={"status": "ok"},
        extra={"k": "v"},
        before={"from": 1},
    )
    assert row.id is not None and row.occurred_at is not None
    assert row.after == {"revision": 3, "result": {"status": "ok"}, "k": "v"}
    assert row.before == {"from": 1}

    audit_domain.append_entry(db, actor_id=None, action="auth.login", entity_type="user", entity_id=7)
    assert len(audit_domain.list_entries(db)) == 2
    assert len(audit_domain.list_entries(db, action="auth.login")) == 1
    assert len(audit_domain.list_entries(db, entity_type="project", entity_id=1)) == 1
    assert audit_domain.list_entries(db, actor_id=999) == []
    # 游标分页：id 倒序
    page = audit_domain.list_entries(db, limit=1)
    assert len(page) == 1
    rest = audit_domain.list_entries(db, cursor=page[0].id, limit=1)
    assert len(rest) == 1 and rest[0].id != page[0].id


def test_audit_service_envelope_and_serialization(db: Session) -> None:
    user = SimpleNamespace(id=7)
    rec = audit_service.audit_user_action(db, user, "task.submitted", "task", 9, result={"status": "ok"})
    assert rec.actor_id == 7 and rec.after == {"result": {"status": "ok"}}

    audit_service.audit(db, None, "auth.login", "user", 7, actor_type="system")
    envelope = audit_service.query_audit(db, limit=1)
    assert len(envelope["items"]) == 1 and envelope["next_cursor"] is not None
    item = envelope["items"][0]
    assert set(item) == {
        "id",
        "entity_type",
        "entity_id",
        "action",
        "actor_id",
        "actor_type",
        "occurred_at",
        "ip",
        "before",
        "after",
        "request_id",
        "trace_id",
    }
    assert isinstance(item["occurred_at"], str)

    # next_cursor 指向页外末条：再取一页为空且无后续游标
    tail = audit_service.query_audit(db, cursor=envelope["next_cursor"], limit=1)
    assert tail["items"] == [] and tail["next_cursor"] is None
    assert audit_service.query_audit(db, action="no.such.action")["items"] == []


def test_config_load_work_graph_through_model_domain(db: Session, data_dir: Path) -> None:
    project = project_domain.create_project(db, name="slice8-proj", owner_id=7, created_by=7)
    assert config_service.load_work_graph(db, project.id) == {"devices": []}

    from iesplan.services import model as model_service

    graph = model_service.get_or_create_working_graph(db, project.id, created_by=7)
    dev = model_domain.create_device(
        db, graph_id=graph.id, device_type="load", kind="new", name="L1", params={"a": 1}
    )
    db.commit()
    got = config_service.load_work_graph(db, project.id)
    assert got == {
        "devices": [
            {
                "id": dev.id,
                "device_type": "load",
                "kind": "new",
                "name": "L1",
                "params": {"a": 1},
            }
        ]
    }
    assert config_service.load_work_graph(db, 999999) == {"devices": []}
