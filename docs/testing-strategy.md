# Blindsided Testing Strategy

- **Status:** Proposed
- **Scope:** Current implementation and target auction architecture
- **Primary contract:** `docs/auction-semantics.md`
- **Related ADRs:** ADR-000 through ADR-011
- **Repository scope:** `backend/`, `api/proto/`, `deploy/`, and `scripts/experiments/`

## 1. Purpose

This document defines how Blindsided will be tested while the system transitions from its original course-project implementation to the stronger behavioral and distributed guarantees defined by the auction semantics contract.

The strategy deliberately separates two questions:

1. **What does the current implementation do today?**
2. **What must the future implementation guarantee?**

Current-behavior tests provide regression protection while the architecture is revised. Contract tests express the intended auction semantics and become the long-term acceptance criteria. A current test must not be treated as proof that the behavior is correct merely because it reflects existing code.

Where the implementation, existing tests, ADRs, and auction semantics disagree, `docs/auction-semantics.md` is authoritative for intended behavior.

## 2. Testing objectives

The test suite should provide confidence that:

- auction domain rules are correct independently of networking and deployment;
- client-visible responses never disclose prohibited information;
- optimistic concurrency prevents lost updates and preserves version rules;
- a mutation is acknowledged only after the required replication commitment;
- committed mutations survive supported failover;
- stale primaries cannot accept authoritative writes;
- replica synchronization produces a promotion-eligible copy of state;
- gRPC contracts remain compatible across the service, controller, and storage layers;
- Kubernetes manifests preserve the intended stateless-service/stateful-storage topology;
- changes can be introduced one guarantee at a time without losing coverage of existing behavior.

## 3. Sources of truth and traceability

### 3.1 Auction semantics

The auction semantics document defines the behavioral contract. Each normative rule should map to at least one executable test. The contract covers:

- lifecycle and reveal rules;
- creation and reserve-price semantics;
- one active bid per bidder;
- bid replacement and withdrawal rules;
- deterministic tie breaking;
- pre-reveal and post-reveal visibility;
- outcome calculation;
- optimistic concurrency and bounded retries;
- request idempotency;
- authoritative and stale-tolerant reads;
- synchronous write acknowledgement;
- failover, epochs, promotion barriers, and overdue auctions;
- authoritative server time;
- system-wide invariants.

### 3.2 Architectural decisions

The ADRs define the architectural boundaries and mechanisms that tests must respect:

| ADR     | Decision                                | Testing consequence                                                                                                                   |
| ------- | --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| ADR-000 | Adopt project architectural constraints | Keep Python, gRPC, containerization, controller, replicated storage, fault tolerance, and service scaling in scope.                   |
| ADR-001 | Use Python for backend services         | Use Python test tooling and isolate dynamic-type risks at service boundaries.                                                         |
| ADR-002 | Use Protocol Buffers and gRPC           | Test generated contracts, RPC status behavior, unary calls, and streaming behavior.                                                   |
| ADR-003 | Centralized controller                  | Test membership, primary assignment, health detection, promotion coordination, and controller metadata separately from auction rules. |
| ADR-004 | Separate request handling from storage  | Keep service-layer policy tests distinct from storage-state and replication tests.                                                    |
| ADR-005 | Primary-backup replication              | Test one authoritative writer, ordered replication, synchronous backup acknowledgement, and failover safety.                          |
| ADR-006 | Envoy gRPC-Web gateway                  | Add edge-contract and routing smoke tests without placing auction logic in proxy tests.                                               |
| ADR-007 | Kubernetes orchestration                | Validate manifests and run selected end-to-end tests against a real local cluster.                                                    |
| ADR-008 | StatefulSets for replicas               | Test stable replica identity, replacement, restart, and catch-up behavior.                                                            |
| ADR-009 | Autoscale stateless service tier        | Test service replicas as interchangeable and verify scaling does not alter storage membership.                                        |
| ADR-010 | Full-state synchronization              | Test complete state copy, synchronization barriers, and exclusion of unsynchronized replicas.                                         |
| ADR-011 | Optimistic concurrency                  | Test expected-version validation, exactly-once version increments, bounded retry, and conflict responses.                             |

### 3.3 Current repository structure

The current repository already separates tests by concern:

```text
backend/tests/
├── helpers.py
├── layers/
│   ├── test_auction_service.py
│   ├── test_controller_service.py
│   └── test_storage_service.py
├── integration/
│   └── test_backend_lifecycle.py
├── distributed/
│   └── test_concurrency_and_replication.py
└── deployment/
    └── test_kubernetes_manifests.py
```

This structure should be retained, but its meaning should be made explicit:

- `layers/`: one process or one class boundary, with external dependencies replaced by fakes or mocks;
- `integration/`: multiple real Blindsided components communicating through in-process/local gRPC;
- `distributed/`: concurrency, replication, failover, timing, and fault-injection scenarios;
- `deployment/`: static manifest checks and cluster-level deployment smoke tests;
- future `contract/`: direct executable mapping from auction semantics to expected behavior;
- future `property/`: invariant and state-machine tests over generated operation sequences.

## 4. Current-state versus target-state testing

### 4.1 Current-state characterization tests

Characterization tests document behavior that exists before architectural revision. They protect refactoring and expose accidental changes. They should be labeled clearly when the behavior is not the intended end state.

Examples already represented by the suite include:

- auction creation reaches the storage primary;
- pre-reveal status masks the `bids` map;
- stale-version bid conflicts trigger a service-layer retry;
- storage commits increment versions;
- reveal prevents later bids;
- local gRPC lifecycle tests cover open, bid, status, reveal, and streaming;
- concurrent bidders do not lose accepted updates in the single-node test stack;
- controller tests cover registration and basic promotion;
- manifest tests cover service deployments and storage StatefulSet identity.

These tests are useful, but several describe only the original implementation and not the full contract.

### 4.2 Target-state contract tests

Target-state tests should be written from `auction-semantics.md`, even before the implementation passes them. During migration they may be marked as expected failures, skipped with a linked issue, or placed in a separate non-blocking suite. They become blocking as each guarantee is implemented.

The target suite must test externally observable guarantees rather than implementation details wherever possible.

### 4.3 Known divergence that must remain visible

The current distributed test `test_primary_storage_allows_degraded_commit_when_peer_is_unreachable` expects a primary to acknowledge a mutation when its peer cannot be reached. This conflicts with the target write-acknowledgement contract, which requires commitment to both the primary and designated synchronous backup before success.

This test should not be silently rewritten. It should be handled in two steps:

1. Rename or annotate it as a **current-behavior characterization test**.
2. Add the target contract test asserting that the same scenario fails without changing state or advancing the version.

When synchronous acknowledgement is implemented, remove or invert the characterization test and promote the contract test into the blocking suite.

Other current/target gaps include request identifiers, withdrawals, deadlines, deterministic acceptance order, epochs, promotion readiness, synchronized-backup establishment, authoritative time, and restricted post-reveal output. These require new protocol and implementation support before full conformance is possible.

## 5. Test levels

## 5.1 Domain and state-machine tests

**Purpose:** Verify auction behavior without gRPC, threads, Kubernetes, or real clocks.

The revised architecture should extract auction transition logic into a cohesive domain component. Tests should invoke commands against an auction state and assert either a new state plus result or a rejection with no state change.

Core cases:

- creation always produces `OPEN`, empty active bids, immutable `ends_at`, and initial version;
- reveal changes `OPEN` to `REVEALED` once;
- every mutation after reveal is rejected;
- first bid creates one active bid;
- a later bid from the same bidder replaces only when permitted and never lowers directly;
- withdrawal removes only the caller's active bid;
- withdrawal followed by a new bid is allowed before the deadline;
- rejected operations leave state and version unchanged;
- successful operations advance the version exactly once;
- equal high bids select the earliest accepted active bid;
- no bids, reserve not met, and successful sale produce distinct outcomes;
- reserve is never represented as a bidder or counted as a bid;
- server time, not client time, controls deadlines.

**Recommended technique:** table-driven tests plus state-machine/property tests.

## 5.2 Service-layer policy tests

**Purpose:** Verify request validation, routing decisions, retry policy, visibility shaping, and response mapping in `backend/blindsided/auction_service/service.py`.

Tests should mock controller and storage clients while treating the service method as the unit under test.

Required coverage:

- all authoritative reads route to the current primary;
- search/listing may use a healthy replica and tolerate stale results;
- mutation retry occurs only on a recognized stale-version conflict;
- each retry fetches current authoritative state and revalidates the full command;
- retry count is bounded;
- retry exhaustion returns a specific concurrency conflict;
- the original request identifier survives retries and failover;
- pre-reveal responses expose only permitted fields;
- post-reveal responses expose winner and winning amount but not losing bid data;
- bidder-own-bid reads reveal only that bidder's active bid;
- unavailable primary, unavailable synchronous backup, and controller outage map to stable public errors;
- live streams use primary-backed authoritative state and do not disclose ranges or reserve status prohibited by the final contract.

The existing `_mask_for_fog` test is a useful starting point, but clearing only the `bids` map is insufficient for the target contract because the protobuf currently contains fields such as `reserve_price` and `reserve_met` that must also be hidden before reveal.

## 5.3 Storage-layer tests

**Purpose:** Verify authoritative state transitions, optimistic concurrency, replication decisions, idempotency records, epoch fencing, and synchronization in `backend/blindsided/storage/service.py` or its future replacements.

Required coverage:

- only a primary in the current epoch accepts authoritative mutations;
- a backup rejects direct client mutations;
- stale expected versions are rejected without state change;
- successful mutations increment exactly once;
- rejected mutations never increment;
- duplicate request identifiers return the stored original result;
- duplicate identifiers with different payloads are rejected;
- primary state is rolled back or never published if synchronous replication fails;
- success requires acknowledgement from the designated synchronized backup;
- replication applies the same version, acceptance order, request ID, and epoch;
- stale or out-of-order replication messages are rejected;
- full-state synchronization copies all committed state and metadata;
- a synchronizing replica cannot acknowledge writes or be promoted;
- a revealed auction rejects every later mutation;
- persistence/restart reconstructs the same committed state when persistence is added.

## 5.4 Controller tests

**Purpose:** Verify cluster metadata and failover coordination in `backend/blindsided/controller/service.py`.

Required coverage:

- first valid cluster bootstrap assigns a primary deterministically;
- node registration is idempotent;
- heartbeat timeout removes unhealthy membership;
- backup health and synchronization status are tracked separately;
- only a complete synchronized backup is eligible for promotion;
- promotion increments the epoch;
- old primary assignments become fenced;
- promotion does not become write-ready until the barrier is complete;
- a synchronized replacement backup is established before writes resume;
- an unsynchronized or stale node cannot be selected;
- concurrent registration, heartbeat, and election operations preserve controller invariants;
- controller restart behavior is explicit and tested once metadata persistence is defined.

The current controller tests validate basic membership and election, but not eligibility, epoch assignment, promotion readiness, or synchronized-backup establishment.

## 5.5 Protocol contract tests

**Purpose:** Keep `api/proto/blindsided.proto`, generated code, and service behavior aligned.

Tests should verify:

- generated Python code is current relative to the `.proto` file;
- required fields for the target contract exist, including request ID, deadline, lifecycle state, bidder acceptance order, epoch, and precise result/error representations;
- public response messages cannot accidentally expose internal-only bid collections;
- backward-incompatible field renumbering or removal is detected;
- unary and streaming RPCs return defined gRPC status codes and application errors;
- Envoy exposes only intended public services and routes them to the correct backend.

Prefer separate public and internal message types rather than relying solely on clearing fields from the internal `Auction` storage message.

## 5.6 Multi-component integration tests

**Purpose:** Verify that real service, controller, and storage components cooperate through gRPC.

The current `running_backend_stack()` helper is appropriate for fast integration tests, but it starts only one storage node. Add reusable fixtures for:

- one primary plus one synchronous backup;
- one primary plus synchronized and unsynchronized backups;
- controllable controller and storage clocks;
- restartable nodes with preserved or discarded state;
- network fault injection at individual RPC boundaries.

Integration scenarios:

- create → bid → own-bid read → withdraw → rebid → reveal;
- pre-reveal and post-reveal visibility through public APIs;
- duplicate request delivery through the service layer;
- stale-version retry with rule revalidation;
- backup rejection causes mutation failure with unchanged primary state;
- primary loss after acknowledgement preserves the committed mutation;
- primary loss before acknowledgement does not expose a partial commit;
- stream reconnect after failover resumes from authoritative state;
- overdue auction is finalized once after promotion readiness.

## 5.7 Distributed fault and concurrency tests

**Purpose:** Exercise schedules and failures that cannot be proven by ordinary happy-path tests.

Fault injection points should include:

- before primary state application;
- after primary application but before replication;
- after backup application but before acknowledgement reaches primary;
- after primary receives backup acknowledgement but before client response;
- during full-state synchronization;
- between election and promotion completion;
- while the former primary is partitioned rather than crashed;
- while an auction deadline passes;
- during concurrent bid, withdrawal, and reveal requests.

For each scenario, assert invariants, not merely response success:

- no acknowledged mutation is lost;
- no request ID is applied more than once;
- no stale primary accepts a write;
- only one authoritative history emerges;
- versions are monotonic and gap-free per successful logical mutation;
- reveal occurs at most once;
- equal bids have the same winner after restart and failover.

Use deterministic barriers/events instead of arbitrary sleeps wherever possible. Tests that rely on timing should use generous deadlines and report the observed event sequence on failure.

## 5.8 Deployment and Kubernetes tests

Static manifest tests should continue to verify:

- service tier uses a Deployment and can have multiple replicas;
- storage uses a StatefulSet and stable DNS identities;
- headless storage service selectors match pods;
- controller and Envoy services select the correct workloads;
- configured peer count matches storage replicas;
- health probes, resource requests, and termination behavior are defined when added;
- autoscaling targets only the stateless service tier;
- storage replacement does not automatically imply promotion eligibility.

Cluster-level smoke tests should deploy to a local Kubernetes environment and verify:

- all components become ready;
- browser-edge traffic reaches the auction service through Envoy;
- service pod deletion does not lose state;
- backup pod deletion triggers replacement and synchronization;
- primary pod deletion triggers safe failover;
- increasing service replicas does not change storage membership;
- rolling updates do not violate write acknowledgement or epoch fencing.

Static YAML regex tests are fast but cannot establish runtime correctness; they should be complemented, not replaced, by cluster tests.

## 5.9 Performance, scalability, and resilience evaluation

These are evaluation tests rather than ordinary pass/fail unit tests.

Track at minimum:

- mutation and read latency percentiles;
- throughput under increasing client concurrency;
- optimistic-concurrency conflict and retry rates;
- replication acknowledgement latency;
- failover detection time;
- promotion-barrier duration;
- replica synchronization duration;
- unavailable-write duration during failover;
- active stream reconnect time;
- service-tier throughput before and after scaling;
- error rate and invariant violations during chaos scenarios.

Results should be reproducible from scripts under `scripts/experiments/` and eventually published as CI artifacts or documented benchmark runs. Performance thresholds should not initially block every pull request; correctness and invariant checks should.

## 6. Contract-to-test matrix

The following matrix is the initial coverage plan. “Current” means meaningful coverage exists in the repository today, not necessarily complete contract compliance.

| Contract guarantee                                                             | Primary test level             | Current coverage | Required target tests                                                                 |
| ------------------------------------------------------------------------------ | ------------------------------ | ---------------: | ------------------------------------------------------------------------------------- |
| Auction starts `OPEN` and reveal is terminal                                   | Domain, storage, integration   |          Partial | Explicit lifecycle enum/state, reveal once, reject every mutation after reveal.       |
| Creation sets seller, immutable deadline, reserve, empty bids, initial version | Domain, protocol               |             Weak | Creation defaults, immutability, validation, no client-supplied active bids.          |
| Reserve is not a bid and is hidden                                             | Domain, service                | Weak/conflicting | Bidder count excludes reserve; pre-reveal responses omit reserve and reserve status.  |
| One active bid per bidder                                                      | Domain, distributed            |          Partial | Same-bidder replacement rules, no duplicate active entries, withdrawal/rebid.         |
| Bid cannot be lowered directly                                                 | Domain                         |             None | Lower bid rejection with unchanged state/version.                                     |
| Successful mutation increments once                                            | Domain, storage                |          Partial | All mutation types, duplicate delivery, retry, failover.                              |
| Rejected mutation does not change state/version                                | Domain, storage                |          Partial | Every rejection reason and replication failure.                                       |
| Withdrawal semantics                                                           | Domain, integration            |             None | Owner-only withdrawal, count decrement, recomputation, deadline/reveal rejection.     |
| Deterministic earliest-accepted tie break                                      | Domain, restart/failover       |             None | Equal bids across concurrency, replication, restart, and promotion.                   |
| Pre-reveal visibility                                                          | Service, protocol, integration |          Partial | No bids, identities, reserve, reserve status, leader, winner, or winning amount.      |
| Post-reveal visibility                                                         | Service, protocol, integration |      Conflicting | Winner and amount only; no losing bids or complete history.                           |
| Outcome: no bids / reserve not met / sale                                      | Domain, integration            |          Partial | Three explicit outcomes and no winner below reserve.                                  |
| Bounded OCC retry with revalidation                                            | Service, integration           |          Partial | Bound, backoff policy, revalidation after state changes, explicit exhausted response. |
| Request idempotency                                                            | Domain, storage, integration   |             None | Same ID/same payload replay; same ID/different payload reject; failover replay.       |
| Authoritative reads use primary                                                | Service, integration           |          Partial | Own bid, status, outcome, mutation reads, stream; search may use replica.             |
| Synchronous write acknowledgement                                              | Storage, distributed           |      Conflicting | Unreachable/rejecting backup causes failure, no version advance, no visible commit.   |
| Promotion eligibility and barrier                                              | Controller, distributed        |             None | Complete state, current epoch, synchronized backup, readiness before writes.          |
| Stale-primary fencing                                                          | Storage, distributed           |             None | Old epoch mutation and replication rejection during partition/rejoin.                 |
| In-flight retry after failover                                                 | Service, distributed           |             None | Wait for readiness, preserve request ID, at-most-once effect.                         |
| Overdue auction after promotion                                                | Domain, distributed            |             None | Validate readiness and finalize exactly once using authoritative time.                |
| Full-state synchronization                                                     | Storage, integration           |          Partial | Metadata completeness, barrier, no early participation, synchronization failure.      |
| Stateless service scaling                                                      | Deployment, cluster            |   Static partial | Interchangeable instances, no local authoritative state, storage topology unchanged.  |

## 7. Test data and determinism

Use builders/factories for canonical auction states rather than hand-constructing incomplete protobuf messages in every test. Recommended fixtures:

- open auction with no bids;
- open auction below reserve;
- open auction at reserve;
- open auction with tied leaders and explicit acceptance order;
- revealed no-bid outcome;
- revealed reserve-not-met outcome;
- revealed successful sale;
- synchronized primary/backup pair at a known epoch;
- unsynchronized recovering replica;
- idempotency record containing request hash and original response.

Inject the following dependencies:

- clock;
- request-ID generator only where the server creates IDs;
- primary locator;
- replication transport;
- persistence adapter;
- retry/backoff policy.

Avoid random sleeps and uncontrolled system time in correctness tests. Randomized/property tests must log the seed and full operation sequence so failures can be reproduced.

## 8. Suite classification and CI policy

Use markers, naming conventions, or separate commands to classify suites:

| Suite                     | Trigger                                        |   Expected duration | Blocking policy                                      |
| ------------------------- | ---------------------------------------------- | ------------------: | ---------------------------------------------------- |
| Layer/unit                | Every commit and pull request                  |             Seconds | Blocking                                             |
| Contract/domain           | Every commit and pull request                  |             Seconds | Blocking once corresponding guarantee is implemented |
| Proto compatibility       | Every pull request changing API/generated code |             Seconds | Blocking                                             |
| Local gRPC integration    | Every pull request                             | Under a few minutes | Blocking                                             |
| Distributed deterministic | Every pull request or merge queue              |       A few minutes | Blocking when stable                                 |
| Kubernetes manifest       | Every pull request                             |             Seconds | Blocking                                             |
| Kubernetes runtime smoke  | Main branch and release candidates             |     Several minutes | Blocking for release                                 |
| Chaos/failover matrix     | Nightly and before releases                    |              Longer | Blocking for release; alert on nightly regression    |
| Performance/scalability   | Scheduled and milestone runs                   |            Variable | Trend/report initially; thresholds later             |

Suggested current commands:

```bash
PYTHONPATH=backend venv/bin/python -m unittest discover -s backend/tests/layers -v
PYTHONPATH=backend venv/bin/python -m unittest discover -s backend/tests/integration -v
PYTHONPATH=backend venv/bin/python -m unittest discover -s backend/tests/distributed -v
PYTHONPATH=backend venv/bin/python -m unittest discover -s backend/tests/deployment -v
PYTHONPATH=backend venv/bin/python -m unittest discover -s backend/tests -v
npm run build --prefix frontend
```

As the suite grows, adopting `pytest` is reasonable for fixtures, parametrization, markers, property testing, and clearer expected-failure management, but converting frameworks is not a prerequisite for defining or implementing the strategy.

## 9. Coverage expectations

Line coverage is a supporting signal, not the definition of correctness. The primary standard is semantic and invariant coverage.

Expectations:

- every auction-semantics rule appears in the contract matrix;
- every accepted ADR has at least one test validating its operational consequence;
- every mutation has success, validation rejection, stale-version, duplicate-request, post-reveal, deadline, and replication-failure cases where applicable;
- every failover phase has at least one injected-failure scenario;
- every public response type has visibility assertions;
- every bug fix includes a regression test at the lowest level that reproduces it and, where appropriate, a higher-level acceptance test.

## 10. Migration plan

### Phase 1 — Baseline and label current behavior

1. Run and stabilize the existing suite.
2. Classify each test as contract-aligned, characterization-only, or obsolete.
3. Rename the degraded-write test to make its current-behavior status explicit.
4. Add a coverage inventory linking existing tests to this matrix.

### Phase 2 — Establish executable domain semantics

1. Extract or define a pure auction state-transition model.
2. Implement lifecycle, bid, withdrawal, visibility, outcome, deadline, and tie-breaking tests.
3. Introduce a fake clock and deterministic acceptance sequence.
4. Make these tests the reference for later storage/service behavior.

### Phase 3 — Strengthen mutation identity and concurrency

1. Extend protobuf contracts with request IDs and explicit conflict/error results.
2. Add idempotency and request-reuse tests.
3. Add bounded-retry and revalidation tests.
4. Verify exactly-one version increment per logical mutation.

### Phase 4 — Enforce synchronous replication

1. Add primary-plus-backup integration fixtures.
2. Add failing target tests before implementation.
3. Change write acknowledgement to require the designated backup.
4. Remove the degraded-success characterization once the target behavior passes.

### Phase 5 — Implement failover safety

1. Add epoch and synchronization metadata to the protocol/state model.
2. Test eligibility, fencing, promotion barrier, and backup establishment.
3. Add partition and in-flight request scenarios.
4. Test overdue-auction processing after safe promotion.

### Phase 6 — Validate deployment and operations

1. Add CI workflow stages for layer, contract, integration, distributed, and manifest suites.
2. Add a local Kubernetes smoke-test job.
3. Convert experiment scripts into reproducible workload/chaos runners.
4. Record metrics and compare milestone results.

## 11. Definition of done for a new guarantee

A new behavioral or architectural guarantee is complete only when:

- the relevant auction-semantics rule is clear;
- any architectural choice is recorded or updated in an ADR;
- the contract-to-test matrix identifies the required coverage;
- a failing test demonstrates the previous gap;
- the lowest practical test level verifies the rule;
- cross-component coverage exists when the guarantee depends on communication;
- fault-path coverage exists when the guarantee concerns distributed failure;
- CI runs the relevant suite;
- obsolete characterization behavior is removed or explicitly retained with rationale;
- documentation and test names describe the guarantee rather than incidental implementation details.

## 12. Immediate next test backlog

The first implementation backlog should be:

1. Add a contract test package and auction-state fixtures.
2. Add complete pre-reveal visibility tests, including reserve fields and bidder identities.
3. Add post-reveal projection tests that expose only the winner, amount, outcome, and final bidder count.
4. Add lower-bid rejection and unchanged-version tests.
5. Add withdrawal and rebid tests.
6. Add deterministic tie-breaking tests with explicit acceptance order.
7. Add bounded retry exhaustion and revalidation tests.
8. Add target synchronous-acknowledgement tests alongside the current degraded-write characterization.
9. Add a two-storage-node local gRPC fixture.
10. Add a protocol-gap checklist for request IDs, deadlines, lifecycle state, acceptance order, and epochs.

This order begins with domain and visibility correctness, then moves into concurrency, replication, and failover. It avoids trying to prove target guarantees that the current protocol cannot yet represent while still making those future obligations explicit.
