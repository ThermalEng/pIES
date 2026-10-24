#!/usr/bin/env bash
# Shared local/cloud Docker entry, from repository root:
#   bash .github/ci/test-frontend-logic.sh [one.test.mjs ...]
set -euo pipefail
cd "$(dirname "$0")/../.."
tag="pies-frontend-logic:run-$$"
cleanup() { docker image rm "$tag" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker build --target build -f frontend/Dockerfile -t "$tag" .
docker run --rm --entrypoint node "$tag" scripts/run-logic-tests.mjs "$@"
