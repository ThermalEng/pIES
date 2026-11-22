# Finance Calculation

> Code Boundary: `backend/iesplan/finance/`; File Format Specs: [Finance YAML Contract](../formats/finance-yaml.md), [Named Amounts, Payments, and Assets](../formats/finance-relations.md)

`finance` manages domain validation and deterministic merging of the regional baseline and project override triplet, defines cost, revenue, asset depreciation, and tax cash-flow relations participating in full-horizon planning optimization, and computes project-investment financial evaluation metrics from the same solve.

## Responsibilities and Boundaries

- **Finance triplet management**: Owns domain validation and deterministic merge semantics for `FinanceProfile`, `FinanceOverrides`, and `EffectiveFinanceConfig`, exposing the immutable `EffectiveFinanceConfig` snapshot to assembly, planning, and computation (field structures live in [Finance YAML Contract](../formats/finance-yaml.md)).
- **Cost components and billing semantics**: Supplies `upfront_capex` (one-time construction), `annual_fixed_om` (`currency/year`), `period_variable_om` (operating quantity charges), and `tariff_bindings` energy billing contributions (`+raw_charge` for `purchase`, `-raw_charge` for `sale`, reversing naturally under negative prices). Each component keeps its own time basis; annual components MUST multiply explicitly by a time span (e.g., `1 year`) before summing with horizon totals.
- **Strict decoupling from technical specs**: Device models (`ies.device-model`) carry zero prices or costs; assembly attaches cost and tariff relations explicitly via `finance_binding` (`costed` / `none`), `tariff_bindings`, and `demand_bindings`, strictly forbidding inference of economic attributes from device names or port directions.

### Evaluation Scope and Baselines

`finance` supports three evaluation scopes—absolute (`absolute`), separate-tax difference (`difference`), and external constant reference (`reference`)—whose complete input schemas live in [Evaluation Scope and Frozen Baseline](../contracts.md#evaluation-scope-and-frozen-baseline):

1. **Independent dimensions**: Clearly separate economic scope, tax scope, and frozen baseline source; baseline facts MUST come solely from user-confirmed `declared` data or sealed `task_result` evidence, prohibiting auto-generating a "zero-capacity baseline" or re-reading live drafts during solving.
2. **Separate-taxation principle**: `difference` mode computes after-tax cash flows separately for candidate and baseline under their respective frozen tax rules prior to taking `CF_with - CF_without`, never re-taxing the cash-flow difference so distortion from `Tax(c) - Tax(b) != Tax(c - b)` is avoided.
3. **Same-solve consistency**: Baseline constants affecting taxable income, credit balances, or constraints MUST enter the optimization model during solving rather than solving pre-tax first and subtracting baselines or taxes afterward.

## Taxation in Planning and Cash Flows

VAT, construction input VAT credits, income tax, tax depreciation, tax loss carry-forwards, named cash movements, and derived taxes that affect planning decisions are compiled into the same full-horizon optimization solve by generators, and final evaluation consumes that same solve directly.

### Planning Objective and Same-Source Evaluation

Full-horizon after-tax planning maximizes project after-tax net present value (`system.after_tax_npv`), expressing cash flows on a tax-inclusive actual cash-receipt/payment basis:

```text
CF[t](x) = R_gross[t](x) - O_gross[t](x) - K_gross[t](x)
           - VAT_paid[t](x) - other_tax[t](x) - income_tax[t](x)
           + tax_refund[t](x) + explicit_net_cash[t](x)

maximize NPV_after_tax(x) = Σ(t=0..H) CF[t](x) / (1 + r)^t
```

Here `t=0..H` covers construction and operating periods, `R_gross` is tax-inclusive operating revenue, `O_gross` is operating O&M plus scheduled maintenance outlays, and `K_gross` follows construction payment timing; remaining terms capture VAT paid, derived taxes, income tax paid, arrived tax refunds, and explicit net cash items.

### Core Tax Accounting Rules

- **VAT and shared credit pools**: A tax-inclusive unit price `p` at tax rate `v` splits into tax-exclusive price `p/(1+v)` and tax amount `p*v/(1+v)`. Each credit pool evaluates monthly `net[k] = output_VAT[k] - eligible_input_VAT[k] - credit[k-1]`, paying tax when positive and carrying forward credit when negative; confirmed refunds deduct from usable credit and enter cash in `arrival_period`, while unrefunded closing carry-forward and post-horizon receivables seal as closing balances outside in-horizon cash inflows.
- **Income tax and depreciation**: Taxable revenue and deductible expenses aggregate on net bases stripping declared output VAT and deductible input VAT; straight-line depreciation accrues as `(B - R_effective)/life_years` only when an asset forms with effective net basis `B > 0` (capitalized outlays are never double-deducted as current expenses, and depreciation causes no second cash outflow); opening and newly generated losses offset positive taxable income within carry-forward limits in earliest-expiry order, expiring automatically thereafter.
- **Pure calculation verification helpers**: The module exports stateless pure functions (tax-inclusive splitting, single-period VAT credit roll-forward, straight-line depreciation, and loss carry-forward) for testing and verification reconciliation.

### Solver Capability Boundaries

Products of known constants (tax rates, prices, fixed depreciation factors) and decision variables stay linear or pre-merged; `max`/`min` piecewise tax branches and cross-period balances use exact linearization encodings requiring finite bounds over tax bases and cumulative balances; unsupported nonlinearities or unmet capabilities fail cleanly with capability diagnostics at submission.

## Project Evaluation, Construction Timeline, and Sealed Results

### Project-Investment Evaluation Metrics

From the complete cash-flow series `CF[0..evaluation_years]` and explicit evaluation parameters, compute NPV, IRR, static payback, and dynamic payback:

- **Near-zero threshold**: IRR root-finding and payback checks filter solver floating-point residuals using `max(1e-6, 1e-12 * max(|CF|))` (raw cash flows and NPV retain sealed precision); when the entire series lies within tolerance, IRR reports as `degenerate`.
- **Payback status**: Evaluated only after cumulative net cash flow (or discounted cumulative net cash flow) first exhibits a substantial negative deficit—interpolated linearly along the original annual axis upon recovery as `recovered`, marked `not_recovered` if unrecovered, or marked `not_applicable` (`null`, never misreporting `0` years) when no substantial deficit ever occurred.
- **IRR status set**: Strictly distinguishes `normal`, `no_solution`, `multiple`, `degenerate`, `out_of_domain`, and `numeric_failure`.

### Construction Schedule and Hand-Calculated Timeline Example

Let `K = construction_period`, `N = operation_years`, `E = K + N`:

- Construction starts at `t=0` and allocates construction outlays over `t=0..K` per `construction_payments` (`t=K` itself has no preceding operating revenue);
- `t=K+j` (`j=1..N`) accumulates net operating cash flow of the `j`-th full operating year; simulation steps span solely the contiguous `N` operating years after commissioning (derived from project `start_year` via Gregorian calendar, independent of financial `base_year`).

**Example**: With `K=1`, `N=2`, `E=3`, schedule `[{t: 0, ratio: 0.6}, {t: 1, ratio: 0.4}]`, construction total `C`, and two years of operating net `R1`, `R2`, the series is `CF = [-0.6C, -0.4C, R1, R2]` of length `4`.

### Operating-Period Renewals and Metric Scope

Maintenance and equipment replacement expenses are declared explicitly in `maintenance_expenses` at task submission with evaluation year and amount sources (fixed amounts combined with `scale_unit_cost` and `cost_component_share`; see [Annual Maintenance Sources](../contracts.md#annual-maintenance-sources)). Each item's `(year, position)` inside the frozen list serves as its unique input-VAT and depreciation/deduction identity; expenses already accounted in O&M are never double-deducted.

### Calendar-Month Demand Billing

When `demand_bindings` are configured, each bound outbound `out` electricity power interface multiplies its measured monthly peak power across each operating calendar month (derived from project baseline and Gregorian calendar including leap years) by its `demand_charges` rate inside the same optimization and rolls into yearly net cash flows. Zero-rate bindings retain true monthly peaks with zero charge; unbound cases emit no monthly demand bills.

### Sealed Evaluation Block (`financial_evaluation`)

Once a usable primal solution is available, the Worker executes one same-solve evaluation and seals `schema: ies.financial-evaluation` (`schema_version: "1.0.0"`) inside result evidence:

| Core Section | Fields Included |
|---|---|
| Echoed frozen inputs | `evaluation_years`, `construction_period`, `operation_years`, `currency`, `price_basis`, `base_year`, `profile_id`, `discount_rate` |
| Construction & cash items | `construction_total` (and unit), `annual_fixed_om`, `construction_units`, `payments_applied`, `net_cashflows` |
| Operations & tax schedules | `annual_operation` (`energy_net` / `variable_om` / `fixed_om` / `maintenance` / `demand_charge` / `net`), `monthly_demand`, `tax_cash`, `annual_net_flows` |
| Comparison & metrics | `comparison` (frozen sources & difference stream), `evaluation` (yearly `rows`, `npv`, `irr`/`irr_status`, `static_payback`/`dynamic_payback` with `_unit`/`_status`) |

All monetary values preserve full precision as decimal fixed-point strings; UI displays and Excel exports project directly from this sealed block.
