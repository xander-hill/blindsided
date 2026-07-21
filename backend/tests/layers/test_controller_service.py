from unittest import mock

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
        self.assertEqual(service.primary_assignment.status, PrimaryStatus.PROMOTING)
        self.assertFalse(still_promoting.success)
        self.assertEqual(still_promoting.epoch, 7)
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
        self.assertEqual(service.primary_assignment.status, PrimaryStatus.PROMOTING)

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

        def synchronize(request):
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

        def complete(request):
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
        self.assertEqual(service.primary_assignment.status, PrimaryStatus.PROMOTING)

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

        def complete(_request):
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

        self.assertEqual(service.primary_assignment.status, PrimaryStatus.PROMOTING)
        self.assertIsNone(service.primary_assignment.sync_backup_address)
        candidate_stub.SynchronizeFromPrimary.assert_not_called()
