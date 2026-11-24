# Roadmap

The Roadmap tracks undelivered feature plans, release ordering, and exit criteria. Features accepted and merged to the main line move into the [Changelog](README.md) and are removed from this page.

## Release Planning Conventions

- `0.x`: Pre-release contract convergence, engineering acceptance, and release preparation;
- `1.0.0`: Limited pilot test release;
- `1.x`: Feature iterations during pilot testing (proprietary contract markers remain fixed at `1.0.0` prior to `2.0` public release);
- `2.0.0`: First general public release (compatibility and migration policy established prior to release).

## `1.1.0` Delivered Baseline

```text
Third-Party Attack Surface & Public Boundaries (0.2–0.4)
  → Input & Assembly Contracts (0.5–0.7)
  → Generation – Execution – Results (0.8–0.10)
  → Application Orchestration & Traceability Cutover (0.11–0.12)
  → GUI & Frontend Convergence (0.13–0.14)
  → Finance & Prediction Calculation Convergence (0.14.5)
  → End-to-End Engineering Verification & Official 1.0.0 Release (1.0.0)
  → Solver-Embedded Structural Acceleration & Large-Scale Optimization (1.1.0)
```

*(Note: `0.2.0` through `1.1.0` have been delivered and recorded in the [Changelog](README.md).)*

## Post-`1.1.0` `1.x` Evolution Plan

### `1.1.5` — Ephemeral Container Computation Pipeline, Unified Plugin Contract, and Backend Orchestration Convergence

- Per `ADR-0016`, converge the former persistent `io_worker` and lightweight task scheduling into `backend` (keeping edit-time fast validations synchronous), introduce a thin allowlisted proxy `job_runner` as the sole controlled holder of the host Docker socket, and consolidate persistent services to five containers (`web`, `backend`, `job_runner`, `postgres`, `redis`);
- Establish the concise unified `ies.algorithm-plugin@1.0.0` (`plugin.yaml` + `/work/in → /work/out`) contract covering `predictor`, `generator`, `solver`, `evaluator`, and single-step `algorithm`, alongside optional advanced extensions and a dedicated internal-loop interface;
- Refactor prediction, assembly generation (HiGHS MILP), solving (HiGHS 1.7.2), result adaptation, and post-solve financial evaluation into per-step network-disabled ephemeral containers launched and torn down by `backend` via `job_runner`, packaging built-in algorithms into immutable computation images;
- Exit criteria: cold-start verification, stage checkpoint recovery, cancellation/timeout/OOM reclamation, `1.1.0` large-scale planning benchmarks, and full Playwright regressions pass with zero functional or performance regression.

### `1.2.0` — Open Model & Algorithm Plugin Ecosystem and pIES Market

- Build on the `1.1.5` unified plugin contract and ephemeral container foundation to enable automatic immutable image builds via `job_runner` when installing third-party plugins and release the standalone zero-backend-dependency developer package `pies-sdk`;
- Decouple an independent public marketplace repository `ThermalEng/pIES-Market` from `ThermalEng/pIES`, ensuring the public market never acts as a runtime dependency for local pIES computation;
- Deliver a unified single-page "Models and Algorithms" catalog inside pIES supporting kind-based filtering and explicit item states (`Remote` / `Local (Not Installed)` / `Local (Installed)`), toggled by local administrators between **Local Mode** (disables remote market and external submission; local sharing reaches administrator level only) and **Online Mode** (all signed-in users can browse, download/install, and submit via email verification);
- Establish cross-instance stable namespaces (`ies.*` / `user.<public_namespace>.*` / `<publisher>.*`), external download/import-boundary SHA-256 verification (not stored or re-verified internally), hot-reload-free immutable installations, and `CalcSnapshot` submission freezing;
- Deliver in verifiable slices: `M1` public contracts and market index finalization → `M2` local model/plugin management, automatic image builds, and standalone `pies-sdk` → `M3` `pIES-Market` GitHub repository and static JSON index pipeline → `M4` public market service (email-code submission, manual review, and standalone download endpoint) → `M5` pIES dual-mode market integration and personal/enterprise acceptance → `M6` curated engineering reference device models, reference Python plugins, and authoring examples.

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
