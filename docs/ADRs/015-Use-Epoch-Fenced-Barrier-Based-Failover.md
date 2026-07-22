# ADR-015: Use Epoch-Fenced, Barrier-Based Failover

- **Status:** Accepted
- **Date:** 2026-07-22
- **Decision Type:** Architectural Decision
- **Related ADRs:**
  - ADR-003: Use a Centralized Controller for Cluster Coordination
  - ADR-005: Use Primary-Backup Replication
  - ADR-013: Use Primary-Authoritative Reads

## Context

Blindsided uses primary-backup replication to maintain authoritative
auction state.

Replica failures must not allow stale replicas to continue accepting
writes, replay outdated state, or overwrite newer assignments.

The system must recover from primary failure while preserving
authoritative auction state and preventing split-brain behavior.

Promotion cannot be based solely on replica availability. A promoted
replica must have synchronized state and a valid cluster assignment
before becoming authoritative.

The failover process must also tolerate candidate rejection, timeout,
replacement-backup failure, and delayed responses from previous recovery
attempts.

## Decision

Use epoch-fenced, barrier-based failover coordinated by the controller.

The controller maintains a monotonically increasing primary epoch. A
replica may act as primary only if it holds the current authoritative epoch
assigned by the controller.

When a failover occurs:

- The controller selects an eligible synchronized backup.
- The candidate enters promotion.
- The candidate must complete the promotion barrier before becoming
  authoritative.
- A synchronized replacement backup must be established.
- The cluster is marked ready only after promotion and synchronization
  complete successfully.

Failover attempts are treated as bounded recovery workflows.

Candidate rejection, timeout, synchronization failure, and replacement
backup failure trigger deterministic fallback behavior using a bounded
eligible set.

If recovery cannot be completed safely, the assignment is cleared and the
cluster remains unavailable rather than serving potentially incorrect
state.

Delayed responses from previous failover attempts are ignored through
attempt-scoped validation.

## Consequences

### Positive

- Prevents stale-primary behavior.
- Prevents split-brain conditions.
- Ensures only synchronized replicas become authoritative.
- Protects correctness during recovery.
- Supports deterministic failover behavior.
- Prevents delayed callbacks from corrupting newer assignments.

### Negative

- Recovery may temporarily reduce availability.
- Promotion requires additional synchronization work.
- Controller coordination becomes more complex.
- Writes remain unavailable until the promotion barrier completes.

## Alternatives Considered

### Promote Any Available Replica Immediately

Rejected because an unsynchronized replica may serve stale state and
produce incorrect auction outcomes.

### Leaderless or Multi-Primary Replication

Rejected because the project prioritizes correctness and architectural
clarity over higher write availability.

### Consensus-Based Replication

Rejected because the complexity was not justified for the project's
requirements and educational goals.
