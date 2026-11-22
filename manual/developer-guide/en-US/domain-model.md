# Domain Model and Traceability Chain

> Status: Effective Blueprint; Governing Spec: [Architecture Constitution](ARCHITECTURE_CONSTITUTION.md)

## Data Lifecycle Classification

pIES classifies all business data into three categories by mutability and authority:

| Category | Typical Entities | Lifecycle Rules |
|---|---|---|
| Editable Working State | Project drafts, unsubmitted forms | Mutable; each save increments `revision` under optimistic locking |
| Immutable Business Evidence | Project versions, dataset versions, calc snapshots, execution receipts, compute results, assessment records | Append-only, never mutated in place; supports full historical traceability |
| Rebuildable Runtime State | Redis queue hints, sub-second progress, caches, query index projections | Rebuildable from authoritative database or raw evidence if lost or corrupted |

PostgreSQL holds all structured transactional facts and reference indices; large immutable file payloads reside in object storage, with business tables holding only opaque `ObjectId` references and ownership records.

## Project and Workspace Aggregate

A project holds its owner, currency, immutable computation baseline, and lifecycle state (`active` / `archived`).

- **Immutable computation baseline**: Resolution, scenario mode (currently `single`), and starting Gregorian year (leap years derived year-by-year from the Gregorian calendar with no separate leap-year setting) are fixed once at project creation and cannot be edited afterward.
- **Drafts and versions**: A project draft stores the live system graph, dataset bindings, finance configuration pointers, planning configuration, and project model catalog; autosave updates only the draft revision without creating project versions. A project version explicitly seals the draft into an immutable snapshot at a point in time; restoring a historical version derives a new version and resets the working draft to that version while leaving prior versions and tasks untouched.
- **Project model catalog**: Project models MUST first pass backend validation covering the safe YAML subset, units, interfaces, and equations before an atomic transaction assigns an in-project unique ID and writes to the catalog; failed candidates never enter the official catalog or consume IDs.
- **Loading a task into draft**: Loading inputs from a historical task restores that task's frozen snapshot inputs into the editable draft without automatically launching a new task; only an explicit calculation submission freezes the draft into a new task.
- **Project package import**: Importing an offline project package (`.zip`) creates a standalone new project owned by the importer. Upload parses structure and stages a proposal; only explicit user confirmation reconstructs models, datasets, historical versions, and dependency mappings within a single database transaction, preserving historical results as imported evidence provenance rather than fabricating local compute tasks.

## Datasets and Series Sources

Dataset containers manage immutable data versions created across uploads. Each version records original sampling interval, field definitions, units, quality diagnostics, and object references.

External data bindings for predefined device interfaces are declared explicitly under `predefined_interfaces` on assembly instances, supporting four source modes: `constant`, `data_repeat`, `data_full`, and `data_predict`. Both import and assembly boundaries strictly validate resolution, contiguous `step` progression, valid value ranges, and mode coverage rules without implicit resampling or interpolation during preprocessing. Full-horizon aligned series are materialized at calculation time from the project baseline and confirmed operating years.

## Planning, Finance, and Calculation Configuration

Three configuration pillars carry distinct responsibilities and revision histories:

- **Planning configuration**: Declares the objective function, planning variables (bound to instance technical parameters and bounds), construction units, and planning/system constraints. Existing devices (`existing`) treat historical outlays as sunk costs excluded from new construction capex; new devices (`new`) follow explicit cost bindings.
- **Finance triplet**: Comprises regional `FinanceProfile` (reusable regional baseline), project `FinanceOverrides` (sparse atomic overrides over existing profile items), and deterministic merger output immutable `EffectiveFinanceConfig`. Assembly, planning, and financial evaluation consume the same `EffectiveFinanceConfig` snapshot without dynamically inheriting external prices at runtime.
- **Calculation configuration**: Defines calculation mode, prediction algorithms and parameters, exact generator and solver refs, precision, coupled prediction convergence criteria, time limit, solver options, random seed, and requested outputs. Calculation configuration does not enter `ValidatedAssemblyArtifact`; instead, it freezes alongside the canonical assembly inside `CalcSnapshot` at task submission.

## User-Defined Content (`1.1.0` Target)

User-defined model libraries and algorithm plugins planned for subsequent releases adopt a "mutable draft + immutable publication" split:

- Forms, online YAML editing, and YAML uploads all converge to the same canonical `ies.device-model` YAML object, pinned by publication `revision` without per-device semantic versions;
- Algorithm plugin packages deliver generator/solver code and offline dependency locks in `ies.algorithm-plugin` ZIP archives validated and run inside isolated containers;
- Shared hub approvals target exact immutable publications; installing into another user's catalog creates a reference record without copying underlying objects, and unsharing or uninstalling never breaks historical projects or frozen tasks.

## Traceability Chain Across Snapshots, Tasks, and Results

```text
Project Version + Dataset Versions + Planning / Finance / Calc Config Revisions
                                    ↓
                        ValidatedAssemblyArtifact
                                    ↓
                              CalcSnapshot
                                    ↓
                             Task → Attempt
                                    ↓
                Full-Horizon Materialization → Solver Bundle
                                    ↓
                        ExecutionReceipt → Result
                                    ↓
       [Coupled Prediction: feedback updates series & rebuilds solve]
                                    ↓
                      Evidence / Assessment / Export
```

- **Immutable snapshot**: At submission, `CalcSnapshot` seals the canonical assembly artifact, assembly validation receipt, frozen per-instance device math contributions, all three config revisions, prediction/solver dependency versions, confirmed evaluation parameters, and comparison baseline. Task execution and retries read only the snapshot, isolated from later draft changes.
- **Separation of technical status and business outcome**: Task completion (`completed`) indicates execution finished; business outcomes MAY still be optimal, restricted feasible (e.g., time limit reached with a feasible incumbent), or no feasible solution / insufficient evidence.
- **Result selection and Apply scheme**: Selecting a candidate records adoption preference on the task result only; **Apply scheme** is an explicit version operation that writes solved planning parameter values onto device instances on top of the snapshot's source project version, deriving a new project version and making it the current workspace. Before execution, a confirmation prompt shows impact scope while keeping historical evidence and source versions immutable.

## Object Lifecycle

- All large files write as immutable objects through `storage`; business modules hold only `ObjectId` values and explicit `ObjectOwner` references.
- Creating, replacing, or publishing assets attaches/detaches references atomically; objects with active references MUST NOT be deleted.
- Administrators MAY preview unreferenced objects for cleanup; executing cleanup deletes objects that remain unreferenced and satisfy retention rules.

## Identity, Permissions, and Auditing

- Authentication and authorization are enforced exclusively by the backend; frontend route guards provide UX guidance only. The system distinguishes project owners (standard engineers) from trusted operational administrators (managing accounts, storage, and global operational audits).
- Each account supports multiple independent active sessions: signing out revokes only the current session, whereas changing password or admin deactivation invalidates all existing sessions. Session lifetime is an idle timeout extended by real user actions on visible pages via the session renewal endpoint (background tasks are never cancelled on session expiry); expired, revoked, or credential-rotated sessions require signing in again.
- Critical operations (permission changes, object cleanup, project/config publication, version restore, system assessments, etc.) write immutable redacted audit logs recording actor, action, target entity, and trace ID without storing passwords, tokens, or large payloads. Administrators inspect audit logs via a read-only cursor-paginated API.

## Modeling and Design Reference

When adding new business data, choose its storage and versioning shape as follows:

1. **Repeatedly edited**: Model a working draft table protected by `revision` optimistic locking;
2. **Referenced by tasks, approvals, or external callers**: Create immutable versions and forbid in-place updates;
3. **Large payload or file stream**: Store bytes in object storage and persist only `ObjectHandle` in the database;
4. **Recomputable from database truth if lost**: Treat as rebuildable cache or projection index, never an authoritative source of truth.
