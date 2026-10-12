"""导入确认并发幂等回归(PG 行锁领取)与测试隔离证明。

sqlite 无法表达跨事务行锁等待, 本模块只在 PG 下运行: 未设
IESPLAN_TEST_PG_URL 时整模块跳过(跳过不是通过, 不计入回归证据)。

隔离实现(不代替注释): 每次运行在目标库内创建唯一临时 schema
(`pietest_` 前缀 + 随机标识), 全部业务连接经 search_path 固定到该
schema, 建表与数据只发生在自有 schema; 退出只删确切自有范围(仅当
CREATE 成功提交、已拥有才删), 不 TRUNCATE 目标库业务表、不 drop
public、不读写其他 schema 的用户数据。正常并发用例完全不碰 public;
隔离证明另用唯一自有 schema 模拟外部已有数据。清理各步骤独立执行,
清理失败不掩盖主异常。失败路径同样清理已拥有范围并保留原始异常。
Docker 证据见对应切片报告(自建隔离 PG, 用后清理)。
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import uuid
import warnings
from collections.abc import Callable, Iterator
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


def _guard(errors: list[str], label: str, fn: Callable[[], object]) -> None:
    """清理单步执行, 失败只记录不抛出(各步骤独立执行)。"""
    try:
        fn()
    except Exception as exc:
        errors.append(f"{label}: {exc!r}")


def _raise_cleanup(errors: list[str]) -> None:
    """无主异常时清理失败必须可见; 有主异常时只附加警告, 不替换主异常。"""
    if not errors:
        return
    message = f"测试清理失败: {'; '.join(errors)}"
    if sys.exception() is None:
        raise RuntimeError(message)
    warnings.warn(f"{message}(主异常保留)", stacklevel=3)


def _drop_schema(admin_engine: Engine, schema: str) -> None:
    with admin_engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def _schema_exists(admin_engine: Engine, schema: str) -> bool:
    with admin_engine.connect() as conn:
        return bool(
            conn.execute(
                text("SELECT 1 FROM pg_namespace WHERE nspname = :schema"),
                {"schema": schema},
            ).first()
        )


def _public_tables(admin_engine: Engine) -> set[str]:
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ).all()
    return {row[0] for row in rows}


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


@contextlib.contextmanager
def _pg_case(tmp_path: Path) -> Iterator[_PgCase]:
    """自建唯一 schema 的隔离环境; 退出只删已拥有的自有 schema 并恢复 data_dir。

    正常并发用例完全不创建/删除 public 资源。schema 仅在 CREATE 成功提交
    后才算已拥有, 失败不删除未拥有资源。
    """
    schema = f"pietest_{uuid.uuid4().hex[:12]}"
    prev_data_dir = settings.data_dir
    admin_engine = create_engine(PG_URL)
    engine = create_engine(PG_URL, connect_args={"options": f"-c search_path={schema}"})
    settings.data_dir = tmp_path
    owned_schema = False
    try:
        with admin_engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        owned_schema = True
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
        errors: list[str] = []
        if owned_schema:
            _guard(errors, f"drop schema {schema}", lambda: _drop_schema(admin_engine, schema))
        _guard(errors, "dispose engine", engine.dispose)
        _guard(errors, "dispose admin engine", admin_engine.dispose)
        settings.data_dir = prev_data_dir
        _raise_cleanup(errors)


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
    (不重复创建)。本用例不创建/删除 public 资源。
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


def test_pg_case_touches_only_own_schema(tmp_path: Path) -> None:
    """隔离证明: 业务写只进自有 schema, 退出只清各自拥有的范围。

    外层另建唯一自有 schema 模拟外部已有数据(public 不动); 业务 case
    退出后自有 schema 消失、外部诱饵原值仍在; 外层再删自己的确切诱饵
    schema。不用固定共享表名。
    """
    admin = create_engine(PG_URL)
    errors: list[str] = []
    decoy_schema = f"pietest_decoy_{uuid.uuid4().hex[:12]}"
    decoy_owned = False
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{decoy_schema}"'))
            conn.execute(
                text(f'CREATE TABLE "{decoy_schema}".decoy (id SERIAL PRIMARY KEY, note TEXT)')
            )
            conn.execute(text(f'INSERT INTO "{decoy_schema}".decoy (note) VALUES (\'decoy-keep\')'))
        decoy_owned = True
        public_before = _public_tables(admin)
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
            with admin.connect() as conn:
                assert conn.execute(
                    text(f'SELECT note FROM "{decoy_schema}".decoy')
                ).first()[0] == "decoy-keep"
            assert _public_tables(admin) == public_before
            schema = case.schema
        assert not _schema_exists(admin, schema)
        with admin.connect() as conn:
            assert conn.execute(
                text(f'SELECT note FROM "{decoy_schema}".decoy')
            ).first()[0] == "decoy-keep"
    finally:
        if decoy_owned:
            _guard(errors, f"drop decoy schema {decoy_schema}", lambda: _drop_schema(admin, decoy_schema))
        _guard(errors, "dispose admin", admin.dispose)
        _raise_cleanup(errors)


def test_pg_case_build_failure_cleans_owned_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """建表失败: 主异常原样抛出, 已拥有的自有 schema 仍被清理, 外层诱饵不变。"""
    admin = create_engine(PG_URL)
    errors: list[str] = []
    decoy_schema = f"pietest_decoy_{uuid.uuid4().hex[:12]}"
    decoy_owned = False
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{decoy_schema}"'))
            conn.execute(
                text(f'CREATE TABLE "{decoy_schema}".decoy (id SERIAL PRIMARY KEY, note TEXT)')
            )
            conn.execute(text(f'INSERT INTO "{decoy_schema}".decoy (note) VALUES (\'decoy-keep\')'))
        decoy_owned = True
        data_dir_before = settings.data_dir

        def _boom(*args, **kwargs):
            raise RuntimeError("injected build failure")

        monkeypatch.setattr(Base.metadata, "create_all", _boom)
        with pytest.raises(RuntimeError, match="injected build failure"):
            with _pg_case(tmp_path):
                pass  # pragma: no cover
        with admin.connect() as conn:
            assert conn.execute(
                text(
                    "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'pietest\\_%' "
                    "AND nspname != :decoy"
                ),
                {"decoy": decoy_schema},
            ).all() == []
            assert conn.execute(
                text(f'SELECT note FROM "{decoy_schema}".decoy')
            ).first()[0] == "decoy-keep"
        assert settings.data_dir == data_dir_before
    finally:
        if decoy_owned:
            _guard(errors, f"drop decoy schema {decoy_schema}", lambda: _drop_schema(admin, decoy_schema))
        _guard(errors, "dispose admin", admin.dispose)
        _raise_cleanup(errors)


def test_pg_case_cleanup_failure_stays_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清理失败: 无主异常时真实清理失败必须可见, settings 照常恢复。

    故障注入在自家清理步骤上(精确短路删 schema, 不碰全局执行路径)。
    """
    import test_package_confirm_concurrency as self_mod

    attempted: list[str] = []

    def _fail_on_drop(admin_engine: Engine, schema: str) -> None:
        attempted.append(schema)
        raise RuntimeError("injected drop failure")

    monkeypatch.setattr(self_mod, "_drop_schema", _fail_on_drop)
    data_dir_before = settings.data_dir
    with pytest.raises(RuntimeError, match="injected drop failure"):
        with _pg_case(tmp_path):
            pass
    assert settings.data_dir == data_dir_before
    # 精确清理本轮自建 schema(注入测试的已知遗留, 非宽泛打扫)。
    assert len(attempted) == 1
    with create_engine(PG_URL).begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{attempted[0]}" CASCADE'))


def test_pg_case_main_error_not_masked_by_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """主异常 + 清理失败: 主异常原样抛出不被替换, settings 照常恢复。"""
    import test_package_confirm_concurrency as self_mod

    attempted: list[str] = []

    def _fail_on_drop(admin_engine: Engine, schema: str) -> None:
        attempted.append(schema)
        raise RuntimeError("injected drop failure")

    monkeypatch.setattr(self_mod, "_drop_schema", _fail_on_drop)
    data_dir_before = settings.data_dir
    with pytest.raises(RuntimeError, match="injected main failure"):
        with _pg_case(tmp_path):
            raise RuntimeError("injected main failure")
    assert settings.data_dir == data_dir_before
    assert len(attempted) == 1
    with create_engine(PG_URL).begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{attempted[0]}" CASCADE'))
