# ADR-004: Separate Request Handling from Replicated Storage

- **Status:** Accepted
- **Date:** 2026-07-12 (Retrospectively documented)
- **Decision Type:** Architectural Decision
- **Related ADRs:**
  - ADR-000: Adopt Project Architectural Constraints
  - ADR-002: Use Protocol Buffers and gRPC for Service Communication
  - ADR-003: Use a Centralized Controller for Cluster Coordination

## Context

Blindsided must support both scalable request processing and replicated state management.

These concerns have different operational characteristics. Request-handling components benefit from being stateless and independently scalable, while storage replicas must preserve authoritative auction state, participate in replication, and maintain stable membership within the replica group.

Combining both responsibilities in the same process would increase coupling between client-facing workloads and storage consistency behavior.

## Decision

Separate request handling from replicated storage.

The system is divided into three primary backend responsibilities:

- **Service tier:** accepts client requests, performs request-level validation, coordinates reads and writes, and returns responses.
- **Controller:** maintains cluster coordination metadata, replica membership, health information, and primary assignment.
- **Storage tier:** owns authoritative auction state and implements replication, synchronization, concurrency control, and storage-node failure recovery.

Service instances do not own authoritative auction state and may be added or removed without changing the replication topology.

## Alternatives Considered

### Combined Service and Storage Nodes

Each node would process client requests and also store replicated auction data.

**Advantages**

- Fewer component types.
- Simpler initial deployment.
- Fewer network hops for locally handled requests.

**Disadvantages**

- Couples service scaling to replication membership.
- Makes node replacement and failover more complex.
- Client workload spikes directly affect storage replicas.
- Blurs responsibility for routing, validation, and consistency.
- Makes independent testing and evolution more difficult.

### Direct Client Access to Storage Replicas

Clients would communicate directly with storage nodes.

**Advantages**

- Removes the intermediate service tier.
- Reduces one layer of request forwarding.

**Disadvantages**

- Exposes replication topology to clients.
- Pushes primary discovery and retry behavior into client code.
- Makes API compatibility dependent on storage internals.
- Weakens the boundary for validation and response shaping.

## Rationale

Separating the service and storage tiers follows separation of concerns and
keeps each component focused on a cohesive responsibility.

The service tier handles client-facing workflows without owning authoritative state. The storage tier concentrates replication and consistency logic in one place. The controller maintains cluster metadata without becoming responsible for auction business rules.

This structure also allows service capacity to scale independently from the fixed replica group, which is important because request volume and replication requirements do not scale in the same way.

## Consequences

### Positive

- Service instances can remain stateless and horizontally scalable.
- Replication logic is isolated within the storage tier.
- Storage membership does not change when service capacity changes.
- Client-facing validation and response shaping are separated from storage
  representation.
- Components can be tested and evolved independently.
- Failure domains and responsibilities are easier to reason about.

### Negative

- Requests may require additional network hops.
- More independently deployed components increase operational complexity.
- Service availability depends on communication with the controller and
  storage tier.
- Cross-component interfaces must be maintained carefully.

## Implementation Notes

The repository reflects this decision through distinct controller, auction-service, and storage-node components.

The service tier communicates with the controller and storage replicas through gRPC. Storage nodes own replicated auction state, while service instances coordinate operations without persisting authoritative state locally.

## References

- ADR-000: Adopt Project Architectural Constraints
- ADR-002: Use Protocol Buffers and gRPC for Service Communication
- ADR-003: Use a Centralized Controller for Cluster Coordination
- CSCI 5105 Project #3 Specification
