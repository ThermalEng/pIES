# Worker (Asynchronous Scheduling & Ephemeral Container Orchestration)

> Code Boundary: `backend/iesplan/worker/`; Governing Spec: [Architecture Constitution](../ARCHITECTURE_CONSTITUTION.md)

`worker` (converged into a lightweight single-process asynchronous scheduling layer inside the `backend` control plane) drives queue discovery and state transitions for long-running background jobs (full-horizon planning solves, sensitivity/batch analyses, and exports/imports), relying on unique task ownership (`TaskAttempt` state) to ensure single effective execution and complete evidence traceability.

## Responsibilities and Boundaries

- **Owns**: Pending task claiming and unique `TaskAttempt` ownership management; dispatching per-step network-disabled ephemeral computation containers via the `job_runner` proxy and propagating in-process cancellation/timeout signals; stage orchestration (prediction → generation → solving → minimal-subprocess result restoration & post-solve financial evaluation / unified algorithm) and persisted stage checkpoints.
- **Does not own**: Modifying project drafts or re-reading unfrozen configuration; executing solvers or importing user plugin code inside control-plane processes; assembling solver shell commands or interpreting device/assembly semantics; mounting the host Docker socket directly or bypassing short-transaction `application.worker` commands for persistence.
- **Unified computation container boundary**: Built-in and third-party computation steps share the `ies.algorithm-plugin@1.0.0` (`/work/in → /work/out`) contract and run inside independent network-disabled ephemeral containers via `job_runner`; control-plane processes never unpack or import user source code.

## Execution State Machine and Container Isolation

```text
DB pending-task scan / in-process wake
  ↓ Claim task (assign unique TaskAttempt)
backend single-process async scheduling control plane (in-process slots & stop signals; no Docker socket)
  │
  ├─ [Stage 1] prediction: launch ephemeral predictor container via job_runner ──→ commit prediction checkpoint
  ├─ [Stage 2] generated: launch ephemeral generator container via job_runner ──→ commit generated checkpoint
  ├─ [Stage 3] execution: launch ephemeral solver container via job_runner ──→ commit execution checkpoint & clean tempdir
  └─ [Stage 4] evaluation: launch ephemeral evaluator container via job_runner (minimal-subprocess adaptation + parallel evaluation)
       │ (Unified single-step algorithm covers stages 2–3 in one container before evaluation; internal loops self-contain inside one container)
       └─ Publish Evidence, query projection & task terminal state
```

- **Control-plane vs. computation-plane separation**: Each computation step runs inside an independent network-disabled ephemeral Docker container launched by `job_runner`, while the control plane manages in-process bounded thread-pool slots, fixed five-operation lifecycle dispatch, and result persistence; inside `backend`, HTTP request threads stay separate from background scheduling, large-file I/O, and image-build workers so API threads never stall.
- **Persist-before-cleanup and startup sweep**: Once an ephemeral container exits, exit status, container limits/OOM state, and declared outputs/checkpoints are collected and persisted before invoking `cleanup_stage` (cleanup failures log warnings without polluting business outcomes). On `backend` startup, leftover ephemeral computation containers are swept once and interrupted active `TaskAttempt` records are marked failed (without cross-restart live-container adoption).
- **Cancellation, exits, and resource exhaustion**: User cancellations and deployment timeouts signal `job_runner` to immediately terminate the ephemeral container, collect exit status and resource observations, clean up the container, and release slots. Confirmed `MemoryError` or container `State.OOMKilled` exits settle cleanly as resource exhausted and release slots without auto-retrying under identical limits.

## Stage Checkpoints and Recovery

- **Stage checkpoints (`ies.worker-checkpoint@1.0.0`)**: `prediction`, `generated`, and `execution` artifacts persist via `storage` per stage. When interrupted and retried by the user, execution can resume from persisted valid checkpoints or recompute from the frozen snapshot (solver search trees are never resumed mid-solve). Missing or unreadable checkpoint objects fail explicitly rather than silently reverting to drafts.
- **Single-node cancel and retry**: Stuck tasks are resolved via normal cancellation and rerun/retry without maintaining separate lease tables, Fencing Tokens, or admin-unlock requeue state machines.
- **Lightweight result projection**: Evidence publication atomically populates a rebuildable `evidence_projection` index used by result summaries, financial panels, and windowed step curves; operators can backfill legacy large evidence via `docker compose exec backend python -m iesplan.cli.index_result --evidence-package-id <id>`.

## Standards for Adding New Task Types

1. Define the task input snapshot, result contract, optional idempotency key semantics, and resource pool (`compute` vs. `io`);
2. Register short-transaction state-advancement and stage commands under `application.worker`;
3. Wire in-process cancellation and timeout signals into long-running steps;
4. Verify normal submission, cancellation, timeouts, and checkpoint execution inside Docker.
