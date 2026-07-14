from blindsided.auction_service.service import AuctionService
from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import BackendTestCase, NoopContext, make_judge


class ActiveBidSpecificationTests(BackendTestCase):
    """Contract tests for docs/auction-specification.md section 3.1."""

    def test_system_enforces_at_most_one_active_bid_per_bidder_per_auction(self):
        judge = make_judge(role="backup")
        judge.auction_store["active-one-per-bidder"] = pb2.Auction(
            auction_id="active-one-per-bidder",
            version=1,
        )

        first = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="active-one-per-bidder",
                    version=1,
                    bids={"buyer-a": 100.0},
                )
            ),
            NoopContext(),
        )
        second = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="active-one-per-bidder",
                    version=2,
                    bids={"buyer-a": 150.0},
                )
            ),
            NoopContext(),
        )

        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertEqual(
            dict(judge.auction_store["active-one-per-bidder"].bids),
            {"buyer-a": 150.0},
        )

    def test_new_accepted_bid_replaces_same_bidder_previous_bid(self):
        judge = make_judge(role="backup")
        judge.auction_store["active-replacement"] = pb2.Auction(
            auction_id="active-replacement",
            version=1,
            bids={"buyer-a": 100.0},
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="active-replacement",
                    version=1,
                    bids={"buyer-a": 250.0},
                )
            ),
            NoopContext(),
        )

        self.assertTrue(response.success)
        self.assertEqual(
            judge.auction_store["active-replacement"].bids["buyer-a"],
            250.0,
        )

    def test_replaced_bid_does_not_remain_eligible_to_win(self):
        service = AuctionService()
        judge = make_judge(role="backup")
        judge.auction_store["active-replaced-not-winner"] = pb2.Auction(
            auction_id="active-replaced-not-winner",
            reserve_price=1.0,
            version=1,
            bids={"buyer-a": 100.0, "buyer-b": 150.0},
        )

        replace = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="active-replaced-not-winner",
                    version=1,
                    bids={"buyer-a": 200.0},
                )
            ),
            NoopContext(),
        )
        reveal = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="active-replaced-not-winner",
                    version=2,
                ),
                is_reveal_event=True,
            ),
            NoopContext(),
        )
        public_update = service._to_public_auction_update(
            judge.auction_store["active-replaced-not-winner"]
        )

        self.assertTrue(replace.success)
        self.assertTrue(reveal.success)
        self.assertEqual(public_update.winning_bidder_id, "buyer-a")
        self.assertEqual(public_update.winning_amount, 200.0)

    def test_replaced_bid_does_not_count_toward_distinct_active_bidder_count(self):
        service = AuctionService()

        update = service._to_public_auction_update(
            pb2.Auction(
                bids={"buyer-a": 250.0, "buyer-b": 300.0},
                state=pb2.AUCTION_STATE_OPEN,
            )
        )

        self.assertEqual(update.bidder_count, 2)
