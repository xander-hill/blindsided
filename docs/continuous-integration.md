# Continuous integration

## Required pull-request checks

`.github/workflows/ci.yml` runs on every pull request and push to `main`, with
read-only repository permissions and cancellation of obsolete runs for the
same branch or pull request.

The **backend** job uses Python 3.11, the runtime version used by the backend
container. It installs `requirements-dev.txt`, compiles backend, tool, script,
and demo-control Python modules, runs `git diff --check`, and executes the full
`unittest` suite (including deployment manifest tests). It then installs the
repository's pinned protobuf generators, regenerates Python and TypeScript
clients, and fails on committed-code drift. The project has no configured
Python formatter or static linter, so CI does not introduce a parallel
toolchain solely for closeout.

The **frontend** job uses Node.js 24 and the committed npm lockfile. It runs:

```bash
cd frontend
npm ci
npm run lint
npm run build
npx playwright test --list
python3 -m py_compile demo-control/server.py
```

Playwright discovery validates configuration and test loading without
requiring browsers or the distributed environment.

## Manual and scheduled system validation

`.github/workflows/manual-evaluation.yml` runs through `workflow_dispatch` and
weekly on Monday. Each matrix entry creates a fresh Compose environment,
waits for Prometheus cluster readiness and the gRPC service port, applies a
scenario-specific timeout, and captures startup, timeline, Compose state, and
service logs. An `always()` teardown removes containers, networks, orphans, and
volumes.

The matrix covers normal flow, concurrent bidding, backup failure, primary
failover, restart durability, watch behavior, observability, and the principal
frontend Playwright E2E flow. These checks
are not branch-protection blockers: they build containers, intentionally stop
replicas in the isolated runner, and include health-detection and
synchronization windows.

Kubernetes scaling is excluded from hosted CI because it requires a known-good
cluster, metrics-server, a locally available application image, and sufficient
cluster permissions. Run it locally or on an explicitly prepared self-hosted
runner.

## Equivalent local commands

Required backend checks:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m compileall -q backend tools scripts frontend/demo-control
git diff --check
PYTHONPATH=backend .venv/bin/python -m unittest discover -s backend/tests -v
```

Required frontend checks:

```bash
cd frontend
npm ci
npm run lint
npm run build
npx playwright test --list
python3 -m py_compile demo-control/server.py
```

System evaluation:

```bash
docker compose -f deploy/compose/docker-compose.yaml up -d --build --remove-orphans
.venv/bin/python tools/evaluation/auction_traffic.py
.venv/bin/python tools/evaluation/concurrent_bidding.py --bidders 24
bash tools/evaluation/backup_failure.sh
bash tools/evaluation/primary_failover.sh
.venv/bin/python tools/evaluation/restart_durability.py
.venv/bin/python tools/evaluation/watch_behavior.py
.venv/bin/python tools/evaluation/observability_check.py
```

With a prepared Kubernetes environment:

```bash
bash tools/evaluation/kubernetes_scaling.sh
```
