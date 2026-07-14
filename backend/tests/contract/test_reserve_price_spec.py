from blindsided.auction_service.service import AuctionService
from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import BackendTestCase, NoopContext, future_timestamp, make_judge


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
                bids={"buyer-a": 250.0},
                reserve_price=500.0,
                state=pb2.AUCTION_STATE_OPEN,
            )
        )

        self.assertEqual(update.bidder_count, 1)

    def test_reserve_price_does_not_become_winning_bid(self):
        service = AuctionService()

        update = service._to_public_auction_update(
            pb2.Auction(
                reserve_price=500.0,
                state=pb2.AUCTION_STATE_REVEALED,
            )
        )

        self.assertEqual(update.winning_amount, 0.0)
        self.assertEqual(update.winning_bidder_id, "")

    def test_reserve_price_is_not_exposed_before_reveal(self):
        service = AuctionService()

        public_auction = service._to_public_auction(
            pb2.Auction(
                reserve_price=500.0,
                reserve_met=True,
                bids={"buyer-a": 750.0},
                state=pb2.AUCTION_STATE_OPEN,
            )
        )
        public_update = service._to_public_auction_update(
            pb2.Auction(
                reserve_price=500.0,
                reserve_met=True,
                bids={"buyer-a": 750.0},
                state=pb2.AUCTION_STATE_OPEN,
            )
        )

        self.assertEqual(public_auction.reserve_price, 0.0)
        self.assertFalse(public_auction.reserve_met)
        self.assertEqual(dict(public_auction.bids), {})
        self.assertFalse(public_update.reserve_met)

    def test_reserve_price_only_determines_successful_sale(self):
        service = AuctionService()

        below_reserve = service._to_public_auction_update(
            pb2.Auction(
                bids={"buyer-a": 499.0},
                reserve_price=500.0,
                state=pb2.AUCTION_STATE_REVEALED,
            )
        )
        meeting_reserve = service._to_public_auction_update(
            pb2.Auction(
                bids={"buyer-a": 500.0},
                reserve_price=500.0,
                state=pb2.AUCTION_STATE_REVEALED,
            )
        )

        self.assertFalse(below_reserve.reserve_met)
        self.assertEqual(below_reserve.winning_amount, 0.0)
        self.assertEqual(below_reserve.winning_bidder_id, "")
        self.assertTrue(meeting_reserve.reserve_met)
        self.assertEqual(meeting_reserve.winning_amount, 500.0)
        self.assertEqual(meeting_reserve.winning_bidder_id, "buyer-a")
