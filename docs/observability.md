# Observability

Blindsided exposes Prometheus metrics from the auction-service process. Metric
labels are intentionally bounded and never contain request, auction, or user
identifiers.

## Mutation outcomes

`blindsided_mutations_total` is a counter labeled by `operation` and `outcome`.
It records exactly one final client-visible outcome for each external logical
mutation in the auction service. Operations are `CreateAuction`, `PlaceBid`,
`WithdrawBid`, and `RevealAuction`. Allowed outcomes are `committed`,
`rejected`, `conflict`, `unknown`, `unavailable`, and `failure`.

The authoritative update point is the completion of the corresponding auction
service handler. Internal storage attempts and retries are not counted. A
failure returned without structured semantics is conservatively classified as
`rejected`; explicit transport failures handled by the service are `failure`.

```promql
sum by (operation, outcome) (rate(blindsided_mutations_total[5m]))
```

## Optimistic-concurrency retries

`blindsided_concurrency_retries_total` is a counter labeled by `operation` and
`outcome`. It is updated only in the auction service's version-conflict retry
loops. Allowed outcomes are `retried`, `succeeded_after_retry`, and `exhausted`.

Each actual attempt after the initial attempt increments `retried` once. A
logical request that commits after one or more such retries increments
`succeeded_after_retry` once. A final concurrency conflict at the retry bound
increments `exhausted` once. Transport and failover retries are excluded.

```promql
sum by (operation, outcome) (rate(blindsided_concurrency_retries_total[5m]))
```

## Idempotency decisions

`blindsided_idempotency_requests_total` is a counter labeled by `operation` and
`outcome`. It is updated by the primary storage replica at the authoritative
idempotency-record lookup. Allowed outcomes are `new`, `replayed`, and
`mismatch`.

`new` means no committed record exists and processing continues. `replayed`
means the request ID and fingerprint match an existing record. `mismatch`
means the request ID exists with a different fingerprint. The auction service
records the final authoritative decision returned by storage exactly once per
external logical mutation. Internal retries and replication prepare/commit
application do not update this metric. Requests that never reach authoritative
storage have no idempotency decision and are not counted.

```promql
sum by (operation, outcome) (rate(blindsided_idempotency_requests_total[5m]))
```

## Synchronous replication attempts

`blindsided_replication_attempts_total` is a counter labeled by `operation` and
`outcome`. The primary storage process emits one sample for every actual
candidate-bearing `PrepareAuctionMutation` RPC to its designated synchronous
backup. Operations use the four external mutation RPC names. Allowed outcomes
are `success`, `timeout`, `rejected`, `unreachable`, and `failure`.

This metric excludes local candidate construction, backup commit/abort control
messages, controller health traffic, and failover synchronization. The current
`PrepareMutationResponse` has no structured stale-epoch reason, so stale-epoch
rejections are included in `rejected`; messages are deliberately not parsed.
The current replication client does not retry prepare internally, so there is
one attempt per coordinator invocation.

```promql
sum by (operation, outcome) (rate(blindsided_replication_attempts_total[5m]))
```

```promql
sum by (outcome) (rate(blindsided_replication_attempts_total{outcome!="success"}[5m]))
```

## Synchronous replication duration

`blindsided_replication_duration_seconds` is a histogram labeled by `operation`
and `outcome`. It measures the complete outbound candidate-prepare call, from
immediately before channel creation until response or exception classification.
The counter and histogram use the same outcome and emit once per actual attempt.
Buckets are tuned for local synchronous calls from 5 ms through 5 seconds.

```promql
histogram_quantile(0.95, sum by (le, operation, outcome) (rate(blindsided_replication_duration_seconds_bucket[5m])))
```

## Primary commit outcomes

`blindsided_commits_total` is a primary-only counter labeled by `operation` and
`outcome`. `committed` means candidate preparation, durable primary decision,
and designated-backup acknowledgement all succeeded. `aborted` means the
coordinator stopped before a durable commit. `unknown` means the primary made a
durable decision but backup acknowledgement remains pending—the protocol's
explicit uncertain client outcome.

The primary coordinator emits exactly one outcome for each mutation reaching
commit coordination. Backup application, candidate-validation failures,
idempotent replay, and external auction-service mutation outcomes are separate
concerns and do not increment this counter.

```promql
sum by (operation, outcome) (rate(blindsided_commits_total[5m]))
```

```promql
sum(rate(blindsided_commits_total{outcome="committed"}[5m])) / sum(rate(blindsided_commits_total[5m]))
```

## Controller cluster state

The controller is the authoritative emitter for four unlabeled gauges:

- `blindsided_registered_replicas` is the size of controller membership.
- `blindsided_healthy_replicas` counts members with no outstanding heartbeat
  failures.
- `blindsided_cluster_ready` is `1` only for an assignment in the existing
  `READY` state and `0` while absent or promoting.
- `blindsided_primary_epoch` is the controller's authoritative last primary
  epoch, initially `0`.

They are derived under the controller state lock after existing transitions;
there is no metrics refresh thread. First-ever primary assignment updates the
gauges but is not counted as failover.

```promql
blindsided_cluster_ready
```

```promql
blindsided_registered_replicas
```

```promql
blindsided_healthy_replicas
```

```promql
blindsided_primary_epoch
```

## Replica health transitions

`blindsided_replica_health_transitions_total{transition}` records authoritative
state changes only. Allowed transitions are `registered`,
`healthy_to_unhealthy`, `unhealthy_to_healthy`, and `removed`. The first failed
probe marks a member unhealthy; repeated failures do not repeat that transition.
Re-registration or a successful heartbeat restores health. Eviction records a
separate removal transition.

```promql
sum by (transition) (increase(blindsided_replica_health_transitions_total[1h]))
```

## Failover attempts

`blindsided_failovers_total{outcome}` and
`blindsided_failover_duration_seconds{outcome}` emit once per logical recovery
cycle. Timing begins when `_elect_new_primary` enters recovery after an existing
epoch and ends with `completed`, `failed`, or `abandoned`. Candidate retries stay
within the same logical failover. Completion means the assignment crossed the
existing promotion and replacement-backup barrier; exhaustion is `failed`; an
ambiguous completion RPC is conservatively `abandoned`.

Attempt timing is retained across candidate retries, while promotion timing is
keyed by candidate address and epoch. Existing assignment guards reject stale
callbacks before metric completion, and terminal records are removed after one
emission.

```promql
sum by (outcome) (increase(blindsided_failovers_total[1h]))
```

```promql
histogram_quantile(0.95, sum by (le, outcome) (rate(blindsided_failover_duration_seconds_bucket[1h])))
```

## Promotion attempts

`blindsided_promotion_attempts_total{outcome}` and
`blindsided_promotion_duration_seconds{outcome}` record one terminal result per
candidate/epoch attempt. Allowed outcomes are `completed`, `rejected`,
`timeout`, `failed`, and `abandoned`. Explicit begin, confirmation, or completion
rejections collapse to `rejected`; non-deadline RPC and unexpected failures
collapse to `failed`; ambiguous completion is `abandoned`.

```promql
sum by (outcome) (increase(blindsided_promotion_attempts_total[1h]))
```

## Replacement-backup synchronization

`blindsided_synchronization_attempts_total{outcome}` and
`blindsided_synchronization_duration_seconds{outcome}` measure each actual
`SynchronizeFromPrimary` attempt during the promotion barrier. Allowed outcomes
are `completed`, `rejected`, `timeout`, and `failed`. Each deterministic backup
retry emits separately. Non-deadline transport errors and unexpected failures
collapse to `failed`. Ordinary synchronous mutation replication is excluded.

```promql
sum by (outcome) (increase(blindsided_synchronization_attempts_total[1h]))
```

Controller recovery events are intentionally low-frequency; `increase()` over
demo-sized windows may be more useful than short `rate()` windows.

## Storage process state

`blindsided_storage_role{role}` is a process-local one-hot gauge. Its bounded
roles are `primary`, `backup`, and `unassigned`; exactly one series is `1` and
the others are `0`. It is refreshed from authoritative storage state at startup
registration, promotion begin or rollback, and retained synchronization state.
No node address, assignment, auction, or epoch is an application label.

`blindsided_storage_ready` is an unlabeled process-local gauge. It follows the
existing barriers: a newly promoted primary is not ready until promotion
completion persists its synchronous backup, and a backup is not ready while
full-state synchronization is in progress. A backup becomes ready only after
state replacement, controller completion reporting, and primary configuration
all succeed.

`blindsided_storage_epoch` is the current assigned epoch, initially `0` before
registration. Role, readiness, and epoch are published together under the
storage state lock. Older promotion requests and synchronization callbacks
whose role or epoch identity no longer matches return without overwriting a
newer metric snapshot.

```promql
blindsided_storage_role
```

```promql
sum(blindsided_storage_ready)
```

```promql
blindsided_storage_epoch
```

Prometheus target labels such as `instance` and `job` distinguish storage
processes; no high-cardinality process identity label is added by the service.

## Auction watch streams

`blindsided_active_watch_streams` is an unlabeled gauge of currently active
`WatchAuction` generators in each auction-service process. It increments when
iteration accepts a stream and decrements from a `finally` block on every exit.

`blindsided_watch_streams_total{outcome}` records exactly one terminal outcome:
`completed` when the revealed update closes the stream or its active deadline
ends normally, `cancelled` when `context.is_active()` becomes false or the
generator is closed, and `failure` when an unexpected active-handler exception
propagates. Cancellation classification uses structured context state and
`GeneratorExit`; exception messages are not inspected.

`blindsided_watch_updates_total` is an unlabeled counter incremented once just
before each public update is yielded. The current protocol has no structured
bounded update type, so `update_type` is deliberately omitted. Auction and
client identifiers are never labels.

```promql
sum(blindsided_active_watch_streams)
```

```promql
sum by (outcome) (rate(blindsided_watch_streams_total[5m]))
```

```promql
sum(rate(blindsided_watch_updates_total[5m]))
```
