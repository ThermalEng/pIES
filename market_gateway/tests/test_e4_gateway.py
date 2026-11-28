"""Unit and local sandbox E2E tests for stateless `pies_market_gateway`."""

from __future__ import annotations

import ast
import base64
import json
import logging
import re
import socketserver
import threading
import time
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

REPO_ROOT = Path(__file__).resolve().parents[2]
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
    """Real local HTTP mock GitHub REST API tracking branches, committed files, and open PRs."""

    def __init__(self, initial_publishers_yaml: str) -> None:
        self.branches: dict[str, dict[str, bytes]] = {
            "main": {"publishers.yaml": initial_publishers_yaml.encode("utf-8")}
        }
        self.pulls: list[dict[str, Any]] = []
        self.write_calls: list[tuple[str, str]] = []
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
                if path == "/repos/ThermalEng/pIES-Market/git/ref/heads/main":
                    self._send(200, {"ref": "refs/heads/main", "object": {"sha": "base-sha-001"}})
                    return
                if path.startswith("/repos/ThermalEng/pIES-Market/contents/"):
                    file_path = path.removeprefix("/repos/ThermalEng/pIES-Market/contents/")
                    ref = qs.get("ref", ["main"])[0]
                    tree = outer.branches.get(ref, {})
                    if file_path not in tree:
                        self._send(404, {"message": "Not Found"})
                        return
                    raw = tree[file_path]
                    self._send(
                        200,
                        {
                            "path": file_path,
                            "sha": f"blob-sha-{len(raw)}",
                            "content": base64.b64encode(raw).decode("ascii"),
                        },
                    )
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
                if path == "/repos/ThermalEng/pIES-Market/git/refs":
                    branch = str(data["ref"]).removeprefix("refs/heads/")
                    if branch in outer.branches:
                        self._send(422, {"message": "Reference already exists"})
                        return
                    outer.branches[branch] = dict(outer.branches["main"])
                    self._send(201, {"ref": data["ref"], "object": {"sha": data["sha"]}})
                    return
                if path == "/repos/ThermalEng/pIES-Market/pulls":
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

            def do_PUT(self) -> None:
                path = urlparse(self.path).path
                data = self._read_json()
                outer.write_calls.append(("PUT", path))
                if path.startswith("/repos/ThermalEng/pIES-Market/contents/"):
                    file_path = path.removeprefix("/repos/ThermalEng/pIES-Market/contents/")
                    branch = str(data["branch"])
                    outer.branches[branch][file_path] = base64.b64decode(data["content"])
                    self._send(201, {"content": {"path": file_path}})
                    return
                self._send(404, {"message": f"Unhandled PUT {path}"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> _MockGitHubServer:
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
    (src / "README.md").write_text(f"# {plugin_id}\n", encoding="utf-8")
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
            data={"kind": "device_model", "revision": "1", "license": "MPL-2.0"},
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
        gh.branches["main"] = dict(gh.branches[dev_body["branch"]])

        # 3) Submit algorithm plugin ZIP -> 201 PR #2 WITHOUT executing plugin code
        plug_resp = client.post(
            "/v1/submissions",
            headers={"Authorization": f"Bearer {token}"},
            data={"kind": "evaluator"},
            files={"file": ("acme.evaluator.carbon-1.0.0.algorithm.zip", plugin_zip.read_bytes())},
        )
        assert plug_resp.status_code == 201, plug_resp.text
        plug_body = plug_resp.json()
        assert plug_body["duplicate"] is False
        assert plug_body["pr_number"] == 2
        assert plug_body["branch"] == "submit/acme.evaluator.carbon-1.0.0"
        assert not sentinel.exists()

        # Materialize the PR #2 tree + device artifact on disk and validate with E3 check_market_pr!
        checkout = tmp_path / "pr_checkout"
        for rel_path, content in gh.branches[plug_body["branch"]].items():
            dest = checkout / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
            if rel_path.endswith((".yaml", ".md")):
                assert "@" not in content.decode("utf-8")

        summary = check_market_pr(checkout, docker_selftest=False)
        assert summary["ok"] is True
        assert summary["artifacts_checked"] == 2
        assert summary["publishers_count"] == 1

        # Zero plaintext email '@' across PR metadata, branches, HTTP responses, or logs
        for pr in gh.pulls:
            assert "maintainer" not in pr["body"].lower()
            assert "@" not in pr["body"]
            assert "@" not in pr["title"]
            assert "@" not in pr["head"]
        assert "maintainer" not in caplog.text.lower()
        assert "@" not in caplog.text


def test_auth_token_failures_and_rate_limiting(tmp_path: Path) -> None:
    del tmp_path
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

        # Purpose confusion & expired submission token -> 401
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

        # Missing Bearer -> 401
        assert (
            client.post(
                "/v1/submissions",
                data={"kind": "device_model", "revision": "1"},
                files={"file": ("dev.device.yaml", _sample_device_yaml().encode())},
            ).status_code
            == 401
        )

        # Rate limit (max=2) on /v1/otp/request and /v1/otp/verify -> 3rd attempt returns 429
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


def test_publisher_conflicts_reserved_namespaces_and_preflight_blocks_github(tmp_path: Path) -> None:
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
        cfg = _make_settings(smtp.port, gh.base_url, max_upload_bytes=2048)
        client = TestClient(create_app(cfg))

        intruder_token = issue_submission_token(email="intruder@evil.org", hmac_secret=SECRET)
        suspended_token = issue_submission_token(email="frozen@beta.org", hmac_secret=SECRET)
        ns = generate_public_namespace()

        # 1) Wrong publisher digest claiming 'acme' -> 403
        r_conflict = client.post(
            "/v1/submissions",
            headers={"Authorization": f"Bearer {intruder_token}"},
            data={"kind": "device_model", "revision": "1"},
            files={"file": ("acme.device.chp.device.yaml", _sample_device_yaml().encode())},
        )
        assert r_conflict.status_code == 403
        assert gh.write_calls == []

        # 2) Suspended publisher 'beta' -> 403
        r_susp = client.post(
            "/v1/submissions",
            headers={"Authorization": f"Bearer {suspended_token}"},
            data={"kind": "device_model", "revision": "1"},
            files={
                "file": (
                    "beta.device.chp.device.yaml",
                    _sample_device_yaml("beta.device.chp").encode(),
                )
            },
        )
        assert r_susp.status_code == 403
        assert gh.write_calls == []

        # 3) Reserved ies.*, admin.*, and user.<ns>.* rejected with 403 BEFORE GitHub write calls
        for forbidden_id in ("ies.device.chp", "admin.device.chp", f"user.{ns}.device.chp"):
            r_forb = client.post(
                "/v1/submissions",
                headers={"Authorization": f"Bearer {intruder_token}"},
                data={"kind": "device_model", "revision": "1"},
                files={
                    "file": (
                        f"{forbidden_id}.device.yaml",
                        _sample_device_yaml(forbidden_id).encode(),
                    )
                },
            )
            assert r_forb.status_code == 403, (forbidden_id, r_forb.text)
        assert gh.write_calls == []

        # 4) Invalid artifact YAML/ZIP rejected at preflight (422) BEFORE GitHub writes
        bad_dev = _sample_device_yaml("gamma.device.chp").replace(
            "elec_eff:", "price:\n    value: 99\n    unit: CNY\n  elec_eff:"
        )
        assert (
            client.post(
                "/v1/submissions",
                headers={"Authorization": f"Bearer {intruder_token}"},
                data={"kind": "device_model", "revision": "1"},
                files={"file": ("gamma.device.chp.device.yaml", bad_dev.encode())},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/submissions",
                headers={"Authorization": f"Bearer {intruder_token}"},
                data={"kind": "evaluator"},
                files={"file": ("broken.algorithm.zip", b"not-a-zip")},
            ).status_code
            == 422
        )
        assert gh.write_calls == []

        # 5) Oversized upload (> max_upload_bytes) -> 413
        assert (
            client.post(
                "/v1/submissions",
                headers={"Authorization": f"Bearer {intruder_token}"},
                data={"kind": "device_model", "revision": "1"},
                files={"file": ("big.device.yaml", b"x" * 3000)},
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
