from unittest import mock

from google.protobuf import timestamp_pb2

from blindsided.auction_service.service import AuctionService
from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import BackendTestCase, NoopContext, active_bid, make_judge


class BidWithdrawalSpecificationTests(BackendTestCase):
    """Contract tests for docs/auction-specification.md section 3.3."""

    def test_bidder_can_withdraw_own_active_bid_before_deadline(self):
        judge = make_judge(role="backup")
        judge.auction_store["withdraw-success"] = pb2.Auction(
            auction_id="withdraw-success",
            version=4,
            bids={
                "buyer-a": active_bid(300.0, 1),
                "buyer-b": active_bid(450.0, 2),
            },
            next_bid_sequence=3,
            ends_at=timestamp_pb2.Timestamp(seconds=1000),
        )

        with mock.patch("blindsided.storage.service.time.time", return_value=999.999):
            response = judge.ApplyAuctionMutation(
                pb2.AuctionMutationRequest(
                    mutation_type=pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID,
                    auction=pb2.Auction(auction_id="withdraw-success"),
                    bidder_id="buyer-a",
                    expected_version=4,
                ),
                NoopContext(),
            )

        self.assertTrue(response.success)
        self.assertEqual(response.current_version, 5)
        self.assertNotIn("buyer-a", judge.auction_store["withdraw-success"].bids)
        self.assertIn("buyer-b", judge.auction_store["withdraw-success"].bids)
        self.assertEqual(judge.auction_store["withdraw-success"].version, 5)
        self.assertEqual(judge.auction_store["withdraw-success"].next_bid_sequence, 3)

        update = AuctionService()._to_public_auction_update(
            judge.auction_store["withdraw-success"]
        )
        self.assertEqual(update.bidder_count, 1)

    def test_withdrawal_after_deadline_is_rejected_without_mutation(self):
        judge = make_judge(role="backup")
        judge.auction_store["withdraw-deadline"] = pb2.Auction(
            auction_id="withdraw-deadline",
            version=4,
            bids={"buyer-a": active_bid(300.0, 1)},
            next_bid_sequence=2,
            ends_at=timestamp_pb2.Timestamp(seconds=1000),
        )
        original = pb2.Auction()
        original.CopyFrom(judge.auction_store["withdraw-deadline"])

        with mock.patch("blindsided.storage.service.time.time", return_value=1000.0):
            response = judge.ApplyAuctionMutation(
                pb2.AuctionMutationRequest(
                    mutation_type=pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID,
                    auction=pb2.Auction(auction_id="withdraw-deadline"),
                    bidder_id="buyer-a",
                    expected_version=4,
                ),
                NoopContext(),
            )

        self.assertFalse(response.success)
        self.assertIn("deadline", response.message)
        self.assertEqual(judge.auction_store["withdraw-deadline"], original)

    def test_withdrawal_without_active_bid_fails_without_mutation(self):
        judge = make_judge(role="backup")
        judge.auction_store["withdraw-missing-bid"] = pb2.Auction(
            auction_id="withdraw-missing-bid",
            version=4,
            bids={"buyer-b": active_bid(450.0, 2)},
            next_bid_sequence=3,
        )
        original = pb2.Auction()
        original.CopyFrom(judge.auction_store["withdraw-missing-bid"])

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID,
                auction=pb2.Auction(auction_id="withdraw-missing-bid"),
                bidder_id="buyer-a",
                expected_version=4,
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertIn("no active bid", response.message)
        self.assertEqual(judge.auction_store["withdraw-missing-bid"], original)

    def test_revealed_auction_rejects_withdrawal(self):
        judge = make_judge(role="backup")
        judge.auction_store["withdraw-revealed"] = pb2.Auction(
            auction_id="withdraw-revealed",
            version=4,
            state=pb2.AUCTION_STATE_REVEALED,
            bids={"buyer-a": active_bid(300.0, 1)},
            next_bid_sequence=2,
        )
        original = pb2.Auction()
        original.CopyFrom(judge.auction_store["withdraw-revealed"])

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID,
                auction=pb2.Auction(auction_id="withdraw-revealed"),
                bidder_id="buyer-a",
                expected_version=4,
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertIn("Gavel", response.message)
        self.assertEqual(judge.auction_store["withdraw-revealed"], original)

    def test_bidder_cannot_withdraw_another_bidder_bid(self):
        judge = make_judge(role="backup")
        judge.auction_store["withdraw-other-bidder"] = pb2.Auction(
            auction_id="withdraw-other-bidder",
            version=4,
            bids={"buyer-b": active_bid(450.0, 2)},
            next_bid_sequence=3,
        )
        original = pb2.Auction()
        original.CopyFrom(judge.auction_store["withdraw-other-bidder"])

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID,
                auction=pb2.Auction(
                    auction_id="withdraw-other-bidder",
                    bids={"buyer-b": active_bid(450.0, 2)},
                ),
                bidder_id="buyer-a",
                expected_version=4,
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertIn("no active bid", response.message)
        self.assertEqual(judge.auction_store["withdraw-other-bidder"], original)

    def test_withdrawn_bid_is_not_eligible_to_win(self):
        judge = make_judge(role="backup")
        judge.auction_store["withdraw-not-winner"] = pb2.Auction(
            auction_id="withdraw-not-winner",
            reserve_price=100.0,
            version=4,
            bids={
                "buyer-a": active_bid(900.0, 1),
                "buyer-b": active_bid(300.0, 2),
            },
            next_bid_sequence=3,
        )

        withdraw = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID,
                auction=pb2.Auction(auction_id="withdraw-not-winner"),
                bidder_id="buyer-a",
                expected_version=4,
            ),
            NoopContext(),
        )
        reveal = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
                auction=pb2.Auction(auction_id="withdraw-not-winner"),
                expected_version=5,
            ),
            NoopContext(),
        )

        self.assertTrue(withdraw.success)
        self.assertTrue(reveal.success)
        self.assertNotIn("buyer-a", judge.auction_store["withdraw-not-winner"].bids)
        self.assertEqual(judge.auction_store["withdraw-not-winner"].bids["buyer-b"].amount, 300.0)

    def test_bidder_may_submit_new_valid_bid_after_withdrawal(self):
        judge = make_judge(role="backup")
        judge.auction_store["withdraw-rebid"] = pb2.Auction(
            auction_id="withdraw-rebid",
            version=4,
            bids={"buyer-a": active_bid(900.0, 1)},
            next_bid_sequence=2,
        )

        withdraw = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID,
                auction=pb2.Auction(auction_id="withdraw-rebid"),
                bidder_id="buyer-a",
                expected_version=4,
            ),
            NoopContext(),
        )
        rebid = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                auction=pb2.Auction(
                    auction_id="withdraw-rebid",
                    bids={"buyer-a": active_bid(100.0)},
                ),
                expected_version=5,
            ),
            NoopContext(),
        )

        self.assertTrue(withdraw.success)
        self.assertTrue(rebid.success)
        self.assertEqual(judge.auction_store["withdraw-rebid"].bids["buyer-a"].amount, 100.0)
        self.assertEqual(
            judge.auction_store["withdraw-rebid"].bids["buyer-a"].acceptance_order,
            2,
        )
        self.assertEqual(judge.auction_store["withdraw-rebid"].next_bid_sequence, 3)

    def test_concurrent_withdrawal_and_replacement_commit_only_one_same_version_mutation(self):
        judge = make_judge(role="backup")
        judge.auction_store["withdraw-replace-race"] = pb2.Auction(
            auction_id="withdraw-replace-race",
            version=4,
            bids={"buyer-a": active_bid(300.0, 1)},
            next_bid_sequence=2,
        )

        withdraw = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID,
                auction=pb2.Auction(auction_id="withdraw-replace-race"),
                bidder_id="buyer-a",
                expected_version=4,
            ),
            NoopContext(),
        )
        stale_replace = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                auction=pb2.Auction(
                    auction_id="withdraw-replace-race",
                    bids={"buyer-a": active_bid(450.0)},
                ),
                expected_version=4,
            ),
            NoopContext(),
        )

        self.assertTrue(withdraw.success)
        self.assertFalse(stale_replace.success)
        self.assertIn("Stale version", stale_replace.message)
        self.assertEqual(judge.auction_store["withdraw-replace-race"].version, 5)
        self.assertEqual(dict(judge.auction_store["withdraw-replace-race"].bids), {})
        self.assertEqual(judge.auction_store["withdraw-replace-race"].next_bid_sequence, 2)

    def test_concurrent_replacement_and_withdrawal_commit_only_one_same_version_mutation(self):
        judge = make_judge(role="backup")
        judge.auction_store["replace-withdraw-race"] = pb2.Auction(
            auction_id="replace-withdraw-race",
            version=4,
            bids={"buyer-a": active_bid(300.0, 1)},
            next_bid_sequence=2,
        )

        replace = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                auction=pb2.Auction(
                    auction_id="replace-withdraw-race",
                    bids={"buyer-a": active_bid(450.0)},
                ),
                expected_version=4,
            ),
            NoopContext(),
        )
        stale_withdraw = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID,
                auction=pb2.Auction(auction_id="replace-withdraw-race"),
                bidder_id="buyer-a",
                expected_version=4,
            ),
            NoopContext(),
        )

        self.assertTrue(replace.success)
        self.assertFalse(stale_withdraw.success)
        self.assertIn("Stale version", stale_withdraw.message)
        self.assertEqual(judge.auction_store["replace-withdraw-race"].version, 5)
        self.assertEqual(
            judge.auction_store["replace-withdraw-race"].bids["buyer-a"],
            active_bid(450.0, 2),
        )
        self.assertEqual(judge.auction_store["replace-withdraw-race"].next_bid_sequence, 3)
