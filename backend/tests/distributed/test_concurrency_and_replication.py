from concurrent import futures
from unittest import mock

import grpc

from blindsided.controller.service import ControllerService
from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc
from backend.tests.helpers import BackendTestCase, NoopContext, make_judge, running_backend_stack


class DistributedBehaviorTests(BackendTestCase):
    def test_concurrent_bids_keep_one_bid_per_buyer_without_lost_updates(self):
        bidder_count = 12

        with running_backend_stack() as stack:
            with grpc.insecure_channel(stack["auction_addr"]) as channel:
                stub = pb2_grpc.BlindSidedStub(channel)
                opened = stub.OpenAuction(pb2.OpenRequest(auction=pb2.Auction(
                    auction_id="chaos-auction",
                    title="Chaos Auction",
                    reserve_price=1000.0,
                )), timeout=5)
                self.assertTrue(opened.ok)

            def place_bid(index: int):
                with grpc.insecure_channel(stack["auction_addr"]) as channel:
                    local_stub = pb2_grpc.BlindSidedStub(channel)
                    status = local_stub.GetStatus(pb2.StatusRequest(
                        auction_id="chaos-auction",
                    ), timeout=5)
                    return local_stub.PlaceSecretBid(pb2.BidRequest(
                        auction_id="chaos-auction",
                        buyer_id=f"buyer-{index}",
                        amount=100.0 + index,
                        expected_version=status.auction.version,
                    ), timeout=20)

            with futures.ThreadPoolExecutor(max_workers=bidder_count) as executor:
                results = list(executor.map(place_bid, range(bidder_count)))

            with grpc.insecure_channel(stack["auction_addr"]) as channel:
                stub = pb2_grpc.BlindSidedStub(channel)
                gavel = stub.DropTheGavel(pb2.GavelRequest(
                    auction_id="chaos-auction",
                ), timeout=20)
                final_status = stub.GetStatus(pb2.StatusRequest(
                    auction_id="chaos-auction",
                ), timeout=20)

        self.assertTrue(all(result.success for result in results))
        self.assertTrue(gavel.ok)
        self.assertEqual(len(final_status.auction.bids), bidder_count)
        self.assertEqual(final_status.auction.state, pb2.AUCTION_STATE_REVEALED)
        self.assertEqual(final_status.auction.bids["buyer-11"], 111.0)

    def test_primary_storage_allows_degraded_commit_when_peer_is_unreachable(self):
        judge = make_judge(
            role="primary",
            peers=["unreachable-peer:50051"],
            address="primary:50051",
        )

        response = judge.CommitToVault(
            pb2.CommitRequest(auction=pb2.Auction(
                auction_id="degraded-auction",
                title="Degraded Auction",
            )),
            NoopContext(),
        )

        self.assertTrue(response.success)
        self.assertIn("degraded-auction", judge.vault)

    def test_replication_and_full_state_sync_copy_primary_state(self):
        primary = make_judge(role="primary", address="primary:50051")
        backup = make_judge(role="backup", address="backup:50051")
        auction = pb2.Auction(
            auction_id="replicated-auction",
            title="Replicated Auction",
            version=7,
            bids={"buyer-a": 400.0},
        )
        primary.vault[auction.auction_id] = auction

        replication = backup.ReplicateSecret(
            pb2.ReplicationRequest(auction=auction),
            NoopContext(),
        )
        state = primary.SyncFullState(pb2.StateRequest(), NoopContext())

        self.assertTrue(replication.success)
        self.assertEqual(backup.vault["replicated-auction"].version, 7)
        self.assertTrue(state.ok)
        self.assertEqual(state.auctions[0].auction_id, "replicated-auction")

    def test_controller_elects_new_primary_after_current_primary_removed(self):
        controller = ControllerService()
        controller.RegisterNode(pb2.RegisterRequest(address="primary:50051"), NoopContext())
        controller.RegisterNode(pb2.RegisterRequest(address="backup:50051"), NoopContext())

        del controller.nodes["primary:50051"]
        controller.primary_address = None
        with mock.patch.object(controller, "NotifyPromotion") as notify:
            controller.ElectNewPrimary()

        self.assertEqual(controller.primary_address, "backup:50051")
        notify.assert_called_once_with("backup:50051")
