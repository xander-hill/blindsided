import os
import socket
import unittest
from itertools import count
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
    synchronous_backup_address: str = "",
    use_test_coordinator: bool = True,
) -> StorageReplicaService:
    judge = StorageReplicaService.__new__(StorageReplicaService)
    judge.state_lock = storage_service_module.threading.Condition()
    judge.auction_store = {}
    judge.idempotency_records = {}
    judge.prepared_mutations = {}
    judge.aborted_mutations = {}
    judge.pending_backup_commits = {}
    judge.port = address.rsplit(":", 1)[-1]
    judge.replica_role = role
    judge.current_epoch = 1 if role == "primary" else 0
    judge.promotion_ready = role == "primary"
    judge.peer_addresses = peers or []
    judge.synchronous_backup_address = synchronous_backup_address
    judge.node_address = address
    judge.state_file_path = state_file_path
    request_sequence = count(1)
    production_apply = judge.ApplyAuctionMutation

    def apply_with_request_id(request, context):
        if not request.request_id or not request.epoch:
            request_copy = pb2.AuctionMutationRequest()
            request_copy.CopyFrom(request)
            if not request_copy.request_id:
                request_copy.request_id = f"test-request-{next(request_sequence)}"
            if not request_copy.epoch:
                request_copy.epoch = judge.current_epoch
            request = request_copy
        return production_apply(request, context)

    judge.ApplyAuctionMutation = apply_with_request_id

    if use_test_coordinator:
        def successful_coordinator(
            request_id,
            candidate_auction,
            idempotency_record,
            success_response,
            previous_version,
        ):
            committed_auction = pb2.Auction()
            committed_auction.CopyFrom(candidate_auction)
            committed_record = pb2.IdempotencyRecord()
            committed_record.CopyFrom(idempotency_record)
            judge.auction_store[candidate_auction.auction_id] = committed_auction
            judge.idempotency_records[request_id] = committed_record
            judge._persist_state_to_disk()
            return success_response

        judge._coordinate_synchronous_commit = successful_coordinator
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
                # Storage advertises this address to the controller and the
                # auction service; include the ephemeral port it actually
                # binds below so in-process gRPC calls can reach it.
                "POD_IP": f"127.0.0.1:{storage_port}",
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

        def successful_stack_coordinator(
            request_id,
            candidate_auction,
            idempotency_record,
            success_response,
            previous_version,
        ):
            committed_auction = pb2.Auction()
            committed_auction.CopyFrom(candidate_auction)
            committed_record = pb2.IdempotencyRecord()
            committed_record.CopyFrom(idempotency_record)
            storage_node.auction_store[candidate_auction.auction_id] = committed_auction
            storage_node.idempotency_records[request_id] = committed_record
            storage_node._persist_state_to_disk()
            return success_response

        storage_node._coordinate_synchronous_commit = successful_stack_coordinator
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


@contextmanager
def running_replicated_backend_stack():
    """Run a real controller, primary, and backup commit path over gRPC."""
    stack = ExitStack()
    servers: list[grpc.Server] = []

    def start_servicer(servicer, add_servicer, address, workers=20):
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=workers))
        add_servicer(servicer, server)
        server.add_insecure_port(address)
        server.start()
        servers.append(server)

    try:
        controller_service = ControllerService()
        controller_addr = f"127.0.0.1:{free_port()}"
        start_servicer(
            controller_service,
            pb2_grpc.add_ClusterControllerServicer_to_server,
            controller_addr,
        )
        stack.enter_context(mock.patch.object(
            storage_service_module, "CONTROLLER_ADDRESS", controller_addr
        ))

        primary_addr = f"127.0.0.1:{free_port()}"
        with mock.patch.dict(os.environ, {
            "POD_IP": primary_addr,
            "NODE_PORT": primary_addr.rsplit(":", 1)[1],
            "NODE_ROLE": "backup",
            "PEER_ADDRESSES": "",
            "AUCTION_STORE_PATH": "",
        }, clear=False):
            primary = StorageReplicaService(initialize_connection=False)
        start_servicer(
            primary,
            pb2_grpc.add_StorageReplicaServiceServicer_to_server,
            primary_addr,
            workers=40,
        )
        primary._initialize_connection()

        backup_addr = f"127.0.0.1:{free_port()}"
        with mock.patch.dict(os.environ, {
            "POD_IP": backup_addr,
            "NODE_PORT": backup_addr.rsplit(":", 1)[1],
            "NODE_ROLE": "backup",
            "PEER_ADDRESSES": primary_addr,
            "AUCTION_STORE_PATH": "",
        }, clear=False):
            backup = StorageReplicaService(initialize_connection=False)
        start_servicer(
            backup,
            pb2_grpc.add_StorageReplicaServiceServicer_to_server,
            backup_addr,
            workers=40,
        )
        backup._initialize_connection()

        auction_addr = f"127.0.0.1:{free_port()}"
        stack.enter_context(mock.patch.object(
            auction_service_module, "CONTROLLER_ADDRESS", controller_addr
        ))
        start_servicer(
            AuctionService(),
            pb2_grpc.add_AuctionServiceServicer_to_server,
            auction_addr,
            workers=40,
        )
        yield {
            "auction_addr": auction_addr,
            "controller_service": controller_service,
            "primary": primary,
            "backup": backup,
        }
    finally:
        for server in reversed(servers):
            server.stop(0).wait(timeout=5)
        stack.close()


class BackendTestCase(unittest.TestCase):
    maxDiff = None
