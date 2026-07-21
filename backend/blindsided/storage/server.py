from concurrent import futures
import threading
import time

import grpc

from blindsided.common.config import NODE_PORT
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc
from blindsided.storage.service import StorageReplicaService


def serve() -> None:
    storage_service = StorageReplicaService()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    pb2_grpc.add_StorageReplicaServiceServicer_to_server(storage_service, server)
    server.add_insecure_port(f"[::]:{NODE_PORT}")
    print(f"Judge Node (Vault) starting on port {NODE_PORT}...")
    server.start()
    threading.Thread(
        target=_periodically_reregister,
        args=(storage_service,),
        daemon=True,
    ).start()
    server.wait_for_termination()


def _periodically_reregister(storage_service: StorageReplicaService) -> None:
    while True:
        time.sleep(5)
        storage_service.reregister_with_controller()


if __name__ == "__main__":
    serve()
