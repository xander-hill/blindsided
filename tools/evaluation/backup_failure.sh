#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/deploy/compose/docker-compose.yaml}"
AUCTION_ADDRESS="${AUCTION_ADDRESS:-localhost:50052}"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9090}"
RUN_ID="${RUN_ID:-backup-failure-$(date +%s)}"

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
    printf '\nRestoring failed backup %s...\n' "$failed_service"
    compose start "$failed_service" >/dev/null || true
  fi
}
trap cleanup EXIT

prometheus_query() {
  local query="$1"

  curl --fail --silent --show-error --get \
    --data-urlencode "query=$query" \
    "$PROMETHEUS_URL/api/v1/query"
}

metric_value() {
  local query="$1"

  prometheus_query "$query" | python3 -c '
import json
import sys

result = json.load(sys.stdin)["data"]["result"]
print(result[0]["value"][1] if len(result) == 1 else "")
'
}

instances_for() {
  local query="$1"

  prometheus_query "$query" | python3 -c '
import json
import sys

result = json.load(sys.stdin)["data"]["result"]
print("\n".join(
    item["metric"].get("instance", "")
    for item in result
))
'
}

service_for_instance() {
  local instance="$1"
  local service="${instance%%:*}"
  local configured_service

  while IFS= read -r configured_service; do
    if [[ "$configured_service" == "$service" ]]; then
      printf '%s\n' "$service"
      return 0
    fi
  done < <(compose config --services)

  return 1
}

counter_increased() {
  local before="$1"
  local after="$2"

  python3 - "$before" "$after" <<'PY'
import sys

try:
    before = float(sys.argv[1])
    after = float(sys.argv[2])
except ValueError:
    raise SystemExit(1)

raise SystemExit(0 if after > before else 1)
PY
}

command -v docker >/dev/null || fail "docker is required"
command -v curl >/dev/null || fail "curl is required"
docker compose version >/dev/null 2>&1 \
  || fail "Docker Compose v2 is required"

curl -fsS "$PROMETHEUS_URL/-/ready" >/dev/null \
  || fail "Prometheus is unavailable at $PROMETHEUS_URL"

printf '%s\n' \
  "Expected Grafana dashboard views:" \
  "  - Replication & Commit Health" \
  "  - Cluster Recovery" \
  "Expected affected metrics:" \
  "  - blindsided_replication_attempts_total" \
  "  - blindsided_replication_duration_seconds" \
  "  - blindsided_commits_total" \
  "  - blindsided_mutations_total" \
  "  - blindsided_cluster_ready" \
  "  - blindsided_replica_health_transitions_total" \
  "  - blindsided_synchronization_attempts_total" \
  "  - blindsided_storage_role" \
  "  - blindsided_storage_ready"

healthy_storage_targets="$(metric_value 'sum(up{job="storage"} == 1)')"
[[ "$healthy_storage_targets" == "3" ]] \
  || fail "Expected three healthy Prometheus storage targets; found ${healthy_storage_targets:-0}"

primary_instances=()
while IFS= read -r instance; do
  [[ -n "$instance" ]] && primary_instances+=("$instance")
done < <(
  instances_for '
    blindsided_storage_role{
      job="storage",
      role="primary"
    } == 1
  '
)

designated_backup_instances=()
while IFS= read -r instance; do
  [[ -n "$instance" ]] && designated_backup_instances+=("$instance")
done < <(
  instances_for '
    (
      blindsided_storage_role{
        job="storage",
        role="backup"
      } == 1
    )
    and on(instance)
    (
      blindsided_storage_ready{
        job="storage"
      } == 1
    )
  '
)

standby_instances=()
while IFS= read -r instance; do
  [[ -n "$instance" ]] && standby_instances+=("$instance")
done < <(
  instances_for '
    (
      blindsided_storage_role{
        job="storage",
        role="backup"
      } == 1
    )
    unless on(instance)
    (
      blindsided_storage_ready{
        job="storage"
      } == 1
    )
  '
)

[[ "${#primary_instances[@]}" -eq 1 ]] \
  || fail "Expected exactly one primary; found ${#primary_instances[@]}"

[[ "${#designated_backup_instances[@]}" -eq 1 ]] \
  || fail \
    "Expected exactly one ready designated backup; found ${#designated_backup_instances[@]}"

[[ "${#standby_instances[@]}" -eq 1 ]] \
  || fail \
    "Expected exactly one non-ready standby backup; found ${#standby_instances[@]}"

primary_instance="${primary_instances[0]}"
backup_instance="${designated_backup_instances[0]}"
standby_instance="${standby_instances[0]}"

[[ "$primary_instance" != "$backup_instance" ]] \
  || fail "Primary and designated backup resolve to the same Prometheus instance"

[[ "$backup_instance" != "$standby_instance" ]] \
  || fail "Designated backup and standby resolve to the same Prometheus instance"

failed_service="$(service_for_instance "$backup_instance")" \
  || fail \
    "Cannot map Prometheus instance '$backup_instance' to a Compose service"

cluster_ready_before="$(metric_value 'blindsided_cluster_ready{job="controller"}')"
[[ "$cluster_ready_before" == "1" ]] \
  || fail "Cluster is not ready before backup failure"

sync_before="$(
  metric_value '
    sum(
      blindsided_synchronization_attempts_total{
        outcome="completed"
      }
    ) or vector(0)
  '
)"

health_before="$(
  metric_value '
    sum(
      blindsided_replica_health_transitions_total{
        transition="healthy_to_unhealthy"
      }
    ) or vector(0)
  '
)"

[[ "$sync_before" =~ ^[0-9]+([.][0-9]+)?$ ]] \
  || fail "Could not read replacement-backup synchronization metric"

[[ "$health_before" =~ ^[0-9]+([.][0-9]+)?$ ]] \
  || fail "Could not read replica health transition metric"

printf '\nResolved topology:\n'
printf '  Primary:            %s\n' "$primary_instance"
printf '  Designated backup:  %s\n' "$backup_instance"
printf '  Standby backup:     %s\n' "$standby_instance"

printf '\nCreating committed state before backup failure...\n'
python3 "$SCRIPT_DIR/auction_traffic.py" \
  --address "$AUCTION_ADDRESS" \
  --run-id "$RUN_ID-before" \
  --create-only

printf '\nStopping designated synchronous backup %s (%s)...\n' \
  "$failed_service" \
  "$backup_instance"

compose stop "$failed_service" >/dev/null

printf 'Submitting a mutation without the designated synchronous backup...\n'

if python3 "$SCRIPT_DIR/auction_traffic.py" \
  --address "$AUCTION_ADDRESS" \
  --run-id "$RUN_ID-unavailable" \
  --create-only; then
  fail "Mutation was acknowledged without its designated synchronous backup"
fi

printf '[ok] Mutation was not acknowledged without its designated synchronous backup.\n'

printf 'Waiting for health detection, standby synchronization, and readiness...\n'

deadline=$((SECONDS + 90))

while true; do
  failed_target="$(
    metric_value "
      up{
        job=\"storage\",
        instance=\"$backup_instance\"
      }
    "
  )"

  old_backup_still_active="$(
    metric_value "
      (
        blindsided_storage_role{
          job=\"storage\",
          instance=\"$backup_instance\",
          role=\"backup\"
        } == 1
      )
      and on(instance)
      (
        up{
          job=\"storage\",
          instance=\"$backup_instance\"
        } == 1
      )
    "
  )"

  standby_is_ready_backup="$(
    metric_value "
      (
        blindsided_storage_role{
          job=\"storage\",
          instance=\"$standby_instance\",
          role=\"backup\"
        } == 1
      )
      and on(instance)
      (
        blindsided_storage_ready{
          job=\"storage\",
          instance=\"$standby_instance\"
        } == 1
      )
      and on(instance)
      (
        up{
          job=\"storage\",
          instance=\"$standby_instance\"
        } == 1
      )
    "
  )"

  health_after="$(
    metric_value '
      sum(
        blindsided_replica_health_transitions_total{
          transition="healthy_to_unhealthy"
        }
      ) or vector(0)
    '
  )"

  sync_after="$(
    metric_value '
      sum(
        blindsided_synchronization_attempts_total{
          outcome="completed"
        }
      ) or vector(0)
    '
  )"

  ready="$(
    metric_value 'blindsided_cluster_ready{job="controller"}'
  )"

  if [[ "$failed_target" == "0" ]] \
    && [[ -z "$old_backup_still_active" ]] \
    && [[ "$standby_is_ready_backup" == "1" ]] \
    && [[ "$ready" == "1" ]] \
    && counter_increased "$health_before" "$health_after" \
    && counter_increased "$sync_before" "$sync_after"; then
    break
  fi

  if (( SECONDS >= deadline )); then
    fail \
      "Backup failure did not produce a healthy synchronized replacement within 90 seconds"
  fi

  sleep 3
done

printf '\nVerifying synchronous writes after replacement-backup recovery...\n'

python3 "$SCRIPT_DIR/auction_traffic.py" \
  --address "$AUCTION_ADDRESS" \
  --run-id "$RUN_ID-recovered" \
  --create-only

printf '\nBackup failure scenario completed successfully.\n'