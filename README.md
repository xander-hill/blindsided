# 🌑 BlindSided: The Opaque Auction Vault

**BlindSided** is a high-integrity, distributed auction system built on the "Fog of War" principle. It enables real-time competitive bidding while keeping individual bid amounts and bidder identities cryptographically isolated until the point of revelation.

---

## 🏗️ Architecture: The Triple-Threat

The system is composed of three primary distributed layers:

- **The Judge (Storage Cluster):** A replicated, stateful vault. It uses **Optimistic Concurrency Control (OCC)** to ensure atomic writes and maintains a sequential version history for every auction.
- **The Controller (Orchestrator):** The brain of the cluster. It manages health checks, handles leader election, and directs traffic to the current Primary Judge.
- **The Service Node (API Gateway):** The "Fog" generator. It masks incoming bid data, manages transparent retries for concurrency conflicts, and provides a gRPC interface for clients.

---

## 🛡️ Proven Resilience

The system has been verified through rigorous **Chaos Monkey** testing:

- **Atomic Integrity:** Handled **500 concurrent bids** from 50 unique users with **0% data loss**.
- **Failover Recovery:** Survived the "assassination" of the Primary Judge mid-traffic.
- **Self-Healing:** Service Nodes successfully pivoted to a new Leader during a 1.5s election window without dropping client requests or corrupting state.

---

## 🚀 Getting Started

### 1. Prerequisites

- Python 3.10+
- Docker & Kubernetes (`kubectl` context set)
- `grpcio-tools`

### 2. Spin up the Cluster

# 1. Start the Storage Cluster (Judges) via Kubernetes

kubectl apply -f k8s/storage-deployment.yaml

# 2. Start the Controller (Election Logic)

python controller.py

# 3. Start the Service Node (API)

python service_node.py

## 🛰️ API Reference (gRPC)

| Method                | Role    | Logic                                                 |
| :-------------------- | :------ | :---------------------------------------------------- |
| **`OpenAuction`**     | Host    | Initializes a new Vault with a reserve price.         |
| **`PlaceSecretBid`**  | Buyer   | Submits an opaque bid with transparent OCC retries.   |
| **`JoinLiveAuction`** | Watcher | A server-side stream providing a "Live Opaque Range." |
| **`GetStatus`**       | System  | Retrieves current vault version and auction metadata. |

---

## 🧬 Core Technical Concepts

### **The Fog of War (Opaque Range)**

Unlike traditional auctions, **BlindSided** does not broadcast individual bids. It broadcasts a dynamic **Range** and a **Bidder Count**.

- **Goal:** Maintain competitive pressure.
- **Result:** No "Winning Margin" leaks to snipers or bots.

### **Optimistic Concurrency (OCC)**

Every bid must target a specific `version` of the auction state to prevent the "Lost Update" problem:

1. **Fetch:** Client retrieves version $N$.
2. **Submit:** Client submits bid for version $N$.
3. **Validate:** If the Judge has moved to $N+1$, the Service Node auto-fetches the update and retries.

### **Strong Consistency Replication**

Judges operate in a **Leader/Follower** model.

> **Note:** A bid is only considered "Vaulted" once the Primary successfully replicates the state change to available Followers, ensuring data survival even during a Primary node failure.

---

## 🛣️ Roadmap

- [x] **Distributed Replicated Storage**
- [x] **Controller-led Leader Election**
- [x] **Chaos/Stress Test Validation**
- [ ] **Envoy Proxy Integration** (Current Phase)
- [ ] **React Dashboard** (gRPC-Web)
- [ ] **Multi-Auction Sharding**

---

**BlindSided** — _Bid in the dark. Win in the light._
