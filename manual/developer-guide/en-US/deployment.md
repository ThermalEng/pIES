# Deployment and Operations

> Status: Effective Blueprint; Spec Version: `1.2.0`; Governing Spec: [Architecture Constitution](ARCHITECTURE_CONSTITUTION.md)

## Runtime Environment Boundary

Builds, tests, formatting, database migration checks, and browser end-to-end acceptance for pIES run exclusively inside Docker containers; application dependencies MUST NOT be installed on the host, which retains only the Docker engine, source repository, and controlled data directories.

## Unified Deployment Configuration

Control-plane services and ephemeral container execution policies share one TOML deployment configuration file (default `backend/deploy/pies.toml`, mounted at `/app/deploy/pies.toml` inside images), parsed by the composition root at startup into an immutable typed config:

- **Override precedence**: Explicit code argument > mounted file at `IESPLAN_DEPLOY_CONFIG_FILE` > built-in package default; runtime parameters do not accept scattered per-field environment overrides to prevent split configuration maintenance.
- **Single source of defaults**: Task concurrency slots, timeout ceilings, solve and container resource policies, and new-form default values (fetched by the frontend via `GET /api/deployment/defaults`) live solely in this file.
- **Secrets and orchestration**: Database credentials, secrets such as `IESPLAN_SECRET_KEY`, and container port/network wiring continue via environment variables or orchestrator injection and MUST NOT be committed in default repo configs.

## Docker Compose Deployment Architecture (`1.2.0`)

Run from the repository root (pass `--build` whenever source code or `Dockerfile` changes to rebuild affected services):

```bash
docker compose up -d --build
```

Default web entry point is `http://localhost:8080`. Persistent services converge to five core containers:

- `web`: Static frontend and Help Center (`nginx`);
- `backend`: Unified control-plane service—handles synchronous HTTP APIs, authorization and transactions, fast edit-time validation, I/O orchestration, and asynchronous computation scheduling (separating HTTP request threads from background scheduling, large-file I/O, and image builds so requests never block);
- `job_runner`: Container orchestration proxy—exposes solely five fixed business operations (`build_plugin`, `start_stage`, `inspect_stage`, `stop_stage`, `cleanup_stage`) to build plugin images and launch/reap ephemeral network-disabled computation containers under the unified contract;
- `postgres`: Authoritative relational database (task queue, snapshots, progress, and result indices all treat PostgreSQL as authoritative; `redis` is not required for task scheduling).

**Task concurrency configuration**: Actual computation concurrency is bounded by `[worker]` slot ceilings in the deployment config and host container capacity; each computation step executes inside an independent ephemeral network-disabled container cleaned up alongside temporary files after status and valid outputs are persisted, replacing standalone `compute_worker` and `io_worker` daemons; configured `evaluators[]` execute in parallel with fault isolation once solving yields a feasible solution.

**Empty database cold-start note**: On an uninitialized database, a single `backend` control-plane instance creates tables, runs migrations, and seeds built-in identities; if initialization fails, inspect logs and database state first.

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
   - **Backend changes**: Run affected modules plus Application/API/scheduling and container execution tests in Docker, using real PostgreSQL when persistence or migrations change;
   - **Frontend or interactive changes**: Run frontend logic tests plus isolated Docker Playwright real-browser regressions (using a separate Compose project name and data volume so dev data stays isolated).
2. Clean up temporary test containers and test-built images (preserving base images) after testing to reclaim disk space.

## Ephemeral Container Isolation and Resource Observation

- **Boundaries drawn at file handoffs and immutable compute nodes**: Ephemeral containers wrap only stages requiring execution isolation, resource caps, or third-party replaceability (`predictor`, `generator`, `solver`, `evaluator`, and unified `algorithm`); device equation parsing, unit conversion, assembly checks, and shared pure-Python libraries remain reusable libraries without blind container-per-module splitting.
- **Per-step network-disabled container isolation**: Computation stages are launched by `backend` via `job_runner`'s fixed business interface as isolated ephemeral containers: defaulting to `--network=none` with explicit CPU/memory caps and zero access to database/object-storage credentials or host repository directories; each step receives only a read-only `/work/in` mount and read-write `/work/out` mount, following one uniform lifecycle order:

```text
Start container → Isolated execution → Natural exit / Cancel / Timeout → Collect exit status & declared outputs → Persist valid artifacts → Clean up container & temp files
```
- **Separated build and runtime**: When installing plugins with extra dependencies, `job_runner`'s `build_plugin` operation builds a plugin computation image on top of the official base image during installation; runtime execution strictly prohibits network access or dynamic package downloads; computation traceability relies on immutable frozen input files, snapshots, and persisted outputs rather than enforcing immutable ephemeral container environments, and temporary cleanup failures log warnings without polluting business outcomes.
- **Fixed business-level container interface**: `backend` and all ephemeral computation containers MUST NOT mount `/var/run/docker.sock` directly; container orchestration converges exclusively on `job_runner`'s five fixed business operations without forwarding raw Docker options or arbitrary host paths.
- **Resource limits and OOM observation**: The platform imposes no hardcoded uniform memory cap; operators configure container limits and deployment policies to match host capacity. CPU/memory limits and `State.OOMKilled` status are read from container metadata for display in task details; unreadable metrics report as unknown without blocking execution. When confirmed OOM or `MemoryError` occurs, the task fails explicitly as resource exhausted and releases its slot without auto-retrying under identical limits or altering model/precision.

## Production Security Baseline

Before going live in production, verify:

1. Configure a strong, unique `IESPLAN_SECRET_KEY`;
2. Inject a randomized initial admin password and rotate it on first sign-in;
3. Replace all default database credentials so development passwords never enter production;
4. Enable HTTPS at a trusted reverse proxy and restrict external access to Postgres, Redis, object storage, `job_runner`, and internal admin ports;
5. Establish data directory, volume sizing, backup cadence, and restore drill ownership.

## `1.1.5 / 1.1.6 → 1.2.0` Upgrade & Migration Playbook

Follow these steps in order when upgrading from `1.1.5` or `1.1.6` to `1.2.0`—never mutate frozen historical records in place:

1. **Drain / pause work & perform full backup**:
   - Before upgrading, wait for or cancel active tasks and back up the PostgreSQL database, managed object storage directory, `backend/deploy/pies.toml`, and environment secrets together.
2. **Run idempotent versioned database migrations**:
   - With the updated `backend` image, run `python -m iesplan.migrations` (or let the single startup instance execute `apply_migrations()`), applying both migrations idempotently in `schema_migrations`:
     - `v1_2_0_calc_config` (version ID `20261126_120_calc_config`): converts editable saved project `CalculationConfig` revisions in one pass to the 13-field `1.2.0` contract (removing `convergence`, mapping `evaluator` to parallel `evaluators[]`, and slotting `options` under `options.generator` / `options.algorithm`); historical `CalcSnapshot` and `Evidence` records remain strictly read-only.
     - `v1_2_0_ecosystem` (version ID `20261127_120_ecosystem`): creates `ecosystem_settings`, `local_artifacts`, and `member_artifact_installs` tables with single-active-version constraints and seeds default Local Mode settings (`market_mode="local"`, `local_upload_requires_approval=True`).
3. **Build the plugin base image and start the Docker Compose stack**:
   - First build the plugin base image from the repository root with `docker build -f sdk/Dockerfile.base -t pies-plugin-base:latest sdk`; then run `docker compose up -d --build` to update the core services and execute the one-shot `builtin_stages` service building `pies-builtin-stages:1.2.0`.
4. **Optional standalone `market/` & `market_gateway/` services**:
   - Deploy `market/` (static `index.json` + artifact tree hosted as static assets / GitHub Pages) and `market_gateway/` (stateless OTP + GitHub PR gateway configured with `MARKET_HMAC_SECRET`, SMTP, and GitHub App credentials) independently if online browsing or contributor submissions are desired; then configure `market_mode = "online"`, `market_index_url`, `submission_gateway_url`, and optional single-source `pip_index_url` under `/admin/ecosystem`.
5. **Single-active-version upgrade policy**:
   - `1.2.0` retains **only 1 current active version** per `(kind, stable_id)` without platform-side cross-version compatibility shims or multi-version pin switchers. Plugin upgrades and uninstallation replace or disable the active entry directly and clean up old plugin images on a best-effort basis; backward input handling is solely the responsibility of the newer model/plugin itself.

## Health Checks, Backup, and Recovery

- **Separating health and readiness**: `healthz` reports process liveness only; `readyz` aggregates checks across database, storage, and `job_runner` proxy reachability. Troubleshoot hierarchically along `container status → healthz → failing readyz check → module logs (correlated by request/task/attempt ID)`.
- **Backup scope**: A valid backup MUST capture **PostgreSQL database**, **managed object storage directory**, and **deployment configuration and secrets** together (Redis queues and query projections are rebuildable). Backing up only the database or only files cannot restore cleanly.
- **Upgrade and recovery drills**: Always perform a full backup before upgrading, then verify versioned migrations and core read/solve workflows in an isolated Docker stack. Recovery drills MUST verify representative projects, task snapshots, and sealed results through normal public read APIs rather than patching raw disk paths.
