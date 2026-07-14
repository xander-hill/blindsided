import threading
import time

import grpc

from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc

class ControllerService(pb2_grpc.ClusterControllerServicer):
    """Tracks storage replicas and promotes a new primary after failures."""

    def __init__(self):
        self.lock = threading.Lock()
        self.nodes = {} 
        self.primary_address = None

    def RegisterNode(self, request, context):
        with self.lock:
            addr = request.address
            self.nodes[addr] = time.time()
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
                node_addresses=list(self.nodes.keys()),
                message=f"Found {len(self.nodes)} active Judges"
            )

    def _monitor_heartbeats(self):
        while True:
            time.sleep(5)
            with self.lock:
                for addr in list(self.nodes.keys()):
                    try:
                        with grpc.insecure_channel(addr) as channel:
                            stub = pb2_grpc.StorageReplicaServiceStub(channel)
                            response = stub.Heartbeat(pb2.HealthCheckRequest(request_source="CONTROLLER"), timeout=2.0)
                            if not response.alive:
                                raise Exception("Node reported unhealthy")
                    except Exception:
                        print(f"[Controller] Judge {addr} failed heartbeat! Evicting...")
                        del self.nodes[addr]
                        if self.primary_address == addr:
                            self.primary_address = None
                            self._elect_new_primary()
    
    def _elect_new_primary(self):
        if not self.nodes:
            print("[Controller] CRITICAL: The Vault is empty. All Judges are dead.")
            return

        new_primary_address = list(self.nodes.keys())[0]
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
