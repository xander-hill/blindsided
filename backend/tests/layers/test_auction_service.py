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
        self.get_responses: list[pb2.GetStoredAuctionResponse] = []
        self.search_responses: list[pb2.GetStoredAuctionsResponse] = []

    def ApplyAuctionMutation(self, request, timeout=None):
        self.mutations.append(request)
        if self.mutation_responses:
            return self.mutation_responses.pop(0)
        return pb2.AuctionMutationResponse(success=True, current_version=1, message="ok")

    def GetAuction(self, request, timeout=None):
        self.gets.append(request)
        if self.get_responses:
            return self.get_responses.pop(0)
        return pb2.GetStoredAuctionResponse(ok=False)

    def SearchAuctions(self, request, timeout=None):
        self.searches.append(request)
        if self.search_responses:
            return self.search_responses.pop(0)
        return pb2.GetStoredAuctionsResponse(ok=True)


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
    def _public_field_names(self, message):
        return {field.name for field in message.DESCRIPTOR.fields}

    def _assert_no_bid_data_exposed(self, message):
        field_names = self._public_field_names(message)
        self.assertNotIn("bids", field_names)
        self.assertNotIn("reserve_price", field_names)
        rendered = str(message)
        self.assertNotIn("losing-bidder", rendered)
        self.assertNotIn("hidden-bidder", rendered)
        self.assertNotIn("12345.5", rendered)
        self.assertNotIn("67890", rendered)

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

    def test_public_auction_projection_preserves_only_allowed_open_fields(self):
        stub = FakeJudgeStub()
        ends_at = future_timestamp()
        stub.get_responses.append(pb2.GetStoredAuctionResponse(
            ok=True,
            auction=pb2.Auction(
                auction_id="auction-1",
                seller_id="seller-a",
                title="Auction Metadata",
                category="collectibles",
                description="Metadata stays visible",
                bids={
                    "hidden-bidder-a": active_bid(12345.5, 1),
                    "hidden-bidder-b": active_bid(67890.0, 2),
                },
                reserve_price=500.0,
                reserve_met=True,
                state=pb2.AUCTION_STATE_OPEN,
                version=7,
                ends_at=ends_at,
            ),
        ))
        service = TestableAuctionService(stub)

        response = service.GetAuction(
            pb2.GetAuctionRequest(auction_id="auction-1"),
            NoopContext(),
        )

        self.assertTrue(response.ok)
        public_auction = response.auction
        self.assertEqual(public_auction.auction_id, "auction-1")
        self.assertEqual(public_auction.seller_id, "seller-a")
        self.assertEqual(public_auction.title, "Auction Metadata")
        self.assertEqual(public_auction.category, "collectibles")
        self.assertEqual(public_auction.description, "Metadata stays visible")
        self.assertEqual(public_auction.ends_at, ends_at)
        self.assertEqual(public_auction.version, 7)
        self.assertEqual(public_auction.state, pb2.AUCTION_STATE_OPEN)
        self.assertEqual(public_auction.bidder_count, 2)

        field_names = {field.name for field in public_auction.DESCRIPTOR.fields}
        self.assertNotIn("bids", field_names)
        self.assertNotIn("reserve_price", field_names)
        self.assertNotIn("reserve_met", field_names)
        self.assertNotIn("winning_amount", field_names)
        self.assertNotIn("winning_bidder_id", field_names)
        self.assertNotIn("has_winner", field_names)
        self.assertNotIn("hidden-bidder-a", str(public_auction))
        self.assertNotIn("hidden-bidder-b", str(public_auction))
        self.assertNotIn("12345.5", str(public_auction))
        self.assertNotIn("67890", str(public_auction))

    def test_revealed_no_bids_public_result_exposes_no_bids(self):
        service = TestableAuctionService(FakeJudgeStub())
        public_auction = service._to_public_auction(pb2.Auction(
            auction_id="auction-1",
            state=pb2.AUCTION_STATE_REVEALED,
            version=3,
            reserve_price=20000.0,
            reserve_met=True,
        ))
        update = service._to_public_auction_update(pb2.Auction(
            auction_id="auction-1",
            state=pb2.AUCTION_STATE_REVEALED,
            version=3,
            reserve_price=20000.0,
            reserve_met=True,
        ))

        self.assertEqual(public_auction.state, pb2.AUCTION_STATE_REVEALED)
        self.assertTrue(public_auction.HasField("result"))
        self.assertEqual(public_auction.result.outcome, pb2.AUCTION_OUTCOME_NO_BIDS)
        self.assertFalse(public_auction.result.reserve_met)
        self.assertFalse(public_auction.result.has_winner)
        self.assertFalse(public_auction.result.HasField("winning_bidder_id"))
        self.assertFalse(public_auction.result.HasField("winning_amount"))
        self.assertEqual(public_auction.bidder_count, 0)
        self._assert_no_bid_data_exposed(public_auction)

        self.assertEqual(update.state, pb2.AUCTION_STATE_REVEALED)
        self.assertTrue(update.HasField("result"))
        self.assertEqual(update.result.outcome, pb2.AUCTION_OUTCOME_NO_BIDS)
        self.assertEqual(update.bidder_count, 0)
        self._assert_no_bid_data_exposed(update)

    def test_revealed_reserve_not_met_public_result_hides_losing_bid_data(self):
        service = TestableAuctionService(FakeJudgeStub())
        auction = pb2.Auction(
            auction_id="auction-1",
            state=pb2.AUCTION_STATE_REVEALED,
            version=3,
            reserve_price=20000.0,
            bids={
                "losing-bidder-a": active_bid(12345.5, 1),
                "losing-bidder-b": active_bid(250.0, 2),
            },
        )

        public_auction = service._to_public_auction(auction)
        update = service._to_public_auction_update(auction)

        self.assertEqual(public_auction.state, pb2.AUCTION_STATE_REVEALED)
        self.assertEqual(
            public_auction.result.outcome,
            pb2.AUCTION_OUTCOME_RESERVE_NOT_MET,
        )
        self.assertFalse(public_auction.result.reserve_met)
        self.assertFalse(public_auction.result.has_winner)
        self.assertFalse(public_auction.result.HasField("winning_bidder_id"))
        self.assertFalse(public_auction.result.HasField("winning_amount"))
        self.assertEqual(public_auction.bidder_count, 2)
        self._assert_no_bid_data_exposed(public_auction)

        self.assertEqual(update.state, pb2.AUCTION_STATE_REVEALED)
        self.assertEqual(update.result.outcome, pb2.AUCTION_OUTCOME_RESERVE_NOT_MET)
        self.assertEqual(update.bidder_count, 2)
        self._assert_no_bid_data_exposed(update)

    def test_revealed_successful_sale_public_result_exposes_only_winner(self):
        service = TestableAuctionService(FakeJudgeStub())
        auction = pb2.Auction(
            auction_id="auction-1",
            state=pb2.AUCTION_STATE_REVEALED,
            version=3,
            reserve_price=500.0,
            bids={
                "winning-bidder": active_bid(750.0, 2),
                "losing-bidder": active_bid(600.0, 1),
            },
        )

        public_auction = service._to_public_auction(auction)
        update = service._to_public_auction_update(auction)

        self.assertEqual(public_auction.state, pb2.AUCTION_STATE_REVEALED)
        self.assertEqual(
            public_auction.result.outcome,
            pb2.AUCTION_OUTCOME_SUCCESSFUL_SALE,
        )
        self.assertTrue(public_auction.result.reserve_met)
        self.assertTrue(public_auction.result.has_winner)
        self.assertEqual(public_auction.result.winning_bidder_id, "winning-bidder")
        self.assertEqual(public_auction.result.winning_amount, 750.0)
        self.assertEqual(public_auction.bidder_count, 2)
        self._assert_no_bid_data_exposed(public_auction)

        self.assertEqual(update.state, pb2.AUCTION_STATE_REVEALED)
        self.assertEqual(update.result.outcome, pb2.AUCTION_OUTCOME_SUCCESSFUL_SALE)
        self.assertEqual(update.result.winning_bidder_id, "winning-bidder")
        self.assertEqual(update.result.winning_amount, 750.0)
        self.assertEqual(update.bidder_count, 2)
        self._assert_no_bid_data_exposed(update)

    def test_search_results_apply_same_pre_reveal_visibility_restrictions(self):
        stub = FakeJudgeStub()
        ends_at = future_timestamp()
        stub.search_responses.append(pb2.GetStoredAuctionsResponse(
            ok=True,
            count=1,
            auctions=[
                pb2.Auction(
                    auction_id="auction-1",
                    seller_id="seller-a",
                    title="Auction Metadata",
                    category="collectibles",
                    description="Metadata stays visible",
                    bids={
                        "hidden-bidder-a": active_bid(12345.5, 1),
                        "hidden-bidder-b": active_bid(67890.0, 2),
                    },
                    reserve_price=500.0,
                    reserve_met=True,
                    state=pb2.AUCTION_STATE_OPEN,
                    version=7,
                    ends_at=ends_at,
                )
            ],
        ))
        service = TestableAuctionService(stub)

        response = service.SearchAuctions(
            pb2.SearchAuctionsRequest(query="auction"),
            NoopContext(),
        )

        self.assertTrue(response.ok)
        self.assertEqual(response.count, 1)
        self.assertEqual(len(response.auctions), 1)
        public_auction = response.auctions[0]
        self.assertEqual(public_auction.auction_id, "auction-1")
        self.assertEqual(public_auction.seller_id, "seller-a")
        self.assertEqual(public_auction.title, "Auction Metadata")
        self.assertEqual(public_auction.category, "collectibles")
        self.assertEqual(public_auction.description, "Metadata stays visible")
        self.assertEqual(public_auction.ends_at, ends_at)
        self.assertEqual(public_auction.version, 7)
        self.assertEqual(public_auction.state, pb2.AUCTION_STATE_OPEN)
        self.assertEqual(public_auction.bidder_count, 2)

        field_names = {field.name for field in public_auction.DESCRIPTOR.fields}
        self.assertNotIn("bids", field_names)
        self.assertNotIn("reserve_price", field_names)
        self.assertNotIn("reserve_met", field_names)
        self.assertNotIn("winning_amount", field_names)
        self.assertNotIn("winning_bidder_id", field_names)
        self.assertNotIn("has_winner", field_names)
        self.assertNotIn("high_range", field_names)
        self.assertNotIn("low_range", field_names)
        self.assertNotIn("hidden-bidder-a", str(public_auction))
        self.assertNotIn("hidden-bidder-b", str(public_auction))
        self.assertNotIn("12345.5", str(public_auction))
        self.assertNotIn("67890", str(public_auction))

    def test_bid_retries_with_latest_version_after_stale_conflict(self):
        stub = FakeJudgeStub()
        stub.mutation_responses.extend([
            pb2.AuctionMutationResponse(success=False, message="Fog conflict: Stale version."),
            pb2.AuctionMutationResponse(success=True, current_version=8, message="ok"),
        ])
        stub.get_responses.append(pb2.GetStoredAuctionResponse(
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
        stub.get_responses.append(pb2.GetStoredAuctionResponse(
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
        stub.get_responses.append(pb2.GetStoredAuctionResponse(
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
            bids={
                "hidden-bidder-a": active_bid(12345.5, 1),
                "hidden-bidder-b": active_bid(67890.0, 2),
            },
            reserve_met=True,
            state=pb2.AUCTION_STATE_OPEN,
            version=9,
        ))
        revealed = service._to_public_auction_update(pb2.Auction(
            bids={"a": active_bid(100.0, 1), "b": active_bid(250.0, 2)},
            reserve_price=200.0,
            state=pb2.AUCTION_STATE_REVEALED,
        ))

        self.assertEqual(hidden.state, pb2.AUCTION_STATE_OPEN)
        self.assertEqual(hidden.bidder_count, 2)
        self.assertEqual(hidden.version, 9)
        field_names = {field.name for field in hidden.DESCRIPTOR.fields}
        self.assertNotIn("bids", field_names)
        self.assertNotIn("bidder_id", field_names)
        self.assertNotIn("bid_amount", field_names)
        self.assertNotIn("reserve_price", field_names)
        self.assertNotIn("reserve_met", field_names)
        self.assertNotIn("winning_amount", field_names)
        self.assertNotIn("winning_bidder_id", field_names)
        self.assertNotIn("high_range", field_names)
        self.assertNotIn("low_range", field_names)
        self.assertNotIn("hidden-bidder-a", str(hidden))
        self.assertNotIn("hidden-bidder-b", str(hidden))
        self.assertNotIn("12345.5", str(hidden))
        self.assertNotIn("67890", str(hidden))
        self.assertEqual(revealed.state, pb2.AUCTION_STATE_REVEALED)
        self.assertEqual(revealed.bidder_count, 2)
