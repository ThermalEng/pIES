"""Environment configuration for the stateless pIES Market submission gateway."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _parse_bool(raw: str | None, *, default: bool = False) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(raw: str | None, *, default: int, minimum: int = 1) -> int:
    if raw is None or not raw.strip():
        return default
    val = int(raw.strip())
    if val < minimum:
        raise ValueError(f"Expected integer >= {minimum}, got {val}")
    return val


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    """Configuration for stateless OTP verification and GitHub PR creation."""

    market_hmac_secret: str
    market_repo: str = "ThermalEng/pIES-Market"
    market_base_branch: str = "main"
    github_token: str = ""
    github_api_base: str = "https://api.github.com"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = False
    smtp_from: str = "noreply@pies-market.local"
    otp_ttl_seconds: int = 600
    submission_token_ttl_seconds: int = 1800
    otp_rate_limit_max: int = 5
    otp_rate_limit_window_seconds: int = 600
    max_upload_bytes: int = 20_000_000
    http_timeout_seconds: float = 30.0


def load_settings_from_env() -> GatewaySettings:
    """Load gateway settings from environment variables, requiring `MARKET_HMAC_SECRET`."""
    secret = os.environ.get("MARKET_HMAC_SECRET", "")
    if not secret.strip():
        raise RuntimeError("MARKET_HMAC_SECRET must be set to a non-empty value")
    return GatewaySettings(
        market_hmac_secret=secret,
        market_repo=os.environ.get("MARKET_REPO", "ThermalEng/pIES-Market").strip()
        or "ThermalEng/pIES-Market",
        market_base_branch=os.environ.get("MARKET_BASE_BRANCH", "main").strip() or "main",
        github_token=os.environ.get("GITHUB_TOKEN", "").strip(),
        github_api_base=(
            os.environ.get("GITHUB_API_BASE", "https://api.github.com").strip().rstrip("/")
            or "https://api.github.com"
        ),
        smtp_host=os.environ.get("SMTP_HOST", "localhost").strip() or "localhost",
        smtp_port=_parse_int(os.environ.get("SMTP_PORT"), default=1025, minimum=1),
        smtp_username=os.environ.get("SMTP_USERNAME", "").strip(),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        smtp_use_tls=_parse_bool(os.environ.get("SMTP_USE_TLS"), default=False),
        smtp_from=os.environ.get("SMTP_FROM", "noreply@pies-market.local").strip()
        or "noreply@pies-market.local",
        otp_ttl_seconds=_parse_int(os.environ.get("OTP_TTL_SECONDS"), default=600, minimum=1),
        submission_token_ttl_seconds=_parse_int(
            os.environ.get("SUBMISSION_TOKEN_TTL_SECONDS"), default=1800, minimum=1
        ),
        otp_rate_limit_max=_parse_int(os.environ.get("OTP_RATE_LIMIT_MAX"), default=5, minimum=1),
        otp_rate_limit_window_seconds=_parse_int(
            os.environ.get("OTP_RATE_LIMIT_WINDOW_SECONDS"), default=600, minimum=1
        ),
        max_upload_bytes=_parse_int(
            os.environ.get("MAX_UPLOAD_BYTES"), default=20_000_000, minimum=1
        ),
        http_timeout_seconds=float(os.environ.get("HTTP_TIMEOUT_SECONDS", "30.0")),
    )
