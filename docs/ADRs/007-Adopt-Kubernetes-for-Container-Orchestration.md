# ADR-007: Adopt Kubernetes for Container Orchestration

- **Status:** Accepted
- **Date:** 2026-07-12 (Retrospectively documented)
- **Decision Type:** Architectural Decision
- **Related ADRs:**
  - ADR-000: Adopt Project Architectural Constraints
  - ADR-004: Separate Request Handling from Replicated Storage

## Context

The project specification permitted either a Docker-only deployment or a Kubernetes-based deployment. While a container-only architecture would satisfy the functional requirements, Kubernetes offered an opportunity to model deployment, networking, service discovery, restart behavior, and service scaling in a manner more representative of modern distributed systems.

## Decision

Deploy Blindsided using Kubernetes.

Kubernetes is responsible for:

- Scheduling and managing application containers.
- Service discovery and networking.
- Restarting failed containers.
- Managing deployments and stateful workloads.
- Supporting horizontal scaling of stateless services.

Application-level replication, consistency, and failover remain the responsibility of Blindsided rather than Kubernetes itself.

## Alternatives Considered

### Docker Compose

**Advantages**

- Simpler local development.
- Easier initial configuration.
- Lower operational overhead.

**Disadvantages**

- Limited orchestration capabilities.
- No native deployment abstraction.
- Less representative of production distributed environments.

### Standalone Containers

**Advantages**

- Minimal infrastructure.
- Straightforward to understand.

**Disadvantages**

- Manual lifecycle management.
- Manual networking and scaling.
- Limited fault recovery capabilities.

## Rationale

Kubernetes aligns well with Blindsided's architecture by separating application logic from deployment concerns.

The platform manages container lifecycle, networking, and service discovery, allowing the project to focus on replication, consistency, and fault tolerance. It also supports the distinction between stateless service components and stateful storage replicas.

## Consequences

### Positive

- Modern deployment model.
- Built-in service discovery.
- Automatic container restart.
- Supports horizontal scaling of stateless services.
- Clear separation between orchestration and application logic.

### Negative

- Increased deployment complexity.
- Additional concepts to understand and configure.
- Local development requires a Kubernetes environment.

## Implementation Notes

Blindsided deploys its backend components as Kubernetes workloads, using different workload types for stateless services and stateful storage replicas. Kubernetes manages infrastructure concerns while the application remains responsible for distributed systems behavior.

## References

- ADR-000: Adopt Project Architectural Constraints
- ADR-004: Separate Request Handling from Replicated Storage
- CSCI 5105 Project #3 Specification
