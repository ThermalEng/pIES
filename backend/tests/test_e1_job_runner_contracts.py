"""E1 contract tests for backend JobRunnerClient build_plugin & delete_plugin_image."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from iesplan.computation.runtime.job_runner_client import (
    DeletePluginImageResult,
    JobRunnerBadRequestError,
    JobRunnerClient,
    JobRunnerConflictError,
    JobRunnerUnavailableError,
)


def test_job_runner_client_build_plugin_pip_index_url_and_timeout() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.content.decode("utf-8")),
                "timeout": request.extensions.get("timeout"),
            }
        )
        return httpx.Response(
            200,
            json={
                "image": "pies-plugin/acme.algo:1.0.0",
                "plugin_id": "acme.algo",
                "version": "1.0.0",
                "built_at": "2026-11-27T00:00:00Z",
                "logs": ["Built ok"],
            },
        )

    client = JobRunnerClient(
        "http://job_runner:8010",
        build_timeout_seconds=600.0,
        client=httpx.Client(
            base_url="http://job_runner:8010",
            transport=httpx.MockTransport(handler),
        ),
    )

    res1 = client.build_plugin(
        "acme.algo",
        "1.0.0",
        "acme.algo/1.0.0",
        requirements_rel="requirements.txt",
        pip_index_url="https://pypi.internal/simple",
        timeout_seconds=180,
    )
    assert res1.image == "pies-plugin/acme.algo:1.0.0"
    assert seen[0]["method"] == "POST"
    assert seen[0]["path"] == "/v1/plugins/build"
    assert seen[0]["body"] == {
        "plugin_id": "acme.algo",
        "version": "1.0.0",
        "source_rel": "acme.algo/1.0.0",
        "requirements_rel": "requirements.txt",
        "pip_index_url": "https://pypi.internal/simple",
        "timeout_seconds": 180,
    }
    assert seen[0]["timeout"]["read"] == 180.0

    res2 = client.build_plugin("acme.algo", "1.0.0", "acme.algo/1.0.0")
    assert res2.logs == ("Built ok",)
    assert seen[1]["body"] == {
        "plugin_id": "acme.algo",
        "version": "1.0.0",
        "source_rel": "acme.algo/1.0.0",
    }
    assert seen[1]["timeout"]["read"] == 600.0


def test_job_runner_client_delete_plugin_image_contracts_and_errors() -> None:
    calls: list[dict[str, Any]] = []
    mode = {"case": "deleted"}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.content.decode("utf-8")),
            }
        )
        case = mode["case"]
        if case == "deleted":
            return httpx.Response(
                200,
                json={
                    "image": "pies-plugin/acme.algo:1.0.0",
                    "plugin_id": "acme.algo",
                    "version": "1.0.0",
                    "deleted": True,
                    "status": "deleted",
                },
            )
        if case == "not_found":
            return httpx.Response(
                200,
                json={
                    "image": "pies-plugin/acme.algo:1.0.0",
                    "plugin_id": "acme.algo",
                    "version": "1.0.0",
                    "deleted": False,
                    "status": "not_found",
                },
            )
        if case == "bad":
            return httpx.Response(400, json={"detail": "refusing to delete unmanaged image"})
        if case == "conflict":
            return httpx.Response(409, json={"detail": "image in use"})
        return httpx.Response(503, json={"detail": "docker unavailable"})

    client = JobRunnerClient(
        "http://job_runner:8010",
        client=httpx.Client(
            base_url="http://job_runner:8010",
            transport=httpx.MockTransport(handler),
        ),
    )

    res_del = client.delete_plugin_image("acme.algo", "1.0.0", force=True)
    assert res_del == DeletePluginImageResult(
        image="pies-plugin/acme.algo:1.0.0",
        plugin_id="acme.algo",
        version="1.0.0",
        deleted=True,
        status="deleted",
    )
    assert calls[0] == {
        "method": "POST",
        "path": "/v1/plugins/images/delete",
        "body": {"plugin_id": "acme.algo", "version": "1.0.0", "force": True},
    }

    mode["case"] = "not_found"
    res_nf = client.delete_plugin_image("acme.algo", "1.0.0")
    assert res_nf.deleted is False
    assert res_nf.status == "not_found"
    assert calls[1]["body"]["force"] is False

    mode["case"] = "bad"
    with pytest.raises(JobRunnerBadRequestError, match="unmanaged"):
        client.delete_plugin_image("acme.algo", "1.0.0")

    mode["case"] = "conflict"
    with pytest.raises(JobRunnerConflictError, match="in use"):
        client.delete_plugin_image("acme.algo", "1.0.0")

    mode["case"] = "unavail"
    with pytest.raises(JobRunnerUnavailableError, match="unavailable"):
        client.delete_plugin_image("acme.algo", "1.0.0")
