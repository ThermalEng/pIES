"""独立 HTTP 契约测试(0.11 退出标准: HTTP 契约测试独立于 ORM/领域私有实现)。

合同层 vs 集成层: 本文件是合同层——数据准备与断言只经公开 HTTP(含管理
端点与自助注册开关); 不导入 ORM(Session/Base)、领域 persistence、私有
helper、私有测试 helper, 也不把同类依赖藏进 conftest(本目录 conftest 仅做
环境变量与公开注册表初始化)。ORM 深入断言保留在 test_*_api 等集成测试中,
不在本文件重复。包装键断言以 iesplan/api/wrapper_keys.py 登记为 oracle。

运行(独立进程, 自带 Postgres; 不与整套 SQLite 测试同进程混跑):
  docker run -d --rm --name pies-contract-pg -p 5433:5432 \\
    -e POSTGRES_USER=iesplan -e POSTGRES_PASSWORD=iesplan_contract \\
    -e POSTGRES_DB=contract_test postgres:16-alpine
  docker build -t pies-contract-test ./backend
  docker run --rm --network host -e IESPLAN_HTTP_CONTRACT=1 pies-contract-test \\
    python -m pytest tests/test_http_contract.py -q -p no:cacheprovider
可用 IESPLAN_CONTRACT_PG_URL 覆盖默认库地址。未显式启用
(IESPLAN_HTTP_CONTRACT=1)时本模块在采集期直接跳过, 不改写任何环境变量,
不导入 iesplan, 不连接数据库; 普通整套 pytest 因此不受影响。

覆盖: 标准错误信封(8 字段)、成功包装键、状态码、关键权限/幂等语义,
涉及认证/项目/草稿/数据/配置/任务/结果(无证据形态)/导出。
未覆盖(需 Worker 真实执行, 属集成层): 有证据的结果成功路径(hourly 数值、
assess/select/check 成功、Excel 成功)、长时任务执行。
"""

import os
import tempfile
import uuid

import pytest

# 运行模式判定必须在任何环境变量修改/iesplan 导入之前: 未显式启用专用开关
# 时直接跳过, 不污染同进程其他测试的环境, 也不连接任何数据库。
if os.environ.get("IESPLAN_HTTP_CONTRACT") != "1":
    pytest.skip(
        "契约测试须显式启用(IESPLAN_HTTP_CONTRACT=1)后独立运行",
        allow_module_level=True,
    )

# 专用运行才到这里: 公开部署配置(须在任何 iesplan 导入之前生效)。
os.environ["IESPLAN_DB_URL"] = os.environ.get(
    "IESPLAN_CONTRACT_PG_URL",
    "postgresql+psycopg://iesplan:iesplan_contract@127.0.0.1:5433/contract_test",
)
os.environ["IESPLAN_DATA_DIR"] = tempfile.mkdtemp(prefix="pies-contract-data-")
os.environ["IESPLAN_QUEUE"] = "memory"

from fastapi.testclient import TestClient  # noqa: E402

from iesplan.main import create_app  # noqa: E402  # 公开组合根, 非 ORM/私有实现

ADMIN_INITIAL_PASSWORD = "iesplan-admin-initial"
ADMIN_NEW_PASSWORD = "AdminChanged12345"

ERROR_FIELDS = {
    "code",
    "message_key",
    "severity",
    "blocking",
    "params",
    "location",
    "fix_hint_key",
    "ref_ids",
}

# 内联测试数据(字面量, 非领域导入): 与 test_config_revisions.PROFILE_PAYLOAD
# 同构的财务画像载荷(公开 registration 契约形状)。
PROFILE_PAYLOAD = {
    "schema": "ies.finance-profile",
    "schema_version": "1.0.0",
    "profile": {
        "id": "cn-north-demo",
        "region": "CN-North",
        "currency": "CNY",
        "base_year": 2025,
        "price_basis": "tax_inclusive",
        "cost_method": "fixed_plus_linear",
    },
    "finance_types": {
        "pv_system": {
            "upfront_capex": {
                "fixed": {"value": "0", "unit": "CNY"},
                "linear": {"capacity_kw": {"unit_cost": {"value": "3500", "unit": "CNY/kW"}}},
            },
            "annual_fixed_om": {
                "linear": {"capacity_kw": {"unit_cost": {"value": "35", "unit": "CNY/kW/a"}}},
            },
            "period_variable_om": {
                "linear": {"generated_kwh": {"unit_cost": {"value": "0.01", "unit": "CNY/kWh"}}},
            },
        },
        "battery_system": {
            "upfront_capex": {
                "linear": {"energy_kwh": {"unit_cost": {"value": "900", "unit": "CNY/kWh"}}},
            },
            "annual_fixed_om": {
                "linear": {"energy_kwh": {"unit_cost": {"value": "18", "unit": "CNY/kWh/a"}}},
            },
            "period_variable_om": {
                "linear": {"cycled_kwh": {"unit_cost": {"value": "0.002", "unit": "CNY/kWh"}}},
            },
        },
    },
    "energy_prices": {
        "grid_import": {
            "carrier": "electricity",
            "direction": "purchase",
            "kind": "constant",
            "value": {"value": "0.7", "unit": "CNY/kWh"},
        },
        "pv_export": {
            "carrier": "electricity",
            "direction": "sale",
            "kind": "constant",
            "value": {"value": "0.4", "unit": "CNY/kWh"},
        },
    },
    "taxes": {},
}

#: 现行装配边界要求的显式计算声明(经 config.patch 草稿命令, 无静默默认)。
EXPLICIT_CALC_PATCH = {
    "id": "c-calc",
    "unit": "config",
    "type": "config.patch",
    "payload": {
        "mode": "fixed_operation",
        "generator": "ies.generator.highs_milp@1.0.0",
        "solver": "ies.solver.highs@1.7.2",
        "time_axis": {"resolution": "1h", "start": "2025-01-01T00:00:00Z"},
    },
}

BATTERY_COMMAND = {
    "id": "c-bat1",
    "unit": "model",
    "type": "model.upsert_device",
    "payload": {
        "name": "bat1",
        "device_type": "ies.device.battery",
        "kind": "new",
        "params": {"capacity_kwh": 200, "rated_power_kw": 100},
        "position": {"x": 1, "y": 1},
    },
}

#: 合法 12 字段 CalculationConfig(与 test_config_api 同构的最小有效载荷)。
_CALC_PAYLOAD = {
    "schema": "ies.calculation-config",
    "schema_version": "1.0.0",
    "mode": "planning",
    "predictions": [],
    "generator": "ies.generator.highs_milp@1.0.0",
    "solver": "ies.solver.highs@1.7.2",
    "precision": "medium",
    "convergence": None,
    "time_limit_seconds": 600,
    "options": {},
    "seed": None,
    "outputs": ["solution"],
}

#: 最小规划修订(公开规划契约形状: objective + 变量字典 + constraints)。
PLANNING_PAYLOAD = {
    "objective": {"sense": "minimize", "expression": "0"},
    "variables": {
        "cap": {
            "device_ref": "bat1",
            "parameter": "capacity_kwh",
            "lower_bound": "0",
            "upper_bound": "1000",
            "unit": "kWh",
        },
    },
    "constraints": {},
}


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _password(uid: str) -> str:
    return f"Pw{uid}12345"


def _assert_envelope(resp, status: int, code: str | None = None) -> dict:
    """标准错误信封断言: 状态码 + 8 字段 + 可选诊断码。"""
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"error"}, body
    err = body["error"]
    assert set(err) == ERROR_FIELDS, err
    if code is not None:
        assert err["code"] == code, err
    return err


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _isolate_auth_state(client: TestClient):
    """每个测试前清空会话 cookie, 防止登录态在测试间泄漏(公开 TestClient API)。"""
    client.cookies.clear()
    yield
    client.cookies.clear()


@pytest.fixture(scope="module")
def admin_headers(client: TestClient) -> dict[str, str]:
    """管理员首登改密 + 打开自助注册(全经公开 HTTP)。

    容器数据库在多次运行时保持不变, 首登改密只在初始密码仍有效时执行一次。
    """
    resp = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": ADMIN_INITIAL_PASSWORD},
    )
    if resp.status_code == 200:
        headers = {"Authorization": f"Bearer {resp.json()['token']}"}
        resp = client.post(
            "/api/auth/change-password",
            json={
                "old_password": ADMIN_INITIAL_PASSWORD,
                "new_password": ADMIN_NEW_PASSWORD,
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        password = ADMIN_NEW_PASSWORD
    else:
        password = ADMIN_NEW_PASSWORD
    resp = client.post(
        "/api/auth/login", json={"username": "admin", "password": password}
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    if resp.json().get("needs_takeover_confirm") is True:
        resp = client.post(
            "/api/auth/confirm-takeover", json={"token": token},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        token = resp.json()["token"]
    admin = {"Authorization": f"Bearer {token}"}
    resp = client.put(
        "/api/auth/settings", json={"registration_enabled": True}, headers=admin
    )
    assert resp.status_code == 200, resp.text
    return admin


def _register_user(client: TestClient, username: str) -> dict[str, str]:
    """自助注册 + 登录, 返回认证头(全经公开 HTTP)。"""
    password = _password(username[-8:] if len(username) >= 8 else username)
    resp = client.post(
        "/api/auth/register", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) >= {"id", "username", "role", "status"}
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _make_project(client: TestClient, headers: dict[str, str], name: str) -> int:
    resp = client.post(
        "/api/projects",
        json={
            "name": name,
            "baseline_resolution": "1h",
            "baseline_leap_year": False,
            "baseline_scenario_mode": "single",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == {"my_role", "project"}
    return body["project"]["id"]


def _draft_revision(client: TestClient, headers: dict[str, str], pid: int) -> int:
    resp = client.get(f"/api/projects/{pid}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["draft"]["revision"]


def _put_draft(
    client: TestClient, headers: dict[str, str], pid: int, commands: list
) -> int:
    revision = _draft_revision(client, headers, pid)
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": revision, "commands": commands},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"results", "revision"}
    return body["revision"]


def _declare_calc_ready(
    client: TestClient, headers: dict[str, str], pid: int
) -> None:
    """经公开 HTTP 把项目声明到可提交计算任务(计算配置/财务画像/设备/规划/装配绑定)。"""
    _put_draft(client, headers, pid, [EXPLICIT_CALC_PATCH])
    resp = client.post(
        "/api/finance-profiles", json={"finance_profile": PROFILE_PAYLOAD}, headers=headers
    )
    assert resp.status_code in (200, 201), resp.text
    assert set(resp.json()) == {"finance_profile", "row"}
    resp = client.put(
        f"/api/projects/{pid}/finance-profile",
        json={"profile_ref": {"id": "cn-north-demo"}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {
        "finance_profile",
        "overrides_revision",
        "effective_finance_config",
        "revision",
    }
    _put_draft(client, headers, pid, [BATTERY_COMMAND])
    resp = client.put(
        f"/api/projects/{pid}/planning-config",
        json={"planning_config": PLANNING_PAYLOAD, "expected_revision": None},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"planning_config", "revision"}
    revision = _draft_revision(client, headers, pid)
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={
            "expected_revision": revision,
            "commands": [
                {
                    "id": "c-finbind",
                    "unit": "assembly",
                    "type": "assembly.set_finance_binding",
                    "payload": {"instances": {"bat1": {"type": "none"}}},
                },
                {
                    "id": "c-tariff",
                    "unit": "assembly",
                    "type": "assembly.set_tariff_bindings",
                    "payload": {"bindings": {}},
                },
            ],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# 认证与权限
# ---------------------------------------------------------------------------


def test_auth_register_login_me(client: TestClient, admin_headers: dict) -> None:
    uid = _uid()
    headers = _register_user(client, f"contract_user_{uid}")
    resp = client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["username"] == f"contract_user_{uid}"
    assert body["role"] == "engineer"


def test_auth_wrong_password_401_envelope(client: TestClient, admin_headers: dict) -> None:
    uid = _uid()
    _register_user(client, f"contract_user_{uid}")
    resp = client.post(
        "/api/auth/login",
        json={"username": f"contract_user_{uid}", "password": "Wrong99999"},
    )
    _assert_envelope(resp, 401)


def test_auth_unauthenticated_401_envelope(client: TestClient) -> None:
    resp = client.get("/api/auth/me")
    _assert_envelope(resp, 401)


def test_auth_duplicate_register_409_envelope(
    client: TestClient, admin_headers: dict
) -> None:
    uid = _uid()
    _register_user(client, f"contract_dup_{uid}")
    resp = client.post(
        "/api/auth/register",
        json={"username": f"contract_dup_{uid}", "password": _password(uid)},
    )
    _assert_envelope(resp, 409)


# ---------------------------------------------------------------------------
# 项目与草稿
# ---------------------------------------------------------------------------


def test_projects_crud_keys_and_visibility(
    client: TestClient, admin_headers: dict
) -> None:
    uid = _uid()
    owner = _register_user(client, f"contract_owner_{uid}")
    outsider = _register_user(client, f"contract_outsider_{uid}")
    pid = _make_project(client, owner, f"contract-proj-{uid}")
    resp = client.get(f"/api/projects/{pid}", headers=owner)
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"project", "draft", "versions", "my_role"}
    resp = client.get("/api/projects", headers=owner)
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"projects"}
    assert any(p["id"] == pid for p in resp.json()["projects"])
    _assert_envelope(
        client.get(f"/api/projects/{pid}", headers=outsider), 403
    )
    _assert_envelope(client.get("/api/projects/999999999", headers=owner), 404)


def test_draft_revision_conflict_409_envelope(
    client: TestClient, admin_headers: dict
) -> None:
    uid = _uid()
    owner = _register_user(client, f"contract_drafter_{uid}")
    pid = _make_project(client, owner, f"contract-draft-{uid}")
    resp = client.put(
        f"/api/projects/{pid}/draft",
        json={"expected_revision": 999999, "commands": [EXPLICIT_CALC_PATCH]},
        headers=owner,
    )
    _assert_envelope(resp, 409)


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------

DATA_COLUMNS = (
    "timestamp",
    "e_load",
    "h_load",
    "c_load",
    "t_ambient",
    "ghi",
    "electricity_price",
    "grid_emission_factor",
)


def _make_year_csv() -> bytes:
    """内联整年 1h CSV(本地 2025-01-01 起, 与集成测试同构的数据形态)。"""
    import datetime as _dt

    lines = [",".join(DATA_COLUMNS)]
    start = _dt.datetime(2025, 1, 1, 0, 0)
    for i in range(8760):
        ts = start + _dt.timedelta(hours=i)
        hour = i % 24
        ghi = 300.0 if 6 <= hour <= 18 else 0.0
        lines.append(
            ",".join(
                [
                    ts.strftime("%Y-%m-%d %H:%M"),
                    f"{100.0 + (i % 24) * 5.0:g}",
                    f"{50.0 + (i % 12) * 3.0:g}",
                    f"{30.0 + (i % 10) * 2.0:g}",
                    f"{15.0 + 8.0 * (i / 24.0 % 1):g}",
                    f"{ghi:g}",
                    "0.6",
                    "0.581",
                ]
            )
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _make_dataset(client: TestClient, headers: dict[str, str], pid: int, name: str) -> int:
    resp = client.post(
        f"/api/projects/{pid}/datasets",
        json={"name": name, "source_category": "user_upload", "license": "CC-BY-4.0"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == {"dataset"}
    return body["dataset"]["id"]


def test_datasets_crud_and_upload(
    client: TestClient, admin_headers: dict
) -> None:
    uid = _uid()
    owner = _register_user(client, f"contract_data_{uid}")
    pid = _make_project(client, owner, f"contract_data_{uid}")
    ds_id = _make_dataset(client, owner, pid, f"负荷数据-{uid}")
    resp = client.get(f"/api/projects/{pid}/datasets/{ds_id}", headers=owner)
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"dataset", "versions"}
    # 垃圾文件阻断: 400 信封
    resp = client.post(
        f"/api/projects/{pid}/datasets/{ds_id}/versions",
        data={"resolution": "1h", "utc_offset_minutes": "480"},
        files={"file": ("bad.csv", b"not,a,csv\n", "text/csv")},
        headers=owner,
    )
    _assert_envelope(resp, 400)
    # 合法整年上传: 201 + 版本键
    resp = client.post(
        f"/api/projects/{pid}/datasets/{ds_id}/versions",
        data={"resolution": "1h", "utc_offset_minutes": "480"},
        files={"file": ("data.csv", _make_year_csv(), "text/csv")},
        headers=owner,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == {"dataset_version", "diagnostics", "quality_report"}
    assert body["dataset_version"]["version_no"] == 1
    assert body["quality_report"]["row_count"] == 8760


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def test_config_read_and_finance_planning_keys(
    client: TestClient, admin_headers: dict
) -> None:
    uid = _uid()
    owner = _register_user(client, f"contract_cfg_{uid}")
    pid = _make_project(client, owner, f"contract_cfg_{uid}")
    # 未保存读 404; 保存合法 CalculationConfig 后读回 revision 链
    resp = client.get(f"/api/projects/{pid}/config", headers=owner)
    assert resp.status_code == 404, resp.text
    resp = client.put(
        f"/api/projects/{pid}/config",
        json={"calculation_config": _CALC_PAYLOAD, "expected_revision": None},
        headers=owner,
    )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"calculation_config", "revision"}
    resp = client.get(f"/api/projects/{pid}/config", headers=owner)
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"calculation_config", "revision"}
    resp = client.get("/api/finance-profiles", headers=owner)
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"items", "count"}
    _declare_calc_ready(client, owner, pid)
    resp = client.get(f"/api/projects/{pid}/planning-config", headers=owner)
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"planning_config", "revision"}
    resp = client.post(f"/api/projects/{pid}/validation/run", headers=owner)
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"report", "stored"}


# ---------------------------------------------------------------------------
# 任务: 提交/幂等/查询/取消/权限
# ---------------------------------------------------------------------------


def _save_config(client: TestClient, headers: dict[str, str], pid: int) -> int:
    """保存一份计算配置, 返回 revision(计算任务提交绑定用)。"""
    resp = client.put(
        f"/api/projects/{pid}/config",
        json={"calculation_config": _CALC_PAYLOAD, "expected_revision": None},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def _submit_task(
    client: TestClient, headers: dict[str, str], pid: int, key: str | None,
    config_revision: int | None = None,
) -> tuple[int, dict]:
    payload: dict = {"task_type": "optimization"}
    if config_revision is not None:
        payload["config"] = {"config_revision": config_revision}
    if key is not None:
        payload["idempotency_key"] = key
    resp = client.post(f"/api/projects/{pid}/tasks", json=payload, headers=headers)
    return resp.status_code, resp.json()


def test_tasks_lifecycle_idempotency_permission(
    client: TestClient, admin_headers: dict
) -> None:
    uid = _uid()
    owner = _register_user(client, f"contract_task_{uid}")
    outsider = _register_user(client, f"contract_taskout_{uid}")
    pid = _make_project(client, owner, f"contract_task_{uid}")
    _declare_calc_ready(client, owner, pid)
    rev = _save_config(client, owner, pid)
    # 缺键拒绝 422 标准信封(API-REQ-001, contracts.md:422 行; pydantic 校验
    # 失败走 RequestValidationError 处理器, 无裸 detail 偏差)
    resp = client.post(
        f"/api/projects/{pid}/tasks", json={"task_type": "optimization"}, headers=owner
    )
    _assert_envelope(resp, 422, code="API-REQ-001")
    # 新建 201 + 包装键
    status, body = _submit_task(client, owner, pid, f"ct-{uid}-1", config_revision=rev)
    assert status == 201, body
    assert set(body) == {"hint", "replayed", "task"}
    assert body["replayed"] is False
    task_id = body["task"]["id"]
    # 同键重试 200 同一任务
    status, body = _submit_task(client, owner, pid, f"ct-{uid}-1", config_revision=rev)
    assert status == 200, body
    assert body["task"]["id"] == task_id
    assert body["replayed"] is True
    assert body["hint"]
    # 查询与列表包装键
    resp = client.get(f"/api/projects/{pid}/tasks/{task_id}", headers=owner)
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"task"}
    resp = client.get(f"/api/projects/{pid}/tasks", headers=owner)
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"items", "next_cursor"}
    # 无权限提交 403 信封
    status, _ = _submit_task(client, outsider, pid, f"ct-{uid}-9")
    assert status == 403
    # 取消 200 + 重复取消 409 信封
    resp = client.post(f"/api/projects/{pid}/tasks/{task_id}/cancel", headers=owner)
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"cancel_status", "diagnostic", "task"}
    resp = client.post(f"/api/projects/{pid}/tasks/{task_id}/cancel", headers=owner)
    _assert_envelope(resp, 409)


# ---------------------------------------------------------------------------
# 结果(无证据形态)与导出
# ---------------------------------------------------------------------------


def test_results_no_evidence_shapes(
    client: TestClient, admin_headers: dict
) -> None:
    uid = _uid()
    owner = _register_user(client, f"contract_result_{uid}")
    pid = _make_project(client, owner, f"contract_result_{uid}")
    _declare_calc_ready(client, owner, pid)
    rev = _save_config(client, owner, pid)
    status, body = _submit_task(client, owner, pid, f"cr-{uid}-1", config_revision=rev)
    assert status == 201, body
    task_id = body["task"]["id"]
    resp = client.get(f"/api/projects/{pid}/tasks/{task_id}/result", headers=owner)
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"result"}
    assert resp.json()["result"]["evidence_status"] == "no_evidence"
    resp = client.get(
        f"/api/projects/{pid}/tasks/{task_id}/result/hourly?field=p_grid_buy", headers=owner
    )
    _assert_envelope(resp, 404)
    resp = client.post(
        f"/api/projects/{pid}/tasks/{task_id}/result/assess", json={}, headers=owner
    )
    _assert_envelope(resp, 404)
    resp = client.post(
        f"/api/projects/{pid}/tasks/{task_id}/result/check",
        json={"idempotency_key": f"cr-{uid}-chk"},
        headers=owner,
    )
    _assert_envelope(resp, 404)
    resp = client.post(
        f"/api/projects/{pid}/tasks/{task_id}/result/select",
        json={"solution_id": 0},
        headers=owner,
    )
    _assert_envelope(resp, 404)
    resp = client.get(
        f"/api/projects/{pid}/tasks/{task_id}/result/diff", headers=owner
    )
    _assert_envelope(resp, 404)


def test_exports_package_download_and_excel_guard(
    client: TestClient, admin_headers: dict
) -> None:
    uid = _uid()
    owner = _register_user(client, f"contract_export_{uid}")
    pid = _make_project(client, owner, f"contract_export_{uid}")
    _declare_calc_ready(client, owner, pid)
    rev = _save_config(client, owner, pid)
    status, body = _submit_task(client, owner, pid, f"ce-{uid}-1", config_revision=rev)
    assert status == 201, body
    # 项目包导出 200 + 包装键, 凭 token 下载 200
    resp = client.post(f"/api/projects/{pid}/exports/package", headers=owner)
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {
        "expires_at",
        "file_name",
        "manifest",
        "media_type",
        "object_id",
        "oid",
        "size_bytes",
        "token",
    }
    token = resp.json()["token"]
    resp = client.get(f"/api/projects/{pid}/exports/package/download?token={token}")
    assert resp.status_code == 200, resp.text
    assert "zip" in resp.headers["content-type"]
    # 坏 token 400 信封
    resp = client.get(f"/api/projects/{pid}/exports/package/download?token=bad-token")
    _assert_envelope(resp, 400)
    # 无证据 Excel 导出 404 信封
    resp = client.post(
        f"/api/projects/{pid}/exports/excel",
        json={"evidence_package_id": 999999999, "assessment_id": 999999999},
        headers=owner,
    )
    _assert_envelope(resp, 404)
