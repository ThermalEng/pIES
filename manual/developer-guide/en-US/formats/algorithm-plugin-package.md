# Algorithm Plugin Package

> Contract ID: `ies.algorithm-plugin`; Target `schema_version`: `"1.0.0"`; Manifest File: `plugin.yaml`; Filename Extension: `*.algorithm.zip`; Target Product Version: `1.1.5` (unified built-in contract & ephemeral container foundation) / `1.2.0` (single-chain replaceable handoffs, controlled build/self-test gate & open market distribution)

The Algorithm Plugin Package defines the unified packaging and runtime contract for all computable extensions in pIES. **Both official built-in computation stages and local/market third-party plugins follow this exact contract**, executed by `backend` orchestration through the allowlisted proxy `job_runner` along a **strict single-pass unidirectional chain** (`Frozen Input → [predictor] → (generator → solver | algorithm) → [evaluator] → Result`) as network-disabled ephemeral containers—ensuring Web/API control-plane processes never load or execute user Python code.

## Five Plugin Kinds (`kind`) and Single-Chain Handoff Contract

Each stage's output directory (`/work/out`) serves directly as the sole input directory (`/work/in`) of the immediately following stage—**cross-stage side-channel directory mounts and cross-container loops are strictly prohibited**; cross-stage mappings and frozen context travel inside explicit `passthrough` fields carried **verbatim** by intermediate stages (with standard `pies-sdk` passthrough helpers):

| `kind` | `/work/in` Single-Chain Input | `/work/out` Single-Chain Output | Replaceable Composition & Contract IDs (`consumes` / `produces`) |
|---|---|---|---|
| `predictor` | `/work/in/request.json` (all correlated `targets[]` routed to this plugin, historical series, per-target parameters) plus upstream `passthrough` | `/work/out/prediction.json`, training artifacts, and merged single-chain downstream input package | Natively supports single-pass multi-target joint prediction (preserving shared latent weather/solar correlations across load and renewables) with no solver-coupled cross-container outer loop |
| `generator` | Frozen assembly, materialized series, calculation config, and frozen finance/evaluation context from upstream | Solver input files (e.g., `problem.mps`), declarative column-to-business-variable/unit `variable_map`, and `passthrough` (frozen `finance` & `evaluation`) | Emits a public bundle contract (e.g., `ies.solver-bundle.mps@1.0.0`) consumable by any `solver` declaring that contract |
| `solver` | **Reads solely `generator`'s output directory** (model files, `variable_map`, `passthrough`, and solver options) | Solver-neutral canonical primal solution (`primal_solution.json`: unified status enum, objective value, column values, receipt metadata) + **verbatim** `variable_map` and `passthrough` | Consumes matching `generator` contract and emits `ies.primal-solution@1.0.0`; performs zero business unit conversion so third-party `.mps` solvers drop-in replace built-in HiGHS |
| `algorithm` | Standardized frozen computation input package from upstream (with algorithm options and `passthrough`) | Business-unit canonical engineering solution (`solution.json`: capacities, build selections, step series, solver summary) + **verbatim** `passthrough` | Strictly replaces the `generator + solver` split, emitting `ies.engineering-solution@1.0.0` without crossing upstream `predictor` or downstream `evaluator` boundaries |
| `evaluator` | **Reads solely the immediately preceding stage's (`solver` or `algorithm`) output directory** (never side-mounting `generator`'s directory) plus evaluator options | `/work/out/result.json` (`ComputeResult`, step series, and same-basis `ies.financial-evaluation@1.0.0` evaluation facts) | Normalizes `{primal_solution + variable_map}` and `{engineering_solution}` uniformly via `pies-sdk` and evaluates forward cash flows from `passthrough`; skipped when `CalculationConfig.evaluator` is `null` |

## ZIP Directory Layout and Dual-Form Self-Test Fixtures

The archive root MUST NOT contain an extra wrapper directory and accepts regular files and directories only (symlinks, hardlinks, absolute paths, `..` traversal, case collisions, encrypted entries, and bundled `Dockerfile`s are prohibited).

### Minimal Valid Layout (Local Authoring & Simple Algorithms)

```text
my-plugin/   # Pack contents at the ZIP root without this wrapper folder
├── plugin.yaml
├── main.py
└── requirements.txt   # Optional; omit when using only base packages (zero-network build)
```

### Full Layout (Supporting Both Single-JSON Shorthand & Multi-File Directory `tests`)

Public market submissions require `README.md`, `LICENSE`, and at least one network-free `tests` fixture. To support both lightweight plugins and multi-file `generator` / `solver` bundles cleanly, `tests` accepts both **single-file JSON shorthand** and **full `/work/in → /work/out` directory fixtures**:

```text
plugin.yaml
requirements.txt       # Optional; extra pure-Python dependencies
src/
  __init__.py
  solver.py
tests/
  minimal_dir/         # Directory form: in/ maps verbatim to /work/in/, expected/ checks /work/out/
    in/
      problem.mps
      variable_map.json
      passthrough.json
    expected/
      primal_solution.json
  simple_json/         # Single-file shorthand: input.json maps to /work/in/request.json
    input.json
    expected.json
LICENSE
README.md
```

## `plugin.yaml` Manifest

The Manifest uses the YAML 1.2 safe subset. Plugins declare consumed and produced public data contract IDs via `consumes` and `produces` so cross-author plugins compose and substitute freely.

### Plugin Manifest Example

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
| `consumes` / `produces` | Public input/output data contract IDs used by the submission gate to verify stage composition compatibility (incompatible pairs are rejected at submission without runtime guessing) |
| `runtime` | Python version and optional relative `requirements` path; omitting `requirements` reuses `pies-plugin-base` with zero network access |
| `entrypoint` | Fixed `<module.path>:<public_function>` format |
| `parameters_schema` | Inline Draft 2020-12 JSON Schema (`additionalProperties: false`) governing this stage's dedicated parameter slot (`predictions[].parameters` or `options.<kind>`) without cross-stage key collisions |
| `resources` | Default and maximum resource requests (`cpu`, `memory_mb`, `timeout_seconds`, `process_limit`), capped by deployment policy ceilings |
| `advanced` | **Optional advanced section**: streaming bundle flags, trusted solver binary argument templates, and **optional** `companions` restrictions (used only when genuinely coupled to a specific companion rather than hardcoding mutual IDs) |
| `iteration` | **Optional internal-loop interface**: single-container iterative progress reporting (see below) |
| `tests` | Network-free self-test fixtures (single JSON or `in/` + `expected/` directory form; required for public market submissions) |

## Dedicated Internal-Loop Interface (`iteration`)

Algorithms containing iterative search loops (such as metaheuristics, simulation-optimization, or intra-solver multi-round recovery) **execute self-contained inside a single ephemeral container** rather than looping across containers. Declaring `iteration` enables unified progress tracking:

```yaml
iteration:
  enabled: true
  max_iterations_default: 20
  convergence_metric: max_relative_delta
  progress_channel: /work/out/iterations.jsonl
```

- After each internal iteration, the container appends one NDJSON record (`iteration`, `metric`, `value`, `unit: "1"`, status) to `/work/out/iterations.jsonl`, bounded strictly by the single container timeout and external stop signals.

## Build Trust Boundary, Network-Disabled Self-Test, and Execution Lifecycle

- **Install-time build trust boundary & network-disabled self-test gate**:
  - Dependency-free plugins MUST build with `--network=none` directly atop `pies-plugin-base`; plugins with `requirements` build inside `job_runner`'s controlled in-memory context (zero host business mounts, zero business credentials, bounded build timeout) using optional `pip_index_url` or default PyPI (marking `build_failed` with an explicit admin-contact warning if offline dependency download fails);
  - Immediately after build, any declared `tests` MUST pass inside a `--network=none` container before advancing status to `ready`. `pies-sdk validate` and `pies-sdk pack` uniformly support all five plugin kinds.
- **Runtime single-chain isolation & non-endorsement boundary**:
  - Every stage runs as a non-root `--network=none --read-only --cap-drop=ALL` ephemeral container (read-only `/work/in`, read-write `/work/out`) cleaned up via `cleanup_stage` after valid outputs are persisted;
  - Declared outputs undergo strict public result-contract validation prior to ingestion; **both the platform and self-test gate verify contract compliance only and never endorse mathematical optimality or engineering/financial correctness**;
  - External digests obey the three-way boundary rule (mandatory `index.json` `sha256` match on market downloads, supplied-digest match when provided offline, archive/contract checks only on bare offline packages), with zero internal SHA-256 storage or recomputation afterward;
  - Admin removal of `pies-plugin/*` images performs a stateless active-task check: `delete_plugin_image` executes only when zero non-terminal tasks reference that exact version, otherwise delisting for new tasks while prompting retry after in-flight runs finish.
