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
        self.synchronous_backup_address = os.getenv(
            "SYNCHRONOUS_BACKUP_ADDRESS",
            "",
        ).strip()
        self.idempotency_records: dict[str, pb2.IdempotencyRecord] = {}
        self.prepared_mutations: dict[str, pb2.PrepareMutationRequest] = {}
        self.aborted_mutations: dict[str, pb2.MutationDecisionRequest] = {}
        self.pending_backup_commits: dict[str, pb2.CommitDecision] = {}
        self.current_epoch = 0
        self.promotion_ready = False

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
                    self.current_epoch = resp.epoch
                    self.promotion_ready = self.replica_role == "primary"

                    if self.replica_role == "backup":
                        p_resp = stub.GetPrimary(pb2.GetPrimaryRequest())
                        if p_resp.success and p_resp.primary_address != self.node_address:
                            if not self._synchronize_from_primary(
                                p_resp.primary_address,
                                epoch=p_resp.epoch,
                            ):
                                raise RuntimeError(
                                    "Full synchronization did not complete successfully."
                                )
                    connected = True
            except Exception as e:
                print(f"[Judge] Booting... Controller not ready: {e}")
                time.sleep(2)

    def ApplyAuctionMutation(self, request: pb2.AuctionMutationRequest, context) -> pb2.AuctionMutationResponse:
        with self.state_lock:
            if self.replica_role != "primary":
                return pb2.AuctionMutationResponse(
                    success=False,
                    failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
                    message="Auction mutations require the primary replica.",
                )
            if not self.promotion_ready:
                return pb2.AuctionMutationResponse(
                    success=False,
                    failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
                    message="Primary promotion is not ready for mutations.",
                )
            if request.epoch != self.current_epoch:
                return pb2.AuctionMutationResponse(
                    success=False,
                    failure_reason=pb2.MUTATION_FAILURE_REASON_STALE_EPOCH,
                    message=(
                        f"Mutation epoch {request.epoch} does not match "
                        f"primary epoch {self.current_epoch}."
                    ),
                )
            if request.request_id and request.request_id in self.aborted_mutations:
                return pb2.AuctionMutationResponse(
                    success=False,
                    failure_reason=pb2.MUTATION_FAILURE_REASON_IDEMPOTENCY_CONFLICT,
                    message="Request id has been aborted.",
                )
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
                if incoming_auction.HasField("result"):
                    return pb2.AuctionMutationResponse(
                        success=False,
                        failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
                        message="An open auction cannot have a committed result.",
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
            previous_version = existing_auction.version if existing_auction else 0
            if idempotency_record is None:
                return pb2.AuctionMutationResponse(
                    success=False,
                    current_version=previous_version,
                    failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
                    message="Auction mutations require a request id.",
                )
            return self._coordinate_synchronous_commit(
                request.request_id,
                incoming_auction,
                idempotency_record,
                response,
                previous_version,
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
        pending_decision = self.pending_backup_commits.get(request_id)
        if pending_decision is not None:
            if not self._complete_pending_backup_commit(request_id):
                return pb2.AuctionMutationResponse(
                    success=False,
                    current_version=pending_decision.auction.version,
                    failure_reason=(
                        pb2.MUTATION_FAILURE_REASON_ACKNOWLEDGEMENT_PENDING
                    ),
                    auction_id=pending_decision.auction.auction_id,
                    message=(
                        "Commit is durable but backup acknowledgement is pending."
                    ),
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

    def _committed_state_error(self, auction: pb2.Auction) -> str:
        state_error = self._acceptance_order_state_error(auction)
        if state_error:
            return state_error
        if auction.state != pb2.AUCTION_STATE_REVEALED:
            if auction.HasField("result"):
                return "An open auction cannot have a committed result."
            return ""
        if not auction.HasField("result"):
            return "A revealed auction must have a committed result."
        if auction.result != self._build_auction_result(auction):
            return "A revealed auction result does not match its committed bids."
        return ""

    def _auction_has_ended(self, auction: pb2.Auction) -> bool:
        if not auction.HasField("ends_at"):
            return False
        deadline = auction.ends_at.seconds + (auction.ends_at.nanos / 1_000_000_000)
        return time.time() >= deadline

    def GetAuction(self, request: pb2.GetAuctionRequest, context) -> pb2.GetStoredAuctionResponse:
        with self.state_lock:
            if self.replica_role != "primary":
                return pb2.GetStoredAuctionResponse(
                    ok=False,
                    message="Authoritative auction reads require the primary replica.",
                )
            if not self.promotion_ready:
                return pb2.GetStoredAuctionResponse(
                    ok=False,
                    message="Primary promotion is not ready for authoritative reads.",
                )
            auction = self.auction_store.get(request.auction_id)
            if auction:
                return pb2.GetStoredAuctionResponse(ok=True, auction=auction)
            return pb2.GetStoredAuctionResponse(ok=False, message="Auction not found")

    def SearchAuctions(self, request: pb2.SearchAuctionsRequest, context) -> pb2.GetStoredAuctionsResponse:
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
            return pb2.GetStoredAuctionsResponse(
                ok=True,
                auctions=matches,
                count=len(matches),
                message="Query successful",
            )

    def _prepare_on_synchronous_backup(
        self,
        request_id: str,
        candidate_auction: pb2.Auction,
        idempotency_record: pb2.IdempotencyRecord,
    ) -> bool:
        if self.replica_role != "primary" or not self.synchronous_backup_address:
            return False

        request = pb2.PrepareMutationRequest(
            request_id=request_id,
            candidate_auction=candidate_auction,
            idempotency_record=idempotency_record,
            primary_id=self.node_address,
            epoch=self.current_epoch,
        )
        try:
            with grpc.insecure_channel(self.synchronous_backup_address) as channel:
                stub = pb2_grpc.StorageReplicaServiceStub(channel)
                response = stub.PrepareAuctionMutation(request, timeout=1.0)
        except grpc.RpcError:
            return False

        return (
            response.success
            and response.prepared_version == candidate_auction.version
        )

    def _record_commit_decision(
        self,
        request_id: str,
        candidate_auction: pb2.Auction,
        idempotency_record: pb2.IdempotencyRecord,
    ) -> bool:
        with self.state_lock:
            return self._record_commit_decision_locked(
                request_id,
                candidate_auction,
                idempotency_record,
            )

    def _record_commit_decision_locked(
        self,
        request_id: str,
        candidate_auction: pb2.Auction,
        idempotency_record: pb2.IdempotencyRecord,
    ) -> bool:
        if (
            self.replica_role != "primary"
            or not request_id
            or not candidate_auction.auction_id
            or not self.synchronous_backup_address
        ):
            return False

        decision = pb2.CommitDecision(
            request_id=request_id,
            primary_id=self.node_address,
            backup_address=self.synchronous_backup_address,
            epoch=self.current_epoch,
        )
        decision.auction.CopyFrom(candidate_auction)
        decision.idempotency_record.CopyFrom(idempotency_record)

        committed_auction = pb2.Auction()
        committed_auction.CopyFrom(decision.auction)
        committed_record = pb2.IdempotencyRecord()
        committed_record.CopyFrom(decision.idempotency_record)
        pending_decision = pb2.CommitDecision()
        pending_decision.CopyFrom(decision)

        auction_id = committed_auction.auction_id
        previous_auction = self.auction_store.get(auction_id)
        previous_record = self.idempotency_records.get(request_id)
        previous_decision = self.pending_backup_commits.get(request_id)
        self.auction_store[auction_id] = committed_auction
        self.idempotency_records[request_id] = committed_record
        self.pending_backup_commits[request_id] = pending_decision
        try:
            self._persist_state_to_disk()
        except Exception:
            if previous_auction is None:
                del self.auction_store[auction_id]
            else:
                self.auction_store[auction_id] = previous_auction
            if previous_record is None:
                del self.idempotency_records[request_id]
            else:
                self.idempotency_records[request_id] = previous_record
            if previous_decision is None:
                del self.pending_backup_commits[request_id]
            else:
                self.pending_backup_commits[request_id] = previous_decision
            return False
        return True

    def _complete_pending_backup_commit(self, request_id: str) -> bool:
        with self.state_lock:
            if self.replica_role != "primary":
                return False
            decision = self.pending_backup_commits.get(request_id)
            if decision is None or not decision.backup_address:
                return False

            commit_request = pb2.MutationDecisionRequest(
                request_id=decision.request_id,
                auction_id=decision.auction.auction_id,
                primary_id=decision.primary_id,
                epoch=decision.epoch,
            )
            try:
                with grpc.insecure_channel(decision.backup_address) as channel:
                    stub = pb2_grpc.StorageReplicaServiceStub(channel)
                    response = stub.CommitPreparedMutation(
                        commit_request,
                        timeout=1.0,
                    )
            except grpc.RpcError:
                return False

            if (
                not response.success
                or response.committed_version != decision.auction.version
            ):
                return False

            del self.pending_backup_commits[request_id]
            try:
                self._persist_state_to_disk()
            except Exception:
                self.pending_backup_commits[request_id] = decision
                return False
            return True

    def _abort_on_synchronous_backup(
        self,
        request_id: str,
        auction_id: str,
    ) -> bool:
        if self.replica_role != "primary" or not self.synchronous_backup_address:
            return False
        request = pb2.MutationDecisionRequest(
            request_id=request_id,
            auction_id=auction_id,
            primary_id=self.node_address,
            epoch=self.current_epoch,
        )
        try:
            with grpc.insecure_channel(self.synchronous_backup_address) as channel:
                stub = pb2_grpc.StorageReplicaServiceStub(channel)
                response = stub.AbortPreparedMutation(request, timeout=1.0)
        except grpc.RpcError:
            return False
        return response.success

    def _coordinate_synchronous_commit(
        self,
        request_id: str,
        candidate_auction: pb2.Auction,
        idempotency_record: pb2.IdempotencyRecord,
        success_response: pb2.AuctionMutationResponse,
        previous_version: int,
    ) -> pb2.AuctionMutationResponse:
        if not self._prepare_on_synchronous_backup(
            request_id,
            candidate_auction,
            idempotency_record,
        ):
            self._abort_on_synchronous_backup(
                request_id,
                candidate_auction.auction_id,
            )
            return pb2.AuctionMutationResponse(
                success=False,
                current_version=previous_version,
                failure_reason=pb2.MUTATION_FAILURE_REASON_REPLICATION_FAILED,
                auction_id=candidate_auction.auction_id,
                message="Synchronous backup preparation failed.",
            )

        if not self._record_commit_decision(
            request_id,
            candidate_auction,
            idempotency_record,
        ):
            self._abort_on_synchronous_backup(
                request_id,
                candidate_auction.auction_id,
            )
            return pb2.AuctionMutationResponse(
                success=False,
                current_version=previous_version,
                failure_reason=pb2.MUTATION_FAILURE_REASON_REPLICATION_FAILED,
                auction_id=candidate_auction.auction_id,
                message="Primary commit decision could not be persisted.",
            )

        if not self._complete_pending_backup_commit(request_id):
            return pb2.AuctionMutationResponse(
                success=False,
                current_version=candidate_auction.version,
                failure_reason=pb2.MUTATION_FAILURE_REASON_ACKNOWLEDGEMENT_PENDING,
                auction_id=candidate_auction.auction_id,
                message="Commit is durable but backup acknowledgement is pending.",
            )

        return success_response

    def PrepareAuctionMutation(self, request, context):
        with self.state_lock:
            if self.replica_role != "backup":
                return pb2.PrepareMutationResponse(
                    success=False,
                    message="Mutation preparation is allowed only on backup replicas.",
                )
            if request.epoch != self.current_epoch:
                return pb2.PrepareMutationResponse(
                    success=False,
                    message=(
                        f"Replication epoch {request.epoch} does not match "
                        f"backup epoch {self.current_epoch}."
                    ),
                )

            request_id = request.request_id.strip()
            primary_id = request.primary_id.strip()
            auction_id = request.candidate_auction.auction_id.strip()
            if not request_id:
                return pb2.PrepareMutationResponse(
                    success=False,
                    message="Mutation preparation requires a request id.",
                )
            if not primary_id:
                return pb2.PrepareMutationResponse(
                    success=False,
                    message="Mutation preparation requires a primary id.",
                )
            if not auction_id:
                return pb2.PrepareMutationResponse(
                    success=False,
                    message="Mutation preparation requires an auction id.",
                )
            if request_id in self.aborted_mutations:
                return pb2.PrepareMutationResponse(
                    success=False,
                    message="Request id has been aborted.",
                )
            existing_preparation = self.prepared_mutations.get(request_id)
            if existing_preparation is not None:
                if existing_preparation == request:
                    return pb2.PrepareMutationResponse(
                        success=True,
                        prepared_version=existing_preparation.candidate_auction.version,
                        message="Mutation prepared.",
                    )
                return pb2.PrepareMutationResponse(
                    success=False,
                    prepared_version=(
                        self.auction_store.get(auction_id).version
                        if self.auction_store.get(auction_id)
                        else 0
                    ),
                    message="Request id is already prepared with different contents.",
                )
            if request_id in self.idempotency_records:
                return pb2.PrepareMutationResponse(
                    success=False,
                    message="Request id belongs to a committed mutation.",
                )
            if (
                not request.HasField("idempotency_record")
                or request.idempotency_record.request_id != request_id
            ):
                return pb2.PrepareMutationResponse(
                    success=False,
                    message="Idempotency record id must match the request id.",
                )

            candidate = request.candidate_auction
            state_error = self._committed_state_error(candidate)
            if state_error:
                return pb2.PrepareMutationResponse(
                    success=False,
                    message=state_error,
                )

            current = self.auction_store.get(auction_id)
            expected_candidate_version = current.version + 1 if current else 1
            if candidate.version != expected_candidate_version:
                return pb2.PrepareMutationResponse(
                    success=False,
                    prepared_version=current.version if current else 0,
                    message=(
                        "Candidate version does not follow the backup's current "
                        "committed version."
                    ),
                )

            prepared = pb2.PrepareMutationRequest()
            prepared.CopyFrom(request)
            self.prepared_mutations[request_id] = prepared
            try:
                self._persist_state_to_disk()
            except Exception as error:
                del self.prepared_mutations[request_id]
                return pb2.PrepareMutationResponse(
                    success=False,
                    message=f"Could not persist prepared mutation: {error}",
                )
            return pb2.PrepareMutationResponse(
                success=True,
                prepared_version=candidate.version,
                message="Mutation prepared.",
            )

    def CommitPreparedMutation(self, request, context):
        with self.state_lock:
            if self.replica_role != "backup":
                return pb2.MutationDecisionResponse(
                    success=False,
                    message="Prepared mutations can be committed only on backup replicas.",
                )
            if request.epoch != self.current_epoch:
                return pb2.MutationDecisionResponse(
                    success=False,
                    message=(
                        f"Replication epoch {request.epoch} does not match "
                        f"backup epoch {self.current_epoch}."
                    ),
                )

            request_id = request.request_id.strip()
            auction_id = request.auction_id.strip()
            primary_id = request.primary_id.strip()
            if not request_id or not auction_id or not primary_id:
                return pb2.MutationDecisionResponse(
                    success=False,
                    message="Commit requires request, auction, and primary ids.",
                )
            if request_id in self.aborted_mutations:
                return pb2.MutationDecisionResponse(
                    success=False,
                    message="Request id has been aborted.",
                )

            prepared = self.prepared_mutations.get(request_id)
            if prepared is None:
                committed_record = self.idempotency_records.get(request_id)
                if (
                    committed_record is not None
                    and committed_record.response.success
                    and committed_record.response.auction_id == auction_id
                ):
                    return pb2.MutationDecisionResponse(
                        success=True,
                        committed_version=committed_record.response.current_version,
                        message="Prepared mutation committed.",
                    )
                return pb2.MutationDecisionResponse(
                    success=False,
                    message="Prepared mutation not found.",
                )

            if prepared.candidate_auction.auction_id != auction_id:
                return pb2.MutationDecisionResponse(
                    success=False,
                    message="Auction id does not match the prepared mutation.",
                )
            if prepared.primary_id != primary_id:
                return pb2.MutationDecisionResponse(
                    success=False,
                    message="Primary id does not match the prepared mutation.",
                )

            current = self.auction_store.get(auction_id)
            expected_candidate_version = current.version + 1 if current else 1
            candidate = prepared.candidate_auction
            if candidate.version != expected_candidate_version:
                return pb2.MutationDecisionResponse(
                    success=False,
                    committed_version=current.version if current else 0,
                    message=(
                        "Prepared candidate version does not follow the backup's "
                        "current committed version."
                    ),
                )

            committed_auction = pb2.Auction()
            committed_auction.CopyFrom(candidate)
            committed_record = pb2.IdempotencyRecord()
            committed_record.CopyFrom(prepared.idempotency_record)

            previous_auction = self.auction_store.get(auction_id)
            previous_record = self.idempotency_records.get(request_id)
            self.auction_store[auction_id] = committed_auction
            self.idempotency_records[request_id] = committed_record
            del self.prepared_mutations[request_id]
            try:
                self._persist_state_to_disk()
            except Exception as error:
                if previous_auction is None:
                    del self.auction_store[auction_id]
                else:
                    self.auction_store[auction_id] = previous_auction
                if previous_record is None:
                    del self.idempotency_records[request_id]
                else:
                    self.idempotency_records[request_id] = previous_record
                self.prepared_mutations[request_id] = prepared
                return pb2.MutationDecisionResponse(
                    success=False,
                    committed_version=previous_auction.version if previous_auction else 0,
                    message=f"Could not persist committed mutation: {error}",
                )

            return pb2.MutationDecisionResponse(
                success=True,
                committed_version=committed_auction.version,
                message="Prepared mutation committed.",
            )

    def AbortPreparedMutation(self, request, context):
        with self.state_lock:
            if self.replica_role != "backup":
                return pb2.MutationDecisionResponse(
                    success=False,
                    message="Prepared mutations can be aborted only on backup replicas.",
                )
            if request.epoch != self.current_epoch:
                return pb2.MutationDecisionResponse(
                    success=False,
                    message=(
                        f"Replication epoch {request.epoch} does not match "
                        f"backup epoch {self.current_epoch}."
                    ),
                )

            request_id = request.request_id.strip()
            auction_id = request.auction_id.strip()
            primary_id = request.primary_id.strip()
            if not request_id or not auction_id or not primary_id:
                return pb2.MutationDecisionResponse(
                    success=False,
                    message="Abort requires request, auction, and primary ids.",
                )
            if request_id in self.idempotency_records:
                return pb2.MutationDecisionResponse(
                    success=False,
                    message="A committed mutation cannot be aborted.",
                )

            current = self.auction_store.get(auction_id)
            committed_version = current.version if current else 0
            previous_tombstone = self.aborted_mutations.get(request_id)
            if previous_tombstone is not None:
                if (
                    previous_tombstone.auction_id != auction_id
                    or previous_tombstone.primary_id != primary_id
                ):
                    return pb2.MutationDecisionResponse(
                        success=False,
                        committed_version=committed_version,
                        message="Request id was aborted for a different auction or primary.",
                    )
                return pb2.MutationDecisionResponse(
                    success=True,
                    committed_version=committed_version,
                    message="Prepared mutation aborted.",
                )

            prepared = self.prepared_mutations.get(request_id)
            if prepared is not None:
                if prepared.candidate_auction.auction_id != auction_id:
                    return pb2.MutationDecisionResponse(
                        success=False,
                        committed_version=committed_version,
                        message="Auction id does not match the prepared mutation.",
                    )
                if prepared.primary_id != primary_id:
                    return pb2.MutationDecisionResponse(
                        success=False,
                        committed_version=committed_version,
                        message="Primary id does not match the prepared mutation.",
                    )

            tombstone = pb2.MutationDecisionRequest()
            tombstone.CopyFrom(request)
            if prepared is not None:
                del self.prepared_mutations[request_id]
            self.aborted_mutations[request_id] = tombstone
            try:
                self._persist_state_to_disk()
            except Exception as error:
                if prepared is not None:
                    self.prepared_mutations[request_id] = prepared
                if previous_tombstone is None:
                    del self.aborted_mutations[request_id]
                else:
                    self.aborted_mutations[request_id] = previous_tombstone
                return pb2.MutationDecisionResponse(
                    success=False,
                    committed_version=committed_version,
                    message=f"Could not persist aborted mutation: {error}",
                )

            return pb2.MutationDecisionResponse(
                success=True,
                committed_version=committed_version,
                message="Prepared mutation aborted.",
            )

    def SyncFullState(self, request, context):
        with self.state_lock:
            if self.replica_role != "primary":
                return pb2.StateResponse(
                    ok=False,
                    message="Full state synchronization requires the primary replica.",
                )
            if request.epoch != self.current_epoch:
                return pb2.StateResponse(
                    ok=False,
                    message=(
                        f"Synchronization epoch {request.epoch} does not match "
                        f"primary epoch {self.current_epoch}."
                    ),
                )
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

    def BeginPrimaryPromotion(self, request, context):
        with self.state_lock:
            if request.epoch <= 0:
                return pb2.BeginPrimaryPromotionResponse(
                    accepted=False,
                    epoch=self.current_epoch,
                    message="Promotion epoch must be positive.",
                )
            if request.epoch < self.current_epoch:
                return pb2.BeginPrimaryPromotionResponse(
                    accepted=False,
                    epoch=self.current_epoch,
                    message="Promotion epoch is older than the current epoch.",
                )
            if request.epoch == self.current_epoch:
                return pb2.BeginPrimaryPromotionResponse(
                    accepted=True,
                    epoch=self.current_epoch,
                    message="Primary promotion already begun for this epoch.",
                )

            previous_role = self.replica_role
            previous_epoch = self.current_epoch
            previous_ready = self.promotion_ready
            self.replica_role = "primary"
            self.current_epoch = request.epoch
            self.promotion_ready = False
            try:
                self._persist_state_to_disk()
            except Exception as error:
                self.replica_role = previous_role
                self.current_epoch = previous_epoch
                self.promotion_ready = previous_ready
                return pb2.BeginPrimaryPromotionResponse(
                    accepted=False,
                    epoch=self.current_epoch,
                    message=f"Could not persist promotion epoch: {error}",
                )
            return pb2.BeginPrimaryPromotionResponse(
                accepted=True,
                epoch=self.current_epoch,
                message="Primary promotion begun.",
            )

    def ConfirmPromotionState(self, request, context):
        with self.state_lock:
            if request.epoch <= 0 or request.epoch != self.current_epoch:
                return pb2.PromotionStateConfirmationResponse(
                    confirmed=False,
                    epoch=self.current_epoch,
                    message="Confirmation epoch must match the current promotion epoch.",
                )
            if self.replica_role != "primary":
                return pb2.PromotionStateConfirmationResponse(
                    confirmed=False,
                    epoch=self.current_epoch,
                    message="Only the primary promotion candidate can confirm state.",
                )
            for auction in self.auction_store.values():
                state_error = self._committed_state_error(auction)
                if state_error:
                    return pb2.PromotionStateConfirmationResponse(
                        confirmed=False,
                        epoch=self.current_epoch,
                        message=f"Committed auction state is invalid: {state_error}",
                    )
            if self.prepared_mutations:
                return pb2.PromotionStateConfirmationResponse(
                    confirmed=False,
                    epoch=self.current_epoch,
                    message="Committed state has unresolved prepared mutations.",
                )
            if self.pending_backup_commits:
                return pb2.PromotionStateConfirmationResponse(
                    confirmed=False,
                    epoch=self.current_epoch,
                    message="Committed state has unresolved backup commit decisions.",
                )
            return pb2.PromotionStateConfirmationResponse(
                confirmed=True,
                epoch=self.current_epoch,
                message="Committed state confirmed for promotion.",
            )

    def CompletePrimaryPromotion(self, request, context):
        with self.state_lock:
            backup_address = request.backup_address.strip()
            if request.epoch != self.current_epoch:
                return pb2.CompletePrimaryPromotionResponse(
                    success=False,
                    epoch=self.current_epoch,
                    message="Completion epoch must match the current epoch.",
                )
            if self.replica_role != "primary":
                return pb2.CompletePrimaryPromotionResponse(
                    success=False,
                    epoch=self.current_epoch,
                    message="Only a primary promotion candidate can complete promotion.",
                )
            if not backup_address:
                return pb2.CompletePrimaryPromotionResponse(
                    success=False,
                    epoch=self.current_epoch,
                    message="Promotion completion requires a backup address.",
                )
            if backup_address == self.node_address:
                return pb2.CompletePrimaryPromotionResponse(
                    success=False,
                    epoch=self.current_epoch,
                    message="The synchronous backup must differ from the primary.",
                )
            if self.promotion_ready:
                if self.synchronous_backup_address == backup_address:
                    return pb2.CompletePrimaryPromotionResponse(
                        success=True,
                        epoch=self.current_epoch,
                        message="Primary promotion already complete.",
                    )
                return pb2.CompletePrimaryPromotionResponse(
                    success=False,
                    epoch=self.current_epoch,
                    message="Primary promotion completed with a different backup.",
                )

            previous_backup = self.synchronous_backup_address
            self.synchronous_backup_address = backup_address
            self.promotion_ready = True
            try:
                self._persist_state_to_disk()
            except Exception as error:
                self.synchronous_backup_address = previous_backup
                self.promotion_ready = False
                return pb2.CompletePrimaryPromotionResponse(
                    success=False,
                    epoch=self.current_epoch,
                    message=f"Could not persist promotion completion: {error}",
                )
            return pb2.CompletePrimaryPromotionResponse(
                success=True,
                epoch=self.current_epoch,
                message="Primary promotion complete.",
            )

    def SynchronizeFromPrimary(self, request, context):
        with self.state_lock:
            if self.replica_role != "backup":
                return pb2.SynchronizeFromPrimaryResponse(
                    success=False,
                    epoch=self.current_epoch,
                    message="Only a backup can synchronize from a primary.",
                )
            if not request.primary_address.strip() or request.epoch <= 0:
                return pb2.SynchronizeFromPrimaryResponse(
                    success=False,
                    epoch=self.current_epoch,
                    message="Primary address and positive epoch are required.",
                )
            if request.epoch < self.current_epoch:
                return pb2.SynchronizeFromPrimaryResponse(
                    success=False,
                    epoch=self.current_epoch,
                    message="Synchronization epoch is older than the current epoch.",
                )
        synchronized = self._synchronize_from_primary(
            request.primary_address,
            epoch=request.epoch,
        )
        return pb2.SynchronizeFromPrimaryResponse(
            success=synchronized,
            epoch=self.current_epoch,
            message=(
                "Synchronization complete."
                if synchronized
                else "Synchronization failed."
            ),
        )

    def _synchronize_from_primary(self, primary_address: str, epoch: int) -> bool:
        try:
            with grpc.insecure_channel(primary_address) as ch:
                stub = pb2_grpc.StorageReplicaServiceStub(ch)
                response = stub.SyncFullState(
                    pb2.StateRequest(
                        requester_id=self.node_address,
                        epoch=epoch,
                    ),
                    timeout=10.0,
                )
            if not self._replace_with_full_state(response, epoch=epoch):
                return False
            return self._report_synchronization_complete(
                primary_address,
                epoch=epoch,
            )
        except Exception as e:
            print(f"[Judge] Sync failed: {e}")
            return False

    def _replace_with_full_state(self, response: pb2.StateResponse, epoch=None) -> bool:
        if not response.ok:
            return False

        replacement_auctions: dict[str, pb2.Auction] = {}
        for auction in response.auctions:
            if (
                not auction.auction_id.strip()
                or auction.auction_id in replacement_auctions
                or self._committed_state_error(auction)
            ):
                return False
            copied_auction = pb2.Auction()
            copied_auction.CopyFrom(auction)
            replacement_auctions[copied_auction.auction_id] = copied_auction

        replacement_records: dict[str, pb2.IdempotencyRecord] = {}
        for record in response.idempotency_records:
            if not record.request_id.strip() or record.request_id in replacement_records:
                return False
            copied_record = pb2.IdempotencyRecord()
            copied_record.CopyFrom(record)
            replacement_records[copied_record.request_id] = copied_record

        with self.state_lock:
            previous_collections = (
                self.auction_store,
                self.idempotency_records,
                self.prepared_mutations,
                self.aborted_mutations,
                self.pending_backup_commits,
                self.current_epoch,
                self.promotion_ready,
            )
            self.auction_store = replacement_auctions
            self.idempotency_records = replacement_records
            self.prepared_mutations = {}
            self.aborted_mutations = {}
            self.pending_backup_commits = {}
            if epoch is not None:
                self.current_epoch = epoch
            self.promotion_ready = False
            try:
                self._persist_state_to_disk()
            except Exception as error:
                (
                    self.auction_store,
                    self.idempotency_records,
                    self.prepared_mutations,
                    self.aborted_mutations,
                    self.pending_backup_commits,
                    self.current_epoch,
                    self.promotion_ready,
                ) = previous_collections
                print(f"[Judge] Could not persist synchronized state: {error}")
                return False
        return True

    def _report_synchronization_complete(
        self,
        primary_address: str,
        epoch: int,
    ) -> bool:
        try:
            with grpc.insecure_channel(CONTROLLER_ADDRESS) as channel:
                stub = pb2_grpc.ClusterControllerStub(channel)
                response = stub.ReportSynchronizationComplete(
                    pb2.SynchronizationCompleteRequest(
                        replica_address=self.node_address,
                        source_primary_address=primary_address,
                        epoch=epoch,
                    ),
                    timeout=2.0,
                )
            return response.success
        except grpc.RpcError:
            return False

    def _load_state_from_disk(self) -> None:
        if not self.state_file_path or not os.path.exists(self.state_file_path):
            return
        try:
            snapshot = pb2.StorageSnapshot()
            with open(self.state_file_path, "rb") as state_file:
                snapshot.ParseFromString(state_file.read())
            self.auction_store = {
                auction.auction_id: auction
                for auction in snapshot.auctions
                if not self._committed_state_error(auction)
            }
            self.idempotency_records = {
                record.request_id: record
                for record in snapshot.idempotency_records
            }
            self.prepared_mutations = {
                prepared.request_id: prepared
                for prepared in snapshot.prepared_mutations
            }
            self.aborted_mutations = {
                aborted.request_id: aborted
                for aborted in snapshot.aborted_mutations
            }
            self.pending_backup_commits = {
                decision.request_id: decision
                for decision in snapshot.pending_backup_commits
            }
            self.current_epoch = snapshot.current_epoch
            self.promotion_ready = snapshot.promotion_ready
            self.synchronous_backup_address = snapshot.synchronous_backup_address
        except Exception as e:
            print(f"[Judge] Could not load local state snapshot: {e}")

    def _persist_state_to_disk(self) -> None:
        if not self.state_file_path:
            return
        snapshot = pb2.StorageSnapshot(
            ok=True,
            auctions=list(self.auction_store.values()),
            idempotency_records=list(self.idempotency_records.values()),
            prepared_mutations=list(self.prepared_mutations.values()),
            aborted_mutations=list(self.aborted_mutations.values()),
            pending_backup_commits=list(self.pending_backup_commits.values()),
            current_epoch=self.current_epoch,
            promotion_ready=self.promotion_ready,
            synchronous_backup_address=self.synchronous_backup_address,
            message="Local storage snapshot",
        )
        state_dir = os.path.dirname(self.state_file_path)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)
        temp_path = f"{self.state_file_path}.tmp"
        with open(temp_path, "wb") as state_file:
            state_file.write(snapshot.SerializeToString())
        os.replace(temp_path, self.state_file_path)
