#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/deploy/compose/docker-compose.yaml}"
AUCTION_ADDRESS="${AUCTION_ADDRESS:-localhost:50052}"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9090}"
RUN_ID="${RUN_ID:-primary-failover-$(date +%s)}"
failed_service=""

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

compose() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

cleanup() {
  if [[ -n "$failed_service" ]]; then
    compose start "$failed_service" >/dev/null || true
  fi
}
trap cleanup EXIT

prometheus_query() {
  local query="$1"
  curl --fail --silent --show-error --get \
    --data-urlencode "query=$query" "$PROMETHEUS_URL/api/v1/query"
}

metric_value() {
  local query="$1"
  prometheus_query "$query" | python3 -c \
    'import json,sys; r=json.load(sys.stdin)["data"]["result"]; print(r[0]["value"][1] if len(r)==1 else "")'
}

instances_for() {
  local query="$1"
  prometheus_query "$query" | python3 -c \
    'import json,sys; r=json.load(sys.stdin)["data"]["result"]; print("\n".join(x["metric"].get("instance", "") for x in r))'
}

one_instance_for() {
  local query="$1"
  local description="$2"
  local instances=()
  local instance
  while IFS= read -r instance; do
    [[ -n "$instance" ]] && instances+=("$instance")
  done < <(instances_for "$query")
  [[ "${#instances[@]}" -eq 1 ]] \
    || fail "Expected exactly one replica reporting $description"
  printf '%s\n' "${instances[0]}"
}

service_for_instance() {
  local instance="$1"
  local service="${instance%%:*}"
  local configured_service
  while IFS= read -r configured_service; do
    [[ "$configured_service" == "$service" ]] && {
      printf '%s\n' "$service"
      return 0
    }
  done < <(compose config --services)
  return 1
}

counter_increased() {
  local before="$1"
  local after="$2"
  python3 - "$before" "$after" <<'PY'
import sys
try:
    raise SystemExit(0 if float(sys.argv[2]) > float(sys.argv[1]) else 1)
except ValueError:
    raise SystemExit(1)
PY
}

epoch_advanced() {
  local before="$1"
  local after="$2"
  python3 - "$before" "$after" <<'PY'
import sys
try:
    raise SystemExit(0 if float(sys.argv[2]) > float(sys.argv[1]) else 1)
except ValueError:
    raise SystemExit(1)
PY
}

command -v docker >/dev/null || fail "docker is required"
command -v curl >/dev/null || fail "curl is required"
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required"

printf '%s\n' \
  "Expected Grafana dashboard views:" \
  "  - Cluster Recovery" \
  "  - Storage Replica State" \
  "  - RPC & Mutation Outcomes" \
  "Expected affected metrics:" \
  "  - blindsided_healthy_replicas" \
  "  - blindsided_cluster_ready" \
  "  - blindsided_primary_epoch" \
  "  - blindsided_replica_health_transitions_total" \
  "  - blindsided_failovers_total" \
  "  - blindsided_failover_duration_seconds" \
  "  - blindsided_promotion_attempts_total" \
  "  - blindsided_promotion_duration_seconds" \
  "  - blindsided_synchronization_attempts_total" \
  "  - blindsided_synchronization_duration_seconds" \
  "  - blindsided_storage_role" \
  "  - blindsided_storage_ready" \
  "  - blindsided_storage_epoch"

primary_query='blindsided_storage_role{job="storage",role="primary"} == 1'
backup_query='
  (blindsided_storage_role{job="storage",role="backup"} == 1)
  and on(instance)
  (blindsided_storage_ready{job="storage"} == 1)
'
standby_query='
  (blindsided_storage_role{job="storage",role="backup"} == 1)
  and on(instance)
  (blindsided_storage_ready{job="storage"} == 0)
'
primary_instance="$(one_instance_for "$primary_query" 'role=primary')"
backup_instance="$(one_instance_for "$backup_query" 'ready designated backup')"
standby_instance="$(one_instance_for "$standby_query" 'non-ready backup standby')"
[[ "$primary_instance" != "$backup_instance" && "$primary_instance" != "$standby_instance" \
  && "$backup_instance" != "$standby_instance" ]] \
  || fail "Primary, backup, and standby must resolve to different Prometheus instances"
failed_service="$(service_for_instance "$primary_instance")" \
  || fail "Cannot map Prometheus instance '$primary_instance' to a Compose service"

initial_epoch="$(metric_value 'blindsided_primary_epoch{job="controller"}')" \
  || fail "Prometheus is unavailable at $PROMETHEUS_URL"
[[ "$initial_epoch" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "Could not read blindsided_primary_epoch"
[[ "$(metric_value 'blindsided_cluster_ready{job="controller"}')" == "1" ]] \
  || fail "Cluster is not ready"
[[ "$(metric_value 'sum(up{job="storage"} == 1)')" == "3" ]] \
  || fail "Expected three healthy Prometheus storage targets before failover"

sync_before="$(metric_value 'sum(blindsided_synchronization_attempts_total{outcome="completed"}) or vector(0)')"
health_before="$(metric_value 'sum(blindsided_replica_health_transitions_total{transition="healthy_to_unhealthy"}) or vector(0)')"
[[ "$sync_before" =~ ^[0-9]+([.][0-9]+)?$ ]] \
  || fail "Could not read replacement-backup synchronization metric"
[[ "$health_before" =~ ^[0-9]+([.][0-9]+)?$ ]] \
  || fail "Could not read replica health transition metric"

printf '\nCreating committed state before failover...\n'
python3 "$SCRIPT_DIR/auction_traffic.py" \
  --address "$AUCTION_ADDRESS" --run-id "$RUN_ID-before" --create-only

printf 'Stopping primary %s (%s)...\n' "$failed_service" "$primary_instance"
compose stop "$failed_service" >/dev/null

printf 'Waiting for health detection, promotion, replacement synchronization, and readiness...\n'
deadline=$((SECONDS + 90))
while true; do
  failed_target="$(metric_value "up{job=\"storage\",instance=\"$primary_instance\"}")"
  former_backup_is_primary="$(metric_value "(blindsided_storage_role{job=\"storage\",instance=\"$backup_instance\",role=\"primary\"} == 1) and on(instance) (up{job=\"storage\"} == 1)")"
  former_standby_is_backup="$(metric_value "(blindsided_storage_role{job=\"storage\",instance=\"$standby_instance\",role=\"backup\"} == 1) and on(instance) (blindsided_storage_ready{job=\"storage\",instance=\"$standby_instance\"} == 1) and on(instance) (up{job=\"storage\"} == 1)")"
  health_after="$(metric_value 'sum(blindsided_replica_health_transitions_total{transition="healthy_to_unhealthy"}) or vector(0)')"
  sync_after="$(metric_value 'sum(blindsided_synchronization_attempts_total{outcome="completed"}) or vector(0)')"
  ready="$(metric_value 'blindsided_cluster_ready{job="controller"}')"
  current_epoch="$(metric_value 'blindsided_primary_epoch{job="controller"}')"

  if [[ "$failed_target" == "0" ]] && [[ "$former_backup_is_primary" == "1" ]] \
    && [[ "$former_standby_is_backup" == "1" ]] && [[ "$ready" == "1" ]] \
    && counter_increased "$health_before" "$health_after" \
    && counter_increased "$sync_before" "$sync_after" \
    && epoch_advanced "$initial_epoch" "$current_epoch"; then
    break
  fi
  (( SECONDS < deadline )) || fail "Failover did not complete within 90 seconds"
  sleep 3
done

printf '\nVerifying writes through the promoted primary...\n'
python3 "$SCRIPT_DIR/auction_traffic.py" \
  --address "$AUCTION_ADDRESS" --run-id "$RUN_ID-after" --create-only

printf '\nRestoring the original replica...\n'
compose start "$failed_service" >/dev/null
failed_service=""
deadline=$((SECONDS + 60))
until [[ "$(metric_value "up{job=\"storage\",instance=\"$primary_instance\"}")" == "1" ]] \
  && [[ "$(metric_value "(blindsided_storage_role{job=\"storage\",instance=\"$primary_instance\",role=\"backup\"} == 1) and on(instance) (blindsided_storage_ready{job=\"storage\",instance=\"$primary_instance\"} == 0) and on(instance) (up{job=\"storage\"} == 1)")" == "1" ]] \
  && [[ "$(metric_value 'blindsided_cluster_ready{job="controller"}')" == "1" ]]; do
  (( SECONDS < deadline )) || fail "Original replica did not return as the unassigned standby within 60 seconds"
  sleep 2
done

printf '\nPrimary failover scenario completed successfully (epoch %s -> %s).\n' \
  "$initial_epoch" "$current_epoch"
