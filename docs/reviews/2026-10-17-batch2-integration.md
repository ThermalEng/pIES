# Batch 2 integration review

Date: 2026-10-17. Local evidence summary, non-normative, ignored by Git.
Integrated commit: `3de3436`; source branch tip: `9a03b38`.

Accepted the finite correction set after inspecting actual models, DTO producers,
request consumers and tests. Removed conflict remnants of both legacy families;
retained Batch 1 task/config/validation migration and Batch 2 auth/admin wiring.
Added AppShell logout rejection display rather than leaving an unhandled Promise.

Docker integration checks (run by Codex on merged production tree):

- `docker build --target build -f frontend/Dockerfile -t pies-codex014-integration-build .`:
  help manifest 47 chapters, TypeScript and Vite successful (294 modules).
- esbuild actual test imports then Node in that build image:
  `auth_admin_contracts.test.mjs` 60 PASS,
  `evidence_collect.test.mjs` 16 PASS,
  `error-envelope.test.mjs` 16 PASS.
- Built runtime image `pies-codex014-integration-web` from the same frontend.
- Isolated Compose project `pies014integration`, fresh named PostgreSQL/app
  volumes and Redis, no worker needed for this authentication-only check.
  Backend image reused unchanged; no production/backend code change.
- Chromium `logout-failure.spec.ts`: 1 passed (3.2s). Actual login, one injected
  POST logout 500, visible alert, protected settings reload still authenticated,
  successful retry, protected route redirects after logout. Exact single 500
  allowance; no blanket console/pageerror suppression.
- Browser artifacts directory: `/tmp/pies014integration-e2e.sLVrHX`.
  Successful list reporter output was observed in the tool transcript; this
  summary is not represented as a saved raw test log.
- OpenCode's 14 unaffected browser cases are reused as agent-reported evidence;
  Codex did not independently rerun them or the backend suite.

The initial attempt to replace the shared piest1e2e web service was denied by
approval review before execution; used a separate new Compose stack instead.
No protected stack was replaced. Own isolated containers/network/volumes and
two own temporary image tags were removed after validation; original/backend/
base images and user resources were preserved.

Batch 3 now assigned to Muse on its own persistent worktree at this baseline.
0.14 is not complete; final audit remains Codex's responsibility.
