#!/usr/bin/env bash
# Staged app startup shared by browser checks and cold-start verification.
set -euo pipefail
: "${CI_TEST_DATA_DIR:?Set CI_TEST_DATA_DIR to an isolated disposable directory}"
mkdir -p "$CI_TEST_DATA_DIR"
compose=(-f docker-compose.yml -f .github/ci/docker-compose.yml)
if [[ "${SPECIAL_DEVICE:-false}" == "true" ]]; then
  : "${PIES_EXT_DEVICE_DIR:?Set PIES_EXT_DEVICE_DIR for external-device scenario}"
  compose+=(-f frontend/tests/e2e/docker-compose.ext-device.yml)
fi
# Start just the API and its healthy DB/Redis dependencies. This initializes
# the schema and seeds the admin before either background worker enters.
docker compose "${compose[@]}" up -d --build --wait backend
docker compose "${compose[@]}" up -d --build --wait web io_worker compute_worker
