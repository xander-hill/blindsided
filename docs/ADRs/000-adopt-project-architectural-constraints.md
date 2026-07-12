# ADR-000: Adopt Project Architectural Constraints

- Status: Accepted
- Date: 2026-07-12 (Retrospectively documented)
- Decision Type: Architectural Constraint
- Related Documents:
  - Architecture Overview
  - Project Specification

## Context

Blindsided was developed as the third project for CSCI 5105: Introduction to Distributed Systems. The project specification defined several architectural requirements while intentionally leaving important distributed systems tradeoffs open to student design.

Rather than selecting technologies from a blank slate, the project began with a defined set of constraints. These constraints establish the baseline architecture within which subsequent architectural decisions were made.

This ADR records those constraints so that later ADRs distinguish between externally imposed requirements and project-specific design decisions.

## Constraints

The project specification required the system to:

- be implemented in Python
- use Protocol Buffers and gRPC for system-to-system communication
- run in a containerized environment
- separate request handling from replicated storage
- include a centralized controller responsible for coordination and replica management
- support replicated storage
- tolerate storage replica failures
- support dynamic scaling of the service tier

The specification intentionally left several architectural decisions to the implementation, including:

- replication strategy (primary-backup or quorum)
- synchronization protocol
- consistency model
- scaling policy
- failure recovery policy
- deployment architecture (Docker or Kubernetes)

## Decision

The project adopts the required architectural constraints as the foundation for the system.

Subsequent ADRs document the architectural decisions made within these constraints, including replication strategy, deployment architecture, component responsibilities, and consistency mechanisms.

## Rationale

Recording the initial constraints provides historical context for later decisions.

Without this ADR, subsequent documentation could incorrectly imply that technologies such as Python or gRPC were selected through an unconstrained technology evaluation rather than inherited from the project requirements.

Separating architectural constraints from architectural decisions improves the accuracy and traceability of the project's design history.

## Consequences

### Positive

- Clarifies the origin of later architectural decisions.
- Prevents repetition across future ADRs.
- Distinguishes project requirements from implementation choices.
- Provides historical context for readers unfamiliar with the project.

### Negative

- Some technologies documented in later ADRs originate from project
  requirements rather than independent evaluation.
- Future architectural changes remain bounded by these initial constraints
  unless the project intentionally departs from the original specification.

## References

- CSCI 5105 Project #3: Fault-Tolerant, Replicated Marketplace
