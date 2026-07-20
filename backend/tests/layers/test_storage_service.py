import tempfile
from unittest import mock

import grpc
from google.protobuf import timestamp_pb2

from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import (
    BackendTestCase,
    ChannelContext,
    NoopContext,
    active_bid,
    future_timestamp,
    make_judge,
)


class PrepareRpcError(grpc.RpcError):
    pass


class StorageServiceTests(BackendTestCase):
    def _call_primary_prepare(self, judge, response=None, error=None):
        stub = mock.Mock()
        if error is not None:
            stub.PrepareAuctionMutation.side_effect = error
        else:
            stub.PrepareAuctionMutation.return_value = response
        with (
            mock.patch(
                "blindsided.storage.service.grpc.insecure_channel",
                return_value=ChannelContext(),
            ) as channel,
            mock.patch(
                "blindsided.storage.service.pb2_grpc.StorageReplicaServiceStub",
                return_value=stub,
            ),
        ):
            result = judge._prepare_on_synchronous_backup(
                "prepare-1",
                pb2.Auction(
                    auction_id="auction-1",
                    version=5,
                    state=pb2.AUCTION_STATE_OPEN,
                ),
                pb2.IdempotencyRecord(request_id="prepare-1"),
            )
        return result, stub, channel

    def _record_primary_decision(self, judge):
        return judge._record_commit_decision(
            "prepare-1",
            pb2.Auction(
                auction_id="auction-1",
                version=5,
                state=pb2.AUCTION_STATE_OPEN,
            ),
            pb2.IdempotencyRecord(
                request_id="prepare-1",
                response=pb2.AuctionMutationResponse(
                    success=True,
                    current_version=5,
                    auction_id="auction-1",
                ),
            ),
        )

    def _call_primary_completion(self, judge, response=None, error=None):
        stub = mock.Mock()
        if error is not None:
            stub.CommitPreparedMutation.side_effect = error
        else:
            stub.CommitPreparedMutation.return_value = response
        with (
            mock.patch(
                "blindsided.storage.service.grpc.insecure_channel",
                return_value=ChannelContext(),
            ) as channel,
            mock.patch(
                "blindsided.storage.service.pb2_grpc.StorageReplicaServiceStub",
                return_value=stub,
            ),
        ):
            result = judge._complete_pending_backup_commit("prepare-1")
        return result, stub, channel

    def _prepare_request(
        self,
        *,
        request_id="prepare-1",
        primary_id="primary-1",
        auction_id="auction-1",
        version=1,
    ):
        return pb2.PrepareMutationRequest(
            request_id=request_id,
            primary_id=primary_id,
            candidate_auction=pb2.Auction(
                auction_id=auction_id,
                version=version,
                state=pb2.AUCTION_STATE_OPEN,
            ),
            idempotency_record=pb2.IdempotencyRecord(
                request_id=request_id,
                response=pb2.AuctionMutationResponse(
                    success=True,
                    current_version=version,
                    auction_id=auction_id,
                ),
            ),
        )

    def _decision_request(
        self,
        *,
        request_id="prepare-1",
        auction_id="auction-1",
        primary_id="primary-1",
    ):
        return pb2.MutationDecisionRequest(
            request_id=request_id,
            auction_id=auction_id,
            primary_id=primary_id,
        )

    def _coordinator_values(self):
        candidate = pb2.Auction(
            auction_id="auction-1",
            version=5,
            state=pb2.AUCTION_STATE_OPEN,
        )
        success = pb2.AuctionMutationResponse(
            success=True,
            current_version=5,
            auction_id="auction-1",
            message="Vault updated.",
        )
        record = pb2.IdempotencyRecord(
            request_id="prepare-1",
            response=success,
        )
        return candidate, record, success

    def _install_pending_idempotent_bid(self, judge):
        request = pb2.AuctionMutationRequest(
            mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
            request_id="pending-request",
            bidder_id="buyer-a",
            expected_version=1,
            auction=pb2.Auction(
                auction_id="auction-1",
                bids={"buyer-a": active_bid(250.0)},
            ),
        )
        committed = pb2.Auction(
            auction_id="auction-1",
            version=2,
            state=pb2.AUCTION_STATE_OPEN,
            next_bid_sequence=2,
            bids={"buyer-a": active_bid(250.0, 1)},
        )
        stored_response = pb2.AuctionMutationResponse(
            success=True,
            current_version=2,
            auction_id="auction-1",
            message="Vault updated.",
        )
        record = pb2.IdempotencyRecord(
            request_id="pending-request",
            request_fingerprint=judge._request_fingerprint(
                request,
                pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
            ),
            response=stored_response,
        )
        decision = pb2.CommitDecision(
            request_id="pending-request",
            primary_id=judge.node_address,
            backup_address="backup:50051",
        )
        decision.auction.CopyFrom(committed)
        decision.idempotency_record.CopyFrom(record)
        judge.auction_store["auction-1"] = committed
        judge.idempotency_records["pending-request"] = record
        judge.pending_backup_commits["pending-request"] = decision
        return request, stored_response

    def test_idempotent_retry_completes_pending_backup_commit_before_replaying_success(self):
        judge = make_judge(role="primary")
        request, stored_response = self._install_pending_idempotent_bid(judge)

        def complete(request_id):
            self.assertEqual(request_id, "pending-request")
            del judge.pending_backup_commits[request_id]
            return True

        with mock.patch.object(
            judge,
            "_complete_pending_backup_commit",
            side_effect=complete,
        ) as completion:
            response = judge.ApplyAuctionMutation(request, NoopContext())

        completion.assert_called_once_with("pending-request")
        self.assertTrue(response.success)
        self.assertTrue(response.replayed)
        self.assertEqual(response.current_version, stored_response.current_version)
        self.assertNotIn("pending-request", judge.pending_backup_commits)

    def test_idempotent_retry_remains_pending_when_backup_is_unavailable(self):
        judge = make_judge(role="primary")
        request, _ = self._install_pending_idempotent_bid(judge)

        with mock.patch.object(
            judge,
            "_complete_pending_backup_commit",
            return_value=False,
        ) as completion:
            response = judge.ApplyAuctionMutation(request, NoopContext())

        completion.assert_called_once_with("pending-request")
        self.assertFalse(response.success)
        self.assertFalse(response.replayed)
        self.assertEqual(response.current_version, 2)
        self.assertEqual(
            response.failure_reason,
            pb2.MUTATION_FAILURE_REASON_ACKNOWLEDGEMENT_PENDING,
        )
        self.assertIn("pending-request", judge.pending_backup_commits)

    def test_idempotency_conflict_does_not_attempt_pending_completion(self):
        judge = make_judge(role="primary")
        self._install_pending_idempotent_bid(judge)
        conflicting_request = pb2.AuctionMutationRequest(
            mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
            request_id="pending-request",
            bidder_id="buyer-a",
            expected_version=1,
            auction=pb2.Auction(
                auction_id="auction-1",
                bids={"buyer-a": active_bid(300.0)},
            ),
        )

        with mock.patch.object(judge, "_complete_pending_backup_commit") as completion:
            response = judge.ApplyAuctionMutation(
                conflicting_request,
                NoopContext(),
            )

        completion.assert_not_called()
        self.assertFalse(response.success)
        self.assertFalse(response.replayed)
        self.assertEqual(
            response.failure_reason,
            pb2.MUTATION_FAILURE_REASON_IDEMPOTENCY_CONFLICT,
        )
        self.assertIn("pending-request", judge.pending_backup_commits)

    def test_best_effort_primary_abort_uses_configured_backup_and_handles_rpc_failure(self):
        judge = make_judge(
            role="primary",
            address="primary.storage:50051",
            synchronous_backup_address="backup.storage:50051",
        )
        stub = mock.Mock()
        stub.AbortPreparedMutation.return_value = pb2.MutationDecisionResponse(
            success=True,
        )
        with (
            mock.patch(
                "blindsided.storage.service.grpc.insecure_channel",
                return_value=ChannelContext(),
            ) as channel,
            mock.patch(
                "blindsided.storage.service.pb2_grpc.StorageReplicaServiceStub",
                return_value=stub,
            ),
        ):
            success = judge._abort_on_synchronous_backup("prepare-1", "auction-1")

        self.assertTrue(success)
        channel.assert_called_once_with("backup.storage:50051")
        request = stub.AbortPreparedMutation.call_args.args[0]
        self.assertEqual(request.request_id, "prepare-1")
        self.assertEqual(request.auction_id, "auction-1")
        self.assertEqual(request.primary_id, "primary.storage:50051")

        stub.AbortPreparedMutation.side_effect = PrepareRpcError()
        with (
            mock.patch(
                "blindsided.storage.service.grpc.insecure_channel",
                return_value=ChannelContext(),
            ),
            mock.patch(
                "blindsided.storage.service.pb2_grpc.StorageReplicaServiceStub",
                return_value=stub,
            ),
        ):
            failure = judge._abort_on_synchronous_backup("prepare-1", "auction-1")
        self.assertFalse(failure)

    def test_coordinator_prepare_failure_aborts_without_recording_decision(self):
        judge = make_judge(role="primary", use_test_coordinator=False)
        candidate, record, original_success = self._coordinator_values()
        calls = []

        with (
            mock.patch.object(
                judge,
                "_prepare_on_synchronous_backup",
                side_effect=lambda *args: calls.append("prepare") or False,
            ),
            mock.patch.object(
                judge,
                "_abort_on_synchronous_backup",
                side_effect=lambda *args: calls.append("abort") or True,
            ),
            mock.patch.object(judge, "_record_commit_decision") as record_decision,
            mock.patch.object(judge, "_complete_pending_backup_commit") as complete,
        ):
            response = judge._coordinate_synchronous_commit(
                "prepare-1",
                candidate,
                record,
                original_success,
                previous_version=4,
            )

        self.assertEqual(calls, ["prepare", "abort"])
        record_decision.assert_not_called()
        complete.assert_not_called()
        self.assertFalse(response.success)
        self.assertEqual(response.current_version, 4)
        self.assertEqual(
            response.failure_reason,
            pb2.MUTATION_FAILURE_REASON_REPLICATION_FAILED,
        )

    def test_coordinator_decision_persist_failure_aborts_without_primary_commit(self):
        judge = make_judge(role="primary", use_test_coordinator=False)
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            version=4,
            state=pb2.AUCTION_STATE_OPEN,
        )
        committed_before = pb2.Auction()
        committed_before.CopyFrom(judge.auction_store["auction-1"])
        candidate, record, original_success = self._coordinator_values()
        calls = []

        with (
            mock.patch.object(
                judge,
                "_prepare_on_synchronous_backup",
                side_effect=lambda *args: calls.append("prepare") or True,
            ),
            mock.patch.object(
                judge,
                "_record_commit_decision",
                side_effect=lambda *args: calls.append("record") or False,
            ),
            mock.patch.object(
                judge,
                "_abort_on_synchronous_backup",
                side_effect=lambda *args: calls.append("abort") or True,
            ),
            mock.patch.object(judge, "_complete_pending_backup_commit") as complete,
        ):
            response = judge._coordinate_synchronous_commit(
                "prepare-1",
                candidate,
                record,
                original_success,
                previous_version=4,
            )

        self.assertEqual(calls, ["prepare", "record", "abort"])
        complete.assert_not_called()
        self.assertFalse(response.success)
        self.assertEqual(response.current_version, 4)
        self.assertEqual(response.failure_reason, pb2.MUTATION_FAILURE_REASON_REPLICATION_FAILED)
        self.assertEqual(judge.auction_store["auction-1"], committed_before)
        self.assertEqual(judge.idempotency_records, {})
        self.assertEqual(judge.pending_backup_commits, {})

    def test_coordinator_backup_commit_failure_leaves_durable_decision_pending(self):
        judge = make_judge(
            role="primary",
            synchronous_backup_address="backup:50051",
            use_test_coordinator=False,
        )
        candidate, record, original_success = self._coordinator_values()
        calls = []
        real_record = judge._record_commit_decision

        def record_decision(*args):
            calls.append("record")
            return real_record(*args)

        with (
            mock.patch.object(
                judge,
                "_prepare_on_synchronous_backup",
                side_effect=lambda *args: calls.append("prepare") or True,
            ),
            mock.patch.object(
                judge,
                "_record_commit_decision",
                side_effect=record_decision,
            ),
            mock.patch.object(
                judge,
                "_complete_pending_backup_commit",
                side_effect=lambda *args: calls.append("complete") or False,
            ),
            mock.patch.object(judge, "_abort_on_synchronous_backup") as abort,
        ):
            response = judge._coordinate_synchronous_commit(
                "prepare-1",
                candidate,
                record,
                original_success,
                previous_version=4,
            )

        self.assertEqual(calls, ["prepare", "record", "complete"])
        abort.assert_not_called()
        self.assertFalse(response.success)
        self.assertEqual(response.current_version, 5)
        self.assertEqual(
            response.failure_reason,
            pb2.MUTATION_FAILURE_REASON_ACKNOWLEDGEMENT_PENDING,
        )
        self.assertIn("prepare-1", judge.pending_backup_commits)
        self.assertEqual(judge.auction_store["auction-1"].version, 5)
        self.assertIn("prepare-1", judge.idempotency_records)

    def test_coordinator_success_runs_all_phases_in_order_and_returns_original_success(self):
        judge = make_judge(role="primary", use_test_coordinator=False)
        candidate, record, original_success = self._coordinator_values()
        calls = []

        with (
            mock.patch.object(
                judge,
                "_prepare_on_synchronous_backup",
                side_effect=lambda *args: calls.append("prepare") or True,
            ),
            mock.patch.object(
                judge,
                "_record_commit_decision",
                side_effect=lambda *args: calls.append("record") or True,
            ),
            mock.patch.object(
                judge,
                "_complete_pending_backup_commit",
                side_effect=lambda *args: calls.append("complete") or True,
            ),
            mock.patch.object(judge, "_abort_on_synchronous_backup") as abort,
        ):
            response = judge._coordinate_synchronous_commit(
                "prepare-1",
                candidate,
                record,
                original_success,
                previous_version=4,
            )

        self.assertEqual(calls, ["prepare", "record", "complete"])
        abort.assert_not_called()
        self.assertIs(response, original_success)

    def test_primary_prepare_returns_false_without_synchronous_backup(self):
        judge = make_judge(role="primary")

        result, stub, channel = self._call_primary_prepare(judge)

        self.assertFalse(result)
        channel.assert_not_called()
        stub.PrepareAuctionMutation.assert_not_called()

    def test_primary_prepare_returns_false_when_backup_rpc_is_unavailable(self):
        judge = make_judge(
            role="primary",
            synchronous_backup_address="backup:50051",
        )

        result, stub, channel = self._call_primary_prepare(
            judge,
            error=PrepareRpcError(),
        )

        self.assertFalse(result)
        channel.assert_called_once_with("backup:50051")
        stub.PrepareAuctionMutation.assert_called_once()

    def test_primary_prepare_returns_false_when_backup_rejects(self):
        judge = make_judge(
            role="primary",
            synchronous_backup_address="backup:50051",
        )

        result, _, _ = self._call_primary_prepare(
            judge,
            response=pb2.PrepareMutationResponse(
                success=False,
                prepared_version=5,
            ),
        )

        self.assertFalse(result)

    def test_primary_prepare_returns_false_for_wrong_acknowledged_version(self):
        judge = make_judge(
            role="primary",
            synchronous_backup_address="backup:50051",
        )

        result, _, _ = self._call_primary_prepare(
            judge,
            response=pb2.PrepareMutationResponse(
                success=True,
                prepared_version=4,
            ),
        )

        self.assertFalse(result)

    def test_primary_prepare_returns_true_for_expected_version_and_payload(self):
        judge = make_judge(
            role="primary",
            address="primary.storage:50051",
            synchronous_backup_address="backup:50051",
        )

        result, stub, _ = self._call_primary_prepare(
            judge,
            response=pb2.PrepareMutationResponse(
                success=True,
                prepared_version=5,
            ),
        )

        self.assertTrue(result)
        request = stub.PrepareAuctionMutation.call_args.args[0]
        self.assertEqual(request.request_id, "prepare-1")
        self.assertEqual(request.primary_id, "primary.storage:50051")
        self.assertEqual(request.candidate_auction.auction_id, "auction-1")
        self.assertEqual(request.candidate_auction.version, 5)
        self.assertTrue(request.HasField("idempotency_record"))
        self.assertEqual(request.idempotency_record.request_id, "prepare-1")

    def test_primary_records_durable_commit_decision_and_restores_after_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = f"{temp_dir}/auction-state.pb"
            judge = make_judge(
                role="primary",
                address="primary.storage:50051",
                synchronous_backup_address="backup.storage:50051",
                state_file_path=state_path,
            )
            candidate = pb2.Auction(
                auction_id="auction-1",
                title="Committed candidate",
                version=5,
                state=pb2.AUCTION_STATE_OPEN,
            )
            record = pb2.IdempotencyRecord(
                request_id="prepare-1",
                response=pb2.AuctionMutationResponse(
                    success=True,
                    current_version=5,
                    auction_id="auction-1",
                ),
            )

            result = judge._record_commit_decision(
                "prepare-1",
                candidate,
                record,
            )
            candidate.title = "Mutated input auction"
            record.response.message = "Mutated input record"

            snapshot = pb2.StorageSnapshot()
            with open(state_path, "rb") as state_file:
                snapshot.ParseFromString(state_file.read())
            recovered = make_judge(
                role="primary",
                synchronous_backup_address="backup.storage:50051",
                state_file_path=state_path,
            )
            recovered._load_state_from_disk()

        self.assertTrue(result)
        self.assertEqual(judge.auction_store["auction-1"].title, "Committed candidate")
        self.assertEqual(
            judge.idempotency_records["prepare-1"].response.message,
            "",
        )
        decision = judge.pending_backup_commits["prepare-1"]
        self.assertEqual(decision.request_id, "prepare-1")
        self.assertEqual(decision.primary_id, "primary.storage:50051")
        self.assertEqual(decision.backup_address, "backup.storage:50051")
        self.assertEqual(decision.auction.title, "Committed candidate")
        self.assertEqual(decision.idempotency_record.request_id, "prepare-1")
        self.assertEqual(len(snapshot.pending_backup_commits), 1)
        self.assertEqual(
            snapshot.pending_backup_commits[0].auction.title,
            "Committed candidate",
        )
        self.assertEqual(recovered.auction_store["auction-1"].version, 5)
        self.assertIn("prepare-1", recovered.idempotency_records)
        self.assertIn("prepare-1", recovered.pending_backup_commits)

    def test_primary_commit_decision_persist_failure_restores_all_collections(self):
        judge = make_judge(
            role="primary",
            synchronous_backup_address="backup.storage:50051",
        )
        previous_auction = pb2.Auction(
            auction_id="auction-1",
            title="Previous auction",
            version=4,
            state=pb2.AUCTION_STATE_OPEN,
        )
        previous_record = pb2.IdempotencyRecord(
            request_id="prepare-1",
            response=pb2.AuctionMutationResponse(
                success=True,
                current_version=4,
                auction_id="auction-1",
            ),
        )
        previous_decision = pb2.CommitDecision(
            request_id="prepare-1",
            primary_id="old-primary",
            backup_address="old-backup",
        )
        previous_decision.auction.CopyFrom(previous_auction)
        previous_decision.idempotency_record.CopyFrom(previous_record)
        judge.auction_store["auction-1"] = previous_auction
        judge.idempotency_records["prepare-1"] = previous_record
        judge.pending_backup_commits["prepare-1"] = previous_decision

        with mock.patch.object(
            judge,
            "_persist_state_to_disk",
            side_effect=OSError("disk unavailable"),
        ):
            result = judge._record_commit_decision(
                "prepare-1",
                pb2.Auction(
                    auction_id="auction-1",
                    title="Candidate auction",
                    version=5,
                    state=pb2.AUCTION_STATE_OPEN,
                ),
                pb2.IdempotencyRecord(
                    request_id="prepare-1",
                    response=pb2.AuctionMutationResponse(
                        success=True,
                        current_version=5,
                        auction_id="auction-1",
                    ),
                ),
            )

        self.assertFalse(result)
        self.assertEqual(judge.auction_store["auction-1"], previous_auction)
        self.assertEqual(judge.idempotency_records["prepare-1"], previous_record)
        self.assertEqual(
            judge.pending_backup_commits["prepare-1"],
            previous_decision,
        )

    def test_primary_completion_commits_backup_and_durably_clears_pending_decision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = f"{temp_dir}/auction-state.pb"
            judge = make_judge(
                role="primary",
                address="primary.storage:50051",
                synchronous_backup_address="configured-but-not-used:50051",
                state_file_path=state_path,
            )
            self.assertTrue(self._record_primary_decision(judge))
            judge.pending_backup_commits["prepare-1"].backup_address = (
                "decision-backup:50051"
            )

            result, stub, channel = self._call_primary_completion(
                judge,
                response=pb2.MutationDecisionResponse(
                    success=True,
                    committed_version=5,
                ),
            )

            snapshot = pb2.StorageSnapshot()
            with open(state_path, "rb") as state_file:
                snapshot.ParseFromString(state_file.read())

        self.assertTrue(result)
        channel.assert_called_once_with("decision-backup:50051")
        commit_request = stub.CommitPreparedMutation.call_args.args[0]
        self.assertEqual(commit_request.request_id, "prepare-1")
        self.assertEqual(commit_request.auction_id, "auction-1")
        self.assertEqual(commit_request.primary_id, "primary.storage:50051")
        self.assertNotIn("prepare-1", judge.pending_backup_commits)
        self.assertEqual(len(snapshot.pending_backup_commits), 0)
        self.assertEqual(judge.auction_store["auction-1"].version, 5)
        self.assertIn("prepare-1", judge.idempotency_records)

    def test_primary_completion_keeps_pending_decision_for_failed_backup_response(self):
        for response in (
            pb2.MutationDecisionResponse(success=False, committed_version=5),
            pb2.MutationDecisionResponse(success=True, committed_version=4),
        ):
            with self.subTest(response=response):
                judge = make_judge(
                    role="primary",
                    synchronous_backup_address="backup:50051",
                )
                self.assertTrue(self._record_primary_decision(judge))

                result, _, _ = self._call_primary_completion(judge, response=response)

                self.assertFalse(result)
                self.assertIn("prepare-1", judge.pending_backup_commits)
                self.assertEqual(judge.auction_store["auction-1"].version, 5)
                self.assertIn("prepare-1", judge.idempotency_records)

    def test_primary_completion_restores_pending_decision_if_removal_persist_fails(self):
        judge = make_judge(
            role="primary",
            synchronous_backup_address="backup:50051",
        )
        self.assertTrue(self._record_primary_decision(judge))
        decision_before = pb2.CommitDecision()
        decision_before.CopyFrom(judge.pending_backup_commits["prepare-1"])

        with mock.patch.object(
            judge,
            "_persist_state_to_disk",
            side_effect=OSError("disk unavailable"),
        ):
            result, _, _ = self._call_primary_completion(
                judge,
                response=pb2.MutationDecisionResponse(
                    success=True,
                    committed_version=5,
                ),
            )

        self.assertFalse(result)
        self.assertEqual(
            judge.pending_backup_commits["prepare-1"],
            decision_before,
        )
        self.assertEqual(judge.auction_store["auction-1"].version, 5)
        self.assertIn("prepare-1", judge.idempotency_records)

    def test_primary_completion_retries_after_lost_backup_response(self):
        judge = make_judge(
            role="primary",
            synchronous_backup_address="backup:50051",
        )
        self.assertTrue(self._record_primary_decision(judge))
        stub = mock.Mock()
        stub.CommitPreparedMutation.side_effect = [
            PrepareRpcError(),
            pb2.MutationDecisionResponse(success=True, committed_version=5),
        ]
        with (
            mock.patch(
                "blindsided.storage.service.grpc.insecure_channel",
                return_value=ChannelContext(),
            ),
            mock.patch(
                "blindsided.storage.service.pb2_grpc.StorageReplicaServiceStub",
                return_value=stub,
            ),
        ):
            first = judge._complete_pending_backup_commit("prepare-1")
            self.assertIn("prepare-1", judge.pending_backup_commits)
            retry = judge._complete_pending_backup_commit("prepare-1")

        self.assertFalse(first)
        self.assertTrue(retry)
        self.assertNotIn("prepare-1", judge.pending_backup_commits)
        self.assertEqual(stub.CommitPreparedMutation.call_count, 2)
        first_request = stub.CommitPreparedMutation.call_args_list[0].args[0]
        retry_request = stub.CommitPreparedMutation.call_args_list[1].args[0]
        self.assertEqual(retry_request, first_request)
        self.assertEqual(judge.auction_store["auction-1"].version, 5)
        self.assertIn("prepare-1", judge.idempotency_records)

    def test_pending_decision_clear_persistence_failure_remains_retryable(self):
        judge = make_judge(
            role="primary",
            synchronous_backup_address="backup:50051",
        )
        self.assertTrue(self._record_primary_decision(judge))
        stub = mock.Mock()
        stub.CommitPreparedMutation.return_value = pb2.MutationDecisionResponse(
            success=True,
            committed_version=5,
        )
        with (
            mock.patch(
                "blindsided.storage.service.grpc.insecure_channel",
                return_value=ChannelContext(),
            ),
            mock.patch(
                "blindsided.storage.service.pb2_grpc.StorageReplicaServiceStub",
                return_value=stub,
            ),
            mock.patch.object(
                judge,
                "_persist_state_to_disk",
                side_effect=[OSError("disk unavailable"), None],
            ),
        ):
            first = judge._complete_pending_backup_commit("prepare-1")
            self.assertIn("prepare-1", judge.pending_backup_commits)
            retry = judge._complete_pending_backup_commit("prepare-1")

        self.assertFalse(first)
        self.assertTrue(retry)
        self.assertEqual(stub.CommitPreparedMutation.call_count, 2)
        self.assertNotIn("prepare-1", judge.pending_backup_commits)
        self.assertEqual(judge.auction_store["auction-1"].version, 5)
        self.assertIn("prepare-1", judge.idempotency_records)

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

    def test_prepare_mutation_is_accepted_only_on_backup(self):
        judge = make_judge(role="primary")

        response = judge.PrepareAuctionMutation(
            self._prepare_request(),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertIn("only on backup", response.message)
        self.assertEqual(judge.prepared_mutations, {})

    def test_prepare_mutation_requires_request_primary_and_auction_ids(self):
        for missing_field in ("request_id", "primary_id", "auction_id"):
            with self.subTest(missing_field=missing_field):
                judge = make_judge(role="backup")
                values = {
                    "request_id": "prepare-1",
                    "primary_id": "primary-1",
                    "auction_id": "auction-1",
                }
                values[missing_field] = ""
                request = self._prepare_request(**values)
                if missing_field == "request_id":
                    request.idempotency_record.request_id = ""

                response = judge.PrepareAuctionMutation(request, NoopContext())

                self.assertFalse(response.success)
                self.assertIn(missing_field.replace("_", " "), response.message)
                self.assertEqual(judge.prepared_mutations, {})

    def test_prepare_mutation_requires_matching_idempotency_record_id(self):
        for record_id in (None, "different-request"):
            with self.subTest(record_id=record_id):
                judge = make_judge(role="backup")
                request = self._prepare_request()
                if record_id is None:
                    request.ClearField("idempotency_record")
                else:
                    request.idempotency_record.request_id = record_id

                response = judge.PrepareAuctionMutation(request, NoopContext())

                self.assertFalse(response.success)
                self.assertIn("must match", response.message)
                self.assertEqual(judge.prepared_mutations, {})

    def test_prepare_mutation_validates_candidate_committed_state(self):
        judge = make_judge(role="backup")
        request = self._prepare_request(version=1)
        request.candidate_auction.state = pb2.AUCTION_STATE_REVEALED

        response = judge.PrepareAuctionMutation(request, NoopContext())

        self.assertFalse(response.success)
        self.assertIn("committed result", response.message)
        self.assertEqual(judge.prepared_mutations, {})

    def test_prepare_mutation_validates_candidate_against_backup_version(self):
        judge = make_judge(role="backup")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            version=4,
            state=pb2.AUCTION_STATE_OPEN,
        )

        response = judge.PrepareAuctionMutation(
            self._prepare_request(version=6),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertEqual(response.prepared_version, 4)
        self.assertIn("current committed version", response.message)
        self.assertEqual(judge.prepared_mutations, {})

    def test_prepare_mutation_stores_copy_without_modifying_committed_state(self):
        judge = make_judge(role="backup")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            title="Committed title",
            version=4,
            state=pb2.AUCTION_STATE_OPEN,
        )
        committed_before = pb2.Auction()
        committed_before.CopyFrom(judge.auction_store["auction-1"])
        request = self._prepare_request(version=5)
        request.candidate_auction.title = "Candidate title"

        response = judge.PrepareAuctionMutation(request, NoopContext())
        request.candidate_auction.title = "Changed after prepare"

        self.assertTrue(response.success)
        self.assertEqual(response.prepared_version, 5)
        self.assertEqual(
            judge.prepared_mutations["prepare-1"].candidate_auction.title,
            "Candidate title",
        )
        self.assertEqual(judge.auction_store["auction-1"], committed_before)
        self.assertEqual(judge.idempotency_records, {})

    def test_identical_repeated_prepare_returns_success_without_duplicate_state(self):
        judge = make_judge(role="backup")
        request = self._prepare_request()

        first = judge.PrepareAuctionMutation(request, NoopContext())
        with mock.patch.object(judge, "_persist_state_to_disk") as persist:
            repeated = judge.PrepareAuctionMutation(request, NoopContext())

        self.assertTrue(first.success)
        self.assertEqual(repeated, first)
        persist.assert_not_called()
        self.assertEqual(list(judge.prepared_mutations), ["prepare-1"])

    def test_same_prepare_request_id_with_different_contents_is_rejected(self):
        judge = make_judge(role="backup")
        original = self._prepare_request()
        conflicting = self._prepare_request()
        conflicting.candidate_auction.title = "Different candidate"

        first = judge.PrepareAuctionMutation(original, NoopContext())
        rejected = judge.PrepareAuctionMutation(conflicting, NoopContext())

        self.assertTrue(first.success)
        self.assertFalse(rejected.success)
        self.assertIn("different contents", rejected.message)
        self.assertEqual(judge.prepared_mutations["prepare-1"], original)

    def test_prepare_mutation_persists_and_restores_storage_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = f"{temp_dir}/auction-state.pb"
            judge = make_judge(role="backup", state_file_path=state_path)
            judge.auction_store["auction-1"] = pb2.Auction(
                auction_id="auction-1",
                title="Committed title",
                version=4,
                state=pb2.AUCTION_STATE_OPEN,
            )
            judge.idempotency_records["committed-request"] = pb2.IdempotencyRecord(
                request_id="committed-request",
            )

            response = judge.PrepareAuctionMutation(
                self._prepare_request(version=5),
                NoopContext(),
            )

            snapshot = pb2.StorageSnapshot()
            with open(state_path, "rb") as state_file:
                snapshot.ParseFromString(state_file.read())

            recovered = make_judge(role="backup", state_file_path=state_path)
            recovered._load_state_from_disk()

        self.assertTrue(response.success)
        self.assertEqual(len(snapshot.auctions), 1)
        self.assertEqual(snapshot.auctions[0].version, 4)
        self.assertEqual(len(snapshot.idempotency_records), 1)
        self.assertEqual(snapshot.idempotency_records[0].request_id, "committed-request")
        self.assertEqual(len(snapshot.prepared_mutations), 1)
        self.assertEqual(snapshot.prepared_mutations[0].request_id, "prepare-1")
        self.assertEqual(
            snapshot.prepared_mutations[0].candidate_auction.version,
            5,
        )
        self.assertEqual(recovered.auction_store["auction-1"].version, 4)
        self.assertIn("committed-request", recovered.idempotency_records)
        self.assertIn("prepare-1", recovered.prepared_mutations)
        self.assertNotIn("prepare-1", recovered.idempotency_records)

    def test_prepare_mutation_fails_and_discards_stage_when_snapshot_write_fails(self):
        judge = make_judge(role="backup", state_file_path="/unused/state.pb")

        with mock.patch.object(
            judge,
            "_persist_state_to_disk",
            side_effect=OSError("disk unavailable"),
        ):
            response = judge.PrepareAuctionMutation(
                self._prepare_request(),
                NoopContext(),
            )

        self.assertFalse(response.success)
        self.assertIn("Could not persist", response.message)
        self.assertEqual(judge.prepared_mutations, {})
        self.assertEqual(judge.auction_store, {})
        self.assertEqual(judge.idempotency_records, {})

    def test_commit_prepared_mutation_is_accepted_only_on_backup(self):
        judge = make_judge(role="primary")

        response = judge.CommitPreparedMutation(
            pb2.MutationDecisionRequest(
                request_id="prepare-1",
                auction_id="auction-1",
                primary_id="primary-1",
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertIn("only on backup", response.message)

    def test_commit_prepared_mutation_requires_matching_auction_and_primary_ids(self):
        for mismatched_field in ("auction_id", "primary_id"):
            with self.subTest(mismatched_field=mismatched_field):
                judge = make_judge(role="backup")
                prepare = judge.PrepareAuctionMutation(
                    self._prepare_request(),
                    NoopContext(),
                )
                decision = {
                    "request_id": "prepare-1",
                    "auction_id": "auction-1",
                    "primary_id": "primary-1",
                }
                decision[mismatched_field] = "different-id"

                response = judge.CommitPreparedMutation(
                    pb2.MutationDecisionRequest(**decision),
                    NoopContext(),
                )

                self.assertTrue(prepare.success)
                self.assertFalse(response.success)
                self.assertIn("does not match", response.message)
                self.assertIn("prepare-1", judge.prepared_mutations)
                self.assertEqual(judge.auction_store, {})
                self.assertEqual(judge.idempotency_records, {})

    def test_commit_prepared_mutation_revalidates_committed_version(self):
        judge = make_judge(role="backup")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            version=4,
            state=pb2.AUCTION_STATE_OPEN,
        )
        prepare = judge.PrepareAuctionMutation(
            self._prepare_request(version=5),
            NoopContext(),
        )
        judge.auction_store["auction-1"].version = 5

        response = judge.CommitPreparedMutation(
            pb2.MutationDecisionRequest(
                request_id="prepare-1",
                auction_id="auction-1",
                primary_id="primary-1",
            ),
            NoopContext(),
        )

        self.assertTrue(prepare.success)
        self.assertFalse(response.success)
        self.assertEqual(response.committed_version, 5)
        self.assertIn("current committed version", response.message)
        self.assertIn("prepare-1", judge.prepared_mutations)
        self.assertNotIn("prepare-1", judge.idempotency_records)

    def test_commit_prepared_mutation_copies_and_atomically_persists_all_collections(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = f"{temp_dir}/auction-state.pb"
            judge = make_judge(role="backup", state_file_path=state_path)
            judge.auction_store["auction-1"] = pb2.Auction(
                auction_id="auction-1",
                title="Committed title",
                version=4,
                state=pb2.AUCTION_STATE_OPEN,
            )
            request = self._prepare_request(version=5)
            request.candidate_auction.title = "Prepared title"
            prepare = judge.PrepareAuctionMutation(request, NoopContext())
            prepared_value = judge.prepared_mutations["prepare-1"]

            response = judge.CommitPreparedMutation(
                pb2.MutationDecisionRequest(
                    request_id="prepare-1",
                    auction_id="auction-1",
                    primary_id="primary-1",
                ),
                NoopContext(),
            )
            prepared_value.candidate_auction.title = "Mutated old preparation"
            prepared_value.idempotency_record.response.message = "Mutated old record"

            snapshot = pb2.StorageSnapshot()
            with open(state_path, "rb") as state_file:
                snapshot.ParseFromString(state_file.read())

        self.assertTrue(prepare.success)
        self.assertTrue(response.success)
        self.assertEqual(response.committed_version, 5)
        self.assertEqual(judge.auction_store["auction-1"].title, "Prepared title")
        self.assertEqual(
            judge.idempotency_records["prepare-1"].response.message,
            "",
        )
        self.assertNotIn("prepare-1", judge.prepared_mutations)
        self.assertEqual(snapshot.auctions[0].version, 5)
        self.assertEqual(snapshot.auctions[0].title, "Prepared title")
        self.assertEqual(snapshot.idempotency_records[0].request_id, "prepare-1")
        self.assertEqual(len(snapshot.prepared_mutations), 0)
        self.assertNotIn("prepare-1", judge.prepared_mutations)
        self.assertIn("prepare-1", judge.idempotency_records)
        self.assertEqual(judge.idempotency_records["prepare-1"].request_id, "prepare-1")

    def test_commit_prepared_mutation_restores_all_collections_on_persist_failure(self):
        judge = make_judge(role="backup")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            version=4,
            state=pb2.AUCTION_STATE_OPEN,
        )
        prepare = judge.PrepareAuctionMutation(
            self._prepare_request(version=5),
            NoopContext(),
        )

        with mock.patch.object(
            judge,
            "_persist_state_to_disk",
            side_effect=OSError("disk unavailable"),
        ):
            response = judge.CommitPreparedMutation(
                pb2.MutationDecisionRequest(
                    request_id="prepare-1",
                    auction_id="auction-1",
                    primary_id="primary-1",
                ),
                NoopContext(),
            )

        self.assertTrue(prepare.success)
        self.assertFalse(response.success)
        self.assertIn("Could not persist", response.message)
        self.assertEqual(judge.auction_store["auction-1"].version, 4)
        self.assertNotIn("prepare-1", judge.idempotency_records)
        self.assertIn("prepare-1", judge.prepared_mutations)

    def test_commit_prepared_mutation_retry_is_idempotent(self):
        judge = make_judge(role="backup")
        prepare = judge.PrepareAuctionMutation(
            self._prepare_request(),
            NoopContext(),
        )
        decision = pb2.MutationDecisionRequest(
            request_id="prepare-1",
            auction_id="auction-1",
            primary_id="primary-1",
        )
        first = judge.CommitPreparedMutation(decision, NoopContext())

        with mock.patch.object(judge, "_persist_state_to_disk") as persist:
            retry = judge.CommitPreparedMutation(decision, NoopContext())

        self.assertTrue(prepare.success)
        self.assertTrue(first.success)
        self.assertTrue(retry.success)
        self.assertEqual(retry, first)
        persist.assert_not_called()
        self.assertEqual(judge.auction_store["auction-1"].version, 1)
        self.assertIn("prepare-1", judge.idempotency_records)
        self.assertNotIn("prepare-1", judge.prepared_mutations)

    def test_abort_prepared_mutation_requires_backup_role_and_all_ids(self):
        primary = make_judge(role="primary")
        role_response = primary.AbortPreparedMutation(
            self._decision_request(),
            NoopContext(),
        )
        self.assertFalse(role_response.success)
        self.assertIn("only on backup", role_response.message)

        for missing_field in ("request_id", "auction_id", "primary_id"):
            with self.subTest(missing_field=missing_field):
                backup = make_judge(role="backup")
                values = {
                    "request_id": "prepare-1",
                    "auction_id": "auction-1",
                    "primary_id": "primary-1",
                }
                values[missing_field] = ""
                response = backup.AbortPreparedMutation(
                    self._decision_request(**values),
                    NoopContext(),
                )
                self.assertFalse(response.success)
                self.assertEqual(backup.aborted_mutations, {})

    def test_abort_moves_preparation_to_durable_defensive_tombstone(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = f"{temp_dir}/auction-state.pb"
            judge = make_judge(role="backup", state_file_path=state_path)
            judge.auction_store["auction-1"] = pb2.Auction(
                auction_id="auction-1",
                version=4,
                state=pb2.AUCTION_STATE_OPEN,
            )
            committed_before = pb2.Auction()
            committed_before.CopyFrom(judge.auction_store["auction-1"])
            prepare = judge.PrepareAuctionMutation(
                self._prepare_request(version=5),
                NoopContext(),
            )
            decision = self._decision_request()

            response = judge.AbortPreparedMutation(decision, NoopContext())
            decision.auction_id = "mutated-after-abort"

            snapshot = pb2.StorageSnapshot()
            with open(state_path, "rb") as state_file:
                snapshot.ParseFromString(state_file.read())
            recovered = make_judge(role="backup", state_file_path=state_path)
            recovered._load_state_from_disk()

        self.assertTrue(prepare.success)
        self.assertTrue(response.success)
        self.assertEqual(response.committed_version, 4)
        self.assertNotIn("prepare-1", judge.prepared_mutations)
        self.assertEqual(judge.auction_store["auction-1"], committed_before)
        self.assertEqual(judge.idempotency_records, {})
        self.assertEqual(
            judge.aborted_mutations["prepare-1"].auction_id,
            "auction-1",
        )
        self.assertEqual(len(snapshot.prepared_mutations), 0)
        self.assertEqual(len(snapshot.aborted_mutations), 1)
        self.assertEqual(snapshot.aborted_mutations[0].auction_id, "auction-1")
        self.assertIn("prepare-1", recovered.aborted_mutations)
        self.assertNotIn("prepare-1", recovered.prepared_mutations)

    def test_abort_unknown_request_is_idempotent_and_identity_stable(self):
        judge = make_judge(role="backup")
        decision = self._decision_request(request_id="unknown-request")

        first = judge.AbortPreparedMutation(decision, NoopContext())
        with mock.patch.object(judge, "_persist_state_to_disk") as persist:
            retry = judge.AbortPreparedMutation(decision, NoopContext())
        different_auction = judge.AbortPreparedMutation(
            self._decision_request(
                request_id="unknown-request",
                auction_id="different-auction",
            ),
            NoopContext(),
        )
        different_primary = judge.AbortPreparedMutation(
            self._decision_request(
                request_id="unknown-request",
                primary_id="different-primary",
            ),
            NoopContext(),
        )

        self.assertTrue(first.success)
        self.assertEqual(retry, first)
        persist.assert_not_called()
        self.assertFalse(different_auction.success)
        self.assertFalse(different_primary.success)
        self.assertEqual(
            judge.aborted_mutations["unknown-request"],
            decision,
        )

    def test_abort_rejects_committed_request(self):
        judge = make_judge(role="backup")
        judge.idempotency_records["prepare-1"] = pb2.IdempotencyRecord(
            request_id="prepare-1",
        )

        response = judge.AbortPreparedMutation(
            self._decision_request(),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertIn("committed mutation", response.message)
        self.assertEqual(judge.aborted_mutations, {})

    def test_abort_requires_existing_preparation_identity_to_match(self):
        for mismatched_field in ("auction_id", "primary_id"):
            with self.subTest(mismatched_field=mismatched_field):
                judge = make_judge(role="backup")
                judge.PrepareAuctionMutation(
                    self._prepare_request(),
                    NoopContext(),
                )
                values = {
                    "request_id": "prepare-1",
                    "auction_id": "auction-1",
                    "primary_id": "primary-1",
                }
                values[mismatched_field] = "different-id"

                response = judge.AbortPreparedMutation(
                    self._decision_request(**values),
                    NoopContext(),
                )

                self.assertFalse(response.success)
                self.assertIn("does not match", response.message)
                self.assertIn("prepare-1", judge.prepared_mutations)
                self.assertEqual(judge.aborted_mutations, {})

    def test_abort_persist_failure_restores_preparation_and_removes_tombstone(self):
        judge = make_judge(role="backup")
        judge.PrepareAuctionMutation(self._prepare_request(), NoopContext())

        with mock.patch.object(
            judge,
            "_persist_state_to_disk",
            side_effect=OSError("disk unavailable"),
        ):
            response = judge.AbortPreparedMutation(
                self._decision_request(),
                NoopContext(),
            )

        self.assertFalse(response.success)
        self.assertIn("Could not persist", response.message)
        self.assertIn("prepare-1", judge.prepared_mutations)
        self.assertEqual(judge.aborted_mutations, {})
        self.assertEqual(judge.auction_store, {})
        self.assertEqual(judge.idempotency_records, {})

    def test_tombstoned_request_id_is_rejected_by_prepare_and_commit(self):
        judge = make_judge(role="backup")
        abort = judge.AbortPreparedMutation(
            self._decision_request(),
            NoopContext(),
        )

        prepare = judge.PrepareAuctionMutation(
            self._prepare_request(),
            NoopContext(),
        )
        commit = judge.CommitPreparedMutation(
            self._decision_request(),
            NoopContext(),
        )
        judge.PromoteToPrimary(
            pb2.PromotionRequest(new_role="primary"),
            NoopContext(),
        )
        mutation = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_CREATE,
                request_id="prepare-1",
                auction=pb2.Auction(
                    auction_id="auction-1",
                    seller_id="seller-a",
                    reserve_price=100.0,
                    ends_at=future_timestamp(),
                ),
            ),
            NoopContext(),
        )

        self.assertTrue(abort.success)
        self.assertFalse(prepare.success)
        self.assertFalse(commit.success)
        self.assertFalse(mutation.success)
        self.assertIn("aborted", prepare.message)
        self.assertIn("aborted", commit.message)
        self.assertIn("aborted", mutation.message)
        self.assertEqual(judge.auction_store, {})
        self.assertEqual(judge.idempotency_records, {})

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

    def test_apply_mutation_delegates_candidate_to_commit_coordinator(self):
        judge = make_judge(role="primary", use_test_coordinator=False)
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            version=1,
            state=pb2.AUCTION_STATE_OPEN,
            bids={"buyer-a": active_bid(100.0, 1)},
        )
        original = pb2.Auction()
        original.CopyFrom(judge.auction_store["auction-1"])

        def coordinate(
            request_id,
            candidate,
            idempotency_record,
            success_response,
            previous_version,
        ):
            self.assertEqual(request_id, "delegate-request")
            self.assertEqual(candidate.version, 2)
            self.assertIn("buyer-b", candidate.bids)
            self.assertEqual(idempotency_record.request_id, "delegate-request")
            self.assertEqual(idempotency_record.response, success_response)
            self.assertEqual(previous_version, 1)
            return success_response

        with (
            mock.patch.object(
                judge,
                "_coordinate_synchronous_commit",
                side_effect=coordinate,
            ) as coordinator,
            mock.patch.object(judge, "_persist_state_to_disk") as persist,
        ):
            response = judge.ApplyAuctionMutation(
                pb2.AuctionMutationRequest(
                    request_id="delegate-request",
                    auction=pb2.Auction(
                        auction_id="auction-1",
                        version=1,
                        bids={"buyer-b": active_bid(200.0)},
                    )
                ),
                NoopContext(),
            )

        self.assertTrue(response.success)
        coordinator.assert_called_once()
        persist.assert_not_called()
        self.assertEqual(judge.auction_store["auction-1"], original)
        self.assertEqual(judge.idempotency_records, {})

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

    def test_prepare_rejects_revealed_state_without_committed_result(self):
        judge = make_judge(role="backup")

        request = self._prepare_request(version=1)
        request.candidate_auction.CopyFrom(
            pb2.Auction(
                    auction_id="uncommitted-reveal",
                    state=pb2.AUCTION_STATE_REVEALED,
                    version=1,
                    bids={"buyer-a": active_bid(750.0, 1)},
            )
        )
        response = judge.PrepareAuctionMutation(request, NoopContext())

        self.assertFalse(response.success)
        self.assertIn("committed result", response.message)
        self.assertNotIn("uncommitted-reveal", judge.auction_store)

    def test_prepare_rejects_revealed_state_with_uncalculated_result(self):
        judge = make_judge(role="backup")

        request = self._prepare_request(version=1)
        request.candidate_auction.CopyFrom(
            pb2.Auction(
                    auction_id="incorrect-reveal",
                    reserve_price=500.0,
                    state=pb2.AUCTION_STATE_REVEALED,
                    version=1,
                    bids={"buyer-a": active_bid(750.0, 1)},
                    result=pb2.AuctionResult(
                        outcome=pb2.AUCTION_OUTCOME_NO_BIDS,
                    ),
            )
        )
        response = judge.PrepareAuctionMutation(request, NoopContext())

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

    def test_full_sync_atomically_replaces_local_state_before_reporting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = f"{temp_dir}/backup.pb"
            judge = make_judge(
                role="backup",
                address="backup:50051",
                state_file_path=state_path,
            )
            judge.auction_store["stale-auction"] = pb2.Auction(
                auction_id="stale-auction", version=8, state=pb2.AUCTION_STATE_OPEN
            )
            judge.idempotency_records["stale-request"] = pb2.IdempotencyRecord(
                request_id="stale-request"
            )
            judge.prepared_mutations["stale-prepare"] = pb2.PrepareMutationRequest(
                request_id="stale-prepare"
            )
            judge.aborted_mutations["stale-abort"] = pb2.MutationDecisionRequest(
                request_id="stale-abort"
            )
            judge.pending_backup_commits["stale-pending"] = pb2.CommitDecision(
                request_id="stale-pending"
            )
            response = pb2.StateResponse(
                ok=True,
                auctions=[pb2.Auction(
                    auction_id="current-auction",
                    version=3,
                    state=pb2.AUCTION_STATE_OPEN,
                )],
                idempotency_records=[pb2.IdempotencyRecord(
                    request_id="current-request"
                )],
            )

            replaced = judge._replace_with_full_state(response)
            snapshot = pb2.StorageSnapshot()
            with open(state_path, "rb") as state_file:
                snapshot.ParseFromString(state_file.read())

        self.assertTrue(replaced)
        self.assertEqual(set(judge.auction_store), {"current-auction"})
        self.assertEqual(set(judge.idempotency_records), {"current-request"})
        self.assertEqual(judge.prepared_mutations, {})
        self.assertEqual(judge.aborted_mutations, {})
        self.assertEqual(judge.pending_backup_commits, {})
        self.assertEqual([auction.auction_id for auction in snapshot.auctions], ["current-auction"])
        self.assertEqual(len(snapshot.prepared_mutations), 0)
        self.assertEqual(len(snapshot.aborted_mutations), 0)
        self.assertEqual(len(snapshot.pending_backup_commits), 0)

    def test_storage_reports_synchronization_only_after_full_state_replacement(self):
        judge = make_judge(role="backup", address="backup:50051")
        state_response = pb2.StateResponse(
            ok=True,
            auctions=[pb2.Auction(
                auction_id="current-auction",
                version=1,
                state=pb2.AUCTION_STATE_OPEN,
            )],
        )
        storage_stub = mock.Mock()
        storage_stub.SyncFullState.return_value = state_response

        with (
            mock.patch(
                "blindsided.storage.service.grpc.insecure_channel",
                return_value=ChannelContext(),
            ),
            mock.patch(
                "blindsided.storage.service.pb2_grpc.StorageReplicaServiceStub",
                return_value=storage_stub,
            ),
            mock.patch.object(
                judge, "_report_synchronization_complete", return_value=True
            ) as report,
        ):
            synchronized = judge._synchronize_from_primary("primary:50051")

        self.assertTrue(synchronized)
        self.assertEqual(set(judge.auction_store), {"current-auction"})
        report.assert_called_once_with("primary:50051")
        sync_request = storage_stub.SyncFullState.call_args.args[0]
        self.assertEqual(sync_request.requester_id, "backup:50051")

    def test_synchronization_report_identifies_backup_and_source_primary(self):
        judge = make_judge(role="backup", address="backup:50051")
        controller_stub = mock.Mock()
        controller_stub.ReportSynchronizationComplete.return_value = (
            pb2.SynchronizationCompleteResponse(success=True)
        )

        with (
            mock.patch(
                "blindsided.storage.service.grpc.insecure_channel",
                return_value=ChannelContext(),
            ),
            mock.patch(
                "blindsided.storage.service.pb2_grpc.ClusterControllerStub",
                return_value=controller_stub,
            ),
        ):
            reported = judge._report_synchronization_complete("primary:50051")

        self.assertTrue(reported)
        request = controller_stub.ReportSynchronizationComplete.call_args.args[0]
        self.assertEqual(request.replica_address, "backup:50051")
        self.assertEqual(request.source_primary_address, "primary:50051")

    def test_failed_synchronize_from_primary_does_not_report_completion(self):
        judge = make_judge(role="backup", address="backup:50051")
        storage_stub = mock.Mock()
        storage_stub.SyncFullState.side_effect = PrepareRpcError()
        controller_stub = mock.Mock()

        with (
            mock.patch(
                "blindsided.storage.service.grpc.insecure_channel",
                return_value=ChannelContext(),
            ),
            mock.patch(
                "blindsided.storage.service.pb2_grpc.StorageReplicaServiceStub",
                return_value=storage_stub,
            ),
            mock.patch(
                "blindsided.storage.service.pb2_grpc.ClusterControllerStub",
                return_value=controller_stub,
            ) as controller_stub_factory,
        ):
            synchronized = judge._synchronize_from_primary("primary:50051")

        self.assertFalse(synchronized)
        controller_stub_factory.assert_not_called()
        controller_stub.ReportSynchronizationComplete.assert_not_called()

    def test_full_sync_persistence_failure_restores_state_and_is_not_reported(self):
        judge = make_judge(role="backup", address="backup:50051")
        stale = pb2.Auction(
            auction_id="stale-auction", version=4, state=pb2.AUCTION_STATE_OPEN
        )
        judge.auction_store[stale.auction_id] = stale
        storage_stub = mock.Mock()
        storage_stub.SyncFullState.return_value = pb2.StateResponse(
            ok=True,
            auctions=[pb2.Auction(
                auction_id="current-auction",
                version=5,
                state=pb2.AUCTION_STATE_OPEN,
            )],
        )

        with (
            mock.patch(
                "blindsided.storage.service.grpc.insecure_channel",
                return_value=ChannelContext(),
            ),
            mock.patch(
                "blindsided.storage.service.pb2_grpc.StorageReplicaServiceStub",
                return_value=storage_stub,
            ),
            mock.patch.object(
                judge, "_persist_state_to_disk", side_effect=OSError("disk unavailable")
            ),
            mock.patch.object(judge, "_report_synchronization_complete") as report,
        ):
            synchronized = judge._synchronize_from_primary("primary:50051")

        self.assertFalse(synchronized)
        self.assertEqual(set(judge.auction_store), {"stale-auction"})
        self.assertEqual(judge.auction_store["stale-auction"], stale)
        report.assert_not_called()
