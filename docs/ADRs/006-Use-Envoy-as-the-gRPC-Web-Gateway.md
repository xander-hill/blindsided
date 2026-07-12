# ADR-006: Use Envoy as the gRPC-Web Gateway

- **Status:** Accepted
- **Date:** 2026-07-12 (Retrospectively documented)
- **Decision Type:** Architectural Decision
- **Related ADRs:**
  - ADR-002: Use Protocol Buffers and gRPC for Service Communication
  - ADR-004: Separate Request Handling from Replicated Storage

## Context

Backend services communicate using native gRPC. Browser clients, however, do not natively support standard HTTP/2 gRPC communication.

The architecture therefore required a compatibility layer between web clients and the internal gRPC service interfaces without changing the internal service protocol.

## Decision

Adopt Envoy as the edge proxy responsible for translating gRPC-Web requests from browser clients into native gRPC requests for backend services.

Envoy acts as the external communication boundary while internal service communication remains unchanged.

## Alternatives Considered

### Native REST API

**Advantages**

- Broad browser compatibility.
- Simple debugging with standard HTTP tools.

**Disadvantages**

- Duplicate API definitions.
- Separate serialization model.
- Loss of a single contract across internal and external communication.

### Custom Translation Service

**Advantages**

- Complete control over translation behavior.
- Can include application-specific processing.

**Disadvantages**

- Additional component to maintain.
- Reimplements functionality already provided by Envoy.
- Increases operational complexity.

## Rationale

Using Envoy preserves a single protobuf and gRPC contract throughout the backend while allowing browser-based clients to interact with the system through gRPC-Web.

Keeping protocol translation at the edge avoids introducing REST-specific logic into backend services and cleanly separates transport compatibility from business logic.

## Consequences

### Positive

- Single internal communication protocol.
- No duplicate service implementations.
- Clear separation between external and internal protocols.
- Mature, well-supported proxy solution.

### Negative

- Additional deployment component.
- Extra network hop for browser requests.
- Proxy configuration becomes part of the deployment.

## Implementation Notes

Envoy is deployed as the entry point for browser traffic and forwards translated requests to the auction service using native gRPC.

## References

- ADR-002: Use Protocol Buffers and gRPC for Service Communication
- ADR-004: Separate Request Handling from Replicated Storage
