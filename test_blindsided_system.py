import grpc
import time
import sys
from proto.src import blindsided_pb2 as pb2
from proto.src import blindsided_pb2_grpc as pb2_grpc

def run_test():
    # Connect to the Service Node (The Fog)
    channel = grpc.insecure_channel('localhost:50051')
    stub = pb2_grpc.BlindSidedStub(channel)
    
    auction_id = f"auction_{int(time.time())}"

    print(f"--- STEP 1: Opening a Blind Auction ({auction_id}) ---")
    new_auction = pb2.Auction(
        auction_id=auction_id,
        seller_id="seller_1",
        title="Ancient GPU",
        description="Rare artifact from the silicon age.",
        reserve_price=100.0
    )
    res = stub.OpenAuction(pb2.OpenRequest(auction=new_auction))
    print(f"Result: {res.message}\n")

    print("--- STEP 2: Placing a Secret High Bid ($500) ---")
    bid_res = stub.PlaceSecretBid(pb2.BidRequest(
        auction_id=auction_id,
        buyer_id="whale_user",
        amount=500.0,
        expected_version=1
    ))
    print(f"Result: {bid_res.message}")
    print(f"Fog Check: Price returned as ${bid_res.revealed_price} (Correct if 0.0)\n")

    print("--- STEP 3: Verifying the Fog of War (Search) ---")
    search_res = stub.SearchAuctions(pb2.SearchRequest(query=auction_id))
    if search_res.auctions:
        a = search_res.auctions[0]
        print(f"Vault Search -> Price: ${a.sealed_bid}, Bidder: {a.lead_bidder_id}")
        print(f"Status: {'REVEALED' if a.is_revealed else 'HIDDEN'}\n")

    print("--- STEP 4: Attempting a Low Bid ($300) ---")
    low_bid = stub.PlaceSecretBid(pb2.BidRequest(
        auction_id=auction_id,
        buyer_id="lowballer",
        amount=300.0,
        expected_version=2
    ))
    print(f"Result: {low_bid.message} (Expected: Blindsided!)\n")

    print("--- STEP 5: Dropping the Gavel (The Reveal) ---")
    gavel_res = stub.DropTheGavel(pb2.GavelRequest(
        auction_id=auction_id,
        seller_id="seller_1",
        expected_version=2
    ))
    print(f"Result: {gavel_res.message}\n")

    print("--- STEP 6: Final Verification (The Truth) ---")
    # Small sleep for replication
    time.sleep(1)
    final_res = stub.GetStatus(pb2.StatusRequest(auction_id=auction_id))
    if final_res.ok:
        a = final_res.auction
        print(f"The Fog has lifted!")
        print(f"Winning Bidder: {a.lead_bidder_id}")
        print(f"Final Price: ${a.sealed_bid}")
        
        if a.lead_bidder_id == "whale_user" and a.sealed_bid == 500.0:
            print("\n✅ SYSTEM VERIFIED: The Judge and Fog are working in harmony.")
        else:
            print("\n❌ VERIFICATION FAILED: Data mismatch in the Vault.")

if __name__ == "__main__":
    try:
        run_test()
    except grpc.RpcError as e:
        print(f"\n🚫 RPC Error: {e.code()} - {e.details()}")
        print("Check if your pods are running and port-forwarded (50051).")