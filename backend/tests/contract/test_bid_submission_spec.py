from unittest import mock

from google.protobuf import timestamp_pb2

from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import BackendTestCase, NoopContext, active_bid, make_judge


class BidSubmissionSpecificationTests(BackendTestCase):
    """Contract tests for docs/auction-specification.md section 3.2."""

    def test_bidder_may_submit_one_active_bid_while_auction_is_open(self):
        judge = make_judge(role="backup")
        judge.auction_store["bid-open-accepted"] = pb2.Auction(
            auction_id="bid-open-accepted",
            version=1,
            state=pb2.AUCTION_STATE_OPEN,
            ends_at=timestamp_pb2.Timestamp(seconds=1000),
        )

        with mock.patch("blindsided.storage.service.time.time", return_value=999.999):
            response = judge.ApplyAuctionMutation(
                pb2.AuctionMutationRequest(
                    mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                    auction=pb2.Auction(
                        auction_id="bid-open-accepted",
                        bids={"buyer-a": active_bid(250.0)},
                    ),
                    expected_version=1,
                ),
                NoopContext(),
            )

        self.assertTrue(response.success)
        self.assertEqual(response.current_version, 2)
        self.assertEqual(
            judge.auction_store["bid-open-accepted"].bids["buyer-a"],
            active_bid(250.0, 1),
        )
        self.assertEqual(judge.auction_store["bid-open-accepted"].version, 2)

    def test_revealed_auction_rejects_bid_submission_without_mutation(self):
        judge = make_judge(role="backup")
        judge.auction_store["bid-revealed-rejected"] = pb2.Auction(
            auction_id="bid-revealed-rejected",
            version=3,
            state=pb2.AUCTION_STATE_REVEALED,
            bids={"buyer-a": active_bid(300.0, 1)},
        )
        original = pb2.Auction()
        original.CopyFrom(judge.auction_store["bid-revealed-rejected"])

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                auction=pb2.Auction(
                    auction_id="bid-revealed-rejected",
                    bids={"buyer-b": active_bid(400.0)},
                ),
                expected_version=3,
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertEqual(judge.auction_store["bid-revealed-rejected"], original)

    def test_bid_submission_after_ends_at_triggers_auto_reveal_and_rejects_bid(self):
        judge = make_judge(role="backup")
        judge.auction_store["bid-deadline-rejected"] = pb2.Auction(
            auction_id="bid-deadline-rejected",
            reserve_price=200.0,
            version=1,
            state=pb2.AUCTION_STATE_OPEN,
            bids={"buyer-a": active_bid(300.0, 1)},
            ends_at=timestamp_pb2.Timestamp(seconds=1000),
        )

        with mock.patch("blindsided.storage.service.time.time", return_value=1000.0):
            response = judge.ApplyAuctionMutation(
                pb2.AuctionMutationRequest(
                    mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                    auction=pb2.Auction(
                        auction_id="bid-deadline-rejected",
                        bids={"buyer-b": active_bid(400.0)},
                    ),
                    expected_version=1,
                ),
                NoopContext(),
            )

        self.assertFalse(response.success)
        self.assertIn("Gavel", response.message)
        self.assertNotIn("buyer-b", judge.auction_store["bid-deadline-rejected"].bids)
        self.assertEqual(
            judge.auction_store["bid-deadline-rejected"].state,
            pb2.AUCTION_STATE_REVEALED,
        )
        self.assertEqual(judge.auction_store["bid-deadline-rejected"].version, 2)
        self.assertEqual(
            judge.auction_store["bid-deadline-rejected"].result.outcome,
            pb2.AUCTION_OUTCOME_SUCCESSFUL_SALE,
        )

    def test_successful_bid_mutation_increments_version_exactly_once(self):
        judge = make_judge(role="backup")
        judge.auction_store["bid-version-once"] = pb2.Auction(
            auction_id="bid-version-once",
            version=10,
            state=pb2.AUCTION_STATE_OPEN,
            next_bid_sequence=7,
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                auction=pb2.Auction(
                    auction_id="bid-version-once",
                    bids={"buyer-a": active_bid(250.0)},
                ),
                expected_version=10,
            ),
            NoopContext(),
        )

        self.assertTrue(response.success)
        self.assertEqual(response.current_version, 11)
        self.assertEqual(judge.auction_store["bid-version-once"].version, 11)
        self.assertEqual(
            judge.auction_store["bid-version-once"].bids["buyer-a"].acceptance_order,
            7,
        )
        self.assertEqual(judge.auction_store["bid-version-once"].next_bid_sequence, 8)

    def test_bid_mutation_without_bid_is_rejected_without_state_or_version_change(self):
        judge = make_judge(role="backup")
        judge.auction_store["bid-empty-rejected"] = pb2.Auction(
            auction_id="bid-empty-rejected",
            version=4,
            state=pb2.AUCTION_STATE_OPEN,
            bids={"buyer-a": active_bid(300.0, 1)},
        )
        original = pb2.Auction()
        original.CopyFrom(judge.auction_store["bid-empty-rejected"])

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                auction=pb2.Auction(auction_id="bid-empty-rejected"),
                expected_version=4,
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertIn("requires at least one bid", response.message)
        self.assertEqual(judge.auction_store["bid-empty-rejected"], original)

    def test_bidder_cannot_lower_active_bid_directly(self):
        judge = make_judge(role="backup")
        judge.auction_store["bid-lower-rejected"] = pb2.Auction(
            auction_id="bid-lower-rejected",
            version=1,
            bids={"buyer-a": active_bid(300.0, 1)},
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="bid-lower-rejected",
                    version=1,
                    bids={"buyer-a": active_bid(250.0)},
                )
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertEqual(
            judge.auction_store["bid-lower-rejected"].bids["buyer-a"],
            active_bid(300.0, 1),
        )
        self.assertEqual(judge.auction_store["bid-lower-rejected"].version, 1)

    def test_bidder_cannot_replace_active_bid_with_same_amount(self):
        judge = make_judge(role="backup")
        judge.auction_store["bid-equal-rejected"] = pb2.Auction(
            auction_id="bid-equal-rejected",
            version=1,
            bids={"buyer-a": active_bid(300.0, 1)},
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="bid-equal-rejected",
                    version=1,
                    bids={"buyer-a": active_bid(300.0)},
                )
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertEqual(
            judge.auction_store["bid-equal-rejected"].bids["buyer-a"],
            active_bid(300.0, 1),
        )
        self.assertEqual(judge.auction_store["bid-equal-rejected"].version, 1)
