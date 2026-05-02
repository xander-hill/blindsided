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

    def _get_primary_address(self, force_refresh=False) -> str | None:
        """Consult the Controller. If force_refresh is True, we don't use cache."""
        # You could implement a local self._cached_primary here 
        # for performance, but for now, let's just ensure it's reliable.
        try:
            with grpc.insecure_channel(CONTROLLER_ADDRESS) as ch:
                stub = pb2_grpc.ControllerStub(ch)
                resp = stub.GetPrimary(pb2.GetPrimaryRequest(), timeout=2.0)
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
        """
        Submits a bid into the 'Fog'. 
        Includes a retry loop to handle Judge failover/election periods.
        """
        max_retries = 3
        last_error = "Unknown error"

        for attempt in range(max_retries):
            primary = self._get_primary_address()
            if not primary:
                last_error = "No Primary Judge available from Controller"
                time.sleep(1.5) # Wait for election
                continue

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
                    ), timeout=3.0)

                    if res.success:
                        return pb2.BidResponse(
                            success=True, 
                            revealed_price=0.0,
                            message="Accepted into the fog."
                        )
                    
                    # If the Judge specifically rejected the bid (e.g. too low), 
                    # don't retry, just return the failure message.
                    return pb2.BidResponse(success=False, message=res.message)

            except grpc.RpcError as e:
                last_error = f"Judge connection failed: {e.details()}"
                print(f"[ServiceNode] Attempt {attempt+1} failed talking to {primary}. Retrying...")
                time.sleep(1.0) # Backoff before asking the Controller again

        return pb2.BidResponse(success=False, message=f"Vault unreachable: {last_error}")

    def DropTheGavel(self, request: pb2.GavelRequest, context):
        primary = self._get_primary_address()
        
        # NEW: Fetch current version to ensure the Judge accepts the 'Reveal'
        stub, channel = self._judge_stub(primary)
        with channel:
            status_res = stub.QueryVault(pb2.QueryRequest(filter=request.auction_id))
            current_v = status_res.auctions[0].version if status_res.auctions else 0
            
            reveal_state = pb2.Auction(
                auction_id=request.auction_id,
                is_revealed=True,
                version=current_v # Use the real version!
            )
            
            return stub.CommitToVault(pb2.CommitRequest(
                auction=reveal_state,
                is_reveal_event=True
            ))

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
    
    def JoinLiveAuction(self, request: pb2.AuctionRequest, context):
        """
        The 'Live Watcher': Keeps a connection open. 
        When the Gavel falls in the Vault, the client gets the reveal instantly.
        """
        auction_id = request.auction_id
        last_version = -1
        
        print(f"[Fog] Watcher joined for {auction_id}")

        while context.is_active():
            primary = self._get_primary_address()
            if not primary:
                time.sleep(1)
                continue

            try:
                stub, channel = self._judge_stub(primary)
                with channel:
                    # Look up the auction
                    res = stub.QueryVault(pb2.QueryRequest(filter=auction_id))
                    
                    if res.auctions:
                        auction = res.auctions[0]
                        
                        # Only send data if something actually changed
                        if auction.version > last_version:
                            last_version = auction.version
                            
                            # Apply the Fog of War masking
                            update = pb2.AuctionUpdate(
                                is_revealed=auction.is_revealed,
                                message="Vault update detected."
                            )
                            
                            if auction.is_revealed:
                                update.revealed_price = auction.sealed_bid
                                update.lead_bidder_id = auction.lead_bidder_id
                                update.message = "🔨 GAVEL FELL: The truth is revealed!"
                                yield update
                                return # Close the stream after the reveal
                            else:
                                update.revealed_price = 0.0
                                update.lead_bidder_id = "REDACTED"
                                yield update
                
                # Poll the judge every second to check for changes
                time.sleep(1)
                
            except grpc.RpcError:
                # If the Primary Judge dies, don't kill the stream! 
                # Just wait for the Controller to elect a new one.
                time.sleep(2)


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