import os
import random
import time

import grpc

from blindsided.common.config import CONTROLLER_PORT
from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc


controller_host = os.getenv("CONTROLLER_HOST", "localhost")
CONTROLLER_ADDRESS = f"{controller_host}:{CONTROLLER_PORT}"


class BlindSidedService(pb2_grpc.BlindSidedServicer):
    """
    The API Gateway for the BlindSided system.
    Enforces the 'Fog of War' by masking sensitive data from the Judge
    before it reaches the client.
    """

    def _get_primary_address(self, force_refresh=False) -> str | None:
        """Consult the Controller. If force_refresh is True, we don't use cache."""
        try:
            with grpc.insecure_channel(CONTROLLER_ADDRESS) as ch:
                stub = pb2_grpc.ControllerStub(ch)
                resp = stub.GetPrimary(pb2.GetPrimaryRequest(), timeout=2.0)
                if resp.success:
                    return resp.primary_address
        except Exception as e:
            print(f"[BlindSided] Controller unreachable: {e}")
        return None

    def _get_all_judge_addresses(self) -> list[str]:
        """Fetch all healthy Judge nodes for distributed reads."""
        try:
            with grpc.insecure_channel(CONTROLLER_ADDRESS) as ch:
                stub = pb2_grpc.ControllerStub(ch)
                resp = stub.GetClusterInfo(pb2.ClusterInfoRequest(), timeout=3.0)
                if resp.success:
                    return list(resp.node_addresses)
        except Exception as e:
            print(f"[BlindSided] Could not fetch cluster info: {e}")
        return []

    def _judge_stub(self, address: str):
        """Helper to create a stub for the Storage/Judge layer."""
        channel = grpc.insecure_channel(address)
        return pb2_grpc.JudgeNodeStub(channel), channel

    def OpenAuction(self, request: pb2.OpenRequest, context) -> pb2.OpenResponse:
        """Initializes a new auction in the vault."""
        primary = self._get_primary_address()
        if not primary:
            return pb2.OpenResponse(ok=False, message="The Vault is unreachable")

        try:
            stub, channel = self._judge_stub(primary)
            with channel:
                res = stub.CommitToVault(pb2.CommitRequest(
                    auction=request.auction,
                    is_reveal_event=False,
                    skip_consistency_check=False
                ), timeout=5.0)

                if res.success:
                    return pb2.OpenResponse(
                        ok=True,
                        auction_id=request.auction.auction_id,
                        message="Auction opened in the Vault.",
                    )
                return pb2.OpenResponse(ok=False, message=res.message)

        except grpc.RpcError as e:
            return pb2.OpenResponse(ok=False, message=f"Judge error: {e.details()}")

    def PlaceSecretBid(self, request: pb2.BidRequest, context) -> pb2.BidResponse:
        """
        Submits a bid with an internal retry loop for Concurrency Conflicts.
        """
        max_retries = int(os.getenv("BID_MAX_RETRIES", "50"))
        last_error = "Unknown error"
        current_attempt_version = request.expected_version

        for attempt in range(max_retries):
            primary = self._get_primary_address()
            if not primary:
                time.sleep(1.0)
                continue

            try:
                stub, channel = self._judge_stub(primary)
                with channel:
                    bid_state = pb2.Auction(
                        auction_id=request.auction_id,
                        bids={request.buyer_id: request.amount},
                        version=current_attempt_version
                    )

                    res = stub.CommitToVault(pb2.CommitRequest(
                        auction=bid_state,
                        is_reveal_event=False
                    ), timeout=3.0)

                    if res.success:
                        return pb2.BidResponse(success=True, message="Vault Updated.")

                    if "Stale version" in res.message:
                        q_res = stub.QueryVault(pb2.QueryRequest(filter=request.auction_id))
                        if q_res.auctions:
                            current_attempt_version = q_res.auctions[0].version
                            print(
                                "[ServiceNode] Version conflict. "
                                f"Retrying with v{current_attempt_version}"
                            )
                            time.sleep(min(0.01 * (attempt + 1), 0.05))
                            continue

                    return pb2.BidResponse(success=False, message=res.message)

            except grpc.RpcError as e:
                last_error = f"Judge connection failed: {e.details()}"
                time.sleep(1.0)

        return pb2.BidResponse(
            success=False,
            message=f"Vault write contention too high: {last_error}",
        )

    def DropTheGavel(self, request: pb2.GavelRequest, context):
        primary = self._get_primary_address()
        if not primary:
            return pb2.GavelResponse(ok=False, message="Judge unreachable")

        stub, channel = self._judge_stub(primary)
        with channel:
            status_res = stub.QueryVault(pb2.QueryRequest(filter=request.auction_id))
            current_v = status_res.auctions[0].version if status_res.auctions else 0

            reveal_state = pb2.Auction(
                auction_id=request.auction_id,
                state=pb2.AUCTION_STATE_REVEALED,
                version=current_v
            )

            res = stub.CommitToVault(pb2.CommitRequest(
                auction=reveal_state,
                is_reveal_event=True
            ))

            return pb2.GavelResponse(
                ok=res.success,
                final_version=res.current_version,
                message=res.message,
            )

    def SearchAuctions(self, request: pb2.SearchRequest, context) -> pb2.SearchResponse:
        """Queries the vault and applies the Fog masking logic to results."""
        candidates = self._get_all_judge_addresses()
        if not candidates:
            primary = self._get_primary_address()
            if not primary:
                return pb2.SearchResponse(ok=False, message="No Judges active")
            candidates = [primary]
        else:
            random.shuffle(candidates)

        query = pb2.QueryRequest(filter=request.query)

        for addr in candidates:
            try:
                stub, channel = self._judge_stub(addr)
                with channel:
                    res = stub.QueryVault(query, timeout=5.0)
                    masked = [self._mask_for_fog(a) for a in res.auctions]
                    return pb2.SearchResponse(
                        ok=True,
                        auctions=masked,
                        message="Results from the Vault",
                    )
            except grpc.RpcError:
                continue

        return pb2.SearchResponse(ok=False, message="Vault unreachable")

    def GetStatus(self, request: pb2.StatusRequest, context) -> pb2.StatusResponse:
        """Fetch a single auction, masked by the Fog if not revealed."""
        primary = self._get_primary_address()
        if not primary:
            return pb2.StatusResponse(ok=False, message="Judge unreachable")

        try:
            stub, channel = self._judge_stub(primary)
            with channel:
                res = stub.QueryVault(pb2.QueryRequest(filter=request.auction_id))
                if res.auctions:
                    masked = self._mask_for_fog(res.auctions[0])
                    return pb2.StatusResponse(ok=True, auction=masked)
                return pb2.StatusResponse(ok=False, message="Auction not found")
        except Exception as e:
            return pb2.StatusResponse(ok=False, message=str(e))

    def _mask_for_fog(self, auction: pb2.Auction) -> pb2.Auction:
        masked = pb2.Auction()
        masked.CopyFrom(auction)

        if auction.state != pb2.AUCTION_STATE_REVEALED:
            masked.bids.clear()

        return masked

    def _mask_for_opaque_fog(self, auction: pb2.Auction) -> pb2.AuctionUpdate:
        """Transforms raw vault data into Opaque Thermal Readings."""
        if auction.state != pb2.AUCTION_STATE_REVEALED:
            prices = list(auction.bids.values())

            return pb2.AuctionUpdate(
                state=pb2.AUCTION_STATE_OPEN,
                message="The Fog is active.",
                low_range=min(prices) if prices else 0.0,
                high_range=max(prices) if prices else 0.0,
                bidder_count=len(prices),
                reserve_status=auction.reserve_met
            )

        winning_price = max(auction.bids.values()) if auction.bids else 0.0
        winner_id = max(auction.bids, key=auction.bids.get) if auction.bids else "N/A"

        return pb2.AuctionUpdate(
            state=pb2.AUCTION_STATE_REVEALED,
            message="GAVEL FELL!",
            final_price=winning_price,
            winner_id=winner_id
        )

    def JoinLiveAuction(self, request: pb2.AuctionRequest, context):
        """
        The 'Opaque Watcher': Streams thermal readings of the vault.
        Reveals the specific winner and price ONLY when the Gavel falls.
        """
        auction_id = request.auction_id
        last_version = -1

        print(f"[Opaque Fog] Watcher joined for {auction_id}")

        while context.is_active():
            primary = self._get_primary_address()
            if not primary:
                time.sleep(1)
                continue

            try:
                stub, channel = self._judge_stub(primary)
                with channel:
                    res = stub.QueryVault(pb2.QueryRequest(filter=auction_id))

                    if res.auctions:
                        auction = res.auctions[0]

                        if auction.version > last_version:
                            last_version = auction.version

                            if auction.state != pb2.AUCTION_STATE_REVEALED:
                                prices = list(auction.bids.values())

                                yield pb2.AuctionUpdate(
                                    state=pb2.AUCTION_STATE_OPEN,
                                    message="Vault update detected.",
                                    low_range=min(prices) if prices else 0.0,
                                    high_range=max(prices) if prices else 0.0,
                                    bidder_count=len(prices),
                                    reserve_status=auction.reserve_met
                                )
                            else:
                                if auction.bids:
                                    winning_price = max(auction.bids.values())
                                    winner_id = max(auction.bids, key=auction.bids.get)
                                else:
                                    winning_price = 0.0
                                    winner_id = "No Bids Received"

                                yield pb2.AuctionUpdate(
                                    state=pb2.AUCTION_STATE_REVEALED,
                                    message="GAVEL FELL: The truth is revealed!",
                                    final_price=winning_price,
                                    winner_id=winner_id,
                                    bidder_count=len(auction.bids),
                                    reserve_status=auction.reserve_met
                                )
                                return

                time.sleep(1)

            except grpc.RpcError:
                time.sleep(2)
