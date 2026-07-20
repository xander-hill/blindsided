from unittest import mock

from blindsided.controller.service import (
    ControllerService,
    ReplicaRecord,
    ReplicaSyncStatus,
)
from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import BackendTestCase, NoopContext


class ControllerServiceTests(BackendTestCase):
    def test_registers_first_node_as_primary_and_reports_cluster(self):
        service = ControllerService()

        first = service.RegisterNode(
            pb2.RegisterRequest(address="storage-0:50051"),
            NoopContext(),
        )
        second = service.RegisterNode(
            pb2.RegisterRequest(address="storage-1:50051"),
            NoopContext(),
        )
        primary_address = service.GetPrimary(pb2.GetPrimaryRequest(), NoopContext())
        cluster = service.GetClusterInfo(pb2.ClusterInfoRequest(), NoopContext())

        self.assertTrue(first.success)
        self.assertTrue(first.is_primary)
        self.assertTrue(second.success)
        self.assertFalse(second.is_primary)
        self.assertEqual(primary_address.primary_address, "storage-0:50051")
        self.assertEqual(service.nodes["storage-0:50051"].address, "storage-0:50051")
        self.assertGreater(service.nodes["storage-0:50051"].last_seen, 0)
        self.assertEqual(
            service.nodes["storage-0:50051"].sync_status,
            ReplicaSyncStatus.UNSYNCHRONIZED,
        )
        self.assertFalse(service.nodes["storage-0:50051"].promotion_eligible)
        self.assertCountEqual(
            cluster.node_addresses,
            ["storage-0:50051", "storage-1:50051"],
        )

    def test_get_primary_fails_cleanly_when_cluster_is_empty(self):
        service = ControllerService()

        primary_address = service.GetPrimary(pb2.GetPrimaryRequest(), NoopContext())

        self.assertFalse(primary_address.success)
        self.assertEqual(primary_address.message, "No Primary Judge available")

    def test_reports_registered_backup_synchronized_with_current_primary(self):
        service = ControllerService()
        service.RegisterNode(pb2.RegisterRequest(address="storage-0:50051"), NoopContext())
        service.RegisterNode(pb2.RegisterRequest(address="storage-1:50051"), NoopContext())

        response = service.ReportSynchronizationComplete(
            pb2.SynchronizationCompleteRequest(
                replica_address="storage-1:50051",
                source_primary_address="storage-0:50051",
            ),
            NoopContext(),
        )

        self.assertTrue(response.success)
        self.assertEqual(
            service.nodes["storage-1:50051"].sync_status,
            ReplicaSyncStatus.SYNCHRONIZED,
        )
        self.assertTrue(service.nodes["storage-1:50051"].promotion_eligible)

    def test_reregistered_replica_loses_promotion_eligibility(self):
        service = ControllerService()
        service.RegisterNode(pb2.RegisterRequest(address="storage-0:50051"), NoopContext())
        service.RegisterNode(pb2.RegisterRequest(address="storage-1:50051"), NoopContext())
        synchronized = service.ReportSynchronizationComplete(
            pb2.SynchronizationCompleteRequest(
                replica_address="storage-1:50051",
                source_primary_address="storage-0:50051",
            ),
            NoopContext(),
        )
        self.assertTrue(synchronized.success)
        self.assertTrue(service.nodes["storage-1:50051"].promotion_eligible)

        reregistered = service.RegisterNode(
            pb2.RegisterRequest(address="storage-1:50051"), NoopContext()
        )

        self.assertTrue(reregistered.success)
        self.assertFalse(reregistered.is_primary)
        self.assertEqual(
            service.nodes["storage-1:50051"].sync_status,
            ReplicaSyncStatus.UNSYNCHRONIZED,
        )
        self.assertFalse(service.nodes["storage-1:50051"].promotion_eligible)

    def test_rejects_synchronization_for_unknown_backup(self):
        service = ControllerService()
        service.RegisterNode(pb2.RegisterRequest(address="storage-0:50051"), NoopContext())

        response = service.ReportSynchronizationComplete(
            pb2.SynchronizationCompleteRequest(
                replica_address="unknown:50051",
                source_primary_address="storage-0:50051",
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertIn("not registered", response.message)
        self.assertNotIn("unknown:50051", service.nodes)

    def test_rejects_synchronization_from_non_current_primary(self):
        service = ControllerService()
        service.RegisterNode(pb2.RegisterRequest(address="storage-0:50051"), NoopContext())
        service.RegisterNode(pb2.RegisterRequest(address="storage-1:50051"), NoopContext())

        response = service.ReportSynchronizationComplete(
            pb2.SynchronizationCompleteRequest(
                replica_address="storage-1:50051",
                source_primary_address="old-primary:50051",
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertIn("current primary", response.message)
        self.assertEqual(
            service.nodes["storage-1:50051"].sync_status,
            ReplicaSyncStatus.UNSYNCHRONIZED,
        )

    def test_rejects_incomplete_or_primary_self_synchronization_reports(self):
        cases = (
            ("", "storage-0:50051", "required"),
            ("storage-0:50051", "storage-0:50051", "cannot report itself"),
        )
        for replica_address, source_address, expected_message in cases:
            with self.subTest(replica_address=replica_address, source=source_address):
                service = ControllerService()
                service.RegisterNode(pb2.RegisterRequest(address="storage-0:50051"), NoopContext())
                service.RegisterNode(pb2.RegisterRequest(address="storage-1:50051"), NoopContext())

                response = service.ReportSynchronizationComplete(
                    pb2.SynchronizationCompleteRequest(
                        replica_address=replica_address,
                        source_primary_address=source_address,
                    ),
                    NoopContext(),
                )

                self.assertFalse(response.success)
                self.assertIn(expected_message, response.message)
                self.assertEqual(
                    service.nodes["storage-1:50051"].sync_status,
                    ReplicaSyncStatus.UNSYNCHRONIZED,
                )

    def test_election_selects_synchronized_replica_and_skips_unsynchronized(self):
        service = ControllerService()
        service.nodes = {
            "storage-0:50051": ReplicaRecord(
                address="storage-0:50051",
                last_seen=2.0,
                sync_status=ReplicaSyncStatus.UNSYNCHRONIZED,
            ),
            "storage-1:50051": ReplicaRecord(
                address="storage-1:50051",
                last_seen=1.0,
                sync_status=ReplicaSyncStatus.SYNCHRONIZED,
            )
        }

        with mock.patch.object(service, "_notify_promotion") as notify:
            service._elect_new_primary()

        self.assertEqual(service.primary_address, "storage-1:50051")
        notify.assert_called_once_with("storage-1:50051")

    def test_controller_flow_skips_first_unsynchronized_backup_during_election(self):
        service = ControllerService()
        service.RegisterNode(pb2.RegisterRequest(address="primary:50051"), NoopContext())
        service.RegisterNode(
            pb2.RegisterRequest(address="backup-unsynchronized:50051"),
            NoopContext(),
        )
        service.RegisterNode(
            pb2.RegisterRequest(address="backup-synchronized:50051"),
            NoopContext(),
        )
        synchronization = service.ReportSynchronizationComplete(
            pb2.SynchronizationCompleteRequest(
                replica_address="backup-synchronized:50051",
                source_primary_address="primary:50051",
            ),
            NoopContext(),
        )
        del service.nodes["primary:50051"]
        service.primary_address = None

        with mock.patch.object(service, "_notify_promotion") as notify:
            service._elect_new_primary()

        self.assertTrue(synchronization.success)
        self.assertFalse(
            service.nodes["backup-unsynchronized:50051"].promotion_eligible
        )
        self.assertEqual(service.primary_address, "backup-synchronized:50051")
        notify.assert_called_once_with("backup-synchronized:50051")

    def test_election_rejects_when_no_synchronized_replica_exists(self):
        service = ControllerService()
        service.nodes = {
            "storage-1:50051": ReplicaRecord(
                address="storage-1:50051",
                last_seen=1.0,
                sync_status=ReplicaSyncStatus.UNSYNCHRONIZED,
            )
        }

        with mock.patch.object(service, "_notify_promotion") as notify:
            service._elect_new_primary()

        self.assertIsNone(service.primary_address)
        notify.assert_not_called()
