# ADR-002: Use Protocol Buffers and gRPC for Service Communication

- **Status:** Accepted
- **Date:** 2026-07-12 (Retrospectively documented)
- **Decision Type:** Architectural Constraint
- **Related ADRs:**
  - ADR-000: Adopt Project Architectural Constraints
  - ADR-001: Use Python for Backend Services

## Context

Blindsided consists of multiple cooperating backend components, including the auction service, controller, and replicated storage nodes. These components require explicit service contracts for commands, queries, health checks, replication, and persistent auction participation.

The project specification required gRPC for system-to-system communication. This ADR documents how that requirement fits the architecture and the consequences of using Protocol Buffers and gRPC.

## Decision

Use Protocol Buffers to define service interfaces and message schemas, and use gRPC for communication between backend components.

The system uses:

- Unary RPCs for request-response operations.
- Streaming RPCs where persistent auction participation or live updates are required.
- Generated Python client and server code from shared `.proto` definitions.
- Envoy as a separate compatibility layer for browser clients using gRPC-Web.

## Alternatives Considered

### REST with JSON

**Advantages**

- Human-readable payloads.
- Easy manual testing with common HTTP tools.
- Native browser compatibility.
- Broad ecosystem support.

**Disadvantages**

- Weaker contract enforcement without additional schema tooling.
- Manual client implementation is more common.
- Streaming requires separate mechanisms such as WebSockets or Server-Sent Events.
- Greater serialization overhead than Protocol Buffers.

### Asynchronous Messaging

Examples include RabbitMQ, Kafka, or another message broker.

**Advantages**

- Loose temporal coupling between components.
- Natural support for event-driven processing.
- Useful for buffering and replaying asynchronous workloads.

**Disadvantages**

- Adds broker infrastructure and operational complexity.
- Less natural for synchronous reads, validation, and immediate mutation responses.
- Requires separate handling for request-response interactions.
- Delivery semantics and message ordering introduce additional design concerns.

### Raw TCP or Custom Protocol

**Advantages**

- Full control over protocol behavior.
- Potentially minimal runtime overhead.

**Disadvantages**

- Requires custom framing, serialization, compatibility, and error handling.
- Increases implementation complexity.
- Provides little value compared with an established RPC framework.

## Rationale

gRPC fits Blindsided because the system is primarily composed of command-oriented and query-oriented interactions between known backend services.

Protocol Buffers provide explicit schemas for auction data, replication messages, controller metadata, and service responses. Generated client and server bindings reduce manual serialization code and help keep service interfaces consistent.

gRPC streaming also supports the assignment requirement for persistent auction participation without introducing an entirely separate communication protocol.

Although REST would have simplified browser access and manual debugging, Blindsided benefits more from typed internal contracts and built-in streaming. Browser compatibility is handled separately through Envoy rather than weakening the internal service protocol.

Asynchronous messaging may become useful for future event publication or background processing, but it is not an appropriate replacement for the system’s synchronous command and query paths.

## Consequences

### Positive

- Strong, explicit service contracts.
- Generated clients and server interfaces reduce integration boilerplate.
- Efficient binary serialization.
- Native support for unary and streaming communication.
- Clear compatibility boundaries between independently deployed services.
- Shared schemas make communication behavior easier to test and document.

### Negative

- Browser clients cannot use native gRPC directly.
- Envoy or another gRPC-Web gateway is required.
- Binary payloads are less convenient to inspect manually than JSON.
- Changes to shared protobuf definitions require compatibility discipline.
- Generated source files add build and repository-management considerations.
- Synchronous RPC calls can increase coupling between service availability and request success.

## Implementation Notes

The repository contains Protocol Buffer definitions for communication among the auction service, controller, and storage nodes.

Python gRPC bindings are generated from these definitions and used by the backend services. Envoy translates browser-facing gRPC-Web traffic into standard gRPC for the internal service layer.

The use of Envoy is documented separately because it is a distinct boundary and deployment decision.

## References

- ADR-000: Adopt Project Architectural Constraints
- ADR-001: Use Python for Backend Services
- CSCI 5105 Project #3 Specification
