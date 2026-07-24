# Blindsided control room

This single-page demonstration presents one blind auction beside the replicated
storage topology that serves it. It is a portfolio demonstration, not a
marketplace or production administration surface.

## Start the demo

Prerequisites: Docker Compose v2, Python 3.11+, and Node.js 24/npm.

From the repository root:

```bash
docker compose -f deploy/compose/docker-compose.yaml up -d --build
```

The failure controls intentionally run outside Compose. They need narrowly
scoped access to the host Docker CLI; the adapter is localhost-only and exposes
fixed actions rather than mounting the Docker socket into a web-facing
container:

```bash
cd frontend
npm run demo-control
```

In another terminal:

```bash
cd frontend
cp .env.example .env.local
npm ci
npm run dev
```

Open `http://localhost:5173`. Grafana is provisioned at
`http://localhost:3000` (`admin` / `admin` in the local Compose demo).

Configuration:

- `VITE_GRPC_WEB_URL`: Envoy gRPC-Web endpoint.
- `VITE_DEMO_CONTROL_URL`: localhost demo-control adapter.
- `VITE_GRAFANA_URL`: Grafana destination for the header link.

Missing variables fail at startup with a readable message. Do not commit
`.env.local`.

## Suggested two-minute sequence

1. Create the demo auction and start simulated bidders.
2. Place or replace the human bid; watch bidder count/version move without
   exposing other bids.
3. Fail the synchronous backup and observe reprotection.
4. Fail the primary and observe temporary unavailability, epoch advancement,
   promotion, and restored synchronous protection.
5. Reveal the auction and inspect only the permitted final outcome.

“Reset demo” clears the browser’s active session; it does not delete durable
auction data. Failure actions wait for replacement synchronization/promotion
and then restore the failed replica, matching the evaluation-script cleanup so
that sequential failures retain a standby. Manual restart controls recover
interrupted actions from the current adapter process.
“Restart cluster” restarts the fixed Compose services and does not delete
volumes.

## End-to-end test

With the cluster and demo-control adapter running:

```bash
npx playwright install chromium
npm run test:e2e
```

There is exactly one principal flow. It creates an auction, drives simulated
and human bids, removes the backup and primary, waits on observable recovery,
then reveals. It skips with an explicit reason if `/demo/status` is unavailable.
Without the environment, `npx playwright test --list` still validates test
discovery without launching the flow.

## Known limitations

- Identity fields are trusted simulation inputs; there is no authentication.
- The current protobuf money fields are floating point. The auction
  specification requires integer minor units before this becomes a publishable
  frontend contract.
- Browser state cannot delete auctions, and the public API exposes versions
  through the watch stream rather than `GetAuction`. Mutation controls wait for
  a watch version; the service retains its existing bounded conflict retry.
- Status is sampled from Prometheus at two-second intervals. Transitional
  states can be shorter than a scrape interval.
- The current histogram cannot identify the most recent individual failover
  duration, so that summary is shown as unknown instead of presenting a
  lifetime average as the latest event.
- The adapter supports the repository’s local Compose service names only and is
  deliberately unsuitable for remote or production deployment.
- The interface manages one active demo auction at a time.
