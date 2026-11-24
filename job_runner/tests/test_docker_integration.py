"""Real Docker daemon integration tests for job_runner.

Requires ``PIES_RUN_DOCKER_TESTS=1`` and ``/var/run/docker.sock`` mounted inside the
test container, with ``PIES_TEST_SHARED_ROOT`` mapped to the same directory on the host.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import docker
import pytest
from fastapi.testclient import TestClient

from pies_job_runner.app import create_app
from pies_job_runner.config import JobRunnerSettings
from pies_job_runner.docker_ops import LABEL_MANAGED, DockerJobRunnerService
from tests.conftest import docker_integration_enabled

pytestmark = pytest.mark.skipif(
    not docker_integration_enabled(),
    reason="Real Docker daemon integration tests run inside Docker with PIES_RUN_DOCKER_TESTS=1",
)


def _wait_until_exited(client: TestClient, stage_execution_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/stages/{stage_execution_id}")
        assert resp.status_code == 200
        body = resp.json()
        if not body["running"] and body["status"] in ("exited", "dead"):
            return body
        time.sleep(0.15)
    raise AssertionError(f"stage {stage_execution_id} did not exit within {timeout}s")


@pytest.fixture
def docker_harness() -> Iterator[tuple[TestClient, JobRunnerSettings, docker.DockerClient, str]]:
    run_id = uuid.uuid4().hex[:8]
    container_shared_root = Path(os.environ.get("PIES_TEST_CONTAINER_ROOT", "/data"))
    host_shared_root = Path(os.environ.get("PIES_TEST_HOST_ROOT", str(container_shared_root)))

    run_dir = container_shared_root / f"jr-test-{run_id}"
    host_run_dir = host_shared_root / f"jr-test-{run_id}"
    stage_root = run_dir / "stages"
    host_stage_root = host_run_dir / "stages"
    plugin_root = run_dir / "plugin_builds"
    stage_root.mkdir(parents=True, exist_ok=True)
    plugin_root.mkdir(parents=True, exist_ok=True)

    base_tag = f"pies-plugin-base-test:{run_id}"
    settings = JobRunnerSettings(
        stage_root=stage_root,
        host_stage_root=host_stage_root,
        plugin_source_root=plugin_root,
        plugin_base_image=base_tag,
        plugin_image_prefix=f"pies-plugin-test-{run_id}",
        stop_grace_seconds=1,
    )
    docker_client = docker.from_env()
    service = DockerJobRunnerService(settings, client=docker_client)
    app = create_app(settings=settings, service=service)
    client = TestClient(app)

    created_images: list[str] = []
    try:
        # Build a minimal ephemeral base image atop python:3.12-slim representing pies-plugin-base
        tar_buf = io.BytesIO()
        df = (
            b"FROM python:3.12-slim\n"
            b"COPY pies_sdk /usr/local/lib/python3.12/site-packages/pies_sdk\n"
        )
        runner_py = (
            b"import pathlib\n"
            b"import sys\n"
            b"sys.path.insert(0, '/plugin')\n"
            b"import main\n"
            b"main.solve(pathlib.Path('/work/in'), pathlib.Path('/work/out'))\n"
        )
        with tarfile.open(fileobj=tar_buf, mode="w") as tar:
            for name, content in (
                ("Dockerfile", df),
                ("pies_sdk/__init__.py", b""),
                ("pies_sdk/runner.py", runner_py),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(content))
        tar_buf.seek(0)
        docker_client.images.build(fileobj=tar_buf, custom_context=True, tag=base_tag, rm=True, pull=False)
        created_images.append(base_tag)

        yield client, settings, docker_client, run_id
    finally:
        # Cleanup all containers created during this test run
        try:
            for c in docker_client.containers.list(all=True, filters={"label": [f"{LABEL_MANAGED}=true"]}):
                labels = (c.attrs.get("Config") or {}).get("Labels") or {}
                if run_id in str(labels.get("pies.attempt_id", "")) or run_id in str(
                    labels.get("pies.stage_execution_id", "")
                ):
                    c.remove(force=True)
        except Exception:
            pass
        # Cleanup test plugin images + test base image
        for img in docker_client.images.list():
            for tag in img.tags:
                if tag.startswith(f"pies-plugin-test-{run_id}/") or tag == base_tag:
                    try:
                        docker_client.images.remove(tag, force=True)
                    except Exception:
                        pass
        for tag in created_images:
            try:
                docker_client.images.remove(tag, force=True)
            except Exception:
                pass
        shutil.rmtree(run_dir, ignore_errors=True)
        docker_client.close()


def test_real_docker_build_plugin_and_run_isolated_stage(docker_harness) -> None:
    client, settings, docker_client, run_id = docker_harness

    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200

    # 1. Stage a plugin with a decoy Dockerfile that must be ignored
    src_dir = settings.plugin_source_root / "acme.algo" / "1.0.0"
    src_dir.mkdir(parents=True)
    (src_dir / "plugin.yaml").write_text(
        "schema: ies.algorithm-plugin\nschema_version: '1.0.0'\n",
        encoding="utf-8",
    )
    (src_dir / "Dockerfile").write_text("FROM nonexistent-invalid-base:999\n", encoding="utf-8")
    (src_dir / "main.py").write_text(
        "import json, os, pathlib, socket\n"
        "def solve(in_dir, out_dir):\n"
        "    req = json.loads((in_dir / 'request.json').read_text())\n"
        "    # Verify network=none\n"
        "    net_blocked = False\n"
        "    try:\n"
        "        socket.create_connection(('1.1.1.1', 80), timeout=0.5)\n"
        "    except OSError:\n"
        "        net_blocked = True\n"
        "    # Verify read-only rootfs\n"
        "    ro_root = False\n"
        "    try:\n"
        "        pathlib.Path('/root-write-test').write_text('x')\n"
        "    except OSError:\n"
        "        ro_root = True\n"
        "    # Verify /work/in is read-only\n"
        "    ro_in = False\n"
        "    try:\n"
        "        (in_dir / 'mutate.txt').write_text('x')\n"
        "    except OSError:\n"
        "        ro_in = True\n"
        "    # Verify /tmp tmpfs is writable\n"
        "    pathlib.Path('/tmp/scratch.txt').write_text('ok')\n"
        "    # Write iterations.jsonl (15 entries)\n"
        "    with (out_dir / 'iterations.jsonl').open('w') as f:\n"
        "        for i in range(1, 16):\n"
        "            f.write(json.dumps({'iteration': i, 'value': 1.0 / i}) + '\\n')\n"
        "    (out_dir / 'result.json').write_text(json.dumps({\n"
        "        'uid': os.geteuid(),\n"
        "        'net_blocked': net_blocked,\n"
        "        'ro_root': ro_root,\n"
        "        'ro_in': ro_in,\n"
        "        'echo': req['value'],\n"
        "    }))\n",
        encoding="utf-8",
    )

    build_resp = client.post(
        "/v1/plugins/build",
        json={
            "plugin_id": "acme.algo",
            "version": "1.0.0",
            "source_rel": "acme.algo/1.0.0",
            "install_requirements": False,
        },
    )
    assert build_resp.status_code == 200, build_resp.text
    built_image = build_resp.json()["image"]

    # 2. Prepare /work/in and launch isolated container via start_stage
    exec_id = f"exec-{run_id}-normal"
    in_dir = settings.stage_root / exec_id / "in"
    in_dir.mkdir(parents=True)
    (in_dir / "request.json").write_text(json.dumps({"value": 42}), encoding="utf-8")

    start_resp = client.post(
        "/v1/stages/start",
        json={
            "attempt_id": f"att-{run_id}",
            "stage_id": "algorithm",
            "stage_execution_id": exec_id,
            "fencing_token": "fence-1",
            "image": built_image,
            "cpu_cores": 1.0,
            "memory_mb": 128,
            "pids_limit": 32,
        },
    )
    assert start_resp.status_code == 200, start_resp.text

    # Verify container security config directly from Docker Engine
    container = docker_client.containers.get(start_resp.json()["container_id"])
    hc = container.attrs["HostConfig"]
    assert hc["NetworkMode"] == "none"
    assert hc["ReadonlyRootfs"] is True
    assert hc["CapDrop"] == ["ALL"]
    assert hc["NanoCpus"] == 1_000_000_000
    assert hc["Memory"] == 128 * 1024 * 1024
    assert hc["PidsLimit"] == 32
    assert container.attrs["Config"]["User"] == "65534:65534"

    done = _wait_until_exited(client, exec_id)
    assert done["exit_code"] == 0, container.logs().decode("utf-8", errors="replace")
    assert done["termination_reason"] == "normal"
    assert done["resources"]["memory_limit_bytes"] == {"status": "limited", "value": 128 * 1024 * 1024}
    assert done["resources"]["cpu_limit_cores"] == {"status": "limited", "value": 1.0}
    assert done["resources"]["pids_limit"] == {"status": "limited", "value": 32}
    assert done["iterations"]["present"] is True
    assert done["iterations"]["count"] == 15
    assert len(done["iterations"]["tail"]) == 15
    assert set(done["output_files"]) == {"iterations.jsonl", "result.json"}

    result = json.loads((settings.stage_root / exec_id / "out" / "result.json").read_text(encoding="utf-8"))
    assert result == {
        "uid": 65534,
        "net_blocked": True,
        "ro_root": True,
        "ro_in": True,
        "echo": 42,
    }

    cleanup_resp = client.post(f"/v1/stages/{exec_id}/cleanup", json={})
    assert cleanup_resp.status_code == 200
    assert cleanup_resp.json()["container_removed"] is True
    assert cleanup_resp.json()["workspace_removed"] is True


def test_real_docker_idempotent_start_reconciliation_stop_and_cleanup(docker_harness) -> None:
    client, settings, docker_client, run_id = docker_harness

    exec_id = f"exec-{run_id}-stop"
    in_dir = settings.stage_root / exec_id / "in"
    in_dir.mkdir(parents=True)

    start_payload = {
        "attempt_id": f"att-{run_id}-stop",
        "stage_id": "solver",
        "stage_execution_id": exec_id,
        "fencing_token": "fence-stop",
        "image": "python:3.12-slim",
        "command": [
            "python",
            "-c",
            "import pathlib, time; "
            "pathlib.Path('/work/out/partial.txt').write_text('saved-artifact'); "
            "pathlib.Path('/work/out/iterations.jsonl').write_text('{\"iteration\":1}\\n'); "
            "time.sleep(60)",
        ],
        "memory_mb": 64,
    }
    first = client.post("/v1/stages/start", json=start_payload)
    assert first.status_code == 200, first.text
    assert first.json()["reused"] is False
    cid = first.json()["container_id"]

    # Wait briefly until partial.txt is written inside container
    partial_path = settings.stage_root / exec_id / "out" / "partial.txt"
    deadline = time.monotonic() + 10.0
    while not partial_path.is_file() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert partial_path.is_file()

    # Idempotent retry for same (attempt_id, stage_id) must NOT spawn duplicate container
    second = client.post("/v1/stages/start", json=start_payload)
    assert second.status_code == 200
    assert second.json()["reused"] is True
    assert second.json()["container_id"] == cid

    # Fresh service instance (simulating job_runner restart) discovers container via Docker labels
    fresh_app = create_app(settings=settings, service=DockerJobRunnerService(settings, client=docker_client))
    fresh_client = TestClient(fresh_app)
    listed = fresh_client.get(f"/v1/stages?attempt_id=att-{run_id}-stop&stage_id=solver")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["stage_execution_id"] == exec_id

    # Premature cleanup rejected while running
    assert fresh_client.post(f"/v1/stages/{exec_id}/cleanup", json={}).status_code == 409

    # Stop immediately terminates container WITHOUT deleting container or /work/out
    stopped = fresh_client.post(
        f"/v1/stages/{exec_id}/stop",
        json={"reason": "cancelled", "grace_seconds": 1},
    )
    assert stopped.status_code == 200
    stop_body = stopped.json()
    assert stop_body["running"] is False
    assert stop_body["termination_reason"] == "cancelled"
    assert partial_path.read_text(encoding="utf-8") == "saved-artifact"
    assert docker_client.containers.get(cid) is not None

    # Cleanup removes container and workspace only after backend persists outputs
    cleaned = fresh_client.post(f"/v1/stages/{exec_id}/cleanup", json={})
    assert cleaned.status_code == 200
    assert cleaned.json()["container_removed"] is True
    assert cleaned.json()["workspace_removed"] is True
    assert not (settings.stage_root / exec_id).exists()


def test_real_docker_timeout_stop_and_oom_detection(docker_harness) -> None:
    client, settings, _, run_id = docker_harness

    # 1. Timeout termination reason
    exec_to = f"exec-{run_id}-timeout"
    (settings.stage_root / exec_to / "in").mkdir(parents=True)
    r_to = client.post(
        "/v1/stages/start",
        json={
            "attempt_id": f"att-{run_id}-to",
            "stage_id": "solver",
            "stage_execution_id": exec_to,
            "fencing_token": "fence-to",
            "image": "python:3.12-slim",
            "command": ["python", "-c", "import time; time.sleep(60)"],
        },
    )
    assert r_to.status_code == 200
    stopped = client.post(f"/v1/stages/{exec_to}/stop", json={"reason": "timed_out", "grace_seconds": 0})
    assert stopped.status_code == 200
    assert stopped.json()["termination_reason"] == "timed_out"
    assert client.post(f"/v1/stages/{exec_to}/cleanup", json={}).status_code == 200

    # 2. OOM detection with hard memory limit (memswap_limit = mem_limit)
    exec_oom = f"exec-{run_id}-oom"
    (settings.stage_root / exec_oom / "in").mkdir(parents=True)
    r_oom = client.post(
        "/v1/stages/start",
        json={
            "attempt_id": f"att-{run_id}-oom",
            "stage_id": "solver",
            "stage_execution_id": exec_oom,
            "fencing_token": "fence-oom",
            "image": "python:3.12-slim",
            "command": ["python", "-c", "buf = bytearray(96 * 1024 * 1024); buf[:] = b'x' * len(buf)"],
            "memory_mb": 16,
        },
    )
    assert r_oom.status_code == 200, r_oom.text
    oom_done = _wait_until_exited(client, exec_oom, timeout=20.0)
    assert oom_done["resources"]["oom_killed"] == {"status": "known", "value": True}
    assert oom_done["termination_reason"] == "oom"
    assert client.post(f"/v1/stages/{exec_oom}/cleanup", json={}).status_code == 200
