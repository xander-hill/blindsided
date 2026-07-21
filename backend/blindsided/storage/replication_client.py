import grpc

from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc

REPLICATION_TIMEOUT_SECONDS = 1.0


class SynchronousReplicationClient:
    """Outbound transport for each synchronous replication protocol step."""

    def prepare(
        self, backup_address: str, request: pb2.PrepareMutationRequest
    ) -> pb2.PrepareMutationResponse | None:
        try:
            with grpc.insecure_channel(backup_address) as channel:
                stub = pb2_grpc.StorageReplicaServiceStub(channel)
                return stub.PrepareAuctionMutation(
                    request,
                    timeout=REPLICATION_TIMEOUT_SECONDS,
                )
        except grpc.RpcError:
            return None

    def commit(
        self, backup_address: str, request: pb2.MutationDecisionRequest
    ) -> pb2.MutationDecisionResponse | None:
        try:
            with grpc.insecure_channel(backup_address) as channel:
                stub = pb2_grpc.StorageReplicaServiceStub(channel)
                return stub.CommitPreparedMutation(
                    request,
                    timeout=REPLICATION_TIMEOUT_SECONDS,
                )
        except grpc.RpcError:
            return None

    def abort(
        self, backup_address: str, request: pb2.MutationDecisionRequest
    ) -> pb2.MutationDecisionResponse | None:
        try:
            with grpc.insecure_channel(backup_address) as channel:
                stub = pb2_grpc.StorageReplicaServiceStub(channel)
                return stub.AbortPreparedMutation(
                    request,
                    timeout=REPLICATION_TIMEOUT_SECONDS,
                )
        except grpc.RpcError:
            return None
