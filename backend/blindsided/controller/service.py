from dataclasses import dataclass
from enum import Enum
import threading
import time

import grpc

from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc

class ReplicaSyncStatus(Enum):
    UNSYNCHRONIZED = "unsynchronized"
    SYNCHRONIZED = "synchronized"

class PrimaryStatus(Enum):
    PROMOTING = "promoting"
    READY = "ready"

@dataclass
class PrimaryAssignment:
    node_id: str
    epoch: int
    status: PrimaryStatus
    sync_backup_address: str | None = None


@dataclass
class ReplicaRecord:
    address: str
    last_seen: float
    sync_status: ReplicaSyncStatus

    @property
    def promotion_eligible(self) -> bool:
        return self.sync_status == ReplicaSyncStatus.SYNCHRONIZED


class ControllerService(pb2_grpc.ClusterControllerServicer):
    """Tracks storage replicas and promotes a new primary after failures."""

    def __init__(self):
        self.lock = threading.Lock()
        self.nodes: dict[str, ReplicaRecord] = {}
        self.primary_assignment: PrimaryAssignment | None = None
        self.last_primary_epoch = 0

    def RegisterNode(self, request, context):
        with self.lock:
            addr = request.address
            registered_at = time.time()
            self.nodes[addr] = ReplicaRecord(
                address=addr,
                last_seen=registered_at,
                sync_status=ReplicaSyncStatus.UNSYNCHRONIZED,
            )
            if self.primary_assignment is None:
                self.last_primary_epoch += 1
                self.primary_assignment = PrimaryAssignment(
                    node_id=addr,
                    epoch=self.last_primary_epoch,
                    status=PrimaryStatus.READY,
                )
                print(f"[Controller] Initial Judge assigned as Primary: {addr}")
            print(f"[Controller] Registered node: {addr}")
            return pb2.RegisterResponse(
                success=True, 
                is_primary=(addr == self.primary_assignment.node_id),
                message="Judge registered successfully",
                epoch=self.primary_assignment.epoch,
            )

    def GetPrimary(self, request, context):
        with self.lock:
            assignment = self.primary_assignment
            if assignment is None:
                return pb2.GetPrimaryResponse(success=False, message="No Primary Judge available")
            if assignment.status != PrimaryStatus.READY:
                return pb2.GetPrimaryResponse(
                    success=False,
                    message="Primary promotion is not complete.",
                )
            return pb2.GetPrimaryResponse(
                success=True, 
                primary_address=assignment.node_id,
                message="Primary retrieved"
            )

    def GetClusterInfo(self, request, context):
        with self.lock:
            return pb2.ClusterInfoResponse(
                success=True,
                node_addresses=[replica.address for replica in self.nodes.values()],
                message=f"Found {len(self.nodes)} active Judges"
            )

    def ReportSynchronizationComplete(self, request, context):
        with self.lock:
            replica_address = request.replica_address.strip()
            source_primary_address = request.source_primary_address.strip()
            if not replica_address or not source_primary_address:
                return pb2.SynchronizationCompleteResponse(
                    success=False,
                    message="Replica and source primary addresses are required.",
                )
            replica = self.nodes.get(replica_address)
            if replica is None:
                return pb2.SynchronizationCompleteResponse(
                    success=False,
                    message="Replica is not registered.",
                )
            assignment = self.primary_assignment
            if (
                assignment is None
                or source_primary_address != assignment.node_id
                or request.epoch != assignment.epoch
            ):
                return pb2.SynchronizationCompleteResponse(
                    success=False,
                    message="Synchronization source or epoch does not match the current primary.",
                )
            if replica_address == assignment.node_id:
                return pb2.SynchronizationCompleteResponse(
                    success=False,
                    message="The primary cannot report itself as a synchronized backup.",
                )
            if (
                assignment.status == PrimaryStatus.PROMOTING
                and replica_address != assignment.sync_backup_address
            ):
                return pb2.SynchronizationCompleteResponse(
                    success=False,
                    message="Replica is not the designated promotion backup.",
                )

            replica.sync_status = ReplicaSyncStatus.SYNCHRONIZED
            if assignment.status == PrimaryStatus.READY:
                return pb2.SynchronizationCompleteResponse(
                    success=True,
                    message="Replica synchronization recorded.",
                )
            candidate_address = assignment.node_id
            epoch = assignment.epoch
            backup_address = assignment.sync_backup_address

        try:
            with grpc.insecure_channel(candidate_address) as channel:
                stub = pb2_grpc.StorageReplicaServiceStub(channel)
                completion = stub.CompletePrimaryPromotion(
                    pb2.CompletePrimaryPromotionRequest(
                        epoch=epoch,
                        backup_address=backup_address,
                    )
                )
        except grpc.RpcError:
            return pb2.SynchronizationCompleteResponse(
                success=False,
                message="Primary promotion completion RPC failed.",
            )
        if not completion.success or completion.epoch != epoch:
            return pb2.SynchronizationCompleteResponse(
                success=False,
                message="Primary promotion completion was rejected.",
            )

        with self.lock:
            assignment = self.primary_assignment
            if (
                assignment is None
                or assignment.node_id != candidate_address
                or assignment.epoch != epoch
                or assignment.sync_backup_address != backup_address
                or assignment.status != PrimaryStatus.PROMOTING
            ):
                return pb2.SynchronizationCompleteResponse(
                    success=False,
                    message="Primary assignment changed during completion.",
                )
            assignment.status = PrimaryStatus.READY
        return pb2.SynchronizationCompleteResponse(
            success=True,
            message="Replica synchronized and primary promotion completed.",
        )

    def _monitor_heartbeats(self):
        while True:
            time.sleep(5)
            with self.lock:
                for addr, replica in list(self.nodes.items()):
                    try:
                        with grpc.insecure_channel(addr) as channel:
                            stub = pb2_grpc.StorageReplicaServiceStub(channel)
                            response = stub.Heartbeat(pb2.HealthCheckRequest(request_source="CONTROLLER"), timeout=2.0)
                            if not response.alive:
                                raise Exception("Node reported unhealthy")
                            replica.last_seen = time.time()
                    except Exception:
                        print(f"[Controller] Judge {addr} failed heartbeat! Evicting...")
                        del self.nodes[addr]
                        if (
                            self.primary_assignment is not None
                            and self.primary_assignment.node_id == addr
                        ):
                            self.primary_assignment = None
                            self._elect_new_primary()
    
    def _elect_new_primary(self):
        eligible_replicas = [
            replica for replica in self.nodes.values() if replica.promotion_eligible
        ]
        if not eligible_replicas:
            print("[Controller] CRITICAL: No synchronized replica can be promoted.")
            return

        new_primary_address = eligible_replicas[0].address
        self.last_primary_epoch += 1
        assignment = PrimaryAssignment(
            node_id=new_primary_address,
            epoch=self.last_primary_epoch,
            status=PrimaryStatus.PROMOTING,
        )
        self.primary_assignment = assignment
        print(
            f"[Controller] ELECTED NEW PRIMARY: {assignment.node_id} "
            f"(epoch {assignment.epoch}, promoting)"
        )
        threading.Thread(
            target=self._notify_promotion,
            args=(assignment.node_id, assignment.epoch),
        ).start()

    def _notify_promotion(self, address, epoch):
        try:
            with self.lock:
                assignment = self.primary_assignment
                replica = self.nodes.get(address)
                if (
                    assignment is None
                    or assignment.node_id != address
                    or assignment.epoch != epoch
                    or assignment.status != PrimaryStatus.PROMOTING
                    or replica is None
                    or not replica.promotion_eligible
                ):
                    return
            with grpc.insecure_channel(address) as channel:
                stub = pb2_grpc.StorageReplicaServiceStub(channel)
                response = stub.BeginPrimaryPromotion(
                    pb2.BeginPrimaryPromotionRequest(epoch=epoch)
                )
                if not response.accepted or response.epoch != epoch:
                    return
                confirmation = stub.ConfirmPromotionState(
                    pb2.PromotionStateConfirmationRequest(epoch=epoch)
                )
            if not confirmation.confirmed or confirmation.epoch != epoch:
                return
            with self.lock:
                assignment = self.primary_assignment
                if (
                    assignment is None
                    or assignment.node_id != address
                    or assignment.epoch != epoch
                    or assignment.status != PrimaryStatus.PROMOTING
                ):
                    return
                backup = next(
                    (
                        replica
                        for replica in self.nodes.values()
                        if replica.address != address
                    ),
                    None,
                )
                if backup is None:
                    return
                backup.sync_status = ReplicaSyncStatus.UNSYNCHRONIZED
                assignment.sync_backup_address = backup.address
                backup_address = backup.address
            with grpc.insecure_channel(backup_address) as channel:
                backup_stub = pb2_grpc.StorageReplicaServiceStub(channel)
                synchronization = backup_stub.SynchronizeFromPrimary(
                    pb2.SynchronizeFromPrimaryRequest(
                        primary_address=address,
                        epoch=epoch,
                    )
                )
            if not synchronization.success or synchronization.epoch != epoch:
                return
            print(
                f"[Controller] Backup {backup_address} synchronized from "
                f"{address} for promotion epoch {epoch}."
            )
        except Exception as e:
            print(f"[Controller] Failed to promote {address}: {e}")
