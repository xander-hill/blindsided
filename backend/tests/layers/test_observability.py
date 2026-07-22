from types import SimpleNamespace
from unittest import TestCase, mock

from prometheus_client import CollectorRegistry, Counter, Histogram

from blindsided.observability import instrumentation
from blindsided.observability.instrumentation import observe_rpc
from blindsided.observability.metrics import RPC_DURATION_SECONDS, RPC_REQUESTS


class RpcInstrumentationTests(TestCase):
    def setUp(self):
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "blindsided_rpc_requests_total",
            "requests",
            ["service", "method", "result"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "blindsided_rpc_duration_seconds",
            "duration",
            ["service", "method", "result"],
            registry=self.registry,
        )
        self.patches = (
            mock.patch.object(instrumentation, "RPC_REQUESTS", self.requests),
            mock.patch.object(
                instrumentation,
                "RPC_DURATION_SECONDS",
                self.duration,
            ),
        )
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def sample(self, name, result):
        return self.registry.get_sample_value(
            name,
            {
                "service": "auction_service",
                "method": "GetAuction",
                "result": result,
            },
        )

    def test_metric_names_and_labels_are_exact(self):
        RPC_REQUESTS.labels(
            service="auction_service",
            method="GetAuction",
            result="success",
        )
        RPC_DURATION_SECONDS.labels(
            service="auction_service",
            method="GetAuction",
            result="success",
        )
        request_sample_names = {
            sample.name for metric in RPC_REQUESTS.collect() for sample in metric.samples
        }
        duration_sample_names = {
            sample.name
            for metric in RPC_DURATION_SECONDS.collect()
            for sample in metric.samples
        }

        self.assertIn("blindsided_rpc_requests_total", request_sample_names)
        self.assertIn("blindsided_rpc_duration_seconds_count", duration_sample_names)
        self.assertIn("blindsided_rpc_duration_seconds_sum", duration_sample_names)
        self.assertEqual(
            RPC_REQUESTS._labelnames,
            ("service", "method", "result"),
        )
        self.assertEqual(
            RPC_DURATION_SECONDS._labelnames,
            ("service", "method", "result"),
        )

    def test_success_records_one_request_and_one_duration_with_same_result(self):
        response = object()

        @observe_rpc("auction_service", "GetAuction")
        def handler():
            return response

        self.assertIs(handler(), response)
        self.assertEqual(
            self.sample("blindsided_rpc_requests_total", "success"),
            1.0,
        )
        self.assertEqual(
            self.sample("blindsided_rpc_duration_seconds_count", "success"),
            1.0,
        )

    def test_explicit_response_classifier_records_failure(self):
        classifier = mock.Mock(return_value="failure")
        response = SimpleNamespace(ok=False)

        @observe_rpc("auction_service", "GetAuction", classifier)
        def handler():
            return response

        self.assertIs(handler(), response)
        classifier.assert_called_once_with(response)
        self.assertEqual(
            self.sample("blindsided_rpc_requests_total", "failure"),
            1.0,
        )
        self.assertEqual(
            self.sample("blindsided_rpc_duration_seconds_count", "failure"),
            1.0,
        )

    def test_original_exception_is_reraised_and_recorded(self):
        original = RuntimeError("original")

        @observe_rpc("auction_service", "GetAuction")
        def handler():
            raise original

        with self.assertRaises(RuntimeError) as raised:
            handler()

        self.assertIs(raised.exception, original)
        self.assertEqual(
            self.sample("blindsided_rpc_requests_total", "failure"),
            1.0,
        )
        self.assertEqual(
            self.sample("blindsided_rpc_duration_seconds_count", "failure"),
            1.0,
        )

    def test_wrapped_handler_metadata_is_preserved(self):
        def handler():
            """handler documentation"""

        wrapped = observe_rpc("auction_service", "GetAuction")(handler)

        self.assertEqual(wrapped.__name__, "handler")
        self.assertEqual(wrapped.__doc__, "handler documentation")
        self.assertIs(wrapped.__wrapped__, handler)

    def test_decorating_multiple_handlers_does_not_register_collectors(self):
        @observe_rpc("auction_service", "GetAuction")
        def first():
            return None

        @observe_rpc(
            "auction_service",
            "GetAuction",
            lambda response: "success" if response.ok else "failure",
        )
        def second():
            return SimpleNamespace(ok=True)

        first()
        second()

        self.assertEqual(
            self.sample("blindsided_rpc_requests_total", "success"),
            2.0,
        )
        self.assertEqual(
            self.sample("blindsided_rpc_duration_seconds_count", "success"),
            2.0,
        )
        self.assertEqual(len(list(self.registry.collect())), 2)

    def test_one_unary_call_is_not_double_counted(self):
        calls = 0

        @observe_rpc("auction_service", "GetAuction")
        def handler():
            nonlocal calls
            calls += 1
            return None

        handler()

        self.assertEqual(calls, 1)
        self.assertEqual(
            self.sample("blindsided_rpc_requests_total", "success"),
            1.0,
        )
