# CalculationConfig

> Contract ID: `ies.calculation-config`; `schema_version`: `"1.0.0"`; Consumers: Task submission validator, orchestrator, and selected computation plugin containers

`CalculationConfig` fixes all plugin selections and runtime parameters for a single calculation task. Computation runs across the project baseline's complete contiguous time axis; this contract contains no typical-day reduction, planning objectives/constraints, device parameters, finance rules, or assembly topology, and freezes independently alongside `ValidatedAssemblyArtifact`.

## Top-Level Fields

The `1.2.0` unidirectional pipeline effective contract requires the following **13 top-level fields** (removing the unused `convergence` field and upgrading the single evaluator to a parallel `evaluators` array; `generator`, `solver`, `algorithm`, and `seed` MUST explicitly pass `null` when absent—never omitted); unknown fields and non-finite numbers are rejected:

| Field | Type & Format | Description |
|---|---|---|
| `schema` | String | Fixed to `ies.calculation-config` |
| `schema_version` | String | Fixed to `"1.0.0"` |
| `mode` | Non-empty string | Computation mode; MUST appear in the selected generator or unified algorithm's `modes` (built-in: `planning`) |
| `predictions` | Array (may be `[]`) | Single-pass upstream prediction entries `{target: {instance, interface}, algorithm, parameters}` (`target` unique); grouped by `algorithm` (`predictor` plugin ref) within the logical prediction stage so targets sharing a plugin execute jointly in one container pass to preserve latent correlations before the framework merges all predicted series |
| `generator` | `<ID>@<version>` or `null` | Exact selected generator ref (required for staged mode; `null` for unified mode) |
| `solver` | `<ID>@<version>` or `null` | Exact selected solver ref (required for staged mode; `null` for unified mode) |
| `algorithm` | `<ID>@<version>` or `null` | Exact selected unified algorithm ref (required for unified mode; `null` for staged mode) |
| `evaluators` | Array (may be `[]`) | Parallel post-solve evaluator list `{evaluator: "<ID>@<version>", parameters: {...}}` (`evaluator` refs unique); `[]` skips post-solve evaluation and delivers the restored canonical engineering solution and step curves directly |
| `precision` | Enum | Computation precision: `low`, `medium`, or `high` |
| `time_limit_seconds` | Positive integer | Requested solver runtime limit in seconds |
| `options` | Object (may be `{}`) | Per-stage option slots for core optimization: `{generator, solver}` in staged mode or `{algorithm}` in unified mode (omitted sub-slots default to `{}`), each validated independently against its plugin's `parameters_schema` (prediction and evaluation parameters live inside `predictions[].parameters` and `evaluators[].parameters` without collision) |
| `seed` | Integer or `null` | Random seed (booleans prohibited) |
| `outputs` | Non-empty string array | Requested output artifacts (order preserved, e.g., `["solution", "metrics"]`) |

### Two Core Optimization Composition Modes

| Field | Staged Mode (`generator + solver`) | Unified Mode (`algorithm`) |
|---|---|---|
| `generator` | Exact ref | `null` |
| `solver` | Exact ref | `null` |
| `algorithm` | `null` | Exact ref |
| `evaluators` | `0..N` independent evaluators (`[]` is valid in staged mode too because column-to-business-unit restoration runs automatically in the platform runtime right after `solver`) | `0..N` independent evaluators (`[]` supported) |

Save and task submission gates strictly enforce mutual exclusivity—specifying `algorithm` alongside `generator`/`solver` or setting all three to `null` is rejected.

## JSON Example

```json
{
  "schema": "ies.calculation-config",
  "schema_version": "1.0.0",
  "mode": "planning",
  "predictions": [
    {
      "target": { "instance": "pv1", "interface": "power_out" },
      "algorithm": "ies.predictor.naive@1.0.0",
      "parameters": {}
    }
  ],
  "generator": "ies.generator.highs_milp@1.0.0",
  "solver": "ies.solver.highs@1.7.2",
  "algorithm": null,
  "evaluators": [
    {
      "evaluator": "ies.evaluator.finance@1.0.0",
      "parameters": {}
    }
  ],
  "precision": "medium",
  "time_limit_seconds": 43200,
  "options": {
    "generator": {
      "acceleration": { "mode": "on_demand_mutex" }
    },
    "solver": {}
  },
  "seed": 42,
  "outputs": ["solution", "metrics"]
}
```

## Time-Limit Budget and Validation Boundaries

- **Solver time limit and outer supervision**: The built-in HiGHS generator passes the user's requested `time_limit_seconds` verbatim to HiGHS `--time_limit` while adding a bounded `+600`-second outer container/process supervision grace (`command.timeout_seconds = time_limit_seconds + 600`) for startup, MPS model reading, and solution writing; total wall-clock budget MUST still fit within deployment policy `max_timeout_seconds` (rejected prior to launch if exceeded).
- **Static capability and composition checks**: At config save and task submission, `mode`, `precision`, `outputs`, and upstream/downstream public data contract IDs (`produces` / `consumes` plus optional `companions` restrictions) are verified, while `predictions[].parameters`, `options` sub-slots, and `evaluators[].parameters` are validated independently against each selected plugin's `parameters_schema`; incompatible combinations fail immediately at the submission gate without runtime guessing.
- **One-pass `1.1.5` → `1.2.0` migration & historical snapshot immutability**: Prior to upgrading from `1.1.5` to `1.2.0`, all non-terminal `1.1.5` tasks (including `queued`, `running`, and `cancelling`) MUST be drained or cancelled; versioned database migration converts saved project `CalculationConfig` revisions in one pass while preserving original intent (removing `convergence`; mapping explicit `ies.result-adapter.highs@1.0.0`—or `1.1.5` staged configs implicitly bound to it with `"solution"` in `outputs`—to `evaluators: [{"evaluator": "ies.evaluator.finance@1.0.0", "parameters": {}}]`, while mapping other `evaluator: null` configs to `evaluators: []`; moving legacy `options` into `options.generator` or `options.algorithm`) so runtime code maintains only the single current contract. Completed historical `1.1.5` tasks continue rendering directly from sealed `Evidence`, and frozen historical `CalcSnapshot` records remain strictly read-only without in-place mutation.
