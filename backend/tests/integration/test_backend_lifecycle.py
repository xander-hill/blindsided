import grpc

from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc
from backend.tests.helpers import BackendTestCase, running_backend_stack


class BackendLifecycleTests(BackendTestCase):
    def test_full_open_bid_status_reveal_flow_over_grpc(self):
        with running_backend_stack() as stack:
            with grpc.insecure_channel(stack["auction_addr"]) as channel:
                stub = pb2_grpc.BlindSidedStub(channel)

                opened = stub.OpenAuction(pb2.OpenRequest(auction=pb2.Auction(
                    auction_id="integration-watch",
                    title="Integration Watch",
                    reserve_price=500.0,
                    bids={"opening": 250.0},
                )), timeout=5)
                hidden_status = stub.GetStatus(pb2.StatusRequest(
                    auction_id="integration-watch",
                ), timeout=5)
                bid = stub.PlaceSecretBid(pb2.BidRequest(
                    auction_id="integration-watch",
                    buyer_id="buyer-a",
                    amount=750.0,
                    expected_version=hidden_status.auction.version,
                ), timeout=5)
                post_bid_status = stub.GetStatus(pb2.StatusRequest(
                    auction_id="integration-watch",
                ), timeout=5)
                gavel = stub.DropTheGavel(pb2.GavelRequest(
                    auction_id="integration-watch",
                ), timeout=5)
                revealed_status = stub.GetStatus(pb2.StatusRequest(
                    auction_id="integration-watch",
                ), timeout=5)

        self.assertTrue(opened.ok)
        self.assertEqual(opened.auction_id, "integration-watch")
        self.assertTrue(hidden_status.ok)
        self.assertEqual(dict(hidden_status.auction.bids), {})
        self.assertTrue(bid.success)
        self.assertEqual(dict(post_bid_status.auction.bids), {})
        self.assertTrue(gavel.ok)
        self.assertTrue(revealed_status.ok)
        self.assertEqual(
            revealed_status.auction.state,
            pb2.AUCTION_STATE_REVEALED,
        )
        self.assertEqual(revealed_status.auction.bids["opening"], 250.0)
        self.assertEqual(revealed_status.auction.bids["buyer-a"], 750.0)
        self.assertTrue(revealed_status.auction.reserve_met)

    def test_live_stream_reports_opaque_update_then_revealed_update(self):
        with running_backend_stack() as stack:
            with grpc.insecure_channel(stack["auction_addr"]) as channel:
                stub = pb2_grpc.BlindSidedStub(channel)
                stub.OpenAuction(pb2.OpenRequest(auction=pb2.Auction(
                    auction_id="streamed-auction",
                    title="Streamed Auction",
                    reserve_price=300.0,
                    bids={"opening": 125.0},
                )), timeout=5)

                first_stream = stub.JoinLiveAuction(pb2.AuctionRequest(
                    auction_id="streamed-auction",
                    user_id="watcher",
                ), timeout=5)
                opaque_update = next(first_stream)

                stub.DropTheGavel(pb2.GavelRequest(
                    auction_id="streamed-auction",
                ), timeout=5)

                reveal_stream = stub.JoinLiveAuction(pb2.AuctionRequest(
                    auction_id="streamed-auction",
                    user_id="watcher",
                ), timeout=5)
                reveal_update = next(reveal_stream)

        self.assertEqual(opaque_update.state, pb2.AUCTION_STATE_OPEN)
        self.assertEqual(opaque_update.bidder_count, 1)
        self.assertEqual(opaque_update.low_range, 125.0)
        self.assertEqual(opaque_update.high_range, 125.0)
        self.assertEqual(reveal_update.state, pb2.AUCTION_STATE_REVEALED)
        self.assertEqual(reveal_update.final_price, 125.0)
        self.assertEqual(reveal_update.winner_id, "opening")
