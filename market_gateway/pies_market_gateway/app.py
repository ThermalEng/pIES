"""Stateless FastAPI app for pIES Market OTP verification and PR submission."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import tempfile
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import uvicorn
import yaml
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse
from pies_market import validate_device_model_mapping
from pies_sdk import (
    MARKET_SUBMISSION_SCHEMA,
    MARKET_SUBMISSION_VERSION,
    PLUGIN_KINDS,
    PUBLISHERS_SCHEMA,
    PUBLISHERS_VERSION,
    RESERVED_PUBLISHERS,
    PiesSdkError,
    PluginValidationError,
    compute_publisher_email_digest,
    issue_otp_challenge,
    issue_submission_token,
    load_plugin_manifest,
    load_safe_yaml,
    normalize_email,
    parse_artifact_id,
    parse_semver,
    validate_market_submission,
    validate_plugin_package,
    validate_publishers_registry,
    verify_otp_challenge,
    verify_submission_token,
)
from pydantic import BaseModel, ConfigDict

from pies_market_gateway.config import GatewaySettings, load_settings_from_env
from pies_market_gateway.github_client import (
    GitHubGatewayError,
    GitHubSubmissionClient,
    SubmissionConflictError,
)
from pies_market_gateway.mailer import MailerDeliveryError, send_otp_email
from pies_market_gateway.rate_limit import RateLimitExceededError, SlidingWindowRateLimiter

_COMPAT_CLAUSE_RE = re.compile(r"^(>=|<=|>|<|==|!=)?\s*(\d+\.\d+\.\d+)$")
_EMAIL_ADDR_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class _BodyLimitExceeded(Exception):
    """Internal control-flow signal raised before multipart parsing finishes."""


class _RequestBodyLimitMiddleware:
    """ASGI middleware bounding raw request body bytes before multipart form parsing."""

    def __init__(self, app: Any, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        raw_cl = headers.get(b"content-length")
        if raw_cl is not None:
            try:
                if int(raw_cl.decode("ascii")) > self.max_body_bytes:
                    await self._send_413(send)
                    return
            except ValueError:
                pass

        received = 0
        response_started = False

        async def bounded_receive() -> dict[str, Any]:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise _BodyLimitExceeded()
            return message

        async def tracking_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, bounded_receive, tracking_send)
        except _BodyLimitExceeded:
            if not response_started:
                await self._send_413(send)

    async def _send_413(self, send: Any) -> None:
        payload = json.dumps(
            {"detail": f"Upload exceeds MAX_UPLOAD_BYTES ({self.max_body_bytes})"}
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


class OtpRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str


class OtpVerifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    otp_code: str
    challenge_token: str


def _client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must use Bearer token",
        )
    return token.strip()


def _device_localized_names(raw_names: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(raw_names.get("zh-CN"), str):
        out["zh_cn"] = raw_names["zh-CN"]
    if isinstance(raw_names.get("en-US"), str):
        out["en_us"] = raw_names["en-US"]
    if not out:
        first = next(iter(raw_names.values()))
        out["en_us"] = str(first)
    return out


def _assert_no_public_email(text: str, *, label: str) -> None:
    lowered = text.lower()
    if "mailto:" in lowered or _EMAIL_ADDR_RE.search(text):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Public {label} must not contain plaintext email addresses",
        )


def _validate_compat_override(raw_json: str) -> dict[str, str]:
    try:
        parsed = json.loads(raw_json)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid compatibility_json: {exc}",
        ) from exc
    if not isinstance(parsed, Mapping) or not parsed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="compatibility_json must be a non-empty JSON object",
        )
    extra = sorted(set(parsed) - {"pies", "sdk"})
    if extra:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"compatibility_json contains unsupported keys: {extra!r}",
        )
    out: dict[str, str] = {}
    for target, expr in parsed.items():
        if not isinstance(expr, str) or not expr.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"compatibility_json.{target} must be a non-empty string",
            )
        for clause in (part.strip() for part in expr.split(",")):
            match = _COMPAT_CLAUSE_RE.fullmatch(clause)
            if not match:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid compatibility_json.{target} constraint: {expr!r}",
                )
            parse_semver(match.group(2))
        out[str(target)] = expr.strip()
    return out


def _check_zip_bounds(
    zip_path: Path,
    *,
    max_expanded_bytes: int,
    max_entries: int = 256,
) -> None:
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            infos = zf.infolist()
            if len(infos) > max_entries:
                raise HTTPException(
                    status_code=413,
                    detail=f"ZIP entry count ({len(infos)}) exceeds limit ({max_entries})",
                )
            total_uncompressed = 0
            for info in infos:
                if info.file_size < 0 or info.compress_size < 0:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="ZIP entry has negative size metadata",
                    )
                if info.file_size > max_expanded_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"ZIP entry {info.filename!r} expanded size exceeds limit",
                    )
                total_uncompressed += info.file_size
                if total_uncompressed > max_expanded_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"ZIP total uncompressed size exceeds limit ({max_expanded_bytes})",
                    )
                if info.compress_size > 0 and info.file_size > 1_000_000:
                    if info.file_size / info.compress_size > 100:
                        raise HTTPException(
                            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="ZIP compression ratio exceeds safety threshold",
                        )
    except zipfile.BadZipFile as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid plugin ZIP archive: {exc}",
        ) from exc


def _read_plugin_meta_from_zip(zip_path: Path) -> tuple[Any, bytes | None]:
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            raw_bytes = zf.read(info)
            if b"\x00" in raw_bytes:
                continue
            try:
                raw_text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                continue
            _assert_no_public_email(raw_text, label=f"plugin archive file {info.filename!r}")
        with tempfile.TemporaryDirectory(prefix="pies-gw-manifest-") as tmp_dir:
            zf.extract("plugin.yaml", tmp_dir)
            manifest = load_plugin_manifest(Path(tmp_dir) / "plugin.yaml", check_compat=True)
        readme_bytes: bytes | None = None
        for name in ("README.md", "readme.md"):
            if name in zf.namelist():
                readme_bytes = zf.read(name)
                break
        return manifest, readme_bytes


def _reject_reserved_or_user_id(artifact_id: str, kind: str) -> Any:
    try:
        parsed = parse_artifact_id(artifact_id, kind=kind, allow_user_namespace=False)
    except PluginValidationError as exc:
        msg = str(exc).lower()
        if "forbidden in public market" in msg or "reserved" in msg:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    if parsed.scope == "official" or parsed.publisher in RESERVED_PUBLISHERS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Reserved publisher namespace {parsed.publisher!r} cannot be submitted via gateway",
        )
    return parsed


def create_app(
    settings: GatewaySettings | None = None,
    *,
    github_client: GitHubSubmissionClient | None = None,
) -> FastAPI:
    """Create the stateless FastAPI submission gateway application."""
    cfg = settings or load_settings_from_env()
    gh = github_client or GitHubSubmissionClient(cfg)
    otp_req_limiter = SlidingWindowRateLimiter(
        max_requests=cfg.otp_rate_limit_max,
        window_seconds=cfg.otp_rate_limit_window_seconds,
    )
    otp_verify_limiter = SlidingWindowRateLimiter(
        max_requests=cfg.otp_rate_limit_max,
        window_seconds=cfg.otp_rate_limit_window_seconds,
    )

    app = FastAPI(title="pIES Market Submission Gateway", version="1.0.0")
    app.add_middleware(_RequestBodyLimitMiddleware, max_body_bytes=cfg.max_upload_bytes)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/otp/request")
    def request_otp(payload: OtpRequestBody, request: Request) -> dict[str, Any]:
        try:
            normalized = normalize_email(payload.email)
            email_digest = compute_publisher_email_digest(
                normalized,
                hmac_secret=cfg.market_hmac_secret,
            )
        except PluginValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

        ip = _client_ip(request)
        try:
            otp_req_limiter.check_and_record(f"ip:{ip}", f"email:{email_digest}")
        except RateLimitExceededError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=str(exc),
                headers={"Retry-After": str(exc.retry_after_seconds)},
            ) from exc

        otp_code = f"{secrets.randbelow(1_000_000):06d}"
        try:
            send_otp_email(
                settings=cfg,
                to_email=normalized,
                otp_code=otp_code,
                ttl_seconds=cfg.otp_ttl_seconds,
            )
        except MailerDeliveryError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Verification email delivery failed",
            ) from exc

        challenge_token = issue_otp_challenge(
            email=normalized,
            otp_code=otp_code,
            hmac_secret=cfg.market_hmac_secret,
            ttl_seconds=cfg.otp_ttl_seconds,
        )
        return {
            "challenge_token": challenge_token,
            "expires_in": cfg.otp_ttl_seconds,
        }

    @app.post("/v1/otp/verify")
    def verify_otp(payload: OtpVerifyBody, request: Request) -> dict[str, Any]:
        try:
            normalized = normalize_email(payload.email)
            email_digest = compute_publisher_email_digest(
                normalized,
                hmac_secret=cfg.market_hmac_secret,
            )
        except PluginValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

        ip = _client_ip(request)
        try:
            otp_verify_limiter.check_and_record(f"ip:{ip}", f"email:{email_digest}")
        except RateLimitExceededError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=str(exc),
                headers={"Retry-After": str(exc.retry_after_seconds)},
            ) from exc

        try:
            verify_otp_challenge(
                payload.challenge_token,
                email=normalized,
                otp_code=payload.otp_code,
                hmac_secret=cfg.market_hmac_secret,
            )
        except PluginValidationError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

        sub_token = issue_submission_token(
            email=normalized,
            hmac_secret=cfg.market_hmac_secret,
            ttl_seconds=cfg.submission_token_ttl_seconds,
        )
        return {
            "submission_token": sub_token,
            "expires_in": cfg.submission_token_ttl_seconds,
        }

    def _require_submission_claims(authorization: str | None) -> dict[str, Any]:
        token = _extract_bearer_token(authorization)
        try:
            return verify_submission_token(token, hmac_secret=cfg.market_hmac_secret)
        except PluginValidationError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    @app.post("/v1/submissions", status_code=status.HTTP_201_CREATED)
    async def submit_artifact(
        request: Request,
        kind: Annotated[str, Form()],
        file: Annotated[UploadFile, File()],
        authorization: Annotated[str | None, Header()] = None,
        revision: Annotated[int | None, Form()] = None,
        name_zh_cn: Annotated[str | None, Form()] = None,
        name_en_us: Annotated[str | None, Form()] = None,
        description: Annotated[str | None, Form()] = None,
        license: Annotated[str | None, Form()] = None,
        compatibility_json: Annotated[str | None, Form()] = None,
        readme_markdown: Annotated[str | None, Form()] = None,
        notes: Annotated[str | None, Form()] = None,
    ) -> Any:
        del request
        claims = _require_submission_claims(authorization)
        submitter_digest = str(claims["email_digest"])

        if kind != "device_model" and kind not in PLUGIN_KINDS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unsupported artifact kind: {kind!r}",
            )

        for field_name, val in (
            ("name_zh_cn", name_zh_cn),
            ("name_en_us", name_en_us),
            ("description", description),
            ("license", license),
            ("readme_markdown", readme_markdown),
            ("notes", notes),
        ):
            if val:
                _assert_no_public_email(val, label=field_name)

        raw_bytes = await file.read()
        if not raw_bytes:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Empty upload file")
        if len(raw_bytes) > cfg.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Upload exceeds MAX_UPLOAD_BYTES ({cfg.max_upload_bytes})",
            )
        sha256 = hashlib.sha256(raw_bytes).hexdigest()

        compat_override: dict[str, str] | None = None
        if compatibility_json is not None and compatibility_json.strip():
            compat_override = _validate_compat_override(compatibility_json)

        files_to_commit: dict[str, bytes] = {}
        with tempfile.TemporaryDirectory(prefix="pies-gw-preflight-") as tmp_dir:
            tmp_root = Path(tmp_dir)
            if kind == "device_model":
                if revision is None or isinstance(revision, bool) or revision < 1:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="device_model submission requires integer revision >= 1",
                    )
                try:
                    yaml_text = raw_bytes.decode("utf-8")
                    _assert_no_public_email(yaml_text, label="device_model YAML")
                    raw_doc = load_safe_yaml(yaml_text)
                    device_doc = validate_device_model_mapping(raw_doc, allow_user_namespace=False)
                except (UnicodeDecodeError, PiesSdkError) as exc:
                    msg = str(exc).lower()
                    if isinstance(exc, PluginValidationError) and (
                        "forbidden in public market" in msg or "reserved" in msg
                    ):
                        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=str(exc),
                    ) from exc

                artifact_id = str(device_doc["device"]["id"])
                parsed_id = _reject_reserved_or_user_id(artifact_id, kind)
                publisher = parsed_id.publisher
                ver_label = f"r{revision}"
                package_filename = f"{artifact_id}-{ver_label}.device.yaml"
                names_map = _device_localized_names(device_doc["device"]["names"])
                if name_zh_cn and name_zh_cn.strip():
                    names_map["zh_cn"] = name_zh_cn.strip()
                if name_en_us and name_en_us.strip():
                    names_map["en_us"] = name_en_us.strip()
                desc_text = (description or "").strip() or f"Device model {artifact_id}"
                lic_text = (license or "").strip() or "MPL-2.0"
                compat_map = compat_override or {"pies": ">=1.2.0,<2.0.0"}
                title_label = names_map.get("en_us") or artifact_id
                readme_content = (
                    (readme_markdown or "").strip()
                    or f"# {title_label} (`{artifact_id}` {ver_label})\n\n{desc_text}\n"
                )
                meta_doc: dict[str, Any] = {
                    "schema": "ies.market-artifact-meta",
                    "schema_version": "1.0.0",
                    "id": artifact_id,
                    "kind": "device_model",
                    "revision": revision,
                    "name": names_map,
                    "description": desc_text,
                    "publisher": publisher,
                    "license": lic_text,
                    "compatibility": compat_map,
                    "delisted": False,
                }
                base_dir = f"artifacts/{publisher}/{artifact_id}/{ver_label}"
                files_to_commit[f"{base_dir}/meta.yaml"] = yaml.safe_dump(
                    meta_doc, sort_keys=False, allow_unicode=True
                ).encode("utf-8")
                files_to_commit[f"{base_dir}/{artifact_id}.device.yaml"] = raw_bytes
                files_to_commit[f"{base_dir}/README.md"] = readme_content.encode("utf-8")
                submission_payload: dict[str, Any] = {
                    "schema": MARKET_SUBMISSION_SCHEMA,
                    "schema_version": MARKET_SUBMISSION_VERSION,
                    "artifact_id": artifact_id,
                    "kind": "device_model",
                    "revision": revision,
                    "publisher": publisher,
                    "publisher_email_digest": submitter_digest,
                    "package_filename": package_filename,
                    "sha256": sha256,
                }
            else:
                if revision is not None:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="Algorithm plugin submission must not specify revision",
                    )
                zip_path = tmp_root / "submission.algorithm.zip"
                zip_path.write_bytes(raw_bytes)
                _check_zip_bounds(
                    zip_path,
                    max_expanded_bytes=cfg.max_upload_bytes * 4,
                    max_entries=cfg.max_zip_entries,
                )
                try:
                    # Strictly read-only preflight: never import or execute plugin code in gateway.
                    validate_plugin_package(
                        zip_path,
                        run_tests=False,
                        require_market_fields=True,
                    )
                    manifest, readme_bytes = _read_plugin_meta_from_zip(zip_path)
                except PiesSdkError as exc:
                    msg = str(exc)
                    if "public market" in msg.lower() or "reserved" in msg.lower():
                        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=msg) from exc
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=msg,
                    ) from exc

                if manifest.kind != kind:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Uploaded plugin.yaml kind {manifest.kind!r} does not match {kind!r}",
                    )
                if license is not None and license.strip() and license.strip() != manifest.license:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"Submitted license {license.strip()!r} conflicts with plugin.yaml "
                            f"license {manifest.license!r}"
                        ),
                    )
                artifact_id = manifest.plugin_id
                parsed_id = _reject_reserved_or_user_id(artifact_id, kind)
                publisher = parsed_id.publisher
                ver_label = manifest.version
                package_filename = f"{artifact_id}-{ver_label}.algorithm.zip"

                names_map = dict(manifest.name)
                if name_zh_cn and name_zh_cn.strip():
                    names_map["zh_cn"] = name_zh_cn.strip()
                if name_en_us and name_en_us.strip():
                    names_map["en_us"] = name_en_us.strip()
                desc_text = (description or "").strip() or manifest.description
                lic_text = manifest.license
                compat_map = compat_override or dict(manifest.compatibility)

                meta_doc = {
                    "schema": "ies.market-artifact-meta",
                    "schema_version": "1.0.0",
                    "id": artifact_id,
                    "kind": kind,
                    "version": ver_label,
                    "name": names_map,
                    "description": desc_text,
                    "publisher": publisher,
                    "license": lic_text,
                    "compatibility": compat_map,
                    "delisted": False,
                }
                base_dir = f"artifacts/{publisher}/{artifact_id}/{ver_label}"
                files_to_commit[f"{base_dir}/meta.yaml"] = yaml.safe_dump(
                    meta_doc, sort_keys=False, allow_unicode=True
                ).encode("utf-8")
                files_to_commit[f"{base_dir}/{package_filename}"] = raw_bytes
                if readme_markdown and readme_markdown.strip():
                    files_to_commit[f"{base_dir}/README.md"] = readme_markdown.strip().encode("utf-8")
                elif readme_bytes is not None:
                    files_to_commit[f"{base_dir}/README.md"] = readme_bytes

                submission_payload = {
                    "schema": MARKET_SUBMISSION_SCHEMA,
                    "schema_version": MARKET_SUBMISSION_VERSION,
                    "artifact_id": artifact_id,
                    "kind": kind,
                    "version": ver_label,
                    "publisher": publisher,
                    "publisher_email_digest": submitter_digest,
                    "package_filename": package_filename,
                    "sha256": sha256,
                }

        if notes and notes.strip():
            submission_payload["notes"] = notes.strip()

        try:
            validated_submission = validate_market_submission(submission_payload)
        except PiesSdkError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

        for rel_path, file_bytes in files_to_commit.items():
            if not rel_path.endswith(".zip"):
                _assert_no_public_email(
                    file_bytes.decode("utf-8", errors="replace"),
                    label=f"committed file {rel_path!r}",
                )

        try:
            registry_snapshot = gh.fetch_publishers_registry()
        except (GitHubGatewayError, PiesSdkError) as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

        publishers_doc = registry_snapshot.publishers_doc
        existing_pub = publishers_doc["publishers"].get(publisher)
        if existing_pub is not None:
            if existing_pub.get("status") != "active":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Publisher {publisher!r} is not active",
                )
            if not hmac.compare_digest(existing_pub["email_digest"], submitter_digest):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Publisher namespace {publisher!r} is owned by another verified digest",
                )
        else:
            updated_publishers = dict(publishers_doc["publishers"])
            updated_publishers[publisher] = {
                "email_digest": submitter_digest,
                "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": "active",
            }
            new_pub_doc = validate_publishers_registry(
                {
                    "schema": PUBLISHERS_SCHEMA,
                    "schema_version": PUBLISHERS_VERSION,
                    "publishers": updated_publishers,
                }
            )
            files_to_commit["publishers.yaml"] = yaml.safe_dump(
                new_pub_doc, sort_keys=False, allow_unicode=True
            ).encode("utf-8")

        branch_name = f"submit/{artifact_id}-{ver_label}"
        try:
            pr_result = gh.create_submission_pr(
                branch_name=branch_name,
                files=files_to_commit,
                base_commit_sha=registry_snapshot.base_commit_sha,
                base_tree_sha=registry_snapshot.base_tree_sha,
                publisher=publisher,
                submission_prefix=f"{base_dir}/",
                submission_metadata=validated_submission,
                pr_title=f"market: submit {artifact_id} ({ver_label})",
                commit_message=f"market: submit {artifact_id} {ver_label}",
            )
        except SubmissionConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except GitHubGatewayError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

        body = {
            "duplicate": pr_result.duplicate,
            "pr_number": pr_result.pr_number,
            "pr_url": pr_result.pr_url,
            "branch": pr_result.branch,
            "artifact_id": artifact_id,
            "publisher": publisher,
            "sha256": sha256,
        }
        if pr_result.duplicate:
            return JSONResponse(status_code=status.HTTP_200_OK, content=body)
        return body

    return app


def main() -> None:
    uvicorn.run("pies_market_gateway.app:create_app", factory=True, host="0.0.0.0", port=8020)
