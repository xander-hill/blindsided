import grpc
from concurrent import futures
import threading
import time

from proto import blindsided_pb2 as pb2
from proto import blindsided_pb2_grpc as pb2_grpc
from src.utils.config import CONTROLLER_PORT

class Controller(pb2_grpc.ControllerServicer):
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
            # FIX: Added 'message' to match the updated Proto
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
            # FIX: Added 'message' to match the updated Proto
            return pb2.ClusterInfoResponse(
                success=True,
                node_addresses=list(self.nodes.keys()),
                message=f"Found {len(self.nodes)} active Judges"
            )

    def HeartbeatMonitor(self):
        while True:
            time.sleep(5)
            with self.lock:
                for addr in list(self.nodes.keys()):
                    try:
                        with grpc.insecure_channel(addr) as channel:
                            stub = pb2_grpc.JudgeNodeStub(channel)
                            # The JudgeNode's Heartbeat response now has a 'message' field too
                            response = stub.Heartbeat(pb2.HealthCheckRequest(request_source="CONTROLLER"), timeout=2.0)
                            if not response.alive:
                                raise Exception("Node reported unhealthy")
                    except Exception:
                        print(f"[Controller] Judge {addr} failed heartbeat! Evicting...")
                        del self.nodes[addr]
                        if self.primary_address == addr:
                            self.primary_address = None
                            self.ElectNewPrimary()
    
    def ElectNewPrimary(self):
        if not self.nodes:
            print("[Controller] CRITICAL: The Vault is empty. All Judges are dead.")
            return

        new_primary = list(self.nodes.keys())[0]
        self.primary_address = new_primary
        print(f"[Controller] ELECTED NEW PRIMARY: {self.primary_address}")
        threading.Thread(target=self.NotifyPromotion, args=(new_primary,)).start()

    def NotifyPromotion(self, address):
        try:
            with grpc.insecure_channel(address) as channel:
                stub = pb2_grpc.JudgeNodeStub(channel)
                # JudgeNode.PromoteToPrimary returns PromotionResponse(success, message)
                stub.PromoteToPrimary(pb2.PromotionRequest(new_role="primary"))
                print(f"[Controller] Node {address} acknowledged promotion.")
        except Exception as e:
            print(f"[Controller] Failed to promote {address}: {e}")

def serve():
    controller_instance = Controller()
    monitor_thread = threading.Thread(target=controller_instance.HeartbeatMonitor, daemon=True)
    monitor_thread.start()

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    pb2_grpc.add_ControllerServicer_to_server(controller_instance, server)
    server.add_insecure_port(f"[::]:{CONTROLLER_PORT}")
    print(f"Controller (Cluster Brain) starting on port {CONTROLLER_PORT}...")
    server.start()
    server.wait_for_termination()

if __name__ == "__main__":
    serve()