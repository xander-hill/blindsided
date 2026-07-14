from blindsided.auction_service.service import AuctionService
from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import BackendTestCase, NoopContext, active_bid, make_judge


class TieBreakingSpecificationTests(BackendTestCase):
    """Contract tests for docs/auction-specification.md section 3.4."""

    def test_acceptance_order_is_assigned_by_the_system(self):
        judge = make_judge(role="backup")
        judge.auction_store["tie-order-system"] = pb2.Auction(
            auction_id="tie-order-system",
            version=1,
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="tie-order-system",
                    version=1,
                    bids={"buyer-a": active_bid(100.0, 99)},
                )
            ),
            NoopContext(),
        )

        self.assertTrue(response.success)
        self.assertEqual(
            judge.auction_store["tie-order-system"].bids["buyer-a"].acceptance_order,
            1,
        )
        self.assertEqual(judge.auction_store["tie-order-system"].next_bid_sequence, 2)

    def test_earliest_accepted_active_bid_wins_tied_highest_amount(self):
        service = AuctionService()
        auction = pb2.Auction(
            auction_id="tie-earliest-wins",
            reserve_price=1.0,
            state=pb2.AUCTION_STATE_REVEALED,
            bids={
                "buyer-a": active_bid(500.0, 1),
                "buyer-b": active_bid(500.0, 2),
            },
        )

        update = service._to_public_auction_update(auction)

        self.assertEqual(update.winning_bidder_id, "buyer-a")
        self.assertEqual(update.winning_amount, 500.0)

    def test_replaced_bid_does_not_keep_original_acceptance_order(self):
        service = AuctionService()
        judge = make_judge(role="backup")
        judge.auction_store["tie-replacement-order"] = pb2.Auction(
            auction_id="tie-replacement-order",
            reserve_price=1.0,
            version=1,
            next_bid_sequence=3,
            bids={
                "buyer-a": active_bid(100.0, 1),
                "buyer-b": active_bid(200.0, 2),
            },
        )

        replace = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="tie-replacement-order",
                    version=1,
                    bids={"buyer-a": active_bid(200.0)},
                )
            ),
            NoopContext(),
        )
        reveal = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="tie-replacement-order",
                    version=2,
                ),
                is_reveal_event=True,
            ),
            NoopContext(),
        )
        update = service._to_public_auction_update(
            judge.auction_store["tie-replacement-order"]
        )

        self.assertTrue(replace.success)
        self.assertTrue(reveal.success)
        self.assertEqual(
            judge.auction_store["tie-replacement-order"]
            .bids["buyer-a"]
            .acceptance_order,
            3,
        )
        self.assertEqual(update.winning_bidder_id, "buyer-b")
