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

# Worker services have no Docker healthcheck. Their process can be "running"
# before the bootstrap assembly is complete; require a post-assembly signal.
for spec in "io_worker:io" "compute_worker:compute"; do
  service="${spec%%:*}"
  worker_type="${spec#*:}"
  ready=false
  for attempt in $(seq 1 60); do
    container_id="$(docker compose "${compose[@]}" ps -q "$service")"
    if [[ -n "$container_id" ]] &&
       [[ "$(docker inspect --format '{{.State.Running}}' "$container_id")" == "true" ]] &&
       docker compose "${compose[@]}" logs --no-color "$service" 2>&1 |
         grep -F "Worker 启动:" | grep -Fq "type=$worker_type"; then
      ready=true
      break
    fi
    sleep 1
  done
  if [[ "$ready" != "true" ]]; then
    echo "Worker $service did not finish bootstrap and enter the loop" >&2
    docker compose "${compose[@]}" logs --no-color --tail=120 "$service" >&2
    exit 1
  fi
  echo "$service assembled and started ($worker_type)"
done
