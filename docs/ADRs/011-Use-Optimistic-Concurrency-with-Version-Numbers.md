# ADR-011: Use Optimistic Concurrency with Version Numbers

- **Status:** Accepted
- **Date:** 2026-07-12 (Retrospectively documented)
- **Decision Type:** Architectural Decision
- **Related ADRs:**
  - ADR-004: Separate Request Handling from Replicated Storage
  - ADR-005: Use Primary-Backup Replication

## Context

Multiple clients may attempt to modify the same auction concurrently. The system requires a mechanism to prevent lost updates while avoiding pessimistic locking that would reduce concurrency and increase coordination overhead.

The architecture therefore needed a concurrency strategy suitable for distributed request processing and replicated state.

## Decision

Use optimistic concurrency control based on monotonically increasing auction version numbers.

Each mutable auction carries a version. Mutation requests are validated against the expected version before being applied. Successful mutations advance the auction version, while stale requests are rejected and may be retried by the service layer using the latest authoritative state.

## Alternatives Considered

### Pessimistic Locking

**Advantages**

- Prevents conflicting updates before they occur.
- Straightforward conflict semantics.

**Disadvantages**

- Reduces concurrency.
- Introduces lock management and timeout concerns.
- Poor fit for distributed request processing.

### Last-Write-Wins

**Advantages**

- Simple implementation.
- Minimal coordination.

**Disadvantages**

- Can silently overwrite valid updates.
- Unsuitable for deterministic auction behavior.
- Loses information about conflicting mutations.

### Serialized Request Queue

**Advantages**

- Guarantees sequential execution.
- Simple conflict handling.

**Disadvantages**

- Limits throughput.
- Introduces a processing bottleneck.
- Reduces responsiveness under concurrent load.

## Rationale

Auction mutations are expected to conflict relatively infrequently, making optimistic concurrency a good fit. Most requests proceed without locking, while conflicting updates are detected through version validation.

This approach balances correctness with throughput and integrates naturally with the primary-backup replication model by ensuring the primary validates each mutation against the current authoritative state before replication.

## Consequences

### Positive

- High concurrency without long-lived locks.
- Detects conflicting updates explicitly.
- Prevents lost updates.
- Supports deterministic ordering of accepted mutations.
- Integrates well with primary-backup replication.

### Negative

- Conflicting requests require retries.
- Service layer must handle version conflicts.
- Additional version metadata must be maintained with each auction.
- Conflict frequency may increase under heavy contention.

## Implementation Notes

The current implementation associates a version number with mutable auction state. Storage nodes validate versions before applying updates, and successful mutations increment the version.

Future architectural work expands this foundation with bounded retries, request idempotency, and stronger mutation guarantees while preserving the core optimistic concurrency model.

## References

- ADR-004: Separate Request Handling from Replicated Storage
- ADR-005: Use Primary-Backup Replication
