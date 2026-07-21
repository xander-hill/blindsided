from concurrent import futures

import grpc

from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc
from backend.tests.distributed.test_concurrency_and_replication import (
    running_storage_pair,
)
from backend.tests.helpers import (
    BackendTestCase,
    NoopContext,
    active_bid,
    free_port,
    make_judge,
)


class FailoverInFlightRequestTests(BackendTestCase):
    def test_response_lost_after_commit_replays_original_result_after_failover(self):
        with running_storage_pair() as pair:
            for replica in (pair["primary"], pair["backup"]):
                replica.auction_store["auction-1"] = pb2.Auction(
                    auction_id="auction-1",
                    version=1,
                    state=pb2.AUCTION_STATE_OPEN,
                    next_bid_sequence=1,
                )
            request = pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                request_id="lost-response-bid",
                bidder_id="buyer-a",
                expected_version=1,
                epoch=1,
                auction=pb2.Auction(
                    auction_id="auction-1",
                    bids={"buyer-a": active_bid(250)},
                ),
            )

            committed_response = pair["primary"].ApplyAuctionMutation(
                request,
                NoopContext(),
            )
            promoted = pair["backup"]
            promotion = promoted.BeginPrimaryPromotion(
                pb2.BeginPrimaryPromotionRequest(epoch=2),
                NoopContext(),
            )
            activation = promoted.CompletePrimaryPromotion(
                pb2.CompletePrimaryPromotionRequest(
                    epoch=2,
                    backup_address="replacement-backup:50051",
                ),
                NoopContext(),
            )
            retry = pb2.AuctionMutationRequest()
            retry.CopyFrom(request)
            retry.epoch = 2
            replay = promoted.ApplyAuctionMutation(retry, NoopContext())

        self.assertTrue(committed_response.success)
        self.assertTrue(promotion.accepted)
        self.assertTrue(activation.success)
        self.assertTrue(replay.success)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.current_version, committed_response.current_version)
        self.assertEqual(promoted.auction_store["auction-1"].version, 2)
        self.assertEqual(promoted.auction_store["auction-1"].next_bid_sequence, 2)
        self.assertEqual(len(promoted.auction_store["auction-1"].bids), 1)
        self.assertEqual(list(promoted.idempotency_records), ["lost-response-bid"])

    def test_primary_failure_after_prepare_allows_safe_retry_after_failover(self):
        promoted = make_judge(
            role="backup",
            address="promoted:50051",
            use_test_coordinator=False,
        )
        promoted.current_epoch = 1
        promoted.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            version=1,
            state=pb2.AUCTION_STATE_OPEN,
            next_bid_sequence=1,
        )
        original = pb2.AuctionMutationRequest(
            mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
            request_id="prepared-before-failure",
            bidder_id="buyer-a",
            expected_version=1,
            epoch=1,
            auction=pb2.Auction(
                auction_id="auction-1",
                bids={"buyer-a": active_bid(300)},
            ),
        )
        candidate = pb2.Auction(
            auction_id="auction-1",
            version=2,
            state=pb2.AUCTION_STATE_OPEN,
            next_bid_sequence=2,
            bids={"buyer-a": active_bid(300, 1)},
        )
        prepared_response = pb2.AuctionMutationResponse(
            success=True,
            current_version=2,
            auction_id="auction-1",
        )
        record = pb2.IdempotencyRecord(
            request_id=original.request_id,
            request_fingerprint=promoted._request_fingerprint(
                original,
                pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
            ),
            response=prepared_response,
        )
        prepared = promoted.PrepareAuctionMutation(
            pb2.PrepareMutationRequest(
                request_id=original.request_id,
                candidate_auction=candidate,
                idempotency_record=record,
                primary_id="failed-primary:50051",
                epoch=1,
            ),
            NoopContext(),
        )

        promotion = promoted.BeginPrimaryPromotion(
            pb2.BeginPrimaryPromotionRequest(epoch=2),
            NoopContext(),
        )
        replacement_address = f"127.0.0.1:{free_port()}"
        replacement = make_judge(
            role="backup",
            address=replacement_address,
        )
        replacement.current_epoch = 2
        replacement.auction_store["auction-1"] = pb2.Auction()
        replacement.auction_store["auction-1"].CopyFrom(
            promoted.auction_store["auction-1"]
        )
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        pb2_grpc.add_StorageReplicaServiceServicer_to_server(replacement, server)
        server.add_insecure_port(replacement_address)
        server.start()
        try:
            with grpc.insecure_channel(replacement_address) as channel:
                grpc.channel_ready_future(channel).result(timeout=5)
            activation = promoted.CompletePrimaryPromotion(
                pb2.CompletePrimaryPromotionRequest(
                    epoch=2,
                    backup_address=replacement_address,
                ),
                NoopContext(),
            )
            retry = pb2.AuctionMutationRequest()
            retry.CopyFrom(original)
            retry.epoch = 2
            response = promoted.ApplyAuctionMutation(retry, NoopContext())
        finally:
            server.stop(0).wait(timeout=5)

        self.assertTrue(prepared.success)
        self.assertTrue(promotion.accepted)
        self.assertNotIn(original.request_id, promoted.prepared_mutations)
        self.assertTrue(activation.success)
        self.assertTrue(response.success)
        self.assertEqual(promoted.auction_store["auction-1"].version, 2)
        self.assertEqual(replacement.auction_store["auction-1"].version, 2)
        self.assertEqual(promoted.auction_store["auction-1"].next_bid_sequence, 2)
        self.assertEqual(replacement.auction_store["auction-1"].next_bid_sequence, 2)
        self.assertIn(original.request_id, promoted.idempotency_records)
        self.assertIn(original.request_id, replacement.idempotency_records)

