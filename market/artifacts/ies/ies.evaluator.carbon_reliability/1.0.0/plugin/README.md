# Carbon & Reliability Evaluator (`ies.evaluator.carbon_reliability@1.0.0`)

Reference post-processing `evaluator` plugin consuming `ies.engineering-solution@1.0.0` (`/work/in/solution.json`) and emitting a self-describing `ies.evaluation-report@1.0.0` (`/work/out/evaluation_report.json`) with carbon & reliability KPIs, a line chart, a bar chart, and a summary table.

## Contracts
- Consumes: `ies.engineering-solution@1.0.0`
- Produces: `ies.evaluation-report@1.0.0`
