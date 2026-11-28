# Iterative Economic Dispatch Algorithm (`ies.algorithm.economic_dispatch@1.0.0`)

Reference end-to-end Python `algorithm` plugin solving a convex multi-step economic dispatch problem via deterministic Lagrangian dual bisection.

## Contracts
- Consumes: `ies.stage-input@1.0.0` (`/work/in/request.json`)
- Produces: `ies.engineering-solution@1.0.0` (`/work/out/solution.json`) and NDJSON progress (`/work/out/iterations.jsonl`)
