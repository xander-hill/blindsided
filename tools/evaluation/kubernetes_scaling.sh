#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
NAMESPACE="${NAMESPACE:-blindsided}"
TIMEOUT="${TIMEOUT:-180}"
POLL_INTERVAL="${POLL_INTERVAL:-2}"

fail() {
  printf 'ERROR: transition=%s\n' "$*" >&2
  exit 1
}

command -v kubectl >/dev/null || fail "prerequisite: kubectl is unavailable"

printf 'Timeline:\n'
storage_replicas="$(
  awk '
    /^kind:[[:space:]]*StatefulSet[[:space:]]*$/ { statefulset=1 }
    statefulset && /^[[:space:]]*replicas:[[:space:]]*[0-9]+/ {
      print $2
      exit
    }
  ' "$ROOT/deploy/kubernetes/storage.yaml"
)"
[[ "$storage_replicas" == "3" ]] \
  || fail "manifest-validation: storage StatefulSet replicas=$storage_replicas, expected 3"
printf '  manifests → storage StatefulSet fixed at three replicas\n'

hpa_names="$(
  grep -R -l \
    '^kind:[[:space:]]*HorizontalPodAutoscaler[[:space:]]*$' \
    "$ROOT/deploy/kubernetes" || true
)"
[[ -n "$hpa_names" ]] \
  || fail "autoscaling-policy: no HorizontalPodAutoscaler exists for service-node"

kubectl apply -f "$ROOT/deploy/kubernetes"
kubectl -n "$NAMESPACE" rollout status statefulset/storage --timeout="${TIMEOUT}s"
kubectl -n "$NAMESPACE" rollout status deployment/service-node --timeout="${TIMEOUT}s"

initial="$(
  kubectl -n "$NAMESPACE" get deployment service-node \
    -o jsonpath='{.status.readyReplicas}'
)"
deadline=$((SECONDS + TIMEOUT))
while true; do
  current="$(
    kubectl -n "$NAMESPACE" get deployment service-node \
      -o jsonpath='{.status.readyReplicas}'
  )"
  if (( current > initial )); then
    break
  fi
  (( SECONDS < deadline )) \
    || fail "scale-up: service-node did not scale above $initial replicas"
  sleep "$POLL_INTERVAL"
done

[[ "$(
  kubectl -n "$NAMESPACE" get statefulset storage \
    -o jsonpath='{.spec.replicas}'
)" == "3" ]] || fail "storage-invariant: StatefulSet no longer has three replicas"

printf '  service tier scaled %s → %s; storage remained fixed at 3\n' \
  "$initial" "$current"
printf 'Metrics: verify request success/latency and pod readiness during the interval.\n'
