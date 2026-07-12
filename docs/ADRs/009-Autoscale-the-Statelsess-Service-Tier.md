# ADR-009: Autoscale the Stateless Service Tier

- **Status:** Accepted
- **Date:** 2026-07-12 (Retrospectively documented)
- **Decision Type:** Architectural Decision
- **Related ADRs:**
  - ADR-004: Separate Request Handling from Replicated Storage
  - ADR-007: Adopt Kubernetes for Container Orchestration
  - ADR-008: Use StatefulSets for Storage Replicas

## Context

Client request volume can fluctuate independently of the size of the storage replica group. While additional service capacity may be required to process incoming requests, increasing the number of storage replicas solely to handle load would unnecessarily complicate replication and consistency.

The architecture therefore needed to determine which components should scale with demand.

## Decision

Horizontally scale only the stateless service tier.

Service instances may be added or removed in response to demand without changing the storage replication topology. Storage replicas remain a separate, stateful concern whose size is determined by replication and fault-tolerance requirements rather than request volume.

## Alternatives Considered

### Scale Both Service and Storage Together

**Advantages**

- Uniform scaling model.
- Simple operational concept.

**Disadvantages**

- Couples request throughput to replication topology.
- Increases replication overhead.
- Makes consistency management more complex.

### Fixed Service Capacity

**Advantages**

- Simpler deployment.
- Predictable infrastructure.

**Disadvantages**

- Cannot adapt to changing workload.
- Risks reduced responsiveness during traffic spikes.
- Underutilizes resources during low demand.

## Rationale

Separating service scaling from storage replication preserves the architectural boundary between request processing and authoritative state management.

Stateless service instances are inexpensive to create and remove, while storage replicas require synchronization, stable identity, and coordinated membership. Scaling only the service tier improves responsiveness without affecting the consistency guarantees of the storage layer.

## Consequences

### Positive

- Independent scaling of request-processing capacity.
- Stable storage topology.
- Reduced replication overhead.
- Better utilization of infrastructure resources.
- Aligns with Kubernetes deployment best practices.

### Negative

- Service capacity cannot compensate for storage bottlenecks.
- Requires load balancing across service instances.
- Autoscaling policy must be selected and tuned appropriately.

## Implementation Notes

Service components are deployed using Kubernetes Deployments and are intended to scale independently of the storage layer. Storage replicas continue to use StatefulSets and are managed according to replication requirements rather than request volume.

## References

- ADR-004: Separate Request Handling from Replicated Storage
- ADR-007: Adopt Kubernetes for Container Orchestration
- ADR-008: Use StatefulSets for Storage Replicas
