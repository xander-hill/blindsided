# ADR-010: Bootstrap Replicas Using Full-State Synchronization

- **Status:** Accepted
- **Date:** 2026-07-12 (Retrospectively documented)
- **Decision Type:** Architectural Decision
- **Related ADRs:**
  - ADR-005: Use Primary-Backup Replication
  - ADR-008: Use StatefulSets for Storage Replicas

## Context

When a new storage replica joins the cluster or an existing replica recovers from failure, it must obtain an authoritative copy of the current auction state before participating in replication.

The architecture required a replica initialization strategy that was simple, deterministic, and appropriate for the project's scope.

## Decision

Initialize new or recovering storage replicas by performing a full-state synchronization from the current primary.

A replica does not participate in normal replication until the synchronization process has completed.

## Alternatives Considered

### Incremental Log Replay

Replicas would receive only the operations missed while offline.

**Advantages**

- Less network traffic.
- Faster synchronization for small gaps.
- Common production approach.

**Disadvantages**

- Requires durable operation logs.
- More complex recovery protocol.
- Additional log management responsibilities.

### Snapshot Plus Incremental Catch-Up

Replicas would restore a snapshot before replaying recent updates.

**Advantages**

- Reduces synchronization time for large datasets.
- Common enterprise recovery strategy.

**Disadvantages**

- Requires snapshot management.
- Introduces multiple recovery phases.
- Significantly increases implementation complexity.

## Rationale

For the current project size, full-state synchronization provides a simple and predictable recovery mechanism.

The implementation avoids maintaining operation logs or snapshot lifecycles while still demonstrating replica recovery, synchronization, and rejoining of the replication group. The approach favors architectural clarity over recovery efficiency.

## Consequences

### Positive

- Simple recovery protocol.
- Deterministic replica initialization.
- No dependency on operation logs.
- Easy to explain and test.
- Suitable for the project's data volume.

### Negative

- Entire state must be transferred for every recovery.
- Recovery time increases as stored state grows.
- Less efficient than incremental synchronization for large datasets.

## Implementation Notes

When a storage node joins or recovers, the current primary transfers its authoritative auction state to the replica. Only after synchronization completes does the replica resume participation in the replication group.

Future iterations may replace this mechanism with snapshot-based or log-based recovery if scalability requirements increase.

## References

- ADR-005: Use Primary-Backup Replication
- ADR-008: Use StatefulSets for Storage Replicas
