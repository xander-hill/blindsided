from concurrent import futures
import tempfile
from unittest import mock

import grpc

from blindsided.controller.service import ControllerService
from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc
from backend.tests.helpers import (
    BackendTestCase,
    NoopContext,
    active_bid,
    future_timestamp,
    make_judge,
    running_backend_stack,
)


class DistributedBehaviorTests(BackendTestCase):
    def _replicate_to_backup(self, backup):
        def replicate(auction, idempotency_record=None):
            request = pb2.ReplicationRequest(auction=auction)
            if idempotency_record:
                request.idempotency_record.CopyFrom(idempotency_record)
            return backup.ReplicateAuction(request, NoopContext()).success

        return replicate

    def test_concurrent_bids_keep_one_bid_per_buyer_without_lost_updates(self):
        bidder_count = 5

        with running_backend_stack() as stack:
            with grpc.insecure_channel(stack["auction_addr"]) as channel:
                stub = pb2_grpc.AuctionServiceStub(channel)
                opened = stub.CreateAuction(pb2.CreateAuctionRequest(
                    seller_id="seller-a",
                    title="Chaos Auction",
                    reserve_price=1000.0,
                    ends_at=future_timestamp(),
                ), timeout=5)
                self.assertTrue(opened.ok)
                auction_id = opened.auction_id

            def place_bid(index: int):
                with grpc.insecure_channel(stack["auction_addr"]) as channel:
                    local_stub = pb2_grpc.AuctionServiceStub(channel)
                    status = local_stub.GetAuction(pb2.GetAuctionRequest(
                        auction_id=auction_id,
                    ), timeout=5)
                    return local_stub.PlaceBid(pb2.BidRequest(
                        auction_id=auction_id,
                        bidder_id=f"buyer-{index}",
                        amount=100.0 + index,
                        expected_version=1,
                    ), timeout=20)

            with futures.ThreadPoolExecutor(max_workers=bidder_count) as executor:
                results = list(executor.map(place_bid, range(bidder_count)))

            with grpc.insecure_channel(stack["auction_addr"]) as channel:
                stub = pb2_grpc.AuctionServiceStub(channel)
                gavel = stub.RevealAuction(pb2.RevealAuctionRequest(
                    auction_id=auction_id,
                ), timeout=20)
                final_status = stub.GetAuction(pb2.GetAuctionRequest(
                    auction_id=auction_id,
                ), timeout=20)

        self.assertTrue(all(result.success for result in results))
        self.assertTrue(gavel.ok)
        self.assertEqual(final_status.auction.bidder_count, bidder_count)
        self.assertEqual(final_status.auction.state, pb2.AUCTION_STATE_REVEALED)
        public_fields = {field.name for field in final_status.auction.DESCRIPTOR.fields}
        self.assertNotIn("bids", public_fields)
        self.assertNotIn("winning_amount", public_fields)
        self.assertNotIn("winning_bidder_id", public_fields)

    def test_primary_storage_allows_degraded_commit_when_peer_is_unreachable(self):
        judge = make_judge(
            role="primary",
            peers=["unreachable-peer:50051"],
            address="primary_address:50051",
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(auction=pb2.Auction(
                auction_id="degraded-auction",
                seller_id="seller-a",
                title="Degraded Auction",
                reserve_price=100.0,
                ends_at=future_timestamp(),
            )),
            NoopContext(),
        )

        self.assertTrue(response.success)
        self.assertIn("degraded-auction", judge.auction_store)

    def test_replication_and_full_state_sync_copy_primary_state(self):
        primary = make_judge(role="primary", address="primary_address:50051")
        backup = make_judge(role="backup", address="backup:50051")
        auction = pb2.Auction(
            auction_id="replicated-auction",
            title="Replicated Auction",
            version=7,
            bids={"buyer-a": active_bid(400.0, 1)},
        )
        primary.auction_store[auction.auction_id] = auction

        replication = backup.ReplicateAuction(
            pb2.ReplicationRequest(auction=auction),
            NoopContext(),
        )
        state = primary.SyncFullState(pb2.StateRequest(), NoopContext())

        self.assertTrue(replication.success)
        self.assertEqual(backup.auction_store["replicated-auction"].version, 7)
        self.assertEqual(
            backup.auction_store["replicated-auction"]
            .bids["buyer-a"]
            .acceptance_order,
            1,
        )
        self.assertTrue(state.ok)
        self.assertEqual(state.auctions[0].auction_id, "replicated-auction")

    def test_retry_after_backup_acknowledgement_replays_on_promoted_backup(self):
        primary = make_judge(role="primary", address="primary:50051")
        backup = make_judge(role="backup", address="backup:50051")
        primary.auction_store["idem-failover"] = pb2.Auction(
            auction_id="idem-failover",
            version=1,
            next_bid_sequence=1,
        )
        request = pb2.AuctionMutationRequest(
            mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
            auction=pb2.Auction(
                auction_id="idem-failover",
                bids={"buyer-a": active_bid(250.0)},
            ),
            bidder_id="buyer-a",
            expected_version=1,
            request_id="request-after-backup-ack",
        )

        with mock.patch.object(
            primary,
            "_replicate_to_peers",
            side_effect=self._replicate_to_backup(backup),
        ):
            original = primary.ApplyAuctionMutation(request, NoopContext())
        backup.PromoteToPrimary(pb2.PromotionRequest(new_role="primary"), NoopContext())
        replay = backup.ApplyAuctionMutation(request, NoopContext())

        self.assertTrue(original.success)
        self.assertTrue(replay.success)
        self.assertTrue(replay.replayed)
        self.assertEqual(backup.auction_store["idem-failover"].version, 2)
        self.assertEqual(backup.auction_store["idem-failover"].next_bid_sequence, 2)
        self.assertEqual(
            backup.auction_store["idem-failover"].bids["buyer-a"],
            active_bid(250.0, 1),
        )

    def test_restart_and_retry_uses_persisted_idempotency_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = f"{temp_dir}/auction-state.pb"
            judge = make_judge(role="backup", state_file_path=state_path)
            judge.auction_store["idem-restart-distributed"] = pb2.Auction(
                auction_id="idem-restart-distributed",
                version=1,
                next_bid_sequence=1,
            )
            request = pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                auction=pb2.Auction(
                    auction_id="idem-restart-distributed",
                    bids={"buyer-a": active_bid(250.0)},
                ),
                bidder_id="buyer-a",
                expected_version=1,
                request_id="request-persisted-retry",
            )

            original = judge.ApplyAuctionMutation(request, NoopContext())
            recovered = make_judge(role="backup", state_file_path=state_path)
            recovered._load_state_from_disk()
            replay = recovered.ApplyAuctionMutation(request, NoopContext())

        self.assertTrue(original.success)
        self.assertTrue(replay.success)
        self.assertTrue(replay.replayed)
        self.assertEqual(
            recovered.auction_store["idem-restart-distributed"].version,
            2,
        )

    def test_full_state_synchronization_transfers_idempotency_records(self):
        primary = make_judge(role="backup", address="primary:50051")
        backup = make_judge(role="backup", address="backup:50051")
        primary.auction_store["idem-sync"] = pb2.Auction(
            auction_id="idem-sync",
            version=1,
            next_bid_sequence=1,
        )
        request = pb2.AuctionMutationRequest(
            mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
            auction=pb2.Auction(
                auction_id="idem-sync",
                bids={"buyer-a": active_bid(250.0)},
            ),
            bidder_id="buyer-a",
            expected_version=1,
            request_id="request-sync-record",
        )

        original = primary.ApplyAuctionMutation(request, NoopContext())
        state = primary.SyncFullState(pb2.StateRequest(), NoopContext())
        for auction in state.auctions:
            backup.auction_store[auction.auction_id] = auction
        backup.idempotency_records = {
            record.request_id: record
            for record in state.idempotency_records
        }
        replay = backup.ApplyAuctionMutation(request, NoopContext())

        self.assertTrue(original.success)
        self.assertEqual(len(state.idempotency_records), 1)
        self.assertTrue(replay.success)
        self.assertTrue(replay.replayed)
        self.assertEqual(backup.auction_store["idem-sync"].version, 2)

    def test_different_payload_with_committed_request_id_is_rejected_after_failover(self):
        primary = make_judge(role="primary", address="primary:50051")
        backup = make_judge(role="backup", address="backup:50051")
        primary.auction_store["idem-failover-conflict"] = pb2.Auction(
            auction_id="idem-failover-conflict",
            version=1,
            next_bid_sequence=1,
        )
        original_request = pb2.AuctionMutationRequest(
            mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
            auction=pb2.Auction(
                auction_id="idem-failover-conflict",
                bids={"buyer-a": active_bid(250.0)},
            ),
            bidder_id="buyer-a",
            expected_version=1,
            request_id="request-failover-conflict",
        )
        conflicting_request = pb2.AuctionMutationRequest(
            mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
            auction=pb2.Auction(
                auction_id="idem-failover-conflict",
                bids={"buyer-a": active_bid(300.0)},
            ),
            bidder_id="buyer-a",
            expected_version=2,
            request_id="request-failover-conflict",
        )

        with mock.patch.object(
            primary,
            "_replicate_to_peers",
            side_effect=self._replicate_to_backup(backup),
        ):
            original = primary.ApplyAuctionMutation(original_request, NoopContext())
        backup.PromoteToPrimary(pb2.PromotionRequest(new_role="primary"), NoopContext())
        conflict = backup.ApplyAuctionMutation(conflicting_request, NoopContext())

        self.assertTrue(original.success)
        self.assertFalse(conflict.success)
        self.assertEqual(
            conflict.failure_reason,
            pb2.MUTATION_FAILURE_REASON_IDEMPOTENCY_CONFLICT,
        )
        self.assertEqual(backup.auction_store["idem-failover-conflict"].version, 2)
        self.assertEqual(
            backup.auction_store["idem-failover-conflict"].bids["buyer-a"],
            active_bid(250.0, 1),
        )

    def test_controller_elects_new_primary_after_current_primary_removed(self):
        controller = ControllerService()
        controller.RegisterNode(pb2.RegisterRequest(address="primary_address:50051"), NoopContext())
        controller.RegisterNode(pb2.RegisterRequest(address="backup:50051"), NoopContext())

        del controller.nodes["primary_address:50051"]
        controller.primary_address = None
        with mock.patch.object(controller, "_notify_promotion") as notify:
            controller._elect_new_primary()

        self.assertEqual(controller.primary_address, "backup:50051")
        notify.assert_called_once_with("backup:50051")
