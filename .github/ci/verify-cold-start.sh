#!/usr/bin/env bash
# CI-only single cold-start round. GitHub matrix schedules rounds 1/2/3 on
# independent ephemeral runners, each with its own empty database/volumes.
# No job touches local or production ./data.
set -euo pipefail
[[ "${GITHUB_ACTIONS:-}" == "true" ]] || {
  echo "Cold-start verification requires an ephemeral GitHub Actions runner" >&2
  exit 2
}
: "${RUNNER_TEMP:?Missing runner temp directory}"
[[ "$#" == 1 && "$1" =~ ^[123]$ ]] || {
  echo "usage: verify-cold-start.sh 1|2|3" >&2
  exit 2
}
round="$1"
export CI_TEST_DATA_DIR="$RUNNER_TEMP/pies-cold-start-$round"
export COMPOSE_PROJECT_NAME="pies-ci-cold-$round"
[[ ! -e "$CI_TEST_DATA_DIR" ]] || {
  echo "Refusing to reuse an existing cold-start directory: $CI_TEST_DATA_DIR" >&2
  exit 1
}
compose=(-f docker-compose.yml -f .github/ci/docker-compose.yml)
trap 'docker compose "${compose[@]}" logs --no-color --tail=120 || true' ERR
echo "Cold-start matrix round $round/3 on fresh PostgreSQL, Redis, and data"
bash .github/ci/start-stack.sh
docker compose "${compose[@]}" ps
echo "Cold-start matrix round $round/3 passed: API and both workers initialized"
# Cleanup is performed by the workflow's if:always() step on this runner.
