"""Wave 1 W1-Package 聚焦测试: package 域收敛与 services.package 去直访。

- services/package.py 不再导入 iesplan.models.*（5 项门禁白名单消除对象）,
  不再跨服务调用 services.audit/project（仅保留 Wave 2 的 config 编排）;
- import_proposals 读写经 package 域 repository（状态机 + 记录视图）;
- project_name_exists 供导入命名去重。
"""

from __future__ import annotations

import ast
import os
from collections.abc import Iterator
from pathlib import Path

# 单文件运行时的安全网: 固定 SQLite + 内存队列, 避免误连部署 Postgres/Redis
os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")

import pytest  # noqa: E402
from auth_helpers import make_user  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan import package as package_domain  # noqa: E402
from iesplan import project as project_domain  # noqa: E402
from iesplan.config import settings  # noqa: E402
from iesplan.core.errors import ConflictError  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.package.contracts import PackageConflictError  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parents[1]
PACKAGE_SERVICE_SRC = (BACKEND_DIR / "iesplan" / "services" / "package.py").read_text(encoding="utf-8")


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
def _clean_state(engine: Engine) -> Iterator[None]:
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def db(engine: Engine, tmp_path: Path) -> Iterator[Session]:
    settings.data_dir = tmp_path
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


def _model_and_service_imports() -> tuple[set[str], set[str]]:
    """解析 services/package.py 的跨表 ORM 与跨服务导入。"""
    tree = ast.parse(PACKAGE_SERVICE_SRC)
    models: set[str] = set()
    services: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "iesplan.models" or node.module.startswith("iesplan.models."):
                models.add(node.module)
            elif node.module == "iesplan.services":
                services.update(f"iesplan.services.{a.name}" for a in node.names)
            elif node.module.startswith("iesplan.services."):
                services.add(node.module)
    return models, services


def test_no_cross_model_imports_in_package_service() -> None:
    """services.package 零 iesplan.models.* 导入（门禁 5 项消除对象）。"""
    models, _ = _model_and_service_imports()
    assert models == set(), f"services.package 仍有跨表 ORM 导入: {sorted(models)}"


def test_no_cross_service_calls_except_config_orchestration() -> None:
    """services.package 不再调用 services.audit/project。

    财务配置读写编排（register/set/save/get）归 Wave 2 application 用例，
    本波次仅保留 services.config_revisions 调用。
    """
    _, services = _model_and_service_imports()
    assert services == {"iesplan.services.config_revisions"}, (
        f"services.package 仍有非预期跨服务调用: {sorted(services)}"
    )


def test_proposal_lifecycle_through_domain(db: Session) -> None:
    """导入提案经 package 域创建/读取/评审推进，终态不可再迁移。"""
    user = make_user(db, "pkg_owner")
    project = project_domain.create_project(
        db,
        name="包提案测试项目",
        owner_id=user.id,
        created_by=user.id,
        baseline_resolution="1h",
        baseline_leap_year=False,
        baseline_scenario_mode="single",
    )
    created = package_domain.create_proposal(
        db,
        project_id=project.id,
        proposer_id=user.id,
        source_type="json",
        source_object_id=None,
    )
    assert created.status == "proposed"
    assert created.created_at, "提案记录应携带创建时间"
    fetched = package_domain.get_proposal(db, created.id)
    assert fetched is not None and fetched.id == created.id
    reviewed = package_domain.set_proposal_review(
        db,
        created.id,
        status="proposed",
        review_summary={"checks": {"zip_ok": True}},
        review_errors={},
    )
    assert reviewed.review_summary == {"checks": {"zip_ok": True}}
    applied = package_domain.set_proposal_review(db, created.id, status="applied", decided_by=user.id)
    assert applied.status == "applied"
    assert applied.decided_by == user.id
    assert applied.decided_at, "applied 应记录确认时间"
    with pytest.raises(PackageConflictError):
        package_domain.set_proposal_review(db, created.id, status="proposed")
    assert isinstance(PackageConflictError("x"), ConflictError)
    assert package_domain.get_proposal(db, 999999999) is None


def test_project_name_exists_for_import_dedup(db: Session) -> None:
    """project_name_exists 与名称唯一约束同口径（含软删行）。"""
    user = make_user(db, "pkg_namer")
    assert project_domain.project_name_exists(db, "导入项目") is False
    project_domain.create_project(
        db,
        name="导入项目",
        owner_id=user.id,
        created_by=user.id,
        baseline_resolution="1h",
        baseline_leap_year=False,
        baseline_scenario_mode="single",
    )
    assert project_domain.project_name_exists(db, "导入项目") is True
    assert project_domain.project_name_exists(db, "导入项目 (导入 2)") is False
