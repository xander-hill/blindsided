# ADR-016: Use Prometheus and Grafana for System Observability

- **Status:** Accepted
- **Date:** 2026-07-22
- **Decision Type:** Architectural Decision
- **Related ADRs:**
  - ADR-005: Use Primary-Backup Replication
  - ADR-015: Use Epoch-Fenced, Barrier-Based Failover

## Context

Blindsided is a distributed system with replication, optimistic
concurrency control, failover, synchronization, and recovery workflows.

Application logs alone are insufficient for understanding system behavior
under contention, replication failures, failover events, and cluster
recovery.

The project requires visibility into both normal operation and
distributed-system guarantees.

Observability should remain lightweight, deployment-agnostic, and easy to
run locally.

## Decision

Use Prometheus for metrics collection and Grafana for visualization.

Each process exposes a Prometheus metrics endpoint.

Prometheus scrapes metrics from:

- Controller
- Auction Service
- Storage Replicas

Metrics focus on distributed-system behavior, including:

- Request throughput
- Request latency
- Mutation outcomes
- Concurrency retries
- Idempotency decisions
- Replication attempts
- Commit outcomes
- Cluster readiness
- Failover activity
- Promotion activity
- Synchronization activity

Metrics use bounded label sets to avoid excessive cardinality and resource
usage.

Grafana is used to visualize operational behavior and evaluation
scenarios.

## Consequences

### Positive

- Provides visibility into distributed-system behavior.
- Supports debugging and performance analysis.
- Enables repeatable evaluation scenarios.
- Integrates easily with Docker and Kubernetes deployments.
- Separates metric collection from visualization.

### Negative

- Introduces additional operational components.
- Requires maintaining metric definitions and dashboards.
- Metrics add minor runtime overhead.

## Alternatives Considered

### Application Logs Only

Rejected because logs do not provide efficient aggregation, visualization,
latency analysis, or long-term operational insight.

### Push-Based Metrics Systems

Rejected because pull-based scraping is simpler for local development and
containerized deployments.

### OpenTelemetry-Based Observability

Rejected because the project currently focuses on metrics rather than
distributed tracing.

### Custom Dashboarding

Rejected because Prometheus and Grafana are widely adopted industry
standards with strong ecosystem support.
