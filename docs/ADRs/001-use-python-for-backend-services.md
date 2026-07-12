# ADR-001: Use Python for Backend Services

- **Status:** Accepted
- **Date:** 2026-07-12 (Retrospectively documented)
- **Decision Type:** Architectural Constraint
- **Related Documents:**
  - ADR-000: Adopt Project Architectural Constraints
  - Architecture Overview
  - CSCI 5105 Project Specification

## Context

The project specification required the backend services to be implemented in
Python. While this was an external constraint rather than a freely selected
technology, the implementation language significantly influences development,
tooling, maintainability, and ecosystem support.

This ADR documents why Python was an appropriate fit for the architecture and
the consequences of adopting it.

## Decision

Implement all backend services in Python.

The backend consists of multiple cooperating services communicating through
Protocol Buffers and gRPC, with deployment in a containerized environment.

## Alternatives Considered

### Java

**Pros**

- Strong static typing and compile-time guarantees
- Excellent concurrency support
- Mature enterprise ecosystem
- High runtime performance

**Cons**

- Greater implementation overhead
- More verbose for rapid prototyping
- Not permitted by the project constraints

### Go

**Pros**

- Excellent concurrency model
- Small deployment footprint
- Strong support for networking and distributed systems
- Fast compilation

**Cons**

- Smaller ecosystem for this project's existing tooling
- Less familiarity during project development
- Not permitted by the project constraints

### Python

**Pros**

- Rapid development and iteration
- Readable implementation of distributed algorithms
- Mature gRPC and Protocol Buffers support
- Strong ecosystem for networking and containerized applications

**Cons**

- Dynamic typing reduces compile-time safety
- Lower runtime performance than compiled languages
- Global Interpreter Lock (GIL) limits CPU-bound parallelism

## Rationale

Although Python was required by the project specification, it aligns well with
the project's primary objectives.

Blindsided emphasizes distributed systems concepts—including replication,
fault tolerance, synchronization, and service communication—rather than
maximizing raw computational performance. Python's simplicity and mature gRPC
ecosystem allowed development effort to focus on architectural behavior rather
than language complexity.

## Consequences

### Positive

- Rapid implementation of distributed system components.
- Excellent support for gRPC and Protocol Buffers.
- Highly readable implementation of replication and coordination logic.
- Large ecosystem and community support.

### Negative

- Reduced compile-time type safety.
- Lower throughput than comparable compiled languages.
- Less suitable for CPU-intensive workloads.

## References

- ADR-000: Adopt Project Architectural Constraints
- CSCI 5105 Project #3 Specification
