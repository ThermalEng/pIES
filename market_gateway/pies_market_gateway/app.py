"""Stateless FastAPI app for pIES Market OTP verification and PR submission."""

from __future__ import annotations

import hashlib
import hmac
import json
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
    PluginValidationError,
    compute_publisher_email_digest,
    issue_otp_challenge,
    issue_submission_token,
    load_plugin_manifest,
    load_safe_yaml,
    normalize_email,
    parse_artifact_id,
    validate_market_submission,
    validate_plugin_package,
    validate_publishers_registry,
    verify_otp_challenge,
    verify_submission_token,
)
from pydantic import BaseModel, ConfigDict

from pies_market_gateway.config import GatewaySettings, load_settings_from_env
from pies_market_gateway.github_client import GitHubGatewayError, GitHubSubmissionClient
from pies_market_gateway.mailer import send_otp_email
from pies_market_gateway.rate_limit import RateLimitExceededError, SlidingWindowRateLimiter


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


def _read_plugin_meta_from_zip(zip_path: Path) -> tuple[Any, bytes | None]:
    with zipfile.ZipFile(zip_path, "r") as zf:
        with tempfile.TemporaryDirectory(prefix="pies-gw-manifest-") as tmp_dir:
            zf.extract("plugin.yaml", tmp_dir)
            manifest = load_plugin_manifest(Path(tmp_dir) / "plugin.yaml", check_compat=True)
        readme_bytes: bytes | None = None
        for name in ("README.md", "readme.md"):
            if name in zf.namelist():
                readme_bytes = zf.read(name)
                break
        return manifest, readme_bytes


async def _read_upload_bounded(upload: UploadFile, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(65536):
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_ENTITY_TOO_LARGE
                if hasattr(status, "HTTP_413_CONTENT_ENTITY_TOO_LARGE")
                else 413,
                detail=f"Upload exceeds MAX_UPLOAD_BYTES ({max_bytes})",
            )
        chunks.append(chunk)
    return b"".join(chunks)


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
        send_otp_email(
            settings=cfg,
            to_email=normalized,
            otp_code=otp_code,
            ttl_seconds=cfg.otp_ttl_seconds,
        )
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

        raw_bytes = await _read_upload_bounded(file, cfg.max_upload_bytes)
        if not raw_bytes:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Empty upload file")
        sha256 = hashlib.sha256(raw_bytes).hexdigest()

        compat_override: dict[str, str] | None = None
        if compatibility_json is not None and compatibility_json.strip():
            try:
                parsed_compat = json.loads(compatibility_json)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid compatibility_json: {exc}",
                ) from exc
            if not isinstance(parsed_compat, Mapping) or not parsed_compat:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="compatibility_json must be a non-empty JSON object",
                )
            compat_override = {str(k): str(v) for k, v in parsed_compat.items()}

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
                    raw_doc = load_safe_yaml(yaml_text)
                    device_doc = validate_device_model_mapping(raw_doc, allow_user_namespace=False)
                except (UnicodeDecodeError, PluginValidationError) as exc:
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
                try:
                    # Strictly read-only preflight: never import or execute plugin code in gateway.
                    validate_plugin_package(
                        zip_path,
                        run_tests=False,
                        require_market_fields=True,
                    )
                    manifest, readme_bytes = _read_plugin_meta_from_zip(zip_path)
                except PluginValidationError as exc:
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
                lic_text = (license or "").strip() or manifest.license
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
            if "@" in notes:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Submission notes must not contain plaintext '@' email addresses",
                )
            submission_payload["notes"] = notes.strip()

        try:
            validated_submission = validate_market_submission(submission_payload)
        except PluginValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

        try:
            publishers_doc, publishers_sha = gh.fetch_publishers_registry()
        except (GitHubGatewayError, PluginValidationError) as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

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
                publishers_sha=publishers_sha,
                submission_metadata=validated_submission,
                pr_title=f"market: submit {artifact_id} ({ver_label})",
                commit_message=f"market: submit {artifact_id} {ver_label}",
            )
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
