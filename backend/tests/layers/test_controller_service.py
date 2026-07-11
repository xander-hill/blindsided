from unittest import mock

from blindsided.controller.service import ControllerService
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
        primary = service.GetPrimary(pb2.GetPrimaryRequest(), NoopContext())
        cluster = service.GetClusterInfo(pb2.ClusterInfoRequest(), NoopContext())

        self.assertTrue(first.success)
        self.assertTrue(first.is_primary)
        self.assertTrue(second.success)
        self.assertFalse(second.is_primary)
        self.assertEqual(primary.primary_address, "storage-0:50051")
        self.assertCountEqual(
            cluster.node_addresses,
            ["storage-0:50051", "storage-1:50051"],
        )

    def test_get_primary_fails_cleanly_when_cluster_is_empty(self):
        service = ControllerService()

        primary = service.GetPrimary(pb2.GetPrimaryRequest(), NoopContext())

        self.assertFalse(primary.success)
        self.assertEqual(primary.message, "No Primary Judge available")

    def test_elect_new_primary_promotes_remaining_node(self):
        service = ControllerService()
        service.nodes = {"storage-1:50051": 1.0}

        with mock.patch.object(service, "NotifyPromotion") as notify:
            service.ElectNewPrimary()

        self.assertEqual(service.primary_address, "storage-1:50051")
        notify.assert_called_once_with("storage-1:50051")
