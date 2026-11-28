# Solver Runtime

> Code Boundary: `backend/iesplan/computation/runtime/`; Handoff Format: [Solver Bundle](../formats/solver-bundle.md)

`SolverRuntime` executes stage computation commands inside controlled ephemeral containers, captures raw outputs, exit status, and container resource status, and emits an immutable `ExecutionReceipt`.

## Responsibilities and Boundaries

- **Responsible for**: Bundle manifest and declared relative output path boundary checks; dispatching network-disabled ephemeral containers via `job_runner`; handling cancellation and timeouts; verifying declared outputs, bounding captured logs, and issuing `ExecutionReceipt`.
- **Not responsible for**: Parsing assembly YAML or device semantics, formulating math problems, choosing solvers, silent retries, interpreting mathematical optimality, or deciding final task states.

## Execution and Isolation Pipeline

```text
Accept stage input / Solver Bundle + AttemptContext
  ↓ Schema / manifest / declared path boundary checks
Launch network-disabled ephemeral container via job_runner (read-only /work/in mount)
  ↓ Monitor timeout / user cancellation signals
  ↓ Collect exit status, container limit/OOM state, declared outputs & bounded stdout/stderr
  ↓ Issue ExecutionReceipt, commit execution checkpoint, then clean temp workdir
```

- **Pre-execution rejection**: Any manifest or path violation refuses to launch a container and returns a rejection receipt (`input_rejected` / `policy_rejected`) directly.
- **Chunked asset handoff**: Follows the [Bundle Chunked Content Protocol](../formats/solver-bundle.md#chunked-content-and-lifecycle), committing outputs via the persistence receiver before cleaning the temporary directory; receiver failures preserve undelivered files and fail explicitly.

## Command and Security Rules

- **No shell execution**: Entrypoints and arguments pass via controlled argument lists without untrusted shell string composition or `..` path traversal.
- **Path and network isolation**: Containers default to `--network=none`, mounting `/work/in` read-only and `/work/out` read-write without access to business DBs, Redis, object storage credentials, or host code directories.

## Cancellation, Timeouts, Resources, and Output Boundaries

- **Termination and reaping**: On cancellation or timeout, signal `job_runner` to immediately terminate and clean up the ephemeral container; cancellation, timeout, OOM, and process crashes record exact corresponding receipt statuses without ever promoting late exits to success.
- **Declared output handoff**: Collects relative paths listed in manifest `outputs` into object storage; exiting cleanly (`0`) while missing a `required: true` output marks `output_protocol_failed`—exit code `0` never directly implies mathematical feasibility.
- **Ephemeral container proxy integration**: Under the per-step ephemeral container pipeline, orchestration invokes `job_runner`'s fixed business operations (`start_stage`, `inspect_stage`, `stop_stage`, `cleanup_stage`) with `/work/in` (read-only) and `/work/out` (read-write) mounts; `cleanup_stage` runs **after** exit status, OOM/limit metrics, and declared outputs are collected and persisted (cleanup failures log warnings only), and leftover ephemeral containers are swept in one pass on control-plane startup.
- **Local development and testing**: Real solver and ephemeral container integration tests execute inside Docker containers.
