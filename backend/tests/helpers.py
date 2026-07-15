import os
import socket
import unittest
from concurrent import futures
from contextlib import ExitStack, contextmanager
from unittest import mock

import grpc
from google.protobuf import timestamp_pb2

from blindsided.auction_service import service as auction_service_module
from blindsided.auction_service.service import AuctionService
from blindsided.controller.service import ControllerService
from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc
from blindsided.storage import service as storage_service_module
from blindsided.storage.service import StorageReplicaService


class NoopContext:
    def is_active(self):
        return True


class ChannelContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def future_timestamp() -> timestamp_pb2.Timestamp:
    return timestamp_pb2.Timestamp(seconds=4102444800)


def active_bid(amount: float, acceptance_order: int = 0) -> pb2.ActiveBid:
    return pb2.ActiveBid(amount=amount, acceptance_order=acceptance_order)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_judge(
    *,
    role: str = "backup",
    peers: list[str] | None = None,
    address: str = "storage-0.storage-service:50051",
    state_file_path: str = "",
) -> StorageReplicaService:
    judge = StorageReplicaService.__new__(StorageReplicaService)
    judge.state_lock = storage_service_module.threading.Condition()
    judge.auction_store = {}
    judge.port = address.rsplit(":", 1)[-1]
    judge.replica_role = role
    judge.peer_addresses = peers or []
    judge.node_address = address
    judge.state_file_path = state_file_path
    return judge


@contextmanager
def running_backend_stack():
    stack = ExitStack()
    servers: list[grpc.Server] = []

    def start(server: grpc.Server):
        server.start()
        servers.append(server)
        return server

    try:
        controller_service = ControllerService()
        controller_server = grpc.server(futures.ThreadPoolExecutor(max_workers=20))
        pb2_grpc.add_ClusterControllerServicer_to_server(
            controller_service,
            controller_server,
        )
        controller_port = free_port()
        controller_addr = f"127.0.0.1:{controller_port}"
        controller_server.add_insecure_port(controller_addr)
        start(controller_server)

        storage_port = free_port()
        storage_addr = f"127.0.0.1:{storage_port}"
        stack.enter_context(mock.patch.dict(
            os.environ,
            {
                "NODE_PORT": str(storage_port),
                "POD_IP": "127.0.0.1",
                "NODE_ROLE": "backup",
                "PEER_ADDRESSES": "",
            },
            clear=False,
        ))
        stack.enter_context(mock.patch.object(
            storage_service_module,
            "CONTROLLER_ADDRESS",
            controller_addr,
        ))
        stack.enter_context(mock.patch.object(
            storage_service_module,
            "NODE_PORT",
            str(storage_port),
        ))

        storage_node = StorageReplicaService()
        storage_server = grpc.server(futures.ThreadPoolExecutor(max_workers=40))
        pb2_grpc.add_StorageReplicaServiceServicer_to_server(storage_node, storage_server)
        storage_server.add_insecure_port(storage_addr)
        start(storage_server)

        auction_server = grpc.server(futures.ThreadPoolExecutor(max_workers=80))
        stack.enter_context(mock.patch.object(
            auction_service_module,
            "CONTROLLER_ADDRESS",
            controller_addr,
        ))
        pb2_grpc.add_AuctionServiceServicer_to_server(
            AuctionService(),
            auction_server,
        )
        auction_port = free_port()
        auction_addr = f"127.0.0.1:{auction_port}"
        auction_server.add_insecure_port(auction_addr)
        start(auction_server)

        with grpc.insecure_channel(auction_addr) as channel:
            grpc.channel_ready_future(channel).result(timeout=5)

        yield {
            "controller_service": controller_service,
            "storage_node": storage_node,
            "auction_addr": auction_addr,
            "storage_addr": storage_addr,
            "controller_addr": controller_addr,
        }
    finally:
        for server in reversed(servers):
            server.stop(0).wait(timeout=5)
        stack.close()


class BackendTestCase(unittest.TestCase):
    maxDiff = None
