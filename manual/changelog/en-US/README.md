# Changelog

This page records only implemented and accepted changes in reverse chronological order. Each entry answers four questions: **when it was completed, what changed, what problem it solved, and where the release stands**. Unimplemented work goes solely into the [Roadmap](roadmap.md).

## Current Version Status

| Scope | Current Status |
|---|---|
| Official product version (`backend/pyproject.toml`) | `1.1.6` |
| Latest released version | `1.1.6` (2026-11-27 `1.2.0` computation core refactoring `F1–F4`, unidirectional pipeline, parallel evaluators & `G0` exit gate closure) |
| General modeling & computation upgrade | Declarative device/system equations, monthly TOU prices, monthly peak demand, embedded tax modeling, structural acceleration, bounded subprocess restoration, and unidirectional parallel evaluation pipeline delivered |
| Next development target | `1.2.0` (`P1: E1–E5` Open Models & Algorithms Ecosystem and `pIES Market`) |
| Limited pilot release version | `1.1.6` |
| First general public release version | `2.0.0` |

> Authoritative product metadata (`backend/pyproject.toml`, `job_runner`, and Help Center manifest; alongside `sdk/pyproject.toml` and `pies-builtin-stages:1.2.0` upgraded to the `1.2.0` computation contracts) has been bumped to **`1.1.6`** on 2026-11-27. Upgrading from `1.1.5` runs a one-pass migration converting editable calculation configs to the 13-field contract while keeping historical `CalcSnapshot` and `EvidencePackage` records strictly read-only. Development next proceeds to `E1–E5` for the open ecosystem and `pIES-Market`.

## `1.1.6` — 2026-11-27 — `1.2.0` Computation Core Refactoring (`F1–F4`) and `G0` Closure

### 2026-11-27 — Unidirectional Computation Pipeline, Bounded Subprocess Restoration & Parallel Retryable Evaluators (`F1–F4` / `G0`) Delivery

- **When completed**: 2026-11-27, completing all four `P0` core slices (`F1–F4`) plus `G0` closure fixes (PRs `#81`–`#86`) merged to `master` after full Docker CI and independent Herdr Codex reviews, bumping the product version from `1.1.5` to **`1.1.6`**.
- **What changed**:
  - **Core Contracts & Standalone SDK (`F1`)**: Finalized the 13-field `CalculationConfig` (removing `convergence`, adding slotted `options` and parallel `evaluators[]`), contract-decoupled `SolverBundle`, manifest `consumes`/`produces` contracts, and canonical schemas for `ies.primal-solution@1.0.0`, `ies.engineering-solution@1.0.0`, and self-describing `ies.evaluation-report@1.0.0`; updated `pies-sdk` (`1.2.0`) with streaming and canonical stage I/O helpers.
  - **HiGHS Quality Cohesion & Bounded `spawn` Restoration (`F2`)**: Consolidated full original-model tolerance and continuous monthly demand envelope tightness checks inside `ies-highs-solver`; retired `ResultAdapterRegistry` and legacy `evaluator_highs`, and built a universal `backend` `ResultAdapter` using zero-copy read-only input reuse (direct local path / `os.link` / read-only symlink) and a bounded `spawn` child subprocess to restore engineering solutions and hourly projections before releasing memory on exit.
  - **Forward Financial Evaluator & Independent Baseline Reconciliation (`F3` / `G0`)**: Added `backend/iesplan/finance/forward.py` and standalone plugin `ies.evaluator.finance@1.0.0` (packaged in `pies-builtin-stages:1.2.0`), reconciled against 6 frozen golden JSON baselines extracted from commit `b490989e` (`1.1.5` reconciliation benchmark).
  - **Unidirectional Pipeline, Parallel Evaluators, Non-Destructive Retry & Migration (`F4` / `G0`)**: Switched production execution to the single-pass unidirectional pipeline with `os.link` hardlink handoffs; added plugin-grouped joint multi-target `predictor` execution and unified `Algorithm` emission of isomorphic `solution.json`; delivered `0..N` parallel isolated evaluators (`[stage].evaluator_max_concurrency = 1` default resource budget, configurable per host) with independent `analysis` child-task single-evaluator retry preserving `completed` source tasks and newer core tasks' `ResultIndex.is_latest`; added one-pass migration ([v1_2_0_calc_config.py](../../../backend/iesplan/migrations/v1_2_0_calc_config.py)) with active-task drain gating and immutable history protection.
- **What problem it solved**:
  - Eliminated the outer cross-container iteration loop and legacy 14-field / HiGHS-specific evaluator coupling, enabling drop-in replacement across `Solver` (`.mps`), unified `Algorithm`, and multi-`Evaluator` plugins, isolating per-evaluator failures with selective retry without re-solving core engineering models, and isolating control-plane memory on long-horizon solution restoration.
  - **Upgrading from `1.1.5`**: Run `docker compose up -d --build` to update `builtin_stages` (`pies-builtin-stages:1.2.0`), `backend`, `job_runner`, and `web`; before migrating configs, [v1_2_0_calc_config.py](../../../backend/iesplan/migrations/v1_2_0_calc_config.py) enforces draining all `queued`/`running`/`cancelling` tasks and then preserves original evaluator intent and read-only historical snapshots/evidence intact.
- **Where the release stands**: `1.1.6` completes the `P0` computation core foundation and satisfies all `12/12` mandatory `G0` exit gate checks; next per [Roadmap](roadmap.md) is `P1` (`E1–E5` open ecosystem and `pIES-Market`) leading to **`1.2.0`**.

## `1.1.5` — 2026-11-25 — Ephemeral Container Computation Pipeline, Unified Plugin Contract, and Backend Orchestration Convergence

### 2026-11-25 — `1.1.5` Ephemeral Container Architecture & Scheduler Convergence (S1–S4) Delivery

- **When completed**: 2026-11-25, completing slices S1–S4 and bumping the product version from `1.1.0` to **`1.1.5`**.
- **What changed**:
  - **`job_runner` proxy & isolated ephemeral containers**: Introduced the thin container proxy `job_runner` (`job_runner/pies_job_runner`) exposing strictly 5 fixed business operations (`build_plugin`, `start_stage`, `inspect_stage`, `stop_stage`, `cleanup_stage`), launching one-shot computation containers with `--network=none --read-only --cap-drop=ALL` and non-root execution; includes host NCPU clamping for `cpu_cores` and `/readyz` checks against required images (`PIES_JOB_RUNNER_REQUIRED_IMAGES=pies-builtin-stages:1.1.5`).
  - **Standalone zero-backend `pies-sdk` & unified plugin contract**: Delivered the standalone `pies-sdk` package (`sdk/pyproject.toml`, version `1.1.5`) establishing the unified `ies.algorithm-plugin@1.0.0` (`plugin.yaml` + `/work/in` → `/work/out`) contract across `predictor`, `generator`, `solver`, `evaluator`, and single-step `algorithm` stages.
  - **Built-in stages image & zero-copy handoffs**: Packaged `pies-builtin-stages:1.1.5` (built via the one-shot `builtin_stages` Compose service from `plugins/Dockerfile`), enabling generator containers to materialize `frozen_series_inputs` directly from `/work/in/objects` and pass stage artifacts via zero-copy `os.link` hardlinks.
  - **14-field `CalculationConfig`**: Standardized the 14 required top-level fields of `ies.calculation-config@1.0.0` to support both multi-stage (`generator` + `solver` + `evaluator`) and single-step (`algorithm`) execution modes cleanly.
  - **`backend` embedded scheduler & service consolidation**: Converged `EmbeddedScheduler` into `backend` with isolated bounded `compute_slots`, `io_slots`, and `build_slots` pools, DB-authoritative startup reconciliation/recovery (`Attempt`, `Fencing Token`, and stage checkpoints preventing duplicate launches), and non-blocking detach on control-plane shutdown; retired legacy persistent `io_worker` and `compute_worker` daemon containers (removing `backend/iesplan/worker/main.py`) and converged persistent Compose services to **5 containers** (`postgres`, `redis`, `job_runner`, `backend`, `web`) plus the one-shot `builtin_stages` build.
- **What problem it solved**:
  - Physically isolated computation and plugin workloads from the `backend` control plane (no Docker socket inside `backend` or compute stages, no DB/object-storage credentials, and no network egress), preventing solver memory spikes from destabilizing API serving while eliminating legacy daemon overhead and redundant stage serialization copies.
  - **Upgrading from `1.1.0`**: Because `io_worker` and `compute_worker` have been removed from `docker-compose.yml`, run `docker compose up -d --build --remove-orphans` (or `docker compose down --remove-orphans` first) when upgrading so deprecated worker containers are cleanly removed.
- **Where the release stands**: `1.1.5` (S1–S4) is complete and merged to `master`; the next planned milestone in [Roadmap](roadmap.md) is **`1.2.0`** (Open Model & Algorithm Plugin Ecosystem and `pIES Market`).

## `1.1.0` — 2026-11-24 — Solver-Embedded Structural Acceleration and Large-Scale Long-Horizon Optimization

### 2026-11-24 — `1.1.0` Solver-Embedded Structural Acceleration (S1–S3) Closure

- **When completed**: 2026-11-24, completing S1 acceleration contracts & guardrails, S2 on-demand mutex recovery with large-LP IPM/Simplex refinement, S3 equal-budget anti-OOM benchmarking, plus fixes for HiGHS infeasible `kNotset` status preservation and TasksPage mount-hydration race conditions, bumping the product version to **`1.1.0`**.
- **What changed**:
  - Enabled `acceleration.mode="on_demand_mutex"` by default in `ies.generator.highs_milp@1.0.0` (while preserving `off` opt-out and legacy frozen configs), exporting structural mutex metadata in `input/acceleration.json` without introducing a standalone presolve plugin layer.
  - Implemented in-handle on-demand mutex relaxation and multi-round recovery in `ies-highs-solver` (`ies.solver.highs@1.7.2`): solves continuous relaxations, switches large LPs (`>= 10,000` columns) to IPM, verifies full original-model constraints with scale-aware tolerances, applies `clearSolver()` + Simplex refinement or on-demand conflict re-injection when needed, and safely falls back to the original MILP on unbounded relaxations or verification failures without ever publishing unverified primals.
  - Enforced strict monotonic `deadline`/`io_margin` guardrails and incumbent-first atomic writes; read `handle.getModelStatus()` and solution data before resetting `solver="choose"` in `finally:` blocks so HiGHS 1.7.2 infeasible presolve models preserve `kInfeasible` instead of reverting to `kNotset`.
  - Fixed a frontend mount-hydration race in `TasksPage` where reopening the submit dialog before initial `refreshDetailed()` settled could let stale snapshot values overwrite in-progress edits.
- **What problem it solved**:
  - Curbed root-node cut memory blowups and solve-time overruns on large-scale long-horizon MILP workloads under `8 GiB` zero-swap isolation: `campus_5y_bat_zero` improved from `OOMKill` (`8,192 MiB` ceiling) to **`11.84s`** proven optimal (`Gap=0.0%`, `-85.4%` peak RSS); `campus_2y_budget1` (`60s` budget) improved from `121.12s` timeout (`not_solved`) to **`49.69s`** proven optimal; `campus_10y_bat_on` (`120s` budget) eliminated `+505s` overruns and bounded wall time to `120.40s`.
  - Preserved exact physical conservation, monthly TOU pricing, natural-month peak demand, and after-tax cash-flow semantics.
- **Where the release stands**: `1.1.0` structural solver acceleration is complete on `master`; the next milestone in [Roadmap](roadmap.md) is **`1.2.0`** (Models & Algorithms, Provider SDK, and Device Extensions).

## `1.0.0` — 2026-11-22 — Official 1.0.0 Release and Full-Workflow Engineering Closure

### 2026-11-22 — Official `1.0.0` Release Packaging

- Bumped the single authoritative product version in `backend/pyproject.toml` (propagating automatically to API `__version__`, package exporter metadata, and the Help Center build manifest) to **`1.0.0`**, packaging the validated full-workflow system for pilot release.
- Given observed solve-time bottlenecks of traditional branch-and-bound MILP solvers on large-scale long-horizon cases, inserted **`1.1.0` — Non-Traditional Solvers and Large-Scale Long-Horizon Acceleration** immediately after `1.0.0` in the [Roadmap](roadmap.md), shifting subsequent `1.x` milestones cleanly to `1.2.0`–`1.7.0`.
- Below is the reverse-chronological archive of all implemented and verified capabilities delivered through `1.0.0`:

### 2026-11-22 — Results Page Tabbed Views, Level-1 Charts, and Paginated Detail Tables

- Restructured the Results page into five tabs (**Overview**, **Financials & Cash Flows**, **Process & Tax**, **Engineering & Curves**, **Assessments & Export**) synchronized with URL `?tab` and `?detail` query parameters.
- Replaced dense inline process dumps on Level-1 pages with native SVG charts (cash flows `CF[0..E]`, annual operating breakdowns, monthly demand peaks/charges, monthly VAT, income tax/depreciation, named cost items/amounts, and asset book values), supporting interactive legend toggles and hover/focus inspection of exact sealed decimal strings.
- Added Level-2 paginated and filterable detail views (and raw step-series data tables) with breadcrumb navigation and assessment history pagination while keeping all sealed numbers un-recalculated.

### 2026-11-21 — Session History Retention and Worker Disconnect Recovery Fix

- Session expiration cleanup updates active sessions only, preserving historical revoked and expired session records.
- Workers probe database connectivity prior to publishing checkpoints. During disconnections they back off and emit rate-limited warnings, persisting artifacts and advancing task completion once connectivity returns.

### 2026-11-21 — Canvas and Sign-In Driven by Actual Network Requests

- Browser offline flags no longer block canvas operations. Refresh, autosave, and sign-in respond directly to actual HTTP request outcomes.

### 2026-11-21 — Simplification of Sessions, Admin Operations, and Export Contracts

- The same account MAY hold multiple concurrent sessions; new passwords require at least 6 characters. Password changes and account deactivation revoke old sessions uniformly.
- Project deletion uses standard confirmation. Administrators preview affected projects prior to deleting an account, and unlocking tasks directly revokes stale attempts and requeues tasks.
- Excel and project package downloads authorize by project permission and export object ID. Cookies continue using `HttpOnly` and `SameSite=Lax`, with cross-origin requests governed by CORS configuration.

### 2026-11-21 — Simplification of Storage Status, Uploads, and Orphan Cleanup

- Tasks attempt persistence directly and report actual storage errors on failure. Health and storage status endpoints inspect capacity, references, and object metadata.
- Administrators MAY preview or directly clean up unreferenced objects. Removed the trash bin, upload quotas, and upload metadata key allowlist while keeping file size, CSV domain rules, and ZIP path validation.
- Removed unused legacy evidence submission endpoints and redundant database grant statements. Worker evidence submission continues using attempt tokens, and the database continues enforcing immutable records via triggers.

### 2026-11-21 — Simplification of Worker Attempts and Built-In Solver Startup

- Active tasks retain execution eligibility across brief sleep, network drops, or database disconnects. Abnormal task exits are reaped by supervisor processes; tasks orphaned by full host exits are unlocked by administrators.
- Built-in HiGHS verifies version and runs self-checks on compute Worker startup. Task execution streams declared outputs in chunks, and expressions validate against an AST allowlist supporting normal identifiers such as `grid_import`.

### 2026-11-20 — Frozen Baseline Submission Reuses Evidence Query Projection

- When referencing an existing task result, sealed financial evaluations load from the specified evidence's query projection (falling back to streaming projection when unindexed). Fixes submission failures from loading huge evidence files into memory while preserving original permissions, evidence identities, and difference evaluation semantics. Existing evidence and snapshots require no recomputation or rewriting.

### 2026-11-19 — Releasing Pre-Solve Modeling Temporaries

- Cooperative stop callbacks for bound analysis no longer retain the entire modeler; tax iterators release upon modeling completion, cancellation, or failure; expression indices release once parameter streaming completes. Chunked generated bundles keep identical contents and lifecycle without altering scheduling models, result semantics, or checkpoint recovery protocols.
- Docker regression with cyclic GC disabled verified prompt modeler release across no-tax, VAT, income-tax, and dual-tax configurations while keeping generated bundles streamable. Resource consumption on full 25-year engineering cases awaits independent acceptance review.

### 2026-11-15 — Alignment of Solver Internal Time Limit and Outer Process Supervision Grace

- `ies.generator.highs_milp@1.0.0` raised outer process supervision grace `PROCESS_IO_GRACE_SECONDS` from 60 s to 600 s (`command.timeout_seconds = time_limit_seconds + 600`), while still passing the user's requested `time_limit_seconds` verbatim to HiGHS `--time_limit` without deducting model-read time or silently truncating solve time to fit deployment ceilings; full process budgets continue pre-launch validation against `DeploymentPolicy.max_timeout_seconds`, rejecting over-budget submissions rather than clipping. This bounded 600 s grace covers process startup, `readModel` (standalone diagnostics showed ~88–93 s; 177 s in prior logs was presolve rather than model reading, and no integer feasible solution appeared in retained logs), clean shutdown, solution generation, and file writing; slower inputs MAY still hit real outer timeouts.
- Trusted executable `ies-highs-solver` logs start/end events, status, and monotonic elapsed timings to `stderr` for `read_model`, `run`, and `solution_write`, reusing existing stdout/stderr capture to pinpoint interrupted phases without adding a monitor service or having Workers parse solver logs for time control; preserves `kTimeLimit` (`feasible` on normal exit with incumbent, `not_solved` without), outer timeout/cancellation/lease fencing (never upgrading residual solution files to success), and non-zero exits on input read/`kError`/write errors without new config fields or protocol versions.
- Targeted Docker backend regressions passed across 371 checks in batches (covering budget generation & determinism, pre-launch `max_timeout_seconds` boundaries, delayed read/write timing, native HiGHS time limits with/without feasible solutions, outer kill/cancel/fencing, phase timing logs, computation pipeline, Worker/task APIs, and architecture gates); Ruff on changed files, `help_markdown` logic tests, and CI selector self-tests passed. Neither frontend UI nor existing engineering acceptance stacks were modified/restarted, and overall `0.15.0` acceptance is not claimed complete.

### 2026-11-15 — Tightness Verification and Reconciliation for Continuous Monthly Demand Upper Envelopes

- When generators enable continuous demand upper envelopes, `input/parameters.jsonl` records actual solver column positions of monthly peaks and same-month flow variables (omitted when disabled); before publishing `kTimeLimit` or `kOptimal` solutions, the HiGHS result adapter checks `peak - max(flow[t]) <= tol` and rejects inflated peaks instead of publishing distorted bills, taxes, or cash flows—mapping to `not_solved` (time limit, no feasible original-model solution) or `numeric_failure` (optimal-tolerance stop failing tightness). `results.rules.check_optimality`, frontend `SolverSummary`, and bilingual solver status messages now support `numeric_failure`.
- Closed the non-optimal-candidate inflated-peak gap left in PR #75 and added standalone/all-zero/mixed-zero-rate real solves, `margin < 0` counterexamples, `assets`/`net_cashflows`/multi-tax-measure and nested derived-tax checks, disabled constraints, leap-year-spanning lagged derived-tax discounting hand checks, same-model certification with exact meter isolation, and real HiGHS inflated-branch interception tests.
- Targeted Docker backend regressions at final code state passed 211 items in batches (25 in `test_demand_envelope.py`, 14 in `test_issue04_d2.py`, 11 in `test_highs_milp_provider.py`, and 161 across adapters/parameter streaming/results API/31 architecture gates); all 26 frontend logic files, build, and help manifest generation passed; isolated Docker Playwright Chromium ran 15 targeted specs (`infra`, `result-planning-solution`, `tax-chain`, `admin-help-lang`) passing; Ruff and CI selector self-tests passed. Known limitations of exact branching under narrow `2**k` scaling remain disclosed; existing snapshots, tasks, and acceptance stacks were untouched, and `0.15.0` acceptance is not claimed complete.

### 2026-11-10 — Near-Zero Cash Flow IRR and Payback Status Fix

- Unified absolute and relative near-zero thresholds for IRR and payback to avoid interpreting solver floating-point residuals as meaningful rates of return; all-zero or near-zero-residual cash flows return no value and `degenerate` status. Raw cash flows, yearly amounts, and NPV keep full precision without rewriting sealed history.
- Payback no longer misreports cases without a substantial funding deficit as 0 years, adding `not_applicable` distinct from `not_recovered`; deferred investments and renewals evaluate along the original annual axis. Synchronized bilingual result cards, types, Excel status consumption, and user/developer manuals.
- Docker backend passed 184 tests in batches (including 6 end-to-end cash-flow solve chains); real Chromium passed 3 financial scenarios covering zero cash flow, normal recovery, unrecovered, historical task immutability, and Excel content; all 26 frontend logic files and production build passed. Does not implement IRR maximization or alter solver objectives or existing acceptance deployments.

### 2026-11-10 — Equivalent Continuous Upper Envelope for Monthly Peak Demand

- When maximizing after-tax NPV and complete financial dependencies satisfy strict monotonicity, monthly demand peaks emit only the continuous upper envelope without binary peak-time indicator variables or upper-bound selector rows; certification covers all charges on shared meter points, input VAT deduction, VAT-paid surtaxes, and income-tax shields rather than checking positive rates in isolation.
- Uncertified cases—zero weights, non-NPV objectives, refund triggers, or consumed amount/asset/cash items and reverse amount constraints—continue using exact maximum encoding. Physical dispatch, finite bounds, calendar months, taxes, provenance, and same-solve result semantics remain unchanged without device/case/solver special cases or altering existing snapshots, models, tasks, or acceptance stacks.
- Targeted Docker regressions passed 241 items in batches, adding 16 comparison and boundary tests spanning hourly/half-hourly real solves, both tax bases, surtaxes, multi-year carry-forwards and losses, non-zero discount rates, shared peaks, and amount dependencies, alongside demand, NPV, tax, amount constraint, and compiler boundary regressions. Small synthetic models do not substitute for full-horizon engineering acceptance or guarantee a fixed speedup factor.

### 2026-11-10 — Preservation of `property_ref` Bounds Across Model Save and Read

- Preserved `{property_ref: ...}` inside `valid_range.minimum/maximum` of interfaces and internal variables across canonical save, final device ID assignment, and reads without altering numeric bounds, unboundedness, default ranges, or state initial values, and without adding contract versions, device special cases, hot-reloading, or duplicate checks.
- Saved models remain constrained by instance parameters or planning decision variables during actual runs. Targeted Docker backend regressions (272 items) and Ruff passed across direct YAML/template saving, immutable snapshots, zero-power complementarity elimination, non-zero power clamping, and planning power relations; real Chromium passed 2 specs verifying saving, palette selection, drag-and-drop, parameters, wiring, freezing, solving, and result consumption, alongside frontend production build.
- Historical objects that previously lost references are not mutated in place and require creating new immutable content from accurate source models; existing engineering test environments, historical versions, snapshots, tasks, and results were untouched, no long engineering solves were launched, and `0.15.0` acceptance is not claimed complete.

### 2026-11-10 — Inactivity-Based Session Expiration

- Changed the default 8-hour fixed session limit to an idle timeout so users are no longer forced out simply 8 hours after sign-in. Real pointer, keyboard edit, and scroll events on visible pages trigger renewal (including unsubmitted form edits), coalesced in 30-second windows; background polling, auto-refresh, and stale activity after tab hiding/sleep do not renew.
- Renewal extends both server session expiry and the `HttpOnly` cookie, keeping HTTPS `Secure` and `SameSite` flags; sessions expire immediately past the cutoff and expired or revoked sessions require signing in again. Password changes, sign-out, and background tasks proceed unchanged. Renewal network failures surface visibly while subsequent real interactions can retry.
- Docker backend auth/CSRF/architecture regressions (75 items), 26/26 frontend logic files, production build, and 4 real Chromium auth scenarios passed. Time-advancement tests proved interactive renewal extends past the original login horizon while read-only polls do not and expired sessions cannot resurrect; browser tests verified cookie sync, unsubmitted-edit renewal, and continued renewal after initial role-fetch disconnect. Existing engineering stacks and tasks were untouched, and `0.15.0` acceptance is not claimed complete.

### 2026-11-09 — Throttled Curve Reads and Cancellation on Tab/Page Switch

- Results page renders summary, financials, and planning solutions first while loading step curves sequentially by series and page, preserving every series, identity, unit, and raw value; switching tabs, tasks, retries, or pages cancels obsolete requests so late responses never overwrite newer selections and intentional cancellation never masquerades as a read failure.
- Hourly queries validate authorization, ownership, and object resolution first, return the DB connection at the top-level read-only use case, and then stream-scan sealed files without widening connection pools or altering solve/task/evidence protocols. Client disconnect signals propagate cleanly to the HTTP layer.
- Docker backend results/auth/architecture regressions (109 items), 25/25 frontend logic files, production build, and 4 isolated Chromium curve regressions passed. Single-connection-pool tests proved concurrent queries remain unblocked during file scans; real browser tests navigated away to Tasks/Versions mid-load and restored completely upon returning. Existing engineering stacks and in-flight tasks were untouched, and `0.15.0` acceptance is not claimed complete.

### 2026-11-09 — Removal of Duplicate Project Title in Workbench

- Removed duplicate project title in the sidebar and meaningless "IES" decorative placeholder; the project title now displays solely at the top of the content pane alongside current version, currency, and autosave indicator, dropping unused desktop/mobile CSS rules.
- Docker Chromium first reproduced duplicate titles in the legacy UI and then verified a single project title across all seven sub-pages in Chinese/English, light/dark modes, and mobile viewports; 3 browser regressions, 24 frontend logic files, and production build passed without altering backend, acceptance stacks, tasks, or results.

### 2026-11-09 — Task Completion Progress and Running State Feedback

- Upon completion, task list and details uniformly display `100%` and the completion stage instead of remaining overwritten by the last `85%` intermediate report; historical stage observations and resource stats remain visible, while failed, timed-out, and cancelled tasks preserve the percentage at interruption. Running status icons use the existing spinner animation and stop upon completion, honoring `prefers-reduced-motion`.
- Clarified that multi-task concurrency is governed by available Worker count and global compute slots; serial discipline during long engineering acceptance is not a code limit, and deployment concurrency stayed unchanged. Docker backend (80 items), 24 frontend logic files, real Chromium task run/completion regressions, and frontend production build passed without restarting existing engineering test environments or claiming `0.15.0` complete.

### 2026-11-09 — Session Expiry Redirect and HTTP Error Presentation

- When cookies expire and protected requests return unauthenticated errors, frontend session state clears uniformly and redirects to sign-in consistently across JSON and download requests while preserving distinct handling for sign-in failures, takeover-required sessions, and 403 authorization errors. HTML proxy error pages render actual HTTP failure messages rather than misreporting malformed success JSON.
- Documented the distinction between the 8-hour session lifetime and 60 s / 120 s HTTP request timeouts, and clarified submitted tasks are not cancelled on sign-out. Docker frontend build, 24 logic test files, and real Chromium regressions for expired cookies on current page/reload, sign-in/takeover, HTTP error pages, and Excel exports passed without altering session TTL or adding auto-renewal.

### 2026-11-09 — On-Demand Large Result Reading and Streaming Reports

- Full evidence remains immutably sealed; publishing results atomically builds a lightweight query index so result display, assessment, candidate selection, and scheme application read only needed sealed facts. Existing large evidence can build the identical index via an explicit CLI command without re-solving; unindexed reads fall back to read-only streaming projection without writing during queries.
- Step curves display in windows of up to 1,000 raw points supporting paging and jump-to-step navigation while preserving all series identities, units, and full counts; Excel exports use streaming worksheets and disk-chunked transposition for full series, and project packages continue including complete evidence.
- Targeted Docker backend regressions and isolated Chromium curve-window, planning-solution, and Excel tests passed; a 1.5 GiB synthetic JSON projected inside a 1 GiB no-swap container at ~25 MiB peak RSS. Existing engineering stacks were untouched, no long solves re-ran, and `0.15.0` acceptance is not claimed complete.

### 2026-11-08 — Persistent Dark Mode

- Language and appearance preferences persist via browser local storage independently of accounts and projects, restoring before first paint; Chinese/English switches immediately update UI strings, number formatting locale, and document language attributes. Initial defaults remain Simplified Chinese and light mode. 23/23 Docker logic files and 9/9 language/theme/help Chromium tests passed, verifying persistence of English+dark and Chinese+light across real browser close/reopen.
- Moved temporary browser profiles for restart specs into container temp directories cleaned afterward so Chromium profile permissions never block CI artifact uploads, keeping all artifact trees traversable by non-root users.
- Both the sign-in page and top navigation bar include a one-click light/dark toggle beside the language button with keyboard support and ARIA status announcements; selections persist independently across reloads, sign-outs, and restarts.
- Unified semantic color tokens cover forms, dialogs, Help Center, model cards, charts, and canvas edges/controls; narrow viewports place navigation on an independent scrollable row while keeping language and theme toggles visible.
- Docker frontend build, 23/23 logic files, and 10/10 isolated Chromium tests passed including browser close/reopen, bidirectional toggling, language independence, dialogs, Help Center, and canvas wiring without modifying existing engineering stacks or submitting compute tasks.

### 2026-11-08 — Generation Pipeline Performance Measurement Correction

- Removed the misleading `--baseline-bytes` flag from the generation-to-run-prep benchmark script that switched only output labels without switching implementations; script output and help text now reflect measurement of mounted code only rather than labeling current runs as legacy baselines. True before/after comparisons require running actual implementations separately while recording commits, dirty diffs, commands, inputs, and container limits without adding legacy-format compatibility to production.
- Prior M3/M4 generation-chain comparisons lacked reproducible legacy commits and exact commands and therefore are withheld as acceptance evidence without invalidating standalone absolute resource measurements of the current pipeline or conflating them with synthetic result-chain benchmarks that did carry real baseline branches. Production computation logic, deployment, and engineering test sites were untouched.
- 15 Docker CLI benchmark and adjacent content/measurement tests plus Ruff passed, verifying rejection of the misleading switch on both input modes alongside real generation, persistence, recovery, and run-dir preparation on public models without launching a solver or substituting prep tests for full-horizon engineering acceptance.
- Fixed CLI regression subprocess `PYTHONPATH` to import explicitly from the checked-out worktree instead of picking up installed image packages and failing prematurely on misplaced deploy config paths without altering production config loading or adding fallback paths.

### 2026-11-08 — Generic Resource Visibility and Exhaustion Settlement

- Task details show latest-stage observed Worker shared memory/swap limits, usage, and container-lifetime peak via cgroup v2 under Docker/Linux without hardware-model branching; unobservable values report as unknown without blocking execution. Absence of hard limits does not imply infinite resources, nor do solver command ceilings reserve memory, avoiding hardcoded 8 GiB engineering caps.
- Python `MemoryError`, executor-confirmed OOM, or `SIGKILL` accompanied by rising cgroup OOM counters record actual stage and resource facts and fail cleanly as resource exhausted without auto-retrying under identical resources; committed stage artifacts persist so manual retries reuse submitted solver outputs. Other process exits preserve true exit codes and unknown causes under standard lease recovery rather than inferring OOM from `SIGKILL` or lease expiry alone.
- 209 adjacent backend tests and 74 final resource/task-API/architecture tests passed in Docker (with overlap); a real 512 MiB no-swap OOM experiment confirmed single failure, slot release, and zero auto-retry. Frontend production build and 1 real Chromium resource display & bilingual toggle spec passed. Also restored two shared fixture registrations missed in prior CI across 14 template tests without skipping cases.
- Did not alter deployment caps, solver options, math relations, horizon, plugin selection, auto-scaling, or model downgrades; does not claim full-horizon engineering acceptance or `0.15.0` completion. Cloud CI and merging remain separate.

### 2026-11-08 — End-to-End Chunked Handoff, Compact Parameters, and Result Archival (M3/M4)

- Unified Bundle, storage, checkpoints, Runtime, recovery, and exports under one repeatable read-only chunked content protocol (using in-memory streams for small payloads). Once the generation checkpoint commits, persistent object references replace in-memory sources and modeling matrices release; runtime tempdirs clean only after raw outputs and receipts persist, and recovery with committed execution outputs never re-runs solvers. Eliminated temporary traceback reference cycles in modeling candidate errors so release never relies on forced GC.
- Companion generators and adapters use a small header plus ordered parameter records sharing frozen expressions to recover units, series, provenance, and financial facts directly by solver column index instead of rescanning MPS files or decoding massive parameter JSONs. Results retain one frozen representation reused across step series, sealed evidence, and project ZIPs without full decompression or duplicate text blobs. Generic layers remain agnostic to companion math record layout without changing algorithm selection, solver options, math relations, or plugin hooks.
- Storage reconciliation inspects only object existence and orphan size metadata without reading full large payloads; downloads send HTTP headers only after reading the first actual byte chunk, preserving original error statuses for missing objects without adding pre-checks, hashes, or integrity verification.
- Passing Docker batches: computation/storage/Runtime/Worker/provenance (297), finance (98), full-horizon & reproductions (53), reconciliation & storage (56), content & downloads (22), templates & release/recovery (46), package import/export adjacency (104) (overlapping counts, not summed). Generator/executor/result protocols & swap registration passed 40 tests; real Chromium export flows passed 3 specs. Pre-existing Starlette deprecation and test cleanup FK-cycle warnings remained without masking or ignoring.
- Legacy vs. new generation-to-run-prep speedup claims are excluded because the script's `--baseline-bytes` flag did not execute legacy code; a separate 100k-column synthetic primal result-chain benchmark with a genuine baseline branch showed median peak RSS `247.4 → 180.6 MiB` and total time `3.24 → 2.39 s` (while 10k columns used slightly more memory and slower archive substage), both without swap or `tracemalloc` under shared host noise, without equating synthetic primals to full engineering solves or claiming universal speedup.
- Completed generation, persistence, release, recovery, and run-dir prep on the frozen engineering input inside an 8 GiB no-swap container (read-only over original data, without claiming the original task or spawning the solver), which does not prove sufficient solve headroom or full solve/finance/export acceptance; cloud CI, deployment checks, and independent end-to-end engineering acceptance for `0.15.0` remain incomplete.

### 2026-11-08 — Line-by-Line Reading and Compact Primal Storage (M4 Kickoff)

- Result adapter now parses primal solution text line by line, eliminating full-file UTF-8 string buffering and `splitlines` lists while storing one `float64` column array shared across variables, components, series, and financial facts. External grammar, result shapes, solver statuses, unit conversions, and financial summation order stay unchanged, preserving output boundary errors without adding internal pre-checks.
- 104 targeted Docker tests passed covering line-by-line parsing, line endings, exact binary floats, 0/1/100k-column layouts, chunk-boundary UTF-8 failure, adapter/finance/real-HiGHS/pipeline/Worker tests, and architecture gates; Ruff and diff checks passed, and Help Center manifest built 49 chapters cleanly.
- Across 3 ordinary runs each of 100k, 300k, and 1M-column public synthetic primals under 2 GiB / 2 CPU Docker limits, 1M-column median process peak RSS fell from `~200.0 → 79.9 MiB` and primal representation from `~30.95 → 7.80 MiB`, while median parse time rose from `0.395 → 0.447 s` (`+13.3%`, no parsing speedup claimed). Excludes projection/archival and `tracemalloc`, and does not represent full engineering cgroup peaks or end-to-end acceptance.
- Delivered only primal reading start of M4; small headers/ordered parameter records, shared expression refs, direct column mapping, chunked result archival, and end-to-end M3 handoffs remain incomplete without claiming M4 or `0.15.0` complete.

### 2026-11-08 — Chunked Content Target Contract and Single MPS Writer (M3 Kickoff)

- Formalized the effective target contract, ownership, and release timing for repeatable read-only chunked content across Bundle, storage, generator, runtime, and Worker docs: generators stay pure, persistent object references replace in-memory content after generation checkpoints commit, and execution directories clean only after raw solution and receipt commits without introducing second contract versions, execution modes, or digest checks. Target contract docs do not imply production migration already finished.
- MPS formatting streams record by record emitting byte chunks up to 64 KiB, with explicit full-text helpers delegating to the same writer while preserving exact row/column order, numeric formatting, objective offsets, integrality flags, bounds, and cancellation/fencing semantics.
- 65 targeted Docker tests and 31 architecture gates passed covering chunk byte-equivalence and repeatability, direct file writing, bounded buffering, cross-chunk UTF-8, cancellation, existing final matrices, full-horizon real solves, and Worker processes; Ruff passed and Help Center built 49 chapters cleanly. Production handoffs and full engineering peaks remain unverified; M3 and `0.15.0` remain incomplete.

### 2026-11-08 — Shared Final Numeric Matrix and Explicit Position Mapping (M2)

- Bound analysis and the final matrix now share symbol identities and concrete affine rows: technical, capacity, bus, closed-port, and step-planning relations reuse numeric rows directly while complementarity, construction activation, and piecewise finance conversions append auxiliary columns/rows to the same store instead of reconstructing and retaining full linear DTO object graphs. Propagation buffers stay decoupled from final symbol IDs so final matrices never pin analysis bounds or queues.
- Centralized allocation of unique solver row/column ordering and position maps; MPS writer uses compact numeric transposition and provenance targets use numeric indices while preserving public provenance and result shapes. Small public linear DTOs remain supported by the same writer, inspection views materialize single items on demand, and trusted column declarations skip redundant item-by-item type checks.
- Decoupled standalone series declarations from capacity constraint lowering so declarations no longer trigger bound analysis upfront. On the public 2-year campus (131,401 columns, 122,641 rows), the full MPS (34,087,664 bytes), units, financial facts, components, series, and provenance match the M1 baseline byte-for-byte.
- Across 3 runs each at three public scales under identical Docker limits, 100k-row median peak RSS dropped `~266.8 → 182.9 MiB`, matrix assembly dropped `1.158 → 0.238 s`, and cumulative full GC dropped `0.429 → 0.025 s`, while total runtime including analysis and MPS write shifted `7.541 → 7.141 s` (50k-row total rose `~6.3%`). Local assembly gains are not generalized as end-to-end speedups.
- Docker targeted batches passed: 115 items (2 heavy pre-existing benchmarks omitted), 152 finance/project/result items, and 49 network/system-equation items (overlapping scope). New compact-store files passed Ruff while legacy large generator/template tests retained baseline lint warnings; diff checks passed. MPS/parameter/Bundle byte handoffs await M3/M4, and full low-memory modeling, cloud CI, and `0.15.0` acceptance remain incomplete.

### 2026-11-08 — Compact Relational Storage and Bound Analysis (M1)

- Shared symbol identities and provenance within a modeling pass using compact numeric storage for per-slot bounds, sparse nonzeros, provenance IDs, and step indices; modeling bindings feed straight into analysis storage and downstream consumers materialize active rows on demand instead of keeping duplicate analysis-relation and step-nested DTO object graphs.
- Equality substitution retains only original row offsets and builds exact substitutes on demand; sparse dependency queues use compact indices, deterministic initial cursors, numeric FIFOs, and pending flags while preserving time-varying coefficients, state links, aliases, missing/one-sided/zero bounds, missing/empty provenance, conflict recovery, propagation order, budgets, and outward rounding without disabling GC or relaxing leases.
- On public growing-relation models across 3 runs each under identical Docker limits, 50,000-step median peak RSS fell from `~152.3 MiB` to `65.5 MiB` and time fell from `3.716 s` to `2.993 s` (small scales retain fixed overhead without uniform proportional speedup). Under the baseline comparison limits, the frozen engineering input passed relation collection and bound analysis completely with zero conflicts, budget exhaustion, swapping, or OOM (raw metrics kept in private acceptance logs).
- Final property/structure (86), cross-module (100), and project/finance/result (124) Docker batches passed covering full public campus MPS equivalence, time-varying/lagged terms, queue ordering, provenance resolution, conditional conversions, construction expenses, VAT/income tax, and result adaptation; Ruff and diff checks passed without executing cloud CI, deployment, or long engineering solves.
- Delivered only relation analysis; final linear DTOs, matrix writing, bundle handoffs, and result declarations await M2–M4 without claiming end-to-end low-memory modeling or `0.15.0` engineering acceptance complete.

### 2026-11-08 — Isolated Task Interpreter Subprocesses and Lease Control Loop

- Workers now execute each Attempt inside an isolated interpreter subprocess while the control process continues heartbeats, lease renewals, expiry recovery, and stop monitoring so long GIL/full-GC pauses during modeling or post-processing never stall the control loop. Subprocesses wire dependencies from the same deployment composition root without inheriting DB connections, provider instances, or large file memory; existing stage checkpoints, terminal states, and fencing rules remain unchanged.
- Fixed task subprocesses reusing service-startup assembly: separated DB initialization from runtime dependency wiring so tasks never reinstall triggers or seed admin accounts or contend for DDL locks with project reads/transactions. API entry continues initializing the DB without adding retry waits, looser leases, or internal re-checks.
- Cross-process stop events bridge lease fencing and Worker exit cleanly across normal completion, abnormal exits, startup failures, and exit grace periods without fabricating terminal business outcomes or extending TTLs/retries.
- 126 targeted/adjacent Docker regressions and 8 formal Worker CLI re-checks passed (overlapping) covering heartbeats/renewals/fencing across GIL pauses, process lifecycles, and small real HiGHS solves; Ruff passed. Mathematical relations, horizons, complementarity, solver budgets/options, and full low-memory modeling stay unchanged, and `0.15` acceptance remains incomplete.
- After the subprocess wiring fix, 85 targeted/adjacent backend tests and all 14 previously failing Group-2 Playwright specs passed; an isolated PostgreSQL test verified task wiring no longer contends with project read transactions on trigger DDL locks.

### 2026-11-07 — Staged Artifact Persistence and Post-Processing Recovery

- Following formal stage handoff contracts, implemented `prediction`, `generated`, and `execution` checkpoints using existing object storage and attempt owner references without adding business tables or compatibility layers. Bundles persist before solving; raw outputs and receipts persist before cleaning temp workdirs, retaining undelivered temp files and failing visibly on receiver failure.
- Retried Attempts resume from committed stages of the same task: reusing actual predictions, skipping modeling when `generated` exists, and skipping solving when `execution` exists. Original receipt identities stay intact while final evidence attaches to the active Attempt; publication failure leaves no partial business result while preserving prior checkpoints (legacy tasks lacking checkpoints still recompute without recovering lost solver trees).
- Final archival reuses committed objects without holding intermediate multi-round Bundles or raw output bytes resident across rounds; empty-queue fallback polls queued DB rows to bridge message-loss windows under row locking without modifying math models, horizons, complementarity, solver options, or Bundle byte contracts.
- 107 Docker real-HiGHS checkpoint recovery, commit rollback, lease, and architecture tests passed alongside 35 prediction tests (including coupled round-2 recovery), 12 multi-year horizon tests, and 45 adjacent adapter/finance/Worker regressions; an isolated PostgreSQL concurrency probe verified single recovery, single claim, slot release, snapshot preservation, and fencing across tokens.
- Narrowed lease row locking strictly to stage publication boundaries rather than holding locks across large disk writes, rolling back metadata/refs if lease eligibility expired mid-write (5 targeted sequential/fencing/recovery tests passed, 3 overlapping).
- Fixed draft-switch browser test session setup by signing out cleanly via UI rather than injecting invalid markers or bulk-aborting requests, passing the previously failing Chromium spec while keeping zero-error and frozen-input assertions; 100k/200k-column synthetic adapter benchmarks (`~1.20 / 1.55 s`, peak `149 / 240 MiB`) reflect constrained adapter runs only without extrapolating full engineering runs or claiming `0.15` complete.

### 2026-11-07 — Explicit Model Identity Display in Device Palette

- Device palette now displays backend model IDs beside model names as well as in hover tooltips and accessible names so same-named project models are unambiguously distinguishable without adding parallel identity/version fields or silently replacing existing instances.
- Docker frontend build and isolated Chromium regression passed: saved two same-named models via real UI, verified distinct IDs, dragged the second model by ID, and confirmed instance bindings, default parameters, and post-reload persistence without creating engineering compute tasks or modifying existing engineering sites.

### 2026-11-07 — Reclaiming Expired Attempts Left by Restarts

- Workers reclaim expired leases per resource pool ahead of slot gating inside a short transaction that terminates stale attempts, releases slots, and records causes: tasks within retry limits requeue on their original snapshot while cancelled or exhausted tasks do not auto-rerun. Cancelled batch tasks keep completed samples and partial-completion outcomes; old progress and attempt logs stay intact while fresh claims issue new tokens.
- Renewals and write eligibility check actual expiry timestamps rather than trusting `active` status flags alone; PostgreSQL lease row locks serialize reclaim, renewal, and result publication so stale tokens never resurrect or commit late.
- 66 Docker lease/session/assembly/real-Worker tests and 32 architecture/boundary tests passed; an isolated empty PostgreSQL dual-reclaim probe confirmed single reclaim/requeue, slot release, snapshot preservation, and token fencing alongside passing Ruff checks. Recovery performs full retry from committed stages rather than unimplemented raw-solution recovery mid-solve.

### 2026-11-07 — Reusing Column Name Index in Result Component Recovery

- HiGHS result adapter component recovery now reuses the prebuilt column-name index instead of linearly scanning full column lists per affine term, while MPS column order reads line by line until section end rather than decoding full-file line lists. Same-solve primal values, summation order, unit conversion, missing-ref errors, math models, solve policies, and public contracts remain unchanged.
- Asserted zero linear scans inside two-column component fixtures covering index 0, multi-component unit conversions, and line-by-line MPS reading; 31 Docker adapter/small-finance tests and Ruff passed without restarting live Workers or claiming existing running engineering post-processing accelerated or `0.15` complete.

### 2026-11-06 — Slice D2 Taxed, Multi-Period, and LP Method Investigation Closeout

- Reused the public 2-year N/F model and identical generation, raw-matrix diagnostics, and result recovery pipeline across 18 runs (3 runs × 2 modes across 3 explicit tax cases: no-tax, VAT+CIT, opening losses) strictly for diagnosis rather than equivalence optimization. Cases with >8,700 tax-aggregation nonzeros still solved natively below `0.52 s` with median modeling times `~18.8–21.6 s`, disproving dense tax rows as the root cause of slow solves.
- Reconciled annual cash flows, VAT carry-forwards, and loss carry-forwards directly from native column values, demonstrating identical annual operations yield distinct after-tax cash flows. Six LP-relaxation trials on the public 2-year campus hit native default time limits, whereas interior-point returned native optimal status in `~23.6–27.6 s` with 1,015 fractional binaries; accuracy warnings, crossover/cleanup logs, and raw-unit residuals stay preserved without substituting relaxed solutions for MILP primals or production policies.
- Audited read-only legacy site evidence: confirmed failure exit and call stacks while native search logs/matrices and primal state remained unrecoverable ("missing solution file" does not prove "no feasible primal existed"), leaving the legacy engineering run's unique root cause unknown without fabricating historical results or mutating live deployments.
- 57 Docker targeted/adapter/2-year N/F regressions and Ruff passed alongside manual verification of all 18 tax reports; D2 closed cleanly on reproducible evidence boundaries without changing production algorithms, templates, tolerances, or active services, leaving D3/D4 and `0.15.0` acceptance separate.

### 2026-11-06 — Preserving Real MIP Gap Within Optimality Tolerance

- Fixed a Stage-C review omission: HiGHS `kOptimal` can accompany a non-zero gap within optimality tolerance, so the solver shim no longer overwrites MIP best bound and gap with objective value / `0` (using objective / `0` solely on optimal LPs lacking MIP statistics). Results UI and exports project sealed values directly without changing tolerances, statuses, or models.
- Verified in Docker using a real 100-column / 50-row synthetic MIP at 50% optimality tolerance reproducing native `kOptimal` with `~9.17%` gap vs. legacy shim `0`, alongside 42 adjacent adapter/solve tests and optimal LP `0`-gap confirmation without changing production default tolerances.

### 2026-11-06 — Slice D2 Initial Root LP and Input Constraint Comparisons

- Added an explicit Docker-only diagnostic tool reusing the complete D1 generation pipeline to record native presolve/search logs, real primal/bound/gap values, and raw-matrix residuals, counting fractional binaries separately from simultaneous physical complementarity flows without inferring device semantics from variable names or altering production code.
- Measured 4 groups × 3 independent processes on the public 2-year campus under fixed 2 CPU / 4 GiB, 2 threads, `seed=0`: under the original budget both MILP (0 nodes, 0 strong branching/cut rounds) and standalone LP timed out; explicitly raising the full-horizon budget solved MILP at root within tolerance while standalone LP held 730 fractional auxiliary binaries yet 0 simultaneous flows across 35,040 complementarity pairs. Budget runs modify inputs and are not optimization speedups; 30 s refers to the native soft limit excluding outer wall-clock overhead.
- Small real counterexamples showed neither buy/sell spreads nor charge/discharge losses universally guarantee redundant complementarity, and repeated inputs cannot reset or reuse first-period state trajectories blindly. 29 Docker regressions and Ruff passed without altering GUI or repeating browser specs.
- Covered public root LP, budget, complementarity, and state counterexamples only (public campus lacks tax rules); taxed performance, legacy site root cause, and D2/D3/D4 acceptance remain open.

### 2026-11-06 — Launching Phased Performance Investigation for Slice D

- Public Roadmap established D1 baseline, D2 single-factor root-cause comparisons, D3 minimal equivalent optimizations, and D4 engineering handoff; added a Docker-only benchmark runner over the public campus reusing existing relations, bound propagation counts, and row/column provenance without modifying production models or solver protocols.
- First independent 2 CPU / 4 GiB observations: public 1-year modeling `13.29 s`, solving `2.62 s`; 2-year modeling `29.11 s` with no incumbent within a 30 s native soft limit, while raising the explicit budget returned optimal within tolerance (an input comparison, not an equivalent optimization). Single runs make no speedup promise nor explain the legacy engineering run directly.
- Bound propagation accounted for roughly half of ordinary modeling time, with separate `cProfile` profiling inspecting rational/outward-rounding work separately from normal timings; 2 lightweight stats checks and Ruff passed without adding per-commit full-year perf gates or touching existing engineering stacks.

### 2026-11-06 — Solver Time-Limit Status and Same-Solve Incumbent Return

- Normal HiGHS time-limit termination no longer fails as a crash: complete feasible primals return `feasible` / `restricted_results`, while runs without incumbents return `not_solved` / `insufficient_evidence`; only `kOptimal` reports proven optimality. True execution errors, supervisor kills, cancellations, OOM, and lease fencing keep failure/interruption semantics without reading residual outputs.
- Seals termination reason, feasibility flag, objective, best bound, and raw relative gap together; when feasible, recovers solutions, curves, and financial facts through the same chain leaving unknown values unzeroed. Results UI, reassessment, and Excel share this sealed summary, restoring objective constants and units once without mislabeling time-limit runs as optimal.
- User `time_limit_seconds` stays verbatim with `+60` s initial Bundle wall-clock I/O allowance bounded by deployment ceilings; proprietary contracts remain `1.0.0` with no new task statuses or compatibility layers.
- Two targeted Docker backend batches (94 and 70 tests, overlapping), 3 time-limit and 4 adapter real-HiGHS specs, frontend build, 23 logic files, and 2 small real Chromium runs passed, supplemented by explicitly isolated display fixtures for time-limit feasible/unfeasible views without treating fixtures as real solves or engineering acceptance.

### 2026-11-06 — Modeling Guidance Corrections for Independent Engineering Cases

- Clarified physical boundaries, distinction between costless bindings and out-of-service equipment, absence of automatic AC limits/curtailment on `power_curve`, and explicit source-allocation obligations in the User Guide without adding device-specific UI sections.
- Corrected outdated documented limitations covering optional construction, cost occurrence conditions, unified relational bounds, partial-net named assets, and frozen baseline comparisons—avoiding workarounds such as artificial reference numbers, positive minimum capacities, or stripping tax rules.
- Offline Docker checks verified modified document structure and local links alongside private work-note syntax checks without modifying software code, catalog defaults, deployments, drafts, tasks, or running real solves.

### 2026-11-06 — Help Center Body Pipe Character Parsing Fix

- Fixed Help Center Markdown parsing where inline pipes such as `purchase|sale` or `mandatory|optional` triggered false table detection with zero cursor progress, hanging browsers; shared strict table-start detection (`isTableStart`) across paragraphs and tables while preserving real tables, code blocks, and section navigation.
- Added parser regressions for inline pipes and tables/rules immediately following paragraphs across all published help pages; browser verification walked the entire live Help Center TOC and verified `assembly-yaml` rendering and reload cleanly without modifying accounts, projects, tasks, solver models, or `0.15.0` status.
- Docker frontend build, 33 help logic checks across 49 live chapters, and 1 full Chromium TOC walk & reload test passed.

### 2026-11-05 — P5 Phase Closeout and Unification of Engineering Tests into `0.15.0`

- Confirmed `0.14.5` milestone completion per user decision, keeping its original exit audit record; closed standalone P5 so no duplicate development/acceptance phase remains, consolidating engineering testing cleanly into `0.15.0` without marking unexecuted tests as passed.
- `0.15.0` unifies Provider extension acceptance and the engineering user checklist (deployment & accounts, case data, generic docs & modeling, independent alternatives & freezing, full-horizon solving, physical & financial checks, exports, and project package round-trips), reusing valid prior evidence while keeping remaining tests under `0.15.0` exit criteria.
- P4 is no longer a mandatory gate with pre-committed model compression or speedup; future optimizations proceed strictly against measured, reproducible bottlenecks with correctness baselines and target resource metrics.
- Pure documentation state/roadmap/link alignment: Docker verified 8 docs/navigation files, 76 local links, and code blocks without re-running solves, browser tests, or altering production code, services, user data, or product versions.

### 2026-11-05 — P3 Network and Finance Integration Acceptance Closeout

- F3 merged in PR #52 (mainline `6d946f4`). Review fixed project-currency conversion of baseline monetary amounts and actual-consumed period checks without adding currency systems, protocol versions, or duplicate checks along trusted paths. Four backend lanes, four Chromium lanes, frontend logic, and CI Gate passed on fix commit `25e1ba7`; cold-start and PG concurrency were skipped per test-selection rules and not counted as passed.
- Added public 2-year, 17,520-step N/F integration acceptance independently solving two alternatives differing in efficiency and unit capex, reconciling physical port buses, closed unconnected ports, transmission losses, system metering, multi-year carry-over state, and planning scales. System metering feeds named revenue, asset capex follows occurrence/payment schedules, and VAT, derived taxes, income tax, and depreciation all consume the same solve.
- Hand-verified per-period candidate cash flows `[-220, 322.84, 293.0608] CNY`, baseline `[-165, 311.6004, 281.6988] CNY`, and difference `[-55, 11.2396, 11.362] CNY` matching objective and evaluation NPV; deferred payments and post-horizon tax arrivals are not counted early, book values are not treated as recoveries, and frozen amounts keep exact original strings.
- 1 local Docker integration test, 55 adjacent network/finance real-solve tests, and 74 F3/F2/architecture checks passed (overlapping scope). Remaining P4, P5, and `0.15` extension acceptance stay separate without claiming independent blind engineering testing complete.

### 2026-11-05 — F3 Execution Chain for Frozen Baselines and Difference Evaluation

- Evaluation confirmation supports `absolute`, `reference`, and `difference` modes, selecting accessible sealed task evidence or user-declared baselines. Submission freezes chosen values, units, periods, selectors, and provenance without running extra baseline solves, reading live drafts, or adding currency conversion systems; missing referenced quantities error by alias without zero-filling.
- Frozen quantities enter the same optimization model along existing named-amount, scalar-constraint, unified-relation, and provenance pipelines. `difference` subtracts separate-taxed baseline cash flows from candidate cash flows for objective and NPV/IRR/payback evaluation; `reference` neither auto-subtracts baselines nor re-taxes differences.
- Task submission UI exposes the generic comparison document input; loading local historical tasks materializes `task_result` references into `declared` preserving frozen values and fixing reload emptying. Results UI and Excel distinguish candidate, baseline, and difference streams, while project packages preserve original evidence and provenance without forging imported task IDs.
- Two targeted Docker backend suites passed (125 and 61 items) covering input, authorization, exact evidence selection, amount/period/tax selectors, real HiGHS differences and constraints, same-solve recovery, project packages, and Excel; frontend build, 4 logic test files, and 1 real Chromium submit/solve/results/Excel/reload spec passed.

### 2026-11-04 — F1 Execution Chain for Named Cost Items and Amount References

- Finance documents now support independent named cost items sharing one driver, unit- and period-scoped affine amounts, and parameter overrides; planning connects amounts into scalar constraints via explicit references. Named items reuse base cost, tax, construction-occurrence, and same-solve machinery; amount definitions themselves add no cash flows unless explicitly consumed by net cash items.
- Analyzes solely the actually consumed closure of amounts, binding devices, system quantities, costs, tariffs, demand charges, and maintenance along unified relations, bound queries, unit conversion, and provenance chains. System-quantity non-negativity is proven from technical relations (pinpointing amount source, system block, variable, and step on failure); unimplemented tax/baseline sources reject explicitly rather than forging capability flags.
- Generic full-document and form round-trips retain named structures and exact decimals; Results UI and Excel consume the same sealed `cost_items` and `amounts` without recomputation or duplicate cash deductions. Overrides bind the exact immutable profile object rather than drifting by shared profile ID, and result recovery no longer mutates generated models.
- Fixed initialization order revealed by CI: amount consumption closures form during full relation collection while pure technical symbol/template init avoids reading finance early; updated Worker sealing assertions preserving empty-collection semantics without relaxing field checks or adding fallback defaults.
- 187 targeted Docker finance/contract/bound/export/real-solve tests, legacy consumer regressions, frontend build, 20 logic files, and 2 real Chromium specs (F1 edit/solve/history/Excel reconciliation and N3 adjacency) passed.

### 2026-11-03 — Closeout of Closed-Port Network Boundary Execution Chain

- Unconnected input, output, and bidirectional ports enforce zero flow per step even when unreferenced by equations or tariffs; predefined inputs, `blind` interfaces, and internal states remain active. Real external supplies, exports, and emissions connect via explicit source/sink devices without gas or grid special cases.
- Assembly partitions port buses and closed ports uniformly, and generators collect closed relations for unified bound analysis before emitting same-solve rows and provenance without presolve hacks, artificial upper bounds, terminal states, auto-wiring, or duplicate internal checks. Removed heuristic blocks based on missing incoming edges, source/sink counts, or supply/demand peak estimates.
- Submission validates `network.closed_ports` on the chosen generator only when ports actually need closing (pinpointing device and port if missing); built-in generator declares `network.closed_ports`, reusing existing series and provenance outputs without new UI pages or output types.
- PR #48 reviewed at commit `c7cc8e6`, passing Docker CI #41636683151 across four backend lanes, four Chromium lanes, frontend logic, and CI Gate (cold-start and PG init skipped per selector rules). N2 closed; N3 system equations and P3 finance slices remained open.

### 2026-11-03 — Target Contract Closeout for Network Boundaries and Generic Finance

- Defined target contracts in format manuals for closed port boundaries, `system_equations` local constants/references/states/outputs, named cost items, amount sources, independent cash movements, capitalization, derived taxes, and frozen baseline vs. `reference`/`difference` evaluation scopes alongside same-solve outputs, overrides, and capabilities.
- Review aligned 1-to-1 mappings from tax selectors → sealed fields → baseline selectors, per-occurrence `cash_movements` VAT confirmation/overrides, `amount_relation` syntax, and device/system series catalogs, pagination, and Excel identities while correcting candidate/baseline subtraction order in tax counterexamples. Closed defined interfaces only without adding tax workflow engines or duplicate validation layers.
- Roadmap clarified N2/N3 and F1/F2/F3 dependencies, acceptance, and exclusions while retaining `1.0.0` proprietary contracts and existing YAML/backend-centric pipelines without adding special forms or second compute engines. Pure documentation delivery.

### 2026-11-03 — Execution Chain for Ideal Port-Network Connections

- Assembly validation and built-in generators share pure port partitioning from `assembly`: shared buses include only ports linked by `connections`, never merging same-instance same-carrier inputs/outputs automatically, while internal device conservation, efficiency, and capacity stay governed by device YAML equations.
- Preserved connection fields, `1.0.0` contracts, and generic editing entry points without mutating frozen objects; re-solving follows explicit port wiring rather than auto-bridging legacy device short-circuits.
- Removed the assembly heuristic marking renewables without grid export or storage infeasible; absence of an export connection does not prove excess energy exists, leaving supply-demand feasibility to actual equations and same-solve execution. Delivered ideal connections only; closed ports, allocation accounts, and finance slices stayed in remaining P3 scope.

### 2026-11-02 — Restoring Actual Step Index in Bound-Conflict Diagnostics

- Relational bound analysis now propagates binding axes and actual step indices into conflict diagnostics instead of labeling step conflicts uniformly as scalar `0`. In three-step state-recurrence counterexamples, conflicts triggered by relations at `step=2` report `step=2` even across predecessor state variables while scalar conflicts remain `scalar/0`, preserving existing propagation, conflict logic, valid models, and exact MILP policies without extra validation layers or name parsing.

### 2026-11-02 — P1/P2 Audit Remediation Batches and Phase Exit

- Merged correctness, duplicate-work optimization, and result-export remediations in PRs #40, #42, and #43. Combined with existing relation/provenance/resource/policy evidence and same-solve construction/cost/tax verification, closed current P1/P2 scope.
- Commit `46d6d49` passed Docker CI #41448301945 (172 backend, 33 Chromium, 4 frontend logic files; 5 PG-specific tests skipped and cold-start/PG-concurrent init not run). Local targeted/interactive evidence lives in PR review comments.
- Production retains full exact MILP without default auxiliary LP, global elimination, or a second presolve pipeline, and without claiming universal tightest bounds or speedups; remaining Roadmap track starts at P3-N/P3-F with `0.15.0` open.

### 2026-11-02 — Result UI and Report Integration for Construction Decisions

- Wired sealed construction units, instances, modes, boolean build states, and unit-bearing scales into Results UI and a dedicated "Construction Decisions" Excel sheet (scales transmitted as JSON numbers / numeric cells, monetary values kept as decimal strings).
- Display and export distinguish unbuilt zero scale, mandatory zero scale, and positive construction without re-deriving build status from capacity thresholds or adding bespoke UI forms, second financial engines, or duplicate runtime validation.
- Strengthened real HiGHS numeric assertions under zero construction for payments, annual fees, depreciation, and explicit recoveries alongside planning save/reopen, frozen task isolation, and package round-trips; kept existing heat-pump/positive scenarios while adding mandatory-zero branches in browser tests.

### 2026-11-02 — Eliminating Duplicate Work in Pre-Solve Bound Analysis

- Final matrices now reuse ordinary affine rows already bound during analysis while complementarity retains exact lowering once real bounds exist; propagation reuses single-row interval sums and suppresses redundant self-tightening revisits while preserving time-varying coefficients, state couplings, outward rounding, and provenance.
- Wired relation binding, indexing, and propagation into cooperative stop signals; retained proven bounds upon budget exhaustion, simplified fixed-residual branches first, and distinguished upper vs. lower missing sides in bound diagnostics.
- On a public 1-year synthetic model (same image, 2 CPU / 2 GiB AB/BA modeling trials), median generation dropped `15.01 → 10.92 s` with byte-identical MPS/parameters and flat peak memory (excluding solve time, without claiming universal speedups). Passed 62 template/provenance/propagation/activation tests and 79 consumer tests plus 2-year multi-instance matrix checks in Docker.

### 2026-11-01 — Closing P2 Capability Gates for Construction and Amount Sources

- Built-in generators declare capabilities for construction activation, cost occurrence conditions, and scoped amount sources; task submission checks declared relations and same-solve financial fact recovery against the selected descriptor, blocking missing capabilities with field-attributed `TASK-REQ-007` without creating tasks or snapshots.
- Empty construction units, `always` occurrences, and idle sources lacking matching new-costed instances require no extra capability flags; avoided guessing from generator names or re-validating/pre-solving bounds at submission or in Workers. Passed 127 targeted Docker task/finance/P2/architecture regressions matching CI images.

### 2026-11-01 — Standardizing on the Single Current Contract Rule

- User ruling: prior to public `2.0`, all proprietary contracts stay uniformly pinned at `1.0.0` maintaining only the current structure and semantics (`1.0` reserved for limited pilot testing without legacy compatibility or auto-migration systems; version numbers in older changelog entries reflect historical facts).
- Merged PR #38 per user directive (noting one backend CI lane remained failing at merge time and does not claim full P2 acceptance).
- Aligned device, data, assembly, finance, computation, snapshot, result, and project-package contracts alongside built-in generator/adapter refs, public samples, frontend producers, and tests—leaving one active assembly schema without major-version dispatch or version ranges while keeping third-party software versions, product metadata, and content revisions intact.
- Passed 475 main-group Docker tests (5 skipped), 36 registration/Bundle/boundary tests, frontend build across 19 logic files, and official manual link/example checks without re-running full E2E or engineering cases.

### 2026-11-01 — Effective Target Contracts for Construction Activation and Asset Sources

- Specified explicit construction units, active scale intervals, instance cost occurrence conditions, and initial construction source scopes: fixed fees do not vanish implicitly with optional switches, nor do sibling instances share or borrow the first instance's state; fixed and ratio residuals coexist cleanly without order-, scale-, or epsilon-based heuristics.
- Published target spec text and Roadmap updates without modifying runtime code or deployments at that step.

### 2026-10-31 — P1-S3 Template Reuse and Direct Row Generation Closeout

- Merged PR #36: device equations and step planning constraints now reuse compiled expression structures within a modeling pass, binding per-step coefficients and independent decision columns directly into final constraints while eliminating redundant local-to-global row copies; public lowering and project modeling share one evaluation kernel preserving initial values, cross-block/cross-year states, complementarity, and same-solve outputs.
- Cloud CI on `2c83be6` passed 2,427 backend checks, 13 Chromium specs, and frontend logic (skipped/cold-start/PG-init/engineering runs excluded); independent Docker runs passed 96 checks.
- On identical 2-year synthetic models, direct row emission cut median modeling time by `~14.8%` vs. prior S3 (`~11.4%` vs. frozen S2 reference on 1-year full generation; matrix sizes unchanged, excluding solve time). Closed S1–S3 leaving provenance persistence and integration acceptance open.

### 2026-10-31 — P1-S2 Full Relation Collection and Unified Analysis Closeout

- Unified collection of technical, network, planning, and existing financial relations ahead of conditional lowering: cost drivers, initial values, depreciation bases, monthly VAT links, and annual income/loss vintages form inspectable bindings shared identically by modeling and same-solve output declarations.
- Complementarity, monthly demand, VAT, and income tax share the unified bound-query API; fixed initialization and symbol-index links for standalone cost declarations, adding project-model consumers to targeted tests while preserving all existing physical, monetary, timing, and tax semantics without new public inputs.
- Cloud CI on PR #35 (`47e3f5c`) passed 204 backend checks, 13 Chromium specs, and frontend logic alongside 138 independent Docker tests spanning prior failing paths, linear lowering, monthly demand, and taxed real solves.

### 2026-10-31 — P1-S1 Modeling Declarations, Internal Provenance, and Baseline Measurements

- Merged PR #34: declares planning symbols upfront so static bindings across all instances resolve prior to expanding columns and technical relations, preserving original ASTs, internal provenance identities, and planning-controlled `initial` links.
- Established modeling, MPS, and metadata measurement baselines on public synthetic samples defining trace overhead and cumulative RSS metrics; migrated battery planning tests to the new stage entry retaining all physical and real-solve assertions. Cloud CI (`15ebd05`: 177 backend, 13 Chromium, frontend logic) and 79 independent Docker checks passed.

### 2026-10-31 — General Modeling & Computation Upgrade P0 Semantic Closeout (Guidance Only)

- Clarified separation of duties across modeling, domain layers, generators, solvers, and result adaptation, unifying model valid domains, project bounds, and derived bounds while distinguishing invalid inputs, insufficient lowering capabilities, mathematical infeasibility/unboundedness, and resource exhaustion.
- Converged authoritative target semantics for port networks, construction activation, amount identities/references, evaluation baselines, and equivalent optimization without overstating future schema fields as shipped (`architecture.md` bumped to `1.3.0`; Constitution, product version, and live schemas unchanged).
- Document-only check verifying semantics, links, navigation, and structure; P1–P5 remained in Roadmap and `0.15.0` incomplete.

### 2026-10-30 — Full YAML Editing and Step Constraint User Chain Closeout

- Added the Full YAML document view in Planning config sharing save, revision, and optimistic conflict recovery with the structured form; step port references, disabled constraints, and exact decimal strings survive view switches, saves, and reloads without auto-renaming blank/duplicate draft rows while retaining full backend validation on save.
- Targeted cloud CI on `d76f1fd` passed 19 frontend logic files and 31 Chromium tests across two lanes including real task runs and HiGHS same-solve verification; independent review ran Docker builds, 20 doc checks, negative browser repros, and a real solve regression while syncing User Guide and frontend docs.

### 2026-10-30 — Step Technical Constraints Same-Solve Execution and Dimensional Fix

- Merged PR #32: built-in generator declares `technical.step_constraints`, compiling and expanding enabled step constraints across the operating horizon using the exact port variables shared with devices, buses, and result series while keeping planning capacities scalar and skipping disabled constraints.
- Fixed public expression dimension handling so `power × time` reconciles cleanly with energy dimensions (`energy = power × time`) across multiplication, division, and integer powers while continuing to reject true dimensional mismatches without device special cases.
- Verified across 93 pre-merge Docker tests, exact-commit CI, and 390 post-merge checks plus 125 dimensional identities covering real HiGHS, NPV objectives, snapshot freezing, and result sealing proving energy constants and equivalent `power × step_duration` emit identical math models.

### 2026-10-29 — Step Technical Constraints Input and Freezing Closeout

- Merged PR #31: `step_relation` supports immutable local port references, affine expressions, `enabled` flags, planning save/readback, canonical assembly, project-package round-trips, and task freezing while strictly checking references, indices, dimensions, and prohibiting `step_duration` variable-name collisions.
- Aligned assembly/validator/canonicalizer at `5.0.0` and built-in generator at `7.0.0` (retaining scalar planning solves while explicitly rejecting enabled step constraints with `TASK-REQ-007` prior to `technical.step_constraints` delivery in PR #32). Passed 253 local Docker tests, 5 counterexamples, and cloud CI (`4f67c51`: 1,039 backend, 96 Chromium, frontend logic, CI Gate).

### 2026-10-29 — Cross-Instance Step Technical Constraint Contract

- Established the `step_relation` target contract under `planning.constraints` specifying explicit port references, local aliases, and single-step affine relations reusing device variables, planning capacities, and operating horizons without conflating port caps with source tracking, altering bus conservation, or adding bespoke forms. Pure specification commit prior to PR #31/#32 implementation.

### 2026-10-29 — Generic Financial Input and Same-Solve Execution Closeout

- Merged PRs #29 and #30 unifying the finance triplet at `6.0.0`: `annual_fixed_om` supports fixed annual fees plus scale-linked terms, initial construction and designated maintenance expenses use explicit amount sources, residuals support fixed monetary amounts or ratios, and extra after-tax net items declare explicitly via `net_cashflows`.
- Component/sub-item VAT, income-tax deductions, initial/renewal depreciation, and net cash items participate in the same optimization (`generator`/`adapter` `6.0.0`, sealed evaluation `3.0.0`), rendering same-solve amounts, net-cash timings, and depreciation provenance across UI and Excel without currency conversion systems.
- Passed 200 S1 isolated Docker tests, 70 pre-S2 tax/recovery/export tests, 8 final execution tests, cloud CI across both PRs, and browser verification covering non-empty `net_cashflows` and renewal depreciation end-to-end.

### 2026-10-27 — Immediate Evaluation Years Display and In-Flight Upload Guard

- Displays derived `E = K + N` immediately once construction and operating years are valid rather than waiting for discount rate, payments, and maintenance fields while still validating full confirmation payloads on submit.
- Disables dataset uploads and sample generation while quick-binding or project state refreshes are in flight, restoring controls afterward so edits in the upload dialog are not wiped by background binding refreshes. Passed Docker task-mapper tests, TypeScript/Vite build, and targeted local Playwright checks.

### 2026-10-27 — Canvas Sequential Edit Preservation and Task Input Feedback Fixes

- Canvas property updates submit sequentially by explicit field intent preserving subsequent edits, renames, and connections, and synchronize unedited parameters/names/sources when closing and reopening the inspector so stale responses never show old values or revert saved edits on blur.
- Added inline error messages, accessible descriptions, and field targeting to task evaluation inputs with explicit JSON formatting examples explaining construction-start offset `t` without adding defaults or relaxing backend rules. Passed 15 Chromium specs, TypeScript, Vite, and 47-chapter Help Center generation in isolated Docker.

### 2026-10-27 — Decoupling Technical Flow Direction from Tariff Accounting Direction

- Unidirectional metering interfaces' physical direction (`in`/`out`) and `energy_prices` accounting direction (`purchase`/`sale`) declare independently, enabling explicit source-side electricity sales while retaining carrier, unit, non-negativity, and bidirectional rejection rules without auto-netting trades.
- Non-negative metering validation inspects effective lower bounds after planning takeover rather than mis-rejecting replaced instance defaults while checking static fixed values and aligning demand metering under the same bound semantics. Cloud CI (235 backend, 7 Chromium, 1 frontend logic file) and 37 independent Docker tests (including taxed export cash-flow sealing) passed.

### 2026-10-26 — Config Capability Feedback, Binding Sync, and Cooperative Stop Lifecycle

- Calculation config save validates mode, precision, solver, outputs, and options against the chosen generator's public descriptor, surfacing field-targeted errors and supported options while keeping edits without bumping revision or pre-building models.
- Dataset bind/unbind actions consistently re-read project draft state across lists and version dialogs, clearly flagging write-succeeded/read-failed cases with read-only retry rather than re-submitting mutations.
- Cancellation signals propagate past authoritative state commits; built-in HiGHS stops cooperatively during generation and reaps resources cleanly once solver processes exit, keeping cancellation, timeout, and lease fencing outcomes distinct without masking timeouts as generic failures. Merged PRs #26 and #25 after passing cloud CI, 55+91+36 independent Docker tests, and 47-chapter Help Center builds.

### 2026-10-26 — Complementarity Tightening for Full-Year PV-Storage Solves

- Swapped operand order in grid purchase/sale complementarity products, flipping only internal Big-M binary indicator polarity while keeping feasible regions, objectives, port directions, assemblies, capacities, horizons, precision, and 600 s budgets completely intact.
- Removed redundant equality bound propagations that yielded no solve gains (along with their one-off tests/probes) while keeping battery energy recurrence, SOC bounds, and generic complementarity intact.
- Independent Docker runs collapsed root-LP fractional grid binaries from 8,759 to 0 and proved optimality `850051.6 CNY` at node 1 in `~2.46 s` solver time; all 4 full-year cases (6 real solves) and 43 battery/complementarity tests passed in `265.98 s` total alongside 290 targeted Docker regressions.

### 2026-10-26 — Battery Capacity and Power Planning Model Closeout

- Switched built-in battery storage to energy-state recurrence whose initial energy and SOC bounds share references to planning capacity rather than dividing by unknown capacity; fixed-parameter and capacity-planned runs share one declarative model, handling zero capacity/power via generic complementarity lowering without injecting default durations, capacity caps, or terminal SOC constraints.
- Added `initial.expression` (`2.2.0` draft numbering historically; unified at `1.0.0` later) and `initial_ast`, updating catalog, templates, editors, and tests; passed real HiGHS capacity/energy/unit/capex/maintenance tests, cloud CI, and 104 independent Docker checks.

### 2026-10-26 — Legitimate Zero Upper Bound Complementarity Elimination Fix

- Generic complementarity lowering now eliminates trivially satisfied products whenever effective bounds prove either factor identically zero instead of rejecting valid zero-export cases or generating superfluous binary switches, while preserving non-degenerate complementarity requirements on finite upper bounds without guessing epsilons. Passed 31 independent Docker checks plus cloud CI (99 backend, 2 browser, 1 logic file).

### 2026-10-25 — Monthly Daily Price Curve Pipeline Delivery

- Delivered calendar-month daily price curve assignments (`monthly_daily`) reusing shared price objects across years along actual Gregorian calendars (reusing month 2 for Feb 29) while preserving `constant` and full-horizon `time_series` pricing.
- Frozen source/month maps flow through the same point-by-point billing, tax, and cash-flow pipeline without building a second pricing engine; Finance UI adds monthly editing, batch-fill, readback, and error recovery alongside updated User/Developer docs. Cloud CI (630 backend, 107 Chromium, 6 logic files; 3 skipped) and independent real-solve cash-flow tests passed.

### 2026-10-25 — Scale-Linked Annual Maintenance Pipeline Delivery

- Delivered ISSUE-06 end-to-end input, freezing, same-solve recovery, and UI/export integration: maintenance schedules combine fixed amounts, scale unit costs, and project upfront-capex shares while retaining evaluation year, currency, and per-item tax identities.
- Maintenance cash, VAT, income tax, and same-solve evaluation share one frozen affine expression; Users edit/save/reload linked items cleanly while historical tasks stay immutable. Verified across 54 backend Docker tests, 23 cloud Chromium tests, and 3 frontend logic files.

### 2026-10-25 — Capacity-Normalized Electric Output Curve Modeling

- Added standalone `Capacity-normalized Electric Output` device model (`ies.device.power_curve`) multiplying installed capacity (kW) by dimensionless output curve `unit_output` per step, supporting both fixed capacities and planning takeover alongside weather-driven PV while displaying series units in the inspector. Passed 105 backend + 11 browser CI tests and 31 independent catalog/solve checks recovering `100 kW × 0.25 = 25 kW`.

### 2026-10-25 — Monthly Daily Price Contract Specification (Guidance Only, Pending Implementation)

- Specified `monthly_daily` pricing across 12 full-day curves expanded deterministically by Worker along actual Gregorian dates while keeping CSVs on `1.0.0`. Pure documentation revision ahead of implementation delivered later on 2026-10-25.

### 2026-10-25 — Scale-Linked Annual Maintenance Contract Specification (Guidance Only, Pending Implementation)

- Specified fixed amounts plus scale/upfront-capex linear terms sharing one affine expression across cash, VAT, income tax, and same-solve evaluation without introducing a separate maintenance engine. Pure specification revision ahead of implementation delivered later on 2026-10-25.

### 2026-10-25 — `0.15.0` Calendar-Month Demand Billing Chain Integrated

- Merged D1, D2, and D3 via PRs #4, #9, and #10: standalone demand charge catalogs, rate overrides, and meter bindings freeze with tasks so real calendar-month peaks and demand bills participate in full-horizon optimization, taxation, annual cash flows, and monthly bills from the same solve.
- Finance UI edits demand catalogs/rates and Bindings UI declares demand meter links (empty when unused, never guessing meters); Results UI and Excel distinguish unbound vs. zero-rate charges using task snapshots rather than live drafts while protecting unsubmitted edits during saves and refreshes.
- D3 final Docker CI passed 77 backend checks (5 skipped) and 104 Chromium checks (3 skipped), plus D2 2-year heat-pump real solves and monthly bill reconciliations.

### 2026-10-24 — Frontend Basic Operations Regression Entry

- Added `test:basic` combining auth, project navigation, canvas wiring, upload, calculation config save/reload, and Help Center chapter navigation & reload checks, asserting binding ID sets and parsed JSON values rather than list sorting or formatting without substituting for specialized financial, solve, export, or conflict suites.

### 2026-10-24 — Separating Browser Exploration from Targeted Regression (Guidance Only)

- Defined the split where agents explore new GUI interactions to codify Playwright specs sharing scripts and assertions across local and cloud CI while scoped commits run impacted regressions without re-exploring or running full suites blindly every turn. Pure guidance sync.

### 2026-10-24 — Calendar-Month Demand Billing Contract Finalized (Guidance Only, Pending Implementation)

- Finalized standalone demand charge catalog and explicit meter binding contracts distinct from volumetric energy tariffs, covering calendar-month peaks, same-solve participation, rate overrides, explicit tax scope, and same-source monthly/annual outputs.

### 2026-10-24 — `0.15.0` Device Cost and Tariff Binding UI Integrated (Browser Acceptance Pending)

- Added explicit instance cost participation (`none`/`costed`), driver-to-property/interface mappings, and energy tariff bindings saved atomically in one draft revision with conflict protection, orphan-binding cleanup, and empty-list support. Merged PR #2 with TypeScript/Vite Docker build passing; full Playwright run deferred until UI wave completion.

### 2026-10-23 — `0.15.0` Planning Config Editing UI Integrated (Browser Acceptance Pending)

- Added standalone Planning config workbench screen editing objective, device decision variables, bounds, and named constraints via existing planning revision endpoints and optimistic conflict handling without preset defaults. Merged PR #1 with TypeScript/Vite Docker build passing.

### 2026-10-23 — `0.15.0` S1 Template Data Source Chain Closeout (Integrated, Pending Independent Re-Verify)

- Aligned device models and templates to pure technical contracts; template data inputs materialize into explicit instance sources at instantiation time, after which instance parameters remain authoritative and project package imports remap instance dataset version references.
- Inspector edits `constant`, `data_repeat`, `data_full`, and `data_predict` sources with visible failure banners; renaming instances preserves parameters, edges, and layout without reapplying template inputs. Passed 45 targeted backend tests, 8 S1 tests, 4 Docker Chromium specs, and frontend build (`7427e32` merged).

### 2026-10-23 — `0.15.0` S0 Finance Entry Fix Integrated

- Aligned finance profile list/register endpoints to public routes and preserved reverse-proxy request ports to prevent redirect failures on non-standard ports; removed stale preflight blocking on legacy finance benchmark audits while preserving authoritative evaluation confirmation at task submission (`80475c1` merged; 137 backend and 2 Playwright specs passed).

### 2026-10-22 — `0.14.5` Tax User Chain and Exit Audit Complete (Integrated)

- Completed and merged A–D into mainline `d5ad97f`: separated prediction paths by solve dependency, pinned explicit tax rules, compiled after-tax objectives, taxes, and full cash flows in the same solve via generator/adapter, and wired UI and Excel to consume the same sealed facts while dropping silent legacy fallbacks.
- Wired Finance UI profile registration/selection, project overrides, and effective inspection preserving edits on failure; updated User Guide and Results docs removing outdated "after-tax planning not yet available" statements.
- Final isolated Docker Playwright passed 10/10 covering non-zero VAT/income tax, refund confirmation/arrival, independent hand-calculated after-tax NPV, cell-level Excel reconciliation, historical task isolation, export guards, and save errors alongside 16 source-tree Docker export/display/package checks. Kept explicit generator exclusions (unbounded load-metered ports, certain variable positive residuals, positive fixed fees under optional construction) and single-API-process DB init notes disclosed. Closed `0.14.5` milestone and set `0.15.0` next.

### 2026-10-21 — `0.14.5` C3 Tax Submission Loop Reviewed (Pending Integration)

- Reviewed execution branch `547d5e7`: submission validates tax-rule/credit-pool support against the selected generator's capability declarations instead of rejecting all non-empty rules; unsupported items continue returning `007` while preserving cross-input diagnostics. HTTP submissions with tax rules sealed same-task financial evaluation via real HiGHS and production adapter.
- Independent Docker checks cleared each of the five financial capability flags individually returning `007`, while permitting matching capabilities under another generator name, reusing 35 task/Worker and 54 finance/reference checks (zero-transaction HTTP chain; non-zero tax math validated via C2). Entered Phase D user chain and final Docker Playwright; `0.14.5` incomplete pending integration.

### 2026-10-21 — `0.14.5` C2 Same-Solve Financial Output Reviewed (Pending Integration)

- Reviewed execution branches `e0749fb`, `40c6380`, and `049d7ef`: generator `3.4.0` and companion adapter `3.0.0` wired same-solve financial facts and versioned sealed evaluations so `application` no longer rebuilds cash flows; public fields and decimal string amounts follow official contracts while removing dual tracks and redundant internal checks.
- Independent Docker real solves verified closing VAT credit carry-forward accurately deducts confirmed refund amounts in refund confirmation months with decimal string outputs. Non-empty tax-rule submission gating, user chain, and final browser acceptance remained open.

### 2026-10-21 — Same-Solve Financial Output Contract Finalized (Guidance Only)

- Established the single source-of-truth chain across generator declarations, adapter recovery, and sealed evaluations so full after-tax cash flows and tax schedules come solely from the same-solve model and primal solution rather than `application` re-computation.
- Specified target companion generator/adapter versions (`3.4.0`/`3.0.0`) and versioned sealed evaluation blocks while keeping Bundle, `ComputeResult` top-level, snapshot, and calculation config schemas unchanged. Pure guidance ahead of C2 implementation.

### 2026-10-21 — `0.14.5` C1d Income Tax Same-Solve Optimization Reviewed (Pending Integration)

- Reviewed branches `03763b8`, `6f167c1`, `b2876c6`, and `8cfc300`: generator `3.3.0` embedded income tax, straight-line depreciation, and exact loss carry-forwards into the same solve; tax-inclusive and tax-exclusive equivalent inputs yield identical tax bases and after-tax NPV, and unformed assets never create phantom income from residual values.
- Independent Docker counterexamples confirmed zero-construction residual scenarios recover from erroneous `-4.1279` back to `0` NPV alongside 37 targeted income-tax/VAT/NPV checks; supports valid positive static residuals while rejecting variable asset allocations lacking valid bounds under fixed positive residuals without forcing construction.

### 2026-10-21 — `0.14.5` C1c VAT Modeling Reviewed (Pending Integration)

- Reviewed branches `e0e2835`, `6e7563a`, and `5a93aa3`: generator `3.2.0` incorporated monthly VAT, shared credit pools, and explicit refunds using tax-inclusive cash receipts/payments, invariant maintenance tax splitting across `price_basis`, and incremental existing-device deduction boundaries. Reused 10 VAT, 29 NPV/adapter, and 6 submission-rejection Docker tests ahead of income tax and output wiring.

### 2026-10-21 — `0.14.5` C1b Full-Horizon NPV Objective Reviewed (Pending Integration)

- Reviewed branches `d472cd9` and `b7623d0`: built explicit NPV objectives over construction payments, annual operations, and occurrence-year maintenance (bumping generator to `3.1.0`, adapter `2.0.0`, retaining non-empty tax/pool submission rejections), verified across 12 new-objective and 58 adjacent Docker tests. Synced generator ref examples and monthly input-VAT / cash-year mappings for upcoming C1c VAT work.

### 2026-10-21 — `0.14.5` C1a Evaluation Input Wiring Reviewed (Pending Integration)

- Reviewed branches `6d3e79d` and `2a32f97`: moved evaluation value objects under finance's public facade and forwarded frozen evaluation confirmations directly to generators and each prediction iteration, deriving full operating horizons from confirmed years and bumping generator refs to `3.0.0`. Reused 28 project-model, 78 targeted, 77 evaluation-parsing, and 3 tax-rejection Docker tests while aligning docs for explicit NPV objectives.

### 2026-10-20 — Direct Transmission of Evaluation Confirmation (User-Approved Guidance Amendment)

- Bumped Constitution to `1.16.0`: generators receive frozen evaluation confirmations directly as an independent input keeping operating step series separate from payment/maintenance schedules, removing the rigid "five-input" cap without adding user fields or outer wrapper objects. Synced architecture, contracts, and module guides; target generator bumped to `3.0.0` ahead of C1a code.

### 2026-10-20 — Aligning Generator Tax Expression Boundaries (Guidance Only)

- Removed the legacy statement in `generators.md` restricting internal binary variables solely to complementarity, aligning with exact piecewise relations and cross-period balances required by finance rules while deriving bounds from parameters and variables without widening general nonlinear solving. Pure guidance clarification.

### 2026-10-20 — Finance Triplet Tax Contract Finalized (Guidance Only)

- Unified target finance formats at `3.0.0`, separating calculation rules from display tax labels and specifying shared credit pools, project parameter overrides, construction confirmation schedules, per-item maintenance flags, and independent income-tax/VAT handling while preserving operating expenses and input-VAT tax bases. Pure guidance ahead of B1 implementation.

### 2026-10-20 — `0.14.5` B0 Pure Financial Calculation Foundation (Code Reviewed & Integrated)

- Integrated `302fa9b` and `153d386`: published pure stateless functions and immutable outputs for tax splitting, single-period VAT credit/refund netting, tax loss utilization/expiry, income tax, and straight-line depreciation preserving transaction signs and decimal precision while rejecting excess refunds loudly without redundant internal checks. Verified via 69 Docker tests and static gates ahead of tax schemas and generator wiring.

### 2026-10-20 — Clarification of Pure Tax Calculation Semantics (Guidance Only)

- Clarified that price splitting preserves transaction signs and declared refunds MUST NOT be silently clamped on insufficient pool credit; B0 remained under closeout review without claiming tax optimization shipped.

### 2026-10-20 — Pure Tax Calculation Boundaries Confirmed (Contract Guidance Only)

- Confirmed pure calculation semantics in `finance` for price splitting, VAT credit balances, loss utilization/expiry, depreciation, and income tax with zero display rounding, explicit inputs, and same-solve responsibility ahead of schema extensions.

### 2026-10-20 — `0.14.5` A2 Prediction Dependency Dispatch (Code Reviewed & Integrated)

- Integrated `4a83c2d` through `eb9d37e`: unified calculation config at `2.0.0`, resolving and validating predictor parameters at save boundaries, dispatching standalone predictions once vs. coupled predictions iteratively with algorithm-supplied dimensionless convergence metrics, and removing legacy count-based Stage-2 routing and missing-catalog fallbacks. Verified across 34 prediction and 13 final boundary Docker tests covering round-7 early convergence, max-iteration bounds, mixed standalone/coupled targets, multi-round transaction rollbacks, and empty-catalog gates.

### 2026-10-20 — Coupled Prediction Convergence Interface Confirmed (Contract Guidance Only)

- Specified dimensionless convergence metrics returned by coupled predictors across consecutive iterations sharing one `tolerance` threshold while preserving per-target evidence, input parameter validation, exact version pinning, and standalone reuse. Pure guidance ahead of A2.

### 2026-10-20 — `0.14.5` A1 Prediction Dependency Declaration (Reviewed)

- Merged `2d2df96`: added required boolean `depends_on_solve_result` to predictor descriptors (`naive` explicitly standalone, synthetic coupled provider explicitly dependent) preserved across catalog snapshots; passed 12 targeted prediction tests, 3-file Ruff, and 11 horizon test collections in Docker ahead of A2 dispatch cutover.

### 2026-10-19 — Confirming Public Prediction Dependency Declaration (Contract Guidance Only)

- Specified required `depends_on_solve_result` on predictor descriptors distinguishing standalone vs. coupled providers (passing `None` on round 1 and read-only `ComputeResult.outputs` thereafter). Pure guidance ahead of A1.

### 2026-10-19 — Establishing `0.14.5` Development Plan (Plan & Docs Only)

- Promoted user-approved embedded tax computation and prediction dependency dispatch from `0.15.0` into dedicated milestone `0.14.5` structured across four items (prediction, tax inputs, after-tax solving, and user chain) ahead of `0.15.0` Provider extension acceptance; kept `0.14.0` as latest completed milestone and `0.1.0` product version.

### 2026-10-19 — Correcting Prediction Dependency Classification (Docs Only)

- Restored standalone vs. coupled prediction workflows governed by solve dependency rather than target count, correcting the prior guidance removal of outer loops and `convergence` inside CalculationConfig `2.0.0` while syncing architecture, domain, application, generator, and Roadmap docs without modifying code or schemas.

### 2026-10-19 — Finance Boundaries and Prediction Simplification Sync (Docs Only)

- Updated architecture and module maps for finance rules entering generators, unified finance guidelines on project cash flows and core metrics (removing outdated claims restricting depreciation to post-evaluation or mixing equity/LCOE into current scope), and archived legacy F1–F6 financial implementation plans and first-round reviews.

### 2026-10-19 — Tightening Tax Guidance Wording (Docs Only)

- Converted tax objectives, relation classes, VAT cash receipts/payments, and module boundaries into direct imperative rules by removing repetitive disclaimers and rejected alternatives without modifying rulings, formats, or code.

### 2026-10-19 — Embedded Tax Computation Guidance Revision (Docs Only)

- Updated Constitution §4.6 alongside finance, generator, and computation boundaries per user ruling adopting same-solve embedded tax computation (`NPV_after_tax`, pre-mergeable coefficients, intra-period relations, cross-period balances, tax-inclusive VAT receipts/credits), keeping known-tax-rate products linear without reviving withdrawn generic finance languages or claiming after-tax solving shipped.

### 2026-10-18 — Project Model and Project Package Mainline Integration Closeout (Reviewed)

- Merged project-model consumption and project-package model dependency slices while retaining CSV source-declaration preflight checks and independent curve display fixes, aligning two preflight test callers to pass project model document collections explicitly without compatibility branches or internal re-checks. Passed 80 tests across four files in Docker and verified diffs via Codex review.

### 2026-10-18 — CSV Source Declaration Preflight and GUI Submit Fix (Reviewed)

- Reused public pure source-declaration parsing in preflight and assembly so valid data-series bindings no longer misreport `PARAM-UNIT-002` unit errors while preserving rejection of malformed declarations, unknown parameters, and constant range checks without internal re-verification. Verified end-to-end external CSV upload, GUI Task Center submit, and Worker solve alongside 50 Docker backend tests and Codex review.

### 2026-10-18 — Independent Step-Curve Display Fix (Reviewed)

- Stopped hiding existing computation step series on Results when metric summaries are legitimately absent; loading, legitimate absence, read failure, and explicit retry follow curve state independently, clearing stale curves on task switches without mutating solve payloads or forging metric summaries. Passed 4 Docker browser specs across real solves, curve identities/units/steps, failure retries, slow reads, absence, and task switches under Codex review.

### 2026-10-18 — `0.15.0` Project Packages Carry and Remap Saved Project Models (Reviewed)

- Upgraded project package format `2.0.0` → `3.0.0`: added `files.models` carrying exact canonical texts for all project models referenced by the current draft and all packaged historical versions (sorted by source suffix); confirmed imports revalidate each packaged model through the official model save gate under `base_device_id` to allocate fresh `_N` suffixes and row IDs while remapping draft and version references instead of reusing source project rows across projects.
- Strictly enforces exact equality between packaged models and referenced models (rejecting missing or extra unreferenced models via `params.reasons`, and rejecting exports with dangling refs); proposal stage checks structure and skeleton keys (`model`, `devices`, `connections`, `project_models`, `files.models`), while confirmation performs authoritative validation once with atomic rollback (`direct_yaml` provenance). Clarified and regression-tested save/delete catalog rebuild semantics after version restore.
- Passed 25 red-to-green backend regression cases, 139 tests across 10 adjacent files (5 pre-existing skips), and 7 real Playwright package import specs including cloned-project canvas consumption of its own remapped model; closed package model dependency Roadmap item without claiming full `0.15.0` completion.

### 2026-10-18 — `0.15.0` Project Saved Model Consumption Chain (Reviewed)

- Fixed the disconnect where saved project models appeared in the catalog yet failed to enter the canvas: palette selectors, modeling commands, assembly gates, and task snapshots resolve models strictly by exact references declared in the current draft or selected version rather than reading the raw catalog table directly, preventing post-restore leakage and cross-project references without introducing hot-reloading or duplicate registries.
- Passed 4 red-to-green model consumption tests, 366 tests across 14 adjacent files, and two clean-DB real-browser full-chain runs reconciled against hand-calculated hourly flows, tariffs, O&M, capex, and sealed objectives.

### 2026-10-18 — `0.15.0` First External Device YAML Consumption Chain Verified (Reviewed)

- Device candidate ingestion rejects duplicate stable IDs across files with two-file diagnostics while retaining atomic candidate catalog publication without fallbacks or internal digest checks.
- Mounted external sample `constant_load.yaml` read-only via `docker-compose.ext-device.yml` and verified end-to-end GUI palette selection, canvas drag-and-drop/params/wiring, assembly submission, real Worker execution proving UI-entered 40 kW active in step series, isolated under `playwright.ext-device.config.ts` alongside 91 backend catalog/adjacency tests (`device-yaml` `2.0.0` path verified ahead of `2.1.0` alignment).

### 2026-10-18 — `0.14.0` Final Review Complete

- Completed review of strict frontend contract and feature boundaries: auth, tasks/config, projects & dependency families, templates, results, and exports consume public entry points and real DTOs; removed speculative response guessing, fabricated resources, business caches, and error-to-empty-data fallbacks while preserving genuine absent states and user input validation.
- Completed exact browser verification of financial pages across real infeasible/unrecovered and positive heat-sale cases, reconciling NPV, IRR, static/dynamic payback values, units, statuses, bases, and annual cash flows against sealed task facts (`~2.358 yr` and `~2.580 yr` payback on the positive case) alongside existing cell-level Excel reconciliation.
- Aligned legacy IRR specs with current contracts (retaining real status on sealed cards without metric summary, never fabricating status without financial blocks or mixing incompatible solver metrics) via reviewed commits `4e2b684` and `8995b04`.
- Reused risk-scoped verification across browser financial/absence specs, IRR specs, and prior login-to-export, versioning/restore, desktop/mobile, bilingual, and Help Center checks. Closed `0.14.0` milestone cleanly with `0.15.0` Provider extension acceptance next.

### 2026-10-17 — `0.14.0` Account Namespace Creation Boundary Closeout (Reviewed)

- Sign-up, OIDC, built-in admin seeding, and default operator creation reuse `identity`'s public namespace allocator at account creation time; template creation reads the existing account value without backfilling or mutating user records.
- Removed the late-allocation module and template forwarding layer along with global rollbacks, cross-module ORM imports, and `SAVEPOINT` retry paths, migrating tests to real account creation, idempotent seeding, and immutable user template tests. Passed 73 Docker regressions without auto-migrating legacy empty-namespace dev rows.

### 2026-10-17 — `0.14.0` Unit Errors and Import Proposal Slimming (Reviewed)

- Assembly unit conversion catches explicit unit business errors only while letting unexpected bugs surface; removed redundant defensive checks in planning diagnostics over trusted parameters and redundant integrity claims in project import proposals while preserving real ZIP/manifest validation and staged object info. Passed 29 assembly/planning expression tests and 1 import HTTP regression in Docker.

### 2026-10-17 — `0.14.0` Template Response Contracts and Online Validation Fix (Reviewed)

- Project modeling and existing custom template pages consume typed responses directly, removing fabricated version/revision/object/receipt/diagnostic defaults while retaining form validation, dynamic inputs, and real diagnostics.
- Fixed online raw YAML validation on custom template editors so unsaved drafts validate on the backend without persisting while retaining exact revision checks. Passed 22 template API tests and 107 bundled mapper assertions in Docker.

### 2026-10-17 — `0.14.0` Frontend Boundary Closeout for Projects, Draft Commands, and Dependency Families (Reviewed)

- Consolidated projects, project versions, project import, modeling, datasets, and config defaults into independent features consumed by pages and workbench via public entries, deleting legacy business duplicates and `api.tasks` from `api/client.ts`; draft semantic commands live in `features/projects`, `saveWorkingInputs` in `features/tasks`, and `applyScheme` in `features/project-versions`.
- Removed redundant draft revision/command pre-checks and fabricated device registry/dataset fields (`is_load`, `capabilities`, `model_method`, `stateful`, `version`), aligning strictly to backend `selector.py` (`schema_version`, real ports/properties) and `{ dataset }` envelopes, keeping only `in`/`out`/`bidirectional` ports as connectable handles.
- Docker verification passed `tsc -b && vite build` (302 modules), 234 assertions across 7 pure Node logic suites, and 31 real Playwright specs across 13 files (1 deferred test skipped) plus post-review 32-check projection tests and a real wiring/rename/reload spec.

### 2026-10-17 — `0.14.0` Auth and Admin Frontend Boundary Closeout (Reviewed)

- Migrated auth, account management, storage health, and audit log requests into dedicated features, deleting legacy aggregator delegates and cross-feature internal imports; kept only real backend user fields while preserving active/disabled/locked states and bilingual feedback.
- Added explicit sign-out failure handling keeping the session active until retry succeeds. Passed Docker build, 60 auth/admin + 16 evidence + 16 envelope checks, 14 existing browser checks, and a new isolated Chromium sign-out failure/retry spec.

### 2026-10-16 — `0.14.0` Tasks, Calc Config, and Validation Frontend Boundary Closeout (Reviewed)

- Consolidated tasks, calculation config, and validation requests/DTOs/pure mappers into dedicated features, preserving nullable idempotency keys, attempt numbers, timestamps, and explicit errors while removing duplicate config checks and internal evidence guards; shared working-input saving stayed on the draft command channel. Passed Docker build, 68 mapper checks, 2 × 16 targeted tests, and focused browser re-runs.

### 2026-10-16 — `0.14.0` Results and Export Contract/Dependency Boundary Closeout

- Following task-identity alignment for results/exports, placed result, assessment, candidate selection, hourly, and export requests inside `features` while extracting common HTTP/session/download helpers to `shared`, dropping legacy envelope unwrapping and circular HTTP/feature imports without adding caches or runtime validators.
- Removed guessed defaults for required IDs, dimensions, download tokens, and filenames; absent package IDs and metrics remain `null`, assessment list failures never collapse into empty lists, and financial evaluation/absence cards render independently of metric summaries. Passed 51 Docker checks, 11-module acyclic dependency verification, `tsc`/`vite` build, and Chromium bilingual Excel, explicit reassessment, assessment failure, financial absence, and auth specs.

### 2026-10-16 — `0.14.0` Explicit Task Identity for Results/Exports and Removal of Module Business Caches (Pending Review)

- Removed module-level business caches `pkgTaskCache` and `resultBodyCache`: `ExcelExportInput.task_id` is now passed explicitly by callers from the selected task/evidence so exports read assessments accurately without pre-warming `tasks.get`, and `hourly()` re-reads directly by task ID (`downloadSessions` stays as a transient download-handle registry). Verified via build, 18 checks in `export_no_implicit_assessment`, `r7`, `r4`, `financial-absence`, and `excel-export-lang`.

### 2026-10-16 — `0.14.0` Open Energy Carrier Identity Closeout (Reviewed)

- Codex reviewed `6d63314` → `d7f102d` confirming real `electricity` wiring/rename flows, open carrier identities, typed port-derived carriers, and verbatim display of unknown carriers. Switched `EnergyCarrier` to open strings driven directly by `ports[].energy_carrier` without fixed allowlist rejection (`water:out:<name>` allowed while keeping `in`/`out` direction rules) and verified via compiled Docker round-trips, real Chromium wiring/rename/reload tests, and `tsc+vite` build.

### 2026-10-16 — `0.14.0` Electricity Carrier Identity and Registry Palette Mapping Closeout (Pending Review)

- Aligned frontend `EnergyCarrier` from `electric` to authoritative `electricity` without compatibility aliases and updated registry mappers to read `names.{zh-CN,en-US}` and `ports[].energy_carrier` directly instead of nonexistent flat fields (`name_zh`/`name_en`/`energy_carriers`), fixing empty device panels, dropped electrical wiring gestures, and 400 errors on rename. Added `model-electricity-carrier.spec.ts` plus `model-load-state` (5) and `auth-navigation-model` Scenario 3.

### 2026-10-16 — `0.14.0` Workbench Project Summary Ownership Closeout (Reviewed)

- Codex reviewed `ef32fcd`: keyed `WorkbenchProvider` directly by `projectId` so switching `:id` remounts the entire workbench cleanly and drops stale in-flight summary/version/task reads from previous projects without leaking state across projects; intra-instance refreshes resolve latest-writer-wins while reporting failures honestly to overridden callers. Verified across `model-load-state.spec.ts` (5), `data-project-d3`, `data-versions-d2`, and `tsc+vite` build.

### 2026-10-16 — `0.14.0` Modeling Page Project Lifecycle Ownership Closeout (Reviewed)

- Codex reviewed `b9c2e42` → `0bbe404`: keyed the modeling canvas by `projectId` so switching projects cleanly resets in-flight flags, drafts, readiness, errors, and shared autosave isolation while discarding late unmount responses cleanly. Verified across 5 `model-load-state.spec.ts` scenarios, StrictMode dev smoke check, and `tsc+vite` build.

### 2026-10-16 — `0.14.0` Modeling Page Read/Refresh Ordering Closeout (Pending Review)

- Locked canvas edits (`palette`, node drags/wiring/selection, inspector, validate) during in-flight refreshes or unrecovered read failures with non-dismissible banners until recovery, disabled Refresh during active saves so stale reads never overwrite saves, and interlocked single-flight read callbacks with true loading states on Retry. Passed 4 `model-load-state.spec.ts` scenarios and `tsc+vite` build.

### 2026-10-16 — `0.14.0` Modeling Page Read-State Gate Closeout (Pending Review)

- Gated modeling edits behind an authoritative initial load + descriptor resolution: shows loading indicator while fetching, localized error + Retry on failure (never clearing to an editable empty graph or marking "Saved"), allows valid empty models only after real success, and preserves prior graphs with an error banner on failed refresh. Added 2 `model-load-state.spec.ts` specs alongside `tsc+vite` build.

### 2026-10-15 — `0.14.0` Dataset Read Contract and Pure Mapper Closeout (Reviewed)

- Established `features/datasets` as sole owner of `{datasets: [{dataset, latest_version}]}`, `{dataset, versions}`, and `{dataset_version, diagnostics}` DTOs and pure mappers (removing `asList` masking of malformed payloads while retaining legitimate empty arrays and `latest_version=null`), eliminated fallback `0`/`hourly`/`480` defaults, and aligned `DatasetFieldType` strictly to `'datetime' | 'float' | 'integer' | 'decimal'`. Passed 18 `dataset-version-mapping.test.mjs` assertions, Docker build, and D2/D3/D5 browser specs (`df58cda` → `03871ff`).

### 2026-10-15 — `0.14.0` Visible Route Chunk Load Failure (Reviewed)

- Shared `PageLoadError` across top-level and workbench nested routes so missing or failed chunk loads display localized errors and reload/retry controls rather than permanent loading spinners, keeping real Suspense loading indicators and clean 404 tracking without exposing source filenames. Verified across 4 `page-loading.spec.ts` scenarios and Docker build.

### 2026-10-15 — `0.14.0` Error Envelope and Message Closeout (Reviewed)

- Removed the producerless top-level `{diagnostics}` fallback (`VALIDATION-FAILED`) from `toApiError`, kept standard `{error}` parsing and `params.diagnostics`, routed Modeling errors through shared `errorMessage` (keeping 409 conflict handling), and mapped non-404 HTTP errors to registered `API-REQ-003` with proper status interpolation across locales. Passed 30 backend app/protocol checks, 16 frontend error/message assertions, build, and live 405 Chromium verification.

### 2026-10-15 — `0.14.0` PostgreSQL Manual Retry Closeout for Failed/Timed-Out/Cancelled Tasks (Reviewed)

- Manual retry strictly transitions `failed`/`timed_out`/`cancelled` tasks back to `queued` reusing the original snapshot and idempotency key (creating a new Attempt upon claim while clearing stale cancellation signals), whereas `completed` and non-terminal tasks reject with `409` across DB triggers, domain transition tables, and use cases. Verified with opt-in isolated-schema `test_task_retry_pg.py` against real PostgreSQL and 57 SQLite task state checks.

### 2026-10-15 — Loading Task Inputs into Project Draft / Binding Results to Selected Task (Reviewed)

- Standardized each project's editable workspace as its working draft: loading a calculation task copies its full frozen input set into the draft (creating new tasks only on explicit submission), while results, assessments, and exports track the chosen task (defaulting to latest completed without falling back across tasks, requiring assessment prior to export). Verified via full task-switch/edit/submit/concurrency/failure-retry Chromium specs and `tsc`/`vite` build.

### 2026-10-14 — Clarifying Project Draft vs. Task Input/Result Relationship in Documentation

- Removed leftover project-member wording and clarified draft loading vs. explicit task submission, task-bound results/assessments/exports, and pre-export assessment requirements in User Guide docs. Pure documentation update.

### 2026-10-14 — `0.14.0` Data Page Tri-State Project Context and Project-Level Timezone Removal D3 (Reviewed)

- Added `projectLoading`/`projectError` states to `WorkbenchProvider` so `DataPage` renders loading spinners until resolved and explicit error + `refreshDetailed` retry on failure (keeping independent D2 version errors intact); removed non-existent `Project.fixed_utc_offset_minutes` type/UI displays and dead UTC mismatch keys (`ies.workbench.utc`), leaving fixed UTC offsets strictly on dataset versions across GUI and User Guides. Verified via build and `data-project-d3.spec.ts`.

### 2026-10-13 — `0.14.0` Export Returning Local Download Sessions R9 (Pending Review)

- `api.exports.excel` and `api.exports.package` return local numeric download session IDs directly instead of fabricating `Report` objects (`report_type: 'pdf'`, etc.), removing dead `Report`/`ReportType`/`ReportStatus` types across all four page call sites while preserving `download()` short-lived token retrieval and existing backend behavior. Verified via build, Excel export specs, and R7 assertions.

### 2026-10-13 — `0.14.0` Removing Unused Body from Takeover Confirmation A2 (Pending Review)

- Removed ignored `{token}` payload from `POST /api/auth/confirm-takeover` and dead `loginResult` state (auth flows purely via Cookie/Bearer `AuthCtx`), correcting stale comments about credential rotation. Passed build and `takeover-no-body.spec.ts`.

### 2026-10-13 — `0.14.0` Removing Non-Semantic Sign-In "Remember Me" A1 (Pending Review)

- Removed the non-functional "Remember me" checkbox, component state, `LoginRequest.remember`, and bilingual i18n keys that the backend `LoginRequest` schema ignored completely. Verified via build and Scenario 1 of `auth-navigation-model.spec.ts`.

### 2026-10-13 — `0.14.0` Require Explicit Assessment Prior to Export R7 (Pending Review)

- Removed silent `POST …/result/assess` auto-creation from `resolveAssessmentId`, throwing explicit missing-assessment errors when empty and passing HTTP/network failures straight through; removed unsubstantiated `pkgAssessmentCache` so exports always inspect live assessment history, while guiding users on Results (via **Reassess**) and Exports (linking `?package=` cleanly). Verified with 14 `export_no_implicit_assessment.test.mjs` checks and 2 `export-assessment-r7.spec.ts` browser tests.

### 2026-10-13 — `0.14.0` Dataset Preview Explicit No-Row Capability D5 (Pending Review)

- Converged `api.datasets.sample` into `versionFields` returning real field names from version metadata without empty `rows`/`total_rows`/`rows_available` shells, updated the preview dialog to explain row-level previews are unavailable alongside field definitions, synced User Guide, and verified with `data-preview-d5.spec.ts`.

### 2026-10-13 — `0.14.0` Results Page IRR Status Driven by Sealed Fields R1/R2 (Pending Review)

- Removed fabricated `diagnostics: []`, `inferIrrStatusKey` guessing, and dead `resultDiag`/`DiagnosticLine` UI from `ResultsApi`/`ResultsPage`; mapped sealed `irr_status` via `irrStatusToKey` consistently across summary and sealed cards (hiding the row cleanly when financial evaluation is absent), passing 6 `result_view_no_synthetic_diagnostics.test.mjs` checks and 2 `result-irr-status-r1r2.spec.ts` browser specs.

### 2026-10-13 — `0.14.0` Data Page Trusted Project Context Cleanup D3/D4 (1/2 Dead-Path Removal, Reviewed)

- Bound `DataPage` project ID solely from `useWorkbench().projectId`, removing unused prop/route/standalone fallbacks and fake empty-project arrays while enforcing required `project_id` on `datasets.list`. Passed build and existing D1/D2 specs.

### 2026-10-13 — `0.14.0` Visible Dataset Version Load Failures D2 (Reviewed)

- Changed Data page loading to publish dataset list and all versions together atomically rather than catching per-dataset errors into `[]`; any version load failure triggers page error + retry and clears stale table/modal targets while preserving genuine empty version lists. Passed build and a multi-scenario Chromium spec.

### 2026-10-13 — `0.14.0` Visible Data Template Download Failures D1 (Reviewed)

- Removed silent local `csvTemplate` fallback on `downloadTemplate` failures so errors surface via `notice` without triggering fake downloads or success banners while keeping retry active. Passed build and a Chromium failure/recovery spec.

### 2026-10-13 — `0.14.0` Visible Task Detail and Evidence Collection Failures R4 (Reviewed)

- Removed `.catch(() => null)` swallowing on `api.tasks.get` supplementary `/result` fetches, Results evidence gathering, and Exports `Promise.allSettled` skims so any failure triggers unified error + retry (clearing stale selections before fetching and avoiding false "No results" empty states). Verified via 6 adapter tests, build, and 2 real-HiGHS Chromium failure/recovery specs (`d8a484d`).

### 2026-10-13 — `0.14.0` Visible Step-Curve Read Failures R3 (Reviewed)

- Removed per-series `.catch(() => null)` shrinkage and `series.filter` in `api.results.hourly`: missing objects (404) and page failures throw as errors rather than faking absence, while genuine absence (`hourly_refs=null`/empty) returns `{absent: true}` cleanly; Results page isolates curve errors/retries from sealed metrics/financial cards using sequence guards against stale responses. Verified via 11 adapter absence/failure checks, 5 consumption checks, 12 pagination tests, build, and 3 Chromium specs (`b262b74`).

### 2026-10-13 — `0.14.0` Step-Series Publication and Sealing Closed Loop R5/R6 (Reviewed)

- Generators emit `step_series` metadata (`series_id`, instance, variable, explicit solver columns, step count, business unit) in Bundle `parameters.json`; ResultAdapter restores `ComputeResult.outputs.series` from same-solve primals (failing loudly on unannotated legacy Bundles); `application` persists hourly objects and seals refs inside the same publish transaction; frontend drops legacy field-name guesses and groups by authoritative identity/unit, bumping built-in generator/adapter to `2.0.0`. Verified via generator/adapter/Worker/API Docker tests and rebuilt Chromium pagination/legend/unit specs (`d1cdac4`).

### 2026-10-12 — `0.14.0` Step-Series Display and Paginated Consumption Closeout R5/R6 (Prior Record)

- Removed fabricated `resolution: ''` from `api.results.hourly`, updated labels/axes to computation steps (`Step curves`, `step`), reflected actual point counts instead of hardcoded `168`, implemented cursor/start pagination above `MAX_HOURLY_LIMIT=50000`, updated User Guide accuracy on decimation vs. Excel contents, and documented the pre-R5/R6 `hourly_refs=null` GUI block prior to closure above.

### 2026-10-12 — `0.14.0` Excel Export Language Contract R8 (Reviewed)

- Added `lang?: 'zh' | 'en'` to `ExcelExportInput` matching backend `ExcelExportRequest`, removing two `as` casts and defaulting explicitly to `'zh'` (`input.lang ?? 'zh'`). Passed Docker build and Chromium tests verifying both Results and Exports pages send selected languages and yield matching language rows inside downloaded workbooks.

### 2026-10-12 — `0.14.0` Help Center Scenario 8 Alignment (Reviewed)

- Updated Scenario 8 assertions (`admin-help-lang.spec.ts`) to inspect code blocks (`GeneratorProvider`) and tables (`DeviceDescriptor`) inside System Architecture Blueprint and navigate via top bar rather than asserting nonexistent tables/code on Getting Started or Constitution. Verified in Docker Chromium (`758b8eb`).

### 2026-10-12 — Rescinding Manual Canvas Acceptance Exception

- Per user ruling, rescinded the prohibition on Playwright for canvas drag-and-drop and port connections; bumped Constitution to `1.12.3` requiring Docker Playwright verification across all GUI workflows and updated AGENTS, CLAUDE, frontend, deployment, Roadmap, and User Guide docs.

### 2026-10-12 — `0.13.0` Documentation Closeout (Reviewed)

- Updated root README and version status to `0.13.0` complete / `0.14.0` next while keeping `pyproject.toml` at `0.1.0`, removed completed `0.13.0` from Roadmap, clarified DTO consumption rules in `frontend.md`, and merged via `fc20abf`.

### 2026-10-12 — `0.13.0` Audit Log Viewer Review and Merge

- Merged `9506cbb` → `cdbea5e` delivering admin read-only audit log GUI, leak-free cursor pagination, visible read failures, and strict permission gates without extra internal validation.

### 2026-10-12 — `0.13.0` Audit Viewer Closeout R1/R2/R3 (Pending Review)

- Fixed `query_audit` `next_cursor` dropping rows between pages (using last displayed row ID instead of unprinted lookahead probe), simplified `auditEntryFromServer` to trust authoritative DTOs directly, preserved `system` actor types without `"—"` placeholders, removed unused `AuditFilterInput`, and clarified sort order as reverse entry ID. Passed 3 PG/backend pagination tests and 4 Chromium audit tests.

### 2026-10-12 — `0.13.0` Admin Read-Only Audit Log GUI (Pending Review)

- Added `/admin/audit` ("Audit log", guarded by `RequireAdmin`) displaying timestamp, action, actor ID/type, entity type/ID, `request_id`, and `trace_id`, supporting exact filters, cursor pagination, and retry preserving filters while converging `AuditEntry`/`AuditListParams` to `GET /admin/audit` and fixing stale version/result UI comments. Passed 4 Playwright audit specs and adjacent version/import/result suites.

### 2026-10-12 — `0.13.0` Exit Audit and Remaining Scope

- Merged read-only exit audit `7dac83a`: confirmed version creation, scheme application, provenance, restore, and package import GUIs complete while scoping remaining `0.13.0` work to the admin read-only audit log viewer.

### 2026-10-12 — `0.13.0` S4-C Package Import GUI State Closeout C1/C2 (Reviewed)

- Reset local import dialog state cleanly across Open New Project and Close (C1), separated import confirmation from list refresh failures so refresh retries re-fetch only `GET` lists without re-submitting confirm or upload `POST`s (C2), and removed redundant `ProposalSnapshotDto`/`toProposalView` aliases (`7b59703` → `3ad55da`). Passed 6 import and 3 version Docker browser tests.

### 2026-10-12 — `0.13.0` S4-C Project Package Import GUI (Reviewed)

- Added **Import project package** on **Projects**: uploading shows staged proposal preview (`project_id=null`), while explicit **Confirm import** creates an independent project without overwriting existing projects; displays itemized `params.reasons` errors on invalid archives, preserves proposals for retry on confirmation failures, and prevents duplicate submits during flight. Passed 4 Docker Playwright tests and 3 version specs.

### 2026-10-12 — `0.13.0` S4-B2 Import Proposal Lifecycle (Backend, Reviewed)

- Eliminated pre-created active projects during upload: upload stores only staged proposals and `import_proposal` storage refs (`project_id=null`), while confirmation creates the project, drafts, versions, configs, and object refs atomically in one transaction (protected by proposal row locks against concurrent confirm races) with idempotent replay (`b402d9c`). Verified with isolated PG concurrency tests and HTTP/cleanup regressions.

### 2026-10-12 — `0.13.0` S4-B1 Project Package 2.0 Historical Dependency Round-Trip (Backend, Reviewed)

- Surfaced itemized validation errors in `params.reasons`, upgraded package format to `2.0.0` (version-isolated datasets and `files.version_configs` per historical version without `1.x` legacy parsing), reconstructed historical dependencies in a single `application` transaction with profile-conflict rejection, re-merged/verified effective snapshots at upload boundaries, and registered `effective_finance_content` object refs. Passed 63 package/restore tests and isolation sentinels.

### 2026-10-11 — `0.13.0` S3-B Apply Scheme Closeout C1/C2/C3 (Reviewed)

- Connected live draft revision `GET` when opening the **Apply scheme** dialog and transmitted `expected_revision` to catch concurrent conflicts (`409`) cleanly (C1), locked dialog dismissal/re-submission during flight (C2), and updated `contracts.md` and `api/projects.py` route docs (`be38239` → `3ed3c42`). Passed real Chromium conflict/locking/error specs, 41 backend API tests, and web build.

### 2026-10-11 — `0.13.0` S3-B Apply Scheme Parameter Writeback and Derivation (Reviewed)

- Added **Apply scheme** alongside planning solution previews: previews source version, per-parameter changes, and replacement impact, then derives a new current project version on top of the snapshot's source version (`reason=apply_result`), writing solved parameters onto instances while restoring config pointers and removing legacy patch endpoints. Passed backend atomic rollback/404/403/409 tests and real Chromium dual-instance optimal writeback specs.

### 2026-10-11 — Scheme Application Baseline Ruling

- User confirmed Apply scheme derives a new version from the calculation snapshot's source project version and replaces current working state without carrying over post-run draft edits, previewing impacts before execution while keeping history immutable.

### 2026-10-11 — `0.13.0` S3-A Real Candidates, Parameter Preview, and Provenance Wiring (Reviewed)

- Replaced metric regex candidate parsing with backend `candidates#index` lists ("No candidates" on single solves), clarified **Select this candidate** records preference without altering versions, added **Planning solution preview** pairing sealed primal variables with snapshot-frozen planning bindings and provenance (`SINGLE_SOLUTION_ID=0`), projected project/dataset versions strictly from snapshot refs, cleared stale state on evidence switch, and removed redundant client field checks. Passed 5 `test_planning_solution.py`, 25 project API, result API, and Chromium multi-instance/failure-switch specs.

### 2026-10-11 — Clarifying Result Adoption vs. Scheme Application Semantics

- Distinguished marking candidate adoption from applying a scheme (writing real planning parameters to derive a new project version using snapshot instance bindings rather than current dataset versions).

### 2026-10-11 — `0.13.0` S2 Project Version Restore GUI (Reviewed)

- Added **Restore this version** inside version details listing replacement impacts (draft content, config pointers, new `restore` version, immutable history), disabled duplicate clicks during flight, surfaced list vs. summary refresh failures separately, allowed restoring any saved version including rolling back unsealed draft edits atop the current version, and fixed feature-to-page reverse imports. Passed `tsc`, 25 backend tests, and 5 restore + 3 version history Chromium specs.

### 2026-10-11 — `0.13.0` S1 Project Version Creation / List / Detail GUI (Reviewed)

- Added **Versions** in the workbench to create immutable versions manually (`name` required, `description` optional), inspect version rows/metadata, enforce `{versions: [...]}`/`{version: {...}}` contracts visibly without empty fallbacks, and guard detail requests against close/reselect/project-switch races. Passed `tsc`, 5 mapper tests, 25 backend tests, and 3 Playwright specs.

### 2026-10-11 — `0.12.0` Exit Review Approved and Merged to Mainline

- Fast-forward merged reviewed `0.12.0` implementation and docs via `da2f3f7` after passing 116 Docker tests, closing full-horizon single solves, snapshot freezing, coupled Stage-2 re-solving, Bundle/receipt/evidence sealing, construction-plus-operation cash-flow evaluation, interface-driven variable O&M, and Excel/UI alignment (retaining explicit exclusions for positive fixed capex under optional construction and timeless scalar O&M). Closed `0.12.0` milestone with `0.13.0` next.

### 2026-10-11 — `0.12` Documentation Closeout and Review Facts

- Recorded Codex approval across T1/T2/T3, scalar O&M bans, and generalized variable O&M aggregation on `slice/0.12f4e-pv-temp`, and synced User Guide rules for `evaluation` inputs, `baseline-confirm` audit notes, and IRR statuses.

### 2026-10-11 — Generalizing Variable Usage Aggregation (Codex Reviewed, Pending Merge)

- Generalized `integrate_positive` (`to_si(flow) × step_duration`) and `sum_positive` (direct SI sum via `core.units`) beyond `J`/`W` to any compatible physical carrier including volumetric gas (`m³/h`, `m³`) and tariffs without unit allowlists, heating-value guesses, or cross-dimension conversions (noting `Nm³` absent from unit vocabulary). Verified via 2-year volumetric hand checks and gas-boiler `m³` solves (`219000` match).

### 2026-10-11 — Assembly Gate Closure for Scalar `period_variable_om` (Codex Reviewed, Pending Merge)

- Rejected scalar `property` × timeless rate consumption under `period_variable_om` at assembly input (`ASM-BIND-002` / `period_variable_om_scalar_unsupported`) while keeping scalar drivers for `upfront_capex`/`annual_fixed_om` and interface series for `period_variable_om`; removed unreachable `PropertyDriverFacts` paths and verified across 24+ Docker regressions without adding post-solve duplicate checks.

### 2026-10-11 — Rulings on O&M Classification and `0.12` Scope Boundaries

- Clarified fixed O&M (`annual_fixed_om` post-commissioning annual fees and `maintenance_expenses` explicit-year outlays) vs. variable O&M (`period_variable_om` usage-based yearly accumulation) without introducing new input fields, aliases, or scalar multi-year lump sums, keeping positive-fixed-capex optional construction rejected visibly in `0.12`.

### 2026-10-10 — `0.12.0` Slice T3: UI and Export Alignment for Sealed Evaluation (Codex Reviewed, Pending Merge)

**Completed**: 2026-10-10 (Docker verification and browser acceptance complete; pending review/merge)

**Release Status**: Product version remains `0.1.0`; does not declare `0.12.0` complete. Connects sealed evaluation display on Results, Excel "Financial Summary" projection, and `financial_absence` explanations.

#### What Changed

- Removed unreachable `period_variable_om.fixed` branch in `compute_cases`; echoed `construction_period`/`operation_years` in sealed evaluation blocks.
- Projected `financial_evaluation` uniformly in `result_view` alongside explicit `financial_absence` (`outcome_not_normal` / `solution_not_requested` / `missing_unknown`) without recomputing or guessing.
- Added the "Sealed evaluation cash flows" card displaying `K`/`N`/`E`, construction totals/payments, operating components, `CF[0..E]`, NPV/IRR/payback, and statuses (distinguishing `t=0..K` construction from `t=K+j` operation, keying multiple payments at identical `t` by index), plus dedicated Excel "Financial Summary" projection and `contracts.md`/`finance.md` specs.

#### What It Solved

- End-to-end Docker real HiGHS + Chromium verified `K1N2E3` (`0.6`/`0.4` payments, two `t=3` maintenance entries `1000.00` + `2500.50`), cell-exact Excel reconciliation across all 24 summary rows (`test_financial_export_content.py`, verified with negative swap controls), `financial-absence.spec.ts` (metrics-only `solution_not_requested` & infeasible `outcome_not_normal`), and fixed frozen tuple `outputs` recognition in `_financial_absence`.

#### Explicitly Excluded

- Multi-year allocation of valid scalar `period_variable_om` drivers, optional positive fixed capex (independent gap), and modifying `outputs` for metrics-only evaluations.

### 2026-10-11 — `0.12.0` Slice T2: Evaluation Timeline Cash-Flow Pipeline (Codex Reviewed, Pending Merge)

**Completed**: 2026-10-11 (Docker verification complete; pending merge)

**Release Status**: Product version remains `0.1.0`; `0.12.0` incomplete pending T3.

#### What Changed

- Implemented pure `finance.timeline` deriving annual boundaries from `start_year`/`resolution`/`N`, summing `price[t] × e[t]` per year, accumulating interface-based variable O&M (failing loudly on scalar property O&M), reading solved `__upfront_capex` and `annual_fixed_om` for `new` instances only (excluding `existing` sunk outlays), applying `construction_payments` (`-ratio * C`) and `maintenance_expenses` in `Decimal`, and invoking `evaluate_project_investment`.
- Wired `compute_cases.build_financial_evaluation` into Worker `runner` inside the existing single publish transaction without invoking legacy cash-flow builders or adding duplicate §2.6 checks (`TimelineInputs.maintenance` removed, missing solution components fail naturally).

#### What It Solved

- Docker real HiGHS chains verified `K1N2E3` (`CF=[-.6C, -.4C, R1, R2]` with `R1 != R2`), dual `t=3` maintenance deductions, `K0N2` leap-year + mandatory fixed cost at `t0`, existing 600 kW heat pump excluding sunk capex/fixed O&M, `no_solution` IRR on all-negative flows, and 9 hand-calculated boundary/sign unit tests.

#### Explicitly Excluded

- T3 UI/export wiring, financial blocks for metrics-only tasks lacking `solution`, timeless scalar variable O&M, and optional positive fixed capex.

### 2026-10-10 — `0.12.0` Slice T1: Construction-Inclusive Evaluation Input/Freezing & Operating Horizon Cutover (Codex Reviewed, Pending Merge)

**Completed**: 2026-10-10

**Release Status**: Product version remains `0.1.0`; `0.12.0` incomplete pending T2/T3.

#### What Changed

- Updated task submission `evaluation` top-level validation (`evaluation_years = K + N`, non-negative integer `construction_period`, positive integer `operation_years`, `0..1` `discount_rate`, exact-sum-1 `construction_payments` in `0..K`, decimal-string `maintenance_expenses` at `year` indexing `CF[year]` with zero numeric rounding).
- Bumped snapshot schema historically to `4.0.0`, switched point/materialization/prediction/generator/assembly counts to `operation_years`, updated task submission UI for `K+N` + JSON payment/maintenance inputs, and hashed normalized evaluation JSON into idempotency keys.

#### What It Solved

- Passed 77 freeze/parsing checks, 11 multi-year horizon regressions, 70+219 API/assembly/solver tests, `evaluation-empty-gate` (2), and `config-revision` (4) in Docker.

#### Explicitly Excluded

- T2 cash flows, T3 UI/exports, and optional positive fixed capex.

### 2026-10-10 — `0.12.0` Slice R1/R2: Rejecting Duplicate Assembly Variables & Fixing Unbounded-Lower MPS Bounds (Reviewed)

**Completed**: 2026-10-10

**Release Status**: Product version remains `0.1.0`; pending remaining `0.12.0` slices.

#### What Changed

- R1: Enforced `(device_ref, parameter)` uniqueness at assembly `check_planning` (`ASM-PLAN-003` / `variable_duplicate_target` pointing to `planning.variables.<name>`) matching config save validation without duplicate generator checks.
- R2: Fixed `write_mps` emitting invalid `FR+UP` (which caused HiGHS to drop `UP` bounds on `lower=None`) to emit `MI+UP` (`-inf` lower + finite upper) while leaving `MI` (`upper=0`) and `FR` (unbounded both sides) intact.
- Synced removal of legacy per-year active-capacity rules in `assembly-yaml.md` and `finance-yaml.md` with mainline `ebea360`.

#### What It Solved

- Passed R1 (3 new tests + 17 in `test_planning_build.py` + 152 regressions), R2 (5 new tests + 47 regressions), and clean Ruff checks in Docker.

#### Explicitly Excluded

- Optional positive fixed capex binary indicators and construction/payment/evaluation timeline fields (pending in `0.12-4` / T1–T3).

### 2026-10-10 — `0.12.0` Slice: Removing Phased Annual Construction; Real First-Year Capacity Bounds (Pending Review)

**Completed**: 2026-10-10

**Release Status**: Product version remains `0.1.0`; pending fixed-cost binary indicators and cash-flow evaluation.

#### What Changed

- Removed `PlanningVariable.year_offset` cleanly across config save, assembly validation, schema, samples, and tests (`(device_ref, parameter)` key), migrating `_check_annual_scope` to `_check_build_scope` (mandatory `lower>0` + fixed upfront cost counted once; optional + positive fixed cost rejected via `positive_fixed_cost_unsupported`).
- Materialized omitted lower bounds on construction capacity variables as `LO 0` instead of `None` (`FR+UP`), restoring valid `LO+UP` bounds accepted by HiGHS 1.7.2 and passing 3 real `N=2` first-year construction solves (PV capped at 50 vs. 62.5 unconstrained optimum, mandatory + 900k fixed cost at `[10, 40]` → 40, heat pump peak 62.5).

#### What It Solved

- Passed `test_planning_build.py` (14), `test_project_model.py` + `test_finance_triplet.py` (69), `test_assembly_contract.py` + `test_config_revisions.py` (130), and 3 first-year e2e scenarios in Docker, while disclosing that generic non-construction device variables with `(None, U)` still awaited R2's `MI+UP` fix above.

### 2026-10-10 — Finance Timeline Ruling: Evaluation Horizon Includes Construction and Operation Periods

- Updated target docs for `evaluation_years = construction_period + operation_years` with non-negative integer construction years, operating-only simulation/prediction/price horizons, exact-sum-1 payment schedules, explicit renewal outlays, and a 4-point hand-calculated example (`K=1`, `N=2`). Pure doc revision ahead of T1–T3.

### 2026-10-10 — Construction Timing Clarification: Start-of-Horizon Build Does Not Imply Immediate Commissioning (Synced `7537eeb`)

- Updated target contracts and Roadmap separating project start construction, payment timing, and commissioning while keeping first-year build sizing without annual build-year selection. Pure doc revision correcting immediate-commissioning wording below.

### 2026-10-10 — `0.12.0` Slice F4e: Built-In PV Temperature Unit Fix (`K`/`°C`)

**Completed**: 2026-10-10

**Release Status**: Built-in PV restores normal output at 25 °C standard irradiance (previously zero due to bare `25` subtracted from Kelvin-converted `ambient_temperature`); product version remains `0.1.0`.

#### What Changed

- Replaced bare constant `25` in `devices/catalog/pv.yaml`'s `pv_generation` equation (and matching sample `acme.device.pv.device.yaml`) with `stc_reference_temperature` property (`value: 298.15`, `unit: K`), clarifying SI evaluation rules in `device-model-yaml.md` without code or grammar hacks.

#### What It Solved

- Reversed failing `+0.0926` temperature factor to expected `-1.0` (zero derating at 25 °C), passing `test_device_model_v2_catalog.py` and `test_project_model.py` in Docker.

### 2026-10-10 — `0.12.0` Slice F4d: Verification of Annual Fixed O&M Rejections and Docs Alignment

**Completed**: 2026-10-10

**Release Status**: Pure documentation slice; product version remains `0.1.0`.

#### What Changed

- Verified `finance/triplet.py` already forbids `annual_fixed_om.fixed` at input boundaries (`test_finance_type_component_requirements`), avoiding speculative duplicate submission checks (`AGENTS.md` #10/#11) and correcting F4c's historical misstatement.
- Clarified static annual O&M rates vs. multi-year explicit duration literals (replacing ambiguous ``N year`` notation with concrete examples like `2 year`) across `finance-yaml.md`, `assembly-yaml.md`, and F4b/F4c entries, and documented a counterexample retracting the prior `b <=> y>0` claim without explicit minimum capacity or discrete units.

#### What It Solved

- Verified via `test_finance_triplet.py` and `test_planning_annual.py` in Docker.

### 2026-10-10 — `0.12.0` Slice F4c: Annual Average Semantics for `annual_fixed_om`

**Completed**: 2026-10-10

**Release Status**: `annual_fixed_om` under annual takeover computes the arithmetic annual mean (`currency/year`); product version remains `0.1.0`.

#### What Changed

- Updated annual-takeover linear `annual_fixed_om` aggregation to divide the sum of active capacity across operating years by `N`, preserving `currency/year` units so objectives multiply by `1 year` for average annual or explicit horizon literals (e.g., `2 year`) for horizon totals, leaving static non-annual capacities untouched and updating [Finance YAML Contract](../../developer-guide/en-US/formats/finance-yaml.md#time-bases-and-components) and [Assembly YAML](../../developer-guide/en-US/formats/assembly-yaml.md#financial-component-identifiers-in-planning-expressions) (subsequently corrected by F4d regarding `annual_fixed_om.fixed` and noting PV `25` °C temperature offset addressed in F4e).

#### What It Solved

- Turned 3 targeted Docker checks green alongside locked static annual-rate checks.

### 2026-10-09 — `0.12.0` Slice F4b: True Multi-Year Capacity Additions (Cumulative Active Capacity)

**Completed**: 2026-10-09

**Release Status**: Demonstrated phased annual capacity additions in a single `N=2` HiGHS solve; product version remains `0.1.0`.

#### What Changed

- Modeled each `(instance, parameter, year_offset)` as an independent capacity-addition variable with derived cumulative in-service capacity `s_k` per year using `horizon_year_boundary_steps`, expanding capacity limits, equations, `upfront_capex`, and `annual_fixed_om` per year while rejecting positive `upfront_capex.fixed > 0` (`positive_fixed_cost_unsupported`) and out-of-scope scalar `period_variable_om` (subsequently superseded by first-year construction simplification on 2026-10-10).

#### What It Solved

- Passed real `N=2` (17,544-step) HiGHS verification (`438000` / `87840` kWh grid imports, 50 kW year-2 build) and generator structure tests in Docker.

### 2026-10-09 — `0.12.0` Slice F4a: Public Contract for Annual Capacity Additions (`year_offset`)

**Completed**: 2026-10-09

**Release Status**: Input/save/submission validation contract established (later removed cleanly on 2026-10-10); product version remains `0.1.0`.

#### What Changed

- Added required non-negative integer `PlanningVariable.year_offset` with `(device_ref, parameter, year_offset)` uniqueness at save, `0..N-1` bounds at submission (`ASM-PLAN-003`), explicit upper-bound gates when `upfront_capex.fixed > 0`, and synced `assembly-3.0.0.schema.json` and samples.

#### What It Solved

- Passed unit, save uniqueness, structural, and submission boundary checks in Docker.

### 2026-10-09 — `0.12.0` Slice F3: Point-by-Point Time-Varying Energy Tariff Billing

**Completed**: 2026-10-09

**Release Status**: Validated end-to-end point-by-point time-varying tariffs across `N=2` HiGHS solves; product version remains `0.1.0`.

#### What Changed

- Introduced `ies.price-series@1.0.0` CSV (`step,price`, positive/zero/negative finite decimals), opaque object ID `time_series.ref` with `series_meta`, upload endpoint auto-detection returning `price_object_id`, submission-gate byte/length verification (`TASK-REQ-017`), Worker materialization into `MaterializedSet.prices`, and point-by-point `_tariff_affine` (`price[t] * e[t]`) honoring negative-price sign reversals per `modules/finance.md` and `formats/finance-yaml.md`.

#### What It Solved

- Passed 9 parser/rejection tests, upload/submission/Worker materialization checks, assembly sign tests, and `N=2` (17,544-step) real HiGHS reconciliation (`589478.4 CNY = 0.7*E1 + 0.35*E2`, ruling out averaged-price variants).

### 2026-10-09 — `0.12.0` Slice F2 Closeout: Single-Pass Full-Horizon Solves, Empty-Series Horizons, and `2.1.0` Headers

**Completed**: 2026-10-09

**Release Status**: Verified multi-year leap-spanning single solves (`N=2`, 17,544 steps) and empty-series horizon modeling; product version remains `0.1.0`.

#### What Changed

- Switched Generator's third argument to materialized `MaterializedSet` carrying `evaluation_years`, deriving all horizon steps from baseline + years and deleting single-year fallbacks (`point_count_for_year`); verified `data_full` year-differentiated inputs across 17,544 steps and cross-year SOC continuity; bumped device data headers historically to `2.1.0` rejecting `2.0.0` (`DATA-META-003`) and removed redundant materialization-side `data_full` length checks per Constitution §2.6.

#### What It Solved

- Passed gating, 17,544-point materialization, `N=2` real solves, empty-series horizon modeling, legacy-header rejections, and backend regressions in Docker.

### 2026-10-09 — `0.12.0` Slice F1 Fix: Typed Top-Level Evaluation Confirmation, Snapshot Freezing, and Frontend Empty-Value Gates

**Completed**: 2026-10-09

**Release Status**: Fixed `0.12.0` Slice F1; product version remains `0.1.0`.

#### What Changed

- Moved `evaluation` to a typed top-level submission payload parsed by `EvaluationInput` (`TASK-REQ-015`), tightened `discount_rate` to `0..1`, removed `TASK-REQ-016` multi-year blocking so `evaluation_years >= 1` freezes cleanly into snapshots, placed `evaluation` into top-level `CalcSnapshot` columns (deduping by confirmer while excluding `confirmed_at`), reused assembly's resolved finance snapshot directly at submission, removed `point_count_for_baseline` and ambiguous `step_count`, and blocked blank `start_year`/`evaluation_years`/`discount_rate` inputs in GUI forms (`Number('')` → 0 eliminated).

#### What It Solved

- Passed submission gate, top-level snapshot freezing, deduplication, override verification, baseline derivation, and Docker Playwright non-canvas empty-value rejection checks.

### 2026-10-08 — `0.12.0` Slice F1: Baseline `start_year`, Submission Evaluation Confirmation, and Snapshot `3.0.0`

**Completed**: 2026-10-08

**Release Status**: First implementation slice of `0.12.0`; product version remains `0.1.0`.

#### What Changed

- Replaced `leap_year` with mandatory Gregorian `start_year` across project creation, versions, assembly (`3.0.0` historically), `data_repeat`, `series_meta`, materialization, and step derivation (`horizon_point_count` / `horizon_year_point_counts` / `horizon_year_boundary_steps`), added `config.evaluation` confirmation (`evaluation_years`, `discount_rate`, initial `TASK-REQ-015`/`TASK-REQ-016`), bumped snapshots to `3.0.0`, moved full-horizon price length validation to submission (`TASK-REQ-017`), and added `start_year` and evaluation confirmation UI fields.

#### What It Solved

- Passed baseline, derivation, submission gate, snapshot freeze, and override checks in Docker.

### 2026-10-10 — Finance Guidance Docs: Clarifying Capex and Operating Renewal Semantics (Synced `36f6916`)

- Preserved target rules counting fixed capex once in the first positive addition year and capacity capex in addition years; clarified annual timing for same-year construction/commissioning and initial-year outlays rather than construction-period averaging or default payment schedules.
- Clarified that operating renewals enter cash flows in occurrence years, expansions stay construction outlays, and simple initial payback vs. full discounted evaluation differ while correcting the negative investment sign on `CF[0]`. Pure documentation clarification ahead of implementation.

### 2026-10-10 — Planning Scope Revision: First-Year Construction with Full-Horizon Operation (Synced `45a751f`)

- Revised assembly and financial target contracts and Roadmap per user ruling: projects build at the start of year 1 optimizing build choices and capacities without solving build years, removing annual capacity addition variables and per-year variable indices.
- Fixed and capacity construction costs enter `t=0` (excluding unbuilt optional instances), leaving mid-horizon expansions as separate studies while keeping renewals in occurrence years. Supersedes phased annual construction guidance above; doc revision only.

### 2026-10-03 — `0.12.0` Slice B5-3 Public Upload for `device-data` Step Format: Recognition → Validation → Canonical Persistence → Full Pipeline

**Completed**: 2026-10-03

**Release Status**: Upload closed-loop slice for `0.12.0` prediction chain; product version remains `0.1.0`; `0.12.0` incomplete pending target-type semantic contracts, horizon/discount confirmation, and multi-year finance.

#### What Changed

- Upload endpoint detects step files via `# schema: ies.device-data` (leaving untagged and generic CSVs unchanged), reusing `parse_data_file_v2` plus upload-boundary checks (matching `# resolution`, contiguous `step` from `0`, non-empty finite values, and `unit.<col>` per column; blocking with `400 DATA-VAL-001`).
- Persists canonical step CSV (`file_kind=data`, ordered metadata + `step,<cols>` header + `repr` values, LF without BOM) directly usable by assembly binding and series materialization, recording accurate `build_data_quality_report_v2` metrics while leaving device/interface/mode checks to assembly.
- `resolution` form field remains required and matches `# resolution`; `fields` form input does not apply to self-describing files.

#### What It Solved

- Docker public full-chain (zero domain-level crafting): HTTP step history upload → project binding → task submit → real HiGHS solve → evidence matching trailing-value predictions alongside a 15-case upload rejection matrix without regressing generic timestamp uploads (`test_dataset_api` 21 passed).

### 2026-10-03 — `0.12.0` Slice B5 Closeout B: Upload UX (`UI Mode`, Unknown Schema Rejection, `fields` Warning) & Browser Acceptance

**Completed**: 2026-10-03

**Release Status**: Upload UX closeout slice for `0.12.0` prediction chain; product version remains `0.1.0`; `0.12.0` incomplete.

#### What Changed

- Upload dialog inspects file headers (`uploadHeadScan` with generation guards and explicit read-failure handling) to enter self-describing step mode automatically (reading resolution from file, hiding field descriptions and UTC offset); unknown `# schema` headers reject immediately with `400 DATA-META-003`, and superfluous `fields` parameters record non-blocking warning `DATA-FILE-005`.
- Synced User Guide distinguishing both upload modes and Developer format specs; removed redundant internal validation in `materialize_predicted_series` already enforced by `TASK-REQ-014` and config freezing.

#### What It Solved

- Passed 20 backend upload matrix checks, `test_device_data_upload`, `upload_head_scan.test.mjs` (11), Ruff, and Docker Playwright Chromium `device-data-upload.spec.ts` covering both self-describing step uploads and generic CSV uploads cleanly.

### 2026-10-03 — `0.12.0` Slice B5-2b Public Prediction Capability: Deterministic `naive` Predictor + Stage-2 Convergence Loop

**Completed**: 2026-10-03

**Release Status**: Public prediction slice for `0.12.0`; product version remains `0.1.0`; `0.12.0` incomplete pending evaluation confirmation and multi-year finance.

#### What Changed

- Added predictor descriptors and independent atomic registry with built-in `ies.predictor.naive@1.0.0` (last-value carry-forward, deterministic, opaque target types) wired in both API and Worker contexts; submission validates unregistered predictors (`400 TASK-REQ-013`) and unsupported target types (`400 TASK-REQ-014`).
- Added prediction stage in computation materializing full-horizon `data_predict` series, persisting training artifacts, predicted series, and receipts atomically with evidence; Stage-2 loop (triggered when >1 target historically, subsequently superseded by `depends_on_solve_result` dispatch) solves each iteration before checking convergence (`max_iterations >= 2`, `400 TASK-REQ-012`), archiving intermediate rounds under `round_history` while keeping final-round evidence authoritative.
- Fixed B4 bug where numeric canonical IDs routed to OID lookups instead of integer primary keys.

#### What It Solved

- Verified single-target pass-through, dual-target multi-round convergence, coupled test-predictor 3-round convergence with distinct objectives, and `400` rejections in Docker (disclosing missing `device-data` public upload later closed in B5-3).

### 2026-10-03 — `0.12.0` Slice B5-2a Data Series Source Declaration Entry: `params` Dict Shape → Assembly Binding

**Completed**: 2026-10-03

**Release Status**: Declaration entry slice for `0.12.0`; product version remains `0.1.0`; `0.12.0` incomplete.

#### What Changed

- Enabled `{"mode": "data_repeat", "dataset_version_id": int}` and `{"mode": "data_predict", "dataset_version_id": int, "target_type": str}` in device `params` on predefined slots, synthesizing `data/ds{vid}.data.csv` paths cleanly without caller path assembly and strictly validating union shapes at conversion boundaries.

#### What It Solved

- Closed the missing API draft entry for data-series bindings, verified from declaration through canonical artifact issuance and 8,760-point materialization.

### 2026-10-03 — `0.12.0` Slice B5-1 Prediction Input Freezing: Bidirectional Config↔Assembly Match + Multi-Target Convergence Gate

**Completed**: 2026-10-03

**Release Status**: First prediction-chain slice for `0.12.0`; product version remains `0.1.0`; `0.12.0` incomplete.

#### What Changed

- Added `_check_prediction_inputs` at task submission validating 1-to-1 coverage between `CalculationConfig.predictions` and assembly `data_predict` bindings (`400 TASK-REQ-010` / `TASK-REQ-011`) and convergence requirements (`400 TASK-REQ-012`), freezing predictions inside existing config/assembly/snapshot artifacts without extra schema columns.

#### What It Solved

- Moved prediction target mismatches and missing convergence criteria from late execution failures to immediate submission rejections.

### 2026-10-03 — `0.12.0` Slice B4 Real Computation Chain: Frozen Snapshot → HiGHS Solve → Single-Transaction Evidence Sealing

**Completed**: 2026-10-03

**Release Status**: First real compute Worker vertical slice for `0.12.0`; product version remains `0.1.0`; `0.12.0` incomplete.

#### What Changed

- Persistent Worker daemon orchestrates long Attempts across short-transaction `application.worker` commands: restoring the frozen snapshot inputs → `GeneratorProvider` building Bundles (baking effective snapshot seeds and exact executor/adapter refs) → real HiGHS `SolverRuntime` execution → `ResultAdapter` interpretation → single DB transaction committing object metadata/refs, evidence, and terminal task state.
- Separated `fenced` vs. `cancelled` handling (discarding fenced results without writing execution progress or reporting cancel), added `compute_worker` to `docker-compose.yml` using unified deploy config, and combined final commit units atomically.

#### What It Solved

- Delivered the first real end-to-end Docker HiGHS run (`completed` / `normal_completion` with lossless evidence retrieval) and eliminated three-transaction splits and fenced/cancelled misattribution.

### 2026-10-03 — `0.12.0` Slice B Unified Deployment Config: Single TOML Authority + Public Form Defaults

**Release Status**: Unified deployment configuration slice for `0.12.0`; product version remains `0.1.0`; `0.12.0` incomplete.

#### What Changed

- Added `backend/deploy/pies.toml` (`/app/deploy/pies.toml` in images) as sole authority for non-secret runtime params across API and Workers (slots, sessions, storage thresholds, rate limits, quotas, solver caps `172800 s / 16 GiB / 8 cores / network off / 64 files / 512 MiB`, HiGHS allowlist, empty env, default form limit `43200 s`) without duplicating defaults in Compose env or frontend code.
- Validated TOML rigorously on startup (replacing `task_timeout_hours = 8` and `IO_SLOT_CAPACITY = 2`), exposed form defaults atomically via `GET /api/deployment/defaults` with visible failure banners and late-read protection.

#### Override Precedence

Explicit argument > `IESPLAN_DEPLOY_CONFIG_FILE` mounted file > built-in package default; secrets and orchestration stay in environment variables.

### 2026-10-02 — `0.12.0` Slice B Device Input Freezing: Snapshots Freeze Device Math Contributions per Instance

**Release Status**: Device input freezing slice for `0.12.0`; product version remains `0.1.0`; `0.12.0` incomplete.

#### What Changed

- At task submission, built per-instance mathematical contributions in the single device-parsing pass and froze non-empty `device_models` (`{instance_id: contribution dict}`) into `CalcSnapshot` (historically bumped to `2.0.0`), adding structural `contribution_from_dict`/`ast_from_dict` restoration in `modeling` so Workers never re-read live `devices` catalogs or re-run equation validation internally.

### 2026-10-01 — `0.12.0` Slice A3: Remaining Snapshot Dependency Freezing (`tolerances` Cleanup, `schema_version`, Adapter/Executor Refs)

**Release Status**: Third slice of `0.12.0`; product version remains `0.1.0`; `0.12.0` incomplete.

#### What Changed

- Removed dead `tolerances` column/fields on snapshots (sole tolerance authority is `CalculationConfig.convergence`), added explicit `schema_version` (`1.0.0`), resolved and froze exact companion `result_adapter` and `executor` refs at submission from the published generator registry (`400 TASK-REQ-007`/`008`/`009`), and aligned samples/tests to `ies.generator.highs_milp@1.0.0`.

### 2026-10-01 — `0.12.0` Slice A2: Task Submission Binds Config Revision and Freezes into Snapshot

**Completed**: 2026-10-01

**Release Status**: Second slice of `0.12.0`; product version remains `0.1.0`; `0.12.0` incomplete.

#### What Changed

- Required explicit `config_revision` (positive int) on calculation submissions, resolving and freezing the exact saved config revision inside the snapshot (`config_ref`, returning `400 TASK-REQ-006` / `404` without draft/default fallbacks), incorporating it into snapshot deduplication, re-enabling all four task types in Tasks UI, aligning preflight to accept valid `constant` scalar inputs on `predefined` ports (`PARAM-UNIT-002` / `PARAM-RNG-003`), and combining logical signature + one-time submit nonce into idempotency keys.

#### What It Solved

- Reopened GUI task creation without risking silent execution against mismatched draft configs.

### 2026-10-01 — `0.12.0` Slice A1: Converging Calculation Config to Immutable Revisions

**Completed**: 2026-10-01

**Release Status**: First slice of `0.12.0`; product version remains `0.1.0`; `0.12.0` incomplete pending A2.

#### What Changed

- Converted Calculation Config page into the 12-field revision editor (initially blank with no defaults) saving via `PUT {calculation_config, expected_revision}` optimistic locking (`409` preserves edits with user-driven reload; `422` surfaces errors), reported `VALID-CONFIG-001` when unsaved, and temporarily gated new task submissions until A2 wired revision references.

#### What It Solved

- Eliminated legacy free-form config overwrites and silent fallbacks to mismatched snapshots.

### 2026-10-01 — `0.11.0` Application Use Cases and API Boundary

**Completed**: 2026-10-01

**Release Status**: Completed `0.11.0` milestone; product version remains `0.1.0`, next target `0.12.0`.

#### What Changed

- Assigned explicit `application` owners across all six public families (`application.projects` / `datasets` / `configuration` / `tasks` / `results` / `packages`); use cases exclusively control `commit`/`rollback` while repositories only `flush`, use savepoints, or raise errors; `api` handles pure transport DTOs, auth, and HTTP semantics without direct ORM queries or cross-module private imports.
- Made task submission idempotency keys mandatory (removing keyless dedup branches and constant-false duplicate flags), exported pure metric types/functions publicly from `metrics` (removing legacy gate exemptions), and created standalone HTTP contract tests `tests/test_http_contract.py` (11 checks using HTTP only, renaming `test_protocol_baseline.py` for integration/gate checks).

#### What It Solved

- Made transaction ownership single and traceable and strictly separated HTTP contract tests from internal integration fixtures.

### 2026-09-29 — `0.10.0` ResultAdapter and Unified Results

**Completed**: 2026-09-29

**Release Status**: Completed `0.10.0` milestone (see 2026-09-28 entry for ResultAdapter contract/registry slice); product version remains `0.1.0`, next target `0.11.0`.

#### What Changed

- Delivered first solver adapter for `ies.solver.highs@1.7.2`: status mapping, declared output parsing, non-finite rejection, reverse business unit conversion (preserving monetary currency), and explicit self-reported time-limit rejection prior to 2026-11-06 incumbent support.
- Validated real API/DB/Worker-free contract chain (campus assembly → Bundle → solver shim receipt → `ResultAdapter` → MPS-verified objective), basic project-investment financial evaluation (`evaluate_project_investment`), and single-solve financial component declarations (`+` purchase / `-` sale) verified across two independent construction alternatives.

#### What It Solved

- Separated solver output interpretation from live project state and gave financial components an authoritative single-solve source.

### 2026-09-28 — `0.10.0` ResultAdapter Contract and Registry Slice

**Completed**: 2026-09-28

**Release Status**: First verifiable slice of `0.10.0` (full milestone in 2026-09-29 entry); product version remains `0.1.0`.

#### What Changed

- Published standalone `ResultAdapterProvider` descriptors and atomic registry resolving exact version refs, and upgraded unified `ComputeResult` (`ies.compute-result` `2.0.0` historically) separating orthogonal technical `execution_status` and `math_status` linked via `bundle_id` and `receipt_id` while removing coupled task outcome fields.

#### What It Solved

- Gave result adapters independent versioned boundaries and eliminated conflating process, mathematical, and business outcomes in a single status field.

### 2026-09-27 — `0.9.0` SolverRuntime and Controlled Execution

**Completed**: 2026-09-27

**Release Status**: Completed `0.9.0` milestone; product version remains `0.1.0`, next target `0.10.0`.

#### What Changed

- Published `ExecutorProvider`, `SolverRuntime`, and `ExecutionReceipt` public contracts and atomic registry rejecting missing/stale heartbeats or mismatched identities.
- Runtime enforces allowlisted executables and `argv` arrays only (no shell, clean env, isolated workdir, read-only inputs, manifest-checked outputs), covering cancellation, fencing, timeouts, confirmed OOM (`SIGKILL` + cgroup `oom_kill` delta), abnormal exits, and bounded logs.
- Verified exact `ies.solver.highs@1.7.2` solving official campus Bundles via a preconfigured isolated persistent runner container (non-root, network disabled, read-only root, dropped capabilities, `no-new-privileges`, CPU/RAM/PID limits, file-IPC exchange volume without mounting Docker socket) recording two-tier `applied` and `observed` resource facts.

#### What It Solved

- Established a zero-trust controlled execution boundary supporting both local controlled processes and isolated container runners under identical protocols.

### 2026-09-25 — `0.8.0` Pure Technical Device Contracts, GeneratorProvider, and Solver Bundle

**Completed**: 2026-09-25

**Release Status**: Completed `0.8.0` milestone; product version remains `0.1.0`, next target `0.9.0`.

#### What Changed

- Migrated device models, device data, and assembly to pure technical contracts (`2.0.0` historically, deleting `1.0.0` drafts and migration shims) with atomic `_N` ID assignment only upon passing validation, and decoupled independent `CalculationConfig` (generator/solver/precision/options/seed/outputs) out of assembly.
- Added full-horizon `constant`/`data_repeat` step materialization and `data_predict` handling, plus the first `ies.generator.highs_milp@1.0.0` producing deterministic linear models, MPS writers, and full Solver Bundles wired explicitly via the composition root.

#### What It Solved

- Converged canonical-assembly-to-solver compilation into a pure, deterministic boundary emitting byte-identical Bundles without touching databases, networks, object storage, or spawning solvers.

### 2026-09-13 — Backend Dependency Architecture Closeout

**Completed**: 2026-09-13

**Release Status**: Product version unchanged; next target remains `0.8.0`.

#### What Changed

- Converged configuration, validation, modeling, version-read, and upload-quota flows into complete `application` use cases leaving routes as thin adapters and eliminating duplicate authorization helpers.
- Converted Worker calls to phased `application.worker` commands removing fake default/placeholder successes; returned username/email/idempotency patterns to `core.patterns` and evidence validation to `results`; cleaned up superseded review gates and empty sentinels while passing all static architecture gates.

### 2026-09-13 — Pre-`0.8` Phase 1 Backend Decoupling Closeout

**Completed**: 2026-09-13

**Release Status**: Completed Phase 1 backend decoupling; product version unchanged, next target `0.8.0`.

#### What Changed

- Achieved zero `iesplan.services` imports from `application` (removing legacy `services` package), removed bare SQL/tables from `application` and `commit`/`rollback` from Worker (moving short transactions to `application.worker`), converged domain rules/persistence under single owners, returned structured `failed` + `TASK-SOLVE-001` for unimplemented computation rather than `lease_rejected`, removed `storage -> audit` reverse dependency, and passed 1,454 Docker tests with zero failures.

#### What It Solved

- Eliminated legacy service bypasses, bare-table leakage, Worker transaction ownership, dual implementations, and masked computation failures ahead of `0.8`.

### 2026-09-11 — `0.6.5` Pre-Assembly Contracts and Assembly Input Semantics

**Completed**: 2026-09-11

**Release Status**: Completed `0.6.5` milestone and merged to `master`; product version unchanged, next target `0.8.0`.

#### What Changed

- Published `FinanceProfile`, `FinanceOverrides`, `EffectiveFinanceConfig`, and `PlanningConfig` contracts pinned via immutable revisions, object refs, and audit receipts.
- Fixed project baselines, source modes, and contiguous `step` semantics in assembly inputs, moving `constant`/`data_repeat`/`data_predict` materialization to computation while retiring pre-assembly materialization, ref replacement, legacy prediction bypasses, and duplicate internal checks.

#### What It Solved

- Ensured planning and finance share one reproducible effective snapshot and established clean boundaries between assembly and computation.

### 2026-08-30 — Pre-Contract for Project Computation Baseline

**Completed**: 2026-08-30

**Release Status**: Product version unchanged; completed `0.6.5` project baseline slice without prematurely entering `0.8.0`.

#### What Changed

- Introduced immutable `ProjectBaseline` (`resolution`, `leap_year`, `scenario_mode` historically, later superseded by Gregorian `start_year`) with canonical JSON, structured diagnostics, and horizon step count derivation; required explicit submission on project creation and import; migrated existing rows and removed project-level `fixed_utc_offset_minutes` with PostgreSQL immutability triggers.

#### What It Solved

- Removed project-level timezone offsets from calculation timelines and enforced explicit blocking on missing or invalid baseline fields.

### 2026-08-25 — Top-Level Accounts Navigation and Removal of Unneeded Project Sharing Features

**Completed**: 2026-08-25

**Release Status**: Product version unchanged; UI and contract cleanup toward `0.8.0`.

#### What Changed

- Promoted **Accounts** (`/admin/users`) to top-level navigation with administrator password reset (issuing one-time temporary passwords, forcing next-login change, revoking active sessions).
- Removed "Duplicate project", "Transfer ownership", "Shared members", and "Admin access grant" toggles (including backend endpoints, services, `projects.admin_access`, and wrapper keys), converging project sharing cleanly onto offline project package export/import.

#### What It Solved

- Surfaced account management cleanly and eliminated over-engineered multi-owner sharing states duplicating project package export/import.

### 2026-08-24 — Project License Update

**Completed**: 2026-08-24

**Release Status**: Product version unchanged; updated source license from GNU GPL v3 to Mozilla Public License 2.0 (SPDX: `MPL-2.0`).

#### What Changed

- Replaced root and backend `LICENSE` texts with official MPL 2.0 and updated Python package metadata and README badges/statements to `MPL-2.0`.

#### What It Solved

- Eliminated license discrepancies across repository files, README, and Python distribution metadata.

### Milestone Summary

| Completed | Milestone | What Changed | What It Solved |
|---|---|---|---|
| 2026-10-01 | `0.11.0` | Six use-case families and transaction boundaries converged; standalone HTTP contract tests added | API acts purely as transport adapter; single transaction owner; contract vs. integration tests separated |
| 2026-09-29 | `0.10.0` | First HiGHS result adapter, real contract chain, basic financial evaluation, single-solve declared components | Decoupled solver output interpretation from live project state; authoritative single-solve financial provenance |
| 2026-09-27 | `0.9.0` | Published executor/runtime/receipt contracts & atomic registry; isolated runner executes exact HiGHS 1.7.2 | Established zero-trust controlled execution boundary with Docker isolation evidence |
| 2026-09-11 | `0.6.5` | Published finance/planning input contracts, pinned config refs & assembly inputs, retired pre-assembly materialization | Shared reproducible effective configs and cleanly separated assembly vs. computation stages |
| 2026-08-24 | `0.6.0 → 0.7.0` | Published canonical assembly contract, unified validation gate, immutable assembly artifact wired into tasks/snapshots/Workers | Closed legacy Checker bypasses and mutable assembly inputs; Workers trust validated artifacts |
| 2026-08-24 | `0.5.0 → 0.6.0` | Published device data CSV contract, unique normalizer, and catalog migration receipts | Eliminated timezone/DST ambiguity, unit mismatches, missing/non-finite cells, and GUI vs. package inconsistencies |
| 2026-08-24 | `0.4.0 → 0.5.0` | Published device model contract, stable ModelCommand IDs, deeply immutable descriptors | Removed functions/modules/host paths from public models and enabled reproducible YAML loading |
| 2026-08-23 | `0.3.0 → 0.4.0` | Narrowed object storage public boundary; stripped storage paths and ref counts | Decoupled business layers from storage internals and prevented host paths leaking in DTOs/audits/errors |
| 2026-08-23 | `0.2.0 → 0.3.0` | Unified error envelopes, immutable diagnostics, absent-evidence semantics, and protocol/architecture gates | Eliminated bare `detail`, silent empty-list fallbacks, mutable diagnostics, and protocol drift |
| 2026-08-22–2026-08-23 | `0.1.0 → 0.2.0` | Hardened sessions, downloads, task results, admin actions, object retention/recovery, quotas, and immutable audits | Closed CSRF, unauthorized reads/downloads, accidental deletes, immediate purging, resource abuse, and leaks |

### `0.7.0` Development Milestone — Canonical Assembly Artifact

**Completed**: 2026-08-24

**Release Status**: Milestone advanced `0.6.0` → `0.7.0`; merged to `master`, unreleased.

#### What Changed

- Published machine-readable `ies.assembly` `1.0.0` schema, canonicalizer `ies.assembly.canonical@1.0.0`, valid/invalid samples, and stable `ASM-*` codes.
- Canonicalizer fixes key order, UTC times, and finite numbers emitting compact JSON + LF with object ID/revision refs, unifying hand-authored assemblies and GUI exports behind four-stage validation to emit immutable `ValidatedAssemblyArtifact` (`ValidationReceipt` + canonical bytes) alongside one-time migration from legacy `AssemblySpec`.

#### What It Solved

- Closed `services/tasks._assembly_gate` bypassing validation via legacy `check_graph_inputs`, eliminated mutable `AssemblySpec`/`CheckResult` persistence, rejected host paths/executables/unpinned versions, and moved assembly diagnostic registration to static core tables.

#### Migration and Compatibility Impact

- Retired legacy assembly migrators, migration receipts, and runtime dual tracks; new snapshots bind canonical text, validation receipts, and exact ID/revision refs.

### `0.6.0` Development Milestone — Device Data File Contract

**Completed**: 2026-08-24

**Release Status**: Milestone advanced `0.5.0` → `0.6.0`; merged to `master`, unreleased.

#### What Changed

- Published `ies.device-data` `1.0.0` schema and unique pure normalizer standardizing CSV metadata, UTF-8/LF/RFC 4180 dialect, timelines, units, numbers, and missing-value bans (`fixed_offset` explicit UTC offset, `periodic` cycle declaration historically), migrating built-in loads with `migration-receipt-0.6.0.json`.

#### What It Solved

- Eliminated machine timezone/DST nondeterminism, floating-point precision loss, `NaN`/`Inf`, spreadsheets/formulas, divergent GUI vs. package parsing, and silent 500/empty-set fallbacks.

#### Migration and Compatibility Impact

- Retained built-in `electric_load`/`heat_load`/`cooling_load` data rows with validated metadata and project-relative CSV references.

### `0.5.0` Development Milestone — Device Model Contract

**Completed**: 2026-08-24

**Release Status**: Milestone advanced `0.4.0` → `0.5.0`; merged to `master`, unreleased.

#### What Changed

- Published initial device model schema (`1.0.0` draft later superseded by `2.0.0` pure technical YAML), migrating from `type_id`/`function` to structured metadata, resolving `<command-id>@<exact-version>` inside composition root/modeling, deeply freezing descriptors, and exposing `get_profile_columns(type_id)`.

#### What It Solved

- Stripped Python functions, modules, and host paths from public descriptors, rejected unmappable legacy `function` strings and unpinned commands, and fixed `MappingProxyType` compatibility in `0.6.0` consumers.

#### Migration and Compatibility Impact

- Required one-time migration of legacy YAML without silent fallback, removing `standard_csv_path` and implementation paths from public descriptors.

### `0.4.0` Development Milestone — Storage Public Boundary

**Completed**: 2026-08-23

**Release Status**: Milestone advanced `0.3.0` → `0.4.0`; merged to `master`, unreleased.

#### What Changed

- Removed `storage_path` and `ref_count` from `ObjectHandle`, standardized `iesplan.storage` on `ObjectId`/`ObjectHandle`/`ObjectOwner`, replaced `ImportProposal.source_path` with `source_object_id` + revisions, and scrubbed internal paths from reconcile/dry-run/health/error payloads.

#### What It Solved

- Eliminated coupling to local paths and ref counts, prevented host path leakage, and enabled swapping storage adapters without altering business contracts.

#### Migration and Compatibility Impact

- Callers MUST access objects via `ObjectId`, revisions, and owner refs rather than storage paths or ref counts.

### `0.3.0` Development Milestone — Error and Protocol Contracts

**Completed**: 2026-08-23

**Release Status**: Milestone advanced `0.2.0` → `0.3.0`; merged to `master`, unreleased.

#### What Changed

- Unified all non-2xx responses behind the standard error envelope (`code`, `message_key`, `severity`, `blocking`, `params`, `location`, `fix_hint_key`, `ref_ids`), made `Diagnostic` deeply immutable with `replace()`/`with_context()`/`to_dict()`, added explicit `has_graph` and `evidence_status` (`no_evidence` / `RES-MISS-003`), standardized config/dataset/FastAPI validation envelopes, and introduced public protocol baselines and static architecture gates.

#### What It Solved

- Removed bare `detail` vs. custom error splits, prevented missing models/evidence from posing as valid empty results, stopped cross-layer diagnostic mutation, and prevented architecture/protocol drift.

#### Migration and Compatibility Impact

- Clients consume the standard `error` envelope (`params.diagnostics`) and check `evidence_status` / `404` instead of equating empty arrays with missing evidence.

### `0.2.0` Development Milestone — Security and Administrative Hardening

**Completed**: Implementation completed 2026-08-22; docs and acceptance closed 2026-08-23.

**Release Status**: Milestone advanced `0.1.0` → `0.2.0`; merged to `master`, unreleased.

#### What Changed

- Added Origin/Referer CSRF verification on cookie-authenticated `POST`/`PUT`/`PATCH`/`DELETE`, object/project/user authorization checks on download tokens, project-task ownership checks on result queries, and exception masking on `/api/readyz`.
- Added impact previews, signed confirmation tokens, and name confirmation on dangerous admin/project operations; introduced 7-day soft-delete retention/restore/purge/reconcile for orphan objects (later simplified on 2026-11-21); added IP rate limits, quotas, metadata allowlists, PostgreSQL immutability triggers across 12 critical tables, and redacted config audit logs.

#### What It Solved

- Closed browser CSRF, forged download tokens, cross-project task probing, accidental cascading admin deletions/unlocks, irreversible accidental purges, upload abuse, and diagnostic leakage while enforcing immutability in PostgreSQL.

#### Migration and Compatibility Impact

- Browser state mutations MUST supply trusted origins; admin dangerous actions follow confirmation workflows; PostgreSQL init idempotently installs audit triggers.

## `0.1.0` — 2026-08-22 — Development Baseline

> Release Date: 2026-08-22; Release Status: Pre-stability development baseline

### Added

- Established the three parallel official portals: User Guide, Developer Guide, and Changelog.
- Created novice-friendly GUI manuals covering projects, modeling, data, configuration, tasks, results, and administration.
- Created architectural, domain-traceability, public-contract, extension, frontend, deployment, contribution, and release blueprints.
- Established semantic-versioned Roadmap and release gates.
- Introduced hand-authorable `1.0.0` target standards for device model YAML, device data CSV, assembly YAML, and generator-produced Solver Bundles.
- Created Computation Generators and Solver Runtime module manuals detailing formulation, structured commands, isolated execution, and result adaptation.

### Changed

- Refactored root `README.md` highlighting multi-energy modeling, edge-port physical semantics, modular composition, and planning analysis with planned highlights clearly tagged.
- Positioned local AI as intelligent data preparation and one-click planning report generation rather than a standalone Roadmap feature.
- Designated `backend/pyproject.toml` as the sole authoritative source of the product version.
- Unified official documentation around `manual/` published directly by the Help Center.
- Restricted `docs/` to Reviews while moving legacy specs, contracts, roadmaps, design inputs, and workflows into read-only archives.
- Distilled stable architectural review decisions into the Developer Guide, superseding conflicting legacy specs.
- Expanded the Developer Guide into per-module manuals covering bootstrap, core, devices, modeling, assembly, computation, finance, analysis, storage, application, API, Worker, and persistence.
- Refined target computation boundary via ADR-0003 to `ValidatedAssemblyArtifact → GeneratorProvider → Solver Bundle → SolverRuntime → ResultAdapter → ComputeResult`, excluding commands from assembly and placing unit conversion in `GeneratorProvider`.
- Scoped the Roadmap exclusively to unimplemented work, moving accepted items immediately to `Unreleased` without keeping completion checkboxes or copies.
- Started post-`0.1.0` Roadmap planning at `0.2.0` without shifting scheduled release numbers when merging AI features.

### Current Implementation Notes

- This baseline updated documentation and blueprints only without changing device loading, CSV import, assembly, computation, or Worker code.
- The four `1.0.0` file contracts and generator/runtime boundaries represented effective target contracts whose implementation progressed incrementally along the `0.x` Roadmap.

### Fixed

- Fixed broken relative links in the Simplified Chinese User Guide.
- Removed or corrected claims exceeding actual GUI capabilities (standalone version publishing, package import, manual assessment persistence, and unexposed admin actions).
- Clarified distinctions among product version, project version, dataset version, config revision, calculation snapshot, and result evidence.

### Removed

- Revoked active normative status from historical specs, implementation plans, and workflows (retained inside `docs/archive/`).

### Existing Capabilities

- User sign-in, first-login password change, project creation, and access control;
- Graphical system modeling, dataset upload/binding, calculation configuration, and full preflight validation;
- Calculation, planning optimization, uncertainty analysis, and batch sweep tasks;
- Results, four-dimensional system assessment, Excel reports, and full project package exports;
- Bilingual Chinese/English UI and Simplified Chinese Help Center;
- Dockerized API, Worker, frontend, database, cache, and object storage runtime.

### Current Boundaries

Established the versioned development baseline prior to security hardening, public contract convergence, historical traceability, scheme application, project versioning GUI, package import restoration, and end-to-end verification along the Roadmap—not a `1.0.0` release candidate.
