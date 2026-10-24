#!/usr/bin/env bash
# Prove the transaction-scoped PostgreSQL advisory lock with two independent
# Python processes starting against the SAME brand-new Postgres database.
set -euo pipefail
[[ "${GITHUB_ACTIONS:-}" == "true" ]] || { echo "Requires an ephemeral CI runner" >&2; exit 2; }
: "${RUNNER_TEMP:?}"
export CI_TEST_DATA_DIR="$RUNNER_TEMP/pies-pg-concurrency-${GITHUB_RUN_ID:?}"
export COMPOSE_PROJECT_NAME="pies-ci-pg-concurrency"
if [[ -e "$CI_TEST_DATA_DIR" ]]; then
  echo "Refusing to touch existing directory: $CI_TEST_DATA_DIR" >&2
  exit 1
fi
mkdir -p "$CI_TEST_DATA_DIR"
compose=(-f docker-compose.yml -f .github/ci/docker-compose.yml)
cleanup() {
  docker compose "${compose[@]}" down --remove-orphans --rmi local || true
}
trap cleanup EXIT
docker compose "${compose[@]}" up -d --wait postgres
docker compose "${compose[@]}" build backend
# The two probes signal readiness, then await one common go marker. A serial
# invocation cannot pass this handshake. Each process exits nonzero on failure.
docker compose "${compose[@]}" run --rm --no-deps backend python tests/pg_init_concurrency_probe.py run first > "$RUNNER_TEMP/pg-first.log" 2>&1 &
first=$!
docker compose "${compose[@]}" run --rm --no-deps backend python tests/pg_init_concurrency_probe.py run second > "$RUNNER_TEMP/pg-second.log" 2>&1 &
second=$!
ready=false
for attempt in $(seq 1 120); do
  if [[ -f "$CI_TEST_DATA_DIR/pg-first.ready" && -f "$CI_TEST_DATA_DIR/pg-second.ready" ]]; then
    ready=true
    break
  fi
  if ! kill -0 "$first" 2>/dev/null || ! kill -0 "$second" 2>/dev/null; then
    break
  fi
  sleep 1
done
if [[ "$ready" != "true" ]]; then
  cat "$RUNNER_TEMP/pg-first.log" "$RUNNER_TEMP/pg-second.log"
  echo "Both PostgreSQL initialization processes must reach the barrier" >&2
  exit 1
fi
touch "$CI_TEST_DATA_DIR/pg-init-go"
first_ok=true
second_ok=true
wait "$first" || first_ok=false
wait "$second" || second_ok=false
cat "$RUNNER_TEMP/pg-first.log" "$RUNNER_TEMP/pg-second.log"
[[ "$first_ok" == "true" && "$second_ok" == "true" ]] || { echo "Concurrent init_db failed" >&2; exit 1; }
# Verify the actual tables and immutable triggers, not just process exit codes.
docker compose "${compose[@]}" run --rm --no-deps backend python tests/pg_init_concurrency_probe.py verify
echo "Two truly concurrent init_db processes passed on one isolated fresh PostgreSQL."
