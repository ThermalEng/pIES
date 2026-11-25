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

- **Framework zero-copy unidirectional pipeline, bounded-subprocess restoration & retryable parallel evaluators**: Converge onto the 13-field `CalculationConfig` (removing `convergence` and upgrading to parallel `evaluators[]`; draining all non-terminal queued/running/cancelling tasks prior to upgrade and migrating saved configs in one pass while preserving original intent and keeping historical `CalcSnapshot` and `Evidence` strictly read-only): upstream `predictions[]` groups targets per `predictor` plugin in one logical stage (supporting joint multi-target prediction); after staged `solver` emits a 0-based original-column-order primal solution, **`backend` faithfully restores the business-unit canonical engineering solution via `variable_map` inside an isolated bounded child subprocess** (reclaiming memory on exit without re-solving third-party solvers; non-feasible outcomes skip evaluators); cross-stage read-only context is **staged into `/work/in` via zero-copy hardlinks by the orchestrator**; under feasible core solves, top-level task outcome reflects only the core solve while `0..N` evaluators run in parallel—**partial evaluator failures preserve the sealed engineering solution and support isolated single-evaluator retry** (the official financial evaluator reuses shared `finance` domain rules for full forward evaluation, while third-party evaluators emit self-describing `metrics`/`charts`/`tables` blocks rendered generically by the frontend); build plugin images via `job_runner` with zero business credentials (`--network=none` when dependency-free, single `--index-url` when `pip_index_url` is set without `--extra-index-url` to prevent dependency confusion, surfacing explicit admin-contact warnings on failure), enforce `--network=none` self-tests prior to `ready`, coordinate disable/uninstall and task submissions under transactional row locks before `delete_plugin_image` removes `pies-plugin/*` images, and keep `pies-sdk` minimal with universal `validate` and `pack`;
- **Decoupled static public market & stateless privacy-preserving submission**: Separate `ThermalEng/pIES-Market` from `ThermalEng/pIES`, publishing `ies.market-index@1.0.0` (`index.json`, integer `revision` for device models and semver `version` for plugins, co-consumed by a co-hosted unauthenticated static browse/download page and pIES clients, where remote delisting never revokes local installs), paired with a self-hostable thin stateless submission gateway (SMTP OTP + HMAC challenge token → short-lived stateless submission token → archive/contract & `publishers.yaml` ownership check under the market's unified `MARKET_HMAC_SECRET` → idempotent GitHub PR creation with zero plaintext emails; read-only `--network=none` PR validation CI and human merge order resolving concurrent first-time namespace claims);
- **Unified two-tab market page & orthogonal admin controls**: Upgrade `/custom` into a unified single-page "Models and Algorithms" view with "Market & Installed" (`Remote` / `Local (Not Installed)` / `Local (Installed)` badges, kind filters, multi-version switching) and "My Creations / Drafts" (bidirectional common-field forms + online YAML + `.device.yaml` import/export, plus local upload records with backend build states); `/admin` manages **two orthogonal policies**—`market_mode` (Local / Online) and `local_upload_requires_approval` (governing non-admin local uploads/offline imports uniformly across both modes to prevent bypasses);
- **Stable namespaces & three-way external digest boundary**: Keep `user.<public_namespace>.*` for local drafts while converging public market submissions to readable `<publisher>.*`; preserve original IDs and publisher `revision`/`version` verbatim across instances (`409` on conflicting content); verify external digests via the three-way rule with zero internal SHA-256 storage afterward;
- **Deliver in verifiable slices**: `M1` 13-field calculation config, framework zero-copy handoffs, canonical engineering solution / self-describing evaluation blocks, `ies.market-index@1.0.0`, and stateless submission contracts → `M2` **first complete unidirectional pipeline cutover, runtime restoration, shared-truth forward financial evaluation, and minimal third-party `solver`/`algorithm`/`evaluator` replacement & parallel task tests, then complete** local model/plugin management, controlled builds/self-tests, race-free stateless uninstall, and minimal `pies-sdk` (`validate`/`pack`) → `M3` `pIES-Market` repository, read-only isolated PR CI, `index.json`, and co-hosted static browse/download page → `M4` self-hostable thin stateless submission gateway (HMAC challenge/submission tokens, `publishers.yaml` binding, idempotent PR creation) → `M5` pIES two-tab market, generic frontend rendering of self-describing evaluation blocks, `/admin` orthogonal approval integration, and personal-online / enterprise-local Playwright acceptance → `M6` curated engineering reference device models (CHP, thermal/cold storage tank, absorption chiller, wind turbine) and replaceable reference Python plugins (joint multi-target `predictor`, `algorithm` with `iteration`, external `.mps` `solver` with `requirements.txt`, and self-describing carbon/reliability reference `evaluator`).

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
