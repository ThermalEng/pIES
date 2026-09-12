"""Wave 2 W2-A: application/datasets 用例测试。

覆盖：用例可独立执行并提交事务（新会话可读回写入）；
关键路径：创建/上传版本/内置样例/拒绝回滚。
（旧 services.dataset 已删除，等价对照测试已随之删除。）
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")

import pytest  # noqa: E402
from auth_helpers import make_user  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan.application.datasets import lifecycle as datasets_uc  # noqa: E402
from iesplan.application.projects import lifecycle as projects_uc  # noqa: E402
from iesplan.config import settings  # noqa: E402
from iesplan.core.errors import NotFoundError  # noqa: E402
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


def _make_csv(n: int = 8760) -> bytes:
    lines = ["timestamp,e_load,h_load,c_load,t_ambient,ghi,electricity_price,grid_emission_factor"]
    t0 = datetime(2025, 1, 1)
    for i in range(n):
        ts = (t0 + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M")
        lines.append(f"{ts},100.0,50.0,20.0,20.0,300.0,0.6,0.581")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _make_project(db: Session, owner, name: str) -> int:
    project = projects_uc.create_project(db, owner, name, **BASELINE)
    return project.id


def test_create_upload_commits(engine: Engine, db_session: Session) -> None:
    owner = make_user(db_session, "w2a_ds_owner1")
    pid = _make_project(db_session, owner, "W2A 数据集项目")
    dataset = datasets_uc.create_dataset(db_session, pid, "负荷数据", user_id=owner.id)
    version = datasets_uc.upload_dataset_version(
        db_session, dataset.id, "1h", 480, {}, _make_csv(), {}, user_id=owner.id
    )
    assert version.version_no == 1
    assert version.quality_report["has_blocking_errors"] is False

    # 独立执行并提交事务：新会话可读回
    with _new_session(engine) as fresh:
        got = datasets_uc.get_dataset_version(fresh, dataset.id)
        assert got["version"].version_no == 1
        assert got["data"]["row_count"] == 8760
        files = datasets_uc.version_files_summary(fresh, got["version"].id)
        assert {f["file_kind"] for f in files} == {"data", "metadata"}


def test_builtin_sample(engine: Engine, db_session: Session) -> None:
    owner = make_user(db_session, "w2a_ds_owner3")
    pid = _make_project(db_session, owner, "W2A 样例项目")
    version = datasets_uc.create_builtin_sample(db_session, pid, "1h", user_id=owner.id)
    assert version.version_no == 1
    with _new_session(engine) as fresh:
        versions = datasets_uc.list_dataset_versions(fresh, version.dataset_id)
        assert len(versions) == 1
        assert versions[0].quality_report["has_blocking_errors"] is False


def test_invalid_csv_rejected_and_rolled_back(engine: Engine, db_session: Session) -> None:
    from iesplan.application.datasets import DataValidationError

    owner = make_user(db_session, "w2a_ds_owner4")
    pid = _make_project(db_session, owner, "W2A 拒绝项目")
    dataset = datasets_uc.create_dataset(db_session, pid, "坏数据集", user_id=owner.id)
    with pytest.raises(DataValidationError):
        datasets_uc.upload_dataset_version(
            db_session, dataset.id, "1h", 480, {}, b"timestamp,e_load\n2025-01-01 00:00,1.0\n",
            {}, user_id=owner.id,
        )
    # 回滚证据：新会话中该数据集无版本
    with _new_session(engine) as fresh:
        assert datasets_uc.list_dataset_versions(fresh, dataset.id) == []
    with pytest.raises(NotFoundError):
        datasets_uc.list_dataset_versions(db_session, 999999)
