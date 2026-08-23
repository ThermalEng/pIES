"""应用入口测试: 健康检查、根路由、404 与全局异常处理。"""

import json
from collections.abc import Iterator

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from iesplan import __version__
from iesplan.main import _app_error_response, create_app


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """独立应用实例的测试客户端 (带 lifespan)。

    raise_server_exceptions=False: 未捕获异常由应用内处理器生成 500 响应,
    不在客户端侧重抛 (ServerErrorMiddleware 处理后会重抛原异常)。
    """
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------


def test_healthz_returns_200(client: TestClient) -> None:
    """存活探针应返回 200 与 status=ok。"""
    resp = client.get("/api/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "iesplan"
    assert body["version"] == __version__


def test_readyz_shape(client: TestClient) -> None:
    """就绪探针: 数据库可用返回 200, 不可用返回 503, 响应体符合约定。"""
    resp = client.get("/api/readyz")
    assert resp.status_code in (200, 503)
    body = resp.json()
    if resp.status_code == 200:
        assert body["status"] == "ok"
    else:
        assert "error" in body
        assert body["error"]["code"]


def test_readyz_503_when_db_unavailable(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """数据库不可用时就绪探针应返回 503 及标准错误体。"""
    monkeypatch.setattr("iesplan.main._db_available", lambda: False)
    resp = client.get("/api/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "API-RZ-001"
    assert body["error"]["message_key"] == "ies.error.db_unavailable"


def test_readyz_200_when_db_available(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """数据库与建模命令注册表均可用时就绪探针应返回 200。"""
    monkeypatch.setattr("iesplan.main._db_available", lambda: True)
    monkeypatch.setattr("iesplan.main._registry_status", "ok")
    resp = client.get("/api/readyz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_readyz_503_when_registry_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """建模命令注册表初始化失败时就绪探针应返回 503(High-6 修复)。

    A3 脱敏: 响应不得泄露原始异常串(内部路径/细节只进日志)。
    """
    monkeypatch.setattr("iesplan.main._db_available", lambda: True)
    monkeypatch.setattr("iesplan.main._registry_status", "error: boom")
    resp = client.get("/api/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "API-RZ-002"
    assert body["error"]["message_key"] == "ies.error.registry_unavailable"
    # 脱敏断言: 响应不含原始异常详情
    assert body["error"]["params"]["detail"] == "unavailable"
    assert "boom" not in resp.text
    assert "error: boom" not in resp.text


def test_cors_allows_local_origin_with_credentials(client: TestClient) -> None:
    """本地开发来源应获得带凭据的 CORS 响应头。"""
    resp = client.get("/api/healthz", headers={"Origin": "http://localhost:3000"})
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert resp.headers.get("access-control-allow-credentials") == "true"


# ---------------------------------------------------------------------------
# 根路由与 404
# ---------------------------------------------------------------------------


def test_api_root_returns_meta(client: TestClient) -> None:
    """GET /api 应返回服务名称、版本与文档地址。"""
    resp = client.get("/api")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "iesplan"
    assert body["version"] == __version__
    assert body["docs"] == "/docs"


def test_unknown_route_returns_404_json(client: TestClient) -> None:
    """未知路由应返回标准错误 JSON 结构 {error: {code, message_key, ...}}。"""
    for path in ("/api/does-not-exist", "/nope"):
        resp = client.get(path)
        assert resp.status_code == 404
        body = resp.json()
        assert "error" in body
        err = body["error"]
        assert isinstance(err["code"], str) and err["code"]
        assert isinstance(err["message_key"], str) and err["message_key"]
        assert "params" in err


def test_http_exception_mapping(client: TestClient) -> None:
    """路由内显式抛出的 HTTPException 应映射为标准错误体。"""
    application = client.app

    @application.get("/api/_test/http-418")
    def _teapot() -> None:
        raise HTTPException(status_code=418, detail="teapot")

    resp = client.get("/api/_test/http-418")
    assert resp.status_code == 418
    body = resp.json()
    assert body["error"]["code"] == "HTTP-418"
    assert body["error"]["params"]["detail"] == "teapot"


# ---------------------------------------------------------------------------
# AppError 映射
# ---------------------------------------------------------------------------


class _FakeAppError(Exception):
    """契约形状的 AppError 替身 (core/errors 尚未就绪时使用)。"""

    code = "DATA-TS-001"
    severity = "blocking"
    blocking = True
    message_key = "ies.diag.data.ts_dup"
    params = {"row": 3}
    location = {"object_type": "timeseries", "field": "t0"}


def test_app_error_response_envelope() -> None:
    """AppError 应映射为 400 + 诊断 JSON (属性驱动, 与具体实现解耦)。"""
    resp = _app_error_response(_FakeAppError("boom"))
    assert resp.status_code == 400
    err = json.loads(resp.body)["error"]
    assert err["code"] == "DATA-TS-001"
    assert err["message_key"] == "ies.diag.data.ts_dup"
    assert err["severity"] == "blocking"
    assert err["blocking"] is True
    assert err["params"] == {"row": 3}
    assert err["location"] == {"object_type": "timeseries", "field": "t0"}
    # 信封字段集与契约固定: 8 字段同构(0.3.0 C1)
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
    assert json.loads(resp.body).keys() == {"error"}


def test_app_error_to_dict_envelope_isomorphic() -> None:
    """AppError.to_dict 与 _error_envelope 输出同一形状(权威源 core.errors)。"""
    from iesplan.core.errors import AppError, error_envelope

    class _Err(AppError):
        code = "DATA-TS-001"
        severity = "blocking"
        message_key = "ies.diag.data.ts_dup"
        http_status = 400

    exc = _Err(
        "boom",
        params={"row": 3},
        location={"object_type": "timeseries", "field": "t0"},
        fix_hint_key="ies.fix.data.ts_dup",
        ref_ids=["r1"],
    )
    assert exc.to_dict() == error_envelope(
        code="DATA-TS-001",
        message_key="ies.diag.data.ts_dup",
        severity="blocking",
        blocking=True,
        params={"row": 3},
        location={"object_type": "timeseries", "field": "t0"},
        fix_hint_key="ies.fix.data.ts_dup",
        ref_ids=["r1"],
    )
    err = exc.to_dict()["error"]
    assert err["fix_hint_key"] == "ies.fix.data.ts_dup"
    assert err["ref_ids"] == ["r1"]
    # 与 Diagnostic.to_dict 字段同构(信封 8 字段 ⊆ 诊断 14 字段)
    from iesplan.core.diagnostics import DATA_TS_DUP, make_diag

    diag_fields = set(make_diag(DATA_TS_DUP).to_dict())
    assert set(err) <= diag_fields
    json.dumps(exc.to_dict(), ensure_ascii=False)


def test_app_error_mapping_integration() -> None:
    """core/errors 就绪后, 路由抛出的 AppError 应按 http_status 映射为 400。"""
    errors = pytest.importorskip("iesplan.core.errors")

    class _BoomError(errors.AppError):
        http_status = 400

    application = create_app()
    boom = _BoomError("boom")

    @application.get("/api/_test/app-error")
    def _boom() -> None:
        raise boom

    with TestClient(application, raise_server_exceptions=False) as test_client:
        resp = test_client.get("/api/_test/app-error")
    assert resp.status_code == 400
    err = resp.json()["error"]
    # 异常自身的字段原样透出
    assert err["code"] == boom.code
    assert err["message_key"] == boom.message_key
    assert err["severity"] == boom.severity


def test_app_error_subclass_status_integration() -> None:
    """ForbiddenError 应映射为 403, 且透出契约字段 (core/errors 就绪后生效)。"""
    errors = pytest.importorskip("iesplan.core.errors")
    forbidden = getattr(errors, "ForbiddenError", None)
    if forbidden is None:
        pytest.skip("ForbiddenError 尚未实现")

    application = create_app()

    @application.get("/api/_test/forbidden")
    def _boom() -> None:
        raise forbidden("forbidden")

    with TestClient(application, raise_server_exceptions=False) as test_client:
        resp = test_client.get("/api/_test/forbidden")
    assert resp.status_code == 403
    err = resp.json()["error"]
    assert err["code"] == "PERM-DENIED-001"
    assert err["message_key"] == "ies.diag.perm.denied"


# ---------------------------------------------------------------------------
# 未捕获异常与模块级入口
# ---------------------------------------------------------------------------


def test_uncaught_exception_returns_500_without_stack(client: TestClient) -> None:
    """未捕获异常应返回 500 通用错误体, 响应不得泄露堆栈。"""
    application = client.app

    @application.get("/api/_test/uncaught")
    def _boom() -> None:
        raise RuntimeError("boom-secret-detail")

    resp = client.get("/api/_test/uncaught")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "API-500-001"
    assert body["error"]["message_key"] == "ies.error.internal"
    # 堆栈只进日志, 响应不泄露
    assert "boom-secret-detail" not in resp.text
    assert "Traceback" not in resp.text


def test_module_level_app_created() -> None:
    """模块级 app 实例存在 (uvicorn 入口 iesplan.main:app)。"""
    from iesplan.main import app as module_app

    assert module_app.title == "pIES API"
    assert module_app.version == __version__
