# pIES Architecture Constitution

> Status: Effective
> Version: 1.23.0
> Effective Date: 2026-08-20
> Last Updated: 2026-11-25
> Scope: Repository root entry points, `manual/`, `docs/`, `backend/`, `frontend/`, database, object storage, Worker, tests, and subsequent plugins
> Basis: 2026-08-20 Special Architecture Review, ADR-0001 through ADR-0008, 2026-09-07 Regional FinanceProfile Triplet Specification, User-Approved Embedded Tax Computation Ruling on 2026-10-19 (ADR-0011), User-Approved Direct Evaluation Confirmation Transmission Ruling on 2026-10-20 (ADR-0012), ADR-0013 through ADR-0015, User-Approved Unified Backend Orchestration & Ephemeral Computation Container Pipeline Ruling on 2026-11-24 (ADR-0016), User-Approved Single-Chain Stage Handoff & Single-Pass Joint Prediction Ruling on 2026-11-25 (ADR-0017)

## 1. Document Precedence

This document establishes the overarching principles that all subsequent design, development, refactoring, and review of pIES MUST obey. Normative keywords have the following meanings:

- **MUST / MUST NOT** (`必须 / 禁止`): Mandatory requirement; any violation blocks merge;
- **SHOULD / SHOULD NOT** (`应该 / 不应该`): Default requirement; any deviation requires submitting an ADR explaining the rationale;
- **MAY** (`可以`): Permitted provided no other clause is violated.

When documents conflict, precedence is resolved in the following order:

1. This Architecture Constitution;
2. Approved ADRs that explicitly amend this Constitution;
3. Effective stable public contracts and developer specifications under `manual/developer-guide/`;
4. Confirmed current user behavior and limitations under `manual/user-guide/`;
5. Current version facts and Roadmap ordering under `manual/changelog/`;
6. Review evidence with explicit baselines under `docs/reviews/`;
7. Existing code, tests, and historical compatibility behavior.

`docs/archive/` carries no current normative force. Anything in legacy contracts, specifications, or 2026-08-20 architecture materials that conflicts with this document is no longer valid—particularly designs such as global cross-module registries, static registry fallbacks, runtime hot-reloading, legacy endpoint forwarding, or backend computation of simple frontend display conversions.

## 2. Supreme Principles

### 2.1 Openness Over Efficiency

The system MUST first guarantee that new device technical content, new equation capabilities, new computation generators, new executors, new storage backends, and new analysis capabilities can plug in via stable protocols; module independence MUST NOT be sacrificed merely to save one query, one object, or to share mutable state.

Caching, indexing, or batching MAY be added once evidence proves a performance bottleneck, provided any cache is rebuildable and never becomes a second authoritative source of truth.

### 2.2 Correctness Over Compatibility

Before formal product release, legacy field aliases, legacy module forwarding, legacy response unions, or silent compatibility branches MUST NOT be retained. Frontend, backend, and development databases SHOULD migrate directly to new contracts in one pass.

Prior to the public `2.0` release, proprietary contract identifiers are uniformly fixed at `1.0.0`, and only the current contract set is maintained at all times; `1.0` is reserved for small-scale testing. Development directly updates current fields, semantics, implementations, samples, and tests without incremental version bumping, legacy parsers, compatibility matrices, automatic migrations, or multi-version dispatch systems. Incompatible inputs MUST fail with concrete errors citing the current structure, references, units, and business rules. Format markers do not substitute for content validation; submitted identities and frozen inputs/results are preserved for traceability without promising that the current program will reinterpret historical formats.

Built-in generators, result adapters, and other proprietary protocols ship alongside the application so users never need to coordinate differing contract versions; capability selection still follows public capability declarations. Product versions, third-party software versions, and user content revisions MUST NOT be rewritten into contract versions. Formal compatibility and migration rules will be established prior to the `2.0` public release. This clause overrides any conflicting independent contract-bumping requirements elsewhere in this document or other manuals.

Invalid inputs, unregistered plugins, missing or unreadable objects, and internal exceptions MUST be visible; falling back to legacy logic, static mappings, default interfaces, empty arrays, or forged success results is strictly prohibited.

### 2.3 Module Autonomy with Public Boundaries

Each module owns its own domain rules, registry state, and persistence implementation, and interacts with other modules solely through public facades, immutable contracts, or events.

Cross-module code MUST NOT:

- Import functions, classes, constants, or variables whose names begin with a single underscore (`_`);
- Import another module's `loader`, `repository`, `persistence`, file-path helpers, or internal registries;
- Query or mutate another module's ORM models;
- Duplicate device, interface, algorithm, unit, or status mappings maintained by another module;
- Catch broad exceptions to probe whether another module is available.

### 2.4 Single Responsibility Is Not a Global Singleton

"Single source of truth" means a concept has exactly one authoritative owner—not that a global cross-module singleton object is constructed. Devices, modeling, generators, executors, and assembly each own their own registry state; storage owns a unified object protocol; and the composition layer merely wires these public capabilities together.

### 2.5 Visible Failures and State Integrity

Module initialization, registration, persistence, and task execution MUST satisfy all-or-nothing publication semantics ("all succeed or publish no new state"). Clearing state first and writing item by item, partial registration, partial compatibility, or swallowing exceptions is prohibited.

### 2.6 Validate Only at True Boundaries

Validation belongs solely at user-input boundaries and public-output boundaries of user-defined capabilities. Device model YAML, device data CSV, assembly YAML, finance YAML, planning/calculation config YAML, and Solver Bundle descriptors validate headers (`schema`/`schema_version`) and domain constraints before entering business facts; public outputs of user plugins or custom generators validate their contracts before entering result models.

Internal file handoffs, reads, persistence, snapshots, and runtime transitions MUST trust typed data and transactional state that have already crossed the boundary; code MUST NOT recompute, compare, or store SHA-256 hashes, content digests, checksums, or similar integrity fields, nor add tests for them. Runtime failures MUST report the actual business or execution error rather than duplicating checks as pre-validation.

## 3. System Boundaries and Overall Data Flow

The core business pipeline is:

```text
Device Technical Definitions / Series Data → Declarative Equations → Assembly & Validation → Computation Generation → Controlled Solving → Result Adaptation
                                                                            ↓                         ↓                      ↓                  ↓
                                                              Canonical Validated Assembly ──── Solver Bundle ──── Evidence / Result Objects
                                                                            └────────── Storage & Task Infrastructure ─────────────┘
```

The frontend-backend boundary is:

```text
User Forms / UI State
      ↓ Frontend mapper: formatting, percentages, simple display units, and DTO assembly
Public HTTP DTO
      ↓ Backend: structure, permissions, ranges, domain constraints, revision checks
Application Use Cases
      ↓ Public module protocols
Domain Modules / Storage / Tasks
      ↓ Assembly validation and canonicalization
ValidatedAssemblyArtifact
      ↓ GeneratorProvider unified unit conversion and file generation
Solver Bundle
      ↓ SolverRuntime controlled execution / ResultAdapter interpretation
Computation Results and Evidence
```

Frontend pre-validation serves immediate interactive feedback only; backend validation is always the authoritative gate. The frontend MUST NOT invoke Python functions directly, and the backend MUST NOT return ad-hoc structures tailored to a single React component.

## 4. Backend Modules and Responsibilities

Module responsibilities, public boundaries, inputs/outputs, failure semantics, and extension mechanisms have their sole module-level authoritative text under `modules/`; file fields, schemas, and enums have their sole field-level authoritative text under `formats/`. This section fixes only each module's owner, permitted dependencies, prohibited dependencies, and public boundaries.

### 4.1 `core`: Stateless Common Foundation

`core` provides only a stateless common vocabulary (diagnostics, units, time, IDs, and pure contracts) with no runtime state.

Prohibited:

- Static registries of devices, generators, or solvers;
- Business default values;
- Database access;
- Importing any business module.

### 4.2 `devices`: Device Specifications and Extension Discovery

`devices` owns device technical specifications, provider registration, and its module-local registry, exposing immutable `DeviceDescriptor` instances.

Prohibited: declaring independent semantic versions per device; carrying prices, costs, financial assumptions, functions, or executable entry points in device files; conflating technical model, finance category, and assembly instance identities; or guessing finance categories from names, carriers, or technical models.

File semantics, interface types, and edit-time gates are detailed in [Device Catalog](modules/devices.md) and [Device Model YAML](formats/device-model-yaml.md).

### 4.3 `modeling`: Declarative Technical Models

`modeling` owns the restricted equation language, common AST, and technical contribution contracts, producing declarative mathematical contributions.

Prohibited: embedding price/cost objectives; launching solvers; returning solver-private objects; reading device catalog paths, price files, or internal profile paths; or substituting device-ID branching or private command mappings for public technical equations.

See [Technical Equation Modeling](modules/modeling.md).

### 4.4 `assembly`: Interface Network Assembly and Synchronous Gate

Assembly is the synchronous gate into computation: it issues `ValidatedAssemblyArtifact` on success, and on failure MUST return diagnostics and block downstream computation.

Assembly does not select generators, solvers, calculation precision, or solve options, nor generate solver files or commands; it consumes only an immutable effective finance snapshot without inlining a second full finance configuration inside assembly. Every device instance MUST declare its finance binding explicitly, and energy purchase/sale metering uses explicit meter bindings.

Prohibited: guessing economic relevance from names, carriers, or technical models; defaulting interfaces to bidirectional; or allowing any computation path that bypasses assembly validation.

Authoritative schemas, fields, formulas, and aggregation enums are defined in [Assembly and Validation](modules/assembly.md), [Assembly YAML](formats/assembly-yaml.md), and [Finance YAML](formats/finance-yaml.md).

### 4.5 `computation`: Generation, Execution, and Result Adaptation

Computation responsibilities MUST maintain three distinct boundaries:

- `GeneratorProvider` directly accepts the issued `ValidatedAssemblyArtifact`, per-instance frozen device mathematical contributions, materialized full-horizon input series, immutable `EffectiveFinanceConfig`, publicly validated `CalculationConfig`, and confirmed frozen evaluation inputs, deterministically producing solver inputs and declarative variable/unit mappings (`variable_map`); inputs are passed separately by responsibility rather than bundled merely to preserve parameter count, without introducing outer wrappers or a second source of business truth;
- `SolverRuntime` executes controlled solver commands solely to emit a solver-neutral canonical primal solution (strictly ordered by 0-based input model columns) and `ExecutionReceipt`, without copying unrelated business files;
- `ResultAdapter` (`backend` cross-step coordination capability) streams `variable_map`, receipt, and canonical primal solutions inside an isolated resource-bounded child subprocess to faithfully restore a business-unit canonical engineering solution (identical in schema to unified `algorithm` outputs, reclaiming subprocess memory immediately upon exit to protect the main control-plane process, and never re-solving or certifying third-party solvers); downstream `Evaluator` plugins consume read-only canonical engineering solutions and framework-staged frozen context to emit standard financial evaluation facts or self-describing evaluation report blocks (`metrics` / `charts` / `tables`).

Built-in and third-party computation plugins follow the single-input/single-output container contract (`/work/in → /work/out`) along a **strict single-pass unidirectional pipeline**: `Frozen Input → [predictions[]: grouped per predictor plugin (supporting joint multi-target prediction)] → (generator → solver → backend bounded-subprocess engineering restoration | algorithm) → [evaluators[]: 0..N independent evaluators in parallel] → Sealed Result`. Cross-stage read-only context is staged into `/work/in` via zero-copy hardlinks by the orchestrator—plugins MUST NOT manually relay unrelated files, and cross-container loops are prohibited; non-feasible core solve outcomes skip solution-dependent evaluators, while under successful core solves the top-level task outcome reflects only the core solve, isolating individual failed evaluators with per-evaluator diagnostics and supporting isolated single-evaluator retry against the same frozen snapshot and sealed engineering solution; algorithms requiring internal iterative search execute self-contained inside one ephemeral container via `iteration`.

`GeneratorProvider` is the sole forward boundary converting business units to solver internal units, and the `backend` bounded-subprocess restoration layer is the sole reverse conversion boundary; stage-specific parameters freeze in dedicated slots at task submission. `SolverRuntime` MUST NOT read assembly semantics, inspect device types, perform business unit conversions, fill missing data, or switch solvers.

Computation modules MUST NOT:

- Access HTTP, cookies, databases, or object paths;
- Read frontend payload shapes;
- Guess units, invent business defaults, or accept unvalidated raw assemblies;
- Accept commands, scripts, implementation paths, or environment variables in assembly YAML;
- Execute solvers via shell command strings or let individual generators manage subprocesses directly.

Details of generation, execution, and result adaptation are defined in [Computation Generation and Solving](modules/engines.md), [Computation Generators](modules/generators.md), and [Solver Runtime](modules/solver-runtime.md).

### 4.6 `finance`: Financial Computation

Financial facts are organized into three layers: regional `FinanceProfile`, project `FinanceOverrides`, and the deterministic merger output—immutable `EffectiveFinanceConfig` (the sole finance snapshot consumed by assembly, planning, and financial calculation). Computation MUST NOT inherit profiles dynamically at runtime, read latest regional prices, or invent silent defaults.

Taxation and tax depreciation are financial rules. Specific methods and parameters are expressed in finance configuration, and decision-affecting relations participate in the same full-horizon planning solve; after-tax objectives, final evaluation, UI pages, and exports use the same frozen rules and the same single solve. Scope is project-investment after-tax optimization and evaluation; debt financing, equity evaluation, and tax filing are left to separate future planning.

`finance` owns financial rules and cash-flow semantics, `GeneratorProvider` incorporates decision-affecting relations into objectives and constraints, and `SolverRuntime` does not interpret tax policy. `finance` MUST NOT read prices from device technical definitions, depend on HTTP, databases, or frontend code, or depend back on application services. Classification of tax relations and amount/time bases follow [Finance Calculation](modules/finance.md#taxation-in-planning-and-cash-flows).

Cost functions, time bases, pricing, and binding rules are defined in [Finance Calculation](modules/finance.md), with field-level definitions in [Finance YAML Contract](formats/finance-yaml.md).

### 4.7 `analysis`: Result Analysis

Pure analysis capabilities responsible for sensitivity, batch sweeps, metrics, and assessment. System assessment and manual assessment MUST be distinct commands and DTOs; silently converting manual input into system assessment is prohibited.

### 4.8 `storage`: Objects and Reference Lifecycle

`storage` owns object IDs, reference lifecycle, cleanup, retention, and reconciliation, together with `BlobStore` providers and module health status.

Business modules MUST hold only public `ObjectId`/`ObjectHandle` values and MUST NOT construct paths, read other modules' ORMs, or write files directly to disk; reference manifests are the authoritative truth.

Protocols, retention policies, write/reference flows are defined in [Object Storage](modules/storage.md).

### 4.9 `application`: Cross-Module Use-Case Orchestration

Cross-module use-case orchestration belongs to the `application` layer: it owns transaction boundaries and invokes public interfaces across multiple modules.

The application layer MAY depend on multiple modules, but MUST NOT access module internal implementations; unbounded catch-all utility directories are prohibited anywhere in the codebase.

Ownership and transitional arrangements are documented in [Application Use Cases](modules/application.md).

### 4.10 `worker`: Asynchronous Execution and Resource Orchestration

Asynchronous task scheduling, I/O orchestration, and computation resource coordination converge into the `backend` control plane (`1.1.5` target architecture no longer runs standalone persistent compute/IO Worker daemons); the scheduling layer claims tasks, holds clockless execution leases, dispatches per-step ephemeral network-disabled containers through the container proxy, and commits stage checkpoints and final results, consuming only immutable snapshots. If snapshot dependencies fail to resolve, startup MUST fail or stay not-ready; claiming a task and degrading dynamically afterward is prohibited. The scheduling layer MUST NOT construct solver shell commands or interpret assembly business semantics. When the control plane is briefly unavailable, running ephemeral containers continue executing, and supervisory logic reconciles status and releases concurrency slots upon container exit. Administrators MAY revoke an attempt's write lease and requeue the task.

Execution workflows, leases, and commit rules are defined in [Worker](modules/worker.md).

### 4.11 `api`: Transport Adaptation

`api` is the transport adapter layer: it handles routing, authentication, DTOs, and use-case delegation only. Inside the `backend` process, the HTTP transport layer and asynchronous scheduling layer remain strictly isolated along module boundaries; synchronous HTTP routes MUST NOT run compute stages or invoke the container proxy inline.

Direct ORM querying, object path construction, domain calculations, cross-endpoint workflow orchestration, or returning legacy compatibility unions is prohibited.

Routes, DTOs, and error mappings are covered in [API Adapter](modules/api.md), and HTTP semantics and envelopes in [Public Contracts](contracts.md).

### 4.12 `persistence` / ORM

Database tables are owned by domain. ORM models are module-internal implementations, not cross-module DTOs, and grant no authorization for arbitrary cross-domain querying.

Table ownership, constraints, transaction implementations, and transitional rules are documented in [Persistence](modules/persistence.md).

## 5. Backend Dependency and Interaction Rules

### 5.1 Permitted Interaction Forms

Cross-module interactions are restricted to:

1. Calling facades exported from a module's `__init__.py` or explicit `public.py`;
2. Implementing a module's public `Protocol`/provider interface;
3. Passing immutable contracts, dataclasses, or Pydantic models;
4. Orchestrating multiple public interfaces via the `application` layer;
5. Publishing versioned events for behaviors not requiring immediate consistency.

Business modules MUST NOT share mutable dictionaries, SQLAlchemy `Session` internals, or raw file handles.

### 5.2 Composition Root

The application startup composition root is the sole location permitted to select concrete providers and perform dependency injection. It MAY:

- Discover and register device content providers, computation generators, executors, result adapters, and storage adapters (the unified equation parser/validator is a versioned public contract; per-device command registries MUST NOT be built);
- Verify that public IDs, versions, and dependencies resolve cleanly;
- Publish module health status.

It MUST NOT read module-internal registries, merge them into a new global authoritative table, or mutate business data.

### 5.3 Registries

- Each module owns its registry independently;
- Registered items MUST have stable IDs and explicit versions;
- A candidate registry MUST be built and validated completely before atomic publication;
- Startup MUST fail if any required provider fails;
- Runtime hot-reloading MUST NOT be implemented prior to public release;
- Static fallbacks, broad-exception fallbacks, and partial registry states are prohibited;
- `devices`, `computation`, and `storage` MUST NOT share registry objects; `modeling` and `assembly` MUST NOT build a second device registry.

Extension IDs SHOULD use stable namespaces such as `ies.device.pv` or `vendor.device.foo`. Device content is pinned by published `revision`; proprietary protocols comply with Section 2.2's single-current-contract rule prior to `2.0`. Third-party solver software versions retain their real values and MUST NOT be rewritten into proprietary contract versions.

### 5.4 Transactions

`application` use cases own transaction boundaries and `commit`/`rollback`; lower-layer modules MUST NOT issue global rollbacks over shared caller state. Operations spanning database and filesystem MUST NOT masquerade as single ACID transactions and MUST implement idempotent recovery protocols.

Repository constraints, upserts, and local rollback rules are covered in [Persistence](modules/persistence.md) and [Application Use Cases](modules/application.md).

### 5.5 Synchronous vs. Asynchronous Execution

- Fast validation, queries, and deterministic mutations use synchronous HTTP;
- Long-running calculations, analyses, imports, and report generation use Tasks;
- Task requests MUST carry an idempotency key;
- Tasks consume immutable snapshots rather than reading continuously mutating working drafts;
- Worker results commit as immutable evidence or objects, and state transitions MUST be retry-safe.

## 6. Target Directory Structure

Directories follow stable business boundaries; all new code MUST land within the target layout.

The repository root MUST remain the unified entry point and top-level capability directory without accumulating topic notes or ad-hoc review files:

```text
pIES/
├── README.md               # Sole project entry point linking the three official doc portals
├── LICENSE
├── AGENTS.md               # Agent operating constraints and Constitution entry
├── docker-compose.yml
├── manual/                 # User Guide, Developer Guide, Changelog / Roadmap
├── docs/                   # Review evidence and read-only historical archive
├── backend/
├── frontend/
└── data/                   # Runtime data; not a documentation source
```

The official product documentation directory MUST use the proper English spelling `manual/`—never the misspelled `mannal/`. `README.md` remains the project entry point without introducing competing site roots.

The backend is organized by stable business boundaries: `bootstrap` (composition root and startup validation), `core` (stateless common foundation), `devices`/`modeling`/`assembly`/`computation`/`finance`/`analysis` (domain capabilities), `storage` (objects and references), `application` (cross-module orchestration), `api` (HTTP adapters), `worker` (asynchronous scheduling and stage orchestration), and `migrations` (versioned database migrations). Runtime persistent services converge to `web`, `backend`, `job_runner`, `postgres`, and `redis`, while heavy computation stages launch ephemeral network-disabled containers on demand. The frontend is sliced vertically by feature: `app/pages` handles route-level composition, `features` own each business capability's contracts, models, forms, mappers, queries, and components, and `shared` contains only business-agnostic utilities.

Dependency direction MUST flow strictly as: backend `api/worker → application → domain modules → core`, with persistence belonging solely to each domain owner; frontend `app/pages → features → shared`, where features MUST NOT import each other's internal files and cross-feature workflows live in pages or explicit application hooks.

High-level directory responsibilities and dependency directions appear in [System Architecture Blueprint](architecture.md); module code boundaries appear under `modules/`, and frontend layering under [Frontend and Help Center](frontend.md). This Constitution no longer maintains a static file inventory.

## 7. Data Types and Serialization Standards

### 7.1 Basic Formatting

- HTTP JSON uses UTF-8 and `application/json`;
- Field names use `snake_case` consistently across frontend and backend contracts without implicit camelCase conversion;
- Every long-lived document, snapshot, assembly file, evidence package, and plugin spec MUST include `schema_version`; plugin files also carry an independent `schema` identifier;
- Enum values use lowercase `snake_case`, and values outside the declared enum MUST be rejected;
- Broad untyped maps MUST NOT replace stabilized public DTOs;
- A single field MUST NOT accept multiple untagged representations.

Schema identifiers, versions, filenames, and authoring subsets are defined in [File Format Standards](file-formats.md) and the corresponding pages under `formats/`.

### 7.2 Identifiers

- External entity identifiers MUST transmit as opaque strings, MUST NOT participate in arithmetic, and MUST NOT be conflated with display names, arbitrary strings, or array indices;
- Stable extension IDs use namespaced strings; device equations are pinned to device publication `revision` rather than carrying separate command IDs;
- Object identifiers are opaque strings rather than content digests used as business identities.

### 7.3 Numbers

- Continuous physical quantities use JSON numbers; monetary amounts and decimals requiring exact round-tripping use decimal fixed-point strings;
- Canonical API values for ratios uniformly use `0..1`; integer counts and years MUST be JSON integers without implicit string-to-number coercion;
- `NaN`, `Infinity`, and `-Infinity` MUST NOT enter JSON, databases, or snapshots;
- `null` represents only domain-permitted "no value"—never a load failure or compatibility fallback.

Internal computation and storage types are defined in [Public Contracts](contracts.md).

### 7.4 Units

The system distinguishes three unit representations:

1. Frontend display units;
2. Canonical units specified by API and business contracts;
3. Generator and solver internal units.

Rules:

- Units MUST be explicitly declared by the governing schema;
- Frontend mappers perform simple display conversions only; the backend validates units, dimensions, ranges, and domain constraints;
- Assembly proves dimensional compatibility and preserves explicit business units; conversion from business units to solver internal units occurs solely at the `GeneratorProvider` boundary, and conversion back occurs solely at the `ResultAdapter` boundary;
- Implicit unit conversions are prohibited inside equation parsing, runtime execution, and business services.

Complete rules appear in [Public Contracts](contracts.md).

### 7.5 Time

- Timestamps use ISO 8601 UTC strings ending in `Z`; durations use explicit units or ISO 8601 durations rather than ambiguous integers;
- Each project permanently fixes an immutable computation baseline at creation, which cannot be modified afterward;
- Project computation series use contiguous `step` indices starting from `0` without timestamps or time zones; no resampling, interpolation, aggregation, blending, or auto-padding occurs prior to materialization; materialized point counts MUST match `step` exactly.

Complete time and collection rules appear in [Public Contracts](contracts.md), and series file rules in [Device Data CSV](formats/device-data-csv.md).

### 7.6 Collections and Ordering

- Whether a list is ordered MUST be explicit in its DTO; collections requiring deterministic output sort by stable public keys;
- Field ordering within a JSON object carries no semantic meaning;
- Paginated lists use cursor pagination rather than unstable page numbers across concurrent writes as a long-term contract.

Complete collection and large-array rules appear in [Public Contracts](contracts.md).

### 7.7 Immutable Revisions and Provenance

- Snapshots, versions, evidence, and published results MUST NOT be mutated in place;
- Canonicalization algorithms MUST be unique and implemented by public pure functions;
- `schema_version`, creation provenance, and dependency versions MUST be persisted together;
- Drafts are mutable, but each persistence forms a new revision without overwriting historical objects.

### 7.8 Public File Contracts

- Unknown core fields in public files are rejected by default; extensions belong solely under namespaced `extensions` keys and MUST NOT alter core semantics or security rules;
- Device model YAML holds pure technical semantics only; assembly YAML and Bundles MUST NOT carry executable entry points, host paths, or shell invocations;
- Proprietary `schema` markers remain fixed at `1.0.0` prior to `2.0` release and interpret only the current contract; old numbers MUST NOT act as dispatch switches or silent downgrade triggers.

Exact fields, versions, authoring subsets, and examples are defined in [File Format Standards](file-formats.md) and pages under `formats/`; financial bases are specified in [Finance Calculation](modules/finance.md).

## 8. HTTP API Contracts

API is the transport adapter layer: it handles routing, authentication, DTOs, and use-case delegation only.

Prohibited: querying ORMs directly; constructing object paths; implementing domain logic; orchestrating cross-endpoint workflows; returning compatibility response unions; exposing ORM objects, file paths, internal function names, or registry objects; masking missing required fields with defaults; or catching errors to return empty lists, "No data", or HTTP `200` masquerading as success.

Success responses, error envelopes, status codes, wrapper keys, error codes, idempotency, and upload/download rules are defined in [Public Contracts](contracts.md); module responsibilities appear in [API Adapter](modules/api.md).

## 9. Frontend Responsibilities and Interaction

The frontend owns display state and unsubmitted form inputs, never authoritative business facts; authoritative validation of permissions, structure, ranges, dimensions, and domain rules belongs to the backend. Frontend pre-checks provide immediate feedback only. Sign-in and canvas saves follow actual HTTP request results.

Prohibited: calling Python functions directly from the frontend; returning component-specific backend shapes; fabricating resources, dropping inputs, or guessing legacy response shapes; or interpreting errors as "No data".

The canvas uses real backend interface IDs and connects according to public interface semantics. Directory structure, mappers, state, schema-driven GUI, and Help Center behavior are authoritatively defined in [Frontend and Help Center](frontend.md).

## 10. Storage and Data Lifecycle

Business modules hold only `ObjectId`/`ObjectHandle` values while paths are resolved by the storage adapter; reference manifests are the authoritative truth for whether an object may be cleaned up. Administrators may preview orphan objects, and cleanup deletes objects that still have no references and satisfy business retention rules.

Public protocols, path rules, reference flows, retention periods, recovery, and admin APIs are authoritatively defined in [Object Storage](modules/storage.md).

## 11. Database and Persistence

After public release, schema changes MUST use versioned migrations; development databases initialize from the current schema. Constraints and immutability are enforced via database constraints and required triggers; repositories belong solely to the domain owner of their tables; cross-domain reads go through public read models or application queries without importing another module's ORM; audit records are immutable; deletions MUST be explicit; and secrets MUST NOT enter ordinary DTOs, logs, or evidence packages.

Full rules appear in [Persistence](modules/persistence.md).

## 12. Snapshots, Tasks, and Results

- Tasks MUST bind an immutable `CalcSnapshot` and MUST NOT re-read a changing project draft during execution;
- Duplicate submissions sharing the same idempotency key return the same logical task without re-executing;
- Failed outcomes MUST preserve structured diagnostics;
- Results, evidence, and assessments MUST be traceable to task, attempt, snapshot, and object;
- Historical results MUST NOT be reinterpreted by re-reading current configuration.

Snapshot contents, attempt sealing, Bundle/receipt semantics, and orchestration are defined in [Domain Model and Traceability Chain](domain-model.md), [Worker](modules/worker.md), and [Computation Generation and Solving](modules/engines.md).

## 13. Failure and Health Semantics

System-wide principles: failures MUST be visible, MUST NOT fall back, MUST NOT forge success, and MUST preserve state integrity.

- Missing required provider or dependency: startup MUST fail or stay not-ready without publishing partial registry state;
- Unavailable database, storage, or queue: reflect clearly in readiness or task status and fail dependent requests explicitly; real write failures MUST fail the operation rather than substituting capacity guesses; health endpoints read only dependency state and storage metadata;
- Missing or unreadable object: return the real error; never substitute empty content or stale copies;
- Invalid project/assembly input: return full diagnostics and block task submission;
- Retryable external failure: retry under explicit policy while preserving attempts and causes;
- Internal exceptions: log request ID and stack trace server-side while returning a standard error envelope to the client.

`healthz` indicates process liveness only; `readyz` indicates all dependencies necessary to serve traffic/tasks are available. "Degraded" health statuses MUST NOT mask states that cannot actually serve requests properly.

Module-specific failure states and handling appear in `modules/` manuals (generation/execution/adaptation in [Computation Generation and Solving](modules/engines.md), task execution in [Worker](modules/worker.md), objects in [Object Storage](modules/storage.md)).

## 14. Testing and Architecture Gates

### 14.1 Environment

All compilation, testing, formatting, and database verification MUST execute inside Docker containers without installing or executing project dependencies on the host. Only this repository and `/tmp` may be read or written.

### 14.2 Testing Principles

Every change MUST be covered proportionally to risk: public contracts, transactions, permissions, and critical user workflows MUST have tests; tests MUST verify key business values, units, IDs, versions, and error semantics rather than merely asserting field presence.

Complete rules for test layers, static architecture gates, definition of done, and change notes live in `docs/development/development-workflow.md` (internal process control); module routing appears in [Module Development Manual](module-development.md), and Docker workflows in [Deployment and Operations](deployment.md).

### 14.3 Real User Acceptance

User-visible features MUST undergo browser-level acceptance before merge: start from page entry points and execute real business actions using accessible locators; GUI workflows including canvas drag-and-drop and port connections uniformly use Playwright; skipping tested steps via state injection is prohibited; both Playwright and the system under test MUST run in Docker. Pure HTTP contract tests cannot substitute for browser acceptance.

Complete browser acceptance workflows and coverage rules appear in [Frontend and Help Center](frontend.md).

## 15. Project Documentation

### 15.1 Single Official Documentation System

Long-term usage instructions, design blueprints, version history, and development sequencing converge in `manual/`. `docs/development/` MAY hold handoffs and process control for a specific implementation effort, but MUST NOT form a parallel product, architecture, or version specification:

```text
README.md                                  # Sole top-level repository entry
manual/                                    # Sole long-term authoritative source
├── README.md                              # Three official entry points
├── SUMMARY.md
├── SUMMARY.<locale>.md
├── user-guide/                            # User Guide
├── developer-guide/                       # Developer Guide & Architecture Constitution
└── changelog/                             # Reverse-chronological Changelog & Roadmap

docs/                                      # Background evidence; not development input
├── README.md
├── development/                           # Internal handoffs & process control; no long-term rules
├── reviews/                               # Dated & baselined review snapshots
└── archive/                               # Unmaintained historical materials
```

`manual/` is the sole authoritative text shared by the Help Center and repository readers. `docs/development/` explains how to execute a piece of work only; `docs/reviews/` provides evidence only; `docs/archive/` carries no current normative force.

### 15.2 Root Entry and Three Official Portals

- Root `README.md` MUST stay concise and actionable, linking the User Guide, Developer Guide, Changelog, Reviews/Archive, License, and security notes;
- `manual/README.md` MUST provide three parallel entry points: "User Guide", "Developer Guide", and "Changelog";
- The Architecture Constitution MUST remain at its fixed path under the Developer Guide as the supreme specification;
- Neither the root README nor portal pages may duplicate full manuals or the Roadmap;
- Any documentation reorganization MUST keep entry links, locale SUMMARY files, and Help Center routes valid.

### 15.3 User Guide

The User Guide targets first-time planning engineers, energy engineers, and system administrators and is organized by GUI task. Every task MUST explain:

1. Prerequisites before starting;
2. Pages, buttons, and fields;
3. Actionable step-by-step instructions;
4. Observable expected results;
5. Failure handling, permissions, and current limits.

The guide MUST describe only actions actually achievable in the current GUI—never inferring GUI availability from backend endpoints. Exposing ORMs, private functions, internal paths, stack traces, implementation debates, or unhelpful backend jargon is prohibited.

### 15.4 Developer Guide

The Developer Guide is the design blueprint for ongoing development, serving maintainers, integration developers, extension authors, and contributors. It MUST cover at least:

- Architectural principles, system context, module public boundaries, and dependency direction;
- Domain model, immutable traceability chain, and object lifecycle;
- Public HTTP, DTO, diagnostics, unit, time, and versioning rules;
- Device model YAML, device data CSV, assembly YAML, Solver Bundle, and device, modeling, generator, executor, result adapter, storage, and data providers;
- Frontend, Help Center, deployment, operations, and verifiable quality boundaries.

The Developer Guide describes stable intent, public invariants, and extension mechanisms; it MUST NOT list file line numbers, private function signatures, ORM table listings, migration SQL, agent task assignments, release schedules, work slices, or current completion status. It MAY provide self-contained rules and examples, and MUST NOT express implementation sequence by citing the Roadmap. Authoritative ownership across stable rules is specified in [Normative Index and Document Boundaries](normative-index.md): the Constitution defines principles, `formats/` defines files, `contracts.md` defines HTTP, `modules/` defines modules, and Roadmap defines release plans only.

### 15.5 Changelog and Roadmap

`manual/changelog/` is an independent path alongside the Developer Guide: the Changelog records completed facts, and the Roadmap is the sole source of release plans and development order. Neither defines architecture, file formats, or public contracts; detailed maintenance workflows belong to internal process control and do not enter the Developer Guide.

### 15.6 Reviews and Archive

- New reviews belong only under `docs/reviews/` and MUST record date, target, commit baseline, conclusion, and verification evidence;
- Reviews do not automatically represent current truth or serve directly as implementation specifications;
- Contracts, specifications, plans, investigations, ADRs, and workflows that have been distilled, superseded, completed, conflicted, or retired MUST move into `docs/archive/` within the same change rather than lingering at active entry points;
- Every newly archived artifact MUST declare its status, archive date, reason, and current replacement source at the top; archived text preserves historical meaning without repeated rewrites for current rules; internal links may go stale, which the index MUST clearly warn;
- Stable conclusions from reviews or archives become long-term norms only once distilled into `manual/`.

### 15.7 Documentation Metadata and Naming

Long-term specifications, ADRs, and reviews SHOULD declare title, stable ID, status, version or date, scope, owner, upstream basis, and supersession relations. Internal implementation materials MUST declare their non-normative nature, corresponding Roadmap item, and authoritative spec links; archived materials follow Section 15.6.

Regular filenames use lowercase `kebab-case`; conventional root files and this Constitution MAY use uppercase. Ambiguous names such as `new` or `final2` are prohibited. Dates apply to review snapshots and MUST NOT replace stable section IDs.

### 15.8 Synchronizing Documentation with Code

- When user-visible behavior, fields, units, errors, or pages change, update the User Guide and Changelog in the same change;
- When public contracts, extensions, architecture, deployment, or maintenance practices change, update the Developer Guide and Changelog in the same change;
- Subsequent plan changes update only the Roadmap; the Developer Guide MUST NOT restate or link planned work order;
- Breaking architectural changes MUST first submit an ADR and amend this Constitution per Section 17;
- Generated content such as OpenAPI specs, schemas, and error tables MUST derive from their authoritative source without manual second copies;
- Examples of current behavior MUST run on the current version; examples labeled "Effective Target Contract" MUST be schema-complete without claiming current-code compatibility; no example may contain real secrets;
- Removing or moving documents MUST repair all references in active documentation, code comments, test notes, and entry links.

### 15.9 Documentation Acceptance

Documentation changes MUST verify at minimum:

- Root README, three official portals, Review/Archive entries, and all SUMMARY links resolve cleanly;
- User Guide tasks are executable from the GUI by a novice without presenting target state as existing functionality;
- Developer Guide captures key architectural intent without duplicating backend implementation internals, release schedules, or execution workflow;
- Changelog reverse-chronological order, Roadmap ordering, and three-part semantic versions remain consistent;
- Commands use Docker workflows only and match actual compose service names;
- Example DTOs, enums, units, and error envelopes conform to public contracts;
- Markdown syntax, code blocks, tables, heading hierarchy, and published locale navigation pass validation.

### 15.10 Web Help Center

The unified top-bar entry is named "帮助中心" (English: `Help Center`) and renders official Markdown directly from `manual/` without maintaining a second source text.

The Help Center MUST:

- Display "User Guide", "Developer Guide", and "Changelog" as three top-level nodes in a unified navigation tree;
- Register available locales in `manual/SUMMARY.md` and define chapter order and stable IDs in `manual/SUMMARY.<locale>.md`;
- Support deep links, heading anchors, previous/next navigation, current-node indicator, mobile TOC drawer, and keyboard navigation;
- Support required Markdown constructs while rejecting raw HTML, scripts, inline event handlers, and unsafe URLs;
- Generate a read-only content manifest during the frontend build without reading host files at runtime or calling the backend for simple Markdown conversion;
- Explicitly list available languages when a requested language is missing rather than silently displaying the wrong language;
- Remain statically readable even when backend, database, and Worker services are unavailable.

## 16. Security and Auditing

The default deployment model is centralized administrator deployment with ordinary users accessing via GUI and API. Administrators are trusted operators; the security clauses below protect against unauthenticated requests, external actors, privilege escalation by ordinary users, and untrusted inputs—never treating intentional administrator changes as attacks or requiring mutual admin isolation or dual approval.

- The backend is always authoritative for authentication, authorization, and business permissions; frontend route guards improve UX only and never substitute for backend authorization;
- Sign-in issues independent sessions, stores passwords with bcrypt, hashes session tokens at rest, and enforces explicit lifetimes. Account deactivation and credential rotation invalidate existing sessions uniformly. Browser session cookies use `HttpOnly` and `SameSite=Lax`. Export downloads verify current session and project permissions;
- Critical mutations emit immutable, minimally redacted audit records without storing passwords, raw tokens, or unbounded payloads; routine reads and everyday operations do not require heavy auditing;
- Error responses MUST NOT leak stack traces, SQL, host absolute paths, or provider credentials;
- Assembly and device files MUST NOT expose arbitrary code execution entry points;
- Both built-in and third-party computation steps (prediction, generation, solving, post-solve financial evaluation, and unified algorithms) run inside ephemeral containers with no business credentials, disabled networking (`--network=none`), a read-only root filesystem, and explicit resource caps, and both user cancellation and timeouts MUST immediately terminate the computation and clean up the container and temporary files after completing status and valid output handoff. `backend` and ephemeral computation containers MUST NOT mount the host Docker socket directly; all image builds and ephemeral container lifecycles execute strictly through the thin allowlisted proxy `job_runner`. Computation traceability is guaranteed by immutable frozen input files, task snapshots, and persisted outputs/evidence rather than mandating immutable or identical runtime environments/images for ephemeral containers.

Solver isolation and execution details appear in [Solver Runtime](modules/solver-runtime.md) and [Deployment and Operations](deployment.md).

## 17. Change and Constitutional Amendment Process

Routine implementations MUST NOT bypass this document. When mandatory clauses truly must change, an ADR MUST be submitted beforehand detailing at least:

- The problem to solve and supporting evidence;
- The specific clauses amended;
- Impact on openness, correctness, security, and migration;
- Rejected alternatives;
- Frontend, backend, data, and test migration plan;
- Rollback plan.

Once the ADR receives explicit approval, this Constitution's version MUST be updated in the same change. Temporary performance tweaks, schedule pressure, or "existing tests work that way" never justify violating boundaries.

During review, an ADR serves as decision-review material under `docs/reviews/`; once approved and reflected in this Constitution or the Developer Guide, the original ADR moves to `docs/archive/adr/` so no competing long-term specification remains.

## 18. Versioning and Planning Boundaries

Product versions use `MAJOR.MINOR.PATCH`: incompatible architectural or public contract changes bump MAJOR, backward-compatible features bump MINOR, and bug fixes bump PATCH. Concrete product version numbers, release procedures, completed milestones, upcoming plans, and exit criteria are maintained separately in version metadata, the Changelog, and the Roadmap—never duplicated in this Constitution or the Developer Guide.

No implementation plan or handoff may reintroduce global registries, assembly fallbacks, legacy API compatibility forwarding, empty-data degradation, or any other path violating this Constitution.

## 19. Final Acceptance Principles

An open, clear pIES system satisfies:

- Adding a new device requires no changes to frontend interface rules or legacy static registries;
- Adding a new device technical model and sample data requires delivering only standard public YAML/CSV artifacts without exposing implementation paths or embedding prices, costs, or solver precision;
- Adding a new generator requires no changes to mapping tables in assembly, API, GUI, or general runtime;
- Adding a new solver primarily adds generator/result-adapter/executor providers without having Workers assemble command strings;
- Swapping object storage requires no changes to projects, datasets, results, or frontend;
- Failure in any module never exposes half-initialized state;
- Historical tasks can rebuild the exact same Solver Bundle from their snapshot and versioned dependencies and explain actual execution via the Bundle and receipt;
- The frontend submits canonical inputs demanded by backend public contracts without offloading simple UI conversions to the backend;
- Modules invoke only other modules' public interfaces without penetrating internal directories or ORMs;
- Errors are never masked by compatibility layers, fallbacks, or fabricated empty data;
- Performance optimizations may swap implementations without altering authoritative data or public semantics.
