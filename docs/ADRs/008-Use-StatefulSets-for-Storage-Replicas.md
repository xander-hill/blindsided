# ADR-008: Use StatefulSets for Storage Replicas

- **Status:** Accepted
- **Date:** 2026-07-12 (Retrospectively documented)
- **Decision Type:** Architectural Decision
- **Related ADRs:**
  - ADR-004: Separate Request Handling from Replicated Storage
  - ADR-005: Use Primary-Backup Replication
  - ADR-007: Adopt Kubernetes for Container Orchestration

## Context

Blindsided distinguishes between stateless service components and stateful storage replicas.

Storage replicas maintain authoritative auction state, participate in replication, and require stable identities for replica membership and coordination. Stateless deployment primitives are not well suited for these requirements.

## Decision

Deploy storage replicas as Kubernetes StatefulSets.

Each replica receives a stable identity that persists across restarts, allowing replica membership and primary assignment to remain predictable.

Stateless application components continue to use deployment resources better suited to horizontally scalable workloads.

## Alternatives Considered

### Kubernetes Deployments

**Advantages**

- Simpler deployment model.
- Well suited for stateless services.
- Easy horizontal scaling.

**Disadvantages**

- Pod identities are ephemeral.
- Replica identity changes across recreation.
- Less appropriate for stateful replication groups.

### Docker Containers

**Advantages**

- Simple local deployment.
- Minimal orchestration requirements.

**Disadvantages**

- No built-in stable identities.
- Manual management of replica lifecycle.
- Limited support for stateful clustered workloads.

## Rationale

Primary-backup replication depends on replicas having stable identities that can be referenced consistently by the controller and other storage nodes.

StatefulSets provide predictable pod names, ordered lifecycle behavior, and a deployment model designed specifically for stateful distributed applications.This aligns naturally with the storage layer while allowing the service tier to remain independently scalable.

## Consequences

### Positive

- Stable replica identities.
- Predictable storage-node membership.
- Better fit for replicated state.
- Clear distinction between stateless and stateful workloads.
- Simplifies controller interaction with storage replicas.

### Negative

- More operational complexity than Deployments.
- Scaling stateful workloads requires additional consideration.
- Replica replacement must preserve replicated state correctly.

## Implementation Notes

Storage replicas are deployed as StatefulSets, while stateless components use standard Kubernetes deployment resources. Replica identity is used by the controller and replication layer to coordinate the storage cluster.

## References

- ADR-004: Separate Request Handling from Replicated Storage
- ADR-005: Use Primary-Backup Replication
- ADR-007: Adopt Kubernetes for Container Orchestration
