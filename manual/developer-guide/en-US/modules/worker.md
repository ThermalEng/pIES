# Worker

> Code Boundary: `backend/iesplan/worker/`; Governing Spec: [Architecture Constitution](../ARCHITECTURE_CONSTITUTION.md)

`worker` is the persistent daemon executing long-running background jobs (full-horizon planning solves, sensitivity/batch analyses, project package exports, and large report exports), ensuring single effective commitment and complete evidence traceability across concurrent claiming, process crashes, timeouts, cancellations, and stale writes.

## Responsibilities and Boundaries

- **Owns**: Task queue discovery and atomic claiming; Attempt lease holding, Fencing Token validation, and admin unlock recovery; isolated task subprocess supervision and stop-signal propagation; computation stage orchestration (materialization → generation → execution → adaptation → financial evaluation) and stage checkpoint recovery.
- **Does not own**: Modifying project drafts or re-reading unfrozen configuration; assembling solver shell commands or interpreting device/assembly semantics; executing raw DB connections or transactions directly (all persistence goes through short-transaction `application.worker` commands).
- **Algorithm plugin payload boundary (`1.1.0` target)**: User algorithm plugins delegate through the generic sandbox Provider to the isolated `plugin_runner` container; the Worker process itself never unpacks or imports user source code.

## Execution State Machine and Process Isolation

```text
Queue hint / DB pending-task scan
  ↓ Atomic claim (assign Attempt + Lease + Fencing Token)
Control process (claims task, emits heartbeats, monitors stop signals & supervises task process)
  │
  └─ Spawn isolated task subprocess (wired via composition root; loads CalcSnapshot lock-free)
       ↓ [Stage 1] prediction: series prediction & materialization ──→ commit prediction checkpoint
       ↓ [Stage 2] generated: GeneratorProvider builds Bundle ──→ commit generated checkpoint
       ↓ [Stage 3] execution: SolverRuntime isolated run ──→ commit execution checkpoint, then clean tempdir
       ↓ [Stage 4] ResultAdapter adaptation + financial evaluation
       ↓ Verify lease still valid (Fencing)
       └─ Atomically publish Evidence, query projection & task terminal state
```

- **Control-plane vs. execution-plane separation**: Each Attempt executes in an independent Python interpreter subprocess while the control process manages claiming, heartbeats, and supervision; missed heartbeats alone neither revoke task ownership nor release slots, and active solves continue through brief control-plane hiccups. Stage commits probe DB connectivity without locks before writing, backing off with rate-limited warnings on disconnects and committing checkpoints, results, or raw failure diagnostics under the original token once connectivity recovers.
- **Cancellation, exits, and resource exhaustion**: User cancellations, admin revocations, and deployment timeouts propagate via dedicated stop channels; abnormal task exits record exact exit codes and last active stages while releasing attempts and slots. Confirmed `MemoryError` or exits accompanied by rising container cgroup OOM counters settle cleanly as resource exhausted and release slots without mechanically auto-retrying under identical limits. `worker_resources` shown in task details reflects observed cgroup v2 stats for the shared container rather than an exclusive per-task quota.

## Stage Checkpoints and Recovery

- **Stage checkpoints (`ies.worker-checkpoint@1.0.0`)**: `prediction`, `generated`, and `execution` artifacts persist via `storage` per stage with attempt owner references (holding no lease row locks during disk I/O, then locking and verifying write eligibility immediately before writing metadata and refs). After interruption and retry, the new Attempt resumes under its new token from the furthest committed checkpoint: existing `execution` skips directly to adaptation, existing `generated` skips modeling and re-runs solving, and missing checkpoints execute from the original snapshot (solver search trees are never resumed mid-solve). Missing or unreadable checkpoint objects fail explicitly rather than silently recomputing or reverting to drafts.
- **Admin unlock and recovery**: When full host restarts leave active attempts occupying slots, an admin unlock runs in a short transaction to revoke old tokens, terminate stale attempts, release slots, and requeue under the original snapshot; each new claim creates a fresh Attempt and Fencing Token, rejecting stale late writes unconditionally.
- **Lightweight result projection**: Evidence publication atomically populates a rebuildable `evidence_projection` index used by result summaries, financial panels, and windowed step curves; operators can backfill legacy large evidence via `docker compose exec backend python -m iesplan.cli.index_result --evidence-package-id <id>`.

## Standards for Adding New Task Types

1. Define the task input snapshot, result contract, idempotency key scope, and resource pool (`compute` vs. `io`);
2. Register short-transaction state-advancement and stage commands under `application.worker`;
3. Wire cancellation and lease Fencing checkpoints into long-running steps;
4. Verify normal submission, duplicate messages, cancellation, timeouts, process exits, control-plane drops, and checkpoint retries inside Docker.
