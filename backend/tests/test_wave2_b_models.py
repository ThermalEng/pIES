"""Wave 2 W2-B: 模型用例搬迁验证(application/models)。

覆盖:
- 新家公开面完整(services/model.py 全部能力 + model_save 用例经新包导入);
- 新模块无 iesplan.models.* 导入;
- 行为抽查: 设备创建/连接/视图/断开经新家往返;
- 旧模块已删除、不可再导入;
- model_save 新家可导入, projects 旧导出路径过渡性可用(Wave 3 移除)。
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

from iesplan import project as project_domain
from iesplan.application import models as models_uc
from iesplan.config import settings
from iesplan.db import Base

_BACKEND_DIR = Path(__file__).resolve().parents[1]

#: 新包内不得出现的导入(门禁 8 由协调者统一更新, 此处锁定 W2-B 约束)。
_NEW_MODELS_MODULES = (
    "iesplan/application/models/__init__.py",
    "iesplan/application/models/service.py",
    "iesplan/application/models/model_save.py",
)

LOAD = "ies.device.electric_load"
GRID = "ies.device.grid_connection"


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


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(settings, "data_dir", d)
    return d


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
    """旧服务全部公开名 + model_save 用例名经新包可用."""
    expected = [
        "CARRIER_PORT_TYPE",
        "CONN_TYPE_BY_PORT",
        "CONN_ENERGY_MISMATCH",
        "CONN_DIRECTION_INVALID",
        "CONN_CROSS_PROJECT",
        "CONN_DUPLICATE",
        "CONN_SELF_LOOP",
        "ModelValidationError",
        "NotFoundError",
        "get_or_create_working_graph",
        "sync_draft_content",
        "validate_device_params",
        "create_device",
        "update_device",
        "delete_device",
        "get_device_ports",
        "connect",
        "disconnect",
        "update_connection",
        "serialize_device",
        "serialize_port",
        "serialize_connection",
        "get_graph",
        "validate_topology",
        "validate_project_model",
        "FINAL_OWNER_NAMESPACE",
        "ModelCandidateRejectedError",
        "save_project_model",
        "validate_candidate",
        "delete_project_model",
        "get_project_models",
        "project_model_to_dict",
    ]
    missing = [name for name in expected if not hasattr(models_uc, name)]
    assert missing == []


def test_carrier_constants_unchanged() -> None:
    """载体/端口/连接类型映射与搬迁前一致."""
    assert models_uc.CARRIER_PORT_TYPE == {
        "electricity": "electric",
        "heat": "thermal",
        "cool": "cooling",
        "gas": "fuel",
    }
    assert models_uc.CONN_TYPE_BY_PORT["electric"] == "electric_line"
    assert models_uc.CONN_CROSS_PROJECT == "CONN-PORT-003"
    assert models_uc.CONN_DUPLICATE == "CONN-DUP-001"


@pytest.mark.parametrize("rel", _NEW_MODELS_MODULES)
def test_new_modules_have_no_direct_models_imports(rel: str) -> None:
    """新模块无 iesplan.models.* 直接导入(只经领域公开门面读写数据)。"""
    assert _models_imports(_BACKEND_DIR / rel) == []


def test_old_service_module_gone() -> None:
    """旧文件已删除: iesplan.services.model 不可再导入(无兼容垫片)。"""
    with pytest.raises(ImportError):
        importlib.import_module("iesplan.services.model")


def test_device_connection_flow_via_new_home(db: Session, data_dir: Path) -> None:
    """设备创建 → 连接 → 视图 → 断开, 经新家往返行为一致."""
    project = project_domain.create_project(db, name="w2b-flow", owner_id=7, created_by=7)

    src = models_uc.create_device(db, project.id, GRID, "G1", created_by=7)
    dst = models_uc.create_device(db, project.id, LOAD, "L1", created_by=7)
    src_ports = {p.name: p for p in models_uc.get_device_ports(db, src.id)}
    dst_ports = {p.name: p for p in models_uc.get_device_ports(db, dst.id)}
    pair = next(
        (
            (o, i)
            for o in src_ports.values()
            if o.direction == "out"
            for i in dst_ports.values()
            if i.direction == "in" and i.port_type == o.port_type
        ),
        None,
    )
    assert pair is not None, "需要一对同载体源/汇端口构造连接"

    conn = models_uc.connect(db, project.id, pair[0].id, pair[1].id)
    assert conn.graph_id == src.graph_id
    with pytest.raises(models_uc.ModelValidationError) as exc:
        models_uc.connect(db, project.id, pair[0].id, pair[1].id)
    assert exc.value.code == models_uc.CONN_DUPLICATE

    view = models_uc.get_graph(db, project.id)
    assert view["has_graph"] is True
    assert any(c["id"] == conn.id for c in view["connections"])

    models_uc.disconnect(db, project.id, conn.id)
    assert models_uc.get_graph(db, project.id)["connections"] == []


def test_model_save_new_home_and_transitional_projects_export() -> None:
    """model_save 归属 models; projects 旧导出过渡性可用(Wave 3 移除)。"""
    from iesplan.application.models import save_project_model as new_home
    from iesplan.application.models.model_save import save_project_model as direct

    assert new_home is direct

    from iesplan.application.projects import save_project_model as legacy

    assert legacy is direct
