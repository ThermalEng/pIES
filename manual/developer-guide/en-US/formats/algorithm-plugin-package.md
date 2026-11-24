# Algorithm Plugin Package

> Contract ID: `ies.algorithm-plugin`; Target `schema_version`: `"1.0.0"`; Manifest File: `plugin.yaml`; Filename Extension: `*.algorithm.zip`; Target Product Version: `1.1.5` (unified built-in contract & ephemeral container foundation) / `1.2.0` (external plugin import, automated image build & market distribution)

The Algorithm Plugin Package defines the unified packaging and runtime contract for all computable extensions in pIES. **Both official built-in computation stages (predictor, generator, solver, result adaptation & post-solve financial evaluation) and local/market third-party plugins follow this exact contract**, executed by `backend` orchestration through the allowlisted container proxy `job_runner` as per-step network-disabled ephemeral containers—ensuring Web/API control-plane processes never load or execute user Python code.

## Five Plugin Kinds (`kind`)

| `kind` | Pipeline Stage | Typical Responsibilities and Entry Shape |
|---|---|---|
| `predictor` | Prediction stage | Consumes historical series and parameters to output predicted series and training receipts (coupled variants integrate with the internal-loop interface) |
| `generator` | Generation stage | Consumes frozen assembly, series, finance, and calculation config to emit a canonical `Solver Bundle` |
| `solver` | Solving stage | Consumes a `Solver Bundle` and produces raw solver outputs and execution logs |
| `evaluator` | Result adaptation & post-solve evaluation | Consumes the `Bundle`, raw solution, and frozen finance/evaluation baseline to emit `ComputeResult` and financial evaluation facts |
| `algorithm` | Minimal unified single-step algorithm | Skips generator/solver split; a single entry function consumes the standard frozen input package and outputs canonical results |

### Standard Input/Output Layout and Composition Rules

| `kind` | `/work/in` Standard Inputs | `/work/out` Standard Outputs | Composition Compatibility Rules |
|---|---|---|---|
| `predictor` | `/work/in/request.json` and historical series | `/work/out/prediction.json` and training artifact | Precedes `generator` or unified `algorithm` |
| `generator` | `/work/in/` frozen assembly, series, finance, and calc config | `/work/out/bundle/` standard `Solver Bundle` | MUST match the selected `solver` contract and media types |
| `solver` | `/work/in/bundle/` (read-only `Solver Bundle`) | `/work/out/solution.*` declared outputs | Declared compatible by the chosen `generator` and verified at submission |
| `evaluator` | `/work/in/bundle/`, raw solution, receipt, and frozen finance/baseline | `/work/out/result.json` (`ComputeResult` and financial evaluation facts) | Consumes paired `Bundle` declarations and receipt under the public result contract |
| `algorithm` | `/work/in/` standardized frozen computation input package | `/work/out/result.json` (canonical result file) | Replaces the `generator + solver` split; feeds the default `evaluator` or emits canonical results directly |

## ZIP Directory Layout (Simplicity First, Complex Packages Supported)

The archive root MUST NOT contain an extra wrapper directory and accepts regular files and directories only (symlinks, hardlinks, absolute paths, `..` traversal, case collisions, and encrypted entries are prohibited).

### Minimal Valid Layout (Local Offline Testing & Simple Algorithms)

```text
my-plugin/   # Pack contents at the ZIP root without this wrapper folder
├── plugin.yaml
├── main.py
└── requirements.txt   # Optional; omit when using only pre-installed base packages
```

### Full Layout (Complex Packages or Public Market Submissions)

```text
plugin.yaml
requirements.txt       # Optional; extra pure-Python dependencies
src/                   # Optional; multi-module Python package
  __init__.py
  solver.py
tests/                 # Required for public market submissions; >= 1 offline self-test
  minimal/
    input.json
    expected.json
LICENSE                # Required for public market submissions
README.md              # Required for public market submissions
```

## `plugin.yaml` Manifest

The Manifest uses the YAML 1.2 safe subset. Standard single-step algorithms and predictors declare core metadata and inline `parameters_schema`; built-in or high-performance solvers MAY declare optional `advanced` and `iteration` sections.

### Minimal Plugin Example

```yaml
schema: ies.algorithm-plugin
schema_version: "1.0.0"
plugin_id: acme.algorithm.capacity_search
version: "1.2.0"
kind: algorithm
name:
  zh_cn: 容量搜索算法
  en_us: Capacity search
description: Restricted search for continuous and integer capacity variables.
license: MPL-2.0
compatibility:
  pies: ">=1.1.5,<2.0.0"
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
  - id: minimal
    input: tests/minimal/input.json
    expected: tests/minimal/expected.json
extensions: {}
```

### Core and Optional Extension Fields

| Field | Description and Constraints |
|---|---|
| `plugin_id` / `version` | Stable namespaced ID (`ies.*` / `<publisher>.*`) and three-part semantic version |
| `kind` | Closed enum: `predictor` / `generator` / `solver` / `evaluator` / `algorithm` |
| `compatibility` | Compatible pIES or SDK version range |
| `runtime` | Python version and optional relative `requirements` file; omitting extra dependencies reuses the official base environment directly |
| `entrypoint` | Fixed `<module.path>:<public_function>` format, supporting single-file `main:solve` or package paths `src.solver:solve` |
| `parameters_schema` | Inline Draft 2020-12 JSON Schema (`additionalProperties: false`), driving dynamic forms and submission validation |
| `resources` | Default and maximum resource requests (`cpu`, `memory_mb`, `timeout_seconds`, `process_limit`), capped by deployment policy ceilings |
| `advanced` | **Optional advanced extension**: declares chunked streaming `Solver Bundle` layouts, external solver binary argument conventions, and paired stage refs (shared by built-in HiGHS and future high-performance solvers) |
| `iteration` | **Optional internal-loop interface**: configures algorithms executing an internal iterative loop (see below) |
| `tests` | Network-free minimal self-test cases (required for public market submissions) |

## Dedicated Internal-Loop Interface (`iteration`)

Algorithms with internal iterative loops (such as coupled prediction-solving or iterative heuristics) **execute self-contained inside a single ephemeral container** rather than cold-starting containers on every iteration. Plugins opt into unified progress and convergence reporting by declaring `iteration` in `plugin.yaml`:

```yaml
iteration:
  enabled: true
  max_iterations_default: 20
  convergence_metric: max_relative_delta
  progress_channel: /work/out/iterations.jsonl
```

- **Input convention**: The container reads frozen `tolerance`, `max_iterations`, and overall time budget from `/work/in/request.json`;
- **Per-round reporting**: After each iteration, the container appends one NDJSON record (`iteration`, `metric`, `value`, `unit: "1"`, status) to `/work/out/iterations.jsonl`;
- **Budget and cancellation**: Internal loops remain strictly bounded by the single container timeout and external stop signals, writing final outputs upon convergence or iteration cap before exiting cleanly.

## Container Execution Contract (`/work/in → /work/out`) and Image Builds

- **Automated immutable image build at install time**: When importing or installing a third-party plugin, `job_runner` applies the official standard template on top of `pies-plugin-base` to build an immutable computation image `pies-plugin/<plugin_id>:<version>` without requiring authors to write Dockerfiles (dependency-free plugins reuse the base layer directly). Dependency installation and runtime execution are strictly separated.
- **Network-disabled runtime handoff**: For each computation step, `backend` writes frozen inputs to a per-attempt directory and requests `job_runner` to launch an ephemeral container (`--network=none --read-only --cap-drop=ALL` with hard cgroup limits):
  - `/work/in` (read-only mount): Standardized frozen input package (JSON / chunked streams), parameters, and seed;
  - `/work/out` (read-write mount): Declared output artifacts, bounded logs, and optional `/work/out/iterations.jsonl`.
- **Result boundary & external verification**:
  - Containers follow the uniform lifecycle in [Deployment and Operations](../deployment.md#ephemeral-container-isolation-and-resource-observation) (natural exit / cancel / timeout → collect exit status & declared outputs → persist valid artifacts → `cleanup_stage` cleanup of container and temp files); declared outputs MUST pass strict public result-contract validation (structure, field types, finite numeric values, and reference consistency) before entering pIES result models, and **the platform does not endorse or certify the mathematical optimality or financial correctness of third-party plugins** (correctness responsibility rests with plugin authors and selectors);
  - Archive safety and `sha256` digests are verified once at the external market download or offline import boundary; once ingested, pIES never stores, recomputes, or compares SHA-256 internally;
  - Changing entry points, schemas, units, or algorithm semantics MUST bump `MAJOR`; backward-compatible additions bump `MINOR`; bug fixes bump `PATCH`. Released versions MUST NOT be overwritten silently.
  - Prior to `2.0.0`, pIES maintains a single current contract without multi-version compatibility matrices; when a plugin's declared `compatibility` does not match the running pIES version, install and submission gates MUST fail with an explicit diagnostic rather than failing silently.
