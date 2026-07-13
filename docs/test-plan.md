# Blindsided Test Plan

## Scope

This plan covers auction-domain behavior, service/storage integration,
replication, failover, and deployment validation.

## Test Levels

### Unit Tests

Auction rules and state transitions.

### Integration Tests

gRPC service-to-storage behavior.

### Distributed Tests

Replication, synchronization, concurrency, and failover.

### Deployment Tests

Docker and Kubernetes configuration.

## Current vs Target Behavior

Existing conflicting tests are retained temporarily as characterization
tests.

Target tests are written when their roadmap phase begins.

## Workflow

1. Select the next specification item.
2. Write the relevant test.
3. Implement the behavior.
4. Add broader integration or distributed coverage where needed.
5. Update the specification status.
