# ADR-013: Use Primary-Authoritative Reads

- **Status:** Accepted
- **Date:** 2026-07-16
- **Decision Type:** Architectural Decision
- **Related ADRs:**
  - ADR-003: Use a Centralized Controller for Cluster Coordination
  - ADR-004: Separate Request Handling from Replicated Storage
  - ADR-005: Use Primary-Backup Replication

## Context

Blindsided uses primary-backup replication to maintain authoritative
auction state.

Not all read operations have the same consistency requirements.
Some reads are used to validate mutations, determine auction outcomes,
publish live auction updates, and expose authoritative auction state to
clients. Other operations, such as auction discovery and search, may
tolerate temporary replica lag.

Because backup replicas may temporarily lag the primary during normal
replication, recovery, or failover events, serving correctness-sensitive
reads from arbitrary replicas could expose stale auction state and
produce inconsistent behavior.

The auction specification therefore distinguishes between
authoritative reads and stale-tolerant discovery reads.

## Decision

Use the current primary as the authoritative read source.

The following operations are classified as authoritative reads:

- auction status retrieval
- bidder count retrieval
- bidder active bid retrieval
- reveal status retrieval
- auction outcome retrieval
- reads used during mutation validation
- live auction updates

Authoritative reads are served from the current primary, or from a
source proven to contain the same committed state.

Replica reads may be used only for stale-tolerant discovery operations,
including auction search and listing operations.

Service nodes are responsible for routing reads according to these
consistency requirements.

## Alternatives Considered

### Serve All Reads from Replicas

All reads would be distributed across storage replicas regardless of
their consistency requirements.

**Advantages**

- Improves read scalability.
- Reduces load on the primary.
- Simpler routing logic.

**Disadvantages**

- May expose stale auction state.
- Risks incorrect mutation validation.
- Can produce inconsistent auction outcomes.
- Complicates reasoning about client-visible state.

### Client-Selectable Consistency Levels

Clients would choose whether a read requires strong or relaxed
consistency.

**Advantages**

- Flexible API.
- Allows clients to trade consistency for performance.

**Disadvantages**

- Pushes distributed-systems concerns into client code.
- Increases API complexity.
- Creates opportunities for incorrect client behavior.

## Rationale

Auction correctness depends on certain reads observing authoritative
committed state.

Routing authoritative reads to the current primary provides a simple and
predictable consistency model that aligns with the primary-backup
architecture. At the same time, allowing stale-tolerant discovery
operations to use replicas preserves flexibility for future read
scaling.

This approach keeps consistency requirements explicit and avoids
exposing replication details to clients.

## Consequences

### Positive

- Mutation validation observes authoritative state.
- Auction outcomes are derived from committed data.
- Live updates reflect committed auction state.
- Read consistency behavior is simple and predictable.
- Replica reads remain available for discovery workloads.

### Negative

- Authoritative reads depend on primary availability.
- Some reads may fail temporarily during failover.
- Read scalability for correctness-sensitive operations is limited by
  the primary.

## Implementation Notes

Service nodes classify reads as either authoritative or
stale-tolerant.

Authoritative reads are routed to the current primary assignment
provided by the controller.

Search and listing operations may use replica reads provided visibility
rules continue to be enforced.

Live auction updates are generated from committed primary state.

## References

- ADR-003: Use a Centralized Controller for Cluster Coordination
- ADR-004: Separate Request Handling from Replicated Storage
- ADR-005: Use Primary-Backup Replication
- Auction Specification §8: Read Consistency
