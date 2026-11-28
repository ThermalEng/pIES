# pIES Market (`ThermalEng/pIES-Market`)

Standalone static public market for pIES device models (`ies.device-model@1.0.0`) and algorithm plugins (`ies.algorithm-plugin@1.0.0`).

- **Zero backend / database dependency**: uses only `pies-sdk` and `PyYAML`.
- **Publisher registry (`publishers.yaml`)**: binds community/vendor prefixes to HMAC-SHA256 email digests (zero plaintext emails); official `ies.*` artifacts live under `artifacts/ies/...` without registering `ies`.
- **Read-only PR validation (`pies_market.check_pr`)**: validates `publishers.yaml`, artifact ownership, device YAML schemas, plugin packages (`require_market_fields=True`), and optional `--network=none --read-only` Docker container self-tests against `pies-plugin-base:latest`.
- **Deterministic static release (`pies_market.build_index`)**: builds `dist/index.json` (`ies.market-index@1.0.0`), deterministic packages under `dist/artifacts/`, and copies the zero-build static browse/download portal `site/` into `dist/`.
