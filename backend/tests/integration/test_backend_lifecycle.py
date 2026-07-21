import grpc

from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc
from backend.tests.helpers import (
    BackendTestCase,
    future_timestamp,
    running_backend_stack,
    running_replicated_backend_stack,
)


class BackendLifecycleTests(BackendTestCase):
    def test_fresh_two_replica_cluster_commits_without_test_coordinator(self):
        with running_replicated_backend_stack() as stack:
            with grpc.insecure_channel(stack["auction_addr"]) as channel:
                opened = pb2_grpc.AuctionServiceStub(channel).CreateAuction(
                    pb2.CreateAuctionRequest(
                        seller_id="seller-real",
                        title="Real replicated startup",
                        reserve_price=500.0,
                        ends_at=future_timestamp(),
                        request_id="real-two-replica-create",
                    ),
                    timeout=5,
                )

        self.assertTrue(opened.ok, opened.message)
        self.assertEqual(
            stack["primary"].synchronous_backup_address,
            stack["backup"].node_address,
        )
        self.assertIn(opened.auction_id, stack["primary"].auction_store)
        self.assertIn(opened.auction_id, stack["backup"].auction_store)

    def test_full_open_bid_status_reveal_flow_over_grpc(self):
        with running_backend_stack() as stack:
            with grpc.insecure_channel(stack["auction_addr"]) as channel:
                stub = pb2_grpc.AuctionServiceStub(channel)

                opened = stub.CreateAuction(pb2.CreateAuctionRequest(
                    seller_id="seller-a",
                    title="Integration Watch",
                    reserve_price=500.0,
                    ends_at=future_timestamp(),
                    request_id="integration-lifecycle-create",
                ), timeout=5)
                auction_id = opened.auction_id
                opening_bid = stub.PlaceBid(pb2.BidRequest(
                    auction_id=auction_id,
                    bidder_id="opening",
                    amount=250.0,
                    expected_version=1,
                    request_id="integration-opening-bid",
                ), timeout=5)
                hidden_status = stub.GetAuction(pb2.GetAuctionRequest(
                    auction_id=auction_id,
                ), timeout=5)
                bid = stub.PlaceBid(pb2.BidRequest(
                    auction_id=auction_id,
                    bidder_id="buyer-a",
                    amount=750.0,
                    expected_version=1,
                    request_id="integration-buyer-bid",
                ), timeout=5)
                post_bid_status = stub.GetAuction(pb2.GetAuctionRequest(
                    auction_id=auction_id,
                    bidder_id="buyer-a",
                ), timeout=5)
                gavel = stub.RevealAuction(pb2.RevealAuctionRequest(
                    auction_id=auction_id,
                    seller_id="seller-a",
                    request_id="integration-reveal",
                ), timeout=5)
                revealed_status = stub.GetAuction(pb2.GetAuctionRequest(
                    auction_id=auction_id,
                ), timeout=5)

        self.assertTrue(opened.ok)
        self.assertNotEqual(opened.auction_id, "integration-watch")
        self.assertTrue(opening_bid.success)
        self.assertTrue(hidden_status.ok)
        self.assertEqual(hidden_status.auction.bidder_count, 1)
        self.assertTrue(bid.success)
        self.assertEqual(post_bid_status.auction.bidder_count, 2)
        self.assertTrue(post_bid_status.HasField("own_active_bid_amount"))
        self.assertEqual(post_bid_status.own_active_bid_amount, 750.0)
        self.assertFalse(hidden_status.HasField("own_active_bid_amount"))
        self.assertTrue(gavel.ok)
        self.assertTrue(revealed_status.ok)
        self.assertEqual(
            revealed_status.auction.state,
            pb2.AUCTION_STATE_REVEALED,
        )
        self.assertEqual(revealed_status.auction.bidder_count, 2)
        self.assertTrue(revealed_status.auction.HasField("result"))
        self.assertEqual(
            revealed_status.auction.result.outcome,
            pb2.AUCTION_OUTCOME_SUCCESSFUL_SALE,
        )
        self.assertEqual(
            revealed_status.auction.result.winning_bidder_id,
            "buyer-a",
        )
        self.assertEqual(revealed_status.auction.result.winning_amount, 750.0)
        public_fields = {field.name for field in revealed_status.auction.DESCRIPTOR.fields}
        self.assertNotIn("bids", public_fields)
        self.assertNotIn("reserve_price", public_fields)
        self.assertNotIn("reserve_met", public_fields)
        self.assertNotIn("winning_amount", public_fields)
        self.assertNotIn("winning_bidder_id", public_fields)

    def test_live_stream_reports_opaque_update_then_revealed_update(self):
        with running_backend_stack() as stack:
            with grpc.insecure_channel(stack["auction_addr"]) as channel:
                stub = pb2_grpc.AuctionServiceStub(channel)
                opened = stub.CreateAuction(pb2.CreateAuctionRequest(
                    seller_id="seller-a",
                    title="Streamed Auction",
                    reserve_price=300.0,
                    ends_at=future_timestamp(),
                    request_id="integration-stream-create",
                ), timeout=5)
                auction_id = opened.auction_id
                stub.PlaceBid(pb2.BidRequest(
                    auction_id=auction_id,
                    bidder_id="opening",
                    amount=125.0,
                    expected_version=1,
                    request_id="integration-stream-opening-bid",
                ), timeout=5)

                first_stream = stub.WatchAuction(pb2.AuctionRequest(
                    auction_id=auction_id,
                    user_id="watcher",
                ), timeout=5)
                opaque_update = next(first_stream)

                stub.RevealAuction(pb2.RevealAuctionRequest(
                    auction_id=auction_id,
                    seller_id="seller-a",
                    request_id="integration-stream-reveal",
                ), timeout=5)

                reveal_stream = stub.WatchAuction(pb2.AuctionRequest(
                    auction_id=auction_id,
                    user_id="watcher",
                ), timeout=5)
                reveal_update = next(reveal_stream)

        self.assertEqual(opaque_update.state, pb2.AUCTION_STATE_OPEN)
        self.assertEqual(opaque_update.bidder_count, 1)
        update_fields = {field.name for field in opaque_update.DESCRIPTOR.fields}
        self.assertNotIn("low_range", update_fields)
        self.assertNotIn("high_range", update_fields)
        self.assertNotIn("reserve_met", update_fields)
        self.assertNotIn("winning_amount", update_fields)
        self.assertNotIn("winning_bidder_id", update_fields)
        self.assertEqual(reveal_update.state, pb2.AUCTION_STATE_REVEALED)
        self.assertEqual(reveal_update.bidder_count, 1)
