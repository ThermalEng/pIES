"""解耦重构切片 4:identity / dataset 域 persistence 实现测试。

- 直接覆盖 `iesplan.identity.persistence`(经 `iesplan.identity` 门面):
  用户/凭证/角色/会话/应用设置/认证事件/第三方 subject 绑定;
- 覆盖迁移后的 `iesplan.services.identity` 身份写入面:
  建用户/登录/改密/重置/窗口会话接管/撤销/续期/过期/停用/删除;
- 覆盖迁移后的 `iesplan.services.dataset` 数据集写入面(经 dataset 域):
  建集/取数/列表/版本上传/版本详情/样例生成/冲突与缺失语义;
- 运行环境与切片 3 一致:SQLite 内存库 + 临时 data_dir(对象存储)。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from test_dataset_api import make_csv

from iesplan import dataset as dataset_domain
from iesplan import identity as identity_domain
from iesplan import project as project_domain
from iesplan.config import settings
from iesplan.core.errors import ConflictError, NotFoundError
from iesplan.db import Base
from iesplan.identity.contracts import IdentityConflictError, UserNotFoundError
from iesplan.services import dataset as dataset_service
from iesplan.services import identity as identity_service


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


@pytest.fixture(autouse=True)
def _reset_rate_limit() -> Iterator[None]:
    identity_service.reset_login_rate_limit()
    yield
    identity_service.reset_login_rate_limit()


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(settings, "data_dir", d)
    return d


def _make_project(db: Session, name: str = "slice4-proj"):
    return project_domain.create_project(
        db,
        name=name,
        owner_id=7,
        created_by=7,
        baseline_resolution="1h",
        baseline_leap_year=False,
        baseline_scenario_mode="single",
    )


# ---------------------------------------------------------------------------
# identity 域:用户与凭证
# ---------------------------------------------------------------------------


def test_identity_user_crud_and_conflicts(db: Session) -> None:
    created = identity_domain.create_user(db, username="u1", display_name="U1", email="u1@example.com")
    db.commit()  # 请求边界: 后续冲突路径的回滚不得抹掉已提交的建置
    assert created.id > 0 and created.status == "active"
    # 注: 记录跨越写读往返时 SQLite 回读时间为 naive(已知测试环境差异),
    # 此处按稳定字段断言, 不做整记录相等比较(切片 3 同例)。
    for found in (
        identity_domain.get_user(db, created.id),
        identity_domain.get_user_by_username(db, "u1"),
        identity_domain.get_user_by_email(db, "u1@example.com"),
    ):
        assert found is not None and found.id == created.id
        assert found.username == "u1" and found.status == "active"
    assert identity_domain.get_user(db, 999999) is None
    assert [u.username for u in identity_domain.list_users(db)] == ["u1"]

    # 域函数不拥有事务: 预期冲突后由调用方回滚(服务层在各自边界内回滚)。
    with pytest.raises(IdentityConflictError):
        identity_domain.create_user(db, username="u1", display_name="dup")
    db.rollback()

    renamed = identity_domain.set_user_status(db, created.id, "disabled")
    assert renamed.status == "disabled"
    with pytest.raises(UserNotFoundError):
        identity_domain.set_user_status(db, 999999, "active")

    namespaced = identity_domain.set_public_namespace(db, created.id, "ns-001")
    assert namespaced.public_namespace == "ns-001"
    found_ns = identity_domain.get_user_by_namespace(db, "ns-001")
    assert found_ns is not None and found_ns.id == created.id
    other = identity_domain.create_user(db, username="u2", display_name="U2")
    db.commit()
    with pytest.raises(IdentityConflictError):
        identity_domain.set_public_namespace(db, other.id, "ns-001")
    db.rollback()

    touched = identity_domain.touch_login(db, created.id)
    assert touched.last_login_at is not None
    bumped = identity_domain.bump_credential_version(db, created.id)
    assert bumped.credential_version == 1


def test_identity_credential_lifecycle(db: Session) -> None:
    user = identity_domain.create_user(db, username="cu", display_name="CU")
    assert identity_domain.get_active_credential(db, user.id) is None
    assert identity_domain.get_active_password_secret(db, user.id) is None

    cred = identity_domain.add_credential(
        db,
        user_id=user.id,
        credential_type="password",
        secret_hash="hashed",
        algorithm="bcrypt",
        strength_score=80,
        requires_change=True,
    )
    assert cred.id > 0 and cred.requires_change is True
    active = identity_domain.get_active_credential(db, user.id)
    assert active is not None and active.id == cred.id
    # 记录永不携带密钥材料, 密钥只经专用访问器流出
    assert not hasattr(active, "secret_hash")
    assert identity_domain.get_active_password_secret(db, user.id) == "hashed"

    assert identity_domain.revoke_credentials(db, user.id) == 1
    assert identity_domain.get_active_credential(db, user.id) is None
    rotated = identity_domain.add_credential(
        db,
        user_id=user.id,
        credential_type="password",
        secret_hash="hashed-2",
        rotated_at="2026-01-02T00:00:00+00:00",
    )
    assert rotated.id > cred.id
    assert identity_domain.get_active_password_secret(db, user.id) == "hashed-2"


def test_identity_roles_and_settings_events(db: Session) -> None:
    user = identity_domain.create_user(db, username="ru", display_name="RU")
    role = identity_domain.ensure_role(db, "engineer", "工程师")
    assert identity_domain.ensure_role(db, "engineer", "工程师").id == role.id
    assert identity_domain.user_roles(db, user.id) == []
    identity_domain.grant_role(db, user_id=user.id, role_id=role.id, granted_by=user.id)
    assert identity_domain.user_roles(db, user.id) == ["engineer"]
    identity_domain.revoke_role(db, user_id=user.id, role_id=role.id, revoked_by=user.id)
    assert identity_domain.user_roles(db, user.id) == []

    assert identity_domain.get_app_setting(db, "site") is None
    saved = identity_domain.set_app_setting(db, "site", {"name": "pIES"}, updated_by=user.id)
    assert saved.value == {"value": {"name": "pIES"}}
    assert identity_domain.get_app_setting(db, "site") == saved

    event = identity_domain.record_auth_event(db, event_type="login_success", user_id=user.id, ip="127.0.0.1")
    assert event.id > 0 and event.event_type == "login_success"

    bound = identity_domain.bind_auth_subject(db, user.id, "oidc:sub-1")
    assert bound.auth_subject == "oidc:sub-1"
    found_sub = identity_domain.get_user_by_auth_subject(db, "oidc:sub-1")
    assert found_sub is not None and found_sub.id == user.id


def test_identity_session_lifecycle(db: Session) -> None:
    user = identity_domain.create_user(db, username="su", display_name="SU")
    assert identity_domain.list_active_sessions(db, user.id) == []

    created = identity_domain.create_session(
        db,
        user_id=user.id,
        token_hash="h" * 64,
        credential_version_at_issue=0,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    assert created.status == "active"
    for found in (
        identity_domain.get_session(db, created.id),
        identity_domain.get_session_by_token_hash(db, "h" * 64),
    ):
        assert found is not None and found.id == created.id
        assert found.status == "active" and found.user_id == user.id
    assert identity_domain.get_session_by_token_hash(db, "0" * 64) is None
    assert [s.id for s in identity_domain.list_active_sessions(db, user.id)] == [created.id]

    heartbeated = identity_domain.heartbeat_session(db, created.id)
    assert heartbeated.last_seen_at is not None
    extended = identity_domain.extend_session(db, created.id, expires_at="2099-06-01T00:00:00+00:00")
    assert extended.expires_at == "2099-06-01T00:00:00+00:00"

    revoked = identity_domain.set_session_status(
        db, created.id, "revoked", revoked_by=user.id, replaced_by_session_id=None
    )
    assert revoked.status == "revoked" and revoked.revoked_by == user.id
    assert identity_domain.list_active_sessions(db, user.id) == []
    with pytest.raises(UserNotFoundError):
        identity_domain.set_session_status(db, 999999, "revoked")

    stale = identity_domain.create_session(
        db,
        user_id=user.id,
        token_hash="s" * 64,
        credential_version_at_issue=0,
        expires_at="2000-01-01T00:00:00+00:00",
        status="takeover_pending",
    )
    assert identity_domain.expire_sessions(db, user.id) == 1
    assert identity_domain.get_session(db, stale.id).status == "expired"


# ---------------------------------------------------------------------------
# services.identity 写入面回归
# ---------------------------------------------------------------------------

_ADMIN_PASSWORD = "Admin12345"
_USER_PASSWORD = "Alice12345"


def _seed_admin(db: Session):
    return identity_service.create_user(
        db,
        "admin",
        _ADMIN_PASSWORD,
        role="admin",
        display_name="管理员",
        force_password_change=False,
    )


def _seed_user(db: Session, username: str = "alice"):
    return identity_service.create_user(
        db,
        username,
        _USER_PASSWORD,
        role="engineer",
        display_name=username.title(),
        force_password_change=False,
    )


def test_service_authenticate_change_and_reset_password(db: Session) -> None:
    _seed_admin(db)
    user = _seed_user(db)
    authed, error = identity_service.authenticate(db, "alice", _USER_PASSWORD)
    assert error is None and authed is not None and authed.id == user.id
    assert identity_service.authenticate(db, "alice", "wrong")[1] == "invalid_credentials"
    assert identity_service.authenticate(db, "ghost", "whatever")[1] == "invalid_credentials"

    identity_service.change_password(db, authed, _USER_PASSWORD, "NewPass12345")
    assert identity_service.authenticate(db, "alice", "NewPass12345")[0] is not None
    with pytest.raises(identity_service.BadOldPasswordError):
        identity_service.change_password(db, authed, "bad-old", "Another12345")

    admin = identity_service.get_user_by_username(db, "admin")
    assert admin is not None
    identity_service.reset_password(db, admin, authed, "TmpPass12345")
    assert identity_service.authenticate(db, "alice", "TmpPass12345")[0] is not None


def test_service_window_session_takeover_flow(db: Session) -> None:
    _seed_admin(db)
    _seed_user(db)
    authed, _ = identity_service.authenticate(db, "alice", _USER_PASSWORD)
    assert authed is not None

    s1, token1, displaced1 = identity_service.create_window_session(db, authed, "web")
    assert displaced1 is False and s1.status == "active"
    looked_up = identity_service.get_session_by_token(db, token1)
    assert looked_up is not None and looked_up.id == s1.id

    s2, _token2, displaced2 = identity_service.create_window_session(db, authed, "web")
    assert displaced2 is True and s2.status == "takeover_pending"
    assert identity_domain.get_session(db, s1.id).status == "revoked"

    kept = identity_service.confirm_takeover(db, authed, s2)
    assert kept.status == "active"
    refreshed = identity_service.extend_session(db, kept)
    assert refreshed is not None

    assert identity_service.revoke_all_user_sessions(db, authed, revoked_by=authed.id) == 1
    assert identity_service.get_session_by_token(db, token1) is not None  # 已撤销但仍可查
    assert identity_domain.get_session(db, kept.id).status == "revoked"


def test_service_session_expiry_and_revoke_helpers(db: Session) -> None:
    _seed_admin(db)
    _seed_user(db)
    authed, _ = identity_service.authenticate(db, "alice", _USER_PASSWORD)
    assert authed is not None
    session, _token, _ = identity_service.create_window_session(db, authed, "web")

    identity_domain.extend_session(db, session.id, expires_at="2000-01-01T00:00:00+00:00")
    assert identity_service.expire_sessions(db, authed.id) == 1
    assert identity_domain.get_session(db, session.id).status == "expired"

    session2, _token2, _ = identity_service.create_window_session(db, authed, "web")
    identity_service.expire_session(db, session2.id)
    assert identity_domain.get_session(db, session2.id).status == "expired"

    session3, _token3, _ = identity_service.create_window_session(db, authed, "web")
    identity_service.revoke_session_after_credential_change(db, session3.id, authed.id)
    revoked = identity_domain.get_session(db, session3.id)
    assert revoked.status == "revoked" and revoked.revoked_by == authed.id

    session4, _token4, _ = identity_service.create_window_session(db, authed, "web")
    before = identity_domain.get_session(db, session4.id).last_seen_at
    identity_service.touch_session(db, session4.id)
    assert identity_domain.get_session(db, session4.id).last_seen_at is not None
    assert before is not None


def test_service_create_user_duplicate_name_and_email(db: Session) -> None:
    _seed_admin(db)
    _seed_user(db)
    with pytest.raises(ConflictError):
        identity_service.create_user(db, "alice", "Another12345", role="engineer", display_name="dup")
    identity_service.create_user(
        db,
        "bob",
        "Bob12345X",
        role="engineer",
        display_name="Bob",
        email="bob@example.com",
    )
    with pytest.raises(ConflictError):
        identity_service.create_user(
            db,
            "bobby",
            "Bobby12345X",
            role="engineer",
            display_name="Bobby",
            email="BOB@EXAMPLE.COM",
        )


def test_service_user_admin_lifecycle(db: Session) -> None:
    admin = _seed_admin(db)
    user = _seed_user(db)

    identity_service.deactivate_user(db, admin, user)
    assert identity_service.get_user_by_id(db, user.id).status == "disabled"
    assert identity_service.authenticate(db, "alice", _USER_PASSWORD)[1] == "invalid_credentials"
    identity_service.reactivate_user(db, admin, user)
    assert identity_service.get_user_by_id(db, user.id).status == "active"

    preview = identity_service.preview_user_delete(db, admin, user)
    assert preview["user_id"] == user.id and preview["project_count"] == 0
    identity_service.verify_delete_confirm_token(db, user, preview["confirm_token"])
    result = identity_service.delete_user(
        db, admin, user, confirm=True, confirm_token=preview["confirm_token"]
    )
    assert result == {"deleted_projects": 0}
    assert identity_service.get_user_by_id(db, user.id).status == "disabled"


# ---------------------------------------------------------------------------
# services.dataset 写入面回归(经 dataset 域)
# ---------------------------------------------------------------------------


def _upload_meta() -> dict:
    return {
        "source_category": "user_upload",
        "license": "CC-BY-4.0",
        "provenance": {"source_category": "user_upload"},
        "created_reason": "upload",
    }


def test_service_dataset_crud_and_conflicts(db: Session, data_dir: Path) -> None:
    proj = _make_project(db)
    db.commit()  # 请求边界: 后续冲突路径的回滚不得抹掉已提交的建置
    created = dataset_service.create_dataset(db, proj.id, "ds1", license="CC-BY-4.0", description="d1")
    db.commit()
    assert created.id > 0 and created.status == "draft"
    assert created.default_license == "CC-BY-4.0"
    assert created.created_at is not None
    found = dataset_service.get_dataset(db, created.id)
    assert found is not None and found.id == created.id and found.name == "ds1"
    assert dataset_service.get_dataset(db, 999999) is None

    with pytest.raises(ConflictError):
        dataset_service.create_dataset(db, proj.id, "ds1")
    with pytest.raises(NotFoundError):
        dataset_service.create_dataset(db, 999999, "ghost")
    with pytest.raises(NotFoundError):
        dataset_service.require_project(db, 999999)
    dataset_service.require_project(db, proj.id)

    items = dataset_service.list_datasets_with_latest(db, proj.id)
    assert [item["dataset"].id for item in items] == [created.id]
    assert items[0]["latest_version"] is None
    with pytest.raises(NotFoundError):
        dataset_service.list_dataset_versions(db, 999999)
    assert dataset_service.list_dataset_versions(db, created.id) == []


def test_service_dataset_upload_and_version_detail(db: Session, data_dir: Path) -> None:
    proj = _make_project(db)
    db.commit()
    created = dataset_service.create_dataset(db, proj.id, "ds-upload")
    db.commit()
    csv_bytes = make_csv("1h", n=8760)

    v1 = dataset_service.upload_dataset_version(db, created.id, "1h", 480, {}, csv_bytes, _upload_meta())
    assert v1.version_no == 1 and v1.created_at is not None
    v2 = dataset_service.upload_dataset_version(db, created.id, "1h", 480, {}, csv_bytes, _upload_meta())
    assert v2.version_no == 2

    versions = dataset_service.list_dataset_versions(db, created.id)
    assert [v.version_no for v in versions] == [2, 1]

    summary = dataset_service.version_files_summary(db, v2.id)
    assert {f["file_kind"] for f in summary} == {"data", "metadata"}

    detail = dataset_service.get_dataset_version(db, created.id, None)
    assert detail["version"].version_no == 2
    assert {f["file_kind"] for f in detail["files"]} == {"data", "metadata"}
    assert detail["data"]["row_count"] == 8760
    detail_v1 = dataset_service.get_dataset_version(db, created.id, 1)
    assert detail_v1["version"].id == v1.id
    with pytest.raises(NotFoundError):
        dataset_service.get_dataset_version(db, created.id, 99)

    items = dataset_service.list_datasets_with_latest(db, proj.id)
    assert items[0]["latest_version"] is not None
    assert items[0]["latest_version"].version_no == 2

    dataset_domain.set_dataset_status(db, created.id, "deprecated")
    with pytest.raises(ConflictError):
        dataset_service.upload_dataset_version(db, created.id, "1h", 480, {}, csv_bytes, _upload_meta())


def test_service_dataset_builtin_sample(db: Session, data_dir: Path) -> None:
    proj = _make_project(db, "slice4-sample-proj")
    db.commit()
    v1 = dataset_service.create_builtin_sample(db, proj.id, "1h", region="beijing")
    assert v1.version_no == 1
    assert v1.provenance is not None and v1.provenance["region"] == "beijing"
    v2 = dataset_service.create_builtin_sample(db, proj.id, "1h", region="beijing")
    assert v2.version_no == 2
    items = dataset_service.list_datasets_with_latest(db, proj.id)
    assert len(items) == 1 and items[0]["latest_version"].version_no == 2
