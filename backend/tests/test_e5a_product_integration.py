"""E5a 产品集成测试：动态阶段目录、公共市场远端索引/安装、无状态投稿代理与第三方评价器 Excel 导出。"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from auth_helpers import login_headers, make_user
from fastapi.testclient import TestClient
from openpyxl import load_workbook
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
from iesplan import identity as identity_domain
from iesplan.api.deps import get_request_db
from iesplan.application import ecosystem as ecosystem_app
from iesplan.application.ecosystem import market_remote
from iesplan.application.packages.transfers import export_excel
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
from iesplan.config import settings as app_settings
from iesplan.db import Base
from iesplan.main import create_app
from iesplan.results.persistence import EvidencePackage, ResultAssessment, ResultIndex
from iesplan.storage import get_object, put_object
from iesplan.storage import service as storage_service
from iesplan.storage.adapters.filesystem import FileSystemBlobStore
from iesplan.tasks.persistence import CalcSnapshot, Task

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
    monkeypatch.setattr(app_settings, "stage_root", tmp_path / "stages")
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
        "  names: {zh-CN: 社区热泵, en-US: Community Heat Pump}\n"
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
    plugin_id: str = "acme.evaluator.carbon",
    version: str = "1.2.0",
    kind: str = "evaluator",
    description: str = "Carbon intensity post-evaluator",
    consumes: list[str] | None = None,
    produces: list[str] | None = None,
) -> bytes:
    consumes_list = consumes or ["ies.engineering-solution@1.0.0"]
    produces_list = produces or ["ies.evaluation-report@1.0.0"]
    lines = [
        "schema: ies.algorithm-plugin",
        'schema_version: "1.0.0"',
        f"plugin_id: {plugin_id}",
        f'version: "{version}"',
        f"kind: {kind}",
        "name:",
        "  zh_cn: 碳排评价器",
        "  en_us: Carbon Evaluator",
        f"description: {description}",
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
        "entrypoint: main:evaluate",
        "parameters_schema:",
        "  type: object",
        "  additionalProperties: false",
        "  properties:",
        "    grid_factor:",
        "      type: number",
        "      default: 0.58",
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
    files: dict[str, bytes] = {
        "plugin.yaml": ("\n".join(lines) + "\n").encode("utf-8"),
        "main.py": b"def evaluate(ctx):\n    return {'ok': True}\n",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for rel, content in sorted(files.items()):
            zf.writestr(rel, content)
    return buf.getvalue()


class _FakeJobRunnerTransport:
    """Mock job_runner HTTP transport for synchronous plugin build + image lifecycle."""

    def __init__(self, *, stage_root: Path) -> None:
        self.stage_root = stage_root
        self.build_calls: list[dict[str, Any]] = []

    def make_client(self) -> JobRunnerClient:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            if path == "/v1/plugins/build":
                self.build_calls.append(body)
                return httpx.Response(
                    200,
                    json={
                        "image": f"pies-plugin/{body['plugin_id']}:{body['version']}",
                        "plugin_id": body["plugin_id"],
                        "version": body["version"],
                        "built_at": "2026-11-28T00:00:00Z",
                        "logs": ["Built ok"],
                    },
                )
            raise AssertionError(f"Unexpected job_runner request: {request.method} {path}")

        return JobRunnerClient(
            "http://job_runner:8090",
            client=httpx.Client(
                base_url="http://job_runner:8090",
                transport=httpx.MockTransport(handler),
            ),
        )


@contextmanager
def _make_client(
    db: Session,
    db_factory: sessionmaker[Session],
    *,
    job_runner_client: JobRunnerClient | None = None,
) -> Iterator[TestClient]:
    app = create_app()

    def _override_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_request_db] = _override_db
    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.scheduler = None
        app.state.bootstrap_context = SimpleNamespace(
            job_runner_client=job_runner_client,
            session_factory=db_factory,
            predictor_registry=build_predictor_registry((NAIVE_PREDICTOR,)),
            generator_registry=build_generator_registry((HIGHS_MILP_PROVIDER,)),
            solver_registry=build_solver_registry((BUILTIN_SOLVER_HIGHS_SPEC,)),
            evaluator_registry=build_evaluator_registry((BUILTIN_EVALUATOR_FINANCE_SPEC,)),
            algorithm_registry=build_algorithm_registry(()),
        )
        yield client


def test_stage_catalog_merges_builtin_and_user_enabled_ready_plugins(
    db: Session,
    db_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    admin = make_user(db, "e5a_cat_admin", role="admin")
    member_a = make_user(db, "e5a_cat_a", role="engineer")
    member_b = make_user(db, "e5a_cat_b", role="engineer")
    db.commit()

    jr_client = _FakeJobRunnerTransport(stage_root=tmp_path / "stages").make_client()
    carbon_zip = _build_plugin_zip_bytes(
        plugin_id="acme.evaluator.carbon",
        version="1.2.0",
        kind="evaluator",
        description="Carbon emission analyzer",
    )
    algo_zip = _build_plugin_zip_bytes(
        plugin_id="acme.algorithm.joint",
        version="0.9.0",
        kind="algorithm",
        description="Joint metaheuristic planner",
        consumes=["ies.stage-input@1.0.0"],
        produces=["ies.engineering-solution@1.0.0"],
    )

    with _make_client(db, db_factory, job_runner_client=jr_client) as client:
        # Admin uploads & auto-approves carbon evaluator (enabled for admin)
        uploaded_carbon = ecosystem_app.upload_plugin_zip_case(
            db,
            admin,
            zip_bytes=carbon_zip,
            job_runner_client=jr_client,
            session_factory=db_factory,
            plugin_source_root=tmp_path / "builds",
            stage_root=tmp_path / "stages",
        )
        assert uploaded_carbon["install_status"] == "ready"
        # Admin uploads joint algorithm, then disables it for admin; member_b keeps pending upload
        uploaded_algo = ecosystem_app.upload_plugin_zip_case(
            db,
            admin,
            zip_bytes=algo_zip,
            job_runner_client=jr_client,
            session_factory=db_factory,
            plugin_source_root=tmp_path / "builds",
            stage_root=tmp_path / "stages",
        )
        assert uploaded_algo["install_status"] == "ready"

        # member_a installs ONLY carbon evaluator
        ecosystem_app.install_artifact_for_user_case(db, member_a, uploaded_carbon["id"], enabled=True)

        # Unapproved pending plugin by member_b must never show up in stage-catalog
        pending_zip = _build_plugin_zip_bytes(
            plugin_id="beta.predictor.solar",
            version="0.1.0",
            kind="predictor",
            description="Pending solar predictor",
            consumes=["ies.stage-input@1.0.0"],
            produces=["ies.prediction-output@1.0.0"],
        )
        ecosystem_app.upload_plugin_zip_case(
            db,
            member_b,
            zip_bytes=pending_zip,
            job_runner_client=jr_client,
            session_factory=db_factory,
            plugin_source_root=tmp_path / "builds",
            stage_root=tmp_path / "stages",
        )

        # Query stage-catalog as member_a
        resp_a = client.get("/api/ecosystem/stage-catalog", headers=login_headers(client, member_a))
        assert resp_a.status_code == 200, resp_a.text
        body_a = resp_a.json()
        assert set(body_a) == {"predictors", "generators", "solvers", "evaluators", "algorithms"}

        assert [x["ref"] for x in body_a["predictors"]] == ["ies.predictor.naive@1.0.0"]
        assert body_a["predictors"][0]["source"] == "builtin"
        assert body_a["generators"][0]["ref"] == "ies.generator.highs_milp@1.0.0"
        assert body_a["solvers"][0]["ref"] == "ies.solver.highs@1.7.2"
        assert body_a["algorithms"] == []

        eval_by_ref = {item["ref"]: item for item in body_a["evaluators"]}
        assert set(eval_by_ref) == {"ies.evaluator.finance@1.0.0", "acme.evaluator.carbon@1.2.0"}
        assert eval_by_ref["ies.evaluator.finance@1.0.0"]["source"] == "builtin"
        eco_item = eval_by_ref["acme.evaluator.carbon@1.2.0"]
        assert eco_item == {
            "ref": "acme.evaluator.carbon@1.2.0",
            "stable_id": "acme.evaluator.carbon",
            "version": "1.2.0",
            "description": "Carbon emission analyzer",
            "consumes": ["ies.engineering-solution@1.0.0"],
            "produces": ["ies.evaluation-report@1.0.0"],
            "parameters_schema": eco_item["parameters_schema"],
            "source": "ecosystem",
        }
        assert "grid_factor" in eco_item["parameters_schema"]["properties"]

        # Query stage-catalog as member_b: sees ONLY builtins (nothing installed for member_b)
        resp_b = client.get("/api/ecosystem/stage-catalog", headers=login_headers(client, member_b))
        assert resp_b.status_code == 200
        assert [x["ref"] for x in resp_b.json()["evaluators"]] == ["ies.evaluator.finance@1.0.0"]


def test_local_mode_blocks_remote_market_and_submission_with_zero_network(
    db: Session,
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = make_user(db, "e5a_loc_admin", role="admin")
    member = make_user(db, "e5a_loc_user", role="engineer")
    db.commit()

    # Configure market URLs while staying in default local mode
    ecosystem_app.update_ecosystem_settings_case(
        db,
        admin,
        market_mode="local",
        local_upload_requires_approval=True,
        market_index_url="https://market.example.test/index.json",
        submission_gateway_url="https://gw.example.test",
    )

    outbound_calls: list[str] = []

    def _forbidden_http_client(*, timeout: float = 30.0) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            outbound_calls.append(str(request.url))
            return httpx.Response(500)

        return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)

    monkeypatch.setattr(market_remote, "build_http_client", _forbidden_http_client)

    with _make_client(db, db_factory) as client:
        headers = login_headers(client, member)
        responses = [
            client.get("/api/ecosystem/market/index", headers=headers),
            client.post(
                "/api/ecosystem/market/install",
                json={"artifact_id": "acme.device.pump", "kind": "device_model"},
                headers=headers,
            ),
            client.post(
                "/api/ecosystem/market/submit/otp-request",
                json={"email": "noleak@example.com"},
                headers=headers,
            ),
            client.post(
                "/api/ecosystem/market/submit/otp-verify",
                json={
                    "email": "noleak@example.com",
                    "otp_code": "123456",
                    "challenge_token": "tok",
                },
                headers=headers,
            ),
            client.post(
                "/api/ecosystem/market/submit",
                data={"submission_token": "subtok", "kind": "device_model"},
                files={"file": ("pump.device.yaml", b"schema: ies.device-model\n", "text/yaml")},
                headers=headers,
            ),
        ]
        for resp in responses:
            assert resp.status_code == 403, resp.text
            err = resp.json()["error"]
            assert err["code"] == "ECO-MARKET-LOCAL-MODE"
            assert set(err) == {
                "code",
                "message_key",
                "severity",
                "blocking",
                "params",
                "location",
                "fix_hint_key",
                "ref_ids",
            }
        assert outbound_calls == []


def test_online_market_index_states_sha256_check_and_trusted_install_bypass(
    db: Session,
    db_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = make_user(db, "e5a_mkt_admin", role="admin")
    member = make_user(db, "e5a_mkt_user", role="engineer")
    other_member = make_user(db, "e5a_mkt_other", role="engineer")
    db.commit()

    # Switch to online while keeping local_upload_requires_approval=True (orthogonal!)
    ecosystem_app.update_ecosystem_settings_case(
        db,
        admin,
        market_mode="online",
        local_upload_requires_approval=True,
        market_index_url="https://market.example.test/index.json",
    )

    device_v1_bytes = _sample_device_yaml("acme.device.hpump", power_rating=3.2).encode("utf-8")
    device_v2_bytes = _sample_device_yaml("acme.device.hpump", power_rating=3.8).encode("utf-8")
    carbon_zip = _build_plugin_zip_bytes(
        plugin_id="acme.evaluator.carbon",
        version="1.2.0",
        kind="evaluator",
        description="Market carbon evaluator",
    )
    solver_zip = _build_plugin_zip_bytes(
        plugin_id="acme.solver.cbc",
        version="2.10.0",
        kind="solver",
        description="Market CBC solver",
        consumes=["ies.solver-bundle.mps@1.0.0"],
        produces=["ies.primal-solution@1.0.0"],
    )

    # Pre-install solver locally by admin (other_member does NOT install, member DOES not yet install)
    jr_client = _FakeJobRunnerTransport(stage_root=tmp_path / "stages").make_client()
    local_solver = ecosystem_app.upload_plugin_zip_case(
        db,
        admin,
        zip_bytes=solver_zip,
        job_runner_client=jr_client,
        session_factory=db_factory,
        plugin_source_root=tmp_path / "builds",
        stage_root=tmp_path / "stages",
    )
    # Pre-install device v1 by admin so member has update_available=True against v2 in market
    local_dev = ecosystem_app.import_device_model_case(
        db, admin, yaml_text=device_v1_bytes, revision=1, source_origin="local_upload"
    )
    ecosystem_app.install_artifact_for_user_case(db, member, local_dev["id"], enabled=True)

    tamper_digest_mode = {"tamper": False}
    market_index_doc = {
        "schema": "ies.market-index",
        "schema_version": "1.0.0",
        "generated_at": "2026-11-28T00:00:00Z",
        "artifacts": [
            {
                "id": "acme.device.hpump",
                "kind": "device_model",
                "revision": 2,
                "name": {"zh_cn": "热泵 v2", "en_us": "Heat Pump v2"},
                "description": "Updated heat pump",
                "publisher": "acme",
                "license": "MPL-2.0",
                "compatibility": {"pies": ">=1.2.0,<2.0.0"},
                "url": "artifacts/acme/acme.device.hpump/r2/acme.device.hpump.device.yaml",
                "readme_url": "artifacts/acme/acme.device.hpump/r2/README.md",
                "sha256": hashlib.sha256(device_v2_bytes).hexdigest(),
                "size_bytes": len(device_v2_bytes),
            },
            {
                "id": "acme.evaluator.carbon",
                "kind": "evaluator",
                "version": "1.2.0",
                "name": {"zh_cn": "碳排评价", "en_us": "Carbon Eval"},
                "description": "Market carbon evaluator",
                "publisher": "acme",
                "license": "MPL-2.0",
                "compatibility": {"pies": ">=1.2.0,<2.0.0"},
                "consumes": ["ies.engineering-solution@1.0.0"],
                "produces": ["ies.evaluation-report@1.0.0"],
                "url": "artifacts/acme/acme.evaluator.carbon/1.2.0/acme.evaluator.carbon-1.2.0.algorithm.zip",
                "sha256": hashlib.sha256(carbon_zip).hexdigest(),
                "size_bytes": len(carbon_zip),
            },
            {
                "id": "acme.solver.cbc",
                "kind": "solver",
                "version": "2.10.0",
                "name": {"zh_cn": "CBC 求解器", "en_us": "CBC Solver"},
                "description": "Market CBC solver",
                "publisher": "acme",
                "license": "EPL-2.0",
                "compatibility": {"pies": ">=1.2.0,<2.0.0"},
                "consumes": ["ies.solver-bundle.mps@1.0.0"],
                "produces": ["ies.primal-solution@1.0.0"],
                "url": "artifacts/acme/acme.solver.cbc/2.10.0/acme.solver.cbc-2.10.0.algorithm.zip",
                "sha256": hashlib.sha256(solver_zip).hexdigest(),
                "size_bytes": len(solver_zip),
            },
        ],
    }

    def market_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/index.json":
            return httpx.Response(200, json=market_index_doc)
        if path.endswith("acme.device.hpump.device.yaml"):
            payload = b"corrupted-device-bytes" if tamper_digest_mode["tamper"] else device_v2_bytes
            return httpx.Response(200, content=payload)
        if path.endswith("acme.evaluator.carbon-1.2.0.algorithm.zip"):
            return httpx.Response(200, content=carbon_zip)
        return httpx.Response(404)

    monkeypatch.setattr(
        market_remote,
        "build_http_client",
        lambda *, timeout=30.0: httpx.Client(
            transport=httpx.MockTransport(market_handler), timeout=timeout
        ),
    )

    with _make_client(db, db_factory, job_runner_client=jr_client) as client:
        member_headers = login_headers(client, member)

        # 1. Inspect 3 member states in GET /api/ecosystem/market/index
        idx_resp = client.get("/api/ecosystem/market/index", headers=member_headers)
        assert idx_resp.status_code == 200, idx_resp.text
        idx_body = idx_resp.json()
        assert idx_body["generated_at"] == "2026-11-28T00:00:00Z"
        assert idx_body["market_index_url"] == "https://market.example.test/index.json"
        by_id = {item["id"]: item for item in idx_body["items"]}

        assert by_id["acme.device.hpump"]["member_state"] == "local_installed"
        assert by_id["acme.device.hpump"]["local_artifact_id"] == local_dev["id"]
        assert by_id["acme.device.hpump"]["local_version"] == "1"
        assert by_id["acme.device.hpump"]["update_available"] is True
        assert by_id["acme.device.hpump"]["readme_url"] == (
            "https://market.example.test/artifacts/acme/acme.device.hpump/r2/README.md"
        )

        assert by_id["acme.solver.cbc"]["member_state"] == "local_uninstalled"
        assert by_id["acme.solver.cbc"]["local_artifact_id"] == local_solver["id"]
        assert by_id["acme.solver.cbc"]["local_version"] == "2.10.0"
        assert by_id["acme.solver.cbc"]["update_available"] is False

        assert by_id["acme.evaluator.carbon"]["member_state"] == "remote"
        assert by_id["acme.evaluator.carbon"]["local_artifact_id"] is None
        assert by_id["acme.evaluator.carbon"]["local_version"] is None

        # 2. Tampered download rejected by authoritative SHA-256 verification before DB write
        tamper_digest_mode["tamper"] = True
        bad_resp = client.post(
            "/api/ecosystem/market/install",
            json={"artifact_id": "acme.device.hpump", "kind": "device_model", "version_or_revision": 2},
            headers=member_headers,
        )
        assert bad_resp.status_code == 400, bad_resp.text
        assert bad_resp.json()["error"]["code"] == "ECO-SHA256-MISMATCH"
        cur_dev = ecosystem_domain.get_artifact_by_stable_id(
            db, "device_model", "acme.device.hpump"
        )
        assert cur_dev is not None and cur_dev.version == "1"

        # 3. Valid download succeeds & auto-approves despite local_upload_requires_approval=True
        tamper_digest_mode["tamper"] = False
        ok_dev_resp = client.post(
            "/api/ecosystem/market/install",
            json={"artifact_id": "acme.device.hpump", "kind": "device_model", "version_or_revision": 2},
            headers=member_headers,
        )
        assert ok_dev_resp.status_code == 200, ok_dev_resp.text
        ok_dev = ok_dev_resp.json()
        assert ok_dev["installed"] is True
        assert ok_dev["artifact"]["source_origin"] == "online_market"
        assert ok_dev["artifact"]["approval_status"] == "approved"
        assert ok_dev["artifact"]["install_status"] == "ready"
        assert ok_dev["artifact"]["version"] == "2"

        # 4. Install plugin as non-admin -> auto-approved, built, enabled, visible in catalog
        ok_plug_resp = client.post(
            "/api/ecosystem/market/install",
            json={"artifact_id": "acme.evaluator.carbon", "kind": "evaluator"},
            headers=member_headers,
        )
        assert ok_plug_resp.status_code == 200, ok_plug_resp.text
        ok_plug = ok_plug_resp.json()
        assert ok_plug["installed"] is True
        assert ok_plug["artifact"]["source_origin"] == "online_market"
        assert ok_plug["artifact"]["approval_status"] == "approved"
        assert ok_plug["artifact"]["install_status"] == "ready"
        assert ok_plug["artifact"]["enabled"] is True

        cat_resp = client.get("/api/ecosystem/stage-catalog", headers=member_headers)
        assert cat_resp.status_code == 200
        assert "acme.evaluator.carbon@1.2.0" in {e["ref"] for e in cat_resp.json()["evaluators"]}

        # 5. Orthogonality check: non-admin local upload still enters pending approval
        local_dev_resp = client.post(
            "/api/ecosystem/device-models/import",
            json={
                "yaml_text": _sample_device_yaml("acme.device.local_only"),
                "revision": 1,
                "source_origin": "local_upload",
            },
            headers=login_headers(client, other_member),
        )
        assert local_dev_resp.status_code == 201, local_dev_resp.text
        assert local_dev_resp.json()["approval_status"] == "pending"


def test_stateless_submission_proxy_forwards_otp_and_pr_with_zero_email_persistence_or_logging(
    db: Session,
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    admin = make_user(db, "e5a_sub_admin", role="admin")
    author = make_user(db, "e5a_sub_author", role="engineer")
    db.commit()

    ecosystem_app.update_ecosystem_settings_case(
        db,
        admin,
        market_mode="online",
        local_upload_requires_approval=True,
        submission_gateway_url="https://gw.example.test",
    )

    secret_email = "Secret.Submitter+99@Example.COM"
    recorded_gw_requests: list[tuple[str, str, dict[str, str], bytes]] = []

    def gw_handler(request: httpx.Request) -> httpx.Response:
        recorded_gw_requests.append(
            (
                request.method,
                request.url.path,
                {k.lower(): v for k, v in request.headers.items()},
                request.content,
            )
        )
        if request.url.path == "/v1/otp/request":
            body = json.loads(request.content.decode("utf-8"))
            assert body == {"email": secret_email}
            return httpx.Response(200, json={"challenge_token": "chg-tok-1", "expires_in": 600})
        if request.url.path == "/v1/otp/verify":
            body = json.loads(request.content.decode("utf-8"))
            assert body == {
                "email": secret_email,
                "otp_code": "654321",
                "challenge_token": "chg-tok-1",
            }
            return httpx.Response(200, json={"submission_token": "sub-tok-xyz", "expires_in": 900})
        if request.url.path == "/v1/submissions":
            assert request.headers.get("authorization") == "Bearer sub-tok-xyz"
            content_text = request.content.decode("latin-1")
            assert 'name="kind"' in content_text
            assert 'name="revision"' in content_text
            assert "acme.device.submitted" in content_text
            return httpx.Response(
                201,
                json={
                    "duplicate": False,
                    "pr_number": 42,
                    "pr_url": "https://github.com/ThermalEng/pIES-Market/pull/42",
                    "branch": "submit/acme.device.submitted-r3",
                    "artifact_id": "acme.device.submitted",
                    "publisher": "acme",
                    "sha256": "a" * 64,
                },
            )
        return httpx.Response(404)

    monkeypatch.setattr(
        market_remote,
        "build_http_client",
        lambda *, timeout=30.0: httpx.Client(
            transport=httpx.MockTransport(gw_handler), timeout=timeout
        ),
    )

    device_yaml = _sample_device_yaml("acme.device.submitted").encode("utf-8")

    with caplog.at_level(logging.DEBUG):
        with _make_client(db, db_factory) as client:
            headers = login_headers(client, author)

            r1 = client.post(
                "/api/ecosystem/market/submit/otp-request",
                json={"email": secret_email},
                headers=headers,
            )
            assert r1.status_code == 200, r1.text
            assert r1.json() == {"challenge_token": "chg-tok-1", "expires_in": 600}

            r2 = client.post(
                "/api/ecosystem/market/submit/otp-verify",
                json={
                    "email": secret_email,
                    "otp_code": "654321",
                    "challenge_token": "chg-tok-1",
                },
                headers=headers,
            )
            assert r2.status_code == 200, r2.text
            assert r2.json() == {"submission_token": "sub-tok-xyz", "expires_in": 900}

            r3 = client.post(
                "/api/ecosystem/market/submit",
                data={
                    "submission_token": "sub-tok-xyz",
                    "kind": "device_model",
                    "artifact_id": "acme.device.submitted",
                    "version_or_revision": "3",
                    "publisher": "acme",
                    "license": "MPL-2.0",
                    "description": "Clean community device",
                    "readme_markdown": "# Community Device",
                },
                files={"file": ("acme.device.submitted.device.yaml", device_yaml, "text/yaml")},
                headers=headers,
            )
            assert r3.status_code == 200, r3.text
            assert r3.json() == {
                "duplicate": False,
                "pr_number": 42,
                "pr_url": "https://github.com/ThermalEng/pIES-Market/pull/42",
                "branch": "submit/acme.device.submitted-r3",
                "artifact_id": "acme.device.submitted",
                "kind": "device_model",
            }

    assert len(recorded_gw_requests) == 3

    # Verify zero plaintext email persistence across all database tables
    email_lower = secret_email.lower()
    for table in Base.metadata.sorted_tables:
        rows = db.execute(sa.select(table)).fetchall()
        for row in rows:
            serialized = repr(tuple(row)).lower()
            assert email_lower not in serialized, f"Plaintext email leaked in table {table.name}"

    # Verify zero plaintext email in Python logs
    for rec in caplog.records:
        assert email_lower not in rec.getMessage().lower()


def test_excel_export_renders_independent_evaluator_sheets_and_preserves_financial_sheets(
    db: Session,
    db_factory: sessionmaker[Session],
) -> None:
    user_ns = make_user(db, "e5a_xls_owner")
    db.commit()
    user = identity_domain.get_user(db, user_ns.id)
    assert user is not None

    with _make_client(db, db_factory) as client:
        headers = login_headers(client, user_ns)
        proj_resp = client.post(
            "/api/projects",
            json={
                "name": "E5a 导出验证项目",
                "baseline_resolution": "1h",
                "baseline_start_year": 2026,
                "baseline_scenario_mode": "single",
            },
            headers=headers,
        )
        assert proj_resp.status_code == 201, proj_resp.text
        project_id = proj_resp.json()["project"]["id"]

        ver_resp = client.post(
            f"/api/projects/{project_id}/versions",
            json={"name": "v1", "reason": "manual_save"},
            headers=headers,
        )
        assert ver_resp.status_code == 201, ver_resp.text
        version_id = ver_resp.json()["version"]["id"]

    sealed_fin = {
        "schema": "ies.financial-evaluation",
        "schema_version": "1.0.0",
        "status": "evaluated",
        "evaluation_years": 2,
        "construction_period": 1,
        "operation_years": 1,
        "currency": "CNY",
        "price_basis": "tax_inclusive",
        "base_year": 2025,
        "discount_rate": "0.08",
        "construction_total": "100000",
        "construction_total_unit": "CNY",
        "annual_fixed_om": "1000",
        "payments_applied": [{"t": 0, "ratio": "1.0", "amount": "100000.0"}],
        "annual_operation": [
            {
                "financial_year": 1,
                "energy_net": "20000",
                "variable_om": "200",
                "fixed_om": "1000",
                "maintenance": "0",
                "demand_charge": "0",
                "net": "18800",
            }
        ],
        "monthly_demand": [],
        "net_cashflows": [],
        "construction_units": [],
        "cost_items": [],
        "amounts": [],
        "cash_movements": [],
        "assets": [],
        "annual_net_flows": ["-100000.0", "18800"],
        "comparison": {"mode": "absolute"},
        "tax_cash": {
            "vat": [],
            "vat_rules": [],
            "refunds": [],
            "income": [],
            "depreciation": [],
            "loss_balances": [],
            "derived": [],
        },
        "evaluation": {
            "currency": "CNY",
            "evaluation_years": 2,
            "rows": [
                {
                    "year": 0,
                    "net": "-100000.0",
                    "discounted": "-100000.0",
                    "cumulative": "-100000.0",
                    "cumulative_discounted": "-100000.0",
                },
                {
                    "year": 1,
                    "net": "18800",
                    "discounted": "17407.41",
                    "cumulative": "-81200.0",
                    "cumulative_discounted": "-82592.59",
                },
            ],
            "npv": "-82592.59",
            "npv_unit": "CNY",
            "irr": None,
            "irr_status": "no_solution",
            "static_payback": None,
            "static_status": "not_recovered",
            "dynamic_payback": None,
            "dynamic_status": "not_recovered",
        },
    }

    long_prefix = "Carbon[2026]/Eval*?:LongTitlePrefix1234567890"
    evidence_payload = {
        "kpis": [{"name": "总负荷", "value": 5000, "unit": "kWh"}],
        "financial_evaluation": sealed_fin,
        "evaluator_results": [
            {
                "evaluator": "ies.evaluator.finance@1.0.0",
                "status": "succeeded",
                "contract": "ies.financial-evaluation@1.0.0",
                "error": None,
            },
            {
                "evaluator": "acme.evaluator.carbon@1.2.0",
                "status": "succeeded",
                "contract": "ies.evaluation-report@1.0.0",
                "error": None,
            },
            {
                "evaluator": "acme.evaluator.reliability_a@1.0.0",
                "status": "succeeded",
                "contract": "ies.evaluation-report@1.0.0",
                "title": long_prefix,
                "metrics": [
                    {"id": "lolp", "label": "=CMD('calc')", "value": 0.002, "unit": "ratio"}
                ],
                "tables": [],
            },
            {
                "evaluator": "acme.evaluator.reliability_b@1.0.0",
                "status": "failed",
                "contract": None,
                "title": long_prefix,
                "error": {"code": "EVAL-FAILED", "message": "Timeout in container"},
            },
        ],
        "evaluation_reports": [
            {
                "schema": "ies.evaluation-report",
                "schema_version": "1.0.0",
                "evaluator": "acme.evaluator.carbon@1.2.0",
                "title": "碳排放核查报告",
                "summary": "基于区域电网因子计算全生命周期排放",
                "metrics": [
                    {"id": "co2_total", "label": "年碳排放总量", "value": 421.5, "unit": "tCO2"},
                    {"id": "intensity", "label": "碳排放强度", "value": 0.48, "unit": "kgCO2/kWh"},
                ],
                "charts": [],
                "tables": [
                    {
                        "id": "monthly_co2",
                        "title": "分月排放清单",
                        "columns": [
                            {"id": "month", "label": "月份"},
                            {"id": "co2", "label": "排放量", "unit": "tCO2"},
                        ],
                        "rows": [
                            ["2026-01", 38.2],
                            ["2026-02", 34.7],
                        ],
                    }
                ],
            }
        ],
    }

    obj = put_object(
        db,
        json.dumps(evidence_payload, ensure_ascii=False).encode("utf-8"),
        "application/json",
        source_category="evidence",
    )
    snap = CalcSnapshot(
        project_version_id=version_id,
        dataset_version_ids=[],
        calc_config_snapshot={"params": {}, "algorithm": "ies.generator.highs_milp@1.0.0"},
        program_version="1.2.0",
        extension_versions={},
        random_seed=7,
        schema_version="1.0.0",
        result_adapter="ies.result-adapter.highs@1.0.0",
        executor="ies.executor.local_process@1.0.0",
        created_by=user.id,
    )
    db.add(snap)
    db.flush()
    task = Task(
        project_id=project_id,
        type="calc",
        status="completed",
        business_outcome="normal_completion",
        calc_snapshot_id=snap.id,
        requested_by=user.id,
    )
    db.add(task)
    db.flush()
    ep = EvidencePackage(
        task_id=task.id,
        calc_snapshot_id=snap.id,
        object_id=obj.id,
        status="complete",
        created_by=user.id,
    )
    db.add(ep)
    db.flush()
    assess = ResultAssessment(
        evidence_package_id=ep.id,
        assessor="system",
        assessed_by=user.id,
        dimension_physical="pass",
        dimension_optimality="pass",
        dimension_financial="pass",
        dimension_reliability="pass",
        overall_score=91.0,
        comment="ok",
        detail={},
    )
    db.add(assess)
    db.flush()
    db.add(
        ResultIndex(
            project_id=project_id,
            project_version_id=version_id,
            evidence_package_id=ep.id,
            assessment_id=assess.id,
            is_latest=True,
        )
    )
    db.commit()

    excel_bytes = export_excel(db, user, project_id, ep.id, assess.id, lang="zh")
    wb = load_workbook(io.BytesIO(excel_bytes))

    # Official financial sheets stay intact
    assert "财务摘要" in wb.sheetnames
    fin_text = "\n".join(str(c.value) for row in wb["财务摘要"].iter_rows() for c in row)
    assert "-82592.59" in fin_text
    assert "建设总额 C" in fin_text and "100000" in fin_text

    # Non-financial evaluator sheets exist with unique, <=31 char, sanitized Excel titles
    for name in wb.sheetnames:
        assert len(name) <= 31
        assert not any(ch in name for ch in "\\/:*?[]")
    assert len({n.lower() for n in wb.sheetnames}) == len(wb.sheetnames)

    assert "碳排放核查报告" in wb.sheetnames
    carbon_text = "\n".join(str(c.value) for row in wb["碳排放核查报告"].iter_rows() for c in row)
    assert "acme.evaluator.carbon@1.2.0" in carbon_text
    assert "年碳排放总量" in carbon_text
    assert "421.5" in carbon_text
    assert "tCO2" in carbon_text
    assert "分月排放清单" in carbon_text
    assert "2026-01" in carbon_text and "38.2" in carbon_text

    # Colliding/long/unsafe titles were sanitized & deduplicated, formula injection escaped
    rel_sheets = [s for s in wb.sheetnames if s.startswith("Carbon_2026__Eval")]
    assert len(rel_sheets) == 2
    rel_a_text = "\n".join(str(c.value) for row in wb[rel_sheets[0]].iter_rows() for c in row)
    assert "'=CMD('calc')" in rel_a_text
    rel_b_text = "\n".join(str(c.value) for row in wb[rel_sheets[1]].iter_rows() for c in row)
    assert "Timeout in container" in rel_b_text

    # Check storage object download path matches
    stored = get_object(db, obj.id)
    assert stored
