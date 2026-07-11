🌑 BlindSided: The Opaque Auction Vault
BlindSided is a high-integrity, distributed auction system built on the "Fog of War" principle. It enables real-time competitive bidding while keeping individual bid amounts and bidder identities cryptographically isolated until the point of revelation.

🏗️ Architecture: The Triple-Threat
The system is composed of three primary distributed layers:

The Judge (Storage Cluster): A replicated, stateful vault using Optimistic Concurrency Control (OCC) to ensure atomic writes.

The Controller (Orchestrator): The cluster brain that manages health checks and handles leader election.

The Service Node (API Gateway): The "Fog" generator. It masks bid data and provides a gRPC-Web interface via an Envoy Proxy for browser compatibility.

🛡️ Recent Progress
[x] Envoy Proxy Integration: Established a gRPC-Web bridge for React frontend compatibility.

[x] K8s Orchestration: Migrated all components into a dedicated Kubernetes namespace with internal discovery.

[x] Real-time Opaque Streaming: Implemented JoinLiveAuction streams that push "Fog" updates (high/low ranges) to the UI.

[x] The Gavel Logic: Added reveal mechanics to transition from masked ranges to the final winning state.

🚀 Essential Commands

1. Deployment & Network
   Bash

# Deploy/Update the full cluster

kubectl apply -f deploy/kubernetes/
kubectl apply -f deploy/envoy/kubernetes.yaml

# Port-forward the Gateway (Run this in a dedicated terminal)

kubectl port-forward svc/envoy-svc 8080:8080 -n blindsided

# Monitor the Service Node logs (The "Fog" logic)

kubectl logs -l app=service-node -n blindsided -f 2. Vault Interaction Scripts
Bash

# Seed the initial auction (The Rolex)

python3 scripts/seed_auction.py

# Inject a new secret bid

python3 scripts/bid.py

# End the auction and reveal results

python3 scripts/gavel.py

🧪 Testing

Run the fast backend layer and Kubernetes manifest tests without local network access:

```bash
PYTHONPATH=backend venv/bin/python -m unittest discover -s backend/tests/layers -v
PYTHONPATH=backend venv/bin/python -m unittest discover -s backend/tests/deployment -v
```

Run the full backend suite, including in-process gRPC integration and distributed stress tests:

```bash
PYTHONPATH=backend venv/bin/python -m unittest discover -s backend/tests -v
```

The full suite starts local gRPC servers on `127.0.0.1`, so sandboxed environments may need permission for local port binding.

Run the frontend build check:

```bash
npm run build --prefix frontend
```

🧬 Core Technical Concepts
The Fog of War (Opaque Range)
Instead of broadcasting individual bids, the system broadcasts a Dynamic Range and Bidder Count. This prevents "bid-sniping" and keeps the true price hidden until the Gavel falls.

Optimistic Concurrency (OCC)
Bids must target a specific version. If the vault state moves while a bid is in flight, the Service Node handles a transparent retry to ensure atomic integrity.

🛣️ Roadmap: What's Next?
[ ] React UI Enhancements:

Thermal Gauge: A visual representation of the bid range (blurrier when wide, sharper when narrow).

Auction Creator: Move from terminal scripts to a browser-based creation form.

[ ] Persistence Layer:

Replace Python dictionaries with PostgreSQL or MongoDB to ensure data survives pod restarts.

[ ] Identity & Auth:

Implement JWT-based authentication to verify bidder identities and wallet balances.

[ ] Settlement Service:

A dedicated microservice to trigger mock payments once an auction is revealed.

BlindSided — Bid in the dark. Win in the light.
