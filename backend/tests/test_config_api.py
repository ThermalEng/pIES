"""计算配置 API 与服务测试(U06)。

覆盖: 默认配置生成(新建设备有容量变量, 存量固定)→ 保存 → 非法变量界被拒 →
表达式约束解析错误诊断 → IRR 与折现率独立 → 算法不兼容拒绝。

- 数据库: SQLite :memory:(models 全部表 create_all);
- 应用: 独立 FastAPI 实例挂载 config_router + registry_router,
  用 dependency_overrides 替换 get_db;
- 设备类型使用 models.devices.device_type 的 CHECK 短名(pv/storage/load),
  经 services.config.resolve_device_type 的短名映射解析到注册表规格。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from iesplan.api.auth import router as auth_router
from iesplan.api.config import config_router, registry_router
from iesplan.db import Base, get_db
from iesplan.main import _register_exception_handlers
from iesplan.models.identity import User
from iesplan.models.model import Device, SystemGraph
from iesplan.models.project import Draft, Project, ProjectMember
from iesplan.services import config as config_service
from iesplan.services import identity

#: 配置域测试所有者(经窗口会话登录)
OWNER_USERNAME = "config_owner"
OWNER_PASSWORD = "Config12345"

#: 草稿/图内容哈希(sha256 十六进制)
_HASH64 = "0" * 64


@pytest.fixture()
def db() -> Iterator[Session]:
    """内存 SQLite 会话(StaticPool 保证跨连接共享同一库)。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with session_factory() as session:
        yield session
    engine.dispose()


def seed_project(db: Session, with_devices: bool = True) -> Project:
    """种子项目: 所有者 + 草稿(revision=1) + 工作图 + 设备清单。

    设备: 存量光伏(kind=existing, 容量固定)、新建光伏(kind=new, 容量变量)、
    新建电池(kind=new, 容量/功率变量)、存量电负荷(kind=existing)。
    """
    owner = db.execute(
        select(User).where(User.username == OWNER_USERNAME)
    ).scalar_one_or_none()
    if owner is None:
        owner = identity.create_user(
            db, OWNER_USERNAME, OWNER_PASSWORD, role="engineer",
            force_password_change=False, display_name="配置测试所有者",
        )
    proj = Project(
        name="配置测试项目",
        currency="CNY",
        fixed_utc_offset_minutes=480,
        owner_id=owner.id,
        created_by=owner.id,
    )
    db.add(proj)
    db.flush()
    db.add(
        ProjectMember(
            project_id=proj.id, user_id=owner.id, role="owner",
            auth_version=1, granted_by=owner.id, granted_at=datetime.now(UTC),
        )
    )
    db.flush()
    draft = Draft(
        project_id=proj.id,
        revision=1,
        content_hash=sha256(b"draft-v1").hexdigest(),
        is_current=True,
        updated_by=owner.id,
    )
    db.add(draft)
    db.flush()
    proj.current_draft_id = draft.id
    graph = SystemGraph(
        project_id=proj.id,
        draft_id=draft.id,
        name="工作图",
        graph_hash=sha256(b"graph-v1").hexdigest(),
        created_by=owner.id,
    )
    db.add(graph)
    db.flush()
    if with_devices:
        db.add_all(
            [
                Device(graph_id=graph.id, device_type="pv", kind="existing", name="存量光伏"),
                Device(graph_id=graph.id, device_type="pv", kind="new", name="新建光伏"),
                Device(graph_id=graph.id, device_type="storage", kind="new", name="储能电池"),
                Device(graph_id=graph.id, device_type="load", kind="existing", name="电负荷"),
            ]
        )
    db.commit()
    db.refresh(proj)
    return proj


def make_app(db: Session) -> FastAPI:
    """挂载配置路由的测试应用(get_db 覆盖为传入会话)。"""
    application = FastAPI(title="pIES Config API Test")
    application.include_router(auth_router)
    application.include_router(config_router)
    application.include_router(registry_router)
    _register_exception_handlers(application)
    application.dependency_overrides[get_db] = lambda: db
    return application


@pytest.fixture()
def client(db: Session) -> Iterator[TestClient]:
    """测试客户端: 预置配置所有者并登录, 客户端默认携带窗口会话凭证头。"""
    owner = db.execute(
        select(User).where(User.username == OWNER_USERNAME)
    ).scalar_one_or_none()
    if owner is None:
        identity.create_user(
            db, OWNER_USERNAME, OWNER_PASSWORD, role="engineer",
            force_password_change=False, display_name="配置测试所有者",
        )
    with TestClient(make_app(db), raise_server_exceptions=False) as test_client:
        resp = test_client.post(
            "/api/auth/login",
            json={"username": OWNER_USERNAME, "password": OWNER_PASSWORD},
        )
        assert resp.status_code == 200, resp.text
        test_client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
        yield test_client


def _default_config(db: Session, project: Project) -> dict:
    """直接调用服务生成默认配置(避免 API 层干扰)。"""
    return config_service.get_default_config(project.id, db)


# ---------------------------------------------------------------------------
# 1. 默认配置生成
# ---------------------------------------------------------------------------


def test_default_config_new_devices_have_capacity_variables(db: Session) -> None:
    """新建设备容量参数生成 continuous 变量; 存量设备固定不生成变量。"""
    project = seed_project(db)
    cfg = _default_config(db, project)

    # 默认目标: 税后项目投资 IRR 最大化(02 §6.1 f1)
    assert cfg["objectives"] == [{"metric": "irr_after_tax", "direction": "max", "weight": 1.0}]
    # 默认最低 IRR 硬约束 0.08(REQ-CALC-006)
    assert cfg["irr_floor"] == 0.08
    # 默认不允许未满足负荷(REQ-CALC-004)
    assert any(
        c.get("type") == "predefined" and c.get("payload", {}).get("kind") == "load_satisfaction"
        for c in cfg["constraints"]
    )
    # 默认算法 auto(自动选择兼容算法, REQ-CALC-005)
    assert cfg["algorithm"]["mode"] == "auto"

    variables = cfg["variables"]
    by_param = {v["param"]: v for v in variables}
    # 新建光伏: rated_capacity_kwp 为 continuous 容量变量, 界 [0, max_capacity_kwp]
    pv_var = by_param["rated_capacity_kwp"]
    assert pv_var["type"] == "continuous"
    assert pv_var["initial"] == 0.0
    assert pv_var["min"] == 0.0
    assert pv_var["max"] == 1000.0  # 注册表 max_capacity_kwp 默认 1000
    assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", pv_var["name"])
    # 新建电池: capacity_kwh / rated_power_kw 两个容量变量
    assert by_param["capacity_kwh"]["type"] == "continuous"
    assert by_param["rated_power_kw"]["type"] == "continuous"
    # 存量设备不生成变量: 存量光伏 + 存量电负荷
    var_names = [v["name"] for v in variables]
    existing_devs = config_service.load_work_graph(db, project.id)["devices"]
    existing_ids = {d["id"] for d in existing_devs if d["kind"] == "existing"}
    for v in variables:
        assert v["device_ref"] not in existing_ids
    assert len(var_names) == 3  # 新建光伏 1 + 新建电池 2


def test_default_config_device_params_use_registry_defaults(db: Session) -> None:
    """设备参数当前值 = 注册表默认值(设备行参数叠加)。"""
    project = seed_project(db)
    cfg = _default_config(db, project)
    devices = config_service.load_work_graph(db, project.id)["devices"]
    pv_dev = next(d for d in devices if d["kind"] == "new" and d["device_type"] == "pv")
    params = cfg["parameters"]["devices"][str(pv_dev["id"])]
    assert params["rated_capacity_kwp"] == 0
    assert params["max_capacity_kwp"] == 1000.0
    assert params["efficiency"] == 0.20
    assert params["unit_invest_cost"] == 3500.0
    # 经济/环境参数默认值
    assert cfg["parameters"]["economic"]["discount_rate"] == 0.08
    assert cfg["parameters"]["environmental"]["emission_factor_grid"] == 0.581


# ---------------------------------------------------------------------------
# 2. 保存与读取
# ---------------------------------------------------------------------------


def test_save_then_get_config(client: TestClient, db: Session) -> None:
    """PUT 保存(与草稿修订绑定)→ GET 读回一致, 且带参数元数据。"""
    project = seed_project(db)
    default = _default_config(db, project)

    resp = client.put(
        f"/api/projects/{project.id}/config",
        json={"config": default, "expected_revision": 1},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == 1
    assert body["status"] == "draft"
    assert body["diagnostics"] == []

    get_resp = client.get(f"/api/projects/{project.id}/config")
    assert get_resp.status_code == 200
    got = get_resp.json()
    assert got["version"] == 1
    assert got["config"]["irr_floor"] == 0.08
    assert got["config"]["objectives"][0]["metric"] == "irr_after_tax"
    # 参数元数据: 每个设备参数带单位/范围/帮助键
    device_meta = got["meta"]["parameters"]["devices"]
    assert device_meta, "必须有设备参数元数据"
    any_pv = next(
        m for m in device_meta.values() if "rated_capacity_kwp" in m
    )
    spec = any_pv["rated_capacity_kwp"]
    assert spec["unit"] == "kWp"
    assert spec["help_key"].startswith("help.param.")
    assert spec["min"] == 0.0
    # 经济参数元数据
    assert got["meta"]["parameters"]["economic"]["discount_rate"]["default"] == 0.08


def test_save_wrong_revision_conflict(client: TestClient, db: Session) -> None:
    """草稿修订不符 → 409 ConflictError, 不落库。"""
    project = seed_project(db)
    default = _default_config(db, project)
    resp = client.put(
        f"/api/projects/{project.id}/config",
        json={"config": default, "expected_revision": 99},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "SYS-STORE-004"


def test_project_not_found(client: TestClient) -> None:
    """未知项目 → 404。"""
    resp = client.get("/api/projects/999999/config")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 3. 非法变量界被拒
# ---------------------------------------------------------------------------


def test_invalid_variable_bounds_rejected(client: TestClient, db: Session) -> None:
    """初始值越界/min>max → 422 + PARAM-RNG-003 / PARAM-CONF-001。"""
    project = seed_project(db)
    default = _default_config(db, project)
    # 篡改: 变量初始值超过上界
    bad = dict(default)
    bad["variables"] = [
        dict(v, initial=5000.0) if v["type"] == "continuous" and v["max"] == 1000.0 else v
        for v in default["variables"]
    ]
    resp = client.put(
        f"/api/projects/{project.id}/config",
        json={"config": bad, "expected_revision": 1},
    )
    assert resp.status_code == 422
    codes = {d["code"] for d in resp.json()["diagnostics"]}
    assert "PARAM-RNG-003" in codes

    # min > max → PARAM-CONF-001
    bad2 = dict(default)
    bad2["variables"] = [
        dict(v, min=900.0, max=100.0) if v["type"] == "continuous" and v["max"] == 1000.0 else v
        for v in default["variables"]
    ]
    resp2 = client.put(
        f"/api/projects/{project.id}/config",
        json={"config": bad2, "expected_revision": 1},
    )
    assert resp2.status_code == 422
    codes2 = {d["code"] for d in resp2.json()["diagnostics"]}
    assert "PARAM-CONF-001" in codes2
    # 非法类型 → SYS-CFG-001
    bad3 = dict(default)
    bad3["variables"] = [dict(default["variables"][0], type="fuzzy")]
    resp3 = client.put(
        f"/api/projects/{project.id}/config",
        json={"config": bad3, "expected_revision": 1},
    )
    assert resp3.status_code == 422
    assert "SYS-CFG-001" in {d["code"] for d in resp3.json()["diagnostics"]}


# ---------------------------------------------------------------------------
# 4. 表达式约束解析错误诊断
# ---------------------------------------------------------------------------


def test_expression_constraint_parse_error_diagnostic(
    client: TestClient, db: Session
) -> None:
    """表达式语法错误 → EXPR-SYN-001; 未登记变量 → EXPR-CODE-001。"""
    project = seed_project(db)
    default = _default_config(db, project)
    var_name = default["variables"][0]["name"]

    # 语法错误
    broken = dict(default)
    broken["constraints"] = [
        {"type": "expression", "payload": {"expression": f"{var_name} >="}}
    ]
    resp = client.put(
        f"/api/projects/{project.id}/config",
        json={"config": broken, "expected_revision": 1},
    )
    assert resp.status_code == 422
    codes = {d["code"] for d in resp.json()["diagnostics"]}
    assert "EXPR-SYN-001" in codes

    # 未登记变量
    undeclared = dict(default)
    undeclared["constraints"] = [
        {"type": "expression", "payload": {"expression": "undeclared_var >= 5"}}
    ]
    resp2 = client.put(
        f"/api/projects/{project.id}/config",
        json={"config": undeclared, "expected_revision": 1},
    )
    assert resp2.status_code == 422
    codes2 = {d["code"] for d in resp2.json()["diagnostics"]}
    assert "EXPR-CODE-001" in codes2

    # 合法表达式通过, 且表达式可引用变量
    ok = dict(default)
    ok["constraints"] = [
        {"type": "expression", "payload": {"expression": f"{var_name} >= 0"}}
    ]
    resp3 = client.put(
        f"/api/projects/{project.id}/config",
        json={"config": ok, "expected_revision": 1},
    )
    assert resp3.status_code == 422, resp3.text
    codes3 = {d["code"] for d in resp3.json()["diagnostics"]}
    # 01 §5.5 量纲检查生效: 带量纲变量(kWp→power)与无量纲字面量 0 比较 → 量纲不一致
    assert "EXPR-DIM-001" in codes3

    # 带单位字面量的同量纲表达式通过(单位后缀由检查器改写为常量)
    ok2 = dict(default)
    ok2["constraints"] = [
        {"type": "expression", "payload": {"expression": f"{var_name} >= 0 W"}}
    ]
    resp4 = client.put(
        f"/api/projects/{project.id}/config",
        json={"config": ok2, "expected_revision": 1},
    )
    assert resp4.status_code == 200, resp4.text


def test_validate_endpoint_does_not_save(client: TestClient, db: Session) -> None:
    """POST /validate 只校验不保存。"""
    project = seed_project(db)
    default = _default_config(db, project)
    bad = dict(default)
    bad["variables"] = [
        dict(v, initial=1e9) if v["type"] == "continuous" and v["max"] == 1000.0 else v
        for v in default["variables"]
    ]
    resp = client.post(
        f"/api/projects/{project.id}/config/validate",
        json={"config": bad},
    )
    assert resp.status_code == 200
    assert resp.json()["count"] >= 1
    # 未落库: 读取仍是默认(version=None)
    got = client.get(f"/api/projects/{project.id}/config").json()
    assert got["version"] is None


# ---------------------------------------------------------------------------
# 5. IRR 硬约束与折现率独立
# ---------------------------------------------------------------------------


def test_irr_floor_and_discount_rate_are_independent_fields(
    client: TestClient, db: Session
) -> None:
    """irr_floor(顶层)与 parameters.economic.discount_rate 独立共存, 无冲突诊断。"""
    project = seed_project(db)
    default = _default_config(db, project)
    cfg = dict(default)
    cfg["irr_floor"] = 0.08  # 最低 IRR 硬约束
    cfg["parameters"]["economic"]["discount_rate"] = 0.05  # 折现率, 独立字段
    resp = client.put(
        f"/api/projects/{project.id}/config",
        json={"config": cfg, "expected_revision": 1},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["diagnostics"] == []


def test_discount_rate_misplaced_rejected(client: TestClient, db: Session) -> None:
    """折现率误放顶层 → PARAM-CONF-001(与最低 IRR 硬约束是两个字段)。"""
    project = seed_project(db)
    default = _default_config(db, project)
    cfg = dict(default)
    cfg["discount_rate"] = 0.05  # 误放顶层
    del cfg["parameters"]["economic"]["discount_rate"]
    resp = client.put(
        f"/api/projects/{project.id}/config",
        json={"config": cfg, "expected_revision": 1},
    )
    assert resp.status_code == 422
    codes = {d["code"] for d in resp.json()["diagnostics"]}
    assert "PARAM-CONF-001" in codes


def test_economic_nondefault_roundtrip_into_snapshot(client: TestClient, db: Session) -> None:
    """非默认经济参数: 保存 → 重读 → 进入任务快照全链路同值(FE-BE-01)。"""
    project = seed_project(db)
    default = _default_config(db, project)
    cfg = dict(default)
    cfg["parameters"]["economic"] = {
        "discount_rate": 0.05,
        "tax_rate": 0.2,
        "project_years": 15,
        "depreciation_years": 5,
        "currency": "CNY",
    }
    resp = client.put(
        f"/api/projects/{project.id}/config",
        json={"config": cfg, "expected_revision": 1},
    )
    assert resp.status_code == 200, resp.text
    # 重读: economic 子对象原值保留
    got = client.get(f"/api/projects/{project.id}/config").json()
    eco = got["config"]["parameters"]["economic"]
    assert eco["discount_rate"] == 0.05
    assert eco["tax_rate"] == 0.2
    assert eco["project_years"] == 15
    assert eco["depreciation_years"] == 5
    # 提交任务 → 快照 calc_config_snapshot 携带同值
    # (service 层直接调用: config 测试 app 已登录过 owner, 再次登录会触发
    # 窗口接管; 快照装配不依赖认证会话)
    from iesplan.models.calc import CalcSnapshot
    from iesplan.models.identity import User
    from iesplan.services import tasks as tasks_service

    owner_user = db.execute(
        select(User).where(User.username == OWNER_USERNAME)
    ).scalar_one()
    task = tasks_service.create_task(db, owner_user, project.id, "calc", config={})
    db.commit()
    snap = db.query(CalcSnapshot).order_by(CalcSnapshot.id.desc()).first()
    assert snap is not None
    eco_snap = snap.calc_config_snapshot["params"]["economic"]
    assert eco_snap["discount_rate"] == 0.05
    assert eco_snap["tax_rate"] == 0.2
    assert eco_snap["project_years"] == 15
    assert eco_snap["depreciation_years"] == 5


def test_irr_floor_missing_rejected(client: TestClient, db: Session) -> None:
    """缺少最低 IRR 硬约束字段 → SYS-CFG-001(REQ-CALC-006)。"""
    project = seed_project(db)
    default = _default_config(db, project)
    cfg = dict(default)
    del cfg["irr_floor"]
    resp = client.put(
        f"/api/projects/{project.id}/config",
        json={"config": cfg, "expected_revision": 1},
    )
    assert resp.status_code == 422
    codes = {d["code"] for d in resp.json()["diagnostics"]}
    assert "SYS-CFG-001" in codes


# ---------------------------------------------------------------------------
# 6. 算法兼容性
# ---------------------------------------------------------------------------


def test_algorithm_incompatible_rejected(client: TestClient, db: Session) -> None:
    """手动选择不支持能力(IRR 硬约束/容量设计)的算法 → SYS-CFG-001。"""
    project = seed_project(db)
    default = _default_config(db, project)
    cfg = dict(default)
    cfg["algorithm"] = {"mode": "manual", "name": "ies.algo.lp_relax"}  # 无 irr_hard_constraint
    resp = client.put(
        f"/api/projects/{project.id}/config",
        json={"config": cfg, "expected_revision": 1},
    )
    assert resp.status_code == 422
    diags = resp.json()["diagnostics"]
    assert any(d["code"] == "SYS-CFG-001" for d in diags)
    lp = next(d for d in diags if d["code"] == "SYS-CFG-001")
    assert "irr_hard_constraint" in lp["params"]["missing_capabilities"]
    assert "capacity_design" in lp["params"]["missing_capabilities"]


def test_algorithm_manual_milp_hybrid_accepted(client: TestClient, db: Session) -> None:
    """手动选择 milp_hybrid(能力齐全)通过。"""
    project = seed_project(db)
    default = _default_config(db, project)
    cfg = dict(default)
    cfg["algorithm"] = {"mode": "manual", "name": "ies.algo.milp_hybrid"}
    resp = client.put(
        f"/api/projects/{project.id}/config",
        json={"config": cfg, "expected_revision": 1},
    )
    assert resp.status_code == 200, resp.text
    # 读回: 手动模式 + 算法名保留
    got = client.get(f"/api/projects/{project.id}/config").json()
    assert got["config"]["algorithm"] == {
        "mode": "manual",
        "name": "ies.algo.milp_hybrid",
    }


def test_algorithm_unknown_rejected(client: TestClient, db: Session) -> None:
    """手动选择未注册算法 → CONN-TYPE-002。"""
    project = seed_project(db)
    default = _default_config(db, project)
    cfg = dict(default)
    cfg["algorithm"] = {"mode": "manual", "name": "ies.algo.not_exist"}
    resp = client.put(
        f"/api/projects/{project.id}/config",
        json={"config": cfg, "expected_revision": 1},
    )
    assert resp.status_code == 422
    codes = {d["code"] for d in resp.json()["diagnostics"]}
    assert "CONN-TYPE-002" in codes


def test_algorithm_auto_skips_capability_check(client: TestClient, db: Session) -> None:
    """auto 模式不查能力(REQ-CALC-005 自动选择由任务层负责)。"""
    project = seed_project(db)
    default = _default_config(db, project)
    cfg = dict(default)
    cfg["algorithm"] = {"mode": "auto", "name": "ies.algo.milp_hybrid"}
    resp = client.put(
        f"/api/projects/{project.id}/config",
        json={"config": cfg, "expected_revision": 1},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# 7. 默认配置 API / 算法注册表
# ---------------------------------------------------------------------------


def test_default_endpoint(client: TestClient, db: Session) -> None:
    """GET /config/default 重新生成默认配置 + 元数据。"""
    project = seed_project(db)
    resp = client.get(f"/api/projects/{project.id}/config/default")
    assert resp.status_code == 200
    body = resp.json()
    assert body["config"]["objectives"][0]["metric"] == "irr_after_tax"
    assert len(body["config"]["variables"]) == 3
    assert body["meta"]["parameters"]["economic"]["discount_rate"]["unit"] == "-"


def test_registry_algorithms_endpoint(client: TestClient) -> None:
    """GET /api/registry/algorithms: 算法列表 + 能力。"""
    resp = client.get("/api/registry/algorithms")
    assert resp.status_code == 200
    algos = resp.json()["algorithms"]
    ids = {a["algo_id"] for a in algos}
    assert "ies.algo.milp_hybrid" in ids
    milp = next(a for a in algos if a["algo_id"] == "ies.algo.milp_hybrid")
    assert "irr_hard_constraint" in milp["capabilities"]
    param_names = {p["name"] for p in milp["parameters"]}
    assert "gap_rel" in param_names


def test_validate_without_saving_keeps_revision(client: TestClient, db: Session) -> None:
    """校验接口不修改草稿修订绑定状态。"""
    project = seed_project(db)
    default = _default_config(db, project)
    resp = client.post(
        f"/api/projects/{project.id}/config/validate",
        json={"config": default},
    )
    assert resp.status_code == 200
    assert resp.json()["diagnostics"] == []


def test_saved_config_row_frozen_creates_new_version(client: TestClient, db: Session) -> None:
    """冻结行不可原地修改: 保存产生版本 +1 的新行(01 §6.1)。"""
    project = seed_project(db)
    default = _default_config(db, project)
    client.put(
        f"/api/projects/{project.id}/config",
        json={"config": default, "expected_revision": 1},
    )
    # 冻结当前行
    row = db.scalar(
        select(config_service.CalcConfig).where(
            config_service.CalcConfig.project_id == project.id
        )
    )
    row.status = "frozen"
    db.commit()
    # 再次保存 → 新版本行(status=draft)
    resp = client.put(
        f"/api/projects/{project.id}/config",
        json={"config": default, "expected_revision": 1},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 2
    assert resp.json()["status"] == "draft"


# ---------------------------------------------------------------------------
# 8. 服务层直测: 图 dict 直接校验(不依赖 DB)
# ---------------------------------------------------------------------------


def test_validate_config_with_dict_graph() -> None:
    """validate_config 直接接受 dict 图(兼容规划模板形态)。"""
    graph: dict[str, Any] = {
        "devices": [
            {"id": 1, "device_type": "ies.device.grid_connection", "kind": "existing", "name": "电网"},
            {
                "id": 2,
                "device_type": "ies.device.pv",
                "kind": "new",
                "name": "光伏",
                "params": {"rated_capacity_kwp": 0},
            },
        ]
    }
    cfg = {
        "parameters": {
            "devices": {"2": {"rated_capacity_kwp": 0, "max_capacity_kwp": 1000, "efficiency": 0.2}},
            "economic": {"discount_rate": 0.08, "tax_rate": 0.25, "project_years": 20,
                         "depreciation_years": 10, "currency": "CNY"},
            "environmental": {"emission_factor_grid": 0.581, "emission_factor_gas": 2.0},
        },
        "variables": [
            {"name": "pv_cap", "type": "continuous", "initial": 100, "min": 0, "max": 1000,
             "device_ref": 2, "param": "rated_capacity_kwp", "unit": "kWp"}
        ],
        "objectives": [{"metric": "irr_after_tax", "direction": "max", "weight": 1.0}],
        "constraints": [
            {"type": "predefined", "payload": {"kind": "load_satisfaction"}},
            {"type": "expression", "payload": {"expression": "pv_cap >= 50 kW"}},
        ],
        "algorithm": {"mode": "auto", "name": "ies.algo.milp_hybrid"},
        "irr_floor": 0.08,
        "tolerances": {"gap_rel": 0.001, "time_limit_s": 600},
        "random_seed": 42,
    }
    diags = config_service.validate_config(cfg, graph)
    assert diags == []


def test_variable_device_ref_unknown_diagnostic() -> None:
    """变量 device_ref 指向图中不存在的设备 → CONN-TYPE-002。"""
    graph = {"devices": [{"id": 1, "device_type": "ies.device.pv", "kind": "new", "name": "光伏"}]}
    cfg = {
        "parameters": {
            "devices": {},
            "economic": {"discount_rate": 0.08, "tax_rate": 0.25, "project_years": 20,
                         "depreciation_years": 10, "currency": "CNY"},
            "environmental": {"emission_factor_grid": 0.581, "emission_factor_gas": 2.0},
        },
        "variables": [
            {"name": "pv_cap", "type": "continuous", "initial": 1, "min": 0, "max": 10,
             "device_ref": 999, "param": "rated_capacity_kwp", "unit": "kWp"}
        ],
        "objectives": [{"metric": "irr_after_tax", "direction": "max", "weight": 1.0}],
        "constraints": [],
        "algorithm": {"mode": "auto", "name": "ies.algo.milp_hybrid"},
        "irr_floor": 0.08,
        "tolerances": {},
        "random_seed": 42,
    }
    diags = config_service.validate_config(cfg, graph)
    assert any(d.code == "CONN-TYPE-002" for d in diags)


def test_anonymous_config_401(db: Session) -> None:
    """配置端点匿名访问 → 401(认证先于项目权限判定)。"""
    with TestClient(make_app(db), raise_server_exceptions=False) as anon:
        resp = anon.get("/api/projects/1/config")
        assert resp.status_code == 401
        resp = anon.put("/api/projects/1/config", json={"config": {}, "expected_revision": 1})
        assert resp.status_code == 401
