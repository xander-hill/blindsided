import os
import random
import time

from uuid import uuid4

import grpc

from blindsided.common.config import CONTROLLER_PORT
from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc


controller_host = os.getenv("CONTROLLER_HOST", "localhost")
CONTROLLER_ADDRESS = f"{controller_host}:{CONTROLLER_PORT}"


class AuctionService(pb2_grpc.AuctionServiceServicer):
    """API layer that hides sealed bid details until an auction is revealed."""

    def _get_primary_address(self, force_refresh=False) -> str | None:
        """Ask the controller which storage replica owns authoritative writes."""
        try:
            with grpc.insecure_channel(CONTROLLER_ADDRESS) as ch:
                stub = pb2_grpc.ClusterControllerStub(ch)
                resp = stub.GetPrimary(pb2.GetPrimaryRequest(), timeout=2.0)
                if resp.success:
                    return resp.primary_address
        except Exception as e:
            print(f"[BlindSided] Controller unreachable: {e}")
        return None

    def _get_storage_node_addresses(self) -> list[str]:
        """Return storage replicas that can serve stale-tolerant discovery reads."""
        try:
            with grpc.insecure_channel(CONTROLLER_ADDRESS) as ch:
                stub = pb2_grpc.ClusterControllerStub(ch)
                resp = stub.GetClusterInfo(pb2.ClusterInfoRequest(), timeout=3.0)
                if resp.success:
                    return list(resp.node_addresses)
        except Exception as e:
            print(f"[BlindSided] Could not fetch cluster info: {e}")
        return []

    def _create_storage_stub(self, address: str):
        """Create a storage replica client and its owning channel."""
        channel = grpc.insecure_channel(address)
        return pb2_grpc.StorageReplicaServiceStub(channel), channel

    def _mutation_retry_limit(self) -> int:
        """Bound optimistic-concurrency retries until write idempotency exists."""
        return int(os.getenv("MUTATION_RETRY_LIMIT", "5"))

    def _is_version_conflict(self, response: pb2.AuctionMutationResponse) -> bool:
        return (
            response.failure_reason
            == pb2.MUTATION_FAILURE_REASON_CONCURRENCY_CONFLICT
        )

    def CreateAuction(self, request: pb2.CreateAuctionRequest, context) -> pb2.CreateAuctionResponse:
        """Persist a new auction through the current primary replica."""
        primary_address = self._get_primary_address()
        if not primary_address:
            return pb2.CreateAuctionResponse(ok=False, message="The Vault is unreachable")

        auction = pb2.Auction(
            auction_id=str(uuid4()),
            seller_id=request.seller_id,
            title=request.title,
            category=request.category,
            description=request.description,
            reserve_price=request.reserve_price,
            state=pb2.AUCTION_STATE_OPEN,
        )
        if request.HasField("ends_at"):
            auction.ends_at.CopyFrom(request.ends_at)

        try:
            stub, channel = self._create_storage_stub(primary_address)
            with channel:
                response = stub.ApplyAuctionMutation(pb2.AuctionMutationRequest(
                    mutation_type=pb2.AUCTION_MUTATION_TYPE_CREATE,
                    auction=auction,
                ), timeout=5.0)

                if response.success:
                    return pb2.CreateAuctionResponse(
                        ok=True,
                        auction_id=auction.auction_id,
                        message="Auction opened in the Vault.",
                    )
                return pb2.CreateAuctionResponse(ok=False, message=response.message)

        except grpc.RpcError as e:
            return pb2.CreateAuctionResponse(ok=False, message=f"Judge error: {e.details()}")

    def PlaceBid(self, request: pb2.BidRequest, context) -> pb2.BidResponse:
        """Retry stale-version bid writes against the latest committed version."""
        mutation_retry_limit = self._mutation_retry_limit()
        current_attempt_version = request.expected_version

        for attempt in range(mutation_retry_limit):
            primary_address = self._get_primary_address()
            if not primary_address:
                time.sleep(min(0.01 * (attempt + 1), 0.05))
                continue

            try:
                stub, channel = self._create_storage_stub(primary_address)
                with channel:
                    bid_mutation = pb2.Auction(
                        auction_id=request.auction_id,
                        bids={request.bidder_id: pb2.ActiveBid(amount=request.amount)},
                        version=current_attempt_version
                    )

                    response = stub.ApplyAuctionMutation(pb2.AuctionMutationRequest(
                        mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                        auction=bid_mutation,
                        expected_version=current_attempt_version,
                    ), timeout=3.0)

                    if response.success:
                        return pb2.BidResponse(success=True, message="Vault Updated.")

                    if (
                        self._is_version_conflict(response)
                        and response.current_version
                        and attempt < mutation_retry_limit - 1
                    ):
                        current_attempt_version = response.current_version
                        print(
                            "[ServiceNode] Version conflict. "
                            f"Retrying with v{current_attempt_version}"
                        )
                        time.sleep(min(0.01 * (attempt + 1), 0.05))
                        continue

                    return pb2.BidResponse(success=False, message=response.message)

            except grpc.RpcError as e:
                return pb2.BidResponse(
                    success=False,
                    message=f"Judge connection failed: {e.details()}",
                )

        return pb2.BidResponse(
            success=False,
            message="Vault write contention too high.",
        )

    def WithdrawBid(self, request: pb2.WithdrawBidRequest, context) -> pb2.WithdrawBidResponse:
        """Withdraw the caller's active bid through the current primary replica."""
        mutation_retry_limit = self._mutation_retry_limit()
        current_attempt_version = request.expected_version

        for attempt in range(mutation_retry_limit):
            primary_address = self._get_primary_address()
            if not primary_address:
                time.sleep(min(0.01 * (attempt + 1), 0.05))
                continue

            try:
                stub, channel = self._create_storage_stub(primary_address)
                with channel:
                    response = stub.ApplyAuctionMutation(pb2.AuctionMutationRequest(
                        mutation_type=pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID,
                        auction=pb2.Auction(auction_id=request.auction_id),
                        bidder_id=request.bidder_id,
                        expected_version=current_attempt_version,
                    ), timeout=3.0)

                    if response.success:
                        return pb2.WithdrawBidResponse(
                            success=True,
                            final_version=response.current_version,
                            message="Vault Updated.",
                        )

                    if (
                        self._is_version_conflict(response)
                        and response.current_version
                        and attempt < mutation_retry_limit - 1
                    ):
                        current_attempt_version = response.current_version
                        print(
                            "[ServiceNode] Version conflict. "
                            f"Retrying withdrawal with v{current_attempt_version}"
                        )
                        time.sleep(min(0.01 * (attempt + 1), 0.05))
                        continue

                    return pb2.WithdrawBidResponse(
                        success=False,
                        final_version=response.current_version,
                        message=response.message,
                    )

            except grpc.RpcError as e:
                return pb2.WithdrawBidResponse(
                    success=False,
                    message=f"Judge connection failed: {e.details()}",
                )

        return pb2.WithdrawBidResponse(
            success=False,
            message="Vault write contention too high.",
        )

    def RevealAuction(self, request: pb2.RevealAuctionRequest, context):
        mutation_retry_limit = self._mutation_retry_limit()
        current_attempt_version = request.expected_version

        for attempt in range(mutation_retry_limit):
            primary_address = self._get_primary_address()
            if not primary_address:
                time.sleep(min(0.01 * (attempt + 1), 0.05))
                continue

            try:
                stub, channel = self._create_storage_stub(primary_address)
                with channel:
                    reveal_mutation = pb2.Auction(
                        auction_id=request.auction_id,
                        state=pb2.AUCTION_STATE_REVEALED,
                        version=current_attempt_version
                    )

                    response = stub.ApplyAuctionMutation(pb2.AuctionMutationRequest(
                        mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
                        auction=reveal_mutation,
                        expected_version=current_attempt_version,
                    ))

                    if (
                        not response.success
                        and self._is_version_conflict(response)
                        and response.current_version
                        and attempt < mutation_retry_limit - 1
                    ):
                        current_attempt_version = response.current_version
                        print(
                            "[ServiceNode] Version conflict. "
                            f"Retrying reveal with v{current_attempt_version}"
                        )
                        time.sleep(min(0.01 * (attempt + 1), 0.05))
                        continue

                    return pb2.RevealAuctionResponse(
                        ok=response.success,
                        final_version=response.current_version,
                        message=response.message,
                    )
            except grpc.RpcError as e:
                return pb2.RevealAuctionResponse(
                    ok=False,
                    message=f"Judge connection failed: {e.details()}",
                )

        return pb2.RevealAuctionResponse(
            ok=False,
            message="Vault write contention too high.",
        )

    def SearchAuctions(self, request: pb2.SearchAuctionsRequest, context) -> pb2.SearchAuctionsResponse:
        """Search available replicas and return only public auction fields."""
        candidates = self._get_storage_node_addresses()
        if not candidates:
            primary_address = self._get_primary_address()
            if not primary_address:
                return pb2.SearchAuctionsResponse(ok=False, message="No Judges active")
            candidates = [primary_address]
        else:
            random.shuffle(candidates)

        query = pb2.SearchAuctionsRequest(query=request.query, category=request.category)

        for addr in candidates:
            try:
                stub, channel = self._create_storage_stub(addr)
                with channel:
                    response = stub.SearchAuctions(query, timeout=5.0)
                    public_auctions = [self._to_public_auction(a) for a in response.auctions]
                    return pb2.SearchAuctionsResponse(
                        ok=True,
                        auctions=public_auctions,
                        count=response.count or len(public_auctions),
                        message="Results from the Vault",
                    )
            except grpc.RpcError:
                continue

        return pb2.SearchAuctionsResponse(ok=False, message="Vault unreachable")

    def GetAuction(self, request: pb2.GetAuctionRequest, context) -> pb2.GetAuctionResponse:
        """Fetch one auction from the primary and hide sealed bids while open."""
        primary_address = self._get_primary_address()
        if not primary_address:
            return pb2.GetAuctionResponse(ok=False, message="Judge unreachable")

        try:
            stub, channel = self._create_storage_stub(primary_address)
            with channel:
                response = stub.GetAuction(pb2.GetAuctionRequest(auction_id=request.auction_id))
                if response.ok:
                    public_auction = self._to_public_auction(response.auction)
                    return pb2.GetAuctionResponse(ok=True, auction=public_auction)
                return pb2.GetAuctionResponse(ok=False, message="Auction not found")
        except Exception as e:
            return pb2.GetAuctionResponse(ok=False, message=str(e))

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

    def WatchAuction(self, request: pb2.AuctionRequest, context):
        """Stream public auction updates, revealing winner and amount only at close."""
        auction_id = request.auction_id
        last_version = -1

        print(f"[Opaque Fog] Watcher joined for {auction_id}")

        while context.is_active():
            primary_address = self._get_primary_address()
            if not primary_address:
                time.sleep(1)
                continue

            try:
                stub, channel = self._create_storage_stub(primary_address)
                with channel:
                    response = stub.GetAuction(pb2.GetAuctionRequest(auction_id=auction_id))

                    if response.ok:
                        auction = response.auction

                        if auction.version > last_version:
                            last_version = auction.version

                            yield self._to_public_auction_update(auction)
                            if auction.state == pb2.AUCTION_STATE_REVEALED:
                                return

                time.sleep(1)

            except grpc.RpcError:
                time.sleep(2)
