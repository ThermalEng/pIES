"""0.12-F3a 定向测试: 价格序列 CSV 契约 + 上传入口 + 提交实长核对。

- 纯协议: ``ies.price-series@1.0.0`` 解析/规范落盘(正/零/负 Decimal、
  step 连续、分辨率/单位/表头/数值逐项拒绝码)。
- 上传入口: 同数据集上传口按文件自描述识别, 质量报告给出
  ``price_object_id``(存储不透明 oid); 分辨率与表单不一致阻断。
- 提交核对: ``_check_submit_coverage`` 读冻结对象实字节做唯一一次
  覆盖/声明元数据核对(实长/分辨率/单位); 缺失或不一致 → 400
  TASK-REQ-017; constant 价格无对象, 不参与核对。
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from types import SimpleNamespace

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")

from collections.abc import Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from auth_helpers import make_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from test_device_data_upload import _make_dataset, _upload  # noqa: E402
from test_finance_triplet import _profile_payload  # noqa: E402
from test_tasks_api import _create_project  # noqa: E402

from iesplan.application.tasks.submissions import (  # noqa: E402
    _check_submit_coverage,
)
from iesplan.db import Base  # noqa: E402
from iesplan.finance.pricecsv import (  # noqa: E402
    emit_price_series_csv,
    is_price_series_csv,
    parse_price_series_file,
)
from iesplan.finance.triplet import (  # noqa: E402
    FinanceProfile,
    merge_effective,
)
from iesplan.storage import put_object  # noqa: E402
from iesplan.tasks.contracts import InvalidRequestError  # noqa: E402


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


@pytest.fixture()
def db(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


@pytest.fixture()
def client(engine: Engine, db: Session, tmp_path: Path) -> Iterator[TestClient]:
    from iesplan.api import projects as projects_api  # noqa: E402
    from iesplan.api import tasks as tasks_api  # noqa: E402
    from iesplan.api.config import config_router  # noqa: E402
    from iesplan.api.datasets import router as datasets_router  # noqa: E402
    from iesplan.api.deps import get_request_db  # noqa: E402
    from iesplan.config import settings  # noqa: E402
    from iesplan.main import create_app  # noqa: E402

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


@pytest.fixture(autouse=True)
def _clean_state(engine: Engine) -> Iterator[None]:
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


PRICE_META = [
    "# schema: ies.price-series",
    "# schema_version: 1.0.0",
    "# resolution: 1h",
    "# unit: CNY/kWh",
]


def _price_bytes(rows: list[str], meta: list[str] | None = None) -> bytes:
    lines = list(meta if meta is not None else PRICE_META)
    lines.append("step,price")
    lines.extend(rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _hourly_rows(values: list[str], count: int) -> list[str]:
    cycled = [values[i % len(values)] for i in range(count)]
    return [f"{i},{v}" for i, v in enumerate(cycled)]


# ---------------------------------------------------------------------------
# 纯协议: 解析与规范落盘
# ---------------------------------------------------------------------------


def test_valid_file_parses_with_signed_decimals() -> None:
    parsed, diags = parse_price_series_file(
        _price_bytes(_hourly_rows(["0.7", "0", "-0.25"], 6))
    )
    assert diags == []
    assert parsed is not None
    assert parsed.meta.resolution == "1h"
    assert parsed.meta.unit == "CNY/kWh"
    assert parsed.values == (
        Decimal("0.7"), Decimal("0"), Decimal("-0.25"),
        Decimal("0.7"), Decimal("0"), Decimal("-0.25"),
    )


def test_emit_round_trip_preserves_text() -> None:
    raw = _price_bytes(_hourly_rows(["0.70", "-0.250"], 4))
    parsed, _ = parse_price_series_file(raw)
    assert parsed is not None
    assert emit_price_series_csv(parsed.meta, parsed.values) == raw


def test_detection_only_by_schema_marker() -> None:
    assert is_price_series_csv(_price_bytes(["0,0.7"])) is True
    assert is_price_series_csv(b"step,value\n0,1\n") is False


def _rejected(raw: bytes) -> str:
    parsed, diags = parse_price_series_file(raw)
    assert parsed is None
    assert diags and all(d.blocking for d in diags)
    return diags[0].code


def test_rejections_carry_price_csv_codes() -> None:
    assert _rejected(b"\xff\xfe\n") == "PRICE-CSV-001"
    assert _rejected(_price_bytes(
        ["0,0.7"], meta=[m for m in PRICE_META if not m.startswith("# schema_version")],
    )) == "PRICE-CSV-003"
    assert _rejected(_price_bytes(["0,0.7"], meta=[*PRICE_META, "# note.x: 1"])) == "PRICE-CSV-003"
    bad_schema = ["# schema: ies.device-data", *PRICE_META[1:]]
    assert _rejected(_price_bytes(["0,0.7"], meta=bad_schema)) == "PRICE-CSV-002"
    dup_meta = [*PRICE_META, "# unit: CNY/kWh"]
    assert _rejected(_price_bytes(["0,0.7"], meta=dup_meta)) == "PRICE-CSV-003"
    bad_version = [m.replace("1.0.0", "9.9.9") if "schema_version" in m else m for m in PRICE_META]
    assert _rejected(_price_bytes(["0,0.7"], meta=bad_version)) == "PRICE-CSV-002"
    bad_res = [m.replace("1h", "1d") if "resolution" in m else m for m in PRICE_META]
    assert _rejected(_price_bytes(["0,0.7"], meta=bad_res)) == "PRICE-CSV-004"
    bad_unit = [m.replace("CNY/kWh", "bogus!!") if m.startswith("# unit") else m for m in PRICE_META]
    assert _rejected(_price_bytes(["0,0.7"], meta=bad_unit)) == "PRICE-CSV-005"
    bad_header = _price_bytes(["0,0.7"]).replace(b"step,price", b"step,value")
    assert _rejected(bad_header) == "PRICE-CSV-006"
    three_cols = _price_bytes(["0,0.7"]).replace(b"step,price", b"step,price,extra")
    assert _rejected(three_cols) == "PRICE-CSV-006"
    assert _rejected(_price_bytes(["1,0.7"])) == "PRICE-CSV-007"
    assert _rejected(_price_bytes(["0,0.7", "2,0.8"])) == "PRICE-CSV-007"
    assert _rejected(_price_bytes(["0,abc"])) == "PRICE-CSV-008"
    assert _rejected(_price_bytes(["0,NaN"])) == "PRICE-CSV-008"
    assert _rejected(_price_bytes(["0,Infinity"])) == "PRICE-CSV-008"
    assert _rejected(_price_bytes([])) == "PRICE-CSV-009"


# ---------------------------------------------------------------------------
# 上传入口: 同口识别 + oid 质量报告
# ---------------------------------------------------------------------------


def test_price_upload_returns_opaque_object_id(client, db) -> None:  # noqa: ANN001, ANN201
    user = make_user(db, "price_owner")
    pid = _create_project(client, user, "价格上传项目")
    ds_id = _make_dataset(client, pid, user, name="价格序列")
    status, resp = _upload(
        client, pid, user, ds_id,
        _price_bytes(_hourly_rows(["0.7", "0.35"], 100)),
    )
    assert status == 201, resp
    report = resp["quality_report"]
    assert report["schema"] == "ies.price-series"
    assert report["schema_version"] == "1.0.0"
    assert report["row_count"] == 100
    oid = report["price_object_id"]
    assert isinstance(oid, str) and len(oid) == 64
    from iesplan.storage import get_object as _get_object  # noqa: E402

    stored = _get_object(db, oid)
    parsed, diags = parse_price_series_file(stored)
    assert diags == [] and parsed is not None
    assert len(parsed.values) == 100


def test_price_upload_blocks_resolution_mismatch(client, db) -> None:  # noqa: ANN001, ANN201
    user = make_user(db, "price_owner_mismatch")
    pid = _create_project(client, user, "价格上传分辨率项目")
    ds_id = _make_dataset(client, pid, user, name="价格序列")
    status, resp = _upload(
        client, pid, user, ds_id,
        _price_bytes(_hourly_rows(["0.7"], 10)),
        resolution="30min",
    )
    assert status == 400, resp
    codes = [d["code"] for d in resp["error"]["params"]["diagnostics"]]
    assert "PRICE-CSV-004" in codes


# ---------------------------------------------------------------------------
# 提交核对: 冻结对象实字节唯一一次覆盖核对
# ---------------------------------------------------------------------------

def _effective_with_price(ref: str, point_count: int):
    payload = _profile_payload()
    payload["energy_prices"]["pv_export"] = {
        "carrier": "electricity",
        "direction": "sale",
        "kind": "time_series",
        "ref": ref,
        "series_meta": {
            "resolution": "1h",
            "point_count": point_count,
            "unit": "CNY/kWh",
        },
    }
    return merge_effective(FinanceProfile.from_dict(payload), None)


def _artifact() -> SimpleNamespace:
    return SimpleNamespace(canonical_text=json.dumps({
        "project_baseline": {"resolution": "1h", "start_year": 2026},
        "tariff_bindings": {
            "m1": {"price": "pv_export", "instance": "d", "interface": "p"},
            "m2": {"price": "grid_import", "instance": "d", "interface": "p"},
        },
    }))


def test_submit_coverage_checks_real_object_bytes(db, tmp_path) -> None:  # noqa: ANN001, ANN201
    from iesplan.config import settings  # noqa: E402

    settings.data_dir = tmp_path
    raw = _price_bytes(_hourly_rows(["0.4", "0.45"], 8760))
    oid = put_object(db, raw, "text/csv", "user_upload").oid
    effective = _effective_with_price(oid, 8760)
    _check_submit_coverage(
        db=db, artifact=_artifact(),
        evaluation={"evaluation_years": 1}, effective=effective,
    )


def test_submit_coverage_blocks_short_object_017(db, tmp_path) -> None:  # noqa: ANN001, ANN201
    from iesplan.config import settings  # noqa: E402

    settings.data_dir = tmp_path
    raw = _price_bytes(_hourly_rows(["0.4"], 100))
    oid = put_object(db, raw, "text/csv", "user_upload").oid
    effective = _effective_with_price(oid, 100)
    with pytest.raises(InvalidRequestError) as exc_info:
        _check_submit_coverage(
            db=db, artifact=_artifact(),
            evaluation={"evaluation_years": 1}, effective=effective,
        )
    assert exc_info.value.code == "TASK-REQ-017"
    assert exc_info.value.params["expected_point_count"] == 8760


def test_submit_coverage_blocks_missing_object_017(db, tmp_path) -> None:  # noqa: ANN001, ANN201
    from iesplan.config import settings  # noqa: E402

    settings.data_dir = tmp_path
    effective = _effective_with_price("0" * 64, 8760)
    with pytest.raises(InvalidRequestError) as exc_info:
        _check_submit_coverage(
            db=db, artifact=_artifact(),
            evaluation={"evaluation_years": 1}, effective=effective,
        )
    assert exc_info.value.code == "TASK-REQ-017"


# ---------------------------------------------------------------------------
# F3b: Worker 物化(绑定引用→冻结对象→价格区)+完成态透传
# ---------------------------------------------------------------------------

def _stored_price(db, tmp_path, values: list[str], count: int) -> str:  # noqa: ANN001, ANN201
    from iesplan.config import settings  # noqa: E402

    settings.data_dir = tmp_path
    raw = _price_bytes(_hourly_rows(values, count))
    return put_object(db, raw, "text/csv", "user_upload").oid


def test_worker_materializes_only_referenced_series(db, tmp_path) -> None:  # noqa: ANN001, ANN201
    from iesplan.application.worker.compute_cases import _materialize_price_series  # noqa: E402

    oid = _stored_price(db, tmp_path, ["0.4", "0.45"], 8760)
    effective = _effective_with_price(oid, 8760)
    prices = _materialize_price_series(db, _artifact(), effective)
    assert [p.price_id for p in prices] == ["pv_export"]
    assert prices[0].point_count == 8760
    assert prices[0].resolution == "1h"
    assert prices[0].unit == "CNY/kWh"
    assert prices[0].values[:2] == (0.4, 0.45)
    assert prices[0].source == {"kind": "price_series", "object_id": oid}


def test_worker_skips_unreferenced_series(db, tmp_path) -> None:  # noqa: ANN001, ANN201
    from test_finance_triplet import _profile_payload  # noqa: E402

    from iesplan.application.worker.compute_cases import _materialize_price_series  # noqa: E402
    from iesplan.finance.triplet import FinanceProfile, merge_effective  # noqa: E402

    oid = _stored_price(db, tmp_path, ["0.4"], 8760)
    payload = _profile_payload()
    payload["energy_prices"]["pv_export"] = {
        "carrier": "electricity",
        "direction": "sale",
        "kind": "time_series",
        "ref": oid,
        "series_meta": {"resolution": "1h", "point_count": 8760, "unit": "CNY/kWh"},
    }
    payload["energy_prices"]["unused_tou"] = {
        "carrier": "electricity",
        "direction": "purchase",
        "kind": "time_series",
        "ref": oid,
        "series_meta": {"resolution": "1h", "point_count": 8760, "unit": "CNY/kWh"},
    }
    effective = merge_effective(FinanceProfile.from_dict(payload), None)
    prices = _materialize_price_series(db, _artifact(), effective)
    assert [p.price_id for p in prices] == ["pv_export"]


def test_worker_skips_without_time_series(db, tmp_path) -> None:  # noqa: ANN001, ANN201
    from test_finance_triplet import _profile_payload  # noqa: E402

    from iesplan.application.worker.compute_cases import _materialize_price_series  # noqa: E402
    from iesplan.config import settings  # noqa: E402
    from iesplan.finance.triplet import FinanceProfile, merge_effective  # noqa: E402

    settings.data_dir = tmp_path
    payload = _profile_payload()
    payload["energy_prices"]["pv_export"] = {
        "carrier": "electricity",
        "direction": "sale",
        "kind": "constant",
        "value": {"value": "0.4", "unit": "CNY/kWh"},
    }
    effective = merge_effective(FinanceProfile.from_dict(payload), None)
    assert _materialize_price_series(db, _artifact(), effective) == ()


def test_worker_missing_object_fails_naturally(db, tmp_path) -> None:  # noqa: ANN001, ANN201
    from iesplan.application.worker.compute_cases import _materialize_price_series  # noqa: E402
    from iesplan.config import settings  # noqa: E402
    from iesplan.core.errors import NotFoundError  # noqa: E402
    from iesplan.storage.contracts import ObjectCorruptError  # noqa: E402

    settings.data_dir = tmp_path
    effective = _effective_with_price("0" * 64, 8760)
    with pytest.raises((NotFoundError, ObjectCorruptError)):
        _materialize_price_series(db, _artifact(), effective)


def test_worker_corrupt_bytes_fail_visibly(db, tmp_path) -> None:  # noqa: ANN001, ANN201
    from iesplan.application.worker.compute_cases import _materialize_price_series  # noqa: E402
    from iesplan.config import settings  # noqa: E402

    settings.data_dir = tmp_path
    oid = put_object(db, b"step,price\n0,abc\n", "text/csv", "user_upload").oid
    effective = _effective_with_price(oid, 8760)
    with pytest.raises(ValueError, match="不可解析"):
        _materialize_price_series(db, _artifact(), effective)


def test_complete_materialized_carries_prices() -> None:
    from iesplan.application.worker.compute_cases import complete_materialized  # noqa: E402
    from iesplan.computation import (  # noqa: E402
        MaterializedPriceSeries,
        MaterializedSeries,
        MaterializedSet,
    )

    base = MaterializedSet(
        evaluation_years=2,
        series=(),
        pending_predict=(
            {"device": "d", "interface": "p", "target_type": "t", "object_id": "o"},
        ),
        prices=(
            MaterializedPriceSeries(
                price_id="pv_export",
                point_count=2,
                resolution="1h",
                unit="CNY/kWh",
                values=(0.4, 0.45),
                source={"kind": "price_series", "object_id": "o" * 64},
            ),
        ),
    )
    completed = complete_materialized(
        base,
        (
            MaterializedSeries(
                device="d", interface="p", mode="data_predict",
                resolution="1h", point_count=2, values=(1.0, 2.0),
                source={"kind": "data_predict", "object_id": "o"},
            ),
        ),
    )
    assert completed.pending_predict == ()
    assert [p.price_id for p in completed.prices] == ["pv_export"]
    assert completed.prices[0].values == (0.4, 0.45)
    assert len(completed.series) == 1
