"""Unit tests for job_runner HTTP contract, validation, cgroup parsing, and proxy logic."""

from __future__ import annotations

import io
import json
import tarfile
import threading
import time
from pathlib import Path
from typing import Any

from docker.errors import APIError, DockerException, ImageNotFound
from fastapi.testclient import TestClient

from pies_job_runner.app import create_app
from pies_job_runner.config import JobRunnerSettings
from pies_job_runner.docker_ops import (
    LABEL_ATTEMPT_ID,
    LABEL_FENCING_TOKEN,
    LABEL_MANAGED,
    LABEL_PLUGIN_ID,
    LABEL_PLUGIN_VERSION,
    LABEL_STAGE_EXECUTION_ID,
    LABEL_STAGE_ID,
    LABEL_WORKSPACE_REL,
    DockerJobRunnerService,
    render_plugin_dockerfile,
)
from pies_job_runner.metrics import read_iterations_summary


class _FakeContainer:
    def __init__(
        self,
        *,
        container_id: str,
        name: str,
        image: str,
        labels: dict[str, str],
        host_config: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        stats_payload: dict[str, Any] | None = None,
    ) -> None:
        self.id = container_id
        self.name = name
        self.labels = dict(labels)
        self.status = (state or {}).get("Status", "running")
        self.attrs = {
            "Id": container_id,
            "Name": f"/{name}",
            "Config": {"Image": image, "Labels": dict(labels)},
            "HostConfig": host_config or {"Memory": 0, "NanoCpus": 0, "PidsLimit": 64},
            "State": state
            or {
                "Status": "running",
                "Running": True,
                "ExitCode": 0,
                "OOMKilled": False,
                "StartedAt": "2026-11-24T01:00:00.000000000Z",
                "FinishedAt": "0001-01-01T00:00:00Z",
            },
        }
        self._stats_payload = stats_payload or {}
        self.client = _FakeClientRef(self._stats_payload)
        self.start_calls = 0
        self.stopped = False
        self.killed = False
        self.removed = False

    def reload(self) -> None:
        self.status = self.attrs["State"]["Status"]

    def start(self) -> None:
        self.start_calls += 1
        self.attrs["State"]["Status"] = "running"
        self.attrs["State"]["Running"] = True
        self.status = "running"

    def stats(self, stream: bool = False) -> dict[str, Any]:
        return self._stats_payload

    def stop(self, timeout: int = 5) -> None:
        self.stopped = True
        self.attrs["State"]["Status"] = "exited"
        self.attrs["State"]["Running"] = False
        self.attrs["State"]["ExitCode"] = 143
        self.attrs["State"]["FinishedAt"] = "2026-11-24T01:01:00.000000000Z"

    def kill(self) -> None:
        self.killed = True
        self.attrs["State"]["Status"] = "exited"
        self.attrs["State"]["Running"] = False
        self.attrs["State"]["ExitCode"] = 137
        self.attrs["State"]["FinishedAt"] = "2026-11-24T01:01:00.000000000Z"

    def remove(self, force: bool = False) -> None:
        self.removed = True


class _FakeClientRef:
    def __init__(self, stats_payload: dict[str, Any]) -> None:
        self._stats_payload = stats_payload
        self.api = self

    def stats(self, _cid: str, stream: bool = False, one_shot: bool = True) -> dict[str, Any]:
        return self._stats_payload


class _FakeContainersManager:
    def __init__(self) -> None:
        self.containers: list[_FakeContainer] = []
        self.run_calls: list[dict[str, Any]] = []

    def list(self, all: bool = True, filters: dict[str, Any] | None = None) -> list[_FakeContainer]:
        label_filters = (filters or {}).get("label") or []
        results: list[_FakeContainer] = []
        for c in self.containers:
            if c.removed:
                continue
            matched = True
            for lf in label_filters:
                k, _, v = lf.partition("=")
                if c.labels.get(k) != v:
                    matched = False
                    break
            if matched:
                results.append(c)
        return results

    def run(self, **kwargs: Any) -> _FakeContainer:
        self.run_calls.append(kwargs)
        c = _FakeContainer(
            container_id=f"cid-{len(self.containers) + 1:012d}",
            name=kwargs["name"],
            image=kwargs["image"],
            labels=kwargs["labels"],
            host_config={
                "Memory": 512 * 1024 * 1024 if kwargs.get("mem_limit") else 0,
                "NanoCpus": kwargs.get("nano_cpus", 0),
                "PidsLimit": kwargs.get("pids_limit", 64),
            },
        )
        self.containers.append(c)
        return c

    def get(self, cid: str) -> _FakeContainer:
        raise DockerException("not in container")


class _FakeDockerAPI:
    def __init__(self, *, stream_delay: float = 0.0) -> None:
        self.build_calls: list[dict[str, Any]] = []
        self.stream_delay = stream_delay

    def build(self, **kwargs: Any):
        raw_tar = kwargs["fileobj"].read()
        self.build_calls.append({"kwargs": kwargs, "tar_bytes": raw_tar})
        yield {"stream": "Step 1/6 : FROM pies-plugin-base:latest\n"}
        if self.stream_delay > 0:
            time.sleep(self.stream_delay)
        yield {"stream": "Successfully built fake123\n"}


class _FakeImageObj:
    def __init__(self, name: str, labels: dict[str, str] | None = None) -> None:
        self.id = f"sha256:{name}"
        self.labels = dict(labels or {})
        self.attrs = {"Id": self.id, "Config": {"Labels": dict(self.labels)}}


class _FakeImagesManager:
    def __init__(self, present_images: set[str] | dict[str, dict[str, str]] | None = None) -> None:
        if isinstance(present_images, dict):
            self.present_images: dict[str, dict[str, str]] = {
                k: dict(v) for k, v in present_images.items()
            }
        else:
            self.present_images = {k: {} for k in (present_images or ())}
        self.get_calls: list[str] = []
        self.remove_calls: list[dict[str, Any]] = []
        self.remove_conflict_images: set[str] = set()

    def get(self, name: str) -> _FakeImageObj:
        self.get_calls.append(name)
        if name not in self.present_images:
            raise ImageNotFound(f"No such image: {name}")
        return _FakeImageObj(name, self.present_images[name])

    def remove(self, *, image: str, force: bool = False) -> None:
        self.remove_calls.append({"image": image, "force": force})
        if image not in self.present_images:
            raise ImageNotFound(f"No such image: {image}")
        if image in self.remove_conflict_images and not force:
            import httpx

            resp = httpx.Response(status_code=409, text="image in use")
            raise APIError("conflict: image is being used by running container", response=resp)
        del self.present_images[image]


class _FakeDockerClient:
    def __init__(
        self,
        *,
        ping_ok: bool = True,
        present_images: set[str] | dict[str, dict[str, str]] | None = None,
        ncpu: int = 16,
        stream_delay: float = 0.0,
    ) -> None:
        self.ping_ok = ping_ok
        self.ncpu = ncpu
        self.info_calls = 0
        self.containers = _FakeContainersManager()
        self.images = _FakeImagesManager(present_images=present_images)
        self.api = _FakeDockerAPI(stream_delay=stream_delay)

    def ping(self) -> bool:
        if not self.ping_ok:
            raise DockerException("daemon down")
        return True

    def info(self) -> dict[str, Any]:
        self.info_calls += 1
        return {"NCPU": self.ncpu}


def _make_client(
    tmp_path: Path,
    *,
    ping_ok: bool = True,
    cgroup_root: Path | None = None,
    required_images: str = "",
    present_images: set[str] | dict[str, dict[str, str]] | None = None,
    ncpu: int = 16,
    default_build_timeout_seconds: int = 600,
    stream_delay: float = 0.0,
) -> tuple[TestClient, _FakeDockerClient, JobRunnerSettings]:
    stage_root = tmp_path / "stages"
    plugin_root = tmp_path / "plugin_builds"
    stage_root.mkdir(parents=True, exist_ok=True)
    plugin_root.mkdir(parents=True, exist_ok=True)
    settings = JobRunnerSettings(
        stage_root=stage_root,
        host_stage_root=stage_root,
        plugin_source_root=plugin_root,
        cgroup_root=cgroup_root or (tmp_path / "nonexistent_cgroup"),
        required_images=required_images,
        default_build_timeout_seconds=default_build_timeout_seconds,
    )
    fake_docker = _FakeDockerClient(
        ping_ok=ping_ok,
        present_images=present_images,
        ncpu=ncpu,
        stream_delay=stream_delay,
    )
    service = DockerJobRunnerService(settings, client=fake_docker)  # type: ignore[arg-type]
    app = create_app(settings=settings, service=service)
    return TestClient(app), fake_docker, settings


def test_only_fixed_endpoints_are_exposed(tmp_path: Path) -> None:
    client, _, _ = _make_client(tmp_path)
    routes = {
        (route.path, tuple(sorted(route.methods or [])))
        for route in client.app.routes  # type: ignore[union-attr]
        if not route.path.startswith(("/openapi", "/docs", "/redoc"))
    }
    assert routes == {
        ("/healthz", ("GET",)),
        ("/readyz", ("GET",)),
        ("/v1/plugins/build", ("POST",)),
        ("/v1/plugins/images/delete", ("POST",)),
        ("/v1/stages/start", ("POST",)),
        ("/v1/stages", ("GET",)),
        ("/v1/stages/{stage_execution_id}", ("GET",)),
        ("/v1/stages/{stage_execution_id}/stop", ("POST",)),
        ("/v1/stages/{stage_execution_id}/cleanup", ("POST",)),
    }


def test_healthz_and_readyz(tmp_path: Path, monkeypatch: Any) -> None:
    client_ok, _, _ = _make_client(tmp_path, ping_ok=True)
    assert client_ok.get("/healthz").json() == {"status": "ok"}
    ready = client_ok.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.json()["required_images"] == {}

    client_down, _, _ = _make_client(tmp_path, ping_ok=False)
    assert client_down.get("/healthz").status_code == 200
    assert client_down.get("/readyz").status_code == 503

    # Required images present -> 200 with required_images status
    client_img_ok, fake_ok, _ = _make_client(
        tmp_path,
        required_images="pies-builtin-stages:1.1.5, pies-plugin-base:latest, pies-builtin-stages:1.1.5",
        present_images={"pies-builtin-stages:1.1.5", "pies-plugin-base:latest"},
    )
    ready_img_ok = client_img_ok.get("/readyz")
    assert ready_img_ok.status_code == 200
    assert ready_img_ok.json()["required_images"] == {
        "pies-builtin-stages:1.1.5": "ok",
        "pies-plugin-base:latest": "ok",
    }
    assert fake_ok.images.get_calls == ["pies-builtin-stages:1.1.5", "pies-plugin-base:latest"]

    # Missing required image -> 503
    client_img_missing, _, _ = _make_client(
        tmp_path,
        required_images="pies-builtin-stages:1.1.5, missing-stage:1.0.0",
        present_images={"pies-builtin-stages:1.1.5"},
    )
    ready_img_missing = client_img_missing.get("/readyz")
    assert ready_img_missing.status_code == 503
    assert "missing-stage:1.0.0" in ready_img_missing.json()["detail"]

    # Env var PIES_JOB_RUNNER_REQUIRED_IMAGES is parsed as comma-separated list
    monkeypatch.setenv("PIES_JOB_RUNNER_REQUIRED_IMAGES", " pies-builtin-stages:1.1.5 , , extra:2 ")
    env_settings = JobRunnerSettings()
    assert env_settings.parsed_required_images() == ("pies-builtin-stages:1.1.5", "extra:2")


def test_build_plugin_uses_controlled_template_and_declared_requirements(tmp_path: Path) -> None:
    client, fake_docker, settings = _make_client(tmp_path)

    # Path traversal rejected on source_rel
    resp_bad = client.post(
        "/v1/plugins/build",
        json={"plugin_id": "acme.algo", "version": "1.0.0", "source_rel": "../escape"},
    )
    assert resp_bad.status_code == 400

    # Valid plugin source with decoy Dockerfile and relative requirements path containing spaces
    src_dir = settings.plugin_source_root / "acme.algo" / "1.0.0"
    (src_dir / "my deps").mkdir(parents=True)
    (src_dir / "plugin.yaml").write_text("schema: ies.algorithm-plugin\n", encoding="utf-8")
    (src_dir / "main.py").write_text("def solve(ctx): pass\n", encoding="utf-8")
    (src_dir / "my deps" / "requirements.txt").write_text("numpy>=1.26\n", encoding="utf-8")
    (src_dir / "Dockerfile").write_text("FROM evil:latest\nRUN curl evil.com\n", encoding="utf-8")

    req_rel = "my deps/requirements.txt"
    resp_ok = client.post(
        "/v1/plugins/build",
        json={
            "plugin_id": "acme.algo",
            "version": "1.0.0",
            "source_rel": "acme.algo/1.0.0",
            "requirements_rel": req_rel,
        },
    )
    assert resp_ok.status_code == 200
    body = resp_ok.json()
    assert body["image"] == "pies-plugin/acme.algo:1.0.0"

    assert len(fake_docker.api.build_calls) == 1
    tar_bytes = fake_docker.api.build_calls[0]["tar_bytes"]
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r") as tar:
        df = tar.extractfile("Dockerfile")
        assert df is not None
        df_text = df.read().decode("utf-8")
        assert df_text == render_plugin_dockerfile(
            base_image="pies-plugin-base:latest",
            plugin_id="acme.algo",
            version="1.0.0",
            requirements_rel=req_rel,
            runtime_user="10001:10001",
        )
        assert "USER root" in df_text
        assert "USER 10001:10001" in df_text
        assert "FROM evil:latest" not in df_text
        assert 'COPY ["my deps/requirements.txt", "/plugin/my deps/requirements.txt"]' in df_text
        assert 'RUN ["pip", "install", "--no-cache-dir", "-r", "/plugin/my deps/requirements.txt"]' in df_text

    # Escaping requirements_rel is rejected
    resp_escape = client.post(
        "/v1/plugins/build",
        json={
            "plugin_id": "acme.algo",
            "version": "1.0.0",
            "source_rel": "acme.algo/1.0.0",
            "requirements_rel": "../secret.txt",
        },
    )
    assert resp_escape.status_code == 400


def test_start_stage_requires_limits_enforces_isolation_and_idempotency(tmp_path: Path) -> None:
    client, fake_docker, settings = _make_client(tmp_path)
    stage_in = settings.stage_root / "exec-1" / "in"
    stage_in.mkdir(parents=True)
    (stage_in / "request.json").write_text("{}", encoding="utf-8")

    # Missing resource limits must be rejected (no silent unlimited containers)
    incomplete = client.post(
        "/v1/stages/start",
        json={
            "attempt_id": "att-1",
            "stage_id": "execution",
            "stage_execution_id": "exec-1",
            "fencing_token": "fence-1",
            "image": "pies-plugin/acme.algo:1.0.0",
        },
    )
    assert incomplete.status_code == 422

    payload = {
        "attempt_id": "att-1",
        "stage_id": "execution",
        "stage_execution_id": "exec-1",
        "fencing_token": "fence-1",
        "image": "pies-plugin/acme.algo:1.0.0",
        "cpu_cores": 2.0,
        "memory_mb": 512,
        "pids_limit": 32,
        "tmpfs_size_mb": 128,
    }
    first = client.post("/v1/stages/start", json=payload)
    assert first.status_code == 200
    assert first.json()["reused"] is False

    assert len(fake_docker.containers.run_calls) == 1
    run_kwargs = fake_docker.containers.run_calls[0]
    assert run_kwargs["network_mode"] == "none"
    assert run_kwargs["read_only"] is True
    assert run_kwargs["cap_drop"] == ["ALL"]
    assert run_kwargs["user"] == "10001:10001"
    assert run_kwargs["nano_cpus"] == 2_000_000_000
    assert run_kwargs["mem_limit"] == "512m"
    assert run_kwargs["memswap_limit"] == "512m"
    assert run_kwargs["pids_limit"] == 32
    assert run_kwargs["tmpfs"] == {"/tmp": "rw,nosuid,nodev,size=128m"}
    assert run_kwargs["labels"] == {
        LABEL_MANAGED: "true",
        LABEL_ATTEMPT_ID: "att-1",
        LABEL_STAGE_ID: "execution",
        LABEL_STAGE_EXECUTION_ID: "exec-1",
        LABEL_FENCING_TOKEN: "fence-1",
        LABEL_WORKSPACE_REL: "exec-1",
    }

    # Restarted caller retries start_stage for same (attempt_id, stage_id) -> idempotent reuse
    second = client.post("/v1/stages/start", json=payload)
    assert second.status_code == 200
    assert second.json()["reused"] is True
    assert second.json()["stage_execution_id"] == "exec-1"
    assert len(fake_docker.containers.run_calls) == 1


def test_start_stage_resumes_created_container_without_restarting_exited(tmp_path: Path) -> None:
    client, fake_docker, settings = _make_client(tmp_path)
    (settings.stage_root / "exec-created" / "in").mkdir(parents=True)

    created_container = _FakeContainer(
        container_id="cid-created-001",
        name="pies-stage-exec-created",
        image="pies-plugin/acme:1.0.0",
        labels={
            LABEL_MANAGED: "true",
            LABEL_ATTEMPT_ID: "att-created",
            LABEL_STAGE_ID: "solver",
            LABEL_STAGE_EXECUTION_ID: "exec-created",
            LABEL_FENCING_TOKEN: "fence-created",
            LABEL_WORKSPACE_REL: "exec-created",
        },
        state={
            "Status": "created",
            "Running": False,
            "ExitCode": 0,
            "OOMKilled": False,
            "StartedAt": "0001-01-01T00:00:00Z",
            "FinishedAt": "0001-01-01T00:00:00Z",
        },
    )
    fake_docker.containers.containers.append(created_container)

    resumed = client.post(
        "/v1/stages/start",
        json={
            "attempt_id": "att-created",
            "stage_id": "solver",
            "stage_execution_id": "exec-created",
            "fencing_token": "fence-created",
            "image": "pies-plugin/acme:1.0.0",
            "cpu_cores": 1.0,
            "memory_mb": 256,
            "pids_limit": 32,
            "tmpfs_size_mb": 64,
        },
    )
    assert resumed.status_code == 200
    assert resumed.json()["reused"] is True
    assert resumed.json()["running"] is True
    assert resumed.json()["status"] == "running"
    assert created_container.start_calls == 1

    # Once exited, calling start_stage again must NOT restart it
    created_container.attrs["State"]["Status"] = "exited"
    created_container.attrs["State"]["Running"] = False
    exited_retry = client.post(
        "/v1/stages/start",
        json={
            "attempt_id": "att-created",
            "stage_id": "solver",
            "stage_execution_id": "exec-created",
            "fencing_token": "fence-created",
            "image": "pies-plugin/acme:1.0.0",
            "cpu_cores": 1.0,
            "memory_mb": 256,
            "pids_limit": 32,
            "tmpfs_size_mb": 64,
        },
    )
    assert exited_retry.status_code == 200
    assert exited_retry.json()["reused"] is True
    assert exited_retry.json()["running"] is False
    assert created_container.start_calls == 1


def test_cgroup_v2_observation_and_unknown_fallback(tmp_path: Path) -> None:
    cgroup_root = tmp_path / "cgroup"
    cid = "cid-000000000001"
    cg_dir = cgroup_root / "system.slice" / f"docker-{cid}.scope"
    cg_dir.mkdir(parents=True)
    (cg_dir / "memory.current").write_text("1048576\n", encoding="utf-8")
    (cg_dir / "memory.peak").write_text("2097152\n", encoding="utf-8")
    (cg_dir / "memory.events").write_text("low 0\noom_kill 0\n", encoding="utf-8")
    (cg_dir / "cpu.stat").write_text("usage_usec 12500\n", encoding="utf-8")

    client, fake_docker, settings = _make_client(tmp_path, cgroup_root=cgroup_root)
    stage_in = settings.stage_root / "exec-cg" / "in"
    stage_out = settings.stage_root / "exec-cg" / "out"
    stage_in.mkdir(parents=True)
    stage_out.mkdir(parents=True)
    (stage_out / "iterations.jsonl").write_text(
        '{"iteration": 1, "value": 0.5}\n{"iteration": 2, "value": 0.01}\npartial-line\n',
        encoding="utf-8",
    )

    fake_docker.containers.containers.append(
        _FakeContainer(
            container_id=cid,
            name="pies-stage-exec-cg",
            image="pies-plugin/acme:1.0.0",
            labels={
                LABEL_MANAGED: "true",
                LABEL_ATTEMPT_ID: "att-cg",
                LABEL_STAGE_ID: "solver",
                LABEL_STAGE_EXECUTION_ID: "exec-cg",
                LABEL_FENCING_TOKEN: "fence-cg",
                LABEL_WORKSPACE_REL: "exec-cg",
            },
            host_config={"Memory": 536870912, "NanoCpus": 1_000_000_000, "PidsLimit": 16},
        )
    )

    resp = client.get("/v1/stages/exec-cg?iterations_tail=2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["resources"]["memory_limit_bytes"] == {"status": "limited", "value": 536870912}
    assert data["resources"]["memory_current_bytes"] == {"status": "known", "value": 1048576}
    assert data["resources"]["memory_peak_bytes"] == {"status": "known", "value": 2097152}
    assert data["resources"]["cpu_usage_ns"] == {"status": "known", "value": 12_500_000}
    assert data["resources"]["oom_kill_count"] == {"status": "known", "value": 0}
    assert data["iterations"] == {
        "present": True,
        "count": 3,
        "tail": [{"iteration": 2, "value": 0.01}, "partial-line"],
    }

    # Missing memory.peak must remain unknown even when memory.current is readable
    (cg_dir / "memory.peak").unlink()
    (settings.stage_root / "exec-cg" / ".job_runner_stats.json").unlink(missing_ok=True)
    resp_no_peak = client.get("/v1/stages/exec-cg")
    assert resp_no_peak.status_code == 200
    res_np = resp_no_peak.json()["resources"]
    assert res_np["memory_current_bytes"] == {"status": "known", "value": 1048576}
    assert res_np["memory_peak_bytes"] == {"status": "unknown", "value": None}

    # Unreadable cgroup files are reported as unknown without blocking
    (cg_dir / "memory.current").unlink()
    (cg_dir / "cpu.stat").unlink()
    (settings.stage_root / "exec-cg" / ".job_runner_stats.json").unlink(missing_ok=True)
    resp_unknown = client.get("/v1/stages/exec-cg")
    assert resp_unknown.status_code == 200
    res_u = resp_unknown.json()["resources"]
    assert res_u["memory_current_bytes"] == {"status": "unknown", "value": None}
    assert res_u["memory_peak_bytes"] == {"status": "unknown", "value": None}
    assert res_u["cpu_usage_ns"] == {"status": "unknown", "value": None}


def test_stop_preserves_container_and_outputs_before_cleanup(tmp_path: Path) -> None:
    client, fake_docker, settings = _make_client(tmp_path)
    stage_in = settings.stage_root / "exec-stop" / "in"
    stage_in.mkdir(parents=True)
    start_resp = client.post(
        "/v1/stages/start",
        json={
            "attempt_id": "att-stop",
            "stage_id": "solver",
            "stage_execution_id": "exec-stop",
            "fencing_token": "fence-stop",
            "image": "pies-plugin/acme:1.0.0",
            "cpu_cores": 1.0,
            "memory_mb": 256,
            "pids_limit": 32,
            "tmpfs_size_mb": 64,
        },
    )
    assert start_resp.status_code == 200
    artifact = settings.stage_root / "exec-stop" / "out" / "solution.json"
    artifact.write_text(json.dumps({"ok": True}), encoding="utf-8")

    # Running container cannot be cleaned up prematurely without force
    premature = client.post("/v1/stages/exec-stop/cleanup", json={})
    assert premature.status_code == 409

    # Stop terminates container WITHOUT deleting container or /work/out
    stopped = client.post(
        "/v1/stages/exec-stop/stop",
        json={"reason": "timed_out", "grace_seconds": 3},
    )
    assert stopped.status_code == 200
    assert stopped.json()["termination_reason"] == "timed_out"
    assert stopped.json()["output_files"] == ["solution.json"]
    assert fake_docker.containers.containers[0].removed is False
    assert artifact.is_file()

    # Cleanup removes container and workspace only when requested
    cleaned = client.post("/v1/stages/exec-stop/cleanup", json={})
    assert cleaned.status_code == 200
    assert cleaned.json() == {
        "stage_execution_id": "exec-stop",
        "status": "cleaned",
        "container_removed": True,
        "workspace_removed": True,
    }
    assert not (settings.stage_root / "exec-stop").exists()

    # Repeat cleanup is idempotent
    cleaned_again = client.post("/v1/stages/exec-stop/cleanup", json={})
    assert cleaned_again.status_code == 200
    assert cleaned_again.json()["container_removed"] is False
    assert cleaned_again.json()["workspace_removed"] is False


def test_read_iterations_summary_absent(tmp_path: Path) -> None:
    summary = read_iterations_summary(tmp_path / "missing_out")
    assert summary.present is False
    assert summary.count == 0
    assert summary.tail == []


def test_start_stage_clamps_cpu_cores_to_host_ncpu_and_logs_errors(tmp_path: Path, caplog: Any) -> None:
    client, fake_docker, settings = _make_client(tmp_path, ncpu=4)
    (settings.stage_root / "exec-clamp-1" / "in").mkdir(parents=True)
    (settings.stage_root / "exec-clamp-2" / "in").mkdir(parents=True)

    r1 = client.post(
        "/v1/stages/start",
        json={
            "attempt_id": "att-clamp-1",
            "stage_id": "solver",
            "stage_execution_id": "exec-clamp-1",
            "fencing_token": "fence-1",
            "image": "pies-builtin-stages:1.1.5",
            "cpu_cores": 8.0,
            "memory_mb": 256,
            "pids_limit": 32,
            "tmpfs_size_mb": 64,
        },
    )
    assert r1.status_code == 200
    assert fake_docker.containers.run_calls[0]["nano_cpus"] == 4_000_000_000

    # Second start reuses cached host NCPU without re-querying client.info()
    r2 = client.post(
        "/v1/stages/start",
        json={
            "attempt_id": "att-clamp-2",
            "stage_id": "solver",
            "stage_execution_id": "exec-clamp-2",
            "fencing_token": "fence-2",
            "image": "pies-builtin-stages:1.1.5",
            "cpu_cores": 6.0,
            "memory_mb": 256,
            "pids_limit": 32,
            "tmpfs_size_mb": 64,
        },
    )
    assert r2.status_code == 200
    assert fake_docker.containers.run_calls[1]["nano_cpus"] == 4_000_000_000
    assert fake_docker.info_calls == 1

    # JobRunnerError is logged so 4xx/5xx reasons appear in container logs
    with caplog.at_level("WARNING", logger="pies_job_runner.app"):
        missing = client.get("/v1/stages/does-not-exist")
    assert missing.status_code == 404
    assert any("job_runner 404:" in rec.message for rec in caplog.records)


def test_build_plugin_pip_index_url_network_none_and_timeout(tmp_path: Path, monkeypatch: Any) -> None:
    client, fake_docker, settings = _make_client(tmp_path, default_build_timeout_seconds=120)
    src_dir = settings.plugin_source_root / "acme.idx" / "1.0.0"
    src_dir.mkdir(parents=True)
    (src_dir / "plugin.yaml").write_text("schema: ies.algorithm-plugin\n", encoding="utf-8")
    (src_dir / "main.py").write_text("def solve(ctx): pass\n", encoding="utf-8")
    (src_dir / "requirements.txt").write_text("numpy>=1.26\n", encoding="utf-8")

    # 1. Valid custom pip_index_url emits ONLY --index-url (never --extra-index-url).
    ok_idx = client.post(
        "/v1/plugins/build",
        json={
            "plugin_id": "acme.idx",
            "version": "1.0.0",
            "source_rel": "acme.idx/1.0.0",
            "requirements_rel": "requirements.txt",
            "pip_index_url": "https://pypi.internal.example/simple",
            "timeout_seconds": 300,
        },
    )
    assert ok_idx.status_code == 200, ok_idx.text
    call0 = fake_docker.api.build_calls[0]
    assert call0["kwargs"]["timeout"] == 300
    assert "network_mode" not in call0["kwargs"]
    with tarfile.open(fileobj=io.BytesIO(call0["tar_bytes"]), mode="r") as tar:
        df_text = tar.extractfile("Dockerfile").read().decode("utf-8")  # type: ignore[union-attr]
    assert (
        'RUN ["pip", "install", "--no-cache-dir", "--index-url", '
        '"https://pypi.internal.example/simple", "-r", "/plugin/requirements.txt"]'
    ) in df_text
    assert "--extra-index-url" not in df_text

    # 2. Empty pip_index_url -> None (default PyPI), omitted timeout -> default_build_timeout_seconds.
    no_idx = client.post(
        "/v1/plugins/build",
        json={
            "plugin_id": "acme.idx",
            "version": "1.0.0",
            "source_rel": "acme.idx/1.0.0",
            "requirements_rel": "requirements.txt",
            "pip_index_url": "",
        },
    )
    assert no_idx.status_code == 200
    call1 = fake_docker.api.build_calls[1]
    assert call1["kwargs"]["timeout"] == 120
    with tarfile.open(fileobj=io.BytesIO(call1["tar_bytes"]), mode="r") as tar:
        df_text1 = tar.extractfile("Dockerfile").read().decode("utf-8")  # type: ignore[union-attr]
    assert "--index-url" not in df_text1
    assert "--extra-index-url" not in df_text1

    # 3. No requirements_rel enforces network_mode="none".
    no_req = client.post(
        "/v1/plugins/build",
        json={
            "plugin_id": "acme.idx",
            "version": "1.0.0",
            "source_rel": "acme.idx/1.0.0",
        },
    )
    assert no_req.status_code == 200
    assert fake_docker.api.build_calls[2]["kwargs"]["network_mode"] == "none"

    # 4. Invalid pip_index_url and out-of-range timeout_seconds fail with 422.
    for bad_url in (
        "   ",
        "https://pypi.example/simple --extra-index-url https://evil",
        "https://pypi.example/simple\nRUN evil",
        "ftp://pypi.example/simple",
        "http://",
        "/relative/path",
    ):
        r_bad = client.post(
            "/v1/plugins/build",
            json={
                "plugin_id": "acme.idx",
                "version": "1.0.0",
                "source_rel": "acme.idx/1.0.0",
                "requirements_rel": "requirements.txt",
                "pip_index_url": bad_url,
            },
        )
        assert r_bad.status_code == 422, (bad_url, r_bad.text)

    for bad_timeout in (9, 3601):
        r_to = client.post(
            "/v1/plugins/build",
            json={
                "plugin_id": "acme.idx",
                "version": "1.0.0",
                "source_rel": "acme.idx/1.0.0",
                "timeout_seconds": bad_timeout,
            },
        )
        assert r_to.status_code == 422

    # 5. Monotonic deadline enforcement across slow build stream.
    ticks = iter([100.0, 115.0, 115.0])
    monkeypatch.setattr("pies_job_runner.docker_ops._monotonic", lambda: next(ticks))
    timed_out = client.post(
        "/v1/plugins/build",
        json={
            "plugin_id": "acme.idx",
            "version": "1.0.0",
            "source_rel": "acme.idx/1.0.0",
            "timeout_seconds": 10,
        },
    )
    assert timed_out.status_code == 400
    assert "timed out" in timed_out.json()["detail"]


def test_build_plugin_hard_deadline_interrupts_blocked_and_slow_streams(
    tmp_path: Path,
) -> None:
    _, fake_docker, settings = _make_client(tmp_path)
    fast_settings = settings.model_copy(update={"default_build_timeout_seconds": 0.05})
    client = TestClient(
        create_app(
            settings=fast_settings,
            service=DockerJobRunnerService(fast_settings, client=fake_docker),  # type: ignore[arg-type]
        )
    )
    src_dir = settings.plugin_source_root / "acme.hang" / "1.0.0"
    src_dir.mkdir(parents=True)
    (src_dir / "plugin.yaml").write_text("schema: ies.algorithm-plugin\n", encoding="utf-8")
    (src_dir / "main.py").write_text("def solve(ctx): pass\n", encoding="utf-8")

    # 1. Stream permanently blocked inside next(stream) must abort promptly at deadline.
    release_stream = threading.Event()
    blocked_closed = threading.Event()

    class _BlockedStream:
        def __init__(self) -> None:
            self._step = 0

        def __iter__(self) -> _BlockedStream:
            return self

        def __next__(self) -> dict[str, str]:
            if self._step == 0:
                self._step = 1
                return {"stream": "Step 1/2 : FROM pies-plugin-base:latest\n"}
            release_stream.wait(10.0)
            raise StopIteration

        def close(self) -> None:
            blocked_closed.set()

    fake_docker.api.build = lambda **kwargs: _BlockedStream()  # type: ignore[method-assign]
    started = time.monotonic()
    try:
        blocked_resp = client.post(
            "/v1/plugins/build",
            json={
                "plugin_id": "acme.hang",
                "version": "1.0.0",
                "source_rel": "acme.hang/1.0.0",
            },
        )
    finally:
        release_stream.set()
    elapsed = time.monotonic() - started
    assert blocked_resp.status_code == 400
    assert "timed out" in blocked_resp.json()["detail"]
    assert elapsed < 1.0
    assert blocked_closed.is_set()

    # 2. Multi-chunk stream whose per-chunk gap < budget still aborts when total > budget.
    def _slow_chunks(**kwargs: Any):
        for idx in range(6):
            time.sleep(0.025)
            yield {"stream": f"Step {idx + 1}/6\n"}

    fake_docker.api.build = _slow_chunks  # type: ignore[method-assign]
    slow_started = time.monotonic()
    slow_resp = client.post(
        "/v1/plugins/build",
        json={
            "plugin_id": "acme.hang",
            "version": "1.0.0",
            "source_rel": "acme.hang/1.0.0",
        },
    )
    slow_elapsed = time.monotonic() - slow_started
    assert slow_resp.status_code == 400
    assert "timed out" in slow_resp.json()["detail"]
    assert slow_elapsed < 1.0


def test_delete_plugin_image_whitelist_labels_and_conflicts(tmp_path: Path) -> None:
    present = {
        "pies-plugin/acme.algo:1.0.0": {
            LABEL_MANAGED: "true",
            LABEL_PLUGIN_ID: "acme.algo",
            LABEL_PLUGIN_VERSION: "1.0.0",
        },
        "pies-plugin/unmanaged.algo:1.0.0": {
            LABEL_PLUGIN_ID: "unmanaged.algo",
            LABEL_PLUGIN_VERSION: "1.0.0",
        },
        "pies-plugin/mismatch.algo:1.0.0": {
            LABEL_MANAGED: "true",
            LABEL_PLUGIN_ID: "other.algo",
            LABEL_PLUGIN_VERSION: "1.0.0",
        },
        "pies-plugin/busy.algo:1.0.0": {
            LABEL_MANAGED: "true",
            LABEL_PLUGIN_ID: "busy.algo",
            LABEL_PLUGIN_VERSION: "1.0.0",
        },
    }
    client, fake_docker, settings = _make_client(tmp_path, present_images=present)
    fake_docker.images.remove_conflict_images.add("pies-plugin/busy.algo:1.0.0")

    # Delete managed matching image -> deleted=True, then idempotent not_found.
    resp_del = client.post(
        "/v1/plugins/images/delete",
        json={"plugin_id": "acme.algo", "version": "1.0.0"},
    )
    assert resp_del.status_code == 200, resp_del.text
    assert resp_del.json() == {
        "image": "pies-plugin/acme.algo:1.0.0",
        "plugin_id": "acme.algo",
        "version": "1.0.0",
        "deleted": True,
        "status": "deleted",
    }

    resp_missing = client.post(
        "/v1/plugins/images/delete",
        json={"plugin_id": "acme.algo", "version": "1.0.0"},
    )
    assert resp_missing.status_code == 200
    assert resp_missing.json()["deleted"] is False
    assert resp_missing.json()["status"] == "not_found"

    # Unmanaged or label-mismatched images are rejected 400 and never removed.
    for pid in ("unmanaged.algo", "mismatch.algo"):
        r_bad = client.post(
            "/v1/plugins/images/delete",
            json={"plugin_id": pid, "version": "1.0.0"},
        )
        assert r_bad.status_code == 400
        assert f"pies-plugin/{pid}:1.0.0" in fake_docker.images.present_images

    # Docker 409 in-use conflict maps to 409; force=True removes cleanly.
    r_busy = client.post(
        "/v1/plugins/images/delete",
        json={"plugin_id": "busy.algo", "version": "1.0.0", "force": False},
    )
    assert r_busy.status_code == 409
    r_force = client.post(
        "/v1/plugins/images/delete",
        json={"plugin_id": "busy.algo", "version": "1.0.0", "force": True},
    )
    assert r_force.status_code == 200
    assert r_force.json()["deleted"] is True

    # Prefix tampering toward builtin/base images is blocked.
    bad_settings = settings.model_copy(update={"plugin_image_prefix": "pies-builtin-stages"})
    bad_client = TestClient(
        create_app(
            settings=bad_settings,
            service=DockerJobRunnerService(bad_settings, client=fake_docker),  # type: ignore[arg-type]
        )
    )
    r_prefix = bad_client.post(
        "/v1/plugins/images/delete",
        json={"plugin_id": "acme.algo", "version": "1.0.0"},
    )
    assert r_prefix.status_code == 400
