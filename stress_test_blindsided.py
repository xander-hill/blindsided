import grpc
import threading
import time
import random
from proto.src import blindsided_pb2 as pb2
from proto.src import blindsided_pb2_grpc as pb2_grpc

SERVICE_ADDR = 'localhost:50051'
AUCTION_ID = f"chaos_auc_{int(time.time())}"

def start_watcher():
    """Simulates a frontend user joined to the live stream."""
    print(f"👀 [Watcher] Joining live stream for {AUCTION_ID}...")
    with grpc.insecure_channel(SERVICE_ADDR) as channel:
        stub = pb2_grpc.BlindSidedStub(channel)
        try:
            for update in stub.JoinLiveAuction(pb2.AuctionRequest(auction_id=AUCTION_ID, user_id="observer_1")):
                print(f"📢 [STREAM UPDATE]: {update.message} | Revealed: {update.is_revealed} | Price: ${update.revealed_price}")
        except grpc.RpcError as e:
            print(f"❌ [Watcher] Stream interrupted: {e.details()}")

def place_bids(n=10):
    """Simulates multiple buyers hammering the vault."""
    with grpc.insecure_channel(SERVICE_ADDR) as channel:
        stub = pb2_grpc.BlindSidedStub(channel)
        current_ver = 1
        for i in range(n):
            bid_amount = 100 + (i * 50)
            print(f"💰 [Buyer] Attempting bid: ${bid_amount} (v{current_ver})")
            try:
                res = stub.PlaceSecretBid(pb2.BidRequest(
                    auction_id=AUCTION_ID,
                    buyer_id=f"buyer_{i}",
                    amount=float(bid_amount),
                    expected_version=current_ver
                ))
                if res.success:
                    print(f"✅ [Buyer] Bid Accepted.")
                    current_ver += 1
                else:
                    print(f"⚠️ [Buyer] Rejected: {res.message}")
                    # In a real stress test, we'd fetch status to update version
            except Exception as e:
                print(f"🔥 [Buyer] RPC Failed: {e}")
            time.sleep(0.5)

def run_stress_test():
    # 1. Open the Auction
    with grpc.insecure_channel(SERVICE_ADDR) as channel:
        stub = pb2_grpc.BlindSidedStub(channel)
        stub.OpenAuction(pb2.OpenRequest(auction=pb2.Auction(
            auction_id=AUCTION_ID, title="Chaos Test Item", reserve_price=50.0
        )))

    # 2. Start Live Watcher in background
    watcher_thread = threading.Thread(target=start_watcher, daemon=True)
    watcher_thread.start()
    
    # 3. Start Bidding War
    place_bids(15)

    # 4. Final Gavel
    print("\n🔨 [Seller] DROPPING THE GAVEL...")
    with grpc.insecure_channel(SERVICE_ADDR) as channel:
        stub = pb2_grpc.BlindSidedStub(channel)
        # We assume version 16 if all bids passed, or use a high version + skip check
        stub.DropTheGavel(pb2.GavelRequest(auction_id=AUCTION_ID, expected_version=16))

if __name__ == "__main__":
    run_stress_test()
    time.sleep(2) # Give stream time to finish