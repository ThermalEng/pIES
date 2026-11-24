# Worker (Asynchronous Scheduling & Ephemeral Container Orchestration)

> Code Boundary: `backend/iesplan/worker/`; Governing Spec: [Architecture Constitution](../ARCHITECTURE_CONSTITUTION.md)

`worker` (converged under the `1.1.5` target architecture into a lightweight asynchronous scheduling layer inside the `backend` control plane) drives queue discovery and state transitions for long-running background jobs (full-horizon planning solves, sensitivity/batch analyses, report checks, and large exports/imports), ensuring single effective commitment and complete evidence traceability across concurrent claiming, process/container failures, timeouts, cancellations, and stale writes.

## Responsibilities and Boundaries

- **Owns**: Task queue discovery and atomic claiming; Attempt lease holding, Fencing Token validation, and admin unlock recovery; dispatching per-step network-disabled ephemeral computation containers via the `job_runner` proxy and propagating stop signals; stage orchestration (prediction → generation → solving → result adaptation & post-solve financial evaluation / unified algorithm) and stage checkpoint recovery.
- **Does not own**: Modifying project drafts or re-reading unfrozen configuration; executing solvers or importing user plugin code inside control-plane processes; assembling solver shell commands or interpreting device/assembly semantics; mounting the host Docker socket directly or bypassing short-transaction `application.worker` commands for persistence.
- **Unified computation container boundary (`1.1.5` target)**: Built-in and third-party computation steps share the `ies.algorithm-plugin@1.0.0` (`/work/in → /work/out`) contract and run inside independent network-disabled ephemeral containers via `job_runner`; control-plane processes never unpack or import user source code.

## Execution State Machine and Container Isolation

```text
Queue hint / DB pending-task scan
  ↓ Atomic claim (assign Attempt + Lease + Fencing Token)
backend async scheduling control plane (heartbeats, stop signals & stage orchestration; no Docker socket)
  │
  ├─ [Stage 1] prediction: launch ephemeral predictor container via job_runner ──→ commit prediction checkpoint
  ├─ [Stage 2] generated: launch ephemeral generator container via job_runner ──→ commit generated checkpoint
  ├─ [Stage 3] execution: launch ephemeral solver container via job_runner ──→ commit execution checkpoint & clean tempdir
  └─ [Stage 4] evaluation: launch ephemeral evaluator container via job_runner (adaptation + post-solve financial evaluation)
       │ (Unified single-step algorithm covers stages 2–3 in one container before evaluation; internal loops self-contain inside one container)
       ↓ Verify lease still valid (Fencing)
       └─ Atomically publish Evidence, query projection & task terminal state
```

- **Control-plane vs. computation-plane separation**: Each computation step runs inside an independent network-disabled ephemeral Docker container launched by `job_runner`, while the control plane manages claiming, heartbeats, fixed five-operation lifecycle dispatch, and reconciliation; inside `backend`, HTTP request threads are strictly isolated from background scheduling, large-file I/O, and image-build workers with separate concurrency limits so API threads never stall.
- **Persist-before-cleanup and restart reconciliation**: Once an ephemeral container exits, exit status, resource usage, and declared outputs/checkpoints MUST be collected and persisted before invoking `cleanup_stage`. After a `backend` or `job_runner` restart, existing `TaskAttempt`, `Fencing Token`, stage checkpoints, and container labels identify and reconcile leftover containers **without introducing a second task state machine or re-launching the same stage**; missed heartbeats alone neither revoke ownership nor release slots, and running containers commit under their original token once connectivity recovers.
- **Cancellation, exits, and resource exhaustion**: User cancellations, admin revocations, and deployment timeouts propagate via dedicated stop channels so `job_runner` immediately terminates and removes ephemeral containers; abnormal exits record exit codes, last active stages, and observed `/work/out/iterations.jsonl` (if any) while releasing attempts and slots. Confirmed `MemoryError` or exits accompanied by rising container cgroup OOM counters settle cleanly as resource exhausted and release slots without mechanically auto-retrying under identical limits. `worker_resources` shown in task details reflects observed cgroup v2 stats for the execution container rather than an unbounded per-task quota.

## Stage Checkpoints and Recovery

- **Stage checkpoints (`ies.worker-checkpoint@1.0.0`)**: `prediction`, `generated`, and `execution` artifacts persist via `storage` per stage with attempt owner references (holding no lease row locks during disk I/O, then locking and verifying write eligibility immediately before writing metadata and refs). After interruption and retry, the new Attempt resumes under its new token from the furthest committed checkpoint: existing `execution` skips directly to adaptation, existing `generated` skips modeling and re-runs solving, and missing checkpoints execute from the original snapshot (solver search trees are never resumed mid-solve). Missing or unreadable checkpoint objects fail explicitly rather than silently recomputing or reverting to drafts.
- **Admin unlock and recovery**: When full host restarts leave active attempts occupying slots, an admin unlock runs in a short transaction to revoke old tokens, terminate stale attempts, release slots, and requeue under the original snapshot; each new claim creates a fresh Attempt and Fencing Token, rejecting stale late writes unconditionally.
- **Lightweight result projection**: Evidence publication atomically populates a rebuildable `evidence_projection` index used by result summaries, financial panels, and windowed step curves; operators can backfill legacy large evidence via `docker compose exec backend python -m iesplan.cli.index_result --evidence-package-id <id>`.

## Standards for Adding New Task Types

1. Define the task input snapshot, result contract, idempotency key scope, and resource pool (`compute` vs. `io`);
2. Register short-transaction state-advancement and stage commands under `application.worker`;
3. Wire cancellation and lease Fencing checkpoints into long-running steps;
4. Verify normal submission, duplicate messages, cancellation, timeouts, process exits, control-plane drops, and checkpoint retries inside Docker.
