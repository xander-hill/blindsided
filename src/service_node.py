import os
import random
from concurrent import futures
import time

import grpc

# Updated imports for the renamed proto
from proto.src import blindsided_pb2 as pb2
from proto.src import blindsided_pb2_grpc as pb2_grpc
from src.utils.config import NODE_PORT

controller_host = os.getenv("CONTROLLER_HOST", "localhost")
CONTROLLER_ADDRESS = f"{controller_host}:50050"
SERVICE_PORT = os.getenv("SERVICE_PORT", NODE_PORT)


class BlindSidedService(pb2_grpc.BlindSidedServicer):
    """
    The API Gateway for the BlindSided system.
    Enforces the 'Fog of War' by masking sensitive data from the Judge 
    before it reaches the client.
    """

    def _get_primary_address(self) -> str | None:
        """Consult the Controller to find the current Judge (Primary)."""
        try:
            with grpc.insecure_channel(CONTROLLER_ADDRESS) as ch:
                stub = pb2_grpc.ControllerStub(ch)
                resp = stub.GetPrimary(pb2.GetPrimaryRequest(), timeout=3.0)
                if resp.success:
                    return resp.primary_address
        except Exception as e:
            print(f"[BlindSided] Controller unreachable: {e}")
        return None

    def _get_all_judge_addresses(self) -> list[str]:
        """Fetch all healthy Judge nodes for distributed reads."""
        try:
            with grpc.insecure_channel(CONTROLLER_ADDRESS) as ch:
                stub = pb2_grpc.ControllerStub(ch)
                resp = stub.GetClusterInfo(pb2.ClusterInfoRequest(), timeout=3.0)
                if resp.success:
                    return list(resp.node_addresses)
        except Exception as e:
            print(f"[BlindSided] Could not fetch cluster info: {e}")
        return []

    def _judge_stub(self, address: str):
        """Helper to create a stub for the Storage/Judge layer."""
        channel = grpc.insecure_channel(address)
        return pb2_grpc.JudgeNodeStub(channel), channel

    # --- PUBLIC API METHODS ---

    def OpenAuction(self, request: pb2.OpenRequest, context) -> pb2.OpenResponse:
        """Initializes a new auction in the vault."""
        primary = self._get_primary_address()
        if not primary:
            return pb2.OpenResponse(ok=False, message="The Vault is unreachable")

        try:
            stub, channel = self._judge_stub(primary)
            with channel:
                # Map Open to CommitToVault (New Entry)
                res = stub.CommitToVault(pb2.CommitRequest(
                    auction=request.auction,
                    is_reveal_event=False,
                    skip_consistency_check=False
                ), timeout=5.0)

                if res.success:
                    return pb2.OpenResponse(ok=True, auction_id=request.auction.auction_id, message="Auction opened in the Vault.")
                return pb2.OpenResponse(ok=False, message=res.message)
                
        except grpc.RpcError as e:
            return pb2.OpenResponse(ok=False, message=f"Judge error: {e.details()}")

    def PlaceSecretBid(self, request: pb2.BidRequest, context) -> pb2.BidResponse:
        """Submits a bid into the 'Fog'. Only the Judge knows if it won."""
        primary = self._get_primary_address()
        if not primary:
            return pb2.BidResponse(success=False, message="The Judge is out of session")

        # Package the bid for the Judge
        bid_state = pb2.Auction(
            auction_id=request.auction_id,
            lead_bidder_id=request.buyer_id,
            sealed_bid=request.amount,
            version=request.expected_version
        )

        try:
            stub, channel = self._judge_stub(primary)
            with channel:
                res = stub.CommitToVault(pb2.CommitRequest(
                    auction=bid_state,
                    is_reveal_event=False,
                    skip_consistency_check=False
                ), timeout=5.0)

                if res.success:
                    return pb2.BidResponse(
                        success=True, 
                        revealed_price=0.0, # The Fog remains
                        message="Accepted into the fog."
                    )
                return pb2.BidResponse(success=False, message=res.message)

        except grpc.RpcError as e:
            return pb2.BidResponse(success=False, message=f"Judge error: {e.details()}")

    def DropTheGavel(self, request: pb2.GavelRequest, context) -> pb2.GavelResponse:
        """The Reveal: Lifts the Fog of War for a specific auction."""
        primary = self._get_primary_address()
        if not primary:
            return pb2.GavelResponse(ok=False, message="The Judge is unreachable")

        reveal_state = pb2.Auction(
            auction_id=request.auction_id,
            seller_id=request.seller_id,
            is_revealed=True,
            version=request.expected_version
        )

        try:
            stub, channel = self._judge_stub(primary)
            with channel:
                res = stub.CommitToVault(pb2.CommitRequest(
                    auction=reveal_state,
                    is_reveal_event=True,
                    skip_consistency_check=False
                ), timeout=5.0)

                if res.success:
                    return pb2.GavelResponse(ok=True, final_version=res.current_version, message="The Gavel has fallen. Truth revealed.")
                return pb2.GavelResponse(ok=False, message=res.message)

        except grpc.RpcError as e:
            return pb2.GavelResponse(ok=False, message=f"Judge error: {e.details()}")

    def SearchAuctions(self, request: pb2.SearchRequest, context) -> pb2.SearchResponse:
        """Queries the vault and applies the Fog masking logic to results."""
        candidates = self._get_all_judge_addresses()
        if not candidates:
            primary = self._get_primary_address()
            if not primary: return pb2.SearchResponse(ok=False, message="No Judges active")
            candidates = [primary]
        else:
            random.shuffle(candidates)

        query = pb2.QueryRequest(filter=request.query)

        for addr in candidates:
            try:
                stub, channel = self._judge_stub(addr)
                with channel:
                    res = stub.QueryVault(query, timeout=5.0)
                    masked = [self._mask_for_fog(a) for a in res.auctions]
                    return pb2.SearchResponse(ok=True, auctions=masked, message="Results from the Vault")
            except grpc.RpcError:
                continue 

        return pb2.SearchResponse(ok=False, message="Vault unreachable")

    def GetStatus(self, request: pb2.StatusRequest, context) -> pb2.StatusResponse:
        """Fetch a single auction, masked by the Fog if not revealed."""
        primary = self._get_primary_address() # Consistent read from primary
        if not primary: return pb2.StatusResponse(ok=False, message="Judge unreachable")
        
        try:
            stub, channel = self._judge_stub(primary)
            with channel:
                res = stub.QueryVault(pb2.QueryRequest(filter=request.auction_id))
                if res.auctions:
                    masked = self._mask_for_fog(res.auctions[0])
                    return pb2.StatusResponse(ok=True, auction=masked)
                return pb2.StatusResponse(ok=False, message="Auction not found")
        except Exception as e:
            return pb2.StatusResponse(ok=False, message=str(e))

    def _mask_for_fog(self, auction: pb2.Auction) -> pb2.Auction:
        """The Secrecy Engine."""
        if not auction.is_revealed:
            masked = pb2.Auction()
            masked.CopyFrom(auction)
            masked.sealed_bid = 0.0 
            masked.lead_bidder_id = "REDACTED"
            return masked
        return auction


def serve() -> None:
    # 1. Instantiate once
    service_instance = BlindSidedService()
    
    # 2. (Optional but recommended) Wait for cluster readiness
    print("Checking Controller connectivity...")
    while not service_instance._get_primary_address():
        print("Waiting for Primary Judge to be elected... retrying in 2s")
        time.sleep(2)

    # 3. Start Server
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    pb2_grpc.add_BlindSidedServicer_to_server(service_instance, server)
    server.add_insecure_port(f"[::]:{SERVICE_PORT}")
    
    print(f"BlindSided API Gateway active on port {SERVICE_PORT}")
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    serve()