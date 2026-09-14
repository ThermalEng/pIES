"""W1-Storage: RetentionRule 归入 storage 持久化边界回归测试。

覆盖:
- retention 规则匹配走 storage 边界(persistence.list_active_retention_rules
  → contracts.RetentionPolicy → service._match_retention_rule → safe_cleanup);
- storage 不再导入 audit ORM(iesplan/storage 下 4 个白名单文件经 AST 断言)。

运行方式: 内存 SQLite + 临时 data_dir(与 test_objects_api 同构, 不触碰真实库)。
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from iesplan.config import settings
from iesplan.db import Base
from iesplan.audit.persistence import RetentionRule
from iesplan.identity.persistence import User
from iesplan.storage import RetentionPolicy, put_object, safe_cleanup
from iesplan.storage.persistence import StoredObject, list_active_retention_rules
from iesplan.storage.service import _match_retention_rule


# ---------------------------------------------------------------------------
# 夹具(与 test_objects_api 同构)
# ---------------------------------------------------------------------------


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
def session(engine) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture()
def data_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(settings, "data_dir", d)
    return d


def _seed_user(session: Session) -> User:
    user = User(username="sysop", display_name="系统")
    session.add(user)
    session.flush()
    return user


def _add_rule(
    session: Session,
    user_id: int,
    *,
    entity_type: str = "objects",
    object_kind: str = "*",
    retention_days: int = 36500,
    apply_to: str = "orphaned",
    status: str = "active",
) -> RetentionRule:
    rule = RetentionRule(
        entity_type=entity_type,
        object_kind=object_kind,
        retention_days=retention_days,
        apply_to=apply_to,
        status=status,
        created_by=user_id,
    )
    session.add(rule)
    session.flush()
    return rule


def _put(session: Session, content: bytes = b"w1-storage"):
    return put_object(session, content, "text/plain", source_category="test")


# ---------------------------------------------------------------------------
# 1. storage 不再导入 audit ORM
# ---------------------------------------------------------------------------


def test_storage_does_not_import_audit_orm() -> None:
    """storage 白名单文件不得导入 iesplan.models.audit / RetentionRule ORM。"""
    storage_dir = Path(__file__).resolve().parent.parent / "iesplan" / "storage"
    checked = []
    for name in ("service.py", "persistence.py", "contracts.py", "__init__.py"):
        tree = ast.parse((storage_dir / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "") in {
                "iesplan.models.audit",
                "iesplan.models",
            }:
                imported = {a.name for a in node.names}
                assert "RetentionRule" not in imported, f"{name}:{node.lineno} 仍导入 RetentionRule"
                assert node.module != "iesplan.models.audit", (
                    f"{name}:{node.lineno} 仍导入 iesplan.models.audit"
                )
            elif isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name != "iesplan.models.audit", (
                        f"{name}:{node.lineno} 仍导入 iesplan.models.audit"
                    )
        checked.append(name)
    assert checked == ["service.py", "persistence.py", "contracts.py", "__init__.py"]


# ---------------------------------------------------------------------------
# 2. 规则读取收敛到 storage 持久化边界
# ---------------------------------------------------------------------------


def test_list_active_retention_rules_returns_contract_values(session: Session) -> None:
    """persistence loader 只返回 active 规则的 RetentionPolicy 值对象。"""
    user = _seed_user(session)
    _add_rule(session, user.id, retention_days=30)
    _add_rule(session, user.id, object_kind="text/plain", retention_days=7)
    _add_rule(session, user.id, object_kind="application/pdf", retention_days=99, status="paused")
    session.commit()

    rules = list_active_retention_rules(session)

    assert len(rules) == 2
    assert all(isinstance(r, RetentionPolicy) for r in rules)
    assert all(r.status == "active" for r in rules)
    assert {r.retention_days for r in rules} == {30, 7}


def test_match_retention_rule_selects_strictest() -> None:
    """匹配语义: 通配/媒体类型命中取最小天数; 非 active/非 objects 忽略。"""
    obj = StoredObject(oid="x" * 64, size_bytes=1, media_type="text/plain")
    rules = [
        RetentionPolicy(id=1, entity_type="objects", object_kind="*", retention_days=30),
        RetentionPolicy(id=2, entity_type="objects", object_kind="text/plain", retention_days=7),
        RetentionPolicy(
            id=3, entity_type="objects", object_kind="image/png", retention_days=1
        ),
        RetentionPolicy(
            id=4, entity_type="objects", object_kind="*", retention_days=5, status="paused"
        ),
        RetentionPolicy(id=5, entity_type="projects", object_kind="*", retention_days=1),
    ]
    matched = _match_retention_rule(rules, obj)
    assert matched is not None
    assert matched.id == 2  # 命中中最严格(7 天); paused/他表/他媒体类型被忽略


def test_match_retention_rule_no_hit_returns_none() -> None:
    obj = StoredObject(oid="y" * 64, size_bytes=1, media_type="image/png")
    rules = [
        RetentionPolicy(id=1, entity_type="objects", object_kind="text/plain", retention_days=7),
    ]
    assert _match_retention_rule(rules, obj) is None
    assert _match_retention_rule([], obj) is None


# ---------------------------------------------------------------------------
# 3. safe_cleanup 经 storage 边界应用保留规则
# ---------------------------------------------------------------------------


def test_safe_cleanup_holds_orphan_under_retention_rule(
    session: Session, data_dir
) -> None:
    """命中 active 规则的孤儿对象在保留期内不可清理(经 storage 边界读取规则)。"""
    user = _seed_user(session)
    _add_rule(session, user.id, retention_days=36500)
    orphan = _put(session, b"young-orphan" * 4)
    session.commit()

    plan = safe_cleanup(session, dry_run=True)

    assert plan["count"] == 0
    assert plan["retained_count"] == 1
    assert plan["retained"][0]["id"] == orphan.id
    assert plan["retained"][0]["retention_days"] == 36500


def test_safe_cleanup_releases_orphan_without_rule(session: Session, data_dir) -> None:
    """无命中规则时默认保留 0 天, 孤儿对象可清理(证明保留来自规则边界)。"""
    orphan = _put(session, b"no-rule-orphan" * 4)
    session.commit()

    plan = safe_cleanup(session, dry_run=True)

    assert plan["count"] == 1
    assert plan["retained_count"] == 0
    assert plan["candidates"][0]["id"] == orphan.id
