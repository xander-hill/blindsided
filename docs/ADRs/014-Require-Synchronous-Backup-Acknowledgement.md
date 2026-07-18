ADR-014: Require Synchronous Backup Acknowledgement

Status: Accepted

Date: 2026-07-18

Decision Type: Architectural Decision

Related ADRs:

ADR-005: Use Primary-Backup Replication

ADR-011: Use Optimistic Concurrency with Version Numbers

ADR-012: Use Durable Idempotency for Auction Mutations

ADR-013: Use Primary-Authoritative Reads

Context

Blindsided uses primary-backup replication for authoritative auction state.Auction mutations include creation, bid placement, bid withdrawal, and reveal.

Acknowledging a mutation after committing it only on the primary could lose anaccepted mutation if the primary fails before replication completes. That couldchange the active bid set, auction version, or final outcome after failover.

A simple apply-and-rollback sequence is also insufficient. If the backup commitsbut its acknowledgement is lost, the primary cannot safely determine whether itshould roll the mutation back. The write protocol therefore needs to distinguishprepared state, an irrevocable commit decision, and fully acknowledged state.

Decision

Require every auction mutation to be committed by the primary and one designated synchronous backup before returning success to the client.

Write Protocol

Use a replication-specific prepare/commit protocol coordinated by the primary:

Build candidate: The primary validates the mutation and constructs the candidate auction state, response, and idempotency record without changing committed state.

Prepare backup: The designated backup durably records the candidate as prepared without exposing it through reads.

Record decision: The primary durably records the commit decision, auction state, and idempotency record.

Commit backup: The primary instructs the backup to commit its prepared mutation.

Verify commit: The backup durably commits the auction and idempotency record and acknowledges the committed version.

Acknowledge client: The primary returns success only after receiving the expected acknowledgement.

Failure Semantics

Failure point

Mutation state

Required action

Before the durable commit decision

Uncommitted

Abort the prepared mutation and do not advance the version

After the durable commit decision

Committed but not fully acknowledged

Retry backup commit; never roll back

After backup acknowledgement

Committed and acknowledged

Return success or replay the original result

If the commit decision is durable but backup acknowledgement remains unresolved, the client receives an acknowledgement-pending response and must retry the same request identifier. Durable idempotency allows that retry to complete replication and return the original result without applying the mutation again.

Durable Protocol State

The following protocol state is persisted so it survives process restart:

prepared mutations;

abort records;

pending backup commits;

committed idempotency records.

Prepare, commit, and abort operations are idempotent and use the mutation request identifier as their stable identity.

Backup Assignment and Scope

The controller or deployment configuration designates the synchronous backup. Additional replicas do not participate in the acknowledgement requirement. If no synchronized backup is available, authoritative mutations are unavailable.

This protocol is specialized for one primary and one synchronous backup. It is not intended to provide a general-purpose distributed transaction manager.

Alternatives Considered

Acknowledge After Primary Commit

The primary would return success before backup replication completed.

Advantages

Lower write latency.

Writes remain available while the backup is unavailable.

Disadvantages

An acknowledged mutation could be lost during primary failure.

A promoted backup could expose an older auction version or produce a differentoutcome.

Does not satisfy the auction write-acknowledgement contract.

Synchronous One-Phase Replication with Rollback

The primary would apply the candidate, replicate it, and restore its previous stateif replication failed.

Advantages

Smaller change to the original implementation.

No explicit prepared or decision state.

Disadvantages

Cannot safely handle a lost acknowledgement after the backup commits.

Can leave replicas with conflicting committed state.

Rollback becomes unsafe once either replica has durably committed.

Generalized Two-Phase Commit

The system would introduce a reusable transaction coordinator and participantframework.

Advantages

Could coordinate transactions across arbitrary services and resources.

Provides a conventional distributed-transaction abstraction.

Disadvantages

Adds transaction-management scope not required by the auction system.

Introduces substantially more recovery, coordination, and operational complexity.

Conflicts with the project's preference for narrowly scoped mechanisms.

Rationale

Auction correctness requires acknowledged mutations to survive primary failover. The specialized prepare/commit protocol provides that guarantee while preserving a single authoritative mutation order and integrating with optimistic concurrency and durable idempotency.

Separating preparation from commitment prevents unacknowledged candidates from appearing as committed backup state. Persisting the primary's decision establishes a clear recovery rule: abort before the decision and finish committing after it.

Consequences

Positive

Acknowledged mutations are durable on both authoritative replicas.

A promoted synchronized backup contains acknowledged auction state.

Failed pre-decision mutations do not advance the auction version.

Lost responses can be resolved through idempotent retry.

Commit recovery follows an explicit and testable state machine.

Negative

Write latency includes two backup round trips and durable writes on both nodes.

Mutations become unavailable without a synchronized backup.

Prepared, aborted, and pending-commit records require persistence and recovery.

A failed coordinator can leave prepared work awaiting resolution.

Clients may temporarily receive an acknowledgement-pending result after anirrevocable commit decision.

Implementation Notes

The storage protocol exposes three idempotent operations:

PrepareAuctionMutation

CommitPreparedMutation

AbortPreparedMutation

Prepared state is excluded from authoritative reads and full committed-state synchronization.

The primary retains pending backup-commit decisions until the backup confirms the expected committed version. Restart recovery and retries reissue commit operations, which are safe because the backup treats repeated decisions idempotently.

Failover rules must prevent a replica with unresolved or unverified protocol state from accepting writes until recovery and synchronization complete.

References

Auction Specification §7: Idempotency

Auction Specification §9: Write Acknowledgement

Auction Specification §10: Failover

ADR-005: Use Primary-Backup Replication

ADR-011: Use Optimistic Concurrency with Version Numbers

ADR-012: Use Durable Idempotency for Auction Mutations

ADR-013: Use Primary-Authoritative Reads
