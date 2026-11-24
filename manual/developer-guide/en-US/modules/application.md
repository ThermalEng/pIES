# Application Use Cases

> Code Boundary: `backend/iesplan/application/`; Governing Spec: [Architecture Constitution](../ARCHITECTURE_CONSTITUTION.md)

`application` orchestrates complete cross-module user business actions, owning database transaction boundaries, business-level authorization checks, and cross-resource recovery logic while exposing transport-agnostic public use-case entry points for both `api` (synchronous requests) and `worker` (staged background commands).

## Responsibilities and Boundaries

- **Owns**: Accepting `ActorContext` and use-case commands; verifying business permissions, optimistic lock revisions (`expected_revision`), and idempotency keys; coordinating multiple domain public facades; owning database `commit`/`rollback` boundaries; and coordinating object references, task creation, and immutable audit logs.
- **Does not own**: Device equation or financial math evaluation, cross-module ORM table definitions, HTTP cookie/response transport details, or the internal execution state machine of long-running worker processes.
- **Worker staged commands (`application.worker`)**: Long-running jobs MUST NOT hold one multi-hour database transaction open across solving. `application.worker` exposes short-transaction public commands to the Worker daemon for task claiming, progress updates, stage checkpoint commits, lease fencing verification, admin unlocking, and invoking computation/analysis domain facades; streaming file scans and solver execution hold no database connection.

## Standard Use-Case Execution Model

```text
Receive ActorContext + Command
  ↓ Load target resources & enforce backend authorization
  ↓ Verify optimistic lock revision / idempotency key / lifecycle state
  ↓ Invoke public domain module facades (devices / modeling / assembly / finance / storage)
  ↓ Coordinate persistence, ObjectOwner references & redacted audit events
Commit DB transaction & return ApplicationResult
```

Repositories perform `flush` or local `savepoint` only and MUST NOT override the caller's global transaction; the same use case SHOULD be reusable from HTTP handlers, background tasks, and CLI tools alike.

## Primary Use-Case Families

| Family | Summary of Responsibilities |
|---|---|
| `identity` | Authentication, multi-session & idle renewal, password changes, roles, admin account management |
| `projects` | Project creation, draft reads/writes, manual version sealing/restore, archiving, confirmed deletion |
| `datasets` | Device dataset & price CSV upload validation, quality diagnostics, version history, project binding |
| `modeling` | Project device instances, property overrides, topology edges, candidate model validation/saving & atomic ID assignment |
| `planning` / `finance` / `calculation` | Planning config, finance triplet merge & effective snapshot refs, calculation config revision management |
| `validation` / `tasks` / `results` | Full project assembly validation, snapshot freezing, idempotent task creation, result projection, scheme application |
| `exports` / `packages` | Excel report generation, offline project package export, two-phase import proposal & confirmation |
| `operations` | Aggregated service health, storage capacity inspection, admin audit log querying |
| `customizations` (`1.2.0` target) | Lifecycle orchestration for local models/plugins and pIES Market (Local/Online dual mode) |

## Key Cross-Module Invariants

- **Project model save consistency**: Candidate model bytes pass to `devices` for parsing and validation; within the same transaction, an incrementing non-reused suffix is allocated atomically alongside storing canonical YAML, validation receipt, and catalog entry—any failure rolls back cleanly without consuming an ID.
- **Computation submission and queue reliability**: Submission use cases validate assembly, freeze `CalcSnapshot`, insert the idempotent `Task`, and attach snapshot object references within one transaction; queue messages publish after commit, and even if a queue message is lost Workers still rediscover queued tasks from the authoritative task table.
- **Cross-store interruption convergence**: Confirmed interruptions between the database and object storage retry idempotently via explicit operation IDs without silently ignoring failures or scanning unrelated objects.
