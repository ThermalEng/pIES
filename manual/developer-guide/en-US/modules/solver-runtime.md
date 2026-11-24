# Solver Runtime

> Code Boundary: `backend/iesplan/computation/runtime/`; Handoff Format: [Solver Bundle](../formats/solver-bundle.md)

`SolverRuntime` validates and executes structured commands declared inside a Solver Bundle in a controlled isolated environment, captures raw outputs, process exit status, and resource statistics, and emits an immutable `ExecutionReceipt`.

## Responsibilities and Boundaries

- **Responsible for**: Bundle manifest, relative path, and command policy checks; `ExecutorProvider` lookup and trusted `executable` resolution; creating an isolated workdir, streaming chunked inputs, and launching controlled commands; handling cancellation, timeouts, and admin fencing with full process-group reaping; verifying declared outputs, bounding captured logs, and issuing `ExecutionReceipt`.
- **Not responsible for**: Parsing assembly YAML or device semantics, formulating math problems, choosing solvers, silent retries, interpreting mathematical optimality, or deciding final task states.

## Execution and Isolation Pipeline

```text
Accept Solver Bundle + AttemptContext
  ↓ Schema / manifest / path-traversal validation
  ↓ ExecutorProvider allowlist & deployment policy validation
Create isolated workdir & write Bundle inputs in chunks
  ↓ Spawn controlled process via argv array (shell prohibited)
Monitor timeout / user cancellation / lease fencing signals
  ↓ Wait and reap entire process group
Verify declared outputs & capture bounded stdout/stderr
  ↓ Issue ExecutionReceipt, commit execution checkpoint, then clean temp workdir
```

- **Pre-execution rejection**: Any manifest, path, or policy violation refuses to spawn external processes and returns a rejection receipt (`input_rejected` / `policy_rejected`) directly.
- **Chunked asset handoff**: Follows the [Bundle Chunked Content Protocol](../formats/solver-bundle.md#chunked-content-and-lifecycle), streaming inputs into the isolated directory in chunks and committing outputs via the persistence receiver before cleaning the temporary directory; receiver failures preserve undelivered files and fail explicitly.

## Command and Security Rules

- **No shell execution**: `executable` MUST match a trusted ID in the deployment allowlist; `arguments` pass element-by-element as an `argv` string array directly to OS process APIs, prohibiting `sh -c`, pipes, redirection, and command substitution.
- **Path and network isolation**: Working directory and declared input/output paths stay strictly inside the isolated workdir; `network: false` by default, with execution identities lacking access to business DBs, object storage credentials, and host code directories; each attempt uses a fresh workdir and transfers outputs only after the process group fully exits.

## Cancellation, Timeouts, Resources, and Output Boundaries

- **Termination and reaping**: On cancellation, timeout, or lease revocation (`fenced`), send a controlled termination signal first and force-kill the entire process group after grace to reap all child processes; cancellation, timeout, OOM, and process crashes record exact corresponding receipt statuses without ever promoting late exits to success.
- **Declared output handoff**: Collects relative paths listed in manifest `outputs`, reading them in chunks into object storage; exiting cleanly (`0`) while missing a `required: true` output marks `output_protocol_failed`—exit code `0` never directly implies mathematical feasibility.
- **Ephemeral container proxy integration (`1.1.5` target)**: Under the per-step ephemeral container pipeline, `SolverRuntime` invokes `job_runner`'s fixed business operations (`start_stage`, `inspect_stage`, `stop_stage`, `cleanup_stage`) with `/work/in` (read-only) and `/work/out` (read-write) mounts for chunked asset handoff; `cleanup_stage` MUST run **only after** exit status, resource metrics, and declared outputs are collected and persisted, and post-restart recovery reconciles leftover containers via existing `Attempt`, `Fencing Token`, and stage checkpoints without re-launching the same stage.
- **Local development and testing**: Contract tests isolate `ExecutorProvider` to verify security rejections, process termination, and output capture; real solver and ephemeral container integration tests execute inside Docker containers.
