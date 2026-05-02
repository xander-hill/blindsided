import threading
from concurrent import futures

import grpc

import os

from proto.src import marketplace_pb2, marketplace_pb2_grpc
from proto.src.marketplace_pb2 import Item
from src.utils.config import CONTROLLER_ADDRESS, MY_ADDRESS, NODE_PORT


class StorageNode(marketplace_pb2_grpc.StorageReplicaServicer):
    def __init__(self) -> None:
        self.cv = threading.Condition()
        self.items_by_id: dict[str, Item] = {}

        self.port = os.getenv("NODE_PORT", "50051")
        self.role = os.getenv("NODE_ROLE", "backup") 
        raw_peers = os.getenv("PEER_ADDRESSES", "")
        self.peer_addresses = [p.strip() for p in raw_peers.split(",") if p.strip()]
        raw_address = os.getenv("POD_IP", "localhost")

        # Handle address formatting
        if "storage-" in raw_address and ".storage-service" not in raw_address:
            self.my_full_address = f"{raw_address}.storage-service:{NODE_PORT}"
        else:
            self.my_full_address = raw_address if ":" in raw_address else f"{raw_address}:{NODE_PORT}"

        try:
            with grpc.insecure_channel(CONTROLLER_ADDRESS) as channel:
                stub = marketplace_pb2_grpc.ControllerStub(channel)
                
                # 1. Register with the controller
                resp = stub.RegisterNode(marketplace_pb2.RegisterRequest(address=self.my_full_address))
                self.role = "primary" if resp.is_primary else "backup"
                print(f"Registered with Controller. Role assigned: {self.role.upper()}")

                # 2. IF BACKUP: Ask Controller for the current Primary to sync state
                if self.role == "backup":
                    primary_resp = stub.GetPrimary(marketplace_pb2.Empty())
                    if primary_resp.success and primary_resp.primary_address != self.my_full_address:
                        self.sync_from_primary(primary_resp.primary_address)
                    else:
                        print("No primary found or I am the first node. Skipping initial sync.")

        except Exception as e:
            print(f"Could not connect to Controller: {e}. Defaulting to {self.role}")

    def PutItem(self, request: marketplace_pb2.PutRequest, context) -> marketplace_pb2.PutResponse:
        with self.cv:
            item_id = request.item.item_id
            existing = self.items_by_id.get(item_id)
            incoming_item = request.item

            # --- CASE A: NEW AUCTION ---
            if not existing:
                if incoming_item.version == 0:
                    incoming_item.version = 1
                # Ensure it starts as open unless specified
                # incoming_item.highest_bid is already set via starting_price in proto logic
            
            # --- CASE B: BIDDING OR CLOSING (The Judge) ---
            elif not request.skip_consistency_check:
                # 1. Version Check (Optimistic Locking)
                if incoming_item.version != existing.version:
                    return marketplace_pb2.PutResponse(
                        success=False,
                        current_version=existing.version,
                        message=f"Fog of War conflict. Version mismatch (Storage: {existing.version})",
                    )

                # 2. Status Check (Is the auction over?)
                if existing.is_closed:
                    return marketplace_pb2.PutResponse(
                        success=False,
                        current_version=existing.version,
                        message="The Gavel has fallen. Auction is closed.",
                    )

                # 3. The Secret Comparison
                # If this is a bid (not just a metadata update like description)
                if incoming_item.highest_bid <= existing.highest_bid:
                    return marketplace_pb2.PutResponse(
                        success=False,
                        current_version=existing.version,
                        message="Blindsided! Your bid was too low.",
                    )
                
                # 4. Success: Increment version
                incoming_item.version = existing.version + 1

            # --- Save & Replicate ---
            self.items_by_id[item_id] = incoming_item
            
            if self.role == "primary":
                if not self.PropagateToBackups(incoming_item):
                    # Rollback
                    if existing: self.items_by_id[item_id] = existing
                    else: del self.items_by_id[item_id]
                    return marketplace_pb2.PutResponse(success=False, message="Sync failed.")

            return marketplace_pb2.PutResponse(
                success=True,
                current_version=incoming_item.version,
                message="Accepted into the Vault."
            )

    def PropagateToBackups(self, item: Item) -> bool:
        all_acks = True
        for addr in self.peer_addresses:
            # EXACT match only
            if addr == self.my_full_address:
                print(f"Skipping replication to self ({addr})")
                continue
            
            print(f"Attempting replication to backup: {addr}")
            try:
                with grpc.insecure_channel(addr) as channel:
                    stub = marketplace_pb2_grpc.StorageReplicaStub(channel)
                    response = stub.ReplicateLog(marketplace_pb2.ReplicationRequest(item=item), timeout=2.0)
                    if not response.success:
                        all_acks = False
            except Exception as e:
                print(f"Replication to {addr} failed: {e}")
                all_acks = False
        return all_acks

    def QueryItems(self, request: marketplace_pb2.QueryRequest, context) -> marketplace_pb2.QueryResponse:
        with self.cv:
            filter_text = request.filter.strip().lower()
            all_items = list(self.items_by_id.values())

            if not filter_text:
                matches = all_items
            else:
                matches = [
                    item
                    for item in all_items
                    if filter_text in item.title.lower()
                    or filter_text in item.category.lower()
                    or filter_text in item.description.lower()
                ]

            return marketplace_pb2.QueryResponse(
                ok=True,
                items=matches,
                items_found=len(matches),
            )

    def SyncFullState(
        self, request: marketplace_pb2.StateRequest, context
    ) -> marketplace_pb2.StateResponse:
        with self.cv:
            items = list(self.items_by_id.values())
            last_version = max((item.version for item in items), default=0)
            return marketplace_pb2.StateResponse(
                ok=True,
                items=items,
                last_included_version=last_version,
            )

    def ReplicateLog(
        self, request: marketplace_pb2.ReplicationRequest, context
    ) -> marketplace_pb2.ReplicationResponse:
        with self.cv:
            print(f"[BACKUP] Received replication for {request.item.item_id} (v{request.item.version})")
            self.items_by_id[request.item.item_id] = request.item
            return marketplace_pb2.ReplicationResponse(
                success=True,
                ack_version=request.item.version,
            )

    def Heartbeat(
        self, request: marketplace_pb2.HealthCheckRequest, context
    ) -> marketplace_pb2.HealthCheckResponse:
        with self.cv:
            return marketplace_pb2.HealthCheckResponse(
                alive=True,
                item_count=len(self.items_by_id),
                role=self.role,
            )
        
    def PromoteToPrimary(self, request, context):
        with self.cv:
            self.role = "primary"
            print("I have been promoted to PRIMARY!")
            return marketplace_pb2.PromotionResponse(success=True)
        
    def GetSnapshot(self, request, context):
        with self.cv:
            print(f"[PRIMARY] Providing snapshot to a joining backup...")
            items_list = list(self.items_by_id.values())
            return marketplace_pb2.SnapshotResponse(items=items_list)
    
    def sync_from_primary(self, primary_addr):
        print(f"[BACKUP] Attempting to sync state from {primary_addr}...")
        try:
            # Use a longer timeout for the full snapshot than a single write
            with grpc.insecure_channel(primary_addr) as channel:
                stub = marketplace_pb2_grpc.StorageReplicaStub(channel)
                response = stub.GetSnapshot(marketplace_pb2.Empty(), timeout=10.0)
                
                for item in response.items:
                    self.items_by_id[item.item_id] = item
                    
                print(f"[BACKUP] Sync complete. Loaded {len(response.items)} items.")
        except Exception as e:
            print(f"[BACKUP] Sync failed: {e}")


def serve() -> None:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    marketplace_pb2_grpc.add_StorageReplicaServicer_to_server(StorageNode(), server)
    server.add_insecure_port(f"[::]:{NODE_PORT}")
    print(f"Storage node gRPC server starting on port {NODE_PORT}...")
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    serve()