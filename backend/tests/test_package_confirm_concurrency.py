"""导入确认并发幂等回归(PG 行锁领取)。

sqlite 无法表达跨事务行锁等待, 本模块只在 PG 下运行: 未设
IESPLAN_TEST_PG_URL 时整模块跳过(跳过不是通过, 不计入回归证据)。
Docker 证据见对应切片报告(自建隔离 PG, 用后清理)。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

PG_URL = os.environ.get("IESPLAN_TEST_PG_URL")

pytestmark = pytest.mark.skipif(not PG_URL, reason="需 PG 行锁并发环境(未设 IESPLAN_TEST_PG_URL)")

from auth_helpers import login_headers, make_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from test_package_configs import (  # noqa: E402
    _create_hist_version,
    _create_project,
    _export_zip,
    _save_planning,
    _setup_finance,
)

from iesplan.api import config_revisions as config_api  # noqa: E402
from iesplan.api import exports as exports_api  # noqa: E402
from iesplan.api import projects as projects_api  # noqa: E402
from iesplan.api.deps import get_request_db  # noqa: E402
from iesplan.application.packages import operations as packages_uc  # noqa: E402
from iesplan.audit.persistence import AuditLog  # noqa: E402, F401
from iesplan.config import settings  # noqa: E402
from iesplan.configuration.persistence import (  # noqa: E402, F401
    EffectiveFinanceRevision,
    FinanceOverridesRevision,
    FinanceProfile,
    PlanningConfigRevision,
)
from iesplan.dataset.persistence import Dataset, DatasetFile, DatasetVersion  # noqa: E402, F401
from iesplan.db import Base  # noqa: E402
from iesplan.identity.persistence import User  # noqa: E402, F401
from iesplan.main import create_app  # noqa: E402
from iesplan.package.persistence import ImportProposal  # noqa: E402, F401
from iesplan.project.persistence import Project  # noqa: E402, F401
from iesplan.results.persistence import EvidencePackage  # noqa: E402, F401
from iesplan.storage.persistence import ObjectRef, StoredObject  # noqa: E402, F401
from iesplan.tasks.persistence import Task  # noqa: E402, F401

# 与确认流无关的模型模板三表互为 FK, 在 PG 下 DDL 建/删排序成环(已知
# 现状, sqlite 仅警告; 本切片不动, 此处建删时排除, 确认流不触及它们)。
_CYCLE_TABLES = frozenset({
    "model_templates",
    "model_template_revisions",
    "model_template_draft_revisions",
})


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = create_engine(PG_URL)
    tables = [t for t in Base.metadata.tables.values() if t.name not in _CYCLE_TABLES]
    Base.metadata.create_all(eng, tables=tables)
    yield eng
    # PG 下 DROP 按约束名拆解成环三表之外的无名约束会失败, 此处只清空数据
    # (TRUNCATE CASCADE, 表保留); 隔离库随容器销毁。
    with eng.begin() as conn:
        names = ", ".join(f'"{t.name}"' for t in tables)
        conn.exec_driver_sql(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    eng.dispose()


@pytest.fixture()
def db(engine: Engine, tmp_path: Path) -> Iterator[Session]:
    settings.data_dir = tmp_path
    factory = sessionmaker(bind=engine)
    with factory() as session:
        yield session


@pytest.fixture()
def client(engine: Engine, db: Session, tmp_path: Path) -> Iterator[TestClient]:
    settings.data_dir = tmp_path
    app = create_app()
    app.include_router(projects_api.router)
    app.include_router(config_api.router)
    app.include_router(config_api.profile_router)
    app.include_router(exports_api.router)

    def _override_request_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_request_db] = _override_request_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _h(client: TestClient, user) -> dict[str, str]:
    return login_headers(client, user)


def test_concurrent_confirm_creates_single_project(
    client: TestClient, db: Session, engine: Engine
) -> None:
    """同一提案两事务同时确认: 只生成一个项目, 两调用返回同一身份。

    两线程各持独立 Session, 起跑门同步后同时进入确认; 领取行锁使后者
    等待前者提交, 等候者读到最新 applied 并重放同一项目(不重复创建)。
    """
    owner = make_user(db, "cc_owner")
    importer = make_user(db, "cc_importer")
    pid = _create_project(client, owner)
    _setup_finance(client, owner, pid)
    _save_planning(client, owner, pid)
    _create_hist_version(client, owner, pid, "cc-a")
    zip_bytes = _export_zip(client, owner, pid)
    resp = client.post(
        "/api/projects/import",
        files={"file": ("pkg.zip", zip_bytes, "application/zip")},
        headers=_h(client, importer),
    )
    assert resp.status_code == 201, resp.text
    prop_id = resp.json()["proposal"]["id"]

    factory = sessionmaker(bind=engine)
    barrier = threading.Barrier(2)
    outcomes: dict[int, tuple] = {}

    def _worker(index: int) -> None:
        session = factory()
        try:
            barrier.wait(timeout=60)
            project = packages_uc.confirm_import(session, importer, prop_id)
            outcomes[index] = ("ok", project.id)
        except Exception as exc:  # noqa: BLE001
            outcomes[index] = ("error", repr(exc))
        finally:
            session.close()

    threads = [threading.Thread(target=_worker, args=(i,)) for i in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)
    assert all(not thread.is_alive() for thread in threads)
    assert outcomes[0][0] == "ok", outcomes
    assert outcomes[1][0] == "ok", outcomes
    assert outcomes[0][1] == outcomes[1][1]

    listed = client.get("/api/projects", headers=_h(client, importer)).json()["projects"]
    assert [p["id"] for p in listed] == [outcomes[0][1]]
