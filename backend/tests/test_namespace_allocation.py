"""M-04: 命名空间创建时分配聚焦测试(公开用例)。

覆盖新账号命名空间契约:
1. 身份域公开分配函数: 直接返回合法且未被占用的命名空间值;
2. 内置 admin 种子: 首次种子携带合法命名空间, 重复种子幂等不变;
3. 注册/管理建号用例(application.identity.create_user)创建时分配并落库;
4. OIDC JIT 建号(iesplan.identity.provision_user)创建时分配并落库;
5. 数据集默认操作者既有创建分支(default_user)携带命名空间, 重复调用复用同一账号。

测试环境: SQLite 内存 + 全表建表(空库 create_all, 无版本化迁移)。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")

import pytest  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan import identity as identity_domain  # noqa: E402
from iesplan.application import identity as identity_uc  # noqa: E402
from iesplan.application.datasets import default_user  # noqa: E402
from iesplan.core.namespace import is_valid_namespace  # noqa: E402
from iesplan.db import Base  # noqa: E402


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


def test_allocate_public_namespace_returns_unused_value(db: Session) -> None:
    """公开分配函数: 无需既有用户即返回合法且未被占用的命名空间值。"""
    ns = identity_domain.allocate_public_namespace(db)
    assert is_valid_namespace(ns)
    assert identity_domain.get_user_by_namespace(db, ns) is None


def test_seed_builtin_admin_allocates_stable_namespace(db: Session) -> None:
    """内置 admin 种子: 首次创建携带合法命名空间, 重复种子幂等不变。"""
    identity_uc.seed_builtin_admin(db)
    admin = identity_uc.get_user_by_username(db, "admin")
    assert admin is not None
    first = admin.public_namespace
    assert is_valid_namespace(first)

    identity_uc.seed_builtin_admin(db)
    again = identity_uc.get_user_by_username(db, "admin")
    assert again is not None
    assert again.id == admin.id
    assert again.public_namespace == first


def test_registration_allocates_namespace(db: Session) -> None:
    """注册用例(application.identity.create_user): 创建时分配并落库。"""
    user = identity_uc.create_user(
        db, "ns_reg_user", "Test12345", force_password_change=False
    )
    assert is_valid_namespace(user.public_namespace)
    reloaded = identity_domain.get_user(db, user.id)
    assert reloaded.public_namespace == user.public_namespace


def test_oidc_provision_allocates_namespace(db: Session) -> None:
    """OIDC JIT 建号: 创建时分配并落库。"""
    claims = {"sub": "ns-oidc-subject", "name": "外部用户", "email": "ns_oidc@example.com"}
    user = identity_domain.provision_user(db, claims)
    assert is_valid_namespace(user.public_namespace)
    db.commit()
    reloaded = identity_domain.get_user(db, user.id)
    assert reloaded.public_namespace == user.public_namespace


def test_default_user_creation_branch_carries_namespace(db: Session) -> None:
    """默认操作者创建分支: 无 admin 时建号并携带命名空间, 重复调用复用同一账号。"""
    user = default_user(db)
    assert user.username == "admin"
    assert is_valid_namespace(user.public_namespace)

    again = default_user(db)
    assert again.id == user.id
    assert again.public_namespace == user.public_namespace
