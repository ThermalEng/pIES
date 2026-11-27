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
import uuid
import zipfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from auth_helpers import login_headers, make_user
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
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
    compat_pies: str = ">=1.2.0,<2.0.0",
    advanced: dict[str, Any] | None = None,
    extensions: dict[str, Any] | None = None,
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
        "description: Test algorithm plugin for E2 verification",
        "license: MPL-2.0",
        "compatibility:",
        f'  pies: "{compat_pies}"',
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
            "resources:",
            "  default:",
            "    cpu: 1",
            "    memory_mb: 512",
            "    timeout_seconds: 60",
            "    process_limit: 8",
            "  maximum:",
            "    cpu: 2",
            "    memory_mb: 1024",
            "    timeout_seconds: 120",
            "    process_limit: 16",
        ]
    )
    if advanced is not None:
        from iesplan.core.yamlmini import dump as _yaml_dump

        adv_yaml = _yaml_dump({"advanced": advanced}).rstrip()
        lines.extend(adv_yaml.splitlines())
    if extensions is not None:
        from iesplan.core.yamlmini import dump as _yaml_dump

        ext_yaml = _yaml_dump({"extensions": extensions}).rstrip()
        lines.extend(ext_yaml.splitlines())
    if tests:
        lines.append("tests:")
        for tc in tests:
            lines.append(f"  - id: {tc['id']}")
            if "in" in tc:
                lines.append(f"    in: {tc['in']}")
            else:
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

    # Untrusted caller cannot self-declare source_origin="online_market" to bypass local approval!
    with pytest.raises(EcosystemValidationError) as exc_origin:
        ecosystem_app.import_device_model_case(
            db,
            member,
            yaml_text=_sample_device_yaml("acme.device.forged_mkt"),
            revision=1,
            source_origin="online_market",
        )
    assert exc_origin.value.code == "ECO-ORIGIN-FORBIDDEN"

    with pytest.raises(EcosystemValidationError) as exc_plg_origin:
        ecosystem_app.upload_plugin_zip_case(
            db,
            member,
            zip_bytes=zip_bytes,
            source_origin="online_market",
            job_runner_client=fake_jr,
            plugin_source_root=tmp_path / "builds",
            stage_root=tmp_path / "stages",
        )
    assert exc_plg_origin.value.code == "ECO-ORIGIN-FORBIDDEN"

    # Only trusted server-side market download (trusted_market=True) skips local approval; admin also skips
    mkt_dev = ecosystem_app.import_device_model_case(
        db,
        member,
        yaml_text=_sample_device_yaml("acme.device.market_hp"),
        revision=1,
        source_origin="online_market",
        trusted_market=True,
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

    # Single active version overwrite upgrade on revision=2
    imp2 = ecosystem_app.import_device_model_case(db, admin, yaml_text=yaml_v1_mod, revision=2)
    assert imp2["id"] == imp1["id"]
    assert imp2["version"] == "2"
    all_dev = [
        r for r in ecosystem_domain.list_artifacts(db, kind="device_model") if r.stable_id == stable_id
    ]
    assert len(all_dev) == 1

    # Pure technical YAML export has NO embedded revision, carries revision in metadata
    exported = ecosystem_app.export_device_model_case(db, admin, artifact_id=imp2["id"])
    assert exported["revision"] == 2
    assert "revision:" not in exported["yaml_text"]
    assert stable_id in exported["yaml_text"]

    # Member installs current active artifact (revision=2) and checks project device selector
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

    ecosystem_app.install_artifact_for_user_case(db, member, imp2["id"], enabled=True)
    sel_member = models_app.list_project_device_types(db, member, proj.id)
    by_id = {item["type_id"]: item for item in sel_member["items"]}
    assert stable_id in by_id
    assert by_id[stable_id]["local_revision"] == 2
    assert by_id[stable_id]["parameters"]["cop"]["default"] == 4.5

    # Disabling member install removes it from member's selector
    ecosystem_app.set_member_artifact_enabled_case(db, member, imp2["id"], enabled=False)
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
                "in": "tests/dir_case/in",
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

    # Rejecting a disabled artifact MUST fail (409) to protect in-flight tasks' stage resolution!
    with pytest.raises(EcosystemConflictError) as rej_err:
        ecosystem_app.reject_artifact_case(db, admin, art["id"], reason="cannot_reject_disabled")
    assert rej_err.value.http_status == 409
    plane = compute_cases.ComputeExecutionPlane(
        generator_registry=build_generator_registry((HIGHS_MILP_PROVIDER,)),
        predictor_registry=build_predictor_registry((NAIVE_PREDICTOR,)),
        stage_runner=None,
        stage_catalog=build_builtin_stage_catalog("pies-builtin-stages:1.2.0"),
        session_factory=db_factory,
    )
    inflight_desc = compute_cases.resolve_stage_descriptor(
        plane, "acme.algorithm.inflight@1.0.0", kind_label="algorithm"
    )
    assert inflight_desc.image == "pies-plugin/acme.algorithm.inflight:1.0.0"

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

    # API upload/import endpoints reject caller-supplied source_origin="online_market" (422)
    r_forged_dev = client.post(
        "/api/ecosystem/device-models/import",
        json={
            "yaml_text": _sample_device_yaml(f"user.{ns}.device.forged"),
            "revision": 1,
            "source_origin": "online_market",
        },
        headers=member_hdrs,
    )
    assert r_forged_dev.status_code == 422

    r_forged_plg = client.post(
        "/api/ecosystem/plugins/upload",
        data={"source_origin": "online_market"},
        files={"file": ("plugin.zip", _build_plugin_zip_bytes(), "application/zip")},
        headers=member_hdrs,
    )
    assert r_forged_plg.status_code == 422


@pytest.mark.skipif(
    not os.environ.get("IESPLAN_TEST_PG_URL"),
    reason="需 IESPLAN_TEST_PG_URL 指向隔离 PostgreSQL 空库",
)
def test_postgres_migration_single_active_upgrade_and_inflight_guard(tmp_path: Path) -> None:
    pg_url = os.environ["IESPLAN_TEST_PG_URL"]
    pg_engine = create_engine(pg_url, pool_pre_ping=True)
    try:
        Base.metadata.create_all(pg_engine)
        PgSession = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)
        with PgSession() as db:
            from iesplan import storage as storage_domain
            from iesplan.model.persistence import (
                ModelTemplate,
                ModelTemplateDraftRevision,
                ModelTemplateRevision,
            )

            admin = make_user(db, f"pg_admin_{uuid.uuid4().hex[:6]}", role="admin")
            member = make_user(db, f"pg_member_{uuid.uuid4().hex[:6]}", role="engineer")
            proj = project_domain.create_project(
                db,
                owner_id=member.id,
                created_by=member.id,
                baseline_start_year=2025,
                name="PG Upgrade Proj",
            )
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
            # Seed multi-revision Option B cleanup fixture before migration runs
            tpl_obj = storage_domain.put_object(db, b"{}", "application/json", "model_template_draft")
            tpl_row = ModelTemplate(
                template_id=f"user.{generate_namespace()}.device.pgcleanup",
                owner_id=member.id,
                status="published",
                slug="pgcleanup",
                public_namespace=generate_namespace(),
                draft_yaml_object_id=tpl_obj.id,
                draft_revision=2,
                published_revision=2,
            )
            db.add(tpl_row)
            db.flush()
            draft_rev_row = ModelTemplateDraftRevision(
                entry_id=tpl_row.id,
                revision=1,
                yaml_object_id=tpl_obj.id,
                source="yaml_editor",
                created_by=member.id,
            )
            pub_old = ModelTemplateRevision(
                template_id=tpl_row.id,
                revision=1,
                schema_version="1.0.0",
                input_count=0,
                yaml_object_id=tpl_obj.id,
                receipt_object_id=tpl_obj.id,
                summary_object_id=tpl_obj.id,
                published_by=member.id,
            )
            pub_cur = ModelTemplateRevision(
                template_id=tpl_row.id,
                revision=2,
                schema_version="1.0.0",
                input_count=0,
                yaml_object_id=tpl_obj.id,
                receipt_object_id=tpl_obj.id,
                summary_object_id=tpl_obj.id,
                published_by=member.id,
            )
            db.add_all([draft_rev_row, pub_old, pub_cur])
            db.flush()
            tpl_row.current_draft_revision_id = draft_rev_row.id
            tpl_row.current_published_revision_id = pub_cur.id
            db.commit()

        # Verify migration idempotency, single active unique constraint, and Option B cleanup on PostgreSQL
        apply_migrations(pg_engine)
        apply_migrations(pg_engine)

        insp = sa.inspect(pg_engine)
        cols = {col["name"] for col in insp.get_columns("local_artifacts")}
        assert "sha256" not in cols
        assert {"kind", "stable_id", "version", "install_status", "approval_status"} <= cols
        uqs = {
            uq["name"]: list(uq["column_names"])
            for uq in insp.get_unique_constraints("local_artifacts")
        }
        assert uqs.get("uq_local_artifacts_identity") == ["kind", "stable_id"]
        with PgSession() as db:
            from iesplan.model.persistence import (
                ModelTemplate,
                ModelTemplateDraftRevision,
                ModelTemplateRevision,
            )

            assert db.execute(select(ModelTemplateDraftRevision)).scalars().all() == []
            rem_revs = db.execute(
                select(ModelTemplateRevision).where(ModelTemplateRevision.template_id == tpl_row.id)
            ).scalars().all()
            assert [r.revision for r in rem_revs] == [2]
            refreshed_tpl = db.get(ModelTemplate, tpl_row.id)
            assert refreshed_tpl is not None
            assert refreshed_tpl.current_draft_revision_id is None

        storage_service._blob_store = FileSystemBlobStore()
        transport = _FakeJobRunnerTransport(
            stage_root=tmp_path / "pg_stages", plugin_source_root=tmp_path / "pg_builds"
        )
        jr_client = transport.make_client()
        plugin_id = f"acme.algorithm.pgupg_{uuid.uuid4().hex[:6]}"

        with PgSession() as db:
            zip_v1 = _build_plugin_zip_bytes(plugin_id=plugin_id, version="1.0.0")
            art = ecosystem_app.upload_plugin_zip_case(
                db,
                admin,
                zip_bytes=zip_v1,
                job_runner_client=jr_client,
                session_factory=PgSession,
                plugin_source_root=tmp_path / "pg_builds",
                stage_root=tmp_path / "pg_stages",
            )
            ecosystem_app.install_artifact_for_user_case(db, member, art["id"], enabled=True)

            calc_cfg = {
                "algorithm": art["ref"],
                "generator": None,
                "solver": None,
                "predictions": [],
                "evaluators": [],
            }
            ecosystem_app.verify_submission_ecosystem_artifacts(
                db, user_id=member.id, calc_config=calc_cfg
            )
            snap = tasks_domain.create_snapshot(
                db,
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
            pg_task = tasks_domain.create_task(
                db,
                project_id=proj.id,
                type="calc",
                requested_by=member.id,
                idempotency_key=f"pg-upg-{uuid.uuid4().hex[:6]}",
                calc_snapshot_id=snap.id,
            )
            db.commit()

            # Non-terminal snapshot blocks both uninstall and plugin overwrite upgrade
            zip_v2 = _build_plugin_zip_bytes(plugin_id=plugin_id, version="2.0.0")
            with pytest.raises(EcosystemUninstallInUseError):
                ecosystem_app.upload_plugin_zip_case(
                    db,
                    admin,
                    zip_bytes=zip_v2,
                    job_runner_client=jr_client,
                    session_factory=PgSession,
                    plugin_source_root=tmp_path / "pg_builds",
                    stage_root=tmp_path / "pg_stages",
                )
            with pytest.raises(EcosystemUninstallInUseError):
                ecosystem_app.uninstall_artifact_image_case(
                    db, admin, art["id"], job_runner_client=jr_client
                )

            # Once task reaches terminal state, overwrite upgrade replaces in-place & cleans old image
            tasks_domain.set_task_status(db, pg_task.id, "completed")
            db.commit()
            upgraded = ecosystem_app.upload_plugin_zip_case(
                db,
                admin,
                zip_bytes=zip_v2,
                job_runner_client=jr_client,
                session_factory=PgSession,
                plugin_source_root=tmp_path / "pg_builds",
                stage_root=tmp_path / "pg_stages",
            )
            assert upgraded["id"] == art["id"]
            assert upgraded["version"] == "2.0.0"
            assert any(c.get("version") == "1.0.0" for c in transport.delete_calls)
            all_rows = [
                r
                for r in ecosystem_domain.list_artifacts(db, kind="algorithm")
                if r.stable_id == plugin_id
            ]
            assert len(all_rows) == 1
            installs = ecosystem_domain.list_member_installs(db, user_id=member.id, kind="algorithm")
            matched_inst = [inst for inst in installs if inst.stable_id == plugin_id]
            assert len(matched_inst) == 1
            assert matched_inst[0].version == "2.0.0"
    finally:
        pg_engine.dispose()


def test_full_manifest_compatibility_descriptor_projection_and_sdk_pack_fixture(
    db_factory: sessionmaker[Session],
    db: Session,
    tmp_path: Path,
) -> None:
    admin = make_user(db, "cfg_admin", role="admin")
    member = make_user(db, "cfg_member", role="engineer")
    proj = project_domain.create_project(
        db,
        owner_id=member.id,
        created_by=member.id,
        baseline_start_year=2025,
        name="Generator Projection Proj",
    )
    db.commit()

    # 1. Bundled schema matches sdk/pies_sdk/schema/plugin-1.0.0.schema.json when sdk is present
    repo_root = Path(__file__).resolve().parents[2]
    sdk_schema = repo_root / "sdk" / "pies_sdk" / "schema" / "plugin-1.0.0.schema.json"
    backend_schema = (
        Path(__file__).resolve().parents[1]
        / "iesplan"
        / "computation"
        / "schema"
        / "plugin-1.0.0.schema.json"
    )
    if sdk_schema.is_file():
        assert json.loads(backend_schema.read_text(encoding="utf-8")) == json.loads(
            sdk_schema.read_text(encoding="utf-8")
        )

    transport = _FakeJobRunnerTransport(
        stage_root=tmp_path / "stages", plugin_source_root=tmp_path / "builds"
    )
    jr_client = transport.make_client()

    # 2. Invalid no-test plugins (incompatible pies / missing additionalProperties) rejected at ingress
    bad_compat = _build_plugin_zip_bytes(
        plugin_id="acme.generator.bad_compat",
        kind="generator",
        produces=["ies.solver-bundle.mps@1.0.0"],
        compat_pies=">=9.0.0",
    )
    with pytest.raises(EcosystemValidationError) as compat_err:
        ecosystem_app.upload_plugin_zip_case(
            db, admin, zip_bytes=bad_compat, job_runner_client=jr_client
        )
    assert compat_err.value.code == "ECO-COMPAT-001"
    assert (
        ecosystem_domain.get_artifact_by_identity(
            db, "generator", "acme.generator.bad_compat", "1.0.0"
        )
        is None
    )

    bad_nested_schema = io.BytesIO()
    with zipfile.ZipFile(bad_nested_schema, mode="w") as zf:
        zf.writestr("main.py", "def solve(ctx): return {}\n")
        zf.writestr(
            "plugin.yaml",
            (
                "schema: ies.algorithm-plugin\n"
                'schema_version: "1.0.0"\n'
                "plugin_id: acme.algorithm.nested_bad\n"
                'version: "1.0.0"\n'
                "kind: algorithm\n"
                "name:\n  en_us: Bad Nested\n"
                "description: Missing nested additionalProperties\n"
                "compatibility:\n  pies: '>=1.2.0,<2.0.0'\n"
                "consumes:\n  - ies.stage-input@1.0.0\n"
                "produces:\n  - ies.engineering-solution@1.0.0\n"
                "runtime:\n  language: python\n  python: '3.12'\n"
                "entrypoint: main:solve\n"
                "parameters_schema:\n"
                "  type: object\n"
                "  additionalProperties: false\n"
                "  properties:\n"
                "    nested:\n"
                "      type: object\n"
                "resources:\n"
                "  default:\n    cpu: 1\n    memory_mb: 512\n"
                "    timeout_seconds: 60\n    process_limit: 8\n"
                "  maximum:\n    cpu: 2\n    memory_mb: 1024\n"
                "    timeout_seconds: 120\n    process_limit: 16\n"
            ),
        )
    with pytest.raises(EcosystemValidationError):
        ecosystem_app.upload_plugin_zip_case(
            db, admin, zip_bytes=bad_nested_schema.getvalue(), job_runner_client=jr_client
        )

    # 3. Local generator projects advanced/extensions/companions into GeneratorDescriptor
    gen_zip = _build_plugin_zip_bytes(
        plugin_id="acme.generator.custom_milp",
        version="1.0.0",
        kind="generator",
        consumes=["ies.stage-input@1.0.0"],
        produces=["ies.solver-bundle.mps@1.0.0"],
        advanced={
            "supported_modes": ["planning"],
            "supported_precisions": ["medium"],
            "companions": {"solver": "ies.solver.highs@1.7.2"},
        },
        extensions={
            "supported_outputs": ["metrics"],
            "unit_system": "SI",
            "seed_semantics": "config.seed injected cleanly",
        },
    )
    gen_res = ecosystem_app.upload_plugin_zip_case(
        db,
        admin,
        zip_bytes=gen_zip,
        job_runner_client=jr_client,
        session_factory=db_factory,
        plugin_source_root=tmp_path / "builds",
        stage_root=tmp_path / "stages",
    )
    ecosystem_app.install_artifact_for_user_case(db, member, gen_res["id"], enabled=True)

    # Widened output ("solution" not in supported_outputs=["metrics"]) MUST be rejected!
    bad_cfg = {
        "schema": "ies.calculation-config",
        "schema_version": "1.0.0",
        "mode": "planning",
        "predictions": [],
        "generator": "acme.generator.custom_milp@1.0.0",
        "solver": "ies.solver.highs@1.7.2",
        "algorithm": None,
        "evaluators": [],
        "precision": "medium",
        "time_limit_seconds": 60,
        "options": {"generator": {}, "solver": {}},
        "seed": 42,
        "outputs": ["solution"],
    }
    with pytest.raises(AppError):
        config_app.save_calculation_config_case(
            db,
            member,
            proj.id,
            bad_cfg,
            expected_revision=None,
            predictor_registry=build_predictor_registry((NAIVE_PREDICTOR,)),
            generator_registry=build_generator_registry((HIGHS_MILP_PROVIDER,)),
            solver_registry=build_solver_registry((BUILTIN_SOLVER_HIGHS_SPEC,)),
            evaluator_registry=build_evaluator_registry((BUILTIN_EVALUATOR_FINANCE_SPEC,)),
            algorithm_registry=build_algorithm_registry(()),
        )

    ok_cfg = {**bad_cfg, "outputs": ["metrics"]}
    saved_gen_cfg = config_app.save_calculation_config_case(
        db,
        member,
        proj.id,
        ok_cfg,
        expected_revision=None,
        predictor_registry=build_predictor_registry((NAIVE_PREDICTOR,)),
        generator_registry=build_generator_registry((HIGHS_MILP_PROVIDER,)),
        solver_registry=build_solver_registry((BUILTIN_SOLVER_HIGHS_SPEC,)),
        evaluator_registry=build_evaluator_registry((BUILTIN_EVALUATOR_FINANCE_SPEC,)),
        algorithm_registry=build_algorithm_registry(()),
    )
    assert saved_gen_cfg["revision"] == 1

    # 4. Real pies-sdk pack fixture (`in:` + `expected:`) passes upload & --network=none selftest
    sdk_root = repo_root / "sdk"
    if (sdk_root / "pies_sdk").is_dir():
        import sys

        if str(sdk_root) not in sys.path:
            sys.path.insert(0, str(sdk_root))
        from pies_sdk.packaging import pack_plugin_package

        pkg_src = tmp_path / "packed_plugin"
        (pkg_src / "tests" / "dir_case" / "in").mkdir(parents=True, exist_ok=True)
        (pkg_src / "tests" / "dir_case" / "expected").mkdir(parents=True, exist_ok=True)
        (pkg_src / "plugin.yaml").write_text(
            (
                "schema: ies.algorithm-plugin\n"
                'schema_version: "1.0.0"\n'
                "plugin_id: acme.algorithm.sdk_packed\n"
                'version: "1.0.0"\n'
                "kind: algorithm\n"
                "name:\n  en_us: SDK Packed Plugin\n"
                "description: Packed by pies-sdk pack\n"
                "license: MPL-2.0\n"
                "compatibility:\n  pies: '>=1.2.0,<2.0.0'\n"
                "consumes:\n  - ies.stage-input@1.0.0\n"
                "produces:\n  - ies.engineering-solution@1.0.0\n"
                "runtime:\n  language: python\n  python: '3.12'\n"
                "entrypoint: algo:run\n"
                "parameters_schema:\n"
                "  type: object\n"
                "  additionalProperties: false\n"
                "  properties: {}\n"
                "resources:\n"
                "  default:\n    cpu: 1\n    memory_mb: 512\n"
                "    timeout_seconds: 60\n    process_limit: 8\n"
                "  maximum:\n    cpu: 2\n    memory_mb: 1024\n"
                "    timeout_seconds: 120\n    process_limit: 16\n"
                "tests:\n"
                "  - id: dir_case\n"
                "    in: tests/dir_case/in\n"
                "    expected: tests/dir_case/expected\n"
            ),
            encoding="utf-8",
        )
        (pkg_src / "algo.py").write_text("def run(ctx):\n    return {'ok': True}\n", encoding="utf-8")
        (pkg_src / "tests" / "dir_case" / "in" / "request.json").write_text("{}", encoding="utf-8")
        exp_dir_sol = b'{"ok": true}'
        (pkg_src / "tests" / "dir_case" / "expected" / "solution.json").write_bytes(exp_dir_sol)

        packed = pack_plugin_package(
            pkg_src,
            output_path=tmp_path / "acme.algorithm.sdk_packed-1.0.0.algorithm.zip",
            run_validate=False,
        )
        sdk_transport = _FakeJobRunnerTransport(
            stage_root=tmp_path / "stages",
            plugin_source_root=tmp_path / "builds",
            stage_outputs={"test-dir_case": {"solution.json": exp_dir_sol}},
        )
        packed_res = ecosystem_app.upload_plugin_zip_case(
            db,
            admin,
            zip_bytes=Path(packed["archive_path"]).read_bytes(),
            expected_sha256=packed["sha256"],
            job_runner_client=sdk_transport.make_client(),
            session_factory=db_factory,
            plugin_source_root=tmp_path / "builds",
            stage_root=tmp_path / "stages",
        )
        assert packed_res["install_status"] == "ready"
        assert len(sdk_transport.start_calls) == 1


def test_lifecycle_single_active_overwrite_stale_build_and_retryable_cleanup(
    db_factory: sessionmaker[Session],
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from iesplan.application import projects as projects_app
    from iesplan.application.ecosystem import device_models as eco_dev_mod
    from iesplan.application.ecosystem import plugins as eco_plg_mod

    admin = make_user(db, "life_admin", role="admin")
    member = make_user(db, "life_member", role="engineer")
    proj = projects_app.create_project(
        db,
        member,
        name="Single Active Device Proj",
        baseline_resolution="1h",
        baseline_start_year=2025,
        baseline_scenario_mode="single",
    )
    db.commit()

    ns = generate_namespace()
    dev_id = f"user.{ns}.device.single_hp"
    imp1 = ecosystem_app.import_device_model_case(
        db, admin, yaml_text=_sample_device_yaml(dev_id, power_rating=3.5), revision=1
    )
    ecosystem_app.install_artifact_for_user_case(db, member, imp1["id"], enabled=True)

    # 1. lifecycle.update_draft accepts installed ecosystem device without writing draft pins
    upd1 = projects_app.update_draft(
        db,
        member,
        proj.id,
        commands=[
            {
                "id": "cmd-eco-dev-1",
                "unit": "model",
                "type": "model.upsert_device",
                "payload": {
                    "name": "hp1",
                    "device_type": dev_id,
                    "kind": "new",
                    "params": {"cop": 3.5},
                },
            }
        ],
        expected_revision=1,
    )
    assert upd1["revision"] == 2
    draft_doc = projects_app.get_current_draft_content(db, proj.id)
    assert "local_device_models" not in (draft_doc.get("extensions") or {})

    # Overwrite upgrade to revision=2 updates active row in-place; selector & submission resolve revision=2
    imp2 = ecosystem_app.import_device_model_case(
        db, admin, yaml_text=_sample_device_yaml(dev_id, power_rating=6.5), revision=2
    )
    assert imp2["id"] == imp1["id"]
    sel_after = models_app.list_project_device_types(db, member, proj.id)
    by_id = {item["type_id"]: item for item in sel_after["items"]}
    assert by_id[dev_id]["local_revision"] == 2
    assert by_id[dev_id]["parameters"]["cop"]["default"] == 6.5

    frozen_vers, loaded_docs = ecosystem_app.verify_submission_ecosystem_artifacts(
        db,
        user_id=member.id,
        calc_config={},
        device_type_ids={dev_id},
    )
    assert frozen_vers == {dev_id: "2"}
    assert loaded_docs[dev_id].properties["cop"].value == 6.5

    # Export does NOT invoke full parse_device_model_v2 re-validation
    def _forbid_reparse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("export must not redundantly re-run parse_device_model_v2")

    with monkeypatch.context() as mp:
        mp.setattr(eco_dev_mod, "parse_device_model_v2", _forbid_reparse)
        exported = ecosystem_app.export_device_model_case(db, member, artifact_id=imp2["id"])
        assert exported["revision"] == 2

    # 2. Concurrent approve blocked while building, and stale build cannot overwrite disabled -> ready
    ecosystem_app.update_ecosystem_settings_case(
        db,
        admin,
        market_mode="local",
        local_upload_requires_approval=True,
    )
    pending_zip = _build_plugin_zip_bytes(
        plugin_id=f"user.{ns}.algorithm.stale_race", version="1.0.0"
    )
    pending_plg = ecosystem_app.upload_plugin_zip_case(db, member, zip_bytes=pending_zip)
    assert pending_plg["install_status"] == "pending"

    # Simulate concurrent admin disabling during build_plugin before testing/ready commit
    race_transport = _FakeJobRunnerTransport(
        stage_root=tmp_path / "stages", plugin_source_root=tmp_path / "builds"
    )

    class _InterruptingJR:
        def __init__(self, inner: JobRunnerClient) -> None:
            self.inner = inner

        def build_plugin(self, *args: Any, **kwargs: Any) -> Any:
            res = self.inner.build_plugin(*args, **kwargs)
            with db_factory() as race_db:
                # Duplicate approve during building MUST return 409 conflict
                with pytest.raises(EcosystemConflictError):
                    ecosystem_app.approve_artifact_case(
                        race_db, admin, pending_plg["id"], job_runner_client=self.inner
                    )
                # Disable artifact mid-build
                ecosystem_app.uninstall_artifact_image_case(
                    race_db, admin, pending_plg["id"], job_runner_client=self.inner
                )
            return res

        def delete_plugin_image(self, *args: Any, **kwargs: Any) -> Any:
            return self.inner.delete_plugin_image(*args, **kwargs)

    # Approve must NOT call _inspect_plugin_zip again (no redundant post-ingress validation)
    def _forbid_reinspect(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("approve must not re-run _inspect_plugin_zip")

    with monkeypatch.context() as mp:
        mp.setattr(eco_plg_mod, "_inspect_plugin_zip", _forbid_reinspect)
        with pytest.raises(EcosystemConflictError):
            ecosystem_app.approve_artifact_case(
                db,
                admin,
                pending_plg["id"],
                job_runner_client=_InterruptingJR(race_transport.make_client()),  # type: ignore[arg-type]
                session_factory=db_factory,
                plugin_source_root=tmp_path / "builds",
                stage_root=tmp_path / "stages",
            )
    race_rec = ecosystem_domain.get_artifact(db, pending_plg["id"])
    assert race_rec is not None
    assert race_rec.install_status == "disabled"
    assert len(race_transport.delete_calls) == 1

    # 3. Failed delete_plugin_image during selftest failure preserves image_ref and remains retryable
    base_transport = _FakeJobRunnerTransport(
        stage_root=tmp_path / "stages",
        plugin_source_root=tmp_path / "builds",
        stage_outputs={"test-single_json": {"solution.json": b'{"mismatch": true}'}},
    )

    class _FailFirstDeleteJR:
        def __init__(self, inner: JobRunnerClient) -> None:
            self.inner = inner
            self.fail_delete = True

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

        def delete_plugin_image(self, plugin_id: str, version: str, *, force: bool = False) -> Any:
            if self.fail_delete:
                self.fail_delete = False
                raise RuntimeError("transient docker daemon timeout")
            return self.inner.delete_plugin_image(plugin_id, version, force=force)

    proxy_jr = _FailFirstDeleteJR(base_transport.make_client())
    fail_zip = _build_plugin_zip_bytes(
        plugin_id="acme.algorithm.cleanup_retry",
        version="1.0.0",
        tests=[
            {
                "id": "single_json",
                "input": "tests/single/input.json",
                "expected": "tests/single/expected.json",
            }
        ],
        dir_mode_outputs={
            "tests/single/input.json": b"{}",
            "tests/single/expected.json": b'{"ok": true}',
        },
    )
    with pytest.raises(AppError) as clean_err:
        ecosystem_app.upload_plugin_zip_case(
            db,
            admin,
            zip_bytes=fail_zip,
            job_runner_client=proxy_jr,  # type: ignore[arg-type]
            session_factory=db_factory,
            plugin_source_root=tmp_path / "builds",
            stage_root=tmp_path / "stages",
        )
    assert clean_err.value.code == "ECO-IMAGE-CLEANUP-FAILED"
    kept_rec = ecosystem_domain.get_artifact_by_identity(
        db, "algorithm", "acme.algorithm.cleanup_retry", "1.0.0"
    )
    assert kept_rec is not None
    assert kept_rec.install_status == "build_failed"
    assert kept_rec.image_ref == "pies-plugin/acme.algorithm.cleanup_retry:1.0.0"
    assert kept_rec.diagnostics["cleanup_retryable"] is True

    # Retry cleanup via uninstall_artifact_image_case -> succeeds and clears image_ref
    retried = ecosystem_app.uninstall_artifact_image_case(
        db, admin, kept_rec.id, job_runner_client=proxy_jr  # type: ignore[arg-type]
    )
    assert retried["image_deleted"] is True
    assert retried["artifact"]["image_ref"] is None

    # 4. Disabling member install blocks submission for consumed local device
    ecosystem_app.set_member_artifact_enabled_case(db, member, imp2["id"], enabled=False)
    with pytest.raises(AppError) as missing_dev_err:
        ecosystem_app.verify_submission_ecosystem_artifacts(
            db,
            user_id=member.id,
            calc_config={},
            device_type_ids={dev_id},
        )
    assert missing_dev_err.value.code == "TASK-REQ-007"

    # 5. Build temp-dir rmtree failure is surfaced even when image build succeeded
    ok_transport = _FakeJobRunnerTransport(
        stage_root=tmp_path / "stages", plugin_source_root=tmp_path / "plugin_builds"
    )

    def _boom_rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
        if "plugin_builds" in str(path):
            raise OSError("simulated build dir cleanup EBUSY")

    with monkeypatch.context() as mp:
        mp.setattr(eco_plg_mod.shutil, "rmtree", _boom_rmtree)
        rmtree_zip = _build_plugin_zip_bytes(plugin_id="acme.algorithm.rmtree_fail", version="1.0.0")
        with pytest.raises(EcosystemValidationError) as rmtree_err:
            ecosystem_app.upload_plugin_zip_case(
                db,
                admin,
                zip_bytes=rmtree_zip,
                job_runner_client=ok_transport.make_client(),
                session_factory=db_factory,
                plugin_source_root=tmp_path / "plugin_builds",
                stage_root=tmp_path / "stages",
            )
        assert rmtree_err.value.code == "ECO-BUILD-CLEANUP"


def test_codex_r3_queued_build_version_binding_replace_order_and_collaborator_precheck(
    db_factory: sessionmaker[Session],
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from iesplan.application import projects as projects_app
    from iesplan.application.ecosystem import plugins as eco_plg_mod
    from iesplan.application.validations import run_validation_case

    admin = make_user(db, "r3_admin", role="admin")
    owner = make_user(db, "r3_owner", role="engineer")
    db.commit()

    # 1. Queued v1 build overwritten by v2 MUST NOT tag/overwrite v2 state or image
    class _DeferredScheduler:
        def __init__(self) -> None:
            self.jobs: list[Any] = []

        def submit_plugin_build(self, fn: Any) -> Any:
            self.jobs.append(fn)
            return SimpleNamespace(result=fn)

    pid = "acme.algorithm.r3_queue"
    zip_v1 = _build_plugin_zip_bytes(plugin_id=pid, version="1.0.0")
    zip_v2 = _build_plugin_zip_bytes(plugin_id=pid, version="2.0.0")
    sched_v1 = _DeferredScheduler()
    queued_v1 = ecosystem_app.upload_plugin_zip_case(
        db, admin, zip_bytes=zip_v1, scheduler=sched_v1, wait_for_build=False,
        job_runner_client=_FakeJobRunnerTransport(
            stage_root=tmp_path / "r3_stages", plugin_source_root=tmp_path / "r3_builds"
        ).make_client(),
        session_factory=db_factory,
        plugin_source_root=tmp_path / "r3_builds",
        stage_root=tmp_path / "r3_stages",
    )
    art_id = queued_v1["id"]
    assert queued_v1["install_status"] == "building"
    assert len(sched_v1.jobs) == 1

    # Overwrite to v2 before the stale v1 build job executes
    jr_v2 = _FakeJobRunnerTransport(
        stage_root=tmp_path / "r3_stages", plugin_source_root=tmp_path / "r3_builds"
    ).make_client()
    upgraded_v2 = ecosystem_app.upload_plugin_zip_case(
        db,
        admin,
        zip_bytes=zip_v2,
        job_runner_client=jr_v2,
        session_factory=db_factory,
        plugin_source_root=tmp_path / "r3_builds",
        stage_root=tmp_path / "r3_stages",
    )
    assert upgraded_v2["version"] == "2.0.0"
    assert upgraded_v2["install_status"] == "ready"

    # Now execute the real queued v1 job bound to expected_version="1.0.0"
    with pytest.raises(EcosystemConflictError):
        sched_v1.jobs[0]()
    after_stale = ecosystem_domain.get_artifact(db, art_id)
    assert after_stale is not None
    assert after_stale.version == "2.0.0"
    assert after_stale.install_status == "ready"
    assert after_stale.image_ref == f"pies-plugin/{pid}:2.0.0"

    # Also guard mid-build overwrite between initial check and testing commit
    class _MidBuildOverwriteJR:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def build_plugin(self, plugin_id: str, ver: str, *args: Any, **kwargs: Any) -> Any:
            res = self.inner.build_plugin(plugin_id, ver, *args, **kwargs)
            if ver == "1.0.0":
                with db_factory() as race_db:
                    ecosystem_domain.update_artifact(
                        race_db, race_art.id, version="2.0.0", install_status="ready",
                        image_ref=f"pies-plugin/{plugin_id}:2.0.0",
                    )
                    race_db.commit()
            return res

        def delete_plugin_image(self, *args: Any, **kwargs: Any) -> Any:
            return self.inner.delete_plugin_image(*args, **kwargs)

    race_pid = "acme.algorithm.r3_midrace"
    race_zip_v1 = _build_plugin_zip_bytes(plugin_id=race_pid, version="1.0.0")
    race_transport = _FakeJobRunnerTransport(
        stage_root=tmp_path / "r3_stages", plugin_source_root=tmp_path / "r3_builds"
    )
    sched_race = _DeferredScheduler()
    race_rec = ecosystem_app.upload_plugin_zip_case(
        db, admin, zip_bytes=race_zip_v1, scheduler=sched_race, wait_for_build=False,
        job_runner_client=_MidBuildOverwriteJR(race_transport.make_client()),
        session_factory=db_factory,
        plugin_source_root=tmp_path / "r3_builds",
        stage_root=tmp_path / "r3_stages",
    )
    race_art = ecosystem_domain.get_artifact(db, race_rec["id"])
    assert race_art is not None
    assert race_art.install_status == "building"
    with pytest.raises(EcosystemConflictError):
        sched_race.jobs[0]()
    assert any(c.get("version") == "1.0.0" for c in race_transport.delete_calls)
    protected_v2 = ecosystem_domain.get_artifact(db, race_art.id)
    assert protected_v2 is not None
    assert protected_v2.version == "2.0.0"
    assert protected_v2.install_status == "ready"
    assert protected_v2.image_ref == f"pies-plugin/{race_pid}:2.0.0"

    # 2. Replacement DB failure MUST NOT delete old ready image; post-commit delete fail preserves retry
    ord_pid = "acme.algorithm.r3_order"
    ord_transport = _FakeJobRunnerTransport(
        stage_root=tmp_path / "r3_stages", plugin_source_root=tmp_path / "r3_builds"
    )
    ord_v1 = ecosystem_app.upload_plugin_zip_case(
        db,
        admin,
        zip_bytes=_build_plugin_zip_bytes(plugin_id=ord_pid, version="1.0.0"),
        job_runner_client=ord_transport.make_client(),
        session_factory=db_factory,
        plugin_source_root=tmp_path / "r3_builds",
        stage_root=tmp_path / "r3_stages",
    )
    assert len(ord_transport.delete_calls) == 0

    def _fail_put(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated object store outage before commit")

    with monkeypatch.context() as mp:
        mp.setattr(eco_plg_mod, "put_object", _fail_put)
        with pytest.raises(RuntimeError, match="simulated object store outage"):
            ecosystem_app.upload_plugin_zip_case(
                db,
                admin,
                zip_bytes=_build_plugin_zip_bytes(plugin_id=ord_pid, version="2.0.0"),
                job_runner_client=ord_transport.make_client(),
                session_factory=db_factory,
                plugin_source_root=tmp_path / "r3_builds",
                stage_root=tmp_path / "r3_stages",
            )
    assert len(ord_transport.delete_calls) == 0
    still_v1 = ecosystem_domain.get_artifact(db, ord_v1["id"])
    assert still_v1 is not None
    assert still_v1.version == "1.0.0"
    assert still_v1.install_status == "ready"
    assert still_v1.image_ref == f"pies-plugin/{ord_pid}:1.0.0"

    class _FailOldDeleteJR:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

        def delete_plugin_image(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("docker rm timeout post-commit")

    with pytest.raises(AppError) as post_del_err:
        ecosystem_app.upload_plugin_zip_case(
            db,
            admin,
            zip_bytes=_build_plugin_zip_bytes(plugin_id=ord_pid, version="2.0.0"),
            job_runner_client=_FailOldDeleteJR(ord_transport.make_client()),  # type: ignore[arg-type]
            session_factory=db_factory,
            plugin_source_root=tmp_path / "r3_builds",
            stage_root=tmp_path / "r3_stages",
        )
    assert post_del_err.value.code == "ECO-IMAGE-CLEANUP-FAILED"
    post_rec = ecosystem_domain.get_artifact(db, ord_v1["id"])
    assert post_rec is not None
    assert post_rec.version == "2.0.0"
    assert post_rec.install_status == "build_failed"
    assert post_rec.diagnostics is not None
    assert post_rec.diagnostics["cleanup_retryable"] is True
    assert post_rec.diagnostics["stale_image_ref"] == f"pies-plugin/{ord_pid}:1.0.0"
    assert post_rec.diagnostics["stale_version"] == "1.0.0"

    # 2b. Retrying same-version upload cleans stale_image_ref, clears markers, and rebuilds v2 -> ready
    ord_retry_transport = _FakeJobRunnerTransport(
        stage_root=tmp_path / "r3_stages", plugin_source_root=tmp_path / "r3_builds"
    )
    recovered_v2 = ecosystem_app.upload_plugin_zip_case(
        db,
        admin,
        zip_bytes=_build_plugin_zip_bytes(plugin_id=ord_pid, version="2.0.0"),
        job_runner_client=ord_retry_transport.make_client(),
        session_factory=db_factory,
        plugin_source_root=tmp_path / "r3_builds",
        stage_root=tmp_path / "r3_stages",
    )
    assert recovered_v2["version"] == "2.0.0"
    assert recovered_v2["install_status"] == "ready"
    assert any(c.get("version") == "1.0.0" for c in ord_retry_transport.delete_calls)
    recovered_rec = ecosystem_domain.get_artifact(db, ord_v1["id"])
    assert recovered_rec is not None
    assert "stale_image_ref" not in (recovered_rec.diagnostics or {})

    # 2c. Uninstall retries stale_image_ref even when image_ref is None
    uninst_pid = "acme.algorithm.r4_uninst_stale"
    uninst_transport = _FakeJobRunnerTransport(
        stage_root=tmp_path / "r3_stages", plugin_source_root=tmp_path / "r3_builds"
    )
    uninst_v1 = ecosystem_app.upload_plugin_zip_case(
        db,
        admin,
        zip_bytes=_build_plugin_zip_bytes(plugin_id=uninst_pid, version="1.0.0"),
        job_runner_client=uninst_transport.make_client(),
        session_factory=db_factory,
        plugin_source_root=tmp_path / "r3_builds",
        stage_root=tmp_path / "r3_stages",
    )
    with pytest.raises(AppError) as uninst_stale_err:
        ecosystem_app.upload_plugin_zip_case(
            db,
            admin,
            zip_bytes=_build_plugin_zip_bytes(plugin_id=uninst_pid, version="2.0.0"),
            job_runner_client=_FailOldDeleteJR(uninst_transport.make_client()),  # type: ignore[arg-type]
            session_factory=db_factory,
            plugin_source_root=tmp_path / "r3_builds",
            stage_root=tmp_path / "r3_stages",
        )
    assert uninst_stale_err.value.code == "ECO-IMAGE-CLEANUP-FAILED"
    uninst_res = ecosystem_app.uninstall_artifact_image_case(
        db, admin, uninst_v1["id"], job_runner_client=uninst_transport.make_client()
    )
    assert uninst_res["image_deleted"] is True
    assert any(c.get("version") == "1.0.0" for c in uninst_transport.delete_calls)
    uninst_rec = ecosystem_domain.get_artifact(db, uninst_v1["id"])
    assert uninst_rec is not None
    assert uninst_rec.install_status == "disabled"
    assert uninst_rec.diagnostics is None

    # 2d. Approval & v2->v3 overwrite also retry and clear stale_image_ref before clearing diagnostics
    appr_pid = "acme.algorithm.r4_appr_stale"
    appr_transport = _FakeJobRunnerTransport(
        stage_root=tmp_path / "r3_stages", plugin_source_root=tmp_path / "r3_builds"
    )
    appr_v1 = ecosystem_app.upload_plugin_zip_case(
        db,
        admin,
        zip_bytes=_build_plugin_zip_bytes(plugin_id=appr_pid, version="1.0.0"),
        job_runner_client=appr_transport.make_client(),
        session_factory=db_factory,
        plugin_source_root=tmp_path / "r3_builds",
        stage_root=tmp_path / "r3_stages",
    )
    ecosystem_app.update_ecosystem_settings_case(
        db, admin, market_mode="local", local_upload_requires_approval=True
    )
    try:
        with pytest.raises(AppError) as pend_err:
            ecosystem_app.upload_plugin_zip_case(
                db,
                owner,
                zip_bytes=_build_plugin_zip_bytes(plugin_id=appr_pid, version="2.0.0"),
                job_runner_client=_FailOldDeleteJR(appr_transport.make_client()),  # type: ignore[arg-type]
                session_factory=db_factory,
                plugin_source_root=tmp_path / "r3_builds",
                stage_root=tmp_path / "r3_stages",
            )
        assert pend_err.value.code == "ECO-IMAGE-CLEANUP-FAILED"
        # First approve while delete still fails -> preserves pending & stale_image_ref
        with pytest.raises(AppError) as appr_fail_err:
            ecosystem_app.approve_artifact_case(
                db,
                admin,
                appr_v1["id"],
                job_runner_client=_FailOldDeleteJR(appr_transport.make_client()),  # type: ignore[arg-type]
                session_factory=db_factory,
                plugin_source_root=tmp_path / "r3_builds",
                stage_root=tmp_path / "r3_stages",
            )
        assert appr_fail_err.value.code == "ECO-IMAGE-CLEANUP-FAILED"
        still_pend = ecosystem_domain.get_artifact(db, appr_v1["id"])
        assert still_pend is not None
        assert still_pend.approval_status == "pending"
        assert (still_pend.diagnostics or {}).get("stale_image_ref") == f"pies-plugin/{appr_pid}:1.0.0"

        # Subsequent v2->v3 overwrite cleans v1 stale_image_ref before installing v3
        v3_transport = _FakeJobRunnerTransport(
            stage_root=tmp_path / "r3_stages", plugin_source_root=tmp_path / "r3_builds"
        )
        v3_res = ecosystem_app.upload_plugin_zip_case(
            db,
            admin,
            zip_bytes=_build_plugin_zip_bytes(plugin_id=appr_pid, version="3.0.0"),
            job_runner_client=v3_transport.make_client(),
            session_factory=db_factory,
            plugin_source_root=tmp_path / "r3_builds",
            stage_root=tmp_path / "r3_stages",
        )
        assert v3_res["version"] == "3.0.0"
        assert v3_res["install_status"] == "ready"
        assert any(c.get("version") == "1.0.0" for c in v3_transport.delete_calls)

        # Also test direct approve recovery on a pending stale record
        appr_pid2 = "acme.algorithm.r4_appr_direct"
        appr2_v1 = ecosystem_app.upload_plugin_zip_case(
            db,
            admin,
            zip_bytes=_build_plugin_zip_bytes(plugin_id=appr_pid2, version="1.0.0"),
            job_runner_client=appr_transport.make_client(),
            session_factory=db_factory,
            plugin_source_root=tmp_path / "r3_builds",
            stage_root=tmp_path / "r3_stages",
        )
        with pytest.raises(AppError):
            ecosystem_app.upload_plugin_zip_case(
                db,
                owner,
                zip_bytes=_build_plugin_zip_bytes(plugin_id=appr_pid2, version="2.0.0"),
                job_runner_client=_FailOldDeleteJR(appr_transport.make_client()),  # type: ignore[arg-type]
                session_factory=db_factory,
                plugin_source_root=tmp_path / "r3_builds",
                stage_root=tmp_path / "r3_stages",
            )
        appr_ok_transport = _FakeJobRunnerTransport(
            stage_root=tmp_path / "r3_stages", plugin_source_root=tmp_path / "r3_builds"
        )
        appr2_ok = ecosystem_app.approve_artifact_case(
            db,
            admin,
            appr2_v1["id"],
            job_runner_client=appr_ok_transport.make_client(),
            session_factory=db_factory,
            plugin_source_root=tmp_path / "r3_builds",
            stage_root=tmp_path / "r3_stages",
        )
        assert appr2_ok["version"] == "2.0.0"
        assert appr2_ok["install_status"] == "ready"
        assert any(c.get("version") == "1.0.0" for c in appr_ok_transport.delete_calls)
        appr2_rec = ecosystem_domain.get_artifact(db, appr2_v1["id"])
        assert appr2_rec is not None
        assert "stale_image_ref" not in (appr2_rec.diagnostics or {})
    finally:
        ecosystem_app.update_ecosystem_settings_case(
            db, admin, market_mode="local", local_upload_requires_approval=False
        )

    # 3. Acting user precheck resolves acting user's enabled devices (not project.owner_id)
    ns = generate_namespace()
    collab_dev_id = f"user.{ns}.device.collab_hp"
    imp_dev = ecosystem_app.import_device_model_case(
        db, admin, yaml_text=_sample_device_yaml(collab_dev_id, power_rating=4.0), revision=1
    )
    ecosystem_app.install_artifact_for_user_case(db, owner, imp_dev["id"], enabled=True)
    ecosystem_app.set_member_artifact_enabled_case(db, admin, imp_dev["id"], enabled=False)
    collab_proj = projects_app.create_project(
        db,
        owner,
        name="Collab Precheck Proj",
        baseline_resolution="1h",
        baseline_start_year=2025,
        baseline_scenario_mode="single",
    )
    db.commit()
    projects_app.update_draft(
        db,
        owner,
        collab_proj.id,
        commands=[
            {
                "id": "cmd-collab-dev",
                "unit": "model",
                "type": "model.upsert_device",
                "payload": {
                    "name": "hp_collab",
                    "device_type": collab_dev_id,
                    "kind": "new",
                    "params": {"cop": 4.0},
                },
            }
        ],
        expected_revision=1,
    )
    # Admin has view access; when admin has NOT installed collab_dev_id, admin precheck raises
    from iesplan.core.errors import NotFoundError

    assert "report" in run_validation_case(db, owner, collab_proj.id)
    with pytest.raises(NotFoundError):
        run_validation_case(db, admin, collab_proj.id)

    # Flip installation: enable for admin and disable for owner -> admin succeeds, owner fails
    ecosystem_app.set_member_artifact_enabled_case(db, admin, imp_dev["id"], enabled=True)
    ecosystem_app.set_member_artifact_enabled_case(db, owner, imp_dev["id"], enabled=False)
    assert "report" in run_validation_case(db, admin, collab_proj.id)
    with pytest.raises(NotFoundError):
        run_validation_case(db, owner, collab_proj.id)



