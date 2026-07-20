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
        self.primary_address = None

    def RegisterNode(self, request, context):
        with self.lock:
            addr = request.address
            registered_at = time.time()
            self.nodes[addr] = ReplicaRecord(
                address=addr,
                last_seen=registered_at,
                sync_status=ReplicaSyncStatus.UNSYNCHRONIZED,
            )
            if not self.primary_address:
                self.primary_address = addr
                print(f"[Controller] Initial Judge assigned as Primary: {addr}")
            print(f"[Controller] Registered node: {addr}")
            return pb2.RegisterResponse(
                success=True, 
                is_primary=(addr == self.primary_address),
                message="Judge registered successfully"
            )

    def GetPrimary(self, request, context):
        with self.lock:
            if not self.primary_address:
                return pb2.GetPrimaryResponse(success=False, message="No Primary Judge available")
            return pb2.GetPrimaryResponse(
                success=True, 
                primary_address=self.primary_address,
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
            if source_primary_address != self.primary_address:
                return pb2.SynchronizationCompleteResponse(
                    success=False,
                    message="Synchronization source is not the current primary.",
                )
            if replica_address == self.primary_address:
                return pb2.SynchronizationCompleteResponse(
                    success=False,
                    message="The primary cannot report itself as a synchronized backup.",
                )

            replica.sync_status = ReplicaSyncStatus.SYNCHRONIZED
            return pb2.SynchronizationCompleteResponse(
                success=True,
                message="Replica synchronization recorded.",
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
                        if self.primary_address == addr:
                            self.primary_address = None
                            self._elect_new_primary()
    
    def _elect_new_primary(self):
        eligible_replicas = [
            replica for replica in self.nodes.values() if replica.promotion_eligible
        ]
        if not eligible_replicas:
            print("[Controller] CRITICAL: No synchronized replica can be promoted.")
            return

        new_primary_address = eligible_replicas[0].address
        self.primary_address = new_primary_address
        print(f"[Controller] ELECTED NEW PRIMARY: {self.primary_address}")
        threading.Thread(target=self._notify_promotion, args=(new_primary_address,)).start()

    def _notify_promotion(self, address):
        try:
            with grpc.insecure_channel(address) as channel:
                stub = pb2_grpc.StorageReplicaServiceStub(channel)
                stub.PromoteToPrimary(pb2.PromotionRequest(new_role="primary"))
                print(f"[Controller] Node {address} acknowledged promotion.")
        except Exception as e:
            print(f"[Controller] Failed to promote {address}: {e}")
