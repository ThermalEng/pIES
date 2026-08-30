"""0.3.0 C4: 公共协议测试基线 —— 错误信封形状 + 成功响应包装键锁定。

目标: 把「错误信封 8 字段形状」与「各域成功响应包装键」固化为可 CI 运行的
契约, 禁止形状漂移。覆盖:

1. test_error_envelope_shape_all_error_paths: 全库代表性错误路径(422/400/403
   CSRF/429/404/500/403 权限/401/业务错误), 每个非 2xx 响应必须为标准信封
   {"error": {code, message_key, severity, blocking, params, location,
   fix_hint_key, ref_ids}} 8 字段且字段集精确(不多不少)。
2. test_success_wrapper_*: 锁定各域成功响应顶层包装键(命名键现状, 用户决策
   2026-08-23: 不迁移通用 {"data","meta"} 包装)。以实际为准, 期望与任务书
   不符处以注释记录差异。
3. test_no_bare_vs_wrapped_duplication: 静态门禁(AST 扫描 backend/iesplan/api/):
   同一端点函数的全部 dict 形状返回必须相互为子集/超集, 禁止「有时裸返回、
   有时包装返回」的两版响应。
4. test_frontend_contract_adapters_consistent: 对照 frontend/src/api/client.ts
   的 asItems/oneOf 适配规则抽查, 确认实际响应与前端适配假设一致。
5. test_pydantic_422_is_bare_detail: 记录已知偏差 —— FastAPI 请求体校验
   (RequestValidationError)的 422 仍为裸 {"detail": [...]}, 未走标准信封
   (业务 bug 记录, 本切片不修)。

运行方式: 与 test_security_regression 同构 —— create_app() 挂载全部业务路由,
SQLite :memory:(StaticPool) + dependency_overrides 替换 get_db,
raise_server_exceptions=False(500 路径断言需要)。
"""

from __future__ import annotations

import ast
import os
from collections.abc import Iterator
from pathlib import Path

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")

import pytest  # noqa: E402
from auth_helpers import make_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan.db import Base, get_db  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.services import identity  # noqa: E402

PASSWORD = "Test12345"

#: 标准错误信封内层 8 字段精确集合(宪法 §8.3, core/errors.error_envelope 权威)
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


# ---------------------------------------------------------------------------
# 测试环境(与 test_security_regression 同构)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """会话级 SQLite 内存引擎(StaticPool: 所有会话共享同一连接)。"""
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
    """每个测试独立事务起点(共用同一连接, 测试间数据保留)。"""
    with Session(engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture()
def client(db: Session) -> Iterator[TestClient]:
    """挂载全部业务路由的应用测试客户端(get_db 替换为内存 SQLite)。

    raise_server_exceptions=False: 500 未捕获异常路径以 HTTP 响应断言,
    不向上抛异常。
    """
    app = create_app()

    def _override_get_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    identity.reset_login_rate_limit()
    # 全局限流状态为进程级共享: 同时清空, 防止本文件残留计数影响后续
    # 文件的 401 断言(或反之被前序高频文件打成 429)
    from iesplan.api.limits import reset_rate_limit

    reset_rate_limit()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _login(client: TestClient, username: str, password: str = PASSWORD) -> str:
    """真实登录返回窗口凭证(TestClient 同时持有会话 Cookie)。"""
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_project(client: TestClient, token: str, name: str) -> int:
    """通过 API 创建项目, 返回 project_id(显式携带默认基线)。"""
    resp = client.post(
        "/api/projects",
        json={
            "name": name,
            "baseline_resolution": "1h",
            "baseline_leap_year": False,
            "baseline_scenario_mode": "single",
        },
        headers=_bearer(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["project"]["id"]


def _add_device(client: TestClient, token: str, pid: int) -> None:
    """添加一台光伏设备(使默认配置产生可校验的容量变量)。"""
    resp = client.post(
        f"/api/projects/{pid}/model/devices",
        json={"device_type": "ies.device.pv", "name": "PV1", "params": {}},
        headers=_bearer(token),
    )
    assert resp.status_code == 201, resp.text


def _make_small_csv(n: int = 10) -> bytes:
    """行数不足的标准 CSV(触发 DATA-TS-004 阻断诊断 → DATA-VAL-001)。"""
    lines = [
        "timestamp,e_load,h_load,c_load,t_ambient,ghi,electricity_price,grid_emission_factor"
    ]
    for i in range(n):
        lines.append(
            f"2025-01-{(i % 28) + 1:02d} 00:{(i % 60):02d},"
            f"{100.0 + i},{50.0},{30.0},{15.0},{0.0},{0.6},{0.581}"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def assert_error_envelope(
    resp, status: int, *, code: str | None = None, message_key: str | None = None
) -> dict:
    """断言非 2xx 响应为标准错误信封(字段集精确, 不多不少)。"""
    assert resp.status_code == status, f"{status} 期望, 实际 {resp.status_code}: {resp.text}"
    body = resp.json()
    # 顶层键集精确 == {"error"}(无 detail/ok 等杂键)
    assert set(body) == {"error"}, f"顶层键集漂移: {sorted(body)}"
    err = body["error"]
    # error 内层 8 字段精确集合(不多不少)
    assert set(err) == ERROR_FIELDS, f"错误信封字段集漂移: {sorted(err)}"
    assert isinstance(err["message_key"], str) and err["message_key"], "message_key 必须为非空字符串"
    if code is not None:
        assert err["code"] == code, f"code 期望 {code}, 实际 {err['code']}"
    if message_key is not None:
        assert err["message_key"] == message_key, (
            f"message_key 期望 {message_key}, 实际 {err['message_key']}"
        )
    return err


def _default_config(client: TestClient, token: str, pid: int) -> dict:
    """读取项目当前默认配置(设备添加后含容量变量)。"""
    resp = client.get(f"/api/projects/{pid}/config", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    return resp.json()["config"]


# ---------------------------------------------------------------------------
# 1. 错误信封形状: 全库错误路径
# ---------------------------------------------------------------------------


def test_error_envelope_shape_all_error_paths(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全库代表性错误路径均输出标准 8 字段信封, 字段集精确。"""
    owner = make_user(db, "proto_owner")
    viewer = make_user(db, "proto_viewer")
    admin = make_user(db, "proto_admin", role="admin")
    owner_tok = _login(client, "proto_owner")
    viewer_tok = _login(client, "proto_viewer")
    admin_tok = _login(client, "proto_admin")

    # --- 1) config PUT 422(CONFIG-VAL-001): 非法变量类型触发校验失败 ---
    pid = _create_project(client, owner_tok, "协议项目")
    _add_device(client, owner_tok, pid)
    cfg = _default_config(client, owner_tok, pid)
    bad_cfg = dict(cfg)
    bad_cfg["variables"] = [dict(cfg["variables"][0], type="fuzzy")]
    resp = client.put(
        f"/api/projects/{pid}/config",
        json={"config": bad_cfg, "expected_revision": 1},
        headers=_bearer(owner_tok),
    )
    err = assert_error_envelope(resp, 422, code="CONFIG-VAL-001")
    assert err["blocking"] is True
    assert isinstance(err["params"]["diagnostics"], list) and err["params"]["count"] >= 1

    # --- 2) datasets 上传 400(DATA-VAL-001): 行数不足 → DATA-TS-004 ---
    resp = client.post(
        f"/api/projects/{pid}/datasets",
        json={"name": "坏数据"},
        headers=_bearer(owner_tok),
    )
    assert resp.status_code == 201, resp.text
    ds_id = resp.json()["dataset"]["id"]
    resp = client.post(
        f"/api/projects/{pid}/datasets/{ds_id}/versions",
        data={"resolution": "1h", "utc_offset_minutes": "480"},
        files={"file": ("small.csv", _make_small_csv(n=10), "text/csv")},
        headers=_bearer(owner_tok),
    )
    err = assert_error_envelope(resp, 400, code="DATA-VAL-001")
    codes = [d["code"] for d in err["params"]["diagnostics"]]
    assert "DATA-TS-004" in codes

    # --- 3) CSRF 403(AUTH-CSRF-001): 带会话 Cookie 的跨源状态变更 ---
    # TestClient 登录后已持有 ies_session Cookie; 携带非可信 Origin 的 POST 被拒
    resp = client.post(
        "/api/projects",
        json={"name": "跨源项目"},
        headers={"Origin": "http://evil.example.com"},
    )
    assert_error_envelope(resp, 403, code="AUTH-CSRF-001")

    # --- 4) 限流 429(API-RL-001): 调低阈值后打满 ---
    from iesplan.api.limits import reset_rate_limit
    from iesplan.config import settings

    monkeypatch.setattr(settings, "rate_limit_max_requests", 3)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 60)
    reset_rate_limit()
    # 限流探针须用匿名客户端: 主 client 持有登录 Cookie, 带 Cookie 的请求会以
    # 会话身份进入业务逻辑(如 admin 无项目权限 → 403 而非 401), 干扰计数预期
    with TestClient(client.app, raise_server_exceptions=False) as anon:
        for _ in range(3):
            resp = anon.get("/api/projects/1/datasets")  # 非豁免路径(401 也计入)
            assert resp.status_code == 401, resp.text
        resp = anon.get("/api/projects/1/datasets")
    err = assert_error_envelope(resp, 429, code="API-RL-001", message_key="ies.error.rate_limited")
    assert err["params"]["retry_after"] == 60
    # 重置限流计数并恢复宽松阈值, 避免本测试后续请求被 429 误伤
    # (窗口内已计 4 次, 后续 404/500/403/401/业务断言还会发多个请求;
    # monkeypatch 在测试结束后自动还原)
    monkeypatch.setattr(settings, "rate_limit_max_requests", 100000)
    reset_rate_limit()

    # --- 5) 404 路由未找到(API-NF-001) ---
    resp = client.get("/api/no-such-route")
    assert_error_envelope(resp, 404, code="API-NF-001", message_key="ies.error.route_not_found")

    # --- 6) 500 未捕获异常(API-500-001): 注入端点抛未捕获 RuntimeError ---
    monkeypatch.setattr(
        "iesplan.services.project.list_visible_projects",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    resp = client.get("/api/projects", headers=_bearer(owner_tok))
    assert_error_envelope(resp, 500, code="API-500-001", message_key="ies.error.internal")

    # --- 7) 403 权限(PERM-DENIED-001): viewer 写项目(config PUT) ---
    resp = client.put(
        f"/api/projects/{pid}/config",
        json={"config": bad_cfg, "expected_revision": 1},
        headers=_bearer(viewer_tok),
    )
    assert_error_envelope(resp, 403, code="PERM-DENIED-001", message_key="ies.diag.perm.denied")

    # --- 8) 401 未认证(AUTH-REQ-001): 匿名客户端(主 client 已带会话 Cookie) ---
    with TestClient(client.app, raise_server_exceptions=False) as anon:
        resp = anon.get("/api/projects")
    assert_error_envelope(resp, 401, code="AUTH-REQ-001", message_key="ies.diag.auth.required")

    # --- 9) 业务错误 A: 任务不存在 404(RES-MISS-003) ---
    resp = client.get(f"/api/projects/{pid}/tasks/999999", headers=_bearer(owner_tok))
    err = assert_error_envelope(resp, 404, code="RES-MISS-003")
    assert err["location"] == {"object_type": "task", "object_id": 999999}

    # --- 10) 业务错误 B: 数据集不存在 404(RES-MISS-003, 归属校验) ---
    resp = client.get(f"/api/projects/{pid}/datasets/999999", headers=_bearer(owner_tok))
    assert_error_envelope(resp, 404, code="RES-MISS-003")

    # --- 11) 业务错误 C: 管理员解锁缺确认 409(ADMIN-CONFIRM-REQUIRED) ---
    # 任务须在无设备的空项目上提交: 含 PV 无汇的项目会被装配检查
    # ASM-CHECK-FAILED(ASM-SOLV-002 no_sink)拦截, 无法进入队列
    pid2 = _create_project(client, owner_tok, "解锁任务项目")
    resp = client.post(f"/api/projects/{pid2}/tasks", json={"task_type": "optimization"}, headers=_bearer(owner_tok))
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["task"]["id"]
    resp = client.post(
        "/api/admin/unlock-task",
        json={"task_id": task_id, "confirm": False},
        headers=_bearer(admin_tok),
    )
    err = assert_error_envelope(resp, 409, code="ADMIN-CONFIRM-REQUIRED")
    assert err["message_key"] == "ies.diag.admin.confirm_required"

    # --- 12) 业务错误 D: 登录失败 401(AUTH-LOGIN-001, 统一文案) ---
    resp = client.post("/api/auth/login", json={"username": "proto_owner", "password": "wrong-pass"})
    assert_error_envelope(resp, 401, code="AUTH-LOGIN-001", message_key="ies.diag.auth.login_failed")


# ---------------------------------------------------------------------------
# 2. 成功响应包装键锁定(命名键现状; 实际与任务书期望的差异以注释记录)
# ---------------------------------------------------------------------------


def _assert_wrapper(resp, expected: set[str]) -> dict:
    """断言 2xx 响应顶层键集与期望精确一致。"""
    assert resp.status_code < 300, f"{resp.status_code}: {resp.text}"
    body = resp.json()
    assert set(body) == expected, f"包装键漂移: 期望 {sorted(expected)}, 实际 {sorted(body)}"
    return body


def test_success_wrappers_projects_domain(client: TestClient, db: Session) -> None:
    """projects 域: 创建/列表/详情包装键锁定。

    差异记录: 任务书期望 GET /api/projects/{id} → {"project", "my_role"};
    实际为 {"project", "draft", "versions", "my_role"}(项目视图含草稿摘要与
    版本列表, services/project.get_project_view)。以实际为准锁定。
    """
    user = make_user(db, "wrap_proj")
    tok = _login(client, "wrap_proj")
    resp = client.post(
        "/api/projects",
        json={
            "name": "包装项目",
            "baseline_resolution": "1h",
            "baseline_leap_year": False,
            "baseline_scenario_mode": "single",
        },
        headers=_bearer(tok),
    )
    body = _assert_wrapper(resp, {"project", "my_role"})
    pid = body["project"]["id"]
    _assert_wrapper(client.get("/api/projects", headers=_bearer(tok)), {"projects"})
    _assert_wrapper(client.get(f"/api/projects/{pid}", headers=_bearer(tok)), {"project", "draft", "versions", "my_role"})
    _assert_wrapper(client.get(f"/api/projects/{pid}/versions", headers=_bearer(tok)), {"versions"})


def test_success_wrappers_tasks_results_domain(client: TestClient, db: Session) -> None:
    """tasks/results 域: 提交/列表/详情 + 结果视图/评估列表包装键锁定。"""
    user = make_user(db, "wrap_task")
    tok = _login(client, "wrap_task")
    pid = _create_project(client, tok, "任务包装项目")
    resp = client.post(f"/api/projects/{pid}/tasks", json={"task_type": "calc"}, headers=_bearer(tok))
    body = _assert_wrapper(resp, {"task", "replayed", "duplicate", "hint"})
    task_id = body["task"]["id"]
    _assert_wrapper(client.get(f"/api/projects/{pid}/tasks", headers=_bearer(tok)), {"items", "next_cursor"})
    _assert_wrapper(client.get(f"/api/projects/{pid}/tasks/{task_id}", headers=_bearer(tok)), {"task"})
    _assert_wrapper(client.get(f"/api/projects/{pid}/tasks/{task_id}/result", headers=_bearer(tok)), {"result"})
    _assert_wrapper(
        client.get(f"/api/projects/{pid}/tasks/{task_id}/result/assessments", headers=_bearer(tok)),
        {"items", "total"},
    )


def test_success_wrappers_config_validation_model_domain(client: TestClient, db: Session) -> None:
    """config/validation/model 域包装键锁定。

    差异记录: 任务书期望 config GET → {"config", "meta", "version", "status",
    "diagnostics"}; 实际 GET 为 {"config", "meta", "version", "status",
    "updated_at"}(diagnostics 只在 PUT 保存成功响应中返回)。以实际为准锁定。
    """
    user = make_user(db, "wrap_cfg")
    tok = _login(client, "wrap_cfg")
    pid = _create_project(client, tok, "配置包装项目")
    _add_device(client, tok, pid)

    _assert_wrapper(
        client.get(f"/api/projects/{pid}/config", headers=_bearer(tok)),
        {"config", "meta", "version", "status", "updated_at"},
    )
    cfg = _default_config(client, tok, pid)
    resp = client.put(
        f"/api/projects/{pid}/config",
        json={"config": cfg, "expected_revision": 1},
        headers=_bearer(tok),
    )
    _assert_wrapper(resp, {"config", "meta", "version", "status", "diagnostics"})
    _assert_wrapper(
        client.post(f"/api/projects/{pid}/config/validate", json={"config": cfg}, headers=_bearer(tok)),
        {"diagnostics", "count"},
    )
    _assert_wrapper(
        client.get(f"/api/projects/{pid}/config/default", headers=_bearer(tok)),
        {"config", "meta"},
    )
    _assert_wrapper(client.post(f"/api/projects/{pid}/validation/run", headers=_bearer(tok)), {"report", "stored"})
    _assert_wrapper(client.get("/api/registry/device-types"), {"items"})
    _assert_wrapper(client.get("/api/registry/algorithms"), {"algorithms"})
    _assert_wrapper(client.get(f"/api/projects/{pid}/model", headers=_bearer(tok)), {"has_graph", "graph_id", "name", "graph_hash", "devices", "ports", "connections", "layout"})
    _assert_wrapper(client.get(f"/api/projects/{pid}/model/validate", headers=_bearer(tok)), {"diagnostics"})


def test_success_wrappers_datasets_auth_admin_exports_domain(client: TestClient, db: Session) -> None:
    """datasets/auth/admin/objects/exports 域包装键锁定。

    差异记录:
    - auth 登录实际返回 {"token", "token_type", "user", "needs_takeover_confirm"}
      (AuthResponse 模型), 非任务书假设的 {"ok", ...}; 登出为 {"ok"}。
    - objects 域两版形状并存: /api/admin/storage 为 7 键单一 StorageStatusDto;
      /api/admin/objects/pending 等列表端点为 {"data", "meta"}。两者分别锁定。
    """
    user = make_user(db, "wrap_ds")
    admin = make_user(db, "wrap_adm", role="admin")
    tok = _login(client, "wrap_ds")
    adm_tok = _login(client, "wrap_adm")
    pid = _create_project(client, tok, "数据集包装项目")

    # datasets
    resp = client.post(f"/api/projects/{pid}/datasets", json={"name": "D1"}, headers=_bearer(tok))
    _assert_wrapper(resp, {"dataset"})
    _assert_wrapper(client.get(f"/api/projects/{pid}/datasets", headers=_bearer(tok)), {"datasets"})

    # exports(在下方重复登录前执行: 重复登录会把旧窗口凭证降级为
    # takeover_pending, 尚未确认接管前无法访问业务端点)
    _assert_wrapper(
        client.post(f"/api/projects/{pid}/exports/package", headers=_bearer(tok)),
        {"token", "expires_at", "file_name", "manifest", "media_type", "object_id", "oid", "sha256", "size_bytes"},
    )

    # auth(登录响应是 AuthResponse 模型; 登出为 {"ok"})
    resp = client.post("/api/auth/login", json={"username": "wrap_ds", "password": PASSWORD})
    _assert_wrapper(resp, {"token", "token_type", "user", "needs_takeover_confirm"})
    resp = client.post("/api/auth/logout", headers=_bearer(adm_tok))
    _assert_wrapper(resp, {"ok"})

    # admin / objects(重新登录被登出的管理员)
    adm_tok = _login(client, "wrap_adm")
    _assert_wrapper(client.get("/api/auth/users", headers=_bearer(adm_tok)), {"users"})
    _assert_wrapper(
        client.get("/api/admin/storage", headers=_bearer(adm_tok)),
        {"objects", "refs", "capacity", "corrupt_count", "cleanup_candidates", "pending_deletion_count", "healthy"},
    )
    _assert_wrapper(client.get("/api/admin/objects/pending", headers=_bearer(adm_tok)), {"data", "meta"})
    _assert_wrapper(client.get("/api/admin/audit", headers=_bearer(adm_tok)), {"items", "next_cursor"})


# ---------------------------------------------------------------------------
# 3. 裸返回 vs 包装返回双重形状门禁(静态 AST 扫描)
# ---------------------------------------------------------------------------

API_DIR = Path(__file__).resolve().parent.parent / "iesplan" / "api"


def _route_handler_functions() -> Iterator[tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """遍历 backend/iesplan/api/ 中带路由装饰器的端点函数。"""
    for path in sorted(API_DIR.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                if (
                    isinstance(deco, ast.Call)
                    and isinstance(deco.func, ast.Attribute)
                    and isinstance(deco.func.value, ast.Name)
                    and deco.func.value.id in ("router", "config_router", "registry_router", "model_router")
                ):
                    yield path, node
                    break


def _dict_literal_keys(node: ast.AST) -> set[str] | None:
    """dict 字面量顶层字符串键集合; 非 dict 字面量返回 None。"""
    if isinstance(node, ast.Dict):
        keys: set[str] = set()
        for k in node.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
        return keys
    return None


def _response_shape_keys(return_node: ast.Return) -> set[str] | None:
    """return 语句对应的 JSON 响应形状键集(dict 字面量 / JSONResponse(content={...})。

    间接返回(return 服务调用结果/变量)无法静态判定 → None(视为未约束)。
    """
    value = return_node.value
    if value is None:
        return None
    keys = _dict_literal_keys(value)
    if keys is not None:
        return keys
    # JSONResponse(content={...}) / Response 的 dict 字面量内容
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in (
        "JSONResponse",
        "Response",
    ):
        for kw in value.keywords:
            if kw.arg == "content":
                return _dict_literal_keys(kw.value)
        return None
    return None


def _collect_shapes(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[set[str]]:
    """函数内全部可静态判定的响应形状键集(去重)。"""
    shapes: list[set[str]] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Return):
            keys = _response_shape_keys(node)
            if keys is not None and keys not in shapes:
                shapes.append(keys)
    return shapes


def test_no_bare_vs_wrapped_duplication() -> None:
    """门禁: 同一端点函数的所有响应形状键集必须相互可比较(子集/超集)。

    判定规则: 两个形状键集 A、B 违规 ⇔ 各自含有对方没有的键且互不为子集
    (即差集 A−B 与 B−A 均非空)。「互为子集」视为同一形状家族(分支仅附带/缺少
    可选项, 如 unlock-task 的 queued 分支多一个 message 键), 不构成
    「有时裸返回、有时包装返回」的两版响应。

    实现选型说明(为何 AST 而非运行时):
    - AST 覆盖 api 包内全部端点函数(含未被本套件调用的), 运行时只能覆盖
      实际调用的路径 —— 门禁要防的是「未来新增分支引入第二形状」, 静态扫描
      在 CI 中对整个包生效;
    - 对本套件实测过的端点, 精确键集断言(第 2 节)已锁形状, 两者互补。

    局限: 间接返回(return 服务调用结果)无法静态判定, 跳过(形状由服务层
    返回, 键集断言覆盖实测端点)。
    """
    violations: list[tuple[str, int, list[set[str]]]] = []
    scanned = 0
    for path, func in _route_handler_functions():
        shapes = _collect_shapes(func)
        if not shapes:
            continue
        scanned += 1
        for i, a in enumerate(shapes):
            for b in shapes[i + 1 :]:
                if a - b and b - a:  # 互不为子集 → 两版响应
                    violations.append((f"{path.name}:{func.name}", func.lineno, shapes))
                    break
            else:
                continue
            break
    assert scanned > 0, "AST 扫描未找到任何路由函数(api 包结构变化?)"
    assert not violations, f"裸/包装双重形状违规: {violations}"


# ---------------------------------------------------------------------------
# 4. 前端适配契约一致性抽查(client.ts asItems/oneOf 适配假设 ↔ 实际响应)
# ---------------------------------------------------------------------------


def test_frontend_contract_adapters_consistent(client: TestClient, db: Session) -> None:
    """抽查 2-3 组端点的实际响应与 frontend/src/api/client.ts 适配规则一致。

    client.ts 适配规则: asItems(body, key) 优先读 rec.items、其次 rec[key];
    oneOf(body, key) 读 rec[key]; asList(body, key) 同理。以下断言保证
    后端形状与适配假设不漂移。
    """
    user = make_user(db, "wrap_fe")
    tok = _login(client, "wrap_fe")
    pid = _create_project(client, tok, "前端契约项目")

    # (1) 任务列表 {"items", "next_cursor"} ↔ asItems(body, 'tasks')(client.ts:1483)
    resp = client.post(f"/api/projects/{pid}/tasks", json={"task_type": "calc"}, headers=_bearer(tok))
    assert resp.status_code == 201, resp.text
    body = client.get(f"/api/projects/{pid}/tasks", headers=_bearer(tok)).json()
    assert set(body) == {"items", "next_cursor"}, "asItems(body,'tasks') 依赖 rec.items"
    assert isinstance(body["items"], list)

    # (2) 项目视图 {"project","draft","versions","my_role"} ↔ projectFromServer
    #     (client.ts:481 读 rec.project / rec.my_role / rec.draft)
    view = client.get(f"/api/projects/{pid}", headers=_bearer(tok)).json()
    assert "project" in view and "my_role" in view and "draft" in view
    assert isinstance(view["draft"], dict) and "revision" in view["draft"]
    assert isinstance(view["my_role"], str)

    # (3) 配置 GET {"config","meta","version","status","updated_at"} ↔
    #     configFromServer(client.ts:619 读 env.config / env.updated_at / env.status)
    cfg = client.get(f"/api/projects/{pid}/config", headers=_bearer(tok)).json()
    assert set(cfg) == {"config", "meta", "version", "status", "updated_at"}
    assert "updated_at" in cfg  # 前端取 env.updated_at 而非 diagnostics

    # (4) 设备类型注册表 {"items"} ↔ asList(body, 'items')(client.ts:1340)
    reg = client.get("/api/registry/device-types").json()
    assert set(reg) == {"items"} and isinstance(reg["items"], list)


# ---------------------------------------------------------------------------
# 5. FastAPI/Pydantic 请求体校验 422 → 标准 8 字段信封(0.3.0 收口)
# ---------------------------------------------------------------------------


def test_pydantic_request_validation_422_uses_envelope(
    client: TestClient, db: Session
) -> None:
    """FastAPI/Pydantic 请求体校验失败走标准 8 字段信封。

    main.py 注册 RequestValidationError 处理器: code=API-REQ-001,
    message_key=ies.error.invalid_request, params.errors 数组按字段
    定位(loc/msg/type), 当前端点进 params.location(经 envelope
    location 字段, 与 Diagnostic 字段同构)。
    """
    user = make_user(db, "wrap_pyd")
    tok = _login(client, "wrap_pyd")
    pid = _create_project(client, tok, "pydantic 422 项目")

    # config PUT 请求体本身非法(config 应为 dict)→ 422 标准信封
    resp = client.put(
        f"/api/projects/{pid}/config",
        json={"config": "not-a-dict"},
        headers=_bearer(tok),
    )
    assert resp.status_code == 422
    assert set(resp.json()) == {"error"}, f"期望标准信封, 实际 {sorted(resp.json())}"
    err = resp.json()["error"]
    assert err["code"] == "API-REQ-001"
    assert err["message_key"] == "ies.error.invalid_request"
    assert err["blocking"] is True
    assert err["severity"] == "error"
    assert err["params"]["count"] >= 1
    assert isinstance(err["params"]["errors"], list)
    assert any("config" in e["loc"] for e in err["params"]["errors"])
    assert err["location"]["path"].endswith("/config")
    assert err["location"]["method"] == "PUT"

    # 任务提交缺必填字段 → 422 同样走信封
    resp = client.post(f"/api/projects/{pid}/tasks", json={}, headers=_bearer(tok))
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "API-REQ-001"
    assert err["message_key"] == "ies.error.invalid_request"
    assert err["params"]["count"] >= 1


# ---------------------------------------------------------------------------
# 6. 命名键集中登记表门禁(对应错误信封 NEW_DIAG_CODES 的管理模式)
# ---------------------------------------------------------------------------

#: 非 JSON 响应端点(302 重定向 / 二进制下载), 不要求登记包装键
_JSON_EXEMPT_HANDLERS = frozenset({
    "oidc_login",          # 302 RedirectResponse → /login?error=...
    "oidc_callback",       # 302 RedirectResponse → /
    "download_template",   # CSV 模板二进制下载
    "download_excel_endpoint",   # excel 二进制下载
    "download_package_endpoint", # 项目包二进制下载
})


def test_wrapper_keys_registered() -> None:
    """门禁: 全部 JSON 路由端点的顶层键集必须命中 WRAPPER_KEYS 登记表。

    对应错误信封的 NEW_DIAG_CODES 强制登记制度 —— 新增端点必须先登记
    (iesplan/api/wrapper_keys.py), 键集超出登记或未登记即失败。
    间接返回(return 服务调用结果)的端点要求登记(键集从服务层提取),
    防止服务层形状变化未被察觉。
    """
    from iesplan.api.wrapper_keys import WRAPPER_KEYS

    scanned = 0
    unregistered: list[str] = []
    overrunning: list[str] = []
    for path, func in _route_handler_functions():
        if func.name in _JSON_EXEMPT_HANDLERS:
            continue
        scanned += 1
        shapes = _collect_shapes(func)
        if not shapes:
            # 间接返回: 必须登记(登记值本身是契约, 服务层形状变化
            # 由运行时断言 test_success_wrapper_* 覆盖实测端点)
            if func.name not in WRAPPER_KEYS:
                unregistered.append(f"{path.name}:{func.name}(间接返回未登记)")
            continue
        actual = set().union(*shapes)
        allowed = WRAPPER_KEYS.get(func.name)
        if allowed is None:
            unregistered.append(f"{path.name}:{func.name} 实际={sorted(actual)}")
        elif not actual.issubset(allowed):
            overrunning.append(
                f"{path.name}:{func.name} 键集超出登记: {sorted(actual - allowed)}"
            )
    assert scanned > 0, "AST 扫描未找到任何路由函数(api 包结构变化?)"
    assert not unregistered, (
        f"命名键未登记(先登记 iesplan/api/wrapper_keys.py): {unregistered}"
    )
    assert not overrunning, (
        f"命名键超出登记(先改登记表再改代码): {overrunning}"
    )
