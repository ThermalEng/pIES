"""解耦重构切片 8: audit 域新建 + services.audit/config 迁移测试。

- 直接覆盖 `iesplan.audit` 门面：追加写入（after 由 revision/result/extra
  组装）与过滤 + 游标分页查询；
- 覆盖 `iesplan.audit` 域门面（旧 services.audit 已删除）：统一入口返回记录、
  查询信封、序列化形状（时间为 ISO 字符串）；
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
from iesplan.identity.persistence import User as _UserForDDL  # noqa: F401


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
    rec = audit_domain.audit_user_action(db, user, "task.submitted", "task", 9, result={"status": "ok"})
    assert rec.actor_id == 7 and rec.after == {"result": {"status": "ok"}}

    audit_domain.audit(db, None, "auth.login", "user", 7, actor_type="system")
    envelope = audit_domain.query_audit(db, limit=1)
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

    # next_cursor 指向最后一条实际展示行：下一页继续展示剩余行，末页无游标
    # （旧实现曾指向未展示的探测行，导致探测行被 `id < cursor` 永远跳过）。
    assert envelope["next_cursor"] == item["id"]
    tail = audit_domain.query_audit(db, cursor=envelope["next_cursor"], limit=1)
    assert [r["id"] for r in tail["items"]] != [item["id"]] and len(tail["items"]) == 1
    assert tail["next_cursor"] is None
    assert audit_domain.query_audit(db, action="no.such.action")["items"] == []


def test_audit_cursor_points_at_last_shown_row(db: Session) -> None:
    """分页游标回归：12 条记录 limit=5，逐页 ID 精确集合/顺序，不重不漏。

    旧实现 `next_cursor=rows[-1].id` 取未展示探测行（首页展示 ID 前 5
    却返回第 6 行 ID），下一页 `id < cursor` 使探测行永久丢失；
    修正后游标恒为最后展示行 ID。
    """
    made = [
        audit_domain.append_entry(
            db, actor_id=7, action="page.probe", entity_type="probe", entity_id=i,
        ).id
        for i in range(1, 13)
    ]
    db.commit()
    desc = sorted(made, reverse=True)

    page1 = audit_domain.query_audit(db, action="page.probe", limit=5)
    assert [r["id"] for r in page1["items"]] == desc[:5]
    assert page1["next_cursor"] == desc[4]

    page2 = audit_domain.query_audit(db, action="page.probe", limit=5, cursor=page1["next_cursor"])
    assert [r["id"] for r in page2["items"]] == desc[5:10]
    assert page2["next_cursor"] == desc[9]

    page3 = audit_domain.query_audit(db, action="page.probe", limit=5, cursor=page2["next_cursor"])
    assert [r["id"] for r in page3["items"]] == desc[10:]
    assert page3["next_cursor"] is None

    seen = [r["id"] for r in page1["items"] + page2["items"] + page3["items"]]
    assert sorted(seen) == sorted(made)
