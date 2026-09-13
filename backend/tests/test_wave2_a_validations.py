"""Wave 2 W2-A: application/validations 用例测试。

覆盖：
- 用例可独立执行并提交事务（确认证据与报告在新会话可读）；
- 收敛后行为（关键路径：完整预检诊断码集合、财务基准确认；
  旧 services.validation 已删除，对照基准即用例自身在独立项目上的运行）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")

import pytest  # noqa: E402
from auth_helpers import make_user  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan.application.projects import lifecycle as projects_uc  # noqa: E402
from iesplan.application.validations import precheck as validations_uc  # noqa: E402
from iesplan.config import settings  # noqa: E402
from iesplan.db import Base  # noqa: E402


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(engine: Engine) -> Iterator[None]:
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def db_session(engine: Engine, tmp_path: Path) -> Iterator[Session]:
    settings.data_dir = tmp_path
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


def _new_session(engine: Engine) -> Session:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return factory()


BASELINE = {
    "baseline_resolution": "1h",
    "baseline_leap_year": False,
    "baseline_scenario_mode": "single",
}


def _codes(report) -> list[str]:
    return sorted(d.code for d in report.diagnostics)


def test_precheck_repeatable_consistent(db_session: Session) -> None:
    owner = make_user(db_session, "w2a_val_owner1")
    p_new = projects_uc.create_project(db_session, owner, "W2A 校验新", **BASELINE)
    p_old = projects_uc.create_project(db_session, owner, "W2A 校验旧", **BASELINE)

    new_report = validations_uc.validate_project(db_session, p_new.id)
    old_report = validations_uc.validate_project(db_session, p_old.id)
    assert new_report.status == old_report.status == "blocked"
    assert new_report.blocks_submit is True
    assert _codes(new_report) == _codes(old_report)
    # 关键诊断存在：未绑定数据 + 缺少财务基准确认 + 未保存配置警告
    assert "VALID-DATA-001" in _codes(new_report)
    assert "VALID-FIN-001" in _codes(new_report)
    assert "VALID-CONFIG-001" in _codes(new_report)


def test_baseline_confirm_flow_and_commit(engine: Engine, db_session: Session) -> None:
    owner = make_user(db_session, "w2a_val_owner2")
    project = projects_uc.create_project(db_session, owner, "W2A 确认项目", **BASELINE)

    before = validations_uc.validate_project(db_session, project.id)
    assert "VALID-FIN-001" in _codes(before)

    record = validations_uc.mark_baseline_confirmed(db_session, project.id, owner, {"note": "v1"})
    assert record.action == validations_uc.BASELINE_ACTION

    # 独立执行并提交事务：新会话可见确认证据，预检不再报 FIN-001
    with _new_session(engine) as fresh:
        after = validations_uc.validate_project(fresh, project.id)
        assert "VALID-FIN-001" not in _codes(after)

    # 报告持久化往返 + 提交证据
    stored = validations_uc.store_validation_report(db_session, project.id, after)
    assert "object_id" in stored
    with _new_session(engine) as fresh:
        latest = validations_uc.get_latest_validation_report(fresh, project.id)
        assert latest is not None
        assert latest["project_id"] == str(project.id)
        assert latest["status"] == after.status
