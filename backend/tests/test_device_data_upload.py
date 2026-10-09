"""S2 公开上传 ies.device-data step 格式：识别 → 校验 → 规范落盘 → 全链可用。

- 纯识别：schema 标记命中 / 无标记 / 异构标记。
- API 负矩阵：每种阻断 400 DATA-VAL-001 + 对应诊断码。
- API 正向：201 + 版本字段/单位/质量报告 + 数据文件对象即规范 step CSV。
- 全链：HTTP 上传 → 项目绑定 → 任务提交 → 真实运行 → 证据
  （单目标；零域级 crafting）。
- 通用时间戳路径不受影响：无标记文件仍走原路径，
  既有 test_dataset_api 覆盖通用上传回归；显式异构标记按 schema
  错误可见拒绝；显式 fields 表单以警告记入诊断，不静默丢弃。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")

import pytest  # noqa: E402
from auth_helpers import make_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from test_prediction_capability import (  # noqa: E402
    _claim,
    _evidence_payload,
    _prepare_predict_project,
    _run,
    _save_calc,
)
from test_tasks_api import (  # noqa: E402
    _CONFIG_BODY,
    _create_project,
    _h,
    _submit_task,
)

from iesplan.api import projects as projects_api  # noqa: E402
from iesplan.api import tasks as tasks_api  # noqa: E402
from iesplan.api.config import config_router  # noqa: E402
from iesplan.api.datasets import router as datasets_router  # noqa: E402
from iesplan.api.deps import get_request_db  # noqa: E402
from iesplan.bootstrap import assemble_compute_worker  # noqa: E402
from iesplan.computation import NAIVE_PREDICTOR_REF  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.devices import is_device_data_csv  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.storage import get_object  # noqa: E402
from iesplan.tasks.persistence import Task  # noqa: E402


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
def _clean_state(engine: Engine) -> Iterator[None]:
    from iesplan.tasks import queue  # noqa: E402

    queue.force_memory()
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def db(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


@pytest.fixture()
def client(engine: Engine, db: Session, tmp_path: Path) -> Iterator[TestClient]:
    from iesplan.config import settings  # noqa: E402

    settings.data_dir = tmp_path

    app = create_app()
    app.include_router(projects_api.router)
    app.include_router(tasks_api.router)
    app.include_router(config_router)
    app.include_router(datasets_router)

    def _override_request_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_request_db] = _override_request_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def plane():
    return assemble_compute_worker().compute_execution_plane()


BASE_META = [
    "# schema: ies.device-data",
    "# schema_version: 2.1.0",
    "# dataset_id: predict_history",
    "# device_id: ies.device.electric_load",
    "# source_mode: data_predict",
    "# resolution: 1h",
    "# unit.electricity_demand: kW",
]
HEADER = "step,electricity_demand"


def _dd_bytes(meta: list[str], rows: list[str], header: str = HEADER) -> bytes:
    return ("\n".join([*meta, header, *rows]) + "\n").encode("utf-8")


def _rows(values: list[float], count: int = 6) -> list[str]:
    cycled = [values[i % len(values)] for i in range(count)]
    return [f"{i},{v}" for i, v in enumerate(cycled)]


def _upload(
    client: TestClient, pid: int, user, ds_id: int, raw: bytes,
    resolution: str = "1h", fields: dict | None = None,
) -> tuple[int, dict]:
    import json as _json  # noqa: E402

    form: dict = {"resolution": resolution, "utc_offset_minutes": "480"}
    if fields is not None:
        form["fields"] = _json.dumps(fields)
    resp = client.post(
        f"/api/projects/{pid}/datasets/{ds_id}/versions",
        data=form,
        files={"file": ("history.csv", raw, "text/csv")},
        headers=_h(client, user),
    )
    return resp.status_code, resp.json()


def _make_dataset(client: TestClient, pid: int, user, name: str = "历史数据") -> int:
    resp = client.post(
        f"/api/projects/{pid}/datasets",
        json={"name": name, "source_category": "user_upload", "license": "CC-BY-4.0"},
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["dataset"]["id"])


def _diag_codes(body: dict) -> list[str]:
    return [d["code"] for d in body["error"]["params"]["diagnostics"]]


class TestDeviceDataDetection:
    def test_schema_marker_detected(self) -> None:
        assert is_device_data_csv(_dd_bytes(BASE_META, _rows([48.0]))) is True

    def test_generic_csv_not_detected(self) -> None:
        raw = b"timestamp,e_load\n2025-01-01 00:00,100.0\n"
        assert is_device_data_csv(raw) is False

    def test_foreign_schema_not_detected(self) -> None:
        meta = ["# schema: something-else", "# resolution: 1h"]
        assert is_device_data_csv(_dd_bytes(meta, ["0,1.0"])) is False


class TestDeviceDataUploadRejected:
    @pytest.mark.parametrize(
        ("meta", "rows", "code", "resolution"),
        [
            (
                [m.replace("2.1.0", "9.9.9") for m in BASE_META],
                _rows([48.0]), "DATA-META-003", "1h",
            ),
            (
                [m.replace("data_predict", "bogus") for m in BASE_META],
                _rows([48.0]), "DATA-META-004", "1h",
            ),
            (
                [m.replace("data_predict", "constant") for m in BASE_META],
                _rows([48.0]), "DATA-META-011", "1h",
            ),
            (
                BASE_META + ["# resolution: 1h"],
                _rows([48.0]), "DATA-META-001", "1h",
            ),
            (
                BASE_META + ["# foo: bar"],
                _rows([48.0]), "DATA-META-002", "1h",
            ),
            (
                [m for m in BASE_META if not m.startswith("# unit.")],
                _rows([48.0]), "DATA-COL-007", "1h",
            ),
            (
                BASE_META, ["0,48.0", "2,49.0"], "DATA-STEP-002", "1h",
            ),
            (
                BASE_META, ["0,48.0", "x,49.0"], "DATA-STEP-001", "1h",
            ),
            (
                BASE_META, ["0,48.0", "1,"], "DATA-VAL-002", "1h",
            ),
            (
                BASE_META, ["0,48.0", "1,nan"], "RES-NUM-001", "1h",
            ),
            (
                BASE_META, [], "DATA-DIAL-001", "1h",
            ),
            (
                BASE_META, ["0,48.0", "# note", "1,49.0"], "DATA-DIAL-001", "1h",
            ),
            (
                BASE_META, _rows([48.0]), "DATA-META-004", "30min",
            ),
        ],
    )
    def test_rejected_with_diag_code(
        self, client: TestClient, db: Session, meta, rows, code, resolution,
    ) -> None:
        user = make_user(db, "dd_owner")
        pid = _create_project(client, user, "上传负矩阵项目")
        ds_id = _make_dataset(client, pid, user)
        status, body = _upload(client, pid, user, ds_id, _dd_bytes(meta, rows), resolution)
        assert status == 400, body
        assert body["error"]["code"] == "DATA-VAL-001"
        assert code in _diag_codes(body), body

    def test_first_column_must_be_step(
        self, client: TestClient, db: Session
    ) -> None:
        user = make_user(db, "dd_owner_step")
        pid = _create_project(client, user, "首列项目")
        ds_id = _make_dataset(client, pid, user)
        raw = _dd_bytes(BASE_META, ["2025-01-01,48.0"], header="time,electricity_demand")
        status, body = _upload(client, pid, user, ds_id, raw)
        assert status == 400, body
        assert "DATA-COL-005" in _diag_codes(body), body

    def test_foreign_schema_rejected_as_schema_error(
        self, client: TestClient, db: Session
    ) -> None:
        """显式异构标记按 schema 错误可见拒绝，不伪装普通时间戳 CSV。"""
        user = make_user(db, "dd_owner_foreign")
        pid = _create_project(client, user, "异构项目")
        ds_id = _make_dataset(client, pid, user)
        meta = ["# schema: something-else", "# resolution: 1h"]
        status, body = _upload(client, pid, user, ds_id, _dd_bytes(meta, ["0,1.0"]))
        assert status == 400, body
        assert body["error"]["code"] == "DATA-VAL-001"
        assert "DATA-META-003" in _diag_codes(body), body


class TestDeviceDataUploadAccepted:
    def test_step_file_stored_as_canonical_step_csv(
        self, client: TestClient, db: Session
    ) -> None:
        user = make_user(db, "dd_owner_ok")
        pid = _create_project(client, user, "上传正向项目")
        ds_id = _make_dataset(client, pid, user)
        raw = _dd_bytes(BASE_META, _rows([48.0, 49.0], 24))
        status, body = _upload(client, pid, user, ds_id, raw)
        assert status == 201, body
        version = body["dataset_version"]
        assert version["version_no"] == 1
        assert version["resolution"] == "1h"
        assert version["timeline"] == "hourly"
        assert version["fields"]["electricity_demand"]["unit"] == "kW"
        report = body["quality_report"]
        assert report["schema"] == "ies.device-data"
        assert report["row_count"] == 24
        assert report["has_blocking_errors"] is False
        assert body["diagnostics"] == []
        detail = client.get(
            f"/api/projects/{pid}/datasets/{ds_id}/versions/1",
            headers=_h(client, user),
        )
        assert detail.status_code == 200, detail.text
        data_files = [f for f in detail.json()["files"] if f["file_kind"] == "data"]
        assert len(data_files) == 1
        stored = get_object(db, data_files[0]["object_id"])
        lines = stored.decode("utf-8").splitlines()
        assert lines[:7] == BASE_META
        assert lines[7] == HEADER
        assert len(lines) == 7 + 1 + 24
        assert lines[8] == "0,48.0"
        assert lines[9] == "1,49.0"
        assert stored.endswith(b"\n") and b"\r" not in stored

    def test_explicit_fields_surfaced_as_warning(
        self, client: TestClient, db: Session
    ) -> None:
        """device-data 显式 fields 表单不静默丢弃：201 + 非阻断警告。"""
        user = make_user(db, "dd_owner_fields")
        pid = _create_project(client, user, "显式字段项目")
        ds_id = _make_dataset(client, pid, user)
        raw = _dd_bytes(BASE_META, _rows([48.0, 49.0], 24))
        status, body = _upload(
            client, pid, user, ds_id, raw,
            fields={"e_load": {"unit": "kWh"}},
        )
        assert status == 201, body
        codes = [d["code"] for d in body["diagnostics"]]
        assert "DATA-FILE-005" in codes, body


def _upload_history_version(db, client, pid, user, name, raw, store=None):
    """公开上传工厂（version_factory 钩子）：零域级 crafting。"""
    resp = client.post(
        f"/api/projects/{pid}/datasets",
        json={"name": name, "source_category": "user_upload", "license": "CC-BY-4.0"},
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text
    ds_id = int(resp.json()["dataset"]["id"])
    status, body = _upload(client, pid, user, ds_id, raw)
    assert status == 201, body
    if store is not None:
        store["dataset_id"] = ds_id
    return int(body["dataset_version"]["id"])


class TestPublicUploadChainEndToEnd:
    def test_upload_bind_submit_run_evidence(
        self, client: TestClient, db: Session, engine: Engine, plane
    ) -> None:
        """公开全链：HTTP 上传 step 历史 → 绑定 → 提交 → 真实运行 → 证据。

        历史版本零域级 crafting；上传的规范字节被装配实际消费
        （预测值即上传历史末值 49.0）。
        """
        user = make_user(db, "dd_owner_chain")
        uploaded: dict = {}
        pid, _vids = _prepare_predict_project(
            client, db, user, [("load1", "load", [48.0, 49.0])],
            version_factory=lambda *args: _upload_history_version(
                *args, store=uploaded
            ),
        )
        body = dict(_CONFIG_BODY)
        body["predictions"] = [
            {
                "target": {"instance": "load1", "interface": "electricity_demand"},
                "algorithm": NAIVE_PREDICTOR_REF,
                "parameters": {},
            }
        ]
        rev = _save_calc(client, pid, user, body)
        status, resp = _submit_task(
            client, pid, user, config={"config_revision": rev},
            evaluation={"evaluation_years": 1, "discount_rate": 0.08},
            idempotency_key="dd-e2e-chain",
        )
        assert status == 201, resp
        task_id = int(resp["task"]["id"])
        claim = _claim(db, task_id)
        assert _run(engine, claim, plane) == "completed"

        task = db.execute(select(Task).where(Task.id == task_id)).scalar_one()
        assert task.status == "completed"
        assert task.business_outcome == "normal_completion"
        sealed, _evidence = _evidence_payload(db, task_id)
        prediction = sealed["prediction"]
        assert prediction["predict_target_count"] == 1
        assert prediction["prediction_rounds"] == 1
        receipt = prediction["receipts"][0]
        series_id = receipt["output_series"]["object_id"]
        series_lines = get_object(db, series_id).decode("utf-8").splitlines()
        assert {line.split(",")[1] for line in series_lines[1:]} == {"49.0"}
        detail = client.get(
            f"/api/projects/{pid}/datasets/{uploaded['dataset_id']}/versions/1",
            headers=_h(client, user),
        )
        assert detail.status_code == 200, detail.text
