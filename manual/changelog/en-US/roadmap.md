# Roadmap

The Roadmap tracks undelivered feature plans, release ordering, and exit criteria. Features accepted and merged to the main line move into the [Changelog](README.md) and are removed from this page.

## Release Planning Conventions

- `0.x`: Pre-release contract convergence, engineering acceptance, and release preparation;
- `1.0.0`: Limited pilot test release;
- `1.x`: Feature iterations during pilot testing (proprietary contract markers remain fixed at `1.0.0` prior to `2.0` public release);
- `2.0.0`: First general public release (compatibility and migration policy established prior to release).

## `0.x` Main Development Track

```text
Third-Party Attack Surface & Public Boundaries (0.2–0.4)
  → Input & Assembly Contracts (0.5–0.7)
  → Generation – Execution – Results (0.8–0.10)
  → Application Orchestration & Traceability Cutover (0.11–0.12)
  → GUI & Frontend Convergence (0.13–0.14)
  → Finance & Prediction Calculation Convergence (0.14.5)
  → Extensions, Operations & Release (0.15–0.17)
```

*(Note: `0.2` through `0.14.5` along with general modeling/finance/compact-modeling and chunked-handoff remediations have been delivered and recorded in the Changelog.)*

## `0.15.0` — Provider Extensions and End-to-End Engineering Acceptance

Goal: Prove on existing public boundaries that external extensions integrate without modifying core modules, and have independent engineering users complete end-to-end modeling, full-horizon solving, financial evaluation, and export delivery.

### Provider Extension Acceptance

1. Verify device YAML, data CSV, public equations, `GeneratorProvider`, `ResultAdapterProvider`, `ExecutorProvider`, and storage adapters sequentially using external extensions outside the core repository.
2. Publish runnable extension examples, file contract verification tools, and Provider version-pinning conventions.

### End-to-End Engineering User Acceptance

| Acceptance Dimension | Completion Evidence |
|---|---|
| Deployment & accounts | Record code baseline and live status of deployed components; engineers sign in and create projects cleanly |
| Case & data preparation | Verify contiguous `step`, resolution, point counts, units, and technical/financial bases; confirm zero missing inputs |
| Real user modeling chain | Complete project creation, dataset upload/binding, canvas wiring, generic YAML/constraint editing, saving, and reopening in Docker Playwright |
| Independent alternatives & freezing | Save and solve each alternative separately, verifying devices, tariffs, taxes, and baseline attribution; draft edits do not mutate historical tasks |
| Full-horizon computation & status | Complete modeling, queuing, Worker execution, and full-horizon solving end-to-end, recording timings, model sizes, and resource observations |
| Physical & operational results | Independently verify energy balance, capacity/power bounds, efficiencies, state initial values and continuity, port flows, and source constraints |
| Financials & alternative comparison | Independently audit construction payments, O&M, demand charges, VAT, income tax, depreciation, cash flows, and NPV/IRR/payback vs. baseline differences |
| Results, exports & delivery | Verify consistency across results UI, time series, Excel reports, and project package round-trips; archive independent checks and acceptance evidence |

Exit criteria: Adding or replacing Providers requires modifying no unrelated business modules, GUI code, or runtime core; independent engineering cases complete real solving, same-source financial evaluation, result verification, and delivery exports using official models with all blocking defects closed.

### Measured Performance and Equivalent Optimization Principles

- Implement targeted optimizations solely for modeling, memory, or solver bottlenecks measured and reproduced during end-to-end engineering acceptance rather than building unverified compression frameworks upfront.
- Any optimization MUST preserve strict mathematical equivalence of constraints and objectives without arbitrarily dropping complementarity relations, shortening the time horizon, or introducing unproven approximations.

## `0.16.0` — Operations, Backups, and Fault Drills

Goal: Mature operational observability and reliable data recovery.

1. Complete aggregated `healthz`/`readyz` probes incorporating API, Workers, database, queue, storage, and required computation Providers into readiness checks.
2. Verify end-to-end trace correlation logs (`request` / `task` / `attempt` / `bundle` / `receipt` / `result` cross-lookup).
3. Complete combined database and object-storage backup/restore drills alongside common fault-injection tests (missing objects, interrupted writes, failed Providers).

## `0.17.0` — `1.0.0` Release Candidate

Goal: Feature freeze and complete pre-release quality gates and documentation convergence.

1. Complete full Docker regression, database migration drills, and end-to-end browser acceptance.
2. Audit external entry permissions, image configuration, open-source licensing, and rollback playbooks.
3. Freeze public contracts and the `1.0.0` User Guide, closing all known blockers.

## `1.0.0` — Limited Pilot Release

Delivery criteria: All `0.17.0` release candidate gates pass, official documentation matches actual GUI behavior, historical tasks remain fully traceable, and upgrade/recovery drills execute cleanly.

## Post-Pilot `1.x` Evolution Plan

### `1.1.0` — Models & Algorithms, Provider SDK, and Device Extensions

- Introduce top-level navigation "Models and Algorithms" for signed-in engineers containing "Model Library", "Algorithm Plugins", and "Shared Hub";
- Model Library unifies forms, online YAML editing, and YAML uploads to publish immutable `ies.device-model` revisions;
- Algorithm Plugins accept `ies.algorithm-plugin` ZIP archives executed in an isolated containerized runner so API and ordinary Workers never load user source code;
- Support owner sharing requests, administrator approval, and user catalog installation by reference;
- Publish extension SDK and verification tooling, gradually adding hydrogen, thermal/cold storage, wind, gas engines, waste-heat boilers, absorption chillers, demand response devices, and extended finance rules.

### `1.2.0` — Planning–Operation Closed Loop and Scenario Analysis

- Typical-day capacity sizing with full-year operational verification;
- Multi-scenario project computation baselines and controlled baseline migration;
- Sensitivity computation, batch parameter sweeps, uncertainty analysis, and multi-objective candidate comparison;
- Expand baseline financial metrics with LCOE and standalone equity evaluation models.

### `1.3.0` — MCP Integration and AI-Assisted Data Preparation

- Expose project operations to external AI clients over MCP with revocable connection credentials issued on a dedicated sign-in page, supporting concurrent multi-client connections and draft conflict alerts;
- Provide a confirmation screen for candidate configurations so AI-assisted inputs always require human review before freezing into tasks.

### `1.4.0` — Excel Data Standardization

- Assist external AI via MCP in converting non-standard Excel workbooks into contract-compliant datasets with transformation notes, quality diagnostics, and formula-injection protection.

### `1.5.0` — One-Click Full Planning Report Generation

- Generate traceable planning reports from frozen project snapshots and structured computation evidence while preserving version provenance and human confirmation logs.

### `1.6.0` — Comprehensive Input Field Guidance and Format Help

- Provide a unified help button on every input control (form inputs, selectors, numeric boxes, and text areas);
- Support extended hover (`hover`) and click inspection;
- Popovers clearly document business meaning, calculation basis, units/dimensions, valid ranges, and formatting conventions;
- Provide full keyboard accessibility and ARIA support without obstructing native input or shortcuts.

## `2.0.0`

First general public release. Requires freezing public boundaries, establishing compatibility and data migration policies, documenting rollback playbooks, and passing public release acceptance.
