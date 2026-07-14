import grpc
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from backend.blindsided.generated import blindsided_pb2 as pb2
from backend.blindsided.generated import blindsided_pb2_grpc as pb2_grpc

def run_test():
    channel = grpc.insecure_channel('localhost:50051')
    stub = pb2_grpc.AuctionServiceStub(channel)
    auc_id = f"test_opaque_{int(time.time())}"

    print(f"🚀 Starting Opaque Logic Test on Auction: {auc_id}")

    # --- SCENARIO 1: Initialization with Reserve ---
    print("\n1️⃣  Initializing Auction (Reserve: $500)...")
    stub.CreateAuction(pb2.CreateAuctionRequest(auction=pb2.Auction(
        auction_id=auc_id, title="Test Item", reserve_price=500.0
    )))

    def get_opaque_stats():
        # Using GetAuction (which we updated to return public_auction data)
        resp = stub.GetAuction(pb2.GetAuctionRequest(auction_id=auc_id))
        # Note: Depending on your exact GetAuction implementation, 
        # you might need to check the 'reserve_met' boolean in the returned auction.
        return resp.auction

    # --- SCENARIO 2: Single User Bidding & Opaque Range ---
    print("\n2️⃣  Xander bids $100...")
    stub.PlaceBid(pb2.BidRequest(auction_id=auc_id, bidder_id="Xander", amount=100.0, expected_version=1))
    
    # --- SCENARIO 3: Overwrite Logic (Pillar #3) ---
    print("3️⃣  Xander updates bid to $600 (Overwriting his $100)...")
    # Fetch version first to avoid stale error
    current_version = stub.GetAuction(pb2.GetAuctionRequest(auction_id=auc_id)).auction.version
    stub.PlaceBid(pb2.BidRequest(auction_id=auc_id, bidder_id="Xander", amount=600.0, expected_version=current_version))
    
    # --- SCENARIO 4: Multiple Users & Silent Minimum (Pillar #1 & #2) ---
    print("4️⃣  Beatz bids $200...")
    current_version = stub.GetAuction(pb2.GetAuctionRequest(auction_id=auc_id)).auction.version
    stub.PlaceBid(pb2.BidRequest(auction_id=auc_id, bidder_id="Beatz", amount=200.0, expected_version=current_version))

    # --- FINAL VERIFICATION ---
    print("\n--- 🔎 Final Vault Inspection ---")
    # We use the live stream logic to see the "Opaque" view
    for update in stub.WatchAuction(pb2.AuctionRequest(auction_id=auc_id)):
        print(f"  [Opaque View] Bidders: {update.bidder_count}")
        print(f"  [Opaque View] Range: ${update.low_range} - ${update.high_range}")
        print(f"  [Opaque View] Reserve Met: {update.reserve_met}")
        
        if update.bidder_count == 2 and update.reserve_met == True:
            print("✅ Logic Check Passed: Unique bidders counted, High/Low range active, Reserve triggered.")
        else:
            print("❌ Logic Check Failed: Inspect Judge logs.")
        
        # Now drop the gavel to see the reveal
        print("\n🔨 Dropping the Gavel...")
        stub.RevealAuction(pb2.RevealAuctionRequest(auction_id=auc_id, expected_version=-1))
        break # The next update in the stream will be the reveal

    for reveal in stub.WatchAuction(pb2.AuctionRequest(auction_id=auc_id)):
        if reveal.state == pb2.AUCTION_STATE_REVEALED:
            print(f"  [Reveal] Winner: {reveal.winning_bidder_id}")
            print(f"  [Reveal] Final Price: ${reveal.winning_amount}")
            
            if reveal.winning_bidder_id == "Xander" and reveal.winning_amount == 600.0:
                print("✅ Winner Check Passed: Xander's overwrite was successful.")
            break

if __name__ == "__main__":
    run_test()
