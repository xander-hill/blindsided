import tempfile

from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import (
    BackendTestCase,
    NoopContext,
    active_bid,
    future_timestamp,
    make_judge,
)


class IdempotencySpecificationTests(BackendTestCase):
    """Contract tests for docs/auction-specification.md section 7."""

    def test_duplicate_bid_with_same_request_id_replays_success_once(self):
        judge = make_judge(role="backup")
        judge.auction_store["idem-bid"] = pb2.Auction(
            auction_id="idem-bid",
            version=1,
            next_bid_sequence=1,
        )
        request = pb2.AuctionMutationRequest(
            mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
            auction=pb2.Auction(
                auction_id="idem-bid",
                bids={"buyer-a": active_bid(250.0)},
            ),
            bidder_id="buyer-a",
            expected_version=1,
            request_id="request-bid-1",
        )

        first = judge.ApplyAuctionMutation(request, NoopContext())
        second = judge.ApplyAuctionMutation(request, NoopContext())

        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertTrue(second.replayed)
        self.assertEqual(judge.auction_store["idem-bid"].version, 2)
        self.assertEqual(judge.auction_store["idem-bid"].next_bid_sequence, 2)
        self.assertEqual(len(judge.auction_store["idem-bid"].bids), 1)
        self.assertEqual(
            judge.auction_store["idem-bid"].bids["buyer-a"],
            active_bid(250.0, 1),
        )

    def test_same_request_id_with_different_bid_amount_is_rejected_as_conflict(self):
        judge = make_judge(role="backup")
        judge.auction_store["idem-bid-conflict"] = pb2.Auction(
            auction_id="idem-bid-conflict",
            version=1,
            next_bid_sequence=1,
        )

        first = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                auction=pb2.Auction(
                    auction_id="idem-bid-conflict",
                    bids={"buyer-a": active_bid(250.0)},
                ),
                bidder_id="buyer-a",
                expected_version=1,
                request_id="request-bid-conflict",
            ),
            NoopContext(),
        )
        second = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                auction=pb2.Auction(
                    auction_id="idem-bid-conflict",
                    bids={"buyer-a": active_bid(300.0)},
                ),
                bidder_id="buyer-a",
                expected_version=2,
                request_id="request-bid-conflict",
            ),
            NoopContext(),
        )

        self.assertTrue(first.success)
        self.assertFalse(second.success)
        self.assertEqual(
            second.failure_reason,
            pb2.MUTATION_FAILURE_REASON_IDEMPOTENCY_CONFLICT,
        )
        self.assertEqual(judge.auction_store["idem-bid-conflict"].version, 2)
        self.assertEqual(
            judge.auction_store["idem-bid-conflict"].bids["buyer-a"],
            active_bid(250.0, 1),
        )

    def test_duplicate_withdrawal_replays_success_without_missing_bid_error(self):
        judge = make_judge(role="backup")
        judge.auction_store["idem-withdraw"] = pb2.Auction(
            auction_id="idem-withdraw",
            version=4,
            bids={"buyer-a": active_bid(300.0, 1)},
            next_bid_sequence=2,
        )
        request = pb2.AuctionMutationRequest(
            mutation_type=pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID,
            auction=pb2.Auction(auction_id="idem-withdraw"),
            bidder_id="buyer-a",
            expected_version=4,
            request_id="request-withdraw-1",
        )

        first = judge.ApplyAuctionMutation(request, NoopContext())
        second = judge.ApplyAuctionMutation(request, NoopContext())

        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertTrue(second.replayed)
        self.assertNotIn("no active bid", second.message)
        self.assertEqual(judge.auction_store["idem-withdraw"].version, 5)
        self.assertEqual(dict(judge.auction_store["idem-withdraw"].bids), {})

    def test_duplicate_reveal_replays_success_once_with_identical_outcome(self):
        judge = make_judge(role="backup")
        judge.auction_store["idem-reveal"] = pb2.Auction(
            auction_id="idem-reveal",
            seller_id="seller-a",
            reserve_price=500.0,
            version=2,
            bids={"buyer-a": active_bid(750.0, 1)},
        )
        request = pb2.AuctionMutationRequest(
            mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
            auction=pb2.Auction(
                auction_id="idem-reveal",
                seller_id="seller-a",
            ),
            expected_version=2,
            request_id="request-reveal-1",
        )

        first = judge.ApplyAuctionMutation(request, NoopContext())
        first_result = pb2.AuctionResult()
        first_result.CopyFrom(judge.auction_store["idem-reveal"].result)
        second = judge.ApplyAuctionMutation(request, NoopContext())

        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertTrue(second.replayed)
        self.assertEqual(judge.auction_store["idem-reveal"].version, 3)
        self.assertEqual(judge.auction_store["idem-reveal"].result, first_result)

    def test_duplicate_creation_replays_original_auction_id_and_creates_once(self):
        judge = make_judge(role="backup")
        first = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_CREATE,
                auction=pb2.Auction(
                    auction_id="created-a",
                    seller_id="seller-a",
                    title="Idempotent Creation",
                    category="collectibles",
                    description="one logical create",
                    reserve_price=500.0,
                    ends_at=future_timestamp(),
                ),
                request_id="request-create-1",
            ),
            NoopContext(),
        )
        second = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_CREATE,
                auction=pb2.Auction(
                    auction_id="created-b",
                    seller_id="seller-a",
                    title="Idempotent Creation",
                    category="collectibles",
                    description="one logical create",
                    reserve_price=500.0,
                    ends_at=future_timestamp(),
                ),
                request_id="request-create-1",
            ),
            NoopContext(),
        )

        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertTrue(second.replayed)
        self.assertEqual(first.auction_id, "created-a")
        self.assertEqual(second.auction_id, "created-a")
        self.assertEqual(list(judge.auction_store), ["created-a"])

    def test_domain_rejections_do_not_create_permanent_idempotency_record(self):
        judge = make_judge(role="backup")
        judge.auction_store["idem-reject"] = pb2.Auction(
            auction_id="idem-reject",
            version=1,
            bids={"buyer-a": active_bid(300.0, 1)},
            next_bid_sequence=2,
        )

        rejected = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                auction=pb2.Auction(
                    auction_id="idem-reject",
                    bids={"buyer-a": active_bid(250.0)},
                ),
                bidder_id="buyer-a",
                expected_version=1,
                request_id="request-domain-reject",
            ),
            NoopContext(),
        )
        accepted = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                auction=pb2.Auction(
                    auction_id="idem-reject",
                    bids={"buyer-a": active_bid(350.0)},
                ),
                bidder_id="buyer-a",
                expected_version=1,
                request_id="request-domain-reject",
            ),
            NoopContext(),
        )

        self.assertFalse(rejected.success)
        self.assertTrue(accepted.success)
        self.assertEqual(judge.auction_store["idem-reject"].version, 2)

    def test_persisted_idempotency_state_survives_restart_and_replays(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = f"{temp_dir}/auction-state.pb"
            judge = make_judge(role="backup", state_file_path=state_path)
            judge.auction_store["idem-restart"] = pb2.Auction(
                auction_id="idem-restart",
                version=1,
                next_bid_sequence=1,
            )
            request = pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                auction=pb2.Auction(
                    auction_id="idem-restart",
                    bids={"buyer-a": active_bid(250.0)},
                ),
                bidder_id="buyer-a",
                expected_version=1,
                request_id="request-restart-1",
            )

            first = judge.ApplyAuctionMutation(request, NoopContext())
            recovered = make_judge(role="backup", state_file_path=state_path)
            recovered._load_state_from_disk()
            second = recovered.ApplyAuctionMutation(request, NoopContext())

        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertTrue(second.replayed)
        self.assertEqual(recovered.auction_store["idem-restart"].version, 2)
