# Observability evaluation harness

These manual scenarios exercise Blindsided through its public gRPC APIs and
normal Docker Compose operations. They do not patch application behavior or
observability instrumentation. Every script prints the Grafana dashboard views
and Prometheus metrics expected to move before it begins.

The repository currently provisions Grafana and its Prometheus data source, but
does not contain dashboard JSON. The dashboard names printed by these tools are
the intended views for a manual demo; use the listed metrics in Grafana Explore
or Prometheus until those dashboards are provisioned.

## Prerequisites

- Python 3 with the packages in `requirements.txt`
- The generated Python protobuf files already present under `backend/`
- Docker Engine with Docker Compose v2 for the failure scenarios
- The three-replica Compose stack running:

```bash
docker compose -f deploy/compose/docker-compose.yaml up --build -d --remove-orphans
```

Wait until Prometheus shows all five scrape targets healthy. The defaults assume
the repository's Compose ports:

- AuctionService: `localhost:50052`
- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3000` (`admin` / `admin`)

The role-neutral storage services are `storage-0`, `storage-1`, and `storage-2`.
If upgrading from the former `storage-primary` / `storage-backup` Compose
topology, `--remove-orphans` prevents those renamed containers from continuing
to register with the controller. The new service names also use correspondingly
named state volumes.

Run commands below from the repository root.

## Normal auction traffic

```bash
python3 tools/evaluation/auction_traffic.py
```

The workload creates, reads, searches, bids, withdraws, bids again, reveals,
and finally reads an auction. It exercises RPC, mutation, idempotency,
replication, and commit metrics.

Use a stable scenario ID to replay the same idempotency keys:

```bash
python3 tools/evaluation/auction_traffic.py --run-id demo-normal-1
```

`--create-only` is available for focused write checks. Override the service
address with `--address`.

## Concurrent bidding

```bash
python3 tools/evaluation/concurrent_bidding.py --bidders 24
```

All bidders submit with version `1` at the same barrier. The public service
performs its configured optimistic-concurrency retries. The script verifies
that the authoritative public bidder count equals the number of successful
responses. Retry activity is scheduler-dependent; increase `--bidders` if the
retry counter does not move during a particular run.

## Backup failure during a write

```bash
bash tools/evaluation/backup_failure.sh
```

The three storage replicas have distinct runtime roles: one **primary**, one
**designated synchronous backup**, and one **unassigned standby**. The script
resolves the latter two roles from public Prometheus metrics, stops only the
designated synchronous backup, and proves that a mutation is not acknowledged.
It then waits for controller health detection, standby promotion to replacement
backup, completed synchronization, and restored cluster readiness before
verifying that a new mutation commits. An exit trap restarts the failed backup
even if the scenario fails.

The backup remains stopped long enough for controller health detection and
replacement-backup synchronization; the primary itself is never stopped, so
this remains a backup-recovery scenario rather than a primary failover.

## Primary failure, promotion, and synchronization

```bash
bash tools/evaluation/primary_failover.sh
```

The scenario requires the runtime primary, designated synchronous backup, and
unassigned standby to be present in Prometheus. It stops the metric-identified
primary, then verifies that the former backup is promoted, the former standby
becomes the synchronized replacement backup, and readiness returns at a higher
epoch. It restores the stopped replica and waits for it to return as the
unassigned standby. An exit trap restores the stopped service if the scenario
exits early.

## Configuration

Shell scenarios accept environment overrides:

```bash
COMPOSE_FILE=/path/to/docker-compose.yaml \
AUCTION_ADDRESS=localhost:50052 \
PROMETHEUS_URL=http://localhost:9090 \
RUN_ID=portfolio-demo \
bash tools/evaluation/primary_failover.sh
```

Python scenarios expose all options with `--help`.

## Useful PromQL

```promql
sum by (service, method, result) (increase(blindsided_rpc_requests_total[10m]))
sum by (operation, outcome) (increase(blindsided_mutations_total[10m]))
sum by (operation, outcome) (increase(blindsided_concurrency_retries_total[10m]))
sum by (operation, outcome) (increase(blindsided_replication_attempts_total[10m]))
sum by (operation, outcome) (increase(blindsided_commits_total[10m]))
sum by (outcome) (increase(blindsided_failovers_total[10m]))
sum by (outcome) (increase(blindsided_promotion_attempts_total[10m]))
sum by (outcome) (increase(blindsided_synchronization_attempts_total[10m]))
```

Controller recovery events are low-frequency, so `increase()` over the complete
demo window is generally clearer than a short `rate()`.
