import unittest
from unittest import mock

import grpc

from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc
from blindsided.storage.server import _start_grpc_server
from blindsided.storage.service import StorageReplicaService
from tests.helpers import free_port, make_judge


class StorageServerBootstrapTests(unittest.TestCase):
    def test_connection_bootstrap_can_be_deferred_until_after_server_start(self):
        with mock.patch.object(
            StorageReplicaService,
            "_initialize_connection",
        ) as initialize:
            node = StorageReplicaService(initialize_connection=False)

        initialize.assert_not_called()
        self.assertEqual(node.replica_role, "backup")
        self.assertFalse(node.promotion_ready)
        self.assertFalse(node._storage_metrics_ready)

    def test_endpoint_answers_heartbeat_before_replica_is_ready(self):
        node = make_judge(role="backup", address="127.0.0.1:0")
        node.current_epoch = 0
        node.promotion_ready = False
        node._storage_metrics_ready = False
        port = free_port()
        server = _start_grpc_server(node, str(port))
        try:
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                grpc.channel_ready_future(channel).result(timeout=2)
                stub = pb2_grpc.StorageReplicaServiceStub(channel)

                heartbeat = stub.Heartbeat(pb2.HealthCheckRequest(), timeout=2)
                authoritative_read = stub.GetAuction(
                    pb2.StorageGetAuctionRequest(auction_id="auction-1"),
                    timeout=2,
                )

            self.assertTrue(heartbeat.alive)
            self.assertEqual(heartbeat.role, "backup")
            self.assertFalse(heartbeat.promotion_ready)
            self.assertFalse(node._storage_metrics_ready)
            self.assertFalse(authoritative_read.ok)
            self.assertEqual(
                authoritative_read.failure_reason,
                pb2.READ_FAILURE_REASON_NOT_PRIMARY,
            )
        finally:
            server.stop(0).wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
