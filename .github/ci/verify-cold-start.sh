#!/usr/bin/env bash
# CI-only: exercise multiple truly empty PostgreSQL / Redis/data directories.
# Never remove or reuse ./data from local/production deployments.
set -euo pipefail
[[ "${GITHUB_ACTIONS:-}" == "true" ]] || { echo "Cold-start check requires ephemeral GitHub Actions runner" >&2; exit 2; }
: "${RUNNER_TEMP:?Missing runner temp directory}"
for round in 1 2 3; do
  export CI_TEST_DATA_DIR="$RUNNER_TEMP/pies-cold-start-$round"
  [[ ! -e "$CI_TEST_DATA_DIR" ]] || { echo "Cold-start directory already exists: $CI_TEST_DATA_DIR" >&2; exit 1; }
  export COMPOSE_PROJECT_NAME="pies-ci-cold-$round"
  echo "Cold-start round $round/3 on fresh data"
  trap 'docker compose -f docker-compose.yml -f .github/ci/docker-compose.yml logs --no-color --tail=120 || true' ERR
  bash .github/ci/start-stack.sh
  docker compose -f docker-compose.yml -f .github/ci/docker-compose.yml ps
  trap - ERR
  docker compose -f docker-compose.yml -f .github/ci/docker-compose.yml down --remove-orphans
  # Files belong to this ephemeral runner and are not reused by later rounds.
done
docker compose -f docker-compose.yml -f .github/ci/docker-compose.yml down --remove-orphans --rmi local
