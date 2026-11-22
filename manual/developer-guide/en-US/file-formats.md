# File Format Standards

> Status: Effective Target Contract; Governing Spec: [Architecture Constitution](ARCHITECTURE_CONSTITUTION.md)

Core pIES business documents and offline interchange packages use standalone, verifiable public formats whose field-level definitions live in the corresponding authoritative pages under `formats/`.

## Overview of Public File Contracts

| Format Contract | Recommended Filename / Suffix | Responsibility | Primary Consumers |
|---|---|---|---|
| [Device Model YAML](formats/device-model-yaml.md) | `*.device.yaml` | Device identity, time-invariant technical parameters, five interface classes, and declarative equations | Device catalog, frontend form schemas, assembly, equation parser |
| [Device Data CSV](formats/device-data-csv.md) | `*.data.csv` | Device time series with contiguous `step`, resolution, and units | Data import, assembly binding checks, series materialization |
| [Price Series CSV](formats/price-series-csv.md) | `*.price.csv` | Full-horizon or intraday energy price curves | Data import, task submission checks, price materialization |
| [Finance YAML Contract](formats/finance-yaml.md) | `finance_profile.yaml` / `finance_overrides.yaml` / `effective_finance.yaml` | Regional finance baseline, sparse project overrides, and deterministic merged snapshot | Finance merger, assembly validation, generation, evaluation |
| [Assembly YAML](formats/assembly-yaml.md) | `*.assembly.yaml` | Project baseline, device instances, source bindings, connection topology, finance bindings, and planning config | Assembly validator |
| [Network Boundaries and System Equations](formats/network-relations.md) | `system_equations` inside assembly | Cross-instance allocation, auxiliary technical variables, and closed unconnected-port semantics | Assembly validator, common technical contributions, generators |
| [Named Amounts, Payments, and Assets](formats/finance-relations.md) | Extension sections inside finance triplet | Named cost items, generic amount definitions, cash movements, asset depreciation, and derived taxes | Finance merger, generators, result adapter |
| [Solver Bundle](formats/solver-bundle.md) | `bundle.yaml` + input/output directories | Solver input files, structured commands, output manifest, and result mappings | Solver runtime, result adapter |
| [Calculation Config](formats/calculation-config.md) | Calculation config mapping | Calculation mode, prediction config, generator/solver versions, precision, time limit, and output requests | Submission validator, generators |
| [Project Package](formats/project-package.md) | `*.zip` (`manifest.json` at root) | Offline export/import of project workspace, version history, and dependencies | Project package exporter/importer |
| [Algorithm Plugin Package](formats/algorithm-plugin-package.md) | `*.algorithm.zip` | User-defined assembly-processing and solving algorithm bundle (`1.1.0` target) | Plugin validator and isolated runner |

**Boundary between hand-authored and machine-generated files**: Device model YAML, data CSV, `FinanceProfile`, `FinanceOverrides`, and assembly YAML support hand authoring; `EffectiveFinanceConfig` MUST be produced by the deterministic merger (and re-merged/verified from its sources on import); Solver Bundles MAY only be produced by generators.

## From Hand-Authored Files to Computation Results

```text
FinanceProfile (regional baseline) + FinanceOverrides (project overrides)
        │
        ▼ Deterministic Merge
EffectiveFinanceConfig (immutable snapshot)
        │ Exact Reference
        ▼
Device YAML + Data CSV + Assembly YAML ──→ ValidatedAssemblyArtifact
                                                      │
        Calculation Config + Evaluation Confirmation ─┼→ Series Materialization → GeneratorProvider → Solver Bundle
                                                                                                              │
                                                                 ComputeResult ← ExecutionReceipt ← SolverRuntime
```

Assembly YAML focuses on system structure, bindings, and planning intent, and MUST NOT include calculation configuration options (generator, solver, precision, etc.) or process/path details.

## General Authoring Rules and Safe YAML Subset

All public text formats obey these common rules:

- **Encoding and line endings**: UTF-8 encoding; canonical output uses LF line endings.
- **Naming and identifiers**: Extension IDs use ASCII lowercase namespaced strings (e.g., `ies.device.pv`); local IDs use `lower_snake_case` or hyphens consistently within a file.
- **Versions**: `schema_version`, plugin versions, and dependency versions use quoted `MAJOR.MINOR.PATCH` strings; individual device models have no semantic version.
- **Numbers and monetary values**: `NaN`, `Infinity`, and language-specific special scalars are prohibited; financial amounts use `{value, unit}` atoms with decimal fixed-point strings.
- **Time and units**: Calculation and data series use contiguous `step` indices starting from `0` and explicit `resolution` with no time zone; units MUST come from the common unit vocabulary.
- **Paths and security boundaries**: Internal file references MUST be relative paths; absolute paths, `..` traversal, symlink escapes, host paths, credentials, and implementation module paths are prohibited.
- **Strict validation**: Unknown core fields are rejected by default; extensions belong under namespaced `extensions` keys and MUST NOT alter core semantics.

YAML uniformly uses the **YAML 1.2 Safe Subset**: two-space indentation, prohibiting tabs, custom tags, anchors, aliases, merge keys, and duplicate keys.

### Single Current Contract and Project Package Dependencies

Prior to the public `2.0` release, proprietary file contract markers are uniformly fixed at `1.0.0`:

- Development updates schemas, producers, consumers, samples, and tests synchronously without multi-version compatibility matrices, legacy header dispatch, or runtime automatic migrations.
- Frozen historical snapshots and sealed results remain as recorded without forcing current code to re-solve legacy formats.
- Portable project package dependency requirements:
  - Each dataset version owns dedicated metadata and directory inside the package;
  - The package MUST self-contain the exact finance triplet, planning configs, and canonical project model texts required by the current draft and packaged historical versions, matching the exact set of referenced items;
  - On confirmed import, the target database reconstructs fresh identities via the official save path and remaps all internal references without reusing source database IDs or revision numbers.

## Frontend Forms and Canonicalization

1. **Unified entry point**: For configurations whose authoritative input is a file contract (such as device models, finance documents, and full planning documents), structured frontend forms MUST serialize into the complete document prior to submission and enter through the same official backend parsing endpoint, avoiding a second parsing implementation in the frontend.
2. **Sole canonical form**: Before producing an assembly artifact, the validator performs safe parsing, dependency resolution, stable key sorting, and comment stripping to produce canonical bytes and a validation receipt; together these form `ValidatedAssemblyArtifact`, the sole credential accepted by generators.

## Minimum Offline Delivery Set for Plugins

Delivering new device technical content SHOULD supply a minimal self-contained set passing validation independently:

1. `*.device.yaml` technical definition;
2. Minimal valid and typical `*.data.csv` samples;
3. Minimal `*.assembly.yaml` referencing that device;
4. Contract tests covering valid cases, common invalid cases, and a verifiable calculation baseline.
