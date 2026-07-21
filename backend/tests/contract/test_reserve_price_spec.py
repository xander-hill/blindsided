from blindsided.auction_service.service import AuctionService
from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import (
    BackendTestCase,
    NoopContext,
    active_bid,
    future_timestamp,
    make_judge,
)


class ReservePriceSpecificationTests(BackendTestCase):
    """Contract tests for docs/auction-specification.md section 2.3."""

    def test_seller_configures_reserve_price_during_creation(self):
        judge = make_judge(role="primary")

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="reserve-configured",
                    seller_id="seller-a",
                    reserve_price=500.0,
                    ends_at=future_timestamp(),
                )
            ),
            NoopContext(),
        )

        self.assertTrue(response.success)
        self.assertEqual(judge.auction_store["reserve-configured"].reserve_price, 500.0)

    def test_reserve_price_is_not_associated_with_any_bidder(self):
        judge = make_judge(role="primary")

        judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="reserve-not-bidder",
                    seller_id="seller-a",
                    reserve_price=500.0,
                    ends_at=future_timestamp(),
                )
            ),
            NoopContext(),
        )

        self.assertEqual(dict(judge.auction_store["reserve-not-bidder"].bids), {})

    def test_reserve_price_does_not_count_toward_active_bidder_count(self):
        service = AuctionService()

        update = service._to_public_auction_update(
            pb2.Auction(
                bids={"buyer-a": active_bid(250.0, 1)},
                reserve_price=500.0,
                state=pb2.AUCTION_STATE_OPEN,
            )
        )

        self.assertEqual(update.bidder_count, 1)

    def test_reserve_price_does_not_become_winning_bid(self):
        judge = make_judge(role="primary")

        result = judge._build_auction_result(
            pb2.Auction(
                reserve_price=500.0,
                state=pb2.AUCTION_STATE_REVEALED,
            )
        )

        self.assertEqual(result.outcome, pb2.AUCTION_OUTCOME_NO_BIDS)
        self.assertFalse(result.HasField("winning_amount"))
        self.assertFalse(result.HasField("winning_bidder_id"))

    def test_reserve_price_is_not_exposed_before_reveal(self):
        service = AuctionService()

        public_auction = service._to_public_auction(
            pb2.Auction(
                reserve_price=500.0,
                bids={"buyer-a": active_bid(750.0, 1)},
                state=pb2.AUCTION_STATE_OPEN,
            )
        )
        public_update = service._to_public_auction_update(
            pb2.Auction(
                reserve_price=500.0,
                bids={"buyer-a": active_bid(750.0, 1)},
                state=pb2.AUCTION_STATE_OPEN,
            )
        )

        auction_fields = {field.name for field in public_auction.DESCRIPTOR.fields}
        update_fields = {field.name for field in public_update.DESCRIPTOR.fields}
        self.assertNotIn("reserve_price", auction_fields)
        self.assertNotIn("reserve_met", auction_fields)
        self.assertNotIn("bids", auction_fields)
        self.assertNotIn("reserve_price", update_fields)
        self.assertNotIn("reserve_met", update_fields)

    def test_reserve_price_only_determines_successful_sale(self):
        judge = make_judge(role="primary")

        below_reserve = judge._build_auction_result(
            pb2.Auction(
                bids={"buyer-a": active_bid(499.0, 1)},
                reserve_price=500.0,
                state=pb2.AUCTION_STATE_REVEALED,
            )
        )
        meeting_reserve = judge._build_auction_result(
            pb2.Auction(
                bids={"buyer-a": active_bid(500.0, 1)},
                reserve_price=500.0,
                state=pb2.AUCTION_STATE_REVEALED,
            )
        )

        self.assertEqual(below_reserve.outcome, pb2.AUCTION_OUTCOME_RESERVE_NOT_MET)
        self.assertFalse(below_reserve.reserve_met)
        self.assertFalse(below_reserve.has_winner)
        self.assertFalse(below_reserve.HasField("winning_amount"))
        self.assertFalse(below_reserve.HasField("winning_bidder_id"))
        self.assertEqual(meeting_reserve.outcome, pb2.AUCTION_OUTCOME_SUCCESSFUL_SALE)
        self.assertTrue(meeting_reserve.reserve_met)
        self.assertTrue(meeting_reserve.has_winner)
        self.assertEqual(meeting_reserve.winning_amount, 500.0)
        self.assertEqual(meeting_reserve.winning_bidder_id, "buyer-a")

    def test_reserve_met_is_calculated_only_when_auction_is_revealed(self):
        judge = make_judge(role="primary")
        judge.auction_store["reserve-finalized-on-reveal"] = pb2.Auction(
            auction_id="reserve-finalized-on-reveal",
            reserve_price=500.0,
            version=1,
        )

        bid_response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="reserve-finalized-on-reveal",
                    version=1,
                    bids={"buyer-a": active_bid(750.0)},
                )
            ),
            NoopContext(),
        )
        result_after_bid = judge.auction_store[
            "reserve-finalized-on-reveal"
        ].HasField("result")
        reveal_response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="reserve-finalized-on-reveal",
                    version=2,
                ),
                mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
            ),
            NoopContext(),
        )

        self.assertTrue(bid_response.success)
        self.assertFalse(result_after_bid)
        self.assertTrue(reveal_response.success)
        self.assertTrue(
            judge.auction_store["reserve-finalized-on-reveal"].result.reserve_met
        )

    def test_internal_services_use_hidden_bid_and_reserve_data_only_for_rules(self):
        judge = make_judge(role="primary")
        service = AuctionService()
        judge.auction_store["reserve-hidden-rule-data"] = pb2.Auction(
            auction_id="reserve-hidden-rule-data",
            seller_id="seller-a",
            title="Hidden Rule Data",
            reserve_price=500.0,
            state=pb2.AUCTION_STATE_OPEN,
            version=1,
            bids={
                "buyer-a": active_bid(750.0, 1),
                "buyer-b": active_bid(600.0, 2),
            },
        )

        public_before_reveal = service._to_public_auction(
            judge.auction_store["reserve-hidden-rule-data"]
        )
        reveal = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
                auction=pb2.Auction(
                    auction_id="reserve-hidden-rule-data",
                    seller_id="seller-a",
                ),
                expected_version=1,
            ),
            NoopContext(),
        )
        public_after_reveal = service._to_public_auction(
            judge.auction_store["reserve-hidden-rule-data"]
        )

        self.assertEqual(public_before_reveal.bidder_count, 2)
        self.assertNotIn("reserve_price", public_before_reveal.DESCRIPTOR.fields_by_name)
        self.assertNotIn("bids", public_before_reveal.DESCRIPTOR.fields_by_name)
        self.assertNotIn("buyer-a", str(public_before_reveal))
        self.assertNotIn("750", str(public_before_reveal))
        self.assertTrue(reveal.success)
        self.assertEqual(
            public_after_reveal.result.outcome,
            pb2.AUCTION_OUTCOME_SUCCESSFUL_SALE,
        )
        self.assertTrue(public_after_reveal.result.reserve_met)
        self.assertEqual(public_after_reveal.result.winning_bidder_id, "buyer-a")
        self.assertEqual(public_after_reveal.result.winning_amount, 750.0)
        self.assertNotIn("buyer-b", str(public_after_reveal))
        self.assertNotIn("600", str(public_after_reveal))
