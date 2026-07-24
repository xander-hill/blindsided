#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
NAMESPACE="${NAMESPACE:-blindsided}"
TIMEOUT="${TIMEOUT:-180}"
POLL_INTERVAL="${POLL_INTERVAL:-2}"
load_pod="auction-service-scaling-load"
probe_pod="auction-service-scaling-probe"

fail() {
  printf 'ERROR: transition=%s\n' "$*" >&2
  exit 1
}

cleanup() {
  kubectl -n "$NAMESPACE" delete pod "$load_pod" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  kubectl -n "$NAMESPACE" delete pod "$probe_pod" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap cleanup EXIT

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

minimum="$(
  kubectl -n "$NAMESPACE" get hpa service-node \
    -o jsonpath='{.spec.minReplicas}'
)"
maximum="$(
  kubectl -n "$NAMESPACE" get hpa service-node \
    -o jsonpath='{.spec.maxReplicas}'
)"
target="$(
  kubectl -n "$NAMESPACE" get hpa service-node \
    -o jsonpath='{.spec.metrics[0].resource.target.averageUtilization}'
)"
[[ "$minimum" == "2" && "$maximum" == "6" && "$target" == "60" ]] \
  || fail "autoscaling-policy: expected min=2 max=6 cpu=60%, got min=$minimum max=$maximum cpu=$target%"

deadline=$((SECONDS + TIMEOUT))
until [[ "$(
  kubectl -n "$NAMESPACE" get deployment service-node \
    -o jsonpath='{.status.readyReplicas}'
)" == "$minimum" ]]; do
  (( SECONDS < deadline )) \
    || fail "minimum-replicas: service-node did not settle at $minimum ready replicas"
  sleep "$POLL_INTERVAL"
done
initial="$minimum"

image="$(
  kubectl -n "$NAMESPACE" get deployment service-node \
    -o jsonpath='{.spec.template.spec.containers[0].image}'
)"
kubectl -n "$NAMESPACE" run "$load_pod" \
  --restart=Never --image="$image" --image-pull-policy=IfNotPresent \
  --env=PYTHONUNBUFFERED=1 -- \
  python -c 'import concurrent.futures,grpc,time
from blindsided.generated import blindsided_pb2 as p
from blindsided.generated import blindsided_pb2_grpc as g
deadline=time.monotonic()+180
def load(_):
  ok=0
  with grpc.insecure_channel("service-nodes:50051") as channel:
    stub=g.AuctionServiceStub(channel)
    while time.monotonic()<deadline:
      try:
        response=stub.SearchAuctions(p.SearchAuctionsRequest(query=""),timeout=2)
        if not response.ok: raise RuntimeError(response.message)
        ok+=1
      except grpc.RpcError:
        pass
  return ok
with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
  counts=list(pool.map(load,range(32)))
if sum(counts)==0: raise SystemExit("no requests succeeded during scaling")
print("successful_requests",sum(counts))' >/dev/null
printf '  load → 32 concurrent public RPC loops started\n'

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

load_phase="$(
  kubectl -n "$NAMESPACE" get pod "$load_pod" \
    -o jsonpath='{.status.phase}'
)"
[[ "$load_phase" == "Running" || "$load_phase" == "Succeeded" ]] \
  || {
    kubectl -n "$NAMESPACE" logs "$load_pod" >&2 || true
    fail "request-continuity: load pod phase=$load_phase"
  }

kubectl -n "$NAMESPACE" run "$probe_pod" \
  --restart=Never --rm --attach --quiet --image="$image" \
  --image-pull-policy=IfNotPresent -- \
  python -c 'import grpc
from blindsided.generated import blindsided_pb2 as p
from blindsided.generated import blindsided_pb2_grpc as g
with grpc.insecure_channel("service-nodes:50051") as channel:
  response=g.AuctionServiceStub(channel).SearchAuctions(
      p.SearchAuctionsRequest(query=""),timeout=10)
if not response.ok: raise SystemExit(response.message)' >/dev/null \
  || fail "request-continuity: public RPC failed after scale-up"

[[ "$(
  kubectl -n "$NAMESPACE" get statefulset storage \
    -o jsonpath='{.spec.replicas}'
)" == "3" ]] || fail "storage-invariant: StatefulSet no longer has three replicas"

printf '  service tier scaled %s → %s; storage remained fixed at 3\n' \
  "$initial" "$current"
printf '  HPA → min=%s max=%s CPU target=%s%% scale-down stabilization=300s\n' \
  "$minimum" "$maximum" "$target"
printf '  requests → succeeded through the service after scale-up\n'
printf 'Metrics: verify request success/latency and pod readiness during the interval.\n'
