# Concepts, Units, and Current Limits

## Distinguishing "Version" Concepts

| Concept | Meaning and Purpose |
|---|---|
| Product version | pIES software release version (currently `1.2.0`), used to identify UI capabilities and report issues |
| Project draft | Editable working state in the workbench; autosave updates the draft only and does not create permanent versions |
| Project version | Immutable design baseline created manually in **Versions** or automatically when submitting a task, used for history tracking and restore |
| Dataset version | Immutable snapshot created each time a data file is uploaded and validated; project bindings do not auto-advance on new uploads |
| Config revision | New revision created each time **Calc Config** or **Planning config** is saved; task submission freezes the active revision |
| Snapshot and evidence | Complete frozen inputs captured at submission time and the sole source for resulting outputs |
| Ecosystem artifact (Single Active Version) | Global device models and algorithm plugins follow the Plan B single active version policy: one active revision/version per ID (`<publisher>.*` for market releases, `user.<ns>.*` for personal drafts) |

## Unit and Time Rules

- **Unit conventions**: Common units are `kW` for power, `kWh` for energy, `°C` for temperature, percentages in UI forms for ratios (e.g., enter `8` for `8%`), and the project's single selected currency for monetary amounts (automatic multi-currency conversion within a project is not supported). Never type unit text into numeric-only fields or mix power and energy units during upload.
- **Time baseline**: Projects have no time zone; generic timestamp CSVs must declare a single fixed UTC offset per dataset version (DST shifts are unsupported), while self-describing step CSVs count in contiguous integer steps. Full-year point counts derive from the Gregorian **Start year** chosen at project creation (leap years automatically include Feb 29).

## Financial Evaluation and IRR

- Project evaluation models full after-tax cash flows spanning both construction and operating periods and supports comparing against a frozen baseline (`difference` mode computes taxes separately on candidate and baseline cash flows under their respective rules before taking the difference, without re-taxing the difference stream).
- Simply configuring tax-inclusive prices or displaying tax labels does not enable tax computation; tax calculation requires explicitly declaring tax rates, rules, credit pools, and scope in the finance document (current scope focuses on project-investment after-tax evaluation and excludes debt financing, equity evaluation, and tax filing).
- IRR values must be interpreted alongside their calculation status (unique solution / no solution / multiple solutions / degenerate / out of domain / numeric failure); there is no hard "minimum IRR" constraint input.
- When model relations requiring finite bounds (such as mutual exclusivity or piecewise taxes) lack bounds or cannot infer them, task submission or model diagnostics identify the exact source relation and missing bound so you can supply valid physical ranges.

## Cost Accounting Conventions

- **Annual O&M and renewals**: Recurring fixed O&M goes into `annual_fixed_om`; usage-dependent O&M goes into `period_variable_om`; specific-year overhauls or equipment replacements are entered explicitly in `maintenance_expenses` at task submission (timeless scalar whole-horizon O&M lump sums are rejected). *(Volumetric fuel flows currently use supported units such as `m³` / `m³/h`.)*
- **Optional construction and fixed costs**: Whether an optional construction unit is built and whether conditional fixed costs trigger are explicitly linked via construction units and `cost_occurrence`; fixed costs without an occurrence condition apply unconditionally.

## Current `1.2.0` Boundaries and Limits

- `1.2.0` delivers the unified `/custom` (Models & Algorithms) dual-tab workspace, `/admin/ecosystem` orthogonal governance, dynamic stage plugin catalog orchestration, parallel retryable evaluation, generic self-describing report rendering, and decoupled `pIES-Market` plus stateless OTP submission gateway;
- Ecosystem artifacts enforce **Plan B single active version**: per-ID multi-version switching, parallel multi-version installs, historical task version pinning, and cross-version config migration layers are not maintained (`1.1.5 → 1.2.0` uses a one-pass migration for editable configs while keeping historical snapshots/evidence strictly read-only);
- External digest checks apply the three-way boundary rule (required `sha256` verification on market downloads, check when provided on offline packages, archive/contract checks only on bare offline uploads) without storing or recomputing `sha256` internally;
- System assessment focuses on automated four-dimensional checks (manual score saving is unsupported), and project package import omits a pending-proposal management queue;
- Dataset **Versions** does not currently preview uploaded rows inline, and administrator **Audit log** provides read-only inspection while maintenance and backup/restore run server-side;
- Help Center publishes Simplified Chinese and English editions.

See [Roadmap](../../changelog/en-US/roadmap.md) for upcoming `1.3.0+` milestones.
