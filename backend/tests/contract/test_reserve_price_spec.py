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
        judge = make_judge(role="backup")

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
        judge = make_judge(role="backup")

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
        service = AuctionService()

        winning_amount, winning_bidder_id = service._winner_from_active_bids(
            pb2.Auction(
                reserve_price=500.0,
                state=pb2.AUCTION_STATE_REVEALED,
            )
        )

        self.assertEqual(winning_amount, 0.0)
        self.assertEqual(winning_bidder_id, "")

    def test_reserve_price_is_not_exposed_before_reveal(self):
        service = AuctionService()

        public_auction = service._to_public_auction(
            pb2.Auction(
                reserve_price=500.0,
                reserve_met=True,
                bids={"buyer-a": active_bid(750.0, 1)},
                state=pb2.AUCTION_STATE_OPEN,
            )
        )
        public_update = service._to_public_auction_update(
            pb2.Auction(
                reserve_price=500.0,
                reserve_met=True,
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
        service = AuctionService()

        below_reserve_amount, below_reserve_bidder_id = service._winner_from_active_bids(
            pb2.Auction(
                bids={"buyer-a": active_bid(499.0, 1)},
                reserve_price=500.0,
                state=pb2.AUCTION_STATE_REVEALED,
            )
        )
        meeting_reserve_amount, meeting_reserve_bidder_id = service._winner_from_active_bids(
            pb2.Auction(
                bids={"buyer-a": active_bid(500.0, 1)},
                reserve_price=500.0,
                state=pb2.AUCTION_STATE_REVEALED,
            )
        )

        self.assertEqual(below_reserve_amount, 0.0)
        self.assertEqual(below_reserve_bidder_id, "")
        self.assertEqual(meeting_reserve_amount, 500.0)
        self.assertEqual(meeting_reserve_bidder_id, "buyer-a")

    def test_reserve_met_is_calculated_only_when_auction_is_revealed(self):
        judge = make_judge(role="backup")
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
        reserve_met_after_bid = (
            judge.auction_store["reserve-finalized-on-reveal"].reserve_met
        )
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
        self.assertFalse(reserve_met_after_bid)
        self.assertTrue(reveal_response.success)
        self.assertTrue(
            judge.auction_store["reserve-finalized-on-reveal"].reserve_met
        )
