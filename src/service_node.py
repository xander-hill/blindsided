import os
import random
from concurrent import futures
import time

import grpc

from proto.src import marketplace_pb2 as pb2
from proto.src import marketplace_pb2_grpc as pb2_grpc
from src.utils.config import NODE_PORT

controller_host = os.getenv("CONTROLLER_HOST", "localhost")
CONTROLLER_ADDRESS = f"{controller_host}:50050"
SERVICE_PORT = os.getenv("SERVICE_PORT", NODE_PORT)


class ServiceNode(pb2_grpc.MarketplaceServicer):
    """
    Stateless service pod — sits behind the Kubernetes load balancer and
    brokers requests between clients and the storage layer.

    Write path:  ask the controller for the current primary, forward there.
    Read path:   ask the controller for any live replica, read from it.
                 Falls back to the primary if no backup is available.

    The node holds no persistent state of its own, so any number of replicas
    can run behind the same Kubernetes Service without coordination.
    """


    def _get_primary_address(self) -> str | None:
        """Ask the controller which storage node is the current primary."""
        try:
            with grpc.insecure_channel(CONTROLLER_ADDRESS) as ch:
                stub = pb2_grpc.ControllerStub(ch)
                resp = stub.GetPrimary(pb2.GetPrimaryRequest(), timeout=3.0)
                if resp.success:
                    return resp.primary_address
        except Exception as e:
            print(f"[ServiceNode] Could not reach controller: {e}")
        return None

    def _get_all_storage_addresses(self) -> list[str]:
        """
        Ask the controller for the full cluster view so we can route reads
        to any healthy replica.
        """
        try:
            with grpc.insecure_channel(CONTROLLER_ADDRESS) as ch:
                stub = pb2_grpc.ControllerStub(ch)
                resp = stub.GetClusterInfo(pb2.ClusterInfoRequest(), timeout=3.0)
                if resp.success:
                    return list(resp.node_addresses)
        except Exception as e:
            print(f"[ServiceNode] Could not fetch cluster info: {e}")
        return []

    def _storage_stub(self, address: str):
        """Open an insecure channel to a storage node and return its stub."""
        channel = grpc.insecure_channel(address)
        return pb2_grpc.StorageReplicaStub(channel), channel


    def CreateItem(self, request: pb2.CreateItemRequest, context) -> pb2.CreateItemResponse:
        """
        Public API: Entry point for a client to add a new item.
        Logic: Get Primary -> Call Storage.PutItem(is_update=False)
        """
        primary = self._get_primary_address()
        if not primary:
            return pb2.CreateItemResponse(ok=False, message="No primary storage available")

        try:
            stub, channel = self._storage_stub(primary)
            with channel:
                # Map the Create request to the Storage Put request
                storage_res = stub.PutItem(pb2.PutRequest(
                    item=request.item,
                    is_update=False,
                    skip_consistency_check=False
                ), timeout=5.0)

                if storage_res.success:
                    return pb2.CreateItemResponse(ok=True, item_id=request.item.item_id, message="Created successfully")
                else:
                    return pb2.CreateItemResponse(ok=False, message=storage_res.message)
                
        except grpc.RpcError as e:
            print(f"[ServiceNode] CreateItem failed on primary {primary}: {e}")
            return pb2.CreateItemResponse(ok=False, message=f"Storage error: {e.details()}")
        
    def UpdateItem(self, request: pb2.UpdateItemRequest, context) -> pb2.UpdateItemResponse:
        """
        Public API: Entry point for a client to update an existing item.
        Logic: Get Primary -> Call Storage.PutItem(is_update=True)
        """
        primary = self._get_primary_address()
        if not primary:
            return pb2.UpdateItemResponse(ok=False, message="No primary storage available")

        # Create a partial Item object for the update
        updated_item = pb2.Item(
            item_id=request.item_id,
            seller_id=request.seller_id,
            description=request.description,
            is_closed=request.status == "CLOSED",
            version=request.expected_version # Send the version we THINK it is
        )

        try:
            stub, channel = self._storage_stub(primary)
            with channel:
                # Map the Update request to the Storage Put request
                storage_res = stub.PutItem(pb2.PutRequest(
                    item=updated_item,
                    is_update=True,
                    skip_consistency_check=False
                ), timeout=5.0)

                if storage_res.success:
                    return pb2.UpdateItemResponse(ok=True, new_version=storage_res.current_version, message="Updated successfully")
                else:
                    return pb2.UpdateItemResponse(ok=False, message=storage_res.message)

        except grpc.RpcError as e:
            print(f"[ServiceNode] UpdateItem failed on primary {primary}: {e}")
            return pb2.UpdateItemResponse(ok=False, message=f"Storage error: {e.details()}")

    def SearchItems(self, request: pb2.SearchRequest, context) -> pb2.SearchResponse:
        """
        Public API: Maps SearchRequest to internal Storage Query logic.
        Intercepts the storage response to apply the Fog of War.
        """
        candidates = self._get_all_storage_addresses()

        if candidates:
            random.shuffle(candidates)
        else:
            primary = self._get_primary_address()
            if not primary:
                return pb2.SearchResponse(ok=False, message="No storage nodes available")
            candidates = [primary]

        # Convert the Marketplace SearchRequest to a Storage QueryRequest
        storage_req = pb2.QueryRequest(filter=request.query)

        for addr in candidates:
            try:
                stub, channel = self._storage_stub(addr)
                with channel:
                    # We still call QueryItems on the STORAGE nodes
                    response = stub.QueryItems(storage_req, timeout=5.0)
                    
                    # Apply the Fog of War masking
                    masked_items = [self._mask_item_for_client(it) for it in response.items]

                    return pb2.SearchResponse(
                        ok=response.ok,
                        items=masked_items,
                        message="Results filtered by Fog of War"
                    )
            except grpc.RpcError as e:
                print(f"[ServiceNode] Search failed on {addr}: {e}")
                continue 

        return pb2.SearchResponse(ok=False, message="All storage replicas unreachable")
    
    def _mask_item_for_client(self, item: pb2.Item) -> pb2.Item:
        """If the auction is still open, hide the sensitive bid data."""
        if not item.is_closed:
            # We create a copy to avoid modifying the version held in memory/cache
            masked_item = pb2.Item()
            masked_item.CopyFrom(item)
            masked_item.highest_bid = 0.0 
            masked_item.highest_bidder_id = "HIDDEN"
            return masked_item
        return item
    
    def PlaceBid(self, request: pb2.BidRequest, context) -> pb2.BidResponse:
        """
        Public API: Entry point for a client to place a hidden bid.
        Logic: Get Primary -> Convert BidRequest to PutRequest -> Return masked result.
        """
        primary = self._get_primary_address()
        if not primary:
            return pb2.BidResponse(success=False, message="No primary storage available")

        # Wrap the bid into an Item object for the storage layer
        # Note: We use highest_bid to store the bid amount
        bid_item = pb2.Item(
            item_id=request.item_id,
            highest_bidder_id=request.buyer_id,
            highest_bid=request.amount,
            version=request.expected_version
        )

        try:
            stub, channel = self._storage_stub(primary)
            with channel:
                # We tell storage this IS an update and to check consistency (price/version)
                storage_res = stub.PutItem(pb2.PutRequest(
                    item=bid_item,
                    is_update=True,
                    skip_consistency_check=False
                ), timeout=5.0)

                if storage_res.success:
                    return pb2.BidResponse(
                        success=True, 
                        current_price=0.0, # MASKED: Don't reveal the bid in the response!
                        new_version=storage_res.current_version,
                        message="Accepted into the fog"
                    )
                else:
                    return pb2.BidResponse(
                        success=False, 
                        message=storage_res.message # Will say "Blindsided!" or "Stale version"
                    )

        except grpc.RpcError as e:
            print(f"[ServiceNode] PlaceBid failed on primary {primary}: {e}")
            return pb2.BidResponse(success=False, message=f"Storage error: {e.details()}")



def serve() -> None:
    node = ServiceNode()
    print("Waiting for Controller to be reachable...")
    while not node._get_primary_address():
        print("Controller not ready. Retrying in 2s...")
        time.sleep(2)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    pb2_grpc.add_MarketplaceServicer_to_server(ServiceNode(), server)
    server.add_insecure_port(f"[::]:{SERVICE_PORT}")
    print(f"Service node gRPC server starting on port {SERVICE_PORT}...")
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    serve()