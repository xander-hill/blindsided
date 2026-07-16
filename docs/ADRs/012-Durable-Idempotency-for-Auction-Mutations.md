# ADR-012: Durable Idempotency for Auction Mutations

## Status

Accepted

## Context

Auction mutations may be retried due to:

- client timeouts
- lost responses
- failover
- service-layer retries

Optimistic concurrency prevents conflicting writes but does not prevent
the same logical mutation from being applied multiple times.

The Auction Specification requires:

- at-most-once mutation application
- duplicate requests return the original result
- idempotency state survives replication and failover

## Decision

Every mutation request MUST include a unique `request_id`.

The storage layer is the authoritative owner of idempotency.

Committed mutations create an idempotency record containing:

- request identifier
- request fingerprint
- original mutation result

Idempotency records are treated as replicated auction state and MUST:

- replicate to the synchronous backup
- participate in state synchronization
- persist across restart
- survive failover

Duplicate requests with the same identifier and same contents return the
original committed result without reapplying the mutation.

Duplicate requests with the same identifier but different contents are
rejected.

## Consequences

### Positive

- Guarantees at-most-once mutation application.
- Supports safe retries after timeout or failover.
- Prevents duplicate auction creation and bid application.
- Preserves auction version correctness.
- Satisfies Specification §7 requirements.

### Negative

- Adds replicated metadata.
- Increases replication and persistence complexity.
- Requires idempotency state transfer during synchronization.

## Alternatives Considered

### Service-Layer Idempotency

Rejected because service nodes are not authoritative and may be replaced
or scaled.

### Primary-Only Idempotency

Rejected because request history would be lost during failover.

## Related

- ADR-011: Optimistic Concurrency Control
- Auction Specification §7 (Idempotency)
- Auction Specification §10 (Failover)
