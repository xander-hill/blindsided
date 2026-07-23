from concurrent import futures
import threading
import time

import grpc

from blindsided.common.config import NODE_PORT
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc
from blindsided.storage.service import StorageReplicaService
from blindsided.observability.server import start_metrics_server
from blindsided.observability.logging import configure_logging


def serve() -> None:
    configure_logging()
    storage_service = StorageReplicaService(initialize_connection=False)
    start_metrics_server(8000)
    server = _start_grpc_server(storage_service)
    storage_service._initialize_connection()
    threading.Thread(
        target=_periodically_reregister,
        args=(storage_service,),
        daemon=True,
    ).start()
    server.wait_for_termination()


def _start_grpc_server(
    storage_service: StorageReplicaService,
    port: str | None = None,
) -> grpc.Server:
    """Start the node endpoint while the replica is still bootstrapping."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    pb2_grpc.add_StorageReplicaServiceServicer_to_server(storage_service, server)
    listen_port = port or NODE_PORT
    server.add_insecure_port(f"[::]:{listen_port}")
    print(f"Judge Node (Vault) starting on port {listen_port}...")
    server.start()
    return server


def _periodically_reregister(storage_service: StorageReplicaService) -> None:
    while True:
        time.sleep(5)
        storage_service.reregister_with_controller()


if __name__ == "__main__":
    serve()
