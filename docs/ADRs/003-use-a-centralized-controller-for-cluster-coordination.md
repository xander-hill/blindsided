# ADR-003: Use a Centralized Controller for Cluster Coordination

- **Status:** Accepted
- **Date:** 2026-07-12 (Retrospectively documented)
- **Decision Type:** Architectural Decision
- **Related ADRs:**
  - ADR-000: Adopt Project Architectural Constraints
  - ADR-002: Use Protocol Buffers and gRPC for Service Communication

## Context

The distributed auction system consists of multiple backend components that must cooperate to process requests, monitor storage replicas, and recover from
node failures.

The project specification required a centralized controller responsible for request routing, replica membership, health monitoring, and failure detection.
Within that constraint, the implementation needed to define the controller's responsibilities and interactions with the remaining system components.

## Decision

Adopt a centralized controller responsible for cluster coordination.

The controller is responsible for:

- Maintaining storage node membership.
- Tracking replica health through heartbeats.
- Determining the current primary replica.
- Coordinating replica registration and replacement.
- Providing cluster metadata to participating services.

Business logic, auction processing, and replicated state remain outside of the
controller and are delegated to the service and storage layers.

## Alternatives Considered

### Peer-to-Peer Coordination

**Advantages**

- Eliminates a central coordination component.
- Reduces reliance on a single control plane.

**Disadvantages**

- More complex membership and leader coordination.
- Increased implementation complexity.
- Harder to reason about in an educational project.

### Distributed Consensus Coordination

Examples include ZooKeeper, etcd, or Raft-based coordination.

**Advantages**

- Fault-tolerant cluster metadata.
- Strong consistency for leader election and membership.
- Proven production approach.

**Disadvantages**

- Significantly increases architectural complexity.
- Introduces infrastructure beyond the scope of the project.
- Shifts focus away from implementing replication behavior.

## Rationale

A centralized controller provides a clear separation between coordination and application logic. Storage replicas remain focused on maintaining replicated auction state, while the controller manages cluster-wide metadata and health.

This architecture simplifies reasoning about node membership and primary assignment while allowing the service layer to remain focused on request processing.

## Consequences

### Positive

- Clear separation of responsibilities.
- Simplified cluster membership management.
- Centralized health monitoring.
- Straightforward implementation of replica registration and failover logic.
- Eases testing of coordination behavior.

### Negative

- Introduces a single coordination authority.
- Controller availability affects cluster management operations.
- Future high-availability deployments would require a more resilient coordination mechanism.

## Implementation Notes

The controller exposes gRPC endpoints used by storage nodes for registration, heartbeat reporting, and cluster coordination. It maintains the authoritative view of replica membership and primary assignment used by the current implementation.

## References

- ADR-000: Adopt Project Architectural Constraints
- ADR-002: Use Protocol Buffers and gRPC for Service Communication
- CSCI 5105 Project #3 Specification
