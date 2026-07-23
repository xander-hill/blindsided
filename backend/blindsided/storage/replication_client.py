import grpc
import time

from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc
from blindsided.observability.instrumentation import (
    current_replication_operation,
    record_replication_attempt,
)

REPLICATION_TIMEOUT_SECONDS = 1.0


class SynchronousReplicationClient:
    """Outbound transport for each synchronous replication protocol step."""

    def prepare(
        self, backup_address: str, request: pb2.PrepareMutationRequest
    ) -> pb2.PrepareMutationResponse | None:
        started_at = time.perf_counter()
        outcome = "failure"
        try:
            with grpc.insecure_channel(backup_address) as channel:
                stub = pb2_grpc.StorageReplicaServiceStub(channel)
                response = stub.PrepareAuctionMutation(
                    request,
                    timeout=REPLICATION_TIMEOUT_SECONDS,
                )
                outcome = (
                    "success"
                    if response.success
                    and response.prepared_version == request.candidate_auction.version
                    else "rejected"
                )
                return response
        except grpc.RpcError as error:
            try:
                status = error.code()
            except Exception:
                status = None
            if status == grpc.StatusCode.DEADLINE_EXCEEDED:
                outcome = "timeout"
            elif status == grpc.StatusCode.UNAVAILABLE:
                outcome = "unreachable"
            return None
        finally:
            operation = current_replication_operation()
            if operation is not None:
                record_replication_attempt(
                    operation,
                    outcome,
                    time.perf_counter() - started_at,
                )

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
