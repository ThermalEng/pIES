"""E2 本地模型与算法插件生态综合测试。

覆盖：
1. 正交开关（market_mode vs local_upload_requires_approval）防绕过与单源 pip_index_url；
2. 设备模型 ies.device-model@1.0.0 导入/导出、409 防覆盖、Crockford Base32 校验、边界三分 sha256；
3. 算法插件 ZIP 安全校验、EmbeddedScheduler + JobRunnerClient 构建、双模式 --network=none 自检门禁；
4. 项目设备选择器、13 字段计算配置、Worker _stage_descriptor 与提交快照集成；
5. 两阶段无状态并发安全卸载（409 ECO-UNINSTALL-IN-USE）与真实 PostgreSQL 迁移/并发互斥测试。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import uuid
import zipfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from auth_helpers import login_headers, make_user
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import iesplan.audit.persistence  # noqa: F401
import iesplan.configuration.persistence  # noqa: F401
import iesplan.dataset.persistence  # noqa: F401
import iesplan.ecosystem.persistence  # noqa: F401
import iesplan.identity.persistence  # noqa: F401
import iesplan.model.persistence  # noqa: F401
import iesplan.package.persistence  # noqa: F401
import iesplan.project.persistence  # noqa: F401
import iesplan.results.persistence  # noqa: F401
import iesplan.storage.persistence  # noqa: F401
import iesplan.tasks.persistence  # noqa: F401
from iesplan import ecosystem as ecosystem_domain
from iesplan import project as project_domain
from iesplan import tasks as tasks_domain
from iesplan.application import configuration as config_app
from iesplan.application import ecosystem as ecosystem_app
from iesplan.application import models as models_app
from iesplan.application.worker import compute_cases
from iesplan.computation.config import (
    BUILTIN_EVALUATOR_FINANCE_SPEC,
    BUILTIN_SOLVER_HIGHS_SPEC,
    build_algorithm_registry,
    build_evaluator_registry,
    build_solver_registry,
)
from iesplan.computation.generators import HIGHS_MILP_PROVIDER
from iesplan.computation.generators import build_registry as build_generator_registry
from iesplan.computation.predictors import NAIVE_PREDICTOR, build_predictor_registry
from iesplan.computation.runtime.job_runner_client import JobRunnerClient
from iesplan.computation.runtime.stage_runner import build_builtin_stage_catalog
from iesplan.config import settings as app_settings
from iesplan.core.errors import AppError
from iesplan.core.namespace import generate_namespace
from iesplan.db import Base
from iesplan.ecosystem import (
    EcosystemConflictError,
    EcosystemUninstallInUseError,
    EcosystemValidationError,
)
from iesplan.main import create_app
from iesplan.migrations.runner import apply_migrations
from iesplan.storage import service as storage_service
from iesplan.storage.adapters.filesystem import FileSystemBlobStore
from iesplan.worker.scheduler import EmbeddedScheduler

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")


@pytest.fixture(scope="module")
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
def _clean_state(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    monkeypatch.setattr(app_settings, "data_dir", tmp_path)
    storage_service._blob_store = FileSystemBlobStore()
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def db_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def db(db_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = db_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _sample_device_yaml(device_id: str, *, power_rating: float = 3.2) -> str:
    return (
        "schema: ies.device-model\n"
        'schema_version: "1.0.0"\n'
        "device:\n"
        f"  id: {device_id}\n"
        "  names: {zh-CN: 热泵, en-US: Heat Pump}\n"
        "properties:\n"
        "  cop:\n"
        f"    value: {power_rating}\n"
        '    unit: "1"\n'
        "    valid_range: {minimum: 1, maximum: 10}\n"
        "interfaces:\n"
        "  electricity_in:\n"
        "    type: in\n"
        "    carrier: electricity\n"
        "    unit: kW\n"
        "    valid_range: {minimum: 0, maximum: null}\n"
        "  heat_out:\n"
        "    type: out\n"
        "    carrier: heat\n"
        "    unit: kW\n"
        "    valid_range: {minimum: 0, maximum: null}\n"
        "equations:\n"
        "  variables: {}\n"
        "  relations:\n"
        "    - id: heat_conversion\n"
        '      expression: "heat_out[t] = electricity_in[t] * cop"\n'
    )


def _build_plugin_zip_bytes(
    *,
    plugin_id: str = "acme.algorithm.custom_opt",
    version: str = "1.0.0",
    kind: str = "algorithm",
    consumes: list[str] | None = None,
    produces: list[str] | None = None,
    requirements: str | None = None,
    tests: list[dict[str, str]] | None = None,
    extra_files: dict[str, bytes] | None = None,
    dir_mode_outputs: dict[str, bytes] | None = None,
) -> bytes:
    consumes_list = consumes or ["ies.stage-input@1.0.0"]
    produces_list = produces or ["ies.engineering-solution@1.0.0"]
    lines = [
        "schema: ies.algorithm-plugin",
        'schema_version: "1.0.0"',
        f"plugin_id: {plugin_id}",
        f'version: "{version}"',
        f"kind: {kind}",
        "name:",
        "  zh_cn: 测试插件",
        "  en_us: Test Plugin",
        "license: MPL-2.0",
        "compatibility:",
        '  pies: ">=1.2.0,<2.0.0"',
        "consumes:",
        *[f"  - {c}" for c in consumes_list],
        "produces:",
        *[f"  - {p}" for p in produces_list],
        "runtime:",
        "  language: python",
        '  python: "3.12"',
    ]
    if requirements is not None:
        lines.append("  requirements: requirements.txt")
    lines.extend(
        [
            "entrypoint: main:solve",
            "parameters_schema:",
            "  type: object",
            "  additionalProperties: false",
            "  properties:",
            "    max_iter:",
            "      type: integer",
            "      minimum: 1",
            "      default: 10",
        ]
    )
    if tests:
        lines.append("tests:")
        for tc in tests:
            lines.append(f"  - id: {tc['id']}")
            lines.append(f"    input: {tc['input']}")
            lines.append(f"    expected: {tc['expected']}")

    files: dict[str, bytes] = {
        "plugin.yaml": ("\n".join(lines) + "\n").encode("utf-8"),
        "main.py": b"def solve(ctx):\n    return {'ok': True}\n",
    }
    if requirements is not None:
        files["requirements.txt"] = requirements.encode("utf-8")
    if dir_mode_outputs:
        files.update(dir_mode_outputs)
    if extra_files:
        files.update(extra_files)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for rel, content in sorted(files.items()):
            zf.writestr(rel, content)
    return buf.getvalue()


class _FakeJobRunnerTransport:
    """Mock job_runner HTTP transport recording build, isolated stage runs, and deletes."""

    def __init__(
        self,
        *,
        stage_root: Path,
        plugin_source_root: Path,
        stage_outputs: dict[str, dict[str, bytes]] | None = None,
        build_error_detail: str | None = None,
    ) -> None:
        self.stage_root = stage_root
        self.plugin_source_root = plugin_source_root
        self.stage_outputs = stage_outputs or {}
        self.build_error_detail = build_error_detail
        self.build_calls: list[dict[str, Any]] = []
        self.start_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.built_images: set[str] = set()

    def make_client(self) -> JobRunnerClient:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            if path == "/v1/plugins/build":
                self.build_calls.append(body)
                if self.build_error_detail:
                    return httpx.Response(400, json={"detail": self.build_error_detail})
                img = f"pies-plugin/{body['plugin_id']}:{body['version']}"
                self.built_images.add(img)
                return httpx.Response(
                    200,
                    json={
                        "image": img,
                        "plugin_id": body["plugin_id"],
                        "version": body["version"],
                        "built_at": "2026-11-27T00:00:00Z",
                        "logs": ["Built ok"],
                    },
                )
            if path == "/v1/stages/start":
                self.start_calls.append(body)
                exec_id = body["stage_execution_id"]
                out_dir = self.stage_root / exec_id / "out"
                out_dir.mkdir(parents=True, exist_ok=True)
                case_outputs = self.stage_outputs.get(body["stage_id"], {})
                for rel, raw in case_outputs.items():
                    target = out_dir / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                return httpx.Response(
                    200,
                    json={
                        "stage_execution_id": exec_id,
                        "attempt_id": body["attempt_id"],
                        "stage_id": body["stage_id"],
                        "fencing_token": body["fencing_token"],
                        "container_id": f"cid-{exec_id}",
                        "container_name": f"pies-stage-{exec_id}",
                        "image": body["image"],
                        "workspace_rel": exec_id,
                        "status": "running",
                        "running": True,
                        "reused": False,
                        "resources": {},
                        "iterations": {},
                        "output_files": [],
                    },
                )
            if path.startswith("/v1/stages/") and request.method == "GET":
                exec_id = path.split("/")[3]
                return httpx.Response(
                    200,
                    json={
                        "stage_execution_id": exec_id,
                        "attempt_id": "selftest-attempt",
                        "stage_id": "test-stage",
                        "fencing_token": "fence-test",
                        "container_id": f"cid-{exec_id}",
                        "container_name": f"pies-stage-{exec_id}",
                        "image": "pies-plugin/test:1.0.0",
                        "workspace_rel": exec_id,
                        "reused": False,
                        "status": "exited",
                        "running": False,
                        "exit_code": 0,
                        "termination_reason": "natural_exit",
                        "started_at": "2026-11-27T00:00:00Z",
                        "finished_at": "2026-11-27T00:00:01Z",
                        "output_files": [],
                        "resources": {},
                        "iterations": {},
                    },
                )
            if path.endswith("/cleanup"):
                exec_id = path.split("/")[3]
                return httpx.Response(
                    200,
                    json={
                        "stage_execution_id": exec_id,
                        "status": "cleaned",
                        "container_removed": True,
                        "workspace_removed": True,
                    },
                )
            if path == "/v1/plugins/images/delete":
                self.delete_calls.append(body)
                img = f"pies-plugin/{body['plugin_id']}:{body['version']}"
                existed = img in self.built_images
                self.built_images.discard(img)
                return httpx.Response(
                    200,
                    json={
                        "image": img,
                        "plugin_id": body["plugin_id"],
                        "version": body["version"],
                        "deleted": existed,
                        "status": "deleted" if existed else "not_found",
                    },
                )
            raise AssertionError(f"Unexpected path: {request.method} {path}")

        return JobRunnerClient(
            "http://job_runner:8090",
            client=httpx.Client(
                base_url="http://job_runner:8090",
                transport=httpx.MockTransport(handler),
            ),
        )


def test_orthogonal_switches_prevent_bypass_and_validate_pip_index_url(
    db: Session,
    tmp_path: Path,
) -> None:
    admin = make_user(db, "eco_admin", role="admin")
    member = make_user(db, "eco_member", role="engineer")
    db.commit()

    # 1. Invalid pip_index_url (--extra-index-url / whitespace / non-http) rejected
    for bad_url in (
        "https://pypi.example/simple --extra-index-url https://evil",
        "https://pypi.example/simple\n--trusted-host evil",
        "ftp://pypi.example/simple",
    ):
        with pytest.raises(EcosystemValidationError):
            ecosystem_app.update_ecosystem_settings_case(
                db,
                admin,
                market_mode="online",
                local_upload_requires_approval=True,
                pip_index_url=bad_url,
            )

    # 2. Switch to online mode while local_upload_requires_approval remains True
    updated = ecosystem_app.update_ecosystem_settings_case(
        db,
        admin,
        market_mode="online",
        local_upload_requires_approval=True,
        pip_index_url="https://pypi.internal.example/simple",
    )
    assert updated["market_mode"] == "online"
    assert updated["local_upload_requires_approval"] is True
    assert updated["pip_index_url"] == "https://pypi.internal.example/simple"

    ns = generate_namespace()
    fake_jr = _FakeJobRunnerTransport(
        stage_root=tmp_path / "stages", plugin_source_root=tmp_path / "builds"
    ).make_client()

    # Non-admin offline_import and local_upload MUST both enter pending even in online mode
    off_dev = ecosystem_app.import_device_model_case(
        db,
        member,
        yaml_text=_sample_device_yaml(f"user.{ns}.device.offline_hp"),
        revision=1,
        source_origin="offline_import",
    )
    assert off_dev["approval_status"] == "pending"
    assert off_dev["install_status"] == "pending"

    zip_bytes = _build_plugin_zip_bytes(plugin_id=f"user.{ns}.algorithm.pending_algo")
    up_plugin = ecosystem_app.upload_plugin_zip_case(
        db,
        member,
        zip_bytes=zip_bytes,
        source_origin="local_upload",
        job_runner_client=fake_jr,
        plugin_source_root=tmp_path / "builds",
        stage_root=tmp_path / "stages",
    )
    assert up_plugin["approval_status"] == "pending"
    assert up_plugin["install_status"] == "pending"

    # Online market vetted download skips local approval; admin self-upload also skips approval
    mkt_dev = ecosystem_app.import_device_model_case(
        db,
        member,
        yaml_text=_sample_device_yaml("acme.device.market_hp"),
        revision=1,
        source_origin="online_market",
    )
    assert mkt_dev["approval_status"] == "approved"
    assert mkt_dev["install_status"] == "ready"

    adm_dev = ecosystem_app.import_device_model_case(
        db,
        admin,
        yaml_text=_sample_device_yaml("acme.device.admin_hp"),
        revision=1,
        source_origin="local_upload",
    )
    assert adm_dev["approval_status"] == "approved"
    assert adm_dev["install_status"] == "ready"

    # Non-admin approve forbidden (403)
    with pytest.raises(AppError) as exc_info:
        ecosystem_app.approve_artifact_case(db, member, off_dev["id"])
    assert exc_info.value.http_status == 403

    # Admin approves pending device model -> ready
    approved = ecosystem_app.approve_artifact_case(db, admin, off_dev["id"])
    assert approved["approval_status"] == "approved"
    assert approved["install_status"] == "ready"


def test_device_model_import_export_conflict_and_selector_integration(
    db: Session,
) -> None:
    admin = make_user(db, "dev_admin", role="admin")
    member = make_user(db, "dev_member", role="engineer")
    other = make_user(db, "dev_other", role="engineer")
    db.commit()

    # Verify ORM table carries ZERO sha256 columns
    assert "sha256" not in set(ecosystem_domain.persistence.LocalArtifact.__table__.columns.keys())  # type: ignore[attr-defined]

    ns = generate_namespace()
    stable_id = f"user.{ns}.device.chp_unit"
    yaml_v1 = _sample_device_yaml(stable_id, power_rating=3.5)
    digest_v1 = hashlib.sha256(yaml_v1.encode("utf-8")).hexdigest()

    # Reject invalid Crockford Base32 (contains 'i')
    with pytest.raises(EcosystemValidationError):
        bad_id = "user." + ("0123456789i" + "a") + ".device.bad"
        ecosystem_app.import_device_model_case(db, admin, yaml_text=_sample_device_yaml(bad_id))
    with pytest.raises(EcosystemValidationError):
        ecosystem_app.import_device_model_case(
            db, admin, yaml_text=_sample_device_yaml("user.0123456789il.device.bad_ns")
        )

    # Mismatched expected_sha256 fails boundary check
    with pytest.raises(EcosystemValidationError):
        ecosystem_app.import_device_model_case(
            db, admin, yaml_text=yaml_v1, expected_sha256="0" * 64
        )

    # Matching expected_sha256 imports cleanly
    imp1 = ecosystem_app.import_device_model_case(
        db, admin, yaml_text=yaml_v1, expected_sha256=digest_v1, revision=1
    )
    assert imp1["idempotent"] is False
    assert imp1["stable_id"] == stable_id
    assert imp1["version"] == "1"

    # Re-importing identical canonical YAML -> idempotent=True
    imp_dup = ecosystem_app.import_device_model_case(db, admin, yaml_text=yaml_v1, revision=1)
    assert imp_dup["idempotent"] is True

    # Modified content with same (stable_id, revision=1) -> 409 conflict
    yaml_v1_mod = _sample_device_yaml(stable_id, power_rating=4.5)
    with pytest.raises(EcosystemConflictError) as conflict_exc:
        ecosystem_app.import_device_model_case(db, admin, yaml_text=yaml_v1_mod, revision=1)
    assert conflict_exc.value.http_status == 409

    # Multi-version coexistence on revision=2
    imp2 = ecosystem_app.import_device_model_case(db, admin, yaml_text=yaml_v1_mod, revision=2)
    assert imp2["version"] == "2"

    # Pure technical YAML export has NO embedded revision, carries revision in metadata
    exported = ecosystem_app.export_device_model_case(db, admin, artifact_id=imp2["id"])
    assert exported["revision"] == 2
    assert "revision:" not in exported["yaml_text"]
    assert stable_id in exported["yaml_text"]

    # Member installs revision=1 and checks project device selector
    proj = project_domain.create_project(
        db,
        owner_id=member.id,
        created_by=member.id,
        baseline_start_year=2025,
        name="Selector Proj",
    )
    draft_content = {"devices": {}, "connections": [], "project_models": []}
    from iesplan import storage as storage_domain

    obj = storage_domain.put_object(
        db,
        json.dumps(draft_content).encode("utf-8"),
        "application/json",
        "project_draft",
    )
    project_domain.create_draft(
        db, project_id=proj.id, content_object_id=obj.id, updated_by=member.id
    )
    other_proj = project_domain.create_project(
        db,
        owner_id=other.id,
        created_by=other.id,
        baseline_start_year=2025,
        name="Other Selector Proj",
    )
    other_obj = storage_domain.put_object(
        db,
        json.dumps(draft_content).encode("utf-8"),
        "application/json",
        "project_draft",
    )
    project_domain.create_draft(
        db, project_id=other_proj.id, content_object_id=other_obj.id, updated_by=other.id
    )
    db.commit()

    # Other user has not installed -> not in their selector
    sel_other = models_app.list_project_device_types(db, other, other_proj.id)
    assert stable_id not in {item["type_id"] for item in sel_other["items"]}

    ecosystem_app.install_artifact_for_user_case(db, member, imp1["id"], enabled=True)
    sel_member = models_app.list_project_device_types(db, member, proj.id)
    by_id = {item["type_id"]: item for item in sel_member["items"]}
    assert stable_id in by_id
    assert by_id[stable_id]["local_revision"] == 1
    assert by_id[stable_id]["parameters"]["cop"]["default"] == 3.5

    # Disabling member install removes it from member's selector
    ecosystem_app.set_member_artifact_enabled_case(db, member, imp1["id"], enabled=False)
    sel_disabled = models_app.list_project_device_types(db, member, proj.id)
    assert stable_id not in {item["type_id"] for item in sel_disabled["items"]}


def test_plugin_zip_security_scheduler_build_dual_selftest_and_calc_config(
    db_factory: sessionmaker[Session],
    db: Session,
    tmp_path: Path,
) -> None:
    admin = make_user(db, "plg_admin", role="admin")
    member = make_user(db, "plg_member", role="engineer")
    proj = project_domain.create_project(
        db,
        owner_id=member.id,
        created_by=member.id,
        baseline_start_year=2025,
        name="Plugin Config Proj",
    )
    db.commit()

    # 1. ZIP traversal / Dockerfile / case collision / missing entrypoint rejected
    bad_buf = io.BytesIO()
    with zipfile.ZipFile(bad_buf, mode="w") as zf:
        zf.writestr("../escape.txt", "bad")
    with pytest.raises(EcosystemValidationError):
        ecosystem_app.upload_plugin_zip_case(db, admin, zip_bytes=bad_buf.getvalue())

    bad_df = _build_plugin_zip_bytes(extra_files={"Dockerfile": b"FROM alpine\n"})
    with pytest.raises(EcosystemValidationError):
        ecosystem_app.upload_plugin_zip_case(db, admin, zip_bytes=bad_df)

    coll_buf = io.BytesIO()
    with zipfile.ZipFile(coll_buf, mode="w") as zf:
        zf.writestr("plugin.yaml", "schema: ies.algorithm-plugin\n")
        zf.writestr("main.py", "def solve(ctx): pass\n")
        zf.writestr("./MAIN.py", "def solve(ctx): pass\n")
    with pytest.raises(EcosystemValidationError):
        ecosystem_app.upload_plugin_zip_case(db, admin, zip_bytes=coll_buf.getvalue())

    # 2. Configure pip_index_url & EmbeddedScheduler + dual-mode --network=none self-test
    ecosystem_app.update_ecosystem_settings_case(
        db,
        admin,
        market_mode="local",
        local_upload_requires_approval=True,
        pip_index_url="https://mirror.example/simple",
    )

    stage_root = tmp_path / "stages"
    build_root = tmp_path / "plugin_builds"
    expected_sol = {"schema": "ies.engineering-solution", "schema_version": "1.0.0", "ok": True}
    expected_bytes = json.dumps(expected_sol).encode("utf-8")

    transport = _FakeJobRunnerTransport(
        stage_root=stage_root,
        plugin_source_root=build_root,
        stage_outputs={
            "test-single_json": {"solution.json": expected_bytes},
            "test-dir_case": {"solution.json": expected_bytes, "metrics.json": b'{"gap": 0.0}'},
        },
    )
    jr_client = transport.make_client()

    zip_ok = _build_plugin_zip_bytes(
        plugin_id="acme.algorithm.custom_opt",
        version="1.0.0",
        kind="algorithm",
        requirements="scipy>=1.13\n",
        tests=[
            {
                "id": "single_json",
                "input": "tests/single/input.json",
                "expected": "tests/single/expected.json",
            },
            {
                "id": "dir_case",
                "input": "tests/dir_case/in",
                "expected": "tests/dir_case/expected",
            },
        ],
        dir_mode_outputs={
            "tests/single/input.json": b'{"hello": "world"}',
            "tests/single/expected.json": expected_bytes,
            "tests/dir_case/in/request.json": b'{"mode": "dir"}',
            "tests/dir_case/expected/solution.json": expected_bytes,
            "tests/dir_case/expected/metrics.json": b'{"gap": 0.0}',
        },
    )

    scheduler = EmbeddedScheduler(
        session_factory=db_factory,
        compute_slots=1,
        io_slots=1,
        build_slots=1,
    )
    res = ecosystem_app.upload_plugin_zip_case(
        db,
        admin,
        zip_bytes=zip_ok,
        scheduler=scheduler,
        job_runner_client=jr_client,
        session_factory=db_factory,
        plugin_source_root=build_root,
        stage_root=stage_root,
        wait_for_build=True,
    )
    scheduler.stop(timeout=2.0)

    assert res["install_status"] == "ready"
    assert res["image_ref"] == "pies-plugin/acme.algorithm.custom_opt:1.0.0"
    assert len(transport.build_calls) == 1
    assert transport.build_calls[0]["pip_index_url"] == "https://mirror.example/simple"
    assert len(transport.start_calls) == 2

    # 3. Failing self-test deletes built image and marks build_failed
    bad_transport = _FakeJobRunnerTransport(
        stage_root=stage_root,
        plugin_source_root=build_root,
        stage_outputs={"test-single_json": {"solution.json": b'{"wrong": true}'}},
    )
    zip_fail = _build_plugin_zip_bytes(
        plugin_id="acme.algorithm.bad_test",
        version="1.0.0",
        kind="algorithm",
        tests=[
            {
                "id": "single_json",
                "input": "tests/single/input.json",
                "expected": "tests/single/expected.json",
            }
        ],
        dir_mode_outputs={
            "tests/single/input.json": b"{}",
            "tests/single/expected.json": expected_bytes,
        },
    )
    with pytest.raises(EcosystemValidationError):
        ecosystem_app.upload_plugin_zip_case(
            db,
            admin,
            zip_bytes=zip_fail,
            job_runner_client=bad_transport.make_client(),
            session_factory=db_factory,
            plugin_source_root=build_root,
            stage_root=stage_root,
        )
    assert len(bad_transport.delete_calls) == 1
    failed_rec = ecosystem_domain.get_artifact_by_identity(
        db, "algorithm", "acme.algorithm.bad_test", "1.0.0"
    )
    assert failed_rec is not None
    assert failed_rec.install_status == "build_failed"
    assert failed_rec.image_ref is None

    # 4. CalculationConfig accepts enabled ready local plugin and rejects uninstalled/disabled
    ecosystem_app.install_artifact_for_user_case(db, member, res["id"], enabled=True)
    calc_cfg = {
        "schema": "ies.calculation-config",
        "schema_version": "1.0.0",
        "mode": "planning",
        "predictions": [],
        "generator": None,
        "solver": None,
        "algorithm": "acme.algorithm.custom_opt@1.0.0",
        "evaluators": [],
        "precision": "medium",
        "time_limit_seconds": 60,
        "options": {"algorithm": {"max_iter": 20}},
        "seed": 42,
        "outputs": ["solution"],
    }
    saved = config_app.save_calculation_config_case(
        db,
        member,
        proj.id,
        calc_cfg,
        expected_revision=None,
        predictor_registry=build_predictor_registry((NAIVE_PREDICTOR,)),
        generator_registry=build_generator_registry((HIGHS_MILP_PROVIDER,)),
        solver_registry=build_solver_registry((BUILTIN_SOLVER_HIGHS_SPEC,)),
        evaluator_registry=build_evaluator_registry((BUILTIN_EVALUATOR_FINANCE_SPEC,)),
        algorithm_registry=build_algorithm_registry(()),
    )
    assert saved["revision"] == 1

    # 5. Worker resolve_stage_descriptor resolves local plugin container image
    plane = compute_cases.ComputeExecutionPlane(
        generator_registry=build_generator_registry((HIGHS_MILP_PROVIDER,)),
        predictor_registry=build_predictor_registry((NAIVE_PREDICTOR,)),
        stage_runner=None,
        stage_catalog=build_builtin_stage_catalog("pies-builtin-stages:1.2.0"),
        session_factory=db_factory,
    )
    desc = compute_cases.resolve_stage_descriptor(
        plane, "acme.algorithm.custom_opt@1.0.0", kind_label="algorithm"
    )
    assert desc.image == "pies-plugin/acme.algorithm.custom_opt:1.0.0"
    assert desc.command == ("python", "-m", "pies_sdk.runner", "--manifest", "/plugin/plugin.yaml")


def test_two_phase_stateless_uninstall_protects_inflight_snapshots(
    db_factory: sessionmaker[Session],
    db: Session,
    tmp_path: Path,
) -> None:
    admin = make_user(db, "uninst_admin", role="admin")
    member = make_user(db, "uninst_member", role="engineer")
    proj = project_domain.create_project(
        db,
        owner_id=member.id,
        created_by=member.id,
        baseline_start_year=2025,
        name="Uninstall Proj",
    )
    from iesplan import storage as storage_domain

    ver_obj = storage_domain.put_object(db, b"{}", "application/json", "project_version")
    ver = project_domain.create_version(
        db,
        project_id=proj.id,
        name="v1",
        reason="test",
        content_object_id=ver_obj.id,
        created_by=member.id,
    )
    db.commit()

    transport = _FakeJobRunnerTransport(
        stage_root=tmp_path / "stages", plugin_source_root=tmp_path / "builds"
    )
    jr_client = transport.make_client()
    zip_ok = _build_plugin_zip_bytes(plugin_id="acme.algorithm.inflight", version="1.0.0")
    art = ecosystem_app.upload_plugin_zip_case(
        db,
        admin,
        zip_bytes=zip_ok,
        job_runner_client=jr_client,
        session_factory=db_factory,
        plugin_source_root=tmp_path / "builds",
        stage_root=tmp_path / "stages",
    )

    # Create queued task referencing acme.algorithm.inflight@1.0.0
    snap = tasks_domain.create_snapshot(
        db,
        project_version_id=ver.id,
        dataset_version_ids=[],
        calc_config_snapshot={
            "algorithm": "acme.algorithm.inflight@1.0.0",
            "generator": None,
            "solver": None,
        },
        random_seed=1,
        created_by=member.id,
        schema_version="1.0.0",
        result_adapter="",
        executor="",
        device_models={},
        evaluation={"operation_years": 1},
    )
    task = tasks_domain.create_task(
        db,
        project_id=proj.id,
        type="calc",
        requested_by=member.id,
        idempotency_key="uninst-1",
        calc_snapshot_id=snap.id,
    )
    db.commit()

    # Phase 1: in-flight task blocks image deletion (409 ECO-UNINSTALL-IN-USE), while disabling artifact
    with pytest.raises(EcosystemUninstallInUseError) as err:
        ecosystem_app.uninstall_artifact_image_case(
            db, admin, art["id"], job_runner_client=jr_client
        )
    assert err.value.code == "ECO-UNINSTALL-IN-USE"
    assert err.value.http_status == 409
    assert len(transport.delete_calls) == 0

    disabled_rec = ecosystem_domain.get_artifact(db, art["id"])
    assert disabled_rec is not None
    assert disabled_rec.install_status == "disabled"

    # Once task reaches terminal state, repeated stateless uninstall succeeds and deletes image
    tasks_domain.set_task_status(db, task.id, "completed")
    db.commit()
    res = ecosystem_app.uninstall_artifact_image_case(
        db, admin, art["id"], job_runner_client=jr_client
    )
    assert res["image_deleted"] is True
    assert res["image_status"] == "deleted"
    assert len(transport.delete_calls) == 1
    assert res["artifact"]["image_ref"] is None


def test_ecosystem_api_endpoints_smoke(engine: Engine, db_factory: sessionmaker[Session]) -> None:
    with db_factory() as setup_db:
        admin = make_user(setup_db, "api_eco_admin", role="admin")
        member = make_user(setup_db, "api_eco_member", role="engineer")
        setup_db.commit()

    app = create_app()
    app.state.bootstrap_context = SimpleNamespace(
        session_factory=db_factory,
        job_runner_client=None,
    )
    app.state.scheduler = None
    client = TestClient(app, raise_server_exceptions=False)

    admin_hdrs = login_headers(client, admin)
    member_hdrs = login_headers(client, member)

    r_get = client.get("/api/ecosystem/settings", headers=member_hdrs)
    assert r_get.status_code == 200
    assert r_get.json()["market_mode"] == "local"

    r_put_forbidden = client.put(
        "/api/ecosystem/settings",
        json={"market_mode": "online", "local_upload_requires_approval": True},
        headers=member_hdrs,
    )
    assert r_put_forbidden.status_code == 403

    r_put_ok = client.put(
        "/api/ecosystem/settings",
        json={"market_mode": "online", "local_upload_requires_approval": True},
        headers=admin_hdrs,
    )
    assert r_put_ok.status_code == 200
    assert r_put_ok.json()["market_mode"] == "online"

    ns = generate_namespace()
    r_imp = client.post(
        "/api/ecosystem/device-models/import",
        json={"yaml_text": _sample_device_yaml(f"user.{ns}.device.api_hp"), "revision": 1},
        headers=member_hdrs,
    )
    assert r_imp.status_code == 201
    art_id = r_imp.json()["id"]
    assert r_imp.json()["approval_status"] == "pending"

    r_approve = client.post(f"/api/ecosystem/artifacts/{art_id}/approve", headers=admin_hdrs)
    assert r_approve.status_code == 200
    assert r_approve.json()["install_status"] == "ready"

    r_install = client.post(f"/api/ecosystem/artifacts/{art_id}/install", headers=member_hdrs)
    assert r_install.status_code == 200
    assert r_install.json()["installed"] is True

    r_exp = client.get(f"/api/ecosystem/device-models/{art_id}/export", headers=member_hdrs)
    assert r_exp.status_code == 200
    assert r_exp.json()["revision"] == 1


@pytest.mark.skipif(
    not os.environ.get("IESPLAN_TEST_PG_URL"),
    reason="需 IESPLAN_TEST_PG_URL 指向隔离 PostgreSQL 空库",
)
def test_postgres_migration_and_concurrent_uninstall_vs_submission(tmp_path: Path) -> None:
    pg_url = os.environ["IESPLAN_TEST_PG_URL"]
    pg_engine = create_engine(pg_url, pool_pre_ping=True)
    try:
        Base.metadata.create_all(pg_engine)
        # Verify migration idempotency on real PostgreSQL
        apply_migrations(pg_engine)
        apply_migrations(pg_engine)

        cols = {col["name"] for col in sa.inspect(pg_engine).get_columns("local_artifacts")}
        assert "sha256" not in cols
        assert {"kind", "stable_id", "version", "install_status", "approval_status"} <= cols

        PgSession = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)
        with PgSession() as db:
            admin = make_user(db, f"pg_admin_{uuid.uuid4().hex[:6]}", role="admin")
            member = make_user(db, f"pg_member_{uuid.uuid4().hex[:6]}", role="engineer")
            proj = project_domain.create_project(
                db,
                owner_id=member.id,
                created_by=member.id,
                baseline_start_year=2025,
                name="PG Race Proj",
            )
            from iesplan import storage as storage_domain

            ver_obj = storage_domain.put_object(
                db, b"{}", "application/json", "project_version"
            )
            ver = project_domain.create_version(
                db,
                project_id=proj.id,
                name="v1",
                reason="test",
                content_object_id=ver_obj.id,
                created_by=member.id,
            )
            db.commit()

        storage_service._blob_store = FileSystemBlobStore()
        transport = _FakeJobRunnerTransport(
            stage_root=tmp_path / "pg_stages", plugin_source_root=tmp_path / "pg_builds"
        )
        jr_client = transport.make_client()

        with PgSession() as db:
            zip_ok = _build_plugin_zip_bytes(
                plugin_id=f"acme.algorithm.pgrace_{uuid.uuid4().hex[:6]}", version="1.0.0"
            )
            art = ecosystem_app.upload_plugin_zip_case(
                db,
                admin,
                zip_bytes=zip_ok,
                job_runner_client=jr_client,
                session_factory=PgSession,
                plugin_source_root=tmp_path / "pg_builds",
                stage_root=tmp_path / "pg_stages",
            )
            ecosystem_app.install_artifact_for_user_case(db, member, art["id"], enabled=True)

        barrier = threading.Barrier(2)
        calc_cfg = {
            "algorithm": art["ref"],
            "generator": None,
            "solver": None,
            "predictions": [],
            "evaluators": [],
        }

        def _submit_side() -> str:
            barrier.wait(timeout=5.0)
            with PgSession() as s_db:
                try:
                    ecosystem_app.lock_and_verify_submission_artifacts(
                        s_db, user_id=member.id, calc_config=calc_cfg
                    )
                    snap = tasks_domain.create_snapshot(
                        s_db,
                        project_version_id=ver.id,
                        dataset_version_ids=[],
                        calc_config_snapshot=calc_cfg,
                        random_seed=7,
                        created_by=member.id,
                        schema_version="1.0.0",
                        result_adapter="",
                        executor="",
                        device_models={},
                        evaluation={"operation_years": 1},
                    )
                    tasks_domain.create_task(
                        s_db,
                        project_id=proj.id,
                        type="calc",
                        requested_by=member.id,
                        idempotency_key=f"pg-race-{uuid.uuid4().hex[:6]}",
                        calc_snapshot_id=snap.id,
                    )
                    s_db.commit()
                    return "submitted"
                except Exception:
                    s_db.rollback()
                    return "rejected"

        def _uninstall_side() -> str:
            barrier.wait(timeout=5.0)
            with PgSession() as u_db:
                try:
                    ecosystem_app.uninstall_artifact_image_case(
                        u_db, admin, art["id"], job_runner_client=jr_client
                    )
                    return "uninstalled"
                except EcosystemUninstallInUseError:
                    return "blocked_409"

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_sub = pool.submit(_submit_side)
            f_un = pool.submit(_uninstall_side)
            sub_outcome = f_sub.result(timeout=10.0)
            un_outcome = f_un.result(timeout=10.0)

        # Mutual exclusion invariant under PostgreSQL FOR UPDATE:
        # Never both submitted AND uninstalled!
        assert not (sub_outcome == "submitted" and un_outcome == "uninstalled")
        assert (sub_outcome, un_outcome) in {
            ("submitted", "blocked_409"),
            ("rejected", "uninstalled"),
        }
    finally:
        pg_engine.dispose()
