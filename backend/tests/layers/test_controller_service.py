from unittest import mock

import grpc

from blindsided.controller.service import (
    ControllerService,
    PrimaryAssignment,
    PrimaryStatus,
    ReplicaRecord,
    ReplicaSyncStatus,
)
from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import BackendTestCase, ChannelContext, NoopContext


class ControllerServiceTests(BackendTestCase):
    def test_controller_restart_reconciles_primary_epoch_from_reregistration(self):
        restarted = ControllerService()

        backup = restarted.RegisterNode(pb2.RegisterRequest(
            address="backup:50051",
            role="backup",
            epoch=7,
        ), NoopContext())
        primary = restarted.RegisterNode(pb2.RegisterRequest(
            address="primary:50051",
            role="primary",
            epoch=7,
            promotion_ready=True,
            synchronous_backup_address="backup:50051",
        ), NoopContext())

        self.assertFalse(backup.is_primary)
        self.assertTrue(primary.is_primary)
        self.assertEqual(restarted.last_primary_epoch, 7)
        self.assertEqual(restarted.primary_assignment.epoch, 7)
        self.assertEqual(
            restarted.primary_assignment.sync_backup_address,
            "backup:50051",
        )

    def test_rejects_blank_registration_address(self):
        service = ControllerService()

        response = service.RegisterNode(
            pb2.RegisterRequest(address="   "), NoopContext()
        )

        self.assertFalse(response.success)
        self.assertEqual(service.nodes, {})
        self.assertIsNone(service.primary_assignment)

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
        self.assertEqual(first.epoch, 1)
        self.assertTrue(second.success)
        self.assertFalse(second.is_primary)
        self.assertEqual(second.epoch, 1)
        self.assertEqual(primary_address.primary_address, "storage-0:50051")
        self.assertEqual(primary_address.epoch, 1)
        self.assertEqual(service.primary_assignment.epoch, 1)
        self.assertEqual(service.primary_assignment.status, PrimaryStatus.READY)
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
        self.assertEqual(primary_address.epoch, 0)
        self.assertEqual(primary_address.message, "No Primary Judge available")

    def test_reports_registered_backup_synchronized_with_current_primary(self):
        service = ControllerService()
        service.RegisterNode(pb2.RegisterRequest(address="storage-0:50051"), NoopContext())
        service.RegisterNode(pb2.RegisterRequest(address="storage-1:50051"), NoopContext())

        response = service.ReportSynchronizationComplete(
            pb2.SynchronizationCompleteRequest(
                replica_address="storage-1:50051",
                source_primary_address="storage-0:50051",
                epoch=1,
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
                epoch=1,
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

    def test_former_primary_must_resynchronize_before_becoming_promotion_eligible(self):
        service = ControllerService()
        initial = service.RegisterNode(
            pb2.RegisterRequest(address="former-primary:50051"),
            NoopContext(),
        )
        service.RegisterNode(
            pb2.RegisterRequest(address="current-primary:50051"),
            NoopContext(),
        )
        service.primary_assignment = PrimaryAssignment(
            node_id="current-primary:50051",
            epoch=2,
            status=PrimaryStatus.READY,
        )
        service.last_primary_epoch = 2

        reregistered = service.RegisterNode(
            pb2.RegisterRequest(address="former-primary:50051"),
            NoopContext(),
        )

        self.assertTrue(initial.is_primary)
        self.assertFalse(reregistered.is_primary)
        self.assertEqual(reregistered.epoch, 2)
        self.assertEqual(
            service.nodes["former-primary:50051"].sync_status,
            ReplicaSyncStatus.UNSYNCHRONIZED,
        )
        self.assertFalse(
            service.nodes["former-primary:50051"].promotion_eligible
        )

        synchronized = service.ReportSynchronizationComplete(
            pb2.SynchronizationCompleteRequest(
                replica_address="former-primary:50051",
                source_primary_address="current-primary:50051",
                epoch=2,
            ),
            NoopContext(),
        )

        self.assertTrue(synchronized.success)
        self.assertEqual(
            service.nodes["former-primary:50051"].sync_status,
            ReplicaSyncStatus.SYNCHRONIZED,
        )
        self.assertTrue(
            service.nodes["former-primary:50051"].promotion_eligible
        )

    def test_rejects_synchronization_for_unknown_backup(self):
        service = ControllerService()
        service.RegisterNode(pb2.RegisterRequest(address="storage-0:50051"), NoopContext())

        response = service.ReportSynchronizationComplete(
            pb2.SynchronizationCompleteRequest(
                replica_address="unknown:50051",
                source_primary_address="storage-0:50051",
                epoch=1,
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
                epoch=1,
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
                        epoch=1,
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

        self.assertEqual(service.primary_assignment.node_id, "storage-1:50051")
        self.assertEqual(service.primary_assignment.epoch, 1)
        self.assertEqual(service.primary_assignment.status, PrimaryStatus.PROMOTING)
        notify.assert_called_once_with("storage-1:50051", 1)

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
                epoch=1,
            ),
            NoopContext(),
        )
        del service.nodes["primary:50051"]
        service.primary_assignment = None

        with mock.patch.object(service, "_notify_promotion") as notify:
            service._elect_new_primary()

        self.assertTrue(synchronization.success)
        self.assertFalse(
            service.nodes["backup-unsynchronized:50051"].promotion_eligible
        )
        self.assertEqual(
            service.primary_assignment.node_id, "backup-synchronized:50051"
        )
        self.assertEqual(service.primary_assignment.epoch, 2)
        self.assertEqual(service.primary_assignment.status, PrimaryStatus.PROMOTING)
        notify.assert_called_once_with("backup-synchronized:50051", 2)

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

        self.assertIsNone(service.primary_assignment)
        notify.assert_not_called()

    def test_controller_sends_primary_assignment_epoch_and_remains_promoting(self):
        service = ControllerService()
        service.primary_assignment = PrimaryAssignment(
            node_id="storage-1:50051",
            epoch=7,
            status=PrimaryStatus.PROMOTING,
        )
        service.nodes["storage-1:50051"] = ReplicaRecord(
            address="storage-1:50051",
            last_seen=1.0,
            sync_status=ReplicaSyncStatus.SYNCHRONIZED,
        )
        storage_stub = mock.Mock()
        storage_stub.BeginPrimaryPromotion.return_value = (
            pb2.BeginPrimaryPromotionResponse(accepted=True, epoch=7)
        )
        storage_stub.ConfirmPromotionState.return_value = (
            pb2.PromotionStateConfirmationResponse(confirmed=True, epoch=7)
        )

        promoting = service.GetPrimary(pb2.GetPrimaryRequest(), NoopContext())
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
            service._notify_promotion("storage-1:50051", 7)
        still_promoting = service.GetPrimary(pb2.GetPrimaryRequest(), NoopContext())

        self.assertFalse(promoting.success)
        self.assertEqual(promoting.epoch, 7)
        self.assertIn("not complete", promoting.message)
        self.assertIsNone(service.primary_assignment)
        self.assertFalse(still_promoting.success)
        self.assertEqual(still_promoting.epoch, 0)
        promotion_request = storage_stub.BeginPrimaryPromotion.call_args.args[0]
        self.assertEqual(promotion_request.epoch, 7)
        confirmation_request = storage_stub.ConfirmPromotionState.call_args.args[0]
        self.assertEqual(confirmation_request.epoch, 7)

    def test_stale_promotion_ack_cannot_mark_newer_assignment_ready(self):
        service = ControllerService()
        service.primary_assignment = PrimaryAssignment(
            node_id="storage-1:50051",
            epoch=8,
            status=PrimaryStatus.PROMOTING,
        )
        storage_stub = mock.Mock()
        storage_stub.BeginPrimaryPromotion.return_value = (
            pb2.BeginPrimaryPromotionResponse(accepted=True, epoch=7)
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
            service._notify_promotion("storage-1:50051", 7)

        self.assertEqual(service.primary_assignment.epoch, 8)
        self.assertEqual(service.primary_assignment.status, PrimaryStatus.PROMOTING)

    def test_controller_does_not_confirm_candidate_that_lost_synchronized_status(self):
        service = ControllerService()
        service.primary_assignment = PrimaryAssignment(
            node_id="storage-1:50051",
            epoch=7,
            status=PrimaryStatus.PROMOTING,
        )
        service.nodes["storage-1:50051"] = ReplicaRecord(
            address="storage-1:50051",
            last_seen=1.0,
            sync_status=ReplicaSyncStatus.UNSYNCHRONIZED,
        )

        with mock.patch(
            "blindsided.controller.service.grpc.insecure_channel"
        ) as channel:
            service._notify_promotion("storage-1:50051", 7)

        channel.assert_not_called()
        self.assertEqual(service.primary_assignment.status, PrimaryStatus.PROMOTING)

    def test_controller_does_not_continue_promotion_after_failed_confirmation(self):
        service = ControllerService()
        service.primary_assignment = PrimaryAssignment(
            node_id="storage-1:50051",
            epoch=7,
            status=PrimaryStatus.PROMOTING,
        )
        service.nodes["storage-1:50051"] = ReplicaRecord(
            address="storage-1:50051",
            last_seen=1.0,
            sync_status=ReplicaSyncStatus.SYNCHRONIZED,
        )
        storage_stub = mock.Mock()
        storage_stub.BeginPrimaryPromotion.return_value = (
            pb2.BeginPrimaryPromotionResponse(accepted=True, epoch=7)
        )
        storage_stub.ConfirmPromotionState.return_value = (
            pb2.PromotionStateConfirmationResponse(confirmed=False, epoch=7)
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
            service._notify_promotion("storage-1:50051", 7)

        storage_stub.BeginPrimaryPromotion.assert_called_once()
        storage_stub.ConfirmPromotionState.assert_called_once()
        self.assertIsNone(service.primary_assignment)

    def test_controller_designates_and_synchronizes_non_primary_after_confirmation(self):
        service = ControllerService()
        service.primary_assignment = PrimaryAssignment(
            node_id="candidate:50051",
            epoch=7,
            status=PrimaryStatus.PROMOTING,
        )
        service.nodes = {
            "candidate:50051": ReplicaRecord(
                address="candidate:50051",
                last_seen=2.0,
                sync_status=ReplicaSyncStatus.SYNCHRONIZED,
            ),
            "backup:50051": ReplicaRecord(
                address="backup:50051",
                last_seen=1.0,
                sync_status=ReplicaSyncStatus.SYNCHRONIZED,
            ),
        }
        candidate_stub = mock.Mock()
        candidate_stub.BeginPrimaryPromotion.return_value = (
            pb2.BeginPrimaryPromotionResponse(accepted=True, epoch=7)
        )
        candidate_stub.ConfirmPromotionState.return_value = (
            pb2.PromotionStateConfirmationResponse(confirmed=True, epoch=7)
        )
        backup_stub = mock.Mock()

        def synchronize(request, **kwargs):
            self.assertGreater(kwargs["timeout"], 0)
            self.assertEqual(
                service.nodes["backup:50051"].sync_status,
                ReplicaSyncStatus.UNSYNCHRONIZED,
            )
            self.assertEqual(
                service.primary_assignment.sync_backup_address,
                "backup:50051",
            )
            return pb2.SynchronizeFromPrimaryResponse(success=True, epoch=7)

        backup_stub.SynchronizeFromPrimary.side_effect = synchronize

        with (
            mock.patch(
                "blindsided.controller.service.grpc.insecure_channel",
                return_value=ChannelContext(),
            ),
            mock.patch(
                "blindsided.controller.service.pb2_grpc.StorageReplicaServiceStub",
                side_effect=[candidate_stub, backup_stub],
            ),
        ):
            service._notify_promotion("candidate:50051", 7)

        self.assertEqual(
            service.nodes["backup:50051"].sync_status,
            ReplicaSyncStatus.UNSYNCHRONIZED,
        )
        self.assertEqual(
            service.primary_assignment.sync_backup_address,
            "backup:50051",
        )
        request = backup_stub.SynchronizeFromPrimary.call_args.args[0]
        self.assertEqual(request.primary_address, "candidate:50051")
        self.assertEqual(request.epoch, 7)
        self.assertEqual(service.primary_assignment.status, PrimaryStatus.PROMOTING)

    def test_matching_completion_marks_designated_promotion_backup_synchronized(self):
        service = ControllerService()
        service.primary_assignment = PrimaryAssignment(
            node_id="candidate:50051",
            epoch=7,
            status=PrimaryStatus.PROMOTING,
            sync_backup_address="backup:50051",
        )
        service.nodes = {
            "candidate:50051": ReplicaRecord(
                address="candidate:50051",
                last_seen=2.0,
                sync_status=ReplicaSyncStatus.SYNCHRONIZED,
            ),
            "backup:50051": ReplicaRecord(
                address="backup:50051",
                last_seen=1.0,
                sync_status=ReplicaSyncStatus.UNSYNCHRONIZED,
            ),
        }
        storage_stub = mock.Mock()

        def complete(request, **kwargs):
            self.assertGreater(kwargs["timeout"], 0)
            self.assertEqual(service.primary_assignment.status, PrimaryStatus.PROMOTING)
            return pb2.CompletePrimaryPromotionResponse(success=True, epoch=7)

        storage_stub.CompletePrimaryPromotion.side_effect = complete

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
                    replica_address="backup:50051",
                    source_primary_address="candidate:50051",
                    epoch=7,
                ),
                NoopContext(),
            )

        self.assertTrue(response.success)
        self.assertEqual(
            service.nodes["backup:50051"].sync_status,
            ReplicaSyncStatus.SYNCHRONIZED,
        )
        completion_request = storage_stub.CompletePrimaryPromotion.call_args.args[0]
        self.assertEqual(completion_request.epoch, 7)
        self.assertEqual(completion_request.backup_address, "backup:50051")
        self.assertEqual(service.primary_assignment.status, PrimaryStatus.READY)

    def test_controller_remains_promoting_when_storage_activation_fails(self):
        service = ControllerService()
        service.primary_assignment = PrimaryAssignment(
            node_id="candidate:50051",
            epoch=7,
            status=PrimaryStatus.PROMOTING,
            sync_backup_address="backup:50051",
        )
        service.nodes = {
            "candidate:50051": ReplicaRecord(
                address="candidate:50051",
                last_seen=2.0,
                sync_status=ReplicaSyncStatus.SYNCHRONIZED,
            ),
            "backup:50051": ReplicaRecord(
                address="backup:50051",
                last_seen=1.0,
                sync_status=ReplicaSyncStatus.UNSYNCHRONIZED,
            ),
        }
        storage_stub = mock.Mock()
        storage_stub.CompletePrimaryPromotion.return_value = (
            pb2.CompletePrimaryPromotionResponse(success=False, epoch=7)
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
                    replica_address="backup:50051",
                    source_primary_address="candidate:50051",
                    epoch=7,
                ),
                NoopContext(),
            )

        self.assertFalse(response.success)
        self.assertIsNone(service.primary_assignment)
        self.assertNotIn("candidate:50051", service.nodes)

    def test_stale_completion_cannot_activate_newer_primary_assignment(self):
        service = ControllerService()
        service.primary_assignment = PrimaryAssignment(
            node_id="candidate:50051",
            epoch=7,
            status=PrimaryStatus.PROMOTING,
            sync_backup_address="backup:50051",
        )
        service.nodes = {
            "candidate:50051": ReplicaRecord(
                address="candidate:50051",
                last_seen=2.0,
                sync_status=ReplicaSyncStatus.SYNCHRONIZED,
            ),
            "backup:50051": ReplicaRecord(
                address="backup:50051",
                last_seen=1.0,
                sync_status=ReplicaSyncStatus.UNSYNCHRONIZED,
            ),
        }
        storage_stub = mock.Mock()

        def complete(_request, **kwargs):
            self.assertGreater(kwargs["timeout"], 0)
            service.primary_assignment = PrimaryAssignment(
                node_id="new-candidate:50051",
                epoch=8,
                status=PrimaryStatus.PROMOTING,
            )
            return pb2.CompletePrimaryPromotionResponse(success=True, epoch=7)

        storage_stub.CompletePrimaryPromotion.side_effect = complete

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
                    replica_address="backup:50051",
                    source_primary_address="candidate:50051",
                    epoch=7,
                ),
                NoopContext(),
            )

        self.assertFalse(response.success)
        self.assertEqual(service.primary_assignment.epoch, 8)
        self.assertEqual(service.primary_assignment.status, PrimaryStatus.PROMOTING)

    def test_get_primary_exposes_address_only_after_assignment_is_ready(self):
        service = ControllerService()
        service.primary_assignment = PrimaryAssignment(
            node_id="candidate:50051",
            epoch=7,
            status=PrimaryStatus.PROMOTING,
        )

        promoting = service.GetPrimary(pb2.GetPrimaryRequest(), NoopContext())
        service.primary_assignment.status = PrimaryStatus.READY
        ready = service.GetPrimary(pb2.GetPrimaryRequest(), NoopContext())

        self.assertFalse(promoting.success)
        self.assertEqual(promoting.primary_address, "")
        self.assertEqual(promoting.epoch, 7)
        self.assertTrue(ready.success)
        self.assertEqual(ready.primary_address, "candidate:50051")
        self.assertEqual(ready.epoch, 7)

    def test_wrong_backup_source_or_epoch_cannot_complete_promotion_sync(self):
        cases = (
            ("other:50051", "candidate:50051", 7),
            ("backup:50051", "old-primary:50051", 7),
            ("backup:50051", "candidate:50051", 6),
        )
        for replica_address, source_address, epoch in cases:
            with self.subTest(
                replica=replica_address, source=source_address, epoch=epoch
            ):
                service = ControllerService()
                service.primary_assignment = PrimaryAssignment(
                    node_id="candidate:50051",
                    epoch=7,
                    status=PrimaryStatus.PROMOTING,
                    sync_backup_address="backup:50051",
                )
                service.nodes = {
                    "candidate:50051": ReplicaRecord(
                        address="candidate:50051",
                        last_seen=2.0,
                        sync_status=ReplicaSyncStatus.SYNCHRONIZED,
                    ),
                    "backup:50051": ReplicaRecord(
                        address="backup:50051",
                        last_seen=1.0,
                        sync_status=ReplicaSyncStatus.UNSYNCHRONIZED,
                    ),
                    "other:50051": ReplicaRecord(
                        address="other:50051",
                        last_seen=1.0,
                        sync_status=ReplicaSyncStatus.UNSYNCHRONIZED,
                    ),
                }

                response = service.ReportSynchronizationComplete(
                    pb2.SynchronizationCompleteRequest(
                        replica_address=replica_address,
                        source_primary_address=source_address,
                        epoch=epoch,
                    ),
                    NoopContext(),
                )

                self.assertFalse(response.success)
                self.assertEqual(
                    service.nodes["backup:50051"].sync_status,
                    ReplicaSyncStatus.UNSYNCHRONIZED,
                )

    def test_no_available_backup_leaves_promotion_incomplete(self):
        service = ControllerService()
        service.primary_assignment = PrimaryAssignment(
            node_id="candidate:50051",
            epoch=7,
            status=PrimaryStatus.PROMOTING,
        )
        service.nodes["candidate:50051"] = ReplicaRecord(
            address="candidate:50051",
            last_seen=1.0,
            sync_status=ReplicaSyncStatus.SYNCHRONIZED,
        )
        candidate_stub = mock.Mock()
        candidate_stub.BeginPrimaryPromotion.return_value = (
            pb2.BeginPrimaryPromotionResponse(accepted=True, epoch=7)
        )
        candidate_stub.ConfirmPromotionState.return_value = (
            pb2.PromotionStateConfirmationResponse(confirmed=True, epoch=7)
        )

        with (
            mock.patch(
                "blindsided.controller.service.grpc.insecure_channel",
                return_value=ChannelContext(),
            ),
            mock.patch(
                "blindsided.controller.service.pb2_grpc.StorageReplicaServiceStub",
                return_value=candidate_stub,
            ),
        ):
            service._notify_promotion("candidate:50051", 7)

        self.assertIsNone(service.primary_assignment)
        candidate_stub.SynchronizeFromPrimary.assert_not_called()

    def test_candidate_and_backup_selection_are_deterministic(self):
        service = ControllerService()
        service.nodes = {
            address: ReplicaRecord(
                address=address,
                last_seen=1.0,
                sync_status=ReplicaSyncStatus.SYNCHRONIZED,
            )
            for address in ("z:1", "a:1", "m:1")
        }
        assignment = PrimaryAssignment(
            node_id="a:1",
            epoch=2,
            status=PrimaryStatus.PROMOTING,
            eligible_backup_addresses=("z:1", "m:1", "a:1"),
        )

        with service.lock:
            candidate = service._select_primary_candidate_locked()
            backup = service._select_backup_locked("a:1", assignment)

        self.assertEqual(candidate, "a:1")
        self.assertEqual(backup, "m:1")

    def test_heartbeat_rpc_runs_without_controller_lock_and_has_deadline(self):
        service = ControllerService()
        service.nodes["replica:1"] = ReplicaRecord(
            address="replica:1",
            last_seen=1.0,
            sync_status=ReplicaSyncStatus.UNSYNCHRONIZED,
        )
        stub = mock.Mock()

        def heartbeat(_request, **kwargs):
            acquired = service.lock.acquire(blocking=False)
            self.assertTrue(acquired)
            service.lock.release()
            self.assertEqual(kwargs["timeout"], service._heartbeat_timeout)
            return pb2.HealthCheckResponse(alive=True)

        stub.Heartbeat.side_effect = heartbeat
        with (
            mock.patch(
                "blindsided.controller.service.grpc.insecure_channel",
                return_value=ChannelContext(),
            ),
            mock.patch(
                "blindsided.controller.service.pb2_grpc.StorageReplicaServiceStub",
                return_value=stub,
            ),
            mock.patch(
                "blindsided.controller.service.time.sleep",
                side_effect=[None, StopIteration],
            ),
        ):
            with self.assertRaises(StopIteration):
                service._monitor_heartbeats()

    def test_promotion_rpcs_run_without_lock_and_have_deadlines(self):
        service = ControllerService()
        service.primary_assignment = PrimaryAssignment(
            node_id="candidate:1",
            epoch=7,
            status=PrimaryStatus.PROMOTING,
            eligible_backup_addresses=("candidate:1", "backup:1"),
        )
        service.nodes = {
            address: ReplicaRecord(
                address=address,
                last_seen=1.0,
                sync_status=ReplicaSyncStatus.UNSYNCHRONIZED,
            )
            for address in ("candidate:1", "backup:1")
        }

        def unlocked(response, expected_timeout):
            def call(_request, **kwargs):
                acquired = service.lock.acquire(blocking=False)
                self.assertTrue(acquired)
                service.lock.release()
                self.assertEqual(kwargs["timeout"], expected_timeout)
                return response
            return call

        candidate_stub = mock.Mock()
        candidate_stub.BeginPrimaryPromotion.side_effect = unlocked(
            pb2.BeginPrimaryPromotionResponse(accepted=True, epoch=7),
            service._promotion_timeout,
        )
        candidate_stub.ConfirmPromotionState.side_effect = unlocked(
            pb2.PromotionStateConfirmationResponse(confirmed=True, epoch=7),
            service._promotion_timeout,
        )
        backup_stub = mock.Mock()
        backup_stub.SynchronizeFromPrimary.side_effect = unlocked(
            pb2.SynchronizeFromPrimaryResponse(success=True, epoch=7),
            service._synchronization_timeout,
        )
        with (
            mock.patch(
                "blindsided.controller.service.grpc.insecure_channel",
                return_value=ChannelContext(),
            ),
            mock.patch(
                "blindsided.controller.service.pb2_grpc.StorageReplicaServiceStub",
                side_effect=[candidate_stub, backup_stub],
            ),
        ):
            service._notify_promotion("candidate:1", 7)

        backup_stub.SynchronizeFromPrimary.assert_called_once()

    def test_primary_failure_starts_only_one_election(self):
        service = ControllerService()
        service.primary_assignment = PrimaryAssignment(
            node_id="primary:1", epoch=1, status=PrimaryStatus.READY
        )
        service.nodes = {
            "primary:1": ReplicaRecord(
                "primary:1", 1.0, ReplicaSyncStatus.UNSYNCHRONIZED
            ),
            "backup:1": ReplicaRecord(
                "backup:1", 1.0, ReplicaSyncStatus.SYNCHRONIZED, 1
            ),
        }

        with mock.patch.object(service, "_elect_new_primary") as elect:
            service._handle_replica_failure("primary:1")
            service._handle_replica_failure("primary:1")

        elect.assert_called_once()

    def test_removing_selected_backup_repairs_assignment_reference(self):
        service = ControllerService()
        service.primary_assignment = PrimaryAssignment(
            node_id="candidate:1",
            epoch=2,
            status=PrimaryStatus.PROMOTING,
            sync_backup_address="backup:1",
            eligible_backup_addresses=("candidate:1", "backup:1"),
        )
        service.nodes = {
            "candidate:1": ReplicaRecord(
                "candidate:1", 1.0, ReplicaSyncStatus.UNSYNCHRONIZED
            ),
            "backup:1": ReplicaRecord(
                "backup:1", 1.0, ReplicaSyncStatus.UNSYNCHRONIZED
            ),
        }

        service._handle_replica_failure("backup:1")

        self.assertNotIn("backup:1", service.nodes)
        self.assertIn("candidate:1", service.nodes)
        self.assertIsNone(service.primary_assignment)

    def test_new_assignment_invalidates_old_epoch_synchronization(self):
        service = ControllerService()
        service.last_primary_epoch = 1
        service.nodes = {
            address: ReplicaRecord(
                address, 1.0, ReplicaSyncStatus.SYNCHRONIZED, 1
            )
            for address in ("candidate:1", "backup:1")
        }

        with mock.patch.object(service, "_notify_promotion"):
            service._elect_new_primary()

        self.assertEqual(service.primary_assignment.epoch, 2)
        self.assertTrue(
            all(
                replica.sync_status == ReplicaSyncStatus.UNSYNCHRONIZED
                and replica.synchronized_epoch == 0
                for replica in service.nodes.values()
            )
        )

    def test_candidate_rejection_evicts_candidate_and_tries_next(self):
        for rejected_stage in ("begin", "confirm"):
            with self.subTest(stage=rejected_stage):
                service = ControllerService()
                service.last_primary_epoch = 7
                service.primary_assignment = PrimaryAssignment(
                    node_id="a:1",
                    epoch=7,
                    status=PrimaryStatus.PROMOTING,
                    eligible_backup_addresses=("a:1", "b:1", "c:1"),
                )
                service.nodes = {
                    address: ReplicaRecord(
                        address, 1.0, ReplicaSyncStatus.UNSYNCHRONIZED
                    )
                    for address in ("a:1", "b:1", "c:1")
                }
                stub = mock.Mock()
                stub.BeginPrimaryPromotion.return_value = (
                    pb2.BeginPrimaryPromotionResponse(
                        accepted=rejected_stage != "begin", epoch=7
                    )
                )
                stub.ConfirmPromotionState.return_value = (
                    pb2.PromotionStateConfirmationResponse(
                        confirmed=False, epoch=7
                    )
                )
                run_promotion = service._notify_promotion
                with (
                    mock.patch(
                        "blindsided.controller.service.grpc.insecure_channel",
                        return_value=ChannelContext(),
                    ),
                    mock.patch(
                        "blindsided.controller.service.pb2_grpc.StorageReplicaServiceStub",
                        return_value=stub,
                    ),
                    mock.patch.object(service, "_notify_promotion") as notify,
                ):
                    run_promotion("a:1", 7)

                self.assertNotIn("a:1", service.nodes)
                self.assertEqual(service.primary_assignment.primary_address, "b:1")
                self.assertEqual(service.primary_assignment.epoch, 8)
                notify.assert_called_once_with("b:1", 8)

    def test_candidate_rpc_timeout_uses_candidate_failure_recovery(self):
        service = ControllerService()
        service.last_primary_epoch = 3
        service.primary_assignment = PrimaryAssignment(
            node_id="a:1",
            epoch=3,
            status=PrimaryStatus.PROMOTING,
            eligible_backup_addresses=("a:1", "b:1"),
        )
        service.nodes = {
            address: ReplicaRecord(
                address, 1.0, ReplicaSyncStatus.UNSYNCHRONIZED
            )
            for address in ("a:1", "b:1")
        }
        stub = mock.Mock()
        stub.BeginPrimaryPromotion.side_effect = grpc.RpcError("timeout")
        run_promotion = service._notify_promotion

        with (
            mock.patch(
                "blindsided.controller.service.grpc.insecure_channel",
                return_value=ChannelContext(),
            ),
            mock.patch(
                "blindsided.controller.service.pb2_grpc.StorageReplicaServiceStub",
                return_value=stub,
            ),
            mock.patch.object(service, "_notify_promotion"),
        ):
            run_promotion("a:1", 3)

        self.assertNotIn("a:1", service.nodes)
        self.assertEqual(service.primary_assignment.primary_address, "b:1")
        self.assertEqual(service.primary_assignment.epoch, 4)

    def test_backup_failures_retry_once_each_in_deterministic_order(self):
        for first_failure in ("rejection", "timeout"):
            with self.subTest(failure=first_failure):
                service = ControllerService()
                service.primary_assignment = PrimaryAssignment(
                    node_id="candidate:1",
                    epoch=5,
                    status=PrimaryStatus.PROMOTING,
                    eligible_backup_addresses=("z:1", "candidate:1", "b:1"),
                )
                service.nodes = {
                    address: ReplicaRecord(
                        address, 1.0, ReplicaSyncStatus.UNSYNCHRONIZED
                    )
                    for address in ("candidate:1", "z:1", "b:1")
                }
                failed_stub = mock.Mock()
                if first_failure == "timeout":
                    failed_stub.SynchronizeFromPrimary.side_effect = grpc.RpcError(
                        "timeout"
                    )
                else:
                    failed_stub.SynchronizeFromPrimary.return_value = (
                        pb2.SynchronizeFromPrimaryResponse(success=False, epoch=5)
                    )
                successful_stub = mock.Mock()
                successful_stub.SynchronizeFromPrimary.return_value = (
                    pb2.SynchronizeFromPrimaryResponse(success=True, epoch=5)
                )
                with (
                    mock.patch(
                        "blindsided.controller.service.grpc.insecure_channel",
                        return_value=ChannelContext(),
                    ),
                    mock.patch(
                        "blindsided.controller.service.pb2_grpc.StorageReplicaServiceStub",
                        side_effect=[failed_stub, successful_stub],
                    ),
                ):
                    service._try_next_promotion_backup("candidate:1", 5)

                self.assertNotIn("b:1", service.nodes)
                self.assertIn("candidate:1", service.nodes)
                self.assertEqual(service.primary_assignment.sync_backup_address, "z:1")
                self.assertEqual(
                    service.primary_assignment.attempted_backup_addresses,
                    {"b:1", "z:1"},
                )
                failed_stub.SynchronizeFromPrimary.assert_called_once()
                successful_stub.SynchronizeFromPrimary.assert_called_once()

    def test_exhausted_backups_leave_cluster_unavailable_without_candidate_eviction(self):
        service = ControllerService()
        service.last_primary_epoch = 9
        service.primary_assignment = PrimaryAssignment(
            node_id="candidate:1",
            epoch=9,
            status=PrimaryStatus.PROMOTING,
            eligible_backup_addresses=("candidate:1",),
        )
        service.nodes["candidate:1"] = ReplicaRecord(
            "candidate:1", 1.0, ReplicaSyncStatus.UNSYNCHRONIZED
        )

        with mock.patch.object(service, "_elect_new_primary") as elect:
            service._try_next_promotion_backup("candidate:1", 9)

        self.assertIn("candidate:1", service.nodes)
        self.assertIsNone(service.primary_assignment)
        self.assertFalse(service._election_in_progress)
        elect.assert_not_called()
        self.assertFalse(service.GetPrimary(pb2.GetPrimaryRequest(), NoopContext()).success)

    def test_stale_failure_handlers_cannot_change_newer_assignment(self):
        service = ControllerService()
        service.primary_assignment = PrimaryAssignment(
            node_id="new:1",
            epoch=11,
            status=PrimaryStatus.PROMOTING,
            sync_backup_address="new-backup:1",
        )
        service.nodes = {
            address: ReplicaRecord(
                address, 1.0, ReplicaSyncStatus.UNSYNCHRONIZED
            )
            for address in ("old:1", "old-backup:1", "new:1", "new-backup:1")
        }

        service._handle_candidate_promotion_failure("old:1", 10, "stale")
        service._handle_promotion_backup_failure(
            "old:1", 10, "old-backup:1", "stale"
        )

        self.assertEqual(service.primary_assignment.primary_address, "new:1")
        self.assertEqual(service.primary_assignment.sync_backup_address, "new-backup:1")
        self.assertIn("old:1", service.nodes)
        self.assertIn("old-backup:1", service.nodes)

    def test_completion_timeout_abandons_without_ready_or_candidate_eviction(self):
        service = ControllerService()
        service.last_primary_epoch = 7
        service.primary_assignment = PrimaryAssignment(
            node_id="candidate:1",
            epoch=7,
            status=PrimaryStatus.PROMOTING,
            sync_backup_address="backup:1",
        )
        service.nodes = {
            "candidate:1": ReplicaRecord(
                "candidate:1", 1.0, ReplicaSyncStatus.UNSYNCHRONIZED
            ),
            "backup:1": ReplicaRecord(
                "backup:1", 1.0, ReplicaSyncStatus.UNSYNCHRONIZED
            ),
        }
        stub = mock.Mock()
        stub.CompletePrimaryPromotion.side_effect = grpc.RpcError("timeout")
        with (
            mock.patch(
                "blindsided.controller.service.grpc.insecure_channel",
                return_value=ChannelContext(),
            ),
            mock.patch(
                "blindsided.controller.service.pb2_grpc.StorageReplicaServiceStub",
                return_value=stub,
            ),
        ):
            response = service.ReportSynchronizationComplete(
                pb2.SynchronizationCompleteRequest(
                    replica_address="backup:1",
                    source_primary_address="candidate:1",
                    epoch=7,
                ),
                NoopContext(),
            )

        self.assertFalse(response.success)
        self.assertIsNone(service.primary_assignment)
        self.assertIn("candidate:1", service.nodes)

    def test_recovery_registration_does_not_bypass_promotion(self):
        service = ControllerService()
        service.last_primary_epoch = 4

        response = service.RegisterNode(
            pb2.RegisterRequest(address="new:1"), NoopContext()
        )

        self.assertTrue(response.success)
        self.assertFalse(response.is_primary)
        self.assertEqual(response.epoch, 4)
        self.assertIsNone(service.primary_assignment)
        self.assertFalse(service.nodes["new:1"].promotion_eligible)
