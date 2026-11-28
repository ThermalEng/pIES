# Roadmap

The Roadmap tracks undelivered feature plans, release ordering, and exit criteria. Features accepted and merged to the main line move into the [Changelog](README.md) and are removed from this page.

## Release Planning Conventions

- `0.x`: Pre-release contract convergence, engineering acceptance, and release preparation;
- `1.0.0`: Limited pilot test release;
- `1.x`: Feature iterations during pilot testing (proprietary contract markers remain fixed at `1.0.0` prior to `2.0` public release);
- `2.0.0`: First general public release (compatibility and migration policy established prior to release).

## `1.2.0` Delivered Baseline

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
  → Unidirectional Computation Pipeline, Bounded Subprocess Restoration & Parallel Evaluators (1.1.6 / F1–F4 & G0)
  → Open Model & Algorithm Plugin Ecosystem, pIES Market & Reference Artifacts (1.2.0 / E1–E5b)
```

*(Note: `0.2.0` through `1.2.0` have been delivered and recorded in the [Changelog](README.md).)*

### `1.2.0` Delivered Milestones (`F1`–`F4` / `G0` & `E1`–`E5b`)

- [x] **`P0` Computation Core Refactoring & `G0` Gate (`1.1.6`)**: Finalized 13-field `CalculationConfig` (removing `convergence`, adding slotted `options` and parallel `evaluators[]`), `SolverBundle`, and `pies-sdk` (`1.2.0`); delivered `backend` bounded-subprocess restoration (`ResultAdapter`), shared-truth forward financial evaluator (`ies.evaluator.finance@1.0.0`), unidirectional hardlink pipeline, independent retryable evaluators, and one-pass `1.1.5 → 1.2.0` config migration.
- [x] **`E1`–`E2` Public Contracts, Single Active Version (Plan B) & Local Governance**: Delivered `pies-sdk validate/pack`, `ies.market-index@1.0.0` / `ies.submission@1.0.0` contracts, Plan B single active version policy (no multi-version pinning/switching and zero cross-version migration layers), controlled `--network=none` / single-`--index-url` builds & self-tests, row-locked uninstall (`409 ECO-UNINSTALL-IN-USE`), and the three-way SHA-256 verification boundary.
- [x] **`E3`–`E4` Decoupled Static Market & Stateless Submission Gateway**: Delivered `pIES-Market` static index/site repo (read-only `--network=none` PR CI) and `market_gateway/` stateless OTP + HMAC PR proxy with zero plaintext email persistence.
- [x] **`E5a` Full-Stack Product Integration**: Delivered `/custom` dual tabs, `/admin/ecosystem` orthogonal governance, `ConfigPage` dynamic stage catalog & schema-driven forms, and `ResultsPage` self-describing evaluation report rendering plus sanitized Excel sheets.
- [x] **`E5b` Curated Reference Artifacts & `1.2.0` Release**: Delivered 4 reference device models (`CHP`, `thermal_storage`, `absorption_chiller`, `wind_turbine`) and 4 replaceable reference plugins (`seasonal_joint` predictor, `highspy_ref` `.mps` solver, `economic_dispatch` algorithm, `carbon_reliability` evaluator) alongside bilingual manual updates.

## Post-`1.2.0` `1.x` Evolution Plan

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
