import os
import threading
import time

import grpc

from blindsided.common.config import CONTROLLER_ADDRESS, NODE_PORT
from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc


class StorageReplicaService(pb2_grpc.StorageReplicaServiceServicer):
    def __init__(self) -> None:
        self.state_lock = threading.Condition()
        self.auction_store: dict[str, pb2.Auction] = {}

        self.port = os.getenv("NODE_PORT", "50051")
        self.replica_role = os.getenv("NODE_ROLE", "backup")
        raw_peers = os.getenv("PEER_ADDRESSES", "")
        self.peer_addresses = [p.strip() for p in raw_peers.split(",") if p.strip()]

        raw_address = os.getenv("POD_IP", "localhost")
        if "storage-" in raw_address and ".storage-service" not in raw_address:
            self.node_address = f"{raw_address}.storage-service:{NODE_PORT}"
        else:
            self.node_address = (
                raw_address if ":" in raw_address else f"{raw_address}:{NODE_PORT}"
            )

        self._initialize_connection()

    def _initialize_connection(self):
        connected = False
        while not connected:
            try:
                with grpc.insecure_channel(CONTROLLER_ADDRESS) as channel:
                    stub = pb2_grpc.ClusterControllerStub(channel)
                    resp = stub.RegisterNode(
                        pb2.RegisterRequest(address=self.node_address),
                        timeout=2.0,
                    )
                    self.replica_role = "primary" if resp.is_primary else "backup"

                    if self.replica_role == "backup":
                        p_resp = stub.GetPrimary(pb2.GetPrimaryRequest())
                        if p_resp.success and p_resp.primary_address != self.node_address:
                            self._synchronize_from_primary(p_resp.primary_address)
                    connected = True
            except Exception as e:
                print(f"[Judge] Booting... Controller not ready: {e}")
                time.sleep(2)

    def CommitToVault(self, request: pb2.CommitRequest, context) -> pb2.CommitResponse:
        with self.state_lock:
            auction_id = request.auction.auction_id
            existing_auction = self.auction_store.get(auction_id)
            incoming_auction = request.auction

            if not existing_auction:
                if request.is_reveal_event:
                    return pb2.CommitResponse(
                        success=False,
                        message="Cannot reveal an auction that does not exist.",
                    )
                if incoming_auction.state == pb2.AUCTION_STATE_REVEALED:
                    return pb2.CommitResponse(
                        success=False,
                        message="Auction creation must begin open.",
                    )
                incoming_auction.state = pb2.AUCTION_STATE_OPEN
                if incoming_auction.version == 0:
                    incoming_auction.version = 1
                if incoming_auction.bids:
                    highest_bid_amount = max(incoming_auction.bids.values())
                    incoming_auction.reserve_met = highest_bid_amount >= incoming_auction.reserve_price

            elif existing_auction.state == pb2.AUCTION_STATE_REVEALED:
                return pb2.CommitResponse(
                    success=False,
                    message="The Gavel has already fallen.",
                )

            elif (
                incoming_auction.state == pb2.AUCTION_STATE_REVEALED
                and not request.is_reveal_event
            ):
                return pb2.CommitResponse(
                    success=False,
                    message="Reveal requires a reveal event.",
                )

            elif not request.skip_consistency_check:
                if incoming_auction.version != existing_auction.version:
                    return pb2.CommitResponse(
                        success=False,
                        message="Fog conflict: Stale version.",
                    )

                updated_auction = pb2.Auction()
                updated_auction.CopyFrom(existing_auction)

                if request.is_reveal_event:
                    updated_auction.state = pb2.AUCTION_STATE_REVEALED
                else:
                    for bidder_id, amount in incoming_auction.bids.items():
                        updated_auction.bids[bidder_id] = amount

                    highest_bid_amount = max(updated_auction.bids.values())
                    updated_auction.reserve_met = (
                        highest_bid_amount >= updated_auction.reserve_price
                    )

                updated_auction.version = existing_auction.version + 1
                incoming_auction = updated_auction

            self.auction_store[auction_id] = incoming_auction

            if self.replica_role == "primary":
                if not self._replicate_to_peers(incoming_auction):
                    if existing_auction:
                        self.auction_store[auction_id] = existing_auction
                    return pb2.CommitResponse(
                        success=False,
                        message="Vault replication failed.",
                    )

            return pb2.CommitResponse(
                success=True,
                current_version=incoming_auction.version,
                message="Vault updated.",
            )

    def QueryVault(self, request: pb2.QueryRequest, context) -> pb2.QueryResponse:
        with self.state_lock:
            f = request.filter.strip().lower()
            all_a = list(self.auction_store.values())
            matches = all_a if not f else [
                a for a in all_a
                if f in a.auction_id.lower()
                or f in a.title.lower()
                or f in a.description.lower()
            ]
            return pb2.QueryResponse(
                ok=True,
                auctions=matches,
                count=len(matches),
                message="Query successful",
            )

    def _replicate_to_peers(self, auction: pb2.Auction) -> bool:
        targets = [peer_address for peer_address in self.peer_addresses if peer_address != self.node_address]
        if not targets:
            return True

        success = True

        for peer_address in targets:
            try:
                with grpc.insecure_channel(peer_address) as ch:
                    stub = pb2_grpc.StorageReplicaServiceStub(ch)
                    resp = stub.ReplicateAuction(
                        pb2.ReplicationRequest(auction=auction),
                        timeout=1.0,
                    )
                    if not resp.success:
                        success = False
            except Exception:
                print(f"[Judge] Peer {peer_address} unreachable. Proceeding in degraded mode.")
                continue

        return success

    def ReplicateAuction(self, request, context):
        with self.state_lock:
            self.auction_store[request.auction.auction_id] = request.auction
            return pb2.ReplicationResponse(
                success=True,
                ack_version=request.auction.version,
                message="Replicated",
            )

    def SyncFullState(self, request, context):
        with self.state_lock:
            return pb2.StateResponse(
                ok=True,
                auctions=list(self.auction_store.values()),
                message="Sync state provided",
            )

    def Heartbeat(self, request, context):
        with self.state_lock:
            return pb2.HealthCheckResponse(
                alive=True,
                role=self.replica_role,
                message="Alive",
            )

    def PromoteToPrimary(self, request, context):
        with self.state_lock:
            self.replica_role = "primary"
            return pb2.PromotionResponse(success=True, message="Promoted to Primary")

    def _synchronize_from_primary(self, primary_address):
        try:
            with grpc.insecure_channel(primary_address) as ch:
                stub = pb2_grpc.StorageReplicaServiceStub(ch)
                resp = stub.SyncFullState(pb2.StateRequest(), timeout=10.0)
                for auction in resp.auctions:
                    self.auction_store[auction.auction_id] = auction
        except Exception as e:
            print(f"[Judge] Sync failed: {e}")
