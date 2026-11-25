# Roadmap

The Roadmap tracks undelivered feature plans, release ordering, and exit criteria. Features accepted and merged to the main line move into the [Changelog](README.md) and are removed from this page.

## Release Planning Conventions

- `0.x`: Pre-release contract convergence, engineering acceptance, and release preparation;
- `1.0.0`: Limited pilot test release;
- `1.x`: Feature iterations during pilot testing (proprietary contract markers remain fixed at `1.0.0` prior to `2.0` public release);
- `2.0.0`: First general public release (compatibility and migration policy established prior to release).

## `1.1.5` Delivered Baseline

```text
Third-Party Attack Surface & Public Boundaries (0.2–0.4)
  → Input & Assembly Contracts (0.5–0.7)
  → Generation – Execution – Results (0.8–0.10)
  → Application Orchestration & Traceability Cutover (0.11–0.12)
  → GUI & Frontend Convergence (0.13–0.14)
  → Finance & Prediction Calculation Convergence (0.14.5)
  → End-to-End Engineering Verification & Official 1.0.0 Release (1.0.0)
  → Solver-Embedded Structural Acceleration & Large-Scale Optimization (1.1.0)
  → Ephemeral Container Computation Pipeline, Unified Plugin Contract & Backend Convergence (1.1.5)
```

*(Note: `0.2.0` through `1.1.5` have been delivered and recorded in the [Changelog](README.md).)*

### `1.1.5` Delivered Milestones (`S1`–`S4`)

- [x] `S1` Standalone computation contract & minimal SDK: Delivered the backend-independent `pies-sdk` (`1.1.5`) and unified `ies.algorithm-plugin@1.0.0` contract passing all four runtime modularity gates.
- [x] `S2` `job_runner` & container lifecycle: Delivered the 5-operation `job_runner` proxy, `--network=none --read-only` isolation, host NCPU clamping, DB-authoritative startup reconciliation, and non-blocking detach.
- [x] `S3` Official pipeline migration: Delivered `pies-builtin-stages:1.1.5`, direct `frozen_series_inputs` materialization with `os.link` zero-copy stage handoffs, and the 14-field `CalculationConfig`.
- [x] `S4` `1.1.5` acceptance & cutover: Converged `EmbeddedScheduler` into `backend`, passed cold-start and full acceptance checks, retired legacy `io_worker`/`compute_worker` daemons, and consolidated persistent services into 5 containers (`postgres`, `redis`, `job_runner`, `backend`, `web`).

## Post-`1.1.5` `1.x` Evolution Plan

### `1.2.0` — Open Model & Algorithm Plugin Ecosystem and pIES Market

- Build on the `1.1.5` unified plugin contract and ephemeral container foundation to support dynamic restart-free installation and configuration selection across all five plugin kinds (`predictor` / `generator` / `solver` / `evaluator` / `algorithm`, where `algorithm` strictly replaces `generator + solver` without crossing upstream/downstream stage boundaries); build plugin images at install time via `job_runner` atop `pies-plugin-base` (with optional pip mirror/intranet index support and zero-network base reuse when dependency-free), enforce a `--network=none` self-test gate whenever `tests` are declared, add the allowlisted `delete_plugin_image` cleanup operation for `pies-plugin/*` images on uninstall, and keep zero-backend-dependency `pies-sdk` minimal with universal `validate` and `pack` commands across all five plugin kinds;
- Decouple an independent public marketplace repository `ThermalEng/pIES-Market` from `ThermalEng/pIES` using a GitHub static JSON index (`index.json`), a co-hosted unauthenticated static browse/download web page, and a self-hostable thin stateless submission gateway (FastAPI + SMTP OTP verification → automated GitHub PR creation → human PR Review → automated Actions validation and release), ensuring the public market never acts as a runtime dependency for local pIES computation;
- Upgrade `/custom` into a unified single-page "Models and Algorithms" view with two peer tabs—"Market & Installed" (kind filtering across device models and five plugin kinds, multi-version coexistence/switching per ID, and `Remote` / `Local (Not Installed)` / `Local (Installed)` states backed by instance-level shared cache + per-member installation state) and "My Creations / Drafts" (bidirectional common-field form + online YAML + `.device.yaml` import/export for custom device models, plus personal local upload records)—while centralizing **Local Mode** / **Online Mode** switching, market/gateway URLs, pip mirror URL, the **`local_upload_requires_approval`** toggle, and the pending approval queue under `/admin`;
- Establish cross-instance stable namespaces (`ies.*` / `user.<public_namespace>.*` / `<publisher>.*`), one-time external download/import-boundary SHA-256 verification (not stored or re-verified internally), hot-reload-free installation, and immutable input/`CalcSnapshot` submission freezing;
- Deliver in verifiable slices: `M1` public contracts and market index finalization → `M2` local model/plugin management, automated image builds & offline self-test gate, dynamic execution across all five plugin kinds, and minimal `pies-sdk` (`validate`/`pack`) → `M3` `pIES-Market` GitHub repository, static JSON index, and co-hosted static browse/download page pipeline → `M4` self-hostable thin stateless submission gateway (email OTP submission, automated PR creation, and PR review workflow) → `M5` pIES two-tab market & `/admin` approval integration with personal-online / enterprise-local acceptance → `M6` curated engineering reference device models (CHP, thermal/cold storage tank, absorption chiller, wind turbine) and 3 reference Python plugins (`predictor`, `algorithm` with `iteration`, and external `.mps`-consuming `solver` with `requirements.txt`).

### `1.3.0` — Planning–Operation Closed Loop and Scenario Analysis

- Typical-day capacity sizing with full-year operational verification;
- Multi-scenario project computation baselines and controlled baseline migration;
- Sensitivity computation, batch parameter sweeps, uncertainty analysis, and multi-objective candidate comparison;
- Expand baseline financial metrics with LCOE and standalone equity evaluation models.

### `1.4.0` — MCP Integration and AI-Assisted Data Preparation

- Expose project operations to external AI clients over MCP with revocable connection credentials issued on a dedicated sign-in page, supporting concurrent multi-client connections and draft conflict alerts;
- Provide a confirmation screen for candidate configurations so AI-assisted inputs always require human review before freezing into tasks.

### `1.5.0` — Excel Data Standardization

- Assist external AI via MCP in converting non-standard Excel workbooks into contract-compliant datasets with transformation notes, quality diagnostics, and formula-injection protection.

### `1.6.0` — One-Click Full Planning Report Generation

- Generate traceable planning reports from frozen project snapshots and structured computation evidence while preserving version provenance and human confirmation logs.

### `1.7.0` — Comprehensive Input Field Guidance and Format Help

- Provide a unified help button on every input control (form inputs, selectors, numeric boxes, and text areas);
- Support extended hover (`hover`) and click inspection;
- Popovers clearly document business meaning, calculation basis, units/dimensions, valid ranges, and formatting conventions;
- Provide full keyboard accessibility and ARIA support without obstructing native input or shortcuts.

## `2.0.0`

First general public release. Requires freezing public boundaries, establishing compatibility and data migration policies, documenting rollback playbooks, and passing public release acceptance.
