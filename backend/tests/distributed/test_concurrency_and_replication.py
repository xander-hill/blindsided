from concurrent import futures
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
    def test_concurrent_bids_keep_one_bid_per_buyer_without_lost_updates(self):
        bidder_count = 12

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
                        expected_version=status.auction.version,
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
