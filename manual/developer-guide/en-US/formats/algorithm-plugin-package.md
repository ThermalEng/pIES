# Algorithm Plugin Package

> Contract ID: `ies.algorithm-plugin`; `schema_version`: `"1.0.0"`; Manifest File: `plugin.yaml`; Filename Extension: `*.algorithm.zip`; Effective Product Version: `1.2.0` (framework zero-copy handoffs, runtime engineering-solution restoration, parallel self-describing evaluators & open market distribution)

The Algorithm Plugin Package defines the unified packaging and runtime contract for all computable extensions in pIES. **Both official built-in computation stages and local/market third-party plugins follow this exact contract**, executed by `backend` orchestration through the allowlisted proxy `job_runner` along a **strict single-pass unidirectional pipeline** as network-disabled ephemeral containers—ensuring control-plane processes never load or execute user Python code.

## Five Plugin Kinds (`kind`) and Framework Unidirectional Handoff Protocol

The pipeline topology is fixed to:
`Frozen Input → [predictions[]: grouped per predictor plugin in parallel] → (generator → solver → backend bounded-subprocess engineering restoration | algorithm) → [evaluators[]: 0..N evaluators in parallel] → Sealed Result`.

**Single-in/single-out containers & framework zero-copy handoffs**: Ephemeral containers run solely single-input (`/work/in`), single-output (`/work/out`) pluggable computation stages and **never relay unrelated upstream files**; cross-stage read-only immutable context (frozen assembly, materialized series/prices, finance/evaluation confirmations) is staged into `/work/in` via zero-copy hardlinks (`os.link`) by the `backend` orchestrator, while cross-step primal-to-engineering restoration executes inside an isolated resource-bounded `backend` child subprocess (`spawn` streaming file I/O, immediately reclaiming memory upon exit):

| `kind` | `/work/in` Framework-Staged Read-Only Input | `/work/out` Plugin's Sole Output | Composition & Contract IDs (`consumes` / `produces`) |
|---|---|---|---|
| `predictor` | All correlated `targets[]` routed to this plugin, historical series, and per-target `parameters` | `/work/out/prediction.json` and training artifacts | Grouped by selected plugin within the logical prediction stage; targets sharing a plugin run jointly in one pass (preserving shared latent correlations), and the framework merges all predicted series |
| `generator` | Frozen assembly, full materialized series, frozen finance/evaluation context, and `options.generator` | Solver model files (e.g., `problem.mps`) and universal public `variable_map` (declaring 0-based column indices, target variable identities, and linear unit conversions) | Emits a public bundle contract (e.g., `ies.solver-bundle.mps@1.0.0`) and performs the sole forward conversion from business units to solver internal units |
| `solver` | Solver model files from `generator` and `options.solver` (no finance or assembly files needed) | Solver-neutral canonical primal solution (`primal_solution.json`: unified status enum, objective value, column values strictly in **0-based input model column order**, receipt metadata)—**emitting internally permuted column orders or copying `variable_map`/finance files is prohibited** | Consumes matching `generator` contract and emits `ies.primal-solution@1.0.0`; upon exit, `backend`'s bounded subprocess faithfully restores the canonical engineering solution via `variable_map` (never re-solving third-party solvers), working universally across any compatible `generator × solver` pair |
| `algorithm` | Frozen assembly, full materialized series/prices, frozen finance/evaluation context, and `options.algorithm` | Business-unit canonical engineering solution (`solution.json`: `ies.engineering-solution@1.0.0`) | Strictly replaces `generator + solver`; emits the **exact same canonical engineering solution schema** produced after staged restoration (`evaluators: []` is valid in both modes) |
| `evaluator` | Feasible canonical engineering solution (`ies.engineering-solution@1.0.0`) + framework-hardlinked read-only frozen context (`ValidatedAssemblyArtifact`, materialized `prices`, `EffectiveFinanceConfig`, `EvaluationInput`) + this evaluator's `parameters` | Official financial evaluator emits canonical `ies.financial-evaluation@1.0.0`; third-party evaluators emit self-describing `evaluation_report.json` (`metrics` / `charts` / `tables`) | Supports `0..N` evaluators running independently in parallel with per-evaluator checkpoints; an individual evaluator failure preserves the sealed engineering solution and successful evaluation blocks, supporting isolated single-evaluator retry |

### Canonical Engineering Solution (`ies.engineering-solution@1.0.0`), Non-Feasible Exemption & Parallel Evaluation Rules

- **Feasible vs. non-feasible dual-state contract**:
  - When core solving finds a usable solution (`math_status` in `solved` or `feasible`, i.e., `has_feasible_solution: true`), the solution MUST provide: ① `construction_units` (`built` boolean and business-unit values for every `planning_variable_id` across all planning construction units); ② `series` / `system_series` (contiguous full-horizon business-unit step series covering all `meter_bindings`, variable O&M, and named amounts); ③ `solver_summary` plus optional `metrics` (missing required entries fail explicitly without silent zero-filling).
  - When core solving yields no feasible solution (`infeasible` / `unbounded` / `not_solved` / `numeric_failure`, i.e., `has_feasible_solution: false`), only `solver_summary` is required (`construction_units` and `series` are omitted/empty so valid infeasibility or unsolved timeouts are never misclassified as protocol errors), and the orchestrator **skips all solution-dependent downstream `evaluators[]`**.
- **Parallel evaluator partial-failure isolation & single-evaluator retry**: When a feasible engineering solution is produced, the top-level task `business_outcome` reflects only the core solve (`solved → normal_completion`, `feasible → restricted_results`); each `evaluator` owns an independent checkpoint and resource quota so a failing evaluator never discards the sealed engineering solution or successful evaluation blocks—rendering an isolated error card on the results page with a **single-evaluator retry action** (re-running only that failed evaluator against the same frozen `CalcSnapshot` and sealed engineering solution).
- **Self-describing evaluation report block (`ies.evaluation-report@1.0.0`)**: Third-party `evaluator` plugins (such as carbon accounting, power/heat reliability, exergy efficiency, or overseas PPA finance) output self-contained titles, `metrics` (`{id, label, value, unit}`), `charts` (axis labels, units, and series arrays), and `tables` (column schemas, units, and row arrays), decoupling evaluators completely from fixed frontend financial tabs.

## ZIP Directory Layout and Dual-Form Self-Test Fixtures

The archive root MUST NOT contain an extra wrapper directory and accepts regular files and directories only (symlinks, hardlinks, absolute paths, `..` traversal, case collisions, encrypted entries, and bundled `Dockerfile`s are prohibited). Public market submissions require `README.md`, `LICENSE`, and at least one network-free `tests` fixture, supporting both **single-file JSON shorthand** and **full `/work/in → /work/out` directory fixtures**:

```text
plugin.yaml
requirements.txt       # Optional; omit when using only base packages (enforces --network=none build)
src/
  __init__.py
  solver.py
tests/
  minimal_dir/         # Directory form: in/ maps to /work/in/, expected/ checks /work/out/
    in/
      problem.mps
    expected/
      primal_solution.json
  simple_json/         # Single-file shorthand: input.json maps to /work/in/request.json
    input.json
    expected.json
LICENSE
README.md
```

## `plugin.yaml` Manifest

The Manifest uses the YAML 1.2 safe subset, declaring consumed and produced public data contract IDs via `consumes` and `produces`:

```yaml
schema: ies.algorithm-plugin
schema_version: "1.0.0"
plugin_id: acme.algorithm.capacity_search
version: "1.2.0"
kind: algorithm
name:
  zh_cn: 容量搜索算法
  en_us: Capacity search
description: Restricted heuristic search for continuous and integer capacity variables.
license: MPL-2.0
compatibility:
  pies: ">=1.2.0,<2.0.0"
consumes:
  - ies.stage-input@1.0.0
produces:
  - ies.engineering-solution@1.0.0
runtime:
  language: python
  python: "3.12"
  requirements: requirements.txt
entrypoint: main:solve
parameters_schema:
  type: object
  additionalProperties: false
  properties:
    max_steps:
      type: integer
      minimum: 1
      default: 100
resources:
  default:
    cpu: 1
    memory_mb: 512
    timeout_seconds: 300
    process_limit: 8
  maximum:
    cpu: 2
    memory_mb: 2048
    timeout_seconds: 3600
    process_limit: 32
tests:
  - id: simple_json
    input: tests/simple_json/input.json
    expected: tests/simple_json/expected.json
extensions: {}
```

### Core and Extension Fields

| Field | Description and Constraints |
|---|---|
| `plugin_id` / `version` | Stable namespaced ID (`ies.*` / `<publisher>.*` / local `user.<public_namespace>.*`) and three-part semantic version |
| `kind` | Closed enum: `predictor` / `generator` / `solver` / `evaluator` / `algorithm` |
| `compatibility` | Compatible pIES or SDK version range; mismatches fail explicitly at install and submission gates |
| `consumes` / `produces` | Public input/output data contract IDs used by the submission gate to verify stage composition compatibility |
| `runtime` | Python version and optional relative `requirements` path; omitting `requirements` reuses `pies-plugin-base` with `--network=none` |
| `entrypoint` | Fixed `<module.path>:<public_function>` format |
| `parameters_schema` | Inline Draft 2020-12 JSON Schema (`additionalProperties: false`) governing this plugin's dedicated parameter slot (`predictions[].parameters`, `options.<kind>`, or `evaluators[].parameters`) |
| `resources` | Default and maximum resource requests, capped by deployment policy ceilings |
| `advanced` | **Optional advanced section**: streaming bundle flags, trusted solver binary argument templates, and **optional** `companions` restrictions |
| `iteration` | **Optional internal-loop interface**: single-container iterative progress reporting (appending NDJSON to `/work/out/iterations.jsonl`) |
| `tests` | Network-free self-test fixtures (single JSON or `in/` + `expected/` directory form; required for public market submissions) |

## Build Trust Boundary, Network-Disabled Self-Test, and Execution Lifecycle

- **Install-time build trust boundary & single-index rule**:
  - Dependency-free plugins MUST build with `--network=none` directly atop `pies-plugin-base`; plugins with `requirements` build inside `job_runner`'s controlled in-memory context (zero host business mounts, zero business credentials, bounded build timeout); `pip_index_url` uses default public PyPI when unset and **passes solely `--index-url <pip_index_url>` when set** (never appending `--extra-index-url`, preventing `pip` multi-index non-determinism and enterprise dependency confusion while leaving upstream PyPI caching/fallback to the mirror service itself); dependency installation failures mark `build_failed` and surface an explicit admin-contact warning;
  - Immediately after build, any declared `tests` MUST pass inside a `--network=none` container before advancing status to `ready`. `pies-sdk validate` and `pies-sdk pack` uniformly support all five plugin kinds.
- **Runtime isolation, shared-truth financial evaluation & race-free stateless uninstall**:
  - Every stage runs as a non-root `--network=none --read-only --cap-drop=ALL` ephemeral container; the official financial evaluator reuses shared `finance` domain rules for full forward evaluation on canonical engineering solutions (maintaining exact mathematical parity with the built-in generator's objective), while **the platform and self-test gate verify public contract compliance only—never re-checking physical optimality or endorsing third-party calculation correctness**;
  - External digests obey the three-way boundary rule with zero internal SHA-256 storage afterward;
  - Plugin disable/uninstall and new task submissions share transactional row-level locking to prevent concurrent TOCTOU races, invoking `delete_plugin_image` only when zero non-terminal tasks reference that exact version.
