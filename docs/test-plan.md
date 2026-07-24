# Testing and evaluation

Automated tests verify correctness. The frontend visualizes the behavior, and
Grafana provides detailed runtime observability; neither substitutes for the
assertions in the test and evaluation suites.

## Test levels

- **Unit and contract:** auction lifecycle, creation, reserve handling, active
  bids, withdrawal, deterministic tie-breaking, visibility, versions, and
  idempotency.
- **Service and integration:** gRPC request paths, public projections, OCC
  retries, storage interaction, metrics, and in-process server behavior.
- **Distributed:** concurrent mutations, synchronous acknowledgement,
  replication failures, fencing, promotion, synchronization, reprotection, and
  in-flight requests.
- **Deployment:** Docker/evaluation-script contracts and Kubernetes manifests,
  including fixed storage membership and service-tier HPA policy.
- **Frontend E2E:** one principal Playwright flow covering auction creation,
  simulated and human bids, backup loss, primary loss, recovery, and reveal.

## What is tested

The backend suite asserts domain semantics and privacy across reads, search,
and watch responses. It verifies exact version changes, accepted-request
deduplication, conflicting payload rejection, primary-authoritative reads, and
the requirement that primary and designated backup commit before success.

Distributed tests and drivers verify stale-epoch fencing, promotion barriers,
full-state synchronization, backup reprotection, prior-primary return, durable
restart recovery, concurrent bidding, and committed watch behavior. The
observability check requires populated Prometheus families and provisioned
Grafana dashboards. The Kubernetes driver generates load, observes HPA
scale-out, and confirms that the storage StatefulSet remains at three replicas.

The browser E2E is demonstration-focused rather than broad UI regression
coverage. It also verifies that failure controls can operate the isolated local
Compose topology and that the user-visible auction survives both recovery
paths.

## Ordered validation scenarios

The closeout run executes:

1. **Backup failure:** remove the designated synchronous backup, prove an
   unsafe write is not acknowledged, synchronize the standby, and restore
   protected writes.
2. **Primary failover:** stop the metric-identified primary, promote the
   synchronized backup at a higher epoch, prepare a replacement backup, and
   verify acknowledged state survives.
3. **Watch behavior:** exercise multiple subscribers, privacy, rejected
   mutations, failover continuity, and cancellation.
4. **Restart durability:** restart storage while preserving volumes, recover
   state/idempotency/ordering, and reveal deterministically.

Ordered execution matters when scenarios share a Compose environment:
membership and roles change after failures, so each driver must finish its
recovery and restore the stopped replica before the next scenario. The manual
CI matrix instead gives each scenario a fresh stack and volumes.

## Current results

The final recorded result is:

- 394 backend tests passed;
- 86 subtests passed;
- complete evaluation suite passed;
- ordered backup-failure, primary-failover, watch, and restart-durability
  scenarios passed;
- observability validation passed;
- Kubernetes service-tier scaling validation passed;
- frontend lint and production build passed;
- Playwright E2E passed;
- demo-control Python compilation passed.

These results demonstrate the listed behavior in the tested local environments;
they are not a claim about arbitrary infrastructure or fault models.

## CI split

Required CI runs deterministic syntax/whitespace checks, the full backend suite,
protobuf drift detection, frontend lockfile installation, lint, build,
Playwright discovery, and adapter compilation for every pull request and push
to `main`.

The weekly/manual system workflow starts isolated Compose stacks and runs the
traffic, concurrency, failure, durability, watch, and observability scenarios.
It is separate because container failure manipulation, image builds, recovery
windows, and log collection are slower and more environment-sensitive than
branch-protection checks.

Kubernetes scaling remains a local or self-hosted known-cluster validation. It
requires a functioning Kubernetes cluster, metrics-server, image availability,
and permissions that a plain GitHub-hosted runner does not provide reliably.
See [continuous integration](continuous-integration.md) and the
[evaluation harness](../tools/evaluation/README.md) for commands.

## What the tests do not prove

- Byzantine fault tolerance or quorum consensus;
- multi-region latency, partitions, or disaster recovery;
- formal verification of the protocol;
- a production authentication, authorization, payment, or security model;
- long-duration soak behavior;
- correctness outside the documented three-storage-replica design;
- production browser compatibility or exhaustive frontend behavior.
