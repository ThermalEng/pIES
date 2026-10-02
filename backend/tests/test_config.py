"""配置层测试: 统一部署配置文件唯一权威 + Settings 单一类型化入口。

约定:
- 非秘密应用运行参数的默认值只在 backend/deploy/pies.toml 维护；
  程序常量、Compose 环境变量与前端均不复制；
- Settings 是唯一类型化入口：编排/秘密/角色走 IESPLAN_ 环境，
  应用运行值只从部署文件装配（环境变量不得覆盖）；
- 缺文件、非法 TOML、未知节/键、缺少必需项一律在构造时明确失败。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from iesplan.config import (
    DEFAULT_DEPLOY_CONFIG_FILE,
    Settings,
)

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


def test_defaults_from_deploy_file() -> None:
    """应用运行值与部署文件一致；编排/秘密默认值不变。"""
    s = Settings()
    assert s.db_url == "postgresql+psycopg://iesplan:iesplan_dev_password@localhost:5432/iesplan"
    assert s.redis_url == "redis://localhost:6379/0"
    assert s.data_dir == Path("/data")
    assert isinstance(s.data_dir, Path)
    assert s.secret_key == "dev-only-secret-change-me"
    assert s.app_url == "http://localhost:8080"
    assert s.worker_type == "compute"
    assert s.session_ttl_minutes == 480
    assert s.default_admin_password == "iesplan-admin-initial"
    assert s.storage_min_free_bytes == 2000000000
    assert s.debug is False
    # 部署文件唯一权威的值
    assert s.compute_slots == 2
    assert s.rate_limit_enabled is True
    assert s.rate_limit_window_seconds == 60
    assert s.rate_limit_max_requests == 1000
    assert s.rate_limit_exempt_paths == "/api/healthz,/api/readyz,/api/auth/login"
    assert s.upload_quota_bytes == 0
    assert s.project_quota_bytes == 0
    assert s.solver_max_timeout_seconds == 172800
    assert s.solver_max_memory_mib == 16384
    assert s.solver_max_cpu_cores == 8
    assert s.solver_allow_network is False
    assert s.solver_max_output_files == 64
    assert s.solver_max_output_bytes == 536870912
    assert tuple(s.solver_allowed_executables) == ("ies.solver.highs@1.7.2",)
    assert tuple(s.solver_allowed_environment_vars) == ()
    assert s.calc_time_limit_seconds == 43200
    assert not hasattr(s, "task_timeout_hours")


def test_sqlalchemy_url_property() -> None:
    """sqlalchemy_url 属性与 db_url 一致(供引擎构造使用)。"""
    s = Settings()
    assert s.sqlalchemy_url == s.db_url


def test_env_override_only_for_env_layer(monkeypatch) -> None:
    """编排/秘密/角色仍走环境；应用运行值不受环境变量覆盖。"""
    monkeypatch.setenv("IESPLAN_DB_URL", "postgresql+psycopg://u:p@db:5432/other")
    monkeypatch.setenv("IESPLAN_REDIS_URL", "redis://redis:6380/1")
    monkeypatch.setenv("IESPLAN_DATA_DIR", "/tmp/ies-data")
    monkeypatch.setenv("IESPLAN_DEBUG", "true")
    monkeypatch.setenv("IESPLAN_WORKER_TYPE", "io")
    monkeypatch.setenv("IESPLAN_COMPUTE_SLOTS", "8")
    monkeypatch.setenv("IESPLAN_SOLVER_MAX_TIMEOUT_SECONDS", "60")
    s = Settings()
    assert s.db_url == "postgresql+psycopg://u:p@db:5432/other"
    assert s.redis_url == "redis://redis:6380/1"
    assert s.data_dir == Path("/tmp/ies-data")
    assert s.debug is True
    assert s.worker_type == "io"
    assert s.compute_slots == 2
    assert s.solver_max_timeout_seconds == 172800


def test_deploy_file_override(tmp_path: Path) -> None:
    """挂载文件覆盖部署文件路径后，应用运行值随之调整。"""
    text = DEFAULT_DEPLOY_CONFIG_FILE.read_text(encoding="utf-8")
    override = tmp_path / "pies-override.toml"
    override.write_text(
        text.replace("compute_slots = 2", "compute_slots = 8"),
        encoding="utf-8",
    )
    s = Settings(deploy_config_file=override)
    assert s.deploy_config_file == override
    assert s.compute_slots == 8
    assert s.solver_max_timeout_seconds == 172800


def test_deploy_file_env_path(monkeypatch, tmp_path: Path) -> None:
    """IESPLAN_DEPLOY_CONFIG_FILE 指向挂载文件（仅路径可走环境）。"""
    text = DEFAULT_DEPLOY_CONFIG_FILE.read_text(encoding="utf-8")
    override = tmp_path / "pies-env.toml"
    override.write_text(
        text.replace("compute_slots = 2", "compute_slots = 4"),
        encoding="utf-8",
    )
    monkeypatch.setenv("IESPLAN_DEPLOY_CONFIG_FILE", str(override))
    assert Settings().compute_slots == 4


def test_missing_deploy_file_fails(tmp_path: Path) -> None:
    """缺文件在构造时明确失败，不回退默认值。"""
    with pytest.raises(ValueError, match="部署配置文件缺失"):
        Settings(deploy_config_file=tmp_path / "absent.toml")


def test_invalid_deploy_file_fails(tmp_path: Path) -> None:
    """非法 TOML / 未知键 / 缺必需项在构造时明确失败。"""
    bad_toml = tmp_path / "bad.toml"
    bad_toml.write_text("worker = [unclosed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="无法解析"):
        Settings(deploy_config_file=bad_toml)

    unknown_key = tmp_path / "unknown.toml"
    unknown_key.write_text(
        DEFAULT_DEPLOY_CONFIG_FILE.read_text(encoding="utf-8")
        + 'compute_slots_typo = 3\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="未知键"):
        Settings(deploy_config_file=unknown_key)

    missing_key = tmp_path / "missing.toml"
    missing_key.write_text(
        DEFAULT_DEPLOY_CONFIG_FILE.read_text(encoding="utf-8").replace(
            "compute_slots = 2\n", ""
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="缺少必需项"):
        Settings(deploy_config_file=missing_key)


def test_unnamed_env_not_read(monkeypatch) -> None:
    """无 IESPLAN_ 前缀的环境变量不影响配置。"""
    monkeypatch.setenv("DB_URL", "sqlite:///should-not-apply.db")
    monkeypatch.setenv("DEBUG", "true")
    s = Settings()
    assert s.db_url.startswith("postgresql+psycopg://")
    assert s.debug is False
