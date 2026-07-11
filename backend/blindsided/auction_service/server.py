import os
from concurrent import futures

import grpc

from blindsided.auction_service.service import BlindSidedService
from blindsided.common.config import NODE_PORT
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc


SERVICE_PORT = os.getenv("SERVICE_PORT", NODE_PORT)


def serve() -> None:
    service_instance = BlindSidedService()

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=100))
    pb2_grpc.add_BlindSidedServicer_to_server(service_instance, server)
    server.add_insecure_port(f"[::]:{SERVICE_PORT}")

    print(f"BlindSided API Gateway is now ONLINE on port {SERVICE_PORT}")
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
