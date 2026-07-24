#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/deploy/compose/docker-compose.yaml}"
AUCTION_ADDRESS="${AUCTION_ADDRESS:-localhost:50052}"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9090}"
RUN_ID="${RUN_ID:-backup-failure-$(date +%s)}"
POLL_INTERVAL="${POLL_INTERVAL:-2}"
RECOVERY_TIMEOUT="${RECOVERY_TIMEOUT:-90}"
REJOIN_TIMEOUT="${REJOIN_TIMEOUT:-60}"
if [[ -x "$REPO_ROOT/venv/bin/python" ]]; then
  PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/venv/bin/python}"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

failed_service=""
backup_stopped=false

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

compose() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

cleanup() {
  if [[ "$backup_stopped" == "true" && -n "$failed_service" ]]; then
    if [[ -n "${backup_instance:-}" ]]; then
      local cleanup_deadline=$((SECONDS + RECOVERY_TIMEOUT))
      local old_target
      local ready_replacement
      while true; do
        old_target="$(
          metric_value "up{job=\"storage\",instance=\"$backup_instance\"}" \
            2>/dev/null || true
        )"
        ready_replacement="$(
          metric_value "
            count(
              (blindsided_storage_role{
                job=\"storage\",
                role=\"backup\",
                instance!=\"$backup_instance\"
              } == 1)
              and on(instance)
              (blindsided_storage_ready{job=\"storage\"} == 1)
            )
          " 2>/dev/null || true
        )"
        if [[ "$old_target" == "0" && "$ready_replacement" == "1" ]]; then
          break
        fi
        if (( SECONDS >= cleanup_deadline )); then
          printf 'WARNING: recovery was not complete before cleanup restart.\n' >&2
          break
        fi
        sleep "$POLL_INTERVAL"
      done
    fi
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

auction_rpc() {
  local operation="$1"
  local auction_id="${2:-}"
  local expected_version="${3:-0}"

  PYTHONPATH="$REPO_ROOT/backend" "$PYTHON_BIN" - \
    "$AUCTION_ADDRESS" "$operation" "$RUN_ID" "$auction_id" \
    "$expected_version" <<'PY'
import hashlib
import sys
import time

import grpc
from google.protobuf import timestamp_pb2

from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc

address, operation, run_id, auction_id, expected_version = sys.argv[1:]
with grpc.insecure_channel(address) as channel:
    grpc.channel_ready_future(channel).result(timeout=10)
    stub = pb2_grpc.AuctionServiceStub(channel)
    if operation == "create":
        ends_at = timestamp_pb2.Timestamp()
        ends_at.FromSeconds(int(time.time()) + 3600)
        response = stub.CreateAuction(
            pb2.CreateAuctionRequest(
                seller_id=f"backup-loss-seller-{run_id}",
                title=f"Backup loss durability {run_id}",
                category="evaluation",
                description="Known state for backup-loss validation",
                reserve_price=100.0,
                ends_at=ends_at,
                request_id=f"{run_id}:create",
            ),
            timeout=10,
        )
        if not response.ok:
            raise SystemExit(
                f"create rejected: {response.message}; "
                f"retryable={response.retryable}; "
                f"outcome_unknown={response.outcome_unknown}"
            )
        auction_id = response.auction_id
    elif operation == "bid":
        response = stub.PlaceBid(
            pb2.BidRequest(
                auction_id=auction_id,
                bidder_id=f"backup-loss-bidder-{run_id}",
                amount=125.0,
                expected_version=int(expected_version),
                request_id=f"{run_id}:bid:{expected_version}",
            ),
            timeout=10,
        )
        print(
            "WRITE\t"
            f"{int(response.success)}\t{int(response.retryable)}\t"
            f"{int(response.outcome_unknown)}\t{response.message}"
        )
        raise SystemExit(0)

    fetched = stub.GetAuction(
        pb2.GetAuctionRequest(auction_id=auction_id),
        timeout=10,
    )
    if not fetched.ok:
        raise SystemExit(f"read failed: {fetched.message}")
    payload = fetched.auction.SerializeToString(deterministic=True)
    print(
        "STATE\t"
        f"{fetched.auction.auction_id}\t{fetched.auction.bidder_count}\t"
        f"{hashlib.sha256(payload).hexdigest()}"
    )
PY
}

snapshot_state() {
  local service="$1"
  local auction_id="$2"

  compose exec -T "$service" python -c '
import hashlib
import sys
from blindsided.generated import blindsided_pb2 as pb2

snapshot = pb2.StorageSnapshot()
with open("/var/lib/blindsided/auction-state.pb", "rb") as state_file:
    snapshot.ParseFromString(state_file.read())
auction = next(
    (item for item in snapshot.auctions if item.auction_id == sys.argv[1]),
    None,
)
if auction is None:
    raise SystemExit(f"auction {sys.argv[1]} missing from durable snapshot")
payload = auction.SerializeToString(deterministic=True)
print(f"{auction.version}\t{hashlib.sha256(payload).hexdigest()}")
' "$auction_id"
}

wait_for_metric() {
  local description="$1"
  local timeout="$2"
  local query="$3"
  local expected="$4"
  local deadline=$((SECONDS + timeout))
  local actual

  while true; do
    actual="$(metric_value "$query")"
    if [[ "$actual" == "$expected" ]]; then
      return 0
    fi
    if (( SECONDS >= deadline )); then
      fail "$description did not reach '$expected' within ${timeout}s (last='$actual')"
    fi
    sleep "$POLL_INTERVAL"
  done
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
primary_service="$(service_for_instance "$primary_instance")" \
  || fail "Cannot map primary instance '$primary_instance' to a Compose service"
standby_service="$(service_for_instance "$standby_instance")" \
  || fail "Cannot map standby instance '$standby_instance' to a Compose service"

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
create_output="$(auction_rpc create)"
IFS=$'\t' read -r state_label auction_id public_bidder_count public_hash \
  <<< "$create_output"
[[ "$state_label" == "STATE" && "$public_bidder_count" == "0" ]] \
  || fail "Could not establish known auction state: $create_output"

primary_before="$(snapshot_state "$primary_service" "$auction_id")"
backup_before="$(snapshot_state "$failed_service" "$auction_id")"
[[ "$primary_before" == "$backup_before" ]] \
  || fail "Known state differs between primary and designated backup before failure"
IFS=$'\t' read -r committed_version committed_hash <<< "$primary_before"
[[ "$committed_version" == "1" ]] \
  || fail "Known auction committed at unexpected version $committed_version"
printf '[ok] Auction %s is durably identical at version %s on primary and backup.\n' \
  "$auction_id" "$committed_version"

printf '\nStopping designated synchronous backup %s (%s)...\n' \
  "$failed_service" \
  "$backup_instance"

compose stop "$failed_service" >/dev/null
backup_stopped=true

printf 'Submitting a mutation without the designated synchronous backup...\n'

write_output="$(auction_rpc bid "$auction_id" "$committed_version")"
IFS=$'\t' read -r write_label write_success write_version \
  write_failure_reason write_message <<< "$write_output"
[[ "$write_label" == "WRITE" && "$write_success" == "0" ]] \
  || fail "Mutation was acknowledged without its designated synchronous backup"

printf '[ok] Mutation was not acknowledged without its designated synchronous backup.\n'

primary_after_rejection="$(snapshot_state "$primary_service" "$auction_id")"
[[ "$primary_after_rejection" == "$primary_before" ]] \
  || fail "Rejected write changed the primary's durable state"
printf '[ok] Rejected write left durable primary state unchanged at version %s.\n' \
  "$committed_version"

printf 'Waiting for health detection, standby synchronization, and readiness...\n'

deadline=$((SECONDS + RECOVERY_TIMEOUT))

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
      "Backup failure did not produce a healthy synchronized replacement within ${RECOVERY_TIMEOUT} seconds"
  fi

  sleep "$POLL_INTERVAL"
done

printf '\nVerifying synchronous writes after replacement-backup recovery...\n'

replacement_service="$standby_service"
replacement_before_write="$(snapshot_state "$replacement_service" "$auction_id")"
[[ "$replacement_before_write" == "$primary_before" ]] \
  || fail "Replacement synchronization completed but durable auction state differs"

recovered_write="$(auction_rpc bid "$auction_id" "$committed_version")"
IFS=$'\t' read -r write_label write_success resumed_version \
  resumed_failure_reason resumed_message <<< "$recovered_write"
[[ "$write_label" == "WRITE" && "$write_success" == "1" ]] \
  || fail "Writes did not resume after replacement synchronization: $recovered_write"

primary_after_recovery="$(snapshot_state "$primary_service" "$auction_id")"
replacement_after_recovery="$(snapshot_state "$replacement_service" "$auction_id")"
[[ "$primary_after_recovery" == "$replacement_after_recovery" ]] \
  || fail "Recovered write is not durably identical on primary and replacement backup"
[[ "$primary_after_recovery" == 2$'\t'* ]] \
  || fail "Recovered durable state has an unexpected version"
printf '[ok] Writes resumed and committed identically at version 2.\n'

printf '\nRestarting failed node and verifying safe standby rejoin...\n'
compose start "$failed_service" >/dev/null

wait_for_metric \
  "rejoined storage target" \
  "$REJOIN_TIMEOUT" \
  "up{job=\"storage\",instance=\"$backup_instance\"}" \
  "1"

deadline=$((SECONDS + REJOIN_TIMEOUT))
stable_observations=0
while true; do
  active_replacement="$(
    metric_value "
      (blindsided_storage_role{job=\"storage\",instance=\"$standby_instance\",role=\"backup\"} == 1)
      and on(instance)
      (blindsided_storage_ready{job=\"storage\",instance=\"$standby_instance\"} == 1)
    "
  )"
  rejoined_ready="$(
    metric_value "
      blindsided_storage_ready{job=\"storage\",instance=\"$backup_instance\"}
    "
  )"
  cluster_ready="$(metric_value 'blindsided_cluster_ready{job="controller"}')"

  if [[ "$active_replacement" == "1" ]] \
    && [[ "$rejoined_ready" == "0" ]] \
    && [[ "$cluster_ready" == "1" ]]; then
    stable_observations=$((stable_observations + 1))
    if (( stable_observations >= 3 )); then
      break
    fi
  else
    stable_observations=0
  fi

  if (( SECONDS >= deadline )); then
    fail "Rejoined node displaced the active backup or was trusted before synchronization"
  fi
  sleep "$POLL_INTERVAL"
done
failed_service=""
backup_stopped=false
printf '[ok] Rejoined node remained non-ready while the active replacement stayed designated.\n'

health_after="$(
  metric_value '
    sum(blindsided_replica_health_transitions_total{
      transition="healthy_to_unhealthy"
    }) or vector(0)
  '
)"
sync_after="$(
  metric_value '
    sum(blindsided_synchronization_attempts_total{
      outcome="completed"
    }) or vector(0)
  '
)"
replication_failures="$(
  metric_value '
    sum(blindsided_replication_attempts_total{outcome="failed"}) or vector(0)
  '
)"
commit_successes="$(
  metric_value '
    sum(blindsided_commits_total{outcome="committed"}) or vector(0)
  '
)"

printf '\nObserved metrics:\n'
printf '  healthy_to_unhealthy: %s -> %s\n' "$health_before" "$health_after"
printf '  synchronization completed: %s -> %s\n' "$sync_before" "$sync_after"
printf '  replication failures (total): %s\n' "$replication_failures"
printf '  committed writes (total): %s\n' "$commit_successes"

printf '\nBackup failure scenario completed successfully.\n'
