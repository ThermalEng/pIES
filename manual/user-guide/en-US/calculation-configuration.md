# Calculation Configuration

The calculation configuration pages (**Finance**, **Cost & tariff bindings**, **Planning config**, and **Calc Config**) together specify economic rules, optimization objectives, constraints, and solver options. Every save generates an immutable revision so subsequent task submissions remain precisely traceable.

---

## 1. Configure Project Finance

Open **Finance** in the workbench to manage regional finance baselines (profiles), project overrides, and the merged **Current Effective (read-only)** snapshot:

1. **Select or register a profile**: Choose from registered profiles and click **Reference / switch**; or paste a complete document conforming to the [Finance YAML Contract](../../developer-guide/en-US/formats/finance-yaml.md) into **Profile JSON document**, add or edit demand charges in the structured section below as needed, click **Register**, and reference it on the project.
2. **Project overrides (optional)**: To fine-tune device unit prices, energy tariffs, or demand rates for the current project, enter a sparse overrides document under **Project overrides** or adjust items in the structured section and click **Save overrides**. Click **Clear overrides** to revert to the bare profile merge.
3. **Inspect effective finance config**: Review the merged equipment costs, energy tariffs, demand charges, and tax parameters in the read-only view. *(Note: After modifying finance configuration, revisit and re-save **Planning config** below so planning validation reflects the updated bindings.)*

### Annual Expenses, Renewal Depreciation, and Net Recoveries

The complete finance document directly configures the following project-level economic relations without requiring separate specialized forms:

- **Recurring fixed O&M (`annual_fixed_om`)**: Annual fixed fees (e.g., `CNY/a`) and capacity-scaled annual fees; usage-dependent variable O&M is configured separately under `period_variable_om`.
- **Renewal depreciation and expense deduction**: Overhaul or renewal outlays are entered in the maintenance list when submitting a task. To capitalize and depreciate them, reference either the initial construction cost or a specific evaluation year's maintenance item ordinal via `base_from` inside the income tax rule's `depreciation` section (`first_year` is the operating year index starting from commissioning, distinct from the evaluation year used by maintenance payments; capitalized items are not simultaneously deducted as current-period expenses).
- **End-of-horizon salvage or extra net cash flows (`net_cashflows`)**: Explicitly declare the timing, inflow/outflow direction, and fixed amount or source ratio (tax residual book value never turns into cash recovery automatically).
- **Optional construction and cost occurrence conditions**: For fixed costs triggered only when a specific construction unit is built, explicitly associate the construction unit under `cost_occurrence` in assembly (see [Cost Occurrence Conditions](../../developer-guide/en-US/formats/assembly-yaml.md#cost-occurrence-conditions)).
- **Named cost items and amount constraints**: To split distinct tax rates on the same scale, define custom payment schedules, or manage asset pools, use the `items`, `amounts`, `cash_movements`, and `assets` sections of the finance document (see [Named Amounts, Payments, and Assets](../../developer-guide/en-US/formats/finance-relations.md)), and enforce budget or amount limits via `amount_relation` in the full YAML of **Planning config** (see [Amount Reference Constraints](../../developer-guide/en-US/formats/assembly-yaml.md#amount-reference-constraints)).

### Configuring Monthly Daily Price Curves

Use this workflow when each calendar month applies a representative daily price profile (if prices vary across years, use a full-horizon price series instead):

1. Upload one complete day's price CSV on the **Data** page and copy its **Price object ID** (see [Price Series Files](data-preparation.md#price-series-files)).
2. Under **Finance** (in a profile or project overrides), locate the target energy price entry and switch its price source to monthly daily curves.
3. Set the sampling resolution and price unit, then fill in the **Price object ID** for months 1–12 (you can fill January first, click the batch-fill button to copy January across all months, and then replace individual months).
4. Register the profile or save overrides, verify the updated effective finance snapshot, and re-save **Planning config**.

---

## 2. Cost & Tariff Bindings

On the **Cost & tariff bindings** page, associate device instances with financial categories and energy prices (saved into the current assembly draft):

1. **Instance cost assignments**: For every device instance on the canvas, explicitly choose **No device cost (type: none)** or **Costed instance (type: costed)**. For `costed` instances, select the `finance_type` and map every required `driver` to a technical property or interface of that instance (time-series interfaces require selecting `integrate_positive` or `sum_positive` aggregation). *(A costless pure metering node can still carry energy and demand bindings below.)*
2. **Energy tariff meters (optional)**: Add tariff bindings as needed by specifying a unique binding ID, registered price entry (buy/sell accounting direction comes from the price entry rather than the port's physical direction label), meter instance, unidirectional time-series interface, and aggregation mode. Leave empty when no energy tariff billing applies.
3. **Monthly peak demand meter bindings (optional)**: Specify a unique demand binding ID and bind a demand charge, metered device instance, and outbound electricity power interface. When left empty, no demand bills are generated.
4. Click **Save cost, energy & demand bindings**. Saving the draft records the bindings only; authoritative reference, unit, and completeness checks run on the **Validation** page.

---

## 3. Planning Configuration and System Technical Equations

1. **Objective**: Select **Minimize** or **Maximize** and enter the objective expression. To optimize after-tax net present value, select **Maximize** and enter `system.after_tax_npv`.
2. **Device decision variables and bounds**: Add capacity or technical parameters to optimize by specifying a unique variable name, device instance reference, technical parameter, and optional decimal lower/upper bounds (leaving a bound blank imposes no project bound on that side without silently inserting zero or a default).
3. **Named constraints and step relations**: Add economic or technical constraints as needed. To author cross-instance step-level port constraints (`step_relation`) or budget/amount constraints (`amount_relation`), switch to **Full YAML document** (see [Step Technical Constraints](../../developer-guide/en-US/formats/assembly-yaml.md#step-technical-constraints)); valid fields not expanded in the form are preserved automatically.
4. **System technical equations (optional)**: To declare cross-device energy allocation, source accounts, or auxiliary system output series, edit `system_equations` and `outputs` in **System technical equations** and save to the draft (see [Network Boundaries and System Equations](../../developer-guide/en-US/formats/network-relations.md)).
5. Click **Save planning config** to generate a new revision.

---

## 4. Calculation Config (Solver and Algorithm Options)

Open **Calc Config** to configure solver execution options for the run (**this page does not contain evaluation years or discount rate**, which are confirmed at task submission):

| Field | Guidance |
|---|---|
| Mode | E.g., `planning` |
| Generator ref & Solver | Select or enter exact version refs (e.g., `ies.generator.highs_milp@1.0.0` and `ies.solver.highs@1.7.2`) |
| Precision | Required: `low`, `medium`, or `high` (no default selection) |
| Time limit (s) | Solver internal runtime limit (newly created projects prefill the deployment default) |
| Seed (empty = unspecified) | Leave empty or enter an integer seed |
| Outputs (JSON array) | Required JSON array (e.g., `["solution", "metrics"]`) |
| Predictions & Stage-2 convergence | Configure prediction algorithms for `data_predict` interfaces; fill `{"tolerance": ..., "max_iterations": ...}` (`max_iterations` ≥ 2) only when coupled prediction algorithms are present, otherwise leave empty |
| Generator/solver options | Optional JSON object conforming to the selected generator's options schema |

Click **Save calculation config** when finished; the page displays the latest revision (e.g., `v1`). If a version conflict (`409`) occurs because another session saved first, the page preserves your current edits so you can choose **Reload latest config** or adjust and retry. Until Calculation Config is saved, project validation reports a blocking diagnostic and task submission remains disabled.

Next: [Validation and Tasks](validation-and-tasks.md).
