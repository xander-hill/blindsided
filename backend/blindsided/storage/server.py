from concurrent import futures

import grpc

from blindsided.common.config import NODE_PORT
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc
from blindsided.storage.service import StorageReplicaService


def serve() -> None:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    pb2_grpc.add_StorageReplicaServiceServicer_to_server(StorageReplicaService(), server)
    server.add_insecure_port(f"[::]:{NODE_PORT}")
    print(f"Judge Node (Vault) starting on port {NODE_PORT}...")
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
