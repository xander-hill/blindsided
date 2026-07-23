# System Evaluation

## Purpose

This document defines the workloads and failure scenarios used to
evaluate Blindsided's performance, concurrency behavior, replication,
and failover guarantees.

## Environment

- Docker Compose deployment
- 1 controller
- 1 auction service
- 3 storage replicas
- Prometheus scraping every 5 seconds
- Grafana dashboards for visualization

## Scenario 1 — Normal Auction Flow

### Goal

Verify normal request processing, synchronous replication, and commit
acknowledgement.

### Steps

1. Start the full Docker Compose stack.
2. Create an auction.
3. Retrieve the auction.
4. Place a bid.
5. Withdraw the bid.
6. Place another bid.
7. Reveal the auction.

### Expected Behavior

- All valid mutations succeed.
- Replication succeeds.
- Commits are acknowledged.
- Cluster readiness remains 1.
- No failover occurs.

### Metrics

- `blindsided_rpc_requests_total`
- `blindsided_rpc_duration_seconds`
- `blindsided_mutations_total`
- `blindsided_replication_attempts_total`
- `blindsided_commits_total`
- `blindsided_cluster_ready`

## Scenario 2 — Concurrent Bidding

### Goal

Evaluate optimistic-concurrency behavior under simultaneous bids.

### Steps

1. Create an open auction.
2. Submit concurrent bids from multiple bidders.
3. Wait for all requests to complete.
4. Retrieve the final authoritative auction state.

### Expected Behavior

- Accepted mutations are applied once.
- Stale-version operations retry according to existing policy.
- Exhausted retries return conflicts.
- Final state remains valid.
- Committed writes remain replicated.

### Metrics

- `blindsided_rpc_duration_seconds`
- `blindsided_mutations_total`
- `blindsided_concurrency_retries_total`
- `blindsided_replication_attempts_total`
- `blindsided_commits_total`

## Scenario 3 — Backup Failure During Writes

### Goal

Verify that mutations are not acknowledged without the designated
synchronous backup.

### Steps

1. Start with a ready cluster.
2. Stop the backup storage container.
3. Submit a mutation.
4. Observe the failure response and metrics.
5. Restart the backup.
6. Wait for the cluster to recover.
7. Submit another mutation.

### Expected Behavior

- Unsafe writes are not acknowledged.
- Failed writes do not advance the auction version.
- Replication failure is recorded.
- Commit outcome is aborted or unknown according to existing behavior.
- Writes resume only after readiness is restored.

### Metrics

- `blindsided_replication_attempts_total`
- `blindsided_replication_duration_seconds`
- `blindsided_commits_total`
- `blindsided_cluster_ready`
- `blindsided_synchronization_attempts_total`

## Scenario 4 — Primary Failure and Recovery

### Goal

Measure failover and verify that only a synchronized replica becomes
primary.

### Steps

1. Start with a ready cluster.
2. Create an auction and place at least one committed bid.
3. Stop the primary storage container.
4. Wait for controller failure detection and recovery.
5. Observe promotion and synchronization.
6. Confirm cluster readiness returns to 1.
7. Retrieve the auction.
8. Submit another valid mutation.

### Expected Behavior

- The cluster temporarily becomes unavailable.
- The primary epoch increases.
- A valid backup is promoted.
- A synchronized replacement backup is established.
- Previously committed state remains available.
- Writes resume only after the promotion barrier completes.

### Metrics

- `blindsided_healthy_replicas`
- `blindsided_cluster_ready`
- `blindsided_primary_epoch`
- `blindsided_failovers_total`
- `blindsided_failover_duration_seconds`
- `blindsided_promotion_attempts_total`
- `blindsided_synchronization_attempts_total`

## Result Recording

For each scenario, record:

| Scenario           | Request Count | p95 Latency | Failures | Recovery Time | Correctness Result |
| ------------------ | ------------: | ----------: | -------: | ------------: | ------------------ |
| Normal flow        |               |             |          |           N/A |                    |
| Concurrent bidding |               |             |          |           N/A |                    |
| Backup failure     |               |             |          |               |                    |
| Primary failure    |               |             |          |               |                    |
