import grpc

from blindsided.common.config import CONTROLLER_ADDRESS
from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc

FULL_STATE_TIMEOUT_SECONDS = 10.0
SYNCHRONIZATION_REPORT_TIMEOUT_SECONDS = 2.0


class ReplicaSynchronizationClient:
    """Outbound transport for full-state synchronization and its report."""

    def fetch_full_state(
        self,
        primary_address: str,
        requester_id: str,
        epoch: int,
    ) -> pb2.StateResponse:
        with grpc.insecure_channel(primary_address) as channel:
            stub = pb2_grpc.StorageReplicaServiceStub(channel)
            return stub.SyncFullState(
                pb2.StateRequest(requester_id=requester_id, epoch=epoch),
                timeout=FULL_STATE_TIMEOUT_SECONDS,
            )

    def report_complete(
        self,
        replica_address: str,
        primary_address: str,
        epoch: int,
        controller_address: str = CONTROLLER_ADDRESS,
    ) -> pb2.SynchronizationCompleteResponse:
        with grpc.insecure_channel(controller_address) as channel:
            stub = pb2_grpc.ClusterControllerStub(channel)
            return stub.ReportSynchronizationComplete(
                pb2.SynchronizationCompleteRequest(
                    replica_address=replica_address,
                    source_primary_address=primary_address,
                    epoch=epoch,
                ),
                timeout=SYNCHRONIZATION_REPORT_TIMEOUT_SECONDS,
            )
