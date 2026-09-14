"""公开请求依赖与 Worker 显式注入契约测试(只覆盖公开行为)。

- ``iesplan.api.deps.get_session_factory``: 有装配上下文时返回其
  ``session_factory``; 无上下文或工厂缺失时显式失败(无静默全局回退);
- ``iesplan.api.deps.get_request_db``: 从装配工厂取请求级会话, 用后关闭;
  HTTP 层无上下文时显式失败, 不触碰全局 ``SessionLocal``;
- ``iesplan.worker.main.Worker``: 构造必须显式传入 ``session_factory``,
  缺失立即失败, 不回退任何默认工厂。
"""

from __future__ import annotations

import os

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402
from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from iesplan.api import deps as api_deps  # noqa: E402
from iesplan.worker.main import Worker  # noqa: E402


def _request(app: object) -> SimpleNamespace:
    """构造仅携带 app 状态的最小请求桩(公开依赖只读 request.app.state)。"""

    class _State:
        pass

    state = _State()
    for key, value in vars(app).items():
        setattr(state, key, value)
    return SimpleNamespace(app=SimpleNamespace(state=state))


# ---------------------------------------------------------------------------
# get_session_factory
# ---------------------------------------------------------------------------


def test_session_factory_comes_from_bootstrap_context() -> None:
    """有装配上下文时返回其 session_factory(组合根装配的依赖)。"""
    sentinel = object()
    req = _request(SimpleNamespace(bootstrap_context=SimpleNamespace(session_factory=sentinel)))
    assert api_deps.get_session_factory(req) is sentinel  # type: ignore[arg-type]


def test_session_factory_missing_context_fails_explicitly() -> None:
    """无 bootstrap_context 时显式失败, 不回退全局工厂。"""
    req = _request(SimpleNamespace())
    with pytest.raises(RuntimeError, match="bootstrap_context"):
        api_deps.get_session_factory(req)  # type: ignore[arg-type]


def test_session_factory_missing_factory_fails_explicitly() -> None:
    """上下文存在但无 session_factory 时同样显式失败。"""
    req = _request(SimpleNamespace(bootstrap_context=SimpleNamespace()))
    with pytest.raises(RuntimeError, match="session_factory"):
        api_deps.get_session_factory(req)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# get_request_db
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_request_db_uses_context_factory_and_closes() -> None:
    """请求会话来自装配工厂, 生成器关闭时会话关闭。"""
    session = _FakeSession()
    req = _request(
        SimpleNamespace(bootstrap_context=SimpleNamespace(session_factory=lambda: session))
    )
    gen = api_deps.get_request_db(req)  # type: ignore[arg-type]
    assert next(gen) is session
    gen.close()
    assert session.closed


def _probe_app() -> FastAPI:
    app = FastAPI()

    @app.get("/probe")
    def probe(db=Depends(api_deps.get_request_db)) -> dict[str, str]:  # noqa: ANN001
        return {"session": type(db).__name__}

    return app


def test_http_request_db_serves_with_context() -> None:
    """HTTP 层: 已装配时路由拿到装配工厂的会话。"""
    app = _probe_app()
    app.state.bootstrap_context = SimpleNamespace(session_factory=_FakeSession)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/probe")
    assert resp.status_code == 200
    assert resp.json() == {"session": "_FakeSession"}


def test_http_request_db_fails_without_context() -> None:
    """HTTP 层: 未装配时显式失败(500), 不静默使用全局会话。"""
    app = _probe_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/probe")
    assert resp.status_code == 500


def test_http_request_db_supports_override() -> None:
    """HTTP 层: 公开依赖可被 dependency_overrides 替换(测试隔离点)。"""
    app = _probe_app()

    def _override():
        yield _FakeSession()

    app.dependency_overrides[api_deps.get_request_db] = _override
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/probe")
    assert resp.status_code == 200
    assert resp.json() == {"session": "_FakeSession"}


# ---------------------------------------------------------------------------
# Worker 显式注入
# ---------------------------------------------------------------------------


def test_worker_requires_explicit_session_factory() -> None:
    """Worker 缺 session_factory 立即失败(无默认、无回退)。"""
    with pytest.raises(TypeError):
        Worker(worker_type="compute")  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="session_factory"):
        Worker(worker_type="io", session_factory=None)  # type: ignore[arg-type]


def test_worker_holds_explicit_session_factory() -> None:
    """显式传入的工厂被 Worker 持有(组合根装配来源)。"""
    sentinel = object()
    worker = Worker(worker_type="compute", session_factory=sentinel)
    assert worker.session_factory is sentinel
