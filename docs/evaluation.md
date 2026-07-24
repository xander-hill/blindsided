# System evaluation scenarios

Blindsided's Docker Compose evaluation uses one controller, one stateless
auction service, three storage replicas, Envoy, Prometheus, and Grafana.
Drivers use public gRPC APIs, runtime metrics, and normal Compose operations;
they do not patch application behavior.

## Workloads

### Normal auction flow

Creates, reads, searches, bids, withdraws, rebids, and reveals an auction. It
checks public visibility, idempotent mutations, synchronous commit, and
deterministic final outcome.

### Concurrent bidding

Releases many version-1 bids at a barrier, then compares successful responses
with authoritative bidder count. This exercises OCC retries, conflicts,
idempotency, latency reporting, replication, and tie-breaking.

### Ambiguous outcome

Interrupts the response path around a mutation and replays the same request
identity. The accepted mutation is applied at most once and the durable receipt
provides a stable retry outcome.

## Failure and durability scenarios

### Backup failure

The driver identifies the designated synchronous backup from Prometheus,
stops it, proves that an unsafe mutation is not acknowledged, and verifies no
version advance. It waits for standby synchronization and restored readiness,
then confirms a protected mutation commits.

### Primary failover

The driver stops the metric-identified primary. It requires a higher epoch,
promotion of the prior synchronized backup, synchronization of a replacement
backup, survival of acknowledged state, and a successful post-recovery
mutation. The stopped replica is returned as a fenced standby.

### Restart durability

Storage is restarted without deleting its volume. Auction state, version,
acceptance order, and idempotency behavior must survive and produce the same
deterministic reveal result.

### Watch behavior

Multiple streams check initial and committed updates, pre-reveal privacy,
absence of events for rejected mutations, behavior through failover, and clean
cancellation.

## Observability and deployment

`observability_check.py` requires the expected Prometheus families to be
populated and all provisioned Grafana dashboards to be discoverable.
`kubernetes_scaling.sh` applies sustained load, observes HPA scale-out of the
service Deployment, and confirms that the storage StatefulSet remains fixed at
three replicas.

## Results

The final recorded closeout run passed the complete evaluation suite, the
ordered backup-failure, primary-failover, watch, and restart scenarios,
observability validation, and Kubernetes scaling validation. It accompanies
394 passing backend tests and 86 passing subtests.

Exact commands, prerequisites, expected metrics, and cleanup behavior are in
[the evaluation harness guide](../tools/evaluation/README.md). Test levels, CI
placement, and proof boundaries are in [the test plan](test-plan.md).
