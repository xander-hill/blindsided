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
