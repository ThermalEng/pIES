"""冒烟测试: 应用可启动, 健康检查可用 (不依赖 DB 场景)。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from iesplan import __version__
from iesplan.main import create_app


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """独立应用实例的测试客户端 (带 lifespan)。"""
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def test_app_starts_and_healthz_200(client: TestClient) -> None:
    """应用可启动且存活探针 /api/healthz 返回 200。"""
    resp = client.get("/api/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "iesplan"
    assert body["version"] == __version__
    # 不依赖 DB: 本测试不触碰 /api/readyz
    assert "time" in body


def test_module_app_instance_usable() -> None:
    """模块级 app 实例 (uvicorn 入口 iesplan.main:app) 可直接用 TestClient 驱动。"""
    from iesplan.main import app as module_app

    with TestClient(module_app, raise_server_exceptions=False) as test_client:
        resp = test_client.get("/api/healthz")
    assert resp.status_code == 200
