# 🔨 BlindSided

---

### 🚩 Project Status: Migration & Refactor

**Note:** This project is a specialized fork/transfer from a previous Distributed Systems repository.

- **Current Objective:** Transitioning from a standard Marketplace model to a "Blind Bid" Auction architecture.
- **Immediate Task:** Infrastructure validation (Scaffolding, Dockerization, and gRPC pathing).
- **Primary Focus:** Ensuring the baseline distributed state (Replication & Sync) is stable before implementing the Auction "Judge" logic.

---

**"The price is hidden. The stakes are distributed. Don't get blindsided."**

## 🕹️ The Game

BlindSided is a distributed, high-concurrency auction engine where users bid against the "Fog of War." Unlike a traditional marketplace, prices are strictly confidential. You have one shot to outmaneuver your rivals using nothing but your gut and the current state version.

### ⚔️ Game Mechanics

- **Shadow Bidding:** The `highest_bid` is never revealed until the Gavel falls (Auction Close).
- **The Judge (Storage Nodes):** Our distributed nodes act as impartial judges, using **Optimistic Locking** to resolve tie-breaks in the dark.
- **State Transfer:** Even if a node "dies" mid-battle, it can sync the current auction history from the Primary upon rebirth.
- **The Reveal:** Only when the `is_closed` flag is flipped do the secrets come to light.

## 🏗️ Architecture

- **API Gateway (Service Nodes):** The frontline where bids are accepted and masked.
- **The Vault (Storage Nodes):** A StatefulSet holding the hidden bids in memory with gRPC replication.
- **The Overseer (Controller):** Manages leader election and keeps the nodes healthy.

## 🛠️ Tech Stack

- **Engine:** Python 3.12
- **Comms:** gRPC & Protobuf
- **Orchestration:** Kubernetes (Kind)
- **Consistency:** Multi-node replication with Snapshot Sync

## 🛠️ Developer Setup (Task #1)

To ensure the transferred logic is running in the new environment:

1. **Environment:**
   - [ ] Create and activate `.venv`
   - [ ] Install dependencies: `pip install -r requirements.txt`
2. **Protobuf Compilation:**
   - [ ] Run the compiler:
         `python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. ./proto/auction.proto`

3. **Local Validation:**
   - [ ] Build Docker image: `docker build -t blindsided:latest .`
   - [ ] Deploy to Kind/K8s: `kubectl apply -f k8s/`
4. **Smoke Test:**
   - [ ] Verify `storage-0` and `storage-1` can perform a state-sync handshake.
