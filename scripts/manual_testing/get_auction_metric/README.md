# Manual `GetAuction` metric check

This workflow starts the local gRPC services, performs a successful
`GetAuction`, and verifies that the service-level Prometheus counter increases.

All commands below run from the repository root. Activate the project virtual
environment once in every terminal:

```bash
cd /Users/xanderhill/blindsided
source .venv/bin/activate
export PYTHONPATH=backend
```

## 1. Start the controller

Terminal 1:

```bash
python -m blindsided.controller.server
```

## 2. Start two storage replicas

Terminal 2:

```bash
CONTROLLER_HOST=localhost \
NODE_PORT=50051 \
POD_IP=localhost:50051 \
AUCTION_STORE_PATH=/tmp/blindsided-manual-primary.pb \
python -m blindsided.storage.server
```

Terminal 3:

```bash
CONTROLLER_HOST=localhost \
NODE_PORT=50054 \
POD_IP=localhost:50054 \
AUCTION_STORE_PATH=/tmp/blindsided-manual-backup.pb \
python -m blindsided.storage.server
```

Wait a few seconds for both replicas to register and synchronize.

## 3. Start the auction service and metrics endpoint

Terminal 4:

```bash
CONTROLLER_HOST=localhost \
SERVICE_PORT=50052 \
python -m blindsided.auction_service.server
```

The gRPC endpoint is now available at `localhost:50052`, and Prometheus metrics
are available at `http://localhost:8000/metrics`.

To inspect the raw metric endpoint:

```bash
curl -s http://localhost:8000/metrics | grep blindsided_rpc_requests_total
```

## 4. Run the check

Terminal 5 (with `.venv` activated and `PYTHONPATH=backend` set):

```bash
python scripts/manual_testing/get_auction_metric/test_metric.py
```

Expected output ends with:

```text
PASS: successful GetAuction incremented the metric by 1
```

Optional endpoint overrides:

```bash
SERVICE_ADDRESS=localhost:50052 \
METRICS_URL=http://localhost:8000/metrics \
python scripts/manual_testing/get_auction_metric/test_metric.py
```

Stop each process with `Ctrl-C` when finished. The two `/tmp` state files can
be removed before a clean rerun.
