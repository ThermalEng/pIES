"""Stateless market submission protocol helpers (zero plaintext emails)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from pies_sdk.contracts import check_finite_json, check_relative_path
from pies_sdk.errors import PluginValidationError
from pies_sdk.market import RESERVED_PUBLISHERS, parse_artifact_id

PUBLISHERS_SCHEMA = "ies.publishers"
PUBLISHERS_VERSION = "1.0.0"

MARKET_SUBMISSION_SCHEMA = "ies.market-submission"
MARKET_SUBMISSION_VERSION = "1.0.0"

_PUBLISHERS_SCHEMA_PATH = Path(__file__).parent / "schema" / "publishers-1.0.0.schema.json"
_SUBMISSION_SCHEMA_PATH = (
    Path(__file__).parent / "schema" / "market-submission-1.0.0.schema.json"
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def load_publishers_schema() -> dict[str, Any]:
    """Load the bundled Draft 2020-12 JSON Schema for ies.publishers@1.0.0."""
    return json.loads(_PUBLISHERS_SCHEMA_PATH.read_text(encoding="utf-8"))


def load_market_submission_schema() -> dict[str, Any]:
    """Load the bundled Draft 2020-12 JSON Schema for ies.market-submission@1.0.0."""
    return json.loads(_SUBMISSION_SCHEMA_PATH.read_text(encoding="utf-8"))


def _require_secret_bytes(hmac_secret: str | bytes) -> bytes:
    if isinstance(hmac_secret, str):
        raw = hmac_secret.encode("utf-8")
    elif isinstance(hmac_secret, (bytes, bytearray)):
        raw = bytes(hmac_secret)
    else:
        raise TypeError("hmac_secret must be str or bytes")
    if not raw:
        raise PluginValidationError("hmac_secret must not be empty")
    return raw


def normalize_email(email: str) -> str:
    """Validate and canonicalize an email address for digest hashing."""
    if not isinstance(email, str):
        raise PluginValidationError("Email must be a string")
    cleaned = email.strip().lower()
    if not cleaned or any(ch.isspace() or ord(ch) < 32 for ch in cleaned):
        raise PluginValidationError("Email contains invalid whitespace or control characters")
    if not _EMAIL_RE.fullmatch(cleaned):
        raise PluginValidationError(f"Invalid email format: {email!r}")
    return cleaned


def compute_publisher_email_digest(email: str, *, hmac_secret: str | bytes) -> str:
    """Compute HMAC-SHA256 hex digest of a normalized email without retaining plaintext."""
    secret = _require_secret_bytes(hmac_secret)
    normalized = normalize_email(email)
    return hmac.new(secret, normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(token_part: str) -> bytes:
    pad_len = (-len(token_part)) % 4
    return base64.urlsafe_b64decode(token_part + ("=" * pad_len))


def _sign_token(payload: Mapping[str, Any], secret: bytes) -> str:
    encoded = _b64url_encode(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    sig = hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{sig}"


def _verify_signed_token(
    token: str,
    *,
    secret: bytes,
    expected_purpose: str,
    now: int | None = None,
) -> dict[str, Any]:
    if not isinstance(token, str) or "." not in token:
        raise PluginValidationError("Malformed signed token")
    encoded, dot, sig = token.partition(".")
    if not dot or not encoded or not sig or "." in sig:
        raise PluginValidationError("Malformed signed token")

    expected_sig = hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        raise PluginValidationError("Invalid token signature")

    try:
        payload = json.loads(_b64url_decode(encoded).decode("utf-8"))
    except Exception as exc:
        raise PluginValidationError(f"Malformed token payload: {exc}") from exc

    if not isinstance(payload, dict):
        raise PluginValidationError("Token payload must be a JSON object")
    if payload.get("purpose") != expected_purpose:
        raise PluginValidationError(
            f"Token purpose mismatch: expected {expected_purpose!r}, got {payload.get('purpose')!r}"
        )

    exp = payload.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int):
        raise PluginValidationError("Token missing integer 'exp'")
    current_time = int(time.time()) if now is None else int(now)
    if current_time > exp:
        raise PluginValidationError("Token has expired")
    return payload


def _otp_digest(secret: bytes, salt: str, email_digest: str, otp_code: str) -> str:
    msg = f"otp:{salt}:{email_digest}:{otp_code}".encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def issue_otp_challenge(
    *,
    email: str,
    otp_code: str,
    hmac_secret: str | bytes,
    ttl_seconds: int = 600,
    now: int | None = None,
    salt: str | None = None,
) -> str:
    """Issue a stateless HMAC-signed OTP challenge token bound to email digest and expiry."""
    if not isinstance(otp_code, str) or not otp_code.strip():
        raise PluginValidationError("otp_code must be a non-empty string")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds < 1:
        raise PluginValidationError("ttl_seconds must be a positive integer")
    secret = _require_secret_bytes(hmac_secret)
    issued_at = int(time.time()) if now is None else int(now)
    challenge_salt = secrets.token_hex(16) if salt is None else salt
    if not isinstance(challenge_salt, str) or not challenge_salt:
        raise PluginValidationError("salt must be a non-empty string")
    email_digest = compute_publisher_email_digest(email, hmac_secret=secret)
    payload = {
        "purpose": "otp_challenge",
        "email_digest": email_digest,
        "salt": challenge_salt,
        "otp_digest": _otp_digest(secret, challenge_salt, email_digest, otp_code),
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
    }
    return _sign_token(payload, secret)


def verify_otp_challenge(
    challenge_token: str,
    *,
    email: str,
    otp_code: str,
    hmac_secret: str | bytes,
    now: int | None = None,
) -> dict[str, Any]:
    """Verify an OTP challenge token against email, otp_code, HMAC signature, and expiry."""
    if not isinstance(otp_code, str) or not otp_code:
        raise PluginValidationError("otp_code must be a non-empty string")
    secret = _require_secret_bytes(hmac_secret)
    payload = _verify_signed_token(
        challenge_token,
        secret=secret,
        expected_purpose="otp_challenge",
        now=now,
    )
    email_digest = compute_publisher_email_digest(email, hmac_secret=secret)
    stored_email_digest = payload.get("email_digest")
    if not isinstance(stored_email_digest, str) or not hmac.compare_digest(
        stored_email_digest, email_digest
    ):
        raise PluginValidationError("OTP challenge email mismatch")

    salt = payload.get("salt")
    stored_otp_digest = payload.get("otp_digest")
    if not isinstance(salt, str) or not isinstance(stored_otp_digest, str):
        raise PluginValidationError("Malformed OTP challenge claims")
    expected_otp_digest = _otp_digest(secret, salt, email_digest, otp_code)
    if not hmac.compare_digest(stored_otp_digest, expected_otp_digest):
        raise PluginValidationError("Invalid OTP code")
    return payload


def issue_submission_token(
    *,
    email: str,
    hmac_secret: str | bytes,
    ttl_seconds: int = 1800,
    now: int | None = None,
    nonce: str | None = None,
) -> str:
    """Issue a short-lived stateless market submission token bound to email digest."""
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds < 1:
        raise PluginValidationError("ttl_seconds must be a positive integer")
    secret = _require_secret_bytes(hmac_secret)
    issued_at = int(time.time()) if now is None else int(now)
    token_nonce = secrets.token_hex(16) if nonce is None else nonce
    if not isinstance(token_nonce, str) or not token_nonce:
        raise PluginValidationError("nonce must be a non-empty string")
    payload = {
        "purpose": "market_submission",
        "email_digest": compute_publisher_email_digest(email, hmac_secret=secret),
        "nonce": token_nonce,
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
    }
    return _sign_token(payload, secret)


def verify_submission_token(
    token: str,
    *,
    hmac_secret: str | bytes,
    expected_email: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Verify a stateless market submission token against HMAC signature, expiry, and optional email."""
    secret = _require_secret_bytes(hmac_secret)
    payload = _verify_signed_token(
        token,
        secret=secret,
        expected_purpose="market_submission",
        now=now,
    )
    stored_email_digest = payload.get("email_digest")
    if not isinstance(stored_email_digest, str) or len(stored_email_digest) != 64:
        raise PluginValidationError("Submission token missing valid email_digest")
    if expected_email is not None:
        expected_digest = compute_publisher_email_digest(expected_email, hmac_secret=secret)
        if not hmac.compare_digest(stored_email_digest, expected_digest):
            raise PluginValidationError("Submission token email mismatch")
    return payload


def validate_publishers_registry(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an ies.publishers@1.0.0 mapping."""
    if not isinstance(data, Mapping):
        raise PluginValidationError("Publishers registry must be a mapping")
    check_finite_json(data, "publishers")

    validator = Draft202012Validator(load_publishers_schema())
    errors = sorted(validator.iter_errors(dict(data)), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        loc = "/".join(str(p) for p in first.absolute_path) or "<root>"
        raise PluginValidationError(f"Publishers registry invalid [{loc}]: {first.message}")

    for pub in data["publishers"]:
        if pub in RESERVED_PUBLISHERS:
            raise PluginValidationError(f"Reserved publisher prefix {pub!r} cannot be registered")
    return dict(data)


def validate_market_submission(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an ies.market-submission@1.0.0 payload, explicitly forbidding plaintext emails."""
    if not isinstance(data, Mapping):
        raise PluginValidationError("Market submission must be a JSON object")
    if "email" in data:
        raise PluginValidationError("Market submission explicitly forbids plaintext 'email'")
    check_finite_json(data, "market_submission")

    validator = Draft202012Validator(load_market_submission_schema())
    errors = sorted(validator.iter_errors(dict(data)), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        loc = "/".join(str(p) for p in first.absolute_path) or "<root>"
        raise PluginValidationError(f"Market submission invalid [{loc}]: {first.message}")

    parsed = parse_artifact_id(data["artifact_id"], kind=data["kind"], allow_user_namespace=False)
    if data["publisher"] != parsed.publisher:
        raise PluginValidationError(
            f"Submission publisher {data['publisher']!r} does not match artifact_id {data['artifact_id']!r}"
        )
    filename = check_relative_path(data["package_filename"], "package_filename")
    if "/" in filename:
        raise PluginValidationError("package_filename must be a basename without directory separators")
    expected_ext = ".device.yaml" if data["kind"] == "device_model" else ".algorithm.zip"
    if not filename.endswith(expected_ext):
        raise PluginValidationError(
            f"package_filename {filename!r} must end with {expected_ext!r} for kind {data['kind']!r}"
        )
    return dict(data)
