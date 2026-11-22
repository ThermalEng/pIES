# Algorithm Plugin Package

> Contract ID: `ies.algorithm-plugin`; Target `schema_version`: `"1.0.0"`; Filename Extension: `*.algorithm.zip`; Target Product Version: `1.1.0`

Algorithm Plugin Packages bundle a user-defined canonical assembly processor ("generator") and solver algorithm into a single immutable release validated and executed inside an isolated containerized runner, ensuring Web/API and ordinary Worker processes never load user Python code.

Version 1 supports pure Python source and offline dependency locks only; native binaries, dynamic shared libraries, embedded container images, install scripts, Git/URL dependencies, and runtime downloads are rejected.

## Fixed ZIP Directory Layout

The archive root MUST NOT contain an extra wrapper directory and accepts regular files and directories only (symlinks, hardlinks, absolute paths, `..` traversal, case collisions, and encrypted entries are prohibited):

```text
algorithm.plugin.yaml
requirements.lock
src/
  generator.py
  algorithm.py
schemas/
  options.schema.json
  result.schema.json
tests/
  minimal/
    assembly.json
    resources/
    expected.json
LICENSE
README.md
```

## `algorithm.plugin.yaml` Manifest

The Manifest uses the YAML 1.2 safe subset:

```yaml
schema: ies.algorithm-plugin
schema_version: "1.0.0"
plugin_id: acme.algorithm.capacity_search
version: "1.2.0"
name:
  zh_cn: 容量搜索算法
  en_us: Capacity search
description: Restricted search for continuous and integer capacity variables.
license: MPL-2.0
runtime:
  language: python
  python: "3.12"
  dependency_lock: requirements.lock
generator:
  entrypoint: src.generator:generate
  deterministic: true
  supported_assembly_schema:
    - "1.0.0"
algorithm:
  entrypoint: src.algorithm:solve
  modes:
    - planning
  variable_types:
    - continuous
    - integer
  supports_seed: true
  options_schema: schemas/options.schema.json
result:
  schema: schemas/result.schema.json
  adapter: ies.result.user_algorithm@1.0.0
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
    input: tests/minimal/assembly.json
    expected: tests/minimal/expected.json
extensions: {}
```

| Field | Description and Constraints |
|---|---|
| `plugin_id` / `version` | Stable namespaced ID and three-part semantic version |
| `runtime` | Supported Python version and in-package `requirements.lock` path (restricted to pure-Python wheels permitted by the deployment offline repository) |
| `generator.entrypoint` / `algorithm.entrypoint` | Fixed `<package.module>:<public_function>` format; modules MUST reside under `src/` |
| `options_schema` / `result.schema` | Strict JSON Schemas rejecting unknown fields by default; `result.adapter` selects the unified system adapter version |
| `resources` | Suggested plugin limits, always capped by deployment policy ceilings |
| `tests` | At least one network-free minimal self-test case |

## Entry-Point Contracts and Isolated Execution Constraints

- **Generator entry point (`generate(request) -> GenerateResult`)**: Receives the packaged canonical assembly, fixed resource manifest, options, seed, and logical working directory path; validates supported capabilities and executes business unit conversions, outputting deterministic solver input manifests and structured solve requests wrapped into a standard `Solver Bundle` by the sandbox generator.
- **Algorithm entry point (`solve(request) -> SolveResult`)**: Reads only relative input paths declared by the generate manifest; shell/subprocess execution, network access, and reading environment or databases are prohibited; outputs MUST conform strictly to `result.schema.json` with finite values and controlled logs only.
- **Sandbox validation gate**: Upload checks proceed strictly in order: `media type & size → archive safety & compression ratio → Manifest & ref integrity → offline dependency lock → static syntax scan → isolated container build → minimal self-test → output & determinism verification`; only packages passing all steps register as selectable versions.
- **Versioning rules**: Changing entry points, schemas, units, or algorithm semantics MUST bump `MAJOR`; backward-compatible additions bump `MINOR`; bug fixes bump `PATCH`. Shared hub approvals and task snapshots bind exact `(plugin_id, version)` pairs, and installation creates references without duplicating underlying archives.
