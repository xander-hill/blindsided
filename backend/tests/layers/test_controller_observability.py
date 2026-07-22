from unittest import TestCase, mock

import grpc
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from blindsided.controller.service import (
    ControllerService,
    PrimaryAssignment,
    PrimaryStatus,
    ReplicaRecord,
    ReplicaSyncStatus,
)
from blindsided.generated import blindsided_pb2 as pb2
from blindsided.observability import instrumentation
from backend.tests.helpers import ChannelContext, NoopContext


class StatusRpcError(grpc.RpcError):
    def __init__(self, status):
        self.status = status

    def code(self):
        return self.status


class ControllerObservabilityTests(TestCase):
    def setUp(self):
        self.registry = CollectorRegistry()
        collectors = {
            "REGISTERED_REPLICAS": Gauge(
                "blindsided_registered_replicas", "registered", registry=self.registry
            ),
            "HEALTHY_REPLICAS": Gauge(
                "blindsided_healthy_replicas", "healthy", registry=self.registry
            ),
            "CLUSTER_READY": Gauge(
                "blindsided_cluster_ready", "ready", registry=self.registry
            ),
            "PRIMARY_EPOCH": Gauge(
                "blindsided_primary_epoch", "epoch", registry=self.registry
            ),
            "REPLICA_HEALTH_TRANSITIONS": Counter(
                "blindsided_replica_health_transitions_total", "health",
                ["transition"], registry=self.registry,
            ),
            "FAILOVERS": Counter(
                "blindsided_failovers_total", "failovers", ["outcome"],
                registry=self.registry,
            ),
            "FAILOVER_DURATION_SECONDS": Histogram(
                "blindsided_failover_duration_seconds", "failover duration",
                ["outcome"], registry=self.registry,
            ),
            "PROMOTION_ATTEMPTS": Counter(
                "blindsided_promotion_attempts_total", "promotions", ["outcome"],
                registry=self.registry,
            ),
            "PROMOTION_DURATION_SECONDS": Histogram(
                "blindsided_promotion_duration_seconds", "promotion duration",
                ["outcome"], registry=self.registry,
            ),
            "SYNCHRONIZATION_ATTEMPTS": Counter(
                "blindsided_synchronization_attempts_total", "sync", ["outcome"],
                registry=self.registry,
            ),
            "SYNCHRONIZATION_DURATION_SECONDS": Histogram(
                "blindsided_synchronization_duration_seconds", "sync duration",
                ["outcome"], registry=self.registry,
            ),
        }
        for name, collector in collectors.items():
            patcher = mock.patch.object(instrumentation, name, collector)
            patcher.start()
            self.addCleanup(patcher.stop)

    def value(self, name, labels=None):
        return self.registry.get_sample_value(name, labels or {})

    def register(self, service, address, **kwargs):
        return service.RegisterNode(
            pb2.RegisterRequest(address=address, **kwargs), NoopContext()
        )

    def promotion_service(self):
        service = ControllerService()
        service.last_primary_epoch = 1
        service.nodes = {
            "candidate:1": ReplicaRecord(
                "candidate:1", 1, ReplicaSyncStatus.SYNCHRONIZED, 1
            ),
            "backup:1": ReplicaRecord(
                "backup:1", 1, ReplicaSyncStatus.SYNCHRONIZED, 1
            ),
        }
        with mock.patch("blindsided.controller.service.threading.Thread.start"):
            service._elect_new_primary(("candidate:1",))
        service.primary_assignment.eligible_backup_addresses = (
            "candidate:1",
            "backup:1",
        )
        return service

    def test_initial_assignment_updates_gauges_without_failover(self):
        service = ControllerService()

        response = self.register(service, "storage:1")

        self.assertTrue(response.success)
        self.assertEqual(self.value("blindsided_registered_replicas"), 1)
        self.assertEqual(self.value("blindsided_healthy_replicas"), 1)
        self.assertEqual(self.value("blindsided_cluster_ready"), 1)
        self.assertEqual(self.value("blindsided_primary_epoch"), 1)
        self.assertEqual(self.value(
            "blindsided_replica_health_transitions_total",
            {"transition": "registered"},
        ), 1)
        self.assertIsNone(self.value(
            "blindsided_failovers_total", {"outcome": "completed"}
        ))

    def test_health_transitions_emit_only_on_state_changes(self):
        service = ControllerService()
        self.register(service, "storage:1")

        service._record_heartbeat_failure("storage:1")
        service._record_heartbeat_failure("storage:1")

        self.assertEqual(self.value("blindsided_healthy_replicas"), 0)
        self.assertEqual(self.value(
            "blindsided_replica_health_transitions_total",
            {"transition": "healthy_to_unhealthy"},
        ), 1)
        self.register(service, "storage:1")
        self.assertEqual(self.value("blindsided_healthy_replicas"), 1)
        self.assertEqual(self.value(
            "blindsided_replica_health_transitions_total",
            {"transition": "unhealthy_to_healthy"},
        ), 1)

        self.register(service, "backup:1")
        service._handle_replica_failure("backup:1")
        service._handle_replica_failure("backup:1")
        self.assertEqual(self.value(
            "blindsided_replica_health_transitions_total",
            {"transition": "removed"},
        ), 1)

    def test_ready_completion_finishes_one_promotion_and_failover(self):
        service = self.promotion_service()
        service.primary_assignment.sync_backup_address = "backup:1"
        storage_stub = mock.Mock()
        storage_stub.CompletePrimaryPromotion.return_value = (
            pb2.CompletePrimaryPromotionResponse(success=True, epoch=2)
        )
        with (
            mock.patch(
                "blindsided.controller.service.grpc.insecure_channel",
                return_value=ChannelContext(),
            ),
            mock.patch(
                "blindsided.controller.service.pb2_grpc.StorageReplicaServiceStub",
                return_value=storage_stub,
            ),
        ):
            response = service.ReportSynchronizationComplete(
                pb2.SynchronizationCompleteRequest(
                    replica_address="backup:1",
                    source_primary_address="candidate:1",
                    epoch=2,
                ),
                NoopContext(),
            )

        self.assertTrue(response.success)
        self.assertEqual(self.value("blindsided_cluster_ready"), 1)
        for prefix in ("failover", "promotion"):
            self.assertEqual(self.value(
                f"blindsided_{prefix}s_total" if prefix == "failover"
                else "blindsided_promotion_attempts_total",
                {"outcome": "completed"},
            ), 1)
            self.assertEqual(self.value(
                f"blindsided_{prefix}_duration_seconds_count",
                {"outcome": "completed"},
            ), 1)

    def test_failed_and_abandoned_terminals_are_idempotent_and_stale_safe(self):
        failed = self.promotion_service()
        candidate, epoch = (
            failed.primary_assignment.primary_address,
            failed.primary_assignment.epoch,
        )
        failed._abandon_promotion_attempt(candidate, epoch, "exhausted")
        failed._abandon_promotion_attempt(candidate, epoch, "repeated")

        self.assertEqual(self.value(
            "blindsided_failovers_total", {"outcome": "failed"}
        ), 1)
        self.assertEqual(self.value("blindsided_cluster_ready"), 0)

        abandoned = self.promotion_service()
        old_candidate = abandoned.primary_assignment.primary_address
        old_epoch = abandoned.primary_assignment.epoch
        abandoned._abandon_promotion_attempt(
            old_candidate, old_epoch, "ambiguous", outcome="abandoned"
        )
        abandoned.primary_assignment = PrimaryAssignment(
            "new:1", old_epoch + 1, PrimaryStatus.PROMOTING
        )
        abandoned._abandon_promotion_attempt(
            old_candidate, old_epoch, "stale", outcome="failed"
        )
        self.assertEqual(self.value(
            "blindsided_failovers_total", {"outcome": "abandoned"}
        ), 1)

    def test_synchronization_attempt_classifies_response_and_timeout(self):
        completed = self.promotion_service()
        success_stub = mock.Mock()
        success_stub.SynchronizeFromPrimary.return_value = (
            pb2.SynchronizeFromPrimaryResponse(success=True, epoch=2)
        )
        with (
            mock.patch(
                "blindsided.controller.service.grpc.insecure_channel",
                return_value=ChannelContext(),
            ),
            mock.patch(
                "blindsided.controller.service.pb2_grpc.StorageReplicaServiceStub",
                return_value=success_stub,
            ),
        ):
            completed._try_next_promotion_backup("candidate:1", 2)
        self.assertEqual(self.value(
            "blindsided_synchronization_attempts_total",
            {"outcome": "completed"},
        ), 1)
        self.assertEqual(self.value(
            "blindsided_synchronization_duration_seconds_count",
            {"outcome": "completed"},
        ), 1)

        timed_out = self.promotion_service()
        timeout_stub = mock.Mock()
        timeout_stub.SynchronizeFromPrimary.side_effect = StatusRpcError(
            grpc.StatusCode.DEADLINE_EXCEEDED
        )
        with (
            mock.patch(
                "blindsided.controller.service.grpc.insecure_channel",
                return_value=ChannelContext(),
            ),
            mock.patch(
                "blindsided.controller.service.pb2_grpc.StorageReplicaServiceStub",
                return_value=timeout_stub,
            ),
        ):
            timed_out._try_next_promotion_backup("candidate:1", 2)
        self.assertEqual(self.value(
            "blindsided_synchronization_attempts_total",
            {"outcome": "timeout"},
        ), 1)
        self.assertEqual(self.value(
            "blindsided_synchronization_duration_seconds_count",
            {"outcome": "timeout"},
        ), 1)
