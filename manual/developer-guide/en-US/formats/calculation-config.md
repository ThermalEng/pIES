# CalculationConfig

> Contract ID: `ies.calculation-config`; Target `schema_version`: `"1.0.0"`; Consumers: Task submission validator, orchestrator, and selected algorithm/generator container

`CalculationConfig` fixes all algorithm and solver options for a single calculation task. Computation runs across the project baseline's complete contiguous time axis; this contract contains no typical-day reduction, planning objectives/constraints, device parameters, finance rules, or assembly topology, and freezes independently alongside `ValidatedAssemblyArtifact`.

## Top-Level Fields

All 14 top-level fields below are required (`generator`, `solver`, `algorithm`, `evaluator`, `convergence`, and `seed` MUST explicitly pass `null` when absent—never omitted); unknown fields and non-finite numbers are rejected:

| Field | Type & Format | Description |
|---|---|---|
| `schema` | String | Fixed to `ies.calculation-config` |
| `schema_version` | String | Fixed to `"1.0.0"` |
| `mode` | Non-empty string | Computation mode; MUST appear in the selected generator or unified algorithm descriptor's `modes` (built-in: `planning`) |
| `predictions` | Array (may be `[]`) | Single-pass upstream prediction entries `{target: {instance, interface}, algorithm, parameters}` (`target` unique); correlated targets routed to the same `predictor` plugin run jointly in one container pass |
| `generator` | `<ID>@<version>` or `null` | Exact selected generator ref (required for staged mode; `null` for unified algorithm mode) |
| `solver` | `<ID>@<version>` or `null` | Exact selected solver ref (required for staged mode; `null` for unified algorithm mode) |
| `algorithm` | `<ID>@<version>` or `null` | Exact selected unified algorithm ref (required for unified algorithm mode; `null` for staged mode) |
| `evaluator` | `<ID>@<version>` or `null` | Exact selected post-solve evaluator ref (under `1.2.0`, `null` explicitly skips post-solve evaluation and delivers upstream results directly; non-null runs the independent evaluator stage) |
| `precision` | Enum | Computation precision: `low`, `medium`, or `high` |
| `convergence` | `null` or `{tolerance, max_iterations}` | Legacy `1.1.5` field (`1.2.0` single-chain removes solver-coupled cross-container outer loops, keeping `null` or streamlining under the single `1.0.0` contract; intra-plugin loops use `plugin.yaml` `iteration` and stage `options`) |
| `time_limit_seconds` | Positive integer | Requested solver runtime limit in seconds |
| `options` | Object (may be `{}`) | Under `1.2.0`, organized into explicit per-stage slots (`{generator, solver, evaluator}` for staged mode or `{algorithm, evaluator}` for unified mode, defaulting omitted slots to `{}`), validated independently against each selected plugin's `parameters_schema` without key collisions |
| `seed` | Integer or `null` | Random seed (booleans prohibited) |
| `outputs` | Non-empty string array | Requested output artifacts (order preserved, e.g., `["solution", "metrics"]`) |

### Two Computation Composition Modes

| Field | Staged Algorithm | Unified Algorithm |
|---|---|---|
| `generator` | Exact ref | `null` |
| `solver` | Exact ref | `null` |
| `algorithm` | `null` | Exact ref |
| `evaluator` | Optional selection | Optional selection |

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
  "evaluator": null,
  "precision": "medium",
  "convergence": null,
  "time_limit_seconds": 43200,
  "options": {},
  "seed": 42,
  "outputs": ["solution", "metrics"]
}
```

## Time-Limit Budget and Validation Boundaries

- **Solver time limit and outer supervision**: The built-in HiGHS generator passes the user's requested `time_limit_seconds` verbatim to HiGHS `--time_limit` while adding a bounded `+600`-second outer container/process supervision grace (`command.timeout_seconds = time_limit_seconds + 600`) for startup, MPS model reading, and solution writing; total wall-clock budget MUST still fit within deployment policy `max_timeout_seconds` (rejected prior to launch if exceeded).
- **Static capability and single-chain composition checks**: At config save and task submission, `mode`, `precision`, `outputs`, upstream/downstream public data contract IDs (`produces` / `consumes` plus optional `companions` restrictions), `predictions[].parameters`, and per-stage `options` slots are validated against each selected plugin's `parameters_schema`; incompatible combinations fail immediately at the submission gate without runtime guessing.
