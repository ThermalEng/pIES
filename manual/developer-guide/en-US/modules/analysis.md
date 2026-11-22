# Result Analysis

> Code Boundary: `backend/iesplan/analysis/`; Governing Spec: [Architecture Constitution](../ARCHITECTURE_CONSTITUTION.md)

`analysis` performs pure post-solve engineering/environmental metric extraction, sensitivity sweeps, multi-scenario batch comparisons, and four-dimensional system assessment on immutable computation results, financial evaluations, and evidence context without modifying raw evidence or re-reading mutable drafts.

## Responsibilities and Boundaries

- **Owns**: Pure engineering and environmental metric computations; single-factor sensitivity, parameter sweep, and multi-scenario comparison logic; public contracts and rules for four-dimensional system assessment (physical feasibility, optimality, financial viability, and reliability).
- **Does not own**: Worker task leases and scheduling, manual assessment persistence (system assessment and manual assessment MUST remain distinct commands and never substitute for one another), or report file download management.

## Input and Output Contracts

- **Input requirements**: Consumes solely status- and version-pinned `ComputeResult`, same-solve sealed financial evaluation, and explicit `AnalysisCommand` payloads (naming metrics, parameter paths, sweep ranges, and random seeds).
- **Output guarantees**: Metrics include physical/financial units, statistical basis, and sample coverage; batch and stochastic sweeps retain independent technical status and exact failure counts per sample (failed samples or missing fields mark unassessable explicitly rather than zero-filling); in four-dimensional assessment, a failure in any single dimension MUST NEVER be masked by an overall summary.

## Extension and Verification Requirements

When adding a new metric or sweep method, explicitly define its formula, unit, orientation, and missing-input behavior, include outputs in versioned analysis contracts, verify pure-function numerical accuracy and seed reproducibility on hand-calculated cases, and ensure the frontend renders published results only without duplicating metric calculations.
