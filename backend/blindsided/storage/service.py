import json
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
        self.state_file_path = (
            os.getenv("AUCTION_STORE_PATH")
            or os.getenv("STORAGE_STATE_PATH")
            or ""
        )

        self.port = os.getenv("NODE_PORT", "50051")
        self.replica_role = os.getenv("NODE_ROLE", "backup")
        raw_peers = os.getenv("PEER_ADDRESSES", "")
        self.peer_addresses = [p.strip() for p in raw_peers.split(",") if p.strip()]
        self.idempotency_records: dict[str, pb2.IdempotencyRecord] = {}

        raw_address = os.getenv("POD_IP", "localhost")
        if "storage-" in raw_address and ".storage-service" not in raw_address:
            self.node_address = f"{raw_address}.storage-service:{NODE_PORT}"
        else:
            self.node_address = (
                raw_address if ":" in raw_address else f"{raw_address}:{NODE_PORT}"
            )

        self._load_state_from_disk()
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
            mutation_type = self._effective_mutation_type(request, existing_auction)
            request_fingerprint = self._request_fingerprint(request, mutation_type)
            idempotency_response = self._check_idempotency(
                request.request_id,
                request_fingerprint,
                existing_auction,
            )
            if idempotency_response:
                return idempotency_response

            if not existing_auction:
                if mutation_type == pb2.AUCTION_MUTATION_TYPE_REVEAL:
                    return pb2.AuctionMutationResponse(
                        success=False,
                        failure_reason=pb2.MUTATION_FAILURE_REASON_NOT_FOUND,
                        message="Cannot reveal an auction that does not exist.",
                    )
                if mutation_type != pb2.AUCTION_MUTATION_TYPE_CREATE:
                    return pb2.AuctionMutationResponse(
                        success=False,
                        failure_reason=pb2.MUTATION_FAILURE_REASON_NOT_FOUND,
                        message="Auction does not exist.",
                    )
                if not auction_id.strip():
                    return pb2.AuctionMutationResponse(
                        success=False,
                        failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
                        message="Auction creation requires an auction id.",
                    )
                if not incoming_auction.seller_id.strip():
                    return pb2.AuctionMutationResponse(
                        success=False,
                        failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
                        message="Auction creation requires a seller id.",
                    )
                if not incoming_auction.HasField("ends_at"):
                    return pb2.AuctionMutationResponse(
                        success=False,
                        failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
                        message="Auction creation requires an immutable closing timestamp.",
                    )
                if incoming_auction.reserve_price <= 0:
                    return pb2.AuctionMutationResponse(
                        success=False,
                        failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
                        message="Auction creation requires a positive reserve price.",
                    )
                if incoming_auction.bids:
                    return pb2.AuctionMutationResponse(
                        success=False,
                        failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
                        message="Auction creation must start with no active bids.",
                    )
                if incoming_auction.state == pb2.AUCTION_STATE_REVEALED:
                    return pb2.AuctionMutationResponse(
                        success=False,
                        failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
                        message="Auction creation must begin open.",
                    )
                incoming_auction.state = pb2.AUCTION_STATE_OPEN
                incoming_auction.version = 1
                incoming_auction.next_bid_sequence = 1

            elif existing_auction.state == pb2.AUCTION_STATE_REVEALED:
                return pb2.AuctionMutationResponse(
                    success=False,
                    current_version=existing_auction.version,
                    failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
                    message="The Gavel has already fallen.",
                )

            elif (
                incoming_auction.state == pb2.AUCTION_STATE_REVEALED
                and mutation_type != pb2.AUCTION_MUTATION_TYPE_REVEAL
            ):
                return pb2.AuctionMutationResponse(
                    success=False,
                    current_version=existing_auction.version,
                    failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
                    message="Reveal requires a reveal event.",
                )

            else:
                if mutation_type != pb2.AUCTION_MUTATION_TYPE_REVEAL:
                    existing_auction = self._auto_reveal_if_due(existing_auction)
                    if existing_auction.state == pb2.AUCTION_STATE_REVEALED:
                        return pb2.AuctionMutationResponse(
                            success=False,
                            current_version=existing_auction.version,
                            failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
                            message="The Gavel has already fallen.",
                        )

                state_error = self._acceptance_order_state_error(existing_auction)
                if state_error:
                    return pb2.AuctionMutationResponse(
                        success=False,
                        current_version=existing_auction.version,
                        failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
                        message=state_error,
                    )

                if (
                    mutation_type != pb2.AUCTION_MUTATION_TYPE_REVEAL
                    and self._includes_creation_metadata(incoming_auction)
                ):
                    return pb2.AuctionMutationResponse(
                        success=False,
                        current_version=existing_auction.version,
                        failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
                        message="Auction creation properties are immutable.",
                    )

                expected_version = request.expected_version or incoming_auction.version
                if expected_version != existing_auction.version:
                    return pb2.AuctionMutationResponse(
                        success=False,
                        current_version=existing_auction.version,
                        failure_reason=pb2.MUTATION_FAILURE_REASON_CONCURRENCY_CONFLICT,
                        message="Fog conflict: Stale version.",
                    )

                updated_auction = pb2.Auction()
                updated_auction.CopyFrom(existing_auction)

                if mutation_type == pb2.AUCTION_MUTATION_TYPE_REVEAL:
                    updated_auction = self._revealed_copy(existing_auction)
                elif mutation_type == pb2.AUCTION_MUTATION_TYPE_PLACE_BID:
                    if not incoming_auction.bids:
                        return pb2.AuctionMutationResponse(
                            success=False,
                            current_version=existing_auction.version,
                            failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
                            message="Bid mutation requires at least one bid.",
                        )
                    if self._auction_has_ended(existing_auction):
                        return pb2.AuctionMutationResponse(
                            success=False,
                            current_version=existing_auction.version,
                            failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
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
                                current_version=existing_auction.version,
                                failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
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
                elif mutation_type == pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID:
                    if self._auction_has_ended(existing_auction):
                        return pb2.AuctionMutationResponse(
                            success=False,
                            current_version=existing_auction.version,
                            failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
                            message="Auction deadline has passed.",
                        )
                    bidder_id = request.bidder_id.strip()
                    if not bidder_id:
                        return pb2.AuctionMutationResponse(
                            success=False,
                            current_version=existing_auction.version,
                            failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
                            message="Withdrawal requires a bidder id.",
                        )
                    if bidder_id not in existing_auction.bids:
                        return pb2.AuctionMutationResponse(
                            success=False,
                            current_version=existing_auction.version,
                            failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
                            message="Bidder has no active bid to withdraw.",
                        )
                    updated_auction.next_bid_sequence = self._next_bid_sequence(
                        existing_auction
                    )
                    del updated_auction.bids[bidder_id]
                else:
                    return pb2.AuctionMutationResponse(
                        success=False,
                        current_version=existing_auction.version,
                        failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
                        message="Unsupported auction mutation type.",
                    )

                updated_auction.version = existing_auction.version + 1
                incoming_auction = updated_auction

            self.auction_store[auction_id] = incoming_auction
            response = pb2.AuctionMutationResponse(
                success=True,
                current_version=incoming_auction.version,
                message="Vault updated.",
                auction_id=incoming_auction.auction_id,
            )
            idempotency_record = self._build_idempotency_record(
                request.request_id,
                request_fingerprint,
                response,
            )

            if self.replica_role == "primary":
                if not self._replicate_to_peers(incoming_auction, idempotency_record):
                    if existing_auction:
                        self.auction_store[auction_id] = existing_auction
                    else:
                        del self.auction_store[auction_id]
                
                    return pb2.AuctionMutationResponse(
                        success=False,
                        current_version=existing_auction.version if existing_auction else 0,
                        failure_reason=pb2.MUTATION_FAILURE_REASON_REPLICATION_FAILED,
                        message="Vault replication failed.",
                    )

            if idempotency_record:
                self.idempotency_records[request.request_id] = idempotency_record
            self._persist_state_to_disk()

            return response

    def _includes_creation_metadata(self, auction: pb2.Auction) -> bool:
        return (
            bool(auction.seller_id.strip())
            or bool(auction.title.strip())
            or bool(auction.category.strip())
            or bool(auction.description.strip())
            or auction.reserve_price > 0
            or auction.HasField("ends_at")
        )

    def _effective_mutation_type(
        self,
        request: pb2.AuctionMutationRequest,
        existing_auction: pb2.Auction | None,
    ) -> pb2.AuctionMutationType:
        if request.mutation_type != pb2.AUCTION_MUTATION_TYPE_UNSPECIFIED:
            return request.mutation_type
        if existing_auction is None:
            return pb2.AUCTION_MUTATION_TYPE_CREATE
        if request.auction.bids:
            return pb2.AUCTION_MUTATION_TYPE_PLACE_BID
        return pb2.AUCTION_MUTATION_TYPE_UNSPECIFIED

    def _request_fingerprint(
        self,
        request: pb2.AuctionMutationRequest,
        mutation_type: pb2.AuctionMutationType,
    ) -> bytes:
        auction = request.auction
        if mutation_type == pb2.AUCTION_MUTATION_TYPE_CREATE:
            payload = {
                "mutation_type": "CREATE",
                "seller_id": auction.seller_id,
                "title": auction.title,
                "category": auction.category,
                "description": auction.description,
                "reserve_price": auction.reserve_price,
                "ends_at": self._timestamp_fingerprint(auction.ends_at)
                if auction.HasField("ends_at")
                else None,
            }
        elif mutation_type == pb2.AUCTION_MUTATION_TYPE_PLACE_BID:
            bidder_id, amount = self._bid_fingerprint_fields(request)
            payload = {
                "mutation_type": "PLACE_BID",
                "auction_id": auction.auction_id,
                "bidder_id": bidder_id,
                "amount": amount,
            }
        elif mutation_type == pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID:
            payload = {
                "mutation_type": "WITHDRAW_BID",
                "auction_id": auction.auction_id,
                "bidder_id": request.bidder_id,
            }
        elif mutation_type == pb2.AUCTION_MUTATION_TYPE_REVEAL:
            payload = {
                "mutation_type": "REVEAL",
                "auction_id": auction.auction_id,
                "seller_id": auction.seller_id,
            }
        else:
            payload = {
                "mutation_type": int(mutation_type),
                "auction_id": auction.auction_id,
            }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    def _timestamp_fingerprint(self, timestamp) -> dict[str, int]:
        return {
            "seconds": timestamp.seconds,
            "nanos": timestamp.nanos,
        }

    def _bid_fingerprint_fields(
        self,
        request: pb2.AuctionMutationRequest,
    ) -> tuple[str, float]:
        if request.bidder_id:
            bid = request.auction.bids.get(request.bidder_id)
            return request.bidder_id, bid.amount if bid is not None else 0.0
        if len(request.auction.bids) == 1:
            bidder_id, bid = next(iter(request.auction.bids.items()))
            return bidder_id, bid.amount
        return "", 0.0

    def _check_idempotency(
        self,
        request_id: str,
        request_fingerprint: bytes,
        existing_auction: pb2.Auction | None,
    ) -> pb2.AuctionMutationResponse | None:
        if not request_id:
            return None
        existing_record = self.idempotency_records.get(request_id)
        if existing_record is None:
            return None
        if existing_record.request_fingerprint != request_fingerprint:
            return pb2.AuctionMutationResponse(
                success=False,
                current_version=existing_auction.version if existing_auction else 0,
                failure_reason=pb2.MUTATION_FAILURE_REASON_IDEMPOTENCY_CONFLICT,
                message="Idempotency conflict: request id was already used for different contents.",
            )
        response = pb2.AuctionMutationResponse()
        response.CopyFrom(existing_record.response)
        response.replayed = True
        return response

    def _build_idempotency_record(
        self,
        request_id: str,
        request_fingerprint: bytes,
        response: pb2.AuctionMutationResponse,
    ) -> pb2.IdempotencyRecord | None:
        if not request_id or not response.success:
            return None
        stored_response = pb2.AuctionMutationResponse()
        stored_response.CopyFrom(response)
        stored_response.replayed = False
        return pb2.IdempotencyRecord(
            request_id=request_id,
            request_fingerprint=request_fingerprint,
            response=stored_response,
        )

    def _build_auction_result(self, auction: pb2.Auction) -> pb2.AuctionResult:
        state_error = self._acceptance_order_state_error(auction)
        if state_error:
            raise ValueError(state_error)

        if not auction.bids:
            return pb2.AuctionResult(
                outcome=pb2.AUCTION_OUTCOME_NO_BIDS,
                reserve_met=False,
                has_winner=False,
            )

        winning_bidder_id, winning_bid = min(
            auction.bids.items(),
            key=lambda item: (-item[1].amount, item[1].acceptance_order, item[0]),
        )
        winning_amount = winning_bid.amount
        if winning_amount < auction.reserve_price:
            return pb2.AuctionResult(
                outcome=pb2.AUCTION_OUTCOME_RESERVE_NOT_MET,
                reserve_met=False,
                has_winner=False,
            )

        return pb2.AuctionResult(
            outcome=pb2.AUCTION_OUTCOME_SUCCESSFUL_SALE,
            reserve_met=True,
            has_winner=True,
            winning_bidder_id=winning_bidder_id,
            winning_amount=winning_amount,
        )

    def _revealed_copy(self, auction: pb2.Auction) -> pb2.Auction:
        revealed_auction = pb2.Auction()
        revealed_auction.CopyFrom(auction)
        revealed_auction.state = pb2.AUCTION_STATE_REVEALED
        revealed_auction.version = auction.version + 1
        revealed_auction.result.CopyFrom(self._build_auction_result(revealed_auction))
        return revealed_auction

    def _auto_reveal_if_due(self, auction: pb2.Auction) -> pb2.Auction:
        if auction.state == pb2.AUCTION_STATE_REVEALED:
            return auction
        if not self._auction_has_ended(auction):
            return auction

        revealed_auction = self._revealed_copy(auction)
        self.auction_store[auction.auction_id] = revealed_auction

        if self.replica_role == "primary" and not self._replicate_to_peers(revealed_auction):
            self.auction_store[auction.auction_id] = auction
            return auction

        self._persist_state_to_disk()
        return revealed_auction

    def _next_bid_sequence(self, auction: pb2.Auction) -> int:
        if auction.next_bid_sequence > 0:
            return auction.next_bid_sequence
        if not auction.bids:
            return 1
        return max(bid.acceptance_order for bid in auction.bids.values()) + 1

    def _acceptance_order_state_error(self, auction: pb2.Auction) -> str:
        active_orders = [
            bid.acceptance_order
            for bid in auction.bids.values()
            if bid.acceptance_order > 0
        ]
        if len(active_orders) != len(set(active_orders)):
            return "Corrupted auction state: duplicate acceptance order."
        if auction.next_bid_sequence > 0 and active_orders:
            next_required_sequence = max(active_orders) + 1
            if auction.next_bid_sequence < next_required_sequence:
                return "Corrupted auction state: next bid sequence is stale."
        return ""

    def _auction_has_ended(self, auction: pb2.Auction) -> bool:
        if not auction.HasField("ends_at"):
            return False
        deadline = auction.ends_at.seconds + (auction.ends_at.nanos / 1_000_000_000)
        return time.time() >= deadline

    def GetAuction(self, request: pb2.GetAuctionRequest, context) -> pb2.GetStoredAuctionResponse:
        with self.state_lock:
            auction = self.auction_store.get(request.auction_id)
            if auction:
                auction = self._auto_reveal_if_due(auction)
                return pb2.GetStoredAuctionResponse(ok=True, auction=auction)
            return pb2.GetStoredAuctionResponse(ok=False, message="Auction not found")

    def SearchAuctions(self, request: pb2.SearchAuctionsRequest, context) -> pb2.GetStoredAuctionsResponse:
        with self.state_lock:
            query = request.query.strip().lower()
            category = request.category.strip().lower()
            auctions = [
                self._auto_reveal_if_due(auction)
                for auction in list(self.auction_store.values())
            ]
            matches = [
                auction for auction in auctions
                if (not query or query in auction.auction_id.lower()
                    or query in auction.title.lower()
                    or query in auction.description.lower())
                and (not category or category == auction.category.lower())
            ]
            return pb2.GetStoredAuctionsResponse(
                ok=True,
                auctions=matches,
                count=len(matches),
                message="Query successful",
            )

    def _replicate_to_peers(
        self,
        auction: pb2.Auction,
        idempotency_record: pb2.IdempotencyRecord | None = None,
    ) -> bool:
        targets = [peer_address for peer_address in self.peer_addresses if peer_address != self.node_address]
        if not targets:
            return True

        success = True

        for peer_address in targets:
            try:
                with grpc.insecure_channel(peer_address) as ch:
                    stub = pb2_grpc.StorageReplicaServiceStub(ch)
                    request = pb2.ReplicationRequest(auction=auction)
                    if idempotency_record:
                        request.idempotency_record.CopyFrom(idempotency_record)
                    resp = stub.ReplicateAuction(
                        request,
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
            state_error = self._acceptance_order_state_error(request.auction)
            if state_error:
                return pb2.ReplicationResponse(
                    success=False,
                    message=state_error,
                )
            self.auction_store[request.auction.auction_id] = request.auction
            if request.HasField("idempotency_record"):
                self.idempotency_records[
                    request.idempotency_record.request_id
                ] = request.idempotency_record
            self._persist_state_to_disk()
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
                idempotency_records=list(self.idempotency_records.values()),
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
                self.idempotency_records = {
                    record.request_id: record
                    for record in resp.idempotency_records
                }
                self._persist_state_to_disk()
        except Exception as e:
            print(f"[Judge] Sync failed: {e}")

    def _load_state_from_disk(self) -> None:
        if not self.state_file_path or not os.path.exists(self.state_file_path):
            return
        try:
            snapshot = pb2.StateResponse()
            with open(self.state_file_path, "rb") as state_file:
                snapshot.ParseFromString(state_file.read())
            self.auction_store = {
                auction.auction_id: auction
                for auction in snapshot.auctions
                if not self._acceptance_order_state_error(auction)
            }
            self.idempotency_records = {
                record.request_id: record
                for record in snapshot.idempotency_records
            }
        except Exception as e:
            print(f"[Judge] Could not load local state snapshot: {e}")

    def _persist_state_to_disk(self) -> None:
        if not self.state_file_path:
            return
        snapshot = pb2.StateResponse(
            ok=True,
            auctions=list(self.auction_store.values()),
            idempotency_records=list(self.idempotency_records.values()),
            message="Local storage snapshot",
        )
        state_dir = os.path.dirname(self.state_file_path)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)
        temp_path = f"{self.state_file_path}.tmp"
        with open(temp_path, "wb") as state_file:
            state_file.write(snapshot.SerializeToString())
        os.replace(temp_path, self.state_file_path)
