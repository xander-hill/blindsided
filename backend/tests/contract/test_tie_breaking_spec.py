from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import BackendTestCase, NoopContext, active_bid, make_judge


class TieBreakingSpecificationTests(BackendTestCase):
    """Contract tests for docs/auction-specification.md section 3.4."""

    def test_acceptance_order_is_assigned_by_the_system(self):
        judge = make_judge(role="primary")
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
        judge = make_judge(role="primary")
        auction = pb2.Auction(
            auction_id="tie-earliest-wins",
            reserve_price=1.0,
            state=pb2.AUCTION_STATE_REVEALED,
            bids={
                "buyer-a": active_bid(500.0, 1),
                "buyer-b": active_bid(500.0, 2),
            },
        )

        result = judge._build_auction_result(auction)

        self.assertEqual(result.winning_bidder_id, "buyer-a")
        self.assertEqual(result.winning_amount, 500.0)

    def test_duplicate_acceptance_order_is_corrupted_not_bidder_id_tiebreak(self):
        judge = make_judge(role="primary")
        auction = pb2.Auction(
            auction_id="tie-duplicate-corrupt",
            reserve_price=1.0,
            state=pb2.AUCTION_STATE_REVEALED,
            bids={
                "buyer-a": active_bid(500.0, 1),
                "buyer-b": active_bid(500.0, 1),
            },
        )

        with self.assertRaisesRegex(ValueError, "duplicate acceptance order"):
            judge._build_auction_result(auction)

    def test_replaced_bid_does_not_keep_original_acceptance_order(self):
        judge = make_judge(role="primary")
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
                mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
            ),
            NoopContext(),
        )
        result = judge.auction_store["tie-replacement-order"].result

        self.assertTrue(replace.success)
        self.assertTrue(reveal.success)
        self.assertEqual(
            judge.auction_store["tie-replacement-order"]
            .bids["buyer-a"]
            .acceptance_order,
            3,
        )
        self.assertEqual(result.winning_bidder_id, "buyer-b")
        self.assertEqual(result.winning_amount, 200.0)

    def test_acceptance_order_remains_stable_across_restart_full_state_sync(self):
        primary = make_judge(role="primary", address="primary:50051")
        recovered = make_judge(role="primary", address="recovered:50051")
        primary.auction_store["tie-restart-stability"] = pb2.Auction(
            auction_id="tie-restart-stability",
            reserve_price=1.0,
            version=7,
            next_bid_sequence=3,
            bids={
                "buyer-a": active_bid(500.0, 1),
                "buyer-b": active_bid(500.0, 2),
            },
        )

        state = primary.SyncFullState(pb2.StateRequest(), NoopContext())
        for auction in state.auctions:
            recovered.auction_store[auction.auction_id] = auction

        bid_after_recovery = recovered.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                auction=pb2.Auction(
                    auction_id="tie-restart-stability",
                    bids={"buyer-c": active_bid(500.0)},
                ),
                expected_version=7,
            ),
            NoopContext(),
        )
        reveal = recovered.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
                auction=pb2.Auction(auction_id="tie-restart-stability"),
                expected_version=8,
            ),
            NoopContext(),
        )
        result = recovered.auction_store["tie-restart-stability"].result

        self.assertTrue(state.ok)
        self.assertTrue(bid_after_recovery.success)
        self.assertTrue(reveal.success)
        self.assertEqual(
            recovered.auction_store["tie-restart-stability"]
            .bids["buyer-a"]
            .acceptance_order,
            1,
        )
        self.assertEqual(
            recovered.auction_store["tie-restart-stability"]
            .bids["buyer-c"]
            .acceptance_order,
            3,
        )
        self.assertEqual(result.winning_bidder_id, "buyer-a")
        self.assertEqual(result.winning_amount, 500.0)

    def test_acceptance_order_remains_stable_across_failover(self):
        backup = make_judge(role="backup", address="backup:50051")
        auction = pb2.Auction(
            auction_id="tie-failover-stability",
            reserve_price=1.0,
            version=7,
            next_bid_sequence=3,
            bids={
                "buyer-a": active_bid(500.0, 1),
                "buyer-b": active_bid(500.0, 2),
            },
        )

        backup.auction_store[auction.auction_id] = auction
        promotion = backup.BeginPrimaryPromotion(
            pb2.BeginPrimaryPromotionRequest(epoch=1),
            NoopContext(),
        )
        backup.promotion_ready = True
        bid_after_failover = backup.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                auction=pb2.Auction(
                    auction_id="tie-failover-stability",
                    bids={"buyer-c": active_bid(500.0)},
                ),
                expected_version=7,
            ),
            NoopContext(),
        )
        reveal = backup.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
                auction=pb2.Auction(auction_id="tie-failover-stability"),
                expected_version=8,
            ),
            NoopContext(),
        )
        result = backup.auction_store["tie-failover-stability"].result

        self.assertTrue(promotion.accepted)
        self.assertTrue(bid_after_failover.success)
        self.assertTrue(reveal.success)
        self.assertEqual(backup.replica_role, "primary")
        self.assertEqual(
            backup.auction_store["tie-failover-stability"]
            .bids["buyer-b"]
            .acceptance_order,
            2,
        )
        self.assertEqual(
            backup.auction_store["tie-failover-stability"]
            .bids["buyer-c"]
            .acceptance_order,
            3,
        )
        self.assertEqual(result.winning_bidder_id, "buyer-a")
        self.assertEqual(result.winning_amount, 500.0)

    def test_withdrawn_bid_does_not_keep_original_acceptance_order(self):
        judge = make_judge(role="primary")
        judge.auction_store["tie-withdrawal-order"] = pb2.Auction(
            auction_id="tie-withdrawal-order",
            reserve_price=1.0,
            version=1,
            next_bid_sequence=3,
            bids={
                "buyer-a": active_bid(500.0, 1),
                "buyer-b": active_bid(500.0, 2),
            },
        )

        withdraw = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID,
                auction=pb2.Auction(auction_id="tie-withdrawal-order"),
                bidder_id="buyer-a",
                expected_version=1,
            ),
            NoopContext(),
        )
        rebid = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                auction=pb2.Auction(
                    auction_id="tie-withdrawal-order",
                    bids={"buyer-a": active_bid(500.0)},
                ),
                expected_version=2,
            ),
            NoopContext(),
        )
        reveal = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
                auction=pb2.Auction(auction_id="tie-withdrawal-order"),
                expected_version=3,
            ),
            NoopContext(),
        )
        result = judge.auction_store["tie-withdrawal-order"].result

        self.assertTrue(withdraw.success)
        self.assertTrue(rebid.success)
        self.assertTrue(reveal.success)
        self.assertEqual(
            judge.auction_store["tie-withdrawal-order"]
            .bids["buyer-a"]
            .acceptance_order,
            3,
        )
        self.assertEqual(result.winning_bidder_id, "buyer-b")
        self.assertEqual(result.winning_amount, 500.0)
