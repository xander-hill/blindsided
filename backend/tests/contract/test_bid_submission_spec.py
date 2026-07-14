from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import BackendTestCase, NoopContext, make_judge


class BidSubmissionSpecificationTests(BackendTestCase):
    """Contract tests for docs/auction-specification.md section 3.2."""

    def test_bidder_cannot_lower_active_bid_directly(self):
        judge = make_judge(role="backup")
        judge.auction_store["bid-lower-rejected"] = pb2.Auction(
            auction_id="bid-lower-rejected",
            version=1,
            bids={"buyer-a": 300.0},
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="bid-lower-rejected",
                    version=1,
                    bids={"buyer-a": 250.0},
                )
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertEqual(
            judge.auction_store["bid-lower-rejected"].bids["buyer-a"],
            300.0,
        )
        self.assertEqual(judge.auction_store["bid-lower-rejected"].version, 1)

    def test_bidder_cannot_replace_active_bid_with_same_amount(self):
        judge = make_judge(role="backup")
        judge.auction_store["bid-equal-rejected"] = pb2.Auction(
            auction_id="bid-equal-rejected",
            version=1,
            bids={"buyer-a": 300.0},
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="bid-equal-rejected",
                    version=1,
                    bids={"buyer-a": 300.0},
                )
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertEqual(
            judge.auction_store["bid-equal-rejected"].bids["buyer-a"],
            300.0,
        )
        self.assertEqual(judge.auction_store["bid-equal-rejected"].version, 1)
