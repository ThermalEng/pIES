"""Wave 1 W1-Model: model 域 persistence 收敛验证。

覆盖:
- application/model_templates/service.py、application/models/model_save.py 与
  application/models/service.py 不再直接导入 iesplan.models.* ORM
  (顶层与函数内均不允许; W2-B 搬迁后路径，旧 services/model.py 已删除);
- 模板草稿创建/发布与项目模型保存经 model 域 repository 读写, 行级结果与
  读取视图一致;
- 审计写入走 audit 域公开门面, after 载荷与旧直写一致。

测试环境: SQLite 内存 + 全表建表 + 版本化迁移(与 test_project_model_save 同构)。
"""

from __future__ import annotations

import ast
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

from iesplan import audit as audit_domain  # noqa: E402
from iesplan import model as model_domain  # noqa: E402
from iesplan import project as project_domain  # noqa: E402
from iesplan.application.model_templates import (  # noqa: E402
    create_template_draft,
    get_draft_revision,
    list_draft_revisions,
    publish_template,
)
from iesplan.application.models import save_project_model  # noqa: E402
from iesplan.application.projects import lifecycle as projects_uc  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.identity.persistence import User  # noqa: E402

_BACKEND_DIR = Path(__file__).resolve().parents[1]

#: 本波次收敛的三个文件: 应用层不得直接导入 iesplan.models.* ORM。
#: (W2-B 搬迁后路径; 旧 services/model.py 已删除。)
_NO_ORM_MODULES = (
    "iesplan/application/model_templates/service.py",
    "iesplan/application/models/model_save.py",
    "iesplan/application/models/service.py",
)

TEMPLATE_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.electric_load, names: {zh-CN: 电负荷, en-US: Electric Load}}
inputs:
  properties:
    peak_power_kw:
      value:
        type: number
        unit: kW
        valid_range: {minimum: 0, maximum: 1000}
        default: 100
properties:
  cop: {value: 3.0, unit: "1", valid_range: {minimum: 1, maximum: 10}}
interfaces:
  electric_demand:
    type: predefined
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: null}
    source: {mode: constant, value: 0}
equations:
  variables: {}
  relations: []
"""

DIRECT_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.electric_load, names: {zh-CN: 电负荷, en-US: Electric Load}}
properties:
  cop: {value: 3.0, unit: "1", valid_range: {minimum: 1, maximum: 10}}
  peak_power_kw: {value: 250, unit: kW, valid_range: {minimum: 0, maximum: 1000}}
interfaces:
  electric_demand:
    type: predefined
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: null}
    source: {mode: constant, value: 0}
equations:
  variables: {}
  relations: []
"""


def _orm_imports(path: Path) -> list[tuple[int, str]]:
    """源码 AST 中全部 iesplan.models.* 导入(顶层与函数内)。"""
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


@pytest.mark.parametrize("rel", _NO_ORM_MODULES)
def test_application_has_no_direct_orm_imports(rel: str):
    """应用/服务层三文件无 iesplan.models.* 直接导入(经域 repository 读写)。"""
    assert _orm_imports(_BACKEND_DIR / rel) == []


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    from iesplan.migrations import apply_migrations

    apply_migrations(eng)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(engine: Engine) -> Iterator[None]:
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def db_session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


@pytest.fixture()
def user(db_session: Session):
    """真实 ORM 用户行(命名空间分配需写行, 快照对象不可写)。"""
    snapshot = make_user(db_session, "w1model")
    return db_session.get(User, snapshot.id)


def test_template_lifecycle_reads_writes_via_model_repository(db_session: Session, user):
    """模板草稿/发布经 model 域 repository 落盘, 域读取视图与用例返回一致。"""
    from iesplan.application.namespace import ensure_public_namespace
    from iesplan.core.namespace import build_stable_id

    namespace = ensure_public_namespace(db_session, user)
    yaml_text = TEMPLATE_YAML.replace(
        "acme.device.electric_load", build_stable_id(namespace, "eload")
    )
    created = create_template_draft(
        db_session, user, model_yaml=yaml_text, slug="eload", description="w1"
    )
    template_id = created["template_id"]
    assert template_id == build_stable_id(namespace, "eload")

    stored = model_domain.get_owned_template(db_session, user.id, template_id)
    assert stored is not None
    assert stored.owner_id == user.id
    assert stored.slug == "eload"
    assert stored.draft_revision == 1
    assert stored.draft_yaml_object_id is not None
    assert created["id"] == str(stored.id)

    drafts = model_domain.list_draft_revisions(db_session, stored.id)
    assert [d.revision for d in drafts] == [1]
    assert drafts[0].yaml_object_id == stored.draft_yaml_object_id
    assert list_draft_revisions(db_session, user, template_id)[0]["revision"] == 1
    assert get_draft_revision(db_session, user, template_id, 1)["revision"] == 1

    published = publish_template(
        db_session, user, template_id, expected_revision=1, idempotency_key="w1-k1"
    )
    assert published["duplicate"] is False
    rev = model_domain.get_published_revision(db_session, stored.id, 1)
    assert rev is not None
    assert rev.yaml_object_id is not None
    assert published["revision"]["id"] == str(rev.id)

    replay = publish_template(
        db_session, user, template_id, expected_revision=1, idempotency_key="w1-k1"
    )
    assert replay["duplicate"] is True

    actions = {
        entry.action: entry.after
        for entry in audit_domain.list_entries(
            db_session, entity_type="model_template", entity_id=stored.id, limit=50
        )
    }
    assert actions["model_template.created"] == {
        "template_id": template_id, "draft_revision": 1,
    }
    assert actions["model_template.published"] == {
        "template_id": template_id, "revision": 1,
    }


def test_project_model_save_reads_writes_via_model_repository(db_session: Session, user):
    """项目模型保存经 model 域 repository 落盘, 审计经 audit 域门面。"""
    project = projects_uc.create_project(
        db_session,
        user,
        "w1 项目",
        baseline_resolution="1h",
        baseline_leap_year=False,
        baseline_scenario_mode="single",
    )
    draft = project_domain.get_current_draft(db_session, project.id)
    assert draft is not None

    result = save_project_model(
        db_session,
        user,
        project.id,
        model_yaml=DIRECT_YAML,
        expected_revision=draft.revision,
    )
    assert result["duplicate"] is False
    assert result["project_model"]["device_id"] == "acme.device.electric_load_1"

    model_id = int(result["project_model"]["id"])
    stored = model_domain.get_project_model(db_session, model_id)
    assert stored is not None
    assert stored.project_id == project.id
    assert stored.suffix == 1
    assert stored.base_device_id == "acme.device.electric_load"
    assert stored.project_revision == result["project_revision"]

    listed = model_domain.list_project_models(db_session, project.id, newest_first=True)
    assert [m.id for m in listed] == [model_id]

    entries = audit_domain.list_entries(
        db_session, entity_type="project_model", entity_id=model_id, limit=10
    )
    assert len(entries) == 1
    assert entries[0].action == "project_model.created"
    assert entries[0].after == {
        "project_id": project.id,
        "device_id": "acme.device.electric_load_1",
        "suffix": 1,
        "source": "direct_yaml",
        "template_id": None,
        "template_revision": None,
    }
