# Results and Exports

## Opening Results

Click **View results** in task details or open **Results** from the left navigation (displays the latest completed task by default, switchable via the task selector). Every value shown—project version, data versions, configs, financial evaluation, and charts—comes strictly from the selected task's sealed evidence package; subsequent draft or data changes never alter historical results.

- **Loading order**: Key metrics, solver summary, financial evaluation, and planning solution load first, followed sequentially by step curves per series (switching tasks or pages cancels obsolete curve requests automatically).
- **Absent results or read failures**: Unfinished or infeasible tasks display an explicit business explanation. If reading fails due to a transient network error, click **Retry** to re-fetch without re-running the computation.

## Reading Result Tabs, Level-1 Charts, and Level-2 Paginated Tables

The top summary card and evidence package selector remain pinned at the top of the **Results** page, while result views below are organized into five tabs. High-volume process datasets open on Level-1 pages as native charts; click **View data table** in a card header to drill into a Level-2 paginated table page with breadcrumb navigation and filters:

- **Overview**: Displays **Sealed solver status**, the four independent dimension badges (physical, optimality, financial, reliability), headline sealed financial metrics (`NPV`, `IRR` and status, static/dynamic payback) with a cash flow trend preview, **Sealed construction decisions**, **Planning solution preview**, and **Candidates**.
- **Financials & Cash Flows**: Presents Level-1 charts for full-horizon net and cumulative cash flows `CF[0..E]`, annual operating breakdowns, frozen baseline comparisons, construction payments, and explicit net cashflows, with **View data table** links to Level-2 paginated tables preserving exact sealed decimals.
- **Process & Tax**: Presents Level-1 visual cards for calendar-month demand peaks and charges (switchable by meter binding), monthly VAT pool trajectories (switchable by pool), yearly income tax and tax depreciation bar charts, named cost/amount charts grouped by axis/unit, and asset book value curves; each card drills down into a filtered, paginated Level-2 table view.
- **Engineering & Curves**: Displays computation step curves (with interactive legend toggles and window pagination), a **View step data table** drill-down for exact window rows, and engineering balance, emissions, and reliability metrics.
- **Assessments & Export**: Lists immutable assessment history (paginated when needed), system reassessment trigger, and Excel / project package downloads.

### 1. Solver Status and Key Metrics

- **Sealed solver status**: Explicitly distinguishes **Optimality proven**, feasible without proven optimality (**Results restricted**, e.g., solver time limit reached with a feasible incumbent), and **No usable feasible solution was found**. Objective value, best bound, and solver relative gap display in actual units, or `—` when unavailable.
- **Energy balance, emissions, and four-dimensional assessment**: Displays electric/heat/cooling balances, emission boundaries, and independent verdicts across physical feasibility, optimality, financial viability, and reliability (a failure in any single dimension is flagged explicitly and never masked by an overall summary).

### 2. Financial Evaluation and Cash Flows

- **Sealed evaluation cash flows**: Presents headline horizon inputs and KPIs on **Overview**, and full construction payments over `t=0..K`, annual operating net cash flows over `t=K+1..K+N` (already net of maintenance expenses and demand charges), full after-tax net cash flows `CF[0..E]`, NPV, IRR, and static/dynamic payback periods across **Financials & Cash Flows** and its Level-2 tables.
  - **IRR and payback status**: Always read IRR alongside its calculation status (unique solution, no solution, multiple solutions, or degenerate). When no substantial cash deficit or construction outlay occurs, IRR shows `— / Degenerate` and payback shows `— / Not applicable`.
  - **Tax schedules**: **Process & Tax** summarizes trends visually, and its Level-2 sub-tabs break down VAT paid, claimed and arrived refunds, ending credit carry-forward, income tax, tax depreciation (annotated by initial asset class or maintenance evaluation year/ordinal), loss carry-forward balances, and derived taxes with filtering and pagination. Closing VAT carry-forward and post-horizon refund receivables do not enter in-horizon cash inflows, and depreciation never creates a second cash outflow.
- **Demand bills, named items, and construction decisions**:
  - When demand meters are bound, **Process & Tax** charts measured peak power and monthly demand charges by meter binding and links to the paginated bill table;
  - **Named cost items (sealed)**, **Named amounts (sealed)**, and **Explicit after-tax cashflows** appear with their own identities and periods (items already included in total cost components are never deducted twice);
  - **Sealed construction decisions** on **Overview** shows whether each declared construction unit was built along with its planning scales.

### 3. Frozen Baseline and Difference Comparison

When a task was submitted in `reference` or `difference` mode, **Financials & Cash Flows** displays the confirmed service basis, documented differences, and comparison curves; click **View data table** to inspect the frozen baseline origin, paginated cash flow comparison table, and `facts`. In `difference` mode, candidate cash flow, baseline cash flow, and evaluation difference (`candidate minus baseline`, each taxed under its own tax rules prior to subtraction) are kept distinct, with comparison metrics computed from that difference stream.

### 4. Step Curves

On **Engineering & Curves**, each window displays up to 1,000 contiguous raw computation steps (supporting **Previous window**, **Next window**, jumping to a starting step, and toggling individual curves via the legend), plus **View step data table** for paginated inspection of raw step values. Device series are grouped by instance and variable name, and auxiliary system series are labeled `System <block>.<variable>`. Visual decimation affects plotting only; exact raw step values are preserved in Excel exports.

## Adopting Candidates, Applying Schemes, and System Assessment

- **Select this candidate**: Records adoption on the current task result without altering project versions.
- **Apply scheme**: Click **Apply scheme** next to the planning solution preview. Starting from the project version referenced by the task's snapshot, the system writes solved planning parameters onto device instances and derives a new immutable project version (making it the current workspace; unsealed draft edits made after the task ran are not carried over). A confirmation dialog previews the exact parameter changes before execution.
- **Reassess / Rerun system assessment**: Re-runs the four-dimensional system checks against the task's sealed evidence and appends an immutable assessment record (manual assessment score entry is not exposed in the current GUI).

## Exporting Excel Reports and Project Packages

Once generated, downloads authenticate directly against your active session and project permissions.

### Export Excel Report

1. Confirm the selected task is completed and has usable result data.
2. If no assessment record exists yet, click **Reassess** to generate a system assessment first.
3. Choose the **Report language** (**Chinese** or **English**) and click **Export Excel report**.

The workbook contains overview, solver status, financial summary, construction decisions, calendar-month demand bills, named cost/amount breakdowns, and full un-decimated `Step Series Catalog` and `Step Series Values` sheets derived from the same sealed evidence as the UI.

### Export Full Project Package

The project owner can click **Export full project package** on the **Exports** page to download a `.zip` archive (containing models, versions, datasets, and historical result evidence, excluding accounts and permissions) for migration or sharing (import steps: [Projects and Access](projects-and-access.md)).

## Export Troubleshooting

- **Button disabled**: Verify the task is finished with usable results (full project package export also requires project owner role).
- **Download does not start**: Check that the browser has not blocked file downloads for the site and retry.
- **Persistent errors**: Record the project ID, task ID, displayed error message, and product version, then contact your administrator.

Next: [Accounts, Administration, and Troubleshooting](administration-and-troubleshooting.md).
