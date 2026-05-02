import threading
from concurrent import futures
import time
import grpc
import os

from proto.src import blindsided_pb2 as pb2
from proto.src import blindsided_pb2_grpc as pb2_grpc
from src.utils.config import CONTROLLER_ADDRESS, NODE_PORT

class JudgeNode(pb2_grpc.JudgeNodeServicer):
    def __init__(self) -> None:
        self.cv = threading.Condition()
        self.vault: dict[str, pb2.Auction] = {}

        self.port = os.getenv("NODE_PORT", "50051")
        self.role = os.getenv("NODE_ROLE", "backup") 
        raw_peers = os.getenv("PEER_ADDRESSES", "")
        self.peer_addresses = [p.strip() for p in raw_peers.split(",") if p.strip()]
        
        raw_address = os.getenv("POD_IP", "localhost")
        if "storage-" in raw_address and ".storage-service" not in raw_address:
            self.my_full_address = f"{raw_address}.storage-service:{NODE_PORT}"
        else:
            self.my_full_address = raw_address if ":" in raw_address else f"{raw_address}:{NODE_PORT}"

        self._initialize_connection()

    def _initialize_connection(self):
        connected = False
        while not connected:
            try:
                with grpc.insecure_channel(CONTROLLER_ADDRESS) as channel:
                    stub = pb2_grpc.ControllerStub(channel)
                    resp = stub.RegisterNode(pb2.RegisterRequest(address=self.my_full_address), timeout=2.0)
                    self.role = "primary" if resp.is_primary else "backup"
                    
                    if self.role == "backup":
                        p_resp = stub.GetPrimary(pb2.GetPrimaryRequest())
                        if p_resp.success and p_resp.primary_address != self.my_full_address:
                            self._sync_vault(p_resp.primary_address)
                    connected = True
            except Exception as e:
                print(f"[Judge] Booting... Controller not ready: {e}")
                time.sleep(2)

    def CommitToVault(self, request: pb2.CommitRequest, context) -> pb2.CommitResponse:
        with self.cv:
            auction_id = request.auction.auction_id
            existing = self.vault.get(auction_id)
            incoming = request.auction

            if not existing:
                if incoming.version == 0: incoming.version = 1
            elif not request.skip_consistency_check:
                if incoming.version != existing.version:
                    return pb2.CommitResponse(success=False, message="Fog conflict: Stale version.")
                if existing.is_revealed:
                    return pb2.CommitResponse(success=False, message="The Gavel has already fallen.")
                if request.is_reveal_event:
                    incoming.sealed_bid = existing.sealed_bid
                    incoming.lead_bidder_id = existing.lead_bidder_id
                else:
                    if incoming.sealed_bid <= existing.sealed_bid:
                        return pb2.CommitResponse(success=False, message="Blindsided! Bid too low.")
                incoming.version = existing.version + 1

            self.vault[auction_id] = incoming
            
            if self.role == "primary":
                if not self._replicate_to_peers(incoming):
                    if existing: self.vault[auction_id] = existing
                    return pb2.CommitResponse(success=False, message="Vault replication failed.")

            return pb2.CommitResponse(success=True, current_version=incoming.version, message="Vault updated.")

    def QueryVault(self, request: pb2.QueryRequest, context) -> pb2.QueryResponse:
        with self.cv:
            f = request.filter.strip().lower()
            all_a = list(self.vault.values())
            matches = all_a if not f else [
                a for a in all_a if f in a.auction_id.lower() or f in a.title.lower() or f in a.description.lower()
            ]
            return pb2.QueryResponse(ok=True, auctions=matches, count=len(matches), message="Query successful")

    def _replicate_to_peers(self, auction: pb2.Auction) -> bool:
        success = True
        for addr in self.peer_addresses:
            if addr == self.my_full_address: continue
            try:
                with grpc.insecure_channel(addr) as ch:
                    stub = pb2_grpc.JudgeNodeStub(ch)
                    resp = stub.ReplicateSecret(pb2.ReplicationRequest(auction=auction), timeout=2.0)
                    if not resp.success: success = False
            except: success = False
        return success

    def ReplicateSecret(self, request, context):
        with self.cv:
            self.vault[request.auction.auction_id] = request.auction
            return pb2.ReplicationResponse(success=True, ack_version=request.auction.version, message="Replicated")

    def SyncFullState(self, request, context):
        with self.cv:
            return pb2.StateResponse(ok=True, auctions=list(self.vault.values()), message="Sync state provided")

    def Heartbeat(self, request, context):
        with self.cv:
            return pb2.HealthCheckResponse(alive=True, role=self.role, message="Alive")

    def PromoteToPrimary(self, request, context):
        with self.cv:
            self.role = "primary"
            return pb2.PromotionResponse(success=True, message="Promoted to Primary")

    def _sync_vault(self, primary_addr):
        try:
            with grpc.insecure_channel(primary_addr) as ch:
                stub = pb2_grpc.JudgeNodeStub(ch)
                resp = stub.SyncFullState(pb2.StateRequest(), timeout=10.0)
                for a in resp.auctions: self.vault[a.auction_id] = a
        except Exception as e:
            print(f"[Judge] Sync failed: {e}")

def serve() -> None:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    pb2_grpc.add_JudgeNodeServicer_to_server(JudgeNode(), server)
    server.add_insecure_port(f"[::]:{NODE_PORT}")
    print(f"Judge Node (Vault) starting on port {NODE_PORT}...")
    server.start()
    server.wait_for_termination()

if __name__ == "__main__":
    serve()