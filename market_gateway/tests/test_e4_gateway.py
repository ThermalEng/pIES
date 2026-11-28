"""Unit and local sandbox E2E tests for stateless `pies_market_gateway`."""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import logging
import re
import smtplib
import socketserver
import threading
import time
import zipfile
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from pies_market import check_market_pr
from pies_sdk import (
    compute_publisher_email_digest,
    generate_public_namespace,
    issue_otp_challenge,
    issue_submission_token,
    pack_plugin_package,
    write_json_file,
)

from pies_market_gateway import GatewaySettings, create_app

GATEWAY_SRC_DIR = Path(__file__).resolve().parents[1] / "pies_market_gateway"
SECRET = "e4-gateway-test-hmac-secret"

_FORBIDDEN_MODULE_PREFIXES = (
    "iesplan",
    "sqlalchemy",
    "psycopg",
    "sqlite3",
    "redis",
    "celery",
    "pies_job_runner",
)


class _LocalSmtpServer:
    """Minimal real TCP SMTP server capturing OTP messages for E2E verification."""

    def __init__(self) -> None:
        outer = self
        self.messages: list[str] = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                self.wfile.write(b"220 localhost ESMTP Ready\r\n")
                self.wfile.flush()
                in_data = False
                lines: list[bytes] = []
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        break
                    if in_data:
                        if raw.rstrip(b"\r\n") == b".":
                            outer.messages.append(b"".join(lines).decode("utf-8", errors="replace"))
                            lines.clear()
                            in_data = False
                            self.wfile.write(b"250 OK\r\n")
                            self.wfile.flush()
                        else:
                            lines.append(raw)
                        continue
                    cmd = raw.decode("utf-8", errors="replace").strip()
                    upper = cmd.upper()
                    if upper.startswith(("EHLO", "HELO")):
                        self.wfile.write(b"250-localhost\r\n250 OK\r\n")
                    elif upper.startswith(("MAIL FROM:", "RCPT TO:")):
                        self.wfile.write(b"250 OK\r\n")
                    elif upper == "DATA":
                        in_data = True
                        self.wfile.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                    elif upper == "QUIT":
                        self.wfile.write(b"221 Bye\r\n")
                        self.wfile.flush()
                        break
                    else:
                        self.wfile.write(b"250 OK\r\n")
                    self.wfile.flush()

        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _LocalSmtpServer:
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def last_otp(self) -> str:
        assert self.messages, "No SMTP OTP message captured"
        match = re.search(r"verification code is:\s*(\d{6})", self.messages[-1])
        assert match is not None, f"OTP not found in SMTP payload: {self.messages[-1]}"
        return match.group(1)


class _MockGitHubServer:
    """Real local HTTP mock GitHub REST API supporting Git Data API + Pulls API."""

    def __init__(self, initial_publishers_yaml: str) -> None:
        self.branches: dict[str, dict[str, bytes]] = {
            "main": {"publishers.yaml": initial_publishers_yaml.encode("utf-8")}
        }
        self.branch_heads: dict[str, str] = {"main": "base-sha-001"}
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, dict[str, bytes]] = {"base-tree-001": dict(self.branches["main"])}
        self.commits: dict[str, dict[str, Any]] = {
            "base-sha-001": {"tree": {"sha": "base-tree-001"}, "parents": []}
        }
        self.pulls: list[dict[str, Any]] = []
        self.write_calls: list[tuple[str, str]] = []
        self.fail_next_tree: bool = False
        self.fail_next_pull: bool = False
        self.on_read_publishers_done: Callable[[], None] | None = None
        self.on_before_create_ref: Callable[[], None] | None = None

    def register_blob(self, content: bytes) -> str:
        bsha = f"blob-{hashlib.sha256(content).hexdigest()[:16]}"
        self.blobs[bsha] = content
        return bsha

    def advance_main(self, new_files: dict[str, bytes]) -> str:
        tsha = f"tree-{len(self.trees) + 1}"
        self.trees[tsha] = dict(new_files)
        csha = f"commit-{len(self.commits) + 1}"
        self.commits[csha] = {
            "tree": {"sha": tsha},
            "parents": [self.branch_heads["main"]],
        }
        self.branches["main"] = dict(new_files)
        self.branch_heads["main"] = csha
        return csha

    def _start_server(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                del format, args

            def _send(self, code: int, payload: Any) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                return json.loads(self.rfile.read(length).decode("utf-8"))

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                path = parsed.path
                qs = parse_qs(parsed.query)
                ref_prefix = "/repos/ThermalEng/pIES-Market/git/ref/heads/"
                if path.startswith(ref_prefix):
                    branch = path.removeprefix(ref_prefix)
                    csha = outer.branch_heads.get(branch)
                    if csha is None:
                        self._send(404, {"message": "Branch not found"})
                        return
                    self._send(200, {"ref": f"refs/heads/{branch}", "object": {"sha": csha}})
                    return
                if path.startswith("/repos/ThermalEng/pIES-Market/git/commits/"):
                    csha = path.rsplit("/", 1)[-1]
                    commit = outer.commits.get(csha)
                    if commit is None:
                        self._send(404, {"message": "Commit not found"})
                        return
                    self._send(200, {"sha": csha, **commit})
                    return
                if path.startswith("/repos/ThermalEng/pIES-Market/git/trees/"):
                    tsha = path.removeprefix("/repos/ThermalEng/pIES-Market/git/trees/")
                    tree = outer.trees.get(tsha)
                    if tree is None:
                        self._send(404, {"message": "Tree not found"})
                        return
                    entries = [
                        {
                            "path": rel_path,
                            "mode": "100644",
                            "type": "blob",
                            "sha": outer.register_blob(content),
                        }
                        for rel_path, content in sorted(tree.items())
                    ]
                    self._send(200, {"sha": tsha, "tree": entries})
                    return
                if path.startswith("/repos/ThermalEng/pIES-Market/git/blobs/"):
                    bsha = path.removeprefix("/repos/ThermalEng/pIES-Market/git/blobs/")
                    raw = outer.blobs.get(bsha)
                    if raw is None:
                        self._send(404, {"message": "Blob not found"})
                        return
                    self._send(
                        200,
                        {
                            "sha": bsha,
                            "encoding": "base64",
                            "content": base64.b64encode(raw).decode("ascii"),
                        },
                    )
                    return
                if path.startswith("/repos/ThermalEng/pIES-Market/contents/"):
                    file_path = path.removeprefix("/repos/ThermalEng/pIES-Market/contents/")
                    ref = qs.get("ref", ["main"])[0]
                    if ref in outer.commits:
                        tree = outer.trees[outer.commits[ref]["tree"]["sha"]]
                    else:
                        tree = outer.branches.get(ref, {})
                    if file_path not in tree:
                        self._send(404, {"message": "Not Found"})
                        return
                    raw = tree[file_path]
                    self._send(
                        200,
                        {
                            "path": file_path,
                            "sha": outer.register_blob(raw),
                            "content": base64.b64encode(raw).decode("ascii"),
                        },
                    )
                    if file_path == "publishers.yaml" and outer.on_read_publishers_done is not None:
                        cb = outer.on_read_publishers_done
                        outer.on_read_publishers_done = None
                        cb()
                    return
                if path == "/repos/ThermalEng/pIES-Market/pulls":
                    state = qs.get("state", ["open"])[0]
                    head = qs.get("head", [""])[0]
                    matches = [
                        pr
                        for pr in outer.pulls
                        if pr["state"] == state and (not head or pr["head_label"] == head)
                    ]
                    self._send(200, matches)
                    return
                self._send(404, {"message": f"Unhandled GET {path}"})

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                data = self._read_json()
                outer.write_calls.append(("POST", path))
                if path == "/repos/ThermalEng/pIES-Market/git/blobs":
                    bsha = outer.register_blob(base64.b64decode(data["content"]))
                    self._send(201, {"sha": bsha})
                    return
                if path == "/repos/ThermalEng/pIES-Market/git/trees":
                    if outer.fail_next_tree:
                        outer.fail_next_tree = False
                        self._send(500, {"message": "Simulated tree write failure"})
                        return
                    base_tree = dict(outer.trees[str(data["base_tree"])])
                    for entry in data["tree"]:
                        base_tree[entry["path"]] = outer.blobs[entry["sha"]]
                    tsha = f"tree-{len(outer.trees) + 1}"
                    outer.trees[tsha] = base_tree
                    self._send(201, {"sha": tsha})
                    return
                if path == "/repos/ThermalEng/pIES-Market/git/commits":
                    csha = f"commit-{len(outer.commits) + 1}"
                    outer.commits[csha] = {
                        "tree": {"sha": str(data["tree"])},
                        "parents": list(data["parents"]),
                    }
                    self._send(201, {"sha": csha})
                    return
                if path == "/repos/ThermalEng/pIES-Market/git/refs":
                    if outer.on_before_create_ref is not None:
                        cb = outer.on_before_create_ref
                        outer.on_before_create_ref = None
                        cb()
                    branch = str(data["ref"]).removeprefix("refs/heads/")
                    if branch in outer.branches:
                        self._send(422, {"message": "Reference already exists"})
                        return
                    csha = str(data["sha"])
                    outer.branches[branch] = dict(outer.trees[outer.commits[csha]["tree"]["sha"]])
                    outer.branch_heads[branch] = csha
                    self._send(201, {"ref": data["ref"], "object": {"sha": csha}})
                    return
                if path == "/repos/ThermalEng/pIES-Market/pulls":
                    if outer.fail_next_pull:
                        outer.fail_next_pull = False
                        self._send(502, {"message": "Simulated PR creation failure"})
                        return
                    num = len(outer.pulls) + 1
                    pr = {
                        "number": num,
                        "html_url": f"https://github.com/ThermalEng/pIES-Market/pull/{num}",
                        "state": "open",
                        "head": data["head"],
                        "head_label": f"ThermalEng:{data['head']}",
                        "base": data["base"],
                        "title": data["title"],
                        "body": data["body"],
                    }
                    outer.pulls.append(pr)
                    self._send(201, pr)
                    return
                self._send(404, {"message": f"Unhandled POST {path}"})

            def do_PATCH(self) -> None:
                path = urlparse(self.path).path
                self._read_json()
                outer.write_calls.append(("PATCH", path))
                self._send(404, {"message": f"Unexpected PATCH {path}"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> _MockGitHubServer:
        self._start_server()
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._server.shutdown()
        self._server.server_close()


def _sample_device_yaml(device_id: str = "acme.device.chp_unit") -> str:
    return (
        "schema: ies.device-model\n"
        'schema_version: "1.0.0"\n'
        "device:\n"
        f"  id: {device_id}\n"
        "  names:\n"
        "    zh-CN: 热电联产机组\n"
        "    en-US: CHP Unit\n"
        "properties:\n"
        "  elec_eff:\n"
        "    value: 0.4\n"
        '    unit: "1"\n'
        "interfaces:\n"
        "  fuel_in:\n"
        "    type: in\n"
        "    carrier: gas\n"
        "    unit: kW\n"
        "    valid_range: {minimum: 0, maximum: null}\n"
        "  electric_out:\n"
        "    type: out\n"
        "    carrier: electricity\n"
        "    unit: kW\n"
        "    valid_range: {minimum: 0, maximum: null}\n"
        "equations:\n"
        "  variables: {}\n"
        "  relations:\n"
        "    - id: gen\n"
        '      expression: "electric_out[t] = fuel_in[t] * elec_eff"\n'
    )


def _build_plugin_zip(
    tmp_path: Path,
    *,
    plugin_id: str = "acme.evaluator.carbon",
    version: str = "1.0.0",
    sentinel: Path | None = None,
    readme_extra: str = "",
) -> Path:
    src = tmp_path / f"{plugin_id}-{version}-src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "plugin.yaml").write_text(
        (
            "schema: ies.algorithm-plugin\n"
            'schema_version: "1.0.0"\n'
            f"plugin_id: {plugin_id}\n"
            f'version: "{version}"\n'
            "kind: evaluator\n"
            "name:\n"
            "  zh_cn: ACME 碳排放评价器\n"
            "  en_us: ACME Carbon Evaluator\n"
            "description: ACME evaluator plugin.\n"
            "license: MPL-2.0\n"
            "compatibility:\n"
            '  pies: ">=1.2.0,<2.0.0"\n'
            '  sdk: ">=1.2.0,<2.0.0"\n'
            "consumes:\n"
            "  - ies.engineering-solution@1.0.0\n"
            "produces:\n"
            "  - ies.evaluation-report@1.0.0\n"
            "runtime:\n"
            "  language: python\n"
            '  python: "3.12"\n'
            "entrypoint: evaluate:evaluate\n"
            "parameters_schema:\n"
            "  type: object\n"
            "  additionalProperties: false\n"
            "  properties: {}\n"
            "resources:\n"
            "  default: {cpu: 1, memory_mb: 128, timeout_seconds: 60, process_limit: 8}\n"
            "  maximum: {cpu: 2, memory_mb: 256, timeout_seconds: 120, process_limit: 16}\n"
            "tests:\n"
            "  - id: minimal\n"
            "    in: tests/minimal/in\n"
            "    expected: tests/minimal/expected\n"
        ),
        encoding="utf-8",
    )
    import_side_effect = (
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n"
        if sentinel is not None
        else ""
    )
    (src / "evaluate.py").write_text(
        f"{import_side_effect}def evaluate(ctx):\n    pass\n",
        encoding="utf-8",
    )
    (src / "README.md").write_text(
        f"# {plugin_id}\n\nContract `ies.evaluation-report@1.0.0`.\n{readme_extra}",
        encoding="utf-8",
    )
    (src / "LICENSE").write_text("MPL-2.0\n", encoding="utf-8")
    (src / "tests/minimal/in").mkdir(parents=True)
    (src / "tests/minimal/expected").mkdir(parents=True)
    write_json_file(src / "tests/minimal/in/request.json", {"parameters": {}})
    write_json_file(
        src / "tests/minimal/expected/evaluation_report.json",
        {
            "schema": "ies.evaluation-report",
            "schema_version": "1.0.0",
            "title": "Stub",
            "evaluator": f"{plugin_id}@{version}",
            "metrics": [],
            "charts": [],
            "tables": [],
        },
    )
    out_zip = tmp_path / f"{plugin_id}-{version}.algorithm.zip"
    pack_plugin_package(src, output_path=out_zip, run_validate=False, require_market_fields=True)
    return out_zip


def _make_settings(smtp_port: int, github_url: str, **overrides: Any) -> GatewaySettings:
    kwargs: dict[str, Any] = {
        "market_hmac_secret": SECRET,
        "market_repo": "ThermalEng/pIES-Market",
        "market_base_branch": "main",
        "github_token": "ghp_test_minimal_token",
        "github_api_base": github_url,
        "smtp_host": "127.0.0.1",
        "smtp_port": smtp_port,
        "smtp_from": "noreply@pies-market.local",
        "otp_rate_limit_max": 5,
        "otp_rate_limit_window_seconds": 600,
        **overrides,
    }
    return GatewaySettings(**kwargs)


def _authenticate_email(client: TestClient, smtp: _LocalSmtpServer, email: str) -> str:
    req_resp = client.post("/v1/otp/request", json={"email": email})
    assert req_resp.status_code == 200, req_resp.text
    challenge = req_resp.json()["challenge_token"]
    otp = smtp.last_otp()
    assert otp not in req_resp.text
    assert email.lower() not in req_resp.text
    ver_resp = client.post(
        "/v1/otp/verify",
        json={"email": email, "otp_code": otp, "challenge_token": challenge},
    )
    assert ver_resp.status_code == 200, ver_resp.text
    return str(ver_resp.json()["submission_token"])


def test_e2e_device_and_plugin_submissions_and_idempotency(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    initial_pub = 'schema: ies.publishers\nschema_version: "1.0.0"\npublishers: {}\n'
    author_email = "Maintainer@Acme.Example"
    sentinel = tmp_path / "untrusted_plugin_executed.flag"
    plugin_zip = _build_plugin_zip(tmp_path, sentinel=sentinel)

    with _LocalSmtpServer() as smtp, _MockGitHubServer(initial_pub) as gh:
        app = create_app(_make_settings(smtp.port, gh.base_url))
        client = TestClient(app)

        assert client.get("/healthz").json() == {"status": "ok"}
        token = _authenticate_email(client, smtp, author_email)

        # 1) Submit device model -> 201 PR #1 claiming publisher 'acme'
        dev_resp = client.post(
            "/v1/submissions",
            headers={"Authorization": f"Bearer {token}"},
            data={
                "kind": "device_model",
                "revision": "1",
                "license": "MPL-2.0",
                "notes": "Compatible with ies.device-model@1.0.0",
            },
            files={"file": ("acme.device.chp_unit-r1.device.yaml", _sample_device_yaml().encode())},
        )
        assert dev_resp.status_code == 201, dev_resp.text
        dev_body = dev_resp.json()
        assert dev_body["duplicate"] is False
        assert dev_body["pr_number"] == 1
        assert dev_body["branch"] == "submit/acme.device.chp_unit-r1"

        # 2) Repeat exact device submission -> 200 idempotent duplicate returning PR #1
        dup_resp = client.post(
            "/v1/submissions",
            headers={"Authorization": f"Bearer {token}"},
            data={"kind": "device_model", "revision": "1"},
            files={"file": ("acme.device.chp_unit-r1.device.yaml", _sample_device_yaml().encode())},
        )
        assert dup_resp.status_code == 200, dup_resp.text
        assert dup_resp.json()["duplicate"] is True
        assert dup_resp.json()["pr_number"] == 1
        assert len(gh.pulls) == 1

        # Simulate PR #1 merged to main (so acme is now in main publishers.yaml)
        gh.advance_main(gh.branches[dev_body["branch"]])

        # 3) Submit algorithm plugin ZIP (with matching license & contract@semver) -> 201 PR #2
        plug_resp = client.post(
            "/v1/submissions",
            headers={"Authorization": f"Bearer {token}"},
            data={"kind": "evaluator", "license": "MPL-2.0"},
            files={"file": ("acme.evaluator.carbon-1.0.0.algorithm.zip", plugin_zip.read_bytes())},
        )
        assert plug_resp.status_code == 201, plug_resp.text
        plug_body = plug_resp.json()
        assert plug_body["duplicate"] is False
        assert plug_body["pr_number"] == 2
        assert plug_body["branch"] == "submit/acme.evaluator.carbon-1.0.0"
        assert not sentinel.exists()

        # Materialize the PR #2 tree on disk and validate with E3 check_market_pr!
        checkout = tmp_path / "pr_checkout"
        for rel_path, content in gh.branches[plug_body["branch"]].items():
            dest = checkout / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)

        summary = check_market_pr(checkout, docker_selftest=False)
        assert summary["ok"] is True
        assert summary["artifacts_checked"] == 2
        assert summary["publishers_count"] == 1

        for pr in gh.pulls:
            assert "maintainer" not in pr["body"].lower()
            assert "acme.example" not in pr["body"].lower()
        assert "maintainer" not in caplog.text.lower()
        assert "acme.example" not in caplog.text.lower()


def test_atomic_git_retry_after_tree_and_pr_failures(tmp_path: Path) -> None:
    del tmp_path
    initial_pub = 'schema: ies.publishers\nschema_version: "1.0.0"\npublishers: {}\n'
    with _LocalSmtpServer() as smtp, _MockGitHubServer(initial_pub) as gh:
        client = TestClient(create_app(_make_settings(smtp.port, gh.base_url)))
        token = issue_submission_token(email="dev@acme.org", hmac_secret=SECRET)
        branch = "submit/acme.device.chp_unit-r1"

        # 1) Tree failure must NOT create an orphaned branch ref
        gh.fail_next_tree = True
        r1 = client.post(
            "/v1/submissions",
            headers={"Authorization": f"Bearer {token}"},
            data={"kind": "device_model", "revision": "1"},
            files={"file": ("acme.device.chp_unit-r1.device.yaml", _sample_device_yaml().encode())},
        )
        assert r1.status_code == 502
        assert branch not in gh.branches

        # 2) PR creation failure leaves branch without open PR -> retry reuses branch cleanly (no PATCH)
        gh.fail_next_pull = True
        r2 = client.post(
            "/v1/submissions",
            headers={"Authorization": f"Bearer {token}"},
            data={"kind": "device_model", "revision": "1"},
            files={"file": ("acme.device.chp_unit-r1.device.yaml", _sample_device_yaml().encode())},
        )
        assert r2.status_code == 502
        assert branch in gh.branches
        assert gh.pulls == []

        r3 = client.post(
            "/v1/submissions",
            headers={"Authorization": f"Bearer {token}"},
            data={"kind": "device_model", "revision": "1"},
            files={"file": ("acme.device.chp_unit-r1.device.yaml", _sample_device_yaml().encode())},
        )
        assert r3.status_code == 201, r3.text
        assert r3.json()["pr_number"] == 1
        assert len(gh.pulls) == 1
        assert all(method != "PATCH" for method, _ in gh.write_calls)


def test_pinned_base_commit_and_concurrent_conflict_409(tmp_path: Path) -> None:
    del tmp_path
    owner_digest = compute_publisher_email_digest("dev@acme.org", hmac_secret=SECRET)
    concurrent_digest = compute_publisher_email_digest("beta@beta.org", hmac_secret=SECRET)
    initial_pub = 'schema: ies.publishers\nschema_version: "1.0.0"\npublishers: {}\n'
    concurrent_pub = (
        'schema: ies.publishers\nschema_version: "1.0.0"\n'
        "publishers:\n"
        "  beta:\n"
        f'    email_digest: "{concurrent_digest}"\n'
        '    created_at: "2026-11-28T00:00:00Z"\n'
        "    status: active\n"
    )
    with _LocalSmtpServer() as smtp, _MockGitHubServer(initial_pub) as gh:
        client = TestClient(create_app(_make_settings(smtp.port, gh.base_url)))
        token = issue_submission_token(email="dev@acme.org", hmac_secret=SECRET)
        branch = "submit/acme.device.chp_unit-r1"

        # 1) Base branch moves after fetch_publishers_registry -> PR branch stays pinned to base-sha-001
        gh.on_read_publishers_done = lambda: gh.advance_main(
            {"publishers.yaml": concurrent_pub.encode("utf-8")}
        )
        r_pinned = client.post(
            "/v1/submissions",
            headers={"Authorization": f"Bearer {token}"},
            data={"kind": "device_model", "revision": "1"},
            files={"file": ("acme.device.chp_unit-r1.device.yaml", _sample_device_yaml().encode())},
        )
        assert r_pinned.status_code == 201, r_pinned.text
        branch_commit = gh.branch_heads[branch]
        assert gh.commits[branch_commit]["parents"] == ["base-sha-001"]
        assert gh.branches["main"]["publishers.yaml"] == concurrent_pub.encode("utf-8")

        # Simulate PR merged and acme registered on main for next tests
        gh.advance_main(
            {
                "publishers.yaml": (
                    'schema: ies.publishers\nschema_version: "1.0.0"\n'
                    "publishers:\n"
                    "  acme:\n"
                    f'    email_digest: "{owner_digest}"\n'
                    '    created_at: "2026-11-28T00:00:00Z"\n'
                    "    status: active\n"
                ).encode()
            }
        )

        # 2) Concurrent PR race on same branch with DIFFERENT YAML -> 409 without overwriting PR #2
        branch_r2 = "submit/acme.device.chp_unit-r2"
        winner_yaml = _sample_device_yaml().replace("0.4", "0.45").encode("utf-8")
        loser_yaml = _sample_device_yaml().replace("0.4", "0.50").encode("utf-8")

        def _inject_raced_pr() -> None:
            tsha = "tree-race-winner"
            gh.trees[tsha] = {
                **gh.branches["main"],
                "artifacts/acme/acme.device.chp_unit/r2/acme.device.chp_unit.device.yaml": winner_yaml,
            }
            csha = "commit-race-winner"
            gh.commits[csha] = {"tree": {"sha": tsha}, "parents": [gh.branch_heads["main"]]}
            gh.branches[branch_r2] = dict(gh.trees[tsha])
            gh.branch_heads[branch_r2] = csha
            gh.pulls.append(
                {
                    "number": 99,
                    "html_url": "https://github.com/ThermalEng/pIES-Market/pull/99",
                    "state": "open",
                    "head": branch_r2,
                    "head_label": f"ThermalEng:{branch_r2}",
                    "base": "main",
                    "title": "Raced PR",
                    "body": "{}",
                }
            )

        gh.on_before_create_ref = _inject_raced_pr
        r_conflict = client.post(
            "/v1/submissions",
            headers={"Authorization": f"Bearer {token}"},
            data={"kind": "device_model", "revision": "2"},
            files={"file": ("acme.device.chp_unit-r2.device.yaml", loser_yaml)},
        )
        assert r_conflict.status_code == 409, r_conflict.text
        assert (
            gh.branches[branch_r2]["artifacts/acme/acme.device.chp_unit/r2/acme.device.chp_unit.device.yaml"]
            == winner_yaml
        )

        # 3) Orphaned branch without an open PR and different payload -> 409 (no force-overwrite)
        branch_r3 = "submit/acme.device.chp_unit-r3"
        tsha_orphan = "tree-orphan-r3"
        gh.trees[tsha_orphan] = {
            **gh.branches["main"],
            "artifacts/acme/acme.device.chp_unit/r3/acme.device.chp_unit.device.yaml": winner_yaml,
        }
        csha_orphan = "commit-orphan-r3"
        gh.commits[csha_orphan] = {"tree": {"sha": tsha_orphan}, "parents": [gh.branch_heads["main"]]}
        gh.branches[branch_r3] = dict(gh.trees[tsha_orphan])
        gh.branch_heads[branch_r3] = csha_orphan
        r_orphan_conflict = client.post(
            "/v1/submissions",
            headers={"Authorization": f"Bearer {token}"},
            data={"kind": "device_model", "revision": "3"},
            files={"file": ("acme.device.chp_unit-r3.device.yaml", loser_yaml)},
        )
        assert r_orphan_conflict.status_code == 409, r_orphan_conflict.text
        assert gh.branch_heads[branch_r3] == csha_orphan
        assert all(method != "PATCH" for method, _ in gh.write_calls)


def test_zip_entry_count_and_non_allowlist_email_scan(tmp_path: Path) -> None:
    owner_digest = compute_publisher_email_digest("owner@acme.org", hmac_secret=SECRET)
    pub_yaml = (
        'schema: ies.publishers\nschema_version: "1.0.0"\n'
        "publishers:\n"
        "  acme:\n"
        f'    email_digest: "{owner_digest}"\n'
        '    created_at: "2026-11-28T00:00:00Z"\n'
        "    status: active\n"
    )
    with _LocalSmtpServer() as smtp, _MockGitHubServer(pub_yaml) as gh:
        cfg = _make_settings(smtp.port, gh.base_url, max_zip_entries=256)
        client = TestClient(create_app(cfg))
        owner_token = issue_submission_token(email="owner@acme.org", hmac_secret=SECRET)

        # 1) >256 0-byte ZIP entries rejected with 413 before extraction
        many_zip = tmp_path / "many.algorithm.zip"
        with zipfile.ZipFile(many_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for idx in range(260):
                zf.writestr(f"empty_{idx:03d}.txt", b"")
        r_many = client.post(
            "/v1/submissions",
            headers={"Authorization": f"Bearer {owner_token}"},
            data={"kind": "evaluator"},
            files={"file": ("many.algorithm.zip", many_zip.read_bytes())},
        )
        assert r_many.status_code == 413

        # 2) Email inside contacts.csv or extensionless text file rejected (422)
        base_csv_zip = _build_plugin_zip(tmp_path / "csv_leak", plugin_id="acme.evaluator.csvleak")
        with zipfile.ZipFile(base_csv_zip, "a") as zf:
            zf.writestr("contacts.csv", "name,email\nMaintainer,maintainer@example.com\n")
        assert (
            client.post(
                "/v1/submissions",
                headers={"Authorization": f"Bearer {owner_token}"},
                data={"kind": "evaluator"},
                files={"file": ("csvleak.algorithm.zip", base_csv_zip.read_bytes())},
            ).status_code
            == 422
        )

        base_extless_zip = _build_plugin_zip(tmp_path / "extless_leak", plugin_id="acme.evaluator.extleak")
        with zipfile.ZipFile(base_extless_zip, "a") as zf:
            zf.writestr("CONTACTS", "Maintainer: maintainer@example.com\n")
        assert (
            client.post(
                "/v1/submissions",
                headers={"Authorization": f"Bearer {owner_token}"},
                data={"kind": "evaluator"},
                files={"file": ("extleak.algorithm.zip", base_extless_zip.read_bytes())},
            ).status_code
            == 422
        )

        # 3) @<semver> inside extensionless file and binary bytes with NUL are allowed
        clean_extless_zip = _build_plugin_zip(tmp_path / "extless_ok", plugin_id="acme.evaluator.extok")
        with zipfile.ZipFile(clean_extless_zip, "a") as zf:
            zf.writestr("CONTRACTS", "ies.evaluation-report@1.0.0\n")
            zf.writestr("weights.bin", b"\x00\xff\xfe\x80maintainer@example.com")
        r_ok = client.post(
            "/v1/submissions",
            headers={"Authorization": f"Bearer {owner_token}"},
            data={"kind": "evaluator"},
            files={"file": ("acme.evaluator.extok-1.0.0.algorithm.zip", clean_extless_zip.read_bytes())},
        )
        assert r_ok.status_code == 201, r_ok.text


def test_auth_token_failures_smtp_sanitization_and_rate_limiting(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with _LocalSmtpServer() as smtp, _MockGitHubServer(
        'schema: ies.publishers\nschema_version: "1.0.0"\npublishers: {}\n'
    ) as gh:
        cfg = _make_settings(smtp.port, gh.base_url, otp_rate_limit_max=2)
        client = TestClient(create_app(cfg))

        # Invalid/expired/tampered OTP & submission tokens -> 401
        expired_challenge = issue_otp_challenge(
            email="dev@acme.org",
            otp_code="123456",
            hmac_secret=SECRET,
            ttl_seconds=1,
            now=int(time.time()) - 10,
        )
        assert (
            client.post(
                "/v1/otp/verify",
                json={"email": "dev@acme.org", "otp_code": "123456", "challenge_token": expired_challenge},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/v1/otp/verify",
                json={
                    "email": "dev@acme.org",
                    "otp_code": "123456",
                    "challenge_token": f"{expired_challenge}x",
                },
            ).status_code
            == 401
        )

        expired_sub = issue_submission_token(
            email="dev@acme.org",
            hmac_secret=SECRET,
            ttl_seconds=1,
            now=int(time.time()) - 10,
        )
        for bad_bearer in (expired_challenge, expired_sub, "bad.token"):
            resp = client.post(
                "/v1/submissions",
                headers={"Authorization": f"Bearer {bad_bearer}"},
                data={"kind": "device_model", "revision": "1"},
                files={"file": ("dev.device.yaml", _sample_device_yaml().encode())},
            )
            assert resp.status_code == 401

        assert (
            client.post(
                "/v1/submissions",
                data={"kind": "device_model", "revision": "1"},
                files={"file": ("dev.device.yaml", _sample_device_yaml().encode())},
            ).status_code
            == 401
        )

        # Rate limit (max=2) on /v1/otp/request and /v1/otp/verify -> 429
        assert client.post("/v1/otp/request", json={"email": "u1@acme.org"}).status_code == 200
        assert client.post("/v1/otp/request", json={"email": "u1@acme.org"}).status_code == 200
        limited = client.post("/v1/otp/request", json={"email": "u1@acme.org"})
        assert limited.status_code == 429
        assert "Retry-After" in limited.headers

        verify_client = TestClient(create_app(cfg))
        verify_payload = {"email": "u2@acme.org", "otp_code": "000000", "challenge_token": "bad.tok"}
        assert verify_client.post("/v1/otp/verify", json=verify_payload).status_code == 401
        assert verify_client.post("/v1/otp/verify", json=verify_payload).status_code == 401
        verify_limited = verify_client.post("/v1/otp/verify", json=verify_payload)
        assert verify_limited.status_code == 429
        assert "Retry-After" in verify_limited.headers

        # SMTPRecipientsRefused must be sanitized (zero plaintext email in HTTP response or logs)
        class BrokenSmtp:
            def __init__(self, *args: object, **kwargs: object) -> None:
                del args, kwargs

            def __enter__(self) -> BrokenSmtp:
                return self

            def __exit__(self, *args: object) -> None:
                del args

            def send_message(self, msg: object) -> None:
                del msg
                raise smtplib.SMTPRecipientsRefused({"secret_user@private.org": (550, b"5.1.1 Refused")})

        monkeypatch.setattr(smtplib, "SMTP", BrokenSmtp)
        smtp_client = TestClient(create_app(cfg))
        caplog.clear()
        smtp_err = smtp_client.post("/v1/otp/request", json={"email": "secret_user@private.org"})
        assert smtp_err.status_code == 502
        assert "secret_user@private.org" not in smtp_err.text
        assert "secret_user@private.org" not in caplog.text


def test_publisher_conflicts_overrides_email_guard_and_size_bounds(tmp_path: Path) -> None:
    owner_digest = compute_publisher_email_digest("owner@acme.org", hmac_secret=SECRET)
    other_digest = compute_publisher_email_digest("frozen@beta.org", hmac_secret=SECRET)
    pub_yaml = (
        'schema: ies.publishers\nschema_version: "1.0.0"\n'
        "publishers:\n"
        "  acme:\n"
        f'    email_digest: "{owner_digest}"\n'
        '    created_at: "2026-11-28T00:00:00Z"\n'
        "    status: active\n"
        "  beta:\n"
        f'    email_digest: "{other_digest}"\n'
        '    created_at: "2026-11-28T00:00:00Z"\n'
        "    status: suspended\n"
    )
    with _LocalSmtpServer() as smtp, _MockGitHubServer(pub_yaml) as gh:
        cfg = _make_settings(smtp.port, gh.base_url, max_upload_bytes=16384)
        client = TestClient(create_app(cfg))

        owner_token = issue_submission_token(email="owner@acme.org", hmac_secret=SECRET)
        intruder_token = issue_submission_token(email="intruder@evil.org", hmac_secret=SECRET)
        suspended_token = issue_submission_token(email="frozen@beta.org", hmac_secret=SECRET)
        ns = generate_public_namespace()

        # 1) Wrong publisher digest -> 403
        assert (
            client.post(
                "/v1/submissions",
                headers={"Authorization": f"Bearer {intruder_token}"},
                data={"kind": "device_model", "revision": "1"},
                files={"file": ("acme.device.chp.device.yaml", _sample_device_yaml().encode())},
            ).status_code
            == 403
        )

        # 2) Suspended publisher -> 403
        beta_yaml = _sample_device_yaml("beta.device.chp").encode()
        assert (
            client.post(
                "/v1/submissions",
                headers={"Authorization": f"Bearer {suspended_token}"},
                data={"kind": "device_model", "revision": "1"},
                files={"file": ("beta.device.chp.device.yaml", beta_yaml)},
            ).status_code
            == 403
        )

        # 3) Reserved ies.*, admin.*, user.<ns>.* -> 403
        for forbidden_id in ("ies.device.chp", "admin.device.chp", f"user.{ns}.device.chp"):
            forb_yaml = _sample_device_yaml(forbidden_id).encode()
            r_forb = client.post(
                "/v1/submissions",
                headers={"Authorization": f"Bearer {intruder_token}"},
                data={"kind": "device_model", "revision": "1"},
                files={"file": (f"{forbidden_id}.device.yaml", forb_yaml)},
            )
            assert r_forb.status_code == 403

        # 4) Malformed YAML -> sanitized 422 (never 500)
        assert (
            client.post(
                "/v1/submissions",
                headers={"Authorization": f"Bearer {intruder_token}"},
                data={"kind": "device_model", "revision": "1"},
                files={"file": ("bad.device.yaml", b"schema: [unclosed")},
            ).status_code
            == 422
        )

        # 5) Plaintext email inside description / device YAML / plugin README rejected (422)
        assert (
            client.post(
                "/v1/submissions",
                headers={"Authorization": f"Bearer {owner_token}"},
                data={"kind": "device_model", "revision": "1", "description": "mail me dev@evil.org"},
                files={"file": ("acme.device.chp.device.yaml", _sample_device_yaml().encode())},
            ).status_code
            == 422
        )
        leak_zip = _build_plugin_zip(
            tmp_path / "leak",
            plugin_id="acme.evaluator.leak",
            readme_extra="\nContact: leak@example.com\n",
        )
        assert (
            client.post(
                "/v1/submissions",
                headers={"Authorization": f"Bearer {owner_token}"},
                data={"kind": "evaluator"},
                files={"file": ("acme.evaluator.leak-1.0.0.algorithm.zip", leak_zip.read_bytes())},
            ).status_code
            == 422
        )

        # 6) Conflicting license override & invalid compatibility_json rejected (422)
        clean_zip = _build_plugin_zip(tmp_path / "clean", plugin_id="acme.evaluator.clean")
        assert (
            client.post(
                "/v1/submissions",
                headers={"Authorization": f"Bearer {owner_token}"},
                data={"kind": "evaluator", "license": "MIT"},
                files={"file": ("acme.evaluator.clean-1.0.0.algorithm.zip", clean_zip.read_bytes())},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/submissions",
                headers={"Authorization": f"Bearer {owner_token}"},
                data={"kind": "evaluator", "compatibility_json": '{"pies": 123}'},
                files={"file": ("acme.evaluator.clean-1.0.0.algorithm.zip", clean_zip.read_bytes())},
            ).status_code
            == 422
        )

        # 7) ZIP-bomb expanded size check rejects BEFORE extraction -> 413
        bomb_zip = tmp_path / "bomb.algorithm.zip"
        with zipfile.ZipFile(bomb_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("huge.txt", b"A" * (cfg.max_upload_bytes * 5))
        assert bomb_zip.stat().st_size < cfg.max_upload_bytes
        assert (
            client.post(
                "/v1/submissions",
                headers={"Authorization": f"Bearer {owner_token}"},
                data={"kind": "evaluator"},
                files={"file": ("bomb.algorithm.zip", bomb_zip.read_bytes())},
            ).status_code
            == 413
        )

        # 8) Pre-parse middleware rejects raw request body > max_upload_bytes with 413
        assert (
            client.post(
                "/v1/submissions",
                headers={"Authorization": f"Bearer {owner_token}"},
                data={"kind": "device_model", "revision": "1"},
                files={"file": ("big.device.yaml", b"x" * 20000)},
            ).status_code
            == 413
        )
        assert gh.write_calls == []


def test_zero_iesplan_and_database_imports() -> None:
    py_files = sorted(GATEWAY_SRC_DIR.rglob("*.py"))
    assert py_files
    for py_file in py_files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    assert root not in _FORBIDDEN_MODULE_PREFIXES, f"{py_file} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".", 1)[0]
                assert root not in _FORBIDDEN_MODULE_PREFIXES, f"{py_file} imports {node.module}"
