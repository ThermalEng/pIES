# pIES Market Submission Gateway (`pies-market-gateway`)

Thin, database-free FastAPI submission gateway bridging external contributors to GitHub PRs in `ThermalEng/pIES-Market`.

- **Stateless auth**: HMAC-SHA256 OTP challenges (`/v1/otp/request` -> `/v1/otp/verify`) and short-lived submission tokens (`/v1/submissions`) via `pies-sdk`.
- **Zero plaintext email retention**: emails are used only to send OTPs via SMTP and derive HMAC-SHA256 digests (`publishers.yaml`, `ies.market-submission@1.0.0`); branches, commits, PRs, and logs never contain plaintext emails.
- **Read-only preflight**: validates `.device.yaml` (`pies_market.validate_device_model_mapping`) and `.algorithm.zip` (`pies_sdk.validate_plugin_package(..., run_tests=False, require_market_fields=True)`) without executing untrusted plugin code.
- **Deterministic PRs**: opens idempotent PRs on `submit/<artifact_id>-<rN_or_version>` against `GITHUB_API_BASE` using a minimal-privilege token.
