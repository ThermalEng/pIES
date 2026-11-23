# Roadmap

The Roadmap tracks undelivered feature plans, release ordering, and exit criteria. Features accepted and merged to the main line move into the [Changelog](README.md) and are removed from this page.

## Release Planning Conventions

- `0.x`: Pre-release contract convergence, engineering acceptance, and release preparation;
- `1.0.0`: Limited pilot test release;
- `1.x`: Feature iterations during pilot testing (proprietary contract markers remain fixed at `1.0.0` prior to `2.0` public release);
- `2.0.0`: First general public release (compatibility and migration policy established prior to release).

## `1.0.0` Delivered Baseline

```text
Third-Party Attack Surface & Public Boundaries (0.2–0.4)
  → Input & Assembly Contracts (0.5–0.7)
  → Generation – Execution – Results (0.8–0.10)
  → Application Orchestration & Traceability Cutover (0.11–0.12)
  → GUI & Frontend Convergence (0.13–0.14)
  → Finance & Prediction Calculation Convergence (0.14.5)
  → End-to-End Engineering Verification & Official 1.0.0 Release (1.0.0)
```

*(Note: `0.2.0` through `1.0.0` development and end-to-end engineering verification have been delivered and officially released as `1.0.0`, recorded in the [Changelog](README.md).)*

## Post-`1.0.0` `1.x` Evolution Plan

### `1.1.0` — Non-Traditional Solvers and Large-Scale Long-Horizon Acceleration

Address the performance bottleneck observed on long-horizon, high-resolution multi-energy planning cases (e.g., 25-year horizons at 30-minute resolution totaling `438,288` steps) where traditional branch-and-bound MILP solvers are too slow, while preserving public architecture boundaries and immutable evidence trails:

- **Pluggable non-traditional solver engines**: Integrate first-order operator-splitting solvers (e.g., PDLP-style algorithms), structural decomposition/coordination methods (Benders / ADMM / temporal decomposition), GPU/massively parallel solvers, or hybrid metaheuristic–exact acceleration plugins through the public `GeneratorProvider`, `SolverRuntime`, and `ResultAdapterProvider` contracts;
- **Solver-embedded structural acceleration**: Embed structural relaxation, on-demand constraint recovery, and resource/incumbent guardrails inside the built-in solver via `CalculationConfig.options` and verified Bundle structural metadata without introducing a standalone presolve plugin layer, reducing long-horizon MILP/LP runtime and memory risk while preserving full-model constraints and status honesty, and keeping future non-HiGHS solvers on the public `ExecutorProvider` and `ResultAdapter` contracts;
- **Strict domain equivalence & transparent reporting**: Preserve physical conservation, monthly TOU pricing, natural-month peak demand billing, and after-tax cash-flow semantics, while explicitly reporting the chosen non-traditional solver method, convergence residuals, optimality gaps, or boundary verification diagnostics in sealed results and summary cards;
- **Zero-trust isolation preserved**: Execute new solver runtimes inside isolated environments driven by structured Solver Bundles without compromising module autonomy, snapshot immutability, or financial auditability.

### `1.2.0` — Models & Algorithms, Provider SDK, and Device Extensions

- Introduce top-level navigation "Models and Algorithms" for signed-in engineers containing "Model Library", "Algorithm Plugins", and "Shared Hub";
- Model Library unifies forms, online YAML editing, and YAML uploads to publish immutable `ies.device-model` revisions;
- Algorithm Plugins accept `ies.algorithm-plugin` ZIP archives executed in an isolated containerized runner so API and ordinary Workers never load user source code;
- Support owner sharing requests, administrator approval, and user catalog installation by reference;
- Publish extension SDK and verification tooling, gradually adding hydrogen, thermal/cold storage, wind, gas engines, waste-heat boilers, absorption chillers, demand response devices, and extended finance rules.

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
