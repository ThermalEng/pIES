"""配置层测试: Settings 默认值与环境变量覆盖。"""

from __future__ import annotations

from pathlib import Path

import pytest

from iesplan.config import Settings

#: compose 服务环境会注入 IESPLAN_* 变量(如 IESPLAN_DB_URL 指向 postgres),
#: 测试默认值前须先清除,保证默认值断言不受部署环境影响。
_IESPLAN_ENV_PREFIX = "IESPLAN_"


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除环境中的 IESPLAN_* 变量,保证测试环境的确定性。"""
    import os

    for key in list(os.environ):
        if key.startswith(_IESPLAN_ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)


def test_defaults() -> None:
    """默认值与契约一致。"""
    s = Settings()
    assert s.db_url == "postgresql+psycopg://iesplan:iesplan_dev_password@localhost:5432/iesplan"
    assert s.redis_url == "redis://localhost:6379/0"
    assert s.data_dir == Path("/data")
    assert isinstance(s.data_dir, Path)
    assert s.secret_key == "dev-only-secret-change-me"
    assert s.app_url == "http://localhost:8080"
    assert s.worker_type == "compute"
    assert s.compute_slots == 2
    assert s.task_timeout_hours == 8
    assert s.session_ttl_minutes == 480
    assert s.default_admin_password == "iesplan-admin-initial"
    assert s.storage_min_free_bytes == 2000000000
    assert s.debug is False


def test_sqlalchemy_url_property() -> None:
    """sqlalchemy_url 属性与 db_url 一致(供引擎构造使用)。"""
    s = Settings()
    assert s.sqlalchemy_url == s.db_url


def test_env_override(monkeypatch) -> None:
    """IESPLAN_ 前缀环境变量覆盖对应字段。"""
    monkeypatch.setenv("IESPLAN_DB_URL", "postgresql+psycopg://u:p@db:5432/other")
    monkeypatch.setenv("IESPLAN_REDIS_URL", "redis://redis:6380/1")
    monkeypatch.setenv("IESPLAN_DATA_DIR", "/tmp/ies-data")
    monkeypatch.setenv("IESPLAN_COMPUTE_SLOTS", "8")
    monkeypatch.setenv("IESPLAN_DEBUG", "true")
    monkeypatch.setenv("IESPLAN_WORKER_TYPE", "io")
    s = Settings()
    assert s.db_url == "postgresql+psycopg://u:p@db:5432/other"
    assert s.redis_url == "redis://redis:6380/1"
    assert s.data_dir == Path("/tmp/ies-data")
    assert s.compute_slots == 8
    assert s.debug is True
    assert s.worker_type == "io"


def test_unnamed_env_not_read(monkeypatch) -> None:
    """无 IESPLAN_ 前缀的环境变量不影响配置。"""
    monkeypatch.setenv("DB_URL", "sqlite:///should-not-apply.db")
    monkeypatch.setenv("DEBUG", "true")
    s = Settings()
    assert s.db_url.startswith("postgresql+psycopg://")
    assert s.debug is False
