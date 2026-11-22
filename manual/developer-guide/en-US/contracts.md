# Public Contracts

> Status: Effective Blueprint; Spec Version: 1.0.0; Governing Spec: [Architecture Constitution](ARCHITECTURE_CONSTITUTION.md)

This chapter defines the transport and business input contracts shared across processes, across modules, and between frontend and backend. Authoritative HTTP paths and field tables come from the running version's OpenAPI specification and schemas; this manual does not maintain a duplicate API field reference.

## JSON and Data Types

- **Encoding and media type**: UTF-8, `application/json`.
- **Naming convention**: Field names and enum values uniformly use lowercase `snake_case`.
- **Identifiers**: External entity IDs are opaque decimal strings (arithmetic prohibited); extension and provider IDs use versioned namespaced strings (e.g., `ies.generator.highs_milp@1.0.0`).
- **Numeric representation**: Exact monetary amounts and settlement rates use decimal fixed-point strings; continuous physical quantities use finite JSON numbers (`NaN` and `Infinity` prohibited); ratios use normalized `0..1` values; integer counts and years use JSON integers.
- **Timestamps**: HTTP payloads, events, and metadata uniformly use ISO 8601 UTC strings suffixed with `Z`; calculation series and data CSVs use contiguous `step` indices starting from `0` without time zone semantics.
- **Long-lived structures**: All declare `schema_version`; missing fields, `null`, and loading failures MUST remain strictly distinct without masking errors via empty arrays or defaults.

## Success and Error Responses

### Response Envelope

Successful resource responses wrap top-level JSON objects using **named top-level resource keys** rather than generic wrappers like `{data, meta}`, and MUST NOT mix bare objects with wrapped objects on the same endpoint:

- Single resource / composite views: `{project, draft, versions, my_role}`, `{task, replayed, hint}`, `{result}`, `{assessment}`, `{report, stored}`, etc.;
- Lists and pagination: `{items, total}` or `{items, next_cursor}`;
- Action responses: `{ok, ...}`; bodyless mutations return HTTP `204 No Content`.

Allowed top-level key sets for all JSON endpoints are centrally registered in the API layer and enforced by protocol baseline tests.

### Standard Error Envelope

All error responses share one uniform structure:

```json
{
  "error": {
    "code": "DOMAIN-CATEGORY-NNN",
    "message_key": "ies.error.example",
    "severity": "error",
    "blocking": true,
    "params": {},
    "location": null,
    "fix_hint_key": "",
    "ref_ids": []
  }
}
```

- `code` follows `<DOMAIN>-<CATEGORY>-<NNN>` (such as `API` / `PROJ` / `TASK` / `DATA` / `CONFIG` / `OBJ` / `PERM` / `AUTH`); new diagnostic codes MUST be centrally registered.
- One `code` MAY map to multiple `message_key` values, but each `message_key` MUST bind to a unique `code`.
- `fix_hint_key` MUST be `""` when no hint applies; stack traces, SQL statements, host paths, and credentials MUST NOT leak to clients.

## HTTP Semantics

- `GET` is read-only and idempotent; `POST` creates resources or executes commands; `PUT` replaces completely; `PATCH` performs explicit partial updates; `DELETE` performs lifecycle deletion.
- Asynchronous acceptance returns `202`; retryable writes carry an idempotency key; concurrent edits use `expected_revision` optimistic locking returning `409` on conflict.
- Upload endpoints MUST declare allowed media types and size limits; export endpoints return `object_id` and filename, and downloads authenticate the active session and project permissions via `GET /api/projects/{id}/exports/{kind}/{object_id}/download` to stream controlled bytes—clients never assemble object storage paths.

| Status Code | Trigger Condition | Typical Error Code Prefix |
|---|---|---|
| `200 / 201 / 204` | Normal success / created / no-content success | — |
| `400` | Business content validation failed on payload (e.g., insufficient CSV rows, out-of-order timestamps) | `*-VAL-*`, `DATA-VAL-*` |
| `401` | Unauthenticated or session expired/invalid | `AUTH-*-001` |
| `403` | Authenticated but unauthorized | `PERM-DENIED-*` |
| `404` | Resource or route does not exist | `RES-MISS-*`, `API-NF-*` |
| `409` | State conflict (optimistic lock mismatch, duplicate operation, missing confirmation) | `*-CONFLICT-*`, `ADMIN-CONFIRM-*` |
| `413` | Uploaded file exceeds limit | File-size error code |
| `422` | Unprocessable structure/schema (type error, missing required field, range violation) | `API-REQ-001`, `CONFIG-VAL-*` |
| `500` | Unhandled internal exception | `API-500-001` |

**Division between `400` and `422`**: Structure, type, or schema validation failures return `422`; syntactically valid payloads whose content violates domain rules (such as data timeline conflicts or invalid semantic references) return `400`.

## Units, Time, and Collections

- **Three-tier unit boundary**: Frontend mappers handle form string formatting, percentages, and simple display conversions only; the backend and assembly prove canonical business units and dimensional compatibility; conversion from business units to solver internal units (e.g., SI) happens solely at the `GeneratorProvider` boundary, and conversion back happens solely at `ResultAdapter`. Public expressions support automatic dimensional reduction for `energy = power × time`.
- **Time baseline**: Projects define no time zone; the `resolution`, `scenario_mode`, and `start_year` fixed at project creation uniquely determine the total point count across the horizon (leap years derived year-by-year from the Gregorian calendar). `data_repeat` repeats a full day, week, or year series as baseline; `data_full` MUST cover the entire operating horizon verbatim; `data_predict` generates full-horizon values from prediction targets. Prior to materialization, all inputs retain their source resolution with contiguous `step` indexing starting at `0`.
- **Collections and large arrays**: Collections requiring deterministic ordering sort by stable business keys; large step-by-step curves are retrieved via a dedicated paginated endpoint rather than unconditionally embedded in summary DTOs.

### Step Series Reading

In result views, `hourly_refs` (`{rows, series}` per group) and result object `meta.series` use the following discriminated union to distinguish device series from system-equation auxiliary series, sharing a unique `series_id` namespace within a result:

| `kind` | Complete Catalog Entry Fields | Notes |
|---|---|---|
| `device` | `{kind: "device", series_id, instance_id, variable_id, unit}` | Identity corresponds to an assembly instance and device variable |
| `system` | `{kind: "system", series_id, block, variable, unit}` | Identity corresponds to a `system_equations` block and auxiliary variable; omits instance fields |

The paginated series endpoint `GET /api/projects/{project_id}/tasks/{task_id}/result/hourly` fetches pages by `field` (`series_id`) and step window `[start, end)`, returning `{field, unit, start, end, values, next_start, total_rows}` plus matching `kind` identity fields; `next_start === null` indicates completion. The step series catalog sheet in Excel exports uses the unified column header `series_id, kind, instance_id, variable_id, block, variable, unit, rows`.

## Snapshots and Asynchronous Contracts

At computation task submission, `CalcSnapshot@1.0.0` freezes the following independent facts:

1. Validated canonical assembly artifact, per-instance frozen device math contributions, bound datasets, and price object references;
2. Selected saved calculation config revision and exact versions of referenced `Generator`, `Solver`, `Executor`, and `ResultAdapter`;
3. **Explicit evaluation confirmation `evaluation`**: Top-level structured submission payload with no defaults on any field:

| Field | Type and Constraints |
|---|---|
| `evaluation_years` | Positive integer total financial evaluation years; MUST equal `construction_period + operation_years` |
| `construction_period` | Non-negative integer years (floats prohibited) |
| `operation_years` | Positive integer operating years after commissioning |
| `discount_rate` | Dimensionless ratio (`0..1`; `0` is valid) |
| `construction_payments` | Construction payment ratio list `[{t, ratio}]`; `t` is integer year offset `0..construction_period` from construction start; ratios MUST sum to `1.0` in exact decimal arithmetic |
| `maintenance_expenses` | Annual maintenance/renewal list `[{year, currency, amount, terms?}]`; `year` is evaluation year (`construction_period+1..evaluation_years`); empty list `[]` allowed |
| `comparison` | Optional evaluation comparison input; omitting is equivalent to `{mode: "absolute"}` |

The server records `confirmed_by` and `confirmed_at` upon submission and validates financial rules and generator capability declarations once at the submission boundary. Apply scheme (`POST /api/projects/{id}/apply-result`) derives a new project version on top of the snapshot's source version working state, carrying `expected_revision` captured when opening the confirmation dialog to detect concurrent conflicts.

Task result queries (`GET /projects/{id}/tasks/{task_id}/result`) project the same sealed evidence directly:

- `solver_summary`: Includes `termination_reason`, `math_status`, `has_feasible_solution`, `objective_value`, `objective_unit`, `best_bound`, and `relative_gap`.
- `financial_evaluation`: Returns the [sealed evaluation block](modules/finance.md#sealed-evaluation-block-financial_evaluation) when a feasible solution exists and was evaluated; otherwise `null` alongside `financial_absence` (`outcome_not_normal` / `solution_not_requested` / `missing_unknown`).

### Evaluation Scope and Frozen Baseline

`evaluation.comparison` is a discriminated union declaring with/without project comparison boundaries and external baseline facts:

| `mode` | Complete Fields | Semantics |
|---|---|---|
| `absolute` | `{mode: "absolute"}` | Evaluates the absolute cash flows of the current project |
| `difference` | `{mode: "difference", baseline, facts, comparability}` | Computes after-tax cash flows separately for candidate and baseline under their respective frozen tax rules, then evaluates `CF_c(x) - CF_b` |
| `reference` | `{mode: "reference", baseline, facts, comparability}` | Supplies baseline constants only for financial amounts or planning `amount_relation`, still evaluating the candidate's own cash flow |

- **`baseline` source**: Exactly one of:
  - `{kind: "task_result", task_id, evidence_id}`: References a completed sealed result containing a valid financial evaluation (MUST NOT reference running tasks or working drafts); at submission, financial facts and provenance are resolved and frozen once from that evidence's [query projection](modules/worker.md#stage-checkpoints-and-recovery) (falling back to streaming projection when unindexed), and automatically materialize into `declared` form on project package export/import.
  - `{kind: "declared", currency, evaluation_years, annual_net_flows, facts}`: Explicitly declared baseline; in `difference` mode `annual_net_flows` MUST contain `E+1` monetary strings, and in `reference` mode is fixed to `null`.
- **`comparability`**: Required `{service_basis, differences}`, both non-empty strings explicitly confirming service equivalence and known differences.
- **`facts` selectors**: Provide local aliases for `kind: baseline` references in financial formulas. `task_result` supports the following typed selectors (`declared` sources use `declared_fact` referencing `{unit, axis, values}` in `facts`):

| `kind` | Selector Structure and Values |
|---|---|
| `operation` | `{kind: "operation", measure}` where `measure` is `energy_net` / `fixed_om` / `variable_om` / `maintenance` / `demand_charge` |
| `cashflow` | `{kind: "cashflow", measure: "net"}` |
| `amount` | `{kind: "amount", id}` |
| `movement` | `{kind: "movement", id, measure}` where `measure` is `gross` / `net_basis` / `net_revenue` / `paid` |
| `asset` | `{kind: "asset", id, measure}` where `measure` is `basis` / `depreciation` / `book_value` |
| `tax` | `{kind: "tax", source, measure}`; mapping follows [Tax Output References](formats/finance-relations.md#tax-output-references) and [Sealed Mapping Table](formats/solver-bundle.md#tax-references-and-sealed-fields) |

**Important financial semantic**: `difference` mode strictly applies **separate taxation followed by subtraction** (`Tax(c) - Tax(b)` generally does not equal `Tax(c - b)`); there is no toggle to re-tax the cash-flow difference. Whenever `comparison` is not `absolute`, the chosen generator MUST declare `finance.frozen_baseline` (`difference` additionally requires `finance.cashflow_difference` and `finance.financial_facts`).

### Annual Maintenance Sources

Each item in `evaluation.maintenance_expenses` has `{year, currency, amount, terms?}`:

- `amount` is a non-negative fixed decimal string (`"0"` for purely term-driven expenses); `terms` is an optional ordered list of linked addends (omitting or `[]` means fixed amount only).
- Linked terms support only the following two `kind` values (scopes, dimensions, and generator `finance.affine_maintenance` capability are validated at submission):

| `kind` | Required Fields and Meaning |
|---|---|
| `scale_unit_cost` | `instance_id`, `property`, `unit_cost: {value, unit}`; multiplies the instance's frozen property or planning variable scale by unit cost |
| `cost_component_share` | `instance_id`, `component: "upfront_capex"`, `ratio` (`0..1`); multiplies the instance's actual construction capex component in the solve by `ratio` (cannot reference `existing` sunk investment) |

Each maintenance entry receives `(year, position)` (1-based sequence index within the same evaluation year) as its unique tax and depreciation identity; combined fixed-plus-term entries still constitute one single payment. Capitalization vs. expense-deduction conflict rules appear in [Finance Amount Sources](formats/finance-yaml.md#amount-sources-and-depreciation-periods).

## Contract Evolution Principles

- Prior to `2.0` public release, proprietary files and internal extension contracts stay uniformly fixed at `1.0.0`, updating producers, consumers, samples, and tests together without multi-version compatibility matrices or legacy field aliases.
- After public release, backward-compatible additions bump MINOR, bug fixes bump PATCH, and breaking changes require an approved ADR before bumping MAJOR.
- Frontend, backend, and module contract tests MUST cover full round-trips from serialized bytes into authoritative backend parsing entry points, verifying key business values, units, IDs, and standard error envelopes.
