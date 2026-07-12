# ADR-005: Use Primary-Backup Replication

- **Status:** Accepted
- **Date:** 2026-07-12 (Retrospectively documented)
- **Decision Type:** Architectural Decision
- **Related ADRs:**
  - ADR-000: Adopt Project Architectural Constraints
  - ADR-003: Use a Centralized Controller for Cluster Coordination
  - ADR-004: Separate Request Handling from Replicated Storage

## Context

Blindsided requires replicated storage so that auction state remains available after a storage-node failure.

The project specification allowed multiple replication strategies, including primary-backup and quorum-based replication. The system therefore needed to choose how writes would be ordered, how replicas would remain consistent, and
how clients would interact with the replica group.

## Decision

Use a primary-backup replication model for authoritative auction state.

One storage replica acts as the primary and processes authoritative mutations. A designated backup receives replicated updates from the primary and maintains a synchronized copy of committed state.

The controller tracks replica membership and the current primary assignment. Service instances direct authoritative operations to the primary rather than allowing independent writes to multiple replicas.

## Alternatives Considered

### Quorum-Based Replication

Reads and writes would be sent to multiple replicas and accepted after reaching the required quorum.

**Advantages**

- Can tolerate some replica failures without a single fixed writer.
- Supports configurable read and write availability.
- Commonly used in distributed key-value systems.

**Disadvantages**

- Requires more complex conflict detection and version reconciliation.
- Makes auction-specific ordering and winner determination harder to reason about.
- Increases coordination work in the service or storage layer.
- More difficult to demonstrate and test within the project scope.

### Single Unreplicated Storage Node

All authoritative state would be stored on one node.

**Advantages**

- Simplest implementation.
- No replication or synchronization overhead.
- Straightforward ordering of operations.

**Disadvantages**

- Storage-node failure would make the system unavailable.
- Committed state could be lost.
- Does not satisfy the project's fault-tolerance goals.

### Multi-Primary Replication

Multiple replicas would accept writes concurrently.

**Advantages**

- Higher write availability.
- Reduced dependency on one active writer.

**Disadvantages**

- Requires conflict resolution between concurrent writes.
- Makes deterministic auction ordering significantly more complex.
- Increases the risk of divergent histories.
- Does not fit the project's emphasis on simple, strongly ordered mutation handling.

## Rationale

Primary-backup replication provides a clear authoritative write path and a single ordered history for each auction.

That model fits the auction domain because bids, withdrawals, reveals, and version changes must be applied deterministically. A single primary simplifies concurrency control and avoids reconciling conflicting writes accepted by different replicas.

Compared with quorum or multi-primary designs, primary-backup is easier to reason about, test, and explain while still demonstrating replication, synchronization, failure detection, and recovery.

## Consequences

### Positive

- Produces one authoritative ordering of auction mutations.
- Simplifies optimistic concurrency and version management.
- Avoids write-conflict reconciliation across replicas.
- Makes failover behavior easier to reason about and test.
- Keeps replication logic localized within the storage tier.
- Fits the project's scope while still demonstrating fault tolerance.

### Negative

- The primary is a write bottleneck.
- Write availability depends on the primary and the replication policy.
- Promotion must be handled carefully to avoid stale or conflicting primaries.
- Replica synchronization and failover correctness become critical.
- Read scaling is more limited when authoritative reads must use the primary.

## Implementation Notes

The current repository assigns one storage node as primary and one or more nodes as backups. The primary processes mutations and propagates state changes to its backup.

The controller maintains the primary assignment and monitors replica health. Later ADRs document the synchronization, failover, and concurrency mechanisms built on top of this replication model.

## References

- ADR-000: Adopt Project Architectural Constraints
- ADR-003: Use a Centralized Controller for Cluster Coordination
- ADR-004: Separate Request Handling from Replicated Storage
- CSCI 5105 Project #3 Specification
