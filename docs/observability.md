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
