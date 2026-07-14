import os
import threading
import time

import grpc

from blindsided.common.config import CONTROLLER_ADDRESS, NODE_PORT
from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc


class StorageReplicaService(pb2_grpc.StorageReplicaServiceServicer):
    """Replicated storage layer that owns auction state and version checks."""

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

    def ApplyAuctionMutation(self, request: pb2.AuctionMutationRequest, context) -> pb2.AuctionMutationResponse:
        with self.state_lock:
            auction_id = request.auction.auction_id
            existing_auction = self.auction_store.get(auction_id)
            incoming_auction = request.auction

            if not existing_auction:
                if request.is_reveal_event:
                    return pb2.AuctionMutationResponse(
                        success=False,
                        message="Cannot reveal an auction that does not exist.",
                    )
                if not auction_id.strip():
                    return pb2.AuctionMutationResponse(
                        success=False,
                        message="Auction creation requires an auction id.",
                    )
                if not incoming_auction.seller_id.strip():
                    return pb2.AuctionMutationResponse(
                        success=False,
                        message="Auction creation requires a seller id.",
                    )
                if not incoming_auction.HasField("ends_at"):
                    return pb2.AuctionMutationResponse(
                        success=False,
                        message="Auction creation requires an immutable closing timestamp.",
                    )
                if incoming_auction.reserve_price <= 0:
                    return pb2.AuctionMutationResponse(
                        success=False,
                        message="Auction creation requires a positive reserve price.",
                    )
                if incoming_auction.bids:
                    return pb2.AuctionMutationResponse(
                        success=False,
                        message="Auction creation must start with no active bids.",
                    )
                if incoming_auction.state == pb2.AUCTION_STATE_REVEALED:
                    return pb2.AuctionMutationResponse(
                        success=False,
                        message="Auction creation must begin open.",
                    )
                incoming_auction.state = pb2.AUCTION_STATE_OPEN
                incoming_auction.version = 1
                incoming_auction.reserve_met = False
                incoming_auction.next_bid_sequence = 1

            elif existing_auction.state == pb2.AUCTION_STATE_REVEALED:
                return pb2.AuctionMutationResponse(
                    success=False,
                    message="The Gavel has already fallen.",
                )

            elif (
                incoming_auction.state == pb2.AUCTION_STATE_REVEALED
                and not request.is_reveal_event
            ):
                return pb2.AuctionMutationResponse(
                    success=False,
                    message="Reveal requires a reveal event.",
                )

            else:
                if self._includes_creation_metadata(incoming_auction):
                    return pb2.AuctionMutationResponse(
                        success=False,
                        message="Auction creation properties are immutable.",
                    )

                if (
                    not request.skip_consistency_check
                    and incoming_auction.version != existing_auction.version
                ):
                    return pb2.AuctionMutationResponse(
                        success=False,
                        message="Fog conflict: Stale version.",
                    )

                updated_auction = pb2.Auction()
                updated_auction.CopyFrom(existing_auction)

                if request.is_reveal_event:
                    updated_auction.state = pb2.AUCTION_STATE_REVEALED
                    updated_auction.reserve_met = self._reserve_met(updated_auction)
                else:
                    if incoming_auction.bids and self._auction_has_ended(existing_auction):
                        return pb2.AuctionMutationResponse(
                            success=False,
                            message="Auction deadline has passed.",
                        )

                    next_bid_sequence = self._next_bid_sequence(existing_auction)
                    for bidder_id, incoming_bid in sorted(incoming_auction.bids.items()):
                        current_bid = existing_auction.bids.get(bidder_id)
                        if (
                            current_bid is not None
                            and incoming_bid.amount <= current_bid.amount
                        ):
                            return pb2.AuctionMutationResponse(
                                success=False,
                                message="Bid must be higher than bidder's active bid.",
                            )
                        updated_auction.bids[bidder_id].CopyFrom(
                            pb2.ActiveBid(
                                amount=incoming_bid.amount,
                                acceptance_order=next_bid_sequence,
                            )
                        )
                        next_bid_sequence += 1
                    updated_auction.next_bid_sequence = next_bid_sequence

                updated_auction.version = existing_auction.version + 1
                incoming_auction = updated_auction

            self.auction_store[auction_id] = incoming_auction

            if self.replica_role == "primary":
                if not self._replicate_to_peers(incoming_auction):
                    if existing_auction:
                        self.auction_store[auction_id] = existing_auction
                    else:
                        del self.auction_store[auction_id]
                
                    return pb2.AuctionMutationResponse(
                        success=False,
                        message="Vault replication failed.",
                    )

            return pb2.AuctionMutationResponse(
                success=True,
                current_version=incoming_auction.version,
                message="Vault updated.",
            )

    def _includes_creation_metadata(self, auction: pb2.Auction) -> bool:
        return (
            bool(auction.seller_id.strip())
            or bool(auction.title.strip())
            or bool(auction.category.strip())
            or bool(auction.description.strip())
            or auction.reserve_price > 0
            or auction.HasField("ends_at")
        )

    def _reserve_met(self, auction: pb2.Auction) -> bool:
        if not auction.bids:
            return False
        return max(bid.amount for bid in auction.bids.values()) >= auction.reserve_price

    def _next_bid_sequence(self, auction: pb2.Auction) -> int:
        if auction.next_bid_sequence > 0:
            return auction.next_bid_sequence
        if not auction.bids:
            return 1
        return max(bid.acceptance_order for bid in auction.bids.values()) + 1

    def _auction_has_ended(self, auction: pb2.Auction) -> bool:
        if not auction.HasField("ends_at"):
            return False
        deadline = auction.ends_at.seconds + (auction.ends_at.nanos / 1_000_000_000)
        return time.time() >= deadline

    def GetAuction(self, request: pb2.GetAuctionRequest, context) -> pb2.GetAuctionResponse:
        with self.state_lock:
            auction = self.auction_store.get(request.auction_id)
            if auction:
                return pb2.GetAuctionResponse(ok=True, auction=auction)
            return pb2.GetAuctionResponse(ok=False, message="Auction not found")

    def SearchAuctions(self, request: pb2.SearchAuctionsRequest, context) -> pb2.SearchAuctionsResponse:
        with self.state_lock:
            query = request.query.strip().lower()
            category = request.category.strip().lower()
            auctions = list(self.auction_store.values())
            matches = [
                auction for auction in auctions
                if (not query or query in auction.auction_id.lower()
                    or query in auction.title.lower()
                    or query in auction.description.lower())
                and (not category or category == auction.category.lower())
            ]
            return pb2.SearchAuctionsResponse(
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
