from dataclasses import dataclass
import logging
import os
import random
import time

from uuid import UUID, uuid4, uuid5  # uuid4 retained as an import compatibility shim

import grpc

from blindsided.common.config import CONTROLLER_PORT
from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc
from blindsided.observability.instrumentation import (
    observe_mutation,
    observe_rpc,
    record_concurrency_retry,
    record_idempotency_decision,
    set_mutation_outcome,
)


controller_host = os.getenv("CONTROLLER_HOST", "localhost")
CONTROLLER_ADDRESS = f"{controller_host}:{CONTROLLER_PORT}"
CREATE_AUCTION_NAMESPACE = UUID("9c056d8e-23e6-5b5f-9f53-ff0c44f30ae1")
logger = logging.getLogger(__name__)


def _classify_ok(response) -> str:
    return "success" if response.ok else "failure"


def _classify_success(response) -> str:
    return "success" if response.success else "failure"


def _classify_ok_mutation(response) -> str:
    if response.outcome_unknown:
        return "unknown"
    if response.ok:
        return "committed"
    if response.retryable:
        return "unavailable"
    return "rejected"


def _classify_success_mutation(response) -> str:
    if response.outcome_unknown:
        return "unknown"
    if response.success:
        return "committed"
    if response.retryable:
        return "unavailable"
    return "rejected"


def _record_idempotency_result(operation: str, response) -> None:
    if (
        response.failure_reason
        == pb2.MUTATION_FAILURE_REASON_IDEMPOTENCY_CONFLICT
    ):
        outcome = "mismatch"
    elif response.replayed:
        outcome = "replayed"
    else:
        outcome = "new"
    record_idempotency_decision(operation, outcome)


def _auction_id_for_request(request_id: str) -> str:
    """Map an idempotency key to the same UUID on every service node."""
    return str(uuid5(CREATE_AUCTION_NAMESPACE, request_id))


@dataclass(frozen=True)
class PrimaryAssignment:
    address: str
    epoch: int


class AuctionService(pb2_grpc.AuctionServiceServicer):
    """API layer that hides sealed bid details until an auction is revealed."""

    # Class defaults keep lightweight test subclasses usable when they bypass
    # normal startup. Production instances validate and cache environment
    # overrides once in __init__.
    mutation_retry_limit = 5
    failover_recovery_window = 10.0
    controller_timeout = 2.0
    cluster_info_timeout = 3.0
    mutation_timeout = 5.0
    read_timeout = 5.0
    search_timeout = 5.0

    def __init__(self) -> None:
        self.mutation_retry_limit = self._positive_int_env("MUTATION_RETRY_LIMIT", 5)
        self.failover_recovery_window = self._nonnegative_float_env(
            "FAILOVER_RECOVERY_WINDOW_SECONDS", 10.0
        )
        self.controller_timeout = self._positive_float_env("CONTROLLER_RPC_TIMEOUT_SECONDS", 2.0)
        self.cluster_info_timeout = self._positive_float_env(
            "CLUSTER_INFO_RPC_TIMEOUT_SECONDS", 3.0
        )
        self.mutation_timeout = self._positive_float_env("MUTATION_RPC_TIMEOUT_SECONDS", 5.0)
        self.read_timeout = self._positive_float_env("READ_RPC_TIMEOUT_SECONDS", 5.0)
        self.search_timeout = self._positive_float_env("SEARCH_RPC_TIMEOUT_SECONDS", 5.0)

    # Configuration and deadline helpers

    @staticmethod
    def _positive_int_env(name: str, default: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except ValueError as error:
            raise ValueError(f"{name} must be an integer") from error
        if value < 1:
            raise ValueError(f"{name} must be at least 1")
        return value

    @staticmethod
    def _positive_float_env(name: str, default: float) -> float:
        try:
            value = float(os.getenv(name, str(default)))
        except ValueError as error:
            raise ValueError(f"{name} must be a number") from error
        if value <= 0:
            raise ValueError(f"{name} must be positive")
        return value

    @staticmethod
    def _nonnegative_float_env(name: str, default: float) -> float:
        try:
            value = float(os.getenv(name, str(default)))
        except ValueError as error:
            raise ValueError(f"{name} must be a number") from error
        if value < 0:
            raise ValueError(f"{name} must be non-negative")
        return value

    def _rpc_timeout(self, configured_timeout: float, context) -> float | None:
        """Return a bounded RPC timeout, or None after client expiration."""
        remaining = None
        if context is not None and hasattr(context, "time_remaining"):
            remaining = context.time_remaining()
        if remaining is not None:
            if remaining <= 0:
                return None
            return min(configured_timeout, remaining)
        return configured_timeout

    # Controller and storage client construction

    def _get_primary_assignment(
        self,
        timeout_seconds: float = 2.0,
    ) -> PrimaryAssignment | None:
        """Ask the controller which storage replica owns authoritative writes."""
        try:
            with grpc.insecure_channel(CONTROLLER_ADDRESS) as ch:
                stub = pb2_grpc.ClusterControllerStub(ch)
                resp = stub.GetPrimary(
                    pb2.GetPrimaryRequest(),
                    timeout=timeout_seconds,
                )
                if resp.success:
                    return PrimaryAssignment(
                        address=resp.primary_address,
                        epoch=resp.epoch,
                    )
        except grpc.RpcError as error:
            logger.warning("Controller GetPrimary failed: status=%s", error.code())
        return None

    def _get_storage_node_addresses(self, timeout_seconds: float = 3.0) -> list[str]:
        """Return storage replicas that can serve stale-tolerant discovery reads."""
        try:
            with grpc.insecure_channel(CONTROLLER_ADDRESS) as ch:
                stub = pb2_grpc.ClusterControllerStub(ch)
                resp = stub.GetClusterInfo(pb2.ClusterInfoRequest(), timeout=timeout_seconds)
                if resp.success:
                    return list(resp.node_addresses)
        except grpc.RpcError as error:
            logger.warning("Controller GetClusterInfo failed: status=%s", error.code())
        return []

    def _create_storage_stub(self, address: str):
        """Create a storage replica client and its owning channel."""
        channel = grpc.insecure_channel(address)
        return pb2_grpc.StorageReplicaServiceStub(channel), channel

    def _mutation_retry_limit(self) -> int:
        """Compatibility accessor for tests that override retry configuration."""
        return self.mutation_retry_limit

    def _failover_recovery_window(self) -> float:
        """Compatibility accessor for tests that override recovery timing."""
        return self.failover_recovery_window

    # Primary recovery and retry classification

    def _wait_for_ready_primary(self, context) -> PrimaryAssignment | None:
        """Poll until failover publishes a ready primary or the request expires."""
        recovery_deadline = time.monotonic() + self._failover_recovery_window()
        while True:
            if hasattr(context, "is_active") and not context.is_active():
                return None
            client_remaining = (
                context.time_remaining()
                if hasattr(context, "time_remaining")
                else None
            )
            if client_remaining is not None and client_remaining <= 0:
                return None

            remaining = recovery_deadline - time.monotonic()
            if remaining <= 0:
                return None
            poll_timeout = min(2.0, remaining)
            if client_remaining is not None:
                poll_timeout = min(poll_timeout, client_remaining)
            assignment = self._get_primary_assignment(
                timeout_seconds=poll_timeout,
            )
            if assignment is not None:
                return assignment

            delay = min(random.uniform(0.25, 0.5), remaining)
            if client_remaining is not None:
                delay = min(delay, client_remaining)
            if delay <= 0:
                return None
            time.sleep(delay)

    def _resolve_ready_primary(self, context, *, allow_recovery: bool) -> PrimaryAssignment | None:
        """Resolve once, optionally waiting through the configured failover window."""
        timeout = self._rpc_timeout(self.controller_timeout, context)
        if timeout is None:
            return None
        assignment = self._get_primary_assignment(timeout_seconds=timeout)
        if assignment is not None or not allow_recovery:
            return assignment
        return self._wait_for_ready_primary(context)

    def _is_version_conflict(self, response: pb2.AuctionMutationResponse) -> bool:
        return (
            response.failure_reason
            == pb2.MUTATION_FAILURE_REASON_CONCURRENCY_CONFLICT
        )

    def _is_failover_rpc_error(self, error: grpc.RpcError) -> bool:
        return error.code() in (
            grpc.StatusCode.UNAVAILABLE,
            grpc.StatusCode.DEADLINE_EXCEEDED,
        )

    def _unknown_outcome_fields(self, request_id: str) -> dict:
        """Describe a sent mutation whose acknowledgement may have been lost."""
        return {
            "retryable": True,
            "outcome_unknown": True,
            "request_id": request_id,
            "message": (
                "UNAVAILABLE: Mutation outcome is unknown after failover recovery expired; "
                "retry with the same request_id."
            ),
        }

    def _acknowledgement_pending_fields(
        self, response: pb2.AuctionMutationResponse, request_id: str
    ) -> dict | None:
        if (
            response.failure_reason
            != pb2.MUTATION_FAILURE_REASON_ACKNOWLEDGEMENT_PENDING
        ):
            return None
        return {
            "retryable": True,
            "outcome_unknown": True,
            "request_id": request_id,
            "message": (
                f"{response.message} Retry with the same request_id ({request_id})."
            ),
        }

    @staticmethod
    def _is_read_authority_failure(response: pb2.GetStoredAuctionResponse) -> bool:
        """Identify read failures that require fresh controller discovery."""
        return response.failure_reason in (
            pb2.READ_FAILURE_REASON_STALE_EPOCH,
            pb2.READ_FAILURE_REASON_NOT_PRIMARY,
            pb2.READ_FAILURE_REASON_PROMOTION_NOT_READY,
        )

    # Mutation RPC handlers. Domain validation and idempotency remain in storage.

    @observe_rpc("auction_service", "CreateAuction", _classify_ok)
    @observe_mutation("CreateAuction", _classify_ok_mutation)
    def CreateAuction(
        self,
        request: pb2.CreateAuctionRequest,
        context,
    ) -> pb2.CreateAuctionResponse:
        """Persist a new auction through the current primary replica."""
        if not request.request_id.strip():
            return pb2.CreateAuctionResponse(
                ok=False,
                message="request_id is required for idempotency.",
            )
        request_id = request.request_id
        auction = pb2.Auction(
            auction_id=_auction_id_for_request(request_id),
            seller_id=request.seller_id,
            title=request.title,
            category=request.category,
            description=request.description,
            reserve_price=request.reserve_price,
            state=pb2.AUCTION_STATE_OPEN,
        )
        if request.HasField("ends_at"):
            auction.ends_at.CopyFrom(request.ends_at)

        recovered_assignment = None
        mutation_retry_limit = self.mutation_retry_limit
        for attempt in range(mutation_retry_limit):
            assignment = recovered_assignment or self._resolve_ready_primary(
                context, allow_recovery=True
            )
            recovered_assignment = None
            if not assignment:
                return pb2.CreateAuctionResponse(
                    ok=False,
                    retryable=True,
                    request_id=request_id,
                    message="No ready primary is available.",
                )
            rpc_timeout = self._rpc_timeout(self.mutation_timeout, context)
            if rpc_timeout is None:
                return pb2.CreateAuctionResponse(
                    ok=False,
                    retryable=True,
                    request_id=request_id,
                    message="Request deadline exceeded",
                )
            try:
                stub, channel = self._create_storage_stub(assignment.address)
                with channel:
                    response = stub.ApplyAuctionMutation(pb2.AuctionMutationRequest(
                        mutation_type=pb2.AUCTION_MUTATION_TYPE_CREATE,
                        auction=auction,
                        request_id=request_id,
                        epoch=assignment.epoch,
                    ), timeout=rpc_timeout)

                    _record_idempotency_result("CreateAuction", response)

                    if response.success:
                        return pb2.CreateAuctionResponse(
                            ok=True,
                            auction_id=response.auction_id or auction.auction_id,
                            message="Auction opened in the Vault.",
                        )
                    pending = self._acknowledgement_pending_fields(response, request_id)
                    if pending is not None:
                        return pb2.CreateAuctionResponse(ok=False, **pending)
                    return pb2.CreateAuctionResponse(ok=False, message=response.message)
            except grpc.RpcError as error:
                if self._is_failover_rpc_error(error):
                    if attempt < mutation_retry_limit - 1:
                        recovered_assignment = self._wait_for_ready_primary(context)
                        if recovered_assignment is not None:
                            continue
                    return pb2.CreateAuctionResponse(
                        ok=False,
                        **self._unknown_outcome_fields(request_id),
                    )
                logger.warning(
                    "Storage mutation failed: operation=create request_id=%s status=%s",
                    request_id,
                    error.code(),
                )
                set_mutation_outcome("failure")
                return pb2.CreateAuctionResponse(ok=False, message="Storage mutation failed.")
        return pb2.CreateAuctionResponse(ok=False, message="Vault write retry failed.")

    @observe_rpc("auction_service", "PlaceBid", _classify_success)
    @observe_mutation("PlaceBid", _classify_success_mutation)
    def PlaceBid(self, request: pb2.BidRequest, context) -> pb2.BidResponse:
        """Retry stale-version bid writes against the latest committed version."""
        if not request.request_id.strip():
            return pb2.BidResponse(
                success=False,
                message="request_id is required for idempotency.",
            )
        mutation_retry_limit = self.mutation_retry_limit
        current_attempt_version = request.expected_version
        request_id = request.request_id
        recovered_assignment = None
        concurrency_retry_count = 0

        for attempt in range(mutation_retry_limit):
            assignment = recovered_assignment or self._resolve_ready_primary(
                context, allow_recovery=True
            )
            recovered_assignment = None
            if not assignment:
                return pb2.BidResponse(
                    success=False,
                    retryable=True,
                    request_id=request_id,
                    message="No ready primary is available.",
                )
            rpc_timeout = self._rpc_timeout(self.mutation_timeout, context)
            if rpc_timeout is None:
                return pb2.BidResponse(
                    success=False,
                    retryable=True,
                    request_id=request_id,
                    message="Request deadline exceeded",
                )

            try:
                stub, channel = self._create_storage_stub(assignment.address)
                with channel:
                    bid_mutation = pb2.Auction(
                        auction_id=request.auction_id,
                        bids={request.bidder_id: pb2.ActiveBid(amount=request.amount)},
                        version=current_attempt_version
                    )

                    response = stub.ApplyAuctionMutation(pb2.AuctionMutationRequest(
                        mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                        auction=bid_mutation,
                        bidder_id=request.bidder_id,
                        expected_version=current_attempt_version,
                        request_id=request_id,
                        epoch=assignment.epoch,
                    ), timeout=rpc_timeout)

                    if response.success:
                        _record_idempotency_result("PlaceBid", response)
                        if concurrency_retry_count:
                            record_concurrency_retry(
                                "PlaceBid", "succeeded_after_retry"
                            )
                        return pb2.BidResponse(success=True, message="Vault Updated.")

                    pending = self._acknowledgement_pending_fields(response, request_id)
                    if pending is not None:
                        _record_idempotency_result("PlaceBid", response)
                        return pb2.BidResponse(success=False, **pending)

                    if (
                        self._is_version_conflict(response)
                        and response.current_version
                        and attempt < mutation_retry_limit - 1
                    ):
                        concurrency_retry_count += 1
                        record_concurrency_retry("PlaceBid", "retried")
                        current_attempt_version = response.current_version
                        logger.info(
                            "Retrying version conflict: operation=place_bid "
                            "auction_id=%s request_id=%s attempt=%s",
                            request.auction_id,
                            request_id,
                            attempt + 1,
                        )
                        time.sleep(min(0.01 * (attempt + 1), 0.05))
                        continue

                    _record_idempotency_result("PlaceBid", response)

                    if self._is_version_conflict(response):
                        set_mutation_outcome("conflict")
                        if attempt == mutation_retry_limit - 1:
                            record_concurrency_retry("PlaceBid", "exhausted")

                    return pb2.BidResponse(success=False, message=response.message)

            except grpc.RpcError as e:
                if self._is_failover_rpc_error(e):
                    if attempt < mutation_retry_limit - 1:
                        recovered_assignment = self._wait_for_ready_primary(context)
                        if recovered_assignment is not None:
                            continue
                    return pb2.BidResponse(
                        success=False,
                        **self._unknown_outcome_fields(request_id),
                    )
                logger.warning(
                    "Storage mutation failed: operation=place_bid "
                    "auction_id=%s request_id=%s status=%s",
                    request.auction_id,
                    request_id,
                    e.code(),
                )
                set_mutation_outcome("failure")
                return pb2.BidResponse(success=False, message="Storage mutation failed.")

        return pb2.BidResponse(
            success=False,
            message="Vault write contention too high.",
        )

    @observe_rpc("auction_service", "WithdrawBid", _classify_success)
    @observe_mutation("WithdrawBid", _classify_success_mutation)
    def WithdrawBid(self, request: pb2.WithdrawBidRequest, context) -> pb2.WithdrawBidResponse:
        """Withdraw the caller's active bid through the current primary replica."""
        if not request.request_id.strip():
            return pb2.WithdrawBidResponse(
                success=False,
                message="request_id is required for idempotency.",
            )
        mutation_retry_limit = self.mutation_retry_limit
        current_attempt_version = request.expected_version
        request_id = request.request_id
        recovered_assignment = None
        concurrency_retry_count = 0

        for attempt in range(mutation_retry_limit):
            assignment = recovered_assignment or self._resolve_ready_primary(
                context, allow_recovery=True
            )
            recovered_assignment = None
            if not assignment:
                return pb2.WithdrawBidResponse(
                    success=False,
                    retryable=True,
                    request_id=request_id,
                    message="No ready primary is available.",
                )
            rpc_timeout = self._rpc_timeout(self.mutation_timeout, context)
            if rpc_timeout is None:
                return pb2.WithdrawBidResponse(
                    success=False,
                    retryable=True,
                    request_id=request_id,
                    message="Request deadline exceeded",
                )

            try:
                stub, channel = self._create_storage_stub(assignment.address)
                with channel:
                    response = stub.ApplyAuctionMutation(pb2.AuctionMutationRequest(
                        mutation_type=pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID,
                        auction=pb2.Auction(auction_id=request.auction_id),
                        bidder_id=request.bidder_id,
                        expected_version=current_attempt_version,
                        request_id=request_id,
                        epoch=assignment.epoch,
                    ), timeout=rpc_timeout)

                    if response.success:
                        _record_idempotency_result("WithdrawBid", response)
                        if concurrency_retry_count:
                            record_concurrency_retry(
                                "WithdrawBid", "succeeded_after_retry"
                            )
                        return pb2.WithdrawBidResponse(
                            success=True,
                            final_version=response.current_version,
                            message="Vault Updated.",
                        )

                    pending = self._acknowledgement_pending_fields(response, request_id)
                    if pending is not None:
                        _record_idempotency_result("WithdrawBid", response)
                        return pb2.WithdrawBidResponse(
                            success=False,
                            final_version=response.current_version,
                            **pending,
                        )

                    if (
                        self._is_version_conflict(response)
                        and response.current_version
                        and attempt < mutation_retry_limit - 1
                    ):
                        concurrency_retry_count += 1
                        record_concurrency_retry("WithdrawBid", "retried")
                        current_attempt_version = response.current_version
                        logger.info(
                            "Retrying version conflict: operation=withdraw_bid "
                            "auction_id=%s request_id=%s attempt=%s",
                            request.auction_id,
                            request_id,
                            attempt + 1,
                        )
                        time.sleep(min(0.01 * (attempt + 1), 0.05))
                        continue

                    _record_idempotency_result("WithdrawBid", response)

                    if self._is_version_conflict(response):
                        set_mutation_outcome("conflict")
                        if attempt == mutation_retry_limit - 1:
                            record_concurrency_retry("WithdrawBid", "exhausted")

                    return pb2.WithdrawBidResponse(
                        success=False,
                        final_version=response.current_version,
                        message=response.message,
                    )

            except grpc.RpcError as e:
                if self._is_failover_rpc_error(e):
                    if attempt < mutation_retry_limit - 1:
                        recovered_assignment = self._wait_for_ready_primary(context)
                        if recovered_assignment is not None:
                            continue
                    return pb2.WithdrawBidResponse(
                        success=False,
                        **self._unknown_outcome_fields(request_id),
                    )
                logger.warning(
                    "Storage mutation failed: operation=withdraw_bid "
                    "auction_id=%s request_id=%s status=%s",
                    request.auction_id,
                    request_id,
                    e.code(),
                )
                set_mutation_outcome("failure")
                return pb2.WithdrawBidResponse(success=False, message="Storage mutation failed.")

        return pb2.WithdrawBidResponse(
            success=False,
            message="Vault write contention too high.",
        )

    @observe_rpc("auction_service", "RevealAuction", _classify_ok)
    @observe_mutation("RevealAuction", _classify_ok_mutation)
    def RevealAuction(self, request: pb2.RevealAuctionRequest, context):
        if not request.request_id.strip():
            return pb2.RevealAuctionResponse(
                ok=False,
                message="request_id is required for idempotency.",
            )
        mutation_retry_limit = self.mutation_retry_limit
        current_attempt_version = request.expected_version
        request_id = request.request_id
        recovered_assignment = None
        concurrency_retry_count = 0

        for attempt in range(mutation_retry_limit):
            assignment = recovered_assignment or self._resolve_ready_primary(
                context, allow_recovery=True
            )
            recovered_assignment = None
            if not assignment:
                return pb2.RevealAuctionResponse(
                    ok=False,
                    retryable=True,
                    request_id=request_id,
                    message="No ready primary is available.",
                )
            rpc_timeout = self._rpc_timeout(self.mutation_timeout, context)
            if rpc_timeout is None:
                return pb2.RevealAuctionResponse(
                    ok=False,
                    retryable=True,
                    request_id=request_id,
                    message="Request deadline exceeded",
                )

            try:
                stub, channel = self._create_storage_stub(assignment.address)
                with channel:
                    reveal_mutation = pb2.Auction(
                        auction_id=request.auction_id,
                        seller_id=request.seller_id,
                        state=pb2.AUCTION_STATE_REVEALED,
                        version=current_attempt_version
                    )

                    response = stub.ApplyAuctionMutation(pb2.AuctionMutationRequest(
                        mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
                        auction=reveal_mutation,
                        expected_version=current_attempt_version,
                        request_id=request_id,
                        epoch=assignment.epoch,
                    ), timeout=rpc_timeout)

                    if (
                        not response.success
                        and self._is_version_conflict(response)
                        and response.current_version
                        and attempt < mutation_retry_limit - 1
                    ):
                        concurrency_retry_count += 1
                        record_concurrency_retry("RevealAuction", "retried")
                        current_attempt_version = response.current_version
                        logger.info(
                            "Retrying version conflict: operation=reveal "
                            "auction_id=%s request_id=%s attempt=%s",
                            request.auction_id,
                            request_id,
                            attempt + 1,
                        )
                        time.sleep(min(0.01 * (attempt + 1), 0.05))
                        continue

                    _record_idempotency_result("RevealAuction", response)

                    if response.success and concurrency_retry_count:
                        record_concurrency_retry(
                            "RevealAuction", "succeeded_after_retry"
                        )
                    elif self._is_version_conflict(response):
                        set_mutation_outcome("conflict")
                        if attempt == mutation_retry_limit - 1:
                            record_concurrency_retry("RevealAuction", "exhausted")

                    pending = self._acknowledgement_pending_fields(response, request_id)
                    if pending is not None:
                        return pb2.RevealAuctionResponse(
                            ok=False,
                            final_version=response.current_version,
                            **pending,
                        )

                    return pb2.RevealAuctionResponse(
                        ok=response.success,
                        final_version=response.current_version,
                        message=response.message,
                    )
            except grpc.RpcError as e:
                if self._is_failover_rpc_error(e):
                    if attempt < mutation_retry_limit - 1:
                        recovered_assignment = self._wait_for_ready_primary(context)
                        if recovered_assignment is not None:
                            continue
                    return pb2.RevealAuctionResponse(
                        ok=False,
                        **self._unknown_outcome_fields(request_id),
                    )
                logger.warning(
                    "Storage mutation failed: operation=reveal "
                    "auction_id=%s request_id=%s status=%s",
                    request.auction_id,
                    request_id,
                    e.code(),
                )
                set_mutation_outcome("failure")
                return pb2.RevealAuctionResponse(ok=False, message="Storage mutation failed.")

        return pb2.RevealAuctionResponse(
            ok=False,
            message="Vault write contention too high.",
        )

    # Public response translation is defined before read handlers so every read
    # follows the same sealed-field boundary.
    def _to_public_auction(self, auction: pb2.Auction) -> pb2.PublicAuction:
        public_auction = pb2.PublicAuction(
            auction_id=auction.auction_id,
            seller_id=auction.seller_id,
            title=auction.title,
            category=auction.category,
            description=auction.description,
            state=auction.state,
            bidder_count=len(auction.bids),
        )
        if auction.HasField("ends_at"):
            public_auction.ends_at.CopyFrom(auction.ends_at)

        if auction.state == pb2.AUCTION_STATE_REVEALED and auction.HasField("result"):
            public_auction.result.CopyFrom(auction.result)

        return public_auction

    def _to_public_auction_update(self, auction: pb2.Auction) -> pb2.AuctionUpdate:
        """Convert private storage state into the live public stream shape."""
        public_auction = self._to_public_auction(auction)
        update = pb2.AuctionUpdate(
            state=public_auction.state,
            message="Auction update.",
            bidder_count=public_auction.bidder_count,
            version=auction.version,
        )
        if public_auction.HasField("result"):
            update.result.CopyFrom(public_auction.result)
        return update

    # Stale-tolerant discovery read; any healthy replica may answer.
    @observe_rpc("auction_service", "SearchAuctions", _classify_ok)
    def SearchAuctions(
        self,
        request: pb2.SearchAuctionsRequest,
        context,
    ) -> pb2.SearchAuctionsResponse:
        """Search available replicas and return only public auction fields."""
        controller_timeout = self._rpc_timeout(self.cluster_info_timeout, context)
        if controller_timeout is None:
            return pb2.SearchAuctionsResponse(ok=False, message="Request deadline exceeded")
        candidates = self._get_storage_node_addresses(timeout_seconds=controller_timeout)
        if not candidates:
            assignment = self._resolve_ready_primary(context, allow_recovery=False)
            if not assignment:
                return pb2.SearchAuctionsResponse(ok=False, message="No Judges active")
            candidates = [assignment.address]
        else:
            random.shuffle(candidates)

        query = pb2.SearchAuctionsRequest(query=request.query, category=request.category)

        for addr in candidates:
            try:
                stub, channel = self._create_storage_stub(addr)
                with channel:
                    timeout = self._rpc_timeout(self.search_timeout, context)
                    if timeout is None:
                        return pb2.SearchAuctionsResponse(
                            ok=False,
                            message="Request deadline exceeded",
                        )
                    response = stub.SearchAuctions(query, timeout=timeout)
                    if not response.ok:
                        continue
                    public_auctions = [self._to_public_auction(a) for a in response.auctions]
                    return pb2.SearchAuctionsResponse(
                        ok=True,
                        auctions=public_auctions,
                        count=len(public_auctions),
                        message="Results from the Vault",
                    )
            except grpc.RpcError:
                continue

        return pb2.SearchAuctionsResponse(ok=False, message="Vault unreachable")

    # Authoritative read RPC handlers. Unlike search, these always use the
    # controller's current primary assignment and epoch.

    @observe_rpc("auction_service", "GetAuction", _classify_ok)
    def GetAuction(self, request: pb2.GetAuctionRequest, context) -> pb2.GetAuctionResponse:
        """Fetch one auction from the primary and hide sealed bids while open."""
        recovery_deadline = time.monotonic() + self.failover_recovery_window
        while True:
            assignment = self._resolve_ready_primary(context, allow_recovery=True)
            if not assignment:
                return pb2.GetAuctionResponse(
                    ok=False,
                    message="Authoritative auction data is unavailable.",
                )
            try:
                stub, channel = self._create_storage_stub(assignment.address)
                with channel:
                    timeout = self._rpc_timeout(self.read_timeout, context)
                    if timeout is None:
                        return pb2.GetAuctionResponse(ok=False, message="Request deadline exceeded")
                    response = stub.GetAuction(pb2.StorageGetAuctionRequest(
                        auction_id=request.auction_id,
                        bidder_id=request.bidder_id,
                        epoch=assignment.epoch,
                    ), timeout=timeout)
                if response.ok:
                    public_auction = self._to_public_auction(response.auction)
                    public_response = pb2.GetAuctionResponse(ok=True, auction=public_auction)
                    if request.bidder_id:
                        own_active_bid = response.auction.bids.get(request.bidder_id)
                        if own_active_bid is not None:
                            public_response.own_active_bid_amount = own_active_bid.amount
                    return public_response
                if response.failure_reason == pb2.READ_FAILURE_REASON_NOT_FOUND:
                    return pb2.GetAuctionResponse(ok=False, message="Auction not found")
                if (
                    not self._is_read_authority_failure(response)
                    or time.monotonic() >= recovery_deadline
                ):
                    return pb2.GetAuctionResponse(
                        ok=False,
                        message="Authoritative auction data is unavailable.",
                    )
                time.sleep(min(0.05, max(0.0, recovery_deadline - time.monotonic())))
            except grpc.RpcError as error:
                if not self._is_failover_rpc_error(error) or time.monotonic() >= recovery_deadline:
                    logger.warning("Storage GetAuction failed: status=%s", error.code())
                    return pb2.GetAuctionResponse(
                        ok=False,
                        message="Authoritative auction data is unavailable.",
                    )
                time.sleep(min(0.05, max(0.0, recovery_deadline - time.monotonic())))

    def WatchAuction(self, request: pb2.AuctionRequest, context):
        """Stream public auction updates, revealing winner and amount only at close."""
        auction_id = request.auction_id
        last_version = -1

        logger.info("Watcher joined: auction_id=%s", auction_id)

        while context.is_active():
            assignment = self._resolve_ready_primary(context, allow_recovery=True)
            if not assignment:
                time.sleep(1)
                continue

            try:
                stub, channel = self._create_storage_stub(assignment.address)
                with channel:
                    timeout = self._rpc_timeout(self.read_timeout, context)
                    if timeout is None:
                        return
                    response = stub.GetAuction(
                        pb2.StorageGetAuctionRequest(
                            auction_id=auction_id,
                            epoch=assignment.epoch,
                        ),
                        timeout=timeout,
                    )

                    if not response.ok and self._is_read_authority_failure(response):
                        time.sleep(0.05)
                        continue

                    if response.ok:
                        auction = response.auction

                        if auction.version > last_version:
                            last_version = auction.version

                            yield self._to_public_auction_update(auction)
                            if auction.state == pb2.AUCTION_STATE_REVEALED:
                                return

                time.sleep(1)

            except grpc.RpcError as error:
                if not self._is_failover_rpc_error(error):
                    logger.warning("Storage WatchAuction read failed: status=%s", error.code())
                time.sleep(2)
