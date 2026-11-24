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
| `predictions` | Array (may be `[]`) | Prediction entries `{target: {instance, interface}, algorithm, parameters}`; `target` entries MUST be unique |
| `generator` | `<ID>@<version>` or `null` | Exact selected generator ref (required for staged mode; `null` for unified algorithm mode) |
| `solver` | `<ID>@<version>` or `null` | Exact selected solver ref (required for staged mode; `null` for unified algorithm mode) |
| `algorithm` | `<ID>@<version>` or `null` | Exact selected unified algorithm ref (required for unified algorithm mode; `null` for staged mode) |
| `evaluator` | `<ID>@<version>` or `null` | Exact selected result adapter & post-solve financial evaluator ref (optional; `null` selects the default paired evaluator) |
| `precision` | Enum | Computation precision: `low`, `medium`, or `high` |
| `convergence` | `null` or `{tolerance, max_iterations}` | Required when coupled prediction targets exist (`tolerance > 0` dimensionless threshold, `max_iterations >= 2`); MUST be `null` for standalone or zero predictions |
| `time_limit_seconds` | Positive integer | Requested solver runtime limit in seconds |
| `options` | Object (may be `{}`) | Algorithm/generator/solver extension options validated strictly against the chosen generator or unified algorithm descriptor's options schema (`options_schema` / `parameters_schema`) |
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
- **Static capability checks**: At config save and task submission, `mode`, `precision`, `solver`, `evaluator`, `outputs`, `options`, and prediction parameter rules are validated against the selected generator or unified algorithm descriptor (see [Prediction Parameters and Convergence Capabilities](../modules/engines.md#prediction-parameters-and-convergence-capabilities)); whether a concrete math model lowers or solves cleanly is determined during the computation stages.
