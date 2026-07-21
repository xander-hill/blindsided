import unittest
from unittest import mock

from google.protobuf import timestamp_pb2

from blindsided.auction_service.service import AuctionService
from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import NoopContext, active_bid, make_judge


DEADLINE = 1_000


class OverdueActionsTests(unittest.TestCase):
    def _auction(
        self,
        auction_id="auction-1",
        *,
        deadline=DEADLINE,
        state=pb2.AUCTION_STATE_OPEN,
        reserve=100.0,
        version=4,
        bids=(),
    ):
        auction = pb2.Auction(
            auction_id=auction_id,
            seller_id="seller-1",
            reserve_price=reserve,
            state=state,
            version=version,
            ends_at=timestamp_pb2.Timestamp(seconds=deadline),
            next_bid_sequence=max((order for _, _, order in bids), default=0) + 1,
        )
        for bidder_id, amount, order in bids:
            auction.bids[bidder_id].CopyFrom(active_bid(amount, order))
        if state == pb2.AUCTION_STATE_REVEALED:
            auction.result.CopyFrom(make_judge()._build_auction_result(auction))
        return auction

    def _ready_judge(self, **kwargs):
        return make_judge(
            role="primary",
            synchronous_backup_address="backup:50051",
            **kwargs,
        )

    def _finalize(self, auction=None, now=DEADLINE):
        judge = self._ready_judge()
        auction = auction or self._auction()
        judge.auction_store[auction.auction_id] = auction
        response = judge._finalize_overdue_auction(
            auction.auction_id, judge.current_epoch, now
        )
        return judge, response

    def _retry_request(self, judge, auction_id="auction-1"):
        return judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
                auction=pb2.Auction(auction_id=auction_id, seller_id="seller-1"),
                expected_version=4,
                request_id=judge._overdue_request_id(auction_id),
                epoch=judge.current_epoch,
            ),
            NoopContext(),
        )

    # Discovery
    def test_find_overdue_open_auction(self):
        judge = self._ready_judge()
        judge.auction_store["a"] = self._auction("a", deadline=999)
        self.assertEqual(judge._find_overdue_open_auction_ids(1_000), ["a"])

    def test_find_overdue_auction_at_exact_deadline(self):
        judge = self._ready_judge()
        judge.auction_store["a"] = self._auction("a")
        self.assertEqual(judge._find_overdue_open_auction_ids(DEADLINE), ["a"])

    def test_ignore_open_auction_before_deadline(self):
        judge = self._ready_judge()
        judge.auction_store["a"] = self._auction("a", deadline=1_001)
        self.assertEqual(judge._find_overdue_open_auction_ids(DEADLINE), [])

    def test_ignore_revealed_auction_during_overdue_scan(self):
        judge = self._ready_judge()
        judge.auction_store["a"] = self._auction(
            "a", deadline=999, state=pb2.AUCTION_STATE_REVEALED
        )
        self.assertEqual(judge._find_overdue_open_auction_ids(DEADLINE), [])

    # Safety gates and revalidation
    def test_overdue_finalization_requires_promotion_ready(self):
        judge = self._ready_judge()
        judge.promotion_ready = False
        judge.auction_store["a"] = self._auction("a")
        self.assertEqual(judge._reconcile_overdue_auctions(1, DEADLINE), [])

    def test_overdue_finalization_requires_current_epoch(self):
        judge = self._ready_judge()
        judge.auction_store["a"] = self._auction("a")
        self.assertEqual(judge._reconcile_overdue_auctions(2, DEADLINE), [])

    def test_overdue_finalization_requires_synchronized_backup(self):
        judge = make_judge(role="primary")
        judge.auction_store["a"] = self._auction("a")
        self.assertEqual(judge._reconcile_overdue_auctions(1, DEADLINE), [])

    def test_overdue_finalization_revalidates_auction_state(self):
        judge = self._ready_judge()
        auction = self._auction("a", state=pb2.AUCTION_STATE_REVEALED)
        judge.auction_store["a"] = auction
        self.assertIsNone(judge._finalize_overdue_auction("a", 1, DEADLINE))

    def test_overdue_finalization_revalidates_deadline(self):
        judge = self._ready_judge()
        judge.auction_store["a"] = self._auction("a", deadline=1_001)
        self.assertIsNone(judge._finalize_overdue_auction("a", 1, DEADLINE))

    # Idempotency
    def test_overdue_request_id_is_deterministic(self):
        first_judge = self._ready_judge()
        second_judge = self._ready_judge()
        second_judge.current_epoch = 99
        first = first_judge._overdue_request_id("a")
        second = second_judge._overdue_request_id("a")
        self.assertEqual(first, second)

    def test_repeated_overdue_finalization_is_idempotent(self):
        judge, first = self._finalize()
        second = self._retry_request(judge)
        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertTrue(second.replayed)
        self.assertEqual(judge.auction_store["auction-1"].version, 5)

    # Outcomes and final active bid set
    def test_overdue_reveal_no_bids_outcome(self):
        judge, _ = self._finalize()
        self.assertEqual(
            judge.auction_store["auction-1"].result.outcome,
            pb2.AUCTION_OUTCOME_NO_BIDS,
        )

    def test_overdue_reveal_reserve_not_met_outcome(self):
        judge, _ = self._finalize(self._auction(bids=(("a", 99, 1),)))
        self.assertEqual(
            judge.auction_store["auction-1"].result.outcome,
            pb2.AUCTION_OUTCOME_RESERVE_NOT_MET,
        )

    def test_overdue_reveal_successful_sale_outcome(self):
        judge, _ = self._finalize(self._auction(bids=(("a", 100, 1),)))
        self.assertEqual(
            judge.auction_store["auction-1"].result.winning_bidder_id, "a"
        )

    def test_overdue_reveal_uses_final_active_bid_set(self):
        judge, _ = self._finalize(
            self._auction(bids=(("a", 110, 1), ("b", 120, 2)))
        )
        self.assertEqual(
            judge.auction_store["auction-1"].result.winning_bidder_id, "b"
        )

    def test_overdue_reveal_uses_tie_breaking_rules(self):
        judge, _ = self._finalize(
            self._auction(bids=(("later", 120, 2), ("earlier", 120, 1)))
        )
        self.assertEqual(
            judge.auction_store["auction-1"].result.winning_bidder_id, "earlier"
        )

    def test_overdue_reveal_ignores_replaced_bid(self):
        # Replacement leaves only the bidder's final active amount in storage.
        judge, _ = self._finalize(self._auction(bids=(("a", 130, 2),)))
        self.assertEqual(judge.auction_store["auction-1"].result.winning_amount, 130)

    def test_overdue_reveal_ignores_withdrawn_bid(self):
        judge, _ = self._finalize(self._auction(bids=(("active", 110, 2),)))
        self.assertEqual(
            judge.auction_store["auction-1"].result.winning_bidder_id, "active"
        )
        self.assertNotIn("withdrawn", judge.auction_store["auction-1"].bids)

    # Mutation/version behavior
    def test_overdue_reveal_increments_version_once(self):
        judge, _ = self._finalize()
        self.assertEqual(judge.auction_store["auction-1"].version, 5)

    def test_repeated_overdue_reconciliation_does_not_increment_version(self):
        judge, _ = self._finalize()
        judge._reconcile_overdue_auctions(1, DEADLINE + 1)
        self.assertEqual(judge.auction_store["auction-1"].version, 5)

    def test_failed_overdue_reveal_does_not_increment_version(self):
        judge = self._ready_judge(use_test_coordinator=False)
        judge.auction_store["a"] = self._auction("a")
        with mock.patch.object(judge, "_prepare_on_synchronous_backup", return_value=False):
            response = judge._finalize_overdue_auction("a", 1, DEADLINE)
        self.assertFalse(response.success)
        self.assertEqual(judge.auction_store["a"].version, 4)

    # Replication contract
    def test_overdue_reveal_replicates_to_synchronous_backup(self):
        judge = self._ready_judge(use_test_coordinator=False)
        judge.auction_store["a"] = self._auction("a")
        with mock.patch.object(judge, "_prepare_on_synchronous_backup", return_value=False) as prepare:
            judge._finalize_overdue_auction("a", 1, DEADLINE)
        self.assertEqual(prepare.call_args.args[1].state, pb2.AUCTION_STATE_REVEALED)

    def test_overdue_reveal_requires_backup_acknowledgement(self):
        judge = self._ready_judge(use_test_coordinator=False)
        judge.auction_store["a"] = self._auction("a")
        with mock.patch.object(judge, "_prepare_on_synchronous_backup", return_value=False):
            response = judge._finalize_overdue_auction("a", 1, DEADLINE)
        self.assertEqual(
            response.failure_reason, pb2.MUTATION_FAILURE_REASON_REPLICATION_FAILED
        )

    def test_backup_failure_aborts_overdue_reveal(self):
        judge = self._ready_judge(use_test_coordinator=False)
        judge.auction_store["a"] = self._auction("a")
        with mock.patch.object(judge, "_prepare_on_synchronous_backup", return_value=False), mock.patch.object(
            judge, "_abort_on_synchronous_backup", return_value=True
        ) as abort:
            judge._finalize_overdue_auction("a", 1, DEADLINE)
        abort.assert_called_once_with(judge._overdue_request_id("a"), "a")
        self.assertEqual(judge.auction_store["a"].state, pb2.AUCTION_STATE_OPEN)

    def test_overdue_idempotency_record_replicates_to_backup(self):
        judge = self._ready_judge(use_test_coordinator=False)
        judge.auction_store["a"] = self._auction("a")
        with mock.patch.object(judge, "_prepare_on_synchronous_backup", return_value=False) as prepare:
            judge._finalize_overdue_auction("a", 1, DEADLINE)
        record = prepare.call_args.args[2]
        self.assertEqual(record.request_id, judge._overdue_request_id("a"))

    # Failover timing and promotion
    def test_failover_does_not_extend_deadline(self):
        judge = make_judge(role="backup")
        judge.auction_store["a"] = self._auction("a")
        judge.BeginPrimaryPromotion(pb2.BeginPrimaryPromotionRequest(epoch=2), None)
        self.assertEqual(judge.auction_store["a"].ends_at.seconds, DEADLINE)

    def test_overdue_auction_finalized_after_promotion_completion(self):
        judge = make_judge(role="backup", address="candidate:50051")
        judge.auction_store["a"] = self._auction("a")
        judge.BeginPrimaryPromotion(pb2.BeginPrimaryPromotionRequest(epoch=2), None)
        with mock.patch("blindsided.storage.service.time.time", return_value=DEADLINE):
            judge.CompletePrimaryPromotion(
                pb2.CompletePrimaryPromotionRequest(epoch=2, backup_address="backup:50051"), None
            )
        self.assertEqual(judge.auction_store["a"].state, pb2.AUCTION_STATE_REVEALED)

    def test_overdue_auction_finalized_after_backup_synchronization(self):
        judge = make_judge(role="primary")
        judge.auction_store["a"] = self._auction("a")
        self.assertEqual(judge._reconcile_overdue_auctions(1, DEADLINE), [])
        self.assertEqual(judge.auction_store["a"].state, pb2.AUCTION_STATE_OPEN)
        judge.synchronous_backup_address = "synchronized-backup:50051"
        judge._reconcile_overdue_auctions(1, DEADLINE)
        self.assertEqual(judge.auction_store["a"].state, pb2.AUCTION_STATE_REVEALED)

    def test_overdue_auction_becomes_overdue_during_failover(self):
        judge = make_judge(role="backup", address="candidate:50051")
        judge.auction_store["a"] = self._auction("a", deadline=DEADLINE)
        judge.BeginPrimaryPromotion(pb2.BeginPrimaryPromotionRequest(epoch=2), None)
        with mock.patch("blindsided.storage.service.time.time", return_value=DEADLINE):
            judge.CompletePrimaryPromotion(
                pb2.CompletePrimaryPromotionRequest(epoch=2, backup_address="backup:50051"), None
            )
        self.assertEqual(judge.auction_store["a"].state, pb2.AUCTION_STATE_REVEALED)

    def test_already_overdue_auction_finalized_after_failover(self):
        judge = make_judge(role="backup", address="candidate:50051")
        judge.auction_store["a"] = self._auction("a", deadline=DEADLINE - 1)
        judge.BeginPrimaryPromotion(pb2.BeginPrimaryPromotionRequest(epoch=2), None)
        with mock.patch("blindsided.storage.service.time.time", return_value=DEADLINE):
            judge.CompletePrimaryPromotion(
                pb2.CompletePrimaryPromotionRequest(epoch=2, backup_address="backup:50051"), None
            )
        self.assertEqual(judge.auction_store["a"].state, pb2.AUCTION_STATE_REVEALED)

    # Recovery
    def test_overdue_reveal_returns_original_result_on_retry(self):
        judge, first = self._finalize()
        retry = self._retry_request(judge)
        self.assertEqual(first.current_version, retry.current_version)
        self.assertTrue(retry.replayed)

    def test_overdue_idempotency_survives_failover(self):
        judge, _ = self._finalize()
        promoted = self._ready_judge()
        promoted.auction_store = judge.auction_store.copy()
        promoted.idempotency_records = judge.idempotency_records.copy()
        retry = self._retry_request(promoted)
        self.assertTrue(retry.replayed)

    def test_committed_overdue_reveal_not_reapplied_after_failover(self):
        judge, _ = self._finalize()
        promoted = self._ready_judge()
        promoted.auction_store = judge.auction_store.copy()
        promoted.idempotency_records = judge.idempotency_records.copy()
        promoted._reconcile_overdue_auctions(1, DEADLINE + 1)
        self.assertEqual(promoted.auction_store["auction-1"].version, 5)

    # Public visibility
    def test_overdue_reveal_obeys_post_reveal_visibility_rules(self):
        judge, _ = self._finalize(self._auction(bids=(("winner", 120, 1),)))
        public = AuctionService()._to_public_auction(judge.auction_store["auction-1"])
        self.assertEqual(public.result.winning_bidder_id, "winner")

    def test_overdue_reveal_without_winner_hides_winner_fields(self):
        judge, _ = self._finalize()
        public = AuctionService()._to_public_auction(judge.auction_store["auction-1"])
        self.assertFalse(public.result.HasField("winning_bidder_id"))
        self.assertFalse(public.result.HasField("winning_amount"))

    def test_overdue_reveal_does_not_expose_losing_bid_data(self):
        judge, _ = self._finalize(
            self._auction(bids=(("winner", 120, 1), ("loser", 110, 2)))
        )
        public = AuctionService()._to_public_auction(judge.auction_store["auction-1"])
        self.assertNotIn("loser", str(public))
        self.assertNotIn("110", str(public))
        self.assertNotIn("bids", {field.name for field in public.DESCRIPTOR.fields})

    # End to end through promotion, reconciliation, and retry
    def test_failover_finalizes_overdue_auction_exactly_once(self):
        judge = make_judge(role="backup", address="candidate:50051")
        judge.auction_store["a"] = self._auction("a")
        judge.BeginPrimaryPromotion(pb2.BeginPrimaryPromotionRequest(epoch=2), None)
        with mock.patch("blindsided.storage.service.time.time", return_value=DEADLINE):
            judge.CompletePrimaryPromotion(
                pb2.CompletePrimaryPromotionRequest(epoch=2, backup_address="backup:50051"), None
            )
            judge._reconcile_overdue_auctions(2, DEADLINE)
        self.assertEqual(judge.auction_store["a"].version, 5)
        self.assertEqual(
            list(judge.idempotency_records), [judge._overdue_request_id("a")]
        )


if __name__ == "__main__":
    unittest.main()
