from unittest import TestCase, mock

from prometheus_client import CollectorRegistry, Counter, Histogram

from blindsided.generated import blindsided_pb2 as pb2
from blindsided.observability import instrumentation
from backend.tests.helpers import NoopContext, future_timestamp, make_judge
from backend.tests.layers.test_auction_service import FakeJudgeStub, TestableAuctionService


class MutationObservabilityTests(TestCase):
    def setUp(self):
        self.registry = CollectorRegistry()
        collectors = {
            "MUTATIONS": Counter(
                "blindsided_mutations_total", "mutations", ["operation", "outcome"],
                registry=self.registry,
            ),
            "CONCURRENCY_RETRIES": Counter(
                "blindsided_concurrency_retries_total", "retries", ["operation", "outcome"],
                registry=self.registry,
            ),
            "IDEMPOTENCY_REQUESTS": Counter(
                "blindsided_idempotency_requests_total", "idempotency", ["operation", "outcome"],
                registry=self.registry,
            ),
            "RPC_REQUESTS": Counter(
                "blindsided_rpc_requests_total", "rpc", ["service", "method", "result"],
                registry=self.registry,
            ),
            "RPC_DURATION_SECONDS": Histogram(
                "blindsided_rpc_duration_seconds", "duration", ["service", "method", "result"],
                registry=self.registry,
            ),
        }
        for name, collector in collectors.items():
            patcher = mock.patch.object(instrumentation, name, collector)
            patcher.start()
            self.addCleanup(patcher.stop)

    def value(self, metric, **labels):
        return self.registry.get_sample_value(metric, labels)

    def create(self, service, request_id="create-1"):
        return service.CreateAuction(
            pb2.CreateAuctionRequest(request_id=request_id, seller_id="seller-a"),
            NoopContext(),
        )

    def bid_request(self):
        return pb2.BidRequest(
            auction_id="auction-1", bidder_id="buyer-a", amount=250,
            expected_version=1, request_id="bid-1",
        )

    def test_final_mutation_outcomes_are_recorded_once(self):
        committed = self.create(TestableAuctionService(FakeJudgeStub()), "committed")
        rejected = self.create(TestableAuctionService(FakeJudgeStub()), "")

        pending_stub = FakeJudgeStub()
        pending_stub.mutation_responses.append(pb2.AuctionMutationResponse(
            failure_reason=pb2.MUTATION_FAILURE_REASON_ACKNOWLEDGEMENT_PENDING,
            message="pending",
        ))
        unknown = self.create(TestableAuctionService(pending_stub), "unknown")
        unavailable_service = TestableAuctionService(
            FakeJudgeStub(), primary_address=None
        )
        unavailable_service.failover_recovery_window = 0
        unavailable = self.create(unavailable_service, "unavailable")

        self.assertTrue(committed.ok)
        self.assertFalse(rejected.ok)
        self.assertTrue(unknown.outcome_unknown)
        self.assertTrue(unavailable.retryable)
        for outcome in ("committed", "rejected", "unknown", "unavailable"):
            self.assertEqual(self.value(
                "blindsided_mutations_total",
                operation="CreateAuction",
                outcome=outcome,
            ), 1.0)
        self.assertEqual(self.value(
            "blindsided_rpc_requests_total",
            service="auction_service", method="CreateAuction", result="success",
        ), 1.0)

    def test_initial_success_records_no_concurrency_retry(self):
        response = TestableAuctionService(FakeJudgeStub()).PlaceBid(
            self.bid_request(), NoopContext()
        )

        self.assertTrue(response.success)
        self.assertIsNone(self.value(
            "blindsided_concurrency_retries_total",
            operation="PlaceBid", outcome="retried",
        ))

    def test_concurrency_retry_then_success_records_both_outcomes(self):
        stub = FakeJudgeStub()
        stub.mutation_responses.extend([
            pb2.AuctionMutationResponse(
                failure_reason=pb2.MUTATION_FAILURE_REASON_CONCURRENCY_CONFLICT,
                current_version=2,
            ),
            pb2.AuctionMutationResponse(success=True, current_version=3),
        ])
        service = TestableAuctionService(stub)

        response = service.PlaceBid(self.bid_request(), NoopContext())

        self.assertTrue(response.success)
        self.assertEqual(stub.mutations[1].expected_version, 2)
        for outcome in ("retried", "succeeded_after_retry"):
            self.assertEqual(self.value(
                "blindsided_concurrency_retries_total",
                operation="PlaceBid", outcome=outcome,
            ), 1.0)
        self.assertEqual(self.value(
            "blindsided_mutations_total",
            operation="PlaceBid", outcome="committed",
        ), 1.0)

    def test_exhausted_concurrency_retries_record_conflict_once(self):
        stub = FakeJudgeStub()
        stub.mutation_responses.extend([
            pb2.AuctionMutationResponse(
                failure_reason=pb2.MUTATION_FAILURE_REASON_CONCURRENCY_CONFLICT,
                current_version=2,
            ),
            pb2.AuctionMutationResponse(
                failure_reason=pb2.MUTATION_FAILURE_REASON_CONCURRENCY_CONFLICT,
                current_version=3,
            ),
        ])
        service = TestableAuctionService(stub)
        service.mutation_retry_limit = 2

        response = service.PlaceBid(self.bid_request(), NoopContext())

        self.assertFalse(response.success)
        self.assertEqual(self.value(
            "blindsided_concurrency_retries_total",
            operation="PlaceBid", outcome="retried",
        ), 1.0)
        self.assertEqual(self.value(
            "blindsided_concurrency_retries_total",
            operation="PlaceBid", outcome="exhausted",
        ), 1.0)
        self.assertEqual(self.value(
            "blindsided_mutations_total", operation="PlaceBid", outcome="conflict",
        ), 1.0)

    def test_non_concurrency_failure_does_not_record_exhausted(self):
        stub = FakeJudgeStub()
        stub.mutation_responses.append(pb2.AuctionMutationResponse(
            failure_reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
        ))

        response = TestableAuctionService(stub).PlaceBid(
            self.bid_request(), NoopContext()
        )

        self.assertFalse(response.success)
        self.assertIsNone(self.value(
            "blindsided_concurrency_retries_total",
            operation="PlaceBid", outcome="exhausted",
        ))

    def test_idempotency_new_replay_and_mismatch_are_authoritative(self):
        judge = make_judge(role="primary")
        storage_stub = mock.Mock()
        storage_stub.ApplyAuctionMutation.side_effect = (
            lambda request, timeout=None: judge.ApplyAuctionMutation(
                request, NoopContext()
            )
        )
        service = TestableAuctionService(
            storage_stub, primary_epoch=judge.current_epoch
        )
        original = pb2.CreateAuctionRequest(
            request_id="idempotent-create",
            seller_id="seller-a",
            title="original",
            reserve_price=100,
            ends_at=future_timestamp(),
        )

        first = service.CreateAuction(original, NoopContext())
        replay = service.CreateAuction(original, NoopContext())
        mismatch_request = pb2.CreateAuctionRequest()
        mismatch_request.CopyFrom(original)
        mismatch_request.title = "different"
        mismatch = service.CreateAuction(mismatch_request, NoopContext())

        self.assertTrue(first.ok)
        self.assertTrue(replay.ok)
        self.assertFalse(mismatch.ok)
        self.assertEqual(len(judge.auction_store), 1)
        self.assertEqual(next(iter(judge.auction_store.values())).version, 1)
        for outcome in ("new", "replayed", "mismatch"):
            self.assertEqual(self.value(
                "blindsided_idempotency_requests_total",
                operation="CreateAuction", outcome=outcome,
            ), 1.0)
        self.assertEqual(
            instrumentation.IDEMPOTENCY_REQUESTS._labelnames,
            ("operation", "outcome"),
        )

    def test_replica_protocol_does_not_count_client_idempotency_decision(self):
        backup = make_judge(role="backup")

        backup.PrepareAuctionMutation(
            pb2.PrepareMutationRequest(),
            NoopContext(),
        )

        for outcome in ("new", "replayed", "mismatch"):
            self.assertIsNone(self.value(
                "blindsided_idempotency_requests_total",
                operation="CreateAuction",
                outcome=outcome,
            ))
