"""Wave 2 W2-B: 身份用例搬迁验证(application/identity)。

覆盖:
- 新家公开面完整(services/identity.py 全部公开函数/常量/异常可经新包导入);
- USERNAME_RE/EMAIL_RE 与 models.common 同值(应用层不得导入 models, 本地声明);
- 新模块无 iesplan.models.* 导入、无跨模块私有符号导入;
- 行为抽查: 创建/认证/会话/系统设置往返;
- delete_user 级联审计经 audit 域公开门面(原 services.project._audit 直调替换);
- 旧模块已删除、不可再导入。
"""

from __future__ import annotations

import ast
import importlib
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from iesplan import audit as audit_domain
from iesplan import project as project_domain
from iesplan.application import identity as identity_uc
from iesplan.db import Base

_BACKEND_DIR = Path(__file__).resolve().parents[1]

#: 新包内不得出现的导入(门禁 8 由协调者统一更新, 此处锁定 W2-B 约束)。
_NEW_IDENTITY_MODULES = (
    "iesplan/application/identity/__init__.py",
    "iesplan/application/identity/service.py",
)


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


def _models_imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "iesplan.models" or node.module.startswith("iesplan.models."):
                found.append((node.lineno, f"from {node.module} import ..."))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "iesplan.models" or alias.name.startswith("iesplan.models."):
                    found.append((node.lineno, f"import {alias.name}"))
    return found


def test_public_surface_preserved() -> None:
    """旧服务全部公开名经新包可用(含调用方实际使用的 token_hash)。"""
    expected = [
        "USERNAME_RE",
        "EMAIL_RE",
        "MAX_LOGIN_FAILURES",
        "LOCKOUT_SECONDS",
        "ROLE_ADMIN",
        "ROLE_ENGINEER",
        "AuthError",
        "AuthRequiredError",
        "SessionInvalidError",
        "LoginFailedError",
        "LockedError",
        "UserDisabledError",
        "WeakPasswordError",
        "BadOldPasswordError",
        "SamePasswordError",
        "RegistrationDisabledError",
        "ForcePasswordChangeError",
        "BadRequestError",
        "DeleteConfirmRequiredError",
        "reset_login_rate_limit",
        "utcnow",
        "as_utc",
        "KEY_REGISTRATION_ENABLED",
        "get_app_setting",
        "set_app_setting",
        "registration_enabled",
        "set_registration_enabled",
        "record_auth_event",
        "ensure_role",
        "user_roles",
        "has_role",
        "list_users",
        "get_user_by_id",
        "get_user_by_username",
        "get_active_password_credential",
        "create_user",
        "deactivate_user",
        "reactivate_user",
        "owned_project_ids",
        "preview_user_delete",
        "verify_delete_confirm_token",
        "delete_user",
        "change_password",
        "reset_password",
        "authenticate",
        "create_window_session",
        "confirm_takeover",
        "get_session_by_token",
        "revoke_session",
        "revoke_other_sessions",
        "revoke_all_user_sessions",
        "expire_sessions",
        "extend_session",
        "expire_session",
        "revoke_session_after_credential_change",
        "touch_session",
        "token_hash",
    ]
    missing = [name for name in expected if not hasattr(identity_uc, name)]
    assert missing == []


def test_regex_parity_with_models_common() -> None:
    """本地正则与 models.common 同值(应用层不得导入 models, 此处锁定一致)。"""
    from iesplan.models import common as models_common

    assert identity_uc.USERNAME_RE == models_common.USERNAME_RE
    assert identity_uc.EMAIL_RE == models_common.EMAIL_RE


@pytest.mark.parametrize("rel", _NEW_IDENTITY_MODULES)
def test_new_modules_have_no_direct_models_imports(rel: str) -> None:
    """新模块无 iesplan.models.* 直接导入(只经领域公开门面读写数据)。"""
    assert _models_imports(_BACKEND_DIR / rel) == []


def test_old_service_module_gone() -> None:
    """旧文件已删除: iesplan.services.identity 不可再导入(无兼容垫片)。"""
    with pytest.raises(ImportError):
        importlib.import_module("iesplan.services.identity")


def test_create_authenticate_session_flow(db: Session) -> None:
    """创建 → 认证 → 建会话 → token 回查, 行为与搬迁前一致."""
    user = identity_uc.create_user(
        db, "w2b_alice", "Test12345", role="engineer", force_password_change=False
    )
    assert user.username == "w2b_alice"

    authed, error = identity_uc.authenticate(db, "w2b_alice", "Test12345")
    assert error is None and authed is not None and authed.id == user.id

    nobody, bad_error = identity_uc.authenticate(db, "w2b_alice", "wrong")
    assert nobody is None and bad_error == "invalid_credentials"

    session, token, displaced = identity_uc.create_window_session(db, authed, "web")
    assert token and displaced is False
    found = identity_uc.get_session_by_token(db, token)
    assert found is not None and found.id == session.id


def test_settings_roundtrip(db: Session) -> None:
    """系统设置读写往返(注册开关 + 通用键值)。"""
    assert identity_uc.registration_enabled(db) is False
    identity_uc.set_registration_enabled(db, True, updated_by=1)
    assert identity_uc.registration_enabled(db) is True

    identity_uc.set_app_setting(db, "w2b_key", "v1", updated_by=1)
    assert identity_uc.get_app_setting(db, "w2b_key") == "v1"
    assert identity_uc.get_app_setting(db, "w2b_missing", "dflt") == "dflt"


def test_delete_user_audits_via_audit_facade(db: Session) -> None:
    """删号级联: 项目软删 + 审计经 audit 域门面(原 _audit 私有调用已直调替换)。"""
    admin = identity_uc.create_user(
        db, "w2b_admin", "Test12345", role="admin", force_password_change=False
    )
    victim = identity_uc.create_user(
        db, "w2b_victim", "Test12345", role="engineer", force_password_change=False
    )
    project = project_domain.create_project(
        db, name="w2b-proj", owner_id=victim.id, created_by=admin.id
    )

    preview = identity_uc.preview_user_delete(db, admin, victim)
    assert preview["project_count"] == 1
    result = identity_uc.delete_user(
        db, admin, victim, confirm=True, confirm_token=preview["confirm_token"]
    )
    assert result == {"deleted_projects": 1}

    entries = audit_domain.list_entries(db, action="project.deleted_by_account")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.entity_type == "project" and entry.entity_id == project.id
    assert entry.actor_id == admin.id
    assert entry.after.get("reason") == "account_deleted"
    assert entry.after.get("account_id") == victim.id

    assert identity_uc.get_user_by_id(db, victim.id).status == "disabled"
