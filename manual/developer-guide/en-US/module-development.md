# Module Development Manual

> Status: Effective Blueprint; Scope: Backend business module boundaries and development collaboration

This manual provides the entry index and collaboration workflow for backend module development. Each module chapter details its public contracts, core boundaries, failure semantics, and verification criteria without duplicating private implementation code here.

## Reading and Development Entry

When picking up single-module development work, build context in this order:

1. Read the [Architecture Constitution](ARCHITECTURE_CONSTITUTION.md) to understand non-negotiable cross-module boundaries;
2. Read the corresponding module manual;
3. Inspect the module's public facade (`__init__.py` / `contracts.py`) and corresponding test suite;
4. Dive into module internals.

When adjusting cross-module data structures, also read [Public Contracts](contracts.md); for file specifications, read [File Format Standards](file-formats.md); and for snapshots and object lifecycles, read [Domain Model and Traceability Chain](domain-model.md).

## Backend Module Map

```text
Device Definitions ─→ Equation Parsing ─→ Assembly & Validation ─┐
Full Series, Finance Rules & Params ─────────────────────────────┼→ Computation Generators ─→ Solver Bundle
Calculation Config ──────────────────────────────────────────────┘                                     ↓
                                                                                            Solver Runtime ─→ Result Adaptation
                                                                                                                    ↓
                                                                                      Same-Solve Financial Eval / Result Analysis

HTTP API ──→ Application Use Cases ──→ Above Domain Modules / Object Storage
                 │
                 └──→ Immutable Snapshots ──→ Worker ──→ Evidence & Results

Composition Root ──→ Discovers & injects module Providers, validates startup dependencies
All Modules      ──→ Core (pure types, diagnostics, units & time axis)
Domain Repos     ──→ Persistence Adapters ──→ PostgreSQL
```

| Module | Core Responsibilities | Primary Inputs | Primary Outputs |
|---|---|---|---|
| [bootstrap](modules/bootstrap.md) | Read config, wire Providers, publish readiness state | Deployment config & candidate Providers | Complete `ApplicationContext` |
| [core](modules/core.md) | Provide stateless common vocabulary | Raw values & primitive args | Immutable value objects, diagnostics, canonical JSON |
| [devices](modules/devices.md) | Maintain device technical specs & catalog publishing | Device YAML & Providers | Immutable `DeviceDescriptor` |
| [modeling](modules/modeling.md) | Parse restricted technical equations & emit math contributions | Device descriptors & equation ASTs | Variables, constraints, state transitions, output mappings |
| [assembly](modules/assembly.md) | Validate project topology & configs and issue canonical assembly | Project graph, baseline, bindings, configs | Diagnostics or `ValidatedAssemblyArtifact` |
| [generators](modules/generators.md) | Lower math models, convert units, generate calculation bundle | Assembly artifact, contributions, series, finance, calc & eval inputs | Solver Bundle |
| [solver runtime](modules/solver-runtime.md) | Execute controlled solver commands in isolation | Solver Bundle, Attempt context | `ExecutionReceipt` & raw outputs |
| [computation](modules/engines.md) | Orchestrate series materialization, predictions, and result adaptation | Assembly artifact, Providers, receipts | `ComputeResult` |
| [finance](modules/finance.md) | Merge finance triplet, planning financial relations, and project evaluation | Finance config, eval confirmation, same solve | Financial relations, cash flows, evaluation metrics |
| [analysis](modules/analysis.md) | Metric calculation, parameter sweeps, and four-dimensional system assessment | Computation results, eval facts, analysis commands | Analysis results & assessment reports |
| [storage](modules/storage.md) | Immutable large-object I/O, owner references, and orphan cleanup | Byte streams, media types, Owner | `ObjectHandle` & reference state |
| [application](modules/application.md) | Control transaction boundaries & cross-module use-case orchestration | `ActorContext`, use-case commands | Application results, events, or background tasks |
| [api](modules/api.md) | HTTP routing, auth adaptation, DTO mapping | HTTP requests | Standardized JSON responses or error envelopes |
| [worker](modules/worker.md) | Control-plane embedded scheduling, task ownership & stage checkpoints | Task queue, `CalcSnapshot` | Attempt progress, stage assets, terminal state |
| [persistence](modules/persistence.md) | Isolate domain facts from database table implementations | Repository commands | Transactional state protected by DB constraints |

## Module Boundary Inspection Principles

Before designing a module feature, trace the interface contract chain:

```text
Upstream Owner → Module Public Input → Module Processing → Module Public Output → Downstream Consumer
```

- If a module has to import another module's private symbols, internal file paths, or ORM models to function, the upstream public facade is incomplete—extend the public contract first rather than bypassing the boundary.
- If a downstream module must infer behavior by inspecting field name patterns or catching broad exceptions, this module's output contract is not explicit enough.

## Cross-Module Change Collaboration Sequence

When a requirement spans multiple modules, contract ownership stays with each domain module and orchestration belongs to [application](modules/application.md), proceeding in this order:

1. Clarify the business scenario and observable final outputs;
2. Partition which domain facts and invariants each domain module owns;
3. Define or update immutable inter-module contracts first;
4. Complete unit and protocol tests for each domain module;
5. Wire transactions, references, and use-case orchestration in `application`;
6. Finally integrate `api`, Worker, and frontend, and run real-browser or end-to-end verification in Docker.
