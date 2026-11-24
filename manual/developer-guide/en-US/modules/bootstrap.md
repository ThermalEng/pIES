# Composition Root and Bootstrap

> Code Boundary: `backend/iesplan/bootstrap/` and process entry points; Governing Spec: [Architecture Constitution](../ARCHITECTURE_CONSTITUTION.md)

`bootstrap` is the sole assembly layer in the system that knows which concrete implementations are active in a deployment. It reads deployment configuration, discovers and constructs each module's Providers and infrastructure connections, wires dependencies, and publishes aggregated readiness (`readyz`), ensuring business modules never self-discover implementations via environment variables or implicit global state.

## Responsibilities and Boundaries

- **Responsible for**: Reading and strictly validating process-level TOML deployment config; establishing database, Redis, and storage connections per process role (API vs. Worker); discovering and cross-validating candidate Providers for devices, equations, generators, executors, result adapters, and storage; atomically publishing each module's registry; and releasing resources in reverse order on shutdown.
- **Not responsible for**: Implementing domain rules, reading module-private registries or merging them into a global authoritative table, or mutating business data at startup.
- **Computation & plugin boundary**: Under `1.1.5`, the composition root inside the `backend` control plane statically wires APIs, lightweight scheduling orchestration, built-in computation image descriptors, and the `job_runner` proxy client (`backend` holds no Docker socket); installed user algorithm plugins reside as immutable records, freeze into task snapshots at submission, and run inside per-step ephemeral network-disabled containers without startup discovery or runtime hot-reloading.

## Startup and Shutdown Flow

```text
Read & validate TOML deployment configuration
  ↓
Establish DB / Redis / Storage connections & verify connectivity
  ↓
Discover candidate domain Providers ──→ Cross-check public IDs, versions & dependency refs
  ↓
Atomically publish independent registry state per module
  ↓
Wire application / API / Worker entry points ──→ Expose readyz
```

- **Separated database initialization**: Table creation, trigger installation, and built-in admin seeding run exclusively inside the API service initialization entry; Worker task subprocesses assemble runtime dependencies independently without re-executing DDL initialization, preventing lock contention with ordinary business transactions.
- **Atomicity and failure semantics**: If any required config is missing or mistyped, or if any required Provider fails construction, the entire candidate state is discarded and startup or readiness blocks without clearing existing state or silently falling back to static defaults; optional capabilities report degraded status only where explicitly permitted by contract.
- **Shutdown order**: Stop accepting new requests or tasks → wait for or cancel in-flight work → release Providers and underlying connection pools.

## Extension and Testing Requirements

When adding a new Provider kind, define its contract and descriptor in the owning domain module, add discovery, config wiring, and dependency checks in `bootstrap`, and tie readiness to its real operational capability. Startup and shutdown tests MUST cover missing config, missing dependencies, version conflicts, partial construction failure, and test-double Provider injection.
