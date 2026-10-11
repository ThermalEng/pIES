"""导入确认并发幂等回归(PG 行锁领取)与测试隔离证明。

sqlite 无法表达跨事务行锁等待, 本模块只在 PG 下运行: 未设
IESPLAN_TEST_PG_URL 时整模块跳过(跳过不是通过, 不计入回归证据)。

隔离实现(不代替注释): 每次运行在目标库内创建唯一临时 schema
(`pietest_` 前缀 + 随机标识), 全部连接经 search_path 固定到该 schema,
建表与数据只发生在自有 schema; 退出只删这个确切 schema, 不 TRUNCATE
目标库业务表、不 drop public、不读写其他 schema 的用户数据。失败路径
同样经 finally 清理自有资源并保留原始异常。Docker 证据见对应切片报告
(自建隔离 PG, 用后清理)。
"""

from __future__ import annotations

import contextlib
import os
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

PG_URL = os.environ.get("IESPLAN_TEST_PG_URL")

pytestmark = pytest.mark.skipif(not PG_URL, reason="需 PG 行锁并发环境(未设 IESPLAN_TEST_PG_URL)")

from auth_helpers import login_headers, make_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
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
# 现状, sqlite 仅警告; 本切片不动, 此处建表时排除, 确认流不触及它们)。
_CYCLE_TABLES = frozenset({
    "model_templates",
    "model_template_revisions",
    "model_template_draft_revisions",
})

_DECOY_TABLE = "_pietest_decoy"


class _PgCase:
    """一次隔离运行的句柄(自有 schema + 业务连接 + 管理连接)。"""

    def __init__(
        self,
        *,
        client: TestClient,
        db: Session,
        engine: Engine,
        admin_engine: Engine,
        schema: str,
        factory: sessionmaker,
    ) -> None:
        self.client = client
        self.db = db
        self.engine = engine
        self.admin_engine = admin_engine
        self.schema = schema
        self.factory = factory


def _public_tables(admin_engine: Engine) -> set[str]:
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ).all()
    return {row[0] for row in rows}


def _schema_exists(admin_engine: Engine, schema: str) -> bool:
    with admin_engine.connect() as conn:
        return bool(
            conn.execute(
                text("SELECT 1 FROM pg_namespace WHERE nspname = :schema"),
                {"schema": schema},
            ).first()
        )


@contextlib.contextmanager
def _pg_case(tmp_path: Path) -> Iterator[_PgCase]:
    """自建唯一 schema 的隔离环境; 退出只删自有 schema 并恢复 data_dir。

    诱饵表建在 public(模拟既有用户数据), 退出时一并只删自建的诱饵表;
    失败路径经 finally 同样清理, 原始异常不吞没。
    """
    schema = f"pietest_{uuid.uuid4().hex[:12]}"
    prev_data_dir = settings.data_dir
    admin_engine = create_engine(PG_URL)
    engine = create_engine(PG_URL, connect_args={"options": f"-c search_path={schema}"})
    settings.data_dir = tmp_path
    try:
        with admin_engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            conn.execute(
                text(
                    f"CREATE TABLE public.{_DECOY_TABLE} "
                    "(id SERIAL PRIMARY KEY, note TEXT NOT NULL)"
                )
            )
            conn.execute(
                text(f"INSERT INTO public.{_DECOY_TABLE} (note) VALUES ('decoy-keep')")
            )
        tables = [t for t in Base.metadata.tables.values() if t.name not in _CYCLE_TABLES]
        Base.metadata.create_all(engine, tables=tables)
        factory = sessionmaker(bind=engine)
        with factory() as session:
            app = create_app()
            app.include_router(projects_api.router)
            app.include_router(config_api.router)
            app.include_router(config_api.profile_router)
            app.include_router(exports_api.router)

            def _override_request_db() -> Iterator[Session]:
                yield session

            app.dependency_overrides[get_request_db] = _override_request_db
            with TestClient(app, raise_server_exceptions=False) as test_client:
                yield _PgCase(
                    client=test_client,
                    db=session,
                    engine=engine,
                    admin_engine=admin_engine,
                    schema=schema,
                    factory=factory,
                )
    finally:
        with admin_engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            conn.execute(text(f"DROP TABLE IF EXISTS public.{_DECOY_TABLE}"))
        engine.dispose()
        admin_engine.dispose()
        settings.data_dir = prev_data_dir


def _decoy_note(admin_engine: Engine) -> str | None:
    with admin_engine.connect() as conn:
        row = conn.execute(text(f"SELECT note FROM public.{_DECOY_TABLE}")).first()
    return row[0] if row else None


def _h(client: TestClient, user) -> dict[str, str]:
    return login_headers(client, user)


def _upload_proposal(client: TestClient, importer, zip_bytes: bytes) -> int:
    resp = client.post(
        "/api/projects/import",
        files={"file": ("pkg.zip", zip_bytes, "application/zip")},
        headers=_h(client, importer),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["proposal"]["id"]


def test_concurrent_confirm_creates_single_project(tmp_path: Path) -> None:
    """同一提案两事务同时确认: 只生成一个项目, 两调用返回同一身份。

    两线程各持独立 Session 同时进入 application 确认用例(非 HTTP 并发);
    领取行锁使后者等待前者提交, 等候者读到最新 applied 并重放同一项目
    (不重复创建)。诱饵行在全程保持不变。
    """
    with _pg_case(tmp_path) as case:
        client, db = case.client, case.db
        owner = make_user(db, "cc_owner")
        importer = make_user(db, "cc_importer")
        pid = _create_project(client, owner)
        _setup_finance(client, owner, pid)
        _save_planning(client, owner, pid)
        _create_hist_version(client, owner, pid, "cc-a")
        zip_bytes = _export_zip(client, owner, pid)
        prop_id = _upload_proposal(client, importer, zip_bytes)

        barrier = threading.Barrier(2)
        outcomes: dict[int, tuple] = {}

        def _worker(index: int) -> None:
            session = case.factory()
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
        assert _decoy_note(case.admin_engine) == "decoy-keep"


def test_pg_case_touches_only_own_schema(tmp_path: Path) -> None:
    """隔离证明: 业务写只进自有 schema, 退出只清自有范围。

    public 在进入时除自建诱饵表外无新增, 全程诱饵行不变; 退出后自有
    schema 与诱饵表均消失, 不动其他 schema 用户数据。
    """
    admin_engine = create_engine(PG_URL)
    try:
        public_before = _public_tables(admin_engine) | {_DECOY_TABLE}
        with _pg_case(tmp_path) as case:
            client, db = case.client, case.db
            assert _schema_exists(case.admin_engine, case.schema)
            owner = make_user(db, "iso_owner")
            importer = make_user(db, "iso_importer")
            pid = _create_project(client, owner)
            _create_hist_version(client, owner, pid, "iso-a")
            zip_bytes = _export_zip(client, owner, pid)
            prop_id = _upload_proposal(client, importer, zip_bytes)
            resp = client.post(
                f"/api/projects/import/{prop_id}/confirm", headers=_h(client, importer)
            )
            assert resp.status_code == 201, resp.text
            assert _decoy_note(case.admin_engine) == "decoy-keep"
            assert _public_tables(case.admin_engine) == public_before
            schema = case.schema
        assert not _schema_exists(admin_engine, schema)
        with admin_engine.connect() as conn:
            assert not conn.execute(
                text(
                    "SELECT 1 FROM pg_tables WHERE schemaname = 'public' "
                    f"AND tablename = '{_DECOY_TABLE}'"
                )
            ).first()
    finally:
        admin_engine.dispose()
