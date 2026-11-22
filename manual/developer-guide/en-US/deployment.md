# Deployment and Operations

> Status: Effective Blueprint; Spec Version: 1.0.0; Governing Spec: [Architecture Constitution](ARCHITECTURE_CONSTITUTION.md)

## Runtime Environment Boundary

Builds, tests, formatting, database migration checks, and browser end-to-end acceptance for pIES run exclusively inside Docker containers; application dependencies MUST NOT be installed on the host, which retains only the Docker engine, source repository, and controlled data directories.

## Unified Deployment Configuration

The API service and all Workers share one TOML deployment configuration file (default `backend/deploy/pies.toml`, mounted at `/app/deploy/pies.toml` inside images), parsed by the composition root at startup into an immutable typed config:

- **Override precedence**: Explicit code argument > mounted file at `IESPLAN_DEPLOY_CONFIG_FILE` > built-in package default; runtime parameters do not accept scattered per-field environment overrides to prevent split configuration maintenance.
- **Single source of defaults**: Worker concurrency slots (`compute_slots`, `io_slots`), timeout ceilings, solve policies, and new-form default values (fetched by the frontend via `GET /api/deployment/defaults`) live solely in this file.
- **Secrets and orchestration**: Database credentials, secrets such as `IESPLAN_SECRET_KEY`, and container port/network wiring continue via environment variables or orchestrator injection and MUST NOT be committed in default repo configs.

## Docker Compose Development Deployment

Run from the repository root (pass `--build` whenever source code or `Dockerfile` changes to rebuild affected services):

```bash
docker compose up -d --build
```

Default web entry point is `http://localhost:8080`. Component responsibilities:

- `web`: Static frontend and Help Center (`nginx`);
- `backend`: Synchronous HTTP API service;
- `compute_worker` / `io_worker`: Background computation and I/O task daemons;
- `postgres` / `redis`: Authoritative relational database and rebuildable task queue / progress store.

**Worker concurrency configuration**: Each Worker process executes one task at a time. Scaling concurrency requires scaling `compute_worker` (or `io_worker`) replicas alongside `[worker]` `compute_slots` (or `io_slots`) in the deployment config; actual concurrency is bounded by the lesser of the two.

**Empty database cold-start note**: On an uninitialized database, a single API process MUST create tables and seed built-in identities before additional API replicas or Workers start; if initialization fails, inspect logs and database state first.

### Moving or Renaming the Repository Directory

`docker-compose.yml` pins the Compose project name to `pies`. Before moving or renaming the repository directory, stop existing containers first and rebuild from the new location:

```bash
docker compose down
# Move or rename directory, then run inside the new path:
docker compose up -d --build
```

Persistent data under `./data` moves intact with the repository. If older legacy Compose project names exist, inspect with `docker compose ls -a` and remove stale references via `docker compose -p <old-project> down --remove-orphans`.

## Daily Developer Verification Workflow

1. Build and launch test containers scoped to the change, selecting verification per [Frontend Acceptance Split](frontend.md#browser-exploration-and-automated-regression):
   - **Documentation-only changes**: Build the frontend Help Center and verify TOC links, Markdown parsing, and example tests;
   - **Backend changes**: Run affected modules plus Application/API/Worker tests in Docker, using real PostgreSQL when persistence or migrations change;
   - **Frontend or interactive changes**: Run frontend logic tests plus isolated Docker Playwright real-browser regressions (using a separate Compose project name and data volume so dev data stays isolated).
2. Clean up temporary test containers and test-built images (preserving base images) after testing to reclaim disk space.

## Solver Execution Isolation and Resource Observation

- **Executor isolation boundary**: `SolverRuntime` MUST spawn processes via a trusted `ExecutorProvider` wired by the composition root. Default policies disable networking (`network: false`) and strip access to business databases, object storage credentials, and repository workspaces; each Attempt uses an isolated working directory for inputs and streams Manifest-declared outputs in chunks.
- **Algorithm plugin isolation (`1.1.0` target)**: Enabling user algorithm plugins requires deploying a dedicated `plugin_runner` service running payloads as non-root with a read-only root filesystem, disabled network, and offline dependency environment; ordinary Workers MUST NOT mount the Docker socket.
- **Resource limits and cgroup v2 observation**: The platform imposes no hardcoded uniform memory cap; operators configure container limits and deployment policies to match host capacity. Under Linux/Docker, task processes read memory/swap limits, current usage, container-lifetime peak, and OOM events from the container's cgroup v2 hierarchy for display in task details; unreadable metrics report as unknown without blocking execution. When confirmed OOM or `MemoryError` occurs, the task fails explicitly as resource exhausted and releases its slot without auto-retrying under the same limits or altering model/precision.

## Production Security Baseline

Before going live in production, verify:

1. Configure a strong, unique `IESPLAN_SECRET_KEY`;
2. Inject a randomized initial admin password and rotate it on first sign-in;
3. Replace all default database credentials so development passwords never enter production;
4. Enable HTTPS at a trusted reverse proxy and restrict external access to Postgres, Redis, object storage, and internal admin ports;
5. Establish data directory, volume sizing, backup cadence, and restore drill ownership.

## Health Checks, Backup, and Recovery

- **Separating health and readiness**: `healthz` reports process liveness only; `readyz` aggregates checks across database, storage, and required runtime dependencies (the compute Worker master verifies the exact built-in solver version and minimal self-test on startup, after which tasks reuse the same executor). Troubleshoot hierarchically along `container status → healthz → failing readyz check → module logs (correlated by request/task/attempt ID)`.
- **Backup scope**: A valid backup MUST capture **PostgreSQL database**, **managed object storage directory**, and **deployment configuration and secrets** together (Redis queues and query projections are rebuildable). Backing up only the database or only files cannot restore cleanly.
- **Upgrade and recovery drills**: Always perform a full backup before upgrading, then verify versioned migrations and core read/solve workflows in an isolated Docker stack. Recovery drills MUST verify representative projects, task snapshots, and sealed results through normal public read APIs rather than patching raw disk paths.
