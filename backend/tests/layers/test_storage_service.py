import tempfile
from unittest import mock

from google.protobuf import timestamp_pb2

from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import (
    BackendTestCase,
    NoopContext,
    active_bid,
    future_timestamp,
    make_judge,
)


class StorageServiceTests(BackendTestCase):
    def test_backup_refuses_auction_mutation_without_changing_state(self):
        judge = make_judge(role="backup")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            version=3,
            state=pb2.AUCTION_STATE_OPEN,
        )
        original = pb2.Auction()
        original.CopyFrom(judge.auction_store["auction-1"])

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                auction=pb2.Auction(
                    auction_id="auction-1",
                    bids={"buyer-a": active_bid(250.0)},
                ),
                expected_version=3,
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertEqual(
            response.failure_reason,
            pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
        )
        self.assertIn("primary replica", response.message)
        self.assertEqual(judge.auction_store["auction-1"], original)

    def test_initial_commit_assigns_version_and_starts_without_active_bids(self):
        judge = make_judge(role="primary")

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="auction-1",
                    seller_id="seller-a",
                    title="Chronograph",
                    reserve_price=500.0,
                    ends_at=future_timestamp(),
                )
            ),
            NoopContext(),
        )

        self.assertTrue(response.success)
        self.assertEqual(response.current_version, 1)
        self.assertEqual(judge.auction_store["auction-1"].version, 1)
        self.assertEqual(dict(judge.auction_store["auction-1"].bids), {})
        self.assertNotIn("reserve_met", pb2.Auction.DESCRIPTOR.fields_by_name)

    def test_commit_rejects_stale_versions_and_preserves_existing_state(self):
        judge = make_judge(role="primary")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            title="Chronograph",
            version=3,
            bids={"buyer-a": active_bid(300.0, 1)},
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="auction-1",
                    version=2,
                    bids={"buyer-b": active_bid(400.0)},
                )
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertIn("Stale version", response.message)
        self.assertEqual(response.current_version, 3)
        self.assertEqual(
            response.failure_reason,
            pb2.MUTATION_FAILURE_REASON_CONCURRENCY_CONFLICT,
        )
        self.assertNotIn("buyer-b", judge.auction_store["auction-1"].bids)

    def test_commit_merges_bids_and_overwrites_same_buyer(self):
        judge = make_judge(role="primary")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            title="Chronograph",
            reserve_price=500.0,
            version=1,
            next_bid_sequence=2,
            bids={"buyer-a": active_bid(300.0, 1)},
        )

        first = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="auction-1",
                    version=1,
                    bids={"buyer-b": active_bid(450.0)},
                )
            ),
            NoopContext(),
        )
        second = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="auction-1",
                    version=2,
                    bids={"buyer-a": active_bid(700.0)},
                )
            ),
            NoopContext(),
        )

        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertEqual(judge.auction_store["auction-1"].version, 3)
        self.assertEqual(judge.auction_store["auction-1"].bids["buyer-a"].amount, 700.0)
        self.assertEqual(judge.auction_store["auction-1"].bids["buyer-a"].acceptance_order, 3)
        self.assertEqual(judge.auction_store["auction-1"].bids["buyer-b"].amount, 450.0)
        self.assertEqual(judge.auction_store["auction-1"].bids["buyer-b"].acceptance_order, 2)
        self.assertEqual(judge.auction_store["auction-1"].next_bid_sequence, 4)
        self.assertFalse(judge.auction_store["auction-1"].HasField("result"))

    def test_commit_rejects_same_buyer_lower_bid_and_preserves_state(self):
        judge = make_judge(role="primary")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            version=1,
            bids={"buyer-a": active_bid(300.0, 1)},
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="auction-1",
                    version=1,
                    bids={"buyer-a": active_bid(250.0)},
                )
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertIn("higher", response.message)
        self.assertEqual(judge.auction_store["auction-1"].bids["buyer-a"].amount, 300.0)
        self.assertEqual(judge.auction_store["auction-1"].version, 1)

    def test_commit_rejects_same_buyer_equal_bid_and_preserves_state(self):
        judge = make_judge(role="primary")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            version=1,
            bids={"buyer-a": active_bid(300.0, 1)},
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="auction-1",
                    version=1,
                    bids={"buyer-a": active_bid(300.0)},
                )
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertIn("higher", response.message)
        self.assertEqual(judge.auction_store["auction-1"].bids["buyer-a"].amount, 300.0)
        self.assertEqual(judge.auction_store["auction-1"].version, 1)

    def test_reveal_calculates_result_reserve_met_from_final_active_bids(self):
        judge = make_judge(role="primary")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            reserve_price=500.0,
            version=1,
            bids={"buyer-a": active_bid(700.0, 1)},
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(auction_id="auction-1", version=1),
                mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
            ),
            NoopContext(),
        )

        self.assertTrue(response.success)
        self.assertTrue(judge.auction_store["auction-1"].result.reserve_met)

    def test_reveal_stores_no_bids_internal_result(self):
        judge = make_judge(role="primary")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            reserve_price=500.0,
            version=1,
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(auction_id="auction-1", version=1),
                mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
            ),
            NoopContext(),
        )

        result = judge.auction_store["auction-1"].result
        self.assertTrue(response.success)
        self.assertEqual(judge.auction_store["auction-1"].state, pb2.AUCTION_STATE_REVEALED)
        self.assertTrue(judge.auction_store["auction-1"].HasField("result"))
        self.assertEqual(result.outcome, pb2.AUCTION_OUTCOME_NO_BIDS)
        self.assertFalse(result.reserve_met)
        self.assertFalse(result.has_winner)
        self.assertFalse(result.HasField("winning_bidder_id"))
        self.assertFalse(result.HasField("winning_amount"))
        self.assertEqual(len(judge.auction_store["auction-1"].bids), 0)

    def test_reveal_stores_reserve_not_met_internal_result(self):
        judge = make_judge(role="primary")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            reserve_price=500.0,
            version=1,
            bids={
                "buyer-a": active_bid(250.0, 1),
                "buyer-b": active_bid(300.0, 2),
            },
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(auction_id="auction-1", version=1),
                mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
            ),
            NoopContext(),
        )

        result = judge.auction_store["auction-1"].result
        self.assertTrue(response.success)
        self.assertEqual(result.outcome, pb2.AUCTION_OUTCOME_RESERVE_NOT_MET)
        self.assertFalse(result.reserve_met)
        self.assertFalse(result.has_winner)
        self.assertFalse(result.HasField("winning_bidder_id"))
        self.assertFalse(result.HasField("winning_amount"))

    def test_reveal_stores_successful_sale_internal_result(self):
        judge = make_judge(role="primary")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            reserve_price=500.0,
            version=1,
            bids={
                "buyer-a": active_bid(750.0, 2),
                "buyer-b": active_bid(600.0, 1),
            },
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(auction_id="auction-1", version=1),
                mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
            ),
            NoopContext(),
        )

        result = judge.auction_store["auction-1"].result
        self.assertTrue(response.success)
        self.assertEqual(result.outcome, pb2.AUCTION_OUTCOME_SUCCESSFUL_SALE)
        self.assertTrue(result.reserve_met)
        self.assertTrue(result.has_winner)
        self.assertEqual(result.winning_bidder_id, "buyer-a")
        self.assertEqual(result.winning_amount, 750.0)

    def test_bid_before_ends_at_is_accepted(self):
        judge = make_judge(role="primary")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            version=1,
            ends_at=timestamp_pb2.Timestamp(seconds=1000),
        )

        with mock.patch("blindsided.storage.service.time.time", return_value=999.999):
            response = judge.ApplyAuctionMutation(
                pb2.AuctionMutationRequest(
                    auction=pb2.Auction(
                        auction_id="auction-1",
                        version=1,
                        bids={"buyer-a": active_bid(100.0)},
                    )
                ),
                NoopContext(),
            )

        self.assertTrue(response.success)
        self.assertEqual(judge.auction_store["auction-1"].bids["buyer-a"].amount, 100.0)
        self.assertEqual(judge.auction_store["auction-1"].bids["buyer-a"].acceptance_order, 1)
        self.assertEqual(judge.auction_store["auction-1"].version, 2)

    def test_bid_at_ends_at_is_rejected_without_revealing(self):
        judge = make_judge(role="primary")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            reserve_price=50.0,
            version=1,
            state=pb2.AUCTION_STATE_OPEN,
            bids={"buyer-a": active_bid(100.0, 1)},
            ends_at=timestamp_pb2.Timestamp(seconds=1000),
        )

        with mock.patch("blindsided.storage.service.time.time", return_value=1000.0):
            response = judge.ApplyAuctionMutation(
                pb2.AuctionMutationRequest(
                    auction=pb2.Auction(
                        auction_id="auction-1",
                        version=1,
                        bids={"buyer-b": active_bid(200.0)},
                    )
                ),
                NoopContext(),
            )

        self.assertFalse(response.success)
        self.assertIn("deadline", response.message)
        self.assertNotIn("buyer-b", judge.auction_store["auction-1"].bids)
        self.assertEqual(judge.auction_store["auction-1"].version, 1)
        self.assertEqual(
            judge.auction_store["auction-1"].state,
            pb2.AUCTION_STATE_OPEN,
        )
        self.assertFalse(judge.auction_store["auction-1"].HasField("result"))

    def test_bid_after_ends_at_is_rejected_without_revealing(self):
        judge = make_judge(role="primary")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            reserve_price=50.0,
            version=1,
            state=pb2.AUCTION_STATE_OPEN,
            bids={"buyer-a": active_bid(100.0, 1)},
            ends_at=timestamp_pb2.Timestamp(seconds=1000),
        )

        with mock.patch("blindsided.storage.service.time.time", return_value=1000.001):
            response = judge.ApplyAuctionMutation(
                pb2.AuctionMutationRequest(
                    auction=pb2.Auction(
                        auction_id="auction-1",
                        version=1,
                        bids={"buyer-b": active_bid(200.0)},
                    )
                ),
                NoopContext(),
            )

        self.assertFalse(response.success)
        self.assertIn("deadline", response.message)
        self.assertNotIn("buyer-b", judge.auction_store["auction-1"].bids)
        self.assertEqual(judge.auction_store["auction-1"].version, 1)
        self.assertEqual(
            judge.auction_store["auction-1"].state,
            pb2.AUCTION_STATE_OPEN,
        )
        self.assertFalse(judge.auction_store["auction-1"].HasField("result"))

    def test_withdrawal_repairs_missing_next_sequence_before_rebid(self):
        judge = make_judge(role="primary")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            version=1,
            bids={
                "buyer-a": active_bid(900.0, 3),
                "buyer-b": active_bid(400.0, 1),
            },
        )

        withdraw = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID,
                auction=pb2.Auction(auction_id="auction-1"),
                bidder_id="buyer-a",
                expected_version=1,
            ),
            NoopContext(),
        )
        rebid = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                auction=pb2.Auction(
                    auction_id="auction-1",
                    bids={"buyer-a": active_bid(100.0)},
                ),
                expected_version=2,
            ),
            NoopContext(),
        )

        self.assertTrue(withdraw.success)
        self.assertTrue(rebid.success)
        self.assertEqual(judge.auction_store["auction-1"].next_bid_sequence, 5)
        self.assertEqual(
            judge.auction_store["auction-1"].bids["buyer-a"].acceptance_order,
            4,
        )

    def test_duplicate_acceptance_order_is_rejected_as_corrupted_state(self):
        judge = make_judge(role="primary")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            version=1,
            next_bid_sequence=3,
            bids={
                "buyer-a": active_bid(500.0, 1),
                "buyer-b": active_bid(500.0, 1),
            },
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                auction=pb2.Auction(
                    auction_id="auction-1",
                    bids={"buyer-c": active_bid(600.0)},
                ),
                expected_version=1,
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertIn("duplicate acceptance order", response.message)
        self.assertNotIn("buyer-c", judge.auction_store["auction-1"].bids)

    def test_stale_next_bid_sequence_is_rejected_as_corrupted_state(self):
        judge = make_judge(role="primary")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            version=1,
            next_bid_sequence=2,
            bids={"buyer-a": active_bid(500.0, 3)},
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                auction=pb2.Auction(
                    auction_id="auction-1",
                    bids={"buyer-b": active_bid(600.0)},
                ),
                expected_version=1,
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertIn("next bid sequence is stale", response.message)
        self.assertNotIn("buyer-b", judge.auction_store["auction-1"].bids)

    def test_committed_state_is_loaded_from_local_snapshot_after_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = f"{temp_dir}/auction-state.pb"
            judge = make_judge(role="primary", state_file_path=state_path)

            response = judge.ApplyAuctionMutation(
                pb2.AuctionMutationRequest(
                    mutation_type=pb2.AUCTION_MUTATION_TYPE_CREATE,
                    auction=pb2.Auction(
                        auction_id="auction-1",
                        seller_id="seller-a",
                        title="Chronograph",
                        reserve_price=500.0,
                        ends_at=future_timestamp(),
                    ),
                ),
                NoopContext(),
            )
            recovered = make_judge(role="primary", state_file_path=state_path)
            recovered._load_state_from_disk()

        self.assertTrue(response.success)
        self.assertIn("auction-1", recovered.auction_store)
        self.assertEqual(recovered.auction_store["auction-1"].version, 1)
        self.assertEqual(recovered.auction_store["auction-1"].next_bid_sequence, 1)

    def test_reveal_locks_auction_against_later_bids(self):
        judge = make_judge(role="primary")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            version=4,
            bids={"buyer-a": active_bid(900.0, 1)},
        )

        reveal = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(auction_id="auction-1", version=4),
                mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
            ),
            NoopContext(),
        )
        bid_after_reveal = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="auction-1",
                    version=5,
                    bids={"buyer-b": active_bid(1000.0)},
                )
            ),
            NoopContext(),
        )

        self.assertTrue(reveal.success)
        self.assertEqual(judge.auction_store["auction-1"].state, pb2.AUCTION_STATE_REVEALED)
        self.assertFalse(bid_after_reveal.success)
        self.assertNotIn("buyer-b", judge.auction_store["auction-1"].bids)

    def test_primary_rolls_back_when_reachable_peer_rejects_replication(self):
        judge = make_judge(role="primary", peers=["peer-a:50051"])
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            version=1,
            bids={"buyer-a": active_bid(100.0, 1)},
        )

        with mock.patch.object(judge, "_replicate_to_peers", return_value=False):
            response = judge.ApplyAuctionMutation(
                pb2.AuctionMutationRequest(
                    auction=pb2.Auction(
                        auction_id="auction-1",
                        version=1,
                        bids={"buyer-b": active_bid(200.0)},
                    )
                ),
                NoopContext(),
            )

        self.assertFalse(response.success)
        self.assertEqual(judge.auction_store["auction-1"].version, 1)
        self.assertNotIn("buyer-b", judge.auction_store["auction-1"].bids)

    def test_primary_deletes_new_auction_when_reachable_peer_rejects_replication(self):
        judge = make_judge(role="primary", peers=["peer-a:50051"])

        with mock.patch.object(judge, "_replicate_to_peers", return_value=False):
            response = judge.ApplyAuctionMutation(
                pb2.AuctionMutationRequest(
                    auction=pb2.Auction(
                        auction_id="auction-1",
                        seller_id="seller-a",
                        title="Chronograph",
                        reserve_price=500.0,
                        ends_at=future_timestamp(),
                    )
                ),
                NoopContext(),
            )

        self.assertFalse(response.success)
        self.assertIn("replication failed", response.message)
        self.assertNotIn("auction-1", judge.auction_store)

    def test_query_filters_by_id_title_and_description(self):
        judge = make_judge(role="backup")
        judge.auction_store["a-1"] = pb2.Auction(
            auction_id="a-1",
            title="Vintage Camera",
            description="Brass body",
        )
        judge.auction_store["a-2"] = pb2.Auction(
            auction_id="a-2",
            title="Modern Watch",
            description="Steel bracelet",
        )

        response = judge.SearchAuctions(pb2.SearchAuctionsRequest(query="brass"), NoopContext())

        self.assertTrue(response.ok)
        self.assertEqual(response.count, 1)
        self.assertEqual(response.auctions[0].auction_id, "a-1")

    def test_primary_serves_authoritative_auction_read(self):
        judge = make_judge(role="primary")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            version=3,
            state=pb2.AUCTION_STATE_OPEN,
        )

        response = judge.GetAuction(
            pb2.GetAuctionRequest(auction_id="auction-1"),
            NoopContext(),
        )

        self.assertTrue(response.ok)
        self.assertEqual(response.auction.auction_id, "auction-1")
        self.assertEqual(response.auction.version, 3)

    def test_backup_refuses_authoritative_auction_read(self):
        judge = make_judge(role="backup")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            version=2,
            state=pb2.AUCTION_STATE_OPEN,
        )

        response = judge.GetAuction(
            pb2.GetAuctionRequest(auction_id="auction-1"),
            NoopContext(),
        )

        self.assertFalse(response.ok)
        self.assertIn("primary replica", response.message)
        self.assertFalse(response.HasField("auction"))

    def test_replica_rejects_revealed_state_without_committed_result(self):
        judge = make_judge(role="backup")

        response = judge.ReplicateAuction(
            pb2.ReplicationRequest(
                auction=pb2.Auction(
                    auction_id="uncommitted-reveal",
                    state=pb2.AUCTION_STATE_REVEALED,
                    version=2,
                    bids={"buyer-a": active_bid(750.0, 1)},
                )
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertIn("committed result", response.message)
        self.assertNotIn("uncommitted-reveal", judge.auction_store)

    def test_replica_rejects_revealed_state_with_uncalculated_result(self):
        judge = make_judge(role="backup")

        response = judge.ReplicateAuction(
            pb2.ReplicationRequest(
                auction=pb2.Auction(
                    auction_id="incorrect-reveal",
                    reserve_price=500.0,
                    state=pb2.AUCTION_STATE_REVEALED,
                    version=2,
                    bids={"buyer-a": active_bid(750.0, 1)},
                    result=pb2.AuctionResult(
                        outcome=pb2.AUCTION_OUTCOME_NO_BIDS,
                    ),
                )
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertIn("does not match", response.message)
        self.assertNotIn("incorrect-reveal", judge.auction_store)

    def test_search_does_not_reveal_overdue_open_auctions(self):
        judge = make_judge(role="backup")
        judge.auction_store["overdue"] = pb2.Auction(
            auction_id="overdue",
            title="Overdue Auction",
            reserve_price=500.0,
            version=2,
            state=pb2.AUCTION_STATE_OPEN,
            bids={"buyer-a": active_bid(750.0, 1)},
            ends_at=timestamp_pb2.Timestamp(seconds=1000),
        )

        with mock.patch("blindsided.storage.service.time.time", return_value=1000.0):
            response = judge.SearchAuctions(
                pb2.SearchAuctionsRequest(query="Overdue"),
                NoopContext(),
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.count, 1)
        self.assertEqual(response.auctions[0].state, pb2.AUCTION_STATE_OPEN)
        self.assertEqual(response.auctions[0].version, 2)
        self.assertFalse(response.auctions[0].HasField("result"))
        self.assertEqual(judge.auction_store["overdue"].state, pb2.AUCTION_STATE_OPEN)
        self.assertEqual(judge.auction_store["overdue"].version, 2)
