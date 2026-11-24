#!/usr/bin/env bash
# Staged 5-container app startup shared by browser checks and cold-start verification.
set -euo pipefail
: "${CI_TEST_DATA_DIR:?Set CI_TEST_DATA_DIR to an isolated disposable directory}"
mkdir -p "$CI_TEST_DATA_DIR"
compose=(-f docker-compose.yml -f .github/ci/docker-compose.yml)
if [[ "${SPECIAL_DEVICE:-false}" == "true" ]]; then
  : "${PIES_EXT_DEVICE_DIR:?Set PIES_EXT_DEVICE_DIR for external-device scenario}"
  compose+=(-f frontend/tests/e2e/docker-compose.ext-device.yml)
fi
if [[ "${CI_DOCKER_CACHE:-false}" == "true" ]]; then
  [[ "${GITHUB_ACTIONS:-false}" == "true" ]] || {
    echo "Remote build cache is CI-only" >&2
    exit 2
  }
  compose+=(-f .github/ci/docker-compose-cache-read.yml)
  if [[ "${CI_DOCKER_CACHE_WRITE:-false}" == "true" ]]; then
    compose+=(-f .github/ci/docker-compose-cache-write.yml)
  fi
fi
# Start healthy DB/Redis/job_runner, then backend (which initializes schema,
# seeds admin, and starts the embedded scheduler), and finally web.
docker compose "${compose[@]}" up -d --build --wait postgres redis job_runner backend web
echo "5-container stack healthy (postgres, redis, job_runner, backend, web)"
