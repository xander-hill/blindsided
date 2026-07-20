# ADR-014: Require Synchronous Backup Acknowledgement

- **Status:** Accepted
- **Date:** 2026-07-18
- **Decision Type:** Architectural Decision
- **Related ADRs:**
  - ADR-005: Use Primary-Backup Replication
  - ADR-011: Use Optimistic Concurrency with Version Numbers
  - ADR-012: Use Durable Idempotency for Auction Mutations
  - ADR-013: Use Primary-Authoritative Reads

## Context

Blindsided uses primary-backup replication to maintain authoritative
auction state.

Auction mutations include auction creation, bid placement, bid
withdrawal, and reveal. Losing an acknowledged mutation during failover
could change an auction version, active bid set, or final outcome.

Acknowledging a mutation after committing it only on the primary would
allow the mutation to be lost if the primary failed before replication
completed.

A simple apply-and-rollback sequence is also insufficient. If the backup
commits a mutation but its acknowledgement is lost, the primary cannot
safely determine whether the mutation should be rolled back.

The write protocol therefore needs to distinguish between prepared
state, a durable commit decision, and fully acknowledged state.

## Decision

Require every auction mutation to be committed by the primary and one
designated synchronous backup before returning success to the client.

Use a replication-specific prepare/commit protocol coordinated by the
primary:

- The primary validates the mutation and constructs the candidate
  auction state, response, and idempotency record without changing
  committed state.
- The designated backup durably records the candidate as prepared
  without exposing it through reads.
- After successful preparation, the primary durably records the commit
  decision, auction state, and idempotency record.
- The primary instructs the backup to commit the prepared mutation.
- The backup durably commits the auction and idempotency record and
  acknowledges the committed version.
- The primary returns success only after receiving the expected
  acknowledgement.

Before the primary records a durable commit decision, a failed mutation
may be aborted and must not advance committed auction state.

After the commit decision is durable, the mutation is irrevocable. The
system must retry backup commit completion rather than roll the mutation
back.

Prepared mutations, abort records, and pending backup commits are
persisted so that protocol state survives process restart. Prepare,
commit, and abort operations are idempotent and use the mutation request
identifier as their stable identity.

If the commit decision is durable but backup acknowledgement remains
unresolved, the client receives an acknowledgement-pending response and
must retry the same request identifier. The retry completes replication
and returns the original result without applying the mutation again.

The controller or deployment configuration designates the synchronous
backup. If no synchronized backup is available, authoritative mutations
are unavailable.

This protocol is limited to one primary and one synchronous backup. It
does not introduce a general-purpose distributed transaction manager.

## Alternatives Considered

### Acknowledge After Primary Commit

The primary would return success before backup replication completed.

**Advantages**

- Lower write latency.
- Writes remain available while the backup is unavailable.
- Simpler mutation handling.

**Disadvantages**

- An acknowledged mutation could be lost during primary failure.
- A promoted backup could expose an older auction version.
- Auction outcomes could change after failover.
- Does not satisfy the write-acknowledgement contract.

### Synchronous One-Phase Replication with Rollback

The primary would apply the mutation, replicate it, and restore its
previous state if replication failed.

**Advantages**

- Smaller change to the original implementation.
- No explicit prepared or commit-decision state.

**Disadvantages**

- Cannot safely handle a lost acknowledgement after backup commit.
- Can leave replicas with conflicting committed state.
- Rollback is unsafe once either replica has durably committed.

### Generalized Two-Phase Commit

The system would introduce a reusable transaction coordinator and
participant framework.

**Advantages**

- Could coordinate transactions across arbitrary services and
  resources.
- Provides a conventional distributed-transaction abstraction.

**Disadvantages**

- Adds transaction-management scope not required by the auction system.
- Introduces substantially more recovery and coordination complexity.
- Conflicts with the project's preference for narrowly scoped
  mechanisms.

## Rationale

Auction correctness requires acknowledged mutations to survive primary
failover.

The specialized prepare/commit protocol provides that guarantee while
preserving a single authoritative mutation order and integrating with
optimistic concurrency and durable idempotency.

Separating preparation from commitment prevents unacknowledged
candidates from appearing as committed backup state. Persisting the
primary's decision also establishes a clear recovery rule: abort before
the decision and finish committing after it.

This approach provides the required write guarantee without introducing
general distributed transaction support beyond the primary-backup
replica group.

## Consequences

### Positive

- Acknowledged mutations are durable on both authoritative replicas.
- A promoted synchronized backup contains acknowledged auction state.
- Failed pre-decision mutations do not advance the auction version.
- Lost responses can be resolved through idempotent retry.
- Commit recovery follows an explicit and testable state machine.

### Negative

- Write latency includes additional network round trips and durable
  writes on both replicas.
- Mutations become unavailable without a synchronized backup.
- Prepared, aborted, and pending-commit records require persistence and
  recovery.
- A failed primary can leave prepared work awaiting resolution.
- Clients may temporarily receive an acknowledgement-pending result
  after an irrevocable commit decision.

## Implementation Notes

The storage protocol exposes idempotent prepare, commit, and abort
operations.

Prepared state is excluded from authoritative reads and full
committed-state synchronization.

The primary retains pending backup-commit decisions until the backup
confirms the expected committed version. Restart recovery and retries
reissue commit operations safely.

The mutation request identifier associates the candidate auction,
idempotency record, prepared state, and commit decision.

Failover rules must prevent a replica with unresolved or unverified
protocol state from accepting writes until recovery and synchronization
complete.

## References

- ADR-005: Use Primary-Backup Replication
- ADR-011: Use Optimistic Concurrency with Version Numbers
- ADR-012: Use Durable Idempotency for Auction Mutations
- ADR-013: Use Primary-Authoritative Reads
- Auction Specification §7: Idempotency
- Auction Specification §9: Write Acknowledgement
- Auction Specification §10: Failover
