from unittest import TestCase, mock

import grpc
from prometheus_client import CollectorRegistry, Counter, Histogram

from blindsided.generated import blindsided_pb2 as pb2
from blindsided.observability import instrumentation
from blindsided.storage.replication_client import SynchronousReplicationClient
from backend.tests.helpers import ChannelContext, NoopContext, make_judge


class StatusRpcError(grpc.RpcError):
    def __init__(self, status):
        self.status = status

    def code(self):
        return self.status


class ReplicationObservabilityTests(TestCase):
    def setUp(self):
        self.registry = CollectorRegistry()
        collectors = {
            "REPLICATION_ATTEMPTS": Counter(
                "blindsided_replication_attempts_total", "attempts",
                ["operation", "outcome"], registry=self.registry,
            ),
            "REPLICATION_DURATION_SECONDS": Histogram(
                "blindsided_replication_duration_seconds", "duration",
                ["operation", "outcome"], registry=self.registry,
            ),
            "COMMITS": Counter(
                "blindsided_commits_total", "commits",
                ["operation", "outcome"], registry=self.registry,
            ),
        }
        for name, collector in collectors.items():
            patcher = mock.patch.object(instrumentation, name, collector)
            patcher.start()
            self.addCleanup(patcher.stop)

    def value(self, metric, outcome):
        return self.registry.get_sample_value(
            metric,
            {"operation": "PlaceBid", "outcome": outcome},
        )

    def prepare_request(self):
        return pb2.PrepareMutationRequest(
            request_id="request-1",
            candidate_auction=pb2.Auction(auction_id="auction-1", version=2),
        )

    def call_prepare(self, *, response=None, error=None):
        stub = mock.Mock()
        if error is not None:
            stub.PrepareAuctionMutation.side_effect = error
        else:
            stub.PrepareAuctionMutation.return_value = response
        with (
            mock.patch(
                "blindsided.storage.replication_client.grpc.insecure_channel",
                return_value=ChannelContext(),
            ),
            mock.patch(
                "blindsided.storage.replication_client.pb2_grpc.StorageReplicaServiceStub",
                return_value=stub,
            ),
            instrumentation.replication_operation("PlaceBid"),
        ):
            result = SynchronousReplicationClient().prepare(
                "backup:50051", self.prepare_request()
            )
        return result

    def assert_attempt(self, outcome):
        self.assertEqual(self.value(
            "blindsided_replication_attempts_total", outcome
        ), 1.0)
        self.assertEqual(self.value(
            "blindsided_replication_duration_seconds_count", outcome
        ), 1.0)

    def test_accepted_replication_records_one_success_and_duration(self):
        response = pb2.PrepareMutationResponse(success=True, prepared_version=2)

        self.assertIs(self.call_prepare(response=response), response)

        self.assert_attempt("success")
        self.assertEqual(sum(
            sample.value
            for metric in instrumentation.REPLICATION_ATTEMPTS.collect()
            for sample in metric.samples
            if sample.name == "blindsided_replication_attempts_total"
        ), 1.0)

    def test_explicit_rejection_records_rejected(self):
        response = pb2.PrepareMutationResponse(success=False)

        self.assertIs(self.call_prepare(response=response), response)

        self.assert_attempt("rejected")

    def test_timeout_records_timeout_and_preserves_none_result(self):
        result = self.call_prepare(
            error=StatusRpcError(grpc.StatusCode.DEADLINE_EXCEEDED)
        )

        self.assertIsNone(result)
        self.assert_attempt("timeout")

    def test_unavailable_transport_records_unreachable(self):
        result = self.call_prepare(
            error=StatusRpcError(grpc.StatusCode.UNAVAILABLE)
        )

        self.assertIsNone(result)
        self.assert_attempt("unreachable")

    def test_unexpected_failure_is_recorded_and_reraised_unchanged(self):
        original = RuntimeError("unexpected")

        with self.assertRaises(RuntimeError) as raised:
            self.call_prepare(error=original)

        self.assertIs(raised.exception, original)
        self.assert_attempt("failure")

    def coordinator_values(self):
        candidate = pb2.Auction(auction_id="auction-1", version=2)
        record = pb2.IdempotencyRecord(request_id="request-1")
        response = pb2.AuctionMutationResponse(success=True, current_version=2)
        return candidate, record, response

    def coordinate(self, prepare=True, decision=True, complete=True):
        judge = make_judge(role="primary", use_test_coordinator=False)
        candidate, record, response = self.coordinator_values()
        with (
            mock.patch.object(judge, "_prepare_on_synchronous_backup", return_value=prepare),
            mock.patch.object(judge, "_record_commit_decision", return_value=decision),
            mock.patch.object(judge, "_complete_pending_backup_commit", return_value=complete),
            mock.patch.object(judge, "_abort_on_synchronous_backup", return_value=True),
            instrumentation.replication_operation("PlaceBid"),
        ):
            result = judge._coordinate_synchronous_commit(
                "request-1", candidate, record, response, previous_version=1
            )
        return result

    def test_commit_coordination_records_each_final_outcome_once(self):
        self.assertTrue(self.coordinate().success)
        self.assertFalse(self.coordinate(prepare=False).success)
        unknown = self.coordinate(complete=False)

        self.assertEqual(
            unknown.failure_reason,
            pb2.MUTATION_FAILURE_REASON_ACKNOWLEDGEMENT_PENDING,
        )
        for outcome in ("committed", "aborted", "unknown"):
            self.assertEqual(self.value("blindsided_commits_total", outcome), 1.0)

    def test_backup_apply_and_invalid_candidate_emit_no_commit_or_attempt(self):
        backup = make_judge(role="backup")
        backup.PrepareAuctionMutation(pb2.PrepareMutationRequest(), NoopContext())
        primary = make_judge(role="primary", use_test_coordinator=False)
        rejected = primary.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                mutation_type=pb2.AUCTION_MUTATION_TYPE_CREATE,
                request_id="invalid",
                epoch=primary.current_epoch,
                auction=pb2.Auction(auction_id="invalid"),
            ),
            NoopContext(),
        )

        self.assertFalse(rejected.success)
        self.assertEqual(len(list(instrumentation.COMMITS.collect())[0].samples), 0)
        self.assertEqual(
            len(list(instrumentation.REPLICATION_ATTEMPTS.collect())[0].samples),
            0,
        )
