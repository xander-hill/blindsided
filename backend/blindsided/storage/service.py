import json
import logging
import os
import threading
import time

import grpc

from blindsided.common.config import CONTROLLER_ADDRESS, NODE_PORT
from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc
from blindsided.observability.instrumentation import (
    observe_rpc,
    record_commit_outcome,
    refresh_storage_state_metrics,
    replication_operation,
)
from blindsided.storage.auction_domain import AuctionDomain
from blindsided.storage.replication_client import SynchronousReplicationClient
from blindsided.storage.snapshot_repository import StorageSnapshotRepository
from blindsided.storage.synchronization_client import ReplicaSynchronizationClient


LOGGER = logging.getLogger(__name__)


def _log_event(level: int, event: str, **fields) -> None:
    def render(value):
        if isinstance(value, bool):
            return str(value).lower()
        return "none" if value is None else str(value)

    LOGGER.log(
        level,
        "event=%s %s",
        event,
        " ".join(f"{key}={render(value)}" for key, value in fields.items()),
    )


def _classify_success(response) -> str:
    return "success" if response.success else "failure"


def _classify_ok(response) -> str:
    return "success" if response.ok else "failure"


def _classify_alive(response) -> str:
    return "success" if response.alive else "unavailable"


def _classify_accepted(response) -> str:
    return "success" if response.accepted else "rejected"


def _classify_confirmed(response) -> str:
    return "success" if response.confirmed else "rejected"


class StorageReplicaService(pb2_grpc.StorageReplicaServiceServicer):
    """Own replica state and orchestrate storage, replication, and failover RPCs."""

    # These collaborators are stateless, so lightweight service instances that
    # bypass normal startup can safely use the shared defaults.
    auction_domain = AuctionDomain()
    synchronization_client = ReplicaSynchronizationClient()
    replication_client = SynchronousReplicationClient()

    def __init__(self, *, initialize_connection: bool = True) -> None:
        # All mutable protocol state remains owned by the service and guarded by
        # the same condition for the lifetime of the replica.
        self.state_lock = threading.Condition()
        self.auction_store: dict[str, pb2.Auction] = {}
        self.idempotency_records: dict[str, pb2.IdempotencyRecord] = {}
        self.prepared_mutations: dict[str, pb2.PrepareMutationRequest] = {}
        self.aborted_mutations: dict[str, pb2.MutationDecisionRequest] = {}
        self.pending_backup_commits: dict[str, pb2.CommitDecision] = {}
        self.current_epoch = 0
        self.promotion_ready = False
        self._storage_metrics_ready = False
        self._last_synchronization_failure_retryable = True
        self._last_synchronization_failure_reason = ""

        self.state_file_path = (
            os.getenv("AUCTION_STORE_PATH")
            or os.getenv("STORAGE_STATE_PATH")
            or ""
        )
        self.snapshot_repository = StorageSnapshotRepository(self.state_file_path)
        self.auction_domain = AuctionDomain()
        self.synchronization_client = ReplicaSynchronizationClient()
        self.replication_client = SynchronousReplicationClient()

        self.port = os.getenv("NODE_PORT", "50051")
        self.replica_role = os.getenv("NODE_ROLE", "backup")
        self.synchronous_backup_address = os.getenv(
            "SYNCHRONOUS_BACKUP_ADDRESS",
            "",
        ).strip()

        self.node_address = os.getenv(
            "POD_IP",
            f"localhost:{NODE_PORT}",
        )

        refresh_storage_state_metrics(role="unassigned", ready=False, epoch=0)
        self._load_state_from_disk()
        if initialize_connection:
            self._initialize_connection()

    def _refresh_storage_state_metrics_locked(self) -> None:
        refresh_storage_state_metrics(
            role=self.replica_role,
            ready=self._storage_metrics_ready,
            epoch=self.current_epoch,
        )

    def _initialize_connection(self) -> None:
        """Register with the controller and synchronize before serving as backup."""
        connected = False
        while not connected:
            try:
                with grpc.insecure_channel(CONTROLLER_ADDRESS) as channel:
                    stub = pb2_grpc.ClusterControllerStub(channel)
                    registration = stub.RegisterNode(
                        self._registration_request(),
                        timeout=2.0,
                    )
                    with self.state_lock:
                        self.replica_role = (
                            "primary" if registration.is_primary else "backup"
                        )
                        self.current_epoch = registration.epoch
                        self.promotion_ready = self.replica_role == "primary"
                        self._storage_metrics_ready = self.promotion_ready
                        self._refresh_storage_state_metrics_locked()

                    if self.replica_role == "backup":
                        primary = stub.GetPrimary(pb2.GetPrimaryRequest())
                        if primary.success and primary.primary_address != self.node_address:
                            if not self._synchronize_from_primary(
                                primary.primary_address,
                                epoch=primary.epoch,
                            ):
                                if not self._last_synchronization_failure_retryable:
                                    _log_event(
                                        logging.INFO,
                                        "storage_standby_settled",
                                        target_replica=self.node_address,
                                        primary=primary.primary_address,
                                        epoch=primary.epoch,
                                        reason=self._last_synchronization_failure_reason,
                                        local_ready=False,
                                    )
                                    connected = True
                                    continue
                                raise RuntimeError(
                                    "Full synchronization did not complete successfully."
                                )
                    connected = True
            except Exception as error:
                print(f"[Judge] Booting... Controller not ready: {error}")
                time.sleep(2)

    def _registration_request(self) -> pb2.RegisterRequest:
        return pb2.RegisterRequest(
            address=self.node_address,
            role=self.replica_role,
            epoch=self.current_epoch,
            # For a primary this is promotion readiness.  For a backup it is
            # the local proof that synchronization completed for current_epoch.
            # A restarted backup deliberately starts with this false even when
            # its persisted epoch is current.
            promotion_ready=getattr(
                self, "_storage_metrics_ready", self.promotion_ready
            ),
            synchronous_backup_address=self.synchronous_backup_address,
        )

    def reregister_with_controller(self) -> bool:
        """Refresh membership and reconcile controller state after its restart."""
        try:
            with grpc.insecure_channel(CONTROLLER_ADDRESS) as channel:
                response = pb2_grpc.ClusterControllerStub(channel).RegisterNode(
                    self._registration_request(), timeout=2.0
                )
            return response.success
        except grpc.RpcError:
            return False

    # Auction mutation and read RPCs

    @observe_rpc("storage", "ApplyAuctionMutation", _classify_success)
    def ApplyAuctionMutation(
        self,
        request: pb2.AuctionMutationRequest,
        context,
    ) -> pb2.AuctionMutationResponse:
        """Validate, construct, and synchronously commit one auction mutation.

        Idempotency is checked before domain validation so a retry replays the
        original committed response even when later state would reject the same
        logical operation. Candidate construction is side-effect free; only the
        commit coordinator may publish the candidate to replica state.
        """
        operation = self._mutation_operation(request)
        with replication_operation(operation), self.state_lock:
            request_error = self._mutation_request_error(request)
            if request_error is not None:
                return request_error

            auction_id = request.auction.auction_id
            existing_auction = self.auction_store.get(auction_id)
            mutation_type = self._effective_mutation_type(request, existing_auction)
            request_fingerprint = self._request_fingerprint(request, mutation_type)
            idempotency_response = self._check_idempotency(
                request.request_id,
                request_fingerprint,
                existing_auction,
            )
            if idempotency_response:
                return idempotency_response

            candidate_auction, candidate_error = self.auction_domain.build_candidate(
                request,
                mutation_type,
                existing_auction,
            )
            if candidate_error is not None:
                return candidate_error

            response = pb2.AuctionMutationResponse(
                success=True,
                current_version=candidate_auction.version,
                message="Vault updated.",
                auction_id=candidate_auction.auction_id,
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
                candidate_auction,
                idempotency_record,
                response,
                previous_version,
            )

    @staticmethod
    def _mutation_operation(request: pb2.AuctionMutationRequest) -> str:
        operations = {
            pb2.AUCTION_MUTATION_TYPE_CREATE: "CreateAuction",
            pb2.AUCTION_MUTATION_TYPE_PLACE_BID: "PlaceBid",
            pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID: "WithdrawBid",
            pb2.AUCTION_MUTATION_TYPE_REVEAL: "RevealAuction",
        }
        if request.mutation_type in operations:
            return operations[request.mutation_type]
        if request.bidder_id or request.auction.bids:
            return "PlaceBid"
        return "CreateAuction"

    def _mutation_request_error(
        self,
        request: pb2.AuctionMutationRequest,
    ) -> pb2.AuctionMutationResponse | None:
        """Return an authority or epoch error before inspecting auction state."""
        if self.replica_role != "primary":
            return self._mutation_error(
                "Auction mutations require the primary replica."
            )
        if not self.promotion_ready:
            return self._mutation_error(
                "Primary promotion is not ready for mutations."
            )
        if request.epoch != self.current_epoch:
            return self._mutation_error(
                (
                    f"Mutation epoch {request.epoch} does not match "
                    f"primary epoch {self.current_epoch}."
                ),
                reason=pb2.MUTATION_FAILURE_REASON_STALE_EPOCH,
            )
        if request.request_id and request.request_id in self.aborted_mutations:
            return self._mutation_error(
                "Request id has been aborted.",
                reason=pb2.MUTATION_FAILURE_REASON_IDEMPOTENCY_CONFLICT,
            )
        return None

    def _mutation_error(
        self,
        message: str,
        *,
        reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
        current_version: int = 0,
    ) -> pb2.AuctionMutationResponse:
        return pb2.AuctionMutationResponse(
            success=False,
            current_version=current_version,
            failure_reason=reason,
            message=message,
        )

    def _build_auction_result(self, auction: pb2.Auction) -> pb2.AuctionResult:
        """Retain the established internal result helper as a domain delegate."""
        return self.auction_domain.build_result(auction)

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
                "auction_id": auction.auction_id,
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

    def _find_overdue_open_auction_ids(self, now_seconds: float) -> list[str]:
        """Return a stable snapshot of open auctions whose deadline has passed."""
        overdue_ids = []
        for auction_id, auction in self.auction_store.items():
            if auction.state != pb2.AUCTION_STATE_OPEN or not auction.HasField(
                "ends_at"
            ):
                continue
            deadline = auction.ends_at.seconds + (
                auction.ends_at.nanos / 1_000_000_000
            )
            if now_seconds >= deadline:
                overdue_ids.append(auction_id)
        return sorted(overdue_ids)

    def _overdue_reveal_request_id(self, auction_id: str) -> str:
        return f"overdue-reveal:{auction_id}"

    # Keep the shorter name available to callers that treat overdue actions
    # generically rather than as reveal mutations.
    def _overdue_request_id(self, auction_id: str) -> str:
        return self._overdue_reveal_request_id(auction_id)

    def _finalize_overdue_auction(
        self,
        auction_id: str,
        epoch: int,
        now_seconds: float | None = None,
    ) -> pb2.AuctionMutationResponse | None:
        """Reveal one overdue auction through the replicated mutation path."""
        with self.state_lock:
            if (
                self.replica_role != "primary"
                or not self.promotion_ready
                or epoch != self.current_epoch
                or not self.synchronous_backup_address
            ):
                return None
            auction = self.auction_store.get(auction_id)
            if (
                auction is None
                or auction.state != pb2.AUCTION_STATE_OPEN
                or not auction.HasField("ends_at")
            ):
                return None
            checked_at = time.time() if now_seconds is None else now_seconds
            deadline = auction.ends_at.seconds + (
                auction.ends_at.nanos / 1_000_000_000
            )
            if checked_at < deadline:
                return None
            return self.ApplyAuctionMutation(
                pb2.AuctionMutationRequest(
                    mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
                    auction=pb2.Auction(
                        auction_id=auction_id,
                        seller_id=auction.seller_id,
                    ),
                    expected_version=auction.version,
                    request_id=self._overdue_request_id(auction_id),
                    epoch=epoch,
                ),
                None,
            )

    def _reconcile_overdue_auctions(
        self,
        epoch: int | None = None,
        now_seconds: float | None = None,
    ) -> list[pb2.AuctionMutationResponse]:
        """Attempt every overdue reveal safe in the current primary epoch."""
        with self.state_lock:
            reconciliation_epoch = self.current_epoch if epoch is None else epoch
            if (
                self.replica_role != "primary"
                or not self.promotion_ready
                or reconciliation_epoch != self.current_epoch
                or not self.synchronous_backup_address
            ):
                return []
            scanned_at = time.time() if now_seconds is None else now_seconds
            auction_ids = self._find_overdue_open_auction_ids(scanned_at)
        results = []
        for auction_id in auction_ids:
            response = self._finalize_overdue_auction(
                auction_id,
                epoch=reconciliation_epoch,
                now_seconds=scanned_at,
            )
            if response is not None:
                results.append(response)
        return results

    @observe_rpc("storage", "GetAuction", _classify_ok)
    def GetAuction(
        self,
        request: pb2.StorageGetAuctionRequest,
        context,
    ) -> pb2.GetStoredAuctionResponse:
        with self.state_lock:
            if self.replica_role != "primary":
                return pb2.GetStoredAuctionResponse(
                    ok=False,
                    message="Authoritative auction reads require the primary replica.",
                    failure_reason=pb2.READ_FAILURE_REASON_NOT_PRIMARY,
                )
            if not self.promotion_ready:
                return pb2.GetStoredAuctionResponse(
                    ok=False,
                    message="Primary promotion is not ready for authoritative reads.",
                    failure_reason=pb2.READ_FAILURE_REASON_PROMOTION_NOT_READY,
                )
            if request.epoch != self.current_epoch:
                return pb2.GetStoredAuctionResponse(
                    ok=False,
                    message="Authoritative read epoch is stale.",
                    failure_reason=pb2.READ_FAILURE_REASON_STALE_EPOCH,
                )
            auction = self.auction_store.get(request.auction_id)
            if auction:
                return pb2.GetStoredAuctionResponse(ok=True, auction=auction)
            return pb2.GetStoredAuctionResponse(
                ok=False,
                message="Auction not found",
                failure_reason=pb2.READ_FAILURE_REASON_NOT_FOUND,
            )

    @observe_rpc("storage", "SearchAuctions", _classify_ok)
    def SearchAuctions(
        self,
        request: pb2.SearchAuctionsRequest,
        context,
    ) -> pb2.GetStoredAuctionsResponse:
        with self.state_lock:
            query = request.query.strip().lower()
            category = request.category.strip().lower()
            auctions = list(self.auction_store.values())
            matches = [
                auction
                for auction in auctions
                if (
                    not query
                    or query in auction.auction_id.lower()
                    or query in auction.title.lower()
                    or query in auction.description.lower()
                )
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
        """Stage a candidate on the assigned backup before deciding to commit."""
        if self.replica_role != "primary" or not self.synchronous_backup_address:
            return False

        request = pb2.PrepareMutationRequest(
            request_id=request_id,
            candidate_auction=candidate_auction,
            idempotency_record=idempotency_record,
            primary_id=self.node_address,
            epoch=self.current_epoch,
        )
        response = self.replication_client.prepare(
            self.synchronous_backup_address,
            request,
        )
        if response is None:
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
        """Durably publish the primary decision before asking the backup to commit.

        The pending decision is persisted with committed state so a lost backup
        acknowledgement can be completed after a primary restart.
        """
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
        """Commit a prepared backup and durably clear its pending decision."""
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
            response = self.replication_client.commit(
                decision.backup_address,
                commit_request,
            )
            if response is None:
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
        response = self.replication_client.abort(
            self.synchronous_backup_address,
            request,
        )
        return response is not None and response.success

    def _coordinate_synchronous_commit(
        self,
        request_id: str,
        candidate_auction: pb2.Auction,
        idempotency_record: pb2.IdempotencyRecord,
        success_response: pb2.AuctionMutationResponse,
        previous_version: int,
    ) -> pb2.AuctionMutationResponse:
        """Run prepare, durable primary decision, and backup acknowledgement.

        Failure before the durable decision is reported as replication failure.
        Failure after it is reported as acknowledgement pending because the
        candidate is already committed and must be recovered idempotently.
        """
        if not self._prepare_on_synchronous_backup(
            request_id,
            candidate_auction,
            idempotency_record,
        ):
            record_commit_outcome("aborted")
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
            record_commit_outcome("aborted")
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
            record_commit_outcome("unknown")
            return pb2.AuctionMutationResponse(
                success=False,
                current_version=candidate_auction.version,
                failure_reason=pb2.MUTATION_FAILURE_REASON_ACKNOWLEDGEMENT_PENDING,
                auction_id=candidate_auction.auction_id,
                message="Commit is durable but backup acknowledgement is pending.",
            )
        record_commit_outcome("committed")
        return success_response

    # Synchronous replication RPCs and commit-protocol helpers

    @observe_rpc("storage", "PrepareAuctionMutation", _classify_success)
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
            state_error = self.auction_domain.committed_state_error(candidate)
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

    @observe_rpc("storage", "CommitPreparedMutation", _classify_success)
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

    @observe_rpc("storage", "AbortPreparedMutation", _classify_success)
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
            if prepared is None:
                return pb2.MutationDecisionResponse(
                    success=True,
                    committed_version=committed_version,
                    message="No prepared mutation to abort.",
                )
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
            del self.prepared_mutations[request_id]
            self.aborted_mutations[request_id] = tombstone
            try:
                self._persist_state_to_disk()
            except Exception as error:
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

    # Promotion and failover RPCs

    @observe_rpc("storage", "BeginPrimaryPromotion", _classify_accepted)
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
                if self.replica_role != "primary":
                    return pb2.BeginPrimaryPromotionResponse(
                        accepted=False,
                        epoch=self.current_epoch,
                        message=(
                            "Same-epoch promotion is valid only for the existing "
                            "primary promotion candidate."
                        ),
                    )
                return pb2.BeginPrimaryPromotionResponse(
                    accepted=True,
                    epoch=self.current_epoch,
                    message="Primary promotion already begun for this epoch.",
                )

            previous_role = self.replica_role
            previous_epoch = self.current_epoch
            previous_ready = self.promotion_ready
            previous_metrics_ready = getattr(
                self, "_storage_metrics_ready", previous_ready
            )
            previous_prepared_mutations = self.prepared_mutations
            self.replica_role = "primary"
            self.current_epoch = request.epoch
            self.promotion_ready = False
            self._storage_metrics_ready = False
            self.prepared_mutations = {}
            try:
                self._persist_state_to_disk()
            except Exception as error:
                self.replica_role = previous_role
                self.current_epoch = previous_epoch
                self.promotion_ready = previous_ready
                self._storage_metrics_ready = previous_metrics_ready
                self.prepared_mutations = previous_prepared_mutations
                self._refresh_storage_state_metrics_locked()
                return pb2.BeginPrimaryPromotionResponse(
                    accepted=False,
                    epoch=self.current_epoch,
                    message=f"Could not persist promotion epoch: {error}",
                )
            self._refresh_storage_state_metrics_locked()
            return pb2.BeginPrimaryPromotionResponse(
                accepted=True,
                epoch=self.current_epoch,
                message="Primary promotion begun.",
            )

    @observe_rpc("storage", "ConfirmPromotionState", _classify_confirmed)
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
                state_error = self.auction_domain.committed_state_error(auction)
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

    @observe_rpc("storage", "CompletePrimaryPromotion", _classify_success)
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

            previous_backup = self.synchronous_backup_address
            previous_promotion_ready = self.promotion_ready
            previous_storage_metrics_ready = self._storage_metrics_ready
            self.synchronous_backup_address = backup_address
            self.promotion_ready = True
            self._storage_metrics_ready = True
            try:
                self._persist_state_to_disk()
            except Exception as error:
                self.synchronous_backup_address = previous_backup
                self.promotion_ready = previous_promotion_ready
                self._storage_metrics_ready = previous_storage_metrics_ready
                self._refresh_storage_state_metrics_locked()
                return pb2.CompletePrimaryPromotionResponse(
                    success=False,
                    epoch=self.current_epoch,
                    message=f"Could not persist promotion completion: {error}",
                )
            self._refresh_storage_state_metrics_locked()
            self._reconcile_overdue_auctions(epoch=request.epoch)
            return pb2.CompletePrimaryPromotionResponse(
                success=True,
                epoch=self.current_epoch,
                message="Primary promotion complete.",
            )

    # Full-state synchronization RPCs and replacement helpers

    @observe_rpc("storage", "SyncFullState", _classify_ok)
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

    @observe_rpc("storage", "SynchronizeFromPrimary", _classify_success)
    def SynchronizeFromPrimary(self, request, context):
        with self.state_lock:
            _log_event(
                logging.INFO,
                "storage_synchronization_received",
                target_replica=self.node_address,
                source_primary=request.primary_address.strip() or "missing",
                epoch=request.epoch,
                recovery_generation="unavailable",
                local_role=self.replica_role,
                local_ready=getattr(
                    self, "_storage_metrics_ready", self.promotion_ready
                ),
            )
            if self.replica_role != "backup":
                _log_event(
                    logging.WARNING,
                    "storage_synchronization_finished",
                    target_replica=self.node_address,
                    source_primary=request.primary_address.strip() or "missing",
                    epoch=request.epoch,
                    recovery_generation="unavailable",
                    success=False,
                    failed_stage="state_fetch",
                    reason="local_role_not_backup",
                )
                return pb2.SynchronizeFromPrimaryResponse(
                    success=False,
                    epoch=self.current_epoch,
                    message="Only a backup can synchronize from a primary.",
                )
            if not request.primary_address.strip() or request.epoch <= 0:
                _log_event(
                    logging.WARNING,
                    "storage_synchronization_finished",
                    target_replica=self.node_address,
                    source_primary=request.primary_address.strip() or "missing",
                    epoch=request.epoch,
                    recovery_generation="unavailable",
                    success=False,
                    failed_stage="state_fetch",
                    reason="invalid_request",
                )
                return pb2.SynchronizeFromPrimaryResponse(
                    success=False,
                    epoch=self.current_epoch,
                    message="Primary address and positive epoch are required.",
                )
            if request.epoch < self.current_epoch:
                _log_event(
                    logging.WARNING,
                    "storage_synchronization_finished",
                    target_replica=self.node_address,
                    source_primary=request.primary_address,
                    epoch=request.epoch,
                    recovery_generation="unavailable",
                    success=False,
                    failed_stage="state_fetch",
                    reason="stale_epoch",
                )
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
        """Replace local state, then report matching-epoch readiness."""
        self._last_synchronization_failure_retryable = True
        self._last_synchronization_failure_reason = ""
        common = dict(
            source_primary=primary_address,
            target_replica=self.node_address,
            epoch=epoch,
            generation="unavailable",
            recovery_generation="unavailable",
        )
        failed_stage = "state_fetch"
        reason = "completed"
        try:
            _log_event(logging.INFO, "storage_state_fetch_started", **common)
            response = self.synchronization_client.fetch_full_state(
                primary_address,
                self.node_address,
                epoch,
            )
            if not response.ok:
                failed_stage = "state_fetch"
                reason = response.message or "source_rejected"
                _log_event(
                    logging.WARNING,
                    "storage_state_fetch_rejected",
                    **common,
                    response_reason=reason,
                    auction_count=0,
                )
                return False
            _log_event(
                logging.INFO,
                "storage_state_fetch_succeeded",
                **common,
                response_reason=response.message or "accepted",
                auction_count=len(response.auctions),
            )
            failed_stage = "state_install"
            _log_event(logging.INFO, "storage_state_install_started", **common)
            if not self._replace_with_full_state(response, epoch=epoch):
                failed_stage = "state_install"
                reason = "validation_or_persistence_failed"
                _log_event(
                    logging.WARNING,
                    "storage_state_install_failed",
                    **common,
                    reason=reason,
                )
                return False
            _log_event(
                logging.INFO,
                "storage_state_install_succeeded",
                **common,
                reason="installed",
            )
            failed_stage = "completion_report"
            _log_event(logging.INFO, "storage_completion_report_started", **common)
            completion = self._report_synchronization_complete(
                primary_address,
                epoch=epoch,
            )
            if not completion.success:
                failed_stage = "completion_report"
                terminal_standby = (
                    completion.message
                    == "No controller-owned synchronization is in progress."
                )
                reason = (
                    "unsolicited_while_ready"
                    if terminal_standby
                    else completion.message or "controller_rejected"
                )
                self._last_synchronization_failure_retryable = not terminal_standby
                self._last_synchronization_failure_reason = reason
                _log_event(
                    logging.WARNING,
                    "storage_completion_report_rejected",
                    **common,
                    response_reason=reason,
                )
                return False
            _log_event(
                logging.INFO,
                "storage_completion_report_succeeded",
                **common,
                response_reason="accepted",
            )
            failed_stage = "none"
            with self.state_lock:
                if self.replica_role == "backup" and self.current_epoch == epoch:
                    self._storage_metrics_ready = True
                    self._refresh_storage_state_metrics_locked()
            return True
        except grpc.RpcError as error:
            reason = type(error).__name__
            self._last_synchronization_failure_retryable = True
            self._last_synchronization_failure_reason = reason
            try:
                rpc_status = error.code()
            except Exception:
                rpc_status = None
            outcome = (
                f"storage_{failed_stage}_timeout"
                if rpc_status == grpc.StatusCode.DEADLINE_EXCEEDED
                else f"storage_{failed_stage}_failed"
            )
            _log_event(
                logging.WARNING,
                outcome,
                **common,
                exception_type=reason,
            )
            return False
        except Exception as error:
            reason = type(error).__name__
            self._last_synchronization_failure_retryable = True
            self._last_synchronization_failure_reason = reason
            _log_event(
                logging.ERROR,
                f"storage_{failed_stage}_failed",
                **common,
                exception_type=reason,
                reason=reason,
            )
            LOGGER.exception(
                "Unexpected full synchronization failure target_replica=%s",
                self.node_address,
            )
            return False
        finally:
            _log_event(
                logging.INFO if failed_stage == "none" else logging.WARNING,
                "storage_synchronization_finished",
                **common,
                success=failed_stage == "none",
                failed_stage=failed_stage,
                reason=reason,
            )

    def _configure_primary_backup(self, primary_address: str, epoch: int) -> bool:
        """Tell the synchronized primary to start using this replica."""
        try:
            with grpc.insecure_channel(primary_address) as channel:
                response = pb2_grpc.StorageReplicaServiceStub(
                    channel
                ).CompletePrimaryPromotion(
                    pb2.CompletePrimaryPromotionRequest(
                        epoch=epoch,
                        backup_address=self.node_address,
                    ),
                    timeout=5.0,
                )
            return response.success and response.epoch == epoch
        except grpc.RpcError:
            return False

    def _replace_with_full_state(self, response: pb2.StateResponse, epoch=None) -> bool:
        """Validate and atomically persist a full-state replacement.

        Existing in-memory state is restored if persistence fails. Promotion
        readiness remains false until the controller completes the barrier.
        """
        if not response.ok:
            return False

        replacement_auctions: dict[str, pb2.Auction] = {}
        for auction in response.auctions:
            if (
                not auction.auction_id.strip()
                or auction.auction_id in replacement_auctions
                or self.auction_domain.committed_state_error(auction)
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
            if epoch is not None and (
                self.replica_role != "backup" or epoch < self.current_epoch
            ):
                return False
            previous_collections = (
                self.auction_store,
                self.idempotency_records,
                self.prepared_mutations,
                self.aborted_mutations,
                self.pending_backup_commits,
                self.current_epoch,
                self.promotion_ready,
                getattr(self, "_storage_metrics_ready", self.promotion_ready),
            )
            self.auction_store = replacement_auctions
            self.idempotency_records = replacement_records
            self.prepared_mutations = {}
            self.aborted_mutations = {}
            self.pending_backup_commits = {}
            if epoch is not None:
                self.current_epoch = epoch
            self.promotion_ready = False
            self._storage_metrics_ready = False
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
                    self._storage_metrics_ready,
                ) = previous_collections
                self._refresh_storage_state_metrics_locked()
                print(f"[Judge] Could not persist synchronized state: {error}")
                return False
            self._refresh_storage_state_metrics_locked()
        return True

    def _report_synchronization_complete(
        self,
        primary_address: str,
        epoch: int,
    ) -> pb2.SynchronizationCompleteResponse:
        return self.synchronization_client.report_complete(
            self.node_address,
            primary_address,
            epoch,
            CONTROLLER_ADDRESS,
        )

    # Health and local snapshot helpers

    @observe_rpc("storage", "Heartbeat", _classify_alive)
    def Heartbeat(self, request, context):
        with self.state_lock:
            return pb2.HealthCheckResponse(
                alive=True,
                role=self.replica_role,
                message="Alive",
                epoch=self.current_epoch,
                # Backup readiness is reported on the existing readiness bit so
                # the controller can revoke READY when local protection is lost.
                promotion_ready=getattr(
                    self, "_storage_metrics_ready", self.promotion_ready
                ),
                synchronous_backup_address=self.synchronous_backup_address,
            )

    def _load_state_from_disk(self) -> None:
        """Validate and apply the local snapshot before controller registration."""
        try:
            repository = getattr(self, "snapshot_repository", None)
            if repository is None:
                repository = StorageSnapshotRepository(self.state_file_path)
                self.snapshot_repository = repository
            snapshot = repository.load()
            if snapshot is None:
                return
            auction_store = {}
            for auction in snapshot.auctions:
                state_error = self.auction_domain.committed_state_error(auction)
                if state_error:
                    raise ValueError(
                        f"invalid committed auction {auction.auction_id!r}: "
                        f"{state_error}"
                    )
                if auction.auction_id in auction_store:
                    raise ValueError(
                        f"duplicate committed auction id {auction.auction_id!r}"
                    )
                auction_store[auction.auction_id] = auction

            self.auction_store = auction_store
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
            if self.promotion_ready and self.synchronous_backup_address:
                self.replica_role = "primary"
                self._storage_metrics_ready = True
        except Exception as e:
            raise RuntimeError(
                f"Could not load local state snapshot {self.state_file_path!r}: {e}"
            ) from e

    def _persist_state_to_disk(self) -> None:
        """Build a snapshot of service-owned state and delegate its storage."""
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
        repository = getattr(self, "snapshot_repository", None)
        if repository is None:
            repository = StorageSnapshotRepository(self.state_file_path)
            self.snapshot_repository = repository
        repository.save(snapshot)
