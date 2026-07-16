import grpc
import sys
import time
from pathlib import Path
from google.protobuf import timestamp_pb2

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from backend.blindsided.generated import blindsided_pb2 as pb2
from backend.blindsided.generated import blindsided_pb2_grpc as pb2_grpc


def future_timestamp():
    return timestamp_pb2.Timestamp(seconds=4102444800)


def run_test():
    channel = grpc.insecure_channel('localhost:50051')
    stub = pb2_grpc.AuctionServiceStub(channel)
    auc_id = f"test_opaque_{int(time.time())}"

    print(f"🚀 Starting Opaque Logic Test on Auction: {auc_id}")

    # --- SCENARIO 1: Initialization with Reserve ---
    print("\n1️⃣  Initializing Auction (Reserve: $500)...")
    opened = stub.CreateAuction(pb2.CreateAuctionRequest(
        seller_id="seller-test",
        title="Test Item",
        reserve_price=500.0,
        ends_at=future_timestamp(),
    ))
    auc_id = opened.auction_id
    print(f"Generated Auction ID: {auc_id}")

    def get_opaque_stats():
        resp = stub.GetAuction(pb2.GetAuctionRequest(auction_id=auc_id))
        return resp.auction

    # --- SCENARIO 2: Single User Bidding ---
    print("\n2️⃣  Xander bids $100...")
    stub.PlaceBid(pb2.BidRequest(auction_id=auc_id, bidder_id="Xander", amount=100.0, expected_version=1))
    
    # --- SCENARIO 3: Overwrite Logic (Pillar #3) ---
    print("3️⃣  Xander updates bid to $600 (Overwriting his $100)...")
    # Fetch version first to avoid stale error
    current_version = stub.GetAuction(pb2.GetAuctionRequest(auction_id=auc_id)).auction.version
    stub.PlaceBid(pb2.BidRequest(auction_id=auc_id, bidder_id="Xander", amount=600.0, expected_version=current_version))
    
    # --- SCENARIO 4: Multiple Users ---
    print("4️⃣  Beatz bids $200...")
    current_version = stub.GetAuction(pb2.GetAuctionRequest(auction_id=auc_id)).auction.version
    stub.PlaceBid(pb2.BidRequest(auction_id=auc_id, bidder_id="Beatz", amount=200.0, expected_version=current_version))

    # --- FINAL VERIFICATION ---
    print("\n--- 🔎 Final Vault Inspection ---")
    for update in stub.WatchAuction(pb2.AuctionRequest(auction_id=auc_id)):
        print(f"  [Opaque View] Bidders: {update.bidder_count}")
        
        if update.bidder_count == 2:
            print("✅ Logic Check Passed: Unique bidders counted without exposing bids.")
        else:
            print("❌ Logic Check Failed: Inspect Judge logs.")
        
        # Now drop the gavel to see the reveal
        print("\n🔨 Dropping the Gavel...")
        stub.RevealAuction(pb2.RevealAuctionRequest(auction_id=auc_id, expected_version=-1))
        break # The next update in the stream will be the reveal

    for reveal in stub.WatchAuction(pb2.AuctionRequest(auction_id=auc_id)):
        if reveal.state == pb2.AUCTION_STATE_REVEALED:
            print(f"  [Reveal] Final bidder count: {reveal.bidder_count}")
            break

if __name__ == "__main__":
    run_test()
