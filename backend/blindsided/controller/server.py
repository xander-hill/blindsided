import threading
from concurrent import futures

import grpc

from blindsided.common.config import CONTROLLER_PORT
from blindsided.controller.service import ControllerService
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc
from blindsided.observability.server import start_metrics_server
from blindsided.observability.logging import configure_logging


def serve():
    configure_logging()
    controller_service = ControllerService()
    start_metrics_server(8000)

    monitor_thread = threading.Thread(
        target=controller_service._monitor_heartbeats,
        daemon=True,
    )
    monitor_thread.start()

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=10)
    )

    pb2_grpc.add_ClusterControllerServicer_to_server(
        controller_service,
        server,
    )

    server.add_insecure_port(f"[::]:{CONTROLLER_PORT}")

    print(
        f"Controller (Cluster Brain) starting "
        f"on port {CONTROLLER_PORT}..."
    )

    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
