# CalculationConfig

> Contract ID: `ies.calculation-config`; Target `schema_version`: `"1.0.0"`; Consumers: Task submission validator and `GeneratorProvider`

`CalculationConfig` fixes all algorithm and solver options for a single calculation task. Computation runs across the project baseline's complete contiguous time axis; this contract contains no typical-day reduction, planning objectives/constraints, device parameters, finance rules, or assembly topology, and freezes independently alongside `ValidatedAssemblyArtifact`.

## Top-Level Fields

All 12 top-level fields below are required (`convergence` and `seed` MUST explicitly pass `null` when absent—never omitted); unknown fields and non-finite numbers are rejected:

| Field | Type & Format | Description |
|---|---|---|
| `schema` | String | Fixed to `ies.calculation-config` |
| `schema_version` | String | Fixed to `"1.0.0"` |
| `mode` | Non-empty string | Computation mode; MUST appear in selected `GeneratorDescriptor.modes` (built-in: `planning`) |
| `predictions` | Array (may be `[]`) | Prediction entries `{target: {instance, interface}, algorithm, parameters}`; `target` entries MUST be unique |
| `generator` | `<ID>@<version>` | Exact selected generator ref (required in staged mode; mutually exclusive when using a unified `algorithm`) |
| `solver` | `<ID>@<version>` | Exact selected solver ref (required in staged mode; mutually exclusive when using a unified `algorithm`) |
| `precision` | Enum | Computation precision: `low`, `medium`, or `high` |
| `convergence` | `null` or `{tolerance, max_iterations}` | Required when coupled prediction targets exist (`tolerance > 0` dimensionless threshold, `max_iterations >= 2`); MUST be `null` for standalone or zero predictions |
| `time_limit_seconds` | Positive integer | Requested solver runtime limit in seconds |
| `options` | Object (may be `{}`) | Generator/solver extension options validated strictly against the chosen `GeneratorDescriptor`'s `options_schema` |
| `seed` | Integer or `null` | Random seed (booleans prohibited) |
| `outputs` | Non-empty string array | Requested output artifacts (order preserved, e.g., `["solution", "metrics"]`) |

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
  "precision": "medium",
  "convergence": null,
  "time_limit_seconds": 43200,
  "options": {},
  "seed": 42,
  "outputs": ["solution", "metrics"]
}
```

## Time-Limit Budget and Validation Boundaries

- **Solver time limit and outer supervision**: The built-in HiGHS generator passes the user's requested `time_limit_seconds` verbatim to HiGHS `--time_limit` while adding a bounded `+600`-second outer process supervision grace (`command.timeout_seconds = time_limit_seconds + 600`) for startup, MPS model reading, and solution writing; total wall-clock budget MUST still fit within deployment policy `max_timeout_seconds` (rejected prior to launch if exceeded).
- **Static capability checks**: At config save and task submission, `mode`, `precision`, `solver`, `outputs`, `options`, and prediction parameter rules are validated against `GeneratorDescriptor` (see [Prediction Parameters and Convergence Capabilities](../modules/engines.md#prediction-parameters-and-convergence-capabilities)); whether a concrete math model lowers cleanly is determined during generation.
- **Unified single-step algorithms and optional evaluators (`1.1.5` target)**: Under the unified plugin contract, staged mode continues specifying exact `generator` and `solver` refs (plus an optional `evaluator` override defaulting to the paired evaluator); when selecting a minimal `kind: algorithm` plugin, an exclusive `algorithm` ref replaces `generator` and `solver`, with save and submission gates strictly rejecting mixed or unknown fields so single-step plugins never fabricate split refs.
