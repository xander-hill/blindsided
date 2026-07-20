from concurrent import futures
from contextlib import contextmanager
import tempfile
import threading
from unittest import mock

import grpc

from blindsided.controller.service import ControllerService, ReplicaSyncStatus
from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc
from backend.tests.helpers import (
    BackendTestCase,
    NoopContext,
    active_bid,
    future_timestamp,
    free_port,
    make_judge,
    running_backend_stack,
)


@contextmanager
def running_storage_pair():
    with tempfile.TemporaryDirectory() as temp_dir:
        backup_port = free_port()
        backup_address = f"127.0.0.1:{backup_port}"
        backup_state_path = f"{temp_dir}/backup-state.pb"
        primary_state_path = f"{temp_dir}/primary-state.pb"
        backup = make_judge(
            role="backup",
            address=backup_address,
            state_file_path=backup_state_path,
        )
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
        pb2_grpc.add_StorageReplicaServiceServicer_to_server(backup, server)
        server.add_insecure_port(backup_address)
        server.start()
        try:
            with grpc.insecure_channel(backup_address) as channel:
                grpc.channel_ready_future(channel).result(timeout=5)
            primary = make_judge(
                role="primary",
                address="primary-direct:50051",
                synchronous_backup_address=backup_address,
                state_file_path=primary_state_path,
                use_test_coordinator=False,
            )
            yield {
                "primary": primary,
                "backup": backup,
                "primary_state_path": primary_state_path,
                "backup_state_path": backup_state_path,
            }
        finally:
            server.stop(0).wait(timeout=5)


class DistributedBehaviorTests(BackendTestCase):
    def _mutation_case(self, mutation_name, request_id):
        auction_id = f"{mutation_name}-auction"
        if mutation_name == "create":
            return None, pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_CREATE,
                request_id=request_id,
                auction=pb2.Auction(
                    auction_id=auction_id,
                    seller_id="seller-a",
                    reserve_price=500.0,
                    ends_at=future_timestamp(),
                ),
            )
        base = pb2.Auction(
            auction_id=auction_id,
            seller_id="seller-a",
            reserve_price=500.0,
            version=1,
            state=pb2.AUCTION_STATE_OPEN,
            next_bid_sequence=2,
            bids={"buyer-a": active_bid(700.0, 1)},
            ends_at=future_timestamp(),
        )
        if mutation_name == "bid":
            request = pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                request_id=request_id,
                bidder_id="buyer-b",
                expected_version=1,
                auction=pb2.Auction(
                    auction_id=auction_id,
                    bids={"buyer-b": active_bid(800.0)},
                ),
            )
        elif mutation_name == "withdraw":
            request = pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID,
                request_id=request_id,
                bidder_id="buyer-a",
                expected_version=1,
                auction=pb2.Auction(auction_id=auction_id),
            )
        else:
            base.ClearField("ends_at")
            request = pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
                request_id=request_id,
                expected_version=1,
                auction=pb2.Auction(auction_id=auction_id, seller_id="seller-a"),
            )
        return base, request

    def _install_committed_base(self, pair, base):
        if base is None:
            return
        for replica in (pair["primary"], pair["backup"]):
            stored = pb2.Auction()
            stored.CopyFrom(base)
            replica.auction_store[base.auction_id] = stored

    def test_two_replica_happy_path_prepares_and_commits_over_grpc(self):
        with running_storage_pair() as pair:
            primary = pair["primary"]
            backup = pair["backup"]

            response = primary.ApplyAuctionMutation(
                pb2.AuctionMutationRequest(
                    mutation_type=pb2.AUCTION_MUTATION_TYPE_CREATE,
                    request_id="two-replica-create",
                    auction=pb2.Auction(
                        auction_id="two-replica-auction",
                        seller_id="seller-a",
                        title="Two Replica Auction",
                        reserve_price=500.0,
                        ends_at=future_timestamp(),
                    ),
                ),
                NoopContext(),
            )

            primary_snapshot = pb2.StorageSnapshot()
            with open(pair["primary_state_path"], "rb") as state_file:
                primary_snapshot.ParseFromString(state_file.read())
            backup_snapshot = pb2.StorageSnapshot()
            with open(pair["backup_state_path"], "rb") as state_file:
                backup_snapshot.ParseFromString(state_file.read())

        self.assertTrue(response.success)
        self.assertEqual(response.current_version, 1)
        self.assertEqual(
            primary.auction_store["two-replica-auction"],
            backup.auction_store["two-replica-auction"],
        )
        self.assertIn("two-replica-create", primary.idempotency_records)
        self.assertIn("two-replica-create", backup.idempotency_records)
        self.assertEqual(primary.pending_backup_commits, {})
        self.assertEqual(backup.prepared_mutations, {})
        self.assertEqual(len(primary_snapshot.auctions), 1)
        self.assertEqual(len(primary_snapshot.idempotency_records), 1)
        self.assertEqual(len(primary_snapshot.pending_backup_commits), 0)
        self.assertEqual(len(backup_snapshot.auctions), 1)
        self.assertEqual(len(backup_snapshot.idempotency_records), 1)
        self.assertEqual(len(backup_snapshot.prepared_mutations), 0)

    def test_two_replica_aborts_after_prepare_when_primary_decision_persist_fails(self):
        with running_storage_pair() as pair:
            primary = pair["primary"]
            backup = pair["backup"]

            with mock.patch.object(
                primary,
                "_persist_state_to_disk",
                side_effect=OSError("primary disk unavailable"),
            ):
                response = primary.ApplyAuctionMutation(
                    pb2.AuctionMutationRequest(
                        mutation_type=pb2.AUCTION_MUTATION_TYPE_CREATE,
                        request_id="failed-primary-decision",
                        auction=pb2.Auction(
                            auction_id="uncommitted-auction",
                            seller_id="seller-a",
                            title="Must Not Commit",
                            reserve_price=500.0,
                            ends_at=future_timestamp(),
                        ),
                    ),
                    NoopContext(),
                )

            backup_snapshot = pb2.StorageSnapshot()
            with open(pair["backup_state_path"], "rb") as state_file:
                backup_snapshot.ParseFromString(state_file.read())

        self.assertFalse(response.success)
        self.assertEqual(response.current_version, 0)
        self.assertEqual(
            response.failure_reason,
            pb2.MUTATION_FAILURE_REASON_REPLICATION_FAILED,
        )
        self.assertNotIn("uncommitted-auction", primary.auction_store)
        self.assertNotIn("failed-primary-decision", primary.idempotency_records)
        self.assertNotIn("failed-primary-decision", primary.pending_backup_commits)
        self.assertNotIn("uncommitted-auction", backup.auction_store)
        self.assertNotIn("failed-primary-decision", backup.idempotency_records)
        self.assertNotIn("failed-primary-decision", backup.prepared_mutations)
        self.assertIn("failed-primary-decision", backup.aborted_mutations)
        self.assertEqual(len(backup_snapshot.auctions), 0)
        self.assertEqual(len(backup_snapshot.idempotency_records), 0)
        self.assertEqual(len(backup_snapshot.prepared_mutations), 0)
        self.assertEqual(len(backup_snapshot.aborted_mutations), 1)

    def test_two_replica_recovers_lost_commit_acknowledgement_on_idempotent_retry(self):
        with running_storage_pair() as pair:
            primary = pair["primary"]
            backup = pair["backup"]
            request = pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_CREATE,
                request_id="lost-backup-ack",
                auction=pb2.Auction(
                    auction_id="durably-decided-auction",
                    seller_id="seller-a",
                    title="Durably Decided Auction",
                    reserve_price=500.0,
                    ends_at=future_timestamp(),
                ),
            )

            def commit_backup_then_lose_response(request_id):
                decision = primary.pending_backup_commits[request_id]
                with grpc.insecure_channel(decision.backup_address) as channel:
                    stub = pb2_grpc.StorageReplicaServiceStub(channel)
                    committed = stub.CommitPreparedMutation(
                        pb2.MutationDecisionRequest(
                            request_id=decision.request_id,
                            auction_id=decision.auction.auction_id,
                            primary_id=decision.primary_id,
                        ),
                        timeout=5,
                    )
                self.assertTrue(committed.success)
                self.assertEqual(committed.committed_version, 1)
                return False

            with mock.patch.object(
                primary,
                "_complete_pending_backup_commit",
                side_effect=commit_backup_then_lose_response,
            ):
                first = primary.ApplyAuctionMutation(request, NoopContext())

            pending_snapshot = pb2.StorageSnapshot()
            with open(pair["primary_state_path"], "rb") as state_file:
                pending_snapshot.ParseFromString(state_file.read())

            retry = primary.ApplyAuctionMutation(request, NoopContext())

            recovered_primary_snapshot = pb2.StorageSnapshot()
            with open(pair["primary_state_path"], "rb") as state_file:
                recovered_primary_snapshot.ParseFromString(state_file.read())
            backup_snapshot = pb2.StorageSnapshot()
            with open(pair["backup_state_path"], "rb") as state_file:
                backup_snapshot.ParseFromString(state_file.read())

        self.assertFalse(first.success)
        self.assertEqual(first.current_version, 1)
        self.assertEqual(
            first.failure_reason,
            pb2.MUTATION_FAILURE_REASON_ACKNOWLEDGEMENT_PENDING,
        )
        self.assertEqual(len(pending_snapshot.pending_backup_commits), 1)
        self.assertTrue(retry.success)
        self.assertTrue(retry.replayed)
        self.assertEqual(retry.current_version, 1)
        self.assertEqual(
            primary.auction_store["durably-decided-auction"],
            backup.auction_store["durably-decided-auction"],
        )
        self.assertNotIn("lost-backup-ack", primary.pending_backup_commits)
        self.assertEqual(len(recovered_primary_snapshot.pending_backup_commits), 0)
        self.assertEqual(len(backup_snapshot.prepared_mutations), 0)
        self.assertEqual(len(backup_snapshot.auctions), 1)
        self.assertEqual(len(backup_snapshot.idempotency_records), 1)

    def test_pending_backup_commit_completes_after_primary_restart(self):
        with running_storage_pair() as pair:
            primary = pair["primary"]
            request = self._mutation_case("create", "restart-pending")[1]
            with mock.patch.object(
                primary, "_complete_pending_backup_commit", return_value=False
            ):
                first = primary.ApplyAuctionMutation(request, NoopContext())

            restarted = make_judge(
                role="primary",
                address=primary.node_address,
                synchronous_backup_address=pair["backup"].node_address,
                state_file_path=pair["primary_state_path"],
                use_test_coordinator=False,
            )
            restarted._load_state_from_disk()
            retry = restarted.ApplyAuctionMutation(request, NoopContext())

            self.assertFalse(first.success)
            self.assertEqual(
                first.failure_reason,
                pb2.MUTATION_FAILURE_REASON_ACKNOWLEDGEMENT_PENDING,
            )
            self.assertTrue(retry.success)
            self.assertTrue(retry.replayed)
            self.assertEqual(retry.current_version, 1)
            self.assertEqual(restarted.pending_backup_commits, {})
            self.assertEqual(
                restarted.auction_store[request.auction.auction_id],
                pair["backup"].auction_store[request.auction.auction_id],
            )

    def test_prepared_mutation_can_commit_after_backup_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = f"{temp_dir}/backup.pb"
            backup = make_judge(role="backup", state_file_path=state_path)
            base, mutation = self._mutation_case("create", "restart-prepared")
            del base
            candidate = pb2.Auction()
            candidate.CopyFrom(mutation.auction)
            candidate.version = 1
            candidate.state = pb2.AUCTION_STATE_OPEN
            record = pb2.IdempotencyRecord(
                request_id=mutation.request_id,
                request_fingerprint=backup._request_fingerprint(
                    mutation, pb2.AUCTION_MUTATION_TYPE_CREATE
                ),
                response=pb2.AuctionMutationResponse(
                    success=True,
                    auction_id=candidate.auction_id,
                    current_version=1,
                ),
            )
            prepared = backup.PrepareAuctionMutation(
                pb2.PrepareMutationRequest(
                    request_id=mutation.request_id,
                    candidate_auction=candidate,
                    idempotency_record=record,
                    primary_id="primary:50051",
                ),
                NoopContext(),
            )
            restarted = make_judge(role="backup", state_file_path=state_path)
            restarted._load_state_from_disk()
            committed = restarted.CommitPreparedMutation(
                pb2.MutationDecisionRequest(
                    request_id=mutation.request_id,
                    auction_id=candidate.auction_id,
                    primary_id="primary:50051",
                ),
                NoopContext(),
            )

        self.assertTrue(prepared.success)
        self.assertTrue(committed.success)
        self.assertEqual(committed.committed_version, 1)
        self.assertIn(candidate.auction_id, restarted.auction_store)
        self.assertIn(mutation.request_id, restarted.idempotency_records)
        self.assertNotIn(mutation.request_id, restarted.prepared_mutations)

    def test_mutation_fails_when_no_synchronous_backup_is_designated(self):
        primary = make_judge(role="primary", use_test_coordinator=False)
        request = self._mutation_case("create", "no-backup")[1]
        response = primary.ApplyAuctionMutation(request, NoopContext())
        self.assertFalse(response.success)
        self.assertEqual(response.current_version, 0)
        self.assertEqual(primary.auction_store, {})
        self.assertEqual(primary.idempotency_records, {})

    def test_mutation_fails_when_synchronous_backup_is_unreachable(self):
        primary = make_judge(
            role="primary",
            synchronous_backup_address="127.0.0.1:1",
            use_test_coordinator=False,
        )
        request = self._mutation_case("create", "unreachable-backup")[1]
        response = primary.ApplyAuctionMutation(request, NoopContext())
        self.assertFalse(response.success)
        self.assertEqual(response.current_version, 0)
        self.assertEqual(primary.auction_store, {})
        self.assertEqual(primary.idempotency_records, {})

    def test_wrong_prepare_acknowledgement_version_prevents_commit(self):
        primary = make_judge(
            role="primary",
            synchronous_backup_address="backup:50051",
            use_test_coordinator=False,
        )
        request = self._mutation_case("create", "wrong-prepare-version")[1]
        with mock.patch.object(
            primary,
            "_prepare_on_synchronous_backup",
            return_value=False,
        ), mock.patch.object(primary, "_record_commit_decision") as record:
            response = primary.ApplyAuctionMutation(request, NoopContext())
        self.assertFalse(response.success)
        record.assert_not_called()
        self.assertEqual(primary.pending_backup_commits, {})

    def test_wrong_commit_acknowledgement_version_remains_pending(self):
        with running_storage_pair() as pair:
            primary = pair["primary"]
            request = self._mutation_case("create", "wrong-commit-version")[1]
            with mock.patch.object(
                primary,
                "_complete_pending_backup_commit",
                return_value=False,
            ):
                response = primary.ApplyAuctionMutation(request, NoopContext())
        self.assertFalse(response.success)
        self.assertEqual(
            response.failure_reason,
            pb2.MUTATION_FAILURE_REASON_ACKNOWLEDGEMENT_PENDING,
        )
        self.assertIn(request.request_id, primary.pending_backup_commits)
        self.assertIn(request.request_id, primary.idempotency_records)

    def test_acknowledged_mutation_survives_backup_promotion(self):
        with running_storage_pair() as pair:
            request = self._mutation_case("create", "promoted-replay")[1]
            acknowledged = pair["primary"].ApplyAuctionMutation(request, NoopContext())
            promoted = pair["backup"].PromoteToPrimary(
                pb2.PromotionRequest(new_role="primary"), NoopContext()
            )
            retry = pair["backup"].ApplyAuctionMutation(request, NoopContext())
        self.assertTrue(acknowledged.success)
        self.assertTrue(promoted.success)
        self.assertTrue(retry.success)
        self.assertTrue(retry.replayed)
        self.assertEqual(retry.current_version, acknowledged.current_version)
        self.assertEqual(pair["backup"].auction_store[request.auction.auction_id].version, 1)

    def test_predecision_replication_failure_preserves_each_mutation_state(self):
        for mutation_name in ("create", "bid", "withdraw", "reveal"):
            with self.subTest(mutation=mutation_name), running_storage_pair() as pair:
                base, request = self._mutation_case(
                    mutation_name, f"failed-{mutation_name}"
                )
                self._install_committed_base(pair, base)
                before = pb2.Auction()
                backup_before = pb2.Auction()
                if base is not None:
                    before.CopyFrom(pair["primary"].auction_store[base.auction_id])
                    backup_before.CopyFrom(pair["backup"].auction_store[base.auction_id])
                with mock.patch.object(
                    pair["primary"], "_prepare_on_synchronous_backup", return_value=False
                ):
                    response = pair["primary"].ApplyAuctionMutation(request, NoopContext())
                self.assertFalse(response.success)
                self.assertNotIn(request.request_id, pair["primary"].idempotency_records)
                if base is None:
                    self.assertNotIn(request.auction.auction_id, pair["primary"].auction_store)
                    self.assertNotIn(request.auction.auction_id, pair["backup"].auction_store)
                else:
                    self.assertEqual(pair["primary"].auction_store[base.auction_id], before)
                    self.assertEqual(pair["backup"].auction_store[base.auction_id], backup_before)
                self.assertNotIn(request.request_id, pair["backup"].idempotency_records)

    def test_acknowledged_success_commits_each_mutation_identically(self):
        for mutation_name in ("create", "bid", "withdraw", "reveal"):
            with self.subTest(mutation=mutation_name), running_storage_pair() as pair:
                base, request = self._mutation_case(
                    mutation_name, f"successful-{mutation_name}"
                )
                self._install_committed_base(pair, base)
                response = pair["primary"].ApplyAuctionMutation(request, NoopContext())
                auction_id = request.auction.auction_id
                self.assertTrue(response.success)
                self.assertEqual(
                    pair["primary"].auction_store[auction_id],
                    pair["backup"].auction_store[auction_id],
                )
                self.assertEqual(
                    pair["primary"].idempotency_records[request.request_id],
                    pair["backup"].idempotency_records[request.request_id],
                )

    def test_concurrent_mutations_remain_consistent_across_primary_and_backup(self):
        bidder_count = 6
        with running_storage_pair() as pair:
            create = self._mutation_case("create", "concurrent-create")[1]
            self.assertTrue(pair["primary"].ApplyAuctionMutation(create, NoopContext()).success)
            barrier = threading.Barrier(bidder_count)
            results = []
            results_lock = threading.Lock()

            def bid(index):
                barrier.wait()
                bidder = f"buyer-{index}"
                while True:
                    version = pair["primary"].auction_store[create.auction.auction_id].version
                    response = pair["primary"].ApplyAuctionMutation(
                        pb2.AuctionMutationRequest(
                            mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                            request_id=f"concurrent-bid-{index}",
                            bidder_id=bidder,
                            expected_version=version,
                            auction=pb2.Auction(
                                auction_id=create.auction.auction_id,
                                bids={bidder: active_bid(600.0 + index)},
                            ),
                        ),
                        NoopContext(),
                    )
                    if response.success:
                        with results_lock:
                            results.append(response)
                        return

            threads = [threading.Thread(target=bid, args=(i,)) for i in range(bidder_count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            primary_auction = pair["primary"].auction_store[create.auction.auction_id]
            backup_auction = pair["backup"].auction_store[create.auction.auction_id]
            self.assertEqual(len(results), bidder_count)
            self.assertEqual(primary_auction, backup_auction)
            self.assertEqual(primary_auction.version, 1 + bidder_count)
            self.assertEqual(len(primary_auction.bids), bidder_count)
            self.assertEqual(
                sorted(b.acceptance_order for b in primary_auction.bids.values()),
                list(range(1, bidder_count + 1)),
            )
            self.assertEqual(
                pair["primary"].idempotency_records,
                pair["backup"].idempotency_records,
            )

    def _coordinate_to_backup(self, primary, backup):
        def coordinate(
            request_id,
            candidate,
            idempotency_record,
            success_response,
            previous_version,
        ):
            prepared = backup.PrepareAuctionMutation(
                pb2.PrepareMutationRequest(
                    request_id=request_id,
                    candidate_auction=candidate,
                    idempotency_record=idempotency_record,
                    primary_id=primary.node_address,
                ),
                NoopContext(),
            )
            if not prepared.success:
                return pb2.AuctionMutationResponse(
                    success=False,
                    current_version=previous_version,
                    failure_reason=pb2.MUTATION_FAILURE_REASON_REPLICATION_FAILED,
                )
            primary.auction_store[candidate.auction_id] = candidate
            primary.idempotency_records[request_id] = idempotency_record
            committed = backup.CommitPreparedMutation(
                pb2.MutationDecisionRequest(
                    request_id=request_id,
                    auction_id=candidate.auction_id,
                    primary_id=primary.node_address,
                ),
                NoopContext(),
            )
            return success_response if committed.success else pb2.AuctionMutationResponse(
                success=False,
                current_version=candidate.version,
                failure_reason=pb2.MUTATION_FAILURE_REASON_ACKNOWLEDGEMENT_PENDING,
            )

        return coordinate

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

    def test_primary_storage_rejects_mutation_when_peer_is_unreachable(self):
        judge = make_judge(
            role="primary",
            address="primary_address:50051",
            synchronous_backup_address="unreachable-peer:50051",
            use_test_coordinator=False,
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                request_id="unreachable-create",
                auction=pb2.Auction(
                    auction_id="unreplicated-auction",
                    seller_id="seller-a",
                    title="Unreplicated Auction",
                    reserve_price=100.0,
                    ends_at=future_timestamp(),
                ),
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertEqual(
            response.failure_reason,
            pb2.MUTATION_FAILURE_REASON_REPLICATION_FAILED,
        )
        self.assertIn("preparation failed", response.message)
        self.assertNotIn("unreplicated-auction", judge.auction_store)

    def test_existing_auction_mutation_is_not_committed_without_peer_acknowledgement(self):
        judge = make_judge(
            role="primary",
            address="primary_address:50051",
            synchronous_backup_address="unreachable-peer:50051",
            use_test_coordinator=False,
        )
        judge.auction_store["existing-auction"] = pb2.Auction(
            auction_id="existing-auction",
            version=4,
            state=pb2.AUCTION_STATE_OPEN,
            next_bid_sequence=2,
            bids={"buyer-a": active_bid(250.0, 1)},
            ends_at=future_timestamp(),
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                auction=pb2.Auction(
                    auction_id="existing-auction",
                    bids={"buyer-b": active_bid(500.0)},
                ),
                bidder_id="buyer-b",
                expected_version=4,
                request_id="existing-auction-unacknowledged-bid",
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertEqual(
            response.failure_reason,
            pb2.MUTATION_FAILURE_REASON_REPLICATION_FAILED,
        )
        self.assertEqual(response.current_version, 4)
        self.assertEqual(judge.auction_store["existing-auction"].version, 4)
        self.assertEqual(
            judge.auction_store["existing-auction"].bids["buyer-a"],
            active_bid(250.0, 1),
        )
        self.assertNotIn("buyer-b", judge.auction_store["existing-auction"].bids)
        self.assertNotIn(
            "existing-auction-unacknowledged-bid",
            judge.idempotency_records,
        )

    def test_full_state_sync_exports_primary_state(self):
        primary = make_judge(role="primary", address="primary_address:50051")
        auction = pb2.Auction(
            auction_id="synced-auction",
            title="Synced Auction",
            version=7,
            bids={"buyer-a": active_bid(400.0, 1)},
        )
        primary.auction_store[auction.auction_id] = auction

        state = primary.SyncFullState(pb2.StateRequest(), NoopContext())

        self.assertTrue(state.ok)
        self.assertEqual(state.auctions[0].auction_id, "synced-auction")
        self.assertEqual(state.auctions[0].version, 7)

    def test_retry_after_backup_acknowledgement_replays_on_promoted_backup(self):
        primary = make_judge(role="primary", address="primary:50051")
        backup = make_judge(role="backup", address="backup:50051")
        primary.auction_store["idem-failover"] = pb2.Auction(
            auction_id="idem-failover",
            version=1,
            next_bid_sequence=1,
        )
        backup.auction_store["idem-failover"] = pb2.Auction()
        backup.auction_store["idem-failover"].CopyFrom(
            primary.auction_store["idem-failover"]
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
            "_coordinate_synchronous_commit",
            side_effect=self._coordinate_to_backup(primary, backup),
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
            judge = make_judge(role="primary", state_file_path=state_path)
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
            recovered = make_judge(role="primary", state_file_path=state_path)
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
        primary = make_judge(role="primary", address="primary:50051")
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
        backup.PromoteToPrimary(
            pb2.PromotionRequest(new_role="primary"),
            NoopContext(),
        )
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
        backup.auction_store["idem-failover-conflict"] = pb2.Auction()
        backup.auction_store["idem-failover-conflict"].CopyFrom(
            primary.auction_store["idem-failover-conflict"]
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
            "_coordinate_synchronous_commit",
            side_effect=self._coordinate_to_backup(primary, backup),
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
        controller.nodes["backup:50051"].sync_status = ReplicaSyncStatus.SYNCHRONIZED

        del controller.nodes["primary_address:50051"]
        controller.primary_address = None
        with mock.patch.object(controller, "_notify_promotion") as notify:
            controller._elect_new_primary()

        self.assertEqual(controller.primary_address, "backup:50051")
        notify.assert_called_once_with("backup:50051")
