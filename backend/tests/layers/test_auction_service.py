from unittest import mock
from uuid import UUID

from blindsided.auction_service.service import AuctionService
from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import (
    BackendTestCase,
    ChannelContext,
    NoopContext,
    active_bid,
    future_timestamp,
)


class FakeJudgeStub:
    def __init__(self):
        self.mutations: list[pb2.AuctionMutationRequest] = []
        self.gets: list[pb2.GetAuctionRequest] = []
        self.searches: list[pb2.SearchAuctionsRequest] = []
        self.mutation_responses: list[pb2.AuctionMutationResponse] = []
        self.get_responses: list[pb2.GetAuctionResponse] = []
        self.search_responses: list[pb2.SearchAuctionsResponse] = []

    def ApplyAuctionMutation(self, request, timeout=None):
        self.mutations.append(request)
        if self.mutation_responses:
            return self.mutation_responses.pop(0)
        return pb2.AuctionMutationResponse(success=True, current_version=1, message="ok")

    def GetAuction(self, request, timeout=None):
        self.gets.append(request)
        if self.get_responses:
            return self.get_responses.pop(0)
        return pb2.GetAuctionResponse(ok=False)

    def SearchAuctions(self, request, timeout=None):
        self.searches.append(request)
        if self.search_responses:
            return self.search_responses.pop(0)
        return pb2.SearchAuctionsResponse(ok=True)


class TestableAuctionService(AuctionService):
    def __init__(self, stub: FakeJudgeStub, primary_address: str | None = "judge:50051"):
        self.stub = stub
        self.primary_address = primary_address

    def _get_primary_address(self, force_refresh=False):
        return self.primary_address

    def _get_storage_node_addresses(self):
        return [self.primary_address] if self.primary_address else []

    def _create_storage_stub(self, address: str):
        return self.stub, ChannelContext()


class AuctionServiceTests(BackendTestCase):
    def test_open_auction_mutations_to_primary_vault(self):
        stub = FakeJudgeStub()
        service = TestableAuctionService(stub)

        with mock.patch(
            "blindsided.auction_service.service.uuid4",
            return_value="generated-auction-id",
        ):
            response = service.CreateAuction(
                pb2.CreateAuctionRequest(
                    seller_id="seller-a",
                    title="Watch",
                    category="collectibles",
                    description="A clean example",
                    reserve_price=100.0,
                    ends_at=future_timestamp(),
                ),
                NoopContext(),
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.auction_id, "generated-auction-id")
        self.assertEqual(stub.mutations[0].auction.auction_id, "generated-auction-id")
        self.assertEqual(stub.mutations[0].auction.seller_id, "seller-a")
        self.assertEqual(stub.mutations[0].auction.title, "Watch")
        self.assertEqual(stub.mutations[0].auction.category, "collectibles")
        self.assertEqual(stub.mutations[0].auction.description, "A clean example")
        self.assertEqual(stub.mutations[0].auction.reserve_price, 100.0)
        self.assertEqual(stub.mutations[0].auction.ends_at, future_timestamp())
        self.assertEqual(stub.mutations[0].auction.state, pb2.AUCTION_STATE_OPEN)
        self.assertEqual(dict(stub.mutations[0].auction.bids), {})
        self.assertEqual(stub.mutations[0].auction.version, 0)
        self.assertFalse(stub.mutations[0].auction.reserve_met)
        self.assertEqual(
            stub.mutations[0].mutation_type,
            pb2.AUCTION_MUTATION_TYPE_CREATE,
        )

    def test_create_auction_generates_unique_uuid_ids(self):
        stub = FakeJudgeStub()
        service = TestableAuctionService(stub)

        first = service.CreateAuction(
            pb2.CreateAuctionRequest(
                seller_id="seller-a",
                title="First",
                reserve_price=100.0,
                ends_at=future_timestamp(),
            ),
            NoopContext(),
        )
        second = service.CreateAuction(
            pb2.CreateAuctionRequest(
                seller_id="seller-a",
                title="Second",
                reserve_price=100.0,
                ends_at=future_timestamp(),
            ),
            NoopContext(),
        )

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertNotEqual(first.auction_id, second.auction_id)
        self.assertEqual(str(UUID(first.auction_id)), first.auction_id)
        self.assertEqual(str(UUID(second.auction_id)), second.auction_id)

    def test_status_masks_private_fields_before_reveal(self):
        stub = FakeJudgeStub()
        stub.get_responses.append(pb2.GetAuctionResponse(
            ok=True,
                auction=pb2.Auction(
                    auction_id="auction-1",
                    bids={"buyer-a": active_bid(100.0, 1)},
                reserve_price=500.0,
                reserve_met=True,
                state=pb2.AUCTION_STATE_OPEN,
            ),
        ))
        service = TestableAuctionService(stub)

        response = service.GetAuction(
            pb2.GetAuctionRequest(auction_id="auction-1"),
            NoopContext(),
        )

        self.assertTrue(response.ok)
        self.assertEqual(response.auction.auction_id, "auction-1")
        self.assertEqual(dict(response.auction.bids), {})
        self.assertEqual(response.auction.reserve_price, 0.0)
        self.assertFalse(response.auction.reserve_met)

    def test_status_reveals_bids_after_gavel(self):
        stub = FakeJudgeStub()
        stub.get_responses.append(pb2.GetAuctionResponse(
            ok=True,
                auction=pb2.Auction(
                    auction_id="auction-1",
                    bids={"buyer-a": active_bid(100.0, 1)},
                state=pb2.AUCTION_STATE_REVEALED,
            ),
        ))
        service = TestableAuctionService(stub)

        response = service.GetAuction(
            pb2.GetAuctionRequest(auction_id="auction-1"),
            NoopContext(),
        )

        self.assertTrue(response.ok)
        self.assertEqual(response.auction.bids["buyer-a"].amount, 100.0)

    def test_bid_retries_with_latest_version_after_stale_conflict(self):
        stub = FakeJudgeStub()
        stub.mutation_responses.extend([
            pb2.AuctionMutationResponse(success=False, message="Fog conflict: Stale version."),
            pb2.AuctionMutationResponse(success=True, current_version=8, message="ok"),
        ])
        stub.get_responses.append(pb2.GetAuctionResponse(
            ok=True,
            auction=pb2.Auction(auction_id="auction-1", version=7),
        ))
        service = TestableAuctionService(stub)

        response = service.PlaceBid(
            pb2.BidRequest(
                auction_id="auction-1",
                bidder_id="buyer-a",
                amount=250.0,
                expected_version=6,
            ),
            NoopContext(),
        )

        self.assertTrue(response.success)
        self.assertEqual(
            stub.mutations[0].mutation_type,
            pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
        )
        self.assertEqual(stub.mutations[0].expected_version, 6)
        self.assertEqual(stub.mutations[0].auction.version, 6)
        self.assertEqual(stub.mutations[0].auction.bids["buyer-a"].amount, 250.0)
        self.assertEqual(
            stub.mutations[0].auction.bids["buyer-a"].acceptance_order,
            0,
        )
        self.assertEqual(stub.mutations[1].auction.version, 7)
        self.assertEqual(stub.mutations[1].expected_version, 7)

    def test_withdraw_bid_retries_with_latest_version_after_stale_conflict(self):
        stub = FakeJudgeStub()
        stub.mutation_responses.extend([
            pb2.AuctionMutationResponse(success=False, message="Fog conflict: Stale version."),
            pb2.AuctionMutationResponse(success=True, current_version=9, message="ok"),
        ])
        stub.get_responses.append(pb2.GetAuctionResponse(
            ok=True,
            auction=pb2.Auction(auction_id="auction-1", version=8),
        ))
        service = TestableAuctionService(stub)

        response = service.WithdrawBid(
            pb2.WithdrawBidRequest(
                auction_id="auction-1",
                bidder_id="buyer-a",
                expected_version=7,
            ),
            NoopContext(),
        )

        self.assertTrue(response.success)
        self.assertEqual(response.final_version, 9)
        self.assertEqual(
            stub.mutations[0].mutation_type,
            pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID,
        )
        self.assertEqual(stub.mutations[0].auction.auction_id, "auction-1")
        self.assertEqual(stub.mutations[0].bidder_id, "buyer-a")
        self.assertEqual(stub.mutations[0].expected_version, 7)
        self.assertEqual(stub.mutations[1].expected_version, 8)

    def test_drop_gavel_returns_public_gavel_response(self):
        stub = FakeJudgeStub()
        stub.get_responses.append(pb2.GetAuctionResponse(
            ok=True,
            auction=pb2.Auction(auction_id="auction-1", version=3),
        ))
        stub.mutation_responses.append(pb2.AuctionMutationResponse(
            success=True,
            current_version=4,
            message="Vault updated.",
        ))
        service = TestableAuctionService(stub)

        response = service.RevealAuction(
            pb2.RevealAuctionRequest(auction_id="auction-1"),
            NoopContext(),
        )

        self.assertIsInstance(response, pb2.RevealAuctionResponse)
        self.assertTrue(response.ok)
        self.assertEqual(response.final_version, 4)
        self.assertEqual(
            stub.mutations[0].mutation_type,
            pb2.AUCTION_MUTATION_TYPE_REVEAL,
        )

    def test_opaque_update_uses_public_auction_update_fields(self):
        service = TestableAuctionService(FakeJudgeStub())

        hidden = service._to_public_auction_update(pb2.Auction(
            bids={"a": active_bid(100.0, 1), "b": active_bid(250.0, 2)},
            reserve_met=True,
        ))
        revealed = service._to_public_auction_update(pb2.Auction(
            bids={"a": active_bid(100.0, 1), "b": active_bid(250.0, 2)},
            reserve_price=200.0,
            state=pb2.AUCTION_STATE_REVEALED,
        ))

        self.assertEqual(hidden.low_range, 100.0)
        self.assertEqual(hidden.high_range, 250.0)
        self.assertEqual(hidden.bidder_count, 2)
        self.assertFalse(hidden.reserve_met)
        self.assertEqual(revealed.state, pb2.AUCTION_STATE_REVEALED)
        self.assertEqual(revealed.winning_amount, 250.0)
        self.assertEqual(revealed.winning_bidder_id, "b")

    def test_revealed_update_publishes_no_winner_when_reserve_not_met(self):
        service = TestableAuctionService(FakeJudgeStub())

        update = service._to_public_auction_update(pb2.Auction(
            bids={"buyer-a": active_bid(250.0, 1)},
            reserve_price=500.0,
            state=pb2.AUCTION_STATE_REVEALED,
        ))

        self.assertEqual(update.state, pb2.AUCTION_STATE_REVEALED)
        self.assertFalse(update.reserve_met)
        self.assertEqual(update.winning_amount, 0.0)
        self.assertEqual(update.winning_bidder_id, "")
