"""Wave 2 W2-C: application/packages 用例测试(包导出/导入/Excel)。

覆盖要求: 用例可独立执行并提交事务(新会话可见, 无需调用方 commit)。
（旧 services.package 已删除，等价对照测试已随之删除。）

环境: SQLite :memory:(StaticPool) + 内存队列 + 临时对象存储目录。
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from collections.abc import Iterator
from pathlib import Path

# 单文件运行时的安全网: 固定 SQLite + 内存队列, 避免误连部署 Postgres/Redis
os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")

import pytest  # noqa: E402
from auth_helpers import login_headers, make_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import load_workbook  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan import identity as identity_domain  # noqa: E402
from iesplan import package as package_domain  # noqa: E402
from iesplan import project as project_domain  # noqa: E402
from iesplan.api import projects as projects_api  # noqa: E402
from iesplan.api.deps import get_request_db  # noqa: E402
from iesplan.application import packages as packages_uc  # noqa: E402
from iesplan.application.packages import transfers as transfers_uc  # noqa: E402
from iesplan.config import settings  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.tasks.persistence import CalcSnapshot, Task  # noqa: E402
from iesplan.results.persistence import EvidencePackage, ResultAssessment, ResultIndex  # noqa: E402
from iesplan.storage import get_object, put_object, storage_stats  # noqa: E402

# 建表前必须显式导入: 包域/结果域表外键指向身份域 users 表, 而身份域
# persistence 为延迟装载, 未显式导入时 create_all 报 NoReferencedTableError
# (与 test_config_api.py / test_integration.py 同一既有做法, 不改生产注册)。
from iesplan.identity.persistence import User as _UserForDDL  # noqa: E402,F401

# ---------------------------------------------------------------------------
# 测试环境
# ---------------------------------------------------------------------------


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
def _clean_state(engine: Engine, db: Session) -> Iterator[None]:
    # 内存队列由 IESPLAN_QUEUE=memory 固定(模块顶部)，包流程不触队列，无需重置。
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
    settings.data_dir = tmp_path
    app = create_app()
    app.include_router(projects_api.router)

    def _override_request_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_request_db] = _override_request_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _h(client: TestClient, user) -> dict[str, str]:
    return login_headers(client, user)


def _user(db: Session, name: str):
    ns = make_user(db, name)
    db.commit()
    rec = identity_domain.get_user(db, ns.id)
    assert rec is not None
    return rec


def _project(client: TestClient, user, name: str) -> int:
    resp = client.post(
        "/api/projects",
        json={
            "name": name,
            "baseline_resolution": "1h",
            "baseline_start_year": 2026,
            "baseline_scenario_mode": "single",
        },
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["project"]["id"]


def _version(client: TestClient, user, project_id: int) -> int:
    resp = client.post(
        f"/api/projects/{project_id}/versions",
        json={"name": "基准版本 v1", "reason": "manual_save"},
        headers=_h(client, user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["version"]["id"]


def _fresh_db(engine: Engine) -> Session:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def _seed_evidence(
    db: Session, project_id: int, version_id: int, owner_id: int,
    content_text: bytes | None = None,
) -> tuple[int, int]:
    """直接建任务/快照/证据包/评估/结果索引, 返回 (evidence_package_id, assessment_id)。"""
    content = content_text or json.dumps(
        {
            "kpis": [{"name": "年购电量", "value": 123456.7, "unit": "kWh"}],
            "financial": {"总投资": 1000, "IRR": 0.12},
            "environmental": {"年碳排放": 123.4},
            "engineering": {"装机容量": 500},
            "applicability": {"适用范围": "华东", "限制": "无"},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    obj = put_object(db, content, "application/json", source_category="evidence")
    snapshot = CalcSnapshot(
        project_version_id=version_id, dataset_version_ids=[],
        calc_config_snapshot={"params": {"horizon_years": 1}, "algorithm": "milp"},
        program_version="0.1.0", extension_versions={}, random_seed=42,
        schema_version="1.0.0",
        result_adapter="ies.result-adapter.highs@3.0.0",
        executor="ies.executor.local_process@1.0.0", created_by=owner_id,
    )
    db.add(snapshot)
    db.flush()
    task = Task(project_id=project_id, type="calc", status="completed",
                business_outcome="normal_completion", calc_snapshot_id=snapshot.id,
                requested_by=owner_id)
    db.add(task)
    db.flush()
    package = EvidencePackage(
        task_id=task.id, calc_snapshot_id=snapshot.id, object_id=obj.id,
        status="complete", created_by=owner_id,
    )
    db.add(package)
    db.flush()
    assessment = ResultAssessment(
        evidence_package_id=package.id, assessor="system", assessed_by=owner_id,
        dimension_physical="pass", dimension_optimality="pass",
        dimension_financial="pass", dimension_reliability="pass",
        overall_score=88.5, comment="ok", detail={},
    )
    db.add(assessment)
    db.flush()
    db.add(ResultIndex(
        project_id=project_id, project_version_id=version_id,
        evidence_package_id=package.id, assessment_id=assessment.id, is_latest=True,
    ))
    db.commit()
    return package.id, assessment.id


# ---------------------------------------------------------------------------
# 包导出: 独立提交
# ---------------------------------------------------------------------------


def test_export_package_usecase_commits(
    client: TestClient, db: Session, engine: Engine
) -> None:
    owner = _user(db, "w2c_pkg_owner")
    pid = _project(client, owner, "w2c-导出项目")
    _version(client, owner, pid)
    user = identity_domain.get_user(db, owner.id)
    assert user is not None

    result = packages_uc.export_package(db, user, pid)
    assert result.manifest["format_version"] == "3.0.0"
    assert result.token

    # 事务提交证明: 新会话可读出包对象
    with _fresh_db(engine) as fresh:
        raw = get_object(fresh, result.object_id)
        assert raw[:2] == b"PK"
        manifest = json.loads(zipfile.ZipFile(io.BytesIO(raw)).read("manifest.json"))
        assert manifest["package_type"] == "project"

    assert result.file_name.endswith(".zip")


# ---------------------------------------------------------------------------
# 包导入: 提案幂等 + 确认建新身份
# ---------------------------------------------------------------------------


def test_import_roundtrip_via_usecases(client: TestClient, db: Session, engine: Engine) -> None:
    owner = _user(db, "w2c_pkg_owner2")
    pid = _project(client, owner, "w2c-导出项目2")
    _version(client, owner, pid)
    user = identity_domain.get_user(db, owner.id)
    assert user is not None
    exported = packages_uc.export_package(db, user, pid)
    zip_bytes = get_object(db, exported.object_id)

    importer = _user(db, "w2c_importer")
    imp = identity_domain.get_user(db, importer.id)
    assert imp is not None

    proposal = packages_uc.propose_import(db, imp, zip_bytes, idempotency_key="w2c-imp-1")
    assert proposal.status == "proposed"
    # 幂等: 同键返回同一提案
    again = packages_uc.propose_import(db, imp, zip_bytes, idempotency_key="w2c-imp-1")
    assert again.id == proposal.id

    new_project = packages_uc.confirm_import(db, imp, proposal.id)
    assert new_project.owner_id == imp.id
    assert new_project.id != pid
    # 事务提交证明: 新会话可见新项目
    with _fresh_db(engine) as fresh:
        got = project_domain.get_project(fresh, new_project.id)
        assert got is not None and got.owner_id == imp.id
    # 确认幂等重放: 已 applied 返回同一项目
    same = packages_uc.confirm_import(db, imp, proposal.id)
    assert same.id == new_project.id


def test_import_proposal_writes_source_object_once(
    client: TestClient, db: Session
) -> None:
    """导入提案源包只落盘一次: 暂存对象 + 源包对象各写一行, 无重复落盘。"""
    owner = _user(db, "w2c_pkg_owner3")
    pid = _project(client, owner, "w2c-导出项目3")
    _version(client, owner, pid)
    user = identity_domain.get_user(db, owner.id)
    assert user is not None
    exported = packages_uc.export_package(db, user, pid)
    zip_bytes = get_object(db, exported.object_id)

    importer = _user(db, "w2c_importer_once")
    imp = identity_domain.get_user(db, importer.id)
    assert imp is not None
    before = storage_stats(db)["objects"]["count"]
    proposal = packages_uc.propose_import(db, imp, zip_bytes, idempotency_key="w2c-imp-once")
    after = storage_stats(db)["objects"]["count"]
    staged_n = len((proposal.review_summary.get("staging") or {}).get("objects", []))
    assert after - before == staged_n + 1


def test_import_rejects_bad_package(client: TestClient, db: Session) -> None:
    owner = _user(db, "w2c_pkg_owner3")
    user = identity_domain.get_user(db, owner.id)
    assert user is not None

    with pytest.raises(package_domain.ImportValidationError) as e1:
        packages_uc.propose_import(db, user, b"not a zip at all")
    assert e1.value.code == "PKG-IMP-001"


# ---------------------------------------------------------------------------
# Excel 导出用例
# ---------------------------------------------------------------------------


def test_export_excel_usecase(client: TestClient, db: Session) -> None:
    owner = _user(db, "w2c_xls_owner")
    pid = _project(client, owner, "w2c-Excel项目")
    vid = _version(client, owner, pid)
    ep_id, a_id = _seed_evidence(db, pid, vid, owner.id)
    user = identity_domain.get_user(db, owner.id)
    assert user is not None

    new_bytes = transfers_uc.export_excel(db, user, pid, ep_id, a_id, lang="zh")
    assert new_bytes[:2] == b"PK"

    new_title = load_workbook(io.BytesIO(new_bytes))["报告总览"].cell(row=1, column=1).value
    assert "pIES 项目结果报告" in new_title


def _sealed_k1n2() -> dict:
    """最小 K1N2 2.0.0 封存评价块(展示用, 双维护已合计)。"""
    return {
        "schema": "ies.financial-evaluation",
        "schema_version": "2.0.0",
        "status": "evaluated",
        "evaluation_years": 3,
        "construction_period": 1,
        "operation_years": 2,
        "currency": "CNY",
        "price_basis": "tax_inclusive",
        "base_year": 2025,
        "discount_rate": "0.08",
        "construction_total": "140000",
        "construction_total_unit": "CNY",
        "annual_fixed_om": "1400",
        "payments_applied": [
            {"t": 0, "ratio": "0.6", "amount": "84000.0"},
            {"t": 1, "ratio": "0.4", "amount": "56000.0"},
        ],
        "annual_operation": [
            {
                "financial_year": 2, "energy_net": "10000", "variable_om": "100",
                "fixed_om": "1400", "maintenance": "0", "demand_charge": "0", "net": "8500",
            },
            {
                "financial_year": 3, "energy_net": "12000", "variable_om": "120",
                "fixed_om": "1400", "maintenance": "3500.50", "demand_charge": "0", "net": "6979.50",
            },
        ],
        "monthly_demand": [],
        "annual_net_flows": ["-84000.0", "-56000.0", "8500", "6979.50"],
        # 无税展示夹具: 五个数组显式为空(合法"不适用"), 不用缺字段伪装。
        "tax_cash": {
            "vat": [],
            "refunds": [],
            "income": [],
            "depreciation": [],
            "loss_balances": [],
        },
        "evaluation": {
            "currency": "CNY", "evaluation_years": 3,
            "rows": [
                {"year": 0, "net": "-84000.0", "discounted": "-84000.0",
                 "cumulative": "-84000.0", "cumulative_discounted": "-84000.0"},
                {"year": 1, "net": "-56000.0", "discounted": "-51851.85",
                 "cumulative": "-140000.0", "cumulative_discounted": "-135851.85"},
                {"year": 2, "net": "8500", "discounted": "7288.07",
                 "cumulative": "-131500.0", "cumulative_discounted": "-128563.78"},
                {"year": 3, "net": "6979.50", "discounted": "5543.47",
                 "cumulative": "-124520.50", "cumulative_discounted": "-123020.31"},
            ],
            "npv": "-123020.31", "npv_unit": "CNY",
            "irr": None, "irr_status": "no_solution",
            "static_payback": None, "static_status": "not_recovered",
            "dynamic_payback": None, "dynamic_status": "not_recovered",
        },
    }


def test_export_excel_renders_sealed_financial_evaluation(
    client: TestClient, db: Session
) -> None:
    """Excel 财务摘要消费封存 financial_evaluation: K/N/E、付款时点、
    运营年分量、CF[0..E]、NPV/IRR 状态同表展示, 不重算。"""
    owner = _user(db, "w2c_xls_fin")
    pid = _project(client, owner, "w2c-Excel财务项目")
    vid = _version(client, owner, pid)
    sealed = json.dumps(
        {"kpis": [], "financial_evaluation": _sealed_k1n2()}, ensure_ascii=False
    ).encode("utf-8")
    ep_id, a_id = _seed_evidence(db, pid, vid, owner.id, content_text=sealed)
    user = identity_domain.get_user(db, owner.id)
    assert user is not None

    new_bytes = transfers_uc.export_excel(db, user, pid, ep_id, a_id, lang="zh")
    sheet = load_workbook(io.BytesIO(new_bytes))["财务摘要"]
    text = "\n".join(str(cell.value) for row in sheet.iter_rows() for cell in row)
    assert "评价年限 E" in text and "3" in text
    assert "建设期 K" in text and "运营年限 N" in text
    assert "建设总额 C" in text and "140000" in text
    assert "建设付款 t=0" in text and "建设付款 t=1" in text
    assert "运营年第 1 年(t=2)" in text and "运营年第 2 年(t=3)" in text
    assert "3500.50" in text
    assert "CF[t=0]" in text and "CF[t=3]" in text
    assert "净现值 NPV" in text and "-123020.31" in text
    assert "no_solution" in text and "not_recovered" in text
    wb = load_workbook(io.BytesIO(new_bytes))
    assert wb["需量月账单"].cell(row=3, column=1).value == "无计量绑定 / No demand meter bindings"


def test_export_excel_consumes_frozen_monthly_demand_with_units_and_ids(
    client: TestClient, db: Session
) -> None:
    """保存的月费/年度费均原样写表；零费率绑定仍保留真实峰。"""
    owner = _user(db, "d3_xls_demand")
    pid = _project(client, owner, "D3 月需量")
    vid = _version(client, owner, pid)
    fin = _sealed_k1n2()
    fin["annual_operation"][0]["demand_charge"] = "125.00"
    fin["annual_operation"][0]["net"] = "8375.00"
    fin["monthly_demand"] = [
        {
            "billing_id": "site_import", "charge_id": "peak_fee",
            "calendar_year": 2025, "calendar_month": 1, "financial_year": 2,
            "peak_power": "12.5", "peak_unit": "kW",
            "rate": {"value": "10", "unit": "CNY/kW"}, "charge": "125.00",
        },
        {
            "billing_id": "zero_meter", "charge_id": "zero_fee",
            "calendar_year": 2025, "calendar_month": 2, "financial_year": 2,
            "peak_power": "20.25", "peak_unit": "kW",
            "rate": {"value": "0", "unit": "CNY/kW"}, "charge": "0",
        },
    ]
    content = json.dumps({"financial_evaluation": fin}, ensure_ascii=False).encode()
    ep_id, a_id = _seed_evidence(db, pid, vid, owner.id, content_text=content)
    user = identity_domain.get_user(db, owner.id)
    wb = load_workbook(io.BytesIO(transfers_uc.export_excel(db, user, pid, ep_id, a_id)))
    sheet = wb["需量月账单"]
    expected = [
        ["site_import", "peak_fee", 2025, 1, 2, "12.5", "kW", "10", "CNY/kW", "125.00", "CNY"],
        ["zero_meter", "zero_fee", 2025, 2, 2, "20.25", "kW", "0", "CNY/kW", "0", "CNY"],
    ]
    assert [[sheet.cell(row=i, column=j).value for j in range(1, 12)] for i in (3, 4)] == expected
    summary = "\n".join(str(c.value) for row in wb["财务摘要"] for c in row)
    assert "需量 125.00" in summary
    assert "= 净额 8375.00 CNY" in summary
    assert "需量 0" in summary
    overview = "\n".join(str(c.value) for row in wb["报告总览"] for c in row)
    assert "计算任务 / Task ID" in overview



def test_download_token_helpers_match_old() -> None:
    token = packages_uc.create_download_token(7, "package", project_id=3, user_id=5)
    parsed = packages_uc.verify_download_token(token, expected_kind="package")
    assert parsed == {"object_id": 7, "kind": "package", "project_id": 3, "user_id": 5}
    old_parsed = package_domain.verify_download_token(token, expected_kind="package")
    assert old_parsed == parsed
