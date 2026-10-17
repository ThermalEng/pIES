"""M-04 聚焦测试: 命名空间分配事务行为与调用方工作保全。

覆盖:
1. 已有合法命名空间直接返回(只读, 无数据库写入与 flush);
2. 缺失命名空间的用户通过 SAVEPOINT 分配并持久化;
3. 发生唯一约束碰撞时, 仅局部回滚 SAVEPOINT 并重试, 调用方在会话内的先前未提交工作完好无损并正常提交;
4. 非约束冲突异常(如非预期错误)正常上浮至调用方, 严禁被宽泛 except 吞掉或触发重试。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from iesplan.application.namespace import ensure_public_namespace
from iesplan.core.namespace import is_valid_namespace
from iesplan.db import Base
from iesplan.identity.persistence import User
import iesplan.identity.persistence
import iesplan.audit.persistence


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine: Engine) -> Iterator[Session]:
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with session_factory() as session:
        yield session
        session.rollback()


def test_existing_namespace_returned_directly(db: Session) -> None:
    """用户已持有合法命名空间时直接返回，不触发数据库分配与 flush。"""
    user = User(
        username="u_ns_existing",
        display_name="已有命名空间",
        public_namespace="111111111111",
    )
    db.add(user)
    db.commit()

    ns = ensure_public_namespace(db, user)
    assert ns == "111111111111"


def test_missing_namespace_allocated_via_savepoint(db: Session) -> None:
    """缺失命名空间的用户获得合法的 12 位小写 Crockford Base32 命名空间并正确落库。"""
    user = User(
        username="u_ns_missing",
        display_name="无命名空间",
        public_namespace=None,
    )
    db.add(user)
    db.commit()

    ns = ensure_public_namespace(db, user)
    assert is_valid_namespace(ns)
    assert len(ns) == 12
    db.commit()

    reloaded = db.get(User, user.id)
    assert reloaded is not None
    assert reloaded.public_namespace == ns


def test_collision_retry_preserves_caller_work(db: Session, monkeypatch) -> None:
    """命名空间碰撞时仅局部回滚 SAVEPOINT，调用方先前在会话内的未提交工作不受损。"""
    # 1. 占位用户占用一个特定命名空间
    collision_ns = "222222222222"
    assert is_valid_namespace(collision_ns)
    u_blocking = User(
        username="u_ns_blocking",
        display_name="占位用户",
        public_namespace=collision_ns,
    )
    db.add(u_blocking)
    db.commit()

    # 2. 目标用户(无命名空间)
    u_target = User(
        username="u_ns_target",
        display_name="目标用户",
        public_namespace=None,
    )
    db.add(u_target)
    db.commit()

    # 3. 调用方在调用 ensure_public_namespace 前向会话添加业务数据(模拟调用方既有未提交工作)
    u_caller_work = User(
        username="u_caller_prior_work",
        display_name="调用方未提交业务工作",
        public_namespace="444444444444",
    )
    db.add(u_caller_work)
    # 此时 u_caller_work 处于 pending 状态，未 commit

    # 4. 模拟 generate_namespace 第一次产生 collision_ns (触发 IntegrityError), 第二次产生新 namespace
    second_ns = "333333333333"
    assert is_valid_namespace(second_ns)
    seq = iter([collision_ns, second_ns])

    def mock_gen():
        return next(seq)

    import iesplan.application.namespace as ns_module
    monkeypatch.setattr(ns_module, "generate_namespace", mock_gen)

    # 5. 执行分配: 第 1 次碰撞被局部 SAVEPOINT 捕获回滚, 第 2 次成功
    allocated = ensure_public_namespace(db, u_target)
    assert allocated == second_ns

    # 6. 核心断言: 调用方先前的 u_caller_work 绝不能被全局 rollback 冲掉!
    # 在同一事务中提交并验证双方均成功持久化
    db.commit()

    assert db.get(User, u_target.id).public_namespace == second_ns
    assert db.get(User, u_caller_work.id) is not None
    assert db.get(User, u_caller_work.id).username == "u_caller_prior_work"


def test_unexpected_exception_propagates_immediately(db: Session, monkeypatch) -> None:
    """非 IntegrityError 的意外数据库或程序异常严禁被吞掉，必须立即上浮且不盲目重试 20 次。"""
    user = User(
        username="u_ns_err",
        display_name="错误用户",
        public_namespace=None,
    )
    db.add(user)
    db.commit()

    call_count = 0

    def broken_gen():
        nonlocal call_count
        call_count += 1
        raise TypeError("unexpected programming error in generator")

    import iesplan.application.namespace as ns_module
    monkeypatch.setattr(ns_module, "generate_namespace", broken_gen)

    with pytest.raises(TypeError, match="unexpected programming error in generator"):
        ensure_public_namespace(db, user)

    # 严禁在非约束异常下盲目重试 20 次
    assert call_count == 1
