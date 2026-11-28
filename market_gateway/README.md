# pIES Market Submission Gateway (`pies-market-gateway`)

Thin, database-free FastAPI submission gateway bridging external contributors to GitHub PRs in `ThermalEng/pIES-Market`.

- **Stateless auth**: HMAC-SHA256 OTP challenges (`/v1/otp/request` -> `/v1/otp/verify`) and short-lived submission tokens (`/v1/submissions`) via `pies-sdk` (supports a simple OTP send cooldown or deployment-layer rate limiting without complex sliding-window or distributed risk-control systems).
- **Zero plaintext email retention**: emails are used only to send OTPs via SMTP and derive HMAC-SHA256 digests (`publishers.yaml`, `ies.market-submission@1.0.0`); branches, commits, PRs, and logs never contain plaintext emails.
- **Read-only preflight**: validates `.device.yaml` (`pies_market.validate_device_model_mapping`) and `.algorithm.zip` (`pies_sdk.validate_plugin_package(..., run_tests=False, require_market_fields=True)`) without executing untrusted plugin code.
- **Direct PR submission**: checks that no open PR already exists for `(<id>, <version/revision>)`, creates a submission branch and opens a PR against `GITHUB_API_BASE` using a minimal-privilege token, and fails explicitly on conflict without complex idempotent PR recovery state machines.
