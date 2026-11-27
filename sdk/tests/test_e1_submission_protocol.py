"""Unit tests for E1 stateless market submission protocol and publishers registry."""

from __future__ import annotations

import pytest

from pies_sdk import (
    MARKET_SUBMISSION_SCHEMA,
    MARKET_SUBMISSION_VERSION,
    PUBLISHERS_SCHEMA,
    PUBLISHERS_VERSION,
    PluginValidationError,
    compute_publisher_email_digest,
    issue_otp_challenge,
    issue_submission_token,
    normalize_email,
    validate_market_submission,
    validate_publishers_registry,
    verify_otp_challenge,
    verify_submission_token,
)

SECRET = "test-market-hmac-secret"


def test_email_normalization_and_hmac_digest_zero_plaintext() -> None:
    assert normalize_email("  Alice.Example+tag@Example.COM ") == "alice.example+tag@example.com"
    d1 = compute_publisher_email_digest("Alice@Example.COM", hmac_secret=SECRET)
    d2 = compute_publisher_email_digest(" alice@example.com ", hmac_secret=SECRET)
    assert d1 == d2
    assert len(d1) == 64
    assert "alice" not in d1

    with pytest.raises(PluginValidationError):
        normalize_email("not-an-email")
    with pytest.raises(PluginValidationError):
        normalize_email("bad\nemail@example.com")


def test_stateless_otp_challenge_and_submission_token_roundtrip() -> None:
    challenge = issue_otp_challenge(
        email="Maintainer@Acme.org",
        otp_code="482910",
        hmac_secret=SECRET,
        ttl_seconds=300,
        now=1000,
    )
    assert "maintainer" not in challenge.lower()
    claims = verify_otp_challenge(
        challenge,
        email="maintainer@acme.org",
        otp_code="482910",
        hmac_secret=SECRET,
        now=1100,
    )
    assert claims["email_digest"] == compute_publisher_email_digest(
        "maintainer@acme.org", hmac_secret=SECRET
    )

    with pytest.raises(PluginValidationError, match="Invalid OTP code"):
        verify_otp_challenge(
            challenge,
            email="maintainer@acme.org",
            otp_code="000000",
            hmac_secret=SECRET,
            now=1100,
        )
    with pytest.raises(PluginValidationError, match="email mismatch"):
        verify_otp_challenge(
            challenge,
            email="other@acme.org",
            otp_code="482910",
            hmac_secret=SECRET,
            now=1100,
        )
    with pytest.raises(PluginValidationError, match="expired"):
        verify_otp_challenge(
            challenge,
            email="maintainer@acme.org",
            otp_code="482910",
            hmac_secret=SECRET,
            now=1301,
        )

    sub_token = issue_submission_token(
        email="maintainer@acme.org",
        hmac_secret=SECRET,
        ttl_seconds=600,
        now=2000,
    )
    assert "maintainer" not in sub_token.lower()
    sub_claims = verify_submission_token(
        sub_token,
        hmac_secret=SECRET,
        expected_email="Maintainer@Acme.org",
        now=2200,
    )
    assert sub_claims["email_digest"] == claims["email_digest"]

    # Purpose confusion and tampering rejected
    with pytest.raises(PluginValidationError, match="purpose"):
        verify_submission_token(challenge, hmac_secret=SECRET, now=1100)
    with pytest.raises(PluginValidationError, match="purpose"):
        verify_otp_challenge(
            sub_token,
            email="maintainer@acme.org",
            otp_code="482910",
            hmac_secret=SECRET,
            now=2200,
        )
    with pytest.raises(PluginValidationError, match="signature"):
        verify_submission_token(sub_token + "x", hmac_secret=SECRET, now=2200)
    with pytest.raises(PluginValidationError, match="expired"):
        verify_submission_token(sub_token, hmac_secret=SECRET, now=2601)


def test_publishers_registry_and_market_submission_contracts() -> None:
    digest = compute_publisher_email_digest("dev@acme.org", hmac_secret=SECRET)
    reg = validate_publishers_registry(
        {
            "schema": PUBLISHERS_SCHEMA,
            "schema_version": PUBLISHERS_VERSION,
            "publishers": {
                "acme": {
                    "email_digest": digest,
                    "created_at": "2026-11-27T00:00:00Z",
                    "status": "active",
                }
            },
        }
    )
    assert reg["publishers"]["acme"]["email_digest"] == digest

    with pytest.raises(PluginValidationError, match="Reserved publisher"):
        validate_publishers_registry(
            {
                "schema": PUBLISHERS_SCHEMA,
                "schema_version": PUBLISHERS_VERSION,
                "publishers": {"ies": {"email_digest": digest}},
            }
        )

    dev_sub = validate_market_submission(
        {
            "schema": MARKET_SUBMISSION_SCHEMA,
            "schema_version": MARKET_SUBMISSION_VERSION,
            "artifact_id": "acme.device.chp",
            "kind": "device_model",
            "revision": 1,
            "publisher": "acme",
            "publisher_email_digest": digest,
            "package_filename": "acme.device.chp-r1.device.yaml",
            "sha256": "a" * 64,
        }
    )
    assert dev_sub["revision"] == 1

    plug_sub = validate_market_submission(
        {
            "schema": MARKET_SUBMISSION_SCHEMA,
            "schema_version": MARKET_SUBMISSION_VERSION,
            "artifact_id": "acme.solver.cbc",
            "kind": "solver",
            "version": "1.0.0",
            "publisher": "acme",
            "publisher_email_digest": digest,
            "package_filename": "acme.solver.cbc-1.0.0.algorithm.zip",
            "sha256": "b" * 64,
        }
    )
    assert plug_sub["version"] == "1.0.0"

    # Plaintext email explicitly forbidden
    with pytest.raises(PluginValidationError, match="plaintext 'email'"):
        validate_market_submission({**plug_sub, "email": "dev@acme.org"})
