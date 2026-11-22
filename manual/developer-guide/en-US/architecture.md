# System Architecture Blueprint

> Status: Effective Blueprint; Spec Version: 1.3.0; Governing Spec: [Architecture Constitution](ARCHITECTURE_CONSTITUTION.md)

## System Overview

pIES transforms device technical models, connection topology, time-series data, financial rules, and planning objectives into verifiable, solvable, and traceable engineering planning results. The architecture centers on two core requirements: **business semantics are unified under the backend and declarative documents**, and **devices, equations, generators, executors, and storage are decoupled through stable boundaries**.

### Model Document and Backend Centric

Device technical definitions, system assemblies, financial configurations, planning objectives, and calculation configurations are all expressed as standalone declarative documents that the backend uniformly parses, validates, and compiles into mathematical problems. As the user interaction layer, structured GUI forms serve only as convenient editing views over those documents rather than deciding what relations the system can express; for advanced constraints not expanded in forms, users can edit, preserve, and submit the full document directly.

New engineering modeling requirements SHOULD be expressed primarily through declarative device equations, system equations, and constraints—extending common capabilities when gaps appear in the generic pipeline rather than hardcoding branches by device ID or case name in core flows.

### Domain Semantics and Mathematical Expression

The domain layer owns the business meaning of devices, connections, expenses, assets, payments, and taxes; the computation generation layer translates them equivalently into solver-specific mathematical models; and the result adaptation layer restores solver outputs back into results with business units and identities.

| Semantic Topic | Authoritative Text |
|---|---|
| Port connections, internal device boundaries, and closed-port semantics | [Port-Level Network Semantics](modules/assembly.md#port-level-network-semantics) |
| Model valid domains, project bounds, and derived bound analysis | [Unified Bounds](modules/generators.md#unified-bounds) |
| Construction unit activation and scale variable constraints | [Construction Activation Target Semantics](formats/assembly-yaml.md#construction-activation-target-semantics) |
| Named amount definitions, payment occurrences, and reference identities | [Generic Amount Reference Target Semantics](formats/finance-yaml.md#generic-amount-reference-target-semantics) |
| Evaluation scope, tax boundaries, and frozen baselines | [Evaluation Scope and Baselines](modules/finance.md#evaluation-scope-and-baselines) |
| Operating steps, settlement periods, financial years, and equivalent optimization | [Time Structure and Equivalent Optimization](modules/generators.md#time-structure-and-equivalent-optimization) |

Prior to the public `2.0` release, proprietary file contract markers remain uniformly fixed at `1.0.0`, updating producers, consumers, and samples together on changes; frozen historical snapshots and result evidence remain as recorded rather than rewriting history from current drafts.

### Scale and Solvability Boundaries

- **Separation of assembly validity and solvability**: Assembly validation confirms that inputs are complete, references are valid, and dimensions are compatible; whether a specific model lowers into a given solver input or whether a problem is feasible/bounded is reported separately by generator capability declarations, lowering diagnostics, and solver status.
- **Controlled equivalent optimization**: Performance optimization SHOULD target measured generation and I/O hotspots and proceed strictly under mathematical equivalence of constraints and objectives (e.g., sparse matrix reuse, structural templating) without arbitrarily altering the problem horizon or business semantics.

## System Context and Actors

```text
Planning Engineer / Administrator
        │ Browser GUI
        ↓
   Web Frontend ──────→ Static Help Center (generated at build time from manual/)
        │ HTTPS / JSON / Files
        ↓
    API Adapter Layer
        ↓
  Application Layer ──→ PostgreSQL (authoritative transactional facts)
        │       └─────→ Object Storage (immutable large objects)
        ├──→ Domain Modules
        └──→ Tasks & Frozen Snapshots ──→ Worker Daemon
                                              └──→ application.worker Stage Commands
                                                        ├──→ GeneratorProvider ──→ Solver Bundle
                                                        ├──→ SolverRuntime ──→ ResultAdapter
                                                        └──→ analysis / Task Processing / Evidence & Results
```

- **Web Frontend**: Manages display state and unsubmitted forms; holds no authoritative business state.
- **API Adapter Layer**: Handles routing, authentication, DTO validation, and use-case invocation.
- **Application Layer**: Coordinates cross-module business actions and owns database transaction boundaries.
- **Domain Modules**: Each owns independent business rules, internal persistence, and public facades.
- **Worker Daemon**: Consumes immutable calculation snapshots and advances long-running background tasks via short-transaction stage commands in `application.worker`.

## End-to-End Business Flow

```text
Device Technical Definitions → Equation Parsing & Assembly Checks ─┐
Full-Horizon Series, Finance Rules & Parameters ───────────────────┼→ Computation Generation → Controlled Solving → Result Adaptation
Independent Calculation Config ────────────────────────────────────┘                                                          ↓
                                                                                 Financial Evaluation & Result Analysis on Same Solve
```

| Stage | Input | Output | Downstream Guarantee |
|---|---|---|---|
| Device Definition | Device YAML | `DeviceDescriptor` | `properties`/`interfaces`/`equations` and units valid; published content identity fixed |
| Equation Parsing | Device descriptor and restricted equations | Common AST and mathematical contributions | Variables, relations, units, and interface references unambiguous |
| Assembly & Validation | Assembly YAML/project graph, baseline, data bindings, planning config, `EffectiveFinanceConfig` | Diagnostics or `ValidatedAssemblyArtifact` | Connection topology, data sources, finance bindings, and planning inputs complete and valid |
| Series Materialization | Canonical assembly, project baseline, calculation config | Full-horizon contiguous `MaterializedSet` | `constant`/`data_repeat`/`data_full`/`data_predict` aligned to the same full-horizon `step` axis |
| Computation Generation | Assembly artifact, device contributions, materialized series, effective finance snapshot, `CalculationConfig`, evaluation confirmation | Solver Bundle | Capabilities matched; solver input files, structured commands, outputs, and adapter declarations complete |
| Controlled Solving | Solver Bundle, resource and cancellation context | `ExecutionReceipt` and raw outputs | Commands execute in isolation; exit status, resource metrics, and outputs intact |
| Result Adaptation | Bundle, execution receipt, raw outputs | `ComputeResult` | Mathematical status, business units, device/system series, financial facts, and version lock complete |
| Finance & Analysis | Computation results, frozen finance & evaluation params, analysis commands | Cash-flow evaluation, assessment, and analysis results | Metric bases, statuses, and evidence provenance chain intact |

## Three Core Business Chains

1. **Editing Chain (Draft Evolution)**: `Forms / Canvas → Frontend mapper → HTTP DTO → application use case → Domain validation → New draft revision`. Write operations carry `expected_revision` for optimistic concurrency conflict detection.
2. **Computation Chain (Snapshot Freezing and Solving)**: `Submit command → Authorization & assembly → Seal CalcSnapshot → Create Task → Worker claims Attempt → Series materialization → Generate Bundle → Isolated solve → Result adaptation (iterative when coupled predictions apply) → Financial evaluation → Commit Evidence`. Once created, tasks read only frozen snapshots and remain isolated from subsequent draft edits.
3. **Traceability Chain (Historical Interpretation)**: `Result View → Evidence → Attempt → Task → CalcSnapshot → Project/Data/Config/Provider Versions & Object Refs`. All display, assessment, scheme application, and exports read frozen facts along this chain.

## Module Responsibilities and Dependency Direction

| Module | Core Responsibilities | Non-Responsibilities |
|---|---|---|
| [core](modules/core.md) | Stateless primitive types, diagnostics, units, time axis, and canonical JSON | Business defaults, provider registration, persistence |
| [devices](modules/devices.md) | Device identity, technical properties, interface declarations, equation text, and catalog publishing | Project instances, economic costs, calculation precision |
| [modeling](modules/modeling.md) | Restricted technical equation parsing, semantic validation, and common math contributions | Device catalog paths, price objectives, solver execution |
| [assembly](modules/assembly.md) | Interface network assembly, four-stage validation, and issuing `ValidatedAssemblyArtifact` | Solver files, command lines, algorithm selection |
| [generators](modules/generators.md) | Mathematical lowering, unit conversion, and Solver Bundle generation | Process spawning, database access, task status |
| [solver runtime](modules/solver-runtime.md) | Controlled process/container execution, resource isolation, cancellation, and `ExecutionReceipt` | Device semantics, problem formulation, result interpretation |
| [computation](modules/engines.md) | Computation stage public contracts, prediction orchestration, result adaptation, and `ComputeResult` | HTTP sessions, ORM, draft management |
| [finance](modules/finance.md) | Finance triplet merging, planning financial relations, and project-investment cash-flow evaluation | Solver scheduling, UI state |
| [analysis](modules/analysis.md) | Sensitivity sweeps, batch comparison, and four-dimensional system assessment | Task leases, HTTP transport |
| [storage](modules/storage.md) | Immutable object I/O, owner reference lifecycle, and orphan cleanup/reconciliation | Understanding project version or dataset business semantics |
| [application](modules/application.md) | Authorization checks, database transactions, and cross-module use-case orchestration | Penetrating private domain implementations |
| [api](modules/api.md) | HTTP routing, authentication context, DTO serialization, and error mapping | ORM queries, domain rule calculations |
| [worker](modules/worker.md) | Queue claiming, lease renewal, subprocess execution isolation, and stage checkpoint recovery | Interpreting assembly semantics, assembling shell strings |

Dependency direction is strictly unidirectional:

```text
External Requests → api → application use cases → Domain Public Facades
Task Scheduling   → worker daemon → application.worker stage commands → Domain / Computation Public Facades
api / worker / application / Domain Modules → core
```

Business modules interact solely via `__init__.py`, `public.py`, or public contract types; importing private symbols, repositories, or ORM models across modules is prohibited. Concrete provider selection and dependency injection happen exclusively in the composition root (`bootstrap`) at process startup.

## Frontend-Backend Boundary

```text
GUI Form State → Frontend mapper (text, percentages, display units) → Public HTTP DTO
  → Backend (authentication, permissions, structure, dimensions, domain constraints) → Application Use Cases → Domain Modules
```

Frontend pre-validation serves immediate interactive feedback only; the backend is always the sole authority on business correctness and authorization.

## Placement and Development Sequence for Cross-Module Features

New capabilities MUST land in modules by responsibility rather than convenience: stateless value rules enter `core`, device technical descriptions enter `devices`, equation syntax enters `modeling`, assembly and network rules enter `assembly`, solver lowering enters `generators`, isolated execution enters `solver runtime`, output mapping enters `result adapter`, economic bases and assessments enter `finance` and `analysis` respectively, and cross-module workflows are orchestrated in `application` and exposed via `api` and the frontend.

Taking a new plannable device type as an example, proceed and validate independently in this order:

1. Define a device model YAML with `properties`, `interfaces`, and `equations` in `devices`;
2. Verify `modeling` parses its equations and produces solver-independent math contributions;
3. Verify `assembly` completes instance binding, port connections, and planning/finance checks;
4. Confirm an existing `GeneratorProvider` (or new standalone Generator) lowers it into a Bundle;
5. Verify `SolverRuntime` and `ResultAdapter` execute cleanly and restore business units;
6. Complete end-to-end integration across `application`, `api`, and the frontend with Docker Playwright browser acceptance.
