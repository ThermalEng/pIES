# Validation and Tasks

## Run Project Validation

1. Open **Validation** in the workbench.
2. Click **Run validation**. The system runs a full preflight check covering model topology, dataset bindings, finance configuration, planning constraints, and calculation configuration.

Validation outcomes are **Passed**, **Warnings**, or **Blocked**. When blocking issues exist, task submission is disabled. Passing validation confirms inputs are complete and structurally valid; whether a feasible or optimal solution exists mathematically is still determined by the solver during execution (unconnected connectable ports enter the equations as zero-flow closed boundaries).

## Submit Calculation Task and Confirm Evaluation Parameters

After confirming **Calc Config** is saved, open **Tasks** and click **Submit task**:

1. **Select task type**: Calculation / Optimization / Uncertainty analysis / Batch analysis.
2. **Enter evaluation confirmation parameters** (all required with no defaults; edits autosave to the draft while composing):
   - **Construction period (`K`)** and **Operating years (`N`)**: A non-negative integer and positive integer respectively; total evaluation years `E = K + N` are derived automatically.
   - **Discount rate**: Decimal value within `0..1` (e.g., `0.08`).
   - **Construction payment ratios**: JSON array such as `[{"t": 0, "ratio": 1}]`, where `t` is the integer year offset from construction start (`0..K`) and ratios sum exactly to `1`.
   - **Maintenance/renewal expenses**: JSON array (explicitly enter `[]` if there are no extra expenses). Each entry specifies the evaluation year (`year: K+j` corresponds to the end of operating year `j`), currency, and fixed `amount`, and may attach dynamic scale-linked terms (`scale_unit_cost`) or upfront-capex share terms (`cost_component_share`) under `terms`:

```json
[
  {
    "year": 3,
    "amount": "1000.00",
    "currency": "CNY",
    "terms": [
      {
        "kind": "scale_unit_cost",
        "instance_id": "hp1",
        "property": "rated_heat_kw",
        "unit_cost": { "value": "2", "unit": "CNY/kW" }
      },
      {
        "kind": "cost_component_share",
        "instance_id": "hp1",
        "component": "upfront_capex",
        "ratio": 0.01
      }
    ]
  },
  { "year": 3, "amount": "2500.50", "currency": "CNY" }
]
```

*(Tip: Multiple maintenance entries in the same evaluation year receive 1-based `position` indices in list order; any `(year, position)` references in finance rules for VAT input tax or depreciation must match this order.)*

3. **Optional: Evaluation comparison document (JSON)**: Used to compare against a baseline. Leave blank or set `{"mode": "absolute"}` to evaluate the project's absolute cash flows; choose `difference` (each side computes its own taxes separately before taking `CF_candidate - CF_baseline`) or `reference` (exposes frozen baseline constants for formulas or budget constraints) to reference a sealed `task_result` or manually confirmed `declared` cash flows and facts (see [Evaluation Scope and Frozen Baseline](../../developer-guide/en-US/contracts.md#evaluation-scope-and-frozen-baseline)):

```json
{
  "mode": "difference",
  "baseline": {
    "kind": "declared",
    "currency": "CNY",
    "evaluation_years": 2,
    "annual_net_flows": ["-100", "60", "60"],
    "facts": {}
  },
  "facts": {},
  "comparability": {
    "service_basis": "Same service demand and economic scope",
    "differences": "Confirmed differences of this alternative"
  }
}
```

4. Review the configuration revision summary and click **Submit task**. Upon submission, the system freezes the current draft, dataset versions, configuration revisions, and confirmed evaluation into an immutable snapshot; later draft edits do not alter submitted tasks.

## Observing Progress, Status, and Runtime Resources

Click any row in **Task list** to inspect its stage, attempt count, snapshot reference, and **Runtime resources (latest stage observation)**:

- **Progress and spinner**: Running tasks show a rotating indicator and unify to `100%` upon normal completion; failed, timed-out, or cancelled tasks retain the last stage percentage where they stopped.
- **Runtime resources**: Displays the Worker container's shared memory hard limit, swap limit, and container-lifetime memory peak for operational troubleshooting (these values reflect container-shared observations, not dedicated per-task reservations).
- **Load frozen inputs into draft**: Select any historical task to load its frozen snapshot inputs into the current editable draft for tuning and submitting a new task.
- **Technical status vs. business outcome (unidirectional pipeline)**: `1.2.0` executes a single-pass pipeline (`predictions[]` grouped per `predictor` → generation & solve or unified `algorithm` → bounded subprocess restoration of engineering solution → `0..N` parallel `evaluators[]`). Once core solving succeeds, top-level task outcome reflects only the core solve; isolated evaluator failures are recorded explicitly **without invalidating the sealed engineering solution** (non-feasible core solves skip post-solve evaluators).

## Cancelling, Retrying, and Failure Troubleshooting

- **Cancel task**: Click **Cancel task** on a queued or running task. The task transitions through **Cancelling** and stops the backend process before entering **Cancelled**.
- **Task retry vs. single-evaluator retry**: If a task failed due to invalid inputs, infeasibility, or insufficient capacity, fix the root cause and submit a new task. Use **Retry task** only when a transient interruption occurred on unchanged inputs (reusing the original snapshot and stage checkpoints). When the core solve succeeded but individual parallel evaluators failed, click **Retry Evaluator** on the failed evaluator card in **Results** to spawn an independent `analysis` child task reusing the sealed engineering solution without re-solving or displacing newer tasks' latest pointers. If `Resource exhausted` is reported, ask an administrator to adjust container memory limits before retrying.

Next: [Results and Exports](results-and-exports.md).
